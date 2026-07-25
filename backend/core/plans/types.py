from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from time import time
from typing import Any, Optional


class PlanStatus(str, Enum):
    ACTIVE = "active"
    AWAITING_QUESTION = "awaiting_question"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass
class PlanSession:
    plan_id: str
    conversation_id: str
    status: PlanStatus = PlanStatus.ACTIVE
    previous_permission_mode: str = "modify_only"
    entered_node_id: Optional[str] = None
    entered_run_id: Optional[str] = None
    approved_run_id: Optional[str] = None
    question_tool_call_id: Optional[str] = None
    exit_tool_call_id: Optional[str] = None
    blocking_node_id: Optional[str] = None
    blocking_run_id: Optional[str] = None
    approved_at: Optional[float] = None
    rejected_at: Optional[float] = None
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanSession":
        payload = dict(data)
        payload["status"] = _coerce_plan_status(payload.get("status", PlanStatus.ACTIVE))
        return cls(**payload)


def _coerce_plan_status(value: PlanStatus | str) -> PlanStatus:
    return value if isinstance(value, PlanStatus) else PlanStatus(str(value))
