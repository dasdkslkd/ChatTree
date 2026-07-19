from __future__ import annotations

import asyncio
import inspect
import os
import re
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, TYPE_CHECKING

import httpx

from backend.core.server import ServerHomeInUseError, ServerHomeLock
from backend.core.subprocess_utils import subprocess_window_kwargs
from client_launcher.http_errors import canonical_request_id
from client_launcher.models import LauncherError

if TYPE_CHECKING:
    from client_launcher.models import ServerProfile
    from client_launcher.settings import LauncherSettings


MIN_PROTOCOL_VERSION = 1
STARTUP_LOG_TAIL_BYTES = 8 * 1024
_HANDSHAKE_PATH = "/api/v1/handshake"
_SHUTDOWN_PATH = "/api/v1/server/shutdown"

PhaseCallback = Callable[[str], object]
Sleep = Callable[[float], Awaitable[None]]


def _loopback_port_is_available(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _configured_python_path(value: str | os.PathLike[str]) -> Path:
    expanded = os.path.expanduser(os.fspath(value))
    return Path(os.path.abspath(expanded))


@dataclass(frozen=True)
class ConnectedServer:
    endpoint: str
    server_instance_id: str
    handshake: dict[str, Any]


class LocalServerError(LauncherError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str,
        retryable: bool,
        status_code: int = 502,
    ) -> None:
        super().__init__(code, message, retryable, status_code)
        self.phase = phase


class LocalServerConfigurationError(LocalServerError):
    pass


class LocalServerResponseError(LocalServerError):
    pass


class LocalServerShutdownUncertainError(LocalServerResponseError):
    def __init__(
        self,
        endpoint: str,
        server_home: Path,
        message: str,
    ) -> None:
        super().__init__(
            "local_server_shutdown_uncertain",
            message,
            phase="local_stop",
            retryable=True,
        )
        self.endpoint = endpoint
        self.server_home = server_home


class LocalServerProtocolError(LocalServerError):
    pass


class LocalServerIdentityError(LocalServerError):
    pass


class LocalServerSpawnError(LocalServerError):
    pass


class LocalServerStartExitedError(LocalServerError):
    def __init__(self, exit_code: int, log_path: Path, log_tail: str) -> None:
        super().__init__(
            "local_server_start_exited",
            _startup_error_message(
                f"Local server exited with code {exit_code}",
                log_path,
                log_tail,
            ),
            phase="local_start",
            retryable=True,
        )
        self.exit_code = exit_code
        self.log_path = log_path
        self.log_tail = log_tail


class LocalServerStartTimeoutError(LocalServerError):
    def __init__(self, endpoint: str, log_path: Path, log_tail: str) -> None:
        super().__init__(
            "local_server_start_timeout",
            _startup_error_message(
                f"Local server did not become ready at {endpoint}",
                log_path,
                log_tail,
            ),
            phase="local_start",
            retryable=True,
            status_code=504,
        )
        self.endpoint = endpoint
        self.log_path = log_path
        self.log_tail = log_tail


class LocalServerStopTimeoutError(LocalServerError):
    def __init__(self, endpoint: str, server_home: Path) -> None:
        super().__init__(
            "local_server_stop_timeout",
            (
                "Local server did not complete cooperative shutdown; "
                f"endpoint={endpoint}; CHATTREE_HOME={server_home}"
            ),
            phase="local_stop",
            retryable=True,
            status_code=504,
        )
        self.endpoint = endpoint
        self.server_home = server_home


def _startup_error_message(base: str, log_path: Path, log_tail: str) -> str:
    message = f"{base}; see {log_path}"
    if log_tail:
        return f"{message}\nLog tail:\n{log_tail}"
    return message


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


class _EndpointUnavailable(Exception):
    def __init__(self, phase: str, cause: httpx.RequestError) -> None:
        super().__init__(str(cause))
        self.phase = phase
        self.cause = cause


class LocalServerConnector:
    def __init__(
        self,
        settings: LauncherSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        platform_name: str = os.name,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        reaper_interval_seconds: float = 1.0,
        port_available: Callable[[int], bool] = _loopback_port_is_available,
        home_lock_factory: Callable[[Path], ServerHomeLock] = ServerHomeLock,
    ) -> None:
        if reaper_interval_seconds <= 0:
            raise ValueError("reaper_interval_seconds must be positive")
        self._settings = settings
        self._popen_factory = popen_factory
        self._platform_name = platform_name
        self._monotonic = monotonic
        self._sleep = sleep
        self._reaper_interval_seconds = reaper_interval_seconds
        self._port_available = port_available
        self._home_lock_factory = home_lock_factory
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(float(settings.connect_timeout_seconds)),
            trust_env=False,
            follow_redirects=False,
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._spawned_processes: set[subprocess.Popen[Any]] = set()
        self._reaper_task: asyncio.Task[None] | None = None

    async def connect(
        self,
        profile: ServerProfile,
        phase_callback: PhaseCallback | None,
        *,
        request_id: str | None = None,
    ) -> ConnectedServer:
        request_id = canonical_request_id(request_id)
        endpoint, server_home, port = self._connection_target(profile)
        lock_key = os.path.normcase(str(server_home))
        lock = self._locks.setdefault(lock_key, asyncio.Lock())

        async with lock:
            try:
                return await self._probe(
                    endpoint,
                    profile,
                    phase_callback,
                    request_id=request_id,
                )
            except _EndpointUnavailable as exc:
                if not self._port_available(port):
                    raise self._transport_error(endpoint, exc) from exc.cause

            await self._emit_phase(phase_callback, "local_start")
            process, log_path = self._spawn(profile, server_home, port)
            self._track_process(process)
            return await self._wait_for_ready(
                endpoint,
                profile,
                phase_callback,
                process,
                log_path,
                request_id=request_id,
            )

    async def close(self) -> None:
        reaper = self._reaper_task
        self._reaper_task = None
        if reaper is not None and not reaper.done():
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
        self._poll_spawned_processes()
        self._spawned_processes.clear()
        await self._client.aclose()

    async def request_shutdown(
        self,
        profile: ServerProfile,
        expected_server_instance_id: str,
        *,
        request_id: str | None = None,
    ) -> tuple[str, Path]:
        endpoint, server_home, _port = self._connection_target(profile)
        request_id = canonical_request_id(request_id)
        try:
            response = await self._client.post(
                f"{endpoint}{_SHUTDOWN_PATH}",
                json={
                    "expected_server_instance_id": expected_server_instance_id,
                },
                headers={"X-Request-ID": request_id},
            )
        except httpx.HTTPError as exc:
            raise LocalServerShutdownUncertainError(
                endpoint,
                server_home,
                f"Local server shutdown request failed: {exc}",
            ) from exc
        if response.status_code != 202:
            raise self._shutdown_response_error(response)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise LocalServerShutdownUncertainError(
                endpoint,
                server_home,
                "Local server shutdown returned invalid JSON",
            ) from exc
        if (
            not isinstance(payload, Mapping)
            or payload.get("server_instance_id") != expected_server_instance_id
            or payload.get("status") != "stopping"
        ):
            raise LocalServerShutdownUncertainError(
                endpoint,
                server_home,
                "Local server shutdown returned an invalid response",
            )
        return endpoint, server_home

    def shutdown_target(self, profile: ServerProfile) -> tuple[str, Path]:
        endpoint, server_home, _port = self._connection_target(profile)
        return endpoint, server_home

    async def wait_stopped(
        self,
        endpoint: str,
        server_home: Path,
        *,
        timeout: float,
    ) -> None:
        deadline = self._monotonic() + max(0.0, float(timeout))
        while True:
            endpoint_down = await self._endpoint_is_down(endpoint)
            home_unlocked = self._home_is_unlocked(server_home)
            if endpoint_down and home_unlocked:
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise LocalServerStopTimeoutError(endpoint, server_home)
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

    def _home_is_unlocked(self, server_home: Path) -> bool:
        lock = self._home_lock_factory(server_home)
        try:
            lock.acquire()
        except ServerHomeInUseError:
            return False
        finally:
            if lock.acquired:
                lock.release()
        return True

    @staticmethod
    def _shutdown_response_error(response: httpx.Response) -> LauncherError:
        try:
            payload = response.json()
            body = payload.get("error") if isinstance(payload, Mapping) else None
        except (TypeError, ValueError):
            body = None
        if isinstance(body, Mapping):
            code = body.get("code")
            message = body.get("message")
            retryable = body.get("retryable")
            details = body.get("details")
            if (
                isinstance(code, str)
                and isinstance(message, str)
                and isinstance(retryable, bool)
            ):
                return LauncherError(
                    code,
                    message,
                    retryable,
                    response.status_code,
                    details=details if isinstance(details, Mapping) else None,
                )
        return LocalServerResponseError(
            "local_server_shutdown_http_error",
            f"Local server shutdown returned HTTP {response.status_code}",
            phase="local_stop",
            retryable=response.status_code >= 500,
            status_code=response.status_code,
        )

    def _track_process(self, process: subprocess.Popen[Any]) -> None:
        self._spawned_processes.add(process)
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_spawned_processes())

    async def _reap_spawned_processes(self) -> None:
        try:
            while self._spawned_processes:
                self._poll_spawned_processes()
                if self._spawned_processes:
                    await asyncio.sleep(self._reaper_interval_seconds)
        except asyncio.CancelledError:
            return

    def _poll_spawned_processes(self) -> None:
        for process in tuple(self._spawned_processes):
            if process.poll() is not None:
                self._spawned_processes.discard(process)

    def _connection_target(
        self,
        profile: ServerProfile,
    ) -> tuple[str, Path, int]:
        if getattr(profile, "kind", None) != "local":
            raise LocalServerConfigurationError(
                "invalid_local_profile",
                "Local server connector requires a local profile",
                phase="handshake",
                retryable=False,
                status_code=400,
            )

        local = getattr(profile, "local", None)
        raw_home = getattr(local, "server_home", None)
        port = getattr(local, "server_port", None)
        if not isinstance(raw_home, (str, os.PathLike)) or not os.fspath(
            raw_home
        ).strip():
            raise LocalServerConfigurationError(
                "invalid_local_profile",
                "Local server home must be a non-empty path",
                phase="handshake",
                retryable=False,
                status_code=400,
            )
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise LocalServerConfigurationError(
                "invalid_local_profile",
                "Local server port must be an integer from 1 to 65535",
                phase="handshake",
                retryable=False,
                status_code=400,
            )

        server_home = Path(raw_home).expanduser().resolve()
        return f"http://127.0.0.1:{port}", server_home, port

    async def _probe(
        self,
        endpoint: str,
        profile: ServerProfile,
        phase_callback: PhaseCallback | None,
        *,
        request_id: str,
    ) -> ConnectedServer:
        await self._emit_phase(phase_callback, "handshake")
        handshake = await self._request_json(
            endpoint,
            _HANDSHAKE_PATH,
            "handshake",
            request_id=request_id,
        )
        handshake_id = self._validate_handshake(handshake)

        return ConnectedServer(
            endpoint=endpoint,
            server_instance_id=handshake_id,
            handshake=dict(handshake),
        )

    async def _request_json(
        self,
        endpoint: str,
        path: str,
        phase: str,
        *,
        request_id: str,
    ) -> Mapping[str, Any]:
        try:
            response = await self._client.get(
                f"{endpoint}{path}",
                headers={"X-Request-ID": request_id},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise _EndpointUnavailable(phase, exc) from exc
        except httpx.HTTPError as exc:
            raise LocalServerResponseError(
                "local_server_transport_error",
                f"Local server {phase} request failed: {exc}",
                phase=phase,
                retryable=True,
            ) from exc

        if response.status_code != 200:
            raise LocalServerResponseError(
                "local_server_http_error",
                f"Local server {phase} returned HTTP {response.status_code}",
                phase=phase,
                retryable=response.status_code >= 500,
            )

        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise LocalServerResponseError(
                "local_server_invalid_json",
                f"Local server {phase} returned invalid JSON",
                phase=phase,
                retryable=False,
            ) from exc
        if not isinstance(payload, Mapping):
            raise LocalServerResponseError(
                "local_server_invalid_json",
                f"Local server {phase} response must be a JSON object",
                phase=phase,
                retryable=False,
            )
        return payload

    def _validate_handshake(self, payload: Mapping[str, Any]) -> str:
        protocol_version = payload.get("protocol_version")
        if (
            isinstance(protocol_version, bool)
            or not isinstance(protocol_version, int)
            or protocol_version < MIN_PROTOCOL_VERSION
        ):
            raise LocalServerProtocolError(
                "local_server_protocol_mismatch",
                (
                    "Local server protocol version is incompatible: "
                    f"minimum {MIN_PROTOCOL_VERSION}, got {protocol_version!r}"
                ),
                phase="handshake",
                retryable=False,
                status_code=409,
            )

        for field in ("server_version", "platform"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                raise LocalServerResponseError(
                    "local_server_invalid_handshake",
                    f"Local server handshake {field} must be a non-empty string",
                    phase="handshake",
                    retryable=False,
                )

        features = payload.get("features")
        if not isinstance(features, list) or any(
            not isinstance(feature, str) or not feature for feature in features
        ):
            raise LocalServerResponseError(
                "local_server_invalid_handshake",
                "Local server handshake features must be a list of strings",
                phase="handshake",
                retryable=False,
            )
        if not isinstance(payload.get("provider_configured"), bool):
            raise LocalServerResponseError(
                "local_server_invalid_handshake",
                "Local server handshake provider_configured must be a boolean",
                phase="handshake",
                retryable=False,
            )

        return self._server_id(payload, "handshake")

    def _server_id(self, payload: Mapping[str, Any], phase: str) -> str:
        value = payload.get("server_instance_id")
        if not isinstance(value, str):
            raise LocalServerIdentityError(
                "invalid_server_identity",
                f"Local server {phase} instance id must be a UUID4 string",
                phase=phase,
                retryable=False,
                status_code=409,
            )
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError) as exc:
            raise LocalServerIdentityError(
                "invalid_server_identity",
                f"Local server {phase} instance id must be a UUID4 string",
                phase=phase,
                retryable=False,
                status_code=409,
            ) from exc
        if parsed.version != 4 or str(parsed) != value:
            raise LocalServerIdentityError(
                "invalid_server_identity",
                f"Local server {phase} instance id must be a canonical UUID4",
                phase=phase,
                retryable=False,
                status_code=409,
            )
        return value

    def _spawn(
        self,
        profile: ServerProfile,
        server_home: Path,
        port: int,
    ) -> tuple[subprocess.Popen[Any], Path]:
        client_home = Path(self._settings.client_home).expanduser().resolve()
        log_dir = client_home / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LocalServerSpawnError(
                "local_server_log_failed",
                f"Could not create local server log directory: {exc}",
                phase="local_start",
                retryable=False,
            ) from exc

        profile_id = str(getattr(profile, "id", "local"))
        safe_profile_id = re.sub(r"[^A-Za-z0-9._-]+", "-", profile_id)
        safe_profile_id = safe_profile_id.strip("._-")[:80] or "local"
        log_path = log_dir / f"local-server-{safe_profile_id}.log"
        project_root = Path(self._settings.project_root).expanduser().resolve()
        server_python = _configured_python_path(self._settings.server_python)
        argv = [
            str(server_python),
            "-m",
            "main",
        ]
        env = os.environ.copy()
        env.update(
            {
                "CHATTREE_HOME": str(server_home),
                "CHATTREE_SERVER_PORT": str(port),
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        kwargs: dict[str, Any] = {
            "cwd": str(project_root),
            "env": env,
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
            raise LocalServerSpawnError(
                "local_server_log_failed",
                f"Could not open local server log: {exc}",
                phase="local_start",
                retryable=False,
            ) from exc

        try:
            process = self._popen_factory(
                argv,
                stdout=log_handle,
                **kwargs,
            )
        except OSError as exc:
            raise LocalServerSpawnError(
                "local_server_spawn_failed",
                f"Could not start local server: {exc}",
                phase="local_start",
                retryable=True,
            ) from exc
        finally:
            log_handle.close()

        return process, log_path

    async def _wait_for_ready(
        self,
        endpoint: str,
        profile: ServerProfile,
        phase_callback: PhaseCallback | None,
        process: subprocess.Popen[Any],
        log_path: Path,
        *,
        request_id: str,
    ) -> ConnectedServer:
        deadline = self._monotonic() + float(
            self._settings.start_timeout_seconds
        )
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise LocalServerStartTimeoutError(
                    endpoint,
                    log_path,
                    _read_log_tail(log_path),
                )
            try:
                return await asyncio.wait_for(
                    self._probe(
                        endpoint,
                        profile,
                        phase_callback,
                        request_id=request_id,
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                raise LocalServerStartTimeoutError(
                    endpoint,
                    log_path,
                    _read_log_tail(log_path),
                ) from None
            except _EndpointUnavailable:
                pass

            exit_code = process.poll()
            if exit_code is not None:
                raise LocalServerStartExitedError(
                    exit_code,
                    log_path,
                    _read_log_tail(log_path),
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise LocalServerStartTimeoutError(
                    endpoint,
                    log_path,
                    _read_log_tail(log_path),
                )
            await self._sleep(
                min(
                    float(self._settings.poll_interval_seconds),
                    remaining,
                )
            )

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

    @staticmethod
    def _transport_error(
        endpoint: str,
        unavailable: _EndpointUnavailable,
    ) -> LocalServerResponseError:
        return LocalServerResponseError(
            "local_server_transport_error",
            (
                f"Local server {unavailable.phase} request failed at "
                f"{endpoint}: {unavailable.cause}"
            ),
            phase=unavailable.phase,
            retryable=True,
        )
