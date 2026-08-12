from __future__ import annotations

from typing import Any, Dict

from ..config.config import cfg
from ..memory import MemoryStore
from .base import BaseTool


class MemoryTool(BaseTool):
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return (
            "Manage compact durable memory automatically across conversations. "
            "Use `user` for stable user preferences, `project` for stable facts of the current ChatTree project, "
            "and `machine` only for durable host constraints that live runtime detection cannot represent. "
            "Use `content` for add/replace and a short unique `old_text` match for replace/remove; "
            "never store secrets, task progress, or transient runtime state."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                "scope": {"type": "string", "enum": ["user", "project", "machine"]},
                "content": {"type": "string", "minLength": 1, "maxLength": 600},
                "old_text": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "required": ["action", "scope"],
        }

    async def execute(self, **kwargs: Any) -> str:
        memory_config = cfg.data.get("memory") if isinstance(cfg.data, dict) else None
        if isinstance(memory_config, dict) and memory_config.get("enabled") is False:
            return "error: disabled"
        runtime = kwargs.get("_runtime_context")
        runtime = runtime if isinstance(runtime, dict) else {}
        run_kind = str(runtime.get("run_kind") or "chat")
        if run_kind != "chat":
            return "error: unavailable"
        workspace = runtime.get("workspace")
        project_id = str(workspace.get("project_id") or "") if isinstance(workspace, dict) else ""
        return self._store.update(
            action=kwargs.get("action"),
            scope=kwargs.get("scope"),
            content=kwargs.get("content") or "",
            old_text=kwargs.get("old_text") or "",
            project_id=project_id,
        )
