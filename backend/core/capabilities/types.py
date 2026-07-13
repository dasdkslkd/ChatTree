from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class CapabilityKind(str, Enum):
    """Supported capability categories."""

    SKILL = "skill"
    AGENT = "agent"
    PLUGIN = "plugin"
    MCP_SERVER = "mcp_server"
    HOOK = "hook"


class CapabilitySource(str, Enum):
    """Where a capability definition came from."""

    SYSTEM = "system"
    USER = "user"
    PROJECT = "project"
    PLUGIN = "plugin"


@dataclass
class CapabilityDefinition:
    """A discovered capability definition."""

    name: str
    kind: CapabilityKind
    source: CapabilitySource
    description: str = ""
    path: Optional[Path] = None
    plugin_id: Optional[str] = None
    plugin_name: Optional[str] = None
    namespace: Optional[str] = None
    when_to_use: Optional[str] = None
    allowed_tools: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    model: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentDefinition:
    """A discovered agent definition."""

    name: str
    description: str = ""
    system_prompt: str = ""
    tools: Optional[List[str]] = None
    disallowed_tools: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    model: Optional[str] = None
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    permission_mode: Optional[str] = None
    max_tool_rounds: Optional[int] = None
    timeout_seconds: Optional[int] = None
    output_mode: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None
    max_turns: Optional[int] = None
    plugin_id: Optional[str] = None
    plugin_name: Optional[str] = None
    path: Optional[Path] = None
    source: CapabilitySource = CapabilitySource.PROJECT
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LoadedPlugin:
    """Plugin load result and the capability roots it contributes."""

    plugin_id: str
    name: str
    root: Path
    enabled: bool = True
    description: str = ""
    version: Optional[str] = None
    skill_roots: list[Path] = field(default_factory=list)
    agent_roots: list[Path] = field(default_factory=list)
    hooks: List[Path] = field(default_factory=list)
    mcp_servers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    interface: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def is_active(self) -> bool:
        return self.enabled and not self.error


@dataclass
class PluginLoadOutcome:
    """Aggregate result of loading plugin capability roots."""

    plugins: list[LoadedPlugin] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def active_plugins(self) -> list[LoadedPlugin]:
        return [plugin for plugin in self.plugins if plugin.is_active()]

    def effective_skill_roots(self) -> list[Path]:
        return _unique_paths(
            root
            for plugin in self.active_plugins()
            for root in plugin.skill_roots
        )

    def effective_agent_roots(self) -> list[Path]:
        return _unique_paths(
            root
            for plugin in self.active_plugins()
            for root in plugin.agent_roots
        )


@dataclass
class SkillInjection:
    """Resolved skill content ready to inject into an agent context."""

    name: str
    path: Path
    content: str


def _unique_paths(paths: Any) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        normalized = Path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
