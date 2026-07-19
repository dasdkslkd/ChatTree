from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import client_launcher.local_server as local_server
from client_launcher.http_errors import REQUEST_ID_RE
from client_launcher.local_server import (
    LocalServerConnector,
    LocalServerIdentityError,
    LocalServerProtocolError,
    LocalServerResponseError,
    LocalServerShutdownUncertainError,
    LocalServerStartExitedError,
    LocalServerStartTimeoutError,
    LocalServerStopTimeoutError,
    STARTUP_LOG_TAIL_BYTES,
)
from client_launcher.models import LauncherError
from backend.core.server import ServerHomeInUseError


SERVER_ID = "5fb0d7cc-785e-40c2-875d-218447b15583"
OTHER_SERVER_ID = "74197461-d4b2-436f-9d7a-16131dccd034"


def test_configured_python_path_never_resolves_symlinks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    configured = tmp_path / "venv" / "bin" / "python"
    expected = Path(os.path.abspath(os.path.expanduser(str(configured))))

    def fail_resolve(_path):
        raise AssertionError("configured interpreter path must not resolve symlinks")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    assert local_server._configured_python_path(configured) == expected


def _settings(tmp_path: Path, **overrides):
    values = {
        "client_home": tmp_path / "client",
        "project_root": tmp_path / "project",
        "server_python": tmp_path / "python",
        "connect_timeout_seconds": 0.1,
        "start_timeout_seconds": 1.0,
        "poll_interval_seconds": 0.1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _profile(
    tmp_path: Path,
    *,
    server_id: str | None = SERVER_ID,
    port: int = 18001,
):
    return SimpleNamespace(
        id="local-profile",
        label="Local",
        kind="local",
        auto_connect=True,
        bound_server_instance_id=server_id,
        local=SimpleNamespace(
            server_home=str(tmp_path / "server-home"),
            server_port=port,
        ),
    )


def _handshake(
    server_id: str = SERVER_ID,
    *,
    protocol_version: int = 1,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "server_instance_id": server_id,
            "protocol_version": protocol_version,
            "server_version": "0.1.0",
            "platform": "windows",
            "features": ["conversations", "runs"],
            "provider_configured": False,
        },
    )


class FakeProcess:
    def __init__(self, return_codes: list[int | None] | None = None) -> None:
        self.pid = 4321
        self._return_codes = list(return_codes or [None])
        self.poll_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        self.poll_calls += 1
        if len(self._return_codes) > 1:
            return self._return_codes.pop(0)
        return self._return_codes[0]

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1


class FakePopen:
    def __init__(
        self,
        process: FakeProcess | None = None,
        *,
        log_bytes: bytes = b"",
    ) -> None:
        self.process = process or FakeProcess()
        self.log_bytes = log_bytes
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self.log_bytes:
            kwargs["stdout"].write(self.log_bytes)
            kwargs["stdout"].flush()
        return self.process


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


class FakeHomeLock:
    def __init__(self, *, busy: bool) -> None:
        self.busy = busy
        self.acquired = False
        self.release_calls = 0

    def acquire(self) -> None:
        if self.busy:
            raise ServerHomeInUseError("home is locked")
        self.acquired = True

    def release(self) -> None:
        self.release_calls += 1
        self.acquired = False


def test_existing_server_is_reused_without_spawn(tmp_path: Path):
    paths: list[str] = []
    request_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        request_ids.extend(request.headers.get_list("x-request-id"))
        return _handshake()

    popen = FakePopen()
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        popen_factory=popen,
    )

    connected = asyncio.run(
        connector.connect(
            _profile(tmp_path),
            None,
            request_id="existing-server-tree",
        )
    )
    asyncio.run(connector.close())

    assert connected.endpoint == "http://127.0.0.1:18001"
    assert connected.server_instance_id == SERVER_ID
    assert connected.handshake["provider_configured"] is False
    assert paths == ["/api/v1/handshake"]
    assert request_ids == ["existing-server-tree"]
    assert popen.calls == []


