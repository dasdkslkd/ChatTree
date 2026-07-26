"""工具结果格式化：将原始工具输出转换为模型可见的紧凑形式。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..config.config import cfg
from ..config.types import Message


def tool_result_preview_chars() -> int:
    tools_config = cfg.data.get("tools", {}) if isinstance(cfg.data, dict) else {}
    return int(tools_config.get("max_result_length", 8000))


def round_tool_result_budget_chars() -> int:
    tools_config = cfg.data.get("tools", {}) if isinstance(cfg.data, dict) else {}
    default_budget = tool_result_preview_chars() * 4
    return int(tools_config.get("max_round_result_length", default_budget))


def read_tool_result_hint(tool_result_id: str, offset: int = 0) -> str:
    args = json.dumps(
        {"source": "tool_result", "tool_result_id": tool_result_id, "offset": offset},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"read({args})"


def parse_command_tool_result(raw_result: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(raw_result)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    command_keys = {"command", "cwd", "exit_code", "stdout", "stderr", "timed_out"}
    if not command_keys.issubset(parsed.keys()):
        return None
    return parsed


def format_command_tool_result(*, raw_result: str, tool_result_id: str) -> str:
    parsed = parse_command_tool_result(raw_result)
    if parsed is None:
        return raw_result
    timed_out = parsed.get("timed_out")
    if isinstance(timed_out, bool):
        timed_out_text = str(timed_out).lower()
    else:
        timed_out_text = str(timed_out)
    stdout = str(parsed.get("stdout") or "")
    stderr = str(parsed.get("stderr") or "")
    return "\n".join(
        [
            f"Command: {parsed.get('command', '')}",
            f"Cwd: {parsed.get('cwd', '')}",
            f"Exit code: {parsed.get('exit_code', '')}",
            f"Timed out: {timed_out_text}",
            f"tool_result_id: {tool_result_id}",
            f"read_more: {read_tool_result_hint(tool_result_id, 0)}",
            "",
            "Stdout:",
            stdout if stdout else "(empty)",
            "",
            "Stderr:",
            stderr if stderr else "(empty)",
        ]
    )


def format_persisted_tool_result(
    *,
    raw_result: str,
    name: str,
    tool_result_id: str,
) -> str:
    command_result = parse_command_tool_result(raw_result)
    if command_result is not None:
        return format_command_tool_result(
            raw_result=raw_result,
            tool_result_id=tool_result_id,
        )

    preview_chars = tool_result_preview_chars()
    preview = raw_result[:preview_chars]
    has_more = len(raw_result) > len(preview)
    payload: Dict[str, Any] = {
        "tool_result_id": tool_result_id,
        "total_chars": len(raw_result),
        "truncated": has_more,
        "preview": preview,
    }
    if has_more:
        payload["read_more"] = read_tool_result_hint(tool_result_id, len(preview))
    return json.dumps(payload, ensure_ascii=False)


def persist_model_visible_tool_result(
    chat_repository,
    *,
    raw_result: str,
    name: str,
    conversation_id: str,
    node_id: str,
    tool_call_id: Optional[str],
) -> Dict[str, Optional[str]]:
    if chat_repository is None:
        return {"content": raw_result, "tool_result_id": None}

    if tool_call_id and not chat_repository.tool_call_exists(conversation_id, tool_call_id):
        chat_repository.add_tool_call(
            conversation_id,
            node_id,
            tool_call_id=tool_call_id,
            name=name,
            arguments=None,
            status="running",
        )
    tool_result_id = chat_repository.add_tool_result(
        conversation_id=conversation_id,
        node_id=node_id,
        tool_call_id=tool_call_id,
        output=raw_result,
        metadata={"tool_name": name},
    )
    return {
        "content": format_persisted_tool_result(
            raw_result=raw_result,
            name=name,
            tool_result_id=tool_result_id,
        ),
        "tool_result_id": tool_result_id,
    }


def build_model_visible_tool_result(
    chat_repository,
    *,
    raw_result: str,
    name: str,
    conversation_id: str,
    node_id: str,
    tool_call_id: Optional[str],
) -> str:
    return str(
        persist_model_visible_tool_result(
            chat_repository,
            raw_result=raw_result,
            name=name,
            conversation_id=conversation_id,
            node_id=node_id,
            tool_call_id=tool_call_id,
        )["content"]
    )


def summarize_persisted_tool_result(message: Message) -> str:
    tool_result_id = message.get("tool_result_id")
    if not tool_result_id:
        return str(message.get("content") or "")
    return "\n".join([
        f"persisted: {tool_result_id}",
        f"read_more: {read_tool_result_hint(str(tool_result_id), 0)}",
    ])


def apply_round_tool_result_budget(tool_messages: List[Message]) -> List[Message]:
    budget = round_tool_result_budget_chars()
    if budget <= 0:
        return [Message(dict(message)) for message in tool_messages]

    out = [Message(dict(message)) for message in tool_messages]

    def visible_len(message: Message) -> int:
        return len(str(message.get("model_visible_content") or message.get("content") or ""))

    total = sum(visible_len(message) for message in out)
    while total > budget:
        candidates = [
            (visible_len(message), index)
            for index, message in enumerate(out)
            if (
                message.get("tool_result_id")
                and not message.get("_round_budget_shortened")
                and len(summarize_persisted_tool_result(message)) < visible_len(message)
            )
        ]
        if not candidates:
            break
        _, index = max(candidates)
        if "raw_content" not in out[index]:
            out[index]["raw_content"] = str(
                out[index].get("model_visible_content") or out[index].get("content") or ""
            )
        shortened = summarize_persisted_tool_result(out[index])
        out[index]["content"] = shortened
        out[index]["model_visible_content"] = shortened
        out[index]["_round_budget_shortened"] = True
        new_total = sum(visible_len(message) for message in out)
        if new_total >= total:
            break
        total = new_total
    for message in out:
        message.pop("_round_budget_shortened", None)
    return out
