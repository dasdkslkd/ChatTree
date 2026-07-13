from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import locale
import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .base import BaseTool
from .security.logical_sandbox import DEFAULT_PROTECTED_PATHS
from .task_contract import task_step_parameter_schema
from ..persistence.home import resolve_chattree_home
from ..runs.types import FINISHED_RUN_STATUSES
from ..shell_profile import ShellProfileResolver, render_command_tool_guidance


DEFAULT_CODE_WORKSPACE = r"D:\Workspace\ChatTree\tmp"
DEFAULT_RIPGREP_VERSION = "15.1.0"
TEXT_READ_CHUNK_CHARS = 8192
FINISHED_STATUS_VALUES = {status.value for status in FINISHED_RUN_STATUSES}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_ripgrep_install_dir() -> Path:
    return resolve_chattree_home() / "tools" / "ripgrep"


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


def _shell_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _should_suppress_command_notification(runtime_context: Dict[str, Any]) -> bool:
    if runtime_context.get("suppress_task_notification") is True:
        return True
    if runtime_context.get("agent_name") == "workflow-worker":
        return True
    if runtime_context.get("delivery_policy") == "silent":
        return True
    return runtime_context.get("run_kind") in {"workflow", "workflow_step"}


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
    shell_initial_wait_seconds: float = 120.0
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
            ripgrep_install_path = resolve_chattree_home() / ripgrep_install_path
        return cls(
            workspace_roots=[Path(root).expanduser().resolve() for root in roots],
            protected_paths=[Path(path) for path in protected],
            command_timeout_seconds=int(cfg.get("command_timeout_seconds", 120)),
            shell_initial_wait_seconds=float(cfg.get("shell_initial_wait_seconds", 120.0)),
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
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Find workspace files with ripgrep-style file listing. Use `pattern` for one glob, `patterns` for multiple globs, "
            "and `path_regex` to match returned paths; do not use a `query` argument. "
            "Use this instead of shell for ls/dir/find/Get-ChildItem/rg --files. Paths are returned relative to the workspace root with / separators."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "default": "."},
                "patterns": {"type": "array", "items": {"type": "string"}, "default": ["**/*"]},
                "pattern": {"type": "string"},
                "path_regex": {"type": "string"},
                "files_only": {"type": "boolean", "default": True},
                "include_hidden": {"type": "boolean", "default": False},
                "respect_gitignore": {"type": "boolean", "default": True},
                "exclude": {"type": "array", "items": {"type": "string"}, "default": []},
                "sort": {"type": "string", "enum": ["path", "mtime"], "default": "path"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
        }

    async def execute(self, **kwargs) -> str:
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
        try:
            root = self.workspace.check_read(kwargs.get("path") or ".")
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("path") or "."))

        patterns = _string_list(kwargs.get("patterns"))
        single_pattern = str(kwargs.get("pattern") or "").strip()
        if single_pattern:
            patterns = [single_pattern]
        if not patterns:
            patterns = ["**/*"]
        path_regex = str(kwargs.get("path_regex") or "").strip()
        try:
            compiled_path_regex = re.compile(path_regex) if path_regex else None
        except re.error as exc:
            return _error("invalid_query", f"invalid path_regex: {exc}")
        files_only = bool(kwargs.get("files_only", True))
        include_hidden = bool(kwargs.get("include_hidden", False))
        respect_gitignore = bool(kwargs.get("respect_gitignore", True))
        exclude_globs = _string_list(kwargs.get("exclude"))
        sort = str(kwargs.get("sort") or "path")
        limit = max(1, min(int(kwargs.get("limit") or 200), 2000))
        offset = max(0, int(kwargs.get("offset") or 0))

        rg_path = _resolve_ripgrep_executable(self.config)
        fallback_reason = None
        if rg_path is not None and files_only:
            rg_result, fallback_reason = _glob_files_with_rg(
                rg_path=rg_path,
                workspace=self.workspace,
                root=root,
                patterns=patterns,
                path_regex=compiled_path_regex,
                respect_gitignore=respect_gitignore,
                include_hidden=include_hidden,
                exclude_globs=exclude_globs,
                sort=sort,
                limit=limit,
                offset=offset,
                timeout_seconds=self.config.command_timeout_seconds,
            )
            if rg_result is not None:
                return _json(rg_result)

        files, truncated, scanned_entries, total = _glob_files_python(
            workspace=self.workspace,
            root=root,
            patterns=patterns,
            path_regex=compiled_path_regex,
            respect_gitignore=respect_gitignore,
            include_hidden=include_hidden,
            files_only=files_only,
            exclude_globs=exclude_globs,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        payload: Dict[str, Any] = {
            "root": self.workspace.relative(root),
            "files": files,
            "count": len(files),
            "total": total,
            "truncated": truncated,
            "next_offset": offset + len(files) if truncated else None,
            "engine": "python",
            "scanned_entries": scanned_entries,
        }
        if fallback_reason:
            payload["fallback_reason"] = fallback_reason
        return _json(payload)


class ReadFileTool(_CodeTool):
    def __init__(self, config: CodeToolConfig, tool_result_store: Any = None):
        super().__init__(config)
        self._tool_result_store = tool_result_store

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "Read UTF-8 workspace files or a persisted tool-result slice. Use this instead of shell for cat/head/tail/type/Get-Content/sed. "
            "File reads support one or more line ranges and return numbered lines by default."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "source": {"type": "string", "enum": ["file", "tool_result"], "default": "file"},
                "id": {"type": "string", "description": "Persisted tool result id when source is tool_result."},
                "tool_result_id": {"type": "string", "description": "Persisted tool result id when source is tool_result."},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1},
                "targets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer", "minimum": 1, "default": 1},
                            "line_count": {"type": "integer", "minimum": 1},
                            "max_chars": {"type": "integer", "minimum": 1},
                        },
                        "required": ["path"],
                    },
                },
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "line_count": {"type": "integer", "minimum": 1},
                "max_chars_per_file": {"type": "integer", "minimum": 1},
                "format": {"type": "string", "enum": ["numbered", "raw", "json"], "default": "numbered"},
            },
        }

    async def execute(self, **kwargs) -> str:
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
        if str(kwargs.get("source") or "").lower() == "tool_result" or kwargs.get("tool_result_id") or (
            kwargs.get("id") and not kwargs.get("path") and not kwargs.get("targets")
        ):
            return self._read_tool_result(kwargs)
        targets = _read_targets(kwargs)
        if not targets:
            return _error("invalid_path", "path or targets is required")
        output_format = str(kwargs.get("format") or "numbered")
        files: List[Dict[str, Any]] = []
        for target_spec in targets:
            raw_path = target_spec["path"]
            try:
                target = self.workspace.check_read(raw_path)
            except CodeToolError as exc:
                files.append({"path": str(raw_path), "error": {"type": exc.error_type, "message": str(exc)}})
                continue
            start_line = max(1, int(target_spec.get("start_line") or kwargs.get("start_line") or 1))
            line_count = target_spec.get("line_count", kwargs.get("line_count"))
            requested_max_chars = target_spec.get("max_chars", kwargs.get("max_chars_per_file"))
            max_chars = max(
                1,
                min(int(requested_max_chars or self.config.max_read_chars), self.config.max_read_chars),
            )
            files.append(_read_payload(
                workspace=self.workspace,
                target=target,
                start_line=start_line,
                line_count=int(line_count) if line_count is not None else None,
                max_chars=max_chars,
                output_format=output_format,
            ))
        if len(files) == 1:
            return _json(files[0])
        return _json({"files": files})

    def _read_tool_result(self, kwargs: Dict[str, Any]) -> str:
        if self._tool_result_store is None:
            return _error("tool_result_unavailable", "tool result storage is not configured")
        tool_result_id = str(kwargs.get("tool_result_id") or kwargs.get("id") or "").strip()
        if not tool_result_id:
            return _error("invalid_path", "tool_result_id or id is required when source is tool_result")
        offset = max(0, int(kwargs.get("offset") or 0))
        requested_limit = kwargs.get("limit") or kwargs.get("max_chars_per_file") or self.config.max_read_chars
        limit = max(1, min(int(requested_limit), self.config.max_read_chars))
        result = self._tool_result_store.read_slice(tool_result_id, offset=offset, limit=limit)
        if result is None:
            return _error("not_found", "tool result not found", tool_result_id=tool_result_id)
        payload = {
            "source": "tool_result",
            "tool_result_id": tool_result_id,
            "offset": offset,
            "content": result.get("content", ""),
        }
        next_offset = result.get("next_offset")
        if next_offset is not None:
            payload["next_offset"] = next_offset
            payload["read_more"] = {
                "source": "tool_result",
                "tool_result_id": tool_result_id,
                "offset": next_offset,
                "limit": limit,
            }
        return _json(payload)


