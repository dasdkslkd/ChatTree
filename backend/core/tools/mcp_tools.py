# tools/mcp_tools.py - MCP tool adapters (MCPSearchTool, MCPUrlReadTool)
import json
from typing import Any, Dict

from .base import BaseTool
from .mcp_client import MCPClient, MCPClientError
from .security.capabilities import ToolCapability
from ..utils.logger import setup_logger

logger = setup_logger("MCPTools")


class MCPSearchTool(BaseTool):
    """MCP-based web search tool using searxng_web_search via MCP protocol."""

    def __init__(self, mcp_client: MCPClient, tool_name: str = "searxng_web_search"):
        self._client = mcp_client
        self._tool_name = tool_name
        # Schema will be populated from MCP server on first use
        self._schema: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def capabilities(self) -> set[ToolCapability]:
        return {ToolCapability.NETWORK_READ, ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE}

    @property
    def description(self) -> str:
        return (
            "Search the web for up-to-date information via MCP SearXNG. "
            "Returns titles, URLs, and snippets. "
            "Only the query parameter is required; all others have sensible defaults."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        """Return JSON Schema for parameters. Uses cached MCP schema if available."""
        if self._schema:
            return self._schema
        # Fallback schema matching mcp-searxng's searxng_web_search tool
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string.",
                },
                "pageno": {
                    "type": "integer",
                    "description": "Search page number (starts at 1).",
                    "default": 1,
                },
                "time_range": {
                    "type": "string",
                    "description": "Time range of search.",
                    "enum": ["day", "month", "year"],
                },
                "language": {
                    "type": "string",
                    "description": "Language code for results (e.g., 'zh-CN', 'en').",
                    "default": "all",
                },
                "safesearch": {
                    "type": "integer",
                    "description": "Safe search filter level (0: None, 1: Moderate, 2: Strict).",
                    "enum": [0, 1, 2],
                    "default": 0,
                },
            },
            "required": ["query"],
        }

    def update_schema(self, mcp_tool_def: Dict[str, Any]):
        """Update schema from MCP server's tool definition."""
        schema = mcp_tool_def.get("inputSchema", {})
        if schema:
            self._schema = schema
            logger.info(f"Updated schema for {self.name} from MCP server")

    async def execute(self, **kwargs) -> str:
        query = kwargs.get("query", "")
        if not query:
            return json.dumps({"error": "query is required"}, ensure_ascii=False)

        # Build arguments matching MCP tool's expected parameter names
        arguments: Dict[str, Any] = {"query": query}
        if "pageno" in kwargs:
            arguments["pageno"] = kwargs["pageno"]
        elif "page" in kwargs:
            # Map legacy 'page' param to MCP's 'pageno'
            arguments["pageno"] = kwargs["page"]
        if "num_results" in kwargs:
            # mcp-searxng doesn't have num_results; note for future use
            pass
        if "time_range" in kwargs and kwargs["time_range"]:
            arguments["time_range"] = kwargs["time_range"]
        if "language" in kwargs and kwargs["language"]:
            arguments["language"] = kwargs["language"]
        if "safesearch" in kwargs and kwargs["safesearch"] is not None:
            arguments["safesearch"] = kwargs["safesearch"]

        try:
            logger.info(f"MCP web_search: query='{query}' args={arguments}")
            result = await self._client.call_tool(self._tool_name, arguments)
            return result
        except MCPClientError as e:
            logger.error(f"MCP web_search failed: {e}")
            return json.dumps({"error": str(e), "query": query}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"MCP web_search unexpected error: {e}")
            return json.dumps({"error": str(e), "query": query}, ensure_ascii=False)


class MCPUrlReadTool(BaseTool):
    """MCP-based URL reader tool using web_url_read via MCP protocol."""

    def __init__(self, mcp_client: MCPClient, tool_name: str = "web_url_read"):
        self._client = mcp_client
        self._tool_name = tool_name
        self._schema: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "fetch_url"

    @property
    def capabilities(self) -> set[ToolCapability]:
        return {ToolCapability.NETWORK_READ, ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE}

    @property
    def description(self) -> str:
        return (
            "Read and extract the text content of a web page via MCP. "
            "Use this after web_search to read a relevant page in detail."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        """Return JSON Schema for parameters."""
        if self._schema:
            return self._schema
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL of the page to fetch.",
                },
            },
            "required": ["url"],
        }

    def update_schema(self, mcp_tool_def: Dict[str, Any]):
        """Update schema from MCP server's tool definition."""
        schema = mcp_tool_def.get("inputSchema", {})
        if schema:
            self._schema = schema
            logger.info(f"Updated schema for {self.name} from MCP server")

    async def execute(self, **kwargs) -> str:
        url = kwargs.get("url", "")
        if not url:
            return json.dumps({"error": "url is required"}, ensure_ascii=False)

        arguments: Dict[str, Any] = {"url": url}
        # Pass through optional MCP parameters if provided
        for opt in ("startChar", "maxLength", "section", "paragraphRange", "readHeadings"):
            if opt in kwargs and kwargs[opt] is not None:
                arguments[opt] = kwargs[opt]

        try:
            logger.info(f"MCP web_url_read: url='{url}'")
            result = await self._client.call_tool(self._tool_name, arguments)
            return result
        except MCPClientError as e:
            logger.error(f"MCP web_url_read failed: {e}")
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"MCP web_url_read unexpected error: {e}")
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)
