from __future__ import annotations

import asyncio
import locale
import logging
import os
import subprocess
import signal
from collections import deque
from pathlib import Path
from time import time
from typing import Any, Deque, Dict, Optional

from .config.config import cfg
from .projects import resolve_dev_environment
from .subprocess_utils import subprocess_window_kwargs
from .runs import RunKind, RunManager, RunStatus
from .runs.public import public_run_dict
from .runs.types import FINISHED_RUN_STATUSES
from .shell_profile import ShellProfile, ShellProfileResolver
from .tasks import ActiveTaskService

FINISHED_STATUS_VALUES = {status.value for status in FINISHED_RUN_STATUSES}
logger = logging.getLogger(__name__)


class CommandExecutorClosingError(RuntimeError):
    pass


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


def _command_env(cwd: str) -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    path_dirs = resolve_dev_environment(
        cfg.data if isinstance(cfg.data, dict) else None,
        cwd,
    )["path_dirs"]
    if path_dirs:
        path_key = next((key for key in env if key.upper() == "PATH"), "PATH")
        separator = ";" if os.name == "nt" else ":"
        existing = env.get(path_key, "")
        env[path_key] = separator.join([*path_dirs, existing]) if existing else separator.join(path_dirs)
    return env


def _subprocess_group_kwargs() -> Dict[str, Any]:
    return subprocess_window_kwargs(new_process_group=True)


