# tools/mcp_server.py - MCP server lifecycle management
import asyncio
import json
import os
import shlex
import shutil
import subprocess
from typing import Any, BinaryIO, Dict, List, Optional

from .mcp_client import MCPClient, MCPClientError
from .tool_filter import ToolFilter
from ..subprocess_utils import subprocess_window_kwargs
from ..utils.logger import setup_logger

logger = setup_logger("MCPServer")


class _AsyncPipeWriter:
    def __init__(self, pipe: BinaryIO):
        self._pipe = pipe

    def write(self, data: bytes):
        self._pipe.write(data)

    async def drain(self):
        await asyncio.to_thread(self._pipe.flush)

    def close(self):
        self._pipe.close()

    async def wait_closed(self):
        return None


class _AsyncPipeReader:
    def __init__(self, pipe: BinaryIO):
        self._pipe = pipe

    async def readline(self) -> bytes:
        return await asyncio.to_thread(self._pipe.readline)

    async def readexactly(self, size: int) -> bytes:
        data = await asyncio.to_thread(self._pipe.read, size)
        if len(data) < size:
            raise asyncio.IncompleteReadError(data, size)
        return data


class _PopenProcess:
    def __init__(self, process: subprocess.Popen[bytes]):
        self._process = process
        self.stdin = _AsyncPipeWriter(process.stdin) if process.stdin else None
        self.stdout = _AsyncPipeReader(process.stdout) if process.stdout else None
        self.stderr = _AsyncPipeReader(process.stderr) if process.stderr else None

    @property
    def returncode(self) -> Optional[int]:
        return self._process.poll()

    def terminate(self):
        self._process.terminate()

    def kill(self):
        self._process.kill()

    async def wait(self) -> int:
        return await asyncio.to_thread(self._process.wait)


