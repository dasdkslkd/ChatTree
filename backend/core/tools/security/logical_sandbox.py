from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_PROTECTED_PATHS = [
    ".git",
    ".agents",
    ".claude",
    ".codex",
    "data/config.json",
    "data/conversations",
]


class SandboxViolation(RuntimeError):
    pass


class LogicalSandbox:
    def __init__(self, workspace_roots: Iterable[str | Path], protected_paths: Iterable[str | Path]):
        self.workspace_roots = [_resolve_path(root) for root in workspace_roots]
        self.protected_paths = [Path(path) for path in protected_paths]

    @classmethod
    def for_config(cls, config: Mapping[str, Any], default_workspace: str | Path) -> "LogicalSandbox":
        sandbox_config = (
            config.get("tools", {})
            .get("permissions", {})
            .get("sandbox", {})
        )
        workspace_roots = sandbox_config.get("workspace_roots") or [default_workspace]
        protected_paths = sandbox_config.get("protected_paths") or DEFAULT_PROTECTED_PATHS
        return cls(workspace_roots=workspace_roots, protected_paths=protected_paths)

    def check_filesystem_write(self, target: str | Path) -> None:
        resolved_target = _resolve_path(target)
        workspace_roots = self._workspace_roots_for(resolved_target)
        if not workspace_roots:
            raise SandboxViolation(f"write target is outside workspace roots: {resolved_target}")

        for workspace_root in workspace_roots:
            for protected_path in self.protected_paths:
                resolved_protected = self._resolve_protected_path(workspace_root, protected_path)
                if _is_relative_to(resolved_target, resolved_protected):
                    raise SandboxViolation(f"write target is within protected path: {protected_path}")

    def _workspace_roots_for(self, target: Path) -> list[Path]:
        return [
            workspace_root
            for workspace_root in self.workspace_roots
            if _is_relative_to(target, workspace_root)
        ]

    def _resolve_protected_path(self, workspace_root: Path, protected_path: Path) -> Path:
        if protected_path.is_absolute():
            return _resolve_path(protected_path)
        return _resolve_path(workspace_root / protected_path)


def _resolve_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