def test_connection_refusal_spawns_detached_production_server(tmp_path: Path):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectError("refused", request=request)
        return _handshake()

    phases: list[str] = []
    process = FakeProcess()
    popen = FakePopen(process)
    project_root = tmp_path / "project"
    server_python = tmp_path / "python"
    connector = LocalServerConnector(
        _settings(
            tmp_path,
            project_root=project_root,
            server_python=server_python,
        ),
        transport=httpx.MockTransport(handler),
        popen_factory=popen,
        platform_name="posix",
    )

    connected = asyncio.run(connector.connect(_profile(tmp_path), phases.append))
    asyncio.run(connector.close())

    assert connected.server_instance_id == SERVER_ID
    assert len(popen.calls) == 1
    argv, kwargs = popen.calls[0]
    assert argv == [
        str(server_python.resolve()),
        "-m",
        "main",
    ]
    assert kwargs["shell"] is False
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["cwd"] == str(project_root.resolve())
    assert kwargs["env"]["CHATTREE_HOME"] == str(
        (tmp_path / "server-home").resolve()
    )
    assert kwargs["env"]["CHATTREE_SERVER_PORT"] == "18001"
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert kwargs["stdout"].closed
    assert not list((tmp_path / "client" / "logs").glob("*.spawn.pid"))
    assert phases == ["handshake", "local_start", "handshake"]
    assert process.terminate_calls == 0
    assert process.kill_calls == 0


def test_loopback_connect_timeout_still_starts_server(tmp_path: Path):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectTimeout("timed out", request=request)
        return _handshake()

    popen = FakePopen()
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        popen_factory=popen,
        port_available=lambda _port: True,
    )

    connected = asyncio.run(connector.connect(_profile(tmp_path), None))
    asyncio.run(connector.close())

    assert connected.server_instance_id == SERVER_ID
    assert len(popen.calls) == 1


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
def test_unavailable_handshake_on_occupied_port_never_spawns(
    tmp_path: Path,
    error_type,
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("unavailable", request=request)

    popen = FakePopen()
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        popen_factory=popen,
        port_available=lambda _port: False,
    )

    with pytest.raises(LocalServerResponseError):
        asyncio.run(connector.connect(_profile(tmp_path), None))
    asyncio.run(connector.close())

    assert popen.calls == []


def test_handshake_connection_error_never_spawns(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dropped", request=request)

    popen = FakePopen()
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        popen_factory=popen,
        port_available=lambda _port: False,
    )

    with pytest.raises(LocalServerResponseError):
        asyncio.run(connector.connect(_profile(tmp_path), None))
    asyncio.run(connector.close())

    assert popen.calls == []


def test_windows_spawn_hides_console_and_keeps_process_group(tmp_path: Path):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("refused", request=request)
        return _handshake()

    popen = FakePopen()
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        popen_factory=popen,
        platform_name="nt",
    )

    asyncio.run(connector.connect(_profile(tmp_path), None))
    asyncio.run(connector.close())

    _, kwargs = popen.calls[0]
    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(
        subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0x00000200,
    )
    assert kwargs["creationflags"] == expected
    assert "start_new_session" not in kwargs


@pytest.mark.parametrize("status_code", [301, 404, 500])
def test_http_response_on_handshake_never_spawns(tmp_path: Path, status_code: int):
    popen = FakePopen()
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code)),
        popen_factory=popen,
    )

    with pytest.raises(LocalServerResponseError):
        asyncio.run(connector.connect(_profile(tmp_path), None))
    asyncio.run(connector.close())

    assert popen.calls == []


def test_malformed_handshake_never_spawns(tmp_path: Path):
    popen = FakePopen()
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b"not-json",
            )
        ),
        popen_factory=popen,
    )

    with pytest.raises(LocalServerResponseError):
        asyncio.run(connector.connect(_profile(tmp_path), None))
    asyncio.run(connector.close())

    assert popen.calls == []


def test_protocol_mismatch_fails_without_spawn(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _handshake(protocol_version=0)

    popen = FakePopen()
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        popen_factory=popen,
    )

    with pytest.raises(LocalServerProtocolError):
        asyncio.run(connector.connect(_profile(tmp_path), None))
    asyncio.run(connector.close())

    assert popen.calls == []


def test_future_protocol_version_is_accepted_without_spawn(tmp_path: Path):
    popen = FakePopen()
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(
            lambda request: _handshake(protocol_version=2)
        ),
        popen_factory=popen,
    )

    connected = asyncio.run(connector.connect(_profile(tmp_path), None))
    asyncio.run(connector.close())

    assert connected.handshake["protocol_version"] == 2
    assert popen.calls == []


def test_handshake_identity_must_be_canonical_uuid4(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _handshake("not-a-server-id")

    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        popen_factory=FakePopen(),
    )

    with pytest.raises(LocalServerIdentityError):
        asyncio.run(connector.connect(_profile(tmp_path), None))
    asyncio.run(connector.close())


def test_profile_binding_is_left_to_session_manager(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _handshake()

    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        popen_factory=FakePopen(),
    )

    connected = asyncio.run(
        connector.connect(
            _profile(tmp_path, server_id=OTHER_SERVER_ID),
            None,
        )
    )
    asyncio.run(connector.close())

    assert connected.server_instance_id == SERVER_ID


