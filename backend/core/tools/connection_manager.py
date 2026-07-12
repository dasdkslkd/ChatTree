# tools/connection_manager.py - aggregate and route MCP servers
import json
import re
from typing import Any, Dict, List

from .mcp_client import MCPClient
from .mcp_server import McpServerManager
from ..utils.logger import setup_logger

logger = setup_logger("MCPConnectionManager")


def _safe_tool_name(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", value.strip())
    return name or "tool"


class ConnectionManager:
    """Aggregate multiple MCP servers and route tool calls by callable name."""

    def __init__(self):
        self._servers: Dict[str, McpServerManager] = {}
        self._routes: Dict[str, Dict[str, str]] = {}

    async def add_server(self, name: str, config: Dict[str, Any]):
        if name in self._servers:
            await self.remove_server(name)
        server = McpServerManager(name, config)
        try:
            await server.start()
            self._servers[name] = server
            self._rebuild_routes()
        except Exception:
            await server.stop()
            raise

    async def remove_server(self, name: str):
        server = self._servers.pop(name, None)
        if server:
            await server.stop()
        self._rebuild_routes()

    def _rebuild_routes(self):
        self._routes.clear()
        seen: set[str] = set()
        for server_name, server in self._servers.items():
            safe_server = _safe_tool_name(server_name)
            for tool in server.tools:
                original_name = tool.get("name", "")
                safe_tool = _safe_tool_name(original_name)
                callable_name = f"{safe_server}__{safe_tool}"
                if callable_name in seen:
                    suffix = 2
                    while f"{callable_name}_{suffix}" in seen:
                        suffix += 1
                    callable_name = f"{callable_name}_{suffix}"
                seen.add(callable_name)
                self._routes[callable_name] = {
                    "server": server_name,
                    "tool": original_name,
                }

    def list_all_tools(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for callable_name, route in self._routes.items():
            server = self._servers.get(route["server"])
            if not server:
                continue
            for tool in server.tools:
                if tool.get("name") != route["tool"]:
                    continue
                schema = MCPClient.mcp_tool_to_openai(tool)
                schema["function"]["name"] = callable_name
                items.append({
                    "server": route["server"],
                    "tool": tool,
                    "callable_name": callable_name,
                    "openai_schema": schema,
                })
                break
        return items

    async def call_tool(self, callable_name: str, arguments: Dict[str, Any]) -> str:
        route = self._routes.get(callable_name)
        if not route:
            return json.dumps({"error": f"MCP tool route '{callable_name}' not found"}, ensure_ascii=False)
        server = self._servers.get(route["server"])
        if not server:
            return json.dumps({"error": f"MCP server '{route['server']}' not found"}, ensure_ascii=False)
        return await server.call_tool(route["tool"], arguments)

    def has_tool(self, callable_name: str) -> bool:
        return callable_name in self._routes

    def server_for_tool(self, callable_name: str) -> str | None:
        route = self._routes.get(callable_name)
        return route.get("server") if route else None

    def list_server_names(self) -> List[str]:
        return list(self._servers.keys())

    async def list_server_statuses(self) -> Dict[str, Dict[str, Any]]:
        statuses: Dict[str, Dict[str, Any]] = {}
        for name, server in list(self._servers.items()):
            statuses[name] = await server.status()
        return statuses

    async def close(self):
        for server in list(self._servers.values()):
            try:
                await server.stop()
            except Exception as e:
                logger.warning(f"Failed to stop MCP server '{server.name}': {e}")
        self._servers.clear()
        self._routes.clear()
