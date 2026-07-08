from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from time import time
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Optional

from .journal import RunJournal
from .types import FINISHED_RUN_STATUSES, RunKind, RunRecord, RunStatus


class RunNotFoundError(Exception):
    pass


class RunWriterConflictError(Exception):
    pass


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
        self._runs: Dict[str, RunRecord] = {}
        self._events: Dict[str, list[Dict[str, Any]]] = {}
        self._conditions: Dict[str, asyncio.Condition] = {}
        self._writers_by_node: Dict[str, str] = {}
        self._stop_events: Dict[str, asyncio.Event] = {}
        self._finish_listeners: list[Callable[[Dict[str, Any]], None]] = []
        self._lock = asyncio.Lock()
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
    ) -> RunRecord:
        run_kind = kind if isinstance(kind, RunKind) else RunKind(str(kind))
        async with self._lock:
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
                run_id = self.repository.create_run(
                    conversation_id,
                    kind=run_kind.value,
                    anchor_node_id=anchor_node_id,
                    target_node_id=target_node_id,
                    created_by_run_id=created_by_run_id,
                    cancellation_parent_run_id=cancellation_parent_run_id,
                    summary=summary,
                    metadata=metadata,
                )
                stored_run = self.repository.get_run(run_id) or {}
                created_at = float(stored_run.get("created_at") or time())
                updated_at = float(stored_run.get("updated_at") or created_at)
            else:
                run_id = f"run_{uuid.uuid4().hex}"
                created_at = time()
                updated_at = created_at
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
                metadata=dict(metadata or {}),
                created_at=created_at,
                updated_at=updated_at,
            )
            if target_node_id:
                self._writers_by_node[target_node_id] = run_id
            self._runs[run_id] = record
            self._events[run_id] = []
            self._conditions[run_id] = asyncio.Condition()
            self._stop_events[run_id] = asyncio.Event()
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
        persist_event: Optional[Dict[str, Any]] = None
        journal_event: Optional[Dict[str, Any]] = None
        journal_conversation_id: Optional[str] = None
        async with self._lock:
            record = self._require_run_locked(run_id)
            event_index = record.event_count
            payload = dict(payload)
            payload.setdefault("run_id", run_id)
            payload.setdefault("conversation_id", record.conversation_id)
            payload.setdefault("kind", record.kind.value)
            payload.setdefault("target_node_id", record.target_node_id)
            payload["event_index"] = event_index
            if self.repository:
                persist_event = self.repository.append_event(run_id, payload)
                payload = deepcopy(persist_event["payload"])
                event = {
                    "run_id": run_id,
                    "event_index": persist_event["event_index"],
                    "payload": payload,
                    "created_at": persist_event["created_at"],
                }
            else:
                event = {
                    "run_id": run_id,
                    "event_index": event_index,
                    "payload": payload,
                    "created_at": time(),
                }
                journal_event = event
                journal_conversation_id = record.conversation_id
            record.event_count = max(record.event_count, event["event_index"] + 1)
            record.updated_at = event["created_at"]
            self._events.setdefault(run_id, []).append(event)
            condition = self._conditions.setdefault(run_id, asyncio.Condition())
        if journal_event is not None and journal_conversation_id is not None:
            self.journal.append_event(journal_conversation_id, run_id, journal_event)
        async with condition:
            condition.notify_all()
        return payload

    async def subscribe(self, run_id: str, from_event: int = 0) -> AsyncIterator[Dict[str, Any]]:
        index = max(0, int(from_event or 0))
        while True:
            async with self._lock:
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
                yield payload
                continue
            async with condition:
                await condition.wait()

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
    ) -> RunRecord:
        run_status = status if isinstance(status, RunStatus) else RunStatus(str(status))
        if run_status not in FINISHED_RUN_STATUSES:
            raise ValueError(f"finish_run requires finished status, got {run_status}")
        journal_event: Optional[Dict[str, Any]] = None
        journal_conversation_id: Optional[str] = None
        async with self._lock:
            record = self._require_run_locked(run_id)
            if record.status in FINISHED_RUN_STATUSES:
                return deepcopy(record)
            event_index = record.event_count
            record.status = run_status
            record.finished_at = time()
            record.updated_at = record.finished_at
            if error:
                record.metadata["error"] = error
            if record.target_node_id and self._writers_by_node.get(record.target_node_id) == run_id:
                self._writers_by_node.pop(record.target_node_id, None)
            condition = self._conditions.setdefault(run_id, asyncio.Condition())
            if self.repository:
                persisted_run = self.repository.finish_run(run_id, run_status.value, error)
                persisted_events = self.repository.read_events(run_id, event_index)
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
                record.event_count = int(persisted_run.get("event_count", event["event_index"] + 1))
                record.finished_at = float(persisted_run.get("finished_at") or record.finished_at)
                record.updated_at = float(persisted_run.get("updated_at") or record.updated_at)
            else:
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
            self._events.setdefault(run_id, []).append(event)
            snapshot = deepcopy(record)
        if journal_event is not None and journal_conversation_id is not None:
            self.journal.append_event(journal_conversation_id, run_id, journal_event)
        async with condition:
            condition.notify_all()
        snapshot_dict = snapshot.to_dict()
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
            ]
        return [
            record.to_dict()
            for record in self._runs.values()
            if record.status not in FINISHED_RUN_STATUSES
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
                )
                if run.get("cancellation_parent_run_id") == cancellation_parent_run_id
            ]
        return [
            record.to_dict()
            for record in self._runs.values()
            if record.status not in FINISHED_RUN_STATUSES
            and record.cancellation_parent_run_id == cancellation_parent_run_id
            and (conversation_id is None or record.conversation_id == conversation_id)
        ]

    def list_runs(self, conversation_id: Optional[str] = None) -> list[Dict[str, Any]]:
        if self.repository and hasattr(self.repository, "list_runs"):
            return [
                self._normalize_repository_run(run)
                for run in self.repository.list_runs(conversation_id)
            ]
        return [
            record.to_dict()
            for record in self._runs.values()
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
                normalized = self._normalize_repository_run(run)
                if normalized.get("target_node_id") != target_node_id:
                    continue
                if expected_kind_value is not None and normalized.get("kind") != expected_kind_value:
                    continue
                return normalized
            return None
        expected_kind = kind if isinstance(kind, RunKind) or kind is None else RunKind(str(kind))
        for record in self._runs.values():
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
                normalized = self._normalize_repository_run(run)
                if normalized.get("anchor_node_id") != anchor_node_id:
                    continue
                if expected_kind_value is not None and normalized.get("kind") != expected_kind_value:
                    continue
                return normalized
            return None
        expected_kind = kind if isinstance(kind, RunKind) or kind is None else RunKind(str(kind))
        for record in self._runs.values():
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
                )
                if run.get("target_node_id") in targets
            ]
        return [
            record.to_dict()
            for record in self._runs.values()
            if record.status not in FINISHED_RUN_STATUSES
            and record.conversation_id == conversation_id
            and record.target_node_id in targets
        ]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
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
        if self.repository and hasattr(self.repository, "read_events"):
            if not self.get_run(run_id):
                raise RunNotFoundError(run_id)
            return [
                deepcopy(event["payload"])
                for event in self.repository.read_events(run_id, from_event)
            ]
        record = self._runs.get(run_id)
        if not record:
            raise RunNotFoundError(run_id)
        events = self._events.get(run_id, [])
        start = max(0, int(from_event or 0))
        return [deepcopy(event["payload"]) for event in events[start:]]

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
        self._events[run_id] = [
            {
                "run_id": event["run_id"],
                "event_index": event["event_index"],
                "payload": deepcopy(event["payload"]),
                "created_at": event["created_at"],
            }
            for event in self.repository.read_events(run_id, 0)
        ]

    def _normalize_repository_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(run)
        data["run_id"] = str(data.get("run_id") or data.get("id"))
        data["kind"] = str(data.get("kind") or RunKind.CHAT.value)
        data["status"] = str(data.get("status") or RunStatus.RUNNING.value)
        data["created_by_run_id"] = data.get("created_by_run_id") or None
        data["cancellation_parent_run_id"] = data.get("cancellation_parent_run_id") or None
        data["metadata"] = dict(data.get("metadata") or {})
        return data
