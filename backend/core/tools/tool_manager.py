# tools/tool_manager.py - Tool manager: register, expose, execute
import json
from typing import Any, Dict, List, Optional

from .base import BaseTool
from .code_tools import (
    ApplyPatchTool,
    CodeToolConfig,
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    RunCommandTool,
    SearchFilesTool,
    WriteFileTool,
)
from .connection_manager import ConnectionManager
from .mcp_client import MCPClient, MCPClientError
from .mcp_tools import MCPSearchTool, MCPUrlReadTool
from .command_tools import (
    ReadCommandTool,
    StartBackgroundCommandTool,
    StopCommandTool,
    WaitCommandTool,
)
from .tool_arguments import normalize_tool_arguments
from .tool_filter import ToolFilter
from .web_search import FetchUrlTool, WebSearchTool
from ..storage.tool_result_storage import ToolResultStorage
from ..utils.logger import setup_logger

logger = setup_logger("ToolManager")


BUILTIN_UTILITY_TOOLS = {"read_tool_result", "list_available_tools"}
BUILTIN_WEB_TOOLS = {"web_search", "fetch_url"}
BUILTIN_CODE_TOOL_GROUPS = {
    "read": {"list_files", "read_file"},
    "search": {"search_files"},
    "edit": {"edit_file", "apply_patch"},
    "shell": {"run_command", "start_background_command", "wait_command", "read_command", "stop_command"},
    "write": {"write_file"},
}
BUILTIN_LOCAL_TOOL_NAMES = (
    BUILTIN_UTILITY_TOOLS
    | BUILTIN_WEB_TOOLS
    | set().union(*BUILTIN_CODE_TOOL_GROUPS.values())
)
BUILTIN_CODE_TOOL_CLASSES = {
    "list_files": ListFilesTool,
    "read_file": ReadFileTool,
    "search_files": SearchFilesTool,
    "edit_file": EditFileTool,
    "run_command": RunCommandTool,
    "start_background_command": StartBackgroundCommandTool,
    "write_file": WriteFileTool,
    "apply_patch": ApplyPatchTool,
}
BUILTIN_EXPOSURE_PROFILES = {
    "minimal": BUILTIN_UTILITY_TOOLS | BUILTIN_WEB_TOOLS,
    "coding": (
        BUILTIN_UTILITY_TOOLS
        | BUILTIN_WEB_TOOLS
        | BUILTIN_CODE_TOOL_GROUPS["read"]
        | BUILTIN_CODE_TOOL_GROUPS["search"]
        | BUILTIN_CODE_TOOL_GROUPS["edit"]
        | BUILTIN_CODE_TOOL_GROUPS["shell"]
    ),
    "full": BUILTIN_LOCAL_TOOL_NAMES,
}


def _tool_exception_error(tool_name: str, exc: Exception) -> Dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "tool_name": tool_name,
    }


