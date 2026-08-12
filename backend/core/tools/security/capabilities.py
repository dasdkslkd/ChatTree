from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Set


class ToolCapability(str, Enum):
    READ_ONLY = "READ_ONLY"
    PARALLEL_SAFE = "PARALLEL_SAFE"
    NETWORK_READ = "NETWORK_READ"
    FILESYSTEM_READ = "FILESYSTEM_READ"
    FILESYSTEM_WRITE = "FILESYSTEM_WRITE"
    COMMAND_EXEC = "COMMAND_EXEC"
    CONFIG_WRITE = "CONFIG_WRITE"
    MUTATES_WORKSPACE = "MUTATES_WORKSPACE"
    MUTATES_RUNTIME_STATE = "MUTATES_RUNTIME_STATE"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    MCP_DYNAMIC = "MCP_DYNAMIC"


DEFAULT_TOOL_CAPABILITIES: Mapping[str, Set[ToolCapability]] = {
    "web": {ToolCapability.NETWORK_READ, ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE},
    "tools": {ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE},
    "memory": {
        ToolCapability.CONFIG_WRITE,
        ToolCapability.MUTATES_RUNTIME_STATE,
    },
    "enter_plan_mode": {ToolCapability.MUTATES_RUNTIME_STATE},
    "ask_user_question": {ToolCapability.MUTATES_RUNTIME_STATE},
    "exit_plan_mode": {ToolCapability.MUTATES_RUNTIME_STATE},
    "glob": {ToolCapability.FILESYSTEM_READ, ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE},
    "read": {ToolCapability.FILESYSTEM_READ, ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE},
    "grep": {ToolCapability.FILESYSTEM_READ, ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE},
    "edit": {
        ToolCapability.FILESYSTEM_WRITE,
        ToolCapability.MUTATES_WORKSPACE,
        ToolCapability.REQUIRES_APPROVAL,
    },
    "shell": {
        ToolCapability.COMMAND_EXEC,
        ToolCapability.MUTATES_RUNTIME_STATE,
        ToolCapability.REQUIRES_APPROVAL,
    },
    "agent": {ToolCapability.MUTATES_RUNTIME_STATE},
    "create_task": {ToolCapability.MUTATES_RUNTIME_STATE},
    "set_task_step": {ToolCapability.MUTATES_RUNTIME_STATE},
    "cancel_task": {ToolCapability.MUTATES_RUNTIME_STATE},
}


class UnknownToolCapabilitiesError(LookupError):
    """Raised when a tool has no explicit capability declaration."""


def capabilities_for_tool(
    tool_name: str,
    overrides: Optional[Mapping[str, Iterable[str | ToolCapability]]] = None,
) -> Set[ToolCapability]:
    if overrides and tool_name in overrides:
        return {_to_capability(capability) for capability in overrides[tool_name]}

    if tool_name in DEFAULT_TOOL_CAPABILITIES:
        return set(DEFAULT_TOOL_CAPABILITIES[tool_name])

    raise UnknownToolCapabilitiesError(f"Tool '{tool_name}' has no declared capabilities")


def capabilities_for_registered_tool(tool: Any) -> Set[ToolCapability]:
    explicit = getattr(tool, "capabilities", None)
    if explicit is not None:
        return {_to_capability(capability) for capability in explicit}
    return capabilities_for_tool(str(getattr(tool, "name", "")))


def capabilities_for_mcp_tool(tool_schema: Mapping[str, Any]) -> Set[ToolCapability]:
    annotations = tool_schema.get("annotations")
    if not isinstance(annotations, Mapping):
        return {ToolCapability.MCP_DYNAMIC}
    if annotations.get("readOnlyHint") is True:
        return {ToolCapability.MCP_DYNAMIC, ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE}
    return {ToolCapability.MCP_DYNAMIC}


def is_parallel_safe(capabilities: Iterable[ToolCapability]) -> bool:
    return ToolCapability.PARALLEL_SAFE in set(capabilities)


def _to_capability(value: str | ToolCapability) -> ToolCapability:
    if isinstance(value, ToolCapability):
        return value
    return ToolCapability(value)
