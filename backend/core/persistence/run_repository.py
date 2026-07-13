from __future__ import annotations

import gzip
import hashlib
import json
import os
import uuid
from typing import Any

from .blob_store import BlobStore
from .content import INLINE_TEXT_LIMIT, StoredText
from .database import SQLitePersistence


FINISHED_STATUSES = {"completed", "failed", "cancelled", "interrupted", "stopped"}
RUN_COLUMNS = """
id,
conversation_id,
kind,
status,
created_by_run_id,
cancellation_parent_run_id,
anchor_node_id,
target_node_id,
summary,
metadata_json,
event_count,
created_at,
updated_at,
finished_at
"""


class SQLiteRunRepository:
    def __init__(self, persistence: SQLitePersistence, *, task_repository: Any = None) -> None:
        self.persistence = persistence
        self.task_repository = task_repository

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
        run_id = str(uuid.uuid4())
        run_metadata = dict(metadata or {})
        with self.persistence.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO runs (
                  id,
                  conversation_id,
                  kind,
                  status,
                  created_by_run_id,
                  cancellation_parent_run_id,
                  anchor_node_id,
                  target_node_id,
                  summary,
                  metadata_json,
                  created_at,
                  updated_at
                )
                VALUES (
                  ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?,
                  strftime('%s', 'now'),
                  strftime('%s', 'now')
                )
                """,
                (
                    run_id,
                    conversation_id,
                    kind,
                    created_by_run_id,
                    cancellation_parent_run_id,
                    anchor_node_id,
                    target_node_id,
                    summary,
                    self._json_field(run_metadata),
                ),
            )
            if task_binding is not None:
                if self.task_repository is None:
                    raise RuntimeError("task repository is required for bound runs")
                bound = self.task_repository.bind_run_in_connection(
                    conn,
                    run_id=run_id,
                    binding=task_binding,
                )
                run_metadata.update(bound)
                conn.execute(
                    "UPDATE runs SET metadata_json = ? WHERE id = ?",
                    (self._json_field(run_metadata), run_id),
                )
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._run_from_row(row)

    def update_target_node(self, run_id: str, target_node_id: str) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET target_node_id = ?,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (target_node_id, run_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(run_id)
            row = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_from_row(row)

    def update_cancellation_parent(self, run_id: str, cancellation_parent_run_id: str | None) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET cancellation_parent_run_id = ?,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (cancellation_parent_run_id, run_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(run_id)
            row = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_from_row(row)

    def list_runs(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        if conversation_id is None:
            sql = f"SELECT {RUN_COLUMNS} FROM runs ORDER BY created_at, id"
            params: tuple[Any, ...] = ()
        else:
            sql = f"""
                SELECT {RUN_COLUMNS}
                FROM runs
                WHERE conversation_id = ?
                ORDER BY created_at, id
            """
            params = (conversation_id,)
        with self.persistence.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._run_from_row(row) for row in rows]

    def list_active(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in FINISHED_STATUSES)
        params: list[Any] = list(sorted(FINISHED_STATUSES))
        conversation_clause = ""
        if conversation_id is not None:
            conversation_clause = "AND conversation_id = ?"
            params.append(conversation_id)
        with self.persistence.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {RUN_COLUMNS}
                FROM runs
                WHERE status NOT IN ({placeholders})
                  {conversation_clause}
                ORDER BY created_at, id
                """,
                tuple(params),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def append_event(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event_row = self._append_event_in_connection(conn, run_id, payload)
        return self._event_from_row(event_row)

    def append_events(self, run_id: str, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not payloads:
            return []
        rows = []
        with self.persistence.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for payload in payloads:
                rows.append(self._append_event_in_connection(conn, run_id, payload))
        return [self._event_from_row(row) for row in rows]

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
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] in FINISHED_STATUSES:
                return self._run_from_row(row)

            metadata = self._load_json(row["metadata_json"]) or {}
            if error:
                metadata["error"] = error
            if self.task_repository is not None:
                task_outcome = self.task_repository.finish_run_binding_in_connection(
                    conn,
                    run_id=run_id,
                    terminal_status=status_value,
                    error=error,
                    summary=str(row["summary"] or ""),
                )
                if task_outcome is not None:
                    metadata["task_outcome"] = task_outcome
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
            finished_row = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if finished_row is None:
                raise KeyError(run_id)
            finished = self._run_from_row(finished_row)
            self._append_event_in_connection(
                conn,
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
            current_row = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if current_row is None:
                raise KeyError(run_id)
            return self._run_from_row(current_row)

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
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT id, summary, metadata_json
                FROM runs
                WHERE status NOT IN ({placeholders})
                ORDER BY created_at, id
                """,
                tuple(sorted(FINISHED_STATUSES)),
            ).fetchall()
            run_ids = [row["id"] for row in rows]
            for row in rows:
                run_id = row["id"]
                metadata = self._load_json(row["metadata_json"]) or {}
                metadata["error"] = "interrupted on startup"
                if self.task_repository is not None:
                    task_outcome = self.task_repository.finish_run_binding_in_connection(
                        conn,
                        run_id=run_id,
                        terminal_status="interrupted",
                        error="interrupted on startup",
                        summary=str(row["summary"] or ""),
                    )
                    if task_outcome is not None:
                        metadata["task_outcome"] = task_outcome
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'interrupted',
                        metadata_json = ?,
                        finished_at = strftime('%s', 'now'),
                        updated_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (self._json_field(metadata), run_id),
                )
                run = conn.execute(
                    f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if run is not None:
                    snapshot = self._run_from_row(run)
                    self._append_event_in_connection(
                        conn,
                        run_id,
                        {
                            "type": "run_finished",
                            "run_id": run_id,
                            "conversation_id": snapshot["conversation_id"],
                            "kind": snapshot["kind"],
                            "target_node_id": snapshot["target_node_id"],
                            "status": "interrupted",
                            "error": "interrupted on startup",
                            "finished_at": snapshot["finished_at"],
                        },
                    )
        return run_ids

    def _append_event_in_connection(
        self,
        conn: Any,
        run_id: str,
        payload: dict[str, Any],
    ) -> Any:
        run = conn.execute(
            """
            SELECT conversation_id, kind, target_node_id, event_count
            FROM runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            raise KeyError(run_id)
        event_index = int(run["event_count"])
        event_payload = dict(payload)
        event_payload.setdefault("run_id", run_id)
        event_payload.setdefault("conversation_id", run["conversation_id"])
        event_payload.setdefault("kind", run["kind"])
        event_payload.setdefault("target_node_id", run["target_node_id"])
        event_payload["event_index"] = event_index
        stored = self._store_text_content(
            conn,
            self._json_field(event_payload) or "{}",
        )
        event_type = str(
            event_payload.get("type")
            or event_payload.get("event_type")
            or event_payload.get("status")
            or "event"
        )
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
            VALUES (?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
            """,
            (
                run_id,
                run["conversation_id"],
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
        return conn.execute(
            """
            SELECT *
            FROM run_events
            WHERE run_id = ? AND event_index = ?
            """,
            (run_id, event_index),
        ).fetchone()

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

    def _store_text_content(
        self,
        conn: Any,
        text: str,
        *,
        preview_limit: int = 4096,
        inline_limit: int = INLINE_TEXT_LIMIT,
    ) -> StoredText:
        value = text or ""
        preview = value[:preview_limit]
        data = value.encode("utf-8")
        size = len(data)
        if size <= inline_limit:
            return StoredText(
                inline=value,
                blob_id=None,
                preview=preview,
                size=size,
            )

        blob_id = hashlib.sha256(data).hexdigest()
        relative_path = f"blobs/{blob_id[:2]}/{blob_id}.gz"
        final_path = self.persistence.blobs_dir / blob_id[:2] / f"{blob_id}.gz"
        compressed = gzip.compress(data)
        row = conn.execute(
            """
            SELECT id
            FROM blobs
            WHERE id = ?
            """,
            (blob_id,),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE blobs
                SET ref_count = ref_count + 1,
                    last_accessed_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (blob_id,),
            )
        else:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            self.persistence.tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = (
                self.persistence.tmp_dir
                / f"{blob_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            tmp_path.write_bytes(compressed)
            try:
                os.replace(tmp_path, final_path)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()
            conn.execute(
                """
                INSERT INTO blobs (
                  id,
                  path,
                  mime_type,
                  compression,
                  byte_size,
                  stored_size,
                  char_count,
                  ref_count,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, strftime('%s', 'now'))
                """,
                (
                    blob_id,
                    relative_path,
                    "text/plain; charset=utf-8",
                    "gzip",
                    size,
                    len(compressed),
                    len(value),
                ),
            )

        return StoredText(
            inline=None,
            blob_id=blob_id,
            preview=preview,
            size=size,
        )
