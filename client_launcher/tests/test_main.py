from __future__ import annotations

from types import SimpleNamespace

import client_launcher.__main__ as launcher_main


def test_main_disables_launcher_date_and_server_headers(monkeypatch):
    settings = SimpleNamespace(host="127.0.0.1", port=18000)
    app = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        launcher_main.LauncherSettings,
        "from_env",
        staticmethod(lambda: settings),
    )
    monkeypatch.setattr(launcher_main, "create_app", lambda *, settings: app)

    def fake_run(app_argument, **kwargs):
        captured["app"] = app_argument
        captured["kwargs"] = kwargs

    monkeypatch.setattr(launcher_main.uvicorn, "run", fake_run)

    launcher_main.main()

    assert captured == {
        "app": app,
        "kwargs": {
            "host": "127.0.0.1",
            "port": 18000,
            "workers": 1,
            "lifespan": "on",
            "date_header": False,
            "server_header": False,
        },
    }
