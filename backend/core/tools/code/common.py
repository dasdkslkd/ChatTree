from __future__ import annotations

import json
import locale
import os
import shlex
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseTool
from ..security.logical_sandbox import DEFAULT_PROTECTED_PATHS
from ...persistence.home import resolve_chattree_home
from ...runs.types import FINISHED_RUN_STATUSES


DEFAULT_CODE_WORKSPACE = Path("workspaces") / "default"
DEFAULT_RIPGREP_VERSION = "15.1.0"
FINISHED_STATUS_VALUES = {status.value for status in FINISHED_RUN_STATUSES}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _default_ripgrep_install_dir() -> Path:
    return resolve_chattree_home() / "tools" / "ripgrep"


def default_code_workspace() -> Path:
    return resolve_chattree_home() / DEFAULT_CODE_WORKSPACE


class CodeToolError(ValueError):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(error_type: str, message: str, **extra: Any) -> str:
    return _json({"error": {"type": error_type, "message": message, **extra}})


def _ripgrep_error(reason: str) -> str:
    if reason == "ripgrep_timeout":
        return _error("ripgrep_timeout", "ripgrep timed out; narrow path/glob or raise the tool timeout")
    if reason.startswith("ripgrep_failed:"):
        return _error("ripgrep_failed", reason.split(":", 1)[1])
    return _error("ripgrep_failed", reason)


def _tool_event_sink(kwargs: Dict[str, Any]) -> Optional[Callable[[Dict[str, Any]], None]]:
    runtime_context = kwargs.get("_runtime_context")
    if not isinstance(runtime_context, dict):
        return None
    sink = runtime_context.get("tool_event_sink")
    return sink if callable(sink) else None


def _emit_tool_observation(
    sink: Optional[Callable[[Dict[str, Any]], None]],
    event_type: str,
    **payload: Any,
) -> None:
    if sink is None:
        return
    try:
        sink({"event_type": event_type, **payload})
    except Exception:
        # Tool observation must never change tool semantics.
        pass


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
        roots = cfg.get("workspace_roots") or [default_code_workspace()]
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


def _normalize_glob_patterns(patterns: List[str]) -> List[str]:
    return [_normalize_glob_pattern(pattern) for pattern in patterns if str(pattern).strip()]


def _normalize_glob_pattern(pattern: str) -> str:
    normalized = str(pattern or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "*"


def _is_match_all_glob(pattern: str) -> bool:
    return _normalize_glob_pattern(pattern) in {"*", "**/*"}


def _glob_has_path_separator(pattern: str) -> bool:
    return "/" in _normalize_glob_pattern(pattern)


def _glob_for_search_root(workspace: Optional[CodeWorkspace], root: Path, pattern: str) -> str:
    normalized = _normalize_glob_pattern(pattern)
    if workspace is None or root.is_file() or _is_match_all_glob(normalized):
        return normalized
    root_relative = workspace.relative(root.resolve()).replace("\\", "/")
    if root_relative and root_relative != ".":
        prefix = root_relative.rstrip("/") + "/"
        if normalized == root_relative:
            return "*"
        if normalized.startswith(prefix):
            return normalized[len(prefix):] or "*"
    return normalized


def _glob_match_texts(path: Path, *, workspace: Optional[CodeWorkspace] = None, root: Optional[Path] = None) -> List[str]:
    texts = [path.name, path.as_posix()]
    if workspace is not None:
        texts.append(workspace.relative(path.resolve()))
    if root is not None:
        try:
            texts.append(path.resolve().relative_to(root.resolve()).as_posix())
        except ValueError:
            pass
    normalized: List[str] = []
    for text in texts:
        value = str(text).replace("\\", "/")
        normalized.append(value)
        while value.startswith("./"):
            value = value[2:]
            normalized.append(value)
    return list(dict.fromkeys(normalized))


def _matches_glob(
    path: Path,
    pattern: str,
    *,
    workspace: Optional[CodeWorkspace] = None,
    root: Optional[Path] = None,
) -> bool:
    normalized = _normalize_glob_pattern(pattern)
    if _is_match_all_glob(normalized):
        return True
    patterns = [normalized]
    if normalized.startswith("**/"):
        patterns.append(normalized[3:])
    return any(
        fnmatch(text, candidate_pattern)
        for candidate_pattern in patterns
        for text in _glob_match_texts(path, workspace=workspace, root=root)
    )


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _glob_payload(
    *,
    workspace: CodeWorkspace,
    root: Path,
    page: List[str],
    engine: str,
    sort: str,
    scanned_entries: int,
    observed_count: int,
    total_known: bool,
    truncated: bool,
    offset: int,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "root": workspace.relative(root),
        "files": page,
        "count": len(page),
        "total": observed_count if total_known else None,
        "total_known": total_known,
        "observed_count": observed_count,
        "truncated": truncated,
        "next_offset": offset + len(page) if truncated else None,
        "engine": engine,
        "sort": sort,
        "scanned_entries": scanned_entries,
    }
    return payload


def _should_skip_python_path(
    path: Path,
    root: Path,
    *,
    hidden: bool,
    no_ignore: bool,
    ignore_matcher: Optional["_GitIgnoreMatcher"],
) -> bool:
    if not hidden and _is_hidden_under(path, root):
        return True
    if not no_ignore and ignore_matcher is not None and ignore_matcher.matches(path):
        return True
    return False


def _is_hidden_under(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = Path(path.name)
    return any(part.startswith(".") for part in relative.parts if part not in {"", "."})


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
