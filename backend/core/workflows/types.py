from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class WorkflowBudget:
    max_seconds: int = 600
    max_host_calls: int = 200
    max_parallel: int = 8


@dataclass
class WorkflowRequest:
    script: str
    args: Dict[str, Any] = field(default_factory=dict)
    parent_node_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    budget: WorkflowBudget = field(default_factory=WorkflowBudget)
