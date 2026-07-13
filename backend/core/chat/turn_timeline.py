from __future__ import annotations

import json
from typing import Any, Iterable

BLOCKING_PLAN_TOOLS = {"plan", "exit_plan_mode", "ask_user_question"}


def tool_call_name(tool_call: dict[str, Any]) -> str:
    fn = tool_call.get("function") or {}
    return str(fn.get("name") or tool_call.get("name") or "")


def tool_call_id(tool_call: dict[str, Any]) -> str:
    return str(tool_call.get("id") or "")


def tool_message_name(message: dict[str, Any]) -> str:
    return str(message.get("name") or "")


def json_tool_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def has_tool_call_named(tool_calls: Iterable[dict[str, Any]], names: set[str]) -> bool:
    return any(tool_call_name(tool_call) in names for tool_call in tool_calls)


def has_blocking_plan_tool_call(tool_calls: Iterable[dict[str, Any]]) -> bool:
    return has_tool_call_named(tool_calls, BLOCKING_PLAN_TOOLS)


def is_blocking_plan_tool_result(message: dict[str, Any]) -> bool:
    name = tool_message_name(message)
    if name not in BLOCKING_PLAN_TOOLS:
        return False
    payload = json_tool_payload(message.get("raw_content") or message.get("content"))
    return str(payload.get("status") or "") in {"awaiting_approval", "awaiting_question"}


def has_blocking_plan_tool_result(messages: Iterable[dict[str, Any]]) -> bool:
    return any(is_blocking_plan_tool_result(message) for message in messages)


def should_emit_as_intermediate_text(*, has_tool_calls: bool, plan_guard_active: bool) -> bool:
    return has_tool_calls or plan_guard_active
