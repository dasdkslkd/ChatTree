import pytest

import main
from main import run_server, uvicorn_server_options


def test_uvicorn_server_defaults_to_loopback():
    assert uvicorn_server_options({}) == {"host": "127.0.0.1", "port": 8001}


def test_uvicorn_server_accepts_port_override_only():
    assert uvicorn_server_options(
        {
            "CHATTREE_SERVER_PORT": "18001",
            "CHATTREE_SERVER_HOST": "0.0.0.0",
        }
    ) == {"host": "127.0.0.1", "port": 18001}


@pytest.mark.parametrize("value", ["", "abc", "0", "65536"])
def test_uvicorn_server_rejects_invalid_port(value):
    with pytest.raises(ValueError, match="CHATTREE_SERVER_PORT"):
        uvicorn_server_options({"CHATTREE_SERVER_PORT": value})


@pytest.mark.parametrize("value", ["1", "65535"])
def test_uvicorn_server_accepts_port_boundaries(value):
    assert uvicorn_server_options({"CHATTREE_SERVER_PORT": value})["port"] == int(
        value
    )


def test_uvicorn_server_accepts_explicit_loopback_host_and_port():
    assert uvicorn_server_options(
        {"CHATTREE_SERVER_PORT": "18001"},
        host="localhost",
        port=18006,
    ) == {"host": "localhost", "port": 18006}


def test_uvicorn_server_rejects_invalid_explicit_port():
    with pytest.raises(ValueError, match="CHATTREE_SERVER_PORT"):
        uvicorn_server_options({}, port=70000)


def test_uvicorn_server_reads_real_environment(monkeypatch):
    monkeypatch.setenv("CHATTREE_SERVER_PORT", "18002")
    monkeypatch.setenv("CHATTREE_SERVER_HOST", "0.0.0.0")

    assert uvicorn_server_options() == {"host": "127.0.0.1", "port": 18002}


def test_run_server_owns_uvicorn_server_and_cooperative_shutdown_hook(monkeypatch):
    captured = {}

    class FakeConfig:
        def __init__(self, app, **kwargs):
            captured["app"] = app
            captured["options"] = kwargs

    class FakeServer:
        def __init__(self, config):
            captured["config"] = config
            self.should_exit = False

        def run(self):
            hook = main.app.state.request_shutdown
            captured["hook_is_callable"] = callable(hook)
            hook()
            captured["should_exit"] = self.should_exit

    monkeypatch.setattr(main.uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(main.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(main.app.state, "server_home_lock", None, raising=False)

    run_server({"CHATTREE_SERVER_PORT": "18003"})

    assert captured["app"] is main.app
    assert captured["options"] == {
        "host": "127.0.0.1",
        "port": 18003,
        "workers": 1,
    }
    assert captured["hook_is_callable"] is True
    assert captured["should_exit"] is True
    assert main.app.state.request_shutdown is None


def test_run_server_uses_explicit_host_and_port(monkeypatch):
    captured = {}

    class FakeConfig:
        def __init__(self, app, **kwargs):
            captured["app"] = app
            captured["options"] = kwargs

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            pass

    monkeypatch.setattr(main.uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(main.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(main.app.state, "server_home_lock", None, raising=False)

    run_server({"CHATTREE_SERVER_PORT": "18003"}, host="localhost", port=18007)

    assert captured["app"] is main.app
    assert captured["options"] == {
        "host": "localhost",
        "port": 18007,
        "workers": 1,
    }
    assert main.app.state.request_shutdown is None


def test_run_server_does_not_exit_while_home_lock_is_retained(monkeypatch):
    retained_lock = object()
    held = []

    class FakeConfig:
        def __init__(self, app, **kwargs):
            pass

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            main.app.state.server_home_lock = retained_lock

    monkeypatch.setattr(main.uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(main.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(
        main,
        "_hold_process_for_retained_home_lock",
        lambda: held.append(True),
    )

    run_server({"CHATTREE_SERVER_PORT": "18004"})

    assert held == [True]
    assert main.app.state.server_home_lock is retained_lock
    main.app.state.server_home_lock = None


def test_run_server_guards_retained_home_lock_when_uvicorn_raises(monkeypatch):
    retained_lock = object()
    held = []

    class FakeConfig:
        def __init__(self, app, **kwargs):
            pass

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            main.app.state.server_home_lock = retained_lock
            raise OSError("uvicorn failed")

    monkeypatch.setattr(main.uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(main.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(
        main,
        "_hold_process_for_retained_home_lock",
        lambda: held.append(True),
    )

    with pytest.raises(OSError, match="uvicorn failed"):
        run_server({"CHATTREE_SERVER_PORT": "18005"})

    assert held == [True]
    assert main.app.state.request_shutdown is None
    assert main.app.state.server_home_lock is retained_lock
    main.app.state.server_home_lock = None
