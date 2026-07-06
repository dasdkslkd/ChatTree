import asyncio

from backend.core.agents.mailbox import AgentMailbox
from backend.core.agents.runtime import AgentRuntime
from backend.core.agents.types import AgentSource
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.runs import RunKind, RunManager, RunStatus


async def _publish_notify_message_enqueues_pending_notification():
    mailbox = AgentMailbox()
    message = await mailbox.publish(
        conversation_id="conversation-1",
        source_run_id="run-1",
        source_run_kind="subagent",
        message_type="result",
        content="done",
        delivery_policy="notify",
    )

    pending = await mailbox.list_pending_notifications("conversation-1")

    assert [item["message_id"] for item in pending] == [message.message_id]
    assert pending[0]["integrated_at"] is None
    assert pending[0]["acknowledged_at"] is None


def test_publish_notify_message_enqueues_pending_notification():
    asyncio.run(_publish_notify_message_enqueues_pending_notification())


async def _claim_release_notification_preserves_retry():
    mailbox = AgentMailbox()
    message = await mailbox.publish(
        conversation_id="conversation-1",
        source_run_id="run-1",
        source_run_kind="subagent",
        message_type="result",
        content="done",
        delivery_policy="notify",
    )

    claimed = await mailbox.claim_notification("conversation-1", message.message_id)
    assert claimed is not None
    assert await mailbox.list_pending_notifications("conversation-1") == []

    await mailbox.release_notification("conversation-1", message.message_id)
    pending = await mailbox.list_pending_notifications("conversation-1")

    assert [item["message_id"] for item in pending] == [message.message_id]


def test_claim_release_notification_preserves_retry():
    asyncio.run(_claim_release_notification_preserves_retry())


async def _mark_integrated_is_idempotent_and_hides_notification():
    mailbox = AgentMailbox()
    message = await mailbox.publish(
        conversation_id="conversation-1",
        source_run_id="run-1",
        source_run_kind="subagent",
        message_type="result",
        content="done",
        delivery_policy="notify",
    )

    first = await mailbox.mark_integrated("conversation-1", message.message_id)
    second = await mailbox.mark_integrated("conversation-1", message.message_id)

    assert first["integrated_at"] == second["integrated_at"]
    assert await mailbox.list_pending_notifications("conversation-1") == []


def test_mark_integrated_is_idempotent_and_hides_notification():
    asyncio.run(_mark_integrated_is_idempotent_and_hides_notification())


async def _duplicate_publish_returns_existing_message():
    mailbox = AgentMailbox()
    first = await mailbox.publish(
        conversation_id="conversation-1",
        source_run_id="run-1",
        source_run_kind="subagent",
        message_type="result",
        content="done",
        metadata={"result_version": "v1"},
        delivery_policy="notify",
    )
    second = await mailbox.publish(
        conversation_id="conversation-1",
        source_run_id="run-1",
        source_run_kind="subagent",
        message_type="result",
        content="done",
        metadata={"result_version": "v1"},
        delivery_policy="notify",
    )

    assert first.message_id == second.message_id
    pending = await mailbox.list_pending_notifications("conversation-1")
    assert len(pending) == 1


def test_duplicate_publish_returns_existing_message():
    asyncio.run(_duplicate_publish_returns_existing_message())


async def _wait_agent_reads_terminal_subagent_result():
    run_manager = RunManager()
    mailbox = AgentMailbox()
    runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=mailbox,
        subagent_executor=object(),
        capability_registry=object(),
    )
    run = await run_manager.create_run(
        conversation_id="conversation-1",
        kind=RunKind.SUBAGENT,
        parent_run_id="chat-1",
        summary="general: ok",
        metadata={"agent_name": "general"},
    )
    await run_manager.append_event(run.run_id, {
        "status": "complete",
        "event_type": "subagent_result",
        "agent_name": "general",
        "content": "OK",
    })
    await run_manager.finish_run(run.run_id, RunStatus.COMPLETED)

    result = await runtime.wait_agent(
        source=AgentSource(
            conversation_id="conversation-1",
            run_id="chat-1",
            run_kind=RunKind.CHAT.value,
        ),
        run_ids=[run.run_id],
        timeout_seconds=0.01,
    )

    assert result["status"] == "completed"
    assert result["runs"][0]["content"] == "OK"
    assert result["runs"][0]["message_type"] == "result"
    assert result["runs"][0]["event_type"] == "subagent_result"


def test_wait_agent_reads_terminal_subagent_result():
    asyncio.run(_wait_agent_reads_terminal_subagent_result())


async def _wait_agent_reads_workflow_error_from_repository_events(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    runs = SQLiteRunRepository(persistence)
    conversation_id = chat.create_conversation(title="workflow")
    node_id = chat.create_node(conversation_id, parent_id=None)
    run_manager = RunManager(repository=runs)
    runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=AgentMailbox(),
        subagent_executor=object(),
        capability_registry=object(),
    )
    chat_run = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.CHAT,
        anchor_node_id=node_id,
        summary="chat",
    )
    run = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.WORKFLOW,
        parent_run_id=chat_run.run_id,
        anchor_node_id=node_id,
        summary="Dynamic workflow",
    )
    await run_manager.append_event(run.run_id, {
        "status": "error",
        "event_type": "workflow_error",
        "error": "workflow boom",
    })
    await run_manager.finish_run(run.run_id, RunStatus.FAILED)

    result = await runtime.wait_agent(
        source=AgentSource(
            conversation_id=conversation_id,
            run_id=chat_run.run_id,
            run_kind=RunKind.CHAT.value,
        ),
        run_ids=[run.run_id],
        timeout_seconds=0.01,
    )

    assert result["status"] == "completed"
    assert result["runs"][0]["status"] == "failed"
    assert result["runs"][0]["message_type"] == "error"
    assert result["runs"][0]["content"] == ""
    assert result["runs"][0]["error"] == "workflow boom"
    assert result["runs"][0]["event_type"] == "workflow_error"


def test_wait_agent_reads_workflow_error_from_repository_events(tmp_path):
    asyncio.run(_wait_agent_reads_workflow_error_from_repository_events(tmp_path))
