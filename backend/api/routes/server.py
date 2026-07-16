from __future__ import annotations

import time as clock

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from backend.core.server.identity import ServerIdentity
from backend.core.server.protocol import (
    PROTOCOL_FEATURES,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    provider_is_configured,
    runtime_platform,
)


router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    server_instance_id: str
    time: int


class HandshakeResponse(BaseModel):
    server_instance_id: str
    protocol_version: int
    server_version: str
    platform: str
    features: list[str]
    provider_configured: bool


def _identity(request: Request) -> ServerIdentity:
    identity = getattr(request.app.state, "server_identity", None)
    if not isinstance(identity, ServerIdentity):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="server identity is not initialized",
        )
    return identity


@router.get("/health", response_model=HealthResponse)
async def get_health(request: Request) -> HealthResponse:
    identity = _identity(request)
    return HealthResponse(
        status="ok",
        server_instance_id=identity.server_instance_id,
        time=int(clock.time()),
    )


@router.get("/handshake", response_model=HandshakeResponse)
async def get_handshake(request: Request) -> HandshakeResponse:
    identity = _identity(request)
    config_manager = getattr(request.app.state, "config_manager", None)
    config_data = getattr(config_manager, "data", {})
    return HandshakeResponse(
        server_instance_id=identity.server_instance_id,
        protocol_version=PROTOCOL_VERSION,
        server_version=SERVER_VERSION,
        platform=runtime_platform(),
        features=list(PROTOCOL_FEATURES),
        provider_configured=provider_is_configured(config_data),
    )
