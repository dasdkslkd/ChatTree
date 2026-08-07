"""Managed lifecycle for an optional bundled SearXNG binary (tools/searxng).

The SearXNG binary is a separate AGPL-licensed program published by a fork of
searxng/searxng.  ChatTree never imports searx code; it only spawns the binary
as a child process and talks to it over HTTP.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from ..home import resolve_chattree_home
from ..subprocess_utils import subprocess_window_kwargs
from ..utils.logger import setup_logger

logger = setup_logger("SearxngRuntime")

DEFAULT_SEARXNG_PORT = 8888
HEALTH_PATH = "/healthz"
READY_TIMEOUT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 3.0
ENGINE_TIMEOUT_SECONDS = 15.0


class SearxngUnavailableError(RuntimeError):
    """Raised when no working SearXNG instance can be started."""


def _searxng_executable() -> Optional[Path]:
    """Locate the SearXNG binary: bundled tools/searxng first, then PATH."""
    name = "searxng-server.exe" if os.name == "nt" else "searxng-server"
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        bundled = Path(bundle_root) / "tools" / "searxng" / name
        if bundled.is_file() and (os.name == "nt" or os.access(bundled, os.X_OK)):
            return bundled
    found = shutil.which(name)
    if found:
        return Path(found)
    return None


class SearxngRuntime:
    """Spawn and own a local SearXNG process on demand.

    Prefers an already reachable configured instance; otherwise starts the
    bundled binary with a generated settings.yml and reuses a healthy instance
    already listening on the managed port.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        binary: Optional[Path] = None,
        home: Optional[Path] = None,
    ):
        config = config or {}
        self._searxng_url = str(config.get("searxng_url") or f"http://127.0.0.1:{DEFAULT_SEARXNG_PORT}").rstrip("/")
        self._use_proxies = bool(config.get("use_proxies", True))
        self._outgoing_proxies = self._parse_proxies(config.get("outgoing_proxies"))
        self._binary = Path(binary).expanduser().resolve() if binary else None
        self._home = (home or resolve_chattree_home()).resolve()
        self._port = self._managed_port()
        self._process: Optional[subprocess.Popen[Any]] = None
        self._managed_url: Optional[str] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._start_lock = asyncio.Lock()
        self._unavailable = False

    def _parse_proxies(self, value: Any) -> List[str]:
        """Normalize the proxy setting into a list of proxy URLs."""
        items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        return [self._normalize_proxy(str(item).strip()) for item in items if str(item).strip()]

    @staticmethod
    def _normalize_proxy(proxy: str) -> str:
        """Ensure the proxy URL carries a scheme; default to http."""
        if "://" in proxy:
            return proxy
        return f"http://{proxy}"

    async def restart(self) -> str:
        """Restart the managed SearXNG child with current settings. When the
        configured instance is already reachable, no managed child is spawned."""
        self._unavailable = False
        async with self._start_lock:
            self._terminate()
            self._managed_url = None
            if await self._probe(self._searxng_url):
                return self._searxng_url
            return await self._start_managed()

    def _managed_port(self) -> int:
        """Managed instance binds the configured URL's port when it is loopback."""
        parsed = urlparse(self._searxng_url)
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port:
            return parsed.port
        return DEFAULT_SEARXNG_PORT

    async def ensure_url(self) -> str:
        """Return the effective SearXNG base URL, starting a managed instance if needed."""
        if self._unavailable:
            raise SearxngUnavailableError(
                "SearXNG could not be started in a previous attempt; web_search is unavailable "
                "(restart the server to retry)"
            )
        if self._managed_url is not None and self._process is not None and self._process.poll() is None:
            return self._managed_url
        if await self._probe(self._searxng_url):
            return self._searxng_url
        target = f"http://127.0.0.1:{self._port}"
        if target != self._searxng_url and await self._probe(target):
            return target
        async with self._start_lock:
            if self._managed_url is not None and self._process is not None and self._process.poll() is None:
                return self._managed_url
            try:
                return await self._start_managed()
            except SearxngUnavailableError:
                self._unavailable = True
                raise

    async def close(self) -> None:
        """Terminate an owned SearXNG process and release resources."""
        self._terminate()
        self._managed_url = None
        self._unavailable = False
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _start_managed(self) -> str:
        binary = self._binary or _searxng_executable()
        if binary is None:
            raise SearxngUnavailableError(
                "SearXNG binary not found (expected bundled tools/searxng or on PATH); "
                "web_search is unavailable"
            )
        ports = [self._port, self._free_port()]
        if ports[1] == ports[0]:
            ports.pop()
        last_error: Optional[Exception] = None
        for port in ports:
            settings_path = self._write_settings(port)
            self._spawn(binary, settings_path)
            target = f"http://127.0.0.1:{port}"
            try:
                await self._wait_ready(target, self._process)
            except SearxngUnavailableError as exc:
                last_error = exc
                self._terminate()
                continue
            self._managed_url = target
            return target
        raise SearxngUnavailableError(f"SearXNG failed to start: {last_error}")

    def _write_settings(self, port: int) -> Path:
        settings_dir = self._home / "searxng"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = settings_dir / "settings.yml"
        outgoing = ["outgoing:", f"  request_timeout: {ENGINE_TIMEOUT_SECONDS}"]
        if self._use_proxies and self._outgoing_proxies:
            outgoing.append("  proxies:")
            outgoing.append("    all://:")
            for proxy in self._outgoing_proxies:
                outgoing.append(f"        - {proxy}")
        settings_path.write_text(
            "use_default_settings: true\n"
            "general:\n"
            "  debug: false\n"
            "  instance_name: ChatTree managed SearXNG\n"
            "server:\n"
            f"  secret_key: {secrets.token_hex(32)}\n"
            "  bind_address: 127.0.0.1\n"
            f"  port: {port}\n"
            + "\n".join(outgoing)
            + "\nsearch:\n"
            "  formats:\n"
            "    - html\n"
            "    - json\n",
            encoding="utf-8",
        )
        return settings_path

    def _spawn(self, binary: Path, settings_path: Path) -> None:
        log_path = self._home / "searxng" / "searxng.log"
        env = os.environ.copy()
        env.update({"SEARXNG_SETTINGS_PATH": str(settings_path), "PYTHONUNBUFFERED": "1"})
        kwargs: Dict[str, Any] = {
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "shell": False,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs.update(subprocess_window_kwargs(new_process_group=True))
        else:
            kwargs["start_new_session"] = True
        log_handle = log_path.open("ab")
        try:
            self._process = subprocess.Popen([str(binary)], stdout=log_handle, **kwargs)
        finally:
            log_handle.close()
        logger.info(f"Started SearXNG pid={self._process.pid} settings={settings_path} log={log_path}")

    async def _wait_ready(self, base_url: str, process: Optional[subprocess.Popen[Any]]) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                raise SearxngUnavailableError(
                    f"SearXNG exited with code {process.returncode} before becoming ready; "
                    f"see {self._home / 'searxng' / 'searxng.log'}"
                )
            if await self._probe(base_url):
                return
            await asyncio.sleep(0.25)
        raise SearxngUnavailableError(
            f"SearXNG did not become ready at {base_url} within {READY_TIMEOUT_SECONDS}s; "
            f"see {self._home / 'searxng' / 'searxng.log'}"
        )

    def _terminate(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(5)
        except (subprocess.TimeoutExpired, OSError):
            process.kill()
            process.wait()

    async def _probe(self, base_url: str) -> bool:
        try:
            response = await self._client().get(f"{base_url}{HEALTH_PATH}")
            return response.status_code < 400
        except httpx.HTTPError:
            return False

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS, trust_env=False)
        return self._http_client

    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
