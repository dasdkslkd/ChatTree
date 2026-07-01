from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Set


ApprovalStatus = Literal["pending", "approved", "denied", "expired", "cancelled"]
ApprovalScope = Literal["once", "session"]
ApprovalAction = Literal["approve", "deny"]


@dataclass
class ApprovalRequest:
    id: str
    conversation_id: str
    node_id: str
    tool_call_id: str
    tool_name: str
    arguments_preview: str
    risk_level: str
    reason: str
    suggested_actions: List[str]
    run_id: Optional[str] = None
    run_kind: Optional[str] = None
    parent_run_id: Optional[str] = None
    root_run_id: Optional[str] = None
    agent_name: Optional[str] = None
    task_summary: Optional[str] = None
    source_label: Optional[str] = None
    created_at: int = field(default_factory=lambda: int(time.time()))
    expires_at: Optional[int] = None
    status: ApprovalStatus = "pending"

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "node_id": self.node_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments_preview": self.arguments_preview,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "suggested_actions": list(self.suggested_actions),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "run_id": self.run_id,
            "run_kind": self.run_kind,
            "parent_run_id": self.parent_run_id,
            "root_run_id": self.root_run_id,
            "agent_name": self.agent_name,
            "task_summary": self.task_summary,
            "source_label": self.source_label,
        }


@dataclass(frozen=True)
class ApprovalDecision:
    status: Literal["approved", "denied", "expired", "cancelled"]
    scope: Optional[ApprovalScope] = None


class ApprovalManager:
    def __init__(self, timeout_seconds: float = 600):
        self.timeout_seconds = timeout_seconds
        self._pending: Dict[str, ApprovalRequest] = {}
        self._futures: Dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._session_allowed_tools: Dict[str, Set[str]] = {}

    def is_session_allowed(self, conversation_id: str, tool_name: str) -> bool:
        return tool_name in self._session_allowed_tools.get(conversation_id, set())

    def begin_request(self, request: ApprovalRequest) -> asyncio.Task[ApprovalDecision]:
        if request.id in self._pending:
            raise ValueError(f"Approval request already pending: {request.id}")

        request.expires_at = int(time.time() + self.timeout_seconds)
        self._pending[request.id] = request
        self._futures[request.id] = asyncio.get_running_loop().create_future()
        return asyncio.create_task(self._wait_for_decision(request.id))

    async def request_and_wait(self, request: ApprovalRequest) -> ApprovalDecision:
        return await self.begin_request(request)

    async def _wait_for_decision(self, approval_id: str) -> ApprovalDecision:
        try:
            return await asyncio.wait_for(
                asyncio.shield(self._futures[approval_id]),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return self._resolve(approval_id, ApprovalDecision("expired"))
        except asyncio.CancelledError:
            self._resolve(approval_id, ApprovalDecision("cancelled"))
            raise
        finally:
            self._pending.pop(approval_id, None)
            self._futures.pop(approval_id, None)

    def decide(
        self,
        approval_id: str,
        decision: ApprovalAction,
        scope: ApprovalScope,
    ) -> ApprovalDecision:
        request = self._pending.get(approval_id)
        if request is None:
            raise KeyError(approval_id)
        if decision == "approve":
            status: Literal["approved", "denied"] = "approved"
            if scope == "session":
                self._session_allowed_tools.setdefault(
                    request.conversation_id,
                    set(),
                ).add(request.tool_name)
        elif decision == "deny":
            status = "denied"
        else:
            raise ValueError(f"Unknown approval decision: {decision}")

        return self._resolve(approval_id, ApprovalDecision(status, scope))

    def cancel_for_node(self, node_id: str) -> None:
        approval_ids = [
            approval_id
            for approval_id, request in self._pending.items()
            if request.node_id == node_id
        ]
        for approval_id in approval_ids:
            self._resolve(approval_id, ApprovalDecision("cancelled"))

    def get(self, approval_id: str) -> Optional[ApprovalRequest]:
        return self._pending.get(approval_id)

    def _resolve(
        self,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> ApprovalDecision:
        request = self._pending.get(approval_id)
        if request is not None:
            request.status = decision.status

        future = self._futures.get(approval_id)
        if future is not None and not future.done():
            future.set_result(decision)
        return decision
