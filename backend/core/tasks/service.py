from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from time import time
from typing import Any, Iterable, Optional

from .types import (
    ActiveTask,
    ActiveTaskStep,
    TaskContextMode,
    TaskLifecycleStatus,
    TaskMutationResult,
    TaskOutcome,
    TaskStateSnapshot,
    TaskStepStatus,
    normalize_context_mode,
    normalize_step_status,
)


class ActiveTaskError(Exception):
    pass


class ActiveTaskNotFoundError(ActiveTaskError):
    pass


class ActiveTaskConflictError(ActiveTaskError):
    pass


class TaskContextDisabledError(ActiveTaskError):
    pass


class ActiveTaskService:
    """Conversation-scoped singleton task state."""

    def __init__(self, repository: Any = None, *, run_manager: Any = None) -> None:
        self.repository = repository
        self.run_manager = run_manager
        self._tasks: dict[str, ActiveTask] = {}
        self._bindings: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def create_task(
        self,
        *,
        conversation_id: str,
        title: str,
        steps: Iterable[dict[str, Any]],
        detail: str = "",
        created_by_run_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> ActiveTask:
        title = str(title or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not title:
            raise ValueError("title is required")
        prepared_steps = self._prepare_steps(steps)
        if self.repository is not None:
            try:
                row = self.repository.create_task(
                    conversation_id,
                    title=title,
                    detail=str(detail or ""),
                    steps=prepared_steps,
                    created_by_run_id=created_by_run_id,
                    created_by_tool_call_id=tool_call_id,
                )
            except RuntimeError as exc:
                raise ActiveTaskConflictError(str(exc)) from exc
            return self._from_row(row)

        async with self._lock:
            existing = self._tasks.get(conversation_id)
            if existing is not None:
                if tool_call_id and existing.created_by_tool_call_id == tool_call_id:
                    return existing
                raise ActiveTaskConflictError("conversation already has an active task")
            now = time()
            task = ActiveTask(
                conversation_id=conversation_id,
                generation_id=f"taskgen_{uuid.uuid4().hex}",
                revision=0,
                title=title,
                detail=str(detail or ""),
                steps=tuple(
                    ActiveTaskStep(
                        position=index,
                        title=item["title"],
                        detail=item["detail"],
                    )
                    for index, item in enumerate(prepared_steps, start=1)
                ),
                created_by_run_id=created_by_run_id,
                created_by_tool_call_id=tool_call_id,
                created_at=now,
                updated_at=now,
            )
            self._tasks[conversation_id] = task
            return task

    async def get_active_task(self, conversation_id: str) -> Optional[ActiveTask]:
        if self.repository is not None:
            row = self.repository.get_active_task(conversation_id)
            return self._from_row(row) if row is not None else None
        async with self._lock:
            return self._with_current_run_state(self._tasks.get(conversation_id))

    def get_active_task_snapshot(self, conversation_id: str) -> Optional[ActiveTask]:
        if self.repository is not None:
            row = self.repository.get_active_task(conversation_id)
            return self._from_row(row) if row is not None else None
        return self._with_current_run_state(self._tasks.get(conversation_id))

    async def set_step_result(
        self,
        *,
        conversation_id: str,
        step: int,
        status: TaskStepStatus | str,
        evidence_summary: str,
        evidence_run_id: Optional[str] = None,
        expected_generation: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> TaskMutationResult:
        step_number = self._step_number(step)
        next_status = normalize_step_status(status)
        evidence = str(evidence_summary or "").strip()
        if next_status in {TaskStepStatus.COMPLETED, TaskStepStatus.BLOCKED} and not (
            evidence or evidence_run_id
        ):
            raise ValueError(f"{next_status.value} task step requires evidence")
        if self.repository is not None:
            try:
                result = self.repository.set_step_result(
                    conversation_id,
                    step=step_number,
                    status=next_status.value,
                    evidence_summary=evidence,
                    evidence_run_id=evidence_run_id,
                    expected_generation=expected_generation,
                    expected_revision=expected_revision,
                )
            except KeyError as exc:
                raise ActiveTaskNotFoundError(str(exc)) from exc
            except RuntimeError as exc:
                raise ActiveTaskConflictError(str(exc)) from exc
            task_row = result.get("task")
            task = self._from_row(task_row) if task_row is not None else None
            task_snapshot = TaskStateSnapshot.from_dict(result.get("task_snapshot"))
            if task_snapshot is None:
                raise RuntimeError("task mutation did not return a task snapshot")
            return TaskMutationResult(
                task=task,
                task_snapshot=task_snapshot,
                task_outcome=TaskOutcome(
                    kind="step_updated",
                    task_status=(
                        TaskLifecycleStatus.COMPLETED
                        if bool(result.get("completed"))
                        else TaskLifecycleStatus.ACTIVE
                    ),
                    step=step_number,
                    step_status=next_status.value,
                    task_snapshot=task_snapshot,
                ),
                completed=bool(result.get("completed")),
            )

        async with self._lock:
            task = self._require_in_memory(
                conversation_id,
                expected_generation,
                expected_revision,
            )
            if conversation_id in self._bindings:
                raise ActiveTaskConflictError("active task already has a running step")
            index = step_number - 1
            if index >= len(task.steps):
                raise ActiveTaskNotFoundError(str(step_number))
            if any(item.status != TaskStepStatus.COMPLETED for item in task.steps[:index]):
                raise ActiveTaskConflictError("previous task steps must be completed first")
            current = task.steps[index]
            if current.status == TaskStepStatus.COMPLETED and next_status != TaskStepStatus.COMPLETED:
                raise ActiveTaskConflictError("completed task steps are immutable")
            updated_step = replace(
                current,
                status=next_status,
                evidence_run_id=evidence_run_id,
                evidence_summary=evidence,
            )
            steps = list(task.steps)
            steps[index] = updated_step
            updated = replace(task, steps=tuple(steps), revision=task.revision + 1, updated_at=time())
            if all(item.status == TaskStepStatus.COMPLETED for item in steps):
                self._tasks.pop(conversation_id, None)
                return TaskMutationResult(
                    task=None,
                    task_snapshot=TaskStateSnapshot.from_task(updated),
                    task_outcome=TaskOutcome(
                        kind="step_updated",
                        task_status=TaskLifecycleStatus.COMPLETED,
                        step=step_number,
                        step_status=next_status.value,
                        task_snapshot=TaskStateSnapshot.from_task(updated),
                    ),
                    completed=True,
                )
            self._tasks[conversation_id] = updated
            return TaskMutationResult(
                task=updated,
                task_snapshot=TaskStateSnapshot.from_task(updated),
                task_outcome=TaskOutcome(
                    kind="step_updated",
                    task_status=TaskLifecycleStatus.ACTIVE,
                    step=step_number,
                    step_status=next_status.value,
                    task_snapshot=TaskStateSnapshot.from_task(updated),
                ),
                completed=False,
            )

    async def cancel_task(
        self,
        *,
        conversation_id: str,
        reason: str,
        expected_generation: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> bool:
        reason_text = str(reason or "").strip()
        if not reason_text:
            raise ValueError("reason is required")
        task = await self.get_active_task(conversation_id)
        if task is None:
            raise ActiveTaskNotFoundError(conversation_id)
        if expected_generation and task.generation_id != expected_generation:
            raise ActiveTaskConflictError("active task generation changed")
        if expected_revision is not None and task.revision != expected_revision:
            raise ActiveTaskConflictError("active task revision changed")
        cancellation_generation = expected_generation or task.generation_id
        cancellation_revision = task.revision if expected_revision is None else expected_revision
        active_run_id = task.active_run_id
        if self.repository is not None:
            try:
                cancelled = bool(self.repository.cancel_task(
                    conversation_id,
                    expected_generation=cancellation_generation,
                    expected_revision=cancellation_revision,
                ))
            except KeyError as exc:
                raise ActiveTaskNotFoundError(str(exc)) from exc
            except RuntimeError as exc:
                raise ActiveTaskConflictError(str(exc)) from exc
        else:
            async with self._lock:
                self._require_in_memory(
                    conversation_id,
                    cancellation_generation,
                    cancellation_revision,
                )
                self._bindings.pop(conversation_id, None)
                self._tasks.pop(conversation_id, None)
                cancelled = True
        if cancelled and active_run_id and self.run_manager is not None:
            await self.run_manager.request_stop(active_run_id)
        return cancelled

    async def prepare_run_binding(
        self,
        *,
        conversation_id: str,
        step: int,
        context_mode: TaskContextMode | str,
        expected_generation: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> dict[str, Any]:
        if normalize_context_mode(context_mode) != TaskContextMode.ATTACHED:
            raise TaskContextDisabledError("task context is detached")
        if not expected_generation or expected_revision is None:
            raise ActiveTaskConflictError("active task context is stale")
        step_number = self._step_number(step)
        task = await self.get_active_task(conversation_id)
        if task is None:
            raise ActiveTaskNotFoundError(conversation_id)
        if task.generation_id != expected_generation:
            raise ActiveTaskConflictError("active task generation changed")
        if task.revision != expected_revision:
            raise ActiveTaskConflictError("active task revision changed")
        if task.active_run_id:
            raise ActiveTaskConflictError("active task already has a running step")
        if step_number > len(task.steps):
            raise ActiveTaskNotFoundError(str(step_number))
        selected = task.steps[step_number - 1]
        if selected.status == TaskStepStatus.COMPLETED:
            raise ActiveTaskConflictError("task step is already completed")
        if any(item.status != TaskStepStatus.COMPLETED for item in task.steps[: step_number - 1]):
            raise ActiveTaskConflictError("previous task steps must be completed first")
        return {
            "conversation_id": conversation_id,
            "task_generation_id": task.generation_id,
            "step_position": step_number,
            "base_revision": expected_revision,
        }

    async def bind_in_memory_run(self, run_id: str, binding: dict[str, Any]) -> None:
        conversation_id = str(binding.get("conversation_id") or "")
        async with self._lock:
            task = self._require_in_memory(
                conversation_id,
                str(binding.get("task_generation_id") or ""),
                int(binding.get("base_revision", -1)),
            )
            if conversation_id in self._bindings:
                raise ActiveTaskConflictError("active task already has a running step")
            step = self._step_number(binding.get("step_position"))
            self._bindings[conversation_id] = {
                **binding,
                "run_id": run_id,
            }
            self._tasks[conversation_id] = replace(
                task,
                active_run_id=run_id,
                active_step=step,
                execution_state="running",
            )

    async def handle_run_finished(self, run: dict[str, Any]) -> Optional[TaskOutcome]:
        run_id = str(run.get("run_id") or "")
        status = str(run.get("status") or "")
        error = str(run.get("error") or (run.get("metadata") or {}).get("error") or "").strip()
        summary = str(run.get("summary") or "").strip()
        if not run_id:
            return None
        if self.repository is not None:
            outcome = self.repository.finish_run_binding(
                run_id=run_id,
                terminal_status=status,
                error=error or None,
                summary=summary,
            )
            return TaskOutcome.from_dict(outcome)
        async with self._lock:
            conversation_id = str(run.get("conversation_id") or "")
            binding = self._bindings.get(conversation_id)
            if binding is None or binding.get("run_id") != run_id:
                return None
            self._bindings.pop(conversation_id, None)
            task = self._tasks.get(conversation_id)
            if task is None or task.generation_id != binding.get("task_generation_id"):
                return None
            steps = list(task.steps)
            index = int(binding["step_position"]) - 1
            step_status = "released"
            if status == "completed":
                step_status = TaskStepStatus.COMPLETED.value
                steps[index] = replace(
                    steps[index],
                    status=TaskStepStatus.COMPLETED,
                    evidence_run_id=run_id,
                    evidence_summary=f"Run completed: {summary}" if summary else "Run completed",
                )
            elif status == "failed":
                step_status = TaskStepStatus.BLOCKED.value
                steps[index] = replace(
                    steps[index],
                    status=TaskStepStatus.BLOCKED,
                    evidence_run_id=run_id,
                    evidence_summary=error or "Run failed",
                )
            updated = replace(
                task,
                steps=tuple(steps),
                active_run_id=None,
                active_step=None,
                execution_state="idle",
                revision=task.revision + 1,
                updated_at=time(),
            )
            task_snapshot = TaskStateSnapshot.from_task(updated)
            if all(item.status == TaskStepStatus.COMPLETED for item in steps):
                self._tasks.pop(conversation_id, None)
                return TaskOutcome(
                    kind="run_finished",
                    task_status=TaskLifecycleStatus.COMPLETED,
                    step=int(binding["step_position"]),
                    step_status=step_status,
                    run_status=status,
                    task_snapshot=task_snapshot,
                )
            self._tasks[conversation_id] = updated
            return TaskOutcome(
                kind="run_finished",
                task_status=TaskLifecycleStatus.ACTIVE,
                step=int(binding["step_position"]),
                step_status=step_status,
                run_status=status,
                task_snapshot=task_snapshot,
            )

    def _prepare_steps(self, steps: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
        prepared: list[dict[str, str]] = []
        for item in steps or []:
            data = dict(item or {})
            title = str(data.get("title") or "").strip()
            if not title:
                raise ValueError("step title is required")
            prepared.append({"title": title, "detail": str(data.get("detail") or "")})
        if not prepared:
            raise ValueError("at least one task step is required")
        if len(prepared) > 64:
            raise ValueError("task supports at most 64 steps")
        return prepared

    def _with_current_run_state(self, task: Optional[ActiveTask]) -> Optional[ActiveTask]:
        if task is None or not task.active_run_id or self.run_manager is None:
            return task
        run = self.run_manager.get_run(task.active_run_id) or {}
        status = str(run.get("status") or "")
        if status == "stopping":
            execution_state = "stopping"
        elif status in {"completed", "failed", "cancelled", "interrupted", "stopped"}:
            execution_state = "idle"
        else:
            execution_state = "running"
        if task.execution_state == execution_state:
            return task
        return replace(task, execution_state=execution_state)

    def _require_in_memory(
        self,
        conversation_id: str,
        expected_generation: Optional[str],
        expected_revision: Optional[int] = None,
    ) -> ActiveTask:
        task = self._tasks.get(conversation_id)
        if task is None:
            raise ActiveTaskNotFoundError(conversation_id)
        if expected_generation and task.generation_id != expected_generation:
            raise ActiveTaskConflictError("active task generation changed")
        if expected_revision is not None and task.revision != expected_revision:
            raise ActiveTaskConflictError("active task revision changed")
        return task

    def _step_number(self, value: Any) -> int:
        try:
            step = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("step must be a positive integer") from exc
        if step <= 0:
            raise ValueError("step must be a positive integer")
        return step

    def _from_row(self, row: dict[str, Any]) -> ActiveTask:
        return ActiveTask(
            conversation_id=str(row.get("conversation_id") or ""),
            generation_id=str(row.get("generation_id") or ""),
            revision=int(row.get("revision") or 0),
            title=str(row.get("title") or ""),
            detail=str(row.get("detail") or ""),
            steps=tuple(
                ActiveTaskStep(
                    position=int(step.get("position") or 0),
                    title=str(step.get("title") or ""),
                    detail=str(step.get("detail") or ""),
                    status=normalize_step_status(step.get("status") or TaskStepStatus.PENDING.value),
                    evidence_run_id=step.get("evidence_run_id"),
                    evidence_summary=str(step.get("evidence_summary") or ""),
                )
                for step in row.get("steps") or []
            ),
            active_run_id=row.get("active_run_id"),
            active_step=(int(row["active_step"]) if row.get("active_step") is not None else None),
            execution_state=str(row.get("execution_state") or "idle"),
            created_by_run_id=row.get("created_by_run_id"),
            created_by_tool_call_id=row.get("created_by_tool_call_id"),
            created_at=float(row.get("created_at") or time()),
            updated_at=float(row.get("updated_at") or time()),
        )
