from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Dict

from . import common
from ..task_contract import task_step_parameter_schema
from ...command_runtime import _command_env
from ...shell_profile import ShellProfileResolver, render_command_tool_guidance
from ...subprocess_utils import subprocess_window_kwargs


class RunCommandTool(common._CodeTool):
    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        profile = ShellProfileResolver().resolve()
        return (
            "Run a synchronous-compatible development command in the code workspace. "
            "Use this for tests, builds, scripts, git, package-manager commands, and environment probes. "
            "Do not use it for ordinary file listing, file reading, or text search; use glob, read, and grep instead. "
            "When ChatTree runtime context is available, the command starts foreground, returns stdout/stderr/exit_code if it finishes within the initial wait window, "
            "and auto-backgrounds with a command_run_id if it keeps running. Commands do not time out on their own; stop them explicitly when needed. "
            "After auto-backgrounding, do not start another shell command to poll it. Use `wait_command` when the terminal result is required; its wait timeout never stops the command.\n\n"
            f"{render_command_tool_guidance(profile)}"
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "step": task_step_parameter_schema(),
            },
            "required": ["command"],
        }

    async def execute(self, **kwargs) -> str:
        command = str(kwargs.get("command") or "")
        if not command.strip():
            return common._error("command_failed", "command is required")
        try:
            cwd = self.workspace.check_read(kwargs.get("cwd") or ".")
        except common.CodeToolError as exc:
            return common._error(exc.error_type, str(exc), path=str(kwargs.get("cwd") or "."))
        runtime_context = kwargs.get("_runtime_context")
        if isinstance(runtime_context, dict) and runtime_context.get("command_executor") is not None:
            return await self._execute_managed(
                command=command,
                cwd=cwd,
                runtime_context=runtime_context,
                step=kwargs.get("step"),
            )
        if kwargs.get("step") is not None:
            return common._error("missing_runtime_context", "step binding requires ChatTree runtime context")
        python_c_args = common._windows_python_c_args(command)
        profile = ShellProfileResolver().resolve()
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                args=python_c_args or profile.command_argv(command),
                shell=False,
                cwd=str(cwd),
                env=_command_env(str(cwd)),
                capture_output=True,
                **subprocess_window_kwargs(),
            )
        except Exception as exc:
            return common._error(
                type(exc).__name__,
                str(exc),
                tool_name=self.name,
                command=command,
                cwd=self.workspace.relative(cwd),
            )
        return common._json({
            "command": command,
            "cwd": self.workspace.relative(cwd),
            "exit_code": proc.returncode,
            "stdout": common._decode_output(proc.stdout, self.config.max_output_chars),
            "stderr": common._decode_output(proc.stderr, self.config.max_output_chars),
            "timed_out": False,
        })

    async def _execute_managed(
        self,
        *,
        command: str,
        cwd: Path,
        runtime_context: Dict[str, Any],
        step: Any = None,
    ) -> str:
        command_executor = runtime_context.get("command_executor")
        if command_executor is None or not hasattr(command_executor, "start"):
            return common._error("missing_command_executor", "managed shell requires a command executor")
        run = await command_executor.start(
            conversation_id=str(runtime_context.get("conversation_id") or ""),
            command=command,
            cwd=str(cwd),
            anchor_node_id=str(runtime_context.get("anchor_node_id") or runtime_context.get("node_id") or "") or None,
            created_by_run_id=str(runtime_context.get("run_id") or "") or None,
            cancellation_parent_run_id=str(runtime_context.get("run_id") or "") or None,
            summary=command[:80],
            step=step,
            task_context_mode=str(runtime_context.get("task_context_mode") or "attached"),
            task_generation_id=str(runtime_context.get("task_generation_id") or "") or None,
            task_revision=(
                int(runtime_context["task_revision"])
                if runtime_context.get("task_revision") is not None
                else None
            ),
            metadata={
                "tool_name": self.name,
                "tool_call_id": runtime_context.get("tool_call_id"),
                "workspace_relative_cwd": self.workspace.relative(cwd),
                "shell_managed": True,
                "agent_name": runtime_context.get("agent_name"),
                "source_run_id": runtime_context.get("run_id"),
                "source_run_kind": runtime_context.get("run_kind"),
                "root_run_id": runtime_context.get("root_run_id"),
            },
        )
        run_id = str(run["run_id"])

        initial_wait = max(0.0, float(self.config.shell_initial_wait_seconds))
        try:
            await command_executor.wait(run_id, timeout=initial_wait)
        except asyncio.TimeoutError:
            if hasattr(command_executor, "run_manager"):
                await command_executor.run_manager.update_cancellation_parent(run_id, None)
                await command_executor.run_manager.update_metadata(run_id, {
                    "shell_auto_backgrounded": True,
                    "shell_initial_wait_seconds": initial_wait,
                })
            return self._managed_background_payload(run_id)

        snapshot = command_executor.snapshot(run_id)
        if snapshot is None:
            return common._error("not_found", "managed command run not found", command=command)
        if snapshot.get("status") in common.FINISHED_STATUS_VALUES and hasattr(command_executor, "mark_observed"):
            await command_executor.mark_observed(
                run_id,
                observer_run_id=str(runtime_context.get("run_id") or "") or None,
                via=self.name,
            )
            snapshot = command_executor.snapshot(run_id) or snapshot
        return common._json(self._managed_finished_payload(
            command=command,
            cwd=cwd,
            run_id=run_id,
            snapshot=snapshot,
        ))

    def _managed_finished_payload(
        self,
        *,
        command: str,
        cwd: Path,
        run_id: str,
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "command": command,
            "cwd": self.workspace.relative(cwd),
            "exit_code": snapshot.get("exit_code"),
            "stdout": common._decode_output(snapshot.get("stdout"), self.config.max_output_chars),
            "stderr": common._decode_output(snapshot.get("stderr"), self.config.max_output_chars),
            "timed_out": False,
            "background": False,
            "managed": True,
            "kind": "command",
            "command_run_id": run_id,
            "run_id": run_id,
            "status": snapshot.get("status"),
        }
        self._attach_public_task_outcome(payload, snapshot)
        return payload

    def _managed_background_payload(
        self,
        run_id: str,
    ) -> str:
        return (
            "Command moved to background. "
            f"command_run_id: {run_id}. "
            "Use `wait_command` to wait for it, continue with other work, "
            "or end the conversation if you expect it to take a long time."
        )

    @staticmethod
    def _attach_public_task_outcome(payload: Dict[str, Any], snapshot: Dict[str, Any]) -> None:
        task_outcome = snapshot.get("task_outcome")
        if isinstance(task_outcome, dict):
            payload["task_outcome"] = task_outcome
        if snapshot.get("step") is not None:
            payload["step"] = snapshot["step"]


