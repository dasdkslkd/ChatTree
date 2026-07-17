from __future__ import annotations

import asyncio
import contextlib
import uuid
from copy import deepcopy
from dataclasses import dataclass
from time import time
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Optional

from backend.core.perf import get_profiler

from .idempotency import RunIdempotencyConflictError
from .journal import RunJournal
from .public import public_run_dict, public_run_event
from .types import FINISHED_RUN_STATUSES, RunKind, RunRecord, RunStatus


class RunNotFoundError(Exception):
    pass


class RunWriterConflictError(Exception):
    pass


class RunManagerClosingError(RuntimeError):
    pass


@dataclass(frozen=True)
class PendingReservationDrainResult:
    pending_run_ids: tuple[str, ...]
    exhausted_run_ids: tuple[str, ...]


class _RunEventWriteBuffer:
    """Batch run-event persistence without delaying live stream subscribers."""

    def __init__(
        self,
        repository: Any,
        *,
        max_batch_size: int = 128,
        flush_interval_seconds: float = 0.05,
    ) -> None:
        self._repository = repository
        self._max_batch_size = max(1, int(max_batch_size))
        self._flush_interval_seconds = max(0.0, float(flush_interval_seconds))
        self._condition = asyncio.Condition()
        self._pending: Dict[str, list[Dict[str, Any]]] = {}
        self._inflight: Dict[str, int] = {}
        self._flush_requested: set[str] = set()
        self._flush_all_requested = False
        self._closed = False
        self._task: Optional[asyncio.Task[None]] = None
        self._error: Optional[BaseException] = None
        self._run_errors: Dict[str, BaseException] = {}

    async def enqueue_many(self, events: list[Dict[str, Any]]) -> None:
        if not events:
            return
        async with self._condition:
            self._raise_if_failed_locked()
            for run_id in {str(event["run_id"]) for event in events}:
                self._raise_run_if_failed_locked(run_id)
            if self._closed:
                raise RuntimeError("run event writer is closed")
            self._ensure_started_locked()
            for event in events:
                self._pending.setdefault(str(event["run_id"]), []).append(deepcopy(event))
            self._condition.notify_all()

    async def flush_run(self, run_id: str) -> None:
        run_key = str(run_id)
        async with self._condition:
            self._raise_if_failed_locked()
            self._raise_run_if_failed_locked(run_key)
            if self._pending.get(run_key):
                self._ensure_started_locked()
            self._flush_requested.add(run_key)
            self._condition.notify_all()
            while self._pending.get(run_key) or self._inflight.get(run_key, 0):
                await self._condition.wait()
                self._raise_if_failed_locked()
                self._raise_run_if_failed_locked(run_key)
            self._raise_run_if_failed_locked(run_key)
            self._flush_requested.discard(run_key)

    async def discard_run(self, run_id: str) -> None:
        run_key = str(run_id)
        async with self._condition:
            self._pending.pop(run_key, None)
            self._flush_requested.discard(run_key)
            self._condition.notify_all()
            while self._inflight.get(run_key, 0):
                await self._condition.wait()
            self._run_errors.pop(run_key, None)

    async def flush_all(self) -> None:
        async with self._condition:
            self._raise_if_failed_locked()
            if self._pending:
                self._ensure_started_locked()
            self._flush_all_requested = True
            self._condition.notify_all()
            while self._pending or any(self._inflight.values()):
                await self._condition.wait()
                self._raise_if_failed_locked()
            self._flush_all_requested = False

    async def close(self) -> None:
        async with self._condition:
            if self._pending:
                self._ensure_started_locked()
            if self._task is not None:
                self._flush_all_requested = True
            self._closed = True
            self._condition.notify_all()
        task = self._task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        async with self._condition:
            self._raise_if_failed_locked()

    def _ensure_started_locked(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="run-event-writer")

    async def _run(self) -> None:
        while True:
            async with self._condition:
                while not self._pending and not self._closed and self._error is None:
                    await self._condition.wait()
                if self._error is not None:
                    self._condition.notify_all()
                    return
                if not self._pending and self._closed:
                    self._condition.notify_all()
                    return

                if (
                    not self._closed
                    and not self._flush_all_requested
                    and not self._pending_has_flush_request_locked()
                    and self._pending_count_locked() < self._max_batch_size
                ):
                    try:
                        await asyncio.wait_for(
                            self._condition.wait(),
                            timeout=self._flush_interval_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
                    if not self._pending:
                        continue

                run_id, batch = self._take_batch_locked()
                self._inflight[run_id] = self._inflight.get(run_id, 0) + len(batch)

            error: Optional[BaseException] = None
            try:
                await asyncio.to_thread(self._repository.append_indexed_events, run_id, batch)
            except BaseException as exc:
                error = exc

            async with self._condition:
                remaining = self._inflight.get(run_id, 0) - len(batch)
                if remaining > 0:
                    self._inflight[run_id] = remaining
                else:
                    self._inflight.pop(run_id, None)
                if not self._pending.get(run_id) and not self._inflight.get(run_id):
                    self._flush_requested.discard(run_id)
                if isinstance(error, KeyError):
                    self._run_errors[run_id] = error
                    self._pending.pop(run_id, None)
                    self._flush_requested.discard(run_id)
                elif error is not None:
                    self._error = error
                self._condition.notify_all()
                if error is not None and not isinstance(error, KeyError):
                    return

    def _take_batch_locked(self) -> tuple[str, list[Dict[str, Any]]]:
        run_id = self._select_run_locked()
        pending = self._pending[run_id]
        take_count = min(len(pending), self._max_batch_size)
        batch = pending[:take_count]
        remaining = pending[take_count:]
        if remaining:
            self._pending[run_id] = remaining
        else:
            self._pending.pop(run_id, None)
        return run_id, batch

    def _select_run_locked(self) -> str:
        if self._flush_all_requested:
            return next(iter(self._pending))
        for run_id in self._flush_requested:
            if self._pending.get(run_id):
                return run_id
        return next(iter(self._pending))

    def _pending_count_locked(self) -> int:
        return sum(len(events) for events in self._pending.values())

    def _pending_has_flush_request_locked(self) -> bool:
        return any(self._pending.get(run_id) for run_id in self._flush_requested)

    def _raise_if_failed_locked(self) -> None:
        if self._error is not None:
            raise RuntimeError("run event writer failed") from self._error

    def _raise_run_if_failed_locked(self, run_id: str) -> None:
        error = self._run_errors.get(run_id)
        if error is not None:
            raise RuntimeError(f"run event writer failed for run {run_id}") from error


class RunManager:
    """In-memory active run registry with durable event replay."""

    def __init__(
        self,
        journal: Optional[RunJournal] = None,
        repository: Optional[Any] = None,
    ) -> None:
        self.journal = journal or RunJournal()
        self.repository = repository
        self.notification_service: Any = None
        self.task_service: Any = None
        self._event_writer: Optional[_RunEventWriteBuffer] = (
            _RunEventWriteBuffer(repository)
            if repository is not None and hasattr(repository, "append_indexed_events")
            else None
        )
        self._runs: Dict[str, RunRecord] = {}
        self._events: Dict[str, list[Dict[str, Any]]] = {}
        self._conditions: Dict[str, asyncio.Condition] = {}
        self._writers_by_node: Dict[str, str] = {}
        self._stop_events: Dict[str, asyncio.Event] = {}
        self._idempotent_runs: Dict[str, tuple[str, str]] = {}
        self._pending_reservations: Dict[str, Dict[str, Any]] = {}
        self._unpublished_run_ids: set[str] = set()
        self._publication_tasks: Dict[str, asyncio.Task[RunRecord]] = {}
        self._published_reservation_ids: set[str] = set()
        self._interrupting_reservation_ids: set[str] = set()
        self._interruption_tasks: Dict[str, asyncio.Task[RunRecord]] = {}
        self._reservation_task_bound: set[str] = set()
        self._reservation_child_event_published: set[str] = set()
        self._finish_listeners: list[Callable[[Dict[str, Any]], None]] = []
        self._lock = asyncio.Lock()
        self._closing = False
        self._close_task: Optional[asyncio.Task[PendingReservationDrainResult]] = None
        self._hydrate_active_runs()

    def add_finish_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        self._finish_listeners.append(listener)

    async def create_run(
        self,
        *,
        conversation_id: str,
        kind: RunKind | str,
        anchor_node_id: Optional[str] = None,
        target_node_id: Optional[str] = None,
        created_by_run_id: Optional[str] = None,
        cancellation_parent_run_id: Optional[str] = None,
        summary: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        task_binding: Optional[Dict[str, Any]] = None,
    ) -> RunRecord:
        profiler = get_profiler()
        run_kind = kind if isinstance(kind, RunKind) else RunKind(str(kind))
        with profiler.span(
            "run.create",
            conversation_id=conversation_id,
            kind=run_kind.value,
            anchor_node_id=anchor_node_id,
            target_node_id=target_node_id,
            created_by_run_id=created_by_run_id,
        ):
            async with self._lock:
                self._ensure_creation_admitted_locked()
                if target_node_id:
                    existing = self._writers_by_node.get(target_node_id)
                    if existing:
                        existing_record = self._runs.get(existing)
                        if (
                            existing_record
                            and existing_record.status not in FINISHED_RUN_STATUSES
                        ):
                            raise RunWriterConflictError(
                                f"target node {target_node_id} already has active writer {existing}"
                            )
                if self.repository:
                    create_kwargs = {
                        "kind": run_kind.value,
                        "anchor_node_id": anchor_node_id,
                        "target_node_id": target_node_id,
                        "created_by_run_id": created_by_run_id,
                        "cancellation_parent_run_id": cancellation_parent_run_id,
                        "summary": summary,
                        "metadata": metadata,
                    }
                    if task_binding is not None:
                        create_kwargs["task_binding"] = task_binding
                    run_id = self.repository.create_run(conversation_id, **create_kwargs)
                    stored_run = self.repository.get_run(run_id) or {}
                    created_at = float(stored_run.get("created_at") or time())
                    updated_at = float(stored_run.get("updated_at") or created_at)
                    stored_metadata = dict(stored_run.get("metadata") or metadata or {})
                else:
                    run_id = f"run_{uuid.uuid4().hex}"
                    created_at = time()
                    updated_at = created_at
                    stored_metadata = dict(metadata or {})
                    if task_binding is not None:
                        stored_metadata["task_generation_id"] = task_binding.get("task_generation_id")
                        stored_metadata["task_step_position"] = task_binding.get("step_position")
                record = RunRecord(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    kind=run_kind,
                    status=RunStatus.RUNNING,
                    anchor_node_id=anchor_node_id,
                    target_node_id=target_node_id,
                    created_by_run_id=created_by_run_id,
                    cancellation_parent_run_id=cancellation_parent_run_id,
                    summary=summary,
                    metadata=stored_metadata,
                    created_at=created_at,
                    updated_at=updated_at,
                )
                if target_node_id:
                    self._writers_by_node[target_node_id] = run_id
                self._runs[run_id] = record
                self._events[run_id] = []
                self._conditions[run_id] = asyncio.Condition()
                self._stop_events[run_id] = asyncio.Event()
        if task_binding is not None and not self.repository:
            task_service = getattr(self, "task_service", None)
            if task_service is None:
                await self._discard_unpublished_run(run_id)
                raise RuntimeError("task service is required for bound runs")
            try:
                await task_service.bind_in_memory_run(run_id, task_binding)
            except Exception:
                await self._discard_unpublished_run(run_id)
                raise
        await self.append_event(run_id, {
            "type": "run_started",
            "run_id": run_id,
            "conversation_id": conversation_id,
            "kind": record.kind.value,
            "status": record.status.value,
            "anchor_node_id": anchor_node_id,
            "target_node_id": target_node_id,
            "created_by_run_id": created_by_run_id,
            "cancellation_parent_run_id": cancellation_parent_run_id,
            "summary": summary,
            "metadata": record.metadata,
            "created_at": record.created_at,
        })
        if created_by_run_id:
            await self._append_child_started_event(created_by_run_id, record)
        return record

    def _ensure_creation_admitted_locked(self) -> None:
        if self._closing:
            raise RunManagerClosingError("run manager is closing")

    def _is_hidden_run_id(self, run_id: str) -> bool:
        return run_id in self._pending_reservations or run_id in self._unpublished_run_ids

    def _assert_target_available_locked(self, target_node_id: Optional[str]) -> None:
        if not target_node_id:
            return
        existing = self._writers_by_node.get(target_node_id)
        if existing:
            existing_record = self._runs.get(existing)
            if existing_record and existing_record.status not in FINISHED_RUN_STATUSES:
                raise RunWriterConflictError(
                    f"target node {target_node_id} already has active writer {existing}"
                )
        for run_id, seed in self._pending_reservations.items():
            if seed.get("target_node_id") == target_node_id:
                raise RunWriterConflictError(
                    f"target node {target_node_id} already has active writer {run_id}"
                )

    def _memory_reservation_seed(
        self,
        *,
        run_id: str,
        conversation_id: str,
        kind: RunKind,
        anchor_node_id: Optional[str],
        target_node_id: Optional[str],
        created_by_run_id: Optional[str],
        cancellation_parent_run_id: Optional[str],
        summary: str,
        metadata: Optional[Dict[str, Any]],
        task_binding: Optional[Dict[str, Any]],
        idempotency_key: str,
        request_fingerprint: str,
    ) -> Dict[str, Any]:
        now = time()
        return {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "kind": kind.value,
            "status": RunStatus.RUNNING.value,
            "anchor_node_id": anchor_node_id,
            "target_node_id": target_node_id,
            "created_by_run_id": created_by_run_id,
            "cancellation_parent_run_id": cancellation_parent_run_id,
            "summary": summary,
            "event_count": 0,
            "metadata": metadata if metadata is not None else {},
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "task_binding": task_binding,
        }

    def _materialize_reserved_run_locked(self, run_id: str) -> RunRecord:
        existing = self._runs.get(run_id)
        if existing is not None:
            return existing
        seed = self._pending_reservations.get(run_id)
        if seed is None:
            raise RunNotFoundError(run_id)
        task_binding = seed.get("task_binding")
        if not self.repository and task_binding is not None:
            stored_metadata = dict(seed.get("metadata") or {})
            stored_metadata["task_generation_id"] = task_binding.get(
                "task_generation_id"
            )
            stored_metadata["task_step_position"] = task_binding.get(
                "step_position"
            )
            seed["metadata"] = stored_metadata
        record = self._hydrate_record(seed)
        self._events.setdefault(run_id, [])
        self._conditions.setdefault(run_id, asyncio.Condition())
        self._stop_events.setdefault(run_id, asyncio.Event())
        self._unpublished_run_ids.add(run_id)
        return record

    def _forget_missing_repository_run_locked(self, run_id: str) -> None:
        self._runs.pop(run_id, None)
        self._pending_reservations.pop(run_id, None)
        self._unpublished_run_ids.discard(run_id)
        self._published_reservation_ids.discard(run_id)
        self._reservation_task_bound.discard(run_id)
        self._reservation_child_event_published.discard(run_id)
        publication_task = self._publication_tasks.get(run_id)
        if publication_task is None or publication_task.done():
            self._publication_tasks.pop(run_id, None)
        interruption_task = self._interruption_tasks.get(run_id)
        if interruption_task is None or interruption_task.done():
            self._interruption_tasks.pop(run_id, None)
            self._interrupting_reservation_ids.discard(run_id)
        self._events.pop(run_id, None)
        self._conditions.pop(run_id, None)
        self._stop_events.pop(run_id, None)
        for target_node_id, writer_run_id in list(self._writers_by_node.items()):
            if writer_run_id == run_id:
                self._writers_by_node.pop(target_node_id, None)
        for idempotency_key, mapping in list(self._idempotent_runs.items()):
            if mapping[0] == run_id:
                self._idempotent_runs.pop(idempotency_key, None)

    def _existing_idempotent_run_locked(
        self,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> Optional[RunRecord]:
        mapping = self._idempotent_runs.get(idempotency_key)
        stored: Optional[Dict[str, Any]] = None
        if mapping is None and self.repository and hasattr(
            self.repository,
            "get_run_by_idempotency_key",
        ):
            stored = self.repository.get_run_by_idempotency_key(idempotency_key)
            if stored is not None:
                run_id = str(stored.get("run_id") or stored.get("id"))
                stored_fingerprint = str(stored.get("request_fingerprint") or "")
                mapping = (run_id, stored_fingerprint)
                self._idempotent_runs[idempotency_key] = mapping
        if mapping is None:
            return None
        run_id, stored_fingerprint = mapping
        if stored_fingerprint != request_fingerprint:
            raise RunIdempotencyConflictError(run_id)
        record = self._runs.get(run_id)
        if record is None and run_id in self._pending_reservations:
            record = self._materialize_reserved_run_locked(run_id)
        if record is None and self.repository:
            stored = stored or self.repository.get_run(run_id)
            if stored is not None:
                record = self._hydrate_record(stored)
        if record is not None and self.repository:
            self._ensure_events_loaded_locked(run_id)
        return record

    async def reserve_or_get_run(
        self,
        *,
        conversation_id: str,
        kind: RunKind | str,
        idempotency_key: str,
        request_fingerprint: str,
        anchor_node_id: Optional[str] = None,
        target_node_id: Optional[str] = None,
        created_by_run_id: Optional[str] = None,
        cancellation_parent_run_id: Optional[str] = None,
        summary: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        task_binding: Optional[Dict[str, Any]] = None,
        on_reserved: Optional[Callable[[str], None]] = None,
    ) -> tuple[RunRecord, bool]:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency key is required")
        if not isinstance(request_fingerprint, str) or not request_fingerprint:
            raise ValueError("request fingerprint is required")
        run_kind = kind if isinstance(kind, RunKind) else RunKind(str(kind))

        async with self._lock:
            self._ensure_creation_admitted_locked()
            existing = self._existing_idempotent_run_locked(
                idempotency_key,
                request_fingerprint,
            )
            if existing is not None:
                return deepcopy(existing), False

            self._assert_target_available_locked(target_node_id)
            if self.repository:
                create_kwargs: Dict[str, Any] = {
                    "kind": run_kind.value,
                    "idempotency_key": idempotency_key,
                    "request_fingerprint": request_fingerprint,
                    "anchor_node_id": anchor_node_id,
                    "target_node_id": target_node_id,
                    "created_by_run_id": created_by_run_id,
                    "cancellation_parent_run_id": cancellation_parent_run_id,
                    "summary": summary,
                    "metadata": metadata,
                }
                if task_binding is not None:
                    create_kwargs["task_binding"] = task_binding
                stored, created = self.repository.create_or_get_run(
                    conversation_id,
                    **create_kwargs,
                )
                seed = stored
                run_id = str(seed["run_id"])
                stored_fingerprint = str(seed["request_fingerprint"])
                if stored_fingerprint != request_fingerprint:
                    raise RunIdempotencyConflictError(run_id)
            else:
                run_id = f"run_{uuid.uuid4().hex}"
                created = True
                seed = self._memory_reservation_seed(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    kind=run_kind,
                    anchor_node_id=anchor_node_id,
                    target_node_id=target_node_id,
                    created_by_run_id=created_by_run_id,
                    cancellation_parent_run_id=cancellation_parent_run_id,
                    summary=summary,
                    metadata=metadata,
                    task_binding=task_binding,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )

            self._idempotent_runs[idempotency_key] = (run_id, request_fingerprint)
            if not created:
                seed = dict(seed)
                record = self._runs.get(run_id)
                if record is None:
                    record = self._hydrate_record(seed)
                    self._ensure_events_loaded_locked(run_id)
                return deepcopy(record), False

            seed["task_binding"] = task_binding
            self._pending_reservations[run_id] = seed
            if on_reserved is not None:
                on_reserved(run_id)
            record = self._materialize_reserved_run_locked(run_id)
            return deepcopy(record), True

    async def _discard_unpublished_run(self, run_id: str) -> None:
        async with self._lock:
            record = self._runs.pop(run_id, None)
            if record and record.target_node_id and self._writers_by_node.get(record.target_node_id) == run_id:
                self._writers_by_node.pop(record.target_node_id, None)
            self._events.pop(run_id, None)
            self._conditions.pop(run_id, None)
            self._stop_events.pop(run_id, None)

    async def _append_child_started_event(self, created_by_run_id: str, child: RunRecord) -> None:
        if not self.get_run(created_by_run_id):
            return
        await self.append_event(created_by_run_id, {
            "type": "child_run_started",
            "event_type": "child_run_started",
            "status": "content",
            "content": "",
            "child_run_id": child.run_id,
            "child_kind": child.kind.value,
            "child_status": child.status.value,
            "child_summary": child.summary,
            "payload": {
                "run_id": child.run_id,
                "kind": child.kind.value,
                "status": child.status.value,
                "summary": child.summary,
                "created_by_run_id": created_by_run_id,
                "cancellation_parent_run_id": child.cancellation_parent_run_id,
                "anchor_node_id": child.anchor_node_id,
                "target_node_id": child.target_node_id,
                "metadata": dict(child.metadata or {}),
            },
        })

    async def bind_target_node(self, run_id: str, target_node_id: str) -> RunRecord:
        async with self._lock:
            record = self._require_run_locked(run_id)
            if record.target_node_id == target_node_id:
                return deepcopy(record)
            previous_writer = self._writers_by_node.get(target_node_id)
            self._acquire_writer_locked(target_node_id, run_id)
            persisted: Optional[Dict[str, Any]] = None
            try:
                if self.repository and hasattr(self.repository, "update_target_node"):
                    persisted = self.repository.update_target_node(run_id, target_node_id)
            except Exception:
                if previous_writer is None:
                    if self._writers_by_node.get(target_node_id) == run_id:
                        self._writers_by_node.pop(target_node_id, None)
                else:
                    self._writers_by_node[target_node_id] = previous_writer
                raise
            if record.target_node_id and self._writers_by_node.get(record.target_node_id) == run_id:
                self._writers_by_node.pop(record.target_node_id, None)
            record.target_node_id = target_node_id
            record.updated_at = float((persisted or {}).get("updated_at") or time())
            snapshot = deepcopy(record)
        await self.append_event(run_id, {
            "type": "run_target_bound",
            "run_id": run_id,
            "target_node_id": target_node_id,
        })
        return snapshot

    async def update_cancellation_parent(
        self,
        run_id: str,
        cancellation_parent_run_id: Optional[str],
    ) -> RunRecord:
        async with self._lock:
            record = self._require_run_locked(run_id)
            if record.cancellation_parent_run_id == cancellation_parent_run_id:
                return deepcopy(record)
            persisted: Optional[Dict[str, Any]] = None
            if self.repository and hasattr(self.repository, "update_cancellation_parent"):
                persisted = self.repository.update_cancellation_parent(run_id, cancellation_parent_run_id)
            record.cancellation_parent_run_id = cancellation_parent_run_id
            record.updated_at = float((persisted or {}).get("updated_at") or time())
            snapshot = deepcopy(record)
        await self.append_event(run_id, {
            "type": "run_cancellation_parent_updated",
            "run_id": run_id,
            "cancellation_parent_run_id": cancellation_parent_run_id,
        })
        return snapshot

    async def append_event(self, run_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        payloads = await self.append_events(run_id, [payload])
        return payloads[0]

    async def flush_run_events(self, run_id: str) -> None:
        if self._event_writer is not None:
            await self._event_writer.flush_run(run_id)

    async def flush_events(self) -> None:
        if self._event_writer is not None:
            await self._event_writer.flush_all()

    def _has_cached_event_locked(self, run_id: str, event_type: str) -> bool:
        return any(
            event.get("payload", {}).get("type") == event_type
            for event in self._events.get(run_id, [])
        )

    async def _publish_reserved_run_once(self, run_id: str) -> RunRecord:
        current_task = asyncio.current_task()
        async with self._lock:
            if run_id in self._interrupting_reservation_ids:
                raise RuntimeError(f"reservation interruption in progress for run {run_id}")
            if not self._is_hidden_run_id(run_id):
                if run_id not in self._published_reservation_ids:
                    raise RuntimeError(f"run {run_id} is not a reserved run")
                record = self._runs.get(run_id) or self._hydrate_run_locked(run_id)
                if record is None:
                    raise RunNotFoundError(run_id)
                return deepcopy(record)
            record = self._materialize_reserved_run_locked(run_id)
            seed = deepcopy(self._pending_reservations.get(run_id) or {})
            task_binding = seed.get("task_binding")
            has_started_event = self._has_cached_event_locked(run_id, "run_started")

        if task_binding is not None and not self.repository:
            task_service = getattr(self, "task_service", None)
            if task_service is None:
                raise RuntimeError("task service is required for bound runs")
            if run_id not in self._reservation_task_bound:
                await task_service.bind_in_memory_run(run_id, task_binding)
                self._reservation_task_bound.add(run_id)

        if not has_started_event:
            await self.append_event(run_id, {
                "type": "run_started",
                "run_id": run_id,
                "conversation_id": record.conversation_id,
                "kind": record.kind.value,
                "status": record.status.value,
                "anchor_node_id": record.anchor_node_id,
                "target_node_id": record.target_node_id,
                "created_by_run_id": record.created_by_run_id,
                "cancellation_parent_run_id": record.cancellation_parent_run_id,
                "summary": record.summary,
                "metadata": record.metadata,
                "created_at": record.created_at,
            })
        if (
            record.created_by_run_id
            and run_id not in self._reservation_child_event_published
        ):
            await self._append_child_started_event(record.created_by_run_id, record)
            self._reservation_child_event_published.add(run_id)
        await self.flush_run_events(run_id)

        async with self._lock:
            if self._closing:
                raise RunManagerClosingError("run manager is closing")
            if run_id in self._interrupting_reservation_ids:
                raise RuntimeError(f"reservation interruption in progress for run {run_id}")
            record = self._runs.get(run_id)
            if record is None:
                raise RunNotFoundError(run_id)
            self._pending_reservations.pop(run_id, None)
            self._unpublished_run_ids.discard(run_id)
            self._published_reservation_ids.add(run_id)
            self._reservation_task_bound.discard(run_id)
            self._reservation_child_event_published.discard(run_id)
            if self._publication_tasks.get(run_id) is current_task:
                self._publication_tasks.pop(run_id, None)
            return deepcopy(record)

    async def publish_reserved_run(self, run_id: str) -> RunRecord:
        async with self._lock:
            if run_id in self._interrupting_reservation_ids:
                raise RuntimeError(f"reservation interruption in progress for run {run_id}")
            if not self._is_hidden_run_id(run_id):
                if run_id not in self._published_reservation_ids:
                    raise RuntimeError(f"run {run_id} is not a reserved run")
                record = self._runs.get(run_id) or self._hydrate_run_locked(run_id)
                if record is None:
                    raise RunNotFoundError(run_id)
                return deepcopy(record)
            task = self._publication_tasks.get(run_id)
            if task is None:
                task = asyncio.create_task(self._publish_reserved_run_once(run_id))
                self._publication_tasks[run_id] = task
        return await asyncio.shield(task)

    async def _interrupt_reserved_run_once(
        self,
        run_id: str,
        error: Optional[str],
    ) -> RunRecord:
        current_task = asyncio.current_task()
        try:
            async with self._lock:
                publication_task = self._publication_tasks.get(run_id)
            if publication_task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    if publication_task.done():
                        publication_task.result()
                    else:
                        await asyncio.shield(publication_task)

            async with self._lock:
                record = self._runs.get(run_id)
                if record is None and run_id in self._pending_reservations:
                    record = self._materialize_reserved_run_locked(run_id)
                if record is None:
                    record = self._hydrate_run_locked(run_id)
                if record is None:
                    raise RunNotFoundError(run_id)
                hidden = self._is_hidden_run_id(run_id)
                if not hidden:
                    if record.status in FINISHED_RUN_STATUSES:
                        return deepcopy(record)
                    raise RuntimeError(f"run {run_id} is not a pending reservation")

            interrupted = await self.finish_run(
                run_id,
                RunStatus.INTERRUPTED,
                error,
                _notify_listeners=False,
            )
            async with self._lock:
                was_hidden = self._is_hidden_run_id(run_id)
                self._pending_reservations.pop(run_id, None)
                self._unpublished_run_ids.discard(run_id)
                self._publication_tasks.pop(run_id, None)
                self._reservation_task_bound.discard(run_id)
                self._reservation_child_event_published.discard(run_id)
                self._interrupting_reservation_ids.discard(run_id)
                condition = self._conditions.setdefault(run_id, asyncio.Condition())
                self._stop_events.setdefault(run_id, asyncio.Event()).set()
            if was_hidden:
                async with condition:
                    condition.notify_all()
                snapshot = interrupted.to_dict()
                for listener in list(self._finish_listeners):
                    listener(snapshot)
            return interrupted
        finally:
            async with self._lock:
                if self._interruption_tasks.get(run_id) is current_task:
                    self._interruption_tasks.pop(run_id, None)
                    self._interrupting_reservation_ids.discard(run_id)

    async def interrupt_reserved_run(
        self,
        run_id: str,
        error: Optional[str],
    ) -> RunRecord:
        async with self._lock:
            if not self._is_hidden_run_id(run_id):
                record = self._runs.get(run_id) or self._hydrate_run_locked(run_id)
                if record is None:
                    raise RunNotFoundError(run_id)
                if record.status in FINISHED_RUN_STATUSES:
                    return deepcopy(record)
                raise RuntimeError(f"run {run_id} is not a pending reservation")
            task = self._interruption_tasks.get(run_id)
            if task is None:
                self._interrupting_reservation_ids.add(run_id)
                task = asyncio.create_task(
                    self._interrupt_reserved_run_once(run_id, error)
                )
                self._interruption_tasks[run_id] = task
        return await asyncio.shield(task)

    async def drain_pending_reservations(
        self,
        timeout: float = 5.0,
    ) -> PendingReservationDrainResult:
        async with self._lock:
            pending_ids = tuple(sorted(self._pending_reservations))
        if not pending_ids:
            return PendingReservationDrainResult((), ())

        tasks = {
            run_id: asyncio.create_task(
                self.interrupt_reserved_run(run_id, "run manager is closing")
            )
            for run_id in pending_ids
        }
        done, _pending = await asyncio.wait(
            set(tasks.values()),
            timeout=max(0.0, float(timeout)),
        )
        for task in done:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                task.result()
        for task in _pending:
            task.add_done_callback(
                lambda completed: completed.exception()
                if not completed.cancelled()
                else None
            )
        async with self._lock:
            exhausted = tuple(
                run_id for run_id in pending_ids
                if run_id in self._pending_reservations
            )
        return PendingReservationDrainResult(pending_ids, exhausted)

    async def _close_after_admission(
        self,
        timeout: float,
    ) -> PendingReservationDrainResult:
        result = await self.drain_pending_reservations(timeout=timeout)
        if result.exhausted_run_ids:
            return result
        await self.flush_events()
        if self._event_writer is not None:
            await self._event_writer.close()
        return result

    async def close(
        self,
        timeout: float = 5.0,
    ) -> PendingReservationDrainResult:
        async with self._lock:
            self._closing = True
            task = self._close_task
            if task is None:
                task = asyncio.create_task(self._close_after_admission(timeout))
                self._close_task = task
        result = await asyncio.shield(task)
        if result.exhausted_run_ids:
            async with self._lock:
                if self._close_task is task:
                    self._close_task = None
        return result

    async def append_events(self, run_id: str, payloads: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        if not payloads:
            return []
        profiler = get_profiler()
        writer_events: list[Dict[str, Any]] = []
        journal_events: list[Dict[str, Any]] = []
        journal_conversation_id: Optional[str] = None
        returned_payloads: list[Dict[str, Any]] = []
        condition: Optional[asyncio.Condition] = None
        with profiler.span(
            "run.append_event" if len(payloads) == 1 else "run.append_events",
            run_id=run_id,
            batch_size=len(payloads),
            status=payloads[-1].get("status"),
            event_type=payloads[-1].get("event_type") or payloads[-1].get("type"),
        ):
            async with self._lock:
                record = self._require_run_locked(run_id)
                if self.repository and self._event_writer is None:
                    normalized_payloads = []
                    for item in payloads:
                        payload = dict(item)
                        payload.setdefault("run_id", run_id)
                        payload.setdefault("conversation_id", record.conversation_id)
                        payload.setdefault("kind", record.kind.value)
                        payload.setdefault("target_node_id", record.target_node_id)
                        normalized_payloads.append(public_run_event(payload))
                    if hasattr(self.repository, "append_events"):
                        persisted_events = self.repository.append_events(run_id, normalized_payloads)
                    else:
                        persisted_events = [
                            self.repository.append_event(run_id, payload)
                            for payload in normalized_payloads
                        ]
                    for persisted in persisted_events:
                        payload = deepcopy(persisted["payload"])
                        event = {
                            "run_id": run_id,
                            "event_index": persisted["event_index"],
                            "payload": payload,
                            "created_at": persisted["created_at"],
                        }
                        record.event_count = max(record.event_count, event["event_index"] + 1)
                        record.updated_at = event["created_at"]
                        self._events.setdefault(run_id, []).append(event)
                        returned_payloads.append(payload)
                else:
                    for item in payloads:
                        event_index = record.event_count
                        payload = dict(item)
                        payload.setdefault("run_id", run_id)
                        payload.setdefault("conversation_id", record.conversation_id)
                        payload.setdefault("kind", record.kind.value)
                        payload.setdefault("target_node_id", record.target_node_id)
                        payload["event_index"] = event_index
                        payload = public_run_event(payload)
                        event = {
                            "run_id": run_id,
                            "event_index": event_index,
                            "payload": payload,
                            "created_at": time(),
                        }
                        record.event_count = event_index + 1
                        record.updated_at = event["created_at"]
                        self._events.setdefault(run_id, []).append(event)
                        returned_payloads.append(payload)
                        if self._event_writer is not None:
                            writer_events.append(event)
                        else:
                            journal_events.append(event)
                            journal_conversation_id = record.conversation_id
                if writer_events and self._event_writer is not None:
                    await self._event_writer.enqueue_many(writer_events)
                    writer_events = []
                condition = self._conditions.setdefault(run_id, asyncio.Condition())
        if journal_events and journal_conversation_id is not None:
            for journal_event in journal_events:
                self.journal.append_event(journal_conversation_id, run_id, journal_event)
        if condition is not None:
            async with condition:
                condition.notify_all()
        return returned_payloads

    async def subscribe(self, run_id: str, from_event: int = 0) -> AsyncIterator[Dict[str, Any]]:
        profiler = get_profiler()
        index = max(0, int(from_event or 0))
        replayed = 0
        waited = 0
        while True:
            with profiler.span("run.subscribe.poll", run_id=run_id, from_event=from_event):
                async with self._lock:
                    if self._is_hidden_run_id(run_id):
                        raise RunNotFoundError(run_id)
                    record = self._runs.get(run_id) or self._hydrate_run_locked(run_id)
                    if not record:
                        raise RunNotFoundError(run_id)
                    self._ensure_events_loaded_locked(run_id)
                    events = self._events.setdefault(run_id, [])
                    condition = self._conditions.setdefault(run_id, asyncio.Condition())
                    finished = record.status in FINISHED_RUN_STATUSES
                    if index < len(events):
                        event = events[index]
                        index += 1
                        payload = deepcopy(event["payload"])
                    elif finished:
                        break
                    else:
                        payload = None
            if payload is not None:
                replayed += 1
                yield public_run_event(payload)
                continue
            async with condition:
                waited += 1
                await condition.wait()
        profiler.mark("run.subscribe.done", run_id=run_id, from_event=from_event, replayed=replayed, waited=waited)

    async def wait_for_terminal_result(
        self,
        run_id: str,
        *,
        result_event_types: Iterable[str],
        error_event_types: Iterable[str] = (),
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        result_types = {str(item) for item in result_event_types}
        error_types = {str(item) for item in error_event_types}

        async def wait() -> Dict[str, Any]:
            result_payload: Optional[Dict[str, Any]] = None
            error_payload: Optional[Dict[str, Any]] = None
            final_payload: Optional[Dict[str, Any]] = None
            async for payload in self.subscribe(run_id, 0):
                event_type = str(payload.get("event_type") or "")
                if event_type in result_types:
                    result_payload = payload
                elif event_type in error_types:
                    error_payload = payload
                if payload.get("type") == "run_finished":
                    final_payload = payload
                    break

            run = self.get_run(run_id) or {}
            status = (
                (final_payload or {}).get("status")
                or run.get("status")
                or RunStatus.COMPLETED.value
            )
            selected = error_payload or result_payload or final_payload or {}
            message_type = "terminal"
            if error_payload is not None:
                message_type = "error"
            elif result_payload is not None:
                message_type = "result"
            return {
                "run_id": run_id,
                "conversation_id": run.get("conversation_id"),
                "kind": run.get("kind"),
                "status": status,
                "message_type": message_type,
                "content": selected.get("content") or "",
                "result": selected.get("result"),
                "error": selected.get("error"),
                "event_type": selected.get("event_type") or selected.get("type"),
                "event": deepcopy(selected),
                "finished_at": run.get("finished_at") or (final_payload or {}).get("finished_at"),
            }

        if timeout is not None:
            return await asyncio.wait_for(wait(), timeout=timeout)
        return await wait()

    async def finish_run(
        self,
        run_id: str,
        status: RunStatus | str = RunStatus.COMPLETED,
        error: Optional[str] = None,
        *,
        _notify_listeners: bool = True,
    ) -> RunRecord:
        profiler = get_profiler()
        run_status = status if isinstance(status, RunStatus) else RunStatus(str(status))
        if run_status not in FINISHED_RUN_STATUSES:
            raise ValueError(f"finish_run requires finished status, got {run_status}")
        journal_event: Optional[Dict[str, Any]] = None
        journal_conversation_id: Optional[str] = None
        with profiler.span("run.finish", run_id=run_id, status=run_status.value, has_error=bool(error)):
            async with self._lock:
                record = self._require_run_locked(run_id)
                if record.status in FINISHED_RUN_STATUSES:
                    return deepcopy(record)
                if self._event_writer is not None:
                    await self._event_writer.flush_run(run_id)
                event_index = record.event_count
                condition = self._conditions.setdefault(run_id, asyncio.Condition())
                if self.repository:
                    persisted_run = self.repository.finish_run(run_id, run_status.value, error)
                    persisted_events = self.repository.read_events(run_id, event_index)
                    persisted_status = RunStatus(str(persisted_run.get("status") or run_status.value))
                    if persisted_status not in FINISHED_RUN_STATUSES:
                        raise RuntimeError(f"repository did not finish run {run_id}")
                    record.status = persisted_status
                    record.finished_at = float(persisted_run.get("finished_at") or time())
                    record.updated_at = float(persisted_run.get("updated_at") or record.finished_at)
                    record.metadata = dict(persisted_run.get("metadata") or record.metadata)
                    if persisted_events:
                        event = {
                            "run_id": run_id,
                            "event_index": persisted_events[0]["event_index"],
                            "payload": deepcopy(persisted_events[0]["payload"]),
                            "created_at": persisted_events[0]["created_at"],
                        }
                    else:
                        payload = {
                            "type": "run_finished",
                            "run_id": run_id,
                            "conversation_id": record.conversation_id,
                            "kind": record.kind.value,
                            "target_node_id": record.target_node_id,
                            "status": persisted_status.value,
                            "error": error,
                            "finished_at": record.finished_at,
                            "event_index": event_index,
                        }
                        event = {
                            "run_id": run_id,
                            "event_index": event_index,
                            "payload": payload,
                            "created_at": record.finished_at,
                        }
                    record.event_count = int(persisted_run.get("event_count", event["event_index"] + 1))
                else:
                    finished_at = time()
                    next_metadata = dict(record.metadata)
                    if error:
                        next_metadata["error"] = error
                    task_service = getattr(self, "task_service", None)
                    should_finalize_task = (
                        not self._is_hidden_run_id(run_id)
                        or run_id in self._reservation_task_bound
                    )
                    if task_service is not None and should_finalize_task:
                        proposed_run = record.to_dict()
                        proposed_run.update({
                            "status": run_status.value,
                            "finished_at": finished_at,
                            "updated_at": finished_at,
                            "metadata": next_metadata,
                        })
                        task_outcome = await task_service.handle_run_finished(proposed_run)
                        if task_outcome is not None:
                            next_metadata["task_outcome"] = task_outcome.public_dict()
                    record.status = run_status
                    record.finished_at = finished_at
                    record.updated_at = finished_at
                    record.metadata = next_metadata
                    payload = {
                        "type": "run_finished",
                        "run_id": run_id,
                        "conversation_id": record.conversation_id,
                        "kind": record.kind.value,
                        "target_node_id": record.target_node_id,
                        "status": run_status.value,
                        "error": error,
                        "finished_at": record.finished_at,
                        "event_index": event_index,
                    }
                    event = {
                        "run_id": run_id,
                        "event_index": event_index,
                        "payload": payload,
                        "created_at": record.finished_at,
                    }
                    record.event_count += 1
                    journal_event = event
                    journal_conversation_id = record.conversation_id
                if record.target_node_id and self._writers_by_node.get(record.target_node_id) == run_id:
                    self._writers_by_node.pop(record.target_node_id, None)
                self._events.setdefault(run_id, []).append(event)
                snapshot = deepcopy(record)
        if journal_event is not None and journal_conversation_id is not None:
            self.journal.append_event(journal_conversation_id, run_id, journal_event)
        snapshot_dict = snapshot.to_dict()
        async with condition:
            condition.notify_all()
        if _notify_listeners:
            for listener in list(self._finish_listeners):
                listener(snapshot_dict)
        return snapshot

    async def request_stop(self, run_id: str) -> bool:
        repository_event: Optional[Dict[str, Any]] = None
        async with self._lock:
            record = self._runs.get(run_id)
            if not record or record.status in FINISHED_RUN_STATUSES:
                return False
            event_index = record.event_count
            record.status = RunStatus.STOPPING
            record.updated_at = time()
            stop_event = self._stop_events.setdefault(run_id, asyncio.Event())
            stop_event.set()
            condition = self._conditions.setdefault(run_id, asyncio.Condition())
            if self.repository:
                if self._event_writer is not None:
                    await self._event_writer.flush_run(run_id)
                if not self.repository.request_stop(run_id):
                    return False
                persisted_run = self.repository.get_run(run_id) or {}
                persisted_events = self.repository.read_events(run_id, event_index)
                record.event_count = int(persisted_run.get("event_count", record.event_count))
                record.updated_at = float(persisted_run.get("updated_at") or record.updated_at)
                if persisted_events:
                    repository_event = {
                        "run_id": run_id,
                        "event_index": persisted_events[0]["event_index"],
                        "payload": deepcopy(persisted_events[0]["payload"]),
                        "created_at": persisted_events[0]["created_at"],
                    }
                    self._events.setdefault(run_id, []).append(repository_event)
        if repository_event is not None:
            async with condition:
                condition.notify_all()
            return True
        await self.append_event(run_id, {
            "type": "run_stop_requested",
            "run_id": run_id,
            "status": RunStatus.STOPPING.value,
        })
        return True

    async def update_status(self, run_id: str, status: RunStatus | str) -> RunRecord:
        run_status = status if isinstance(status, RunStatus) else RunStatus(str(status))
        if run_status in FINISHED_RUN_STATUSES:
            raise ValueError(f"update_status requires non-finished status, got {run_status}")
        async with self._lock:
            record = self._require_run_locked(run_id)
            if record.status in FINISHED_RUN_STATUSES:
                return deepcopy(record)
            if record.status == run_status:
                return deepcopy(record)
            record.status = run_status
            record.updated_at = time()
            snapshot = deepcopy(record)
        await self.append_event(run_id, {
            "type": "run_status_changed",
            "run_id": run_id,
            "status": run_status.value,
        })
        return snapshot

    async def update_metadata(self, run_id: str, metadata: Dict[str, Any]) -> RunRecord:
        async with self._lock:
            record = self._require_run_locked(run_id)
            record.metadata.update(dict(metadata or {}))
            record.updated_at = time()
            snapshot = deepcopy(record)
        await self.append_event(run_id, {
            "type": "run_metadata_updated",
            "run_id": run_id,
            "metadata": dict(metadata or {}),
        })
        return snapshot

    async def mark_observed(
        self,
        run_id: str,
        *,
        observer_run_id: Optional[str] = None,
        via: str,
    ) -> RunRecord:
        metadata: Dict[str, Any] = {
            "result_observed_at": time(),
            "result_observed_via": via,
        }
        if observer_run_id:
            metadata["result_observed_by_run_id"] = observer_run_id
        updated = await self.update_metadata(run_id, metadata)
        service = getattr(self, "notification_service", None)
        if service is not None:
            service.mark_observed_for_run(run_id)
        return updated

    def stop_event(self, run_id: str) -> asyncio.Event:
        return self._stop_events.setdefault(run_id, asyncio.Event())

    async def is_stop_requested(self, run_id: str) -> bool:
        return self.stop_event(run_id).is_set()

    def list_active(self, conversation_id: Optional[str] = None) -> list[Dict[str, Any]]:
        if self.repository and hasattr(self.repository, "list_active"):
            return [
                self._normalize_repository_run(run)
                for run in self.repository.list_active(conversation_id)
                if not self._is_hidden_run_id(
                    str(run.get("run_id") or run.get("id"))
                )
            ]
        return [
            record.to_dict()
            for run_id, record in self._runs.items()
            if record.status not in FINISHED_RUN_STATUSES
            and not self._is_hidden_run_id(run_id)
            and (conversation_id is None or record.conversation_id == conversation_id)
        ]

    def list_active_cancellation_children(
        self,
        *,
        cancellation_parent_run_id: str,
        conversation_id: Optional[str] = None,
    ) -> list[Dict[str, Any]]:
        if self.repository and hasattr(self.repository, "list_active"):
            return [
                run
                for run in (
                    self._normalize_repository_run(item)
                    for item in self.repository.list_active(conversation_id)
                    if not self._is_hidden_run_id(
                        str(item.get("run_id") or item.get("id"))
                    )
                )
                if run.get("cancellation_parent_run_id") == cancellation_parent_run_id
            ]
        return [
            record.to_dict()
            for run_id, record in self._runs.items()
            if record.status not in FINISHED_RUN_STATUSES
            and not self._is_hidden_run_id(run_id)
            and record.cancellation_parent_run_id == cancellation_parent_run_id
            and (conversation_id is None or record.conversation_id == conversation_id)
        ]

    def list_runs(self, conversation_id: Optional[str] = None) -> list[Dict[str, Any]]:
        if self.repository and hasattr(self.repository, "list_runs"):
            return [
                self._normalize_repository_run(run)
                for run in self.repository.list_runs(conversation_id)
                if not self._is_hidden_run_id(
                    str(run.get("run_id") or run.get("id"))
                )
            ]
        return [
            record.to_dict()
            for run_id, record in self._runs.items()
            if not self._is_hidden_run_id(run_id)
            if conversation_id is None or record.conversation_id == conversation_id
        ]

    def find_active_by_target(
        self,
        *,
        conversation_id: str,
        target_node_id: str,
        kind: Optional[RunKind | str] = None,
    ) -> Optional[Dict[str, Any]]:
        if self.repository and hasattr(self.repository, "list_active"):
            expected_kind_value = (
                kind.value if isinstance(kind, RunKind) else str(kind) if kind is not None else None
            )
            for run in self.repository.list_active(conversation_id):
                if self._is_hidden_run_id(str(run.get("run_id") or run.get("id"))):
                    continue
                normalized = self._normalize_repository_run(run)
                if normalized.get("target_node_id") != target_node_id:
                    continue
                if expected_kind_value is not None and normalized.get("kind") != expected_kind_value:
                    continue
                return normalized
            return None
        expected_kind = kind if isinstance(kind, RunKind) or kind is None else RunKind(str(kind))
        for run_id, record in self._runs.items():
            if self._is_hidden_run_id(run_id):
                continue
            if record.status in FINISHED_RUN_STATUSES:
                continue
            if record.conversation_id != conversation_id:
                continue
            if record.target_node_id != target_node_id:
                continue
            if expected_kind is not None and record.kind != expected_kind:
                continue
            return record.to_dict()
        return None

    def find_active_by_anchor(
        self,
        *,
        conversation_id: str,
        anchor_node_id: str,
        kind: Optional[RunKind | str] = None,
    ) -> Optional[Dict[str, Any]]:
        if self.repository and hasattr(self.repository, "list_active"):
            expected_kind_value = (
                kind.value if isinstance(kind, RunKind) else str(kind) if kind is not None else None
            )
            for run in self.repository.list_active(conversation_id):
                if self._is_hidden_run_id(str(run.get("run_id") or run.get("id"))):
                    continue
                normalized = self._normalize_repository_run(run)
                if normalized.get("anchor_node_id") != anchor_node_id:
                    continue
                if expected_kind_value is not None and normalized.get("kind") != expected_kind_value:
                    continue
                return normalized
            return None
        expected_kind = kind if isinstance(kind, RunKind) or kind is None else RunKind(str(kind))
        for run_id, record in self._runs.items():
            if self._is_hidden_run_id(run_id):
                continue
            if record.status in FINISHED_RUN_STATUSES:
                continue
            if record.conversation_id != conversation_id:
                continue
            if record.anchor_node_id != anchor_node_id:
                continue
            if expected_kind is not None and record.kind != expected_kind:
                continue
            return record.to_dict()
        return None

    def active_runs_for_targets(
        self,
        *,
        conversation_id: str,
        target_node_ids: Iterable[str],
    ) -> list[Dict[str, Any]]:
        targets = set(target_node_ids)
        if self.repository and hasattr(self.repository, "list_active"):
            return [
                run
                for run in (
                    self._normalize_repository_run(item)
                    for item in self.repository.list_active(conversation_id)
                    if not self._is_hidden_run_id(
                        str(item.get("run_id") or item.get("id"))
                    )
                )
                if run.get("target_node_id") in targets
            ]
        return [
            record.to_dict()
            for run_id, record in self._runs.items()
            if record.status not in FINISHED_RUN_STATUSES
            and not self._is_hidden_run_id(run_id)
            and record.conversation_id == conversation_id
            and record.target_node_id in targets
        ]

    async def get_run_for_recovery(self, run_id: str) -> Optional[RunRecord]:
        """Return hidden or terminal run state for internal recovery only."""

        async with self._lock:
            if self.repository and hasattr(self.repository, "get_run"):
                stored = self.repository.get_run(run_id)
                if stored is None:
                    active_recovery_task = any(
                        task is not None and not task.done()
                        for task in (
                            self._publication_tasks.get(run_id),
                            self._interruption_tasks.get(run_id),
                        )
                    )
                    self._forget_missing_repository_run_locked(run_id)
                    if self._event_writer is not None and not active_recovery_task:
                        await self._event_writer.discard_run(run_id)
                    return None
                return deepcopy(self._hydrate_record(stored))

            record = self._runs.get(run_id)
            if record is None and run_id in self._pending_reservations:
                record = self._materialize_reserved_run_locked(run_id)
            return deepcopy(record) if record is not None else None

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        if self._is_hidden_run_id(run_id):
            return None
        record = self._runs.get(run_id)
        if record:
            return record.to_dict()
        if self.repository and hasattr(self.repository, "get_run"):
            run = self.repository.get_run(run_id)
            if run:
                normalized = self._normalize_repository_run(run)
                if normalized["status"] not in {status.value for status in FINISHED_RUN_STATUSES}:
                    self._hydrate_record(run)
                return normalized
        return None

    def read_events(self, run_id: str, from_event: int = 0) -> list[Dict[str, Any]]:
        if self._is_hidden_run_id(run_id):
            raise RunNotFoundError(run_id)
        start = max(0, int(from_event or 0))
        record = self._runs.get(run_id)
        cached = list(self._events.get(run_id, []))
        if record is not None:
            if len(cached) >= record.event_count or not self.repository:
                return [public_run_event(event["payload"]) for event in cached[start:]]
            if self.repository and hasattr(self.repository, "read_events"):
                stored = {
                    int(event["event_index"]): {
                        "run_id": event["run_id"],
                        "event_index": event["event_index"],
                        "payload": deepcopy(event["payload"]),
                        "created_at": event["created_at"],
                    }
                    for event in self.repository.read_events(run_id, 0)
                }
                for event in cached:
                    stored[int(event["event_index"])] = event
                merged = [stored[index] for index in sorted(stored) if index >= start]
                return [public_run_event(event["payload"]) for event in merged]
        if self.repository and hasattr(self.repository, "read_events"):
            if not self.get_run(run_id):
                raise RunNotFoundError(run_id)
            return [
                public_run_event(event["payload"])
                for event in self.repository.read_events(run_id, start)
            ]
        raise RunNotFoundError(run_id)

    def _require_run_locked(self, run_id: str) -> RunRecord:
        record = self._runs.get(run_id) or self._hydrate_run_locked(run_id)
        if not record:
            raise RunNotFoundError(run_id)
        return record

    def _acquire_writer_locked(self, target_node_id: str, run_id: str) -> None:
        existing = self._writers_by_node.get(target_node_id)
        if existing and existing != run_id:
            existing_record = self._runs.get(existing)
            if existing_record and existing_record.status not in FINISHED_RUN_STATUSES:
                raise RunWriterConflictError(
                    f"target node {target_node_id} already has active writer {existing}"
                )
        self._writers_by_node[target_node_id] = run_id

    def _hydrate_active_runs(self) -> None:
        if not self.repository or not hasattr(self.repository, "list_active"):
            return
        for run in self.repository.list_active():
            self._hydrate_record(run)

    def _hydrate_run_locked(self, run_id: str) -> Optional[RunRecord]:
        if not self.repository or not hasattr(self.repository, "get_run"):
            return None
        run = self.repository.get_run(run_id)
        if not run:
            return None
        return self._hydrate_record(run)

    def _hydrate_record(self, run: Dict[str, Any]) -> RunRecord:
        run_id = str(run.get("run_id") or run.get("id"))
        idempotency_key = run.get("idempotency_key")
        request_fingerprint = run.get("request_fingerprint")
        if idempotency_key is not None and request_fingerprint is not None:
            self._idempotent_runs[str(idempotency_key)] = (
                run_id,
                str(request_fingerprint),
            )
        existing = self._runs.get(run_id)
        status = RunStatus(str(run.get("status") or RunStatus.RUNNING.value))
        kind = RunKind(str(run.get("kind") or RunKind.CHAT.value))
        if existing:
            existing.status = status
            existing.event_count = int(run.get("event_count") or existing.event_count or 0)
            existing.updated_at = float(run.get("updated_at") or existing.updated_at)
            existing.finished_at = (
                float(run["finished_at"]) if run.get("finished_at") is not None else None
            )
            existing.metadata = dict(run.get("metadata") or {})
            record = existing
        else:
            record = RunRecord(
                run_id=run_id,
                conversation_id=str(run.get("conversation_id") or ""),
                kind=kind,
                status=status,
                anchor_node_id=run.get("anchor_node_id"),
                target_node_id=run.get("target_node_id"),
                created_by_run_id=run.get("created_by_run_id"),
                cancellation_parent_run_id=run.get("cancellation_parent_run_id"),
                summary=str(run.get("summary") or ""),
                event_count=int(run.get("event_count") or 0),
                metadata=dict(run.get("metadata") or {}),
                created_at=float(run.get("created_at") or time()),
                updated_at=float(run.get("updated_at") or time()),
                finished_at=(
                    float(run["finished_at"]) if run.get("finished_at") is not None else None
                ),
            )
            self._runs[run_id] = record
            self._conditions.setdefault(run_id, asyncio.Condition())
            self._stop_events.setdefault(run_id, asyncio.Event())
        if record.target_node_id and record.status not in FINISHED_RUN_STATUSES:
            self._writers_by_node[record.target_node_id] = run_id
        return record

    def _ensure_events_loaded_locked(self, run_id: str) -> None:
        if not self.repository or not hasattr(self.repository, "read_events"):
            return
        record = self._runs.get(run_id)
        if not record:
            return
        cached = self._events.get(run_id)
        if cached is not None and len(cached) >= record.event_count:
            return
        loaded = [
            {
                "run_id": event["run_id"],
                "event_index": event["event_index"],
                "payload": deepcopy(event["payload"]),
                "created_at": event["created_at"],
            }
            for event in self.repository.read_events(run_id, 0)
        ]
        if cached:
            merged = {int(event["event_index"]): event for event in loaded}
            for event in cached:
                merged[int(event["event_index"])] = event
            loaded = [merged[index] for index in sorted(merged)]
        self._events[run_id] = loaded

    def _normalize_repository_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(run)
        data["run_id"] = str(data.get("run_id") or data.get("id"))
        data["kind"] = str(data.get("kind") or RunKind.CHAT.value)
        data["status"] = str(data.get("status") or RunStatus.RUNNING.value)
        data["created_by_run_id"] = data.get("created_by_run_id") or None
        data["cancellation_parent_run_id"] = data.get("cancellation_parent_run_id") or None
        data["metadata"] = dict(data.get("metadata") or {})
        return public_run_dict(data)
