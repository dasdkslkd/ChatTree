from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
import pytest

from client_launcher.models import LauncherError, ServerProfile, SshTarget
from client_launcher.settings import LauncherSettings
from client_launcher.ssh_connector import SshServerConnector


SERVER_A = "11111111-1111-4111-8111-111111111111"


class FakeProcess:
    def __init__(
        self,
        exit_code: int | None = None,
        *,
        output: str = "",
    ) -> None:
        self.exit_code = exit_code
        self.output = output
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        if self.exit_code is None:
            if self.terminated or self.killed:
                self.exit_code = 0
            else:
                raise subprocess.TimeoutExpired("ssh", timeout)
        return self.exit_code

    def communicate(self, timeout=None):
        return self.output, None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.exit_code = -9


class FakeTimeoutProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__(None)

    def communicate(self, timeout=None):
        raise subprocess.TimeoutExpired("ssh", timeout)

    def wait(self, timeout=None):
        self.exit_code = 0
        return 0


def _settings(tmp_path: Path) -> LauncherSettings:
    return LauncherSettings(
        client_home=tmp_path / "client",
        project_root=tmp_path / "project",
        server_python="python",
        connect_timeout_seconds=0.01,
        start_timeout_seconds=0.1,
        poll_interval_seconds=0.001,
    )


def _profile(host: str = "gpu-box") -> ServerProfile:
    return ServerProfile(
        id="ssh:gpu",
        label="SSH: gpu-box",
        kind="ssh",
        auto_connect=False,
        bound_server_instance_id=None,
        ssh=SshTarget(config_host=host),
    )


def test_connect_starts_remote_server_then_tunnel_to_reported_port(
    tmp_path: Path,
):
    async def scenario() -> None:
        calls: list[list[str]] = []
        processes: list[FakeProcess] = []

        def popen(argv, **kwargs):
            calls.append(list(argv))
            assert kwargs["shell"] is False
            assert kwargs["stdin"] is subprocess.DEVNULL
            if "-N" in argv:
                process = FakeProcess(None)
            elif "--version" in argv:
                process = FakeProcess(0, output="chattree-server 0.1.0\n")
            else:
                process = FakeProcess(
                    0,
                    output=(
                        '{"status":"started","host":"127.0.0.1",'
                        '"port":18082,"server_instance_id":null}\n'
                    ),
                )
            processes.append(process)
            return process

        async def upstream(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "server_instance_id": SERVER_A,
                    "protocol_version": 1,
                    "server_version": "0.1.0",
                    "platform": "linux",
                    "features": [],
                    "provider_configured": True,
                },
            )

        connector = SshServerConnector(
            _settings(tmp_path),
            transport=httpx.MockTransport(upstream),
            popen_factory=popen,
            platform_name="nt",
            allocate_port=lambda: 19081,
        )

        connected = await connector.connect(_profile(), lambda _phase: None)
        await connector.disconnect(_profile())
        await connector.close()

        assert connected.endpoint == "http://127.0.0.1:19081"
        assert connected.server_instance_id == SERVER_A
        assert calls[0] == [
            "ssh",
            "gpu-box",
            "chattree-server",
            "--version",
        ]
        assert calls[1] == [
            "ssh",
            "gpu-box",
            "chattree-server",
            "start",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ]
        assert calls[2] == [
            "ssh",
            "-o",
            "ExitOnForwardFailure=yes",
            "-N",
            "-L",
            "127.0.0.1:19081:127.0.0.1:18082",
            "gpu-box",
        ]
        assert processes[2].terminated is True

    asyncio.run(scenario())


def test_already_running_remote_start_output_is_accepted(tmp_path: Path):
    async def scenario() -> None:
        calls: list[list[str]] = []

        def popen(argv, **_kwargs):
            calls.append(list(argv))
            if "-N" in argv:
                return FakeProcess(None)
            if "--version" in argv:
                return FakeProcess(0, output="chattree-server 0.1.0\n")
            return FakeProcess(
                0,
                output=(
                    '{"status":"already_running","host":"127.0.0.1",'
                    f'"port":18083,"server_instance_id":"{SERVER_A}"}}\n'
                ),
            )

        async def upstream(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "server_instance_id": SERVER_A,
                    "protocol_version": 1,
                    "server_version": "0.1.0",
                    "platform": "linux",
                    "features": [],
                    "provider_configured": True,
                },
            )

        connector = SshServerConnector(
            _settings(tmp_path),
            transport=httpx.MockTransport(upstream),
            popen_factory=popen,
            allocate_port=lambda: 19082,
        )

        await connector.connect(_profile(), None)
        await connector.close()

        assert len(calls) == 3
        assert calls[1][-2:] == ["--port", "0"]
        assert calls[2][5] == "127.0.0.1:19082:127.0.0.1:18083"

    asyncio.run(scenario())


