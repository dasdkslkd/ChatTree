from __future__ import annotations

import json
import uuid
from typing import Any

from .database import SQLitePersistence


class TranscriptProjection:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self.persistence = persistence

    def upsert_message_item(
        self,
        conversation_id: str,
        node_id: str,
        message_id: str,
        item_type: str,
        *,
        local_order: int,
        visibility: str = "main",
        status: str | None = None,
        summary: str = "",
        preview: str | None = None,
        anchor_node_id: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> str:
        with self.persistence.connect() as conn:
            message = conn.execute(
                """
                SELECT preview, node_id
                FROM messages
                WHERE conversation_id = ? AND id = ?
                """,
                (conversation_id, message_id),
            ).fetchone()
            if message is None:
                raise KeyError(message_id)
            if message["node_id"] != node_id:
                raise ValueError(
                    f"Message {message_id} belongs to node {message['node_id']}"
                )
            return self._upsert_item(
                conn,
                lookup_sql="""
                    SELECT id
                    FROM transcript_items
                    WHERE conversation_id = ?
                      AND message_id = ?
                      AND item_type = ?
                """,
                lookup_params=(conversation_id, message_id, item_type),
                values={
                    "conversation_id": conversation_id,
                    "node_id": node_id,
                    "anchor_node_id": anchor_node_id,
                    "message_id": message_id,
                    "item_type": item_type,
                    "local_order": local_order,
                    "visibility": visibility,
                    "status": status,
                    "summary": summary,
                    "preview": message["preview"] if preview is None else preview,
                    "props_json": self._json_field(props),
                },
            )

    def upsert_plan_card(
        self,
        conversation_id: str,
        node_id: str,
        *,
        plan_id: str,
        status: str,
        preview: str,
        local_order: int,
        visibility: str = "main",
        summary: str = "",
        anchor_node_id: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> str:
        with self.persistence.connect() as conn:
            self._ensure_plan(conn, conversation_id, plan_id, status, preview)
            return self._upsert_item(
                conn,
                lookup_sql="""
                    SELECT id
                    FROM transcript_items
                    WHERE conversation_id = ?
                      AND plan_id = ?
                      AND item_type = 'plan_card'
                """,
                lookup_params=(conversation_id, plan_id),
                values={
                    "conversation_id": conversation_id,
                    "node_id": node_id,
                    "anchor_node_id": anchor_node_id,
                    "plan_id": plan_id,
                    "item_type": "plan_card",
                    "local_order": local_order,
                    "visibility": visibility,
                    "status": status,
                    "summary": summary,
                    "preview": preview,
                    "props_json": self._json_field(props),
                },
            )

    def upsert_run_draft(
        self,
        conversation_id: str,
        node_id: str,
        *,
        run_id: str,
        status: str = "running",
        preview: str = "",
        local_order: int = 0,
        visibility: str = "main",
        summary: str = "",
        anchor_node_id: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> str:
        with self.persistence.connect() as conn:
            self._ensure_run(conn, conversation_id, run_id, status, summary)
            return self._upsert_item(
                conn,
                lookup_sql="""
                    SELECT id
                    FROM transcript_items
                    WHERE conversation_id = ?
                      AND run_id = ?
                      AND item_type = 'run_draft'
                """,
                lookup_params=(conversation_id, run_id),
                values={
                    "conversation_id": conversation_id,
                    "node_id": node_id,
                    "anchor_node_id": anchor_node_id,
                    "run_id": run_id,
                    "item_type": "run_draft",
                    "local_order": local_order,
                    "visibility": visibility,
                    "status": status,
                    "summary": summary,
                    "preview": preview,
                    "props_json": self._json_field(props),
                },
            )

    def upsert_control_event(
        self,
        conversation_id: str,
        node_id: str,
        *,
        event_type: str,
        plan_id: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
        preview: str = "",
        local_order: int = 0,
        visibility: str = "hidden",
        summary: str = "",
        anchor_node_id: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> str:
        event_props = dict(props or {})
        event_props.setdefault("event_type", event_type)
        with self.persistence.connect() as conn:
            if run_id:
                self._ensure_run(conn, conversation_id, run_id, "running", summary)
            return self._upsert_item(
                conn,
                lookup_sql="""
                    SELECT id
                    FROM transcript_items
                    WHERE conversation_id = ?
                      AND node_id = ?
                      AND item_type = 'control_event'
                """,
                lookup_params=(conversation_id, node_id),
                values={
                    "conversation_id": conversation_id,
                    "node_id": node_id,
                    "anchor_node_id": anchor_node_id,
                    "plan_id": plan_id,
                    "run_id": run_id,
                    "item_type": "control_event",
                    "local_order": local_order,
                    "visibility": visibility,
                    "status": status,
                    "summary": summary,
                    "preview": preview,
                    "props_json": self._json_field(event_props),
                },
            )

    def upsert_task_item(
        self,
        conversation_id: str,
        node_id: str,
        *,
        task_id: str,
        item_type: str = "task_notification",
        status: str = "",
        preview: str = "",
        local_order: int = 0,
        visibility: str = "main",
        summary: str = "",
        anchor_node_id: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> str:
        with self.persistence.connect() as conn:
            self._ensure_task(conn, conversation_id, task_id, status, preview)
            return self._upsert_item(
                conn,
                lookup_sql="""
                    SELECT id
                    FROM transcript_items
                    WHERE conversation_id = ?
                      AND task_id = ?
                      AND item_type = ?
                """,
                lookup_params=(conversation_id, task_id, item_type),
                values={
                    "conversation_id": conversation_id,
                    "node_id": node_id,
                    "anchor_node_id": anchor_node_id,
                    "task_id": task_id,
                    "item_type": item_type,
                    "local_order": local_order,
                    "visibility": visibility,
                    "status": status,
                    "summary": summary,
                    "preview": preview,
                    "props_json": self._json_field(props),
                },
            )

    def list_for_branch(
        self, conversation_id: str, tip_node_id: str | None
    ) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            if tip_node_id is None:
                conversation = conn.execute(
                    """
                    SELECT current_node_id, root_node_id
                    FROM conversations
                    WHERE id = ?
                    """,
                    (conversation_id,),
                ).fetchone()
                if conversation is None:
                    raise KeyError(conversation_id)
                tip_node_id = (
                    conversation["current_node_id"] or conversation["root_node_id"]
                )
                if tip_node_id is None:
                    return []
            else:
                tip = conn.execute(
                    """
                    SELECT id
                    FROM nodes
                    WHERE conversation_id = ? AND id = ?
                    """,
                    (conversation_id, tip_node_id),
                ).fetchone()
                if tip is None:
                    raise KeyError(tip_node_id)

            rows = conn.execute(
                """
                WITH RECURSIVE branch(id, parent_id, node_depth) AS (
                  SELECT id, parent_id, depth
                  FROM nodes
                  WHERE conversation_id = ? AND id = ?

                  UNION ALL

                  SELECT parent.id, parent.parent_id, parent.depth
                  FROM nodes AS parent
                  JOIN branch ON branch.parent_id = parent.id
                  WHERE parent.conversation_id = ?
                )
                SELECT transcript_items.*
                FROM branch
                JOIN transcript_items
                  ON transcript_items.conversation_id = ?
                 AND (
                       transcript_items.node_id = branch.id
                    OR (
                         transcript_items.node_id IS NULL
                     AND transcript_items.anchor_node_id = branch.id
                    )
                 )
                ORDER BY branch.node_depth, transcript_items.local_order,
                         transcript_items.created_at, transcript_items.id
                """,
                (conversation_id, tip_node_id, conversation_id, conversation_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def _upsert_item(
        self,
        conn: Any,
        *,
        lookup_sql: str,
        lookup_params: tuple[Any, ...],
        values: dict[str, Any],
    ) -> str:
        existing = conn.execute(lookup_sql, lookup_params).fetchone()
        item_id = existing["id"] if existing else str(uuid.uuid4())
        if existing:
            conn.execute(
                """
                UPDATE transcript_items
                SET conversation_id = ?,
                    node_id = ?,
                    anchor_node_id = ?,
                    run_id = ?,
                    plan_id = ?,
                    task_id = ?,
                    message_id = ?,
                    item_type = ?,
                    local_order = ?,
                    visibility = ?,
                    status = ?,
                    summary = ?,
                    preview = ?,
                    props_json = ?,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (
                    values.get("conversation_id"),
                    values.get("node_id"),
                    values.get("anchor_node_id"),
                    values.get("run_id"),
                    values.get("plan_id"),
                    values.get("task_id"),
                    values.get("message_id"),
                    values.get("item_type"),
                    values.get("local_order"),
                    values.get("visibility"),
                    values.get("status"),
                    values.get("summary", ""),
                    values.get("preview", ""),
                    values.get("props_json"),
                    item_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO transcript_items (
                  id,
                  conversation_id,
                  node_id,
                  anchor_node_id,
                  run_id,
                  plan_id,
                  task_id,
                  message_id,
                  item_type,
                  local_order,
                  visibility,
                  status,
                  summary,
                  preview,
                  props_json,
                  created_at,
                  updated_at
                )
                VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  strftime('%s', 'now'),
                  strftime('%s', 'now')
                )
                """,
                (
                    item_id,
                    values.get("conversation_id"),
                    values.get("node_id"),
                    values.get("anchor_node_id"),
                    values.get("run_id"),
                    values.get("plan_id"),
                    values.get("task_id"),
                    values.get("message_id"),
                    values.get("item_type"),
                    values.get("local_order"),
                    values.get("visibility"),
                    values.get("status"),
                    values.get("summary", ""),
                    values.get("preview", ""),
                    values.get("props_json"),
                ),
            )
        return item_id

    def _ensure_plan(
        self, conn: Any, conversation_id: str, plan_id: str, status: str, preview: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO plans (
              id,
              conversation_id,
              status,
              plan_preview,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
            ON CONFLICT(id) DO UPDATE SET
              status = excluded.status,
              plan_preview = excluded.plan_preview,
              updated_at = strftime('%s', 'now')
            """,
            (plan_id, conversation_id, status, preview),
        )

    def _ensure_run(
        self, conn: Any, conversation_id: str, run_id: str, status: str, summary: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO runs (
              id,
              conversation_id,
              kind,
              status,
              summary,
              created_at,
              updated_at
            )
            VALUES (?, ?, 'chat', ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
            ON CONFLICT(id) DO UPDATE SET
              status = excluded.status,
              summary = excluded.summary,
              updated_at = strftime('%s', 'now')
            """,
            (run_id, conversation_id, status, summary),
        )

    def _ensure_task(
        self, conn: Any, conversation_id: str, task_id: str, status: str, title: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO tasks (
              id,
              conversation_id,
              status,
              owner_type,
              title,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, 'system', ?, strftime('%s', 'now'), strftime('%s', 'now'))
            ON CONFLICT(id) DO UPDATE SET
              status = excluded.status,
              title = excluded.title,
              updated_at = strftime('%s', 'now')
            """,
            (task_id, conversation_id, status, title),
        )

    def _json_field(self, value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
