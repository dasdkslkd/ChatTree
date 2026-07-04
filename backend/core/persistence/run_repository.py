from __future__ import annotations

import json
import uuid
from typing import Any

from .blob_store import BlobStore
from .content import store_text_content
from .database import SQLitePersistence


FINISHED_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


class SQLiteRunRepository:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self.persistence = persistence

    def create_run(
        self,
        conversation_id: str,
        *,
        kind: str,
        anchor_node_id: str | None = None,
        target_node_id: str | None = None,
        parent_run_id: str | None = None,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        with self.persistence.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                  id,
                  conversation_id,
                  kind,
                  status,
                  parent_run_id,
                  anchor_node_id,
                  target_node_id,
                  summary,
                  metadata_json,
                  created_at,
                  updated_at
                )
                VALUES (
                  ?, ?, ?, 'running', ?, ?, ?, ?, ?,
                  strftime('%s', 'now'),
                  strftime('%s', 'now')
                )
                """,
                (
                    run_id,
                    conversation_id,
                    kind,
                    parent_run_id,
                    anchor_node_id,
                    target_node_id,
                    summary,
                    self._json_field(metadata or {}),
                ),
            )
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._run_from_row(row)

    def append_event(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        while True:
            run = self.get_run(run_id)
            if run is None:
                raise KeyError(run_id)

            event_index = int(run["event_count"])
            event_payload = dict(payload)
            event_payload.setdefault("run_id", run_id)
            event_payload.setdefault("conversation_id", run["conversation_id"])
            event_payload.setdefault("kind", run["kind"])
            event_payload.setdefault("target_node_id", run["target_node_id"])
            event_payload["event_index"] = event_index
            payload_json = self._json_field(event_payload) or "{}"
            stored = store_text_content(self.persistence, payload_json)
            event_type = (
                str(
                    event_payload.get("type")
                    or event_payload.get("event_type")
                    or event_payload.get("status")
                    or "event"
                )
            )

            event_row = None
            with self.persistence.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    """
                    SELECT conversation_id, event_count
                    FROM runs
                    WHERE id = ?
                    """,
                    (run_id,),
                ).fetchone()
                if current is None:
                    raise KeyError(run_id)
                if int(current["event_count"]) != event_index:
                    continue
                conn.execute(
                    """
                    INSERT INTO run_events (
                      run_id,
                      conversation_id,
                      event_index,
                      event_type,
                      payload_inline,
                      payload_blob_id,
                      created_at
                    )
                    VALUES (
                      ?, ?, ?, ?, ?, ?,
                      strftime('%s', 'now')
                    )
                    """,
                    (
                        run_id,
                        current["conversation_id"],
                        event_index,
                        event_type,
                        stored.inline,
                        stored.blob_id,
                    ),
                )
                conn.execute(
                    """
                    UPDATE runs
                    SET event_count = event_count + 1,
                        updated_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (run_id,),
                )
                row = conn.execute(
                    """
                    SELECT *
                    FROM run_events
                    WHERE run_id = ? AND event_index = ?
                    """,
                    (run_id, event_index),
                ).fetchone()
                event_row = row
            return self._event_from_row(event_row)

    def read_events(self, run_id: str, from_event: int = 0) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM run_events
                WHERE run_id = ? AND event_index >= ?
                ORDER BY event_index
                """,
                (run_id, max(0, int(from_event or 0))),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def finish_run(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        status_value = str(status)
        with self.persistence.connect() as conn:
            row = conn.execute(
                "SELECT status, metadata_json FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] in FINISHED_STATUSES:
                current = self.get_run(run_id)
                if current is None:
                    raise KeyError(run_id)
                return current

            metadata = self._load_json(row["metadata_json"]) or {}
            if error:
                metadata["error"] = error
            conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    metadata_json = ?,
                    finished_at = strftime('%s', 'now'),
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (status_value, self._json_field(metadata), run_id),
            )

        finished = self.get_run(run_id)
        if finished is None:
            raise KeyError(run_id)
        self.append_event(
            run_id,
            {
                "type": "run_finished",
                "run_id": run_id,
                "conversation_id": finished["conversation_id"],
                "kind": finished["kind"],
                "target_node_id": finished["target_node_id"],
                "status": status_value,
                "error": error,
                "finished_at": finished["finished_at"],
            },
        )
        return self.get_run(run_id) or finished

    def request_stop(self, run_id: str) -> bool:
        with self.persistence.connect() as conn:
            row = conn.execute(
                "SELECT status FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None or row["status"] in FINISHED_STATUSES:
                return False
            conn.execute(
                """
                UPDATE runs
                SET status = 'stopping',
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (run_id,),
            )

        self.append_event(
            run_id,
            {
                "type": "run_stop_requested",
                "run_id": run_id,
                "status": "stopping",
            },
        )
        return True

    def mark_unfinished_as_interrupted(self) -> list[str]:
        placeholders = ",".join("?" for _ in FINISHED_STATUSES)
        with self.persistence.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id
                FROM runs
                WHERE status NOT IN ({placeholders})
                ORDER BY created_at, id
                """,
                tuple(sorted(FINISHED_STATUSES)),
            ).fetchall()
            run_ids = [row["id"] for row in rows]
            for run_id in run_ids:
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'interrupted',
                        finished_at = strftime('%s', 'now'),
                        updated_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (run_id,),
                )

        for run_id in run_ids:
            run = self.get_run(run_id)
            if run is None:
                continue
            self.append_event(
                run_id,
                {
                    "type": "run_finished",
                    "run_id": run_id,
                    "conversation_id": run["conversation_id"],
                    "kind": run["kind"],
                    "target_node_id": run["target_node_id"],
                    "status": "interrupted",
                    "error": "interrupted on startup",
                    "finished_at": run["finished_at"],
                },
            )
        return run_ids

    def _event_from_row(self, row: Any) -> dict[str, Any]:
        payload_text = row["payload_inline"]
        if payload_text is None and row["payload_blob_id"]:
            payload_text = BlobStore(self.persistence).get_text(row["payload_blob_id"])
        payload = self._load_json(payload_text) or {}
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "conversation_id": row["conversation_id"],
            "event_index": row["event_index"],
            "event_type": row["event_type"],
            "payload": payload,
            "payload_inline": row["payload_inline"],
            "payload_blob_id": row["payload_blob_id"],
            "created_at": row["created_at"],
        }

    def _run_from_row(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["run_id"] = data["id"]
        data["metadata"] = self._load_json(data.pop("metadata_json")) or {}
        return data

    def _json_field(self, value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _load_json(self, value: str | None) -> Any:
        if not value:
            return None
        return json.loads(value)
