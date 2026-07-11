from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import time
from typing import Any, Optional


class TaskStepStatus(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    COMPLETED = "completed"


class TaskContextMode(str, Enum):
    ATTACHED = "attached"
    DETACHED = "detached"


class TaskLifecycleStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ActiveTaskStep:
    position: int
    title: str
    detail: str = ""
    status: TaskStepStatus = TaskStepStatus.PENDING
    evidence_run_id: Optional[str] = None
    evidence_summary: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "title": self.title,
            "detail": self.detail,
            "status": self.status.value,
            "evidence_summary": self.evidence_summary,
        }


@dataclass(frozen=True)
class ActiveTask:
    conversation_id: str
    generation_id: str
    revision: int
    title: str
    detail: str = ""
    steps: tuple[ActiveTaskStep, ...] = field(default_factory=tuple)
    active_run_id: Optional[str] = None
    active_step: Optional[int] = None
    execution_state: str = "idle"
    created_by_run_id: Optional[str] = None
    created_by_tool_call_id: Optional[str] = None
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)

    @property
    def status(self) -> str:
        if any(step.status == TaskStepStatus.BLOCKED for step in self.steps):
            return TaskStepStatus.BLOCKED.value
        return TaskStepStatus.PENDING.value

    def public_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "detail": self.detail,
            "status": self.status,
            "execution_state": self.execution_state,
            "active_run_id": self.active_run_id,
            "active_step": self.active_step,
            "steps": [step.public_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class TaskStateStep:
    position: int
    title: str
    status: TaskStepStatus

    @classmethod
    def from_dict(cls, value: Any) -> Optional["TaskStateStep"]:
        if not isinstance(value, dict):
            return None
        try:
            position = int(value.get("position"))
            status = TaskStepStatus(str(value.get("status") or ""))
        except (TypeError, ValueError):
            return None
        return cls(
            position=position,
            title=str(value.get("title") or ""),
            status=status,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "title": self.title,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class TaskStateSnapshot:
    title: str
    steps: tuple[TaskStateStep, ...] = field(default_factory=tuple)

    @classmethod
    def from_task(cls, task: ActiveTask) -> "TaskStateSnapshot":
        return cls(
            title=task.title,
            steps=tuple(
                TaskStateStep(position=step.position, title=step.title, status=step.status)
                for step in task.steps
            ),
        )

    @classmethod
    def from_dict(cls, value: Any) -> Optional["TaskStateSnapshot"]:
        if not isinstance(value, dict):
            return None
        steps: list[TaskStateStep] = []
        for raw_step in value.get("steps") or []:
            step = TaskStateStep.from_dict(raw_step)
            if step is None:
                return None
            steps.append(step)
        return cls(
            title=str(value.get("title") or ""),
            steps=tuple(steps),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "steps": [step.public_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class TaskMutationResult:
    task: Optional[ActiveTask]
    task_snapshot: TaskStateSnapshot
    task_outcome: "TaskOutcome"
    completed: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "task": self.task.public_dict() if self.task is not None else None,
            "task_snapshot": self.task_snapshot.public_dict(),
            "task_outcome": self.task_outcome.public_dict(),
        }


@dataclass(frozen=True)
class TaskOutcome:
    kind: str
    task_status: TaskLifecycleStatus
    step: Optional[int] = None
    step_status: Optional[str] = None
    run_status: Optional[str] = None
    task_snapshot: Optional[TaskStateSnapshot] = None

    def public_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "task_status": self.task_status.value,
        }
        if self.step is not None:
            data["step"] = self.step
        if self.step_status:
            data["step_status"] = self.step_status
        if self.run_status:
            data["run_status"] = self.run_status
        if self.task_snapshot is not None:
            data["task_snapshot"] = self.task_snapshot.public_dict()
        return data

    @classmethod
    def from_dict(cls, value: Any) -> Optional["TaskOutcome"]:
        if not isinstance(value, dict):
            return None
        try:
            task_status = TaskLifecycleStatus(str(value.get("task_status") or ""))
        except ValueError:
            return None
        raw_step = value.get("step")
        try:
            step = int(raw_step) if raw_step is not None else None
        except (TypeError, ValueError):
            return None
        return cls(
            kind=str(value.get("kind") or "task_updated"),
            task_status=task_status,
            step=step,
            step_status=str(value.get("step_status") or "") or None,
            run_status=str(value.get("run_status") or "") or None,
            task_snapshot=TaskStateSnapshot.from_dict(value.get("task_snapshot")),
        )


@dataclass(frozen=True)
class TaskTurnOutcome:
    generation_id: Optional[str]
    outcome: TaskOutcome


@dataclass
class TaskTurnContext:
    mode: TaskContextMode
    baseline_task: Optional[ActiveTask]
    current_task: Optional[ActiveTask]
    outcomes: list[TaskTurnOutcome] = field(default_factory=list)

    @classmethod
    def start(
        cls,
        mode: TaskContextMode | str,
        task: Optional[ActiveTask],
    ) -> "TaskTurnContext":
        normalized_mode = normalize_context_mode(mode)
        visible_task = task if normalized_mode == TaskContextMode.ATTACHED else None
        return cls(
            mode=normalized_mode,
            baseline_task=visible_task,
            current_task=visible_task,
        )

    def refresh(
        self,
        task: Optional[ActiveTask],
        outcome: Optional[TaskOutcome] = None,
    ) -> None:
        if self.mode == TaskContextMode.DETACHED:
            return
        previous_task = self.current_task
        self.current_task = task
        if outcome is not None:
            related_task = previous_task or task or self.baseline_task
            observed = TaskTurnOutcome(
                generation_id=(related_task.generation_id if related_task is not None else None),
                outcome=outcome,
            )
            if not self.outcomes or self.outcomes[-1] != observed:
                self.outcomes.append(observed)

    @property
    def generation_id(self) -> Optional[str]:
        return self.current_task.generation_id if self.current_task is not None else None

    @property
    def revision(self) -> Optional[int]:
        return self.current_task.revision if self.current_task is not None else None


def normalize_context_mode(value: TaskContextMode | str | None) -> TaskContextMode:
    if isinstance(value, TaskContextMode):
        return value
    return TaskContextMode(str(value or TaskContextMode.ATTACHED.value))


def normalize_step_status(value: TaskStepStatus | str) -> TaskStepStatus:
    if isinstance(value, TaskStepStatus):
        return value
    return TaskStepStatus(str(value))
