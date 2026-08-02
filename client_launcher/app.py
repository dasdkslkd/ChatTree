from __future__ import annotations

import asyncio
import os
import secrets
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from backend.core.server import SERVER_VERSION
from client_launcher.http_errors import (
    ErrorEnvelope,
    RequestBoundaryMiddleware,
    install_error_handlers,
    launcher_error_response,
)
from client_launcher.local_server import LocalServerConnector
from client_launcher.models import LauncherError, LocalTarget, ServerProfile, ServerSession, ssh_profile_id
from client_launcher.profiles import ProfileStore
from client_launcher.proxy import (
    CONNECTION_LEASE_HEADER,
    ProxyError,
    create_proxy_router,
)
from client_launcher.sessions import SessionManager
from client_launcher.settings import (
    PROFILES_FILENAME,
    LauncherSettings,
)
from client_launcher.ssh_config import SshConfigStore
from client_launcher.ssh_connector import SshServerConnector


class _LauncherApp(FastAPI):
    def __init__(
        self,
        *,
        allowed_origins: tuple[str, ...],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._launcher_allowed_origins = allowed_origins

    def build_middleware_stack(self):
        stack = super().build_middleware_stack()
        stack = CORSMiddleware(
            stack,
            allow_origins=list(self._launcher_allowed_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[CONNECTION_LEASE_HEADER, "X-Request-ID"],
        )
        return RequestBoundaryMiddleware(
            stack,
            allowed_origins=self._launcher_allowed_origins,
        )


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateProfileRequest(StrictRequestModel):
    label: str = Field(min_length=1, max_length=120)
    auto_connect: bool = False
    server_home: str = Field(min_length=1)
    server_port: int = Field(
        ge=1,
        le=65535,
    )


class UpdateProfileRequest(StrictRequestModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    auto_connect: bool | None = None
    server_home: str | None = Field(default=None, min_length=1)
    server_port: int | None = Field(default=None, ge=1, le=65535)


class ConnectProfileRequest(StrictRequestModel):
    rebind: bool = False
    expected_server_instance_id: str | None = None


class ServerLifecycleRequest(StrictRequestModel):
    expected_server_instance_id: str = Field(min_length=1, max_length=128)
    timeout_seconds: int = Field(default=30, ge=1, le=600)


class SshConfigUpdateRequest(StrictRequestModel):
    text: str


def create_app(
    *,
    settings: LauncherSettings | None = None,
    profiles: ProfileStore | None = None,
    connector: Any | None = None,
    ssh_config: SshConfigStore | None = None,
    proxy_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    resolved_settings = settings or LauncherSettings.from_env()
    # 生成反向代理 token：本地 server 子进程继承环境变量，SSH 隧道注入远程 server provider
    if not os.environ.get("CHATTREE_PROXY_TOKEN"):
        os.environ["CHATTREE_PROXY_TOKEN"] = secrets.token_urlsafe(24)
    profile_store = profiles or ProfileStore(
        resolved_settings.client_home / PROFILES_FILENAME,
        default_server_port=resolved_settings.default_local_server_port,
    )
    local_connector = connector or {
        "local": LocalServerConnector(resolved_settings),
        "ssh": SshServerConnector(
            resolved_settings,
            local_port_resolver=lambda: _resolve_local_server_port(profile_store),
        ),
    }
    sessions = SessionManager(profile_store, local_connector)
    ssh_config_store = ssh_config or SshConfigStore()
    owns_proxy_client = proxy_client is None
    upstream_client = proxy_client or httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await sessions.start()
        try:
            yield
        finally:
            await sessions.close()
            if owns_proxy_client:
                await upstream_client.aclose()

    allowed_origins = tuple(
        dict.fromkeys(
            (
                *resolved_settings.allowed_origins,
                f"http://localhost:{resolved_settings.port}",
                f"http://127.0.0.1:{resolved_settings.port}",
            )
        )
    )
    app = _LauncherApp(
        allowed_origins=allowed_origins,
        title="ChatTree Client Launcher",
        version=SERVER_VERSION,
        lifespan=lifespan,
        responses={422: {"model": ErrorEnvelope}},
    )
    app.state.launcher_settings = resolved_settings
    app.state.profile_store = profile_store
    app.state.session_manager = sessions
    app.state.proxy_client = upstream_client
    install_error_handlers(app)

    @app.exception_handler(ProxyError)
    async def proxy_error_handler(
        request: Request,
        exc: ProxyError,
    ) -> Response:
        response = await launcher_error_response(request, exc)
        connection_lease_id = getattr(exc, "connection_lease_id", None)
        if isinstance(connection_lease_id, str):
            response.headers[CONNECTION_LEASE_HEADER] = connection_lease_id
        return response

    @app.get("/client/v1/profiles")
    async def list_profiles() -> list[dict[str, Any]]:
        return [
            profile.to_dict()
            for profile in profile_store.list()
            if profile.kind == "local"
        ]

    @app.post("/client/v1/profiles", status_code=201)
    async def create_profile(body: CreateProfileRequest) -> dict[str, Any]:
        try:
            profile = ServerProfile(
                id=str(uuid4()),
                label=body.label.strip(),
                kind="local",
                auto_connect=body.auto_connect,
                bound_server_instance_id=None,
                local=LocalTarget(
                    server_home=body.server_home,
                    server_port=body.server_port,
                ),
            )
        except ValueError as exc:
            raise LauncherError(
                "profile_invalid",
                str(exc),
                retryable=False,
                status_code=422,
            ) from exc
        return profile_store.create(profile).to_dict()

    @app.patch("/client/v1/profiles/{profile_id}")
    async def update_profile(
        profile_id: str,
        body: UpdateProfileRequest,
    ) -> dict[str, Any]:
        current = profile_store.get(profile_id)
        if current.kind != "local":
            raise LauncherError(
                "profile_update_unsupported",
                "Only local profiles can be edited through the profile API",
                False,
                409,
            )
        local = None
        if body.server_home is not None or body.server_port is not None:
            try:
                local = LocalTarget(
                    server_home=body.server_home or current.local.server_home,
                    server_port=(
                        body.server_port
                        if body.server_port is not None
                        else current.local.server_port
                    ),
                )
            except ValueError as exc:
                raise LauncherError(
                    "profile_invalid",
                    str(exc),
                    retryable=False,
                    status_code=422,
                ) from exc
        updated = await sessions.update_profile(
            profile_id,
            label=body.label.strip() if body.label is not None else None,
            auto_connect=body.auto_connect,
            local=local,
        )
        return updated.to_dict()

    @app.delete("/client/v1/profiles/{profile_id}", status_code=204)
    async def delete_profile(profile_id: str) -> Response:
        if profile_store.get(profile_id).kind != "local":
            raise LauncherError(
                "profile_delete_unsupported",
                "SSH profiles are managed from SSH Hosts",
                False,
                409,
            )
        await sessions.delete_profile(profile_id)
        return Response(status_code=204)

    @app.post("/client/v1/profiles/{profile_id}/connect")
    async def connect_profile(
        profile_id: str,
        request: Request,
        body: ConnectProfileRequest | None = None,
    ) -> dict[str, Any]:
        options = body or ConnectProfileRequest()
        return (
            await sessions.connect(
                profile_id,
                rebind=options.rebind,
                expected_server_instance_id=options.expected_server_instance_id,
                request_id=request.state.request_id,
            )
        ).to_dict()

    @app.post("/client/v1/profiles/{profile_id}/disconnect")
    async def disconnect_profile(profile_id: str) -> dict[str, Any]:
        return (await sessions.disconnect(profile_id)).to_dict()

    @app.post("/client/v1/profiles/{profile_id}/server/stop")
    async def stop_profile_server(
        profile_id: str,
        request: Request,
        body: ServerLifecycleRequest,
    ) -> dict[str, Any]:
        return (
            await sessions.stop(
                profile_id,
                expected_server_instance_id=body.expected_server_instance_id,
                timeout=float(body.timeout_seconds),
                request_id=request.state.request_id,
            )
        ).to_dict()

    @app.post("/client/v1/profiles/{profile_id}/server/restart")
    async def restart_profile_server(
        profile_id: str,
        request: Request,
        body: ServerLifecycleRequest,
    ) -> dict[str, Any]:
        return (
            await sessions.restart(
                profile_id,
                expected_server_instance_id=body.expected_server_instance_id,
                timeout=float(body.timeout_seconds),
                request_id=request.state.request_id,
            )
        ).to_dict()

    @app.get("/client/v1/profiles/{profile_id}/status")
    async def profile_status(profile_id: str) -> dict[str, Any]:
        return sessions.status(profile_id).to_dict()

    @app.post("/client/v1/shutdown", status_code=202)
    async def shutdown_launcher() -> dict[str, str]:
        async def _trigger_shutdown() -> None:
            # Allow the HTTP response to flush before signalling uvicorn to
            # perform a graceful shutdown which runs the lifespan finally
            # block (sessions.close() -> cascades server termination).
            await asyncio.sleep(0.3)
            try:
                signal.raise_signal(signal.SIGINT)
            except Exception:
                # Fallback for environments where raise_signal is unavailable.
                os._exit(0)

        asyncio.create_task(_trigger_shutdown())
        return {"status": "shutting_down"}

    @app.get("/client/v1/ssh/config")
    async def get_ssh_config() -> dict[str, Any]:
        return ssh_config_store.read().to_dict()

    @app.put("/client/v1/ssh/config")
    async def put_ssh_config(body: SshConfigUpdateRequest) -> dict[str, Any]:
        return ssh_config_store.write(body.text).to_dict()

    @app.get("/client/v1/ssh/hosts")
    async def list_ssh_hosts() -> dict[str, Any]:
        snapshot = ssh_config_store.read()
        return {
            "path": snapshot.path,
            "hosts": list(snapshot.hosts),
            "warnings": list(snapshot.warnings),
        }

    def require_config_host(host_alias: str) -> str:
        snapshot = ssh_config_store.read()
        if host_alias not in snapshot.hosts:
            raise LauncherError(
                "ssh_host_not_found",
                f"SSH Host alias is not present in {snapshot.path}: {host_alias}",
                False,
                404,
            )
        return host_alias

    @app.post("/client/v1/ssh/hosts/{host_alias}/connect")
    async def connect_ssh_host(
        host_alias: str,
        request: Request,
        body: ConnectProfileRequest | None = None,
    ) -> dict[str, Any]:
        require_config_host(host_alias)
        profile = profile_store.ensure_ssh_profile(host_alias)
        options = body or ConnectProfileRequest()
        session = await sessions.connect(
            profile.id,
            rebind=options.rebind,
            expected_server_instance_id=options.expected_server_instance_id,
            request_id=request.state.request_id,
        )
        return {
            "profile_id": profile.id,
            "host_alias": host_alias,
            "session": session.to_dict(),
        }

    @app.post("/client/v1/ssh/hosts/{host_alias}/disconnect")
    async def disconnect_ssh_host(host_alias: str) -> dict[str, Any]:
        require_config_host(host_alias)
        profile_id = ssh_profile_id(host_alias)
        try:
            session = await sessions.disconnect(profile_id)
        except LauncherError as exc:
            if exc.code != "profile_not_found":
                raise
            session = ServerSession(profile_id=profile_id)
        return {
            "profile_id": profile_id,
            "host_alias": host_alias,
            "session": session.to_dict(),
        }

    @app.get("/client/v1/ssh/hosts/{host_alias}/status")
    async def ssh_host_status(host_alias: str) -> dict[str, Any]:
        require_config_host(host_alias)
        profile_id = ssh_profile_id(host_alias)
        try:
            session = sessions.status(profile_id)
        except LauncherError as exc:
            if exc.code != "profile_not_found":
                raise
            session = ServerSession(profile_id=profile_id)
        return {
            "profile_id": profile_id,
            "host_alias": host_alias,
            "session": session.to_dict(),
        }

    app.include_router(
        create_proxy_router(
            sessions.resolve_endpoint,
            upstream_client,
            resolved_settings.max_request_body_bytes,
            connect_timeout=resolved_settings.connect_timeout_seconds,
        )
    )
    _mount_frontend(app, resolved_settings)
    return app


def _mount_frontend(app: FastAPI, settings: LauncherSettings) -> None:
    if settings.frontend_dist is None:
        return
    dist = settings.frontend_dist
    index_path = dist / "index.html"
    if not index_path.exists():
        return

    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend_assets")

    @app.get("/s/{path:path}")
    async def spa_fallback() -> FileResponse:
        return FileResponse(str(index_path))


def _resolve_local_server_port(profile_store: ProfileStore) -> int | None:
    """查找本地 profile 的 server_port，用于建立 SSH 反向隧道。"""
    for profile in profile_store.list():
        if profile.kind == "local" and profile.local is not None:
            return profile.local.server_port
    return None
