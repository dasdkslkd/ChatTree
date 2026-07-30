from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from backend.core.capabilities.agent_loader import load_agent_roots
from backend.core.capabilities.plugin_loader import load_plugins_from_roots
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.skill_loader import load_skill_roots
from backend.core.capabilities.types import CapabilitySource
from backend.core.persistence.home import resolve_chattree_home
from backend.core.prompts.catalog import PROMPT_SOURCES, TEMPLATE_ROOT


def build_capability_registry(
    project_root: Path,
    config_data: dict | None = None,
    capability_home: Path | str | None = None,
) -> CapabilityRegistry:
    """Build the read-only capability registry for a project."""
    home = resolve_chattree_home(capability_home)
    capabilities_config = _capabilities_config(config_data)
    skill_roots = _roots_from_config(
        home,
        [home / "skills"],
        capabilities_config.get("skill_roots"),
    )
    project_agent_roots = _roots_from_config(
        home,
        [],
        capabilities_config.get("agent_roots"),
    )
    plugin_roots = _roots_from_config(
        home,
        [home / "plugins"],
        capabilities_config.get("plugin_roots"),
    )

    registry = CapabilityRegistry()
    registry.add_capabilities(
        load_skill_roots(skill_roots, source=CapabilitySource.PROJECT)
    )
    registry.add_agents(
        load_agent_roots([home / "agents"], source=CapabilitySource.USER)
    )
    registry.add_agents(
        load_agent_roots(project_agent_roots, source=CapabilitySource.PROJECT)
    )
    system_agents = load_agent_roots(
        [TEMPLATE_ROOT / "agents"],
        source=CapabilitySource.SYSTEM,
    )
    required_system_agents = {
        name.removeprefix("agent:")
        for name in PROMPT_SOURCES
        if name.startswith("agent:")
    }
    loaded_system_agents = {agent.name for agent in system_agents}
    if loaded_system_agents != required_system_agents:
        missing = sorted(required_system_agents - loaded_system_agents)
        unexpected = sorted(loaded_system_agents - required_system_agents)
        raise RuntimeError(
            f"invalid packaged agent set: missing={missing}, unexpected={unexpected}"
        )
    registry.add_agents(system_agents)

    plugin_outcome = load_plugins_from_roots(plugin_roots)
    active_plugins = plugin_outcome.active_plugins()
    registry.add_plugins(active_plugins)
    for plugin in active_plugins:
        registry.add_capabilities(
            load_skill_roots(
                plugin.skill_roots,
                source=CapabilitySource.PLUGIN,
                plugin_id=plugin.plugin_id,
                plugin_name=plugin.name,
            )
        )
        registry.add_agents(
            load_agent_roots(
                plugin.agent_roots,
                source=CapabilitySource.PLUGIN,
                plugin_id=plugin.plugin_id,
                plugin_name=plugin.name,
            )
        )

    return registry


def build_runtime_config_with_plugin_mcp(
    config_data: dict | None,
    registry: CapabilityRegistry,
) -> dict:
    """Return runtime config with active plugin MCP servers overlaid.

    The returned config is safe to pass to runtime managers. It never mutates or
    persists the user config, and user-defined MCP server names win on conflict.
    """
    runtime_config = deepcopy(config_data) if isinstance(config_data, dict) else {}
    plugin_servers = _plugin_mcp_servers(registry)
    if not plugin_servers:
        return runtime_config

    tools_config = runtime_config.setdefault("tools", {})
    if not isinstance(tools_config, dict):
        tools_config = {}
        runtime_config["tools"] = tools_config

    mcp_config = tools_config.setdefault("mcp", {})
    if not isinstance(mcp_config, dict):
        mcp_config = {}
        tools_config["mcp"] = mcp_config

    servers_config = mcp_config.setdefault("servers", {})
    if not isinstance(servers_config, dict):
        servers_config = {}
        mcp_config["servers"] = servers_config

    for server_name, server_config in plugin_servers.items():
        servers_config.setdefault(server_name, server_config)

    mcp_config["enabled"] = True
    return runtime_config


def _capabilities_config(config_data: dict | None) -> dict[str, Any]:
    if not isinstance(config_data, dict):
        return {}
    capabilities = config_data.get("capabilities", {})
    return capabilities if isinstance(capabilities, dict) else {}


def _plugin_mcp_servers(registry: CapabilityRegistry) -> dict[str, dict[str, Any]]:
    servers: dict[str, dict[str, Any]] = {}
    for plugin in registry.plugins():
        for server_name, server_config in plugin.mcp_servers.items():
            if not isinstance(server_config, dict):
                continue
            exposed_name = f"{plugin.name}.{server_name}"
            server_copy = deepcopy(server_config)
            server_copy.update(
                {
                    "source": "plugin",
                    "plugin_id": plugin.plugin_id,
                    "plugin_name": plugin.name,
                }
            )
            servers.setdefault(exposed_name, server_copy)
    return servers


def _roots_from_config(
    project_root: Path,
    defaults: list[Path],
    configured: Any,
) -> list[Path]:
    return _unique_paths(
        [
            *defaults,
            *(_resolve_project_path(project_root, item) for item in _as_list(configured)),
        ]
    )


def _resolve_project_path(project_root: Path, value: Any) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project_root / path


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return []


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        normalized = Path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
