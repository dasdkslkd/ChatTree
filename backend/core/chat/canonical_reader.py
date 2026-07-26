"""Canonical 只读访问：从 SQLite 仓库读取消息/工具历史/compact 元数据等。

所有函数都是无状态的模块级函数，接受 ``chat_repository`` 作为第一个参数。
当 ``chat_repository`` 为 None 或 ``node_ids`` 为空时返回空容器。
"""
from __future__ import annotations

import json
import uuid
from time import time
from typing import Any, Dict, List, Optional, Tuple

from ..config.types import Message, Role
from ..model.usage import add_usage, estimated_usage
from ..persistence.blob_store import BlobStore
from .tool_result_format import format_persisted_tool_result

BLOCKING_PLAN_TOOLS = {"ask_user_question", "exit_plan_mode"}


def _tool_result_payload(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def has_blocking_plan_participation_result(messages: List[Dict[str, Any]]) -> bool:
    for message in messages:
        if str(message.get("name") or "") not in BLOCKING_PLAN_TOOLS:
            continue
        payload = _tool_result_payload(message.get("raw_content") or message.get("content"))
        if str(payload.get("status") or "") in {"awaiting_approval", "awaiting_question"}:
            return True
    return False


def usage_from_message(msg: Optional[Message]) -> Optional[Any]:
    if not msg:
        return None
    generation_info = msg.get("generation_info") or {}
    usage_info = generation_info.get("usage_info")
    if usage_info:
        return usage_info
    tokens = generation_info.get("tokens_used")
    if tokens:
        return estimated_usage(tokens)
    return None


def messages_by_node(
    chat_repository,
    conversation_id: str,
    node_ids: List[str],
) -> Dict[str, List[Message]]:
    if chat_repository is None or not node_ids:
        return {}
    placeholders = ",".join("?" for _ in node_ids)
    blobs = BlobStore(chat_repository.persistence)
    with chat_repository.persistence.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT rowid AS _rowid, *
            FROM messages
            WHERE conversation_id = ?
              AND node_id IN ({placeholders})
            ORDER BY node_id, created_at, rowid
            """,
            (conversation_id, *node_ids),
        ).fetchall()
    grouped: Dict[str, List[Message]] = {}
    for row in rows:
        content = row["content_inline"]
        if content is None and row["content_blob_id"]:
            content = blobs.get_text(str(row["content_blob_id"]))
        metadata = _tool_result_payload(row["metadata_json"] or "")
        message = Message({
            "id": str(row["id"]),
            "role": Role(str(row["role"])),
            "content": content or "",
            "timestamp": int(row["created_at"] or 0),
            "node_id": row["node_id"],
            "_rowid": int(row["_rowid"] or 0),
        })
        if row["subtype"]:
            message["subtype"] = str(row["subtype"])
        if row["hidden"]:
            message["is_hidden_from_transcript"] = True
        if row["transcript_only"]:
            message["is_visible_in_transcript_only"] = True
        message.update({
            key: value
            for key, value in metadata.items()
            if key not in {"id", "role", "content", "subtype", "timestamp", "node_id"}
        })
        grouped.setdefault(str(row["node_id"]), []).append(message)
    return grouped


def compact_metadata_by_node(
    chat_repository,
    conversation_id: str,
    node_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    messages = messages_by_node(chat_repository, conversation_id, node_ids)
    compact: Dict[str, Dict[str, Any]] = {}
    for node_id, node_messages in messages.items():
        for message in node_messages:
            if message.get("role") == Role.SYSTEM and message.get("subtype") == "compact_boundary":
                compact[node_id] = {
                    key: value
                    for key, value in dict(message).items()
                    if key not in {
                        "id",
                        "role",
                        "content",
                        "timestamp",
                        "node_id",
                        "subtype",
                        "is_hidden_from_transcript",
                        "is_visible_in_transcript_only",
                    }
                }
                break
    return compact


def prune_summaries_by_node(
    chat_repository,
    conversation_id: str,
    node_ids: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    messages = messages_by_node(chat_repository, conversation_id, node_ids)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for node_id, node_messages in messages.items():
        for message in node_messages:
            if message.get("subtype") != "prune_summary":
                continue
            record = {
                key: value
                for key, value in dict(message).items()
                if key not in {
                    "role",
                    "timestamp",
                    "node_id",
                    "is_hidden_from_transcript",
                    "is_visible_in_transcript_only",
                }
            }
            record["id"] = str(message.get("id") or record.get("id") or "")
            record["summary"] = str(message.get("content") or "")
            record["created_at"] = int(message.get("timestamp") or 0)
            record["type"] = "prune_summary"
            record["status"] = "completed"
            grouped.setdefault(node_id, []).append(record)
    for summaries in grouped.values():
        summaries.sort(
            key=lambda item: (
                int(item.get("created_at") or 0),
                int(item.get("_rowid") or 0),
            ),
            reverse=True,
        )
        for summary in summaries:
            summary.pop("_rowid", None)
    return grouped


def latest_assistant_answer(
    chat_repository,
    conversation_id: str,
    node_id: str,
) -> Optional[Message]:
    for message in reversed(messages_by_node(chat_repository, conversation_id, [node_id]).get(node_id, [])):
        if (
            message.get("role") == Role.ASSISTANT
            and message.get("subtype") in {None, "", "assistant_answer"}
        ):
            return message
    return None


def process_content_for_node(
    chat_repository,
    conversation_id: str,
    node_id: str,
    subtype: str,
) -> str:
    parts = [
        str(message.get("content") or "")
        for message in messages_by_node(chat_repository, conversation_id, [node_id]).get(node_id, [])
        if message.get("role") == Role.ASSISTANT and message.get("subtype") == subtype
    ]
    return "".join(parts)


def tool_history_by_node(
    chat_repository,
    conversation_id: str,
    node_ids: List[str],
) -> Dict[str, List[Tuple[Message, Message]]]:
    if chat_repository is None or not node_ids:
        return {}
    placeholders = ",".join("?" for _ in node_ids)
    blobs = BlobStore(chat_repository.persistence)
    with chat_repository.persistence.connect() as conn:
        call_rows = conn.execute(
            f"""
            SELECT *
            FROM tool_calls
            WHERE conversation_id = ?
              AND node_id IN ({placeholders})
            ORDER BY node_id, call_index, created_at, id
            """,
            (conversation_id, *node_ids),
        ).fetchall()
        result_rows = conn.execute(
            f"""
            SELECT *
            FROM tool_results
            WHERE conversation_id = ?
              AND node_id IN ({placeholders})
            ORDER BY created_at, id
            """,
            (conversation_id, *node_ids),
        ).fetchall()

    result_by_call_id = {
        str(row["tool_call_id"]): row
        for row in result_rows
        if row["tool_call_id"]
    }
    grouped: Dict[str, List[Tuple[Message, Message]]] = {}
    for row in call_rows:
        node_id = str(row["node_id"] or "")
        call_id = str(row["id"] or "")
        if not node_id or not call_id:
            continue
        arguments = row["args_inline"]
        if arguments is None and row["args_blob_id"]:
            arguments = blobs.get_text(str(row["args_blob_id"]))
        name = str(row["name"] or "")
        tool_call_message = Message({
            "role": Role.ASSISTANT,
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments or "",
                },
            }],
        })
        result = result_by_call_id.get(call_id)
        if result is None:
            content = f"Tool result missing for tool_call_id {call_id}."
            result_id = None
        else:
            result_id = str(result["id"] or "")
            raw_result = result["output_preview"]
            if result["output_blob_id"]:
                raw_result = blobs.get_text(str(result["output_blob_id"]))
            content = format_persisted_tool_result(
                raw_result=str(raw_result or ""),
                name=name,
                tool_result_id=result_id,
            )
        grouped.setdefault(node_id, []).append((
            tool_call_message,
            Message({
                "id": result_id or str(uuid.uuid4()),
                "role": Role.TOOL,
                "content": content,
                "model_visible_content": content,
                "name": name,
                "tool_call_id": call_id,
                "tool_result_id": result_id,
                "timestamp": int(time()),
            }),
        ))
    return grouped


def tool_context_by_node(
    chat_repository,
    conversation_id: str,
    node_ids: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    return {
        node_id: [dict(result_message) for _, result_message in pairs]
        for node_id, pairs in tool_history_by_node(chat_repository, conversation_id, node_ids).items()
    }


def turn_usage_for_node(chat_repository, conversation_id: str, node_id: str):
    messages = messages_by_node(chat_repository, conversation_id, [node_id]).get(node_id, [])
    usage = None
    for message in messages:
        if message.get("role") == Role.ASSISTANT and message.get("subtype") in {None, "", "assistant_answer"}:
            usage = add_usage(usage, usage_from_message(message))
    return usage
