from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.runs import RunKind, RunManager
from backend.core.tasks import TaskLedger, TaskOwnerType
from backend.core.tools.security.permissions import normalize_permission_mode

from .mailbox import AgentMailbox
from .subagent_executor import SubagentExecutor
from .types import AgentSource


class AgentRuntime:
    """Backend-owned control plane for model-facing agent tools."""

    def __init__(
        self,
        *,
        run_manager: RunManager,
        mailbox: AgentMailbox,
        subagent_executor: SubagentExecutor,
        workflow_manager: Any = None,
        capability_registry: CapabilityRegistry,
        task_ledger: TaskLedger | None = None,
    ) -> None:
        self.run_manager = run_manager
        self.mailbox = mailbox
        self.subagent_executor = subagent_executor
        self.workflow_manager = workflow_manager
        self.capability_registry = capability_registry
        self.task_ledger = task_ledger

    async def spawn_agent(
        self,
        *,
        source: AgentSource,
        agent_name: str,
        task: str,
        context_mode: str = "fresh",
        delivery_policy: str = "auto",
        parent_run_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        permission_mode: Optional[str] = None,
        workspace: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        auto_create_task: bool = True,
    ) -> Dict[str, Any]:
        agent_name = (agent_name or "implementer").strip() or "implementer"
        task = (task or "").strip()
        if not task:
            raise ValueError("task is required")
        if self.capability_registry.get_agent(agent_name) is None:
            raise KeyError(agent_name)
        await self._validate_existing_task(source.conversation_id, task_id)
        run = await self.subagent_executor.start(
            conversation_id=source.conversation_id,
            agent_name=agent_name,
            input_data=task,
            parent_node_id=source.anchor_node_id,
            parent_run_id=parent_run_id or source.run_id,
            context_mode=context_mode,
            provider_id=provider_id,
            model_id=model_id,
            permission_mode=normalize_permission_mode(permission_mode),
            workspace=workspace,
            delegated_task=task,
            original_slash_input=None,
            delivery_policy=delivery_policy,
            task_id=task_id,
        )
        run_id = str(run.get("run_id") or "")
        task_id = await self._bind_task_for_run(
            source=source,
            run_id=run_id,
            owner_type=TaskOwnerType.SUBAGENT,
            task_id=task_id,
            auto_create_task=auto_create_task,
            title=task,
        )
        metadata = dict(run.get("metadata") or {})
        metadata.update({
            "agent_name": agent_name,
            "task": task,
            "context_mode": context_mode,
            "delivery_policy": delivery_policy,
            "root_run_id": source.root_run_id or source.run_id,
            "source_run_id": source.run_id,
        })
        if task_id:
            metadata["task_id"] = task_id
        await self.run_manager.update_metadata(run_id, metadata)
        return {
            "run_id": run_id,
            "kind": run.get("kind", RunKind.SUBAGENT.value),
            "status": run.get("status"),
            "agent_name": agent_name,
            "task": task,
            "task_id": task_id,
            "context_mode": context_mode,
            "delivery_policy": delivery_policy,
            "message": "Agent spawned.",
        }

    async def start_workflow(
        self,
        *,
        source: AgentSource,
        script: str,
        args: Optional[Dict[str, Any]] = None,
        delivery_policy: str = "auto",
        permission_mode: Optional[str] = None,
        task_id: Optional[str] = None,
        auto_create_task: bool = True,
    ) -> Dict[str, Any]:
        if self.workflow_manager is None:
            raise RuntimeError("workflow manager is not configured")
        await self._validate_existing_task(source.conversation_id, task_id)
        run = await self.workflow_manager.start(
            conversation_id=source.conversation_id,
            script=script,
            args=args or {},
            parent_node_id=source.anchor_node_id,
            parent_run_id=source.run_id,
            permission_mode=normalize_permission_mode(permission_mode),
            delegated_task=script,
            delivery_policy=delivery_policy,
            task_id=task_id,
        )
        run_id = str(run.get("run_id") or "")
        task_id = await self._bind_task_for_run(
            source=source,
            run_id=run_id,
            owner_type=TaskOwnerType.WORKFLOW,
            task_id=task_id,
            auto_create_task=auto_create_task,
            title=script,
        )
        metadata = {
                "delivery_policy": delivery_policy,
                "root_run_id": source.root_run_id or source.run_id,
                "source_run_id": source.run_id,
        }
        if task_id:
            metadata["task_id"] = task_id
        await self.run_manager.update_metadata(run_id, metadata)
        run["task_id"] = task_id
        return run

    async def _bind_task_for_run(
        self,
        *,
        source: AgentSource,
        run_id: str,
        owner_type: TaskOwnerType,
        task_id: Optional[str],
        auto_create_task: bool,
        title: str,
    ) -> Optional[str]:
        if self.task_ledger is None or not run_id:
            return task_id
        resolved_task_id = str(task_id or "").strip()
        if not resolved_task_id and auto_create_task:
            task = await self.task_ledger.create_task(
                conversation_id=source.conversation_id,
                title=(title or owner_type.value)[:160],
                detail=title or "",
                created_by_run_id=source.run_id,
                owner_type=owner_type,
                metadata={
                    "source_run_id": source.run_id,
                    "source_run_kind": source.run_kind,
                },
            )
            resolved_task_id = task.task_id
        if resolved_task_id:
            await self.task_ledger.bind_run(
                conversation_id=source.conversation_id,
                task_id=resolved_task_id,
                run_id=run_id,
                owner_type=owner_type,
            )
        return resolved_task_id or None

    async def _validate_existing_task(self, conversation_id: str, task_id: Optional[str]) -> None:
        if self.task_ledger is None:
            return
        resolved_task_id = str(task_id or "").strip()
        if not resolved_task_id:
            return
        task = await self.task_ledger.get_task(conversation_id, resolved_task_id)
        if task is None:
            raise KeyError(resolved_task_id)

    async def wait_agent(
        self,
        *,
        source: AgentSource,
        run_ids: list[str],
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        async def wait_one(run_id: str) -> Dict[str, Any]:
            run = self.run_manager.get_run(run_id)
            if not run:
                return {
                    "run_id": run_id,
                    "status": "missing",
                    "message_type": "error",
                    "content": "",
                    "result": None,
                    "error": "run not found",
                }
            try:
                terminal = await self.run_manager.wait_for_terminal_result(
                    run_id,
                    result_event_types={"subagent_result", "workflow_result"},
                    error_event_types={"subagent_error", "workflow_error"},
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                current = self.run_manager.get_run(run_id) or run
                return {
                    "run_id": run_id,
                    "kind": current.get("kind"),
                    "status": current.get("status") or "timeout",
                    "agent_name": (current.get("metadata") or {}).get("agent_name"),
                    "message_type": "timeout",
                    "content": "",
                    "result": None,
                    "error": None,
                    "timed_out": True,
                }
            current = self.run_manager.get_run(run_id) or run
            await self.run_manager.mark_observed(
                run_id,
                observer_run_id=source.run_id,
                via="wait_agent",
            )
            terminal["agent_name"] = (current.get("metadata") or {}).get("agent_name")
            terminal["task_id"] = (current.get("metadata") or {}).get("task_id")
            terminal["timed_out"] = False
            return terminal

        runs = list(await asyncio.gather(*(wait_one(str(run_id)) for run_id in run_ids)))
        if any(run.get("timed_out") for run in runs):
            status = "timeout"
        elif any(run.get("status") == "missing" for run in runs):
            status = "error"
        elif all(run.get("message_type") != "error" for run in runs):
            status = "completed"
        else:
            status = "completed"
        return {"status": status, "runs": runs}

    async def list_agents(
        self,
        *,
        conversation_id: str,
        parent_run_id: Optional[str] = None,
        include_completed: bool = True,
    ) -> Dict[str, Any]:
        runs = []
        for run in self.run_manager.list_runs(conversation_id):
            if run.get("kind") not in {RunKind.SUBAGENT.value, RunKind.WORKFLOW.value, RunKind.WORKFLOW_STEP.value}:
                continue
            if parent_run_id and run.get("parent_run_id") != parent_run_id:
                continue
            if not include_completed and run.get("status") in {"completed", "failed", "cancelled"}:
                continue
            runs.append(run)
        return {"runs": runs}

    async def send_message(self, *, source: AgentSource, run_id: str, message: str) -> Dict[str, Any]:
        item = await self.mailbox.publish(
            conversation_id=source.conversation_id,
            source_run_id=run_id,
            source_run_kind=RunKind.SUBAGENT.value,
            message_type="user_input",
            content=message,
            metadata={"from_run_id": source.run_id},
            delivery_policy="silent",
        )
        return {"message_id": item.message_id, "status": "queued"}

    async def send_input(self, *, source: AgentSource, run_id: str, input_data: Any) -> Dict[str, Any]:
        return await self.send_message(source=source, run_id=run_id, message=json.dumps(input_data, ensure_ascii=False))

    async def followup_task(self, *, source: AgentSource, run_id: str, task: str) -> Dict[str, Any]:
        item = await self.mailbox.publish(
            conversation_id=source.conversation_id,
            source_run_id=run_id,
            source_run_kind=RunKind.SUBAGENT.value,
            message_type="followup",
            content=task,
            metadata={"from_run_id": source.run_id},
            delivery_policy="notify",
        )
        return {"message_id": item.message_id, "status": "queued"}

    async def resume_agent(self, *, run_id: str) -> Dict[str, Any]:
        run = self.run_manager.get_run(run_id)
        return {"run_id": run_id, "status": run.get("status") if run else "missing"}

    async def close_agent(self, *, run_id: str) -> Dict[str, Any]:
        await self._stop_owned_run(run_id)
        return {"run_id": run_id, "status": "close_requested"}

    async def interrupt_agent(self, *, run_id: str) -> Dict[str, Any]:
        await self._stop_owned_run(run_id)
        return {"run_id": run_id, "status": "interrupt_requested"}

    async def _stop_owned_run(self, run_id: str) -> bool:
        run = self.run_manager.get_run(run_id) or {}
        kind = run.get("kind")
        if kind in {RunKind.SUBAGENT.value, RunKind.WORKFLOW_STEP.value} and self.subagent_executor is not None:
            return bool(await self.subagent_executor.stop(run_id))
        if kind == RunKind.WORKFLOW.value and self.workflow_manager is not None:
            return bool(await self.workflow_manager.stop(run_id))
        return bool(await self.run_manager.request_stop(run_id))
