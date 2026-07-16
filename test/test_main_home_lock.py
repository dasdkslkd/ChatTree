from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import main
from backend.core.server import ServerHomeLock


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

    class FailingRunManager:
        async def close(self):
            raise RuntimeError("shutdown failed")

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
    monkeypatch.setattr(main.app.state, "tool_manager", None, raising=False)

    with pytest.raises(RuntimeError, match="shutdown failed"):
        asyncio.run(main.shutdown_event())

    assert main.app.state.server_home_lock is None
    with ServerHomeLock(tmp_path):
        pass
