import asyncio
import json
from pathlib import Path

import sys

sys.path.insert(0, ".")
sys.path.insert(0, "test")

from backend.core.chat.chat_manager import ChatManager
from backend.core.config.types import StreamChunk, StreamController, StreamStatus
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.transcript import TranscriptProjection
from backend.core.plans import PlanLedger
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.storage.tool_result_storage import ToolResultStorage
from backend.core.tools.orchestrator import ToolOrchestrator
from backend.core.tools.security.approval import ApprovalManager
from backend.core.tools.security.logical_sandbox import LogicalSandbox
from backend.core.tools.security.permissions import PermissionEngine
from backend.api.routes.conversations import to_transcript_item_dto
from test_chat_manager_prompt_slash import (
    CapturingProvider,
    CapturingModelManager,
    PlanFinalWithoutExitProvider,
    PlanModeToolManager,
    collect_chunks,
)


class TextThenExitPlanProvider:
    def __init__(self):
        self.calls = []

    async def generate_response_stream(self, model, messages, stream_controller=None, **kwargs):
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        node_id = stream_controller.node_id
        conversation_id = stream_controller.conversation_id
        yield StreamChunk(
            status=StreamStatus.CONTENT,
            content="这是正文里不应该成为最终回复的计划摘要。\n",
            node_id=node_id,
            conversation_id=conversation_id,
            tokens_used=1,
        )
        yield StreamChunk(
            status=StreamStatus.CONTENT,
            content="它只能进入已处理折叠区。\n",
            node_id=node_id,
            conversation_id=conversation_id,
            tokens_used=1,
        )
        yield StreamChunk(
            status=StreamStatus.CONTENT,
            content="",
            node_id=node_id,
            conversation_id=conversation_id,
            tokens_used=1,
            tool_calls=[{
                "id": "call_exit_plan_test",
                "type": "function",
                "function": {
                    "name": "exit_plan_mode",
                    "arguments": json.dumps({"plan": "## Canonical Plan\n\n1. Only this appears in the plan card."}),
                },
            }],
        )
        yield StreamChunk(
            status=StreamStatus.COMPLETE,
            content="",
            node_id=node_id,
            conversation_id=conversation_id,
            tokens_used=3,
        )


class ToolThenFinalAnswerProvider:
    def __init__(self):
        self.calls = []

    async def generate_response_stream(self, model, messages, stream_controller=None, **kwargs):
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        node_id = stream_controller.node_id
        conversation_id = stream_controller.conversation_id
        if len(self.calls) == 1:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="我先检查一下环境。\n",
                node_id=node_id,
                conversation_id=conversation_id,
                tokens_used=1,
            )
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="",
                node_id=node_id,
                conversation_id=conversation_id,
                tokens_used=1,
                tool_calls=[{
                    "id": "call_large_tool",
                    "type": "function",
                    "function": {"name": "large_tool", "arguments": "{\"value\":\"small\"}"},
                }],
            )
        else:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="最终总结只应该是这一段。",
                node_id=node_id,
                conversation_id=conversation_id,
                tokens_used=1,
            )
        yield StreamChunk(
            status=StreamStatus.COMPLETE,
            content="",
            node_id=node_id,
            conversation_id=conversation_id,
            tokens_used=3,
        )


def _make_manager(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path / "sqlite")
    persistence.initialize()
    repository = ChatRepository(persistence)
    projection = TranscriptProjection(persistence)
    model_manager = CapturingModelManager()
    try:
        manager = ChatManager(
            model_manager,
            ChatStorage(str(tmp_path / "conversations")),
            PromptStorage(str(tmp_path / "prompts")),
            chat_repository=repository,
            transcript_projection=projection,
        )
    except TypeError:
        manager = ChatManager(
            model_manager,
            ChatStorage(str(tmp_path / "conversations")),
            PromptStorage(str(tmp_path / "prompts")),
        )
        manager.chat_repository = repository
        manager.transcript_projection = projection
    return manager, repository, projection


