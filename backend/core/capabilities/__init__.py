"""Capability loading primitives."""

from backend.core.capabilities.types import (
    AgentDefinition,
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySource,
    LoadedPlugin,
    PluginLoadOutcome,
    SkillInjection,
)
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.plugin_loader import load_plugin, load_plugins_from_roots
from backend.core.capabilities.agent_loader import load_agent_file, load_agent_roots
from backend.core.capabilities.skill_loader import load_skill_file, load_skill_roots

__all__ = [
    "AgentDefinition",
    "CapabilityRegistry",
    "CapabilityDefinition",
    "CapabilityKind",
    "CapabilitySource",
    "LoadedPlugin",
    "PluginLoadOutcome",
    "SkillInjection",
    "load_agent_file",
    "load_agent_roots",
    "load_plugin",
    "load_plugins_from_roots",
    "load_skill_file",
    "load_skill_roots",
]
