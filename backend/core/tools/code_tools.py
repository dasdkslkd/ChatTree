from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class CodeToolConfig:
    workspace_roots: List[Path]
    protected_paths: List[Path]
    command_timeout_seconds: int = 120
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
            max_read_chars=int(cfg.get("max_read_chars", 20000)),
            max_output_chars=int(cfg.get("max_output_chars", 60000)),
            allow_parent_dir_creation=bool(cfg.get("allow_parent_dir_creation", False)),
        )


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
        return "Run a development command in the code workspace and return stdout, stderr, and exit code."

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
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
            timed_out = True
        stdout = stdout_b.decode("utf-8", errors="replace")[:self.config.max_output_chars]
        stderr = stderr_b.decode("utf-8", errors="replace")[:self.config.max_output_chars]
        return _json({
            "command": command,
            "cwd": self.workspace.relative(cwd),
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
        })


class ApplyPatchTool(_CodeTool):
    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return "Apply a simple unified diff patch to existing UTF-8 files in the code workspace."

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
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", lines[i])
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
                raise ValueError(f"hunk does not match target file: {new_path}")
            text_lines[old_index:old_index + len(remove)] = add
        target.write_text("\n".join(text_lines) + "\n", encoding="utf-8")
        changed.append(workspace.relative(target))
    if not changed:
        raise ValueError("no file changes found in patch")
    return changed
