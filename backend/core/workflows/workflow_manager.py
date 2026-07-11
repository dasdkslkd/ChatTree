from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from backend.core.agents import SubagentExecutor
from backend.core.agents.types import AgentDeliveryPolicy
from backend.core.runs import RunKind, RunManager, RunStatus
from .js_runner import WorkflowJsRunner
from .runtime_bridge import WorkflowRuntimeBridge


logger = logging.getLogger(__name__)


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
        created_by_run_id: Optional[str] = None,
        cancellation_parent_run_id: Optional[str] = None,
        budget: Optional[Dict[str, Any]] = None,
        permission_mode: Optional[str] = None,
        delegated_task: Any = None,
        original_slash_input: Optional[str] = None,
        delivery_policy: str = "auto",
        task_binding: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.runner.validate_script(script)
        delivery_policy = AgentDeliveryPolicy(str(delivery_policy or "auto")).value
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
            created_by_run_id=created_by_run_id,
            cancellation_parent_run_id=cancellation_parent_run_id,
            summary="Dynamic workflow",
            metadata={
                "args": args or {},
                "budget": budget,
                "permission_mode": permission_mode,
                "delegated_task": delegated_task if delegated_task is not None else script,
                "original_slash_input": original_slash_input,
                "delivery_policy": delivery_policy,
            },
            task_binding=task_binding,
        )
        try:
            await self._register_task_notification(run.run_id)
        except Exception:
            logger.exception("Failed to register workflow notification for %s", run.run_id)
        task = asyncio.create_task(self._produce(
            run_id=run.run_id,
            conversation_id=conversation_id,
            script=script,
            args=args or {},
            parent_node_id=parent_node_id,
            created_by_run_id=created_by_run_id,
            cancellation_parent_run_id=cancellation_parent_run_id,
            budget=budget,
            permission_mode=permission_mode,
        ))
        self._tasks[run.run_id] = task
        return run.to_dict()

    async def _register_task_notification(self, run_id: str) -> None:
        run = self.run_manager.get_run(run_id)
        if not run:
            return
        metadata = dict(run.get("metadata") or {})
        if str(metadata.get("delivery_policy") or "auto") == "silent":
            return
        service = getattr(self.run_manager, "notification_service", None)
        if service is None:
            return
        await service.register_run_notification(
            run_id=run_id,
            summary="Workflow running",
            payload={
                "delegated_task": metadata.get("delegated_task"),
                "original_slash_input": metadata.get("original_slash_input"),
            },
        )

    async def stop(self, run_id: str) -> bool:
        requested = await self.run_manager.request_stop(run_id)
        for child in self.run_manager.list_active_cancellation_children(cancellation_parent_run_id=run_id):
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
        created_by_run_id: Optional[str],
        cancellation_parent_run_id: Optional[str],
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
                notification_payload = {
                    "status": "stopped",
                    "event_type": "workflow_cancelled",
                    "content": "",
                }
                await self.run_manager.append_event(run_id, notification_payload)
                return
            notification_payload = {
                "status": "complete",
                "event_type": "workflow_result",
                "content": _result_preview(result),
                "result": result,
            }
            await self.run_manager.append_event(run_id, notification_payload)
        except asyncio.CancelledError:
            final_status = RunStatus.CANCELLED
            notification_payload = {
                "status": "stopped",
                "event_type": "workflow_cancelled",
                "content": "",
            }
            await self.run_manager.append_event(run_id, notification_payload)
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
            if notification_payload and final_status in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                source_status = {
                    RunStatus.COMPLETED: "completed",
                    RunStatus.FAILED: "failed",
                    RunStatus.CANCELLED: "cancelled",
                }[final_status]
                try:
                    await self._publish_task_notification(
                        run_id,
                        source_status,
                        notification_payload.get("content") or notification_payload.get("error") or "",
                        event_payload=notification_payload,
                    )
                except Exception:
                    logger.exception("Failed to publish workflow notification for %s", run_id)

    async def _publish_task_notification(
        self,
        run_id: str,
        source_status: str,
        content: str,
        *,
        event_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        run = self.run_manager.get_run(run_id)
        if not run:
            return None
        if run.get("kind") != RunKind.WORKFLOW.value:
            return None
        metadata = dict(run.get("metadata") or {})
        slash_metadata = metadata.get("slash_command") if isinstance(metadata.get("slash_command"), dict) else {}
        original_slash_input = metadata.get("original_slash_input") or slash_metadata.get("original_input")
        event_payload = dict(event_payload or {})
        delivery_policy = str(metadata.get("delivery_policy") or "auto")
        if delivery_policy == "silent":
            return None
        service = getattr(self.run_manager, "notification_service", None)
        if service is None:
            return None
        return await service.publish_run_notification(
            run_id=run_id,
            source_status=source_status,
            summary=f"Workflow {source_status}",
            content=content,
            payload={
                "event_type": event_payload.get("event_type"),
                "delegated_task": metadata.get("delegated_task"),
                "original_slash_input": original_slash_input,
            },
        )


def _result_preview(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
