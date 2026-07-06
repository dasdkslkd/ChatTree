from __future__ import annotations

import asyncio
import base64
import json
import locale
import os
import platform
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .base import BaseTool
from .security.logical_sandbox import DEFAULT_PROTECTED_PATHS
from ..shell_profile import ShellProfileResolver, render_command_tool_guidance


DEFAULT_CODE_WORKSPACE = r"D:\Workspace\ChatTree\tmp"
DEFAULT_RIPGREP_VERSION = "15.1.0"
TEXT_READ_CHUNK_CHARS = 8192


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_ripgrep_install_dir() -> Path:
    return _project_root() / "data" / "tools" / "ripgrep"


class CodeToolError(ValueError):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(error_type: str, message: str, **extra: Any) -> str:
    return _json({"error": {"type": error_type, "message": message, **extra}})


def _decode_output(value: bytes | str | None, max_chars: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return _decode_output_bytes(value)[:max_chars]
    return value[:max_chars]


def _decode_output_bytes(value: bytes) -> str:
    try:
        decoded = value.decode("utf-8")
        if "\ufffd" not in decoded:
            return decoded
    except UnicodeDecodeError:
        pass

    detected_encoding = _detect_output_encoding(value)
    if detected_encoding:
        try:
            return value.decode(detected_encoding)
        except (LookupError, UnicodeDecodeError):
            pass

    preferred_encoding = locale.getpreferredencoding(False)
    if preferred_encoding:
        try:
            return value.decode(preferred_encoding)
        except (LookupError, UnicodeDecodeError):
            pass

    return value.decode("utf-8", errors="replace")


def _detect_output_encoding(value: bytes) -> Optional[str]:
    try:
        import chardet  # type: ignore[import-not-found]
    except ImportError:
        return None
    result = chardet.detect(value) or {}
    encoding = result.get("encoding")
    return str(encoding) if encoding else None


def _run_command_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _windows_python_c_args(command: str) -> Optional[List[str]]:
    if os.name != "nt":
        return None
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return None
    if len(parts) != 3 or parts[1] != "-c":
        return None
    executable_name = parts[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable_name.endswith(".exe"):
        executable_name = executable_name[:-4]
    if executable_name not in {"python", "python3", "py"}:
        return None
    return [parts[0], "-c", parts[2]]


@dataclass(frozen=True)
class CodeToolConfig:
    workspace_roots: List[Path]
    protected_paths: List[Path]
    command_timeout_seconds: int = 120
    run_command_initial_wait_seconds: float = 120.0
    max_read_chars: int = 20000
    max_output_chars: int = 60000
    allow_parent_dir_creation: bool = False
    ripgrep_version: str = DEFAULT_RIPGREP_VERSION
    ripgrep_install_dir: Path = _default_ripgrep_install_dir()

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]] = None) -> "CodeToolConfig":
        cfg = raw or {}
        roots = cfg.get("workspace_roots") or [DEFAULT_CODE_WORKSPACE]
        protected = cfg.get("protected_paths") or DEFAULT_PROTECTED_PATHS
        ripgrep_cfg = cfg.get("ripgrep") if isinstance(cfg.get("ripgrep"), dict) else {}
        ripgrep_install_dir = (
            ripgrep_cfg.get("install_dir")
            or cfg.get("ripgrep_install_dir")
            or _default_ripgrep_install_dir()
        )
        ripgrep_install_path = Path(str(ripgrep_install_dir)).expanduser()
        if not ripgrep_install_path.is_absolute():
            ripgrep_install_path = _project_root() / ripgrep_install_path
        return cls(
            workspace_roots=[Path(root).expanduser().resolve() for root in roots],
            protected_paths=[Path(path) for path in protected],
            command_timeout_seconds=int(cfg.get("command_timeout_seconds", 120)),
            run_command_initial_wait_seconds=float(cfg.get("run_command_initial_wait_seconds", 120.0)),
            max_read_chars=int(cfg.get("max_read_chars", 20000)),
            max_output_chars=int(cfg.get("max_output_chars", 60000)),
            allow_parent_dir_creation=bool(cfg.get("allow_parent_dir_creation", False)),
            ripgrep_version=str(
                ripgrep_cfg.get("version")
                or cfg.get("ripgrep_version")
                or DEFAULT_RIPGREP_VERSION
            ),
            ripgrep_install_dir=ripgrep_install_path.resolve(),
        )

    @classmethod
    def for_workspace(
        cls,
        raw: Optional[Dict[str, Any]],
        workspace: Dict[str, Any],
    ) -> "CodeToolConfig":
        cfg = dict(raw or {})
        cfg["workspace_roots"] = workspace.get("workspace_roots") or [workspace.get("cwd")]
        cfg["protected_paths"] = workspace.get("protected_paths") or cfg.get("protected_paths") or DEFAULT_PROTECTED_PATHS
        return cls.from_dict(cfg)


