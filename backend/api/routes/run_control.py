from __future__ import annotations

from typing import Any, Optional

from backend.core.runs import RunKind, RunManager


async def stop_run_tree(
    run_id: str,
    *,
    run_manager: RunManager,
    chat_manager: Any = None,
    subagent_executor: Any = None,
    command_executor: Any = None,
    workflow_manager: Any = None,
    _seen: Optional[set[str]] = None,
) -> list[str]:
    seen = _seen if _seen is not None else set()
    if run_id in seen:
        return []
    seen.add(run_id)

    run = run_manager.get_run(run_id)
    if not run:
        return []

    stopped: list[str] = []
    if await run_manager.request_stop(run_id):
        stopped.append(run_id)

    kind = run.get("kind")
    if kind == RunKind.CHAT.value and run.get("target_node_id") and chat_manager is not None:
        await chat_manager.stop_stream(str(run["target_node_id"]))
    elif kind == RunKind.SIDE_QUESTION.value and chat_manager is not None:
        await chat_manager.stop_stream(run_id)
    elif kind in {RunKind.SUBAGENT.value, RunKind.WORKFLOW_STEP.value}:
        if subagent_executor is not None and hasattr(subagent_executor, "stop"):
            await subagent_executor.stop(run_id)
    elif kind == RunKind.COMMAND.value:
        if command_executor is not None and hasattr(command_executor, "stop"):
            await command_executor.stop(run_id)
    elif kind == RunKind.WORKFLOW.value:
        if workflow_manager is not None and hasattr(workflow_manager, "stop"):
            await workflow_manager.stop(run_id)

    for child in run_manager.list_active_children(
        parent_run_id=run_id,
        conversation_id=str(run.get("conversation_id") or ""),
    ):
        stopped.extend(await stop_run_tree(
            str(child["run_id"]),
            run_manager=run_manager,
            chat_manager=chat_manager,
            subagent_executor=subagent_executor,
            command_executor=command_executor,
            workflow_manager=workflow_manager,
            _seen=seen,
        ))

    return stopped