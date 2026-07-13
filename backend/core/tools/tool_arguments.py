from __future__ import annotations

import json
from typing import Any, Dict


COMMAND_ARGUMENT_TOOLS = {"shell"}


def normalize_tool_arguments(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize compact or alias-heavy tool arguments into canonical shapes."""
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

    normalized = dict(arguments)
    _rename_first(normalized, "path", ("file_path", "filepath", "file"))
    if tool_name.lower() in COMMAND_ARGUMENT_TOOLS:
        _rename_first(normalized, "command", ("cmd", "script"))
    else:
        _rename_first(normalized, "command", ("cmd",))
    if tool_name.lower() == "grep":
        _rename_first(normalized, "pattern", ("q", "query"))
    else:
        _rename_first(normalized, "query", ("q", "pattern"))
    return normalized


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
    if name in {"read", "glob"}:
        return {"path": raw}
    if name == "grep":
        return {"pattern": raw}
    if name == "shell":
        return {"command": raw}
    if name == "patch":
        return {"patch": raw}
    return None


def _rename_first(target: Dict[str, Any], canonical: str, aliases: tuple[str, ...]) -> None:
    if canonical in target:
        return
    for alias in aliases:
        if alias in target:
            target[canonical] = target.pop(alias)
            return