class CodeWorkspace:
    def __init__(self, config: CodeToolConfig):
        self.config = config
        for root in self.config.workspace_roots:
            root.mkdir(parents=True, exist_ok=True)

    @property
    def default_root(self) -> Path:
        return self.config.workspace_roots[0]

    def resolve(self, path: str | os.PathLike[str] | None = ".") -> Path:
        raw = Path(str(path or ".")).expanduser()
        target = raw if raw.is_absolute() else self.default_root / raw
        return target.resolve()

    def relative(self, path: Path) -> str:
        root = self._containing_root(path)
        if root is None:
            return str(path)
        relative = path.relative_to(root)
        return "." if str(relative) == "." else relative.as_posix()

    def check_read(self, path: str | os.PathLike[str] | None = ".") -> Path:
        target = self.resolve(path)
        self._check_contained(target)
        self._check_unprotected(target)
        return target

    def check_write(self, path: str | os.PathLike[str]) -> Path:
        target = self.resolve(path)
        self._check_contained(target)
        self._check_unprotected(target)
        return target

    def is_visible(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            self._check_contained(resolved)
            self._check_unprotected(resolved)
            return True
        except CodeToolError:
            return False

    def _containing_root(self, target: Path) -> Optional[Path]:
        for root in self.config.workspace_roots:
            try:
                target.relative_to(root)
                return root
            except ValueError:
                continue
        return None

    def _check_contained(self, target: Path) -> None:
        if self._containing_root(target) is None:
            raise CodeToolError("invalid_path", f"path is outside workspace roots: {target}")

    def _check_unprotected(self, target: Path) -> None:
        root = self._containing_root(target)
        if root is None:
            raise CodeToolError("invalid_path", f"path is outside workspace roots: {target}")
        for protected in self.config.protected_paths:
            protected_target = protected if protected.is_absolute() else root / protected
            protected_target = protected_target.resolve()
            try:
                target.relative_to(protected_target)
            except ValueError:
                continue
            raise CodeToolError("protected_path", f"path is within protected path: {protected}")


class _CodeTool(BaseTool):
    def __init__(self, config: CodeToolConfig):
        self.workspace = CodeWorkspace(config)
        self.config = config


class ListFilesTool(_CodeTool):
    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return (
            "List files under the code workspace. Paths are relative to the workspace root. "
            "Uses the project-bundled ripgrep binary when available, with a Python fallback."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "default": "."},
                "pattern": {"type": "string", "default": "*"},
                "recursive": {"type": "boolean", "default": False},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "include_ignored": {"type": "boolean", "default": False},
                "hidden": {"type": "boolean", "default": False},
                "files_only": {"type": "boolean", "default": False},
            },
        }

    async def execute(self, **kwargs) -> str:
        try:
            root = self.workspace.check_read(kwargs.get("path") or ".")
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("path") or "."))

        pattern = str(kwargs.get("pattern") or "*")
        recursive = bool(kwargs.get("recursive", False))
        include_ignored = bool(kwargs.get("include_ignored", False))
        hidden = bool(kwargs.get("hidden", False))
        files_only = bool(kwargs.get("files_only", False))
        max_results = max(1, min(int(kwargs.get("max_results") or 200), 2000))

        fallback_reason: Optional[str] = None
        if recursive or files_only:
            rg_path = _resolve_ripgrep_executable(self.config)
            if rg_path is not None:
                rg_payload, fallback_reason = _list_files_with_rg(
                    rg_path=rg_path,
                    workspace=self.workspace,
                    root=root,
                    pattern=pattern,
                    recursive=recursive,
                    include_ignored=include_ignored,
                    hidden=hidden,
                    files_only=files_only,
                    max_results=max_results,
                    timeout_seconds=self.config.command_timeout_seconds,
                )
                if rg_payload is not None:
                    return _json(rg_payload)
            else:
                fallback_reason = "ripgrep_not_installed"

        items, truncated = _list_files_python(
            workspace=self.workspace,
            root=root,
            pattern=pattern,
            recursive=recursive,
            include_ignored=include_ignored,
            hidden=hidden,
            files_only=files_only,
            max_results=max_results,
        )
        payload: Dict[str, Any] = {
            "root": self.workspace.relative(root),
            "items": items,
            "truncated": truncated,
            "engine": "python",
        }
        if fallback_reason:
            payload["fallback_reason"] = fallback_reason
        return _json(payload)


class ReadFileTool(_CodeTool):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read a UTF-8 text file from the code workspace."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
        }

    async def execute(self, **kwargs) -> str:
        try:
            target = self.workspace.check_read(kwargs.get("path"))
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("path") or ""))
        if not target.exists() or not target.is_file():
            return _error("not_found", "file not found", path=self.workspace.relative(target))
        offset = max(0, int(kwargs.get("offset") or 0))
        limit = max(1, min(int(kwargs.get("limit") or self.config.max_read_chars), self.config.max_read_chars))
        try:
            content, truncated = _read_text_window(target, offset=offset, limit=limit)
        except UnicodeDecodeError:
            return _error("not_utf8", "file is not valid UTF-8 text", path=self.workspace.relative(target))
        next_offset = offset + len(content)
        payload: Dict[str, Any] = {
            "path": self.workspace.relative(target),
            "content": content,
            "truncated": truncated,
        }
        if truncated:
            payload["next_offset"] = next_offset
        return _json(payload)


