from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from client_launcher.local_server import LocalServerConnector
from client_launcher.models import LauncherError, LocalTarget, ServerProfile
from client_launcher.profiles import ProfileStore
from client_launcher.proxy import ProxyError, create_proxy_router
from client_launcher.sessions import SessionManager
from client_launcher.settings import (
    DEFAULT_LOCAL_PROFILE_ID,
    DEFAULT_LOCAL_SERVER_PORT,
    PROFILES_FILENAME,
    LauncherSettings,
)


class CreateProfileRequest(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    auto_connect: bool = False
    server_home: str = Field(min_length=1)
    server_port: int = Field(
        default=DEFAULT_LOCAL_SERVER_PORT,
        ge=1,
        le=65535,
    )


class UpdateProfileRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    auto_connect: bool | None = None
    server_home: str | None = Field(default=None, min_length=1)
    server_port: int | None = Field(default=None, ge=1, le=65535)


class ConnectProfileRequest(BaseModel):
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

    app = FastAPI(
        title="ChatTree Client Launcher",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.launcher_settings = resolved_settings
    app.state.profile_store = profile_store
    app.state.session_manager = sessions
    app.state.proxy_client = upstream_client

    allowed_origins = tuple(
        dict.fromkeys(
            (
                *resolved_settings.allowed_origins,
                f"http://localhost:{resolved_settings.port}",
                f"http://127.0.0.1:{resolved_settings.port}",
            )
        )
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        incoming_request_id = request.headers.get("x-request-id", "").strip()
        request_id = (
            incoming_request_id
            if incoming_request_id and len(incoming_request_id) <= 128
            else f"req_{uuid4().hex}"
        )
        request.state.request_id = request_id
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins:
            response = _error_response(
                request,
                code="origin_not_allowed",
                message=f"Origin is not allowed: {origin}",
                retryable=False,
                status_code=403,
            )
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(LauncherError)
    async def launcher_error_handler(
        request: Request,
        exc: LauncherError,
    ) -> JSONResponse:
        return _error_response(
            request,
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            status_code=exc.status_code,
        )

    @app.exception_handler(ProxyError)
    async def proxy_error_handler(
        request: Request,
        exc: ProxyError,
    ) -> JSONResponse:
        profile_id = getattr(exc, "profile_id", None)
        if profile_id and exc.code == "proxy_upstream_unavailable":
            sessions.mark_error(
                profile_id,
                LauncherError(
                    exc.code,
                    exc.message,
                    exc.retryable,
                    exc.status_code,
                ),
            )
        return _error_response(
            request,
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        return _error_response(
            request,
            code="invalid_request",
            message="Request validation failed",
            retryable=False,
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        return _error_response(
            request,
            code="route_not_found" if exc.status_code == 404 else "http_error",
            message=str(exc.detail),
            retryable=False,
            status_code=exc.status_code,
        )

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
        endpoint_changed = body.server_home is not None or body.server_port is not None
        if endpoint_changed:
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
            await sessions.disconnect(profile_id)
        updated = profile_store.update(
            profile_id,
            label=body.label.strip() if body.label is not None else None,
            auto_connect=body.auto_connect,
            local=local,
        )
        return updated.to_dict()

    @app.delete("/client/v1/profiles/{profile_id}", status_code=204)
    async def delete_profile(profile_id: str) -> Response:
        if profile_id == DEFAULT_LOCAL_PROFILE_ID:
            profile_store.delete(profile_id)
        profile_store.get(profile_id)
        await sessions.disconnect(profile_id)
        profile_store.delete(profile_id)
        return Response(status_code=204)

    @app.post("/client/v1/profiles/{profile_id}/connect")
    async def connect_profile(
        profile_id: str,
        body: ConnectProfileRequest | None = None,
    ) -> dict[str, Any]:
        options = body or ConnectProfileRequest()
        return (
            await sessions.connect(
                profile_id,
                rebind=options.rebind,
                expected_server_instance_id=options.expected_server_instance_id,
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


def _error_response(
    request: Request,
    *,
    code: str,
    message: str,
    retryable: bool,
    status_code: int,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", f"req_{uuid4().hex}")
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "request_id": request_id,
            }
        },
    )