def test_create_empty_conversation_writes_sqlite_conversation(tmp_path: Path):
    manager, repository, projection = _make_manager(tmp_path)

    conversation = manager.create_conversation("empty sqlite")

    stored = repository.get_conversation(conversation.metadata["id"])
    assert stored["title"] == "empty sqlite"
    assert projection.list_for_branch(conversation.metadata["id"], None) == []


def test_delete_conversation_removes_sqlite_transcript_projection(tmp_path: Path):
    manager, _repository, projection = _make_manager(tmp_path)
    conversation = manager.create_conversation("delete sqlite")
    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "to delete",
                model_id="fake-model",
            )
        )
    )
    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    assert projection.list_for_branch(conversation.metadata["id"], reloaded.current_node_id)

    manager.delete_conversation(conversation.metadata["id"])

    try:
        projection.list_for_branch(conversation.metadata["id"], reloaded.current_node_id)
    except KeyError:
        pass
    else:
        raise AssertionError("deleted conversation transcript should not remain")


def test_delete_node_removes_sqlite_branch_items(tmp_path: Path):
    manager, _repository, projection = _make_manager(tmp_path)
    conversation = manager.create_conversation("delete node")
    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "delete node content",
                model_id="fake-model",
            )
        )
    )
    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    node_id = reloaded.current_node_id
    assert projection.list_for_branch(conversation.metadata["id"], node_id)

    result = asyncio.run(manager.delete_node(conversation.metadata["id"], node_id))

    assert result["deleted_node_id"] == node_id
    try:
        projection.list_for_branch(conversation.metadata["id"], node_id)
    except KeyError:
        pass
    else:
        raise AssertionError("deleted node transcript should not remain")


def test_send_message_stream_writes_transcript_projection(tmp_path: Path):
    manager, _repository, projection = _make_manager(tmp_path)
    conversation = manager.create_conversation("sqlite transcript")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "hello sqlite",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    try:
        items = projection.list_for_branch(
            conversation.metadata["id"],
            reloaded.current_node_id,
        )
    except KeyError:
        items = []

    assert [item["item_type"] for item in items] == [
        "user_message",
        "assistant_answer",
    ]
    assert [item["preview"] for item in items] == ["hello sqlite", "ok"]


def test_send_message_stream_updates_run_draft_item(tmp_path: Path):
    manager, _repository, projection = _make_manager(tmp_path)
    conversation = manager.create_conversation("sqlite run draft")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "hello run",
                model_id="fake-model",
                run_id="run-chat-1",
            )
        )
    )

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    items = projection.list_for_branch(
        conversation.metadata["id"],
        reloaded.current_node_id,
    )

    run_items = [item for item in items if item["item_type"] == "run_draft"]
    assert len(run_items) == 1
    assert run_items[0]["run_id"] == "run-chat-1"
    assert run_items[0]["status"] == "completed"
    assert run_items[0]["preview"] == "ok"


def test_plan_control_turn_writes_process_timeline_without_answer(tmp_path: Path):
    manager, _repository, projection = _make_manager(tmp_path)
    plan_ledger = PlanLedger()
    tool_manager = PlanModeToolManager(plan_ledger)
    manager.plan_ledger = plan_ledger
    manager.tool_manager = tool_manager
    manager.tool_orchestrator = ToolOrchestrator(
        tool_manager=tool_manager,
        permission_engine=PermissionEngine.default(),
        approval_manager=ApprovalManager(),
        logical_sandbox=LogicalSandbox.for_config({}, tmp_path),
    )
    manager.model_manager.provider = PlanFinalWithoutExitProvider()
    conversation = manager.create_conversation("sqlite plan")
    asyncio.run(
        plan_ledger.enter_plan_mode(
            conversation_id=conversation.metadata["id"],
            previous_permission_mode="modify_only",
        )
    )

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "写计划",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    items = projection.list_for_branch(
        conversation.metadata["id"],
        reloaded.current_node_id,
    )
    item_types = [item["item_type"] for item in items]
    assert "user_message" in item_types
    assert "assistant_process" in item_types
    assert "plan_card" not in item_types
    assert "assistant_answer" not in item_types
    process_item = next(item for item in items if item["item_type"] == "assistant_process")
    dto = to_transcript_item_dto(process_item)
    assert dto["props"]["tool_interactions"]
    assert "reasoning" in dto["props"]
    proposal = next(block for block in dto["props"]["timeline"] if block["type"] == "plan_proposal")
    assert proposal["status"] == "awaiting_approval"
    assert "修改设置页" in proposal["plan"]