class SearchFilesTool(_CodeTool):
    @property
    def name(self) -> str:
        return "search_files"

    @property
    def description(self) -> str:
        return (
            "Search UTF-8 text files in the code workspace and return matching file lines. "
            "Uses the project-bundled ripgrep binary when available, with a Python fallback."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "glob": {"type": "string", "default": "*"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                "regex": {"type": "boolean", "default": False},
                "ignore_case": {"type": "boolean", "default": False},
                "include_ignored": {"type": "boolean", "default": False},
                "hidden": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs) -> str:
        query = str(kwargs.get("query") or "")
        if not query:
            return _error("invalid_query", "query is required")
        try:
            root = self.workspace.check_read(kwargs.get("path") or ".")
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("path") or "."))

        glob = str(kwargs.get("glob") or "*")
        max_results = max(1, min(int(kwargs.get("max_results") or 50), 500))
        regex = bool(kwargs.get("regex", False))
        ignore_case = bool(kwargs.get("ignore_case", False))
        include_ignored = bool(kwargs.get("include_ignored", False))
        hidden = bool(kwargs.get("hidden", False))

        fallback_reason: Optional[str] = None
        rg_path = _resolve_ripgrep_executable(self.config)
        if rg_path is not None:
            rg_payload, fallback_reason = _search_files_with_rg(
                rg_path=rg_path,
                workspace=self.workspace,
                root=root,
                query=query,
                glob=glob,
                max_results=max_results,
                regex=regex,
                ignore_case=ignore_case,
                include_ignored=include_ignored,
                hidden=hidden,
                timeout_seconds=self.config.command_timeout_seconds,
            )
            if rg_payload is not None:
                return _json(rg_payload)
            if fallback_reason and fallback_reason.startswith("ripgrep_invalid_regex:"):
                return _error("invalid_query", fallback_reason.split(":", 1)[1])
        else:
            fallback_reason = "ripgrep_not_installed"

        if regex:
            try:
                re.compile(query, re.IGNORECASE if ignore_case else 0)
            except re.error as exc:
                return _error("invalid_query", f"invalid regex: {exc}")

        payload = _search_files_python(
            workspace=self.workspace,
            root=root,
            query=query,
            glob=glob,
            max_results=max_results,
            regex=regex,
            ignore_case=ignore_case,
            include_ignored=include_ignored,
            hidden=hidden,
        )
        if fallback_reason:
            payload["fallback_reason"] = fallback_reason
        return _json(payload)


class EditFileTool(_CodeTool):
    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a UTF-8 file by replacing an exact old_string with new_string. "
            "By default old_string must occur exactly once."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False},
            },
            "required": ["path", "old_string", "new_string"],
        }

    async def execute(self, **kwargs) -> str:
        try:
            target = self.workspace.check_write(kwargs.get("path"))
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("path") or ""))
        if not target.exists() or not target.is_file():
            return _error("not_found", "file not found", path=self.workspace.relative(target))
        old_string = str(kwargs.get("old_string") or "")
        if not old_string:
            return _error("invalid_edit", "old_string is required", path=self.workspace.relative(target))
        new_string = str(kwargs.get("new_string") or "")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _error("not_utf8", "file is not valid UTF-8 text", path=self.workspace.relative(target))

        occurrences = text.count(old_string)
        if occurrences == 0:
            return _error("edit_not_found", "old_string was not found", path=self.workspace.relative(target))
        replace_all = bool(kwargs.get("replace_all", False))
        if occurrences > 1 and not replace_all:
            return _error(
                "edit_not_unique",
                "old_string occurs more than once; set replace_all=true or provide a more specific old_string",
                path=self.workspace.relative(target),
                occurrences=occurrences,
            )

        updated = text.replace(old_string, new_string, -1 if replace_all else 1)
        target.write_text(updated, encoding="utf-8")
        return _json({
            "path": self.workspace.relative(target),
            "replacements": occurrences if replace_all else 1,
            "bytes_written": len(updated.encode("utf-8")),
        })


class WriteFileTool(_CodeTool):
    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Create or overwrite a UTF-8 text file in the code workspace."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "create_parent_dirs": {"type": "boolean", "default": False},
            },
            "required": ["path", "content"],
        }

    async def execute(self, **kwargs) -> str:
        try:
            target = self.workspace.check_write(kwargs.get("path"))
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("path") or ""))
        create_parents = bool(kwargs.get("create_parent_dirs", False))
        if create_parents and not self.config.allow_parent_dir_creation:
            return _error("invalid_path", "parent directory creation is disabled", path=self.workspace.relative(target))
        if not target.parent.exists():
            if create_parents:
                target.parent.mkdir(parents=True, exist_ok=True)
            else:
                return _error("not_found", "parent directory does not exist", path=self.workspace.relative(target))
        content = str(kwargs.get("content") or "")
        target.write_text(content, encoding="utf-8")
        return _json({"path": self.workspace.relative(target), "bytes_written": len(content.encode("utf-8"))})