def test_remote_start_rejects_non_ipv4_loopback_host(tmp_path: Path):
    async def scenario() -> None:
        calls: list[list[str]] = []

        def popen(argv, **_kwargs):
            calls.append(list(argv))
            if "--version" in argv:
                return FakeProcess(0, output="chattree-server 0.1.0\n")
            return FakeProcess(
                0,
                output='{"status":"already_running","host":"::1","port":18083}\n',
            )

        connector = SshServerConnector(
            _settings(tmp_path),
            popen_factory=popen,
            allocate_port=lambda: 19082,
        )

        with pytest.raises(LauncherError) as exc_info:
            await connector.connect(_profile(), None)
        await connector.close()

        assert exc_info.value.code == "remote_start_unsupported_host"
        assert exc_info.value.status_code == 502
        assert len(calls) == 2

    asyncio.run(scenario())


def test_tunnel_exit_maps_to_typed_launcher_error(tmp_path: Path):
    async def scenario() -> None:
        def popen(argv, **_kwargs):
            if "-N" in argv:
                return FakeProcess(255, output="channel 1: open failed")
            if "--version" in argv:
                return FakeProcess(0, output="chattree-server 0.1.0\n")
            return FakeProcess(
                0,
                output='{"status":"started","host":"127.0.0.1","port":18084}\n',
            )

        async def upstream(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("forward failed", request=request)

        connector = SshServerConnector(
            _settings(tmp_path),
            transport=httpx.MockTransport(upstream),
            popen_factory=popen,
            allocate_port=lambda: 19083,
        )

        with pytest.raises(LauncherError) as exc_info:
            await connector.connect(_profile(), None)
        await connector.close()

        assert exc_info.value.code == "ssh_tunnel_not_ready"
        assert exc_info.value.status_code == 502

    asyncio.run(scenario())


def test_transient_channel_open_failure_waits_for_remote_server(
    tmp_path: Path,
):
    async def scenario() -> None:
        calls: list[list[str]] = []
        attempts = 0

        def popen(argv, **_kwargs):
            calls.append(list(argv))
            if "-N" in argv:
                return FakeProcess(None)
            if "--version" in argv:
                return FakeProcess(0, output="chattree-server 0.1.0\n")
            return FakeProcess(
                0,
                output='{"status":"started","host":"127.0.0.1","port":18085}\n',
            )

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ReadError(
                    "channel 1: open failed: connect failed",
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "server_instance_id": SERVER_A,
                    "protocol_version": 1,
                    "server_version": "0.1.0",
                    "platform": "linux",
                    "features": [],
                    "provider_configured": True,
                },
            )

        connector = SshServerConnector(
            _settings(tmp_path),
            transport=httpx.MockTransport(upstream),
            popen_factory=popen,
            allocate_port=lambda: 19085,
        )

        connected = await connector.connect(_profile(), None)
        await connector.close()

        assert connected.server_instance_id == SERVER_A
        assert attempts == 2
        assert calls[2][5] == "127.0.0.1:19085:127.0.0.1:18085"

    asyncio.run(scenario())


def test_remote_start_invalid_json_is_typed_error(tmp_path: Path):
    async def scenario() -> None:
        def popen(argv, **_kwargs):
            if "--version" in argv:
                return FakeProcess(0, output="chattree-server 0.1.0\n")
            return FakeProcess(0, output="not json\n")

        connector = SshServerConnector(
            _settings(tmp_path),
            popen_factory=popen,
            allocate_port=lambda: 19084,
        )

        with pytest.raises(LauncherError) as exc_info:
            await connector.connect(_profile(), None)
        await connector.close()

        assert exc_info.value.code == "remote_start_invalid_json"
        assert exc_info.value.status_code == 502

    asyncio.run(scenario())


