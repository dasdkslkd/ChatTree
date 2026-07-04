from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from time import time
from typing import Any, Dict, Iterable, Optional, Sequence

from backend.core.tools.security.permissions import normalize_permission_mode

from .types import ACTIVE_PLAN_STATUSES, PlanContextInjection, PlanProposal, PlanSession, PlanStatus


class PlanLedgerError(Exception):
    pass


class PlanNotFoundError(PlanLedgerError):
    pass


class PlanLedger:
    """Process-local per-conversation plan-mode ledger."""

    def __init__(self, repository=None) -> None:
        self._repository = repository
        self._plans_by_conversation: Dict[str, Dict[str, PlanSession]] = {}
        self._pending_context_by_conversation: Dict[str, list[PlanContextInjection]] = {}
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
        now = time()
        async with self._lock:
            if self._repository is not None:
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
            current = self._active_or_awaiting_locked(conversation_id)
            if current is not None:
                return deepcopy(current)
            record = PlanSession(
                plan_id=f"plan_{uuid.uuid4().hex}",
                conversation_id=conversation_id,
                previous_permission_mode=previous_mode,
                entered_node_id=node_id,
                entered_run_id=run_id,
                created_at=now,
                updated_at=now,
            )
            self._plans_by_conversation.setdefault(conversation_id, {})[record.plan_id] = record
            return deepcopy(record)

    async def submit_plan(
        self,
        *,
        conversation_id: str,
        plan: str,
        node_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> PlanSession:
        plan_text = str(plan or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not plan_text:
            raise ValueError("plan is required")
        async with self._lock:
            if self._repository is not None:
                record = self._repository.get_active_or_awaiting(conversation_id)
                if record is None or record.get("status") not in (
                    PlanStatus.ACTIVE.value,
                    PlanStatus.AWAITING_APPROVAL.value,
                ):
                    raise ValueError("active plan session is required")
                updated = self._repository.submit_plan(
                    conversation_id,
                    record["plan_id"],
                    plan=plan_text,
                    submitted_node_id=node_id,
                    submitted_run_id=run_id,
                    tool_call_id=tool_call_id,
                )
                return self._session_from_record(updated)
            record = self._active_or_awaiting_locked(conversation_id)
            if record is None or record.status not in (PlanStatus.ACTIVE, PlanStatus.AWAITING_APPROVAL):
                raise ValueError("active plan session is required")
            updated = deepcopy(record)
            if updated.proposal_status == "awaiting_approval" and updated.proposal_id:
                updated = self._mark_current_proposal(
                    updated,
                    status="superseded",
                    resolved_at=self._now_ms(),
                )
            revision = updated.proposal_revision + 1
            proposal = PlanProposal(
                proposal_id=str(uuid.uuid4()),
                plan_id=updated.plan_id,
                revision=revision,
                plan=plan_text,
                status="awaiting_approval",
                tool_call_id=tool_call_id,
                run_id=run_id,
                node_id=node_id,
                created_at=self._now_ms(),
            )
            updated.status = PlanStatus.AWAITING_APPROVAL
            updated.plan = plan_text
            updated.submitted_node_id = node_id
            updated.submitted_run_id = run_id
            updated.exit_tool_call_id = tool_call_id
            updated.blocking_run_id = run_id
            updated.proposal_id = proposal.proposal_id
            updated.proposal_revision = revision
            updated.proposal_status = proposal.status
            updated.proposals.append(proposal)
            updated.updated_at = time()
            self._plans_by_conversation[conversation_id][updated.plan_id] = updated
            return deepcopy(updated)

    async def ask_user_question(
        self,
        *,
        conversation_id: str,
        question: str,
        options: Optional[Sequence[Dict[str, Any]]] = None,
        node_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> PlanSession:
        question_text = str(question or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not question_text:
            raise ValueError("question is required")
        now = time()
        async with self._lock:
            if self._repository is not None:
                record = self._repository.get_active_or_awaiting(conversation_id)
                if record is None or record.get("status") not in (
                    PlanStatus.ACTIVE.value,
                    PlanStatus.AWAITING_QUESTION.value,
                ):
                    raise ValueError("active plan session is required")
                payload = {
                    "question": question_text,
                    "options": self._normalize_question_options(options),
                    "asked_node_id": node_id,
                    "asked_run_id": run_id,
                    "created_at": now,
                }
                updated = self._repository.ask_question(
                    conversation_id,
                    record["plan_id"],
                    question=payload,
                    tool_call_id=tool_call_id,
                    run_id=run_id,
                )
                return self._session_from_record(updated)
            record = self._active_or_awaiting_locked(conversation_id)
            if record is None or record.status not in (PlanStatus.ACTIVE, PlanStatus.AWAITING_QUESTION):
                raise ValueError("active plan session is required")
            updated = deepcopy(record)
            updated.status = PlanStatus.AWAITING_QUESTION
            updated.question = {
                "question": question_text,
                "options": self._normalize_question_options(options),
                "asked_node_id": node_id,
                "asked_run_id": run_id,
                "created_at": now,
            }
            updated.question_tool_call_id = tool_call_id
            updated.blocking_run_id = run_id
            updated.updated_at = now
            self._plans_by_conversation[conversation_id][updated.plan_id] = updated
            return deepcopy(updated)

    async def answer_question(
        self,
        *,
        conversation_id: str,
        plan_id: str,
        answer: str,
    ) -> PlanSession:
        answer_text = str(answer or "").strip()
        if not answer_text:
            raise ValueError("answer is required")
        async with self._lock:
            if self._repository is not None:
                record = self._repository.get_plan(conversation_id, plan_id)
                if record is None:
                    raise PlanNotFoundError(plan_id)
                if record.get("status") != PlanStatus.AWAITING_QUESTION.value:
                    raise ValueError("plan must be awaiting question")
                updated_record = self._repository.answer_question(
                    conversation_id,
                    plan_id,
                    answer=answer_text,
                )
                updated = self._session_from_record(updated_record)
                self._repository.append_pending_context(
                    conversation_id,
                    plan_id,
                    PlanContextInjection(
                        kind="plan_question_answer",
                        conversation_id=conversation_id,
                        plan_id=plan_id,
                        content=self._question_answer_content(updated),
                        permission_mode="plan",
                    ).to_dict(),
                )
                return deepcopy(updated)
            record = self._require_plan_locked(conversation_id, plan_id)
            if record.status != PlanStatus.AWAITING_QUESTION:
                raise ValueError("plan must be awaiting question")
            now = time()
            updated = deepcopy(record)
            question_payload = dict(updated.question or {})
            question_payload["answer"] = answer_text
            question_payload["answered_at"] = now
            updated.question = question_payload
            updated.feedback.append({
                "question": str(question_payload.get("question") or ""),
                "answer": answer_text,
                "created_at": now,
                "kind": "question_answer",
            })
            updated.status = PlanStatus.ACTIVE
            updated.updated_at = now
            self._plans_by_conversation[conversation_id][plan_id] = updated
            self._pending_context_by_conversation.setdefault(conversation_id, []).append(
                PlanContextInjection(
                    kind="plan_question_answer",
                    conversation_id=conversation_id,
                    plan_id=plan_id,
                    content=self._question_answer_content(updated),
                    permission_mode="plan",
                )
            )
            return deepcopy(updated)

    async def approve_plan(self, *, conversation_id: str, plan_id: str) -> PlanSession:
        async with self._lock:
            if self._repository is not None:
                record = self._repository.get_plan(conversation_id, plan_id)
                if record is None:
                    raise PlanNotFoundError(plan_id)
                if record.get("status") != PlanStatus.AWAITING_APPROVAL.value:
                    raise ValueError("plan must be awaiting approval")
                updated_record = self._repository.approve_plan(conversation_id, plan_id)
                updated = self._session_from_record(updated_record)
                self._repository.append_pending_context(
                    conversation_id,
                    plan_id,
                    PlanContextInjection(
                        kind="approved_plan",
                        conversation_id=conversation_id,
                        plan_id=plan_id,
                        content=self._approved_plan_content(updated),
                        permission_mode=updated.previous_permission_mode,
                    ).to_dict(),
                )
                return deepcopy(updated)
            record = self._require_plan_locked(conversation_id, plan_id)
            if record.status != PlanStatus.AWAITING_APPROVAL:
                raise ValueError("plan must be awaiting approval")
            updated = deepcopy(record)
            updated.status = PlanStatus.APPROVED
            updated.approved_at = time()
            updated.updated_at = updated.approved_at
            updated = self._mark_current_proposal(
                updated,
                status="approved",
                resolved_at=self._now_ms(),
            )
            self._plans_by_conversation[conversation_id][plan_id] = updated
            self._pending_context_by_conversation.setdefault(conversation_id, []).append(
                PlanContextInjection(
                    kind="approved_plan",
                    conversation_id=conversation_id,
                    plan_id=plan_id,
                    content=self._approved_plan_content(updated),
                    permission_mode=updated.previous_permission_mode,
                )
            )
            return deepcopy(updated)

    async def reject_plan(self, *, conversation_id: str, plan_id: str, feedback: str = "") -> PlanSession:
        async with self._lock:
            if self._repository is not None:
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
                updated = self._session_from_record(updated_record)
                self._repository.append_pending_context(
                    conversation_id,
                    plan_id,
                    PlanContextInjection(
                        kind="plan_feedback",
                        conversation_id=conversation_id,
                        plan_id=plan_id,
                        content=self._feedback_content(updated),
                        permission_mode="plan",
                    ).to_dict(),
                )
                return deepcopy(updated)
            record = self._require_plan_locked(conversation_id, plan_id)
            if record.status != PlanStatus.AWAITING_APPROVAL:
                raise ValueError("plan must be awaiting approval")
            now = time()
            updated = deepcopy(record)
            updated.status = PlanStatus.ACTIVE
            updated.rejected_at = now
            updated.updated_at = now
            updated.feedback.append({"feedback": str(feedback or ""), "created_at": now})
            updated = self._mark_current_proposal(
                updated,
                status="rejected",
                resolved_at=self._now_ms(),
                feedback=str(feedback or ""),
            )
            self._plans_by_conversation[conversation_id][plan_id] = updated
            self._pending_context_by_conversation.setdefault(conversation_id, []).append(
                PlanContextInjection(
                    kind="plan_feedback",
                    conversation_id=conversation_id,
                    plan_id=plan_id,
                    content=self._feedback_content(updated),
                    permission_mode="plan",
                )
            )
            return deepcopy(updated)

    async def get_plan(self, conversation_id: str, plan_id: str) -> Optional[PlanSession]:
        async with self._lock:
            if self._repository is not None:
                record = self._repository.get_plan(conversation_id, plan_id)
                return self._session_from_record(record) if record else None
            record = self._plans_by_conversation.get(conversation_id, {}).get(plan_id)
            return deepcopy(record) if record else None

    async def get_active_or_awaiting(self, conversation_id: str) -> Optional[PlanSession]:
        async with self._lock:
            if self._repository is not None:
                record = self._repository.get_active_or_awaiting(conversation_id)
                return self._session_from_record(record) if record else None
            record = self._active_or_awaiting_locked(conversation_id)
            return deepcopy(record) if record else None

    async def get_latest(self, conversation_id: str) -> Optional[PlanSession]:
        async with self._lock:
            if self._repository is not None:
                record = self._repository.get_latest(conversation_id)
                return self._session_from_record(record) if record else None
            records = list(self._plans_by_conversation.get(conversation_id, {}).values())
            if not records:
                return None
            records.sort(key=lambda item: (item.updated_at, item.created_at, item.plan_id), reverse=True)
            return deepcopy(records[0])

    async def consume_pending_context(self, conversation_id: str) -> list[PlanContextInjection]:
        async with self._lock:
            if self._repository is not None:
                pending = self._repository.consume_pending_context(conversation_id)
                return [PlanContextInjection.from_dict(dict(item)) for item in pending]
            pending = self._pending_context_by_conversation.pop(conversation_id, [])
            return [deepcopy(item) for item in pending]

    async def snapshot(self, conversation_id: str) -> Dict[str, Any]:
        async with self._lock:
            if self._repository is not None:
                plans = [
                    self._session_from_record(record)
                    for record in self._repository.list_plans(conversation_id)
                ]
                pending = [
                    PlanContextInjection.from_dict(dict(item))
                    for item in self._repository.peek_pending_context(conversation_id)
                ]
                return {
                    "plans": [plan.to_dict() for plan in plans],
                    "pending_context": [item.to_dict() for item in pending],
                }
            plans = list(self._plans_by_conversation.get(conversation_id, {}).values())
            pending = list(self._pending_context_by_conversation.get(conversation_id, []))
            return {
                "plans": [
                    plan.to_dict()
                    for plan in sorted(plans, key=lambda item: (item.created_at, item.plan_id))
                ],
                "pending_context": [item.to_dict() for item in pending],
            }

    async def load_snapshot(self, conversation_id: str, snapshot: Iterable[Dict[str, Any]] | Dict[str, Any]) -> None:
        if isinstance(snapshot, dict):
            plan_items = snapshot.get("plans") or []
            context_items = snapshot.get("pending_context") or []
        else:
            plan_items = list(snapshot)
            context_items = []
        plans: Dict[str, PlanSession] = {}
        for item in plan_items:
            record = PlanSession.from_dict(dict(item))
            if record.conversation_id != conversation_id:
                raise ValueError("snapshot record conversation_id mismatch")
            plans[record.plan_id] = record
        pending = []
        for item in context_items:
            injection = PlanContextInjection.from_dict(dict(item))
            if injection.conversation_id != conversation_id:
                raise ValueError("snapshot context conversation_id mismatch")
            pending.append(injection)
        async with self._lock:
            if self._repository is not None:
                self._repository.replace_snapshot(
                    conversation_id,
                    plans=[record.to_dict() for record in plans.values()],
                    pending_context=[item.to_dict() for item in pending],
                )
                return
            self._plans_by_conversation[conversation_id] = plans
            self._pending_context_by_conversation[conversation_id] = pending

    def _active_or_awaiting_locked(self, conversation_id: str) -> Optional[PlanSession]:
        records = [
            record
            for record in self._plans_by_conversation.get(conversation_id, {}).values()
            if record.status in ACTIVE_PLAN_STATUSES
        ]
        if not records:
            return None
        records.sort(key=lambda item: (item.updated_at, item.created_at, item.plan_id), reverse=True)
        return records[0]

    @staticmethod
    def _normalize_question_options(options: Optional[Sequence[Dict[str, Any]]]) -> list[Dict[str, str]]:
        normalized: list[Dict[str, str]] = []
        if not options:
            return normalized
        for option in options:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "").strip()
            description = str(option.get("description") or "").strip()
            if not label:
                continue
            normalized.append({"label": label, "description": description})
        return normalized

    def _require_plan_locked(self, conversation_id: str, plan_id: str) -> PlanSession:
        record = self._plans_by_conversation.get(conversation_id, {}).get(plan_id)
        if record is None:
            raise PlanNotFoundError(plan_id)
        return record

    def _session_from_record(self, record: Dict[str, Any]) -> PlanSession:
        return PlanSession.from_dict({
            "plan_id": record.get("plan_id") or record.get("id"),
            "conversation_id": record.get("conversation_id"),
            "status": record.get("status", PlanStatus.ACTIVE.value),
            "previous_permission_mode": record.get("previous_permission_mode", "modify_only"),
            "plan": record.get("plan") or "",
            "question": record.get("question") if isinstance(record.get("question"), dict) else None,
            "feedback": list(record.get("feedback") or []),
            "entered_node_id": record.get("entered_node_id"),
            "entered_run_id": record.get("entered_run_id"),
            "submitted_node_id": record.get("submitted_node_id"),
            "submitted_run_id": record.get("submitted_run_id"),
            "exit_tool_call_id": record.get("exit_tool_call_id"),
            "question_tool_call_id": record.get("question_tool_call_id"),
            "blocking_run_id": record.get("blocking_run_id"),
            "proposal_id": record.get("proposal_id"),
            "proposal_revision": record.get("proposal_revision") or 0,
            "proposal_status": record.get("proposal_status"),
            "proposals": list(record.get("proposals") or []),
            "approved_at": record.get("approved_at"),
            "rejected_at": record.get("rejected_at"),
            "created_at": record.get("created_at", time()),
            "updated_at": record.get("updated_at", time()),
        })

    @staticmethod
    def _now_ms() -> int:
        return int(time() * 1000)

    def _mark_current_proposal(
        self,
        session: PlanSession,
        *,
        status: str,
        resolved_at: Optional[int] = None,
        feedback: Optional[str] = None,
    ) -> PlanSession:
        proposal_id = session.proposal_id
        if not proposal_id:
            return session
        proposals: list[PlanProposal] = []
        for proposal in session.proposals:
            updated = deepcopy(proposal)
            if updated.proposal_id == proposal_id:
                updated.status = status  # type: ignore[assignment]
                updated.resolved_at = resolved_at if resolved_at is not None else updated.resolved_at
                if feedback is not None:
                    updated.feedback = feedback
            proposals.append(updated)
        session.proposals = proposals
        session.proposal_status = status
        return session

    def approved_tool_result_content(self, session: PlanSession) -> str:
        return (
            "User has approved your plan. You can now start coding. "
            "Start with updating your todo list if applicable.\n\n"
            "## Approved Plan:\n"
            f"{session.plan}"
        )

    def rejected_tool_result_content(self, session: PlanSession) -> str:
        feedback = session.feedback[-1].get("feedback", "") if session.feedback else ""
        if feedback.strip():
            return (
                "User did not approve the plan.\n\n"
                f"Feedback:\n{feedback}\n\n"
                "Remain in plan mode. Revise the plan and call exit_plan_mode again when ready."
            )
        return (
            "User did not approve the plan and did not provide specific feedback.\n\n"
            "Remain in plan mode. Re-evaluate and improve the plan. "
            "Call exit_plan_mode again when ready, or ask_user_question only if a real decision is required."
        )

    def question_answer_tool_result_content(self, session: PlanSession) -> str:
        question = str((session.question or {}).get("question") or "")
        answer = str((session.question or {}).get("answer") or "")
        return (
            "The user answered your plan-mode clarification question.\n\n"
            f"Question: {question}\n"
            f"Answer: {answer}\n\n"
            "Remain in plan mode. Continue planning and call exit_plan_mode when ready."
        )

    def _approved_plan_content(self, session: PlanSession) -> str:
        return (
            "Approved plan for this conversation:\n"
            f"{session.plan}\n\n"
            "Continue with the approved plan. The tool permission mode should be restored "
            f"to {session.previous_permission_mode}."
        )

    def _feedback_content(self, session: PlanSession) -> str:
        feedback = session.feedback[-1]["feedback"] if session.feedback else ""
        return (
            "The submitted plan was rejected by the user.\n"
            f"Feedback: {feedback}\n\n"
            "Remain in plan mode. Revise the plan before calling exit_plan_mode again."
        )

    def _question_answer_content(self, session: PlanSession) -> str:
        question_payload = session.question or {}
        question = str(question_payload.get("question") or "")
        answer = str(question_payload.get("answer") or "")
        return (
            "The user answered a plan-mode clarification question.\n"
            f"Question: {question}\n"
            f"Answer: {answer}\n\n"
            "Remain in plan mode. Use this answer to continue read-only exploration and then call "
            "exit_plan_mode when the plan is ready, or ask_user_question only if another genuine "
            "decision is required."
        )