class RunCommandTool(_CodeTool):
    @property
    def name(self) -> str:
        return "run_command"

    @property
    def description(self) -> str:
        profile = ShellProfileResolver().resolve()
        return (
            "Run a synchronous-compatible development command in the code workspace. "
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
                "cwd": {"type": "string", "default": "."},
                "timeout_seconds": {"type": "integer", "minimum": 1},
                "task_id": {"type": "string", "description": "Optional TaskLedger task id to bind this command run."},
            },
            "required": ["command"],
        }

    async def execute(self, **kwargs) -> str:
        command = str(kwargs.get("command") or "")
        if not command.strip():
            return _error("command_failed", "command is required")
        try:
            cwd = self.workspace.check_read(kwargs.get("cwd") or ".")
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("cwd") or "."))
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
                runtime_context={
                    **runtime_context,
                    "task_id": kwargs.get("task_id") or runtime_context.get("task_id"),
                },
            )
        python_c_args = _windows_python_c_args(command)
        profile = ShellProfileResolver().resolve()
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                args=python_c_args or profile.command_argv(command),
                shell=False,
                cwd=str(cwd),
                env=_run_command_env(),
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return _json({
                "command": command,
                "cwd": self.workspace.relative(cwd),
                "exit_code": None,
                "stdout": _decode_output(exc.output, self.config.max_output_chars),
                "stderr": _decode_output(exc.stderr, self.config.max_output_chars),
                "timed_out": True,
            })
        except Exception as exc:
            return _error(
                type(exc).__name__,
                str(exc),
                tool_name=self.name,
                command=command,
                cwd=self.workspace.relative(cwd),
            )
        return _json({
            "command": command,
            "cwd": self.workspace.relative(cwd),
            "exit_code": proc.returncode,
            "stdout": _decode_output(proc.stdout, self.config.max_output_chars),
            "stderr": _decode_output(proc.stderr, self.config.max_output_chars),
            "timed_out": False,
        })

    async def _execute_managed(
        self,
        *,
        command: str,
        cwd: Path,
        timeout: int,
        runtime_context: Dict[str, Any],
    ) -> str:
        command_executor = runtime_context.get("command_executor")
        if command_executor is None or not hasattr(command_executor, "start"):
            return _error("missing_command_executor", "managed run_command requires a command executor")
        run = await command_executor.start(
            conversation_id=str(runtime_context.get("conversation_id") or ""),
            command=command,
            cwd=str(cwd),
            anchor_node_id=str(runtime_context.get("anchor_node_id") or runtime_context.get("node_id") or "") or None,
            parent_run_id=str(runtime_context.get("run_id") or "") or None,
            summary=command[:80],
            timeout_seconds=timeout,
            metadata={
                "tool_name": self.name,
                "tool_call_id": runtime_context.get("tool_call_id"),
                "workspace_relative_cwd": self.workspace.relative(cwd),
                "run_command_managed": True,
                "task_id": runtime_context.get("task_id"),
            },
        )
        run_id = str(run["run_id"])

        initial_wait = max(0.0, float(self.config.run_command_initial_wait_seconds))
        try:
            await command_executor.wait(run_id, timeout=initial_wait)
        except asyncio.TimeoutError:
            if hasattr(command_executor, "run_manager"):
                await command_executor.run_manager.update_metadata(run_id, {
                    "run_command_auto_backgrounded": True,
                    "run_command_initial_wait_seconds": initial_wait,
                })
            snapshot = command_executor.snapshot(run_id) or {}
            return _json(self._managed_background_payload(command, cwd, run_id, snapshot, auto_backgrounded=True))

        snapshot = command_executor.snapshot(run_id)
        if snapshot is None:
            return _error("not_found", "managed command run not found", command=command)
        if snapshot.get("status") in {"completed", "failed", "cancelled"} and hasattr(command_executor, "mark_observed"):
            await command_executor.mark_observed(
                run_id,
                observer_run_id=str(runtime_context.get("run_id") or "") or None,
                via=self.name,
            )
            snapshot = command_executor.snapshot(run_id) or snapshot
        return _json({
            "command": command,
            "cwd": self.workspace.relative(cwd),
            "exit_code": snapshot.get("exit_code"),
            "stdout": _decode_output(snapshot.get("stdout"), self.config.max_output_chars),
            "stderr": _decode_output(snapshot.get("stderr"), self.config.max_output_chars),
            "timed_out": False,
            "background": False,
            "managed": True,
            "kind": "command",
            "command_run_id": run_id,
            "run_id": run_id,
            "status": snapshot.get("status"),
            "shell": snapshot.get("shell"),
        })

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
        return {
            "command": command,
            "cwd": self.workspace.relative(cwd),
            "status": "running",
            "kind": "command",
            "command_run_id": run_id,
            "run_id": run_id,
            "background": True,
            "managed": True,
            "auto_backgrounded": auto_backgrounded,
            "shell": snapshot.get("shell"),
            "stdout_tail": snapshot.get("stdout_tail") or "",
            "stderr_tail": snapshot.get("stderr_tail") or "",
            "message": (
                f"Command is {action} as a managed side run. "
                "Use read_command to inspect it, wait_command only when this answer must join the result, or stop_command to cancel it."
            ),
        }