class SearchFilesTool(_CodeTool):
    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search UTF-8 workspace file contents with ripgrep-style regex. Use this instead of shell for grep/rg/Select-String."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "glob": {"type": "string", "default": "*"},
                "type": {"type": "string"},
                "output": {"type": "string", "enum": ["content", "files", "count"], "default": "files"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 250},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "regex": {"type": "boolean", "default": True},
                "ignore_case": {"type": "boolean", "default": False},
                "respect_gitignore": {"type": "boolean", "default": True},
                "include_hidden": {"type": "boolean", "default": False},
                "multiline": {"type": "boolean", "default": False},
                "context": {"type": "integer", "minimum": 0, "default": 0},
                "before_context": {"type": "integer", "minimum": 0, "default": 0},
                "after_context": {"type": "integer", "minimum": 0, "default": 0},
                "exclude": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["pattern"],
        }

    async def execute(self, **kwargs) -> str:
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
        pattern = str(kwargs.get("pattern") or "")
        if not pattern:
            return _error("invalid_query", "pattern is required")
        try:
            root = self.workspace.check_read(kwargs.get("path") or ".")
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("path") or "."))

        glob = _glob_for_type(str(kwargs.get("glob") or "*"), str(kwargs.get("type") or ""))
        limit = max(1, min(int(kwargs.get("limit") or kwargs.get("head_limit") or 250), 500))
        offset = max(0, int(kwargs.get("offset") or 0))
        fixed_strings = not bool(kwargs.get("regex", True))
        ignore_case = bool(kwargs.get("ignore_case", False))
        no_ignore = not bool(kwargs.get("respect_gitignore", True))
        hidden = bool(kwargs.get("include_hidden", False))
        multiline = bool(kwargs.get("multiline", False))
        output = str(kwargs.get("output") or kwargs.get("output_mode") or "files")
        files_with_matches = output == "files"
        count_mode = output == "count"
        context = max(0, int(kwargs.get("context") or 0))
        before_context = max(context, int(kwargs.get("before_context") or 0))
        after_context = max(context, int(kwargs.get("after_context") or 0))
        exclude_globs = _string_list(kwargs.get("exclude"))

        if count_mode or multiline:
            payload = _grep_files_python(
                workspace=self.workspace,
                root=root,
                pattern=pattern,
                glob=glob,
                limit=limit,
                offset=offset,
                fixed_strings=fixed_strings,
                ignore_case=ignore_case,
                multiline=multiline,
                no_ignore=no_ignore,
                hidden=hidden,
                before_context=before_context,
                after_context=after_context,
                output=output,
                exclude_globs=exclude_globs,
            )
            return _json(payload)

        fallback_reason: Optional[str] = None
        rg_path = _resolve_ripgrep_executable(self.config)
        if rg_path is not None:
            rg_payload, fallback_reason = _grep_with_rg(
                rg_path=rg_path,
                workspace=self.workspace,
                root=root,
                pattern=pattern,
                glob=glob,
                max_results=limit + offset,
                fixed_strings=fixed_strings,
                ignore_case=ignore_case,
                no_ignore=no_ignore,
                hidden=hidden,
                before_context=before_context,
                after_context=after_context,
                files_with_matches=files_with_matches,
                exclude_globs=exclude_globs,
                timeout_seconds=self.config.command_timeout_seconds,
            )
            if rg_payload is not None:
                return _json(_shape_grep_payload(rg_payload, output=output, limit=limit, offset=offset))
            if fallback_reason and fallback_reason.startswith("ripgrep_invalid_regex:"):
                return _error("invalid_query", fallback_reason.split(":", 1)[1])
        else:
            fallback_reason = "ripgrep_not_installed"

        if not fixed_strings:
            try:
                re.compile(pattern, re.IGNORECASE if ignore_case else 0)
            except re.error as exc:
                return _error("invalid_query", f"invalid regex: {exc}")

        payload = _grep_python(
            workspace=self.workspace,
            root=root,
            pattern=pattern,
            glob=glob,
            max_results=limit + offset,
            fixed_strings=fixed_strings,
            ignore_case=ignore_case,
            no_ignore=no_ignore,
            hidden=hidden,
            before_context=before_context,
            after_context=after_context,
            files_with_matches=files_with_matches,
            exclude_globs=exclude_globs,
        )
        if fallback_reason:
            payload["fallback_reason"] = fallback_reason
        return _json(_shape_grep_payload(payload, output=output, limit=limit, offset=offset))


