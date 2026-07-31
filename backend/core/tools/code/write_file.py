from __future__ import annotations

import asyncio
from typing import Any, Dict

from . import common, patch


class WriteFileTool(common._CodeTool):
    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "Create or intentionally overwrite a UTF-8 text file in the workspace, creating missing parent "
            "directories. Existing files require expected_version."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["create", "overwrite"]},
                "expected_version": {"type": "string"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, **kwargs) -> str:
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
        try:
            target = self.workspace.check_write(kwargs.get("path"))
        except common.CodeToolError as exc:
            return common._error(exc.error_type, str(exc), path=str(kwargs.get("path") or ""))
        if not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        mode = str(kwargs.get("mode") or "create")
        exists = target.exists()
        if mode == "create" and exists:
            return common._error("file_exists", "file already exists; use edit or overwrite with expected_version", path=self.workspace.relative(target), current_version=patch._file_version(target) if target.is_file() else None)
        if mode == "overwrite" and exists:
            expected_version = str(kwargs.get("expected_version") or "")
            current_version = patch._file_version(target)
            if not expected_version:
                return common._error("stale_file", "expected_version is required to overwrite an existing file", path=self.workspace.relative(target), current_version=current_version)
            if expected_version != current_version:
                return common._error("stale_file", "file changed since read; read again before overwriting", path=self.workspace.relative(target), current_version=current_version)
        content = str(kwargs.get("content") or "")
        target.write_text(content, encoding="utf-8")
        return common._json({"path": self.workspace.relative(target), "bytes_written": len(content.encode("utf-8")), "version": patch._file_version(target), "mode": "overwrite" if exists else "create"})
