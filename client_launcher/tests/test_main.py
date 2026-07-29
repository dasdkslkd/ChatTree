from __future__ import annotations

import asyncio
import json
from pathlib import Path

import client_launcher.__main__ as launcher_main
from client_launcher.settings import LauncherSettings


def test_main_binds_socket_before_creating_launcher_app(
    monkeypatch,
    tmp_path: Path,
):
    settings = LauncherSettings(
        client_home=tmp_path / "client",
        project_root=tmp_path,
        server_python="python",
        port=0,
    )
    captured: dict[str, object] = {}

    class FakeListener:
        def getsockname(self):
            return ("127.0.0.1", 43125)

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

        def bind_socket(self):
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
    assert captured["sockets"] == [listener]
    assert captured["listener_closed"] is True


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
