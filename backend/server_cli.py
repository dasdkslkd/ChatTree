from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from importlib import import_module
from pathlib import Path
from typing import Any, Sequence

from backend.core.home import resolve_chattree_home
from backend.core.server import (
    SERVER_HOME_LOCK_FILENAME,
    SERVER_VERSION,
    ServerHomeInUseError,
    ServerHomeLock,
)
from backend.core.subprocess_utils import subprocess_window_kwargs


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001


class CliError(RuntimeError):
    pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = getattr(args, "command", None)
    if command == "serve":
        return _serve(args)
    if command == "start":
        return _start(args)
    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chattree-server")
    parser.add_argument(
        "--version",
        action="version",
        version=f"chattree-server {SERVER_VERSION}",
    )
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="run ChatTree Server in foreground")
    _add_server_options(serve, allow_auto_port=False)

    start = subparsers.add_parser(
        "start",
        help="start ChatTree Server as a detached background process",
    )
    _add_server_options(start, allow_auto_port=True)
    return parser


def _add_server_options(
    parser: argparse.ArgumentParser,
    *,
    allow_auto_port: bool,
) -> None:
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        type=_loopback_host,
        help="loopback host to bind",
    )
    parser.add_argument(
        "--port",
        default=DEFAULT_PORT,
        type=_tcp_port_or_auto if allow_auto_port else _tcp_port,
        help=(
            "loopback TCP port to bind, or 0/auto for a free port"
            if allow_auto_port
            else "loopback TCP port to bind"
        ),
    )
    parser.add_argument(
        "--home",
        default=None,
        help="CHATTREE_HOME for server state",
    )


def _loopback_host(value: str) -> str:
    if value not in LOOPBACK_HOSTS:
        raise argparse.ArgumentTypeError(
            "--host must be one of 127.0.0.1, localhost, ::1"
        )
    return value


def _tcp_port(value: str | int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "--port must be an integer from 1 to 65535"
        ) from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            "--port must be an integer from 1 to 65535"
        )
    return port


def _tcp_port_or_auto(value: str | int) -> int:
    if isinstance(value, str) and value.lower() == "auto":
        return 0
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "--port must be an integer from 0 to 65535, or auto"
        ) from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            "--port must be an integer from 0 to 65535, or auto"
        )
    return port


def _apply_home(home: str | None) -> None:
    if home:
        os.environ["CHATTREE_HOME"] = str(Path(home).expanduser().resolve())


def _serve(args: argparse.Namespace) -> int:
    _apply_home(args.home)
    server_main = _load_main_module()
    server_main.run_server(host=args.host, port=args.port)
    return 0


def _start(args: argparse.Namespace) -> int:
    _apply_home(args.home)
    home = resolve_chattree_home()
    requested_port = int(args.port)
    owner = _locked_home_owner(home)
    owner_host = _owner_host(owner) if owner else None
    owner_port = _owner_port(owner) if owner else None
    if owner is not None:
        if owner_port is not None or requested_port != 0:
            if owner_port is not None:
                probe_host = owner_host or args.host
            else:
                probe_host = args.host
            probe_port = owner_port if owner_port is not None else requested_port
            existing = _probe_handshake(probe_host, probe_port)
            if existing is not None:
                print(
                    json.dumps(
                        {
                            "status": "already_running",
                            "pid": owner.get("pid"),
                            "command": None,
                            "home": str(home),
                            "host": probe_host,
                            "port": probe_port,
                            "log_path": str(home / "logs" / "server.log"),
                            "server_instance_id": existing.get("server_instance_id"),
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
        print(
            (
                "ChatTree Server home is already in use but the running "
                "Server could not be probed"
            ),
            file=sys.stderr,
        )
        return 1

    port = (
        _allocate_loopback_port(args.host)
        if requested_port == 0
        else requested_port
    )
    existing = _probe_handshake(args.host, port)
    if existing is not None:
        print(
            json.dumps(
                {
                    "status": "already_running",
                    "pid": None,
                    "command": None,
                    "home": str(home),
                    "host": args.host,
                    "port": port,
                    "log_path": str(home / "logs" / "server.log"),
                    "server_instance_id": existing.get("server_instance_id"),
                },
                ensure_ascii=False,
            )
        )
        return 0

    log_path = home / "logs" / "server.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("ab")
    except OSError as exc:
        print(f"Could not open server log: {log_path}: {exc}", file=sys.stderr)
        return 1

    command = _serve_command(args.host, port)
    if args.home:
        command.extend(["--home", os.environ["CHATTREE_HOME"]])

    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            env=env,
            cwd=str(Path.cwd()),
            shell=False,
            **subprocess_window_kwargs(new_process_group=True),
        )
    except OSError as exc:
        log_file.close()
        print(f"Could not start ChatTree Server: {exc}", file=sys.stderr)
        return 1
    finally:
        if not log_file.closed:
            log_file.close()

    print(
        json.dumps(
            {
                "status": "started",
                "pid": process.pid,
                "command": command,
                "home": str(home),
                "host": args.host,
                "port": port,
                "log_path": str(log_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _probe_handshake(host: str, port: int) -> dict[str, Any] | None:
    url = f"http://{_http_host(host)}:{port}/api/v1/handshake"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        TimeoutError,
        ValueError,
        urllib.error.URLError,
        urllib.error.HTTPError,
    ):
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("server_instance_id"), str):
        return None
    if not isinstance(payload.get("protocol_version"), int):
        return None
    return payload


def _allocate_loopback_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    bind_host = "127.0.0.1" if host == "localhost" else host
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((bind_host, 0))
        return int(probe.getsockname()[1])


def _locked_home_owner(home: Path) -> dict[str, Any] | None:
    lock = ServerHomeLock(home)
    try:
        lock.acquire()
    except ServerHomeInUseError:
        return _read_lock_owner(home)
    finally:
        if lock.acquired:
            lock.release()
    return None


def _read_lock_owner(home: Path) -> dict[str, Any] | None:
    try:
        raw = (home / SERVER_HOME_LOCK_FILENAME).read_bytes()[1:8193]
        value = json.loads(raw.decode("utf-8"))
    except (OSError, IndexError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _owner_host(owner: dict[str, Any]) -> str | None:
    value = owner.get("host")
    return value if isinstance(value, str) and value in LOOPBACK_HOSTS else None


def _owner_port(owner: dict[str, Any]) -> int | None:
    value = owner.get("port")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 65535 else None


def _http_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _serve_command(host: str, port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        command = [sys.executable, "serve"]
    else:
        command = [sys.executable, "-m", "backend.server_cli", "serve"]
    command.extend(["--host", host, "--port", str(port)])
    return command


def _load_main_module() -> Any:
    return import_module("main")


if __name__ == "__main__":
    raise SystemExit(main())