def test_child_early_exit_is_reported(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    omitted_prefix = b"must-not-leak-from-full-log\n"
    visible_suffix = b"fatal: child startup failed"
    log_bytes = (
        omitted_prefix
        + (b"x" * STARTUP_LOG_TAIL_BYTES)
        + visible_suffix
    )
    popen = FakePopen(FakeProcess([17]), log_bytes=log_bytes)
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        popen_factory=popen,
    )

    with pytest.raises(LocalServerStartExitedError) as exc_info:
        asyncio.run(connector.connect(_profile(tmp_path), None))
    asyncio.run(connector.close())

    assert exc_info.value.exit_code == 17
    assert exc_info.value.log_path.name == "local-server-local-profile.log"
    assert exc_info.value.log_tail.endswith(visible_suffix.decode("utf-8"))
    assert omitted_prefix.decode("utf-8").strip() not in exc_info.value.log_tail
    assert len(exc_info.value.log_tail.encode("utf-8")) <= STARTUP_LOG_TAIL_BYTES
    assert exc_info.value.log_tail in str(exc_info.value)


def test_start_timeout_does_not_kill_server(tmp_path: Path):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ConnectError("refused", request=request)

    process = FakeProcess([None])
    popen = FakePopen(process, log_bytes=b"startup is still waiting")
    clock = FakeClock()
    connector = LocalServerConnector(
        _settings(tmp_path, start_timeout_seconds=0.2, poll_interval_seconds=0.1),
        transport=httpx.MockTransport(handler),
        popen_factory=popen,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    with pytest.raises(LocalServerStartTimeoutError) as exc_info:
        asyncio.run(connector.connect(_profile(tmp_path), None))
    asyncio.run(connector.close())

    assert process.terminate_calls == 0
    assert process.kill_calls == 0
    assert exc_info.value.log_tail == "startup is still waiting"
    assert exc_info.value.log_tail in str(exc_info.value)
    assert requests == 3


def test_start_timeout_bounds_a_slow_readiness_probe(tmp_path: Path):
    async def scenario() -> None:
        requests = 0
        never_ready = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            if requests == 1:
                raise httpx.ConnectError("refused", request=request)
            await never_ready.wait()
            raise AssertionError("unreachable")

        process = FakeProcess([None])
        connector = LocalServerConnector(
            _settings(tmp_path, start_timeout_seconds=0.05),
            transport=httpx.MockTransport(handler),
            popen_factory=FakePopen(process),
        )

        with pytest.raises(LocalServerStartTimeoutError):
            await asyncio.wait_for(
                connector.connect(_profile(tmp_path), None),
                timeout=0.25,
            )
        await connector.close()

        assert requests == 2
        assert process.terminate_calls == 0
        assert process.kill_calls == 0

    asyncio.run(scenario())


def test_startup_retries_reuse_one_canonical_parent_request_id(tmp_path: Path):
    paths: list[str] = []
    request_ids: list[str] = []
    attempt = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempt
        attempt += 1
        paths.append(request.url.path)
        request_ids.extend(request.headers.get_list("x-request-id"))
        if attempt in {1, 2}:
            raise httpx.ConnectError("not ready", request=request)
        return _handshake()

    clock = FakeClock()
    connector = LocalServerConnector(
        _settings(tmp_path, start_timeout_seconds=2.0),
        transport=httpx.MockTransport(handler),
        popen_factory=FakePopen(FakeProcess([None])),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    connected = asyncio.run(
        connector.connect(
            _profile(tmp_path),
            None,
            request_id="invalid parent id",
        )
    )
    asyncio.run(connector.close())

    assert connected.server_instance_id == SERVER_ID
    assert paths == [
        "/api/v1/handshake",
        "/api/v1/handshake",
        "/api/v1/handshake",
    ]
    assert len(request_ids) == len(paths)
    assert len(set(request_ids)) == 1
    assert request_ids[0] != "invalid parent id"
    assert request_ids[0].startswith("req_")
    assert REQUEST_ID_RE.fullmatch(request_ids[0])


def test_spawned_server_exit_is_reaped_without_termination(tmp_path: Path):
    async def scenario() -> None:
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            if requests == 1:
                raise httpx.ConnectError("refused", request=request)
            return _handshake()

        process = FakeProcess([None])
        connector = LocalServerConnector(
            _settings(tmp_path),
            transport=httpx.MockTransport(handler),
            popen_factory=FakePopen(process),
            reaper_interval_seconds=0.001,
        )

        await connector.connect(_profile(tmp_path), None)
        process._return_codes = [0]
        await asyncio.sleep(0.02)
        await connector.close()

        assert process.poll_calls > 0
        assert process.terminate_calls == 0
        assert process.kill_calls == 0

    asyncio.run(scenario())


def test_shutdown_request_uses_expected_identity_and_request_id(tmp_path: Path):
    seen: list[tuple[str, dict, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.url.path,
                json.loads(request.content.decode("utf-8")),
                request.headers.get("x-request-id"),
            )
        )
        return httpx.Response(
            202,
            json={"server_instance_id": SERVER_ID, "status": "stopping"},
        )

    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        popen_factory=FakePopen(),
    )

    endpoint, server_home = asyncio.run(
        connector.request_shutdown(
            _profile(tmp_path),
            SERVER_ID,
            request_id="stop-request-tree",
        )
    )
    asyncio.run(connector.close())

    assert endpoint == "http://127.0.0.1:18001"
    assert server_home == (tmp_path / "server-home").resolve()
    assert seen == [
        (
            "/api/v1/server/shutdown",
            {"expected_server_instance_id": SERVER_ID},
            "stop-request-tree",
        )
    ]


def test_shutdown_preserves_modern_server_error(tmp_path: Path):
    response = httpx.Response(
        409,
        json={
            "error": {
                "code": "server_busy",
                "message": "Active runs prevent shutdown",
                "retryable": True,
                "details": {"active_run_ids": ["run-1"]},
            },
            "request_id": "stop-busy-tree",
        },
    )
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(lambda _request: response),
        popen_factory=FakePopen(),
    )

    with pytest.raises(LauncherError) as exc_info:
        asyncio.run(connector.request_shutdown(_profile(tmp_path), SERVER_ID))
    asyncio.run(connector.close())

    assert exc_info.value.code == "server_busy"
    assert exc_info.value.status_code == 409
    assert exc_info.value.retryable is True
    assert exc_info.value.details == {"active_run_ids": ["run-1"]}


