from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import httpx

from backend.core.subprocess_utils import subprocess_window_kwargs
from client_launcher.http_errors import canonical_request_id
from client_launcher.local_server import (
    ConnectedServer,
    PhaseCallback,
)
from client_launcher.models import LauncherError, ServerProfile
from client_launcher.server_compat import (
    MIN_SERVER_VERSION,
    check_server_version,
    handshake_protocol_error,
    handshake_protocol_details,
    handshake_version_details,
    parse_chattree_server_version,
)
from client_launcher.settings import LauncherSettings


_HANDSHAKE_PATH = "/api/v1/handshake"
_SHUTDOWN_PATH = "/api/v1/server/shutdown"
STARTUP_LOG_TAIL_BYTES = 8 * 1024
REMOTE_SERVER_HOST = "127.0.0.1"
REVERSE_PROXY_PROVIDER_ID = "local-proxy"

Sleep = Callable[[float], Awaitable[None]]


class SshConnectionError(LauncherError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str,
        retryable: bool,
        status_code: int = 502,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code,
            message,
            retryable,
            status_code,
            details=details,
        )
        self.phase = phase


@dataclass(frozen=True)
class _Tunnel:
    process: subprocess.Popen[Any]
    local_port: int
    endpoint: str
    log_path: Path
    reverse_port: int = 0
    proxy_token: str = ""


