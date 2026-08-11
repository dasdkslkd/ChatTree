from __future__ import annotations

import asyncio
from time import time
from typing import Any, Optional, Sequence

from backend.core.tools.security.permissions import normalize_permission_mode

from .types import PlanSession, PlanStatus


class PlanLedgerError(Exception):
    pass


class PlanNotFoundError(PlanLedgerError):
    pass


class PlanLedger:
    """Persistent per-conversation plan-mode ledger."""

    def __init__(self, *, repository) -> None:
        if repository is None:
            raise ValueError("PlanLedger repository is required")
        self._repository = repository
        self._lock = asyncio.Lock()

    async def enter_plan_mode(
        self,
        *,
        conversation_id: str,
        node_id: Optional[str] = None,
        previous_permission_mode: str = "modify_only",
        run_id: Optional[str] = None,
    ) -> PlanSession:
        if not conversation_id:
            raise ValueError("conversation_id is required")
        previous_mode = normalize_permission_mode(previous_permission_mode)
        if previous_mode == "plan":
            previous_mode = "modify_only"
        async with self._lock:
            current = self._repository.get_active_or_awaiting(conversation_id)
            if current is not None:
                return self._session_from_record(current)
            plan_id = self._repository.create_plan(
                conversation_id,
                entered_node_id=node_id,
                previous_permission_mode=previous_mode,
                entered_run_id=run_id,
            )
            created = self._repository.get_plan(conversation_id, plan_id)
            if created is None:
                raise PlanNotFoundError(plan_id)
            return self._session_from_record(created)

    async def ask_user_question(
        self,
        *,
        conversation_id: str,
        questions: Sequence[dict[str, Any]],
        node_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> PlanSession:
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not questions:
            raise ValueError("questions is required")
        async with self._lock:
            record = self._repository.get_active_or_awaiting(conversation_id)
            if record is None or record.get("status") not in (
                PlanStatus.ACTIVE.value,
                PlanStatus.AWAITING_QUESTION.value,
            ):
                raise ValueError("active plan session is required")
            updated = self._repository.ask_question(
                conversation_id,
                record["plan_id"],
                tool_call_id=tool_call_id,
                asked_node_id=node_id,
                run_id=run_id,
            )
            return self._session_from_record(updated)

    async def exit_plan_mode(
        self,
        *,
        conversation_id: str,
        node_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> PlanSession:
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not tool_call_id:
            raise ValueError("tool_call_id is required")
        async with self._lock:
            record = self._repository.get_active_or_awaiting(conversation_id)
            if record is None or record.get("status") not in (
                PlanStatus.ACTIVE.value,
                PlanStatus.AWAITING_APPROVAL.value,
            ):
                raise ValueError("active plan session is required")
            updated = self._repository.await_approval(
                conversation_id,
                record["plan_id"],
                tool_call_id=tool_call_id,
                node_id=node_id,
                run_id=run_id,
            )
            return self._session_from_record(updated)

    async def answer_question(
        self,
        *,
        conversation_id: str,
        plan_id: str,
        answers: Sequence[str],
    ) -> PlanSession:
        if not answers:
            raise ValueError("answers is required")
        async with self._lock:
            record = self._repository.get_plan(conversation_id, plan_id)
            if record is None:
                raise PlanNotFoundError(plan_id)
            if record.get("status") != PlanStatus.AWAITING_QUESTION.value:
                raise ValueError("plan must be awaiting question")
            updated_record = self._repository.answer_question(
                conversation_id,
                plan_id,
            )
            return self._session_from_record(updated_record)

    async def approve_plan(
        self,
        *,
        conversation_id: str,
        plan_id: str,
        approved_run_id: Optional[str] = None,
    ) -> PlanSession:
        async with self._lock:
            record = self._repository.get_plan(conversation_id, plan_id)
            if record is None:
                raise PlanNotFoundError(plan_id)
            if record.get("status") != PlanStatus.AWAITING_APPROVAL.value:
                raise ValueError("plan must be awaiting approval")
            updated_record = self._repository.approve_plan(
                conversation_id,
                plan_id,
                approved_run_id=approved_run_id,
            )
            return self._session_from_record(updated_record)

    async def update_approved_run_id(
        self,
        *,
        conversation_id: str,
        plan_id: str,
        approved_run_id: Optional[str],
    ) -> PlanSession:
        async with self._lock:
            record = self._repository.update_approved_run_id(
                conversation_id,
                plan_id,
                approved_run_id=approved_run_id,
            )
            return self._session_from_record(record)

    async def reject_plan(self, *, conversation_id: str, plan_id: str, feedback: str = "") -> PlanSession:
        async with self._lock:
            record = self._repository.get_plan(conversation_id, plan_id)
            if record is None:
                raise PlanNotFoundError(plan_id)
            if record.get("status") != PlanStatus.AWAITING_APPROVAL.value:
                raise ValueError("plan must be awaiting approval")
            updated_record = self._repository.reject_plan(
                conversation_id,
                plan_id,
                feedback=feedback,
            )
            return self._session_from_record(updated_record)

    async def get_plan(self, conversation_id: str, plan_id: str) -> Optional[PlanSession]:
        async with self._lock:
            record = self._repository.get_plan(conversation_id, plan_id)
            return self._session_from_record(record) if record else None

    async def get_active_or_awaiting(self, conversation_id: str) -> Optional[PlanSession]:
        async with self._lock:
            record = self._repository.get_active_or_awaiting(conversation_id)
            return self._session_from_record(record) if record else None

    async def get_latest(self, conversation_id: str) -> Optional[PlanSession]:
        async with self._lock:
            record = self._repository.get_latest(conversation_id)
            return self._session_from_record(record) if record else None

    def _session_from_record(self, record: dict[str, Any]) -> PlanSession:
        return PlanSession.from_dict({
            "plan_id": record.get("plan_id") or record.get("id"),
            "conversation_id": record.get("conversation_id"),
            "status": record.get("status", PlanStatus.ACTIVE.value),
            "previous_permission_mode": record.get("previous_permission_mode", "modify_only"),
            "entered_node_id": record.get("entered_node_id"),
            "entered_run_id": record.get("entered_run_id"),
            "approved_run_id": record.get("approved_run_id"),
            "question_tool_call_id": record.get("question_tool_call_id"),
            "exit_tool_call_id": record.get("exit_tool_call_id"),
            "blocking_node_id": record.get("blocking_node_id"),
            "blocking_run_id": record.get("blocking_run_id"),
            "approved_at": record.get("approved_at"),
            "rejected_at": record.get("rejected_at"),
            "created_at": record.get("created_at", time()),
            "updated_at": record.get("updated_at", time()),
        })
