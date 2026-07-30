from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import main
from backend.core.server import ServerHomeInUseError, ServerHomeLock


def test_import_does_not_touch_locked_home(tmp_path: Path):
    config_path = tmp_path / "config.json"
    legacy_config = {
        "provider": {
            "openai": {
                "api_key": "sentinel",
            }
        }
    }
    config_path.write_text(
        json.dumps(legacy_config, ensure_ascii=False),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["CHATTREE_HOME"] = str(tmp_path)

    with ServerHomeLock(tmp_path):
        completed = subprocess.run(
            [sys.executable, "-c", "import main"],
            cwd=main.PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(config_path.read_text(encoding="utf-8")) == legacy_config
    assert not (tmp_path / "model_metadata.toml").exists()


def test_startup_failure_releases_home_lock(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))

    def fail_config():
        raise RuntimeError("startup failed")

    monkeypatch.setattr(main, "Config", fail_config)

    with pytest.raises(RuntimeError, match="startup failed"):
        asyncio.run(main.startup_event())

    assert getattr(main.app.state, "server_home_lock", None) is None
    with ServerHomeLock(tmp_path):
        pass


def test_shutdown_failure_retains_home_lock_for_cleanup_retry(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    home_lock = ServerHomeLock(tmp_path)
    home_lock.acquire()

    close_order = []

    class FailingRunManager:
        async def close(self):
            close_order.append("run")
            raise RuntimeError("shutdown failed")

    class RecordingToolManager:
        async def close(self):
            close_order.append("tool")
            with pytest.raises(ServerHomeInUseError):
                with ServerHomeLock(tmp_path):
                    pass

    run_manager = FailingRunManager()
    tool_manager = RecordingToolManager()
    monkeypatch.setattr(
        main.app.state,
        "server_home_lock",
        home_lock,
        raising=False,
    )
    monkeypatch.setattr(
        main.app.state,
        "run_manager",
        run_manager,
        raising=False,
    )
    monkeypatch.setattr(
        main.app.state,
        "tool_manager",
        tool_manager,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="shutdown failed"):
        asyncio.run(main.shutdown_event())

    assert close_order == ["run"]
    assert main.app.state.run_manager is run_manager
    assert main.app.state.tool_manager is tool_manager
    assert main.app.state.server_home_lock is home_lock
    try:
        with pytest.raises(ServerHomeInUseError):
            with ServerHomeLock(tmp_path):
                pass
    finally:
        home_lock.release()
        main.app.state.server_home_lock = None


def test_startup_failure_closes_initialized_resources_before_unlock(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    close_order = []

    class RecordingRunManager:
        async def close(self):
            close_order.append("run")
            with pytest.raises(ServerHomeInUseError):
                with ServerHomeLock(tmp_path):
                    pass

    class RecordingToolManager:
        async def close(self):
            close_order.append("tool")
            with pytest.raises(ServerHomeInUseError):
                with ServerHomeLock(tmp_path):
                    pass

    async def fail_after_resources_initialized():
        main.app.state.run_manager = RecordingRunManager()
        main.app.state.tool_manager = RecordingToolManager()
        raise RuntimeError("startup failed after resource initialization")

    monkeypatch.setattr(main, "_initialize_server", fail_after_resources_initialized)

    with pytest.raises(RuntimeError, match="startup failed after resource"):
        asyncio.run(main.startup_event())

    assert close_order == ["run", "tool"]
    assert main.app.state.server_home_lock is None
    with ServerHomeLock(tmp_path):
        pass


def test_real_uvicorn_process_stays_alive_when_shutdown_drain_is_incomplete(
    tmp_path: Path,
):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = os.environ.copy()
    environment["CHATTREE_HOME"] = str(tmp_path)
    environment["CHATTREE_SERVER_PORT"] = str(port)
    script = """
import asyncio
import main

class IncompleteCoordinator:
    async def close(self):
        return ("still-live",)

async def initialize():
    main.app.state.run_start_coordinator = IncompleteCoordinator()
    main.app.state.producer_registry = None
    main.app.state.command_executor = None
    main.app.state.run_manager = None
    main.app.state.tool_manager = None
    asyncio.get_running_loop().call_later(
        0.2,
        main.app.state.request_shutdown,
    )

main._initialize_server = initialize
main.run_server()
"""
    creationflags = (
        subprocess.CREATE_NO_WINDOW
        if os.name == "nt"
        else 0
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=main.PROJECT_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with ServerHomeLock(tmp_path):
                    pass
            except ServerHomeInUseError:
                break
            if process.poll() is not None:
                pytest.fail("Uvicorn process exited before acquiring Home lock")
            if time.monotonic() >= deadline:
                pytest.fail("Uvicorn process did not acquire Home lock")
            time.sleep(0.05)

        time.sleep(1)
        assert process.poll() is None
        with pytest.raises(ServerHomeInUseError):
            with ServerHomeLock(tmp_path):
                pass
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    with ServerHomeLock(tmp_path):
        pass
