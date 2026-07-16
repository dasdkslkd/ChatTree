from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


CLIENT_HOME_ENV = "CHATTREE_CLIENT_HOME"
DEFAULT_CLIENT_HOME_NAME = ".chattree-client"
DEFAULT_LOCAL_PROFILE_ID = "local"
DEFAULT_LOCAL_SERVER_PORT = 8001
DEFAULT_LAUNCHER_PORT = 8000
PROFILES_FILENAME = "profiles.json"
PROFILES_SCHEMA_VERSION = 1

DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def resolve_client_home(
    value: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    explicit = value or values.get(CLIENT_HOME_ENV)
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (Path.home() / DEFAULT_CLIENT_HOME_NAME).resolve()


def _integer_setting(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        limit = f" from {minimum} to {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be an integer{limit}")
    return value


def _float_setting(
    environ: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    raw = environ.get(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


@dataclass(frozen=True)
class LauncherSettings:
    client_home: Path
    project_root: Path
    server_python: str
    host: str = "127.0.0.1"
    port: int = DEFAULT_LAUNCHER_PORT
    default_local_server_port: int = DEFAULT_LOCAL_SERVER_PORT
    connect_timeout_seconds: float = 2.0
    start_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 0.2
    max_request_body_bytes: int = 64 * 1024 * 1024
    proxy_idle_timeout_seconds: float = 300.0
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS

    @classmethod
    def from_env(
        cls,
        project_root: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> LauncherSettings:
        values = os.environ if environ is None else environ
        root = (
            Path(project_root)
            if project_root is not None
            else Path(__file__).resolve().parent.parent
        ).expanduser().resolve()
        origins_raw = values.get("CHATTREE_CLIENT_ALLOWED_ORIGINS")
        if origins_raw is None:
            allowed_origins = DEFAULT_ALLOWED_ORIGINS
        else:
            allowed_origins = tuple(
                origin.strip() for origin in origins_raw.split(",") if origin.strip()
            )
            if not allowed_origins:
                raise ValueError(
                    "CHATTREE_CLIENT_ALLOWED_ORIGINS must contain at least one origin"
                )

        return cls(
            client_home=resolve_client_home(environ=values),
            project_root=root,
            server_python=values.get("CHATTREE_SERVER_PYTHON", sys.executable),
            host="127.0.0.1",
            port=_integer_setting(
                values,
                "CHATTREE_CLIENT_PORT",
                DEFAULT_LAUNCHER_PORT,
                minimum=1,
                maximum=65535,
            ),
            default_local_server_port=_integer_setting(
                values,
                "CHATTREE_LOCAL_SERVER_PORT",
                DEFAULT_LOCAL_SERVER_PORT,
                minimum=1,
                maximum=65535,
            ),
            connect_timeout_seconds=_float_setting(
                values,
                "CHATTREE_CLIENT_CONNECT_TIMEOUT_SECONDS",
                2.0,
            ),
            start_timeout_seconds=_float_setting(
                values,
                "CHATTREE_CLIENT_START_TIMEOUT_SECONDS",
                30.0,
            ),
            poll_interval_seconds=_float_setting(
                values,
                "CHATTREE_CLIENT_POLL_INTERVAL_SECONDS",
                0.2,
            ),
            max_request_body_bytes=_integer_setting(
                values,
                "CHATTREE_CLIENT_MAX_REQUEST_BODY_BYTES",
                64 * 1024 * 1024,
                minimum=1,
            ),
            proxy_idle_timeout_seconds=_float_setting(
                values,
                "CHATTREE_CLIENT_PROXY_IDLE_TIMEOUT_SECONDS",
                300.0,
            ),
            allowed_origins=allowed_origins,
        )
