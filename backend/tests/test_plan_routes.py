from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.dependencies import get_chat_manager, get_plan_ledger, get_run_manager
from backend.api.routes.config import _sync_runtime_managers
from backend.api.routes import plans as plans_route
from backend.core.chat.conversation import Conversation
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.plans import PlanLedger, PlanStatus
from backend.core.runs import RunManager


def run(coro):
    return asyncio.run(coro)


def client_for(ledger: PlanLedger) -> TestClient:
    app = FastAPI()
    app.include_router(plans_route.router)
    app.dependency_overrides[get_plan_ledger] = lambda: ledger
    return TestClient(app)


def client_for_current_plan_restore(ledger: PlanLedger, chat_manager) -> TestClient:
    app = FastAPI()
    app.include_router(plans_route.router)
    app.state.chat_manager = chat_manager
    app.dependency_overrides[get_plan_ledger] = lambda: ledger
    return TestClient(app)


class FakePlanChatManager:
    def __init__(self):
        self.calls = []

    async def continue_plan_tool_result_stream(self, **kwargs):
        self.calls.append(kwargs)
        run_id = kwargs.get("run_id")
        conversation_id = kwargs.get("conversation_id")
        yield {
            "status": "start",
            "content": None,
            "node_id": "node-generated",
            "target_node_id": "node-generated",
            "conversation_id": conversation_id,
            "run_id": run_id,
            "tokens_used": 0,
        }
        yield {
            "status": "complete",
            "content": None,
            "node_id": "node-generated",
            "target_node_id": "node-generated",
            "conversation_id": conversation_id,
            "run_id": run_id,
            "tokens_used": 0,
        }


class RestoringPlanChatManager(FakePlanChatManager):
    def __init__(self, conversation: Conversation, ledger: PlanLedger):
        super().__init__()
        self.conversation = conversation
        self.plan_ledger = ledger

    async def restore_plan_snapshot(self, conversation_id: str) -> None:
        if conversation_id != self.conversation.metadata["id"]:
            return
        snapshot = self.conversation.metadata.get("plan_ledger")
        if snapshot:
            await self.plan_ledger.load_snapshot(conversation_id, snapshot)


class SnapshotChatManager:
    def __init__(self, conversation: Conversation, ledger: PlanLedger):
        self.conversation = conversation
        self.plan_ledger = ledger

    def get_conversation(self, conversation_id: str):
        if conversation_id == self.conversation.metadata["id"]:
            return self.conversation
        return None

    async def restore_plan_snapshot(self, conversation_id: str) -> None:
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return
        snapshot = conversation.metadata.get("plan_ledger")
        if snapshot:
            await self.plan_ledger.load_snapshot(conversation_id, snapshot)


def client_for_plan_stream(
    chat_manager: FakePlanChatManager,
    run_manager: RunManager,
    ledger: PlanLedger,
) -> TestClient:
    app = FastAPI()
    app.include_router(plans_route.router)
    app.state.chat_manager = chat_manager
    app.dependency_overrides[get_chat_manager] = lambda: chat_manager
    app.dependency_overrides[get_run_manager] = lambda: run_manager
    app.dependency_overrides[get_plan_ledger] = lambda: ledger
    return TestClient(app)


class FakeToolManager:
    def __init__(self) -> None:
        self.tools = {}
        self.command_executor = None

    def register(self, tool) -> None:
        self.tools[tool.name] = tool


def test_plan_route_returns_null_when_no_active_or_awaiting_plan():
    client = client_for(PlanLedger())

    response = client.get("/conversations/conv-1/plans/current")

    assert response.status_code == 200
    assert response.json() == {"plan": None}


def test_plan_routes_get_approve_and_expose_pending_context():
    ledger = PlanLedger()
    active = run(
        ledger.enter_plan_mode(
            conversation_id="conv-1",
            node_id="node-1",
            previous_permission_mode="ask_always",
        )
    )
    awaiting = run(ledger.submit_plan(conversation_id="conv-1", plan="Backend-only plan"))
    client = client_for(ledger)

    current = client.get("/conversations/conv-1/plans/current")
    approved = client.post(f"/conversations/conv-1/plans/{awaiting.plan_id}/approve")
    pending_context = client.post("/conversations/conv-1/plans/context/consume")

    assert current.status_code == 200
    assert current.json()["plan"]["plan_id"] == active.plan_id
    assert current.json()["plan"]["status"] == PlanStatus.AWAITING_APPROVAL.value
    assert approved.status_code == 200
    assert approved.json()["status"] == PlanStatus.APPROVED.value
    assert approved.json()["next_permission_mode"] == "ask_always"
    assert pending_context.status_code == 200
    assert pending_context.json()["context"][0]["kind"] == "approved_plan"
    assert pending_context.json()["context"][0]["permission_mode"] == "ask_always"


