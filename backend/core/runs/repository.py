from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from time import time
from typing import Any, Protocol

from .idempotency import RunIdempotencyConflictError


FINISHED_STATUSES = {"completed", "failed", "cancelled", "interrupted", "stopped"}


class RunRepository(Protocol):
    manages_task_bindings: bool

    def create_run(self, conversation_id: str, **kwargs: Any) -> str: ...

    def create_or_get_run(
        self,
        conversation_id: str,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], bool]: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def get_run_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> dict[str, Any] | None: ...

    def list_runs(
        self,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_active(
        self,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def append_event(
        self,
        run_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def append_events(
        self,
        run_id: str,
        payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...

    def read_events(
        self,
        run_id: str,
        from_event: int = 0,
    ) -> list[dict[str, Any]]: ...

    def finish_run(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any]: ...

    def request_stop(self, run_id: str) -> bool: ...

    def update_status(self, run_id: str, status: str) -> dict[str, Any]: ...

    def merge_metadata(
        self,
        run_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]: ...

    def delete_run(self, run_id: str) -> None: ...

    def update_target_node(
        self,
        run_id: str,
        target_node_id: str,
    ) -> dict[str, Any]: ...

    def update_anchor_node(
        self,
        run_id: str,
        anchor_node_id: str,
    ) -> dict[str, Any]: ...

    def update_cancellation_parent(
        self,
        run_id: str,
        cancellation_parent_run_id: str | None,
    ) -> dict[str, Any]: ...

    def validate_run_references(
        self,
        conversation_id: str,
        **kwargs: Any,
    ) -> None: ...


class MemoryRunRepository:
    """Repository contract for tests and non-persistent embedded runtimes."""

    manages_task_bindings = False
    mirrors_to_journal = True

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = threading.RLock()

    def create_run(
        self,
        conversation_id: str,
        *,
        kind: str,
        anchor_node_id: str | None = None,
        target_node_id: str | None = None,
        created_by_run_id: str | None = None,
        cancellation_parent_run_id: str | None = None,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
        task_binding: dict[str, Any] | None = None,
    ) -> str:
        run, created = self.create_or_get_run(
            conversation_id,
            kind=kind,
            idempotency_key=None,
            request_fingerprint=None,
            anchor_node_id=anchor_node_id,
            target_node_id=target_node_id,
            created_by_run_id=created_by_run_id,
            cancellation_parent_run_id=cancellation_parent_run_id,
            summary=summary,
            metadata=metadata,
            task_binding=task_binding,
        )
        if not created:
            raise RuntimeError("non-idempotent run creation returned an existing run")
        return str(run["run_id"])

    def create_or_get_run(
        self,
        conversation_id: str,
        *,
        kind: str,
        idempotency_key: str | None,
        request_fingerprint: str | None,
        anchor_node_id: str | None = None,
        target_node_id: str | None = None,
        created_by_run_id: str | None = None,
        cancellation_parent_run_id: str | None = None,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
        task_binding: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if (idempotency_key is None) != (request_fingerprint is None):
            raise ValueError(
                "idempotency key and request fingerprint must be provided together"
            )
        with self._lock:
            if idempotency_key is not None:
                existing_id = self._idempotency.get(idempotency_key)
                if existing_id is not None:
                    existing = self._runs[existing_id]
                    if existing.get("request_fingerprint") != request_fingerprint:
                        raise RunIdempotencyConflictError(existing_id)
                    return deepcopy(existing), False

            now = time()
            run_id = f"run_{uuid.uuid4().hex}"
            run_metadata = dict(metadata or {})
            if task_binding is not None:
                run_metadata["task_generation_id"] = task_binding.get(
                    "task_generation_id"
                )
                run_metadata["task_step_position"] = task_binding.get(
                    "step_position"
                )
            run = {
                "id": run_id,
                "run_id": run_id,
                "conversation_id": conversation_id,
                "kind": kind,
                "status": "running",
                "created_by_run_id": created_by_run_id,
                "cancellation_parent_run_id": cancellation_parent_run_id,
                "anchor_node_id": anchor_node_id,
                "target_node_id": target_node_id,
                "summary": summary,
                "metadata": run_metadata,
                "idempotency_key": idempotency_key,
                "request_fingerprint": request_fingerprint,
                "event_count": 0,
                "created_at": now,
                "updated_at": now,
                "finished_at": None,
            }
            self._runs[run_id] = run
            self._events[run_id] = []
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = run_id
            return deepcopy(run), True

    def get_run_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            run_id = self._idempotency.get(idempotency_key)
            return deepcopy(self._runs.get(run_id)) if run_id is not None else None

    def validate_run_references(self, _conversation_id: str, **_kwargs: Any) -> None:
        return None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            return deepcopy(run) if run is not None else None

    def _update_reference(
        self,
        run_id: str,
        field: str,
        value: str | None,
    ) -> dict[str, Any]:
        with self._lock:
            run = self._require_run(run_id)
            run[field] = value
            run["updated_at"] = time()
            return deepcopy(run)

    def update_target_node(self, run_id: str, target_node_id: str) -> dict[str, Any]:
        return self._update_reference(run_id, "target_node_id", target_node_id)

    def update_anchor_node(self, run_id: str, anchor_node_id: str) -> dict[str, Any]:
        return self._update_reference(run_id, "anchor_node_id", anchor_node_id)

    def update_cancellation_parent(
        self,
        run_id: str,
        cancellation_parent_run_id: str | None,
    ) -> dict[str, Any]:
        return self._update_reference(
            run_id,
            "cancellation_parent_run_id",
            cancellation_parent_run_id,
        )

    def list_runs(
        self,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            runs = [
                deepcopy(run)
                for run in self._runs.values()
                if conversation_id is None
                or run["conversation_id"] == conversation_id
            ]
        return sorted(runs, key=lambda run: (run["created_at"], run["run_id"]))

    def list_active(
        self,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            run
            for run in self.list_runs(conversation_id)
            if run["status"] not in FINISHED_STATUSES
        ]

    def append_event(
        self,
        run_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self.append_events(run_id, [payload])[0]

    def append_events(
        self,
        run_id: str,
        payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        with self._lock:
            run = self._require_run(run_id)
            events = self._events[run_id]
            appended: list[dict[str, Any]] = []
            for source in payloads:
                created_at = time()
                event_index = len(events)
                payload = dict(source)
                payload.setdefault("run_id", run_id)
                payload.setdefault("conversation_id", run["conversation_id"])
                payload.setdefault("kind", run["kind"])
                payload.setdefault("target_node_id", run["target_node_id"])
                payload["event_index"] = event_index
                event = {
                    "id": event_index + 1,
                    "run_id": run_id,
                    "conversation_id": run["conversation_id"],
                    "event_index": event_index,
                    "event_type": str(
                        payload.get("type")
                        or payload.get("event_type")
                        or payload.get("status")
                        or "event"
                    ),
                    "payload": payload,
                    "payload_inline": None,
                    "payload_blob_id": None,
                    "created_at": created_at,
                }
                events.append(event)
                appended.append(deepcopy(event))
            if appended:
                run["event_count"] = len(events)
                run["updated_at"] = appended[-1]["created_at"]
            return appended

    def read_events(
        self,
        run_id: str,
        from_event: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._require_run(run_id)
            return deepcopy(self._events[run_id][max(0, int(from_event or 0)):])

    def finish_run(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            run = self._require_run(run_id)
            if run["status"] in FINISHED_STATUSES:
                return deepcopy(run)
            finished_at = time()
            run["status"] = str(status)
            run["finished_at"] = finished_at
            run["updated_at"] = finished_at
            if error:
                run["metadata"] = {**run["metadata"], "error": error}
        self.append_event(
            run_id,
            {
                "type": "run_finished",
                "status": str(status),
                "error": error,
                "finished_at": finished_at,
            },
        )
        return self.get_run(run_id) or {}

    def request_stop(self, run_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run["status"] in FINISHED_STATUSES:
                return False
            run["status"] = "stopping"
            run["updated_at"] = time()
        self.append_event(
            run_id,
            {"type": "run_stop_requested", "status": "stopping"},
        )
        return True

    def update_status(self, run_id: str, status: str) -> dict[str, Any]:
        with self._lock:
            run = self._require_run(run_id)
            if run["status"] in FINISHED_STATUSES:
                return deepcopy(run)
            run["status"] = str(status)
            run["updated_at"] = time()
            return deepcopy(run)

    def mark_unfinished_as_interrupted(self) -> list[str]:
        run_ids = [run["run_id"] for run in self.list_active()]
        for run_id in run_ids:
            self.finish_run(run_id, "interrupted", "interrupted on startup")
        return run_ids

    def merge_metadata(
        self,
        run_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            run = self._require_run(run_id)
            run["metadata"] = {**run["metadata"], **dict(metadata)}
            run["updated_at"] = time()
            return deepcopy(run)

    def delete_run(self, run_id: str) -> None:
        with self._lock:
            run = self._runs.pop(run_id, None)
            self._events.pop(run_id, None)
            if run is not None and run.get("idempotency_key") is not None:
                self._idempotency.pop(str(run["idempotency_key"]), None)

    def _require_run(self, run_id: str) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run


__all__ = ["MemoryRunRepository", "RunRepository"]
