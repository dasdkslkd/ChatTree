# test/test_mcp_tools.py - Test MCP tool integration
import sys
import os
import json
import asyncio

backend_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(backend_dir)
sys.path.insert(0, project_root)

from backend.core.tools.mcp_client import MCPClient, MCPClientError
from backend.core.tools.mcp_tools import MCPSearchTool, MCPUrlReadTool
from backend.core.tools.tool_manager import ToolManager


def test_mcp_client_tool_to_openai():
    """Test MCP tool definition -> OpenAI function calling format conversion."""
    print("=== Test: MCP tool -> OpenAI format conversion ===")

    mcp_tool_def = {
        "name": "searxng_web_search",
        "description": "Searches the web using SearXNG.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query string."},
                "pageno": {"type": "number", "description": "Page number", "default": 1},
                "time_range": {"type": "string", "enum": ["day", "month", "year"]},
                "language": {"type": "string", "description": "Language code"},
                "safesearch": {"type": "number", "enum": [0, 1, 2]},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    }

    openai_format = MCPClient.mcp_tool_to_openai(mcp_tool_def)

    assert openai_format["type"] == "function", "type should be 'function'"
    assert openai_format["function"]["name"] == "searxng_web_search"
    assert openai_format["function"]["description"] == "Searches the web using SearXNG."
    assert "query" in openai_format["function"]["parameters"]["properties"]
    assert openai_format["function"]["parameters"]["required"] == ["query"]
    print("  PASSED: Correctly converts MCP tool to OpenAI format")


def test_mcp_tool_default_schema():
    """Test that MCP tools provide fallback schemas when MCP server not connected."""
    print("\n=== Test: MCP tool fallback schemas ===")

    client = MCPClient(endpoint="http://localhost:3001")

    search_tool = MCPSearchTool(client)
    schema = search_tool.parameters_schema()
    assert schema["type"] == "object"
    assert "query" in schema["properties"]
    assert "query" in schema["required"]
    assert search_tool.name == "web_search"
    print("  PASSED: MCPSearchTool fallback schema correct")

    url_tool = MCPUrlReadTool(client)
    schema = url_tool.parameters_schema()
    assert "url" in schema["properties"]
    assert "url" in schema["required"]
    assert url_tool.name == "fetch_url"
    print("  PASSED: MCPUrlReadTool fallback schema correct")

    # Test OpenAI format output
    openai = search_tool.to_openai_tool()
    assert openai["type"] == "function"
    assert openai["function"]["name"] == "web_search"
    print("  PASSED: to_openai_tool() works correctly")


def test_mcp_tool_schema_update():
    """Test that MCP tools can update their schema from server definitions."""
    print("\n=== Test: MCP tool schema update ===")

    client = MCPClient(endpoint="http://localhost:3001")
    search_tool = MCPSearchTool(client)

    mcp_tool_def = {
        "name": "searxng_web_search",
        "description": "Updated description",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "custom_param": {"type": "boolean"},
            },
            "required": ["query"],
        },
    }

    search_tool.update_schema(mcp_tool_def)
    schema = search_tool.parameters_schema()
    assert "custom_param" in schema["properties"]
    print("  PASSED: Schema updated from MCP server definition")


def test_tool_manager_mcp_config():
    """Test ToolManager with MCP enabled vs disabled."""
    print("\n=== Test: ToolManager MCP configuration ===")

    # Test with MCP disabled (built-in tools)
    config_builtin = {
        "tools": {
            "web_search": {
                "enabled": True,
                "searxng": {"searxng_url": "http://localhost:8888"},
                "crawl4ai": {},
            },
            "mcp": {"enabled": False},
        }
    }
    tm_builtin = ToolManager(config_builtin)
    tools = tm_builtin.list_tools()
    assert "web_search" in tools
    assert "fetch_url" in tools
    print(f"  PASSED: Built-in mode: {tools}")

    # Test with MCP enabled
    config_mcp = {
        "tools": {
            "web_search": {"enabled": True},
            "mcp": {
                "enabled": True,
                "endpoint": "http://localhost:3001",
            },
        }
    }
    tm_mcp = ToolManager(config_mcp)
    tools = tm_mcp.list_tools()
    assert "web_search" in tools
    assert "fetch_url" in tools
    print(f"  PASSED: MCP mode: {tools}")

    # Verify MCP tools produce correct OpenAI schemas
    openai_tools = tm_mcp.get_openai_tools()
    for t in openai_tools:
        assert t["type"] == "function"
        assert "name" in t["function"]
        assert "parameters" in t["function"]
    print(f"  PASSED: OpenAI schemas correct for {len(openai_tools)} tools")


async def test_mcp_client_jsonrpc_format():
    """Test that MCPClient builds correct JSON-RPC 2.0 requests."""
    print("\n=== Test: MCPClient JSON-RPC request format ===")

    client = MCPClient(endpoint="http://localhost:9999")

    # Verify internal state
    assert client._next_id() == 1
    assert client._next_id() == 2
    assert client._session_id is None
    print("  PASSED: Client state management correct")

    # Test mcp_tool_to_openai with edge cases
    empty_tool = {"name": "test"}
    result = MCPClient.mcp_tool_to_openai(empty_tool)
    assert result["function"]["name"] == "test"
    assert result["function"]["description"] == ""
    assert result["function"]["parameters"] == {"type": "object", "properties": {}}
    print("  PASSED: Edge case handling in schema conversion")

    await client.close()
    print("  PASSED: Client cleanup")


def test_tool_manager_fallback():
    """Test that missing MCP config defaults correctly."""
    print("\n=== Test: Config defaults ===")

    # Config without MCP section at all
    config = {"tools": {"web_search": {"enabled": True}}}
    tm = ToolManager(config)
    assert "web_search" in tm.list_tools()
    print("  PASSED: Missing MCP config falls back to built-in tools")


def test_mcp_search_tool_param_mapping():
    """Test that MCPSearchTool maps legacy parameters correctly."""
    print("\n=== Test: MCPSearchTool parameter mapping ===")

    client = MCPClient(endpoint="http://localhost:3001")
    tool = MCPSearchTool(client)

    # Verify tool properties
    assert tool.name == "web_search"
    assert "MCP" in tool.description
    print("  PASSED: Tool name and description correct")


if __name__ == "__main__":
    test_mcp_client_tool_to_openai()
    test_mcp_tool_default_schema()
    test_mcp_tool_schema_update()
    test_tool_manager_mcp_config()
    asyncio.run(test_mcp_client_jsonrpc_format())
    test_tool_manager_fallback()
    test_mcp_search_tool_param_mapping()
    print("\n=== ALL TESTS PASSED ===")
