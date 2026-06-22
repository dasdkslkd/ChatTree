from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from backend.core.capabilities.types import (
    AgentDefinition,
    CapabilityDefinition,
    CapabilityKind,
    LoadedPlugin,
)


class CapabilityRegistry:
    """In-memory registry for discovered capabilities."""

    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityDefinition] = {}
        self._agents: dict[str, AgentDefinition] = {}
        self._plugins: dict[str, LoadedPlugin] = {}

    def add_capabilities(
        self, capabilities: Iterable[CapabilityDefinition]
    ) -> None:
        for capability in capabilities:
            self._capabilities.setdefault(capability.name, capability)

    def add_agents(self, agents: Iterable[AgentDefinition]) -> None:
        for agent in agents:
            self._agents.setdefault(agent.name, agent)

    def add_plugins(self, plugins: Iterable[LoadedPlugin]) -> None:
        for plugin in plugins:
            self._plugins.setdefault(plugin.plugin_id, plugin)

    def get(self, name: str) -> Optional[CapabilityDefinition]:
        return self._capabilities.get(name)

    def get_agent(self, name: str) -> Optional[AgentDefinition]:
        return self._agents.get(name)

    def skills(self) -> list[CapabilityDefinition]:
        return [
            capability
            for capability in self._capabilities.values()
            if capability.kind == CapabilityKind.SKILL
        ]

    def agents(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    def plugins(self) -> list[LoadedPlugin]:
        return list(self._plugins.values())

    def inventory(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "skills": [_to_inventory_dict(skill) for skill in self.skills()],
            "agents": [_to_inventory_dict(agent) for agent in self._agents.values()],
            "plugins": [
                _to_inventory_dict(plugin) for plugin in self._plugins.values()
            ],
        }


def _to_inventory_dict(item: Any) -> dict[str, Any]:
    if not is_dataclass(item):
        return _json_friendly(item)

    result: dict[str, Any] = {}
    for field in fields(item):
        result[field.name] = _json_friendly(getattr(item, field.name))
    return result


def _json_friendly(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    if is_dataclass(value):
        return _to_inventory_dict(value)
    if isinstance(value, dict):
        return {key: _json_friendly(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_friendly(item) for item in value]
    return value
