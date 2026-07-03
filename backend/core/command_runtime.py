from __future__ import annotations

import asyncio
import locale
import os
import subprocess
import signal
from collections import deque
from pathlib import Path
from time import time
from typing import Any, Deque, Dict, Optional

from .runs import RunKind, RunManager, RunStatus
from .runs.types import FINISHED_RUN_STATUSES
from .shell_profile import ShellProfile, ShellProfileResolver


def _decode_bytes(value: bytes) -> str:
    if not value:
        return ""
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        if not encoding:
            continue
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


def _command_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _subprocess_group_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class CommandExecutor:
    """Managed command runs backed by RunManager events."""

    def __init__(
        self,
        run_manager: RunManager,
        *,
        max_tail_chars: int = 12000,
        shell_profile: Optional[ShellProfile] = None,
    ) -> None:
        self.run_manager = run_manager
        self.max_tail_chars = max(1000, int(max_tail_chars))
        self.shell_profile = shell_profile or ShellProfileResolver().resolve()
        self._processes: Dict[str, Any] = {}
        self._tasks: Dict[str, asyncio.Task[None]] = {}
        self._stdout_tail: Dict[str, Deque[str]] = {}
        self._stderr_tail: Dict[str, Deque[str]] = {}
        self._lock = asyncio.Lock()
        self.run_manager.add_finish_listener(self._handle_run_finished)

    async def start(
        self,
        *,
        conversation_id: str,
        command: str,
        cwd: str | os.PathLike[str],
        anchor_node_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        summary: str = "",
        timeout_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cwd_path = str(Path(cwd).expanduser().resolve())
        shell_snapshot = self.shell_profile.snapshot()
        run = await self.run_manager.create_run(
            conversation_id=conversation_id,
            kind=RunKind.COMMAND,
            anchor_node_id=anchor_node_id,
            parent_run_id=parent_run_id,
            summary=summary or command[:80],
            metadata={
                "command": command,
                "cwd": cwd_path,
                "shell": shell_snapshot,
                "shell_id": self.shell_profile.id,
                "platform": self.shell_profile.platform,
                "timeout_seconds": timeout_seconds,
                **dict(metadata or {}),
            },
        )
        self._stdout_tail[run.run_id] = deque()
        self._stderr_tail[run.run_id] = deque()
        task = asyncio.create_task(self._run_process(
            run_id=run.run_id,
            conversation_id=conversation_id,
            command=command,
            cwd=cwd_path,
            shell_snapshot=shell_snapshot,
            timeout_seconds=timeout_seconds,
            anchor_node_id=anchor_node_id,
        ))
        async with self._lock:
            self._tasks[run.run_id] = task
        return run.to_dict()

    async def wait(self, run_id: str, timeout: Optional[float] = None) -> None:
        task = self._tasks.get(run_id)
        if task is None:
            return
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def mark_observed(
        self,
        run_id: str,
        *,
        observer_run_id: Optional[str],
        via: str,
    ) -> Optional[Dict[str, Any]]:
        run = self.run_manager.get_run(run_id)
        if not run or run.get("kind") != RunKind.COMMAND.value:
            return None
        if run.get("status") not in FINISHED_RUN_STATUSES:
            return run
        observer = observer_run_id or ""
        metadata = {
            "command_observed_at": time(),
            "command_observed_via": via,
            "command_notification_state": "observed",
            "notification_suppressed_reason": f"Command result observed via {via}",
        }
        if observer:
            metadata["command_observed_by_run_id"] = observer
        updated = await self.run_manager.update_metadata(run_id, metadata)
        self.run_manager.synthetic_inputs.mark_consumed_by_source(
            conversation_id=str(run.get("conversation_id") or ""),
            kind="task_notification",
            source_run_kind=RunKind.COMMAND.value,
            source_run_id=run_id,
        )
        return updated.to_dict()

    def snapshot(self, run_id: str) -> Optional[Dict[str, Any]]:
        run = self.run_manager.get_run(run_id)
        if not run:
            return None
        exit_code = None
        duration_seconds = None
        command_status = run.get("status")
        error = (run.get("metadata") or {}).get("error")
        events = self.run_manager.journal.read_from_index(str(run.get("conversation_id") or ""), run_id, 0)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        for event in events:
            payload = event.get("payload") or {}
            event_type = payload.get("event_type")
            if event_type == "command_stdout":
                stdout_parts.append(str(payload.get("content") or ""))
            elif event_type == "command_stderr":
                stderr_parts.append(str(payload.get("content") or ""))
            elif event_type in {"command_exited", "command_stopped", "command_error"}:
                exit_code = payload.get("exit_code", exit_code)
                duration_seconds = payload.get("duration_seconds", duration_seconds)
                command_status = payload.get("command_status", command_status)
                error = payload.get("error", error)
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        return {
            "command_run_id": run_id,
            "run_id": run_id,
            "status": run.get("status"),
            "command_status": command_status,
            "kind": run.get("kind"),
            "command": (run.get("metadata") or {}).get("command"),
            "cwd": (run.get("metadata") or {}).get("cwd"),
            "shell": (run.get("metadata") or {}).get("shell"),
            "shell_id": (run.get("metadata") or {}).get("shell_id"),
            "platform": (run.get("metadata") or {}).get("platform"),
            "exit_code": exit_code,
            "duration_seconds": duration_seconds,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_tail": stdout[-self.max_tail_chars:],
            "stderr_tail": stderr[-self.max_tail_chars:],
            "error": error,
            "metadata": dict(run.get("metadata") or {}),
        }

    async def stop(self, run_id: str) -> bool:
        run = self.run_manager.get_run(run_id)
        if not run or run.get("status") in {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
            return False
        await self.run_manager.request_stop(run_id)
        process = self._processes.get(run_id)
        if process is None:
            return run_id in self._tasks
        await self._kill_process_tree(process)
        return True

    async def _run_process(
        self,
        *,
        run_id: str,
        conversation_id: str,
        command: str,
        cwd: str,
        shell_snapshot: Dict[str, Any],
        timeout_seconds: Optional[int],
        anchor_node_id: Optional[str],
    ) -> None:
        final_status = RunStatus.COMPLETED
        error: str | None = None
        exit_code: int | None = None
        started_at = time()
        argv = self.shell_profile.command_argv(command)
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    env=_command_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **_subprocess_group_kwargs(),
                )
            except NotImplementedError:
                await self._run_process_with_popen(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    command=command,
                    cwd=cwd,
                    shell=shell_snapshot,
                    timeout_seconds=timeout_seconds,
                    started_at=started_at,
                    anchor_node_id=anchor_node_id,
                )
                run = self.run_manager.get_run(run_id) or {}
                final_status = RunStatus(str(run.get("status") or RunStatus.FAILED.value))
                error = (run.get("metadata") or {}).get("error")
                return
            self._processes[run_id] = process
            await self.run_manager.append_event(run_id, {
                "event_type": "command_started",
                "status": "content",
                "content": "",
                "command": command,
                "cwd": cwd,
                "shell": shell_snapshot,
                "shell_id": self.shell_profile.id,
                "pid": process.pid,
            })
            if await self.run_manager.is_stop_requested(run_id):
                await self._kill_process_tree(process)
            readers = [
                asyncio.create_task(self._read_stream(run_id, process.stdout, "stdout")),
                asyncio.create_task(self._read_stream(run_id, process.stderr, "stderr")),
            ]
            try:
                if timeout_seconds and timeout_seconds > 0:
                    exit_code = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
                else:
                    exit_code = await process.wait()
            except asyncio.TimeoutError:
                final_status = RunStatus.FAILED
                error = f"command timed out after {timeout_seconds} seconds"
                await self._kill_process_tree(process)
                exit_code = process.returncode
            finally:
                await asyncio.gather(*readers, return_exceptions=True)

            if await self.run_manager.is_stop_requested(run_id):
                final_status = RunStatus.CANCELLED
            elif final_status != RunStatus.FAILED:
                final_status = RunStatus.COMPLETED if exit_code == 0 else RunStatus.FAILED
            if final_status == RunStatus.FAILED and not error:
                stderr_tail = self._tail_text(run_id, "stderr").strip()
                error = stderr_tail or f"command exited with code {exit_code}"
            await self.run_manager.append_event(run_id, {
                "event_type": "command_stopped" if final_status == RunStatus.CANCELLED else "command_exited",
                "status": "stopped" if final_status == RunStatus.CANCELLED else "content",
                "content": "",
                "exit_code": exit_code,
                "duration_seconds": round(time() - started_at, 3),
                "command_status": final_status.value,
                "error": error,
                "shell": shell_snapshot,
                "shell_id": self.shell_profile.id,
            })
            self._enqueue_completion(
                run_id=run_id,
                conversation_id=conversation_id,
                anchor_node_id=anchor_node_id,
                command=command,
                cwd=cwd,
                exit_code=exit_code,
                final_status=final_status,
                error=error,
            )
        except Exception as exc:
            final_status = RunStatus.FAILED
            error = f"{type(exc).__name__}: {exc} while starting Command {command!r} in {cwd}"
            await self.run_manager.append_event(run_id, {
                "event_type": "command_error",
                "status": "error",
                "content": "",
                "error": error,
                "command": command,
                "cwd": cwd,
                "shell": shell_snapshot,
                "shell_id": self.shell_profile.id,
            })
            self._enqueue_completion(
                run_id=run_id,
                conversation_id=conversation_id,
                anchor_node_id=anchor_node_id,
                command=command,
                cwd=cwd,
                exit_code=exit_code,
                final_status=final_status,
                error=error,
            )
        finally:
            await self.run_manager.finish_run(run_id, final_status, error)
            self._processes.pop(run_id, None)
            self._tasks.pop(run_id, None)

    async def _read_stream(
        self,
        run_id: str,
        stream: Optional[asyncio.StreamReader],
        channel: str,
    ) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.readline()
            if not chunk:
                break
            text = _decode_bytes(chunk)
            self._append_tail(run_id, channel, text)
            await self.run_manager.append_event(run_id, {
                "event_type": f"command_{channel}",
                "status": "content",
                "content": text,
                "channel": channel,
            })

    async def _run_process_with_popen(
        self,
        *,
        run_id: str,
        conversation_id: str,
        command: str,
        cwd: str,
        shell: Dict[str, Any],
        timeout_seconds: Optional[int],
        started_at: float,
        anchor_node_id: Optional[str],
    ) -> None:
        argv = self.shell_profile.command_argv(command)
        process = await asyncio.to_thread(
            subprocess.Popen,
            argv,
            cwd=cwd,
            env=_command_env(),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_subprocess_group_kwargs(),
        )
        self._processes[run_id] = process
        await self.run_manager.append_event(run_id, {
            "event_type": "command_started",
            "status": "content",
            "content": "",
            "command": command,
            "cwd": cwd,
            "shell": shell,
            "shell_id": self.shell_profile.id,
            "pid": process.pid,
            "backend": "popen",
        })
        if await self.run_manager.is_stop_requested(run_id):
            await self._kill_process_tree(process)
        readers = [
            asyncio.create_task(self._read_blocking_stream(run_id, process.stdout, "stdout")),
            asyncio.create_task(self._read_blocking_stream(run_id, process.stderr, "stderr")),
        ]
        final_status = RunStatus.COMPLETED
        error: str | None = None
        exit_code: int | None = None
        try:
            try:
                if timeout_seconds and timeout_seconds > 0:
                    exit_code = await asyncio.to_thread(process.wait, timeout=timeout_seconds)
                else:
                    exit_code = await asyncio.to_thread(process.wait)
            except subprocess.TimeoutExpired:
                final_status = RunStatus.FAILED
                error = f"command timed out after {timeout_seconds} seconds"
                await self._kill_process_tree(process)
                exit_code = process.returncode
        finally:
            await asyncio.gather(*readers, return_exceptions=True)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

        if await self.run_manager.is_stop_requested(run_id):
            final_status = RunStatus.CANCELLED
        elif final_status != RunStatus.FAILED:
            final_status = RunStatus.COMPLETED if exit_code == 0 else RunStatus.FAILED
        if final_status == RunStatus.FAILED and not error:
            stderr_tail = self._tail_text(run_id, "stderr").strip()
            error = stderr_tail or f"command exited with code {exit_code}"
        await self.run_manager.append_event(run_id, {
            "event_type": "command_stopped" if final_status == RunStatus.CANCELLED else "command_exited",
            "status": "stopped" if final_status == RunStatus.CANCELLED else "content",
            "content": "",
            "exit_code": exit_code,
            "duration_seconds": round(time() - started_at, 3),
            "command_status": final_status.value,
            "error": error,
            "shell": shell,
            "shell_id": self.shell_profile.id,
            "backend": "popen",
        })
        self._enqueue_completion(
            run_id=run_id,
            conversation_id=conversation_id,
            anchor_node_id=anchor_node_id,
            command=command,
            cwd=cwd,
            exit_code=exit_code,
            final_status=final_status,
            error=error,
        )
        await self.run_manager.finish_run(run_id, final_status, error)

    async def _read_blocking_stream(
        self,
        run_id: str,
        stream: Any,
        channel: str,
    ) -> None:
        if stream is None:
            return
        while True:
            chunk = await asyncio.to_thread(stream.readline)
            if not chunk:
                break
            text = _decode_bytes(chunk)
            self._append_tail(run_id, channel, text)
            await self.run_manager.append_event(run_id, {
                "event_type": f"command_{channel}",
                "status": "content",
                "content": text,
                "channel": channel,
                "backend": "popen",
            })

    def _append_tail(self, run_id: str, channel: str, text: str) -> None:
        target = self._stdout_tail if channel == "stdout" else self._stderr_tail
        tail = target.setdefault(run_id, deque())
        tail.append(text)
        total = sum(len(part) for part in tail)
        while total > self.max_tail_chars and tail:
            removed = tail.popleft()
            total -= len(removed)

    def _tail_text(self, run_id: str, channel: str) -> str:
        target = self._stdout_tail if channel == "stdout" else self._stderr_tail
        return "".join(target.get(run_id, deque()))[-self.max_tail_chars:]

    def _enqueue_completion(
        self,
        *,
        run_id: str,
        conversation_id: str,
        anchor_node_id: Optional[str],
        command: str,
        cwd: str,
        exit_code: Optional[int],
        final_status: RunStatus,
        error: Optional[str],
    ) -> None:
        run = self.run_manager.get_run(run_id) or {}
        metadata = dict(run.get("metadata") or {})
        if metadata.get("command_observed_at"):
            self._record_notification_metadata(run_id, {
                "command_notification_state": "observed",
                "notification_suppressed_reason": "Command result already observed",
            })
            return
        if metadata.get("command_notification_state") == "sent":
            return
        parent_run_id = run.get("parent_run_id")
        parent_run = self.run_manager.get_run(str(parent_run_id)) if parent_run_id else None
        if parent_run and parent_run.get("status") not in FINISHED_RUN_STATUSES:
            self._record_notification_metadata(run_id, {
                "command_notification_state": "deferred",
                "notification_suppressed_reason": "parent run is still active",
            })
            return
        status_text = final_status.value
        summary = f"Command {status_text}"
        if exit_code is not None:
            summary += f" (exit code {exit_code})"
        payload = {
            "command_run_id": run_id,
            "status": status_text,
            "command": command,
            "cwd": cwd,
            "shell": metadata.get("shell"),
            "shell_id": metadata.get("shell_id"),
            "platform": metadata.get("platform"),
            "exit_code": exit_code,
            "stdout_tail": self._tail_text(run_id, "stdout"),
            "stderr_tail": self._tail_text(run_id, "stderr"),
            "error": error,
        }
        self.run_manager.synthetic_inputs.enqueue(
            kind="task_notification",
            conversation_id=conversation_id,
            anchor_node_id=anchor_node_id,
            source_run_id=run_id,
            source_run_kind=RunKind.COMMAND.value,
            summary=summary,
            content=json_dumps(payload),
            metadata={
                "source_status": status_text,
                "command_run_id": run_id,
                "exit_code": exit_code,
            },
        )
        self._record_notification_metadata(run_id, {
            "command_notification_state": "sent",
            "command_notification_sent_at": time(),
            "notification_suppressed_reason": None,
        })

    def _handle_run_finished(self, run: Dict[str, Any]) -> None:
        parent_run_id = str(run.get("run_id") or "")
        if not parent_run_id:
            return
        for child in self.run_manager.list_runs(str(run.get("conversation_id") or "")):
            if child.get("kind") != RunKind.COMMAND.value:
                continue
            if child.get("parent_run_id") != parent_run_id:
                continue
            if child.get("status") not in FINISHED_RUN_STATUSES:
                continue
            metadata = dict(child.get("metadata") or {})
            if metadata.get("command_observed_at") or metadata.get("command_notification_state") == "sent":
                continue
            snapshot = self.snapshot(str(child.get("run_id") or ""))
            if not snapshot:
                continue
            status = snapshot.get("status")
            if status not in FINISHED_RUN_STATUSES:
                continue
            self._enqueue_completion(
                run_id=str(child["run_id"]),
                conversation_id=str(child["conversation_id"]),
                anchor_node_id=child.get("anchor_node_id"),
                command=str(snapshot.get("command") or metadata.get("command") or ""),
                cwd=str(snapshot.get("cwd") or metadata.get("cwd") or ""),
                exit_code=snapshot.get("exit_code"),
                final_status=RunStatus(str(status)),
                error=snapshot.get("error"),
            )

    def _record_notification_metadata(self, run_id: str, metadata: Dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.run_manager.update_metadata(run_id, metadata))

    async def _kill_process_tree(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            pid = process.pid
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            try:
                await self._wait_process(process, timeout=2)
            except asyncio.TimeoutError:
                with suppress_process_lookup():
                    process.kill()
            return
        with suppress_process_lookup():
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await self._wait_process(process, timeout=2)
        except asyncio.TimeoutError:
            with suppress_process_lookup():
                os.killpg(process.pid, signal.SIGKILL)

    async def _wait_process(self, process: Any, *, timeout: float) -> None:
        if isinstance(process, asyncio.subprocess.Process):
            await asyncio.wait_for(process.wait(), timeout=timeout)
            return
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=timeout)


def json_dumps(payload: Dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


class suppress_process_lookup:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type in {ProcessLookupError}

