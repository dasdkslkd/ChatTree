from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from backend.core.server import (
    SERVER_HOME_LOCK_FILENAME,
    ServerHomeInUseError,
    ServerHomeLock,
)


def _hold_home_lock(home: str, ready) -> None:
    with ServerHomeLock(home):
        ready.set()
        time.sleep(30)


def test_same_home_is_exclusive_within_one_process(tmp_path: Path):
    home = tmp_path / "home"

    with ServerHomeLock(home):
        with pytest.raises(ServerHomeInUseError) as exc_info:
            ServerHomeLock(home).acquire()

    message = str(exc_info.value)
    assert str(home.resolve()) in message
    assert SERVER_HOME_LOCK_FILENAME in message
    assert str(os.getpid()) in message


def test_different_homes_can_be_locked_at_the_same_time(tmp_path: Path):
    with ServerHomeLock(tmp_path / "first"):
        with ServerHomeLock(tmp_path / "second"):
            pass


def test_release_and_stale_lock_file_allow_reacquire(tmp_path: Path):
    home = tmp_path / "home"
    lock = ServerHomeLock(home)

    lock.acquire()
    lock.release()
    lock.release()

    lock_path = home / SERVER_HOME_LOCK_FILENAME
    assert lock_path.is_file()

    with ServerHomeLock(home):
        pass


def test_context_manager_releases_after_exception(tmp_path: Path):
    home = tmp_path / "home"

    with pytest.raises(RuntimeError, match="boom"):
        with ServerHomeLock(home):
            raise RuntimeError("boom")

    with ServerHomeLock(home):
        pass


def test_process_crash_releases_os_lock(tmp_path: Path):
    home = tmp_path / "home"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_hold_home_lock, args=(str(home), ready))
    process.start()

    try:
        assert ready.wait(10), f"lock holder failed with exit code {process.exitcode}"
        with pytest.raises(ServerHomeInUseError):
            ServerHomeLock(home).acquire()

        process.kill()
        process.join(10)
        assert not process.is_alive()

        with ServerHomeLock(home):
            pass
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
