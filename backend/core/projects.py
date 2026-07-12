from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Optional

from backend.core.capabilities.registry import CapabilityRegistry


PROJECTS_CONFIG_KEY = "projects"


def normalize_project_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except OSError:
        return text


def workspace_project_path(workspace: Mapping[str, Any] | None) -> str:
    if not isinstance(workspace, Mapping):
        return ""
    return normalize_project_path(workspace.get("cwd"))


def normalize_project_config(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, Mapping) else {}
    result: dict[str, Any] = {
        "label": str(source.get("label") or ""),
        "visible": source.get("visible", True) is not False,
    }
    for key in ("enabled_skills", "enabled_mcp_servers", "enabled_agents"):
        result[key] = _normalize_optional_string_list(source.get(key))
    return result


def normalize_projects_config(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    projects: dict[str, dict[str, Any]] = {}
    for path_value, config in raw.items():
        path = normalize_project_path(path_value)
        if not path:
            continue
        projects[path] = normalize_project_config(config)
    return projects


def project_config_for_workspace(
    config_data: Mapping[str, Any] | None,
    workspace: Mapping[str, Any] | None,
) -> Optional[dict[str, Any]]:
    path = workspace_project_path(workspace)
    if not path or not isinstance(config_data, Mapping):
        return None
    return normalize_projects_config(config_data.get(PROJECTS_CONFIG_KEY)).get(path)


def project_is_visible(
    config_data: Mapping[str, Any] | None,
    workspace: Mapping[str, Any] | None,
) -> bool:
    project = project_config_for_workspace(config_data, workspace)
    return True if project is None else project.get("visible", True) is not False


def allowed_project_names(
    config_data: Mapping[str, Any] | None,
    workspace: Mapping[str, Any] | None,
    key: str,
) -> Optional[set[str]]:
    project = project_config_for_workspace(config_data, workspace)
    if project is None:
        return None
    value = project.get(key)
    if value is None:
        return None
    return {str(item) for item in value if str(item)}


def filter_capability_registry_for_workspace(
    registry: CapabilityRegistry | None,
    config_data: Mapping[str, Any] | None,
    workspace: Mapping[str, Any] | None,
) -> CapabilityRegistry | None:
    if registry is None:
        return None

    allowed_skills = allowed_project_names(config_data, workspace, "enabled_skills")
    allowed_agents = allowed_project_names(config_data, workspace, "enabled_agents")
    allowed_mcp = allowed_project_names(config_data, workspace, "enabled_mcp_servers")
    if allowed_skills is None and allowed_agents is None and allowed_mcp is None:
        return registry

    scoped = CapabilityRegistry()
    scoped.add_capabilities(
        skill for skill in registry.skills()
        if allowed_skills is None or skill.name in allowed_skills
    )
    scoped.add_agents(
        agent for agent in registry.agents()
        if allowed_agents is None or agent.name in allowed_agents
    )

    visible_plugin_ids: set[str] = set()
    if allowed_skills is None:
        visible_plugin_ids.update(skill.plugin_id for skill in registry.skills() if skill.plugin_id)
    else:
        visible_plugin_ids.update(
            skill.plugin_id
            for skill in registry.skills()
            if skill.plugin_id and skill.name in allowed_skills
        )
    if allowed_agents is None:
        visible_plugin_ids.update(agent.plugin_id for agent in registry.agents() if agent.plugin_id)
    else:
        visible_plugin_ids.update(
            agent.plugin_id
            for agent in registry.agents()
            if agent.plugin_id and agent.name in allowed_agents
        )
    for plugin in registry.plugins():
        exposed_mcp = {f"{plugin.name}.{server_name}" for server_name in plugin.mcp_servers}
        if allowed_mcp is None and plugin.mcp_servers:
            visible_plugin_ids.add(plugin.plugin_id)
        elif exposed_mcp & (allowed_mcp or set()):
            visible_plugin_ids.add(plugin.plugin_id)
        if plugin.plugin_id in visible_plugin_ids:
            scoped.add_plugins([plugin])
    return scoped


def filter_runtime_config_for_workspace(
    config_data: Mapping[str, Any] | None,
    workspace: Mapping[str, Any] | None,
) -> dict[str, Any]:
    runtime_config = deepcopy(config_data) if isinstance(config_data, Mapping) else {}
    allowed_mcp = allowed_project_names(runtime_config, workspace, "enabled_mcp_servers")
    if allowed_mcp is None:
        return runtime_config

    tools_config = runtime_config.get("tools")
    if not isinstance(tools_config, dict):
        return runtime_config
    mcp_config = tools_config.get("mcp")
    if not isinstance(mcp_config, dict):
        return runtime_config
    servers = mcp_config.get("servers")
    if not isinstance(servers, dict):
        return runtime_config
    mcp_config["servers"] = {
        name: server
        for name, server in servers.items()
        if name in allowed_mcp
    }
    return runtime_config


def _normalize_optional_string_list(value: Any) -> Optional[list[str]]:
    if value is None:
        return None
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
