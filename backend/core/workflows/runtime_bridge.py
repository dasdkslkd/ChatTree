from __future__ import annotations

import asyncio
from typing import Any, Dict

from backend.core.agents import AgentSource, SubagentExecutor
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
        agent_runtime: Any = None,
        max_parallel: int = 8,
        permission_mode: str | None = None,
    ) -> None:
        self.workflow_run_id = workflow_run_id
        self.conversation_id = conversation_id
        self.parent_node_id = parent_node_id
        self.run_manager = run_manager
        self.subagent_executor = subagent_executor
        self.agent_runtime = agent_runtime
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
            if self.agent_runtime is not None:
                run = await self.agent_runtime.spawn_agent(
                    source=AgentSource(
                        conversation_id=self.conversation_id,
                        run_id=self.workflow_run_id,
                        run_kind="workflow",
                        anchor_node_id=options.get("parent_node_id") or self.parent_node_id,
                        root_run_id=self.workflow_run_id,
                        task_summary=str(input_data)[:160],
                    ),
                    agent_name=agent_name,
                    task=input_data if isinstance(input_data, str) else str(input_data),
                    context_mode=str(options.get("context_mode") or "fresh"),
                    delivery_policy=str(options.get("delivery") or "silent"),
                    created_by_run_id=self.workflow_run_id,
                    cancellation_parent_run_id=self.workflow_run_id,
                    provider_id=options.get("provider_id"),
                    model_id=options.get("model_id"),
                    permission_mode=options.get("permission_mode") or self.permission_mode,
                    workspace=options.get("workspace"),
                )
            else:
                run = await self.subagent_executor.start(
                    conversation_id=self.conversation_id,
                    agent_name=agent_name,
                    input_data=input_data,
                    parent_node_id=options.get("parent_node_id") or self.parent_node_id,
                    created_by_run_id=self.workflow_run_id,
                    cancellation_parent_run_id=self.workflow_run_id,
                    provider_id=options.get("provider_id"),
                    model_id=options.get("model_id"),
                    permission_mode=options.get("permission_mode") or self.permission_mode,
                    workspace=options.get("workspace"),
                    delivery_policy=str(options.get("delivery") or "silent"),
                )
            run_id = str(run["run_id"])
            try:
                result = await self.run_manager.wait_for_terminal_result(
                    run_id,
                    result_event_types={"subagent_result"},
                    error_event_types={"subagent_error"},
                )
            except asyncio.CancelledError:
                await self.subagent_executor.stop(run_id)
                raise
            return {
                "run_id": run_id,
                "status": result.get("status") or RunStatus.COMPLETED.value,
                "content": result.get("content") or "",
            }
