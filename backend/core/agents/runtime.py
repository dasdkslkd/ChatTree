from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.runs import (
    FINISHED_RUN_STATUSES,
    RunIdempotency,
    RunKind,
    RunManager,
    RunRecord,
    RunStartResult,
    RunStartValidationError,
)
from backend.core.tasks import ActiveTaskService
from backend.core.tools.security.permissions import normalize_permission_mode

from .mailbox import AgentMailbox
from .subagent_executor import SubagentExecutor
from .types import AgentDeliveryPolicy, AgentSource


FINISHED_AGENT_STATUS_VALUES = {status.value for status in FINISHED_RUN_STATUSES}


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
        task_service: ActiveTaskService | None = None,
        default_wait_timeout_seconds: float = 30.0,
    ) -> None:
        self.run_manager = run_manager
        self.mailbox = mailbox
        self.subagent_executor = subagent_executor
        self.workflow_manager = workflow_manager
        self.capability_registry = capability_registry
        self.task_service = task_service
        self.default_wait_timeout_seconds = default_wait_timeout_seconds

    async def spawn_agent(
        self,
        *,
        source: AgentSource,
        agent_name: str,
        task: str,
        context_mode: str = "fresh",
        delivery_policy: str = "auto",
        created_by_run_id: Optional[str] = None,
        cancellation_parent_run_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        permission_mode: Optional[str] = None,
        workspace: Optional[Dict[str, Any]] = None,
        step: Optional[int] = None,
        task_context_mode: str = "attached",
        task_generation_id: Optional[str] = None,
        task_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        agent_name = (agent_name or "implementer").strip() or "implementer"
        task = (task or "").strip()
        if not task:
            raise ValueError("task is required")
        task_binding = await self._prepare_task_binding(
            source=source,
            step=step,
            task_context_mode=task_context_mode,
            task_generation_id=task_generation_id,
            task_revision=task_revision,
        )
        normalized_context = self._normalize_context_mode(context_mode)
        normalized_delivery = AgentDeliveryPolicy(
            str(delivery_policy or "auto")
        ).value
        source_run_id = self._normalize_optional_id(source.run_id)
        lineage_run_id = created_by_run_id or source_run_id
        runtime_metadata = self._agent_runtime_metadata(
            source=source,
            source_run_id=source_run_id,
            agent_name=agent_name,
            task_text=task,
            context_mode=normalized_context,
            delivery_policy=normalized_delivery,
        )
        run = await self.subagent_executor.start(
            conversation_id=source.conversation_id,
            agent_name=agent_name,
            input_data=task,
            parent_node_id=source.anchor_node_id,
            created_by_run_id=lineage_run_id,
            cancellation_parent_run_id=cancellation_parent_run_id,
            context_mode=normalized_context,
            provider_id=provider_id,
            model_id=model_id,
            permission_mode=normalize_permission_mode(permission_mode),
            workspace=workspace,
            delegated_task=task,
            original_slash_input=None,
            delivery_policy=normalized_delivery,
            task_binding=task_binding,
            runtime_metadata=runtime_metadata,
        )
        run_id = str(run.get("run_id") or "")
        return {
            "run_id": run_id,
            "kind": run.get("kind", RunKind.SUBAGENT.value),
            "status": run.get("status"),
            "agent_name": agent_name,
            "task": task,
            "step": step,
            "context_mode": normalized_context,
            "delivery_policy": normalized_delivery,
            "message": "Agent spawned.",
        }

    async def spawn_agent_idempotent(
        self,
        *,
        source: AgentSource,
        agent_name: str,
        input_data: Any,
        idempotency: RunIdempotency,
        request_id: str,
        context_mode: str = "fresh",
        delivery_policy: str = "auto",
        created_by_run_id: Optional[str] = None,
        cancellation_parent_run_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        permission_mode: Optional[str] = None,
        workspace: Optional[Dict[str, Any]] = None,
        step: Optional[int] = None,
        task_context_mode: str = "attached",
        task_generation_id: Optional[str] = None,
        task_revision: Optional[int] = None,
        winner_anchor_factory: Callable[[RunRecord], Awaitable[str | None]] | None = None,
    ) -> RunStartResult:
        normalized_agent_name = (
            (agent_name or "implementer").strip() or "implementer"
        )
        if isinstance(input_data, str) and not input_data.strip():
            raise RunStartValidationError("input is required")
        task_text = self._render_task_text(input_data)
        coordinator = self.subagent_executor.run_start_coordinator
        if coordinator is not None:
            replay = await coordinator.replay_existing(idempotency)
            if replay is not None:
                return replay
        try:
            task_binding = await self._prepare_task_binding(
                source=source,
                step=step,
                task_context_mode=task_context_mode,
                task_generation_id=task_generation_id,
                task_revision=task_revision,
            )
        except Exception:
            if coordinator is not None:
                replay = await coordinator.replay_existing(idempotency)
                if replay is not None:
                    return replay
            raise
        normalized_context = self._normalize_context_mode(context_mode)
        normalized_delivery = AgentDeliveryPolicy(
            str(delivery_policy or "auto")
        ).value
        source_run_id = self._normalize_optional_id(source.run_id)
        lineage_run_id = created_by_run_id or source_run_id
        runtime_metadata = self._agent_runtime_metadata(
            source=source,
            source_run_id=source_run_id,
            agent_name=normalized_agent_name,
            task_text=task_text,
            context_mode=normalized_context,
            delivery_policy=normalized_delivery,
        )
        return await self.subagent_executor.start_idempotent(
            conversation_id=source.conversation_id,
            agent_name=normalized_agent_name,
            input_data=input_data,
            parent_node_id=source.anchor_node_id,
            created_by_run_id=lineage_run_id,
            cancellation_parent_run_id=cancellation_parent_run_id,
            provider_id=provider_id,
            model_id=model_id,
            permission_mode=normalize_permission_mode(permission_mode),
            workspace=workspace,
            delegated_task=input_data,
            original_slash_input=None,
            delivery_policy=normalized_delivery,
            context_mode=normalized_context,
            task_binding=task_binding,
            idempotency=idempotency,
            request_id=request_id,
            winner_anchor_factory=winner_anchor_factory,
            runtime_metadata=runtime_metadata,
        )

    async def _prepare_task_binding(
        self,
        *,
        source: AgentSource,
        step: Optional[int],
        task_context_mode: str,
        task_generation_id: Optional[str],
        task_revision: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        if step is None:
            return None
        if self.task_service is None:
            raise RuntimeError("task service is not configured")
        return await self.task_service.prepare_run_binding(
            conversation_id=source.conversation_id,
            step=step,
            context_mode=task_context_mode,
            expected_generation=task_generation_id,
            expected_revision=task_revision,
        )

    @staticmethod
    def _normalize_optional_id(value: Any) -> Optional[str]:
        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def _normalize_context_mode(value: Any) -> str:
        normalized = str(value or "fresh")
        return normalized if normalized in {"fresh", "fork"} else "fresh"

    @staticmethod
    def _render_task_text(input_data: Any) -> str:
        if isinstance(input_data, str):
            return input_data.strip()
        try:
            return json.dumps(
                input_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise RunStartValidationError(
                "agent input must contain finite JSON values"
            ) from exc

    def _agent_runtime_metadata(
        self,
        *,
        source: AgentSource,
        source_run_id: Optional[str],
        agent_name: str,
        task_text: str,
        context_mode: str,
        delivery_policy: str,
    ) -> Dict[str, Any]:
        return {
            "agent_name": agent_name,
            "task": task_text,
            "context_mode": context_mode,
            "delivery_policy": delivery_policy,
            "root_run_id": self._normalize_optional_id(source.root_run_id)
            or source_run_id,
            "source_run_id": source_run_id,
        }

    async def start_workflow(
        self,
        *,
        source: AgentSource,
        script: str,
        args: Optional[Dict[str, Any]] = None,
        delivery_policy: str = "auto",
        permission_mode: Optional[str] = None,
        step: Optional[int] = None,
        task_context_mode: str = "attached",
        task_generation_id: Optional[str] = None,
        task_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        if self.workflow_manager is None:
            raise RuntimeError("workflow manager is not configured")
        task_binding = None
        if step is not None:
            if self.task_service is None:
                raise RuntimeError("task service is not configured")
            task_binding = await self.task_service.prepare_run_binding(
                conversation_id=source.conversation_id,
                step=step,
                context_mode=task_context_mode,
                expected_generation=task_generation_id,
                expected_revision=task_revision,
            )
        run = await self.workflow_manager.start(
            conversation_id=source.conversation_id,
            script=script,
            args=args or {},
            parent_node_id=source.anchor_node_id,
            created_by_run_id=source.run_id,
            cancellation_parent_run_id=None,
            permission_mode=normalize_permission_mode(permission_mode),
            delegated_task=script,
            delivery_policy=delivery_policy,
            task_binding=task_binding,
        )
        run_id = str(run.get("run_id") or "")
        metadata = {
                "delivery_policy": delivery_policy,
                "root_run_id": source.root_run_id or source.run_id,
                "source_run_id": source.run_id,
        }
        await self.run_manager.update_metadata(run_id, metadata)
        run["step"] = step
        return run

    def _compact_event_for_status(self, event: Dict[str, Any] | None) -> Optional[Dict[str, Any]]:
        if not event:
            return None
        compact: Dict[str, Any] = {}
        for key in ("event_index", "type", "event_type", "status"):
            if event.get(key) is not None:
                compact[key] = event.get(key)
        tool_name = None
        tool_call = event.get("tool_call")
        if isinstance(tool_call, dict):
            tool_name = tool_call.get("name")
        if tool_name is None:
            tool_calls = event.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                first_call = tool_calls[0]
                if isinstance(first_call, dict):
                    function = first_call.get("function")
                    if isinstance(function, dict):
                        tool_name = function.get("name")
        if tool_name:
            compact["tool_name"] = tool_name
        content = event.get("content")
        if isinstance(content, str) and content:
            compact["content_preview"] = content[:500]
        error = event.get("error")
        if error:
            compact["error"] = str(error)[:500]
        return compact

    def _run_progress_snapshot(self, run_id: str, run: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        current = run or self.run_manager.get_run(run_id) or {}
        try:
            events = self.run_manager.read_events(run_id)
        except Exception:
            events = []
        created_at = current.get("created_at")
        updated_at = current.get("updated_at")
        snapshot: Dict[str, Any] = {
            "run_id": run_id,
            "kind": current.get("kind"),
            "status": current.get("status") or "missing",
            "agent_name": (current.get("metadata") or {}).get("agent_name"),
            "step": (current.get("metadata") or {}).get("task_step_position"),
            "event_count": current.get("event_count") if current.get("event_count") is not None else len(events),
            "created_at": created_at,
            "updated_at": updated_at,
            "elapsed_seconds": max(0.0, time.time() - float(created_at)) if created_at else None,
            "last_activity_at": updated_at,
            "last_event": self._compact_event_for_status(events[-1] if events else None),
        }
        return {key: value for key, value in snapshot.items() if value is not None}

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
                snapshot = self._run_progress_snapshot(run_id, current)
                snapshot.update({
                    "wait_status": "timeout",
                    "message_type": "in_progress",
                    "content": "",
                    "result": None,
                    "error": None,
                    "timed_out": True,
                    "message": "wait_agent reached its wait timeout; the run is still active unless status is terminal.",
                })
                return snapshot
            current = self.run_manager.get_run(run_id) or run
            await self.run_manager.mark_observed(
                run_id,
                observer_run_id=source.run_id,
                via="wait_agent",
            )
            terminal["agent_name"] = (current.get("metadata") or {}).get("agent_name")
            terminal["step"] = (current.get("metadata") or {}).get("task_step_position")
            task_outcome = (current.get("metadata") or {}).get("task_outcome")
            if isinstance(task_outcome, dict):
                terminal["task_outcome"] = dict(task_outcome)
            terminal["wait_status"] = "completed"
            terminal["timed_out"] = False
            return terminal

        runs = list(await asyncio.gather(*(wait_one(str(run_id)) for run_id in run_ids)))
        if any(run.get("wait_status") == "timeout" for run in runs):
            if any(run.get("status") in {"running", "waiting", "stopping"} for run in runs):
                status = "running"
            else:
                status = "timeout"
            wait_status = "timeout"
        elif any(run.get("status") == "missing" for run in runs):
            status = "error"
            wait_status = "completed"
        elif all(run.get("message_type") != "error" for run in runs):
            status = "completed"
            wait_status = "completed"
        else:
            status = "completed"
            wait_status = "completed"
        return {"status": status, "wait_status": wait_status, "runs": runs}

    async def list_agents(
        self,
        *,
        conversation_id: str,
        created_by_run_id: Optional[str] = None,
        include_completed: bool = True,
    ) -> Dict[str, Any]:
        runs = []
        for run in self.run_manager.list_runs(conversation_id):
            if run.get("kind") not in {RunKind.SUBAGENT.value, RunKind.WORKFLOW.value, RunKind.WORKFLOW_STEP.value}:
                continue
            if created_by_run_id and run.get("created_by_run_id") != created_by_run_id:
                continue
            if not include_completed and run.get("status") in FINISHED_AGENT_STATUS_VALUES:
                continue
            runs.append(self._public_agent_run(run))
        return {"runs": runs}

    def _public_agent_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        metadata = dict(run.get("metadata") or {})
        delegated_task = metadata.get("delegated_task") or metadata.get("task")
        result = {
            "run_id": run.get("run_id"),
            "kind": run.get("kind"),
            "status": run.get("status"),
            "agent_name": metadata.get("agent_name"),
            "summary": run.get("summary"),
            "task": str(delegated_task)[:500] if delegated_task is not None else None,
            "step": metadata.get("task_step_position"),
            "delivery": metadata.get("delivery_policy"),
            "event_count": run.get("event_count"),
            "created_at": run.get("created_at"),
            "updated_at": run.get("updated_at"),
            "finished_at": run.get("finished_at"),
            "error": metadata.get("error"),
            "task_outcome": metadata.get("task_outcome"),
        }
        return {key: value for key, value in result.items() if value is not None}

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
        return self._run_progress_snapshot(run_id, run)

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