def test_exit_plan_mode_treats_same_round_text_as_process_not_answer(tmp_path: Path):
    manager, _repository, projection = _make_manager(tmp_path)
    plan_ledger = PlanLedger()
    tool_manager = PlanModeToolManager(plan_ledger)
    manager.plan_ledger = plan_ledger
    manager.tool_manager = tool_manager
    manager.tool_orchestrator = ToolOrchestrator(
        tool_manager=tool_manager,
        permission_engine=PermissionEngine.default(),
        approval_manager=ApprovalManager(),
        logical_sandbox=LogicalSandbox.for_config({}, tmp_path),
    )
    manager.model_manager.provider = TextThenExitPlanProvider()
    conversation = manager.create_conversation("blocking exit plan")
    asyncio.run(plan_ledger.enter_plan_mode(
        conversation_id=conversation.metadata["id"],
        previous_permission_mode="modify_only",
    ))

    chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "写计划",
        model_id="fake-model",
        run_id="run-plan-blocking",
    )))

    main_text = "".join(
        str(chunk.get("content") or "")
        for chunk in chunks
        if chunk.get("status") == "content" and not chunk.get("event_type")
    )
    assert "不应该成为最终回复" not in main_text
    assert chunks[-1]["status"] == "complete"

    reloaded = manager.get_conversation(conversation.metadata["id"])
    items = projection.list_for_branch(conversation.metadata["id"], reloaded.current_node_id)
    assert "assistant_answer" not in [item["item_type"] for item in items]

    process_item = next(item for item in items if item["item_type"] == "assistant_process")
    dto = to_transcript_item_dto(process_item)
    interactions = dto["props"]["tool_interactions"]
    assert "不应该成为最终回复" in interactions[-1]["assistant"]["content"]

    assert "plan_card" not in [item["item_type"] for item in items]
    timeline = dto["props"]["timeline"]
    proposal = next(block for block in timeline if block["type"] == "plan_proposal")
    assert proposal["tool_name"] == "exit_plan_mode"
    assert proposal["status"] == "awaiting_approval"
    assert proposal["plan"].startswith("## Canonical Plan")
    assert "不应该成为最终回复" not in proposal["plan"]


def test_tool_turn_uses_last_non_tool_text_as_assistant_answer(tmp_path: Path):
    manager, _repository, projection = _make_manager(tmp_path)
    manager.tool_manager = LargeToolManager(tmp_path)
    manager.model_manager.provider = ToolThenFinalAnswerProvider()
    conversation = manager.create_conversation("tool then final")

    chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "检查后总结",
        model_id="fake-model",
    )))

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    items = projection.list_for_branch(conversation.metadata["id"], reloaded.current_node_id)
    answer = next(item for item in items if item["item_type"] == "assistant_answer")
    assert answer["preview"] == "最终总结只应该是这一段。"

    process = next(item for item in items if item["item_type"] == "assistant_process")
    dto = to_transcript_item_dto(process)
    timeline = dto["props"]["timeline"]
    assert timeline[0]["type"] == "content"
    assert timeline[0]["content"] == "我先检查一下环境。\n"
    assert timeline[1]["type"] == "tool_call"


