from __future__ import annotations

import asyncio
import inspect
import uuid
from copy import deepcopy
from time import time
from typing import Any, Dict, Iterable, Optional

from .types import (
    FINISHED_TASK_STATUSES,
    OPEN_TASK_STATUSES,
    TaskOwnerType,
    TaskRecord,
    TaskStatus,
)


class TaskLedgerError(Exception):
    pass


class TaskNotFoundError(TaskLedgerError):
    pass


def _status(value: TaskStatus | str | None) -> Optional[TaskStatus]:
    if value is None:
        return None
    return value if isinstance(value, TaskStatus) else TaskStatus(str(value))


def _owner_type(value: TaskOwnerType | str) -> TaskOwnerType:
    return value if isinstance(value, TaskOwnerType) else TaskOwnerType(str(value))


class TaskLedger:
    """Process-local per-conversation task ledger."""

    def __init__(self) -> None:
        self._tasks_by_conversation: Dict[str, Dict[str, TaskRecord]] = {}
        self._lock = asyncio.Lock()
        self._listener_run_managers: set[int] = set()

    async def create_task(
        self,
        *,
        conversation_id: str,
        title: str,
        detail: str = "",
        created_by_run_id: Optional[str] = None,
        owner_type: TaskOwnerType | str = TaskOwnerType.ASSISTANT,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskRecord:
        title = title.strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not title:
            raise ValueError("title is required")
        now = time()
        record = TaskRecord(
            task_id=f"task_{uuid.uuid4().hex}",
            conversation_id=conversation_id,
            title=title,
            detail=detail,
            owner_type=_owner_type(owner_type),
            created_by_run_id=created_by_run_id,
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._tasks_by_conversation.setdefault(conversation_id, {})[record.task_id] = record
            return deepcopy(record)

    async def update_task(
        self,
        *,
        conversation_id: str,
        task_id: str,
        status: TaskStatus | str | None = None,
        title: Optional[str] = None,
        detail: Optional[str] = None,
        evidence_run_id: Optional[str] = None,
        evidence_summary: Optional[str] = None,
        metadata_patch: Optional[Dict[str, Any]] = None,
    ) -> TaskRecord:
        next_status = _status(status)
        async with self._lock:
            record = self._require_task_locked(conversation_id, task_id)
            updated = deepcopy(record)
            if title is not None:
                stripped_title = title.strip()
                if not stripped_title:
                    raise ValueError("title must not be empty")
                updated.title = stripped_title
            if detail is not None:
                updated.detail = detail
            if next_status is not None:
                updated.status = next_status
            if evidence_run_id is not None:
                updated.evidence_run_id = evidence_run_id
            if evidence_summary is not None:
                updated.evidence_summary = evidence_summary
            if metadata_patch:
                updated.metadata.update(dict(metadata_patch))
            if (
                updated.status in {TaskStatus.COMPLETED, TaskStatus.BLOCKED}
                and not (updated.evidence_run_id or updated.evidence_summary.strip())
            ):
                raise ValueError(f"{updated.status.value} task requires evidence_run_id or evidence_summary")
            if updated.status in FINISHED_TASK_STATUSES:
                updated.finished_at = updated.finished_at or time()
            elif updated.status in OPEN_TASK_STATUSES:
                updated.finished_at = None
            updated.updated_at = time()
            self._tasks_by_conversation[conversation_id][task_id] = updated
            return deepcopy(updated)

    async def bind_run(
        self,
        *,
        conversation_id: str,
        task_id: str,
        run_id: str,
        owner_type: TaskOwnerType | str,
        status: TaskStatus | str = TaskStatus.IN_PROGRESS,
    ) -> TaskRecord:
        if not run_id:
            raise ValueError("run_id is required")
        async with self._lock:
            record = self._require_task_locked(conversation_id, task_id)
            record.owner_run_id = run_id
            record.owner_type = _owner_type(owner_type)
            record.status = _status(status) or TaskStatus.IN_PROGRESS
            if record.status in OPEN_TASK_STATUSES:
                record.finished_at = None
            record.updated_at = time()
            return deepcopy(record)

    async def get_task(self, conversation_id: str, task_id: str) -> Optional[TaskRecord]:
        async with self._lock:
            record = self._tasks_by_conversation.get(conversation_id, {}).get(task_id)
            return deepcopy(record) if record else None

    async def list_tasks(
        self,
        conversation_id: str,
        statuses: Optional[Iterable[TaskStatus | str]] = None,
        include_finished: bool = True,
    ) -> list[TaskRecord]:
        status_filter = {_status(value) for value in statuses} if statuses is not None else None
        async with self._lock:
            records = list(self._tasks_by_conversation.get(conversation_id, {}).values())
            result = []
            for record in records:
                if status_filter is not None and record.status not in status_filter:
                    continue
                if not include_finished and record.status in FINISHED_TASK_STATUSES:
                    continue
                result.append(deepcopy(record))
            return sorted(result, key=lambda task: (task.created_at, task.task_id))

    async def list_open_tasks(self, conversation_id: str) -> list[TaskRecord]:
        return await self.list_tasks(
            conversation_id,
            statuses=OPEN_TASK_STATUSES,
            include_finished=False,
        )

    def list_open_tasks_snapshot(self, conversation_id: str, *, limit: int = 8) -> list[TaskRecord]:
        records = [
            record
            for record in self._tasks_by_conversation.get(conversation_id, {}).values()
            if record.status in OPEN_TASK_STATUSES
        ]
        records.sort(key=lambda task: (task.updated_at, task.created_at, task.task_id), reverse=True)
        return [deepcopy(record) for record in records[: max(0, int(limit or 0))]]

    async def find_by_owner_run(self, conversation_id: str, run_id: str) -> Optional[TaskRecord]:
        async with self._lock:
            for record in self._tasks_by_conversation.get(conversation_id, {}).values():
                if record.owner_run_id == run_id:
                    return deepcopy(record)
            return None

    async def handle_run_finished(self, run: Dict[str, Any]) -> None:
        conversation_id = str(run.get("conversation_id") or "")
        run_id = str(run.get("run_id") or "")
        if not conversation_id or not run_id:
            return
        task = await self.find_by_owner_run(conversation_id, run_id)
        if task is None:
            return
        status = str(run.get("status") or "")
        if status == "completed":
            next_status = TaskStatus.COMPLETED
        elif status == "failed":
            next_status = TaskStatus.BLOCKED
        elif status == "cancelled":
            next_status = TaskStatus.CANCELLED
        else:
            return
        await self.update_task(
            conversation_id=conversation_id,
            task_id=task.task_id,
            status=next_status,
            evidence_run_id=run_id,
            evidence_summary=self._run_evidence_summary(run, next_status),
        )

    async def snapshot(self, conversation_id: str) -> list[Dict[str, Any]]:
        # Stable persistence boundary: callers may store this per-conversation
        # list and later restore it with load_snapshot without inspecting internals.
        async with self._lock:
            records = list(self._tasks_by_conversation.get(conversation_id, {}).values())
            return [record.to_dict() for record in sorted(records, key=lambda task: (task.created_at, task.task_id))]

    async def load_snapshot(self, conversation_id: str, records: Iterable[Dict[str, Any]]) -> None:
        loaded: Dict[str, TaskRecord] = {}
        for item in records:
            record = TaskRecord.from_dict(dict(item))
            if record.conversation_id != conversation_id:
                raise ValueError("snapshot record conversation_id mismatch")
            loaded[record.task_id] = record
        async with self._lock:
            self._tasks_by_conversation[conversation_id] = loaded

    def install_run_finish_listener(self, run_manager: Any) -> bool:
        key = id(run_manager)
        if key in self._listener_run_managers:
            return False

        def listener(run: Dict[str, Any]) -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            result = self.handle_run_finished(run)
            if inspect.isawaitable(result):
                loop.create_task(result)

        run_manager.add_finish_listener(listener)
        self._listener_run_managers.add(key)
        return True

    def _require_task_locked(self, conversation_id: str, task_id: str) -> TaskRecord:
        record = self._tasks_by_conversation.get(conversation_id, {}).get(task_id)
        if record is None:
            raise TaskNotFoundError(task_id)
        return record

    def _run_evidence_summary(self, run: Dict[str, Any], status: TaskStatus) -> str:
        summary = str(run.get("summary") or "").strip()
        metadata = dict(run.get("metadata") or {})
        error = str(run.get("error") or metadata.get("error") or "").strip()
        if status == TaskStatus.COMPLETED:
            return f"Run completed: {summary}" if summary else "Run completed"
        if status == TaskStatus.CANCELLED:
            return f"Run cancelled: {summary}" if summary else "Run cancelled"
        reason = error or summary
        return f"Run failed: {reason}" if reason else "Run failed"
