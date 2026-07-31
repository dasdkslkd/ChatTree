from __future__ import annotations

import json
from typing import Any, Dict


COMMAND_ARGUMENT_TOOLS = {"shell"}


def normalize_tool_arguments(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize compact single-argument tool calls into canonical shapes."""
    if not isinstance(arguments, dict):
        return {}

    raw_single_argument = _single_raw_argument(arguments)
    if raw_single_argument is not None:
        parsed = _parse_json_object(raw_single_argument)
        if parsed is not None:
            return normalize_tool_arguments(tool_name, parsed)
        compact = _compact_argument_for_tool(tool_name, raw_single_argument)
        if compact is not None:
            return compact

    return dict(arguments)


def _single_raw_argument(arguments: Dict[str, Any]) -> str | None:
    if set(arguments.keys()) != {"arguments"}:
        return None
    value = arguments.get("arguments")
    return value if isinstance(value, str) and value.strip() else None


def _parse_json_object(raw: str) -> Dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _compact_argument_for_tool(tool_name: str, raw: str) -> Dict[str, Any] | None:
    name = tool_name.lower()
    if name == "read":
        return {"path": raw}
    if name == "glob":
        return {"pattern": raw} if _looks_like_glob_pattern(raw) else {"path": raw}
    if name == "grep":
        return {"pattern": raw}
    if name == "shell":
        return {"command": raw}
    if name == "edit":
        return {"operation": "patch", "patch": raw} if raw.lstrip().startswith(("--- ", "diff ")) else None
    if name == "web":
        return {"action": "search", "query": raw}
    return None


def _looks_like_glob_pattern(raw: str) -> bool:
    normalized = raw.replace("\\", "/")
    return any(marker in normalized for marker in ("*", "?", "["))
