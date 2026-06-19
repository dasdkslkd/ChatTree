# tools/mcp_client.py - MCP client (JSON-RPC 2.0 over Streamable HTTP or stdio)
import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx

from ..utils.logger import setup_logger

logger = setup_logger("MCPClient")


class MCPClientError(Exception):
    """MCP client error."""


class MCPClient:
    """Lightweight MCP client communicating via JSON-RPC 2.0."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        timeout: float = 30.0,
        transport: str = "streamable_http",
        process: Optional[asyncio.subprocess.Process] = None,
        http_retries: int = 0,
        http_retry_backoff: float = 0.5,
        bearer_token: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        stdio_framing: str = "jsonl",
    ):
        self.endpoint = (endpoint or "").rstrip("/")
        self.timeout = timeout
        self.transport = transport
        self.http_retries = max(0, int(http_retries))
        self.http_retry_backoff = max(0.0, float(http_retry_backoff))
        self.bearer_token = bearer_token or ""
        self.extra_headers = dict(headers or {})
        self.stdio_framing = stdio_framing
        self._process = process
        self._session_id: Optional[str] = None
        self._request_id: int = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._stdio_lock = asyncio.Lock()
        self._stdio_stdout_tail: List[str] = []

    def _headers(self) -> Dict[str, str]:
        headers = dict(self.extra_headers)
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    **self._headers(),
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
        request_id = self._next_id()
        body: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            body["params"] = params

        if self.transport == "stdio":
            return await self._send_stdio_message(body, request_id)
        return await self._send_http_message(body, request_id)

    async def _send_notification(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ):
        """Send a JSON-RPC notification."""
        body: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            body["params"] = params

        if self.transport == "stdio":
            async with self._stdio_lock:
                await self._write_stdio_json(body)
            return

        client = await self._get_client()
        headers: Dict[str, str] = self._headers()
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        await client.post(f"{self.endpoint}/mcp", json=body, headers=headers)

    async def _send_http_message(self, body: Dict[str, Any], request_id: int) -> Any:
        client = await self._get_client()
        headers: Dict[str, str] = self._headers()
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        mcp_url = f"{self.endpoint}/mcp"
        logger.info(f"MCP request #{request_id}: {body.get('method')} -> {mcp_url}")
        logger.debug(f"MCP request body: {json.dumps(body, ensure_ascii=False)[:500]}")

        resp: Optional[httpx.Response] = None
        last_error: Optional[Exception] = None
        for attempt in range(self.http_retries + 1):
            try:
                resp = await client.post(mcp_url, json=body, headers=headers)
                if resp.status_code < 500:
                    break
                last_error = MCPClientError(f"MCP HTTP error {resp.status_code}: {resp.text[:500]}")
            except httpx.RequestError as e:
                last_error = e

            if attempt < self.http_retries:
                await asyncio.sleep(self.http_retry_backoff * (2 ** attempt))

        if resp is None:
            raise MCPClientError(f"MCP HTTP request failed: {last_error}") from last_error

        if "mcp-session-id" in resp.headers:
            self._session_id = resp.headers["mcp-session-id"]

        if resp.status_code == 204:
            return None
        if resp.status_code >= 400:
            raise MCPClientError(f"MCP HTTP error {resp.status_code}: {resp.text[:500]}")

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse_response(resp.text)

        try:
            data = resp.json()
        except Exception as e:
            raise MCPClientError(f"MCP response is not valid JSON: {resp.text[:300]}") from e

        if "error" in data:
            err = data["error"]
            raise MCPClientError(
                f"MCP JSON-RPC error {err.get('code')}: {err.get('message', '')}"
            )
        return data.get("result")

    async def _send_stdio_message(self, body: Dict[str, Any], request_id: int) -> Any:
        async with self._stdio_lock:
            await self._write_stdio_json(body)
            while True:
                data = await self._read_stdio_json()
                if not isinstance(data, dict):
                    continue
                if data.get("id") != request_id:
                    continue
                if "error" in data:
                    err = data["error"]
                    raise MCPClientError(
                        f"MCP JSON-RPC error {err.get('code')}: {err.get('message', '')}"
                    )
                return data.get("result")

    async def _write_stdio_json(self, body: Dict[str, Any]):
        if not self._process or not self._process.stdin:
            raise MCPClientError("MCP stdio process is not available")
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self.stdio_framing == "jsonl":
            payload = payload + b"\n"
        else:
            payload = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
        self._process.stdin.write(payload)
        await self._process.stdin.drain()

    async def _read_stdio_json(self) -> Any:
        if not self._process or not self._process.stdout:
            raise MCPClientError("MCP stdio process is not available")

        first = await self._process.stdout.readline()
        if not first:
            raise MCPClientError("MCP stdio stream closed")

        stripped = first.strip()
        if stripped.startswith(b"{"):
            try:
                return json.loads(stripped.decode("utf-8"))
            except json.JSONDecodeError as e:
                raise MCPClientError(f"MCP stdio response is not valid JSON: {stripped[:200]!r}") from e

        if self.stdio_framing == "jsonl":
            text = stripped.decode("utf-8", errors="replace")
            if text:
                self._stdio_stdout_tail.append(text)
                self._stdio_stdout_tail = self._stdio_stdout_tail[-20:]
            return None

        header_lines = [first]
        while True:
            line = await self._process.stdout.readline()
            if not line:
                raise MCPClientError("MCP stdio stream closed while reading headers")
            if line in (b"\r\n", b"\n"):
                break
            header_lines.append(line)

        content_length: Optional[int] = None
        for line in header_lines:
            text = line.decode("ascii", errors="ignore").strip()
            if text.lower().startswith("content-length:"):
                content_length = int(text.split(":", 1)[1].strip())
                break
        if content_length is None:
            raise MCPClientError("MCP stdio response missing Content-Length")

        payload = await self._process.stdout.readexactly(content_length)
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise MCPClientError(f"MCP stdio response is not valid JSON: {payload[:200]!r}") from e

    def _parse_sse_response(self, text: str) -> Any:
        """Extract the last JSON-RPC result from an SSE stream."""
        result = None
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if "result" in msg:
                result = msg["result"]
            elif "error" in msg:
                err = msg["error"]
                raise MCPClientError(
                    f"MCP SSE error {err.get('code')}: {err.get('message', '')}"
                )
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
        try:
            await self._send_notification("notifications/initialized")
        except Exception as e:
            logger.debug(f"MCP initialized notification failed or unsupported: {e}")
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
        if self.transport == "stdio":
            return bool(self._process and self._process.returncode is None)
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.endpoint}/health", headers=self._headers())
            if resp.status_code == 200:
                return True
            if self._session_id:
                await self.list_tools()
                return True
            return False
        except Exception as e:
            logger.warning(f"MCP health check failed: {e}")
            return False

    async def close(self):
        """Close client resources."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        self._session_id = None

    @staticmethod
    def mcp_tool_to_openai(tool_def: Dict[str, Any]) -> Dict[str, Any]:
        """Convert MCP tool definition to OpenAI function calling format."""
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