def test_plan_approval_stream_uses_tool_result_continuation(tmp_path: Path):
    manager, repository, projection = _make_manager(tmp_path)
    plan_ledger = PlanLedger()
    tool_manager = PlanModeToolManager(plan_ledger)
    manager.plan_ledger = plan_ledger
    manager.tool_manager = tool_manager
    manager.tool_orchestrator = ToolOrchestrator(
        tool_manager=tool_manager,
        permission_engine=PermissionEngine.default(),
        approval_manager=ApprovalManager(),
        logical_sandbox=LogicalSandbox.for_config({}, tmp_path),
    )
    manager.model_manager.provider = PlanFinalWithoutExitProvider()
    conversation = manager.create_conversation("sqlite plan approval")
    asyncio.run(
        plan_ledger.enter_plan_mode(
            conversation_id=conversation.metadata["id"],
            previous_permission_mode="modify_only",
        )
    )
    plan_chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "写计划",
                model_id="fake-model",
            )
        )
    )
    assert plan_chunks[-1]["status"] == "complete"
    plan_node_id = manager.get_conversation(conversation.metadata["id"]).current_node_id
    manager.model_manager.provider = CapturingProvider()
    awaiting_plan = asyncio.run(plan_ledger.get_active_or_awaiting(conversation.metadata["id"]))
    approved_plan = asyncio.run(plan_ledger.approve_plan(
        conversation_id=conversation.metadata["id"],
        plan_id=awaiting_plan.plan_id,
    ))
    manager.update_plan_proposal_projection(conversation.metadata["id"], approved_plan)

    approval_chunks = asyncio.run(
        collect_chunks(
            manager.continue_plan_tool_result_stream(
                conversation_id=conversation.metadata["id"],
                plan_id=approved_plan.plan_id,
                tool_result_content=plan_ledger.approved_tool_result_content(approved_plan),
                tool_call_id=approved_plan.exit_tool_call_id,
                tool_name="exit_plan_mode",
                model_id="fake-model",
                node_id=plan_node_id,
                tool_permission_mode="modify_only",
                run_id="run-impl-1",
            )
        )
    )

    assert approval_chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    items = projection.list_for_branch(
        conversation.metadata["id"],
        reloaded.current_node_id,
    )
    item_types = [item["item_type"] for item in items]
    process_index = item_types.index("assistant_process")
    run_index = next(
        index
        for index, item in enumerate(items)
        if item["item_type"] == "run_draft" and item["run_id"] == "run-impl-1"
    )
    assistant_index = next(
        index
        for index, item in enumerate(items)
        if item["item_type"] == "assistant_answer" and item["preview"] == "ok"
    )
    assert "control_event" not in item_types
    assert "plan_card" not in item_types
    assert process_index < run_index < assistant_index
    assert reloaded.nodes[reloaded.current_node_id]["tool_permission_mode"] == "modify_only"
    with repository.persistence.connect() as conn:
        hidden_user_messages = conn.execute(
            """
            SELECT id, content_inline, subtype
            FROM messages
            WHERE conversation_id = ?
              AND role = 'user'
              AND hidden = 1
            """,
            (conversation.metadata["id"],),
        ).fetchall()
    assert hidden_user_messages == []
    first_prompt = "\n\n".join(
        str(message.get("content") or "")
        for message in manager.model_manager.provider.calls[0]["messages"]
    )
    assert "User has approved your plan" in first_prompt
    assert "## Approved Plan:" in first_prompt
    process_item = next(item for item in items if item["item_type"] == "assistant_process")
    timeline = to_transcript_item_dto(process_item)["props"]["timeline"]
    proposal = next(block for block in timeline if block["type"] == "plan_proposal")
    assert proposal["status"] == "approved"
    assert proposal["plan"].startswith("1. 修改设置页")


class ToolCallingProvider:
    def __init__(self):
        self.calls = []

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        if len(self.calls) == 1:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
                tool_calls=[
                    {
                        "id": "call_large_tool",
                        "type": "function",
                        "function": {
                            "name": "large_tool",
                            "arguments": "{\"value\":\"large\"}",
                        },
                    }
                ],
            )
        else:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="done",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
            )
        yield StreamChunk(
            status=StreamStatus.COMPLETE,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=1,
            usage_info={
                "input_tokens": 1,
                "output_tokens": 0,
                "total_tokens": 1,
                "source": "test",
                "raw": {},
            },
        )


