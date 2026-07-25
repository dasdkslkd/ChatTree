from __future__ import annotations

import json
import uuid
from time import time
from typing import Any

from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.run_repository import SQLiteRunRepository


TASK_NOTIFICATION_DELIVERY_RUN_KINDS = {"subagent", "workflow", "workflow_step"}
TASK_NOTIFICATION_DELIVERY_POLICIES = {"auto", "notify"}


class TaskNotificationTransitionError(ValueError):
    pass


class TaskNotificationService:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self.persistence = persistence

    def create(
        self,
        *,
        conversation_id: str,
        source_run_id: str,
        source_run_kind: str = "",
        summary: str = "",
        content: str = "",
        notification_id: str | None = None,
    ) -> dict[str, Any]:
        now = int(time())
        row_id = notification_id or f"task_notification_{uuid.uuid4().hex}"
        self._validate_source_run(conversation_id, source_run_id)
        with self.persistence.connect() as conn:
            existing = conn.execute(
                """
                SELECT source_run_id
                FROM task_notifications
                WHERE conversation_id = ? AND id = ?
                """,
                (conversation_id, row_id),
            ).fetchone()
            if existing is not None and existing["source_run_id"] != source_run_id:
                raise TaskNotificationTransitionError(
                    "cannot change task notification source run"
                )
            conn.execute(
                """
                INSERT INTO task_notifications (
                  id,
                  conversation_id,
                  source_run_id,
                  source_run_kind,
                  status,
                  summary,
                  content,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, id) DO UPDATE SET
                  source_run_id = excluded.source_run_id,
                  source_run_kind = excluded.source_run_kind,
                  status = task_notifications.status,
                  summary = excluded.summary,
                  content = excluded.content,
                  updated_at = excluded.updated_at
                """,
                (
                    row_id,
                    conversation_id,
                    source_run_id,
                    source_run_kind,
                    "unbound",
                    summary,
                    content,
                    now,
                    now,
                ),
            )
        return self.get(conversation_id, row_id)

    def create_from_finished_run(self, run: dict[str, Any]) -> dict[str, Any] | None:
        run_kind = str(run.get("kind") or "")
        if run_kind not in TASK_NOTIFICATION_DELIVERY_RUN_KINDS:
            return None
        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        if str(metadata.get("delivery_policy") or "auto") not in TASK_NOTIFICATION_DELIVERY_POLICIES:
            return None
        conversation_id = str(run.get("conversation_id") or "")
        run_id = str(run.get("run_id") or run.get("id") or "")
        if not conversation_id or not run_id:
            return None
        payload = self._terminal_payload(run_id)
        summary = str(run.get("summary") or metadata.get("delegated_task") or run_kind)
        content = self._notification_content(payload)
        return self.create(
            conversation_id=conversation_id,
            source_run_id=run_id,
            source_run_kind=run_kind,
            summary=summary,
            content=content,
            notification_id=f"task_notification_{run_id}",
        )

    def list(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  task_notifications.*,
                  runs.anchor_node_id,
                  runs.target_node_id,
                  COALESCE(NULLIF(task_notifications.source_run_kind, ''), runs.kind, '') AS resolved_source_run_kind
                FROM task_notifications
                LEFT JOIN runs
                  ON runs.conversation_id = task_notifications.conversation_id
                 AND runs.id = task_notifications.source_run_id
                WHERE task_notifications.conversation_id = ?
                ORDER BY task_notifications.created_at, task_notifications.id
                """,
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, conversation_id: str, notification_id: str) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            row = conn.execute(
                """
                SELECT
                  task_notifications.*,
                  runs.anchor_node_id,
                  runs.target_node_id,
                  COALESCE(NULLIF(task_notifications.source_run_kind, ''), runs.kind, '') AS resolved_source_run_kind
                FROM task_notifications
                LEFT JOIN runs
                  ON runs.conversation_id = task_notifications.conversation_id
                 AND runs.id = task_notifications.source_run_id
                WHERE task_notifications.conversation_id = ? AND task_notifications.id = ?
                """,
                (conversation_id, notification_id),
            ).fetchone()
        if row is None:
            raise KeyError(notification_id)
        return dict(row)

    def bind(self, conversation_id: str, notification_id: str, delivery_node_id: str) -> dict[str, Any]:
        now = int(time())
        with self.persistence.connect() as conn:
            self._require_node(conn, conversation_id, delivery_node_id)
            updated = conn.execute(
                """
                UPDATE task_notifications
                SET status = 'bound',
                    delivery_node_id = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                  AND status = 'unbound'
                """,
                (delivery_node_id, now, conversation_id, notification_id),
            )
            if updated.rowcount == 0:
                self._raise_missing_or_invalid_transition(conn, conversation_id, notification_id, "bind")
        return self.get(conversation_id, notification_id)

    def delete(self, conversation_id: str, notification_id: str) -> dict[str, Any]:
        now = int(time())
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE task_notifications
                SET status = 'delivery_cancelled',
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                  AND status IN ('unbound', 'bound', 'delivering')
                """,
                (now, conversation_id, notification_id),
            )
            if updated.rowcount == 0:
                self._raise_missing_or_invalid_transition(conn, conversation_id, notification_id, "delete")
        return self.get(conversation_id, notification_id)

    def start_delivery(
        self,
        conversation_id: str,
        notification_id: str,
        delivery_run_id: str,
    ) -> dict[str, Any]:
        now = int(time())
        with self.persistence.connect() as conn:
            self._require_run(conn, conversation_id, delivery_run_id)
            updated = conn.execute(
                """
                UPDATE task_notifications
                SET status = 'delivering',
                    delivery_run_id = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                  AND status = 'bound'
                """,
                (delivery_run_id, now, conversation_id, notification_id),
            )
            if updated.rowcount == 0:
                self._raise_missing_or_invalid_transition(conn, conversation_id, notification_id, "start_delivery")
        return self.get(conversation_id, notification_id)

    def mark_delivered(self, conversation_id: str, notification_id: str) -> dict[str, Any]:
        return self._finish_delivery(conversation_id, notification_id, "delivered")

    def mark_delivery_failed(self, conversation_id: str, notification_id: str) -> dict[str, Any]:
        return self._finish_delivery(conversation_id, notification_id, "delivery_failed")

    def _finish_delivery(
        self,
        conversation_id: str,
        notification_id: str,
        status: str,
    ) -> dict[str, Any]:
        now = int(time())
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE task_notifications
                SET status = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                  AND status = 'delivering'
                """,
                (status, now, conversation_id, notification_id),
            )
            if updated.rowcount == 0:
                self._raise_missing_or_invalid_transition(conn, conversation_id, notification_id, status)
        return self.get(conversation_id, notification_id)

    def _validate_source_run(self, conversation_id: str, source_run_id: str) -> None:
        with self.persistence.connect() as conn:
            self._require_run(conn, conversation_id, source_run_id)

    @staticmethod
    def _require_run(conn: Any, conversation_id: str, run_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE conversation_id = ? AND id = ?",
            (conversation_id, run_id),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)

    @staticmethod
    def _require_node(conn: Any, conversation_id: str, node_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM nodes WHERE conversation_id = ? AND id = ?",
            (conversation_id, node_id),
        ).fetchone()
        if row is None:
            raise KeyError(node_id)

    @staticmethod
    def _raise_missing_or_invalid_transition(
        conn: Any,
        conversation_id: str,
        notification_id: str,
        action: str,
    ) -> None:
        row = conn.execute(
            "SELECT status FROM task_notifications WHERE conversation_id = ? AND id = ?",
            (conversation_id, notification_id),
        ).fetchone()
        if row is None:
            raise KeyError(notification_id)
        raise TaskNotificationTransitionError(
            f"cannot {action} task notification from {row['status']}"
        )

    def _terminal_payload(self, run_id: str) -> dict[str, Any]:
        events = SQLiteRunRepository(self.persistence).read_events(run_id, 0)
        for event in reversed(events):
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            event_type = str(payload.get("event_type") or payload.get("type") or "")
            if event_type in {
                "subagent_result",
                "subagent_error",
                "workflow_result",
                "workflow_error",
                "workflow_cancelled",
            }:
                return payload
        return {}

    @staticmethod
    def _notification_content(payload: dict[str, Any]) -> str:
        content = payload.get("content")
        if isinstance(content, str) and content.strip():
            return content
        error = payload.get("error")
        if error:
            return str(error)
        result = payload.get("result")
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)
