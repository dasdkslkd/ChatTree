from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from . import common, python_fallback, ripgrep


def _glob_for_type(glob: str, type_name: str) -> str:
    if glob and glob != "*":
        return glob
    mapping = {
        "py": "*.py",
        "python": "*.py",
        "js": "*.js",
        "ts": "*.ts",
        "tsx": "*.tsx",
        "jsx": "*.jsx",
        "rust": "*.rs",
        "rs": "*.rs",
        "go": "*.go",
        "java": "*.java",
    }
    return mapping.get(type_name.lower(), glob or "*")


def _shape_grep_payload(payload: Dict[str, Any], *, output: str, limit: int, offset: int) -> Dict[str, Any]:
    shaped: Dict[str, Any] = {
        "pattern": payload.get("pattern"),
        "output": output,
        "engine": payload.get("engine"),
        "skipped_non_utf8": payload.get("skipped_non_utf8", []),
    }
    if payload.get("fallback_reason"):
        shaped["fallback_reason"] = payload.get("fallback_reason")
    if output == "files":
        files = list(payload.get("files") or [])
        page = files[offset:offset + limit]
        shaped.update({
            "files": page,
            "count": len(page),
            "truncated": bool(payload.get("truncated")) or offset + limit < len(files),
            "next_offset": offset + len(page) if (bool(payload.get("truncated")) or offset + limit < len(files)) else None,
        })
        return shaped
    if output == "count":
        counts = list(payload.get("counts") or [])
        page = counts[offset:offset + limit]
        shaped.update({
            "counts": page,
            "count": len(page),
            "truncated": bool(payload.get("truncated")) or offset + limit < len(counts),
            "next_offset": offset + len(page) if (bool(payload.get("truncated")) or offset + limit < len(counts)) else None,
        })
        return shaped
    matches = [
        {
            "path": match.get("path"),
            "line": match.get("line"),
            "text": match.get("preview", ""),
            "type": match.get("type", "match"),
        }
        for match in list(payload.get("matches") or [])[offset:offset + limit]
    ]
    shaped.update({
        "matches": matches,
        "count": len(matches),
        "truncated": bool(payload.get("truncated")) or offset + limit < len(payload.get("matches") or []),
        "next_offset": offset + len(matches) if (bool(payload.get("truncated")) or offset + limit < len(payload.get("matches") or [])) else None,
    })
    return shaped


