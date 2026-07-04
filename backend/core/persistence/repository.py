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

    def create_conversation(
        self,
        title: str = "",
        *,
        conversation_id: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
        workspace: dict[str, Any] | None = None,
    ) -> str:
        conversation_id = conversation_id or str(uuid.uuid4())
        with self.persistence.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations (
                  id,
                  title,
                  provider_id,
                  model_id,
                  workspace_json,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
                """,
                (
                    conversation_id,
                    title,
                    provider_id,
                    model_id,
                    self._json_field(workspace),
                ),
            )
        return conversation_id

    def ensure_conversation(
        self,
        conversation_id: str,
        *,
        title: str = "",
        provider_id: str | None = None,
        model_id: str | None = None,
        workspace: dict[str, Any] | None = None,
    ) -> str:
        with self.persistence.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations (
                  id,
                  title,
                  provider_id,
                  model_id,
                  workspace_json,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
                ON CONFLICT(id) DO UPDATE SET
                  title = COALESCE(NULLIF(excluded.title, ''), conversations.title),
                  provider_id = COALESCE(excluded.provider_id, conversations.provider_id),
                  model_id = COALESCE(excluded.model_id, conversations.model_id),
                  workspace_json = COALESCE(excluded.workspace_json, conversations.workspace_json),
                  updated_at = strftime('%s', 'now')
                """,
                (
                    conversation_id,
                    title,
                    provider_id,
                    model_id,
                    self._json_field(workspace),
                ),
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
        node_id: str | None = None,
        **fields: Any,
    ) -> str:
        node_id = node_id or str(uuid.uuid4())
        if parent_id == "None":
            parent_id = None
        with self.persistence.connect() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM nodes
                WHERE conversation_id = ? AND id = ?
                """,
                (conversation_id, node_id),
            ).fetchone()
            if existing is not None:
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
            if (
                parent_id is None
                and conversation["root_node_id"] is not None
                and conversation["root_node_id"] != node_id
            ):
                raise ValueError(
                    f"Conversation {conversation_id} already has a root node"
                )

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

            if parent_id is None:
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

    def ensure_node(
        self,
        conversation_id: str,
        node_id: str,
        parent_id: str | None,
        child_order: int = 0,
        **fields: Any,
    ) -> str:
        return self.create_node(
            conversation_id,
            parent_id,
            child_order=child_order,
            node_id=node_id,
            **fields,
        )

    def add_message(
        self,
        conversation_id: str,
        node_id: str | None,
        role: str,
        content: str,
        subtype: str | None = None,
        hidden: bool = False,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> str:
        message_id = message_id or str(uuid.uuid4())
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
                ON CONFLICT(id) DO UPDATE SET
                  node_id = excluded.node_id,
                  role = excluded.role,
                  subtype = excluded.subtype,
                  content_inline = excluded.content_inline,
                  content_blob_id = excluded.content_blob_id,
                  preview = excluded.preview,
                  hidden = excluded.hidden,
                  metadata_json = excluded.metadata_json
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

    def add_tool_call(
        self,
        conversation_id: str,
        node_id: str,
        *,
        tool_call_id: str | None,
        name: str,
        arguments: Any = None,
        call_index: int = 0,
        status: str = "complete",
        run_id: str | None = None,
        assistant_message_id: str | None = None,
    ) -> str:
        call_id = tool_call_id or str(uuid.uuid4())
        args_text = arguments if isinstance(arguments, str) else self._json_field(arguments)
        stored = store_text_content(self.persistence, args_text or "")
        with self.persistence.connect() as conn:
            run_id = self._existing_id(conn, "runs", conversation_id, run_id)
            assistant_message_id = self._existing_id(
                conn, "messages", conversation_id, assistant_message_id
            )
            conn.execute(
                """
                INSERT INTO tool_calls (
                  id,
                  conversation_id,
                  node_id,
                  run_id,
                  assistant_message_id,
                  call_index,
                  name,
                  args_inline,
                  args_blob_id,
                  args_preview,
                  status,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
                ON CONFLICT(conversation_id, id) DO UPDATE SET
                  run_id = COALESCE(excluded.run_id, tool_calls.run_id),
                  assistant_message_id = COALESCE(excluded.assistant_message_id, tool_calls.assistant_message_id),
                  call_index = CASE
                    WHEN excluded.call_index = 0 AND tool_calls.call_index <> 0
                      THEN tool_calls.call_index
                    ELSE excluded.call_index
                  END,
                  name = COALESCE(NULLIF(excluded.name, ''), tool_calls.name),
                  args_inline = CASE
                    WHEN COALESCE(excluded.args_inline, '') = '' AND excluded.args_blob_id IS NULL
                      THEN tool_calls.args_inline
                    ELSE excluded.args_inline
                  END,
                  args_blob_id = CASE
                    WHEN COALESCE(excluded.args_inline, '') = '' AND excluded.args_blob_id IS NULL
                      THEN tool_calls.args_blob_id
                    ELSE excluded.args_blob_id
                  END,
                  args_preview = CASE
                    WHEN COALESCE(excluded.args_inline, '') = '' AND excluded.args_blob_id IS NULL
                      THEN tool_calls.args_preview
                    ELSE excluded.args_preview
                  END,
                  status = excluded.status,
                  updated_at = strftime('%s', 'now')
                """,
                (
                    call_id,
                    conversation_id,
                    node_id,
                    run_id,
                    assistant_message_id,
                    call_index,
                    name,
                    stored.inline,
                    stored.blob_id,
                    stored.preview,
                    status,
                ),
            )
        return call_id

    def tool_call_exists(self, conversation_id: str, tool_call_id: str | None) -> bool:
        if not tool_call_id:
            return False
        with self.persistence.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM tool_calls
                WHERE conversation_id = ? AND id = ?
                """,
                (conversation_id, tool_call_id),
            ).fetchone()
        return row is not None

    def add_tool_result(
        self,
        conversation_id: str,
        node_id: str,
        *,
        tool_result_id: str | None = None,
        tool_call_id: str | None,
        output: str,
        status: str = "complete",
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        preview_limit: int = 4096,
    ) -> str:
        result_id = tool_result_id or str(uuid.uuid4())
        value = output or ""
        preview = value[:preview_limit]
        blob_id = None
        if len(value) > len(preview):
            blob_id = BlobStore(self.persistence).put_text(value).blob_id
        with self.persistence.connect() as conn:
            run_id = self._existing_id(conn, "runs", conversation_id, run_id)
            conn.execute(
                """
                INSERT INTO tool_results (
                  id,
                  conversation_id,
                  node_id,
                  run_id,
                  tool_call_id,
                  status,
                  output_preview,
                  output_blob_id,
                  output_size,
                  truncated,
                  metadata_json,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(id) DO UPDATE SET
                  run_id = COALESCE(excluded.run_id, tool_results.run_id),
                  tool_call_id = COALESCE(excluded.tool_call_id, tool_results.tool_call_id),
                  status = excluded.status,
                  output_preview = excluded.output_preview,
                  output_blob_id = excluded.output_blob_id,
                  output_size = excluded.output_size,
                  truncated = excluded.truncated,
                  metadata_json = excluded.metadata_json
                """,
                (
                    result_id,
                    conversation_id,
                    node_id,
                    run_id,
                    tool_call_id,
                    status,
                    preview,
                    blob_id,
                    len(value),
                    1 if blob_id else 0,
                    self._json_field(metadata),
                ),
            )
        return result_id

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

    def _existing_id(
        self,
        conn: Any,
        table: str,
        conversation_id: str,
        value: str | None,
    ) -> str | None:
        if not value:
            return None
        row = conn.execute(
            f"SELECT id FROM {table} WHERE conversation_id = ? AND id = ?",
            (conversation_id, value),
        ).fetchone()
        return value if row is not None else None
