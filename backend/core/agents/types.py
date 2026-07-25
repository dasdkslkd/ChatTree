from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from time import time
from typing import Any, Dict, Optional


class MultiAgentMode(str, Enum):
    NONE = "none"
    EXPLICIT_REQUEST_ONLY = "explicit_request_only"
    PROACTIVE = "proactive"


class AgentContextMode(str, Enum):
    FRESH = "fresh"
    FORK = "fork"


class AgentDeliveryPolicy(str, Enum):
    AUTO = "auto"
    NOTIFY = "notify"
    SILENT = "silent"


class AgentMailboxMessageType(str, Enum):
    STATUS = "status"
    RESULT = "result"
    ERROR = "error"
    INPUT_REQUEST = "input_request"
    USER_INPUT = "user_input"
    FOLLOWUP = "followup"
    CONTROL = "control"


@dataclass(frozen=True)
class AgentSource:
    conversation_id: str
    run_id: str
    run_kind: str
    anchor_node_id: Optional[str] = None
    root_run_id: Optional[str] = None
    agent_name: Optional[str] = None
    task_summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentMailboxMessage:
    message_id: str
    conversation_id: str
    source_run_id: str
    source_run_kind: str
    message_type: AgentMailboxMessageType
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    delivery_policy: AgentDeliveryPolicy = AgentDeliveryPolicy.AUTO
    created_at: float = field(default_factory=time)
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["message_type"] = self.message_type.value
        data["delivery_policy"] = self.delivery_policy.value
        return data
