from __future__ import annotations

import asyncio
import json
import locale
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import BaseTool
from .security.logical_sandbox import DEFAULT_PROTECTED_PATHS


DEFAULT_CODE_WORKSPACE = r"D:\Workspace\ChatTree\tmp"


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


def _windows_multiline_python_c_args(command: str) -> Optional[List[str]]:
    if os.name != "nt" or "\n" not in command:
        return None
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return None
    if len(parts) != 3 or parts[1] != "-c" or "\n" not in parts[2]:
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
    run_command_initial_wait_seconds: float = 3.0
    max_read_chars: int = 20000
    max_output_chars: int = 60000
    allow_parent_dir_creation: bool = False

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]] = None) -> "CodeToolConfig":
        cfg = raw or {}
        roots = cfg.get("workspace_roots") or [DEFAULT_CODE_WORKSPACE]
        protected = cfg.get("protected_paths") or DEFAULT_PROTECTED_PATHS
        return cls(
            workspace_roots=[Path(root).expanduser().resolve() for root in roots],
            protected_paths=[Path(path) for path in protected],
            command_timeout_seconds=int(cfg.get("command_timeout_seconds", 120)),
            run_command_initial_wait_seconds=float(cfg.get("run_command_initial_wait_seconds", 3.0)),
            max_read_chars=int(cfg.get("max_read_chars", 20000)),
            max_output_chars=int(cfg.get("max_output_chars", 60000)),
            allow_parent_dir_creation=bool(cfg.get("allow_parent_dir_creation", False)),
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
        return "List files under the code workspace. Paths are relative to the workspace root."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "default": "."},
                "pattern": {"type": "string", "default": "*"},
                "recursive": {"type": "boolean", "default": False},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            },
        }

    async def execute(self, **kwargs) -> str:
        try:
            root = self.workspace.check_read(kwargs.get("path") or ".")
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("path") or "."))

        pattern = str(kwargs.get("pattern") or "*")
        max_results = max(1, min(int(kwargs.get("max_results") or 200), 1000))
        iterator = root.rglob(pattern) if bool(kwargs.get("recursive", False)) else root.glob(pattern)
        items = []
        for item in sorted(iterator, key=lambda p: p.as_posix()):
            resolved = item.resolve()
            if not self.workspace.is_visible(resolved):
                continue
            items.append({
                "path": self.workspace.relative(resolved),
                "type": "dir" if resolved.is_dir() else "file",
                "size": resolved.stat().st_size if resolved.is_file() else None,
            })
            if len(items) >= max_results:
                return _json({"root": self.workspace.relative(root), "items": items, "truncated": True})
        return _json({"root": self.workspace.relative(root), "items": items, "truncated": False})


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
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _error("not_utf8", "file is not valid UTF-8 text", path=self.workspace.relative(target))
        offset = max(0, int(kwargs.get("offset") or 0))
        limit = max(1, min(int(kwargs.get("limit") or self.config.max_read_chars), self.config.max_read_chars))
        content = text[offset:offset + limit]
        next_offset = offset + len(content)
        truncated = next_offset < len(text)
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
        return "Search UTF-8 text files in the code workspace and return matching file lines."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "glob": {"type": "string", "default": "*"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
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
        max_results = max(1, min(int(kwargs.get("max_results") or 50), 200))
        matches: List[Dict[str, Any]] = []
        searched_files = 0
        skipped_files: List[str] = []

        for file_path in _iter_search_files(root, glob):
            resolved = file_path.resolve()
            if not resolved.is_file() or not self.workspace.is_visible(resolved):
                continue
            searched_files += 1
            try:
                text = resolved.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                skipped_files.append(self.workspace.relative(resolved))
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query not in line:
                    continue
                matches.append({
                    "path": self.workspace.relative(resolved),
                    "line": line_number,
                    "preview": line.strip(),
                })
                if len(matches) >= max_results:
                    return _json({
                        "query": query,
                        "matches": matches,
                        "searched_files": searched_files,
                        "skipped_non_utf8": skipped_files,
                        "truncated": True,
                    })

        return _json({
            "query": query,
            "matches": matches,
            "searched_files": searched_files,
            "skipped_non_utf8": skipped_files,
            "truncated": False,
        })


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
        return (
            "Run a synchronous-compatible development command in the code workspace. "
            "When ChatTree runtime context is available, the command starts foreground, returns stdout/stderr/exit_code if it finishes within the initial wait window, "
            "and auto-backgrounds with a command_run_id if it keeps running."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "default": "."},
                "timeout_seconds": {"type": "integer", "minimum": 1},
                "background": {
                    "type": "boolean",
                    "default": False,
                    "description": "compatibility alias for starting a managed background command immediately. Prefer start_background_command for new true background command work.",
                },
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
        if isinstance(runtime_context, dict) and runtime_context.get("terminal_executor") is not None:
            return await self._execute_managed(
                command=command,
                cwd=cwd,
                timeout=timeout,
                background=bool(kwargs.get("background", False)),
                runtime_context=runtime_context,
            )
        if bool(kwargs.get("background", False)):
            return _error("missing_terminal_executor", "background run_command requires a terminal executor")
        python_c_args = _windows_multiline_python_c_args(command)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                args=python_c_args or command,
                shell=python_c_args is None,
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
        background: bool,
        runtime_context: Dict[str, Any],
    ) -> str:
        terminal_executor = runtime_context.get("terminal_executor")
        if terminal_executor is None or not hasattr(terminal_executor, "start"):
            return _error("missing_terminal_executor", "managed run_command requires a terminal executor")
        run = await terminal_executor.start(
            conversation_id=str(runtime_context.get("conversation_id") or ""),
            command=command,
            cwd=str(cwd),
            anchor_node_id=str(runtime_context.get("node_id") or "") or None,
            parent_run_id=str(runtime_context.get("run_id") or "") or None,
            summary=command[:80],
            timeout_seconds=timeout,
            metadata={
                "tool_name": self.name,
                "tool_call_id": runtime_context.get("tool_call_id"),
                "workspace_relative_cwd": self.workspace.relative(cwd),
                "run_command_managed": True,
                "run_command_background_requested": background,
            },
        )
        run_id = str(run["run_id"])
        if background:
            snapshot = terminal_executor.snapshot(run_id) or {}
            return _json(self._managed_background_payload(command, cwd, run_id, snapshot, auto_backgrounded=False))

        initial_wait = max(0.0, float(self.config.run_command_initial_wait_seconds))
        try:
            await terminal_executor.wait(run_id, timeout=initial_wait)
        except asyncio.TimeoutError:
            snapshot = terminal_executor.snapshot(run_id) or {}
            return _json(self._managed_background_payload(command, cwd, run_id, snapshot, auto_backgrounded=True))

        snapshot = terminal_executor.snapshot(run_id)
        if snapshot is None:
            return _error("not_found", "managed command run not found", command=command)
        if snapshot.get("status") in {"completed", "failed", "cancelled"} and hasattr(terminal_executor, "mark_observed"):
            await terminal_executor.mark_observed(
                run_id,
                observer_run_id=str(runtime_context.get("run_id") or "") or None,
                via=self.name,
            )
            snapshot = terminal_executor.snapshot(run_id) or snapshot
        return _json({
            "command": command,
            "cwd": self.workspace.relative(cwd),
            "exit_code": snapshot.get("exit_code"),
            "stdout": _decode_output(snapshot.get("stdout"), self.config.max_output_chars),
            "stderr": _decode_output(snapshot.get("stderr"), self.config.max_output_chars),
            "timed_out": False,
            "background": False,
            "managed": True,
            "kind": "terminal",
            "command_run_id": run_id,
            "terminal_run_id": run_id,
            "run_id": run_id,
            "status": snapshot.get("status"),
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
            "kind": "terminal",
            "command_run_id": run_id,
            "terminal_run_id": run_id,
            "run_id": run_id,
            "background": True,
            "managed": True,
            "auto_backgrounded": auto_backgrounded,
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


def _apply_simple_unified_patch(workspace: CodeWorkspace, base: Path, patch: str) -> List[str]:
    lines = patch.splitlines()
    changed: List[str] = []
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
        target = workspace.check_write(base / new_path)
        if not target.exists():
            raise ValueError(f"target file does not exist: {new_path}")
        text_lines = target.read_text(encoding="utf-8").splitlines()
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
            if text_lines[old_index:old_index + len(remove)] != remove:
                old_index = _find_unique_hunk_offset(text_lines, remove, new_path, hunk_header)
            text_lines[old_index:old_index + len(remove)] = add
        target.write_text("\n".join(text_lines) + "\n", encoding="utf-8")
        changed.append(workspace.relative(target))
    if not changed:
        raise ValueError("no file changes found in patch")
    return changed


def _iter_search_files(root: Path, glob: str):
    if root.is_file():
        if fnmatch(root.name, glob):
            yield root
        return
    yield from sorted(root.rglob(glob), key=lambda p: p.as_posix())


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
