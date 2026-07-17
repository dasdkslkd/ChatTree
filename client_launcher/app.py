from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from client_launcher.http_errors import (
    ErrorEnvelope,
    RequestBoundaryMiddleware,
    install_error_handlers,
    launcher_error_response,
)
from client_launcher.local_server import LocalServerConnector
from client_launcher.models import LauncherError, LocalTarget, ServerProfile
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
    auto_connect: bool = True
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


def create_app(
    *,
    settings: LauncherSettings | None = None,
    profiles: ProfileStore | None = None,
    connector: Any | None = None,
    proxy_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    resolved_settings = settings or LauncherSettings.from_env()
    profile_store = profiles or ProfileStore(
        resolved_settings.client_home / PROFILES_FILENAME,
        default_server_port=resolved_settings.default_local_server_port,
    )
    local_connector = connector or LocalServerConnector(resolved_settings)
    sessions = SessionManager(profile_store, local_connector)
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
        version="0.1.0",
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
        profile_id = getattr(exc, "profile_id", None)
        connection_epoch = getattr(exc, "connection_epoch", None)
        if (
            profile_id
            and connection_epoch is not None
            and exc.code == "proxy_upstream_unavailable"
        ):
            sessions.mark_error(
                profile_id,
                LauncherError(
                    exc.code,
                    exc.message,
                    exc.retryable,
                    exc.status_code,
                ),
                connection_epoch=connection_epoch,
            )
        response = await launcher_error_response(request, exc)
        connection_lease_id = getattr(exc, "connection_lease_id", None)
        if isinstance(connection_lease_id, str):
            response.headers[CONNECTION_LEASE_HEADER] = connection_lease_id
        return response

    @app.get("/client/v1/profiles")
    async def list_profiles() -> list[dict[str, Any]]:
        return [profile.to_dict() for profile in profile_store.list()]

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

    @app.get("/client/v1/profiles/{profile_id}/status")
    async def profile_status(profile_id: str) -> dict[str, Any]:
        return sessions.status(profile_id).to_dict()

    app.include_router(
        create_proxy_router(
            sessions.resolve_endpoint,
            upstream_client,
            resolved_settings.max_request_body_bytes,
            connect_timeout=resolved_settings.connect_timeout_seconds,
            read_timeout=resolved_settings.proxy_idle_timeout_seconds,
        )
    )
    return app
