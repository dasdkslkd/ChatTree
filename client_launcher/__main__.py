from __future__ import annotations

import uvicorn

from client_launcher.app import create_app
from client_launcher.settings import LauncherSettings


def main() -> None:
    settings = LauncherSettings.from_env()
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        lifespan="on",
    )


if __name__ == "__main__":
    main()
