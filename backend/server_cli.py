from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from importlib import import_module
from pathlib import Path
from typing import Any, Sequence

from backend.core.persistence.home import resolve_chattree_home
from backend.core.server import SERVER_VERSION
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
    _add_server_options(serve)

    start = subparsers.add_parser(
        "start",
        help="start ChatTree Server as a detached background process",
    )
    _add_server_options(start)
    return parser


def _add_server_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        type=_loopback_host,
        help="loopback host to bind",
    )
    parser.add_argument(
        "--port",
        default=DEFAULT_PORT,
        type=_tcp_port,
        help="loopback TCP port to bind",
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
    existing = _probe_handshake(args.host, args.port)
    if existing is not None:
        print(
            json.dumps(
                {
                    "status": "already_running",
                    "pid": None,
                    "command": None,
                    "home": str(home),
                    "host": args.host,
                    "port": args.port,
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

    command = [
        sys.executable,
        "-m",
        "backend.server_cli",
        "serve",
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.home:
        command.extend(["--home", os.environ["CHATTREE_HOME"]])

    env = os.environ.copy()
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
                "port": args.port,
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


def _http_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _load_main_module() -> Any:
    return import_module("main")


if __name__ == "__main__":
    raise SystemExit(main())
