from __future__ import annotations

import json
import uuid
from contextlib import suppress
from typing import Any, Dict, List, Optional

from .blob_store import BlobStore
from .content import store_text_content
from .database import SQLitePersistence


_UNSET = object()


def _role_value(role: Any) -> str:
    return str(getattr(role, "value", role))


def tool_result_preview(output: str, limit: int = 4096) -> str:
    value = output or ""
    if len(value) <= limit:
        return value
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value[:limit]
    if not isinstance(payload, (dict, list)):
        return value[:limit]

    def compact(item: Any, string_limit: int, item_limit: int) -> Any:
        if isinstance(item, dict):
            compacted = {}
            for key, child in item.items():
                child_limit = item_limit
                if key == "files" and isinstance(child, list) and all(isinstance(entry, dict) for entry in child):
                    child_limit = len(child)
                compacted[key] = compact(child, string_limit, child_limit)
            return compacted
        if isinstance(item, list):
            return [compact(child, string_limit, item_limit) for child in item[:item_limit]]
        if isinstance(item, str) and len(item) > string_limit:
            return f"{item[:max(0, string_limit - 1)]}…"
        return item

    for string_limit, item_limit in ((1024, 20), (512, 10), (256, 5), (128, 3), (64, 2), (32, 1)):
        preview = json.dumps(
            compact(payload, string_limit, item_limit),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(preview) <= limit:
            return preview

    fallback = {"truncated": True, "preview": value[:limit // 2]}
    preview = json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
    while len(preview) > limit:
        fallback["preview"] = fallback["preview"][:-(len(preview) - limit)]
        preview = json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
    return preview


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
        deleted = cursor.rowcount > 0
        if deleted:
            # 仅回收孤立 Blob；全量 VACUUM 由显式手动 compact 流程触发（需独占锁且结束退出服务）。
            self.persistence.reclaim_blobs()
        return deleted

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
        self.persistence.reclaim_blobs()
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
        model_route_id: str | None = None,
        model_round_index: int | None = None,
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
                  model_route_id,
                  model_round_index,
                  metadata_json,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(id) DO UPDATE SET
                  node_id = excluded.node_id,
                  role = excluded.role,
                  subtype = excluded.subtype,
                  content_inline = excluded.content_inline,
                  content_blob_id = excluded.content_blob_id,
                  preview = excluded.preview,
                  hidden = excluded.hidden,
                  transcript_only = excluded.transcript_only,
                  model_route_id = excluded.model_route_id,
                  model_round_index = excluded.model_round_index,
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
                    stored.preview if stored.blob_id else "",
                    1 if hidden else 0,
                    1 if transcript_only else 0,
                    model_route_id,
                    model_round_index,
                    self._json_field(metadata),
                ),
            )
        return message_id

    def mark_assistant_answer_continued(
        self,
        conversation_id: str,
        message_id: str,
    ) -> None:
        with self.persistence.connect() as conn:
            conn.execute(
                """
                UPDATE messages
                SET subtype = 'assistant_continuation',
                    hidden = 1,
                    transcript_only = 1
                WHERE conversation_id = ?
                  AND id = ?
                  AND subtype = 'assistant_answer'
                """,
                (conversation_id, message_id),
            )

    def persist_model_state_items(
        self,
        conversation_id: str,
        assistant_message_id: str,
        *,
        output_items: List[Dict[str, Any]],
    ) -> None:
        """只保存适配器明确标出的、无法从语义事实重建的续接状态。"""
        prepared_items = []
        for position, item in enumerate(output_items):
            payload = item.get("state_payload")
            if not isinstance(payload, dict):
                continue
            prepared_items.append((
                position,
                item,
                store_text_content(
                    self.persistence,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            ))
        with self.persistence.connect() as conn:
            conn.execute(
                """
                DELETE FROM model_state_items
                WHERE conversation_id = ? AND assistant_message_id = ?
                """,
                (conversation_id, assistant_message_id),
            )
            for position, item, stored in prepared_items:
                conn.execute(
                    """
                    INSERT INTO model_state_items (
                      conversation_id,
                      assistant_message_id,
                      item_index,
                      kind,
                      payload_inline,
                      payload_blob_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        assistant_message_id,
                        int(item.get("index", position)),
                        str(item.get("kind") or "provider_state"),
                        stored.inline,
                        stored.blob_id,
                    ),
                )

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
                    stored.preview if stored.blob_id else "",
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
        preview = tool_result_preview(value, preview_limit)
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

    def ensure_branch(
        self,
        conversation: Any,
        node_id: str,
        *,
        provider_id: Optional[str],
        model_id: Optional[str],
        focus_node_id: Optional[str] = None,
    ) -> None:
        """确保 conversation + 节点链（从 root 到 node_id）都已落盘。"""
        metadata = conversation.metadata
        self.ensure_conversation(
            metadata["id"],
            title=str(metadata.get("title") or ""),
            provider_id=provider_id or metadata.get("provider_id"),
            model_id=model_id or metadata.get("model_id"),
            reasoning_effort=metadata.get("reasoning_effort"),
            thinking_enabled=metadata.get("thinking_enabled"),
            multi_agent_mode=metadata.get("multi_agent_mode"),
            workspace=metadata.get("workspace") if isinstance(metadata.get("workspace"), dict) else None,
        )
        chain = conversation.get_node_chain(node_id)
        for item in chain:
            parent_id = item.get("parent_id")
            if parent_id == "None":
                parent_id = None
            child_order = 0
            if parent_id and parent_id in conversation.nodes:
                siblings = conversation.nodes[parent_id].get("children_ids") or []
                with suppress(ValueError):
                    child_order = siblings.index(item["id"])
            self.ensure_node(
                metadata["id"],
                item["id"],
                parent_id,
                child_order=child_order,
                model_id=item.get("model_id") or model_id,
                provider_id=provider_id,
                tool_permission_mode=item.get("tool_permission_mode"),
                task_context_mode=item.get("task_context_mode") or "attached",
                turn_usage=(item.get("usage") or {}).get("turn_usage"),
                branch_usage=(item.get("usage") or {}).get("branch_usage") or item.get("branch_usage_info"),
                active_context_usage=(item.get("usage") or {}).get("active_context_usage"),
                focus=item["id"] == focus_node_id,
            )

    def save(self, conversation: Any) -> None:
        """保存 canonical conversation/node 结构。"""
        if conversation.current_node_id:
            self.ensure_branch(
                conversation,
                conversation.current_node_id,
                provider_id=conversation.metadata.get("provider_id"),
                model_id=conversation.metadata.get("model_id"),
                focus_node_id=conversation.current_node_id,
            )
            return
        metadata = conversation.metadata
        self.ensure_conversation(
            metadata["id"],
            title=str(metadata.get("title") or ""),
            provider_id=metadata.get("provider_id"),
            model_id=metadata.get("model_id"),
            reasoning_effort=metadata.get("reasoning_effort"),
            thinking_enabled=metadata.get("thinking_enabled"),
            multi_agent_mode=metadata.get("multi_agent_mode"),
            workspace=metadata.get("workspace") if isinstance(metadata.get("workspace"), dict) else None,
        )

    def persist_user_turn(
        self,
        conversation: Any,
        node: Dict[str, Any],
        user_msg: Dict[str, Any],
        *,
        provider_id: Optional[str],
        model_id: Optional[str],
        run_id: Optional[str],
    ) -> None:
        """持久化用户消息：先 ensure_branch，再 add_message。"""
        conversation_id = conversation.metadata["id"]
        node_id = node["id"]
        self.ensure_branch(
            conversation,
            node_id,
            provider_id=provider_id,
            model_id=model_id,
            focus_node_id=conversation.current_node_id,
        )
        self.add_message(
            conversation_id,
            node_id,
            role=_role_value(user_msg.get("role") or "user"),
            content=str(user_msg.get("content") or ""),
            subtype=user_msg.get("subtype"),
            hidden=bool(user_msg.get("is_hidden_from_transcript")),
            metadata={
                key: value
                for key, value in dict(user_msg).items()
                if key not in {"id", "role", "content", "subtype"}
            },
            message_id=user_msg.get("id"),
        )

    def persist_tool_metadata(
        self,
        conversation_id: str,
        node_id: str,
        *,
        run_id: Optional[str],
        assistant_message_id: Optional[str],
        tool_calls: List[Dict[str, Any]],
        tool_messages: List[Dict[str, Any]],
        approval_events: List[Dict[str, Any]],
        generation_status: str,
    ) -> None:
        """持久化工具调用与结果元数据。"""
        approval_status_by_call_id: Dict[str, str] = {}
        for event in approval_events:
            approval = event.get("approval") if isinstance(event, dict) else None
            if not isinstance(approval, dict):
                continue
            tool_call_id = str(approval.get("tool_call_id") or "")
            if not tool_call_id:
                continue
            if event.get("event_type") == "tool_approval_request":
                approval_status_by_call_id[tool_call_id] = "waiting_approval"
                continue
            status = str(approval.get("status") or "")
            if status == "approved":
                approval_status_by_call_id[tool_call_id] = "approved"
            elif status in {"denied", "expired", "cancelled", "rejected"}:
                approval_status_by_call_id[tool_call_id] = "rejected"
        result_call_ids = {
            str(message.get("tool_call_id") or "")
            for message in tool_messages
            if message.get("tool_call_id")
        }
        unresolved_status = "stopped" if generation_status == "stopped" else "error"
        for index, call in enumerate(tool_calls):
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            arguments = fn.get("arguments")
            call_id = str(call.get("id") or "")
            try:
                call_index = int(call.get("call_index") if call.get("call_index") is not None else index)
            except (TypeError, ValueError):
                call_index = index
            call_status = approval_status_by_call_id.get(call_id)
            if call_status is None:
                call_status = "complete" if call_id in result_call_ids else unresolved_status
            self.add_tool_call(
                conversation_id,
                node_id,
                tool_call_id=call_id or call.get("id"),
                name=name,
                arguments=arguments,
                call_index=call_index,
                status=call_status,
                run_id=run_id,
                assistant_message_id=assistant_message_id,
            )
        for message in tool_messages:
            raw_output = str(message.get("raw_content") or message.get("content") or "")
            tool_call_id = message.get("tool_call_id")
            if tool_call_id and not self.tool_call_exists(
                conversation_id,
                str(tool_call_id),
            ):
                self.add_tool_call(
                    conversation_id,
                    node_id,
                    tool_call_id=tool_call_id,
                    name=str(message.get("name") or ""),
                    arguments=None,
                    call_index=0,
                    status=approval_status_by_call_id.get(str(tool_call_id), "complete"),
                    run_id=run_id,
                    assistant_message_id=assistant_message_id,
                )
            result_id = message.get("tool_result_id")
            self.add_tool_result(
                conversation_id,
                node_id,
                tool_result_id=result_id,
                tool_call_id=tool_call_id,
                output=raw_output,
                status="complete",
                run_id=run_id,
                metadata={
                    "tool_name": message.get("name"),
                    "tool_result_id": message.get("tool_result_id"),
                },
            )

    def persist_assistant_turn(
        self,
        conversation: Any,
        node: Dict[str, Any],
        assistant_msg: Dict[str, Any],
        *,
        provider_id: Optional[str],
        model_id: Optional[str],
        run_id: Optional[str],
        tool_messages: List[Dict[str, Any]],
        rounds: List[Dict[str, Any]],
        model_route_id: str,
        approval_events: Optional[List[Dict[str, Any]]] = None,
        plan_participation_only: bool = False,
    ) -> None:
        """把每个模型轮次拆成语义消息、工具事实与最小续接状态。"""
        conversation_id = conversation.metadata["id"]
        node_id = node["id"]
        self.ensure_branch(
            conversation,
            node_id,
            provider_id=provider_id,
            model_id=model_id,
            focus_node_id=conversation.current_node_id,
        )
        content = str(assistant_msg.get("content") or "")
        base_message_id = str(assistant_msg.get("id") or run_id or uuid.uuid4())
        final_round = rounds[-1] if rounds and content and not plan_participation_only else None
        generation_info = assistant_msg.get("generation_info")
        generation_status = (
            str(generation_info.get("status") or "completed")
            if isinstance(generation_info, dict)
            else "completed"
        )
        remaining_tool_messages = list(tool_messages)

        for position, round_data in enumerate(rounds):
            round_index = int(round_data.get("round_index", position))
            is_final = round_data is final_round
            round_message_id = (
                base_message_id
                if is_final
                else f"{base_message_id}:round:{round_index}"
            )
            round_content = content if is_final else str(round_data.get("content") or "")
            metadata = {
                **(
                    {
                        key: value
                        for key, value in dict(assistant_msg).items()
                        if key not in {
                            "id",
                            "role",
                            "content",
                            "tool_calls",
                            "tool_results",
                            "approval_events",
                            "reasoning",
                        }
                    }
                    if is_final
                    else {}
                ),
                **({"run_id": run_id} if run_id else {}),
                **(
                    {"order": int(round_data.get("content_order"))}
                    if not is_final and round_data.get("content_order") is not None
                    else {}
                ),
            }
            self.add_message(
                conversation_id,
                node_id,
                role=_role_value(assistant_msg.get("role") or "assistant"),
                content=round_content,
                subtype="assistant_answer" if is_final else "assistant_round",
                hidden=not is_final,
                transcript_only=not is_final,
                model_route_id=model_route_id,
                model_round_index=round_index,
                metadata=metadata,
                message_id=round_message_id,
            )

            reasoning = str(round_data.get("reasoning") or "")
            if reasoning:
                self.add_message(
                    conversation_id,
                    node_id,
                    role="assistant",
                    content=reasoning,
                    subtype="assistant_process_reasoning",
                    hidden=True,
                    transcript_only=True,
                    model_route_id=model_route_id,
                    model_round_index=round_index,
                    metadata={
                        **({"run_id": run_id} if run_id else {}),
                        "order": int(
                            round_data.get("reasoning_order")
                            if round_data.get("reasoning_order") is not None
                            else round_index * 100
                        ),
                    },
                    message_id=f"{round_message_id}:reasoning",
                )

            round_calls = list(round_data.get("tool_calls") or [])
            call_ids = {
                str(call.get("id") or "")
                for call in round_calls
                if call.get("id")
            }
            round_tool_messages = [
                message
                for message in remaining_tool_messages
                if str(message.get("tool_call_id") or "") in call_ids
            ]
            remaining_tool_messages = [
                message
                for message in remaining_tool_messages
                if str(message.get("tool_call_id") or "") not in call_ids
            ]
            if round_calls or round_tool_messages:
                self.persist_tool_metadata(
                    conversation_id,
                    node_id,
                    run_id=run_id,
                    assistant_message_id=round_message_id,
                    tool_calls=round_calls,
                    tool_messages=round_tool_messages,
                    approval_events=list(approval_events or []),
                    generation_status=generation_status,
                )
            self.persist_model_state_items(
                conversation_id,
                round_message_id,
                output_items=list(round_data.get("output_items") or []),
            )

        if remaining_tool_messages:
            self.persist_tool_metadata(
                conversation_id,
                node_id,
                run_id=run_id,
                assistant_message_id=None,
                tool_calls=[],
                tool_messages=remaining_tool_messages,
                approval_events=list(approval_events or []),
                generation_status=generation_status,
            )

        if not rounds and content and not plan_participation_only:
            self.add_message(
                conversation_id,
                node_id,
                role=_role_value(assistant_msg.get("role") or "assistant"),
                content=content,
                subtype="assistant_answer",
                model_route_id=model_route_id,
                model_round_index=0,
                metadata={
                    **{
                        key: value
                        for key, value in dict(assistant_msg).items()
                        if key not in {
                            "id",
                            "role",
                            "content",
                            "tool_calls",
                            "tool_results",
                            "approval_events",
                            "reasoning",
                        }
                    },
                    **({"run_id": run_id} if run_id else {}),
                },
                message_id=base_message_id,
            )
