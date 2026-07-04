from __future__ import annotations

import json
import uuid
from typing import Any

from .blob_store import BlobStore
from .content import store_text_content
from .database import SQLitePersistence


class ChatRepository:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self.persistence = persistence

    def create_conversation(self, title: str = "") -> str:
        conversation_id = str(uuid.uuid4())
        with self.persistence.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations (
                  id,
                  title,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
                """,
                (conversation_id, title),
            )
        return conversation_id

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return dict(row)

    def create_node(
        self,
        conversation_id: str,
        parent_id: str | None,
        child_order: int = 0,
        **fields: Any,
    ) -> str:
        node_id = str(uuid.uuid4())
        with self.persistence.connect() as conn:
            parent = None
            if parent_id is not None:
                parent = conn.execute(
                    """
                    SELECT depth
                    FROM nodes
                    WHERE conversation_id = ? AND id = ?
                    """,
                    (conversation_id, parent_id),
                ).fetchone()
                if parent is None:
                    raise KeyError(parent_id)

            depth = 0 if parent is None else parent["depth"] + 1
            status = fields.pop("status", "complete")
            model_id = fields.pop("model_id", None)
            provider_id = fields.pop("provider_id", None)
            tool_permission_mode = fields.pop("tool_permission_mode", None)
            turn_usage_json = self._json_field(fields.pop("turn_usage", None))
            branch_usage_json = self._json_field(fields.pop("branch_usage", None))
            active_context_usage_json = self._json_field(
                fields.pop("active_context_usage", None)
            )
            if fields:
                unknown = ", ".join(sorted(fields))
                raise TypeError(f"Unsupported node fields: {unknown}")

            conn.execute(
                """
                INSERT INTO nodes (
                  id,
                  conversation_id,
                  parent_id,
                  child_order,
                  depth,
                  status,
                  model_id,
                  provider_id,
                  tool_permission_mode,
                  turn_usage_json,
                  branch_usage_json,
                  active_context_usage_json,
                  created_at,
                  updated_at
                )
                VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  strftime('%s', 'now'),
                  strftime('%s', 'now')
                )
                """,
                (
                    node_id,
                    conversation_id,
                    parent_id,
                    child_order,
                    depth,
                    status,
                    model_id,
                    provider_id,
                    tool_permission_mode,
                    turn_usage_json,
                    branch_usage_json,
                    active_context_usage_json,
                ),
            )

            conversation = conn.execute(
                """
                SELECT root_node_id
                FROM conversations
                WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise KeyError(conversation_id)
            if conversation["root_node_id"] is None:
                conn.execute(
                    """
                    UPDATE conversations
                    SET root_node_id = ?,
                        current_node_id = ?,
                        updated_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (node_id, node_id, conversation_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE conversations
                    SET current_node_id = ?,
                        updated_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (node_id, conversation_id),
                )

        return node_id

    def add_message(
        self,
        conversation_id: str,
        node_id: str | None,
        role: str,
        content: str,
        subtype: str | None = None,
        hidden: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        message_id = str(uuid.uuid4())
        stored = store_text_content(self.persistence, content)
        with self.persistence.connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                  id,
                  conversation_id,
                  node_id,
                  role,
                  subtype,
                  content_inline,
                  content_blob_id,
                  preview,
                  hidden,
                  metadata_json,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
                """,
                (
                    message_id,
                    conversation_id,
                    node_id,
                    role,
                    subtype,
                    stored.inline,
                    stored.blob_id,
                    stored.preview,
                    1 if hidden else 0,
                    self._json_field(metadata),
                ),
            )
        return message_id

    def get_message(self, message_id: str) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return dict(row)

    def get_message_content(self, message_id: str) -> str:
        message = self.get_message(message_id)
        if message["content_inline"] is not None:
            return message["content_inline"]
        blob_id = message["content_blob_id"]
        if not blob_id:
            return ""
        return BlobStore(self.persistence).get_text(blob_id)

    def list_node_messages(self, node_id: str) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM messages
                WHERE node_id = ?
                ORDER BY created_at, rowid
                """,
                (node_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _json_field(self, value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
