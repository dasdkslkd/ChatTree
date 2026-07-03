from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from backend.core.agents import SubagentExecutor
from backend.core.runs import RunKind, RunManager, RunStatus
from .js_runner import WorkflowJsRunner
from .runtime_bridge import WorkflowRuntimeBridge


class WorkflowManager:
    def __init__(
        self,
        *,
        run_manager: RunManager,
        subagent_executor: SubagentExecutor,
        runner: Optional[WorkflowJsRunner] = None,
        mailbox: Any = None,
        agent_runtime: Any = None,
    ) -> None:
        self.run_manager = run_manager
        self.subagent_executor = subagent_executor
        self.runner = runner or WorkflowJsRunner()
        self.mailbox = mailbox
        self.agent_runtime = agent_runtime
        self._tasks: dict[str, asyncio.Task] = {}

    def validate(self, script: str) -> Dict[str, Any]:
        self.runner.validate_script(script)
        return {"valid": True}

    async def start(
        self,
        *,
        conversation_id: str,
        script: str,
        args: Optional[Dict[str, Any]] = None,
        parent_node_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        budget: Optional[Dict[str, Any]] = None,
        permission_mode: Optional[str] = None,
        delegated_task: Any = None,
        original_slash_input: Optional[str] = None,
        delivery_policy: str = "auto",
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        budget = {
            "max_seconds": 600,
            "max_host_calls": 200,
            "max_parallel": 8,
            **(budget or {}),
        }
        run = await self.run_manager.create_run(
            conversation_id=conversation_id,
            kind=RunKind.WORKFLOW,
            anchor_node_id=parent_node_id,
            parent_run_id=parent_run_id,
            summary="Dynamic workflow",
            metadata={
                "args": args or {},
                "budget": budget,
                "permission_mode": permission_mode,
                "delegated_task": delegated_task if delegated_task is not None else script,
                "original_slash_input": original_slash_input,
                "delivery_policy": delivery_policy,
                "task_id": task_id,
            },
        )
        task = asyncio.create_task(self._produce(
            run_id=run.run_id,
            conversation_id=conversation_id,
            script=script,
            args=args or {},
            parent_node_id=parent_node_id,
            parent_run_id=parent_run_id,
            budget=budget,
            permission_mode=permission_mode,
        ))
        self._tasks[run.run_id] = task
        return run.to_dict()

    async def stop(self, run_id: str) -> bool:
        requested = await self.run_manager.request_stop(run_id)
        for child in self.run_manager.list_active_children(parent_run_id=run_id):
            if child.get("kind") in {RunKind.SUBAGENT.value, RunKind.WORKFLOW_STEP.value}:
                await self.subagent_executor.stop(str(child["run_id"]))
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            return True
        return requested

    async def _produce(
        self,
        *,
        run_id: str,
        conversation_id: str,
        script: str,
        args: Dict[str, Any],
        parent_node_id: Optional[str],
        parent_run_id: Optional[str],
        budget: Dict[str, Any],
        permission_mode: Optional[str] = None,
    ) -> None:
        final_status = RunStatus.COMPLETED
        final_error = None
        notification_payload: Optional[Dict[str, Any]] = None
        try:
            bridge = WorkflowRuntimeBridge(
                workflow_run_id=run_id,
                conversation_id=conversation_id,
                parent_node_id=parent_node_id,
                run_manager=self.run_manager,
                subagent_executor=self.subagent_executor,
                agent_runtime=self.agent_runtime,
                max_parallel=int(budget.get("max_parallel") or 8),
                permission_mode=permission_mode,
            )
            await self.run_manager.append_event(run_id, {
                "status": "start",
                "event_type": "workflow_start",
                "content": None,
            })
            result = await asyncio.wait_for(
                self.runner.run(script=script, args=args, budget=budget, bridge=bridge),
                timeout=int(budget.get("max_seconds") or 600),
            )
            if await self.run_manager.is_stop_requested(run_id):
                final_status = RunStatus.CANCELLED
                await self.run_manager.append_event(run_id, {
                    "status": "stopped",
                    "event_type": "workflow_cancelled",
                    "content": "",
                })
                return
            notification_payload = {
                "status": "complete",
                "event_type": "workflow_result",
                "content": "" if result is None else str(result),
                "result": result,
            }
            await self.run_manager.append_event(run_id, notification_payload)
        except asyncio.CancelledError:
            final_status = RunStatus.CANCELLED
            await self.run_manager.append_event(run_id, {
                "status": "stopped",
                "event_type": "workflow_cancelled",
                "content": "",
            })
        except Exception as exc:
            final_status = RunStatus.FAILED
            final_error = str(exc) or exc.__class__.__name__
            notification_payload = {
                "status": "error",
                "event_type": "workflow_error",
                "content": "",
                "error": final_error,
            }
            await self.run_manager.append_event(run_id, notification_payload)
        finally:
            self._tasks.pop(run_id, None)
            await self.run_manager.finish_run(run_id, final_status, final_error)
            if notification_payload and final_status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                source_status = "completed" if final_status == RunStatus.COMPLETED else "failed"
                await self._enqueue_synthetic_task_notification(
                    run_id,
                    source_status,
                    notification_payload.get("content") or notification_payload.get("error") or "",
                    event_payload=notification_payload,
                )

    async def _enqueue_synthetic_task_notification(
        self,
        run_id: str,
        source_status: str,
        content: str,
        *,
        event_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        run = self.run_manager.get_run(run_id)
        if not run or run.get("parent_run_id"):
            return None
        if run.get("kind") != RunKind.WORKFLOW.value:
            return None
        metadata = dict(run.get("metadata") or {})
        slash_metadata = metadata.get("slash_command") if isinstance(metadata.get("slash_command"), dict) else {}
        original_slash_input = metadata.get("original_slash_input") or slash_metadata.get("original_input")
        event_payload = dict(event_payload or {})
        mailbox_message_id = None
        if self.mailbox is not None:
            message_type = "result" if source_status == "completed" else "error"
            mailbox_item = await self.mailbox.publish(
                conversation_id=str(run["conversation_id"]),
                source_run_id=run_id,
                source_run_kind=RunKind.WORKFLOW.value,
                message_type=message_type,
                content=content,
                metadata={
                    "origin": "task_notification",
                    "source_status": source_status,
                    "event_type": event_payload.get("event_type"),
                    "delegated_task": metadata.get("delegated_task"),
                    "original_slash_input": original_slash_input,
                    "task_id": metadata.get("task_id"),
                },
                delivery_policy=str(metadata.get("delivery_policy") or "auto"),
            )
            mailbox_message_id = mailbox_item.message_id
        item = self.run_manager.synthetic_inputs.enqueue(
            kind="task_notification",
            conversation_id=str(run["conversation_id"]),
            anchor_node_id=run.get("anchor_node_id"),
            source_run_id=run_id,
            source_run_kind=RunKind.WORKFLOW.value,
            status="pending",
            summary=f"Workflow {source_status}",
            content=content,
            metadata={
                "origin": "task_notification",
                "source_status": source_status,
                "event_type": event_payload.get("event_type"),
                "delegated_task": metadata.get("delegated_task"),
                "original_slash_input": original_slash_input,
                "mailbox_message_id": mailbox_message_id,
                "task_id": metadata.get("task_id"),
            },
        )
        return item.to_dict()
