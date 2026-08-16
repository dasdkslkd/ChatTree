from __future__ import annotations

import asyncio
import json
from pathlib import Path

import client_launcher.__main__ as launcher_main
from client_launcher.settings import LauncherSettings


def _make_settings(tmp_path: Path, *, port: int = 0) -> LauncherSettings:
    return LauncherSettings(
        client_home=tmp_path / "client",
        project_root=tmp_path,
        server_python="python",
        port=port,
    )


def _sticky_file(tmp_path: Path) -> Path:
    return tmp_path / "client" / launcher_main.STICKY_PORT_FILENAME


def _run_main(
    monkeypatch,
    *,
    settings: LauncherSettings,
    listener_port: int,
    bind_errors: int = 0,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    class FakeListener:
        def getsockname(self):
            return ("127.0.0.1", listener_port)

        def close(self):
            captured["listener_closed"] = True

    listener = FakeListener()

    monkeypatch.setattr(
        launcher_main.LauncherSettings,
        "from_env",
        staticmethod(lambda: settings),
    )

    def fake_create_app(*, settings):
        captured["app_settings"] = settings
        return object()

    monkeypatch.setattr(launcher_main, "create_app", fake_create_app)

    class FakeConfig:
        def __init__(self, app_argument, **kwargs):
            self.app_factory = app_argument
            captured["kwargs"] = kwargs
            captured.setdefault("bound_ports", [])

        def bind_socket(self):
            captured["bound_ports"].append(captured["kwargs"]["port"])
            if len(captured["bound_ports"]) <= bind_errors:
                raise OSError("port in use")
            return listener

    class FakeServer:
        def __init__(self, config):
            self.config = config

        def run(self, *, sockets):
            self.config.app_factory()
            captured["sockets"] = sockets
            raise KeyboardInterrupt

    monkeypatch.setattr(launcher_main.uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(launcher_main, "LauncherServer", FakeServer)

    launcher_main.main()
    return captured


def test_main_binds_socket_before_creating_launcher_app(
    monkeypatch,
    tmp_path: Path,
):
    settings = _make_settings(tmp_path)
    captured = _run_main(monkeypatch, settings=settings, listener_port=43125)

    assert captured["app_settings"].port == 43125
    assert captured["kwargs"] == {
        "factory": True,
        "host": "127.0.0.1",
        "port": 0,
        "workers": 1,
        "lifespan": "on",
        "date_header": False,
        "server_header": False,
    }
    assert [sock.getsockname()[1] for sock in captured["sockets"]] == [43125]
    assert captured["sockets"]
    assert captured["listener_closed"] is True
    assert _sticky_file(tmp_path).read_text(encoding="utf-8").strip() == "43125"


def test_main_reuses_sticky_port(monkeypatch, tmp_path: Path):
    settings = _make_settings(tmp_path)
    settings.client_home.mkdir(parents=True)
    _sticky_file(tmp_path).write_text("43126\n", encoding="utf-8", newline="\n")

    captured = _run_main(monkeypatch, settings=settings, listener_port=43126)

    assert captured["bound_ports"] == [43126]
    assert captured["kwargs"]["port"] == 43126
    assert captured["app_settings"].port == 43126
    assert _sticky_file(tmp_path).read_text(encoding="utf-8").strip() == "43126"


def test_main_falls_back_to_random_port_when_sticky_port_is_taken(
    monkeypatch,
    tmp_path: Path,
):
    settings = _make_settings(tmp_path)
    settings.client_home.mkdir(parents=True)
    _sticky_file(tmp_path).write_text("43126\n", encoding="utf-8", newline="\n")

    captured = _run_main(
        monkeypatch,
        settings=settings,
        listener_port=43127,
        bind_errors=1,
    )

    assert captured["bound_ports"] == [43126, 0]
    assert captured["app_settings"].port == 43127
    assert _sticky_file(tmp_path).read_text(encoding="utf-8").strip() == "43127"


def test_main_does_not_write_sticky_file_for_explicit_port(monkeypatch, tmp_path: Path):
    settings = _make_settings(tmp_path, port=9000)

    captured = _run_main(monkeypatch, settings=settings, listener_port=9000)

    assert captured["bound_ports"] == [9000]
    assert captured["app_settings"].port == 9000
    assert not _sticky_file(tmp_path).exists()


def test_main_ignores_invalid_sticky_port(monkeypatch, tmp_path: Path):
    settings = _make_settings(tmp_path)
    settings.client_home.mkdir(parents=True)
    _sticky_file(tmp_path).write_text("not-a-port\n", encoding="utf-8", newline="\n")

    captured = _run_main(monkeypatch, settings=settings, listener_port=43125)

    assert captured["bound_ports"] == [0]
    assert captured["app_settings"].port == 43125
    assert _sticky_file(tmp_path).read_text(encoding="utf-8").strip() == "43125"


def test_read_sticky_port_returns_none_for_missing_or_invalid(tmp_path: Path):
    client_home = tmp_path / "client"
    assert launcher_main._read_sticky_port(client_home) is None

    client_home.mkdir(parents=True)
    sticky = client_home / launcher_main.STICKY_PORT_FILENAME
    for raw in ("   \n", "65536", "0", "-1", "abc"):
        sticky.write_text(raw, encoding="utf-8", newline="\n")
        assert launcher_main._read_sticky_port(client_home) is None

    sticky.write_text("43125\n", encoding="utf-8", newline="\n")
    assert launcher_main._read_sticky_port(client_home) == 43125


def test_launcher_reports_bound_endpoint_after_startup(monkeypatch, capsys):
    class FakeSocket:
        def getsockname(self):
            return ("127.0.0.1", 43125)

    async def fake_startup(self, sockets=None):
        assert sockets and len(sockets) == 1

    monkeypatch.setattr(launcher_main.uvicorn.Server, "startup", fake_startup)
    server = object.__new__(launcher_main.LauncherServer)
    server.should_exit = False

    asyncio.run(server.startup(sockets=[FakeSocket()]))

    line = capsys.readouterr().out.strip()
    assert line.startswith(launcher_main.LAUNCHER_READY_PREFIX)
    assert json.loads(line.removeprefix(launcher_main.LAUNCHER_READY_PREFIX)) == {
        "host": "127.0.0.1",
        "port": 43125,
    }