class ApplyPatchTool(_CodeTool):
    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return (
            "Apply a unified diff patch to existing UTF-8 files in the code workspace. "
            "Prefer edit_file for small exact replacements; use apply_patch for multi-line or multi-file changes."
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
        patch = str(kwargs.get("patch") or "")
        if not patch.strip():
            return _error("patch_failed", "patch is required")
        try:
            base = self.workspace.check_read(kwargs.get("cwd") or ".")
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("cwd") or "."))
        try:
            changed = _apply_simple_unified_patch(self.workspace, base, patch)
        except (CodeToolError, UnicodeDecodeError, ValueError) as exc:
            message = str(exc) or type(exc).__name__
            return _error("patch_failed", message)
        return _json({"applied": True, "files_changed": changed})


@dataclass(frozen=True)
class _PatchHunk:
    old_index: int
    remove: List[str]
    add: List[str]
    header: str


@dataclass(frozen=True)
class _FilePatch:
    path: str
    hunks: List[_PatchHunk]


def _patch_path(raw: str) -> str:
    path = raw.strip()
    if path == "/dev/null":
        raise ValueError("file deletion patches are not supported")
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    parsed = Path(path)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("patch path must stay inside the workspace")
    return path


def _read_text_window(target: Path, *, offset: int, limit: int) -> tuple[str, bool]:
    with target.open("r", encoding="utf-8") as handle:
        remaining = offset
        while remaining > 0:
            chunk = handle.read(min(remaining, TEXT_READ_CHUNK_CHARS))
            if not chunk:
                return "", False
            remaining -= len(chunk)
        content = handle.read(limit + 1)
    if len(content) > limit:
        return content[:limit], True
    return content, False


def _apply_simple_unified_patch(workspace: CodeWorkspace, base: Path, patch: str) -> List[str]:
    file_patches = _parse_unified_patch(patch)
    changed: List[str] = []
    for file_patch in file_patches:
        target = workspace.check_write(base / file_patch.path)
        if not target.exists():
            raise ValueError(f"target file does not exist: {file_patch.path}")
        _apply_file_patch_streaming(target, file_patch)
        changed.append(workspace.relative(target))
    if not changed:
        raise ValueError("no file changes found in patch")
    return changed


def _parse_unified_patch(patch: str) -> List[_FilePatch]:
    lines = patch.splitlines()
    file_patches: List[_FilePatch] = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- "):
            i += 1
            continue
        old_path = _patch_path(lines[i][4:].split("\t", 1)[0].strip())
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise ValueError("invalid unified diff header")
        new_path = _patch_path(lines[i][4:].split("\t", 1)[0].strip())
        if old_path != new_path:
            raise ValueError("renames are not supported")
        hunks: List[_PatchHunk] = []
        i += 1
        while i < len(lines) and lines[i].startswith("@@"):
            hunk_header = lines[i]
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", hunk_header)
            if not match:
                raise ValueError("invalid hunk header")
            old_index = int(match.group(1)) - 1
            i += 1
            remove: List[str] = []
            add: List[str] = []
            while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("--- "):
                if lines[i] == r"\ No newline at end of file":
                    i += 1
                    continue
                marker = lines[i][:1]
                value = lines[i][1:]
                if marker == " ":
                    remove.append(value)
                    add.append(value)
                elif marker == "-":
                    remove.append(value)
                elif marker == "+":
                    add.append(value)
                else:
                    raise ValueError("invalid hunk line")
                i += 1
            hunks.append(_PatchHunk(old_index=old_index, remove=remove, add=add, header=hunk_header))
        file_patches.append(_FilePatch(path=new_path, hunks=hunks))
    if not file_patches:
        raise ValueError("no file changes found in patch")
    return file_patches


def _apply_file_patch_streaming(target: Path, file_patch: _FilePatch) -> None:
    resolved_hunks = _resolve_patch_hunk_offsets(target, file_patch)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=str(target.parent),
            delete=False,
        ) as output:
            temp_name = output.name
            _rewrite_patch_stream(target, output, resolved_hunks, file_patch.path)
        os.replace(temp_name, target)
    except Exception:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _resolve_patch_hunk_offsets(target: Path, file_patch: _FilePatch) -> List[_PatchHunk]:
    resolved: List[_PatchHunk] = []
    for hunk in file_patch.hunks:
        old_index = hunk.old_index
        if hunk.remove and not _stream_lines_match_at(target, old_index, hunk.remove):
            old_index = _find_unique_hunk_offset_streaming(target, hunk.remove, file_patch.path, hunk.header)
        resolved.append(_PatchHunk(old_index=old_index, remove=hunk.remove, add=hunk.add, header=hunk.header))

    resolved.sort(key=lambda item: item.old_index)
    previous_end = 0
    for hunk in resolved:
        if hunk.old_index < previous_end:
            raise ValueError(f"overlapping hunks are not supported: {file_patch.path}; {hunk.header}")
        previous_end = hunk.old_index + len(hunk.remove)
    return resolved


def _stream_lines_match_at(target: Path, old_index: int, expected: List[str]) -> bool:
    if not expected:
        return True
    with target.open("r", encoding="utf-8") as handle:
        for current_index, raw_line in enumerate(handle):
            if current_index < old_index:
                continue
            expected_index = current_index - old_index
            if expected_index >= len(expected):
                return True
            if _strip_line_ending(raw_line) != expected[expected_index]:
                return False
        return False