class ToolManager:
    """Tool manager supporting built-in tools and MCP servers."""

    def __init__(self, config: Dict[str, Any], tool_result_store: Optional[ToolResultStorage] = None):
        self._tools: Dict[str, BaseTool] = {}
        self._config = config
        self.tool_result_store = tool_result_store or ToolResultStorage()
        self.command_executor: Any = None
        self._mcp_client: Optional[MCPClient] = None
        self._mcp_tools: Dict[str, BaseTool] = {}
        self._connection_manager = ConnectionManager()
        self._mcp_servers_config: Dict[str, Dict[str, Any]] = {}
        self._mcp_init_errors: Dict[str, str] = {}
        tools_config = config.get("tools", {})
        self._enabled = tools_config.get("enabled", True)
        self._filter = ToolFilter(
            enabled=tools_config.get("enabled_tools"),
            disabled=tools_config.get("disabled_tools"),
        )
        self._model_visible_builtin_tools = self._resolve_model_visible_builtin_tools(tools_config)
        self._code_tools_config: Dict[str, Any] = {}
        self._command_tools_config: Dict[str, Any] = {}
        if self._enabled:
            self._register_tools(config)
            self.register(ReadToolResultTool(self.tool_result_store, tools_config))
            self.register(ToolInventoryTool(self))

    def _register_tools(self, config: Dict[str, Any]):
        """Register tools based on legacy or redesigned configuration."""
        tools_config = config.get("tools", {})
        mcp_config = tools_config.get("mcp", {})
        servers = mcp_config.get("servers") or {}
        builtin_config = self._builtin_runtime_config(tools_config)

        if mcp_config.get("enabled", False) and servers:
            if builtin_config.get("enabled", True) is not False:
                self._register_builtin_tools(builtin_config)
            self._mcp_servers_config = servers
            return

        if mcp_config.get("enabled", False):
            self._register_legacy_mcp_tools(mcp_config)
            return

        if builtin_config.get("enabled", True) is not False:
            self._register_builtin_tools(builtin_config)

    def _builtin_runtime_config(self, tools_config: Dict[str, Any]) -> Dict[str, Any]:
        """Build the config shape consumed by built-in tool registration."""
        builtin_config = dict(tools_config.get("builtin") or tools_config)
        if "web_search" not in builtin_config and "web_search" in tools_config:
            builtin_config["web_search"] = tools_config["web_search"]
        if "fetch_url" not in builtin_config and "fetch_url" in tools_config:
            builtin_config["fetch_url"] = tools_config["fetch_url"]
        return builtin_config

    def _register_legacy_mcp_tools(self, mcp_config: Dict[str, Any]):
        """Register compatibility adapters for the old single-server MCP config."""
        endpoint = mcp_config.get("endpoint", "http://localhost:3001")
        timeout = mcp_config.get("timeout", 30.0)
        http_retries = mcp_config.get("http_retries", mcp_config.get("retry_attempts", 2))
        http_retry_backoff = mcp_config.get("http_retry_backoff", mcp_config.get("retry_backoff", 0.5))
        search_tool_name = mcp_config.get("search_tool", "searxng_web_search")
        url_read_tool_name = mcp_config.get("url_read_tool", "web_url_read")

        self._mcp_client = MCPClient(
            endpoint=endpoint,
            timeout=timeout,
            http_retries=http_retries,
            http_retry_backoff=http_retry_backoff,
            bearer_token=mcp_config.get("bearer_token", mcp_config.get("token", "")),
            headers=mcp_config.get("headers", mcp_config.get("http_headers", {})),
        )

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

        code_config = tools_config.get("code", {})
        command_config = tools_config.get("command", {})
        code_enabled = code_config.get("enabled", True)
        command_enabled = command_config.get("enabled", code_enabled)
        if code_enabled or command_enabled:
            self._register_code_tools(
                code_config,
                command_config,
                include_code=code_enabled,
                include_command=command_enabled,
            )

    def _register_code_tools(
        self,
        code_config: Dict[str, Any],
        command_config: Optional[Dict[str, Any]] = None,
        *,
        include_code: bool = True,
        include_command: bool = True,
    ):
        """Register built-in code browsing and modification tools."""
        self._code_tools_config = dict(code_config)
        self._command_tools_config = {**dict(code_config), **dict(command_config or {})}
        code_tool_config = CodeToolConfig.from_dict(code_config)
        command_tool_config = CodeToolConfig.from_dict(self._command_tools_config)
        tools: list[BaseTool] = []
        if include_code:
            tools.extend([
                ListFilesTool(code_tool_config),
                ReadFileTool(code_tool_config),
                SearchFilesTool(code_tool_config),
                EditFileTool(code_tool_config),
                WriteFileTool(code_tool_config),
                ApplyPatchTool(code_tool_config),
            ])
        if include_command:
            tools.extend([
                RunCommandTool(command_tool_config),
                StartBackgroundCommandTool(command_tool_config),
                WaitCommandTool(),
                ReadCommandTool(),
                StopCommandTool(),
            ])
        for tool in tools:
            self.register(tool)
        logger.info("Registered built-in code tools")

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
                self._mcp_init_errors.pop(name, None)
            except Exception as e:
                error = str(e) or type(e).__name__
                self._mcp_init_errors[name] = error
                logger.error(f"MCP server '{name}' initialization failed: {error}")

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
            if self._is_model_visible_local_tool(name)
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
            if self._is_model_visible_local_tool(name):
                tools.append(tool.to_openai_tool())
        for info in self._connection_manager.list_all_tools():
            original_name = info["tool"].get("name", "")
            if self._filter.is_allowed(
                info["callable_name"],
                aliases=(original_name, f"{info['server']}.{original_name}"),
            ):
                tools.append(info["openai_schema"])
        return tools

    async def execute_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        workspace: Optional[Dict[str, Any]] = None,
        runtime_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Execute a tool by exposed name."""
        if not self._filter.is_allowed(name):
            return json.dumps({"error": f"Tool '{name}' is disabled"}, ensure_ascii=False)
        arguments = normalize_tool_arguments(name, arguments)

        try:
            if self._connection_manager.has_tool(name):
                logger.info(f"Executing MCP tool route: {name}")
                result = await self._connection_manager.call_tool(name, arguments)
                return result

            tool = self._tool_for_execution(name, workspace)
            if not tool:
                logger.error(f"Tool not found: {name}")
                return json.dumps({"error": f"Tool '{name}' not found"}, ensure_ascii=False)

            logger.info(f"Executing tool: {name} with args: {json.dumps(arguments, ensure_ascii=False)[:200]}")
            execute_arguments = dict(arguments)
            if runtime_context is not None:
                enriched_context = dict(runtime_context)
                if self.command_executor is not None:
                    enriched_context.setdefault("command_executor", self.command_executor)
                execute_arguments["_runtime_context"] = enriched_context
            result = await tool.execute(**execute_arguments)
            logger.info(f"Tool {name} returned {len(result)} chars")
            return result
        except Exception as e:
            error = _tool_exception_error(name, e)
            logger.error(f"Tool {name} execution failed: {error['type']}: {error['message']}")
            return json.dumps({"error": error}, ensure_ascii=False)

    def _tool_for_execution(self, name: str, workspace: Optional[Dict[str, Any]]) -> Optional[BaseTool]:
        if workspace and name in BUILTIN_CODE_TOOL_CLASSES:
            source_config = self._command_tools_config if name in BUILTIN_CODE_TOOL_GROUPS["shell"] else self._code_tools_config
            config = CodeToolConfig.for_workspace(source_config, workspace)
            return BUILTIN_CODE_TOOL_CLASSES[name](config)
        return self._tools.get(name)

    def describe_inventory(self) -> Dict[str, Any]:
        mcp_tools = self._connection_manager.list_all_tools()
        configured_servers = sorted(self._mcp_servers_config.keys())
        connected_servers = set(self._connection_manager.list_server_names())
        return {
            "tools_enabled": self._enabled,
            "model_visible_tools": [
                tool.get("function", {}).get("name")
                for tool in self.get_openai_tools()
            ],
            "local_tools": [
                name for name in self._tools
                if self._filter.is_allowed(name)
            ],
            "hidden_local_tools": [
                name for name in self._tools
                if self._filter.is_allowed(name) and not self._is_model_visible_local_tool(name)
            ],
            "mcp_servers": [
                self._describe_mcp_server(name, connected_servers)
                for name in configured_servers
            ],
            "mcp_tools": [
                {
                    "server": info["server"],
                    "name": info["tool"].get("name", ""),
                    "callable_name": info["callable_name"],
                }
                for info in mcp_tools
            ],
        }

    def _describe_mcp_server(self, name: str, connected_servers: set[str]) -> Dict[str, Any]:
        server_config = self._mcp_servers_config.get(name, {})
        source = server_config.get("source") or "user"
        inventory = {
            "name": name,
            "enabled": server_config.get("enabled", True) is not False,
            "connected": name in connected_servers,
            "error": self._mcp_init_errors.get(name),
            "source": source,
        }
        if source == "plugin":
            if server_config.get("plugin_id") is not None:
                inventory["plugin_id"] = server_config.get("plugin_id")
            if server_config.get("plugin_name") is not None:
                inventory["plugin_name"] = server_config.get("plugin_name")
        return inventory

    async def describe_inventory_async(self) -> Dict[str, Any]:
        inventory = self.describe_inventory()
        runtime_statuses = await self._connection_manager.list_server_statuses()
        for server in inventory["mcp_servers"]:
            runtime = runtime_statuses.get(server["name"])
            if not runtime:
                continue
            server.update({
                "transport": runtime.get("transport"),
                "connected": runtime.get("connected", False),
                "tools_count": runtime.get("tools_count", 0),
                "error": runtime.get("error"),
            })
        return inventory

    async def connect_mcp_server(self, name: str) -> Dict[str, Any]:
        server_cfg = self._mcp_servers_config.get(name)
        if server_cfg is None:
            raise KeyError(name)
        if server_cfg.get("enabled", True) is False:
            self._mcp_init_errors[name] = "MCP server is disabled"
            await self._connection_manager.remove_server(name)
            return await self.describe_inventory_async()
        try:
            await self._connection_manager.add_server(name, server_cfg)
            self._mcp_init_errors.pop(name, None)
        except Exception as e:
            error = str(e) or type(e).__name__
            self._mcp_init_errors[name] = error
            logger.error(f"MCP server '{name}' connection failed: {error}")
        return await self.describe_inventory_async()

    async def close(self):
        """Clean up resources."""
        for tool in self._tools.values():
            if hasattr(tool, "close"):
                await tool.close()  # type: ignore[attr-defined]
        if self._mcp_client:
            await self._mcp_client.close()
            self._mcp_client = None
        await self._connection_manager.close()

    def _resolve_model_visible_builtin_tools(self, tools_config: Dict[str, Any]) -> Optional[set[str]]:
        builtin_config = tools_config.get("builtin", {})
        exposure = str(
            builtin_config.get("exposure")
            or tools_config.get("builtin_exposure")
            or tools_config.get("exposure")
            or "coding"
        ).lower()
        visible = set(BUILTIN_EXPOSURE_PROFILES.get(exposure, BUILTIN_EXPOSURE_PROFILES["coding"]))

        code_config = builtin_config.get("code", tools_config.get("code", {}))
        groups = code_config.get("groups")
        if groups is not None:
            visible -= set().union(*BUILTIN_CODE_TOOL_GROUPS.values())
            for group in groups:
                visible |= BUILTIN_CODE_TOOL_GROUPS.get(str(group), set())

        explicit_visible = (
            builtin_config["model_visible_tools"]
            if "model_visible_tools" in builtin_config
            else tools_config.get("model_visible_tools")
        )
        if explicit_visible is not None:
            visible = set(str(name) for name in explicit_visible)

        hidden = (
            builtin_config["hidden_tools"]
            if "hidden_tools" in builtin_config
            else tools_config.get("hidden_tools", [])
        )
        visible -= set(str(name) for name in hidden)

        return visible

    def _is_model_visible_local_tool(self, name: str) -> bool:
        if not self._filter.is_allowed(name):
            return False
        if name not in BUILTIN_LOCAL_TOOL_NAMES:
            return True
        return name in self._model_visible_builtin_tools


class ReadToolResultTool(BaseTool):
    """Read a slice from a persisted full tool result."""

    def __init__(self, store: ToolResultStorage, tools_config: Dict[str, Any]):
        self._store = store
        self._max_limit = int(tools_config.get("read_tool_result_max_chars", 16000))

    @property
    def name(self) -> str:
        return "read_tool_result"

    @property
    def description(self) -> str:
        return (
            "Read a slice of a full persisted tool result by tool_result_id. "
            "Use this when a previous tool result preview says more content is available."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tool_result_id": {
                    "type": "string",
                    "description": "ID of the persisted tool result to read.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Zero-based character offset. Defaults to 0.",
                    "minimum": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum characters to read. Capped at {self._max_limit}.",
                    "minimum": 1,
                },
            },
            "required": ["tool_result_id"],
        }

    async def execute(self, **kwargs) -> str:
        tool_result_id = kwargs.get("tool_result_id") or ""
        if not tool_result_id:
            return json.dumps({"error": "tool_result_id is required"}, ensure_ascii=False)
        offset = int(kwargs.get("offset") or 0)
        requested_limit = int(kwargs.get("limit") or self._max_limit)
        limit = min(max(1, requested_limit), self._max_limit)
        result = self._store.read_slice(tool_result_id, offset=offset, limit=limit)
        if result is None:
            return json.dumps({
                "error": "tool result not found",
                "tool_result_id": tool_result_id,
            }, ensure_ascii=False)
        payload = {"content": result.get("content", "")}
        next_offset = result.get("next_offset")
        if next_offset is not None:
            payload["read_more"] = (
                f'read_tool_result({{"tool_result_id":"{tool_result_id}",'
                f'"offset":{next_offset}}})'
            )
        return json.dumps(payload, ensure_ascii=False)


class ToolInventoryTool(BaseTool):
    """Expose the current tool inventory to the model."""

    def __init__(self, manager: ToolManager):
        self._manager = manager

    @property
    def name(self) -> str:
        return "list_available_tools"

    @property
    def description(self) -> str:
        return (
            "List the tools and MCP servers currently available in ChatTree, "
            "including MCP connection status and callable tool names."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, **kwargs) -> str:
        return json.dumps(self._manager.describe_inventory(), ensure_ascii=False, indent=2)
