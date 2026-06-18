# tools/tool_manager.py - Tool manager: register, expose, execute
import json
from typing import Any, Dict, List, Optional

from .base import BaseTool
from .connection_manager import ConnectionManager
from .mcp_client import MCPClient, MCPClientError
from .mcp_tools import MCPSearchTool, MCPUrlReadTool
from .tool_filter import ToolFilter
from .web_search import FetchUrlTool, WebSearchTool
from ..utils.logger import setup_logger

logger = setup_logger("ToolManager")

MAX_TOOL_RESULT_LENGTH = 8000


def truncate_tool_result(result: str, max_length: int = MAX_TOOL_RESULT_LENGTH) -> str:
    if len(result) <= max_length:
        return result
    return result[:max_length] + f"\n\n[结果已截断，共 {len(result)} 字符]"


class ToolManager:
    """Tool manager supporting built-in tools and MCP servers."""

    def __init__(self, config: Dict[str, Any]):
        self._tools: Dict[str, BaseTool] = {}
        self._config = config
        self._mcp_client: Optional[MCPClient] = None
        self._mcp_tools: Dict[str, BaseTool] = {}
        self._connection_manager = ConnectionManager()
        self._mcp_servers_config: Dict[str, Dict[str, Any]] = {}
        tools_config = config.get("tools", {})
        self._enabled = tools_config.get("enabled", True)
        self._max_result_length = int(tools_config.get("max_result_length", MAX_TOOL_RESULT_LENGTH))
        self._filter = ToolFilter(
            enabled=tools_config.get("enabled_tools"),
            disabled=tools_config.get("disabled_tools"),
        )
        if self._enabled:
            self._register_tools(config)

    def _register_tools(self, config: Dict[str, Any]):
        """Register tools based on legacy or redesigned configuration."""
        tools_config = config.get("tools", {})
        mcp_config = tools_config.get("mcp", {})
        servers = mcp_config.get("servers") or {}

        if mcp_config.get("enabled", False) and servers:
            builtin_config = tools_config.get("builtin", {})
            if builtin_config.get("enabled", False) or builtin_config.get("web_search", {}).get("enabled", False):
                self._register_builtin_tools(builtin_config)
            self._mcp_servers_config = servers
            return

        if mcp_config.get("enabled", False):
            self._register_legacy_mcp_tools(mcp_config)
            return

        self._register_builtin_tools(tools_config.get("builtin", tools_config))

    def _register_legacy_mcp_tools(self, mcp_config: Dict[str, Any]):
        """Register compatibility adapters for the old single-server MCP config."""
        endpoint = mcp_config.get("endpoint", "http://localhost:3001")
        timeout = mcp_config.get("timeout", 30.0)
        search_tool_name = mcp_config.get("search_tool", "searxng_web_search")
        url_read_tool_name = mcp_config.get("url_read_tool", "web_url_read")

        self._mcp_client = MCPClient(endpoint=endpoint, timeout=timeout)

        search_tool = MCPSearchTool(self._mcp_client, tool_name=search_tool_name)
        url_read_tool = MCPUrlReadTool(self._mcp_client, tool_name=url_read_tool_name)

        self.register(search_tool)
        self.register(url_read_tool)
        self._mcp_tools = {
            "web_search": search_tool,
            "fetch_url": url_read_tool,
        }

        logger.info(f"Registered legacy MCP tools from {endpoint}")

    def _register_builtin_tools(self, tools_config: Dict[str, Any]):
        """Register built-in direct HTTP tools."""
        search_config = tools_config.get("web_search", {})
        if search_config.get("enabled", True):
            searxng_cfg = search_config.get("searxng", search_config.get("searxng_config", {}))
            crawl_cfg = search_config.get("crawl4ai", tools_config.get("fetch_url", {}))
            self.register(WebSearchTool(searxng_cfg))
            self.register(FetchUrlTool(crawl_cfg))
            logger.info("Registered built-in web_search and fetch_url tools")

    async def init(self):
        """Initialize all configured MCP connections."""
        if not self._enabled:
            return
        if self._mcp_client:
            await self.init_mcp()
        for name, server_cfg in self._mcp_servers_config.items():
            if server_cfg.get("enabled", True) is False:
                continue
            try:
                await self._connection_manager.add_server(name, server_cfg)
            except Exception as e:
                logger.error(f"MCP server '{name}' initialization failed: {e}")

    async def init_mcp(self):
        """Initialize legacy MCP session and discover schemas."""
        if not self._mcp_client:
            return

        try:
            healthy = await self._mcp_client.health_check()
            if not healthy:
                logger.warning("MCP server health check failed, tools may not work")

            await self._mcp_client.initialize()
            mcp_tools = await self._mcp_client.list_tools()
            for mcp_tool in mcp_tools:
                tool_name = mcp_tool.get("name", "")
                for local_tool in self._mcp_tools.values():
                    if getattr(local_tool, "_tool_name", None) == tool_name:
                        local_tool.update_schema(mcp_tool)  # type: ignore[attr-defined]
                        logger.info(f"Synced schema for MCP tool: {tool_name}")

        except MCPClientError as e:
            logger.error(f"MCP initialization failed: {e}")
            logger.warning("MCP tools registered but may not work correctly")
        except Exception as e:
            logger.error(f"Unexpected error during MCP init: {e}")

    def register(self, tool: BaseTool):
        """Register a local tool."""
        self._tools[tool.name] = tool
        logger.info(f"Tool registered: {tool.name}")

    def get_tool(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        names = [
            name for name in self._tools
            if self._filter.is_allowed(name)
        ]
        names.extend(
            info["callable_name"]
            for info in self._connection_manager.list_all_tools()
            if self._filter.is_allowed(
                info["callable_name"],
                aliases=(info["tool"].get("name", ""), f"{info['server']}.{info['tool'].get('name', '')}"),
            )
        )
        return names

    def get_openai_tools(self) -> List[Dict[str, Any]]:
        """Get all model-visible tools as OpenAI function calling schemas."""
        tools: List[Dict[str, Any]] = []
        for name, tool in self._tools.items():
            if self._filter.is_allowed(name):
                tools.append(tool.to_openai_tool())
        for info in self._connection_manager.list_all_tools():
            original_name = info["tool"].get("name", "")
            if self._filter.is_allowed(
                info["callable_name"],
                aliases=(original_name, f"{info['server']}.{original_name}"),
            ):
                tools.append(info["openai_schema"])
        return tools

    async def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool by exposed name."""
        if not self._filter.is_allowed(name):
            return json.dumps({"error": f"Tool '{name}' is disabled"}, ensure_ascii=False)

        try:
            if self._connection_manager.has_tool(name):
                logger.info(f"Executing MCP tool route: {name}")
                result = await self._connection_manager.call_tool(name, arguments)
                return truncate_tool_result(result, self._max_result_length)

            tool = self._tools.get(name)
            if not tool:
                logger.error(f"Tool not found: {name}")
                return json.dumps({"error": f"Tool '{name}' not found"}, ensure_ascii=False)

            logger.info(f"Executing tool: {name} with args: {json.dumps(arguments, ensure_ascii=False)[:200]}")
            result = await tool.execute(**arguments)
            logger.info(f"Tool {name} returned {len(result)} chars")
            return truncate_tool_result(result, self._max_result_length)
        except Exception as e:
            logger.error(f"Tool {name} execution failed: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    async def close(self):
        """Clean up resources."""
        for tool in self._tools.values():
            if hasattr(tool, "close"):
                await tool.close()  # type: ignore[attr-defined]
        if self._mcp_client:
            await self._mcp_client.close()
            self._mcp_client = None
        await self._connection_manager.close()
