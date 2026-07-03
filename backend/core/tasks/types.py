from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from time import time
from typing import Any, Dict, Optional


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


OPEN_TASK_STATUSES = {
    TaskStatus.PENDING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.BLOCKED,
}

FINISHED_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.CANCELLED,
}


class TaskOwnerType(str, Enum):
    ASSISTANT = "assistant"
    SUBAGENT = "subagent"
    WORKFLOW = "workflow"
    COMMAND = "command"


@dataclass
class TaskRecord:
    task_id: str
    conversation_id: str
    title: str
    status: TaskStatus = TaskStatus.PENDING
    detail: str = ""
    owner_type: TaskOwnerType = TaskOwnerType.ASSISTANT
    owner_run_id: Optional[str] = None
    created_by_run_id: Optional[str] = None
    evidence_run_id: Optional[str] = None
    evidence_summary: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["owner_type"] = self.owner_type.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskRecord":
        payload = dict(data)
        payload["status"] = _coerce_task_status(payload.get("status", TaskStatus.PENDING))
        payload["owner_type"] = _coerce_owner_type(payload.get("owner_type", TaskOwnerType.ASSISTANT))
        payload["metadata"] = dict(payload.get("metadata") or {})
        return cls(**payload)


def _coerce_task_status(value: TaskStatus | str) -> TaskStatus:
    return value if isinstance(value, TaskStatus) else TaskStatus(str(value))


def _coerce_owner_type(value: TaskOwnerType | str) -> TaskOwnerType:
    return value if isinstance(value, TaskOwnerType) else TaskOwnerType(str(value))
