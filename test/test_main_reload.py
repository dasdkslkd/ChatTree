from pathlib import Path

import pytest

import main
from main import (
    PROJECT_ROOT,
    run_server,
    uvicorn_reload_options,
    uvicorn_server_options,
)


def test_uvicorn_reload_excludes_runtime_tool_workspace():
    options = uvicorn_reload_options()

    reload_dirs = [Path(path) for path in options["reload_dirs"]]
    assert reload_dirs == [PROJECT_ROOT / "backend"]
    assert options["reload_includes"] == ["*.py"]

    assert not (PROJECT_ROOT / "tmp").is_relative_to(reload_dirs[0])
    assert not (PROJECT_ROOT / "data").is_relative_to(reload_dirs[0])
    assert not (PROJECT_ROOT / "frontend").is_relative_to(reload_dirs[0])
    assert "**/__pycache__/**" in set(options["reload_excludes"])


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


def test_uvicorn_server_reads_real_environment(monkeypatch):
    monkeypatch.setenv("CHATTREE_SERVER_PORT", "18002")
    monkeypatch.setenv("CHATTREE_SERVER_HOST", "0.0.0.0")

    assert uvicorn_server_options() == {"host": "127.0.0.1", "port": 18002}


def test_run_server_wires_loopback_port_and_reload_options(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        main.uvicorn,
        "run",
        lambda app_ref, **kwargs: captured.update(app_ref=app_ref, **kwargs),
    )

    run_server({"CHATTREE_SERVER_PORT": "18003"})

    assert captured["app_ref"] == "main:app"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 18003
    assert captured["reload"] is True
    assert captured["workers"] == 1
    assert captured["reload_dirs"] == [str(main.PROJECT_ROOT / "backend")]