def _find_unique_hunk_offset_streaming(target: Path, expected: List[str], path: str, hunk_header: str) -> int:
    if not expected:
        raise ValueError(
            f"hunk does not match target file: {path}; {hunk_header}; empty context cannot be relocated"
        )

    matches: List[int] = []
    window: List[str] = []
    with target.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle):
            window.append(_strip_line_ending(raw_line))
            if len(window) < len(expected):
                continue
            if len(window) > len(expected):
                window.pop(0)
            if window == expected:
                matches.append(line_number - len(expected) + 1)
                if len(matches) > 5:
                    break
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        lines = ", ".join(str(index + 1) for index in matches[:5])
        suffix = "..." if len(matches) > 5 else ""
        raise ValueError(
            f"hunk does not match target file: {path}; {hunk_header}; "
            f"multiple matching locations found at lines {lines}{suffix}"
        )
    raise ValueError(
        f"hunk does not match target file: {path}; {hunk_header}; no matching context found"
    )


def _rewrite_patch_stream(target: Path, output, hunks: List[_PatchHunk], path: str) -> None:
    hunk_index = 0
    current_index = 0
    with target.open("r", encoding="utf-8") as source:
        while True:
            next_hunk = hunks[hunk_index] if hunk_index < len(hunks) else None
            if next_hunk is not None and current_index == next_hunk.old_index:
                _consume_expected_lines(source, next_hunk.remove, path, next_hunk.header)
                for line in next_hunk.add:
                    output.write(line + "\n")
                current_index += len(next_hunk.remove)
                hunk_index += 1
                continue

            raw_line = source.readline()
            if raw_line == "":
                break
            output.write(_strip_line_ending(raw_line) + "\n")
            current_index += 1

        while hunk_index < len(hunks):
            hunk = hunks[hunk_index]
            if hunk.old_index > current_index or hunk.remove:
                raise ValueError(f"hunk does not match target file: {path}; {hunk.header}")
            for line in hunk.add:
                output.write(line + "\n")
            hunk_index += 1


def _consume_expected_lines(source, expected: List[str], path: str, hunk_header: str) -> None:
    for expected_line in expected:
        raw_line = source.readline()
        if raw_line == "" or _strip_line_ending(raw_line) != expected_line:
            raise ValueError(f"hunk does not match target file: {path}; {hunk_header}")


def _strip_line_ending(line: str) -> str:
    return line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line


