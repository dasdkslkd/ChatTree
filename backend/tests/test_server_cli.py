from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.core.subprocess_utils import subprocess_window_kwargs

import backend.server_cli as server_cli


def test_version_uses_server_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        server_cli.main(["--version"])

    assert exc_info.value.code == 0
    assert f"chattree-server {server_cli.SERVER_VERSION}" in capsys.readouterr().out


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_serve_rejects_non_loopback_host(host):
    with pytest.raises(SystemExit) as exc_info:
        server_cli.main(["serve", "--host", host])

    assert exc_info.value.code == 2


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_serve_rejects_invalid_port(port):
    with pytest.raises(SystemExit) as exc_info:
        server_cli.main(["serve", "--port", port])

    assert exc_info.value.code == 2


@pytest.mark.parametrize("port", ["0", "auto"])
def test_start_accepts_auto_port(port, tmp_path, monkeypatch, capsys):
    captured = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            self.pid = 12346

    monkeypatch.setattr(server_cli, "_allocate_loopback_port", lambda host: 18019)
    monkeypatch.setattr(server_cli.subprocess, "Popen", FakePopen)

    result = server_cli.main(["start", "--home", str(tmp_path), "--port", port])

    assert result == 0
    assert captured["command"][captured["command"].index("--port") + 1] == "18019"
    payload = json.loads(capsys.readouterr().out)
    assert payload["port"] == 18019


def test_serve_sets_home_before_importing_main(tmp_path, monkeypatch):
    captured = {}

    def fake_load_main_module():
        captured["home_at_import"] = os.environ.get("CHATTREE_HOME")
        return SimpleNamespace(
            run_server=lambda *, host, port: captured.update(
                {"host": host, "port": port}
            )
        )

    monkeypatch.delenv("CHATTREE_HOME", raising=False)
    monkeypatch.setattr(server_cli, "_load_main_module", fake_load_main_module)

    result = server_cli.main(
        [
            "serve",
            "--home",
            str(tmp_path),
            "--host",
            "localhost",
            "--port",
            "18010",
        ]
    )

    assert result == 0
    assert captured == {
        "home_at_import": str(tmp_path.resolve()),
        "host": "localhost",
        "port": 18010,
    }