class EditFileTool(_CodeTool):
    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return (
            "Edit UTF-8 workspace files by exact replacements, create/overwrite content, or apply a unified patch. "
            "Read existing files first and pass expected_version for replacement or overwrite operations."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "operation": {"type": "string", "enum": ["replace", "create", "overwrite", "patch"], "default": "replace"},
                "expected_version": {"type": "string"},
                "content": {"type": "string"},
                "patch": {"type": "string"},
                "cwd": {"type": "string", "default": "."},
                "create_parents": {"type": "boolean", "default": False},
                "replacements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "old": {"type": "string"},
                            "new": {"type": "string"},
                            "replace_all": {"type": "boolean", "default": False},
                            "expected_count": {"type": "integer", "minimum": 1},
                        },
                        "required": ["old", "new"],
                    },
                },
            },
        }

    async def execute(self, **kwargs) -> str:
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
        operation = str(kwargs.get("operation") or "").strip().lower()
        if not operation:
            if kwargs.get("patch") and not kwargs.get("replacements"):
                operation = "patch"
            elif "content" in kwargs:
                operation = str(kwargs.get("mode") or "create").strip().lower()
            else:
                operation = "replace"
        if operation == "patch":
            return ApplyPatchTool(self.config)._execute_sync(kwargs)
        if operation in {"create", "overwrite"}:
            write_args = dict(kwargs)
            write_args["mode"] = operation
            return WriteFileTool(self.config)._execute_sync(write_args)
        if operation != "replace":
            return _error("invalid_edit", "operation must be replace, create, overwrite, or patch")
        try:
            target = self.workspace.check_write(kwargs.get("path"))
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("path") or ""))
        if not target.exists() or not target.is_file():
            return _error("not_found", "file not found", path=self.workspace.relative(target))
        expected_version = str(kwargs.get("expected_version") or "")
        current_version = _file_version(target)
        if not expected_version:
            return _error("stale_file", "expected_version is required; read the file before editing", path=self.workspace.relative(target))
        if expected_version != current_version:
            return _error("stale_file", "file changed since read; read again before editing", path=self.workspace.relative(target), current_version=current_version)
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _error("not_utf8", "file is not valid UTF-8 text", path=self.workspace.relative(target))
        replacements = kwargs.get("replacements")
        if not isinstance(replacements, list) or not replacements:
            return _error("invalid_edit", "replacements must be a non-empty array", path=self.workspace.relative(target))
        updated = text
        applied = 0
        for index, replacement in enumerate(replacements):
            if not isinstance(replacement, dict):
                return _error("invalid_edit", f"replacement {index} must be an object", path=self.workspace.relative(target))
            old_string = str(replacement.get("old") or "")
            new_string = str(replacement.get("new") or "")
            if not old_string:
                return _error("invalid_edit", "old text is required", path=self.workspace.relative(target), index=index)
            if _looks_like_numbered_read_line(old_string):
                return _error("invalid_edit", "old text includes read line-number prefixes; remove prefixes before editing", path=self.workspace.relative(target), index=index)
            occurrences = updated.count(old_string)
            expected_count = replacement.get("expected_count")
            if expected_count is not None and occurrences != int(expected_count):
                return _error("edit_count_mismatch", "old text occurrence count did not match expected_count", path=self.workspace.relative(target), index=index, expected_count=int(expected_count), occurrences=occurrences)
            if occurrences == 0:
                return _error("edit_not_found", "old text was not found", path=self.workspace.relative(target), index=index)
            replace_all = bool(replacement.get("replace_all", False))
            if occurrences > 1 and not replace_all:
                return _error(
                    "edit_not_unique",
                    "old text occurs more than once; set replace_all=true or provide more context",
                    path=self.workspace.relative(target),
                    index=index,
                    occurrences=occurrences,
                )
            updated = updated.replace(old_string, new_string, -1 if replace_all else 1)
            applied += occurrences if replace_all else 1
        target.write_text(updated, encoding="utf-8")
        return _json({
            "path": self.workspace.relative(target),
            "replacements": applied,
            "bytes_written": len(updated.encode("utf-8")),
            "version": _file_version(target),
        })


