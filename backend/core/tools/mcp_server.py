# tools/mcp_server.py - MCP server lifecycle management
import asyncio
import json
from typing import Any, Dict, List, Optional

from .mcp_client import MCPClient, MCPClientError
from .tool_filter import ToolFilter
from ..utils.logger import setup_logger

logger = setup_logger("MCPServer")


class McpServerManager:
    """Manage one MCP server connection and its discovered tool cache."""

    def __init__(self, name: str, server_config: Dict[str, Any]):
        self.name = name
        self.config = server_config
        self.transport = server_config.get("transport", "streamable_http")
        self.endpoint = server_config.get("endpoint", "http://localhost:3001")
        self.command = server_config.get("command")
        self.timeout = float(server_config.get("timeout", 30.0))
        self.startup_timeout = float(server_config.get("startup_timeout", self.timeout))
        self.tool_call_timeout = float(server_config.get("tool_call_timeout", server_config.get("call_timeout", 120.0)))
        self.filter = ToolFilter(
            enabled=server_config.get("enabled_tools"),
            disabled=server_config.get("disabled_tools"),
        )
        self._process: Optional[asyncio.subprocess.Process] = None
        self._client: Optional[MCPClient] = None
        self._tools_cache: List[Dict[str, Any]] = []

    @property
    def tools(self) -> List[Dict[str, Any]]:
        return list(self._tools_cache)

    async def start(self):
        if self.transport == "stdio":
            if not self.command:
                raise MCPClientError(f"MCP server '{self.name}' missing stdio command")
            command = self.command if isinstance(self.command, list) else [self.command]
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.config.get("cwd"),
            )
            self._client = MCPClient(
                transport="stdio",
                process=self._process,
                timeout=self.timeout,
            )
        else:
            self._client = MCPClient(
                endpoint=self.endpoint,
                transport="streamable_http",
                timeout=self.timeout,
            )

        await asyncio.wait_for(self._client.initialize(), timeout=self.startup_timeout)
        discovered = await asyncio.wait_for(self._client.list_tools(), timeout=self.startup_timeout)
        self._tools_cache = [
            tool for tool in discovered
            if self.filter.is_allowed(tool.get("name", ""))
        ]
        logger.info(f"MCP server '{self.name}' started with {len(self._tools_cache)} visible tools")

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        if not self._client:
            return json.dumps({"error": f"MCP server '{self.name}' is not initialized"}, ensure_ascii=False)
        if not self.filter.is_allowed(tool_name):
            return json.dumps({"error": f"MCP tool '{tool_name}' is disabled"}, ensure_ascii=False)
        return await asyncio.wait_for(
            self._client.call_tool(tool_name, arguments),
            timeout=self.tool_call_timeout,
        )

    async def stop(self):
        if self._client:
            await self._client.close()
            self._client = None
        if self._process:
            if self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
            self._process = None
