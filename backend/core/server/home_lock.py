from __future__ import annotations

import errno
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import BinaryIO

from backend.core.home import resolve_chattree_home


SERVER_HOME_LOCK_FILENAME = ".server.lock"

_ACTIVE_LOCKS: set[str] = set()
_ACTIVE_LOCKS_GUARD = threading.Lock()


class ServerHomeLockError(RuntimeError):
    pass


class ServerHomeInUseError(ServerHomeLockError):
    pass


class ServerHomeLock:
    """Process-lifetime exclusive lock for one ChatTree Server home."""

    def __init__(self, home: str | os.PathLike[str] | None = None) -> None:
        self.home = resolve_chattree_home(home)
        self.lock_path = self.home / SERVER_HOME_LOCK_FILENAME
        self._registry_key = os.path.normcase(str(self.lock_path.resolve()))
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self._handle is not None:
            raise ServerHomeLockError(
                f"server home lock is already acquired: {self.lock_path}"
            )

        self.home.mkdir(parents=True, exist_ok=True)
        handle = self._open_lock_file()
        owner: dict[str, object] | None = None

        with _ACTIVE_LOCKS_GUARD:
            if self._registry_key in _ACTIVE_LOCKS:
                owner = self._read_owner(handle)
                handle.close()
                raise self._in_use_error(owner)

            try:
                self._lock_file(handle)
            except OSError as exc:
                if not self._is_lock_conflict(exc):
                    handle.close()
                    raise ServerHomeLockError(
                        f"failed to lock ChatTree home {self.home}: {exc}"
                    ) from exc
                owner = self._read_owner(handle)
                handle.close()
                raise self._in_use_error(owner) from exc

            _ACTIVE_LOCKS.add(self._registry_key)
            self._handle = handle

        try:
            self._write_owner(handle)
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None

        try:
            self._unlock_file(handle)
        finally:
            try:
                handle.close()
            finally:
                with _ACTIVE_LOCKS_GUARD:
                    _ACTIVE_LOCKS.discard(self._registry_key)

    def __enter__(self) -> ServerHomeLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

    def _open_lock_file(self) -> BinaryIO:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            os.set_inheritable(fd, False)
            handle = os.fdopen(fd, "r+b", buffering=0)
        except BaseException:
            os.close(fd)
            raise

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        return handle

    @staticmethod
    def _lock_file(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_file(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _is_lock_conflict(exc: OSError) -> bool:
        return isinstance(exc, BlockingIOError) or exc.errno in {
            errno.EACCES,
            errno.EAGAIN,
            errno.EDEADLK,
        }

    @staticmethod
    def _read_owner(handle: BinaryIO) -> dict[str, object] | None:
        try:
            handle.seek(1)
            payload = handle.read(8192)
            decoded = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None

    @staticmethod
    def _write_owner(handle: BinaryIO) -> None:
        owner = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": int(time.time()),
        }
        host = os.environ.get("CHATTREE_SERVER_HOST")
        port = os.environ.get("CHATTREE_SERVER_PORT")
        if host:
            owner["host"] = host
        if port:
            try:
                owner["port"] = int(port)
            except ValueError:
                owner["port"] = port
        payload = json.dumps(owner, ensure_ascii=False, sort_keys=True).encode("utf-8")
        handle.seek(1)
        handle.truncate()
        handle.write(payload)
        handle.flush()

    def _in_use_error(
        self,
        owner: dict[str, object] | None,
    ) -> ServerHomeInUseError:
        owner_text = "unavailable"
        if owner:
            fields = [
                f"{name}={owner[name]}"
                for name in ("pid", "hostname", "started_at")
                if name in owner
            ]
            if fields:
                owner_text = ", ".join(fields)
        return ServerHomeInUseError(
            "ChatTree Server cannot start because this CHATTREE_HOME is already "
            "used by another running ChatTree Server. "
            f"CHATTREE_HOME={self.home}; lock_file={self.lock_path}; "
            f"owner({owner_text}). Stop the existing Server or choose a different "
            "CHATTREE_HOME."
        )