def test_plan_route_current_hides_recent_approved_plan_content():
    ledger = PlanLedger()
    run(
        ledger.enter_plan_mode(
            conversation_id="conv-1",
            node_id="node-1",
            previous_permission_mode="modify_only",
        )
    )
    awaiting = run(ledger.submit_plan(conversation_id="conv-1", plan="Approved visible plan"))
    run(ledger.approve_plan(conversation_id="conv-1", plan_id=awaiting.plan_id))
    client = client_for(ledger)

    current = client.get("/conversations/conv-1/plans/current")

    assert current.status_code == 200
    assert current.json()["plan"]["plan_id"] == awaiting.plan_id
    assert current.json()["plan"]["status"] == PlanStatus.APPROVED.value
    assert current.json()["plan"]["plan"] == ""


def test_plan_route_current_restores_persisted_snapshot_after_restart():
    original_ledger = PlanLedger()
    conversation = Conversation(title="persisted plan")
    conversation.initialize_with_system_message(None)
    run(
        original_ledger.enter_plan_mode(
            conversation_id=conversation.metadata["id"],
            node_id="node-1",
            previous_permission_mode="modify_only",
        )
    )
    awaiting = run(
        original_ledger.submit_plan(
            conversation_id=conversation.metadata["id"],
            plan="Persisted plan awaiting approval",
        )
    )
    conversation.metadata["plan_ledger"] = run(original_ledger.snapshot(conversation.metadata["id"]))
    restored_ledger = PlanLedger()
    client = client_for_current_plan_restore(
        restored_ledger,
        SnapshotChatManager(conversation, restored_ledger),
    )

    current = client.get(f"/conversations/{conversation.metadata['id']}/plans/current")

    assert current.status_code == 200
    assert current.json()["plan"]["plan_id"] == awaiting.plan_id
    assert current.json()["plan"]["status"] == PlanStatus.AWAITING_APPROVAL.value
    assert current.json()["plan"]["plan"] == "Persisted plan awaiting approval"


def test_plan_approve_route_restores_persisted_snapshot_before_decision():
    original_ledger = PlanLedger()
    conversation = Conversation(title="approve persisted plan")
    conversation.initialize_with_system_message(None)
    run(
        original_ledger.enter_plan_mode(
            conversation_id=conversation.metadata["id"],
            node_id="node-1",
            previous_permission_mode="modify_only",
        )
    )
    awaiting = run(
        original_ledger.submit_plan(
            conversation_id=conversation.metadata["id"],
            plan="Approve after backend restart",
        )
    )
    conversation.metadata["plan_ledger"] = run(original_ledger.snapshot(conversation.metadata["id"]))
    restored_ledger = PlanLedger()
    client = client_for_current_plan_restore(
        restored_ledger,
        SnapshotChatManager(conversation, restored_ledger),
    )

    approved = client.post(
        f"/conversations/{conversation.metadata['id']}/plans/{awaiting.plan_id}/approve",
    )

    assert approved.status_code == 200
    assert approved.json()["status"] == PlanStatus.APPROVED.value
    assert approved.json()["next_permission_mode"] == "modify_only"


def test_plan_route_reject_keeps_plan_active_with_feedback_context():
    ledger = PlanLedger()
    run(
        ledger.enter_plan_mode(
            conversation_id="conv-1",
            node_id="node-1",
            previous_permission_mode="auto_approve",
        )
    )
    awaiting = run(ledger.submit_plan(conversation_id="conv-1", plan="Too broad"))
    client = client_for(ledger)

    rejected = client.post(
        f"/conversations/conv-1/plans/{awaiting.plan_id}/reject",
        json={"feedback": "Keep it backend-only."},
    )
    current = client.get("/conversations/conv-1/plans/current")

    assert rejected.status_code == 200
    assert rejected.json()["status"] == PlanStatus.ACTIVE.value
    assert rejected.json()["next_permission_mode"] == "plan"
    assert current.json()["plan"]["status"] == PlanStatus.ACTIVE.value
    assert current.json()["plan"]["feedback"][-1]["feedback"] == "Keep it backend-only."


