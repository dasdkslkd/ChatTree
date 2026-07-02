from __future__ import annotations

import json
from typing import Any, Dict, Optional

from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.runs import RunKind, RunManager
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
    ) -> None:
        self.run_manager = run_manager
        self.mailbox = mailbox
        self.subagent_executor = subagent_executor
        self.workflow_manager = workflow_manager
        self.capability_registry = capability_registry
        self.run_manager.add_finish_listener(self._handle_run_finished)

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
    ) -> Dict[str, Any]:
        agent_name = (agent_name or "implementer").strip() or "implementer"
        task = (task or "").strip()
        if not task:
            raise ValueError("task is required")
        if self.capability_registry.get_agent(agent_name) is None:
            raise KeyError(agent_name)
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
        await self.run_manager.update_metadata(str(run.get("run_id") or ""), metadata)
        return {
            "run_id": run.get("run_id"),
            "kind": run.get("kind", RunKind.SUBAGENT.value),
            "status": run.get("status"),
            "agent_name": agent_name,
            "task": task,
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
    ) -> Dict[str, Any]:
        if self.workflow_manager is None:
            raise RuntimeError("workflow manager is not configured")
        run = await self.workflow_manager.start(
            conversation_id=source.conversation_id,
            script=script,
            args=args or {},
            parent_node_id=source.anchor_node_id,
            parent_run_id=source.run_id,
            permission_mode=normalize_permission_mode(permission_mode),
            delegated_task=script,
            delivery_policy=delivery_policy,
        )
        await self.run_manager.update_metadata(str(run.get("run_id") or ""), {
                "delivery_policy": delivery_policy,
                "root_run_id": source.root_run_id or source.run_id,
                "source_run_id": source.run_id,
        })
        return run

    async def wait_agent(
        self,
        *,
        source: AgentSource,
        run_ids: list[str],
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        runs = []
        for run_id in run_ids:
            messages = await self.mailbox.wait_for_run(
                conversation_id=source.conversation_id,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
            )
            run = self.run_manager.get_run(run_id) or {}
            if not messages and run.get("status") in {"completed", "failed", "cancelled"}:
                messages = self._result_messages_from_run_journal(source.conversation_id, run_id)
            for message in messages:
                message_id = str(message.get("message_id") or "")
                if message_id:
                    await self.mailbox.mark_integrated(source.conversation_id, message_id)
            runs.append({
                "run_id": run_id,
                "status": run.get("status") or ("completed" if messages else "timeout"),
                "agent_name": (run.get("metadata") or {}).get("agent_name"),
                "messages": messages,
            })
        return {"status": "completed" if any(run["messages"] for run in runs) else "timeout", "runs": runs}

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
            delivery_policy="wait",
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

    def _result_messages_from_run_journal(self, conversation_id: str, run_id: str) -> list[Dict[str, Any]]:
        messages: list[Dict[str, Any]] = []
        for event in self.run_manager.journal.read_events(conversation_id, run_id):
            payload = dict(event.get("payload") or {})
            event_type = str(payload.get("event_type") or "")
            if event_type in {"subagent_result", "workflow_result"}:
                messages.append({
                    "conversation_id": conversation_id,
                    "source_run_id": run_id,
                    "source_run_kind": payload.get("kind") or (self.run_manager.get_run(run_id) or {}).get("kind"),
                    "message_type": "result",
                    "content": payload.get("content") or "",
                    "metadata": {
                        "origin": "run_journal",
                        "event_type": event_type,
                        "agent_name": payload.get("agent_name"),
                    },
                    "created_at": event.get("created_at"),
                })
            elif event_type in {"subagent_error", "workflow_error"}:
                messages.append({
                    "conversation_id": conversation_id,
                    "source_run_id": run_id,
                    "source_run_kind": payload.get("kind") or (self.run_manager.get_run(run_id) or {}).get("kind"),
                    "message_type": "error",
                    "content": payload.get("error") or payload.get("content") or "",
                    "metadata": {
                        "origin": "run_journal",
                        "event_type": event_type,
                        "agent_name": payload.get("agent_name"),
                    },
                    "created_at": event.get("created_at"),
                })
        return messages

    def _handle_run_finished(self, run: Dict[str, Any]) -> None:
        if run.get("kind") not in {RunKind.SUBAGENT.value, RunKind.WORKFLOW.value}:
            return
        # Executors still enqueue legacy synthetic inputs; mailbox publishing is
        # handled there once the runtime is wired in a follow-up patch. This
        # listener intentionally stays side-effect-free for now to avoid
        # duplicate result publication from event replay.
        return