def test_start_spawns_detached_serve_with_log_and_json(tmp_path, monkeypatch, capsys):
    captured = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            self.pid = 12345

    monkeypatch.delenv("CHATTREE_HOME", raising=False)
    monkeypatch.setattr(server_cli.subprocess, "Popen", FakePopen)

    result = server_cli.main(
        ["start", "--home", str(tmp_path), "--port", "18011"]
    )

    assert result == 0
    expected_command = [
        sys.executable,
        "-m",
        "backend.server_cli",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "18011",
        "--home",
        str(tmp_path.resolve()),
    ]
    assert captured["command"] == expected_command
    kwargs = captured["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["shell"] is False
    assert kwargs["env"]["CHATTREE_HOME"] == str(tmp_path.resolve())
    assert kwargs["cwd"]
    expected_spawn = subprocess_window_kwargs(new_process_group=True)
    assert kwargs["creationflags"] == expected_spawn["creationflags"]
    if "startupinfo" in expected_spawn:
        assert kwargs["startupinfo"].dwFlags == expected_spawn["startupinfo"].dwFlags
        assert (
            kwargs["startupinfo"].wShowWindow
            == expected_spawn["startupinfo"].wShowWindow
        )
    if "start_new_session" in expected_spawn:
        assert kwargs["start_new_session"] is expected_spawn["start_new_session"]
    assert (tmp_path / "logs" / "server.log").exists()

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "started"
    assert payload["pid"] == 12345
    assert payload["command"] == expected_command
    assert payload["home"] == str(tmp_path.resolve())
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 18011
    assert payload["log_path"] == str(tmp_path / "logs" / "server.log")


def test_start_uses_frozen_executable_for_detached_serve(
    tmp_path,
    monkeypatch,
):
    captured = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            self.pid = 12345

    monkeypatch.setattr(server_cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(server_cli.sys, "executable", "chattree-server.exe")
    monkeypatch.setattr(server_cli.subprocess, "Popen", FakePopen)

    result = server_cli.main(["start", "--home", str(tmp_path), "--port", "18011"])

    assert result == 0
    assert captured["command"][:6] == [
        "chattree-server.exe",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "18011",
    ]
    assert captured["kwargs"]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_start_reuses_running_server_without_spawning(tmp_path, monkeypatch, capsys):
    def fail_popen(*args, **kwargs):
        raise AssertionError("start must not spawn when handshake is ready")

    monkeypatch.delenv("CHATTREE_HOME", raising=False)
    monkeypatch.setattr(
        server_cli,
        "_probe_handshake",
        lambda host, port: {
            "server_instance_id": "server-1",
            "protocol_version": 1,
        },
    )
    monkeypatch.setattr(server_cli.subprocess, "Popen", fail_popen)

    result = server_cli.main(
        ["start", "--home", str(tmp_path), "--port", "18012"]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "already_running"
    assert payload["pid"] is None
    assert payload["command"] is None
    assert payload["home"] == str(tmp_path.resolve())
    assert payload["port"] == 18012
    assert payload["server_instance_id"] == "server-1"


def test_start_auto_reuses_locked_home_owner_port(
    tmp_path,
    monkeypatch,
    capsys,
):
    def fail_popen(*args, **kwargs):
        raise AssertionError("start must not spawn when locked owner is ready")

    lock_path = tmp_path / ".server.lock"
    lock_path.write_bytes(
        b"\0"
        + json.dumps(
            {
                "pid": 12345,
                "host": "127.0.0.1",
                "port": 18020,
                "started_at": 1,
            }
        ).encode("utf-8")
    )

    monkeypatch.setattr(
        server_cli,
        "_locked_home_owner",
        lambda home: {
            "pid": 12345,
            "host": "127.0.0.1",
            "port": 18020,
            "started_at": 1,
        },
    )
    monkeypatch.setattr(
        server_cli,
        "_probe_handshake",
        lambda host, port: {
            "server_instance_id": "server-2",
            "protocol_version": 1,
        },
    )
    monkeypatch.setattr(server_cli.subprocess, "Popen", fail_popen)

    result = server_cli.main(["start", "--home", str(tmp_path), "--port", "auto"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "already_running"
    assert payload["pid"] == 12345
    assert payload["port"] == 18020
    assert payload["server_instance_id"] == "server-2"


def test_start_reuses_legacy_locked_home_with_requested_port(
    tmp_path,
    monkeypatch,
    capsys,
):
    def fail_popen(*args, **kwargs):
        raise AssertionError("start must not spawn when legacy owner is ready")

    monkeypatch.setattr(
        server_cli,
        "_locked_home_owner",
        lambda home: {
            "pid": 12345,
            "hostname": "dev-host",
            "started_at": 1,
        },
    )

    probes = []

    def fake_probe(host, port):
        probes.append((host, port))
        return {
            "server_instance_id": "server-legacy",
            "protocol_version": 1,
        }

    monkeypatch.setattr(server_cli, "_probe_handshake", fake_probe)
    monkeypatch.setattr(server_cli.subprocess, "Popen", fail_popen)

    result = server_cli.main(
        [
            "start",
            "--home",
            str(tmp_path),
            "--host",
            "127.0.0.1",
            "--port",
            "18021",
        ]
    )

    assert result == 0
    assert probes == [("127.0.0.1", 18021)]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "already_running"
    assert payload["pid"] == 12345
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 18021
    assert payload["server_instance_id"] == "server-legacy"


def test_start_auto_rejects_legacy_locked_home_without_owner_port(
    tmp_path,
    monkeypatch,
    capsys,
):
    def fail_popen(*args, **kwargs):
        raise AssertionError("start must not spawn when home is locked")

    monkeypatch.setattr(
        server_cli,
        "_locked_home_owner",
        lambda home: {
            "pid": 12345,
            "hostname": "dev-host",
            "started_at": 1,
        },
    )
    monkeypatch.setattr(server_cli, "_probe_handshake", lambda host, port: None)
    monkeypatch.setattr(server_cli.subprocess, "Popen", fail_popen)

    result = server_cli.main(["start", "--home", str(tmp_path), "--port", "auto"])

    assert result == 1
    assert "already in use" in capsys.readouterr().err


def test_start_returns_nonzero_when_spawn_fails(tmp_path, monkeypatch, capsys):
    def fail_popen(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(server_cli, "_probe_handshake", lambda host, port: None)
    monkeypatch.setattr(server_cli.subprocess, "Popen", fail_popen)

    result = server_cli.main(["start", "--home", str(tmp_path)])

    assert result == 1
    assert "spawn failed" in capsys.readouterr().err