def _resolve_ripgrep_executable(config: CodeToolConfig) -> Optional[Path]:
    executable = "rg.exe" if os.name == "nt" else "rg"
    platform_dir = _ripgrep_platform_dir()
    candidates = [
        config.ripgrep_install_dir / config.ripgrep_version / platform_dir / executable,
        config.ripgrep_install_dir / config.ripgrep_version / platform_dir / "rg",
        config.ripgrep_install_dir / platform_dir / executable,
        config.ripgrep_install_dir / platform_dir / "rg",
        config.ripgrep_install_dir / executable,
        config.ripgrep_install_dir / "rg",
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if os.name != "nt" and not os.access(candidate, os.X_OK):
            continue
        return candidate
    return None


def _ripgrep_platform_dir() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    if system == "windows":
        return f"win32-{arch}"
    if system == "darwin":
        return f"darwin-{arch}"
    if system == "linux":
        return f"linux-{arch}"
    return f"{system or 'unknown'}-{arch}"


def _search_files_with_rg(
    *,
    rg_path: Path,
    workspace: CodeWorkspace,
    root: Path,
    query: str,
    glob: str,
    max_results: int,
    regex: bool,
    ignore_case: bool,
    include_ignored: bool,
    hidden: bool,
    timeout_seconds: int,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if root.is_file() and not _matches_glob(root, glob):
        return ({
            "query": query,
            "matches": [],
            "searched_files": 0,
            "skipped_non_utf8": [],
            "truncated": False,
            "engine": "rg",
        }, None)

    cwd = root if root.is_dir() else root.parent
    target = "." if root.is_dir() else root.name
    argv = [
        str(rg_path),
        "--json",
        "--color",
        "never",
        "--no-config",
        "--line-number",
    ]
    if not regex:
        argv.append("--fixed-strings")
    if ignore_case:
        argv.append("--ignore-case")
    if include_ignored:
        argv.append("--no-ignore")
    if hidden:
        argv.append("--hidden")
    if glob and glob != "*":
        argv.extend(["--glob", glob])
    argv.extend(["--", query, target])

    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=_run_command_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return None, "ripgrep_not_installed"
    except subprocess.TimeoutExpired:
        return None, "ripgrep_timeout"
    except OSError as exc:
        return None, f"ripgrep_failed:{type(exc).__name__}"

    if proc.returncode not in {0, 1}:
        return None, _ripgrep_failure_reason(proc.stderr)

    matches: List[Dict[str, Any]] = []
    skipped_files: set[str] = set()
    searched_paths: set[str] = set()
    truncated = False
    for raw_line in proc.stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return None, "ripgrep_invalid_json"
        if event.get("type") != "match":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        path_text = _rg_json_text(data.get("path"))
        if not path_text:
            continue
        resolved = (cwd / path_text).resolve()
        if not resolved.is_file() or not workspace.is_visible(resolved):
            continue
        relative_path = workspace.relative(resolved)
        searched_paths.add(relative_path)
        line_text = _rg_json_text(data.get("lines"))
        if line_text is None:
            skipped_files.add(relative_path)
            continue
        matches.append({
            "path": relative_path,
            "line": int(data.get("line_number") or 0),
            "preview": line_text.strip(),
        })
        if len(matches) >= max_results:
            truncated = True
            break

    return ({
        "query": query,
        "matches": matches,
        "searched_files": len(searched_paths),
        "skipped_non_utf8": sorted(skipped_files),
        "truncated": truncated,
        "engine": "rg",
    }, None)


def _list_files_with_rg(
    *,
    rg_path: Path,
    workspace: CodeWorkspace,
    root: Path,
    pattern: str,
    recursive: bool,
    include_ignored: bool,
    hidden: bool,
    files_only: bool,
    max_results: int,
    timeout_seconds: int,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if root.is_file():
        if _matches_glob(root, pattern):
            item = _list_item(workspace, root)
            return ({
                "root": workspace.relative(root),
                "items": [item] if item else [],
                "truncated": False,
                "engine": "rg",
            }, None)
        return ({
            "root": workspace.relative(root),
            "items": [],
            "truncated": False,
            "engine": "rg",
        }, None)

    argv = [str(rg_path), "--files", "--color", "never", "--no-config"]
    if include_ignored:
        argv.append("--no-ignore")
    if hidden:
        argv.append("--hidden")
    if not recursive:
        argv.extend(["--max-depth", "1"])
    if pattern and pattern != "*":
        argv.extend(["--glob", pattern])
    argv.append(".")

    try:
        proc = subprocess.run(
            argv,
            cwd=str(root),
            env=_run_command_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return None, "ripgrep_not_installed"
    except subprocess.TimeoutExpired:
        return None, "ripgrep_timeout"
    except OSError as exc:
        return None, f"ripgrep_failed:{type(exc).__name__}"

    if proc.returncode not in {0, 1}:
        return None, _ripgrep_failure_reason(proc.stderr)

    items_by_path: Dict[str, Dict[str, Any]] = {}
    for raw_line in proc.stdout.splitlines():
        text = raw_line.strip()
        if not text:
            continue
        resolved = (root / text).resolve()
        if not resolved.is_file() or not workspace.is_visible(resolved):
            continue
        if not files_only:
            _add_parent_dirs(workspace, root, resolved, pattern, items_by_path)
        item = _list_item(workspace, resolved)
        if item:
            items_by_path[item["path"]] = item

    items = list(items_by_path.values())
    truncated = len(items) > max_results
    return ({
        "root": workspace.relative(root),
        "items": items[:max_results],
        "truncated": truncated,
        "engine": "rg",
    }, None)


def _search_files_python(
    *,
    workspace: CodeWorkspace,
    root: Path,
    query: str,
    glob: str,
    max_results: int,
    regex: bool,
    ignore_case: bool,
    include_ignored: bool,
    hidden: bool,
) -> Dict[str, Any]:
    matches: List[Dict[str, Any]] = []
    searched_files = 0
    skipped_files: List[str] = []
    matcher = _compile_python_matcher(query, regex=regex, ignore_case=ignore_case)
    ignore_matcher = _GitIgnoreMatcher.for_root(root, workspace)

    for file_path in _iter_search_files(root, glob):
        resolved = file_path.resolve()
        if (
            not resolved.is_file()
            or not workspace.is_visible(resolved)
            or _should_skip_python_path(resolved, root, hidden=hidden, include_ignored=include_ignored, ignore_matcher=ignore_matcher)
        ):
            continue
        searched_files += 1
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped_files.append(workspace.relative(resolved))
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not matcher(line):
                continue
            matches.append({
                "path": workspace.relative(resolved),
                "line": line_number,
                "preview": line.strip(),
            })
            if len(matches) >= max_results:
                return {
                    "query": query,
                    "matches": matches,
                    "searched_files": searched_files,
                    "skipped_non_utf8": skipped_files,
                    "truncated": True,
                    "engine": "python",
                }

    return {
        "query": query,
        "matches": matches,
        "searched_files": searched_files,
        "skipped_non_utf8": skipped_files,
        "truncated": False,
        "engine": "python",
    }


def _list_files_python(
    *,
    workspace: CodeWorkspace,
    root: Path,
    pattern: str,
    recursive: bool,
    include_ignored: bool,
    hidden: bool,
    files_only: bool,
    max_results: int,
) -> tuple[List[Dict[str, Any]], bool]:
    ignore_matcher = _GitIgnoreMatcher.for_root(root, workspace)
    if root.is_file():
        if files_only and not root.is_file():
            return [], False
        if not _matches_glob(root, pattern):
            return [], False
        item = _list_item(workspace, root)
        return ([item] if item else []), False

    iterator = root.rglob(pattern) if recursive else root.glob(pattern)
    items: List[Dict[str, Any]] = []
    for item_path in iterator:
        resolved = item_path.resolve()
        if files_only and not resolved.is_file():
            continue
        if (
            not workspace.is_visible(resolved)
            or _should_skip_python_path(resolved, root, hidden=hidden, include_ignored=include_ignored, ignore_matcher=ignore_matcher)
        ):
            continue
        item = _list_item(workspace, resolved)
        if item is None:
            continue
        items.append(item)
        if len(items) >= max_results:
            return items, True
    return items, False


def _iter_search_files(root: Path, glob: str) -> Iterable[Path]:
    if root.is_file():
        if _matches_glob(root, glob):
            yield root
        return
    yield from root.rglob(glob)


def _compile_python_matcher(query: str, *, regex: bool, ignore_case: bool):
    if regex:
        flags = re.IGNORECASE if ignore_case else 0
        compiled = re.compile(query, flags)
        return lambda line: compiled.search(line) is not None
    if ignore_case:
        needle = query.lower()
        return lambda line: needle in line.lower()
    return lambda line: query in line


def _matches_glob(path: Path, pattern: str) -> bool:
    return fnmatch(path.name, pattern) or fnmatch(path.as_posix(), pattern)


def _list_item(workspace: CodeWorkspace, path: Path) -> Optional[Dict[str, Any]]:
    if not workspace.is_visible(path):
        return None
    return {
        "path": workspace.relative(path),
        "type": "dir" if path.is_dir() else "file",
        "size": path.stat().st_size if path.is_file() else None,
    }


def _add_parent_dirs(
    workspace: CodeWorkspace,
    root: Path,
    file_path: Path,
    pattern: str,
    items_by_path: Dict[str, Dict[str, Any]],
) -> None:
    parent = file_path.parent
    parents: List[Path] = []
    while parent != root and _is_relative_to(parent, root):
        parents.append(parent)
        parent = parent.parent
    for directory in reversed(parents):
        if pattern != "*" and not _matches_glob(directory, pattern):
            continue
        item = _list_item(workspace, directory)
        if item:
            items_by_path.setdefault(item["path"], item)


def _rg_json_text(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    if isinstance(text, str):
        return text
    raw_bytes = value.get("bytes")
    if isinstance(raw_bytes, str):
        try:
            return base64.b64decode(raw_bytes).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return None


def _ripgrep_failure_reason(stderr: str) -> str:
    stderr_text = stderr or ""
    if "regex parse error" in stderr_text.lower():
        message = " ".join(line.strip() for line in stderr_text.splitlines() if line.strip())
        return f"ripgrep_invalid_regex:{message[:200]}"
    message = (stderr or "").strip().splitlines()
    if message:
        first_line = message[0][:120].replace("\n", " ")
        return f"ripgrep_failed:{first_line}"
    return "ripgrep_failed"


def _should_skip_python_path(
    path: Path,
    root: Path,
    *,
    hidden: bool,
    include_ignored: bool,
    ignore_matcher: "_GitIgnoreMatcher",
) -> bool:
    if not hidden and _is_hidden_under(path, root):
        return True
    if not include_ignored and ignore_matcher.matches(path):
        return True
    return False


def _is_hidden_under(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = Path(path.name)
    return any(part.startswith(".") for part in relative.parts if part not in {"", "."})


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class _GitIgnoreMatcher:
    def __init__(self, root: Path, patterns: List[str]):
        self.root = root.resolve()
        self.patterns = patterns

    @classmethod
    def for_root(cls, root: Path, workspace: CodeWorkspace) -> "_GitIgnoreMatcher":
        workspace_root = workspace._containing_root(root.resolve()) or workspace.default_root
        gitignore = workspace_root / ".gitignore"
        patterns: List[str] = []
        try:
            for line in gitignore.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                patterns.append(stripped)
        except (OSError, UnicodeDecodeError):
            pass
        return cls(workspace_root, patterns)

    def matches(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return False
        ignored = False
        for raw_pattern in self.patterns:
            negated = raw_pattern.startswith("!")
            pattern = raw_pattern[1:] if negated else raw_pattern
            if self._matches_pattern(rel, pattern):
                ignored = not negated
        return ignored

    def _matches_pattern(self, rel: str, pattern: str) -> bool:
        pattern = pattern.strip()
        if not pattern:
            return False
        anchored = pattern.startswith("/")
        pattern = pattern.lstrip("/")
        directory_only = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        if not pattern:
            return False
        if directory_only:
            return rel == pattern or rel.startswith(pattern + "/")
        if "/" in pattern or anchored:
            return fnmatch(rel, pattern) or rel.startswith(pattern + "/")
        parts = rel.split("/")
        return any(fnmatch(part, pattern) for part in parts) or fnmatch(rel, pattern)


def _find_unique_hunk_offset(text_lines: List[str], remove: List[str], path: str, hunk_header: str) -> int:
    if not remove:
        raise ValueError(
            f"hunk does not match target file: {path}; {hunk_header}; empty context cannot be relocated"
        )

    matches = [
        index
        for index in range(0, len(text_lines) - len(remove) + 1)
        if text_lines[index:index + len(remove)] == remove
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        lines = ", ".join(str(index + 1) for index in matches[:5])
        suffix = "..." if len(matches) > 5 else ""
        raise ValueError(
            f"hunk does not match target file: {path}; {hunk_header}; "
            f"multiple matching locations found at lines {lines}{suffix}"
        )
    raise ValueError(
        f"hunk does not match target file: {path}; {hunk_header}; no matching context found"
    )
