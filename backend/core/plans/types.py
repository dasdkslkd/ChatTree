from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from time import time
from typing import Any, Dict, List, Optional


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
