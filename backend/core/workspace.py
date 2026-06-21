from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, TypedDict

from .tools.security.logical_sandbox import DEFAULT_PROTECTED_PATHS


class WorkspaceContext(TypedDict):
    cwd: str
    workspace_roots: list[str]
    protected_paths: list[str]
    label: str


def normalize_workspace(raw: Mapping[str, Any] | None, default: Mapping[str, Any] | None = None) -> WorkspaceContext:
    """规范化对话工作区快照。"""
    source = dict(default or {})
    source.update(dict(raw or {}))
    cwd_value = source.get("cwd")
    roots_value = source.get("workspace_roots")

    if not cwd_value:
        if roots_value:
            cwd_value = list(roots_value)[0]
        else:
            cwd_value = Path.cwd()

    cwd = Path(str(cwd_value)).expanduser().resolve()
    roots = roots_value or [str(cwd)]
    workspace_roots = [str(Path(str(root)).expanduser().resolve()) for root in roots]
    if str(cwd) not in workspace_roots:
        workspace_roots.insert(0, str(cwd))

    protected = source.get("protected_paths") or DEFAULT_PROTECTED_PATHS
    label = str(source.get("label") or cwd.name or str(cwd))

    return {
        "cwd": str(cwd),
        "workspace_roots": workspace_roots,
        "protected_paths": [str(path) for path in protected],
        "label": label,
    }


def build_default_workspace(config: Mapping[str, Any] | None = None, default_cwd: str | Path | None = None) -> WorkspaceContext:
    """从全局配置构造旧会话和未指定请求使用的默认 workspace。"""
    config = config or {}
    tools = config.get("tools", {}) if isinstance(config, Mapping) else {}
    builtin = tools.get("builtin", {}) if isinstance(tools, Mapping) else {}
    code_config = {}
    if isinstance(builtin, Mapping):
        code_config = dict(builtin.get("code", {}) or {})
    if not code_config and isinstance(tools, Mapping):
        code_config = dict(tools.get("code", {}) or {})

    sandbox = {}
    permissions = tools.get("permissions", {}) if isinstance(tools, Mapping) else {}
    if isinstance(permissions, Mapping):
        sandbox = dict(permissions.get("sandbox", {}) or {})

    roots = code_config.get("workspace_roots") or sandbox.get("workspace_roots")
    protected = code_config.get("protected_paths") or sandbox.get("protected_paths") or DEFAULT_PROTECTED_PATHS
    cwd = roots[0] if roots else (default_cwd or Path.cwd())
    return normalize_workspace({
        "cwd": str(cwd),
        "workspace_roots": roots or [str(cwd)],
        "protected_paths": protected,
    })
