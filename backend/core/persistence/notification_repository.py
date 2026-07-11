from __future__ import annotations

import json
import uuid
from typing import Any

from .database import SQLitePersistence


class SQLiteTaskNotificationRepository:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self.persistence = persistence

    def upsert_for_run(
        self,
        *,
        conversation_id: str,
        source_run_id: str,
        source_run_kind: str,
        summary: str = "",
        content: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_json = self._json_field(payload or {})
        with self.persistence.connect() as conn:
            existing = conn.execute(
                """
                SELECT *
                FROM task_notifications
                WHERE conversation_id = ? AND source_run_id = ?
                """,
                (conversation_id, source_run_id),
            ).fetchone()
            if existing is None:
                notification_id = str(uuid.uuid4())
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
                      payload_json,
                      created_at,
                      updated_at
                    )
                    VALUES (
                      ?, ?, ?, ?, 'unbound', ?, ?, ?,
                      strftime('%s', 'now'),
                      strftime('%s', 'now')
                    )
                    """,
                    (
                        notification_id,
                        conversation_id,
                        source_run_id,
                        source_run_kind,
                        summary,
                        content,
                        payload_json,
                    ),
                )
            else:
                notification_id = existing["id"]
                if existing["status"] not in {"observed", "deleted", "delivered", "delivering", "delivery_failed", "delivery_cancelled"}:
                    conn.execute(
                        """
                        UPDATE task_notifications
                        SET source_run_kind = ?,
                            summary = ?,
                            content = ?,
                            payload_json = ?,
                            updated_at = strftime('%s', 'now')
                        WHERE id = ?
                        """,
                        (
                            source_run_kind,
                            summary,
                            content,
                            payload_json,
                            notification_id,
                        ),
                    )
            row = conn.execute(
                "SELECT * FROM task_notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
        return self._from_row(row)

    def get(self, notification_id: str) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
        return self._from_row(row) if row else None

    def get_by_source_run(self, source_run_id: str) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_notifications WHERE source_run_id = ?",
                (source_run_id,),
            ).fetchone()
        return self._from_row(row) if row else None

    def list_for_conversation(
        self,
        conversation_id: str,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT *
            FROM task_notifications
            WHERE conversation_id = ?
        """
        params: list[Any] = [conversation_id]
        if not include_deleted:
            sql += " AND status != 'deleted'"
        sql += " ORDER BY updated_at DESC, id"
        with self.persistence.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._from_row(row) for row in rows]

    def list_bound_for_node(self, conversation_id: str, delivery_node_id: str) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM task_notifications
                WHERE conversation_id = ?
                  AND delivery_node_id = ?
                  AND status = 'delivering'
                ORDER BY updated_at, id
                """,
                (conversation_id, delivery_node_id),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_bound(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        conversation_clause = ""
        if conversation_id is not None:
            conversation_clause = "AND conversation_id = ?"
            params.append(conversation_id)
        with self.persistence.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM task_notifications
                WHERE status = 'bound'
                  {conversation_clause}
                ORDER BY updated_at, id
                """,
                tuple(params),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_pending_publications(self) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM task_notifications
                WHERE status IN ('unbound', 'bound', 'delivering')
                ORDER BY updated_at, id
                """
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def bind(self, notification_id: str, delivery_node_id: str, *, bound_by: str) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            row = conn.execute(
                "SELECT status FROM task_notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise KeyError(notification_id)
            if row["status"] in {"observed", "deleted", "delivered", "delivering"}:
                raise ValueError(f"notification {notification_id} is {row['status']}")
            conn.execute(
                """
                UPDATE task_notifications
                SET status = 'bound',
                    delivery_node_id = ?,
                    bound_at = strftime('%s', 'now'),
                    bound_by = ?,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (delivery_node_id, bound_by, notification_id),
            )
            updated = conn.execute(
                "SELECT * FROM task_notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
        return self._from_row(updated)

    def mark_delivering(self, notification_id: str, delivered_run_id: str) -> dict[str, Any]:
        return self._update(
            notification_id,
            status="delivering",
            delivered_run_id=delivered_run_id,
        )

    def mark_delivered(
        self,
        notification_id: str,
        *,
        delivered_run_id: str | None = None,
        delivered_node_id: str | None = None,
    ) -> dict[str, Any]:
        return self._update(
            notification_id,
            status="delivered",
            delivered_run_id=delivered_run_id,
            delivered_node_id=delivered_node_id,
        )

    def mark_delivery_failed(
        self,
        notification_id: str,
        *,
        delivered_run_id: str | None = None,
        delivered_node_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return self._update(
            notification_id,
            status="delivery_failed",
            delivered_run_id=delivered_run_id,
            delivered_node_id=delivered_node_id,
        )

    def mark_delivery_cancelled(
        self,
        notification_id: str,
        *,
        delivered_run_id: str | None = None,
        delivered_node_id: str | None = None,
    ) -> dict[str, Any]:
        return self._update(
            notification_id,
            status="delivery_cancelled",
            delivered_run_id=delivered_run_id,
            delivered_node_id=delivered_node_id,
        )

    def mark_observed_by_source(self, source_run_id: str) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                "SELECT id FROM task_notifications WHERE source_run_id = ?",
                (source_run_id,),
            ).fetchone()
            if row is None:
                return None
            notification_id = row["id"]
            conn.execute(
                """
                UPDATE task_notifications
                SET status = 'observed',
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                  AND status NOT IN ('deleted', 'delivered')
                """,
                (notification_id,),
            )
            updated = conn.execute(
                "SELECT * FROM task_notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
        return self._from_row(updated)

    def delete(self, notification_id: str) -> dict[str, Any]:
        return self._update(notification_id, status="deleted")

    def _update(self, notification_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"status", "delivered_run_id", "delivered_node_id"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            raise ValueError("no notification fields to update")
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values()) + [notification_id]
        with self.persistence.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE task_notifications
                SET {assignments},
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                tuple(params),
            )
            if cursor.rowcount == 0:
                raise KeyError(notification_id)
            row = conn.execute(
                "SELECT * FROM task_notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
        return self._from_row(row)

    def _from_row(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = self._load_json(data.pop("payload_json", None)) or {}
        return data

    @staticmethod
    def _json_field(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load_json(value: str | None) -> Any:
        if not value:
            return None
        return json.loads(value)