def test_plan_route_answer_question_keeps_plan_active_with_context():
    ledger = PlanLedger()
    run(
        ledger.enter_plan_mode(
            conversation_id="conv-1",
            node_id="node-1",
            previous_permission_mode="modify_only",
        )
    )
    question = run(
        ledger.ask_user_question(
            conversation_id="conv-1",
            question="项目栏默认显示吗？",
            options=[{"label": "默认显示", "description": "进入页面直接看到"}],
        )
    )
    client = client_for(ledger)

    answered = client.post(
        f"/conversations/conv-1/plans/{question.plan_id}/answer",
        json={"answer": "默认显示"},
    )
    current = client.get("/conversations/conv-1/plans/current")
    pending_context = client.post("/conversations/conv-1/plans/context/consume")

    assert answered.status_code == 200
    assert answered.json()["status"] == PlanStatus.ACTIVE.value
    assert answered.json()["next_permission_mode"] == "plan"
    assert current.json()["plan"]["status"] == PlanStatus.ACTIVE.value
    assert pending_context.json()["context"][0]["kind"] == "plan_question_answer"
    assert "默认显示" in pending_context.json()["context"][0]["content"]


def test_sync_runtime_managers_creates_sqlite_backed_plan_ledger(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    conversation_id = chat.create_conversation(title="sqlite plan runtime")
    node_id = chat.create_node(conversation_id, parent_id=None, node_id="node-1")
    app = type("App", (), {})()
    app.state = type("State", (), {})()
    app.state.persistence = persistence

    _sync_runtime_managers(app, {}, object(), FakeToolManager())

    ledger = app.state.plan_ledger
    run(ledger.enter_plan_mode(conversation_id=conversation_id, node_id=node_id))
    awaiting = run(
        ledger.submit_plan(
            conversation_id=conversation_id,
            plan="Persist this proposal",
            node_id=node_id,
            run_id="run-1",
            tool_call_id="call-1",
        )
    )
    reloaded = PlanLedger(repository=app.state.plan_repository)

    current = run(reloaded.get_active_or_awaiting(conversation_id))

    assert current is not None
    assert current.plan_id == awaiting.plan_id
    assert current.plan == "Persist this proposal"
    assert current.proposals[0].tool_call_id == "call-1"


def test_plan_approve_stream_uses_tool_result_continuation():
    ledger = PlanLedger()
    run(ledger.enter_plan_mode(
        conversation_id="conv-1",
        node_id="node-current",
        previous_permission_mode="modify_only",
    ))
    awaiting = run(ledger.submit_plan(
        conversation_id="conv-1",
        plan="## Plan\nDo it.",
        node_id="node-current",
        run_id="run-plan",
        tool_call_id="call-exit-1",
    ))
    chat_manager = FakePlanChatManager()
    client = client_for_plan_stream(chat_manager, RunManager(), ledger)

    response = client.post(
        f"/conversations/conv-1/plans/{awaiting.plan_id}/approve/stream",
        json={"node_id": "node-current", "reasoning_effort": "medium"},
    )

    assert response.status_code == 200
    assert "node-generated" in response.text
    assert chat_manager.calls[0]["conversation_id"] == "conv-1"
    assert chat_manager.calls[0]["plan_id"] == awaiting.plan_id
    assert chat_manager.calls[0]["node_id"] == "node-current"
    assert chat_manager.calls[0]["continuation_of_run_id"] == "run-plan"
    assert chat_manager.calls[0]["continuation_marker"] == "计划已批准，开始实现"
    assert chat_manager.calls[0]["tool_name"] == "exit_plan_mode"
    assert chat_manager.calls[0]["tool_call_id"] == "call-exit-1"
    assert "User has approved your plan" in chat_manager.calls[0]["tool_result_content"]


def test_plan_approve_stream_without_exit_tool_call_id_does_not_mutate_or_context():
    ledger = PlanLedger()
    run(ledger.enter_plan_mode(
        conversation_id="conv-1",
        node_id="node-current",
        previous_permission_mode="modify_only",
    ))
    awaiting = run(ledger.submit_plan(
        conversation_id="conv-1",
        plan="## Plan\nDo it.",
        node_id="node-current",
        run_id="run-plan",
    ))
    chat_manager = FakePlanChatManager()
    client = client_for_plan_stream(chat_manager, RunManager(), ledger)

    response = client.post(
        f"/conversations/conv-1/plans/{awaiting.plan_id}/approve/stream",
        json={"node_id": "node-current"},
    )

    current = run(ledger.get_plan("conv-1", awaiting.plan_id))
    pending_context = run(ledger.consume_pending_context("conv-1"))
    assert response.status_code == 409
    assert current.status == PlanStatus.AWAITING_APPROVAL
    assert current.proposal_status == "awaiting_approval"
    assert pending_context == []
    assert chat_manager.calls == []


def test_plan_approve_stream_restores_snapshot_before_decision():
    original_ledger = PlanLedger()
    conversation = Conversation(title="approve stream persisted plan")
    conversation.initialize_with_system_message(None)
    run(original_ledger.enter_plan_mode(
        conversation_id=conversation.metadata["id"],
        node_id="node-current",
        previous_permission_mode="modify_only",
    ))
    awaiting = run(original_ledger.submit_plan(
        conversation_id=conversation.metadata["id"],
        plan="## Plan\nApprove after restart.",
        node_id="node-current",
        run_id="run-plan",
        tool_call_id="call-exit-1",
    ))
    conversation.metadata["plan_ledger"] = run(original_ledger.snapshot(conversation.metadata["id"]))
    restored_ledger = PlanLedger()
    chat_manager = RestoringPlanChatManager(conversation, restored_ledger)
    client = client_for_plan_stream(chat_manager, RunManager(), restored_ledger)

    response = client.post(
        f"/conversations/{conversation.metadata['id']}/plans/{awaiting.plan_id}/approve/stream",
        json={"node_id": "node-current"},
    )

    assert response.status_code == 200
    assert "node-generated" in response.text
    assert chat_manager.calls[0]["plan_id"] == awaiting.plan_id


def test_plan_answer_stream_uses_tool_result_continuation():
    ledger = PlanLedger()
    run(ledger.enter_plan_mode(
        conversation_id="conv-1",
        node_id="node-current",
        previous_permission_mode="modify_only",
    ))
    question = run(ledger.ask_user_question(
        conversation_id="conv-1",
        question="项目栏默认显示吗？",
        node_id="node-current",
        run_id="run-plan",
        tool_call_id="call-question-1",
    ))
    chat_manager = FakePlanChatManager()
    client = client_for_plan_stream(chat_manager, RunManager(), ledger)

    response = client.post(
        f"/conversations/conv-1/plans/{question.plan_id}/answer/stream",
        json={"node_id": "node-current", "answer": "默认显示"},
    )

    assert response.status_code == 200
    assert "node-generated" in response.text
    assert chat_manager.calls[0]["tool_name"] == "ask_user_question"
    assert chat_manager.calls[0]["tool_call_id"] == "call-question-1"
    assert "默认显示" in chat_manager.calls[0]["tool_result_content"]


def test_plan_answer_stream_without_question_tool_call_id_does_not_mutate_or_context():
    ledger = PlanLedger()
    run(ledger.enter_plan_mode(
        conversation_id="conv-1",
        node_id="node-current",
        previous_permission_mode="modify_only",
    ))
    question = run(ledger.ask_user_question(
        conversation_id="conv-1",
        question="项目栏默认显示吗？",
        node_id="node-current",
        run_id="run-plan",
    ))
    chat_manager = FakePlanChatManager()
    client = client_for_plan_stream(chat_manager, RunManager(), ledger)

    response = client.post(
        f"/conversations/conv-1/plans/{question.plan_id}/answer/stream",
        json={"node_id": "node-current", "answer": "默认显示"},
    )

    current = run(ledger.get_plan("conv-1", question.plan_id))
    pending_context = run(ledger.consume_pending_context("conv-1"))
    assert response.status_code == 409
    assert current.status == PlanStatus.AWAITING_QUESTION
    assert current.question.get("answer") is None
    assert pending_context == []
    assert chat_manager.calls == []


def test_plan_answer_stream_restores_snapshot_before_decision():
    original_ledger = PlanLedger()
    conversation = Conversation(title="answer stream persisted question")
    conversation.initialize_with_system_message(None)
    run(original_ledger.enter_plan_mode(
        conversation_id=conversation.metadata["id"],
        node_id="node-current",
        previous_permission_mode="modify_only",
    ))
    question = run(original_ledger.ask_user_question(
        conversation_id=conversation.metadata["id"],
        question="项目栏默认显示吗？",
        node_id="node-current",
        run_id="run-plan",
        tool_call_id="call-question-1",
    ))
    conversation.metadata["plan_ledger"] = run(original_ledger.snapshot(conversation.metadata["id"]))
    restored_ledger = PlanLedger()
    chat_manager = RestoringPlanChatManager(conversation, restored_ledger)
    client = client_for_plan_stream(chat_manager, RunManager(), restored_ledger)

    response = client.post(
        f"/conversations/{conversation.metadata['id']}/plans/{question.plan_id}/answer/stream",
        json={"node_id": "node-current", "answer": "默认显示"},
    )

    assert response.status_code == 200
    assert "node-generated" in response.text
    assert chat_manager.calls[0]["tool_name"] == "ask_user_question"
    assert "默认显示" in chat_manager.calls[0]["tool_result_content"]


def test_reject_plan_stream_without_feedback_uses_tool_result_continuation():
    ledger = PlanLedger()
    run(ledger.enter_plan_mode(
        conversation_id="conv-1",
        node_id="node-current",
        previous_permission_mode="modify_only",
    ))
    awaiting = run(ledger.submit_plan(
        conversation_id="conv-1",
        plan="## Plan\nDo it.",
        node_id="node-current",
        run_id="run-plan",
        tool_call_id="call-exit-1",
    ))
    chat_manager = FakePlanChatManager()
    client = client_for_plan_stream(chat_manager, RunManager(), ledger)
    response = client.post(
        f"/conversations/conv-1/plans/{awaiting.plan_id}/reject/stream",
        json={"feedback": "", "model_id": "fake-model"},
    )
    assert response.status_code == 200
    assert chat_manager.calls[-1]["tool_name"] == "exit_plan_mode"
    assert chat_manager.calls[-1]["tool_call_id"] == "call-exit-1"
    assert "did not provide specific feedback" in chat_manager.calls[-1]["tool_result_content"]
    assert "message_subtype" not in chat_manager.calls[-1]


def test_plan_reject_stream_without_exit_tool_call_id_does_not_mutate_or_context():
    ledger = PlanLedger()
    run(ledger.enter_plan_mode(
        conversation_id="conv-1",
        node_id="node-current",
        previous_permission_mode="modify_only",
    ))
    awaiting = run(ledger.submit_plan(
        conversation_id="conv-1",
        plan="## Plan\nDo it.",
        node_id="node-current",
        run_id="run-plan",
    ))
    chat_manager = FakePlanChatManager()
    client = client_for_plan_stream(chat_manager, RunManager(), ledger)

    response = client.post(
        f"/conversations/conv-1/plans/{awaiting.plan_id}/reject/stream",
        json={"feedback": "需要缩小范围"},
    )

    current = run(ledger.get_plan("conv-1", awaiting.plan_id))
    pending_context = run(ledger.consume_pending_context("conv-1"))
    assert response.status_code == 409
    assert current.status == PlanStatus.AWAITING_APPROVAL
    assert current.proposal_status == "awaiting_approval"
    assert current.feedback == []
    assert pending_context == []
    assert chat_manager.calls == []


def test_plan_reject_stream_restores_snapshot_before_decision():
    original_ledger = PlanLedger()
    conversation = Conversation(title="reject stream persisted plan")
    conversation.initialize_with_system_message(None)
    run(original_ledger.enter_plan_mode(
        conversation_id=conversation.metadata["id"],
        node_id="node-current",
        previous_permission_mode="modify_only",
    ))
    awaiting = run(original_ledger.submit_plan(
        conversation_id=conversation.metadata["id"],
        plan="## Plan\nReject after restart.",
        node_id="node-current",
        run_id="run-plan",
        tool_call_id="call-exit-1",
    ))
    conversation.metadata["plan_ledger"] = run(original_ledger.snapshot(conversation.metadata["id"]))
    restored_ledger = PlanLedger()
    chat_manager = RestoringPlanChatManager(conversation, restored_ledger)
    client = client_for_plan_stream(chat_manager, RunManager(), restored_ledger)

    response = client.post(
        f"/conversations/{conversation.metadata['id']}/plans/{awaiting.plan_id}/reject/stream",
        json={"feedback": "需要缩小范围"},
    )

    assert response.status_code == 200
    assert "node-generated" in response.text
    assert chat_manager.calls[0]["tool_call_id"] == "call-exit-1"
    assert "需要缩小范围" in chat_manager.calls[0]["tool_result_content"]


def test_plan_route_returns_404_for_missing_plan_decision():
    client = client_for(PlanLedger())

    response = client.post("/conversations/conv-1/plans/plan_missing/approve")

    assert response.status_code == 404
