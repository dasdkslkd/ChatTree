from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _start_launcher(
    env: dict[str, str],
    log_path: Path,
) -> subprocess.Popen[bytes]:
    with log_path.open("ab") as output:
        return subprocess.Popen(
            [sys.executable, "-m", "client_launcher"],
            cwd=PROJECT_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
        )


def _stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _wait_for_json(
    url: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    process: subprocess.Popen[Any] | None,
    log_path: Path,
    timeout: float = 30.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    with httpx.Client(trust_env=False, timeout=0.5) as client:
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                raise AssertionError(
                    f"Launcher exited with {process.returncode}:\n{_read_log(log_path)}"
                )
            try:
                response = client.get(url)
                if response.status_code == 200:
                    payload = response.json()
                    if predicate(payload):
                        return payload
                    if payload.get("status") == "error":
                        raise AssertionError(
                            f"Launcher connection failed: {payload}\n{_read_log(log_path)}"
                        )
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = exc
            time.sleep(0.05)
    raise AssertionError(
        f"Timed out waiting for {url}: {last_error}\n{_read_log(log_path)}"
    )


def _raw_headers(port: int, path: str) -> tuple[int, list[tuple[str, str]]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response.status, response.getheaders()
    finally:
        connection.close()


def _server_owner_pid(lock_path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with lock_path.open("rb") as handle:
                handle.seek(1)
                owner = json.loads(handle.read().decode("utf-8"))
            pid = owner.get("pid")
            if isinstance(pid, int) and pid > 0:
                return pid
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.05)
    raise AssertionError(f"Could not read Server owner pid: {last_error}")


def _port_is_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _pid_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _stop_server(pid: int | None, port: int) -> None:
    if pid is None or not _pid_is_running(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _port_is_closed(port) and not _pid_is_running(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _port_is_closed(port) and not _pid_is_running(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"Server pid {pid} did not exit and release port {port}")


def test_real_launcher_entry_proxy_and_home_lock(tmp_path: Path) -> None:
    client_home = tmp_path / "client"
    server_home = tmp_path / "server"
    ports: set[int] = set()
    while len(ports) < 3:
        ports.add(_free_port())
    launcher_port, server_port, second_server_port = sorted(ports)
    launcher_log = tmp_path / "launcher.log"
    second_server_log = tmp_path / "second-server.log"
    env = os.environ.copy()
    env.update(
        {
            "CHATTREE_CLIENT_HOME": str(client_home),
            "CHATTREE_HOME": str(server_home),
            "CHATTREE_CLIENT_PORT": str(launcher_port),
            "CHATTREE_LOCAL_SERVER_PORT": str(server_port),
            "CHATTREE_SERVER_PYTHON": sys.executable,
            "CHATTREE_CLIENT_CONNECT_TIMEOUT_SECONDS": "0.5",
            "CHATTREE_CLIENT_START_TIMEOUT_SECONDS": "30",
            "CHATTREE_CLIENT_POLL_INTERVAL_SECONDS": "0.05",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
    )
    status_url = (
        f"http://127.0.0.1:{launcher_port}/client/v1/profiles/local/status"
    )
    launcher: subprocess.Popen[bytes] | None = None
    second_server: subprocess.Popen[bytes] | None = None
    server_pid: int | None = None

    try:
        launcher = _start_launcher(env, launcher_log)
        ready = _wait_for_json(
            status_url,
            lambda payload: payload.get("status") == "ready",
            process=launcher,
            log_path=launcher_log,
        )
        instance_id = str(ready["server_instance_id"])
        server_pid = _server_owner_pid(server_home / ".server.lock")

        direct_status, direct_headers = _raw_headers(
            server_port,
            "/api/v1/health",
        )
        proxy_status, proxy_headers = _raw_headers(
            launcher_port,
            "/p/local/api/v1/health",
        )
        assert direct_status == proxy_status == 200
        for name in ("date", "server"):
            assert len(
                [value for key, value in direct_headers if key.lower() == name]
            ) == 1
            assert len(
                [value for key, value in proxy_headers if key.lower() == name]
            ) == 1

        base_url = f"http://127.0.0.1:{launcher_port}"
        with httpx.Client(
            base_url=base_url,
            trust_env=False,
            timeout=10,
            follow_redirects=False,
        ) as client:
            redirect = client.get("/p/local/api/v1/conversations/")
            assert redirect.status_code == 307
            assert redirect.headers["location"] == (
                "/p/local/api/v1/conversations"
            )

            conversation = client.post(
                "/p/local/api/v1/conversations",
                json={"title": "Launcher E2E"},
            )
            conversation.raise_for_status()
            conversation_data = conversation.json()
            statuses: list[str] = []
            content_parts: list[str] = []
            done = False
            with client.stream(
                "POST",
                "/p/local/api/v1/conversations/"
                f"{conversation_data['id']}/messages/stream",
                json={
                    "content": "/help",
                    "parent_node_id": conversation_data["current_node_id"],
                },
            ) as stream:
                stream.raise_for_status()
                assert stream.headers["content-type"].startswith(
                    "text/event-stream"
                )
                for line in stream.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    value = line[5:].strip()
                    if value == "[DONE]":
                        done = True
                        break
                    event = json.loads(value)
                    statuses.append(str(event.get("status")))
                    if event.get("content"):
                        content_parts.append(str(event["content"]))
            assert done is True
            assert {"start", "content", "complete"}.issubset(statuses)
            assert "".join(content_parts).strip()

        second_env = env.copy()
        second_env["CHATTREE_SERVER_PORT"] = str(second_server_port)
        with second_server_log.open("wb") as output:
            second_server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(second_server_port),
                    "--workers",
                    "1",
                    "--lifespan",
                    "on",
                    "--app-dir",
                    str(PROJECT_ROOT),
                ],
                cwd=PROJECT_ROOT,
                env=second_env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                shell=False,
                close_fds=True,
            )
            assert second_server.wait(timeout=15) != 0
        assert "CHATTREE_HOME" in _read_log(second_server_log)
        health = httpx.get(
            f"http://127.0.0.1:{server_port}/api/v1/health",
            trust_env=False,
            timeout=2,
        )
        assert health.json()["server_instance_id"] == instance_id

        _stop_process(launcher)
        launcher = None
        direct_health = httpx.get(
            f"http://127.0.0.1:{server_port}/api/v1/health",
            trust_env=False,
            timeout=2,
        )
        assert direct_health.json()["server_instance_id"] == instance_id

        launcher = _start_launcher(env, launcher_log)
        restarted = _wait_for_json(
            status_url,
            lambda payload: payload.get("status") == "ready",
            process=launcher,
            log_path=launcher_log,
        )
        assert restarted["server_instance_id"] == instance_id
        assert _server_owner_pid(server_home / ".server.lock") == server_pid

        with closing(
            sqlite3.connect(server_home / "chattree.sqlite")
        ) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        _stop_process(second_server)
        _stop_process(launcher)
        if server_pid is None and (server_home / ".server.lock").exists():
            try:
                server_pid = _server_owner_pid(
                    server_home / ".server.lock",
                    timeout=1,
                )
            except AssertionError:
                pass
        _stop_server(server_pid, server_port)

    assert _port_is_closed(launcher_port)
    assert _port_is_closed(server_port)
    assert _port_is_closed(second_server_port)
