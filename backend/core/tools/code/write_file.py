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
            "directories. Read an existing file completely before overwriting it."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["create", "overwrite"]},
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
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = str(kwargs.get("mode") or "create")
        content = str(kwargs.get("content") or "")
        observations = common._file_observations(kwargs)
        key = str(target)
        with common._path_locks([target]):
            exists = target.exists()
            before = ""
            if mode == "create":
                try:
                    with target.open("x", encoding="utf-8") as handle:
                        handle.write(content)
                except FileExistsError:
                    return common._error("file_exists", "file already exists; read it completely before overwriting, or use exact replacement edit", path=self.workspace.relative(target))
            else:
                observed = observations.get(key)
                if (exists and (observed is None or patch._file_version(target) != observed)) or (not exists and observed is not None):
                    observations.pop(key, None)
                    return common._error("stale_file", "file changed or was not read completely; read it again before overwriting", path=self.workspace.relative(target))
                try:
                    before = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    return common._error("not_utf8", "file is not valid UTF-8 text", path=self.workspace.relative(target))
                target.write_text(content, encoding="utf-8")
            observations[key] = patch._file_version(target)
        return common._json({
            "path": self.workspace.relative(target),
            "bytes_written": len(content.encode("utf-8")),
            "mode": "overwrite" if exists else "create",
            "before_path": str(target),
            "before": before,
            "existed": exists,
        })