class WriteFileTool(_CodeTool):
    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return "Create or intentionally overwrite a UTF-8 text file in the workspace. Existing files require expected_version."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["create", "overwrite"], "default": "create"},
                "expected_version": {"type": "string"},
                "create_parents": {"type": "boolean", "default": False},
            },
            "required": ["path", "content"],
        }

    async def execute(self, **kwargs) -> str:
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
        try:
            target = self.workspace.check_write(kwargs.get("path"))
        except CodeToolError as exc:
            return _error(exc.error_type, str(exc), path=str(kwargs.get("path") or ""))
        create_parents = bool(kwargs.get("create_parents", False))
        if create_parents and not self.config.allow_parent_dir_creation:
            return _error("invalid_path", "parent directory creation is disabled", path=self.workspace.relative(target))
        if not target.parent.exists():
            if create_parents:
                target.parent.mkdir(parents=True, exist_ok=True)
            else:
                return _error("not_found", "parent directory does not exist", path=self.workspace.relative(target))
        mode = str(kwargs.get("mode") or "create")
        exists = target.exists()
        if mode == "create" and exists:
            return _error("file_exists", "file already exists; use edit or overwrite with expected_version", path=self.workspace.relative(target), current_version=_file_version(target) if target.is_file() else None)
        if mode == "overwrite" and exists:
            expected_version = str(kwargs.get("expected_version") or "")
            current_version = _file_version(target)
            if not expected_version:
                return _error("stale_file", "expected_version is required to overwrite an existing file", path=self.workspace.relative(target), current_version=current_version)
            if expected_version != current_version:
                return _error("stale_file", "file changed since read; read again before overwriting", path=self.workspace.relative(target), current_version=current_version)
        content = str(kwargs.get("content") or "")
        target.write_text(content, encoding="utf-8")
        return _json({"path": self.workspace.relative(target), "bytes_written": len(content.encode("utf-8")), "version": _file_version(target), "mode": "overwrite" if exists else "create"})


