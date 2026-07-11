from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from .base import BaseTool
from .code_tools import CodeToolConfig, CodeToolError, CodeWorkspace
from .task_contract import task_step_parameter_schema
from ..runs.types import FINISHED_RUN_STATUSES
from ..shell_profile import ShellProfileResolver, render_command_tool_guidance


FINISHED_STATUS_VALUES = {status.value for status in FINISHED_RUN_STATUSES}


def _runtime_context(kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = kwargs.get("_runtime_context")
    return value if isinstance(value, dict) else None


def _command_executor(kwargs: Dict[str, Any]) -> Any:
    context = _runtime_context(kwargs)
    if not context:
        return None
    return context.get("command_executor")


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _missing_executor() -> str:
    return _json({"error": {"type": "missing_command_executor", "message": "Command executor is not available"}})


def _command_run_id(kwargs: Dict[str, Any]) -> str:
    return str(kwargs.get("command_run_id") or "")


def _should_suppress_task_notification(context: Dict[str, Any]) -> bool:
    if context.get("suppress_task_notification") is True:
        return True
    if context.get("agent_name") == "workflow-worker":
        return True
    if context.get("delivery_policy") == "silent":
        return True
    return context.get("run_kind") in {"workflow", "workflow_step"}


class StartBackgroundCommandTool(BaseTool):
    def __init__(self, config: CodeToolConfig):
        self.workspace = CodeWorkspace(config)
        self.config = config

    @property
    def name(self) -> str:
        return "start_background_command"

    @property
    def description(self) -> str:
        profile = ShellProfileResolver().resolve()
        return (
            "Start a true background command in the code workspace and return a command_run_id immediately. "
            "Use command tools for runtime work such as tests, builds, scripts, servers, git, or package-manager commands. "
            "Do not use command tools for ordinary file listing, file reading, or text search; use list_files, read_file, and search_files instead. "
            "Use run_command for foreground command execution; it auto-backgrounds commands that keep running past the initial wait window. "
            "Do not report completion, exit code, or output from this launch response; consume a terminal read_command or wait_command result first.\n\n"
            f"{render_command_tool_guidance(profile)}"
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "default": "."},
                "timeout_seconds": {"type": "integer", "minimum": 1},
                "step": task_step_parameter_schema(),
            },
            "required": ["command"],
        }

    async def execute(self, **kwargs) -> str:
        command = str(kwargs.get("command") or "")
        if not command.strip():
            return _json({"error": {"type": "invalid_arguments", "message": "command is required"}})
        executor = _command_executor(kwargs)
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
            anchor_node_id=str(context.get("anchor_node_id") or context.get("node_id") or "") or None,
            created_by_run_id=str(context.get("run_id") or "") or None,
            cancellation_parent_run_id=None,
            summary=command[:80],
            timeout_seconds=timeout,
            step=kwargs.get("step"),
            task_context_mode=str(context.get("task_context_mode") or "attached"),
            task_generation_id=str(context.get("task_generation_id") or "") or None,
            task_revision=(
                int(context["task_revision"])
                if context.get("task_revision") is not None
                else None
            ),
            metadata={
                "tool_name": self.name,
                "tool_call_id": context.get("tool_call_id"),
                "workspace_relative_cwd": self.workspace.relative(cwd),
                "agent_name": context.get("agent_name"),
                "source_run_id": context.get("run_id"),
                "source_run_kind": context.get("run_kind"),
                "root_run_id": context.get("root_run_id"),
                "suppress_task_notification": _should_suppress_task_notification(context),
            },
        )
        snapshot = executor.snapshot(str(run["run_id"])) or {}
        return _json({
            "command": command,
            "cwd": self.workspace.relative(cwd),
            "status": "running",
            "kind": "command",
            "command_run_id": run["run_id"],
            "run_id": run["run_id"],
            "result_observed": False,
            "shell": snapshot.get("shell") or run.get("metadata", {}).get("shell"),
            "message": (
                "Command launch is confirmed, but this response does not contain the command result. "
                "Do not report completion, exit code, or output from it. "
                "Use read_command to inspect it, wait_command only when this answer must join the result, "
                "or stop_command to cancel it."
            ),
        })


