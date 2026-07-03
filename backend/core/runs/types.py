from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from time import time
from typing import Any, Dict, Optional


class RunKind(str, Enum):
    CHAT = "chat"
    SIDE_QUESTION = "side_question"
    SUBAGENT = "subagent"
    COMMAND = "command"
    WORKFLOW = "workflow"
    WORKFLOW_STEP = "workflow_step"
    DIRECT_RESPONSE = "direct_response"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


FINISHED_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


@dataclass
class RunRecord:
    run_id: str
    conversation_id: str
    kind: RunKind
    status: RunStatus = RunStatus.QUEUED
    anchor_node_id: Optional[str] = None
    target_node_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    summary: str = ""
    event_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["status"] = self.status.value
        return data


@dataclass
class RunEvent:
    run_id: str
    event_index: int
    payload: Dict[str, Any]
    created_at: float = field(default_factory=time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "event_index": self.event_index,
            "payload": self.payload,
            "created_at": self.created_at,
        }