@dataclass(frozen=True)
class _RemoteStart:
    host: str
    port: int
    server_instance_id: str | None
    payload: Mapping[str, Any]


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _safe_profile_id(profile_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", profile_id)
    return safe.strip("._-")[:80] or "ssh"


def _read_log_tail(log_path: Path) -> str:
    try:
        with log_path.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            log_file.seek(max(0, size - STARTUP_LOG_TAIL_BYTES))
            tail = log_file.read(STARTUP_LOG_TAIL_BYTES)
    except OSError:
        return ""
    return tail.decode("utf-8", errors="replace").strip()


def _classify_ssh_failure(text: str, *, phase: str) -> tuple[str, str, bool]:
    lower = text.lower()
    if (
        "no such file" in lower
        or "not recognized" in lower
        or "cannot find the file" in lower
    ):
        return "ssh_not_found", "OpenSSH command was not found", False
    if "could not resolve hostname" in lower or "name or service not known" in lower:
        return "ssh_host_not_found", "SSH host alias could not be resolved", False
    if "host key verification failed" in lower:
        return "ssh_known_hosts_rejected", "SSH host key verification failed", False
    if "permission denied" in lower or "authentication failed" in lower:
        return "ssh_authentication_failed", "SSH authentication failed", False
    if "open failed" in lower or "forwarding failed" in lower:
        return "ssh_port_forward_failed", "SSH port forwarding failed", True
    if "chattree-server" in lower and (
        "not found" in lower or "not recognized" in lower
    ):
        return (
            "remote_chattree_server_not_found",
            "Remote chattree-server command was not found",
            False,
        )
    return (
        "ssh_command_failed",
        f"SSH {phase} command failed",
        phase != "remote_start",
    )


class SshServerConnector:
    def __init__(
        self,
        settings: LauncherSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        platform_name: str = os.name,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        allocate_port: Callable[[], int] = _allocate_loopback_port,
        local_port_resolver: Callable[[], int | None] | None = None,
    ) -> None:
        self._settings = settings
        self._popen_factory = popen_factory
        self._platform_name = platform_name
        self._monotonic = monotonic
        self._sleep = sleep
        self._allocate_port = allocate_port
        self._local_port_resolver = local_port_resolver
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(float(settings.connect_timeout_seconds)),
            trust_env=False,
            follow_redirects=False,
        )
        self._tunnels: dict[str, _Tunnel] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def connect(
        self,
        profile: ServerProfile,
        phase_callback: PhaseCallback | None,
        *,
        request_id: str | None = None,
    ) -> ConnectedServer:
        request_id = canonical_request_id(request_id)
        host_alias, remote_port = self._target(profile)
        lock = self._locks.setdefault(profile.id, asyncio.Lock())
        async with lock:
            await self.disconnect(profile)
            await self._remote_version(profile, host_alias, phase_callback)
            remote = await self._remote_start(
                profile,
                host_alias,
                remote_port,
                phase_callback,
            )
            tunnel = self._spawn_tunnel(profile, host_alias, remote.port)
            try:
                self._tunnels[profile.id] = tunnel
                connected = await self._wait_for_ready(
                    tunnel,
                    profile,
                    phase_callback,
                    request_id=request_id,
                    allow_timeout=True,
                )
                await self._setup_reverse_proxy(profile, tunnel)
                return connected
            except BaseException:
                await self.disconnect(profile)
                raise

    async def disconnect(self, profile: ServerProfile) -> None:
        tunnel = self._tunnels.pop(profile.id, None)
        if tunnel is None:
            return
        if tunnel.reverse_port:
            await self._cleanup_reverse_proxy(profile, tunnel)
        process = tunnel.process
        if process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 5)
        except (subprocess.TimeoutExpired, OSError):
            process.kill()
            await asyncio.to_thread(process.wait)

    def shutdown_target(self, profile: ServerProfile) -> tuple[str, Path]:
        tunnel = self._tunnels.get(profile.id)
        if tunnel is None:
            raise SshConnectionError(
                "ssh_tunnel_not_ready",
                "SSH tunnel is not established for this profile",
                phase="handshake",
                retryable=False,
                status_code=409,
            )
        return tunnel.endpoint, Path()

    async def request_shutdown(
        self,
        profile: ServerProfile,
        expected_server_instance_id: str,
        *,
        request_id: str | None = None,
    ) -> tuple[str, Path]:
        tunnel = self._tunnels.get(profile.id)
        if tunnel is None:
            raise SshConnectionError(
                "ssh_tunnel_not_ready",
                "SSH tunnel is not established for this profile",
                phase="handshake",
                retryable=False,
                status_code=409,
            )
        request_id = canonical_request_id(request_id)
        try:
            response = await self._client.post(
                f"{tunnel.endpoint}{_SHUTDOWN_PATH}",
                json={
                    "expected_server_instance_id": expected_server_instance_id,
                },
                headers={"X-Request-ID": request_id},
            )
        except httpx.HTTPError as exc:
            raise SshConnectionError(
                "ssh_shutdown_failed",
                f"SSH server shutdown request failed: {exc}",
                phase="handshake",
                retryable=True,
                details={"endpoint": tunnel.endpoint},
            ) from exc
        if response.status_code != 202:
            raise SshConnectionError(
                "ssh_shutdown_failed",
                f"SSH server shutdown returned HTTP {response.status_code}",
                phase="handshake",
                retryable=response.status_code >= 500,
                status_code=response.status_code,
                details={"endpoint": tunnel.endpoint},
            )
        return tunnel.endpoint, Path()

    async def wait_stopped(
        self,
        endpoint: str,
        server_home: Path,
        *,
        timeout: float,
    ) -> None:
        deadline = self._monotonic() + max(0.0, float(timeout))
        while True:
            if await self._endpoint_is_down(endpoint):
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise SshConnectionError(
                    "ssh_server_stop_timeout",
                    f"Remote Server did not stop within {timeout}s at {endpoint}",
                    phase="handshake",
                    retryable=True,
                    status_code=504,
                    details={"endpoint": endpoint},
                )
            await self._sleep(
                min(float(self._settings.poll_interval_seconds), remaining)
            )

    async def _endpoint_is_down(self, endpoint: str) -> bool:
        try:
            await self._client.get(f"{endpoint}{_HANDSHAKE_PATH}")
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return True
        except httpx.HTTPError:
            return False
        return False

    async def close(self) -> None:
        for tunnel in list(self._tunnels.values()):
            if tunnel.process.poll() is None:
                tunnel.process.terminate()
        for tunnel in list(self._tunnels.values()):
            try:
                await asyncio.to_thread(tunnel.process.wait, 5)
            except (subprocess.TimeoutExpired, OSError):
                tunnel.process.kill()
                await asyncio.to_thread(tunnel.process.wait)
        self._tunnels.clear()
        await self._client.aclose()

    def _target(self, profile: ServerProfile) -> tuple[str, int]:
        if profile.kind != "ssh" or profile.ssh is None:
            raise SshConnectionError(
                "invalid_ssh_profile",
                "SSH connector requires an ssh profile",
                phase="handshake",
                retryable=False,
                status_code=400,
            )
        return profile.ssh.config_host, profile.ssh.remote_server_port

    def _spawn_tunnel(
        self,
        profile: ServerProfile,
        host_alias: str,
        remote_port: int,
    ) -> _Tunnel:
        local_port = self._allocate_port()
        endpoint = f"http://127.0.0.1:{local_port}"
        log_path = self._log_path(profile.id)
        reverse_port = 0
        proxy_token = ""
        argv = [
            "ssh",
            "-o",
            "ExitOnForwardFailure=yes",
            "-N",
            "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        ]
        local_server_port = (
            self._local_port_resolver() if self._local_port_resolver else None
        )
        if local_server_port:
            reverse_port = self._allocate_port()
            proxy_token = os.environ.get("CHATTREE_PROXY_TOKEN", "")
            argv.extend([
                "-R",
                f"127.0.0.1:{reverse_port}:127.0.0.1:{local_server_port}",
            ])
        argv.append(host_alias)
        process = self._spawn(argv, log_path, phase="ssh_tunnel")
        return _Tunnel(
            process=process,
            local_port=local_port,
            endpoint=endpoint,
            log_path=log_path,
            reverse_port=reverse_port,
            proxy_token=proxy_token,
        )

    async def _setup_reverse_proxy(
        self,
        profile: ServerProfile,
        tunnel: _Tunnel,
    ) -> None:
        if not tunnel.reverse_port or not tunnel.proxy_token:
            return
        base_url = f"http://127.0.0.1:{tunnel.reverse_port}/api/v1/proxy"
        # 直接写入完整 provider 配置（PUT /config 为整体替换，必须包含全部字段），
        # 模型列表随后由 refresh 从本地 server 拉取
        update = {
            "provider_configs": {
                REVERSE_PROXY_PROVIDER_ID: {
                    "name": "本地代理",
                    "base_url": base_url,
                    # base_url 不符合 OpenAI 惯例路径，显式指定模型列表端点
                    "models_url_override": f"{base_url}/models",
                    "api_key": tunnel.proxy_token,
                    "models": [],
                    "hidden_models": [],
                    "enabled": True,
                    "source": "reverse_proxy",
                }
            }
        }
        try:
            response = await self._client.put(
                f"{tunnel.endpoint}/api/v1/config",
                json=update,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError:
            return
        if response.status_code >= 400:
            return
        # 触发远程 server 拉取本地代理的模型列表
        try:
            await self._client.post(
                f"{tunnel.endpoint}/api/v1/config/providers/{REVERSE_PROXY_PROVIDER_ID}/models/refresh",
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError:
            pass

    async def _cleanup_reverse_proxy(
        self,
        profile: ServerProfile,
        tunnel: _Tunnel,
    ) -> None:
        if not tunnel.reverse_port:
            return
        try:
            await self._client.delete(
                f"{tunnel.endpoint}/api/v1/config/providers/{REVERSE_PROXY_PROVIDER_ID}",
            )
        except httpx.HTTPError:
            pass

    async def _remote_start(
        self,
        profile: ServerProfile,
        host_alias: str,
        remote_port: int,
        phase_callback: PhaseCallback | None,
    ) -> _RemoteStart:
        await self._emit_phase(phase_callback, "remote_start")
        log_path = self._log_path(profile.id)
        argv = [
            "ssh",
            host_alias,
            "chattree-server",
            "start",
            "--host",
            REMOTE_SERVER_HOST,
            "--port",
            str(remote_port),
        ]
        process = self._spawn_capture(argv, log_path, phase="remote_start")

        def communicate() -> tuple[str, int | None]:
            output, _stderr = process.communicate(
                timeout=float(self._settings.start_timeout_seconds)
            )
            return str(output or ""), process.poll()

        try:
            output, exit_code = await asyncio.to_thread(communicate)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            try:
                output, _stderr = process.communicate(timeout=5)
                if output:
                    self._append_log(log_path, str(output))
            except (subprocess.TimeoutExpired, OSError):
                await asyncio.to_thread(process.wait)
            raise SshConnectionError(
                "remote_start_timeout",
                f"Remote chattree-server start timed out; see {log_path}",
                phase="remote_start",
                retryable=True,
                status_code=504,
                details={"log_path": str(log_path)},
            ) from exc
        self._append_log(log_path, output)
        if exit_code != 0:
            tail = _read_log_tail(log_path) or output.strip()
            code, message, retryable = _classify_ssh_failure(
                tail,
                phase="remote_start",
            )
            raise SshConnectionError(
                code,
                f"{message}; see {log_path}",
                phase="remote_start",
                retryable=retryable,
                details={"log_path": str(log_path), "log_tail": tail},
            )
        return self._parse_remote_start(output, log_path)

    async def _remote_version(
        self,
        profile: ServerProfile,
        host_alias: str,
        phase_callback: PhaseCallback | None,
    ) -> str:
        await self._emit_phase(phase_callback, "remote_version")
        log_path = self._log_path(profile.id)
        argv = ["ssh", host_alias, "chattree-server", "--version"]
        process = self._spawn_capture(argv, log_path, phase="remote_version")

        def communicate() -> tuple[str, int | None]:
            output, _stderr = process.communicate(
                timeout=float(self._settings.connect_timeout_seconds)
            )
            return str(output or ""), process.poll()

        try:
            output, exit_code = await asyncio.to_thread(communicate)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            try:
                output, _stderr = process.communicate(timeout=5)
                if output:
                    self._append_log(log_path, str(output))
            except (subprocess.TimeoutExpired, OSError):
                await asyncio.to_thread(process.wait)
            raise SshConnectionError(
                "remote_server_version_timeout",
                f"Remote chattree-server --version timed out; see {log_path}",
                phase="remote_version",
                retryable=True,
                status_code=504,
                details={"log_path": str(log_path)},
            ) from exc
        self._append_log(log_path, output)
        if exit_code != 0:
            tail = _read_log_tail(log_path) or output.strip()
            code, message, _retryable = _classify_ssh_failure(
                tail,
                phase="remote_version",
            )
            if code == "ssh_command_failed":
                code = "remote_server_version_failed"
                message = "Remote chattree-server --version failed"
            raise SshConnectionError(
                code,
                f"{message}; see {log_path}",
                phase="remote_version",
                retryable=False,
                details={"log_path": str(log_path), "log_tail": tail},
            )
        version = parse_chattree_server_version(output)
        if version is None:
            raise SshConnectionError(
                "remote_server_version_invalid",
                f"Remote chattree-server --version returned an invalid response; see {log_path}",
                phase="remote_version",
                retryable=False,
                details={"log_path": str(log_path), "log_tail": output.strip()},
            )
        check = check_server_version(version)
        if not check.compatible:
            raise SshConnectionError(
                "remote_server_version_incompatible",
                (
                    "Remote chattree-server binary is incompatible: "
                    f"required {MIN_SERVER_VERSION}, got {version}"
                ),
                phase="remote_version",
                retryable=False,
                status_code=409,
                details={
                    "required_server_version": check.minimum_version,
                    "observed_server_version": check.observed_version,
                },
            )
        return version

    def _spawn(
        self,
        argv: list[str],
        log_path: Path,
        *,
        phase: str,
    ) -> subprocess.Popen[Any]:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SshConnectionError(
                "ssh_log_failed",
                f"Could not create SSH log directory: {exc}",
                phase=phase,
                retryable=False,
                status_code=500,
            ) from exc
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "shell": False,
            "close_fds": True,
        }
        if self._platform_name == "nt":
            kwargs.update(subprocess_window_kwargs(new_process_group=True))
        else:
            kwargs["start_new_session"] = True
        try:
            log_handle = log_path.open("ab")
        except OSError as exc:
            raise SshConnectionError(
                "ssh_log_failed",
                f"Could not open SSH log: {exc}",
                phase=phase,
                retryable=False,
                status_code=500,
            ) from exc
        try:
            return self._popen_factory(argv, stdout=log_handle, **kwargs)
        except OSError as exc:
            code, message, retryable = _classify_ssh_failure(str(exc), phase=phase)
            raise SshConnectionError(
                code,
                f"{message}: {exc}",
                phase=phase,
                retryable=retryable,
            ) from exc
        finally:
            log_handle.close()

    def _spawn_capture(
        self,
        argv: list[str],
        log_path: Path,
        *,
        phase: str,
    ) -> subprocess.Popen[Any]:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SshConnectionError(
                "ssh_log_failed",
                f"Could not create SSH log directory: {exc}",
                phase=phase,
                retryable=False,
                status_code=500,
            ) from exc
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "shell": False,
            "close_fds": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if self._platform_name == "nt":
            kwargs.update(subprocess_window_kwargs(new_process_group=True))
        else:
            kwargs["start_new_session"] = True
        try:
            return self._popen_factory(argv, **kwargs)
        except OSError as exc:
            code, message, retryable = _classify_ssh_failure(str(exc), phase=phase)
            raise SshConnectionError(
                code,
                f"{message}: {exc}",
                phase=phase,
                retryable=retryable,
            ) from exc

    @staticmethod
    def _append_log(log_path: Path, output: str) -> None:
        if not output:
            return
        try:
            with log_path.open("ab") as handle:
                handle.write(output.encode("utf-8", errors="replace"))
                if not output.endswith("\n"):
                    handle.write(b"\n")
        except OSError:
            return

    def _parse_remote_start(self, output: str, log_path: Path) -> _RemoteStart:
        payload: Mapping[str, Any] | None = None
        for line in reversed(output.splitlines()):
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                candidate = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(candidate, Mapping):
                payload = candidate
                break
        if payload is None:
            raise SshConnectionError(
                "remote_start_invalid_json",
                f"Remote chattree-server start did not return JSON; see {log_path}",
                phase="remote_start",
                retryable=True,
                details={
                    "log_path": str(log_path),
                    "log_tail": _read_log_tail(log_path),
                },
            )
        host = payload.get("host")
        port = payload.get("port")
        if host != REMOTE_SERVER_HOST:
            raise SshConnectionError(
                "remote_start_unsupported_host",
                (
                    "Remote chattree-server start returned an unsupported host; "
                    f"SSH managed servers must bind {REMOTE_SERVER_HOST}"
                ),
                phase="remote_start",
                retryable=False,
                details={"log_path": str(log_path), "payload": dict(payload)},
            )
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise SshConnectionError(
                "remote_start_invalid_json",
                "Remote chattree-server start returned an invalid port",
                phase="remote_start",
                retryable=False,
                details={"log_path": str(log_path), "payload": dict(payload)},
            )
        instance_id = payload.get("server_instance_id")
        if instance_id is not None and not isinstance(instance_id, str):
            raise SshConnectionError(
                "remote_start_invalid_json",
                "Remote chattree-server start returned an invalid server identity",
                phase="remote_start",
                retryable=False,
                details={"log_path": str(log_path), "payload": dict(payload)},
            )
        return _RemoteStart(
            host=str(host),
            port=port,
            server_instance_id=instance_id,
            payload=payload,
        )

    def _log_path(self, profile_id: str) -> Path:
        return (
            Path(self._settings.client_home).expanduser().resolve()
            / "logs"
            / "ssh"
            / f"{_safe_profile_id(profile_id)}.log"
        )

    async def _wait_for_ready(
        self,
        tunnel: _Tunnel,
        profile: ServerProfile,
        phase_callback: PhaseCallback | None,
        *,
        request_id: str,
        allow_timeout: bool,
    ) -> ConnectedServer:
        deadline = self._monotonic() + float(
            self._settings.start_timeout_seconds
            if allow_timeout
            else self._settings.connect_timeout_seconds
        )
        last_error: BaseException | None = None
        while True:
            await self._emit_phase(phase_callback, "handshake")
            try:
                payload = await self._request_json(
                    tunnel.endpoint,
                    request_id=request_id,
                )
                server_id = self._validate_handshake(payload)
                return ConnectedServer(
                    endpoint=tunnel.endpoint,
                    server_instance_id=server_id,
                    handshake=dict(payload),
                )
            except httpx.ConnectError as exc:
                last_error = exc
            except httpx.ConnectTimeout as exc:
                last_error = exc
            except httpx.HTTPError as exc:
                last_error = exc

            exit_code = tunnel.process.poll()
            if exit_code is not None:
                tail = _read_log_tail(tunnel.log_path)
                code, message, retryable = _classify_ssh_failure(
                    tail,
                    phase="ssh_tunnel",
                )
                if code == "ssh_command_failed":
                    code = "ssh_tunnel_not_ready"
                    message = f"SSH tunnel exited with code {exit_code}"
                raise SshConnectionError(
                    code,
                    f"{message}; see {tunnel.log_path}",
                    phase="ssh_tunnel",
                    retryable=retryable,
                    details={"log_path": str(tunnel.log_path), "log_tail": tail},
                )

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise SshConnectionError(
                    "ssh_server_handshake_timeout",
                    f"Remote Server did not become ready through {tunnel.endpoint}",
                    phase="handshake",
                    retryable=True,
                    status_code=504,
                    details={
                        "endpoint": tunnel.endpoint,
                        "log_path": str(tunnel.log_path),
                        "cause": str(last_error) if last_error else None,
                    },
                )
            await self._sleep(
                min(float(self._settings.poll_interval_seconds), remaining)
            )

    async def _request_json(
        self,
        endpoint: str,
        *,
        request_id: str,
    ) -> Mapping[str, Any]:
        response = await self._client.get(
            f"{endpoint}{_HANDSHAKE_PATH}",
            headers={"X-Request-ID": request_id},
        )
        if response.status_code != 200:
            raise SshConnectionError(
                "ssh_server_http_error",
                f"Remote Server handshake returned HTTP {response.status_code}",
                phase="handshake",
                retryable=response.status_code >= 500,
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise SshConnectionError(
                "ssh_server_invalid_json",
                "Remote Server handshake returned invalid JSON",
                phase="handshake",
                retryable=False,
            ) from exc
        if not isinstance(payload, Mapping):
            raise SshConnectionError(
                "ssh_server_invalid_json",
                "Remote Server handshake response must be a JSON object",
                phase="handshake",
                retryable=False,
            )
        return payload

    def _validate_handshake(self, payload: Mapping[str, Any]) -> str:
        protocol_error = handshake_protocol_error(payload)
        if protocol_error is not None:
            raise SshConnectionError(
                "ssh_server_protocol_mismatch",
                f"Remote {protocol_error}",
                phase="handshake",
                retryable=False,
                status_code=409,
                details=handshake_protocol_details(payload),
            )
        version = payload.get("server_version")
        if not isinstance(version, str) or not version.strip():
            raise SshConnectionError(
                "ssh_server_invalid_handshake",
                "Remote Server handshake server_version must be a non-empty string",
                phase="handshake",
                retryable=False,
            )
        check = check_server_version(version)
        if not check.compatible:
            raise SshConnectionError(
                "ssh_server_version_mismatch",
                (
                    "Remote Server version is incompatible: "
                    f"minimum {MIN_SERVER_VERSION}, got {version}"
                ),
                phase="handshake",
                retryable=False,
                status_code=409,
                details=handshake_version_details(payload),
            )
        value = payload.get("server_instance_id")
        if not isinstance(value, str):
            raise SshConnectionError(
                "invalid_server_identity",
                "Remote Server instance id must be a UUID4 string",
                phase="handshake",
                retryable=False,
                status_code=409,
            )
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError) as exc:
            raise SshConnectionError(
                "invalid_server_identity",
                "Remote Server instance id must be a UUID4 string",
                phase="handshake",
                retryable=False,
                status_code=409,
            ) from exc
        if parsed.version != 4 or str(parsed) != value:
            raise SshConnectionError(
                "invalid_server_identity",
                "Remote Server instance id must be a canonical UUID4",
                phase="handshake",
                retryable=False,
                status_code=409,
            )
        return value

    @staticmethod
    async def _emit_phase(
        callback: PhaseCallback | None,
        phase: str,
    ) -> None:
        if callback is None:
            return
        result = callback(phase)
        if inspect.isawaitable(result):
            await result