class SearchFilesTool(common._CodeTool):
    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search UTF-8 workspace file contents with ripgrep-style regex. Use this to locate files and line numbers, "
            "not to read large known-file excerpts. When the file and target area are known, use a precise grep first "
            "and then read that file window. Prefer output=files for broad discovery; use output=content only for a "
            "specific pattern with a small limit/context. Use this instead of shell for grep/rg/Select-String."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex or fixed string to search for. Keep it specific; avoid broad alternations when read can fetch a known file window.",
                },
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file or directory to search. Use a single file path when it is known.",
                },
                "glob": {
                    "type": "string",
                    "description": "File glob within path. Narrow this for directory searches, for example *.py.",
                },
                "type": {"type": "string", "description": "Language shortcut such as py, ts, or rust when glob is not set."},
                "output": {
                    "type": "string",
                    "enum": ["content", "files", "count"],
                    "description": "Use files for broad discovery, content for a small precise match set, and count for statistics.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "offset": {"type": "integer", "minimum": 0},
                "regex": {"type": "boolean"},
                "ignore_case": {"type": "boolean"},
                "respect_gitignore": {"type": "boolean"},
                "include_hidden": {"type": "boolean"},
                "multiline": {"type": "boolean"},
                "context": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Context lines around each match. Keep this small; for larger excerpts use read after finding the line.",
                },
                "before_context": {"type": "integer", "minimum": 0},
                "after_context": {"type": "integer", "minimum": 0},
                "exclude": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["pattern"],
        }

    async def execute(self, **kwargs) -> str:
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
        event_sink = common._tool_event_sink(kwargs)
        pattern = str(kwargs.get("pattern") or "")
        if not pattern:
            return common._error("invalid_query", "pattern is required")
        try:
            root = self.workspace.check_read(kwargs.get("path") or ".")
        except common.CodeToolError as exc:
            return common._error(exc.error_type, str(exc), path=str(kwargs.get("path") or "."))

        glob = common._normalize_glob_pattern(_glob_for_type(str(kwargs.get("glob") or "*"), str(kwargs.get("type") or "")))
        limit = max(1, min(int(kwargs.get("limit") or kwargs.get("head_limit") or 250), 500))
        offset = max(0, int(kwargs.get("offset") or 0))
        fixed_strings = not bool(kwargs.get("regex", True))
        ignore_case = bool(kwargs.get("ignore_case", False))
        no_ignore = not bool(kwargs.get("respect_gitignore", True))
        hidden = bool(kwargs.get("include_hidden", False))
        multiline = bool(kwargs.get("multiline", False))
        output = str(kwargs.get("output") or kwargs.get("output_mode") or "files")
        files_with_matches = output == "files"
        count_mode = output == "count"
        context = max(0, int(kwargs.get("context") or 0))
        before_context = max(context, int(kwargs.get("before_context") or 0))
        after_context = max(context, int(kwargs.get("after_context") or 0))
        exclude_globs = common._normalize_glob_patterns(common._string_list(kwargs.get("exclude")))

        common._emit_tool_observation(
            event_sink,
            "tool_progress",
            status="running",
            progress={
                "phase": "prepare",
                "path": self.workspace.relative(root),
                "pattern": pattern[:200],
                "glob": glob,
                "output": output,
            },
        )
        fallback_reason: Optional[str] = "ripgrep_not_installed"
        rg_path = ripgrep._resolve_ripgrep_executable()
        if rg_path is not None:
            rg_payload, fallback_reason = ripgrep._grep_with_rg(
                rg_path=rg_path,
                workspace=self.workspace,
                root=root,
                pattern=pattern,
                glob=glob,
                max_results=limit + offset,
                fixed_strings=fixed_strings,
                ignore_case=ignore_case,
                no_ignore=no_ignore,
                hidden=hidden,
                before_context=before_context,
                after_context=after_context,
                files_with_matches=files_with_matches,
                count_mode=count_mode,
                multiline=multiline,
                exclude_globs=exclude_globs,
                timeout_seconds=self.config.command_timeout_seconds,
                event_sink=event_sink,
            )
            if rg_payload is not None:
                common._emit_tool_observation(
                    event_sink,
                    "tool_progress",
                    status="running",
                    progress={
                        "phase": "complete",
                        "engine": "rg",
                        "searched_files": rg_payload.get("searched_files"),
                        "matched_files": len(rg_payload.get("files") or []),
                        "matches": len(rg_payload.get("matches") or []),
                    },
                )
                return common._json(_shape_grep_payload(rg_payload, output=output, limit=limit, offset=offset))
            if fallback_reason and fallback_reason.startswith("ripgrep_invalid_regex:"):
                return common._error("invalid_query", fallback_reason.split(":", 1)[1])
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
        payload = python_fallback._grep_files_python(
            workspace=self.workspace,
            root=root,
            pattern=pattern,
            glob=glob,
            limit=limit,
            offset=offset,
            fixed_strings=fixed_strings,
            ignore_case=ignore_case,
            multiline=multiline,
            no_ignore=no_ignore,
            hidden=hidden,
            before_context=before_context,
            after_context=after_context,
            output=output,
            exclude_globs=exclude_globs,
            event_sink=event_sink,
        )
        if fallback_reason and "error" not in payload:
            payload["fallback_reason"] = fallback_reason
        common._emit_tool_observation(
            event_sink,
            "tool_progress",
            status="running",
            progress={
                "phase": "complete",
                "engine": "python",
                "searched_files": payload.get("searched_files"),
                "matches": len(payload.get("matches") or []),
            },
        )
        return common._json(payload)