class RunCommandTool(_CodeTool):
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
                "cwd": {"type": "string", "default": "."},
                "timeout_seconds": {"type": "integer", "minimum": 1},
                "step": task_step_parameter_schema(),
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
                runtime_context=runtime_context,
                step=kwargs.get("step"),
            )
        if kwargs.get("step") is not None:
            return _error("missing_runtime_context", "step binding requires ChatTree runtime context")
        python_c_args = _windows_python_c_args(command)
        profile = ShellProfileResolver().resolve()
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                args=python_c_args or profile.command_argv(command),
                shell=False,
                cwd=str(cwd),
                env=_shell_env(),
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
        step: Any = None,
    ) -> str:
        command_executor = runtime_context.get("command_executor")
        if command_executor is None or not hasattr(command_executor, "start"):
            return _error("missing_command_executor", "managed shell requires a command executor")
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
                "suppress_task_notification": _should_suppress_command_notification(runtime_context),
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
            return _json(self._managed_background_payload(command, cwd, run_id, snapshot, auto_backgrounded=True))

        snapshot = command_executor.snapshot(run_id)
        if snapshot is None:
            return _error("not_found", "managed command run not found", command=command)
        if snapshot.get("status") in FINISHED_STATUS_VALUES and hasattr(command_executor, "mark_observed"):
            await command_executor.mark_observed(
                run_id,
                observer_run_id=str(runtime_context.get("run_id") or "") or None,
                via=self.name,
            )
            snapshot = command_executor.snapshot(run_id) or snapshot
        return _json(self._managed_finished_payload(
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
            "shell": snapshot.get("shell"),
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


class ApplyPatchTool(_CodeTool):
    @property
    def name(self) -> str:
        return "patch"

    @property
    def description(self) -> str:
        return (
            "Apply a unified diff patch to existing UTF-8 files in the code workspace. "
            "Prefer edit for small exact replacements; use patch for multi-line or multi-file changes."
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
        return await asyncio.to_thread(self._execute_sync, dict(kwargs))

    def _execute_sync(self, kwargs: Dict[str, Any]) -> str:
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


def _read_targets(kwargs: Dict[str, Any]) -> List[Dict[str, Any]]:
    targets = kwargs.get("targets")
    if isinstance(targets, list):
        normalized: List[Dict[str, Any]] = []
        for target in targets:
            if not isinstance(target, dict):
                continue
            path = str(target.get("path") or "")
            if path:
                normalized.append(dict(target, path=path))
        return normalized
    path = kwargs.get("path")
    return [{"path": str(path)}] if path else []


def _read_payload(
    *,
    workspace: CodeWorkspace,
    target: Path,
    start_line: int,
    line_count: Optional[int],
    max_chars: int,
    output_format: str,
) -> Dict[str, Any]:
    if not target.exists() or not target.is_file():
        return {"path": workspace.relative(target), "error": {"type": "not_found", "message": "file not found"}}
    try:
        with target.open("r", encoding="utf-8") as handle:
            selected: List[str] = []
            current_line = 0
            chars = 0
            truncated = False
            for raw_line in handle:
                current_line += 1
                in_range = current_line >= start_line and (line_count is None or len(selected) < line_count)
                if not in_range:
                    if current_line < start_line:
                        continue
                    if line_count is not None and len(selected) >= line_count:
                        truncated = True
                    continue
                remaining = max_chars - chars
                if remaining <= 0:
                    truncated = True
                    continue
                if len(raw_line) > remaining:
                    selected.append(raw_line[:remaining])
                    chars += remaining
                    truncated = True
                    continue
                selected.append(raw_line)
                chars += len(raw_line)
    except UnicodeDecodeError:
        return {"path": workspace.relative(target), "error": {"type": "not_utf8", "message": "file is not valid UTF-8 text"}}
    selected_text = "".join(selected)
    payload: Dict[str, Any] = {
        "path": workspace.relative(target),
        "start_line": start_line,
        "line_count": len(selected),
        "total_lines": current_line,
        "version": _file_version(target),
        "truncated": truncated,
    }
    if output_format == "json":
        payload["lines"] = [
            {"line": start_line + index, "text": _strip_line_ending(line)}
            for index, line in enumerate(selected)
        ]
    elif output_format == "raw":
        payload["content"] = selected_text
    else:
        payload["content"] = _number_lines(selected, start_line)
    return payload


def _file_version(target: Path) -> str:
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _number_lines(lines: List[str], start_line: int) -> str:
    return "\n".join(f"{start_line + index}\t{_strip_line_ending(line)}" for index, line in enumerate(lines))


def _strip_line_ending(line: str) -> str:
    return line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line


def _looks_like_numbered_read_line(text: str) -> bool:
    return any(re.match(r"^\s*\d+(?:\t|\u2192)", line) for line in text.splitlines())


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
    path_executable = shutil.which(executable) or shutil.which("rg")
    if path_executable:
        return Path(path_executable)
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


def _grep_with_rg(
    *,
    rg_path: Path,
    workspace: CodeWorkspace,
    root: Path,
    pattern: str,
    glob: str,
    max_results: int,
    fixed_strings: bool,
    ignore_case: bool,
    no_ignore: bool,
    hidden: bool,
    before_context: int,
    after_context: int,
    files_with_matches: bool,
    exclude_globs: List[str],
    timeout_seconds: int,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if root.is_file() and not _matches_glob(root, glob):
        return ({
            "pattern": pattern,
            "matches": [],
            "searched_files": 0,
            "skipped_non_utf8": [],
            "truncated": False,
            "engine": "rg",
        }, None)

    cwd = root if root.is_dir() else root.parent
    target = "." if root.is_dir() else root.name
    argv = [str(rg_path), "--color", "never", "--no-config", "--line-number"]
    if not files_with_matches:
        argv.append("--json")
    if fixed_strings:
        argv.append("--fixed-strings")
    if ignore_case:
        argv.append("--ignore-case")
    if no_ignore:
        argv.append("--no-ignore")
    if hidden:
        argv.append("--hidden")
    if before_context:
        argv.extend(["--before-context", str(before_context)])
    if after_context:
        argv.extend(["--after-context", str(after_context)])
    if files_with_matches:
        argv.append("--files-with-matches")
    if glob and glob != "*":
        argv.extend(["--glob", glob])
    for exclude_glob in exclude_globs:
        argv.extend(["--glob", f"!{exclude_glob}"])
    argv.extend(["--", pattern, target])

    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=_shell_env(),
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
    matched_files: set[str] = set()
    skipped_files: set[str] = set()
    searched_paths: set[str] = set()
    truncated = False
    for raw_line in proc.stdout.splitlines():
        if files_with_matches:
            resolved = (cwd / raw_line.strip()).resolve()
            if resolved.is_file() and workspace.is_visible(resolved):
                matched_files.add(workspace.relative(resolved))
                if len(matched_files) >= max_results:
                    truncated = True
                    break
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return None, "ripgrep_invalid_json"
        if event.get("type") not in {"match", "context"}:
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
            "type": event.get("type"),
        })
        if len(matches) >= max_results:
            truncated = True
            break

    payload: Dict[str, Any] = {
        "pattern": pattern,
        "matches": matches,
        "searched_files": len(searched_paths),
        "skipped_non_utf8": sorted(skipped_files),
        "truncated": truncated,
        "engine": "rg",
    }
    if files_with_matches:
        payload["files"] = sorted(matched_files)
        payload["matches"] = []
    return (payload, None)


def _glob_files_with_rg(
    *,
    rg_path: Path,
    workspace: CodeWorkspace,
    root: Path,
    patterns: List[str],
    path_regex: Optional[re.Pattern[str]],
    respect_gitignore: bool,
    include_hidden: bool,
    exclude_globs: List[str],
    sort: str,
    limit: int,
    offset: int,
    timeout_seconds: int,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if root.is_file():
        if not any(_matches_glob(root, pattern) for pattern in patterns):
            return ({
                "root": workspace.relative(root),
                "files": [],
                "count": 0,
                "total": 0,
                "truncated": False,
                "next_offset": None,
                "engine": "rg",
                "scanned_entries": 1,
            }, None)
        relative = workspace.relative(root)
        if path_regex and not path_regex.search(relative):
            files: List[str] = []
        else:
            files = [relative]
        return ({
            "root": workspace.relative(root),
            "files": files[offset:offset + limit],
            "count": len(files[offset:offset + limit]),
            "total": len(files),
            "truncated": offset + limit < len(files),
            "next_offset": offset + len(files[offset:offset + limit]) if offset + limit < len(files) else None,
            "engine": "rg",
            "scanned_entries": 1,
        }, None)

    argv = [str(rg_path), "--files", "--color", "never", "--no-config"]
    if not respect_gitignore:
        argv.append("--no-ignore")
    if include_hidden:
        argv.append("--hidden")
    for pattern in patterns:
        if pattern and pattern != "**/*":
            argv.extend(["--glob", pattern])
    for exclude_glob in exclude_globs:
        argv.extend(["--glob", f"!{exclude_glob}"])
    argv.extend(["--", "."])

    try:
        proc = subprocess.run(
            argv,
            cwd=str(root),
            env=_shell_env(),
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

    matches: List[Path] = []
    seen: set[Path] = set()
    scanned_entries = 0
    for raw_line in proc.stdout.splitlines():
        relative_text = raw_line.strip()
        if not relative_text:
            continue
        scanned_entries += 1
        resolved = (root / relative_text).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_file() or not workspace.is_visible(resolved):
            continue
        if not any(_matches_glob(resolved, pattern) for pattern in patterns):
            continue
        if _matches_excluded_glob(resolved, root, exclude_globs):
            continue
        relative = workspace.relative(resolved)
        if path_regex and not path_regex.search(relative):
            continue
        matches.append(resolved)

    if sort == "mtime":
        matches.sort(key=lambda path: (-path.stat().st_mtime, workspace.relative(path)))
    else:
        matches.sort(key=lambda path: workspace.relative(path))

    total = len(matches)
    page = matches[offset:offset + limit]
    truncated = offset + limit < total
    return ({
        "root": workspace.relative(root),
        "files": [workspace.relative(path) for path in page],
        "count": len(page),
        "total": total,
        "truncated": truncated,
        "next_offset": offset + len(page) if truncated else None,
        "engine": "rg",
        "scanned_entries": scanned_entries,
    }, None)


def _grep_python(
    *,
    workspace: CodeWorkspace,
    root: Path,
    pattern: str,
    glob: str,
    max_results: int,
    fixed_strings: bool,
    ignore_case: bool,
    no_ignore: bool,
    hidden: bool,
    before_context: int,
    after_context: int,
    files_with_matches: bool,
    exclude_globs: List[str],
) -> Dict[str, Any]:
    matches: List[Dict[str, Any]] = []
    matched_files: set[str] = set()
    searched_files = 0
    skipped_files: List[str] = []
    matcher = _compile_python_matcher(pattern, fixed_strings=fixed_strings, ignore_case=ignore_case)
    ignore_matcher = _GitIgnoreMatcher.for_root(root, workspace)

    for file_path in _iter_grep_files(root, glob):
        resolved = file_path.resolve()
        if (
            not resolved.is_file()
            or not workspace.is_visible(resolved)
            or _should_skip_python_path(resolved, root, hidden=hidden, no_ignore=no_ignore, ignore_matcher=ignore_matcher)
            or _matches_excluded_glob(resolved, root, exclude_globs)
        ):
            continue
        searched_files += 1
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped_files.append(workspace.relative(resolved))
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not matcher(line):
                continue
            relative_path = workspace.relative(resolved)
            matched_files.add(relative_path)
            if files_with_matches:
                if len(matched_files) >= max_results:
                    return _search_payload(pattern, [], searched_files, skipped_files, True, "python", matched_files)
                break
            start = max(0, index - before_context)
            stop = min(len(lines), index + after_context + 1)
            for context_index in range(start, stop):
                matches.append({
                    "path": relative_path,
                    "line": context_index + 1,
                    "preview": lines[context_index].strip(),
                    "type": "match" if context_index == index else "context",
                })
                if len(matches) >= max_results:
                    return _search_payload(pattern, matches, searched_files, skipped_files, True, "python", matched_files)
            if len(matches) >= max_results:
                return {
                    "pattern": pattern,
                    "matches": matches,
                    "searched_files": searched_files,
                    "skipped_non_utf8": skipped_files,
                    "truncated": True,
                    "engine": "python",
                }

    return _search_payload(pattern, matches, searched_files, skipped_files, False, "python", matched_files if files_with_matches else None)


def _glob_files_python(
    *,
    workspace: CodeWorkspace,
    root: Path,
    patterns: List[str],
    path_regex: Optional[re.Pattern[str]],
    respect_gitignore: bool,
    include_hidden: bool,
    files_only: bool,
    exclude_globs: List[str],
    sort: str,
    limit: int,
    offset: int,
) -> tuple[List[str], bool, int, int]:
    ignore_matcher = _GitIgnoreMatcher.for_root(root, workspace)
    matches: List[Path] = []
    seen: set[Path] = set()
    scanned_entries = 0
    scan_limit = max(1000, (limit + offset) * 50)

    candidates: Iterable[Path]
    if root.is_file():
        candidates = [root]
    else:
        candidates = root.rglob("*")

    for candidate in candidates:
        scanned_entries += 1
        if scanned_entries > scan_limit:
            break
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not workspace.is_visible(resolved):
            continue
        if files_only and not resolved.is_file():
            continue
        if not files_only and not (resolved.is_file() or resolved.is_dir()):
            continue
        if _should_skip_python_path(
            resolved,
            root if root.is_dir() else root.parent,
            hidden=include_hidden,
            no_ignore=respect_gitignore is False,
            ignore_matcher=ignore_matcher,
        ):
            continue
        if _matches_excluded_glob(resolved, root if root.is_dir() else root.parent, exclude_globs):
            continue
        if not any(_matches_glob(resolved, pattern) for pattern in patterns):
            continue
        relative = workspace.relative(resolved)
        if path_regex and not path_regex.search(relative):
            continue
        matches.append(resolved)

    if sort == "mtime":
        matches.sort(key=lambda path: (-path.stat().st_mtime, workspace.relative(path)))
    else:
        matches.sort(key=lambda path: workspace.relative(path))

    total = len(matches)
    page = matches[offset:offset + limit]
    truncated = scanned_entries > scan_limit or offset + limit < total
    return [workspace.relative(path) for path in page], truncated, scanned_entries, total


def _shape_grep_payload(payload: Dict[str, Any], *, output: str, limit: int, offset: int) -> Dict[str, Any]:
    shaped: Dict[str, Any] = {
        "pattern": payload.get("pattern"),
        "output": output,
        "engine": payload.get("engine"),
        "skipped_non_utf8": payload.get("skipped_non_utf8", []),
    }
    if payload.get("fallback_reason"):
        shaped["fallback_reason"] = payload.get("fallback_reason")
    if output == "files":
        files = list(payload.get("files") or [])
        page = files[offset:offset + limit]
        shaped.update({
            "files": page,
            "count": len(page),
            "truncated": bool(payload.get("truncated")) or offset + limit < len(files),
            "next_offset": offset + len(page) if (bool(payload.get("truncated")) or offset + limit < len(files)) else None,
        })
        return shaped
    matches = [
        {
            "path": match.get("path"),
            "line": match.get("line"),
            "text": match.get("preview", ""),
            "type": match.get("type", "match"),
        }
        for match in list(payload.get("matches") or [])[offset:offset + limit]
    ]
    shaped.update({
        "matches": matches,
        "count": len(matches),
        "truncated": bool(payload.get("truncated")) or offset + limit < len(payload.get("matches") or []),
        "next_offset": offset + len(matches) if (bool(payload.get("truncated")) or offset + limit < len(payload.get("matches") or [])) else None,
    })
    return shaped


def _grep_files_python(
    *,
    workspace: CodeWorkspace,
    root: Path,
    pattern: str,
    glob: str,
    limit: int,
    offset: int,
    fixed_strings: bool,
    ignore_case: bool,
    multiline: bool,
    no_ignore: bool,
    hidden: bool,
    before_context: int,
    after_context: int,
    output: str,
    exclude_globs: List[str],
) -> Dict[str, Any]:
    if not fixed_strings:
        try:
            flags = re.IGNORECASE | (re.DOTALL if multiline else 0)
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return {"error": {"type": "invalid_query", "message": f"invalid regex: {exc}"}}
    else:
        compiled = None
    ignore_matcher = _GitIgnoreMatcher.for_root(root, workspace)
    matches: List[Dict[str, Any]] = []
    files: List[str] = []
    counts: List[Dict[str, Any]] = []
    skipped_files: List[str] = []
    searched_files = 0

    for file_path in _iter_grep_files(root, glob):
        resolved = file_path.resolve()
        if (
            not resolved.is_file()
            or not workspace.is_visible(resolved)
            or _should_skip_python_path(resolved, root, hidden=hidden, no_ignore=no_ignore, ignore_matcher=ignore_matcher)
            or _matches_excluded_glob(resolved, root, exclude_globs)
        ):
            continue
        relative = workspace.relative(resolved)
        searched_files += 1
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped_files.append(relative)
            continue
        file_match_count = 0
        if multiline:
            found = list(compiled.finditer(text)) if compiled else []
            file_match_count = len(found)
            if found and output == "files":
                files.append(relative)
            elif output == "content":
                lines = text.splitlines()
                for match in found:
                    line_no = text.count("\n", 0, match.start()) + 1
                    line_text = lines[line_no - 1] if 0 <= line_no - 1 < len(lines) else ""
                    matches.append({"path": relative, "line": line_no, "text": line_text, "type": "match"})
        else:
            lines = text.splitlines()
            for index, line in enumerate(lines):
                ok = (compiled.search(line) is not None) if compiled else (
                    pattern.lower() in line.lower() if ignore_case else pattern in line
                )
                if not ok:
                    continue
                file_match_count += 1
                if output == "content":
                    start = max(0, index - before_context)
                    stop = min(len(lines), index + after_context + 1)
                    for context_index in range(start, stop):
                        matches.append({
                            "path": relative,
                            "line": context_index + 1,
                            "text": lines[context_index],
                            "type": "match" if context_index == index else "context",
                        })
            if file_match_count and output == "files":
                files.append(relative)
        if output == "count" and file_match_count:
            counts.append({"path": relative, "count": file_match_count})

    if output == "files":
        page = files[offset:offset + limit]
        return {"pattern": pattern, "output": output, "files": page, "count": len(page), "searched_files": searched_files, "skipped_non_utf8": skipped_files, "truncated": offset + limit < len(files), "next_offset": offset + len(page) if offset + limit < len(files) else None, "engine": "python"}
    if output == "count":
        page = counts[offset:offset + limit]
        return {"pattern": pattern, "output": output, "counts": page, "count": len(page), "searched_files": searched_files, "skipped_non_utf8": skipped_files, "truncated": offset + limit < len(counts), "next_offset": offset + len(page) if offset + limit < len(counts) else None, "engine": "python"}
    page = matches[offset:offset + limit]
    return {"pattern": pattern, "output": output, "matches": page, "count": len(page), "searched_files": searched_files, "skipped_non_utf8": skipped_files, "truncated": offset + limit < len(matches), "next_offset": offset + len(page) if offset + limit < len(matches) else None, "engine": "python"}


def _glob_for_type(glob: str, type_name: str) -> str:
    if glob and glob != "*":
        return glob
    mapping = {
        "py": "*.py",
        "python": "*.py",
        "js": "*.js",
        "ts": "*.ts",
        "tsx": "*.tsx",
        "jsx": "*.jsx",
        "rust": "*.rs",
        "rs": "*.rs",
        "go": "*.go",
        "java": "*.java",
    }
    return mapping.get(type_name.lower(), glob or "*")


def _iter_grep_files(root: Path, glob: str) -> Iterable[Path]:
    if root.is_file():
        if _matches_glob(root, glob):
            yield root
        return
    yield from root.rglob(glob)


def _compile_python_matcher(pattern: str, *, fixed_strings: bool, ignore_case: bool):
    if not fixed_strings:
        flags = re.IGNORECASE if ignore_case else 0
        compiled = re.compile(pattern, flags)
        return lambda line: compiled.search(line) is not None
    if ignore_case:
        needle = pattern.lower()
        return lambda line: needle in line.lower()
    return lambda line: pattern in line


def _search_payload(
    pattern: str,
    matches: List[Dict[str, Any]],
    searched_files: int,
    skipped_files: List[str],
    truncated: bool,
    engine: str,
    files: Optional[set[str]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "pattern": pattern,
        "matches": [] if files is not None else matches,
        "searched_files": searched_files,
        "skipped_non_utf8": skipped_files,
        "truncated": truncated,
        "engine": engine,
    }
    if files is not None:
        payload["files"] = sorted(files)
    return payload


def _matches_glob(path: Path, pattern: str) -> bool:
    return fnmatch(path.name, pattern) or fnmatch(path.as_posix(), pattern)


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _matches_excluded_glob(path: Path, root: Path, exclude_globs: List[str]) -> bool:
    if not exclude_globs:
        return False
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = path.name
    return any(fnmatch(relative, pattern) or fnmatch(path.name, pattern) for pattern in exclude_globs)


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
    no_ignore: bool,
    ignore_matcher: "_GitIgnoreMatcher",
) -> bool:
    if not hidden and _is_hidden_under(path, root):
        return True
    if not no_ignore and ignore_matcher.matches(path):
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