def test_shutdown_marks_invalid_accepted_payload_as_uncertain(tmp_path: Path):
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                202,
                json={"server_instance_id": OTHER_SERVER_ID, "status": "stopping"},
            )
        ),
        popen_factory=FakePopen(),
    )

    with pytest.raises(LocalServerShutdownUncertainError) as exc_info:
        asyncio.run(connector.request_shutdown(_profile(tmp_path), SERVER_ID))
    asyncio.run(connector.close())

    assert exc_info.value.code == "local_server_shutdown_uncertain"
    assert exc_info.value.endpoint == "http://127.0.0.1:18001"
    assert exc_info.value.server_home == (tmp_path / "server-home").resolve()


def test_shutdown_marks_transport_failure_as_uncertain(tmp_path: Path):
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("response lost", request=request)

    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(fail),
        popen_factory=FakePopen(),
    )

    with pytest.raises(LocalServerShutdownUncertainError) as exc_info:
        asyncio.run(connector.request_shutdown(_profile(tmp_path), SERVER_ID))
    asyncio.run(connector.close())

    assert exc_info.value.retryable is True
    assert exc_info.value.endpoint == "http://127.0.0.1:18001"
    assert exc_info.value.server_home == (tmp_path / "server-home").resolve()


def test_wait_stopped_requires_endpoint_down_and_home_lock_free(tmp_path: Path):
    attempts = 0
    locks: list[FakeHomeLock] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _handshake()
        raise httpx.ConnectError("stopped", request=request)

    def home_lock_factory(_server_home: Path) -> FakeHomeLock:
        lock = FakeHomeLock(busy=len(locks) == 0)
        locks.append(lock)
        return lock

    clock = FakeClock()
    connector = LocalServerConnector(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        popen_factory=FakePopen(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        home_lock_factory=home_lock_factory,
    )

    asyncio.run(
        connector.wait_stopped(
            "http://127.0.0.1:18001",
            (tmp_path / "server-home").resolve(),
            timeout=1.0,
        )
    )
    asyncio.run(connector.close())

    assert attempts == 2
    assert len(locks) == 2
    assert locks[0].release_calls == 0
    assert locks[1].release_calls == 1


def test_wait_stopped_times_out_without_terminating_process(tmp_path: Path):
    process = FakeProcess()
    popen = FakePopen(process)
    clock = FakeClock()
    connector = LocalServerConnector(
        _settings(tmp_path, poll_interval_seconds=0.1),
        transport=httpx.MockTransport(lambda _request: _handshake()),
        popen_factory=popen,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        home_lock_factory=lambda _path: FakeHomeLock(busy=True),
    )

    with pytest.raises(LocalServerStopTimeoutError):
        asyncio.run(
            connector.wait_stopped(
                "http://127.0.0.1:18001",
                (tmp_path / "server-home").resolve(),
                timeout=0.2,
            )
        )
    asyncio.run(connector.close())

    assert popen.calls == []
    assert process.terminate_calls == 0
    assert process.kill_calls == 0
