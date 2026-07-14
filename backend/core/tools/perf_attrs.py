from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping


TEXT_PREVIEW_CHARS = 160


def summarize_tool_arguments(tool_name: str, arguments: Mapping[str, Any] | None) -> Dict[str, Any]:
    args = dict(arguments or {})
    attrs: Dict[str, Any] = {
        "tool_name": tool_name,
        "argument_keys": ",".join(sorted(str(key) for key in args.keys()))[:240],
    }

    for key in (
        "path",
        "output",
        "glob",
        "type",
        "context",
        "before_context",
        "after_context",
        "limit",
        "offset",
        "sort",
        "regex",
        "ignore_case",
        "respect_gitignore",
        "include_hidden",
        "multiline",
    ):
        if key in args:
            attrs[f"arg_{key}"] = _safe_scalar(args.get(key))

    pattern = args.get("pattern")
    if isinstance(pattern, str):
        attrs.update(_text_fingerprint("pattern", pattern))

    query = args.get("query")
    if isinstance(query, str):
        attrs.update(_text_fingerprint("query", query))

    return attrs


def summarize_tool_result(content: Any) -> Dict[str, Any]:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    attrs: Dict[str, Any] = {"result_chars": len(text)}
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return attrs
    if not isinstance(payload, dict):
        return attrs

    for key in (
        "engine",
        "output",
        "sort",
        "searched_files",
        "scanned_entries",
        "observed_count",
        "total",
        "total_known",
        "truncated",
        "fallback_reason",
        "next_offset",
    ):
        if key in payload:
            attrs[f"result_{key}"] = _safe_scalar(payload.get(key))

    matches = payload.get("matches")
    if isinstance(matches, list):
        attrs["result_entry_count"] = len(matches)
        attrs["result_match_count"] = sum(
            1 for item in matches
            if isinstance(item, dict) and item.get("type", "match") == "match"
        )

    files = payload.get("files")
    if isinstance(files, list):
        attrs["result_file_count"] = len(files)

    counts = payload.get("counts")
    if isinstance(counts, list):
        attrs["result_count_file_count"] = len(counts)
        attrs["result_match_count"] = sum(
            int(item.get("count") or 0)
            for item in counts
            if isinstance(item, dict)
        )

    count = payload.get("count")
    if isinstance(count, int):
        attrs["result_count"] = count

    skipped = payload.get("skipped_non_utf8")
    if isinstance(skipped, list):
        attrs["result_skipped_non_utf8_count"] = len(skipped)

    return attrs


def _text_fingerprint(prefix: str, text: str) -> Dict[str, Any]:
    return {
        f"{prefix}_preview": text[:TEXT_PREVIEW_CHARS],
        f"{prefix}_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        f"{prefix}_chars": len(text),
    }


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value[:TEXT_PREVIEW_CHARS]
    return json.dumps(value, ensure_ascii=False, default=str)[:TEXT_PREVIEW_CHARS]
