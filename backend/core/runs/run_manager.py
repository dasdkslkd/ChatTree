from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from time import time
from typing import Any, AsyncIterator, Dict, Iterable, Optional

from .journal import RunJournal
from .synthetic_inputs import SyntheticInputQueue
from .types import TERMINAL_RUN_STATUSES, RunKind, RunRecord, RunStatus


class RunNotFoundError(Exception):
    pass


class RunWriterConflictError(Exception):
    pass


class RunManager:
    """In-memory active run registry with durable event replay."""

    def __init__(self, journal: Optional[RunJournal] = None) -> None:
        self.journal = journal or RunJournal()
        self.synthetic_inputs = SyntheticInputQueue()
        self._runs: Dict[str, RunRecord] = {}
        self._events: Dict[str, list[Dict[str, Any]]] = {}
        self._conditions: Dict[str, asyncio.Condition] = {}
        self._writers_by_node: Dict[str, str] = {}
        self._stop_events: Dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def create_run(
        self,
        *,
        conversation_id: str,
        kind: RunKind | str,
        anchor_node_id: Optional[str] = None,
        target_node_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        summary: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RunRecord:
        run_kind = kind if isinstance(kind, RunKind) else RunKind(str(kind))
        run_id = f"run_{uuid.uuid4().hex}"
        record = RunRecord(
            run_id=run_id,
            conversation_id=conversation_id,
            kind=run_kind,
            status=RunStatus.RUNNING,
            anchor_node_id=anchor_node_id,
            target_node_id=target_node_id,
            parent_run_id=parent_run_id,
            summary=summary,
            metadata=dict(metadata or {}),
        )
        async with self._lock:
            if target_node_id:
                self._acquire_writer_locked(target_node_id, run_id)
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
            "parent_run_id": parent_run_id,
            "summary": summary,
            "metadata": record.metadata,
            "created_at": record.created_at,
        })
        return record

    async def bind_target_node(self, run_id: str, target_node_id: str) -> RunRecord:
        async with self._lock:
            record = self._require_run_locked(run_id)
            if record.target_node_id == target_node_id:
                return deepcopy(record)
            self._acquire_writer_locked(target_node_id, run_id)
            if record.target_node_id and self._writers_by_node.get(record.target_node_id) == run_id:
                self._writers_by_node.pop(record.target_node_id, None)
            record.target_node_id = target_node_id
            record.updated_at = time()
            snapshot = deepcopy(record)
        await self.append_event(run_id, {
            "type": "run_target_bound",
            "run_id": run_id,
            "target_node_id": target_node_id,
        })
        return snapshot

    async def append_event(self, run_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            record = self._require_run_locked(run_id)
            event_index = record.event_count
            payload = dict(payload)
            payload.setdefault("run_id", run_id)
            payload.setdefault("conversation_id", record.conversation_id)
            payload.setdefault("kind", record.kind.value)
            payload.setdefault("target_node_id", record.target_node_id)
            payload["event_index"] = event_index
            event = {
                "run_id": run_id,
                "event_index": event_index,
                "payload": payload,
                "created_at": time(),
            }
            record.event_count += 1
            record.updated_at = event["created_at"]
            self._events.setdefault(run_id, []).append(event)
            condition = self._conditions.setdefault(run_id, asyncio.Condition())
            conversation_id = record.conversation_id
        self.journal.append_event(conversation_id, run_id, event)
        async with condition:
            condition.notify_all()
        return payload

    async def subscribe(self, run_id: str, from_event: int = 0) -> AsyncIterator[Dict[str, Any]]:
        index = max(0, int(from_event or 0))
        while True:
            async with self._lock:
                record = self._runs.get(run_id)
                if not record:
                    raise RunNotFoundError(run_id)
                events = self._events.setdefault(run_id, [])
                condition = self._conditions.setdefault(run_id, asyncio.Condition())
                terminal = record.status in TERMINAL_RUN_STATUSES
                if index < len(events):
                    event = events[index]
                    index += 1
                    payload = deepcopy(event["payload"])
                elif terminal:
                    break
                else:
                    payload = None
            if payload is not None:
                yield payload
                continue
            async with condition:
                await condition.wait()

    async def finish_run(
        self,
        run_id: str,
        status: RunStatus | str = RunStatus.COMPLETED,
        error: Optional[str] = None,
    ) -> RunRecord:
        run_status = status if isinstance(status, RunStatus) else RunStatus(str(status))
        if run_status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"finish_run requires terminal status, got {run_status}")
        async with self._lock:
            record = self._require_run_locked(run_id)
            if record.status in TERMINAL_RUN_STATUSES:
                return deepcopy(record)
            record.status = run_status
            record.finished_at = time()
            record.updated_at = record.finished_at
            if error:
                record.metadata["error"] = error
            if record.target_node_id and self._writers_by_node.get(record.target_node_id) == run_id:
                self._writers_by_node.pop(record.target_node_id, None)
            condition = self._conditions.setdefault(run_id, asyncio.Condition())
            event_index = record.event_count
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
            self._events.setdefault(run_id, []).append(event)
            snapshot = deepcopy(record)
        self.journal.append_event(snapshot.conversation_id, run_id, event)
        async with condition:
            condition.notify_all()
        return snapshot

    async def request_stop(self, run_id: str) -> bool:
        async with self._lock:
            record = self._runs.get(run_id)
            if not record or record.status in TERMINAL_RUN_STATUSES:
                return False
            record.status = RunStatus.STOPPING
            record.updated_at = time()
            stop_event = self._stop_events.setdefault(run_id, asyncio.Event())
            stop_event.set()
        await self.append_event(run_id, {
            "type": "run_stop_requested",
            "run_id": run_id,
            "status": RunStatus.STOPPING.value,
        })
        return True

    def stop_event(self, run_id: str) -> asyncio.Event:
        return self._stop_events.setdefault(run_id, asyncio.Event())

    async def is_stop_requested(self, run_id: str) -> bool:
        return self.stop_event(run_id).is_set()

    def list_active(self, conversation_id: Optional[str] = None) -> list[Dict[str, Any]]:
        return [
            record.to_dict()
            for record in self._runs.values()
            if record.status not in TERMINAL_RUN_STATUSES
            and (conversation_id is None or record.conversation_id == conversation_id)
        ]

    def list_runs(self, conversation_id: Optional[str] = None) -> list[Dict[str, Any]]:
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
        expected_kind = kind if isinstance(kind, RunKind) or kind is None else RunKind(str(kind))
        for record in self._runs.values():
            if record.status in TERMINAL_RUN_STATUSES:
                continue
            if record.conversation_id != conversation_id:
                continue
            if record.target_node_id != target_node_id:
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
        return [
            record.to_dict()
            for record in self._runs.values()
            if record.status not in TERMINAL_RUN_STATUSES
            and record.conversation_id == conversation_id
            and record.target_node_id in targets
        ]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        record = self._runs.get(run_id)
        return record.to_dict() if record else None

    def _require_run_locked(self, run_id: str) -> RunRecord:
        record = self._runs.get(run_id)
        if not record:
            raise RunNotFoundError(run_id)
        return record

    def _acquire_writer_locked(self, target_node_id: str, run_id: str) -> None:
        existing = self._writers_by_node.get(target_node_id)
        if existing and existing != run_id:
            existing_record = self._runs.get(existing)
            if existing_record and existing_record.status not in TERMINAL_RUN_STATUSES:
                raise RunWriterConflictError(
                    f"target node {target_node_id} already has active writer {existing}"
                )
        self._writers_by_node[target_node_id] = run_id
