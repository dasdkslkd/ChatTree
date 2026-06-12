# tools/tool_manager.py - Tool manager: register, execute, schema generation
import json
import asyncio
from typing import Any, Dict, List, Optional
from .base import BaseTool
from .web_search import WebSearchTool, FetchUrlTool
from .mcp_client import MCPClient, MCPClientError
from .mcp_tools import MCPSearchTool, MCPUrlReadTool
from ..utils.logger import setup_logger

logger = setup_logger('ToolManager')


class ToolManager:
    """Tool manager supporting both built-in and MCP-based tools."""

    def __init__(self, config: Dict[str, Any]):
        self._tools: Dict[str, BaseTool] = {}
        self._config = config
        self._mcp_client: Optional[MCPClient] = None
        self._mcp_tools: Dict[str, Any] = {}  # name -> MCPSearchTool / MCPUrlReadTool
        self._register_tools(config)

    def _register_tools(self, config: Dict[str, Any]):
        """Register tools based on configuration (MCP or built-in)."""
        tools_config = config.get("tools", {})
        mcp_config = tools_config.get("mcp", {})

        if mcp_config.get("enabled", False):
            self._register_mcp_tools(mcp_config)
        else:
            self._register_builtin_tools(tools_config)

    def _register_mcp_tools(self, mcp_config: Dict[str, Any]):
        """Register MCP-based tools."""
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

        logger.info(f"Registered MCP tools from {endpoint}")

    def _register_builtin_tools(self, tools_config: Dict[str, Any]):
        """Register built-in tools (direct SearXNG/crawl4ai)."""
        search_config = tools_config.get("web_search", {})
        if search_config.get("enabled", True):
            searxng_cfg = search_config.get("searxng", {})
            crawl_cfg = search_config.get("crawl4ai", {})
            self.register(WebSearchTool(searxng_cfg))
            self.register(FetchUrlTool(crawl_cfg))
            logger.info("Registered web_search and fetch_url tools")

    async def init_mcp(self):
        """Initialize MCP session and discover tool schemas.
        Call this after construction when using MCP tools."""
        if not self._mcp_client:
            return

        try:
            # Check if MCP server is reachable
            healthy = await self._mcp_client.health_check()
            if not healthy:
                logger.warning("MCP server health check failed, tools may not work")

            # Initialize MCP session
            await self._mcp_client.initialize()

            # Discover tool schemas from MCP server
            mcp_tools = await self._mcp_client.list_tools()
            for mcp_tool in mcp_tools:
                tool_name = mcp_tool.get("name", "")
                # Update schema on matching local tools
                for local_tool in self._mcp_tools.values():
                    if hasattr(local_tool, "_tool_name") and local_tool._tool_name == tool_name:
                        local_tool.update_schema(mcp_tool)
                        logger.info(f"Synced schema for MCP tool: {tool_name}")

        except MCPClientError as e:
            logger.error(f"MCP initialization failed: {e}")
            logger.warning("MCP tools registered but may not work correctly")
        except Exception as e:
            logger.error(f"Unexpected error during MCP init: {e}")

    def register(self, tool: BaseTool):
        """Register a tool."""
        self._tools[tool.name] = tool
        logger.info(f"Tool registered: {tool.name}")

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        """List all registered tool names."""
        return list(self._tools.keys())

    def get_openai_tools(self) -> List[Dict[str, Any]]:
        """Get all tools as OpenAI function calling schema."""
        return [tool.to_openai_tool() for tool in self._tools.values()]

    async def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool by name."""
        tool = self._tools.get(name)
        if not tool:
            logger.error(f"Tool not found: {name}")
            return json.dumps({"error": f"Tool '{name}' not found"}, ensure_ascii=False)

        try:
            logger.info(f"Executing tool: {name} with args: {json.dumps(arguments, ensure_ascii=False)[:200]}")
            result = await tool.execute(**arguments)
            logger.info(f"Tool {name} returned {len(result)} chars")
            return result
        except Exception as e:
            logger.error(f"Tool {name} execution failed: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    async def close(self):
        """Clean up resources."""
        for tool in self._tools.values():
            if hasattr(tool, "close"):
                await tool.close()
        if self._mcp_client:
            await self._mcp_client.close()
            self._mcp_client = None
