from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from backend.core.home import resolve_chattree_home


DEFAULT_AGENTS_MD_FILENAME = "AGENTS.md"
LOCAL_AGENTS_MD_FILENAME = "AGENTS.override.md"
DEFAULT_PROJECT_DOC_MAX_BYTES = 32 * 1024
AGENTS_MD_SEPARATOR = "\n\n--- project-doc ---\n\n"
DEFAULT_PROJECT_ROOT_MARKERS = [".git"]


@dataclass(frozen=True)
class InstructionEntry:
    path: Path
    scope: str
    content: str


@dataclass(frozen=True)
class LoadedInstructionFiles:
    entries: tuple[InstructionEntry, ...] = ()
    warnings: tuple[str, ...] = ()
    cwd: Optional[Path] = None

    @property
    def sources(self) -> list[str]:
        return [str(entry.path) for entry in self.entries]

    @property
    def text(self) -> str:
        global_entries = [entry.content for entry in self.entries if entry.scope == "global"]
        project_entries = [entry.content for entry in self.entries if entry.scope == "project"]
        global_text = "\n\n".join(global_entries)
        project_text = "\n\n".join(project_entries)
        if global_text and project_text:
            return f"{global_text}{AGENTS_MD_SEPARATOR}{project_text}"
        return global_text or project_text

    def is_empty(self) -> bool:
        return not self.text.strip()

    def render(self) -> str:
        if self.is_empty():
            return ""
        directory = f" for {self.cwd}" if self.cwd is not None else ""
        return "\n".join([
            f"# AGENTS.md instructions{directory}",
            "",
            "<INSTRUCTIONS>",
            self.text,
            "</INSTRUCTIONS>",
        ])


def load_agents_instructions(
    *,
    cwd: str | Path,
    chattree_home: str | Path | None = None,
    config_data: Mapping[str, Any] | None = None,
) -> LoadedInstructionFiles:
    """Load Codex-style global and hierarchical AGENTS.md instructions."""

    instructions_config = _instructions_config(config_data)
    if instructions_config.get("include_agents_md") is False:
        return LoadedInstructionFiles(cwd=_resolve_cwd(cwd))

    resolved_cwd = _resolve_cwd(cwd)
    warnings: list[str] = []
    entries: list[InstructionEntry] = []

    home = resolve_chattree_home(chattree_home)
    global_entry, global_warnings = _load_global_agents(home)
    warnings.extend(global_warnings)
    if global_entry is not None:
        entries.append(global_entry)

    max_project_bytes = _project_doc_max_bytes(instructions_config)
    project_entries, project_warnings = _load_project_agents(
        resolved_cwd,
        max_project_bytes=max_project_bytes,
        markers=_project_root_markers(instructions_config),
    )
    warnings.extend(project_warnings)
    entries.extend(project_entries)

    return LoadedInstructionFiles(
        entries=tuple(entries),
        warnings=tuple(warnings),
        cwd=resolved_cwd,
    )


def _instructions_config(config_data: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config_data, Mapping):
        return {}
    value = config_data.get("instructions")
    return value if isinstance(value, Mapping) else {}


def _resolve_cwd(cwd: str | Path) -> Path:
    return Path(cwd).expanduser().resolve(strict=False)


def _candidate_paths(directory: Path) -> Iterable[Path]:
    yield directory / LOCAL_AGENTS_MD_FILENAME
    yield directory / DEFAULT_AGENTS_MD_FILENAME


def _load_global_agents(home: Path) -> tuple[Optional[InstructionEntry], list[str]]:
    warnings: list[str] = []
    for path in _candidate_paths(home):
        if not path.exists() or not path.is_file():
            continue
        content = _read_utf8_lossy(path, warnings=warnings)
        if content.strip():
            return InstructionEntry(path=path, scope="global", content=content.strip()), warnings
    return None, warnings


def _load_project_agents(
    cwd: Path,
    *,
    max_project_bytes: int,
    markers: list[str],
) -> tuple[list[InstructionEntry], list[str]]:
    warnings: list[str] = []
    if max_project_bytes <= 0:
        return [], warnings

    directories = _search_directories(cwd, markers)
    remaining = max_project_bytes
    entries: list[InstructionEntry] = []
    for directory in directories:
        candidate = next(
            (path for path in _candidate_paths(directory) if path.exists() and path.is_file()),
            None,
        )
        if candidate is None:
            continue
        raw = _read_bytes(candidate, warnings=warnings)
        if raw is None:
            continue
        if len(raw) > remaining:
            raw = raw[:remaining]
            warnings.append(
                f"AGENTS.md project instructions exceeded remaining byte budget and were truncated: {candidate}"
            )
        content = raw.decode("utf-8", errors="replace").strip()
        if content:
            entries.append(InstructionEntry(path=candidate, scope="project", content=content))
            remaining -= len(raw)
        if remaining <= 0:
            break
    return entries, warnings


def _read_utf8_lossy(path: Path, *, warnings: list[str]) -> str:
    raw = _read_bytes(path, warnings=warnings)
    if raw is None:
        return ""
    return raw.decode("utf-8", errors="replace")


def _read_bytes(path: Path, *, warnings: list[str]) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError as exc:
        warnings.append(f"Failed to read AGENTS.md instructions from `{path}`: {exc}")
        return None


def _search_directories(cwd: Path, markers: list[str]) -> list[Path]:
    if not markers:
        return [cwd]
    root = _nearest_ancestor_with_markers(cwd, markers)
    if root is None:
        return [cwd]

    dirs: list[Path] = []
    cursor = cwd
    while True:
        dirs.append(cursor)
        if cursor == root:
            break
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    dirs.reverse()
    return dirs


def _nearest_ancestor_with_markers(cwd: Path, markers: list[str]) -> Optional[Path]:
    cursor = cwd
    while True:
        if any((cursor / marker).exists() for marker in markers):
            return cursor
        parent = cursor.parent
        if parent == cursor:
            return None
        cursor = parent


def _project_doc_max_bytes(config: Mapping[str, Any]) -> int:
    value = config.get("project_doc_max_bytes", DEFAULT_PROJECT_DOC_MAX_BYTES)
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return DEFAULT_PROJECT_DOC_MAX_BYTES


def _project_root_markers(config: Mapping[str, Any]) -> list[str]:
    value = config.get("project_root_markers")
    if value is None:
        return list(DEFAULT_PROJECT_ROOT_MARKERS)
    if not isinstance(value, list):
        return list(DEFAULT_PROJECT_ROOT_MARKERS)
    return [str(item) for item in value if str(item)]
