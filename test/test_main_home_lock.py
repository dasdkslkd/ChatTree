from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
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


def test_shutdown_failure_still_releases_home_lock(monkeypatch, tmp_path: Path):
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

    monkeypatch.setattr(
        main.app.state,
        "server_home_lock",
        home_lock,
        raising=False,
    )
    monkeypatch.setattr(
        main.app.state,
        "run_manager",
        FailingRunManager(),
        raising=False,
    )
    monkeypatch.setattr(
        main.app.state,
        "tool_manager",
        RecordingToolManager(),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="shutdown failed"):
        asyncio.run(main.shutdown_event())

    assert close_order == ["run", "tool"]
    assert main.app.state.server_home_lock is None
    with ServerHomeLock(tmp_path):
        pass


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