class WaitCommandTool(BaseTool):
    @property
    def name(self) -> str:
        return "wait_command"

    @property
    def description(self) -> str:
        return (
            "Join a managed background command and return its final status and output. "
            "Use this only when the current answer must consume the command result; final results are marked observed."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command_run_id": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600, "default": 30},
            },
            "required": ["command_run_id"],
        }

    async def execute(self, **kwargs) -> str:
        executor = _command_executor(kwargs)
        if executor is None:
            return _missing_executor()
        run_id = _command_run_id(kwargs)
        if not run_id:
            return _json({"error": {"type": "invalid_arguments", "message": "command_run_id is required"}})
        timeout = max(1, min(int(kwargs.get("timeout_seconds") or 30), 3600))
        try:
            await executor.wait(run_id, timeout=timeout)
        except asyncio.TimeoutError:
            snapshot = executor.snapshot(run_id) or {"command_run_id": run_id}
            snapshot["wait_timed_out"] = True
            return _json(snapshot)
        snapshot = executor.snapshot(run_id)
        if snapshot is None:
            return _json({"error": {"type": "not_found", "message": "command run not found", "command_run_id": run_id}})
        snapshot["wait_timed_out"] = False
        context = _runtime_context(kwargs) or {}
        if snapshot.get("status") in FINISHED_STATUS_VALUES and hasattr(executor, "mark_observed"):
            await executor.mark_observed(
                run_id,
                observer_run_id=str(context.get("run_id") or "") or None,
                via=self.name,
            )
            snapshot = executor.snapshot(run_id) or snapshot
            snapshot["wait_timed_out"] = False
        return _json(snapshot)


class ReadCommandTool(BaseTool):
    @property
    def name(self) -> str:
        return "read_command"

    @property
    def description(self) -> str:
        return (
            "Read stdout, stderr, status, shell metadata, and run metadata for a managed command without blocking. "
            "A final read marks the command result observed and suppresses later task notification."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command_run_id": {"type": "string"},
            },
            "required": ["command_run_id"],
        }

    async def execute(self, **kwargs) -> str:
        executor = _command_executor(kwargs)
        if executor is None:
            return _missing_executor()
        run_id = _command_run_id(kwargs)
        if not run_id:
            return _json({"error": {"type": "invalid_arguments", "message": "command_run_id is required"}})
        snapshot = executor.snapshot(run_id)
        if snapshot is None:
            return _json({"error": {"type": "not_found", "message": "command run not found", "command_run_id": run_id}})
        context = _runtime_context(kwargs) or {}
        if snapshot.get("status") in FINISHED_STATUS_VALUES and context.get("run_id") and hasattr(executor, "mark_observed"):
            await executor.mark_observed(
                run_id,
                observer_run_id=str(context.get("run_id") or "") or None,
                via=self.name,
            )
            snapshot = executor.snapshot(run_id) or snapshot
        return _json(snapshot)


class StopCommandTool(BaseTool):
    @property
    def name(self) -> str:
        return "stop_command"

    @property
    def description(self) -> str:
        return "Stop a running managed background command."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command_run_id": {"type": "string"},
            },
            "required": ["command_run_id"],
        }

    async def execute(self, **kwargs) -> str:
        executor = _command_executor(kwargs)
        if executor is None:
            return _missing_executor()
        run_id = _command_run_id(kwargs)
        if not run_id:
            return _json({"error": {"type": "invalid_arguments", "message": "command_run_id is required"}})
        stopped = await executor.stop(run_id)
        snapshot = executor.snapshot(run_id) or {"command_run_id": run_id}
        snapshot["stopped"] = bool(stopped)
        return _json(snapshot)
