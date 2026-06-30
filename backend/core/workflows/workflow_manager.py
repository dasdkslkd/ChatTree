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
    ) -> None:
        self.run_manager = run_manager
        self.subagent_executor = subagent_executor
        self.runner = runner or WorkflowJsRunner()
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
            metadata={"args": args or {}, "budget": budget},
        )
        task = asyncio.create_task(self._produce(
            run_id=run.run_id,
            conversation_id=conversation_id,
            script=script,
            args=args or {},
            parent_node_id=parent_node_id,
            budget=budget,
        ))
        self._tasks[run.run_id] = task
        return run.to_dict()

    async def stop(self, run_id: str) -> bool:
        requested = await self.run_manager.request_stop(run_id)
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
        budget: Dict[str, Any],
    ) -> None:
        final_status = RunStatus.COMPLETED
        final_error = None
        try:
            bridge = WorkflowRuntimeBridge(
                workflow_run_id=run_id,
                conversation_id=conversation_id,
                parent_node_id=parent_node_id,
                run_manager=self.run_manager,
                subagent_executor=self.subagent_executor,
                max_parallel=int(budget.get("max_parallel") or 8),
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
            await self.run_manager.append_event(run_id, {
                "status": "complete",
                "event_type": "workflow_result",
                "content": "" if result is None else str(result),
                "result": result,
            })
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
            await self.run_manager.append_event(run_id, {
                "status": "error",
                "event_type": "workflow_error",
                "content": "",
                "error": final_error,
            })
        finally:
            self._tasks.pop(run_id, None)
            await self.run_manager.finish_run(run_id, final_status, final_error)
