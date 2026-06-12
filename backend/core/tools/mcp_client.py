# tools/mcp_client.py - MCP HTTP client (JSON-RPC 2.0 over Streamable HTTP)
import json
import asyncio
from typing import Any, Dict, List, Optional

import httpx

from ..utils.logger import setup_logger

logger = setup_logger("MCPClient")


class MCPClientError(Exception):
    """MCP client error"""


class MCPClient:
    """Lightweight MCP HTTP client communicating via JSON-RPC 2.0.

    Supports MCP servers using Streamable HTTP transport (e.g. mcp-searxng).
    """

    def __init__(self, endpoint: str, timeout: float = 30.0):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._session_id: Optional[str] = None
        self._request_id: int = 0
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
        return self._client

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _send_request(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        """Send a JSON-RPC 2.0 request and return the result."""
        client = await self._get_client()
        request_id = self._next_id()

        body: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            body["params"] = params

        headers: Dict[str, str] = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        mcp_url = f"{self.endpoint}/mcp"
        logger.info(f"MCP request #{request_id}: {method} -> {mcp_url}")
        logger.debug(f"MCP request body: {json.dumps(body, ensure_ascii=False)[:500]}")

        try:
            resp = await client.post(mcp_url, json=body, headers=headers)
        except httpx.RequestError as e:
            raise MCPClientError(f"MCP HTTP request failed: {e}") from e

        # Save session id if returned by server
        if "mcp-session-id" in resp.headers:
            self._session_id = resp.headers["mcp-session-id"]

        if resp.status_code == 204:
            return None

        if resp.status_code >= 400:
            error_text = resp.text[:500]
            raise MCPClientError(f"MCP HTTP error {resp.status_code}: {error_text}")

        # Parse response (JSON or SSE)
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse_response(resp.text)

        try:
            data = resp.json()
        except Exception:
            raise MCPClientError(f"MCP response is not valid JSON: {resp.text[:300]}")

        if "error" in data:
            err = data["error"]
            raise MCPClientError(
                f"MCP JSON-RPC error {err.get('code')}: {err.get('message', '')}"
            )

        return data.get("result")

    def _parse_sse_response(self, text: str) -> Any:
        """Extract the last JSON-RPC result from an SSE stream."""
        result = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    msg = json.loads(payload)
                    if "result" in msg:
                        result = msg["result"]
                    elif "error" in msg:
                        err = msg["error"]
                        raise MCPClientError(
                            f"MCP SSE error {err.get('code')}: {err.get('message', '')}"
                        )
                except json.JSONDecodeError:
                    continue
        return result

    async def initialize(self) -> Dict[str, Any]:
        """Send initialize request to establish MCP session."""
        result = await self._send_request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "chattree", "version": "0.1.0"},
            },
        )
        logger.info(f"MCP session initialized: {result}")
        return result

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Get all tool definitions from the MCP server."""
        result = await self._send_request("tools/list")
        tools = result.get("tools", []) if result else []
        logger.info(f"MCP server returned {len(tools)} tools")
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Call an MCP tool and return concatenated text result."""
        result = await self._send_request(
            "tools/call", {"name": name, "arguments": arguments}
        )

        if result is None:
            return json.dumps(
                {"error": "MCP tool returned empty result"}, ensure_ascii=False
            )

        contents = result.get("content", [])
        if not contents:
            return json.dumps(
                {"error": "MCP tool returned no content"}, ensure_ascii=False
            )

        parts = []
        for item in contents:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(f"[{item.get('type', 'unknown')} content]")

        return "\n".join(parts)

    async def health_check(self) -> bool:
        """Check if MCP server is reachable."""
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.endpoint}/health")
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"MCP health check failed: {e}")
            return False

    async def close(self):
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        self._session_id = None

    @staticmethod
    def mcp_tool_to_openai(tool_def: Dict[str, Any]) -> Dict[str, Any]:
        """Convert MCP tool definition to OpenAI function calling format.

        MCP format:  { name, description, inputSchema, annotations? }
        OpenAI format: { type: "function", function: { name, description, parameters } }
        """
        return {
            "type": "function",
            "function": {
                "name": tool_def.get("name", ""),
                "description": tool_def.get("description", ""),
                "parameters": tool_def.get(
                    "inputSchema", {"type": "object", "properties": {}}
                ),
            },
        }
