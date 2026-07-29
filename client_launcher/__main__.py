from __future__ import annotations

import json
import socket
from dataclasses import replace

import uvicorn

from client_launcher.app import create_app
from client_launcher.settings import LauncherSettings


LAUNCHER_READY_PREFIX = "CHATTREE_LAUNCHER_READY "


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
    config = uvicorn.Config(
        lambda: create_app(settings=settings),
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        lifespan="on",
        date_header=False,
        server_header=False,
    )
    listener = config.bind_socket()
    settings = replace(settings, port=listener.getsockname()[1])
    try:
        LauncherServer(config).run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == "__main__":
    main()
