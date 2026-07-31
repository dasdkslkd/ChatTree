from __future__ import annotations

import asyncio
from typing import Any, Dict

from . import common, patch
from .apply_patch import ApplyPatchTool
from .write_file import WriteFileTool


class EditFileTool(common._CodeTool):
    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return (
            "Edit UTF-8 workspace files by exact replacements, create/overwrite content, or apply a unified patch. "
            "Create operations make missing parent directories inside the workspace. Read existing files first and "
            "pass expected_version for replacement or overwrite operations."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "operation": {"type": "string", "enum": ["replace", "create", "overwrite", "patch"]},
                "expected_version": {"type": "string"},
                "content": {"type": "string"},
                "patch": {"type": "string"},
                "cwd": {"type": "string"},
                "replacements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "old": {"type": "string"},
                            "new": {"type": "string"},
                            "replace_all": {"type": "boolean"},
                            "expected_count": {"type": "integer", "minimum": 1},
                        },
                        "required": ["old", "new"],
                    },
                },
            },
            "required": ["operation"],
        }

    async def execute(self, **kwargs) -> str:
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
        operation = str(kwargs.get("operation") or "").strip().lower()
        if not operation:
            return common._error("invalid_edit", "operation is required")
        if operation == "patch":
            return ApplyPatchTool(self.config)._execute_sync(kwargs)
        if operation in {"create", "overwrite"}:
            write_args = dict(kwargs)
            write_args["mode"] = operation
            return WriteFileTool(self.config)._execute_sync(write_args)
        if operation != "replace":
            return common._error("invalid_edit", "operation must be replace, create, overwrite, or patch")
        try:
            target = self.workspace.check_write(kwargs.get("path"))
        except common.CodeToolError as exc:
            return common._error(exc.error_type, str(exc), path=str(kwargs.get("path") or ""))
        if not target.exists() or not target.is_file():
            return common._error("not_found", "file not found", path=self.workspace.relative(target))
        expected_version = str(kwargs.get("expected_version") or "")
        current_version = patch._file_version(target)
        if not expected_version:
            return common._error("stale_file", "expected_version is required; read the file before editing", path=self.workspace.relative(target))
        if expected_version != current_version:
            return common._error("stale_file", "file changed since read; read again before editing", path=self.workspace.relative(target), current_version=current_version)
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return common._error("not_utf8", "file is not valid UTF-8 text", path=self.workspace.relative(target))
        replacements = kwargs.get("replacements")
        if not isinstance(replacements, list) or not replacements:
            return common._error("invalid_edit", "replacements must be a non-empty array", path=self.workspace.relative(target))
        updated = text
        applied = 0
        for index, replacement in enumerate(replacements):
            if not isinstance(replacement, dict):
                return common._error("invalid_edit", f"replacement {index} must be an object", path=self.workspace.relative(target))
            old_string = str(replacement.get("old") or "")
            new_string = str(replacement.get("new") or "")
            if not old_string:
                return common._error("invalid_edit", "old text is required", path=self.workspace.relative(target), index=index)
            if patch._looks_like_numbered_read_line(old_string):
                return common._error("invalid_edit", "old text includes read line-number prefixes; remove prefixes before editing", path=self.workspace.relative(target), index=index)
            occurrences = updated.count(old_string)
            expected_count = replacement.get("expected_count")
            if expected_count is not None and occurrences != int(expected_count):
                return common._error("edit_count_mismatch", "old text occurrence count did not match expected_count", path=self.workspace.relative(target), index=index, expected_count=int(expected_count), occurrences=occurrences)
            if occurrences == 0:
                return common._error("edit_not_found", "old text was not found", path=self.workspace.relative(target), index=index)
            replace_all = bool(replacement.get("replace_all", False))
            if occurrences > 1 and not replace_all:
                return common._error(
                    "edit_not_unique",
                    "old text occurs more than once; set replace_all=true or provide more context",
                    path=self.workspace.relative(target),
                    index=index,
                    occurrences=occurrences,
                )
            updated = updated.replace(old_string, new_string, -1 if replace_all else 1)
            applied += occurrences if replace_all else 1
        target.write_text(updated, encoding="utf-8")
        return common._json({
            "path": self.workspace.relative(target),
            "replacements": applied,
            "bytes_written": len(updated.encode("utf-8")),
            "version": patch._file_version(target),
        })
