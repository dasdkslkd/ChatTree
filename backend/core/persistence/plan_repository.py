from __future__ import annotations

import gzip
import hashlib
import json
import os
import uuid
from time import time
from typing import Any, Iterable

from .blob_store import BlobStore
from .content import INLINE_TEXT_LIMIT, StoredText, store_text_content
from .database import SQLitePersistence


ACTIVE_PLAN_STATUS_VALUES = ("active", "awaiting_question", "awaiting_approval")


class SQLitePlanRepository:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self.persistence = persistence

    def create_plan(
        self,
        conversation_id: str,
        *,
        entered_node_id: str | None = None,
        previous_permission_mode: str = "modify_only",
        entered_run_id: str | None = None,
    ) -> str:
        plan_id = f"plan_{uuid.uuid4().hex}"
        now = time()
        with self.persistence.connect() as conn:
            conn.execute(
                """
                INSERT INTO plans (
                  id,
                  conversation_id,
                  status,
                  entered_node_id,
                  entered_run_id,
                  previous_permission_mode,
                  feedback_json,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    conversation_id,
                    entered_node_id,
                    entered_run_id,
                    previous_permission_mode,
                    self._json_field([]),
                    now,
                    now,
                ),
            )
            self._append_event(
                conn,
                conversation_id,
                plan_id,
                "created",
                {
                    "entered_node_id": entered_node_id,
                    "entered_run_id": entered_run_id,
                    "previous_permission_mode": previous_permission_mode,
                },
                now,
            )
        return plan_id

    def submit_plan(
        self,
        conversation_id: str,
        plan_id: str,
        *,
        plan: str,
        submitted_node_id: str | None = None,
        submitted_run_id: str | None = None,
    ) -> dict[str, Any]:
        stored = store_text_content(self.persistence, plan)
        now = time()
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE plans
                SET status = 'awaiting_approval',
                    plan_inline = ?,
                    plan_blob_id = ?,
                    plan_preview = ?,
                    submitted_node_id = ?,
                    submitted_run_id = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                """,
                (
                    stored.inline,
                    stored.blob_id,
                    stored.preview,
                    submitted_node_id,
                    submitted_run_id,
                    now,
                    conversation_id,
                    plan_id,
                ),
            )
            if updated.rowcount == 0:
                raise KeyError(plan_id)
            self._append_event(
                conn,
                conversation_id,
                plan_id,
                "submitted",
                {
                    "submitted_node_id": submitted_node_id,
                    "submitted_run_id": submitted_run_id,
                    "plan_preview": stored.preview,
                },
                now,
            )
        return self._require_plan(conversation_id, plan_id)

    def ask_question(
        self,
        conversation_id: str,
        plan_id: str,
        *,
        question: dict[str, Any] | str,
        options: Iterable[dict[str, Any]] | None = None,
        asked_node_id: str | None = None,
        asked_run_id: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(question) if isinstance(question, dict) else {
            "question": str(question),
            "options": list(options or []),
            "asked_node_id": asked_node_id,
            "asked_run_id": asked_run_id,
            "created_at": time(),
        }
        now = time()
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE plans
                SET status = 'awaiting_question',
                    question_json = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                """,
                (self._json_field(payload), now, conversation_id, plan_id),
            )
            if updated.rowcount == 0:
                raise KeyError(plan_id)
            self._append_event(conn, conversation_id, plan_id, "question_asked", payload, now)
        return self._require_plan(conversation_id, plan_id)

    def answer_question(
        self,
        conversation_id: str,
        plan_id: str,
        *,
        answer: str,
    ) -> dict[str, Any]:
        plan = self._require_plan(conversation_id, plan_id)
        question_payload = dict(plan.get("question") or {})
        now = time()
        question_payload["answer"] = str(answer)
        question_payload["answered_at"] = now
        feedback = list(plan.get("feedback") or [])
        feedback.append({
            "question": str(question_payload.get("question") or ""),
            "answer": str(answer),
            "created_at": now,
            "kind": "question_answer",
        })
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE plans
                SET status = 'active',
                    question_json = ?,
                    feedback_json = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                """,
                (
                    self._json_field(question_payload),
                    self._json_field(feedback),
                    now,
                    conversation_id,
                    plan_id,
                ),
            )
            if updated.rowcount == 0:
                raise KeyError(plan_id)
            self._append_event(
                conn,
                conversation_id,
                plan_id,
                "question_answered",
                question_payload,
                now,
            )
        return self._require_plan(conversation_id, plan_id)

    def approve_plan(
        self,
        conversation_id: str,
        plan_id: str,
        *,
        approved_run_id: str | None = None,
    ) -> dict[str, Any]:
        now = time()
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE plans
                SET status = 'approved',
                    approved_run_id = ?,
                    approved_at = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                """,
                (approved_run_id, now, now, conversation_id, plan_id),
            )
            if updated.rowcount == 0:
                raise KeyError(plan_id)
            self._append_event(
                conn,
                conversation_id,
                plan_id,
                "approved",
                {"approved_run_id": approved_run_id},
                now,
            )
        return self._require_plan(conversation_id, plan_id)

    def reject_plan(
        self,
        conversation_id: str,
        plan_id: str,
        *,
        feedback: str = "",
    ) -> dict[str, Any]:
        plan = self._require_plan(conversation_id, plan_id)
        now = time()
        feedback_items = list(plan.get("feedback") or [])
        feedback_items.append({"feedback": str(feedback or ""), "created_at": now})
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE plans
                SET status = 'active',
                    rejected_at = ?,
                    feedback_json = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                """,
                (
                    now,
                    self._json_field(feedback_items),
                    now,
                    conversation_id,
                    plan_id,
                ),
            )
            if updated.rowcount == 0:
                raise KeyError(plan_id)
            self._append_event(
                conn,
                conversation_id,
                plan_id,
                "rejected",
                {"feedback": str(feedback or "")},
                now,
            )
        return self._require_plan(conversation_id, plan_id)

    def get_plan(self, conversation_id: str, plan_id: str) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM plans
                WHERE conversation_id = ? AND id = ?
                """,
                (conversation_id, plan_id),
            ).fetchone()
        return self._plan_from_row(row) if row is not None else None

    def get_active_or_awaiting(self, conversation_id: str) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_PLAN_STATUS_VALUES)
        with self.persistence.connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM plans
                WHERE conversation_id = ?
                  AND status IN ({placeholders})
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (conversation_id, *ACTIVE_PLAN_STATUS_VALUES),
            ).fetchone()
        return self._plan_from_row(row) if row is not None else None

    def get_latest(self, conversation_id: str) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM plans
                WHERE conversation_id = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return self._plan_from_row(row) if row is not None else None

    def list_plans(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM plans
                WHERE conversation_id = ?
                ORDER BY created_at, rowid
                """,
                (conversation_id,),
            ).fetchall()
        return [self._plan_from_row(row) for row in rows]

    def replace_snapshot(
        self,
        conversation_id: str,
        *,
        plans: Iterable[dict[str, Any]],
        pending_context: Iterable[dict[str, Any]],
    ) -> None:
        with self.persistence.connect() as conn:
            conn.execute(
                "DELETE FROM plan_events WHERE conversation_id = ?",
                (conversation_id,),
            )
            conn.execute(
                "DELETE FROM plans WHERE conversation_id = ?",
                (conversation_id,),
            )
            plan_ids: set[str] = set()
            for item in plans:
                plan_id = self._insert_plan_snapshot(conn, conversation_id, dict(item))
                plan_ids.add(plan_id)
            for item in pending_context:
                payload = dict(item)
                payload_conversation_id = payload.get("conversation_id")
                if payload_conversation_id is None or payload_conversation_id == "":
                    payload["conversation_id"] = conversation_id
                elif str(payload_conversation_id) != conversation_id:
                    raise ValueError("snapshot context conversation_id mismatch")
                plan_id = str(payload.get("plan_id") or "")
                if not plan_id or plan_id not in plan_ids:
                    raise KeyError(plan_id)
                self._append_event(
                    conn,
                    conversation_id,
                    plan_id,
                    "pending_context",
                    payload,
                    self._float_field(payload.get("created_at"), time()),
                )

    def append_pending_context(
        self,
        conversation_id: str,
        plan_id: str,
        payload: dict[str, Any],
    ) -> None:
        with self.persistence.connect() as conn:
            self._append_event(
                conn,
                conversation_id,
                plan_id,
                "pending_context",
                dict(payload),
                time(),
            )

    def consume_pending_context(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            rows = conn.execute(
                """
                WITH pending AS (
                  SELECT id
                  FROM plan_events
                  WHERE conversation_id = ? AND event_type = 'pending_context'
                )
                DELETE FROM plan_events
                WHERE id IN (SELECT id FROM pending)
                RETURNING id, payload_json, created_at
                """,
                (conversation_id,),
            ).fetchall()
        rows = sorted(rows, key=lambda row: (row["created_at"], row["id"]))
        return [self._load_json(row["payload_json"]) or {} for row in rows]

    def peek_pending_context(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM plan_events
                WHERE conversation_id = ? AND event_type = 'pending_context'
                ORDER BY created_at, id
                """,
                (conversation_id,),
            ).fetchall()
        return [self._load_json(row["payload_json"]) or {} for row in rows]

    def _require_plan(self, conversation_id: str, plan_id: str) -> dict[str, Any]:
        plan = self.get_plan(conversation_id, plan_id)
        if plan is None:
            raise KeyError(plan_id)
        return plan

    def _plan_from_row(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["plan_id"] = data["id"]
        data["plan"] = data["plan_inline"]
        if data["plan"] is None and data.get("plan_blob_id"):
            data["plan"] = BlobStore(self.persistence).get_text(data["plan_blob_id"])
        data["plan"] = data["plan"] or ""
        data["question"] = self._load_json(data.pop("question_json")) if data.get("question_json") else None
        data["feedback"] = self._load_json(data.pop("feedback_json")) or []
        return data

    def _append_event(
        self,
        conn: Any,
        conversation_id: str,
        plan_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO plan_events (
              plan_id,
              conversation_id,
              event_type,
              payload_json,
              created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (plan_id, conversation_id, event_type, self._json_field(payload), created_at),
        )

    def _insert_plan_snapshot(
        self,
        conn: Any,
        conversation_id: str,
        item: dict[str, Any],
    ) -> str:
        plan_id = str(item.get("plan_id") or item.get("id") or "")
        if not plan_id:
            raise KeyError(plan_id)
        if str(item.get("conversation_id") or "") != conversation_id:
            raise ValueError("snapshot record conversation_id mismatch")
        plan_text = str(item.get("plan") or "")
        stored = self._store_text_content(conn, plan_text)
        created_at = self._float_field(item.get("created_at"), time())
        updated_at = self._float_field(item.get("updated_at"), created_at)
        conn.execute(
            """
            INSERT INTO plans (
              id,
              conversation_id,
              status,
              entered_node_id,
              submitted_node_id,
              entered_run_id,
              submitted_run_id,
              approved_run_id,
              previous_permission_mode,
              plan_inline,
              plan_blob_id,
              plan_preview,
              question_json,
              feedback_json,
              created_at,
              updated_at,
              approved_at,
              rejected_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                conversation_id,
                self._value(item.get("status") or "active"),
                item.get("entered_node_id"),
                item.get("submitted_node_id"),
                item.get("entered_run_id"),
                item.get("submitted_run_id"),
                str(item.get("previous_permission_mode") or "modify_only"),
                stored.inline,
                stored.blob_id,
                stored.preview,
                self._json_field(item.get("question"))
                if isinstance(item.get("question"), dict)
                else None,
                self._json_field(list(item.get("feedback") or [])),
                created_at,
                updated_at,
                item.get("approved_at"),
                item.get("rejected_at"),
            ),
        )
        return plan_id

    def _json_field(self, value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _load_json(self, value: str | None) -> Any:
        if not value:
            return None
        return json.loads(value)

    def _float_field(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _value(self, value: Any) -> str:
        return str(getattr(value, "value", value))

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
