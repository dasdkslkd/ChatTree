from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import agents as agents_route
from backend.api.routes import notifications as notifications_route
from backend.core.notifications import TaskNotificationService, TaskNotificationTransitionError
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.transcript import TranscriptAssembler


def _storage(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation(title="notification")
    node_id = repository.create_node(conversation_id, parent_id=None)
    runs = SQLiteRunRepository(persistence)
    service = TaskNotificationService(persistence)
    return persistence, conversation_id, node_id, runs, service


def _notifications_client(persistence: SQLitePersistence, service: TaskNotificationService) -> TestClient:
    app = FastAPI()
    app.include_router(notifications_route.router)
    app.state.persistence = persistence
    app.state.task_notification_service = service
    app.state.transcript_assembler = TranscriptAssembler(persistence)
    return TestClient(app)


def test_create_route_forbids_status_override_and_uses_unbound(tmp_path):
    persistence, conversation_id, node_id, runs, service = _storage(tmp_path)
    run_id = runs.create_run(
        conversation_id,
        kind="workflow",
        anchor_node_id=node_id,
        summary="workflow",
    )
    client = _notifications_client(persistence, service)

    invalid = client.post(
        f"/conversations/{conversation_id}/task-notifications",
        json={
            "source_run_id": run_id,
            "status": "delivered",
            "summary": "bad",
        },
    )
    created = client.post(
        f"/conversations/{conversation_id}/task-notifications",
        json={
            "source_run_id": run_id,
            "source_run_kind": "workflow",
            "summary": "工作流完成",
            "content": "产物已生成",
            "notification_id": "notification-1",
        },
    )

    assert invalid.status_code == 422
    assert created.status_code == 200
    assert created.json()["notification"]["status"] == "unbound"


def test_notification_state_machine_rejects_illegal_transitions(tmp_path):
    persistence, conversation_id, node_id, runs, service = _storage(tmp_path)
    source_run_id = runs.create_run(
        conversation_id,
        kind="workflow",
        anchor_node_id=node_id,
        summary="workflow",
    )
    delivery_run_id = runs.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="deliver notification",
    )
    service.create(
        conversation_id=conversation_id,
        source_run_id=source_run_id,
        source_run_kind="workflow",
        notification_id="notification-1",
    )

    bound = service.bind(conversation_id, "notification-1", node_id)
    delivering = service.start_delivery(conversation_id, "notification-1", delivery_run_id)
    delivered = service.mark_delivered(conversation_id, "notification-1")

    assert [bound["status"], delivering["status"], delivered["status"]] == [
        "bound",
        "delivering",
        "delivered",
    ]
    assert delivering["delivery_run_id"] == delivery_run_id
    with pytest.raises(TaskNotificationTransitionError):
        service.delete(conversation_id, "notification-1")
    with pytest.raises(TaskNotificationTransitionError):
        service.bind(conversation_id, "notification-1", node_id)


def test_bind_delete_do_not_create_messages_or_nodes(tmp_path):
    persistence, conversation_id, node_id, runs, service = _storage(tmp_path)
    source_run_id = runs.create_run(
        conversation_id,
        kind="subagent",
        anchor_node_id=node_id,
        summary="agent",
    )
    service.create(
        conversation_id=conversation_id,
        source_run_id=source_run_id,
        source_run_kind="subagent",
        notification_id="notification-1",
    )

    service.bind(conversation_id, "notification-1", node_id)
    service.delete(conversation_id, "notification-1")

    with persistence.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1
        row = conn.execute("SELECT status FROM task_notifications").fetchone()
    assert row["status"] == "delivery_cancelled"


def test_duplicate_create_does_not_rewind_state(tmp_path):
    _, conversation_id, node_id, runs, service = _storage(tmp_path)
    source_run_id = runs.create_run(
        conversation_id,
        kind="workflow",
        anchor_node_id=node_id,
        summary="workflow",
    )
    service.create(
        conversation_id=conversation_id,
        source_run_id=source_run_id,
        source_run_kind="workflow",
        summary="first",
        notification_id="notification-1",
    )
    service.bind(conversation_id, "notification-1", node_id)

    duplicate = service.create(
        conversation_id=conversation_id,
        source_run_id=source_run_id,
        source_run_kind="workflow",
        summary="updated",
        notification_id="notification-1",
    )

    assert duplicate["status"] == "bound"
    assert duplicate["summary"] == "updated"
    assert duplicate["delivery_node_id"] == node_id


def test_duplicate_create_cannot_change_source_run(tmp_path):
    _, conversation_id, node_id, runs, service = _storage(tmp_path)
    first_run_id = runs.create_run(
        conversation_id,
        kind="workflow",
        anchor_node_id=node_id,
        summary="workflow 1",
    )
    second_run_id = runs.create_run(
        conversation_id,
        kind="workflow",
        anchor_node_id=node_id,
        summary="workflow 2",
    )
    service.create(
        conversation_id=conversation_id,
        source_run_id=first_run_id,
        source_run_kind="workflow",
        notification_id="notification-1",
    )

    with pytest.raises(TaskNotificationTransitionError):
        service.create(
            conversation_id=conversation_id,
            source_run_id=second_run_id,
            source_run_kind="workflow",
            notification_id="notification-1",
        )


async def _finished_run_creates_canonical_notification(tmp_path):
    persistence, conversation_id, node_id, runs, service = _storage(tmp_path)
    run_manager = RunManager(repository=runs)
    run_manager.add_finish_listener(service.create_from_finished_run)
    run = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.SUBAGENT,
        anchor_node_id=node_id,
        summary="实现子任务",
        metadata={"delivery_policy": "notify"},
    )
    await run_manager.append_event(
        run.run_id,
        {
            "status": "complete",
            "event_type": "subagent_result",
            "content": "子任务完成",
        },
    )
    await run_manager.finish_run(run.run_id, RunStatus.COMPLETED)

    notification = service.get(conversation_id, f"task_notification_{run.run_id}")
    assert notification["status"] == "unbound"
    assert notification["source_run_id"] == run.run_id
    assert notification["source_run_kind"] == "subagent"
    assert notification["summary"] == "实现子任务"
    assert notification["content"] == "子任务完成"


def test_finished_run_creates_canonical_notification(tmp_path):
    asyncio.run(_finished_run_creates_canonical_notification(tmp_path))


async def _silent_finished_run_does_not_create_notification(tmp_path):
    _, conversation_id, node_id, runs, service = _storage(tmp_path)
    run_manager = RunManager(repository=runs)
    run_manager.add_finish_listener(service.create_from_finished_run)
    run = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.WORKFLOW,
        anchor_node_id=node_id,
        summary="silent workflow",
        metadata={"delivery_policy": "silent"},
    )
    await run_manager.append_event(
        run.run_id,
        {
            "status": "complete",
            "event_type": "workflow_result",
            "content": "done",
        },
    )
    await run_manager.finish_run(run.run_id, RunStatus.COMPLETED)

    with pytest.raises(KeyError):
        service.get(conversation_id, f"task_notification_{run.run_id}")


def test_silent_finished_run_does_not_create_notification(tmp_path):
    asyncio.run(_silent_finished_run_does_not_create_notification(tmp_path))


def test_agent_mailbox_pending_route_is_deleted():
    app = FastAPI()
    app.include_router(agents_route.router)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/conversations/conv-1/agents/mailbox/pending")

    assert response.status_code == 404