def test_remote_version_mismatch_stops_before_remote_start(tmp_path: Path):
    async def scenario() -> None:
        calls: list[list[str]] = []

        def popen(argv, **_kwargs):
            calls.append(list(argv))
            return FakeProcess(0, output="chattree-server 0.0.1\n")

        connector = SshServerConnector(
            _settings(tmp_path),
            popen_factory=popen,
            allocate_port=lambda: 19084,
        )

        with pytest.raises(LauncherError) as exc_info:
            await connector.connect(_profile(), None)
        await connector.close()

        assert exc_info.value.code == "remote_server_version_incompatible"
        assert exc_info.value.status_code == 409
        assert exc_info.value.details == {
            "required_server_version": "0.1.0",
            "observed_server_version": "0.0.1",
        }
        assert calls == [["ssh", "gpu-box", "chattree-server", "--version"]]

    asyncio.run(scenario())


def test_remote_handshake_version_mismatch_is_typed_error(tmp_path: Path):
    async def scenario() -> None:
        def popen(argv, **_kwargs):
            if "-N" in argv:
                return FakeProcess(None)
            if "--version" in argv:
                return FakeProcess(0, output="chattree-server 0.1.0\n")
            return FakeProcess(
                0,
                output='{"status":"started","host":"127.0.0.1","port":18086}\n',
            )

        async def upstream(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "server_instance_id": SERVER_A,
                    "protocol_version": 1,
                    "server_version": "0.0.1",
                    "platform": "linux",
                    "features": [],
                    "provider_configured": True,
                },
            )

        connector = SshServerConnector(
            _settings(tmp_path),
            transport=httpx.MockTransport(upstream),
            popen_factory=popen,
            allocate_port=lambda: 19086,
        )

        with pytest.raises(LauncherError) as exc_info:
            await connector.connect(_profile(), None)
        await connector.close()

        assert exc_info.value.code == "ssh_server_version_mismatch"
        assert exc_info.value.status_code == 409
        assert exc_info.value.details == {
            "minimum_server_version": "0.1.0",
            "observed_server_version": "0.0.1",
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("version_output", "exit_code", "expected_code"),
    [
        ("not a version\n", 0, "remote_server_version_invalid"),
        ("fatal\n", 17, "remote_server_version_failed"),
    ],
)
def test_remote_version_probe_failures_stop_before_remote_start(
    tmp_path: Path,
    version_output: str,
    exit_code: int,
    expected_code: str,
):
    async def scenario() -> None:
        calls: list[list[str]] = []

        def popen(argv, **_kwargs):
            calls.append(list(argv))
            return FakeProcess(exit_code, output=version_output)

        connector = SshServerConnector(
            _settings(tmp_path),
            popen_factory=popen,
            allocate_port=lambda: 19087,
        )

        with pytest.raises(LauncherError) as exc_info:
            await connector.connect(_profile(), None)
        await connector.close()

        assert exc_info.value.code == expected_code
        assert calls == [["ssh", "gpu-box", "chattree-server", "--version"]]

    asyncio.run(scenario())


def test_remote_version_timeout_is_typed_error(tmp_path: Path):
    async def scenario() -> None:
        def popen(argv, **_kwargs):
            if "--version" in argv:
                return FakeTimeoutProcess()
            raise AssertionError("timed out remote version probe must not start")

        connector = SshServerConnector(
            _settings(tmp_path),
            popen_factory=popen,
            allocate_port=lambda: 19088,
        )

        with pytest.raises(LauncherError) as exc_info:
            await connector.connect(_profile(), None)
        await connector.close()

        assert exc_info.value.code == "remote_server_version_timeout"
        assert exc_info.value.status_code == 504

    asyncio.run(scenario())


def test_invalid_profile_kind_is_rejected(tmp_path: Path):
    connector = SshServerConnector(_settings(tmp_path))

    class BadProfile:
        id = "local"
        kind = "local"
        ssh = None

    async def scenario() -> None:
        with pytest.raises(LauncherError) as exc_info:
            await connector.connect(BadProfile(), None)  # type: ignore[arg-type]
        await connector.close()
        assert exc_info.value.code == "invalid_ssh_profile"

    asyncio.run(scenario())
