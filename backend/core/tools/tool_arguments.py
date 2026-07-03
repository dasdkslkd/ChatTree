from __future__ import annotations

import json
from typing import Any, Dict


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
    _rename_first(normalized, "command", ("cmd", "script"))
    _rename_first(normalized, "query", ("q", "pattern"))
    _rename_first(normalized, "old_string", ("old", "oldString", "old_text"))
    _rename_first(normalized, "new_string", ("new", "newString", "new_text"))
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
    if name in {"read_file", "list_files"}:
        return {"path": raw}
    if name == "search_files":
        return {"query": raw}
    if name in {"run_command", "start_background_command", "start_command", "start_terminal"}:
        return {"command": raw}
    if name == "apply_patch":
        return {"patch": raw}
    return None


def _rename_first(target: Dict[str, Any], canonical: str, aliases: tuple[str, ...]) -> None:
    if canonical in target:
        return
    for alias in aliases:
        if alias in target:
            target[canonical] = target.pop(alias)
            return
