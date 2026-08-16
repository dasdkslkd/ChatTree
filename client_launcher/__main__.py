from __future__ import annotations

import json
import socket
from dataclasses import replace
from pathlib import Path

import uvicorn

from client_launcher.app import create_app
from client_launcher.settings import LauncherSettings


LAUNCHER_READY_PREFIX = "CHATTREE_LAUNCHER_READY "

# 自动端口模式下记住上次绑定的端口，保证前端 origin 跨重启稳定，
# localStorage（外观设置等）才能持久化
STICKY_PORT_FILENAME = "launcher-port"


def _read_sticky_port(client_home: Path) -> int | None:
    try:
        raw = (client_home / STICKY_PORT_FILENAME).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _write_sticky_port(client_home: Path, port: int) -> None:
    client_home.mkdir(parents=True, exist_ok=True)
    (client_home / STICKY_PORT_FILENAME).write_text(
        str(port), encoding="utf-8", newline="\n"
    )


class LauncherServer(uvicorn.Server):
    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.should_exit or not sockets:
            return
        host, port = sockets[0].getsockname()[:2]
        payload = json.dumps({"host": host, "port": port})
        print(f"{LAUNCHER_READY_PREFIX}{payload}", flush=True)


def main() -> None:
    settings = LauncherSettings.from_env()
    requested_port = (
        _read_sticky_port(settings.client_home) if settings.port == 0 else settings.port
    )

    def make_config(port: int) -> uvicorn.Config:
        return uvicorn.Config(
            lambda: create_app(settings=settings),
            factory=True,
            host=settings.host,
            port=port,
            workers=1,
            lifespan="on",
            date_header=False,
            server_header=False,
        )

    config = make_config(requested_port or 0)
    try:
        listener = config.bind_socket()
    except OSError:
        # 显式端口或随机端口都失败时直接上抛；只有自动端口被占用时退回随机端口
        if settings.port != 0 or requested_port is None:
            raise
        config = make_config(0)
        listener = config.bind_socket()
    bound_port = listener.getsockname()[1]
    if settings.port == 0:
        _write_sticky_port(settings.client_home, bound_port)
    settings = replace(settings, port=bound_port)
    try:
        LauncherServer(config).run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == "__main__":
    main()
