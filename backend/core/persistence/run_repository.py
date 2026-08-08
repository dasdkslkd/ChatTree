from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import uuid
from time import time
from typing import Any

from backend.core.runs.idempotency import (
    RunIdempotencyConflictError,
    RunReferenceConversationMismatchError,
    RunReferenceNotFoundError,
)

from .blob_store import BlobStore
from .content import INLINE_TEXT_LIMIT, StoredText
from .database import SQLitePersistence


FINISHED_STATUSES = {"completed", "failed", "cancelled", "interrupted", "stopped"}
TERMINAL_RESULT_EVENT_TYPES = {
    "command": {"command_exited", "command_stopped", "command_error"},
    "subagent": {"subagent_result", "subagent_error"},
    "workflow_step": {"subagent_result", "subagent_error"},
    "workflow": {"workflow_result", "workflow_error", "workflow_cancelled"},
}
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
idempotency_key,
request_fingerprint,
event_count,
created_at,
updated_at,
finished_at
"""


class SQLiteRunRepository:
    manages_task_bindings = True

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
        run_id = str(uuid.uuid4())
        try:
            with self.persistence.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._insert_run_in_connection(
                    conn,
                    run_id=run_id,
                    conversation_id=conversation_id,
                    kind=kind,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    anchor_node_id=anchor_node_id,
                    target_node_id=target_node_id,
                    created_by_run_id=created_by_run_id,
                    cancellation_parent_run_id=cancellation_parent_run_id,
                    summary=summary,
                    metadata=metadata,
                    task_binding=task_binding,
                )
                created_row = conn.execute(
                    f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if created_row is None:
                    raise RuntimeError(f"created run {run_id} is missing")
                created_run = self._run_from_row(created_row)
        except sqlite3.IntegrityError:
            if idempotency_key is None:
                raise
            existing = self.get_run_by_idempotency_key(idempotency_key)
            if existing is None:
                raise
            if existing["request_fingerprint"] != request_fingerprint:
                raise RunIdempotencyConflictError(str(existing["run_id"]))
            return existing, False
        return created_run, True

    def get_run_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return self._run_from_row(row)

    def validate_run_references(
        self,
        conversation_id: str,
        *,
        anchor_node_id: str | None = None,
        created_by_run_id: str | None = None,
        cancellation_parent_run_id: str | None = None,
    ) -> None:
        with self.persistence.connect() as conn:
            conversation = conn.execute(
                "SELECT id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                self._raise_missing_or_wrong_type_reference(
                    conn,
                    reference_kind="conversation_id",
                    reference_id=conversation_id,
                )

            if anchor_node_id is not None:
                anchor = conn.execute(
                    "SELECT conversation_id FROM nodes WHERE id = ?",
                    (anchor_node_id,),
                ).fetchone()
                self._validate_reference_conversation(
                    conn,
                    anchor,
                    conversation_id=conversation_id,
                    reference_kind="anchor_node_id",
                    reference_id=anchor_node_id,
                )

            for reference_kind, reference_id in (
                ("created_by_run_id", created_by_run_id),
                ("cancellation_parent_run_id", cancellation_parent_run_id),
            ):
                if reference_id is None:
                    continue
                run = conn.execute(
                    "SELECT conversation_id FROM runs WHERE id = ?",
                    (reference_id,),
                ).fetchone()
                self._validate_reference_conversation(
                    conn,
                    run,
                    conversation_id=conversation_id,
                    reference_kind=reference_kind,
                    reference_id=reference_id,
                )

    def _insert_run_in_connection(
        self,
        conn: Any,
        *,
        run_id: str,
        conversation_id: str,
        kind: str,
        idempotency_key: str | None,
        request_fingerprint: str | None,
        anchor_node_id: str | None,
        target_node_id: str | None,
        created_by_run_id: str | None,
        cancellation_parent_run_id: str | None,
        summary: str,
        metadata: dict[str, Any] | None,
        task_binding: dict[str, Any] | None,
    ) -> None:
        run_metadata = dict(metadata or {})
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
              idempotency_key,
              request_fingerprint,
              created_at,
              updated_at
            )
            VALUES (
              ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?,
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
                idempotency_key,
                request_fingerprint,
            ),
        )
        if task_binding is None:
            return
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

    @classmethod
    def _validate_reference_conversation(
        cls,
        conn: Any,
        row: Any,
        *,
        conversation_id: str,
        reference_kind: str,
        reference_id: str,
    ) -> None:
        if row is None:
            cls._raise_missing_or_wrong_type_reference(
                conn,
                reference_kind=reference_kind,
                reference_id=reference_id,
            )
        if row["conversation_id"] != conversation_id:
            raise RunReferenceConversationMismatchError(reference_kind, reference_id)

    @staticmethod
    def _raise_missing_or_wrong_type_reference(
        conn: Any,
        *,
        reference_kind: str,
        reference_id: str,
    ) -> None:
        existing = conn.execute(
            """
            SELECT 1 FROM conversations WHERE id = ?
            UNION ALL
            SELECT 1 FROM nodes WHERE id = ?
            UNION ALL
            SELECT 1 FROM runs WHERE id = ?
            LIMIT 1
            """,
            (reference_id, reference_id, reference_id),
        ).fetchone()
        if existing is not None:
            raise RunReferenceConversationMismatchError(
                reference_kind,
                reference_id,
            )
        raise RunReferenceNotFoundError(reference_kind, reference_id)

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

    def update_anchor_node(self, run_id: str, anchor_node_id: str) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET anchor_node_id = ?,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (anchor_node_id, run_id),
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

    def delete_runs_for_deleted_nodes(
        self,
        conversation_id: str,
        node_ids: list[str],
    ) -> int:
        """节点子树被删除后清理对应 run：锚点/落点在子树内的终态 run，
        以及历史上因节点删除而落点悬空的终态 run。活跃 run 不受影响；
        run_events 与 task_notifications 随外键级联删除。"""
        status_placeholders = ",".join("?" for _ in FINISHED_STATUSES)
        statuses = sorted(FINISHED_STATUSES)
        deleted = 0
        with self.persistence.connect() as conn:
            if node_ids:
                node_placeholders = ",".join("?" for _ in node_ids)
                deleted += conn.execute(
                    f"""
                    DELETE FROM runs
                    WHERE conversation_id = ?
                      AND status IN ({status_placeholders})
                      AND (
                        target_node_id IN ({node_placeholders})
                        OR anchor_node_id IN ({node_placeholders})
                      )
                    """,
                    (conversation_id, *statuses, *node_ids, *node_ids),
                ).rowcount
            deleted += conn.execute(
                f"""
                DELETE FROM runs
                WHERE conversation_id = ?
                  AND status IN ({status_placeholders})
                  AND target_node_id IS NULL
                """,
                (conversation_id, *statuses),
            ).rowcount
        if deleted:
            self.persistence.reclaim_blobs()
        return deleted

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

    def append_indexed_events(
        self,
        run_id: str,
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not events:
            return []
        ordered = sorted(events, key=lambda event: int(event["event_index"]))
        rows = []
        with self.persistence.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
            expected_index = int(run["event_count"])
            updated_at = float(ordered[-1].get("created_at") or time())
            for event in ordered:
                event_index = int(event["event_index"])
                if event_index != expected_index:
                    raise ValueError(
                        f"run {run_id} expected event_index {expected_index}, got {event_index}"
                    )
                rows.append(
                    self._insert_indexed_event_in_connection(
                        conn,
                        run_id,
                        run,
                        event_index=event_index,
                        payload=dict(event["payload"]),
                        created_at=float(event.get("created_at") or time()),
                    )
                )
                expected_index += 1
            conn.execute(
                """
                UPDATE runs
                SET event_count = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (expected_index, updated_at, run_id),
            )
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

            terminal_result = {}
            result_event_types = TERMINAL_RESULT_EVENT_TYPES.get(
                str(row["kind"]),
                set(),
            )
            if result_event_types:
                placeholders = ",".join("?" for _ in result_event_types)
                result_event_row = conn.execute(
                    f"""
                    SELECT payload_inline, payload_blob_id
                    FROM run_events
                    WHERE run_id = ?
                      AND event_type IN ({placeholders})
                    ORDER BY event_index DESC
                    LIMIT 1
                    """,
                    (run_id, *sorted(result_event_types)),
                ).fetchone()
            else:
                result_event_row = None
            if result_event_row is not None:
                payload_text = result_event_row["payload_inline"]
                if payload_text is None and result_event_row["payload_blob_id"]:
                    payload_text = BlobStore(self.persistence).get_text_in_connection(
                        conn,
                        str(result_event_row["payload_blob_id"]),
                    )
                terminal_result = self._load_json(payload_text) or {}
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
            conn.execute("DELETE FROM run_events WHERE run_id = ?", (run_id,))
            terminal_event = {
                "type": "run_finished",
                "run_id": run_id,
                "conversation_id": finished["conversation_id"],
                "kind": finished["kind"],
                "target_node_id": finished["target_node_id"],
                "status": status_value,
                "error": error,
                "finished_at": finished["finished_at"],
            }
            if terminal_result:
                terminal_event["terminal_result"] = terminal_result
            self._append_event_in_connection(conn, run_id, terminal_event)
            current_row = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if current_row is None:
                raise KeyError(run_id)
            result = self._run_from_row(current_row)
        self.persistence.reclaim_blobs()
        return result

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

    def update_status(self, run_id: str, status: str) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                  AND status NOT IN ('completed', 'failed', 'cancelled', 'interrupted', 'stopped')
                """,
                (str(status), run_id),
            )
            row = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_from_row(row)

    def merge_metadata(
        self,
        run_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            merged = self._load_json(row["metadata_json"]) or {}
            merged.update(dict(metadata or {}))
            conn.execute(
                """
                UPDATE runs
                SET metadata_json = ?,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (self._json_field(merged), run_id),
            )
            updated = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(run_id)
        return self._run_from_row(updated)

    def delete_run(self, run_id: str) -> None:
        with self.persistence.connect() as conn:
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        self.persistence.reclaim_blobs()

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
        run_ids = [str(row["id"]) for row in rows]
        for run_id in run_ids:
            self.finish_run(run_id, "interrupted", "interrupted on startup")
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
        row = self._insert_indexed_event_in_connection(
            conn,
            run_id,
            run,
            event_index=event_index,
            payload=payload,
            created_at=float(time()),
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
        return row

    def _insert_indexed_event_in_connection(
        self,
        conn: Any,
        run_id: str,
        run: Any,
        *,
        event_index: int,
        payload: dict[str, Any],
        created_at: float,
    ) -> Any:
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
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                run["conversation_id"],
                event_index,
                event_type,
                stored.inline,
                stored.blob_id,
                created_at,
            ),
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
                SET last_accessed_at = strftime('%s', 'now')
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
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
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
