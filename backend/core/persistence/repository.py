from __future__ import annotations

import json
import uuid
from typing import Any

from .blob_store import BlobStore
from .content import store_text_content
from .database import SQLitePersistence


_UNSET = object()


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
        reasoning_effort: str | None = None,
        thinking_enabled: bool | None = None,
        multi_agent_mode: str | None = None,
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
                  reasoning_effort,
                  thinking_enabled,
                  multi_agent_mode,
                  workspace_json,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, 'explicit_request_only'), ?, strftime('%s', 'now'), strftime('%s', 'now'))
                """,
                (
                    conversation_id,
                    title,
                    provider_id,
                    model_id,
                    reasoning_effort,
                    None if thinking_enabled is None else int(bool(thinking_enabled)),
                    multi_agent_mode,
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
        reasoning_effort: str | None = None,
        thinking_enabled: bool | None = None,
        multi_agent_mode: str | None = None,
    ) -> str:
        with self.persistence.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations (
                  id,
                  title,
                  provider_id,
                  model_id,
                  reasoning_effort,
                  thinking_enabled,
                  multi_agent_mode,
                  workspace_json,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, 'explicit_request_only'), ?, strftime('%s', 'now'), strftime('%s', 'now'))
                ON CONFLICT(id) DO UPDATE SET
                  title = COALESCE(NULLIF(excluded.title, ''), conversations.title),
                  provider_id = COALESCE(excluded.provider_id, conversations.provider_id),
                  model_id = COALESCE(excluded.model_id, conversations.model_id),
                  reasoning_effort = excluded.reasoning_effort,
                  thinking_enabled = excluded.thinking_enabled,
                  multi_agent_mode = COALESCE(excluded.multi_agent_mode, conversations.multi_agent_mode),
                  workspace_json = COALESCE(excluded.workspace_json, conversations.workspace_json),
                  updated_at = strftime('%s', 'now')
                """,
                (
                    conversation_id,
                    title,
                    provider_id,
                    model_id,
                    reasoning_effort,
                    None if thinking_enabled is None else int(bool(thinking_enabled)),
                    multi_agent_mode,
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

    def list_conversations(self) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  conversations.*,
                  COUNT(nodes.id) AS node_count
                FROM conversations
                LEFT JOIN nodes
                  ON nodes.conversation_id = conversations.id
                GROUP BY conversations.id
                ORDER BY conversations.updated_at DESC, conversations.created_at DESC
                """
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["workspace"] = self._json_object(item.pop("workspace_json", None))
            item.pop("settings_json", None)
            result.append(item)
        return result

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: Any = _UNSET,
        provider_id: Any = _UNSET,
        model_id: Any = _UNSET,
        reasoning_effort: Any = _UNSET,
        thinking_enabled: Any = _UNSET,
        multi_agent_mode: Any = _UNSET,
        workspace: Any = _UNSET,
        current_node_id: Any = _UNSET,
    ) -> bool:
        assignments = ["updated_at = strftime('%s', 'now')"]
        values: list[Any] = []
        fields = {
            "title": title,
            "provider_id": provider_id,
            "model_id": model_id,
            "reasoning_effort": reasoning_effort,
            "thinking_enabled": (
                _UNSET
                if thinking_enabled is _UNSET
                else None
                if thinking_enabled is None
                else int(bool(thinking_enabled))
            ),
            "multi_agent_mode": multi_agent_mode,
            "workspace_json": self._json_field(workspace) if workspace is not _UNSET else _UNSET,
            "current_node_id": current_node_id,
        }
        for column, value in fields.items():
            if value is _UNSET:
                continue
            assignments.append(f"{column} = ?")
            values.append(value)
        with self.persistence.connect() as conn:
            cursor = conn.execute(
                f"UPDATE conversations SET {', '.join(assignments)} WHERE id = ?",
                (*values, conversation_id),
            )
        return cursor.rowcount > 0

    def delete_conversation(self, conversation_id: str) -> bool:
        with self.persistence.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM conversations WHERE id = ?",
                (conversation_id,),
            )
        return cursor.rowcount > 0

    def create_node(
        self,
        conversation_id: str,
        parent_id: str | None,
        child_order: int = 0,
        node_id: str | None = None,
        focus: bool = True,
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
                updates = [
                    "updated_at = strftime('%s', 'now')",
                    "model_id = COALESCE(?, model_id)",
                    "provider_id = COALESCE(?, provider_id)",
                    "tool_permission_mode = COALESCE(?, tool_permission_mode)",
                    "task_context_mode = COALESCE(?, task_context_mode)",
                    "turn_usage_json = COALESCE(?, turn_usage_json)",
                    "branch_usage_json = COALESCE(?, branch_usage_json)",
                    "active_context_usage_json = COALESCE(?, active_context_usage_json)",
                ]
                conn.execute(
                    f"""
                    UPDATE nodes
                    SET {', '.join(updates)}
                    WHERE conversation_id = ? AND id = ?
                    """,
                    (
                        fields.pop("model_id", None),
                        fields.pop("provider_id", None),
                        fields.pop("tool_permission_mode", None),
                        fields.pop("task_context_mode", None),
                        self._json_field(fields.pop("turn_usage", None)),
                        self._json_field(fields.pop("branch_usage", None)),
                        self._json_field(fields.pop("active_context_usage", None)),
                        conversation_id,
                        node_id,
                    ),
                )
                if fields:
                    unknown = ", ".join(sorted(fields))
                    raise TypeError(f"Unsupported node fields: {unknown}")
                if focus:
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
                    SELECT depth, task_context_mode
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
            task_context_mode = fields.pop("task_context_mode", None)
            if task_context_mode is None:
                task_context_mode = (
                    str(parent["task_context_mode"] or "attached")
                    if parent is not None
                    else "attached"
                )
            if task_context_mode not in {"attached", "detached"}:
                raise ValueError("task_context_mode must be attached or detached")
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
                  task_context_mode,
                  turn_usage_json,
                  branch_usage_json,
                  active_context_usage_json,
                  created_at,
                  updated_at
                )
                VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
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
                    task_context_mode,
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
            elif focus:
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

    def list_nodes(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM nodes
                WHERE conversation_id = ?
                ORDER BY depth, child_order, created_at, id
                """,
                (conversation_id,),
            ).fetchall()
        nodes = [dict(row) for row in rows]
        children: dict[str, list[str]] = {str(node["id"]): [] for node in nodes}
        for node in nodes:
            parent_id = node.get("parent_id")
            if parent_id in children:
                children[str(parent_id)].append(str(node["id"]))
        for node in nodes:
            node["children_ids"] = children.get(str(node["id"]), [])
            node["parent_id"] = node.get("parent_id") or "None"
            node["timestamp"] = int(node.get("created_at") or 0)
            branch_usage = self._json_object(node.pop("branch_usage_json", None))
            turn_usage = self._json_object(node.pop("turn_usage_json", None))
            active_context_usage = self._json_object(node.pop("active_context_usage_json", None))
            node["branch_usage_info"] = branch_usage
            node["usage"] = {
                "turn_usage": turn_usage,
                "branch_usage": branch_usage,
                "active_context_usage": active_context_usage,
                "model_context_window": None,
            }
            node["total_tokens"] = int((branch_usage or {}).get("total_tokens") or 0)
        return nodes

    def ensure_node(
        self,
        conversation_id: str,
        node_id: str,
        parent_id: str | None,
        child_order: int = 0,
        focus: bool = True,
        **fields: Any,
    ) -> str:
        return self.create_node(
            conversation_id,
            parent_id,
            child_order=child_order,
            node_id=node_id,
            focus=focus,
            **fields,
        )

    def delete_node(
        self,
        conversation_id: str,
        node_id: str,
        *,
        new_current_node_id: str | None = None,
    ) -> bool:
        with self.persistence.connect() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM nodes
                WHERE conversation_id = ? AND id = ?
                """,
                (conversation_id, node_id),
            ).fetchone()
            if existing is None:
                return False
            conn.execute(
                """
                UPDATE conversations
                SET current_node_id = ?,
                    root_node_id = CASE
                      WHEN root_node_id = ? THEN NULL
                      ELSE root_node_id
                    END,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (new_current_node_id, node_id, conversation_id),
            )
            conn.execute(
                """
                DELETE FROM nodes
                WHERE conversation_id = ? AND id = ?
                """,
                (conversation_id, node_id),
            )
        return True

    def add_message(
        self,
        conversation_id: str,
        node_id: str | None,
        role: str,
        content: str,
        subtype: str | None = None,
        hidden: bool = False,
        transcript_only: bool = False,
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
                  transcript_only,
                  metadata_json,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(id) DO UPDATE SET
                  node_id = excluded.node_id,
                  role = excluded.role,
                  subtype = excluded.subtype,
                  content_inline = excluded.content_inline,
                  content_blob_id = excluded.content_blob_id,
                  preview = excluded.preview,
                  hidden = excluded.hidden,
                  transcript_only = excluded.transcript_only,
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
                    1 if transcript_only else 0,
                    self._json_field(metadata),
                ),
            )
        return message_id

    def add_tool_call(
        self,
        conversation_id: str,
        node_id: str | None,
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
            node_id = self._existing_id(conn, "nodes", conversation_id, node_id)
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
                  call_index = excluded.call_index,
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
        node_id: str | None,
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
        blob_id = self._existing_tool_result_blob_id(result_id, value)
        if blob_id is None and len(value) > len(preview):
            blob_id = BlobStore(self.persistence).put_text(value).blob_id
        with self.persistence.connect() as conn:
            node_id = self._existing_id(conn, "nodes", conversation_id, node_id)
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
                ON CONFLICT(conversation_id, tool_call_id) DO UPDATE SET
                  node_id = COALESCE(excluded.node_id, tool_results.node_id),
                  run_id = COALESCE(excluded.run_id, tool_results.run_id),
                  status = excluded.status,
                  output_preview = excluded.output_preview,
                  output_blob_id = excluded.output_blob_id,
                  output_size = excluded.output_size,
                  truncated = excluded.truncated,
                  metadata_json = excluded.metadata_json
                ON CONFLICT(conversation_id, id) DO UPDATE SET
                  node_id = COALESCE(excluded.node_id, tool_results.node_id),
                  run_id = COALESCE(excluded.run_id, tool_results.run_id),
                  tool_call_id = COALESCE(excluded.tool_call_id, tool_results.tool_call_id),
                  status = excluded.status,
                  output_preview = excluded.output_preview,
                  output_blob_id = excluded.output_blob_id,
                  output_size = excluded.output_size,
                  truncated = excluded.truncated,
                  metadata_json = excluded.metadata_json
                ON CONFLICT(id) DO UPDATE SET
                  node_id = COALESCE(excluded.node_id, tool_results.node_id),
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
            if tool_call_id:
                row = conn.execute(
                    """
                    SELECT id
                    FROM tool_results
                    WHERE conversation_id = ? AND tool_call_id = ?
                    """,
                    (conversation_id, tool_call_id),
                ).fetchone()
                if row is not None:
                    result_id = str(row["id"])
        return result_id

    def get_tool_result_slice(
        self,
        tool_result_id: str,
        *,
        offset: int = 0,
        limit: int = 16000,
    ) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                """
                SELECT
                  tool_results.id,
                  tool_results.output_preview,
                  tool_results.output_blob_id,
                  tool_results.output_size,
                  tool_results.metadata_json,
                  tool_calls.name AS tool_name
                FROM tool_results
                LEFT JOIN tool_calls
                  ON tool_calls.conversation_id = tool_results.conversation_id
                 AND tool_calls.id = tool_results.tool_call_id
                WHERE tool_results.id = ?
                """,
                (tool_result_id,),
            ).fetchone()
        if row is None:
            return None

        content = row["output_preview"] or ""
        if row["output_blob_id"]:
            content = BlobStore(self.persistence).get_text(str(row["output_blob_id"]))
        metadata = self._json_object(row["metadata_json"])
        offset = max(0, int(offset or 0))
        limit = max(1, int(limit or 16000))
        chunk = content[offset:offset + limit]
        next_offset = offset + len(chunk)
        total_chars = len(content)
        if not total_chars and row["output_size"]:
            total_chars = int(row["output_size"])
        return {
            "tool_result_id": tool_result_id,
            "tool_name": row["tool_name"] or metadata.get("tool_name"),
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset if next_offset < total_chars else None,
            "total_chars": total_chars,
            "has_more": next_offset < total_chars,
            "content": chunk,
        }

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

    def _json_object(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _existing_tool_result_blob_id(self, result_id: str, output: str) -> str | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                """
                SELECT output_preview, output_blob_id, output_size
                FROM tool_results
                WHERE id = ?
                """,
                (result_id,),
            ).fetchone()
        if row is None or int(row["output_size"] or 0) != len(output):
            return None
        try:
            existing = (
                BlobStore(self.persistence).get_text(str(row["output_blob_id"]))
                if row["output_blob_id"]
                else row["output_preview"] or ""
            )
        except KeyError:
            return None
        return str(row["output_blob_id"]) if existing == output and row["output_blob_id"] else None

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
