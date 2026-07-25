import asyncio

from backend.core.agents.runtime import AgentRuntime
from backend.core.agents.types import AgentSource
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.runs import RunKind, RunManager, RunStatus

async def _wait_agent_reads_terminal_subagent_result():
    run_manager = RunManager()
    runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=None,
        subagent_executor=object(),
        capability_registry=object(),
    )
    run = await run_manager.create_run(
        conversation_id="conversation-1",
        kind=RunKind.SUBAGENT,
        created_by_run_id="chat-1",
        cancellation_parent_run_id=None,
        summary="general: ok",
        metadata={
            "agent_name": "general",
            "task_outcome": {
                "kind": "run_finished",
                "task_status": "completed",
                "step": 2,
                "step_status": "completed",
                "run_status": "completed",
            },
        },
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
    assert result["runs"][0]["task_outcome"]["task_status"] == "completed"


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
        mailbox=None,
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
        created_by_run_id=chat_run.run_id,
        cancellation_parent_run_id=None,
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


async def _wait_agent_timeout_reports_running_progress():
    run_manager = RunManager()
    runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=None,
        subagent_executor=object(),
        capability_registry=object(),
    )
    run = await run_manager.create_run(
        conversation_id="conversation-1",
        kind=RunKind.SUBAGENT,
        created_by_run_id="chat-1",
        cancellation_parent_run_id=None,
        summary="explorer: scan",
        metadata={"agent_name": "explorer"},
    )
    await run_manager.append_event(run.run_id, {
        "status": "content",
        "event_type": "tool_call",
        "tool_calls": [{"function": {"name": "glob"}}],
    })

    result = await runtime.wait_agent(
        source=AgentSource(
            conversation_id="conversation-1",
            run_id="chat-1",
            run_kind=RunKind.CHAT.value,
        ),
        run_ids=[run.run_id],
        timeout_seconds=0.01,
    )

    assert result["status"] == "running"
    assert result["wait_status"] == "timeout"
    assert result["runs"][0]["status"] == "running"
    assert result["runs"][0]["message_type"] == "in_progress"
    assert result["runs"][0]["wait_status"] == "timeout"
    assert result["runs"][0]["timed_out"] is True
    assert result["runs"][0]["last_event"]["event_type"] == "tool_call"
    assert result["runs"][0]["last_event"]["tool_name"] == "glob"


def test_wait_agent_timeout_reports_running_progress():
    asyncio.run(_wait_agent_timeout_reports_running_progress())


async def _resume_agent_reports_running_progress():
    run_manager = RunManager()
    runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=None,
        subagent_executor=object(),
        capability_registry=object(),
    )
    run = await run_manager.create_run(
        conversation_id="conversation-1",
        kind=RunKind.SUBAGENT,
        created_by_run_id="chat-1",
        cancellation_parent_run_id=None,
        summary="explorer: scan",
        metadata={"agent_name": "explorer"},
    )
    await run_manager.append_event(run.run_id, {
        "status": "content",
        "event_type": "tool_result",
        "tool_call": {"name": "glob"},
    })

    result = await runtime.resume_agent(run_id=run.run_id)

    assert result["status"] == "running"
    assert result["event_count"] == 2
    assert result["last_event"]["event_type"] == "tool_result"
    assert result["last_event"]["tool_name"] == "glob"


def test_resume_agent_reports_running_progress():
    asyncio.run(_resume_agent_reports_running_progress())
