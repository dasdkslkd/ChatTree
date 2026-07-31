from __future__ import annotations

import asyncio
from typing import Any, Dict

from . import common, patch
from ...persistence.repository import ChatRepository


class ReadFileTool(common._CodeTool):
    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "Read UTF-8 workspace files or a persisted tool-result slice. Choose exactly one input: path, "
            "non-empty targets, or tool_result_id. Use this instead of shell for cat/head/tail/type/Get-Content/sed. "
            "File reads return numbered lines by default."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path of one UTF-8 text file."},
                "tool_result_id": {
                    "type": "string",
                    "description": "Persisted tool result id. Do not combine with path or targets.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Zero-based character offset for tool_result_id reads.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum characters for tool_result_id reads.",
                },
                "targets": {
                    "type": "array",
                    "minItems": 1,
                    "description": "Batch file reads. Do not combine with path or tool_result_id.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer", "minimum": 1},
                            "line_count": {"type": "integer", "minimum": 1},
                            "max_chars": {"type": "integer", "minimum": 1},
                        },
                        "required": ["path"],
                    },
                },
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1},
                "max_chars_per_file": {"type": "integer", "minimum": 1},
                "format": {"type": "string", "enum": ["numbered", "raw", "json"]},
            },
        }

    async def execute(self, **kwargs) -> str:
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
        event_sink = common._tool_event_sink(kwargs)
        if kwargs.get("tool_result_id"):
            common._emit_tool_observation(
                event_sink,
                "tool_progress",
                status="running",
                progress={"phase": "read_tool_result", "tool_result_id": kwargs.get("tool_result_id")},
            )
            return self._read_tool_result(kwargs)
        targets = patch._read_targets(kwargs)
        if not targets:
            return common._error("invalid_path", "path or targets is required")
        output_format = str(kwargs.get("format") or "numbered")
        files: list[Dict[str, Any]] = []
        common._emit_tool_observation(
            event_sink,
            "tool_progress",
            status="running",
            progress={"phase": "prepare", "target_count": len(targets)},
        )
        for target_spec in targets:
            raw_path = target_spec["path"]
            try:
                target = self.workspace.check_read(raw_path)
            except common.CodeToolError as exc:
                files.append({"path": str(raw_path), "error": {"type": exc.error_type, "message": str(exc)}})
                continue
            start_line = max(1, int(target_spec.get("start_line") or kwargs.get("start_line") or 1))
            line_count = target_spec.get("line_count", kwargs.get("line_count"))
            requested_max_chars = target_spec.get("max_chars", kwargs.get("max_chars_per_file"))
            max_chars = max(
                1,
                min(int(requested_max_chars or self.config.max_read_chars), self.config.max_read_chars),
            )
            files.append(patch._read_payload(
                workspace=self.workspace,
                target=target,
                start_line=start_line,
                line_count=int(line_count) if line_count is not None else None,
                max_chars=max_chars,
                output_format=output_format,
            ))
            common._emit_tool_observation(
                event_sink,
                "tool_progress",
                status="running",
                progress={
                    "phase": "read_file",
                    "path": self.workspace.relative(target),
                    "completed_files": len(files),
                    "target_count": len(targets),
                },
            )
        common._emit_tool_observation(
            event_sink,
            "tool_progress",
            status="running",
            progress={"phase": "complete", "completed_files": len(files), "target_count": len(targets)},
        )
        if len(files) == 1:
            return common._json(files[0])
        return common._json({"files": files})

    def _read_tool_result(self, kwargs: Dict[str, Any]) -> str:
        repository = self._runtime_chat_repository(kwargs)
        if repository is None:
            return common._error("tool_result_unavailable", "canonical tool result repository is not configured")
        tool_result_id = str(kwargs.get("tool_result_id") or "").strip()
        if not tool_result_id:
            return common._error("invalid_path", "tool_result_id is required")
        offset = max(0, int(kwargs.get("offset") or 0))
        requested_limit = kwargs.get("limit") or kwargs.get("max_chars_per_file") or self.config.max_read_chars
        limit = max(1, min(int(requested_limit), self.config.max_read_chars))
        try:
            result = repository.get_tool_result_slice(tool_result_id, offset=offset, limit=limit)
        except KeyError:
            result = None
        if result is None:
            return common._error("not_found", "tool result not found", tool_result_id=tool_result_id)
        payload = {
            "source": "tool_result",
            "tool_result_id": tool_result_id,
            "offset": offset,
            "content": result.get("content", ""),
        }
        next_offset = result.get("next_offset")
        if next_offset is not None:
            payload["next_offset"] = next_offset
            payload["read_more"] = {
                "tool_result_id": tool_result_id,
                "offset": next_offset,
                "limit": limit,
            }
        return common._json(payload)

    def _runtime_chat_repository(self, kwargs: Dict[str, Any]) -> ChatRepository | None:
        context = kwargs.get("_runtime_context")
        if not isinstance(context, dict):
            return None
        repository = context.get("chat_repository")
        if isinstance(repository, ChatRepository):
            return repository
        persistence = context.get("persistence")
        if persistence is not None and hasattr(persistence, "connect"):
            return ChatRepository(persistence)
        return None
