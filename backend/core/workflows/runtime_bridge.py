from __future__ import annotations

import asyncio
from typing import Any, Dict

from backend.core.agents import SubagentExecutor
from backend.core.runs import RunManager, RunStatus


class WorkflowRuntimeBridge:
    def __init__(
        self,
        *,
        workflow_run_id: str,
        conversation_id: str,
        parent_node_id: str | None,
        run_manager: RunManager,
        subagent_executor: SubagentExecutor,
        max_parallel: int = 8,
        permission_mode: str | None = None,
    ) -> None:
        self.workflow_run_id = workflow_run_id
        self.conversation_id = conversation_id
        self.parent_node_id = parent_node_id
        self.run_manager = run_manager
        self.subagent_executor = subagent_executor
        self.permission_mode = permission_mode
        self._parallel_sem = asyncio.Semaphore(max_parallel)

    async def handle_call(self, method: str, params: Dict[str, Any]) -> Any:
        if method == "log":
            return await self._log(params)
        if method == "phase_start":
            return await self._phase("phase_start", params)
        if method == "phase_end":
            return await self._phase("phase_end", params)
        if method == "agent":
            return await self._agent(params)
        if method == "workflow":
            return {
                "run_id": self.workflow_run_id,
                "conversation_id": self.conversation_id,
                "parent_node_id": self.parent_node_id,
            }
        raise ValueError(f"unsupported workflow host method: {method}")

    async def _log(self, params: Dict[str, Any]) -> Dict[str, Any]:
        await self.run_manager.append_event(self.workflow_run_id, {
            "status": "content",
            "event_type": "workflow_log",
            "content": str(params.get("message") or ""),
            "payload": params.get("data"),
        })
        return {"ok": True}

    async def _phase(self, event_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
        await self.run_manager.append_event(self.workflow_run_id, {
            "status": "content",
            "event_type": event_type,
            "phase": str(params.get("name") or ""),
            "payload": params.get("data"),
        })
        return {"ok": True}

    async def _agent(self, params: Dict[str, Any]) -> Dict[str, Any]:
        async with self._parallel_sem:
            agent_name = str(params.get("name") or params.get("agent") or "workflow-worker")
            input_data = params.get("input", params.get("prompt", ""))
            options = params.get("options") if isinstance(params.get("options"), dict) else {}
            run = await self.subagent_executor.start(
                conversation_id=self.conversation_id,
                agent_name=agent_name,
                input_data=input_data,
                parent_node_id=options.get("parent_node_id") or self.parent_node_id,
                parent_run_id=self.workflow_run_id,
                provider_id=options.get("provider_id"),
                model_id=options.get("model_id"),
                permission_mode=options.get("permission_mode") or self.permission_mode,
                workspace=options.get("workspace"),
            )
            run_id = str(run["run_id"])
            content = ""
            status = RunStatus.COMPLETED.value
            try:
                async for payload in self.run_manager.subscribe(run_id, 0):
                    await self.run_manager.append_event(self.workflow_run_id, {
                        "status": "content",
                        "event_type": "workflow_child_event",
                        "child_run_id": run_id,
                        "child_kind": "subagent",
                        "payload": payload,
                    })
                    if payload.get("event_type") in {"subagent_result", "subagent_error"}:
                        content = payload.get("content") or content
                    if payload.get("type") == "run_finished":
                        status = payload.get("status") or status
                        break
            except asyncio.CancelledError:
                await self.subagent_executor.stop(run_id)
                raise
            return {"run_id": run_id, "status": status, "content": content}
