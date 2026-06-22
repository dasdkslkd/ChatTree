from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from backend.core.capabilities.paths import read_text_utf8, resolve_inside_root
from backend.core.capabilities.types import LoadedPlugin, PluginLoadOutcome


PLUGIN_MANIFEST = ".chattree-plugin/plugin.json"


def load_plugins_from_roots(roots: Iterable[str | Path]) -> PluginLoadOutcome:
    outcome = PluginLoadOutcome()
    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            continue

        for plugin_root in sorted(
            path for path in root_path.iterdir() if path.is_dir()
        ):
            manifest_path = plugin_root / PLUGIN_MANIFEST
            if not manifest_path.exists():
                continue
            outcome.plugins.append(load_plugin(plugin_root, manifest_path))

    return outcome


def load_plugin(
    plugin_root: str | Path,
    manifest_path: str | Path,
    enabled: bool = True,
) -> LoadedPlugin:
    root = Path(plugin_root).resolve()
    manifest_file = resolve_inside_root(root, manifest_path)
    manifest = json.loads(read_text_utf8(manifest_file))
    if not isinstance(manifest, dict):
        manifest = {}

    paths = manifest.get("paths")
    if not isinstance(paths, dict):
        paths = {}

    manifest_name = _optional_str(manifest.get("name"))
    interface = _dict_or_empty(manifest.get("interface")).copy()
    if manifest_name:
        interface.setdefault("display_name", manifest_name)
    plugin_enabled = enabled and _parse_enabled(manifest.get("enabled"), True)

    return LoadedPlugin(
        plugin_id=f"{root.name}@local",
        name=root.name,
        root=root,
        enabled=plugin_enabled,
        description=_optional_str(manifest.get("description")) or "",
        version=_optional_str(manifest.get("version")),
        skill_roots=_existing_paths(root, paths.get("skills", "skills")),
        agent_roots=_existing_paths(root, paths.get("agents", "agents")),
        hooks=_existing_paths(root, paths.get("hooks", [])),
        mcp_servers=_dict_or_empty(paths.get("mcp_servers")),
        interface=interface,
    )


def _existing_paths(root: Path, value: Any) -> list[Path]:
    return [
        resolved
        for resolved in (
            _resolve_manifest_path(root, item) for item in _as_path_list(value)
        )
        if resolved.exists()
    ]


def _resolve_manifest_path(root: Path, value: Any) -> Path:
    return resolve_inside_root(root, str(value))


def _as_path_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return []


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_enabled(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
