from __future__ import annotations

from enum import Enum
from typing import Iterable, Mapping, Optional, Set


class ToolCapability(str, Enum):
    READ_ONLY = "READ_ONLY"
    NETWORK_READ = "NETWORK_READ"
    FILESYSTEM_READ = "FILESYSTEM_READ"
    FILESYSTEM_WRITE = "FILESYSTEM_WRITE"
    COMMAND_EXEC = "COMMAND_EXEC"
    CONFIG_WRITE = "CONFIG_WRITE"
    MCP_DYNAMIC = "MCP_DYNAMIC"


DEFAULT_TOOL_CAPABILITIES: Mapping[str, Set[ToolCapability]] = {
    "web_search": {ToolCapability.NETWORK_READ},
    "fetch_url": {ToolCapability.NETWORK_READ},
    "read_tool_result": {ToolCapability.READ_ONLY},
    "list_available_tools": {ToolCapability.READ_ONLY},
}


def capabilities_for_tool(
    tool_name: str,
    overrides: Optional[Mapping[str, Iterable[str | ToolCapability]]] = None,
) -> Set[ToolCapability]:
    if overrides and tool_name in overrides:
        return {_to_capability(capability) for capability in overrides[tool_name]}

    if tool_name in DEFAULT_TOOL_CAPABILITIES:
        return set(DEFAULT_TOOL_CAPABILITIES[tool_name])

    if _looks_like_mcp_tool(tool_name):
        return {ToolCapability.MCP_DYNAMIC}

    return {ToolCapability.READ_ONLY}


def _to_capability(value: str | ToolCapability) -> ToolCapability:
    if isinstance(value, ToolCapability):
        return value
    return ToolCapability(value)


def _looks_like_mcp_tool(tool_name: str) -> bool:
    route_name = tool_name[5:] if tool_name.startswith("mcp__") else tool_name
    parts = route_name.split("__", 1)
    return len(parts) == 2 and bool(parts[0]) and bool(parts[1])
