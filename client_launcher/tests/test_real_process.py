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

from backend.core.server import ServerHomeLock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEASE_HEADER = "X-ChatTree-Connection-Lease-ID"


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


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 15.0,
    description: str,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {description}")


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


def _cleanup_server(pid: int, port: int) -> None:
    if not _pid_is_running(pid):
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


def _send_without_reading_response(
    port: int,
    path: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest("POST", path)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders(body)
    finally:
        connection.close()


def _wait_for_conversation_run(
    client: httpx.Client,
    conversation_id: str,
    headers: dict[str, str],
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(
            f"/p/local/api/v1/conversations/{conversation_id}/runs",
            headers=headers,
        )
        response.raise_for_status()
        runs = response.json()
        if runs:
            return runs[0]
        time.sleep(0.05)
    raise AssertionError("Response-lost start did not create a durable run")


def test_real_process_protocol_idempotency_restart_and_stop(tmp_path: Path) -> None:
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
    server_pids: set[int] = set()
    instance_id: str | None = None

    try:
        launcher = _start_launcher(env, launcher_log)
        ready = _wait_for_json(
            status_url,
            lambda payload: payload.get("status") == "ready",
            process=launcher,
            log_path=launcher_log,
        )
        instance_id = str(ready["server_instance_id"])
        lease_1 = str(ready["connection_lease_id"])
        lease_headers_1 = {LEASE_HEADER: lease_1}
        server_pid_1 = _server_owner_pid(server_home / ".server.lock")
        server_pids.add(server_pid_1)
        assert not list((client_home / "logs").glob("*.spawn.pid"))

        base_url = f"http://127.0.0.1:{launcher_port}"
        with httpx.Client(
            base_url=base_url,
            trust_env=False,
            timeout=30,
            follow_redirects=False,
        ) as client:
            handshake = client.get(
                "/p/local/api/v1/handshake",
                headers=lease_headers_1,
            )
            handshake.raise_for_status()
            assert handshake.json()["server_instance_id"] == instance_id
            assert {
                "error_envelope_v1",
                "idempotent_run_start_v1",
                "cooperative_shutdown_v1",
            }.issubset(handshake.json()["features"])

            missing_lease = client.get("/p/local/api/v1/handshake")
            assert missing_lease.status_code == 409
            assert missing_lease.json()["error"]["code"] == "stale_connection_epoch"

            conversation = client.post(
                "/p/local/api/v1/conversations",
                headers=lease_headers_1,
                json={"title": "Launcher E2E"},
            )
            conversation.raise_for_status()
            conversation_data = conversation.json()
            conversation_id = str(conversation_data["id"])
            start_path = (
                "/p/local/api/v1/conversations/"
                f"{conversation_id}/messages/runs"
            )
            start_payload = {
                "content": "/help",
                "parent_node_id": conversation_data["current_node_id"],
            }
            idempotency_key = "real-response-loss-message"
            _send_without_reading_response(
                launcher_port,
                start_path,
                headers={
                    **lease_headers_1,
                    "Idempotency-Key": idempotency_key,
                    "X-Request-ID": "real-lost-response",
                },
                payload=start_payload,
            )
            first_run = _wait_for_conversation_run(
                client,
                conversation_id,
                lease_headers_1,
            )

            replay = client.post(
                start_path,
                headers={
                    **lease_headers_1,
                    "Idempotency-Key": idempotency_key,
                },
                json=start_payload,
            )
            assert replay.status_code == 200
            assert replay.json()["created"] is False
            assert replay.json()["run_id"] == first_run["run_id"]

            conflict = client.post(
                start_path,
                headers={
                    **lease_headers_1,
                    "Idempotency-Key": idempotency_key,
                },
                json={**start_payload, "content": "/status"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "idempotency_key_conflict"

            statuses: list[str] = []
            with client.stream(
                "GET",
                f"/p/local/api/v1/runs/{first_run['run_id']}/attach",
                headers=lease_headers_1,
            ) as attached:
                attached.raise_for_status()
                done = False
                for line in attached.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    value = line[5:].strip()
                    if value == "[DONE]":
                        done = True
                        break
                    statuses.append(str(json.loads(value).get("status")))
            assert done is True
            assert {"start", "content", "complete"}.issubset(statuses)

        second_env = env.copy()
        second_env["CHATTREE_SERVER_PORT"] = str(second_server_port)
        with second_server_log.open("wb") as output:
            second_server = subprocess.Popen(
                [sys.executable, "-m", "main"],
                cwd=PROJECT_ROOT,
                env=second_env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                shell=False,
                close_fds=True,
            )
            second_server.wait(timeout=15)
        second_log = _read_log(second_server_log)
        assert "ServerHomeInUseError" in second_log
        assert "CHATTREE_HOME" in second_log
        assert _port_is_closed(second_server_port)

        _stop_process(launcher)
        launcher = None
        direct_health = httpx.get(
            f"http://127.0.0.1:{server_port}/api/v1/health",
            trust_env=False,
            timeout=2,
        )
        assert direct_health.json()["server_instance_id"] == instance_id

        launcher = _start_launcher(env, launcher_log)
        reconnected = _wait_for_json(
            status_url,
            lambda payload: payload.get("status") == "ready",
            process=launcher,
            log_path=launcher_log,
        )
        assert reconnected["server_instance_id"] == instance_id
        assert _server_owner_pid(server_home / ".server.lock") == server_pid_1
        lease_before_restart = str(reconnected["connection_lease_id"])

        with httpx.Client(
            base_url=f"http://127.0.0.1:{launcher_port}",
            trust_env=False,
            timeout=60,
        ) as client:
            stale_after_launcher_restart = client.post(
                "/p/local/api/v1/conversations",
                headers=lease_headers_1,
                json={"title": "must not be created"},
            )
            assert stale_after_launcher_restart.status_code == 409
            assert stale_after_launcher_restart.json()["error"]["code"] == (
                "stale_connection_epoch"
            )

            restarted = client.post(
                "/client/v1/profiles/local/server/restart",
                json={
                    "expected_server_instance_id": instance_id,
                    "timeout_seconds": 30,
                },
            )
            restarted.raise_for_status()
            restarted_data = restarted.json()
            assert restarted_data["status"] == "ready"
            assert restarted_data["server_instance_id"] == instance_id
            lease_2 = str(restarted_data["connection_lease_id"])
            assert lease_2 != lease_before_restart

            server_pid_2 = _server_owner_pid(server_home / ".server.lock")
            server_pids.add(server_pid_2)
            assert server_pid_2 != server_pid_1
            _wait_until(
                lambda: not _pid_is_running(server_pid_1),
                description="old Server process exit after restart",
            )

            stale_after_server_restart = client.post(
                "/p/local/api/v1/conversations",
                headers={LEASE_HEADER: lease_before_restart},
                json={"title": "must not be created either"},
            )
            assert stale_after_server_restart.status_code == 409
            assert stale_after_server_restart.headers[LEASE_HEADER] == lease_2

            current_handshake = client.get(
                "/p/local/api/v1/handshake",
                headers={LEASE_HEADER: lease_2},
            )
            current_handshake.raise_for_status()
            assert current_handshake.json()["server_instance_id"] == instance_id

            stopped = client.post(
                "/client/v1/profiles/local/server/stop",
                json={
                    "expected_server_instance_id": instance_id,
                    "timeout_seconds": 30,
                },
            )
            stopped.raise_for_status()
            assert stopped.json()["status"] == "disconnected"

            rejected_after_stop = client.post(
                "/p/local/api/v1/conversations",
                headers={LEASE_HEADER: lease_2},
                json={"title": "Server is stopped"},
            )
            assert rejected_after_stop.status_code == 503
            assert rejected_after_stop.json()["error"]["code"] == "profile_not_ready"

        _wait_until(
            lambda: _port_is_closed(server_port),
            description="Server port release after cooperative stop",
        )
        _wait_until(
            lambda: not _pid_is_running(server_pid_2),
            description="Server process exit after cooperative stop",
        )
        with ServerHomeLock(server_home):
            pass
        assert not list((client_home / "logs").glob("*.spawn.pid"))

        with closing(sqlite3.connect(server_home / "chattree.sqlite")) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        _stop_process(second_server)
        if launcher is not None and launcher.poll() is None and instance_id is not None:
            try:
                httpx.post(
                    f"http://127.0.0.1:{launcher_port}"
                    "/client/v1/profiles/local/server/stop",
                    json={
                        "expected_server_instance_id": instance_id,
                        "timeout_seconds": 5,
                    },
                    trust_env=False,
                    timeout=10,
                )
            except httpx.HTTPError:
                pass
        _stop_process(launcher)
        for pid in server_pids:
            _cleanup_server(pid, server_port)

    assert _port_is_closed(launcher_port)
    assert _port_is_closed(server_port)
    assert _port_is_closed(second_server_port)
    assert all(not _pid_is_running(pid) for pid in server_pids)
