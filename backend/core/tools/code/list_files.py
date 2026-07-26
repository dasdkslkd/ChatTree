from __future__ import annotations

import asyncio
import re
from typing import Any, Dict

from . import common, python_fallback, ripgrep


class ListFilesTool(common._CodeTool):
    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Find workspace files with ripgrep-style file listing. Default sort=discovery is optimized for fast paged discovery "
            "and may not know the exact total; continue with next_offset when needed and trust total only when total_known is true. "
            "Use sort=path only when deterministic path order matters, and sort=mtime only for recently modified files because it requires a full scan. "
            "Use `pattern` for one glob, `patterns` for multiple globs, and `path_regex` to match returned paths; do not use a `query` argument. "
            "Use this instead of shell for ls/dir/find/Get-ChildItem/rg --files. Paths are returned relative to the workspace root with / separators."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "default": "."},
                "patterns": {"type": "array", "items": {"type": "string"}, "default": ["**/*"]},
                "pattern": {"type": "string"},
                "path_regex": {"type": "string"},
                "files_only": {"type": "boolean", "default": True},
                "include_hidden": {"type": "boolean", "default": False},
                "respect_gitignore": {"type": "boolean", "default": True},
                "exclude": {"type": "array", "items": {"type": "string"}, "default": []},
                "sort": {
                    "type": "string",
                    "enum": ["discovery", "path", "mtime"],
                    "default": "discovery",
                    "description": "discovery is fastest and supports early pagination; path is deterministic; mtime requires a full scan.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
        }

    async def execute(self, **kwargs) -> str:
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
        event_sink = common._tool_event_sink(kwargs)
        try:
            root = self.workspace.check_read(kwargs.get("path") or ".")
        except common.CodeToolError as exc:
            return common._error(exc.error_type, str(exc), path=str(kwargs.get("path") or "."))

        patterns = common._normalize_glob_patterns(common._string_list(kwargs.get("patterns")))
        single_pattern = str(kwargs.get("pattern") or "").strip()
        if single_pattern:
            patterns = [common._normalize_glob_pattern(single_pattern)]
        if not patterns:
            patterns = ["**/*"]
        path_regex = str(kwargs.get("path_regex") or "").strip()
        try:
            compiled_path_regex = re.compile(path_regex) if path_regex else None
        except re.error as exc:
            return common._error("invalid_query", f"invalid path_regex: {exc}")
        files_only = bool(kwargs.get("files_only", True))
        include_hidden = bool(kwargs.get("include_hidden", False))
        respect_gitignore = bool(kwargs.get("respect_gitignore", True))
        exclude_globs = common._normalize_glob_patterns(common._string_list(kwargs.get("exclude")))
        sort = str(kwargs.get("sort") or "discovery")
        if sort not in {"discovery", "path", "mtime"}:
            return common._error("invalid_query", "sort must be one of discovery, path, or mtime")
        limit = max(1, min(int(kwargs.get("limit") or 200), 2000))
        offset = max(0, int(kwargs.get("offset") or 0))

        common._emit_tool_observation(
            event_sink,
            "tool_progress",
            status="running",
            progress={
                "phase": "prepare",
                "path": self.workspace.relative(root),
                "patterns": patterns,
                "files_only": files_only,
                "respect_gitignore": respect_gitignore,
                "sort": sort,
            },
        )
        rg_path = ripgrep._resolve_ripgrep_executable(self.config)
        fallback_reason = "ripgrep_not_installed" if rg_path is None else None
        if rg_path is not None:
            rg_result, fallback_reason = ripgrep._glob_files_with_rg(
                rg_path=rg_path,
                workspace=self.workspace,
                root=root,
                patterns=patterns,
                path_regex=compiled_path_regex,
                files_only=files_only,
                respect_gitignore=respect_gitignore,
                include_hidden=include_hidden,
                exclude_globs=exclude_globs,
                sort=sort,
                limit=limit,
                offset=offset,
                timeout_seconds=self.config.command_timeout_seconds,
                event_sink=event_sink,
            )
            if rg_result is not None:
                return common._json(rg_result)
            if fallback_reason != "ripgrep_not_installed":
                common._emit_tool_observation(
                    event_sink,
                    "tool_progress",
                    status="running",
                    progress={"phase": "rg_failed", "engine": "rg", "reason": fallback_reason},
                )

        common._emit_tool_observation(
            event_sink,
            "tool_progress",
            status="running",
            progress={"phase": "python_fallback", "reason": fallback_reason},
        )
        payload = python_fallback._glob_files_python(
            workspace=self.workspace,
            root=root,
            patterns=patterns,
            path_regex=compiled_path_regex,
            respect_gitignore=respect_gitignore,
            include_hidden=include_hidden,
            files_only=files_only,
            exclude_globs=exclude_globs,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        if fallback_reason:
            payload["fallback_reason"] = fallback_reason
        common._emit_tool_observation(
            event_sink,
            "tool_progress",
            status="running",
            progress={
                "phase": "complete",
                "engine": "python",
                "scanned_entries": payload.get("scanned_entries"),
                "matched_entries": payload.get("observed_count"),
                "truncated": payload.get("truncated"),
            },
        )
        return common._json(payload)