class WaitCommandTool(common._CodeTool):
    @property
    def name(self) -> str:
        return "wait_command"

    @property
    def description(self) -> str:
        return (
            "Wait for a managed shell command run and return its terminal result. "
            "The timeout applies only to this wait call; it never stops or times out the command. "
            "A completed result is marked observed and removed from the run panel."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command_run_id": {
                    "type": "string",
                    "description": "The command_run_id returned by shell.",
                },
                "timeout_seconds": {"type": "number", "minimum": 0},
            },
            "required": ["command_run_id"],
        }

    async def execute(self, **kwargs) -> str:
        runtime_context = kwargs.get("_runtime_context")
        command_executor = runtime_context.get("command_executor") if isinstance(runtime_context, dict) else None
        if (
            command_executor is None
            or not hasattr(command_executor, "wait")
            or not hasattr(command_executor, "snapshot")
        ):
            return common._error("missing_command_executor", "wait_command requires ChatTree runtime context")
        run_id = str(kwargs.get("command_run_id") or "").strip()
        if not run_id:
            return common._error("command_run_id_required", "command_run_id is required")
        snapshot = command_executor.snapshot(run_id)
        if snapshot is None:
            return common._error("not_found", "managed command run not found", command_run_id=run_id)
        if snapshot.get("kind") != "command":
            return common._error("invalid_run_kind", "run_id is not a command run", command_run_id=run_id)

        timeout = kwargs.get("timeout_seconds")
        try:
            await command_executor.wait(
                run_id,
                timeout=float(timeout) if timeout is not None else 30.0,
            )
        except asyncio.TimeoutError:
            snapshot = command_executor.snapshot(run_id) or snapshot
            return common._json(self._payload(snapshot, run_id, wait_expired=True))

        snapshot = command_executor.snapshot(run_id) or snapshot
        if snapshot.get("status") in common.FINISHED_STATUS_VALUES and hasattr(command_executor, "mark_observed"):
            await command_executor.mark_observed(
                run_id,
                observer_run_id=str(runtime_context.get("run_id") or "") or None,
                via=self.name,
            )
            snapshot = command_executor.snapshot(run_id) or snapshot
        return common._json(self._payload(snapshot, run_id, wait_expired=False))

    def _payload(
        self,
        snapshot: Dict[str, Any],
        run_id: str,
        *,
        wait_expired: bool,
    ) -> Dict[str, Any]:
        return {
            "command_run_id": run_id,
            "run_id": run_id,
            "kind": "command",
            "status": snapshot.get("status"),
            "command_status": snapshot.get("command_status") or snapshot.get("status"),
            "wait_expired": wait_expired,
            "result_observed": not wait_expired and snapshot.get("status") in common.FINISHED_STATUS_VALUES,
            "command": snapshot.get("command"),
            "cwd": snapshot.get("cwd"),
            "exit_code": snapshot.get("exit_code"),
            "duration_seconds": snapshot.get("duration_seconds"),
            "stdout": common._decode_output(snapshot.get("stdout"), self.config.max_output_chars),
            "stderr": common._decode_output(snapshot.get("stderr"), self.config.max_output_chars),
            "error": snapshot.get("error"),
        }
