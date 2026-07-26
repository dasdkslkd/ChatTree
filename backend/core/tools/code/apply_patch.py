from __future__ import annotations

import asyncio
from typing import Any, Dict

from . import common, patch


class ApplyPatchTool(common._CodeTool):
    @property
    def name(self) -> str:
        return "patch"

    @property
    def description(self) -> str:
        return (
            "Apply a unified diff patch to existing UTF-8 files in the code workspace. "
            "Prefer edit for small exact replacements; use patch for multi-line or multi-file changes."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "patch": {"type": "string"},
                "cwd": {"type": "string", "default": "."},
            },
            "required": ["patch"],
        }

    async def execute(self, **kwargs) -> str:
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
        patch_text = str(kwargs.get("patch") or "")
        if not patch_text.strip():
            return common._error("patch_failed", "patch is required")
        try:
            base = self.workspace.check_read(kwargs.get("cwd") or ".")
        except common.CodeToolError as exc:
            return common._error(exc.error_type, str(exc), path=str(kwargs.get("cwd") or "."))
        try:
            changed = patch._apply_simple_unified_patch(self.workspace, base, patch_text)
        except (common.CodeToolError, UnicodeDecodeError, ValueError) as exc:
            message = str(exc) or type(exc).__name__
            return common._error("patch_failed", message)
        return common._json({"applied": True, "files_changed": changed})