class CommandExecutor:
    """Managed command runs backed by RunManager events."""

    def __init__(
        self,
        run_manager: RunManager,
        *,
        max_tail_chars: int = 12000,
        shell_profile: Optional[ShellProfile] = None,
        task_service: Optional[ActiveTaskService] = None,
    ) -> None:
        self.run_manager = run_manager
        self.max_tail_chars = max(1000, int(max_tail_chars))
        self.shell_profile = shell_profile or ShellProfileResolver().resolve()
        self.task_service = task_service
        self._processes: Dict[str, Any] = {}
        self._tasks: Dict[str, asyncio.Task[None]] = {}
        self._stdout_tail: Dict[str, Deque[str]] = {}
        self._stderr_tail: Dict[str, Deque[str]] = {}
        self._lock = asyncio.Lock()
        self._admission_lock = asyncio.Lock()
        self._closing = False
        self._close_task: asyncio.Task[tuple[str, ...]] | None = None
        self._undrained_run_ids: set[str] = set()
        self._inflight_start_run_id: str | None = None

    async def start(
        self,
        *,
        conversation_id: str,
        command: str,
        cwd: str | os.PathLike[str],
        anchor_node_id: Optional[str] = None,
        created_by_run_id: Optional[str] = None,
        cancellation_parent_run_id: Optional[str] = None,
        summary: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        step: Optional[int] = None,
        task_context_mode: str = "attached",
        task_generation_id: Optional[str] = None,
        task_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        async with self._admission_lock:
            self._inflight_start_run_id = None
            try:
                return await self._start_admitted(
                    conversation_id=conversation_id,
                    command=command,
                    cwd=cwd,
                    anchor_node_id=anchor_node_id,
                    created_by_run_id=created_by_run_id,
                    cancellation_parent_run_id=cancellation_parent_run_id,
                    summary=summary,
                    metadata=metadata,
                    step=step,
                    task_context_mode=task_context_mode,
                    task_generation_id=task_generation_id,
                    task_revision=task_revision,
                )
            finally:
                self._inflight_start_run_id = None

    async def _start_admitted(
        self,
        *,
        conversation_id: str,
        command: str,
        cwd: str | os.PathLike[str],
        anchor_node_id: Optional[str] = None,
        created_by_run_id: Optional[str] = None,
        cancellation_parent_run_id: Optional[str] = None,
        summary: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        step: Optional[int] = None,
        task_context_mode: str = "attached",
        task_generation_id: Optional[str] = None,
        task_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        async with self._lock:
            if self._closing:
                raise CommandExecutorClosingError("command executor is closing")
        cwd_path = str(Path(cwd).expanduser().resolve())
        run_metadata = dict(metadata or {})
        task_binding = None
        if step is not None:
            if self.task_service is None:
                raise RuntimeError("task service is not configured")
            task_binding = await self.task_service.prepare_run_binding(
                conversation_id=conversation_id,
                step=step,
                context_mode=task_context_mode,
                expected_generation=task_generation_id,
                expected_revision=task_revision,
            )
        shell_snapshot = self.shell_profile.snapshot()
        run = await self.run_manager.create_run(
            conversation_id=conversation_id,
            kind=RunKind.COMMAND,
            anchor_node_id=anchor_node_id,
            created_by_run_id=created_by_run_id,
            cancellation_parent_run_id=cancellation_parent_run_id,
            summary=summary or command[:80],
            metadata={
                "command": command,
                "cwd": cwd_path,
                "shell": shell_snapshot,
                "shell_id": self.shell_profile.id,
                "platform": self.shell_profile.platform,
                **run_metadata,
            },
            task_binding=task_binding,
        )
        self._inflight_start_run_id = run.run_id
        producer = None
        producer_registered = False
        try:
            self._stdout_tail[run.run_id] = deque()
            self._stderr_tail[run.run_id] = deque()
            producer = self._run_process(
                run_id=run.run_id,
                conversation_id=conversation_id,
                command=command,
                cwd=cwd_path,
                shell_snapshot=shell_snapshot,
                anchor_node_id=anchor_node_id,
            )
            async with self._lock:
                if self._closing:
                    raise CommandExecutorClosingError("command executor is closing")
                task = asyncio.create_task(
                    producer,
                    name=f"command-producer:{run.run_id}",
                )
                self._tasks[run.run_id] = task
                task.add_done_callback(
                    lambda completed, current_run_id=run.run_id: (
                        self._command_task_done(current_run_id, completed)
                    )
                )
                producer_registered = True
            return run.to_dict()
        except BaseException:
            if not producer_registered and producer is not None:
                producer.close()
            try:
                await asyncio.shield(self.run_manager.finish_run(
                    run.run_id,
                    RunStatus.INTERRUPTED,
                    "command start interrupted before producer registration",
                ))
            except BaseException:
                if not self._is_durably_terminal(run.run_id):
                    self._undrained_run_ids.add(run.run_id)
                raise
            raise

    async def wait(self, run_id: str, timeout: Optional[float] = None) -> None:
        if self.run_manager.get_run(run_id) is None:
            return
        await self.run_manager.wait_for_terminal_result(
            run_id,
            result_event_types={"command_exited", "command_stopped"},
            error_event_types={"command_error"},
            timeout=timeout,
        )

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
        if run.get("status") not in FINISHED_STATUS_VALUES:
            return run
        updated = await self.run_manager.mark_observed(
            run_id,
            observer_run_id=observer_run_id,
            via=via,
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
        events = self.run_manager.read_events(run_id, 0)
        public_run = public_run_dict(run)
        public_metadata = dict(public_run["metadata"])
        task_outcome = public_metadata.pop("task_outcome", None)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        for payload in events:
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
        snapshot = {
            "command_run_id": run_id,
            "run_id": run_id,
            "status": run.get("status"),
            "command_status": command_status,
            "kind": run.get("kind"),
        }
        if isinstance(task_outcome, dict):
            snapshot["task_outcome"] = task_outcome
        snapshot.update({
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
            "metadata": public_metadata,
        })
        if public_run.get("step") is not None:
            snapshot["step"] = public_run["step"]
        return snapshot

    async def stop(self, run_id: str) -> bool:
        run = self.run_manager.get_run(run_id)
        process = self._processes.get(run_id)
        task_active = run_id in self._tasks
        if (
            process is None
            and not task_active
            and (
                not run
                or run.get("status")
                in {
                    RunStatus.COMPLETED.value,
                    RunStatus.FAILED.value,
                    RunStatus.CANCELLED.value,
                }
            )
        ):
            return False
        persistence_error: BaseException | None = None
        try:
            await self.run_manager.request_stop(run_id)
        except BaseException as exc:
            persistence_error = exc
        if process is None:
            stopped = task_active
        else:
            await self._kill_process_tree(process)
            stopped = True
        if persistence_error is not None:
            raise persistence_error
        return stopped

    async def close(self, timeout: float = 5.0) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        async with self._lock:
            self._closing = True
            close_task = self._close_task
        if close_task is not None:
            return await asyncio.shield(close_task)

        acquired = False
        remaining = deadline - loop.time()
        if not self._admission_lock.locked():
            await self._admission_lock.acquire()
            acquired = True
        elif remaining > 0:
            try:
                await asyncio.wait_for(
                    self._admission_lock.acquire(),
                    timeout=remaining,
                )
                acquired = True
            except asyncio.TimeoutError:
                pass
        if not acquired:
            unresolved = (
                self._inflight_start_run_id or "command-start-in-flight",
            )
            async with self._lock:
                if self._close_task is None:
                    self._close_task = asyncio.create_task(
                        self._return_close_report(unresolved),
                        name="command-executor-close",
                    )
                close_task = self._close_task
            return await asyncio.shield(close_task)

        try:
            async with self._lock:
                if self._close_task is None:
                    tasks = dict(self._tasks)
                    run_ids = tuple(sorted(
                        set(tasks)
                        | set(self._processes)
                        | self._undrained_run_ids
                    ))
                    self._close_task = asyncio.create_task(
                        self._close_once(
                            run_ids,
                            tasks,
                            max(0.0, deadline - loop.time()),
                        ),
                        name="command-executor-close",
                    )
                close_task = self._close_task
        finally:
            self._admission_lock.release()
        return await asyncio.shield(close_task)

    @staticmethod
    async def _return_close_report(
        report: tuple[str, ...],
    ) -> tuple[str, ...]:
        return report

    async def _close_once(
        self,
        run_ids: tuple[str, ...],
        tasks: Dict[str, asyncio.Task[None]],
        timeout: float,
    ) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        stop_tasks = {
            run_id: asyncio.create_task(
                self._request_stop_for_close(run_id),
                name=f"command-close-stop:{run_id}",
            )
            for run_id in run_ids
        }
        kill_tasks: dict[str, asyncio.Task[None]] = {}

        while True:
            for run_id, kill_task in list(kill_tasks.items()):
                if kill_task.done():
                    kill_tasks.pop(run_id, None)
            for run_id in run_ids:
                process = self._processes.get(run_id)
                if (
                    process is not None
                    and process.returncode is None
                    and run_id not in kill_tasks
                ):
                    kill_tasks[run_id] = asyncio.create_task(
                        self._kill_for_close(run_id, process),
                        name=f"command-close-kill:{run_id}",
                    )

            unresolved: set[str] = set()
            for run_id in run_ids:
                command_task = tasks.get(run_id)
                process = self._processes.get(run_id)
                if process is not None and process.returncode is not None:
                    if self._processes.get(run_id) is process:
                        self._processes.pop(run_id, None)
                    process = None
                kill_task = kill_tasks.get(run_id)
                durably_terminal = self._is_durably_terminal(run_id)
                if durably_terminal and process is None:
                    self._undrained_run_ids.discard(run_id)
                if (
                    (command_task is not None and not command_task.done())
                    or not stop_tasks[run_id].done()
                    or (process is not None and process.returncode is None)
                    or (kill_task is not None and not kill_task.done())
                    or not durably_terminal
                ):
                    unresolved.add(run_id)
            if not unresolved:
                return ()
            remaining = deadline - loop.time()
            if remaining <= 0:
                return tuple(sorted(unresolved))
            pending = [
                task
                for task in [
                    *(tasks.get(run_id) for run_id in unresolved),
                    *(stop_tasks.get(run_id) for run_id in unresolved),
                    *(kill_tasks.get(run_id) for run_id in unresolved),
                ]
                if task is not None and not task.done()
            ]
            if pending:
                await asyncio.wait(
                    pending,
                    timeout=min(0.05, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                await asyncio.sleep(min(0.01, remaining))

    async def _request_stop_for_close(self, run_id: str) -> None:
        try:
            await self.run_manager.request_stop(run_id)
        except BaseException as exc:
            logger.error(
                "Failed to persist command stop during close for %s",
                run_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _kill_for_close(self, run_id: str, process: Any) -> None:
        try:
            await self._kill_process_tree(process)
        except BaseException as exc:
            logger.error(
                "Failed to kill command process during close for %s",
                run_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _command_task_done(
        self,
        run_id: str,
        task: asyncio.Task[None],
    ) -> None:
        process = self._processes.get(run_id)
        physically_drained = process is None or process.returncode is not None
        if self._is_durably_terminal(run_id) and physically_drained:
            self._undrained_run_ids.discard(run_id)
        else:
            self._undrained_run_ids.add(run_id)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Command producer failed: %s",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    def _is_durably_terminal(self, run_id: str) -> bool:
        try:
            run = self.run_manager.repository.get_run(run_id)
        except Exception:
            return False
        return bool(
            run is not None
            and str(run.get("status") or "") in FINISHED_STATUS_VALUES
        )

    async def _run_process(
        self,
        *,
        run_id: str,
        conversation_id: str,
        command: str,
        cwd: str,
        shell_snapshot: Dict[str, Any],
        anchor_node_id: Optional[str],
    ) -> None:
        final_status = RunStatus.COMPLETED
        error: str | None = None
        exit_code: int | None = None
        started_at = time()
        completion_handled_by_fallback = False
        argv = self.shell_profile.command_argv(command)
        process: asyncio.subprocess.Process | None = None
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    env=_command_env(cwd),
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
                    started_at=started_at,
                    anchor_node_id=anchor_node_id,
                )
                run = self.run_manager.get_run(run_id) or {}
                final_status = RunStatus(str(run.get("status") or RunStatus.FAILED.value))
                error = (run.get("metadata") or {}).get("error")
                completion_handled_by_fallback = True
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
            await self.run_manager.flush_run_events(run_id)
            if await self.run_manager.is_stop_requested(run_id):
                await self._kill_process_tree(process)
            readers = [
                asyncio.create_task(self._read_stream(run_id, process.stdout, "stdout")),
                asyncio.create_task(self._read_stream(run_id, process.stderr, "stderr")),
            ]
            try:
                exit_code = await process.wait()
            except asyncio.CancelledError:
                final_status = RunStatus.CANCELLED
                error = "command producer cancelled"
                await self._kill_process_tree(process)
                raise
            finally:
                await asyncio.gather(*readers, return_exceptions=True)

            if await self.run_manager.is_stop_requested(run_id):
                final_status = RunStatus.CANCELLED
            elif final_status != RunStatus.FAILED:
                final_status = RunStatus.COMPLETED if exit_code == 0 else RunStatus.FAILED
            if final_status == RunStatus.FAILED and not error:
                stderr_tail = self._tail_text(run_id, "stderr").strip()
                error = stderr_tail or f"command exited with code {exit_code}: {command}"
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
        except asyncio.CancelledError:
            final_status = RunStatus.CANCELLED
            error = "command producer cancelled"
            raise
        except Exception as exc:
            final_status = RunStatus.FAILED
            error = f"{type(exc).__name__}: {exc} while starting Command {command!r} in {cwd}"
            try:
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
            except Exception:
                # finish_run owns recovery when this run's event writer failed.
                pass
        finally:
            owned_process = self._processes.get(run_id, process)
            if owned_process is not None and owned_process.returncode is None:
                try:
                    await self._kill_process_tree(owned_process)
                except Exception:
                    logger.exception(
                        "Failed to kill command process during cleanup for %s",
                        run_id,
                    )
            if owned_process is not None and owned_process.returncode is None:
                self._undrained_run_ids.add(run_id)
            try:
                try:
                    await self.run_manager.finish_run(run_id, final_status, error)
                except BaseException:
                    if not self._is_durably_terminal(run_id):
                        self._undrained_run_ids.add(run_id)
                    raise
            finally:
                if owned_process is None or owned_process.returncode is not None:
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
        started_at: float,
        anchor_node_id: Optional[str],
    ) -> None:
        argv = self.shell_profile.command_argv(command)
        # Popen and handle registration must be one non-cancellable handoff.
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=_command_env(cwd),
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
        await self.run_manager.flush_run_events(run_id)
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
                exit_code = await asyncio.to_thread(process.wait)
            except asyncio.CancelledError:
                final_status = RunStatus.CANCELLED
                error = "command producer cancelled"
                await self._kill_process_tree(process)
                raise
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
            error = stderr_tail or f"command exited with code {exit_code}: {command}"
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
                **subprocess_window_kwargs(),
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