class McpServerManager:
    """Manage one MCP server connection and its discovered tool cache."""

    def __init__(self, name: str, server_config: Dict[str, Any]):
        self.name = name
        self.config = server_config
        self.transport = server_config.get("transport", "streamable_http")
        self.endpoint = server_config.get("url", server_config.get("endpoint", "http://localhost:3001"))
        self.command = server_config.get("command")
        self.args = server_config.get("args", server_config.get("arguments", []))
        self.env = self._dict_from_pairs(server_config.get("env", server_config.get("environment", {})))
        self.bearer_token = server_config.get("bearer_token", server_config.get("token", ""))
        self.headers = self._dict_from_pairs(server_config.get("headers", server_config.get("http_headers", {})))
        self.stdio_framing = server_config.get("stdio_framing", "jsonl")
        self.timeout = float(server_config.get("timeout", 30.0))
        self.startup_timeout = float(server_config.get("startup_timeout", self.timeout))
        self.tool_call_timeout = float(server_config.get("tool_call_timeout", server_config.get("call_timeout", 120.0)))
        self.heartbeat_enabled = bool(server_config.get("heartbeat_enabled", True))
        self.heartbeat_interval = float(server_config.get("heartbeat_interval", 30.0))
        self.auto_reconnect = bool(server_config.get("auto_reconnect", True))
        self.max_reconnect_attempts = int(server_config.get("max_reconnect_attempts", 3))
        self.http_retries = int(server_config.get("http_retries", server_config.get("retry_attempts", 2)))
        self.http_retry_backoff = float(server_config.get("http_retry_backoff", server_config.get("retry_backoff", 0.5)))
        self.filter = ToolFilter(
            enabled=server_config.get("enabled_tools"),
            disabled=server_config.get("disabled_tools"),
        )
        self._process: Optional[Any] = None
        self._client: Optional[MCPClient] = None
        self._tools_cache: List[Dict[str, Any]] = []
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._reconnect_lock = asyncio.Lock()
        self._last_error: Optional[str] = None
        self._stderr_tail: List[str] = []
        self._stderr_task: Optional[asyncio.Task] = None
        self._resolved_command: List[str] = []

    @property
    def tools(self) -> List[Dict[str, Any]]:
        return list(self._tools_cache)

    @staticmethod
    def _dict_from_pairs(value: Any) -> Dict[str, str]:
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items() if str(k)}
        if isinstance(value, list):
            result: Dict[str, str] = {}
            for item in value:
                if not isinstance(item, dict):
                    continue
                key = item.get("key") or item.get("name")
                if key:
                    result[str(key)] = str(item.get("value", ""))
            return result
        return {}

    @staticmethod
    def _split_args(value: Any) -> List[str]:
        if isinstance(value, list):
            parts: List[str] = []
            for item in value:
                if item is None:
                    continue
                text = str(item).strip()
                if not text:
                    continue
                try:
                    parts.extend(shlex.split(text, posix=False))
                except ValueError:
                    parts.append(text)
            return parts
        if isinstance(value, str):
            try:
                return shlex.split(value, posix=False)
            except ValueError:
                return [value]
        return []

    def _build_stdio_command(self) -> List[str]:
        if isinstance(self.command, list):
            command = self._split_args(self.command)
        else:
            executable = str(self.command).strip()
            if not executable:
                return []
            command = [executable, *self._split_args(self.args)]
        if command:
            command[0] = shutil.which(command[0]) or command[0]
        return command

    def _should_use_popen_stdio(self) -> bool:
        """Windows selector loops cannot spawn asyncio subprocesses."""
        return os.name == "nt"

    async def _start_stdio_process(
        self,
        command: List[str],
        cwd: Optional[str],
        env: Dict[str, str],
    ):
        if self._should_use_popen_stdio():
            return self._start_popen_stdio_process(command, cwd, env)
        try:
            return await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                **subprocess_window_kwargs(),
            )
        except NotImplementedError:
            logger.warning(
                "asyncio subprocess is unavailable on this event loop; "
                "falling back to subprocess.Popen for MCP stdio"
            )
            return self._start_popen_stdio_process(command, cwd, env)

    def _start_popen_stdio_process(
        self,
        command: List[str],
        cwd: Optional[str],
        env: Dict[str, str],
    ) -> _PopenProcess:
        return _PopenProcess(subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            **subprocess_window_kwargs(),
        ))

    async def start(self):
        try:
            await self._connect()
            self._last_error = None
        except Exception as e:
            self._last_error = str(e) or type(e).__name__
            raise
        if self.heartbeat_enabled:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _connect(self):
        if self.transport == "stdio":
            if not self.command:
                raise MCPClientError(f"MCP server '{self.name}' missing stdio command")
            command = self._build_stdio_command()
            if not command:
                raise MCPClientError(f"MCP server '{self.name}' missing stdio command")
            self._resolved_command = command
            env = os.environ.copy()
            env.update(self.env)
            cwd = self.config.get("cwd") or None
            self._process = await self._start_stdio_process(command, cwd, env)
            if self._process.stderr:
                self._stderr_task = asyncio.create_task(self._capture_stderr())
            self._client = MCPClient(
                transport="stdio",
                process=self._process,
                timeout=self.timeout,
                http_retries=self.http_retries,
                http_retry_backoff=self.http_retry_backoff,
                bearer_token=self.bearer_token,
                headers=self.headers,
                stdio_framing=self.stdio_framing,
            )
        else:
            self._client = MCPClient(
                endpoint=self.endpoint,
                transport="streamable_http",
                timeout=self.timeout,
                http_retries=self.http_retries,
                http_retry_backoff=self.http_retry_backoff,
                bearer_token=self.bearer_token,
                headers=self.headers,
            )

        try:
            await asyncio.wait_for(self._client.initialize(), timeout=self.startup_timeout)
            discovered = await asyncio.wait_for(self._client.list_tools(), timeout=self.startup_timeout)
        except asyncio.TimeoutError as e:
            raise MCPClientError(
                self._startup_timeout_message() + self._stderr_suffix()
            ) from e
        except MCPClientError as e:
            suffix = self._stderr_suffix()
            if suffix and suffix not in str(e):
                raise MCPClientError(f"{e}{suffix}") from e
            raise
        self._tools_cache = [
            tool for tool in discovered
            if self.filter.is_allowed(tool.get("name", ""))
        ]
        self._last_error = None
        logger.info(f"MCP server '{self.name}' started with {len(self._tools_cache)} visible tools")

    async def _capture_stderr(self):
        if not self._process or not self._process.stderr:
            return
        while True:
            try:
                line = await self._process.stderr.readline()
            except asyncio.CancelledError:
                break
            except Exception:
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            self._stderr_tail.append(text)
            self._stderr_tail = self._stderr_tail[-20:]

    def _stderr_suffix(self) -> str:
        if not self._stderr_tail:
            return ""
        excerpt = "\n".join(self._stderr_tail[-8:])
        return f"; stderr: {excerpt[:1200]}"

    def _startup_timeout_message(self) -> str:
        base = f"MCP server '{self.name}' initialization timed out after {self.startup_timeout:g}s"
        executable = os.path.basename(self._resolved_command[0]).lower() if self._resolved_command else ""
        if executable in {"npx", "npx.cmd"}:
            return (
                f"{base}; npx did not produce an MCP response. "
                "If the package is already installed, try using command 'mcp-searxng' or "
                "command 'node' with args pointing to the package's dist/cli.js to avoid npm registry startup delays."
            )
        return base

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        if self.auto_reconnect and not await self._is_healthy():
            await self._reconnect()
        if not self._client:
            return json.dumps({"error": f"MCP server '{self.name}' is not initialized"}, ensure_ascii=False)
        if not self.filter.is_allowed(tool_name):
            return json.dumps({"error": f"MCP tool '{tool_name}' is disabled"}, ensure_ascii=False)
        return await asyncio.wait_for(
            self._client.call_tool(tool_name, arguments),
            timeout=self.tool_call_timeout,
        )

    async def _is_healthy(self) -> bool:
        if not self._client:
            return False
        try:
            return await asyncio.wait_for(self._client.health_check(), timeout=min(self.timeout, 10.0))
        except Exception as e:
            self._last_error = str(e) or type(e).__name__
            return False

    async def status(self) -> Dict[str, Any]:
        healthy = await self._is_healthy()
        return {
            "name": self.name,
            "enabled": self.config.get("enabled", True) is not False,
            "transport": self.transport,
            "connected": healthy,
            "tools_count": len(self._tools_cache) if healthy else 0,
            "error": None if healthy else self._last_error,
        }

    async def _heartbeat_loop(self):
        while True:
            try:
                await asyncio.sleep(max(1.0, self.heartbeat_interval))
                if await self._is_healthy():
                    continue
                logger.warning(f"MCP server '{self.name}' heartbeat failed")
                if self.auto_reconnect:
                    await self._reconnect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"MCP server '{self.name}' heartbeat error: {e}")

    async def _reconnect(self):
        async with self._reconnect_lock:
            if await self._is_healthy():
                return
            last_error: Optional[Exception] = None
            attempts = max(1, self.max_reconnect_attempts)
            for attempt in range(attempts):
                try:
                    await self._stop_connection()
                    await self._connect()
                    self._last_error = None
                    logger.info(f"MCP server '{self.name}' reconnected")
                    return
                except Exception as e:
                    last_error = e
                    self._last_error = str(e) or type(e).__name__
                    logger.warning(f"MCP server '{self.name}' reconnect attempt {attempt + 1}/{attempts} failed: {e}")
                    if attempt + 1 < attempts:
                        await asyncio.sleep(min(30.0, self.http_retry_backoff * (2 ** attempt)))
            if last_error:
                raise last_error

    async def _stop_connection(self):
        if self._client:
            await self._client.close()
            self._client = None
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None
        if self._process:
            if self._process.stdin:
                self._process.stdin.close()
                try:
                    await self._process.stdin.wait_closed()
                except Exception:
                    pass
            if self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
            self._process = None
            await asyncio.sleep(0.1)

    async def stop(self):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        await self._stop_connection()
