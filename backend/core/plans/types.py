from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from time import time
from typing import Any, Dict, List, Literal, Optional


class PlanStatus(str, Enum):
    ACTIVE = "active"
    AWAITING_QUESTION = "awaiting_question"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


ACTIVE_PLAN_STATUSES = {
    PlanStatus.ACTIVE,
    PlanStatus.AWAITING_QUESTION,
    PlanStatus.AWAITING_APPROVAL,
}


@dataclass
class PlanProposal:
    proposal_id: str
    plan_id: str
    revision: int
    plan: str
    status: Literal["awaiting_approval", "approved", "rejected", "superseded"]
    tool_call_id: Optional[str] = None
    run_id: Optional[str] = None
    node_id: Optional[str] = None
    created_at: Optional[int] = None
    resolved_at: Optional[int] = None
    feedback: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanProposal":
        return cls(
            proposal_id=str(data.get("proposal_id") or ""),
            plan_id=str(data.get("plan_id") or ""),
            revision=int(data.get("revision") or 0),
            plan=str(data.get("plan") or ""),
            status=str(data.get("status") or "awaiting_approval"),  # type: ignore[arg-type]
            tool_call_id=data.get("tool_call_id"),
            run_id=data.get("run_id"),
            node_id=data.get("node_id"),
            created_at=data.get("created_at"),
            resolved_at=data.get("resolved_at"),
            feedback=data.get("feedback"),
        )


@dataclass
class PlanSession:
    plan_id: str
    conversation_id: str
    status: PlanStatus = PlanStatus.ACTIVE
    previous_permission_mode: str = "modify_only"
    plan: str = ""
    question: Optional[Dict[str, Any]] = None
    feedback: List[Dict[str, Any]] = field(default_factory=list)
    entered_node_id: Optional[str] = None
    entered_run_id: Optional[str] = None
    submitted_node_id: Optional[str] = None
    submitted_run_id: Optional[str] = None
    exit_tool_call_id: Optional[str] = None
    question_tool_call_id: Optional[str] = None
    blocking_run_id: Optional[str] = None
    proposal_id: Optional[str] = None
    proposal_revision: int = 0
    proposal_status: Optional[str] = None
    proposals: List[PlanProposal] = field(default_factory=list)
    approved_at: Optional[float] = None
    rejected_at: Optional[float] = None
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanSession":
        payload = dict(data)
        payload["status"] = _coerce_plan_status(payload.get("status", PlanStatus.ACTIVE))
        payload["question"] = payload.get("question") if isinstance(payload.get("question"), dict) else None
        payload["feedback"] = list(payload.get("feedback") or [])
        payload["proposals"] = [
            item if isinstance(item, PlanProposal) else PlanProposal.from_dict(dict(item))
            for item in list(payload.get("proposals") or [])
        ]
        payload["proposal_revision"] = int(payload.get("proposal_revision") or 0)
        return cls(**payload)


@dataclass
class PlanContextInjection:
    kind: str
    conversation_id: str
    plan_id: str
    content: str
    permission_mode: str
    created_at: float = field(default_factory=time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanContextInjection":
        return cls(**dict(data))


def _coerce_plan_status(value: PlanStatus | str) -> PlanStatus:
    return value if isinstance(value, PlanStatus) else PlanStatus(str(value))