class LargeToolManager:
    def __init__(self, tmp_path: Path):
        self.tool_result_store = ToolResultStorage(str(tmp_path / "tool-results"))

    def get_openai_tools(self, include_disabled=False):
        return [
            {
                "type": "function",
                "function": {
                    "name": "large_tool",
                    "description": "Return a large result",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        return "x" * 5000


class TwoToolCallingProvider:
    def __init__(self):
        self.calls = []

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        if len(self.calls) == 1:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
                tool_calls=[
                    {
                        "id": "call_first",
                        "type": "function",
                        "function": {
                            "name": "first_tool",
                            "arguments": "{\"a\":1}",
                        },
                    },
                    {
                        "id": "call_second",
                        "type": "function",
                        "function": {
                            "name": "second_tool",
                            "arguments": "{\"b\":2}",
                        },
                    },
                ],
            )
        else:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="done",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
            )
        yield StreamChunk(
            status=StreamStatus.COMPLETE,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=1,
            usage_info={
                "input_tokens": 1,
                "output_tokens": 0,
                "total_tokens": 1,
                "source": "test",
                "raw": {},
            },
        )


class TwoToolManager:
    def __init__(self, tmp_path: Path):
        self.tool_result_store = ToolResultStorage(str(tmp_path / "tool-results"))

    def get_openai_tools(self, include_disabled=False):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Run {name}",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in ("first_tool", "second_tool")
        ]

    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        return json.dumps({"tool": name, "arguments": arguments}, sort_keys=True)


def test_tool_results_are_copied_to_sqlite_blobs(tmp_path: Path):
    manager, repository, projection = _make_manager(tmp_path)
    tool_manager = LargeToolManager(tmp_path)
    manager.tool_manager = tool_manager
    tool_manager.tool_result_store.sqlite_repository = repository
    manager.model_manager.provider = ToolCallingProvider()
    conversation = manager.create_conversation("sqlite tools")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "run tool",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    items = projection.list_for_branch(
        conversation.metadata["id"],
        reloaded.current_node_id,
    )
    assert [item["item_type"] for item in items] == [
        "user_message",
        "assistant_process",
        "assistant_answer",
    ]
    with repository.persistence.connect() as conn:
        row = conn.execute(
            """
            SELECT output_preview, output_blob_id, output_size
            FROM tool_results
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchone()
    assert row["output_preview"] == "x" * 4096
    assert row["output_blob_id"]
    assert row["output_size"] == 5000


def test_tool_result_persistence_preserves_tool_call_arguments_and_index(tmp_path: Path):
    manager, repository, _projection = _make_manager(tmp_path)
    tool_manager = TwoToolManager(tmp_path)
    manager.tool_manager = tool_manager
    tool_manager.tool_result_store.sqlite_repository = repository
    manager.model_manager.provider = TwoToolCallingProvider()
    conversation = manager.create_conversation("sqlite two tools")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "run two tools",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == "complete"
    with repository.persistence.connect() as conn:
        call_rows = conn.execute(
            """
            SELECT id, call_index, args_inline
            FROM tool_calls
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchall()
        result_rows = conn.execute(
            """
            SELECT tool_call_id
            FROM tool_results
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchall()

    calls = {row["id"]: row for row in call_rows}
    assert set(calls) == {"call_first", "call_second"}
    assert calls["call_first"]["call_index"] == 0
    assert json.loads(calls["call_first"]["args_inline"]) == {"a": 1}
    assert calls["call_second"]["call_index"] == 1
    assert json.loads(calls["call_second"]["args_inline"]) == {"b": 2}
    assert sorted(row["tool_call_id"] for row in result_rows) == [
        "call_first",
        "call_second",
    ]
