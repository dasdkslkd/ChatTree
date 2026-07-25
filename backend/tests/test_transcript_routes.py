from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import conversations as conversations_route
from backend.api.routes import messages as messages_route
from backend.api.routes import notifications as notifications_route
from backend.api.routes import plans as plans_route
from backend.api.routes import tool_results as tool_results_route
from backend.core.persistence.blob_store import BlobStore
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.plan_repository import SQLitePlanRepository
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.plans import PlanLedger
from backend.core.runs import RunManager
from backend.core.transcript import TranscriptAssembler


def client_for(assembler: TranscriptAssembler, run_manager: RunManager | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(conversations_route.router)
    app.state.transcript_assembler = assembler
    app.state.run_manager = run_manager or RunManager(repository=SQLiteRunRepository(assembler.persistence))
    return TestClient(app)


class _PlanActionChatManager:
    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        self.calls = []

    def get_conversation(self, conversation_id: str):
        return None

    async def send_message_stream(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["parent_node_id"] == self.node_id
        assert kwargs["suppress_user_message"] is True
        assert kwargs["append_to_existing_node"] is True
        assert kwargs["continuation_messages"][0]["tool_call_id"] == "call-exit"
        yield {
            "status": "start",
            "content": None,
            "node_id": self.node_id,
            "target_node_id": self.node_id,
            "conversation_id": kwargs["conversation_id"],
            "run_id": kwargs["run_id"],
            "assistant_message_id": "assistant-continuation",
        }
        yield {
            "status": "content",
            "content": "继续执行",
            "node_id": self.node_id,
            "target_node_id": self.node_id,
            "conversation_id": kwargs["conversation_id"],
            "run_id": kwargs["run_id"],
            "assistant_message_id": "assistant-continuation",
        }


def test_conversation_transcript_route_returns_backend_assembled_snapshot(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation(title="Transcript route")
    node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    message_id = repository.add_message(
        conversation_id,
        node_id,
        role="user",
        content="hello from sqlite",
    )
    client = client_for(TranscriptAssembler(persistence))

    response = client.get(
        f"/conversations/{conversation_id}/transcript",
        params={"node_id": node_id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["conversation_id"] == conversation_id
    assert payload["node_id"] == node_id
    assert isinstance(payload["revision"], int)
    assert payload["items"] == [
        {
            "type": "user_message",
            "id": f"message:{message_id}",
            "conversation_id": conversation_id,
            "node_id": node_id,
            "parent_node_id": None,
            "message_id": message_id,
            "content": "hello from sqlite",
            "import_files": [],
            "image_refs": [],
            "tool_permission_mode": None,
            "task_context_mode": "attached",
            "created_at": payload["items"][0]["created_at"],
        }
    ]


def test_messages_router_does_not_publish_legacy_history_routes():
    route_paths = {
        getattr(route, "path", "")
        for route in messages_route.router.routes
        if "GET" in getattr(route, "methods", set())
    }

    assert "/conversations/{conversation_id}/messages" not in route_paths
    assert "/conversations/{conversation_id}/messages/{node_id}" not in route_paths


def test_conversation_transcript_route_includes_active_run_buffer(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation(title="Transcript route")
    node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    repository.add_message(conversation_id, node_id, role="user", content="hello")
    run_manager = RunManager(repository=SQLiteRunRepository(persistence))

    async def create_live_run():
        run = await run_manager.create_run(
            conversation_id=conversation_id,
            kind="chat",
            target_node_id=node_id,
            summary="live",
        )
        await run_manager.append_event(run.run_id, {
            "status": "content",
            "conversation_id": conversation_id,
            "node_id": node_id,
            "target_node_id": node_id,
            "assistant_message_id": "assistant-live",
            "content": "正在回答",
        })

    asyncio.run(create_live_run())
    client = client_for(TranscriptAssembler(persistence), run_manager)

    response = client.get(
        f"/conversations/{conversation_id}/transcript",
        params={"node_id": node_id},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert "assistant_answer" not in {item["type"] for item in items}
    process = next(item for item in items if item["type"] == "assistant_process")
    assert process["blocks"] == [
        {
            "type": "content",
            "id": f"content:{next(iter(run_manager.list_active(conversation_id)))['run_id']}:0",
            "content": "正在回答",
            "streaming": True,
        }
    ]


def test_conversation_transcript_route_returns_404_for_unknown_conversation(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    client = client_for(TranscriptAssembler(persistence))

    response = client.get("/conversations/missing/transcript", params={"node_id": "missing-tip"})

    assert response.status_code == 404


def test_conversation_transcript_route_returns_empty_items_for_empty_branch(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation(title="Empty")
    node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    client = client_for(TranscriptAssembler(persistence))

    response = client.get(f"/conversations/{conversation_id}/transcript", params={"node_id": node_id})

    assert response.status_code == 200
    assert response.json() == {
        "conversation_id": conversation_id,
        "node_id": node_id,
        "revision": 0,
        "items": [],
    }


def test_tool_result_route_falls_back_to_sqlite_blob_result(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation(title="Tool result route")
    node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    blob = BlobStore(persistence).put_text("0123456789")
    with persistence.connect() as conn:
        conn.execute(
            """
            INSERT INTO tool_calls (
              id,
              conversation_id,
              node_id,
              call_index,
              name,
              status,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
            """,
            ("call-1", conversation_id, node_id, 0, "shell", "complete"),
        )
        conn.execute(
            """
            INSERT INTO tool_results (
              id,
              conversation_id,
              node_id,
              tool_call_id,
              status,
              output_preview,
              output_blob_id,
              output_size,
              created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
            """,
            (
                "tool-result-1",
                conversation_id,
                node_id,
                "call-1",
                "complete",
                "0123",
                blob.blob_id,
                10,
            ),
        )
    app = FastAPI()
    app.include_router(tool_results_route.router)
    app.state.persistence = persistence
    client = TestClient(app)

    response = client.get("/tool-results/tool-result-1", params={"offset": 3, "limit": 4})

    assert response.status_code == 200
    assert response.json() == {
        "tool_result_id": "tool-result-1",
        "tool_name": "shell",
        "offset": 3,
        "limit": 4,
        "next_offset": 7,
        "total_chars": 10,
        "has_more": True,
        "content": "3456",
    }


def test_plan_approve_stream_updates_tool_result_without_user_message_or_node(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    plan_repository = SQLitePlanRepository(persistence)
    ledger = PlanLedger(repository=plan_repository)
    conversation_id = repository.create_conversation(title="Plan route")
    node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    run_repository = SQLiteRunRepository(persistence)
    run_id = run_repository.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="plan",
    )
    run_repository.finish_run(run_id, "completed", None)
    plan_id = plan_repository.create_plan(
        conversation_id,
        entered_node_id=node_id,
        entered_run_id=run_id,
    )
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-exit",
        name="exit_plan_mode",
        arguments={"plan": "后端实现计划"},
        run_id=run_id,
    )
    plan_repository.await_approval(
        conversation_id,
        plan_id,
        tool_call_id="call-exit",
        node_id=node_id,
        run_id=run_id,
    )
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-exit",
        tool_call_id="call-exit",
        output='{"status":"awaiting_approval"}',
        run_id=run_id,
    )
    with persistence.connect() as conn:
        before_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        before_user_messages = conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'user'").fetchone()[0]

    app = FastAPI()
    app.include_router(plans_route.router)
    app.state.persistence = persistence
    app.state.plan_ledger = ledger
    app.state.transcript_assembler = TranscriptAssembler(persistence)
    app.state.run_manager = RunManager(repository=run_repository)
    app.state.chat_manager = _PlanActionChatManager(node_id)
    client = TestClient(app)

    response = client.post(f"/conversations/{conversation_id}/plans/{plan_id}/approve")

    assert response.status_code == 200
    patches = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    assert [patch["revision"] for patch in patches] == sorted({patch["revision"] for patch in patches})
    assert "plan" not in patches[0]
    assert patches[0]["type"] == "transcript_patch"
    assert patches[0]["operations"][0]["item"]["id"] == "plan-approval:call-exit"
    assert patches[0]["operations"][0]["item"]["status"] == "approved"
    assert patches[-1]["type"] == "transcript_patch"
    final_ops = patches[-1]["operations"]
    assert any(operation["op"] == "remove" and operation["id"].startswith(f"process:{node_id}:") for operation in final_ops)
    final_answer = next(operation["item"] for operation in final_ops if operation["op"] == "upsert" and operation["item"]["type"] == "assistant_answer")
    assert final_answer["id"] == "message:assistant-continuation"
    assert final_answer["content"] == "继续执行"
    with persistence.connect() as conn:
        after_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        after_user_messages = conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'user'").fetchone()[0]
        result = conn.execute(
            """
            SELECT output_preview
            FROM tool_results
            WHERE conversation_id = ? AND tool_call_id = ?
            """,
            (conversation_id, "call-exit"),
        ).fetchone()
    assert after_nodes == before_nodes
    assert after_user_messages == before_user_messages
    assert result is not None
    assert json.loads(result["output_preview"])["status"] == "approved"
    with persistence.connect() as conn:
        continuation = conn.execute(
            """
            SELECT id, anchor_node_id, target_node_id
            FROM runs
            WHERE conversation_id = ? AND id != ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (conversation_id, run_id),
        ).fetchone()
    assert continuation is not None
    assert continuation["anchor_node_id"] == node_id
    assert continuation["target_node_id"] == node_id


def test_task_notification_bind_and_delete_return_patch_for_same_item(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation(title="Notification route")
    node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="workflow",
        anchor_node_id=node_id,
        summary="workflow finished",
    )
    with persistence.connect() as conn:
        conn.execute(
            """
            INSERT INTO task_notifications (
              id,
              conversation_id,
              source_run_id,
              source_run_kind,
              status,
              summary,
              content,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
            """,
            (
                "notification-1",
                conversation_id,
                run_id,
                "workflow",
                "unbound",
                "工作流完成",
                "产物已生成",
            ),
        )
    app = FastAPI()
    app.include_router(notifications_route.router)
    app.state.persistence = persistence
    app.state.transcript_assembler = TranscriptAssembler(persistence)
    client = TestClient(app)

    bind_response = client.post(
        f"/conversations/{conversation_id}/task-notifications/notification-1/bind",
        json={"delivery_node_id": node_id},
    )
    delete_response = client.delete(
        f"/conversations/{conversation_id}/task-notifications/notification-1"
    )

    assert bind_response.status_code == 200
    assert delete_response.status_code == 200
    bind_patch = bind_response.json()["transcript_patch"]
    delete_patch = delete_response.json()["transcript_patch"]
    assert [bind_patch["revision"], delete_patch["revision"]] == [1, 2]
    assert bind_patch["operations"][0]["item"]["id"] == "task-notification:notification-1"
    assert bind_patch["operations"][0]["item"]["status"] == "bound"
    assert delete_patch["operations"][0]["item"]["id"] == "task-notification:notification-1"
    assert delete_patch["operations"][0]["item"]["status"] == "delivery_cancelled"
    with persistence.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1
