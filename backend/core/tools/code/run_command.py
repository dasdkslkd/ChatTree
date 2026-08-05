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
            "and auto-backgrounds with a command_run_id if it keeps running.\n\n"
            f"{render_command_tool_guidance(profile)}"
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1},
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
        timeout = max(
            1,
            min(
                int(kwargs.get("timeout_seconds") or self.config.command_timeout_seconds),
                self.config.command_timeout_seconds,
            ),
        )
        runtime_context = kwargs.get("_runtime_context")
        if isinstance(runtime_context, dict) and runtime_context.get("command_executor") is not None:
            return await self._execute_managed(
                command=command,
                cwd=cwd,
                timeout=timeout,
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
                timeout=timeout,
                **subprocess_window_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            return common._json({
                "command": command,
                "cwd": self.workspace.relative(cwd),
                "exit_code": None,
                "stdout": common._decode_output(exc.output, self.config.max_output_chars),
                "stderr": common._decode_output(exc.stderr, self.config.max_output_chars),
                "timed_out": True,
            })
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
        timeout: int,
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
            timeout_seconds=timeout,
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
            snapshot = command_executor.snapshot(run_id) or {}
            return common._json(self._managed_background_payload(command, cwd, run_id, snapshot, auto_backgrounded=True))

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
        command: str,
        cwd: Path,
        run_id: str,
        snapshot: Dict[str, Any],
        *,
        auto_backgrounded: bool,
    ) -> Dict[str, Any]:
        action = "auto-backgrounded" if auto_backgrounded else "running in the background"
        payload: Dict[str, Any] = {
            "command": command,
            "cwd": self.workspace.relative(cwd),
            "status": "running",
            "kind": "command",
            "command_run_id": run_id,
            "run_id": run_id,
            "background": True,
            "managed": True,
            "auto_backgrounded": auto_backgrounded,
            "stdout_tail": snapshot.get("stdout_tail") or "",
            "stderr_tail": snapshot.get("stderr_tail") or "",
            "message": (
                f"Command is {action} as a managed side run. "
                "Watch the task notification for progress, and only report final command status after the managed run finishes."
            ),
        }
        self._attach_public_task_outcome(payload, snapshot)
        return payload

    @staticmethod
    def _attach_public_task_outcome(payload: Dict[str, Any], snapshot: Dict[str, Any]) -> None:
        task_outcome = snapshot.get("task_outcome")
        if isinstance(task_outcome, dict):
            payload["task_outcome"] = task_outcome
        if snapshot.get("step") is not None:
            payload["step"] = snapshot["step"]
