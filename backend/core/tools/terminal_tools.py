from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from .base import BaseTool
from .code_tools import CodeToolConfig, CodeWorkspace, CodeToolError


def _runtime_context(kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = kwargs.get("_runtime_context")
    return value if isinstance(value, dict) else None


def _terminal_executor(kwargs: Dict[str, Any]) -> Any:
    context = _runtime_context(kwargs)
    if not context:
        return None
    return context.get("terminal_executor")


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _missing_executor() -> str:
    return _json({"error": {"type": "missing_terminal_executor", "message": "Terminal executor is not available"}})


class WaitTerminalTool(BaseTool):
    @property
    def name(self) -> str:
        return "wait_terminal"

    @property
    def description(self) -> str:
        return "Wait for a managed background terminal run to finish and return its status and output tails."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "terminal_run_id": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600, "default": 30},
            },
            "required": ["terminal_run_id"],
        }

    async def execute(self, **kwargs) -> str:
        executor = _terminal_executor(kwargs)
        if executor is None:
            return _missing_executor()
        run_id = str(kwargs.get("terminal_run_id") or "")
        if not run_id:
            return _json({"error": {"type": "invalid_arguments", "message": "terminal_run_id is required"}})
        timeout = max(1, min(int(kwargs.get("timeout_seconds") or 30), 3600))
        try:
            await executor.wait(run_id, timeout=timeout)
        except asyncio.TimeoutError:
            snapshot = executor.snapshot(run_id) or {"terminal_run_id": run_id}
            snapshot["wait_timed_out"] = True
            return _json(snapshot)
        snapshot = executor.snapshot(run_id)
        if snapshot is None:
            return _json({"error": {"type": "not_found", "message": "terminal run not found", "terminal_run_id": run_id}})
        snapshot["wait_timed_out"] = False
        return _json(snapshot)


class StartTerminalTool(BaseTool):
    def __init__(self, config: CodeToolConfig):
        self.workspace = CodeWorkspace(config)
        self.config = config

    @property
    def name(self) -> str:
        return "start_terminal"

    @property
    def description(self) -> str:
        return "Start a managed background terminal command in the code workspace and return a terminal_run_id immediately."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "default": "."},
                "timeout_seconds": {"type": "integer", "minimum": 1},
            },
            "required": ["command"],
        }

    async def execute(self, **kwargs) -> str:
        command = str(kwargs.get("command") or "")
        if not command.strip():
            return _json({"error": {"type": "invalid_arguments", "message": "command is required"}})
        executor = _terminal_executor(kwargs)
        if executor is None:
            return _missing_executor()
        try:
            cwd = self.workspace.check_read(kwargs.get("cwd") or ".")
        except CodeToolError as exc:
            return _json({"error": {"type": exc.error_type, "message": str(exc), "path": str(kwargs.get("cwd") or ".")}})
        timeout = max(
            1,
            min(
                int(kwargs.get("timeout_seconds") or self.config.command_timeout_seconds),
                self.config.command_timeout_seconds,
            ),
        )
        context = _runtime_context(kwargs) or {}
        run = await executor.start(
            conversation_id=str(context.get("conversation_id") or ""),
            command=command,
            cwd=str(cwd),
            anchor_node_id=str(context.get("node_id") or "") or None,
            parent_run_id=str(context.get("run_id") or "") or None,
            summary=command[:80],
            timeout_seconds=timeout,
            metadata={
                "tool_name": self.name,
                "tool_call_id": context.get("tool_call_id"),
                "workspace_relative_cwd": self.workspace.relative(cwd),
            },
        )
        return _json({
            "command": command,
            "cwd": self.workspace.relative(cwd),
            "status": "running",
            "kind": "terminal",
            "terminal_run_id": run["run_id"],
            "run_id": run["run_id"],
            "message": "Terminal command is running in the background. Use wait_terminal, read_terminal, or stop_terminal with this terminal_run_id.",
        })


class ReadTerminalTool(BaseTool):
    @property
    def name(self) -> str:
        return "read_terminal"

    @property
    def description(self) -> str:
        return "Read stdout, stderr, status, and metadata for a managed terminal run."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "terminal_run_id": {"type": "string"},
            },
            "required": ["terminal_run_id"],
        }

    async def execute(self, **kwargs) -> str:
        executor = _terminal_executor(kwargs)
        if executor is None:
            return _missing_executor()
        run_id = str(kwargs.get("terminal_run_id") or "")
        if not run_id:
            return _json({"error": {"type": "invalid_arguments", "message": "terminal_run_id is required"}})
        snapshot = executor.snapshot(run_id)
        if snapshot is None:
            return _json({"error": {"type": "not_found", "message": "terminal run not found", "terminal_run_id": run_id}})
        return _json(snapshot)


class StopTerminalTool(BaseTool):
    @property
    def name(self) -> str:
        return "stop_terminal"

    @property
    def description(self) -> str:
        return "Stop a running managed background terminal run."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "terminal_run_id": {"type": "string"},
            },
            "required": ["terminal_run_id"],
        }

    async def execute(self, **kwargs) -> str:
        executor = _terminal_executor(kwargs)
        if executor is None:
            return _missing_executor()
        run_id = str(kwargs.get("terminal_run_id") or "")
        if not run_id:
            return _json({"error": {"type": "invalid_arguments", "message": "terminal_run_id is required"}})
        stopped = await executor.stop(run_id)
        snapshot = executor.snapshot(run_id) or {"terminal_run_id": run_id}
        snapshot["stopped"] = bool(stopped)
        return _json(snapshot)
