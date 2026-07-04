from __future__ import annotations

import gzip
import hashlib
import json
import os
import uuid
from time import time
from typing import Any, Iterable

from backend.core.tasks.types import FINISHED_TASK_STATUSES, OPEN_TASK_STATUSES

from .blob_store import BlobStore
from .content import INLINE_TEXT_LIMIT, StoredText, store_text_content
from .database import SQLitePersistence


class SQLiteTaskRepository:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self.persistence = persistence

    def create_task(
        self,
        conversation_id: str,
        *,
        title: str,
        detail: str = "",
        created_by_run_id: str | None = None,
        owner_type: str = "assistant",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        task_id = f"task_{uuid.uuid4().hex}"
        stored = store_text_content(self.persistence, detail)
        now = time()
        with self.persistence.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                  id,
                  conversation_id,
                  status,
                  owner_type,
                  title,
                  detail_inline,
                  detail_blob_id,
                  evidence_summary,
                  metadata_json,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, 'pending', ?, ?, ?, ?, '', ?, ?, ?)
                """,
                (
                    task_id,
                    conversation_id,
                    self._value(owner_type),
                    title,
                    stored.inline,
                    stored.blob_id,
                    self._metadata_field(
                        metadata or {},
                        created_by_run_id=created_by_run_id,
                        finished_at=None,
                    ),
                    now,
                    now,
                ),
            )
            self._append_event(
                conn,
                conversation_id,
                task_id,
                "created",
                {
                    "title": title,
                    "created_by_run_id": created_by_run_id,
                    "owner_type": self._value(owner_type),
                },
                now,
            )
        return task_id

    def update_task(
        self,
        conversation_id: str,
        task_id: str,
        *,
        status: str | None = None,
        title: str | None = None,
        detail: str | None = None,
        owner_type: str | None = None,
        owner_run_id: str | None = None,
        evidence_run_id: str | None = None,
        evidence_summary: str | None = None,
        metadata: dict[str, Any] | None = None,
        metadata_patch: dict[str, Any] | None = None,
        finished_at: float | None = None,
    ) -> dict[str, Any]:
        current = self._require_task(conversation_id, task_id)
        next_metadata = dict(current.get("metadata") or {})
        if metadata is not None:
            next_metadata = dict(metadata)
        if metadata_patch:
            next_metadata.update(dict(metadata_patch))

        next_status = self._value(status) if status is not None else current["status"]
        if finished_at is None:
            if next_status in {self._value(value) for value in FINISHED_TASK_STATUSES}:
                finished_at = current.get("finished_at") or time()
            elif next_status in {self._value(value) for value in OPEN_TASK_STATUSES}:
                finished_at = None
            else:
                finished_at = current.get("finished_at")

        detail_inline = current["detail_inline"]
        detail_blob_id = current["detail_blob_id"]
        if detail is not None:
            stored = store_text_content(self.persistence, detail)
            detail_inline = stored.inline
            detail_blob_id = stored.blob_id

        now = time()
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    owner_type = ?,
                    owner_run_id = ?,
                    title = ?,
                    detail_inline = ?,
                    detail_blob_id = ?,
                    evidence_summary = ?,
                    evidence_run_id = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                """,
                (
                    next_status,
                    self._value(owner_type) if owner_type is not None else current["owner_type"],
                    owner_run_id if owner_run_id is not None else current["owner_run_id"],
                    title if title is not None else current["title"],
                    detail_inline,
                    detail_blob_id,
                    evidence_summary
                    if evidence_summary is not None
                    else current["evidence_summary"],
                    evidence_run_id if evidence_run_id is not None else current["evidence_run_id"],
                    self._metadata_field(
                        next_metadata,
                        created_by_run_id=current.get("created_by_run_id"),
                        finished_at=finished_at,
                    ),
                    now,
                    conversation_id,
                    task_id,
                ),
            )
            if updated.rowcount == 0:
                raise KeyError(task_id)
            self._append_event(
                conn,
                conversation_id,
                task_id,
                "updated",
                {
                    "status": next_status,
                    "title": title,
                    "evidence_run_id": evidence_run_id,
                    "evidence_summary": evidence_summary,
                },
                now,
            )
        return self._require_task(conversation_id, task_id)

    def get_task(self, conversation_id: str, task_id: str) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM tasks
                WHERE conversation_id = ? AND id = ?
                """,
                (conversation_id, task_id),
            ).fetchone()
        return self._task_from_row(row) if row is not None else None

    def list_tasks(
        self,
        conversation_id: str,
        *,
        statuses: Iterable[str] | None = None,
        include_finished: bool = True,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [conversation_id]
        where = ["conversation_id = ?"]
        if statuses is not None:
            status_values = [self._value(status) for status in statuses]
            if not status_values:
                return []
            where.append(f"status IN ({','.join('?' for _ in status_values)})")
            params.extend(status_values)
        if not include_finished:
            finished_values = [self._value(status) for status in FINISHED_TASK_STATUSES]
            where.append(f"status NOT IN ({','.join('?' for _ in finished_values)})")
            params.extend(finished_values)
        with self.persistence.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM tasks
                WHERE {' AND '.join(where)}
                ORDER BY created_at, rowid
                """,
                tuple(params),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def list_open_tasks(self, conversation_id: str) -> list[dict[str, Any]]:
        return self.list_tasks(
            conversation_id,
            statuses=[self._value(status) for status in OPEN_TASK_STATUSES],
            include_finished=False,
        )

    def replace_snapshot(
        self,
        conversation_id: str,
        records: Iterable[dict[str, Any]],
    ) -> None:
        with self.persistence.connect() as conn:
            conn.execute(
                "DELETE FROM task_events WHERE conversation_id = ?",
                (conversation_id,),
            )
            conn.execute(
                "DELETE FROM tasks WHERE conversation_id = ?",
                (conversation_id,),
            )
            for item in records:
                self._insert_task_snapshot(conn, conversation_id, dict(item))

    def _require_task(self, conversation_id: str, task_id: str) -> dict[str, Any]:
        task = self.get_task(conversation_id, task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _task_from_row(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["task_id"] = data["id"]
        data["detail"] = data["detail_inline"]
        if data["detail"] is None and data.get("detail_blob_id"):
            data["detail"] = BlobStore(self.persistence).get_text(data["detail_blob_id"])
        data["detail"] = data["detail"] or ""
        envelope = self._load_json(data.pop("metadata_json")) or {}
        if isinstance(envelope, dict) and "metadata" in envelope:
            data["metadata"] = dict(envelope.get("metadata") or {})
            data["created_by_run_id"] = envelope.get("created_by_run_id")
            data["finished_at"] = envelope.get("finished_at")
        else:
            data["metadata"] = dict(envelope or {})
            data["created_by_run_id"] = None
            data["finished_at"] = None
        data["evidence_summary"] = data["evidence_summary"] or ""
        return data

    def _append_event(
        self,
        conn: Any,
        conversation_id: str,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_events (
              task_id,
              conversation_id,
              event_type,
              payload_json,
              created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, conversation_id, event_type, self._json_field(payload), created_at),
        )

    def _insert_task_snapshot(
        self,
        conn: Any,
        conversation_id: str,
        item: dict[str, Any],
    ) -> str:
        task_id = str(item.get("task_id") or item.get("id") or "")
        if not task_id:
            raise KeyError(task_id)
        if str(item.get("conversation_id") or "") != conversation_id:
            raise ValueError("snapshot record conversation_id mismatch")
        detail = str(item.get("detail") or "")
        stored = self._store_text_content(conn, detail)
        created_at = self._float_field(item.get("created_at"), time())
        updated_at = self._float_field(item.get("updated_at"), created_at)
        finished_at = item.get("finished_at")
        conn.execute(
            """
            INSERT INTO tasks (
              id,
              conversation_id,
              status,
              owner_type,
              owner_run_id,
              title,
              detail_inline,
              detail_blob_id,
              evidence_summary,
              evidence_run_id,
              metadata_json,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                conversation_id,
                self._value(item.get("status") or "pending"),
                self._value(item.get("owner_type") or "assistant"),
                item.get("owner_run_id"),
                str(item.get("title") or ""),
                stored.inline,
                stored.blob_id,
                str(item.get("evidence_summary") or ""),
                item.get("evidence_run_id"),
                self._metadata_field(
                    dict(item.get("metadata") or {}),
                    created_by_run_id=item.get("created_by_run_id"),
                    finished_at=self._float_or_none(finished_at),
                ),
                created_at,
                updated_at,
            ),
        )
        return task_id

    def _metadata_field(
        self,
        metadata: dict[str, Any],
        *,
        created_by_run_id: str | None,
        finished_at: float | None,
    ) -> str:
        return self._json_field(
            {
                "metadata": dict(metadata),
                "created_by_run_id": created_by_run_id,
                "finished_at": finished_at,
            }
        ) or "{}"

    def _json_field(self, value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _load_json(self, value: str | None) -> Any:
        if not value:
            return None
        return json.loads(value)

    def _value(self, value: Any) -> str:
        return str(getattr(value, "value", value))

    def _float_field(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _float_or_none(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

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
