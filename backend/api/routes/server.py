from __future__ import annotations

import time as clock

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from backend.api.errors import ApiError
from backend.core.runs import ProducerRegistry
from backend.core.server.admission import (
    MutationAdmission,
    MutationAdmissionClosed,
    ServerBusyError,
    ServerBusyState,
)
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


class ShutdownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_server_instance_id: str = Field(min_length=1)


class ShutdownResponse(BaseModel):
    server_instance_id: str
    status: str


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


@router.post(
    "/server/shutdown",
    response_model=ShutdownResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def shutdown_server(
    body: ShutdownRequest,
    request: Request,
) -> ShutdownResponse:
    identity = _identity(request)
    if body.expected_server_instance_id != identity.server_instance_id:
        raise ApiError(
            409,
            "server_identity_mismatch",
            "Server instance ID does not match",
            False,
            {
                "expected_server_instance_id": body.expected_server_instance_id,
                "observed_server_instance_id": identity.server_instance_id,
            },
        )

    admission = getattr(request.app.state, "mutation_admission", None)
    producer_registry = getattr(request.app.state, "producer_registry", None)
    request_shutdown = getattr(request.app.state, "request_shutdown", None)
    if (
        not isinstance(admission, MutationAdmission)
        or not isinstance(producer_registry, ProducerRegistry)
        or not callable(request_shutdown)
    ):
        raise ApiError(
            503,
            "shutdown_unavailable",
            "Cooperative shutdown is not available",
            True,
        )

    run_manager = getattr(request.app.state, "run_manager", None)
    approval_manager = getattr(request.app.state, "approval_manager", None)

    def inspect_busy() -> ServerBusyState:
        active = run_manager.list_active() if run_manager is not None else []
        approvals = (
            approval_manager.list_pending()
            if approval_manager is not None
            else []
        )
        return ServerBusyState(
            active_run_ids=tuple(
                str(run["run_id"])
                for run in active
                if run.get("run_id") is not None
            ),
            pending_approval_ids=tuple(
                str(approval["id"])
                for approval in approvals
                if approval.get("id") is not None
            ),
        )

    try:
        await admission.close_if_idle(inspect_busy)
    except MutationAdmissionClosed as exc:
        raise ApiError(
            409,
            "server_shutting_down",
            "Server is already shutting down",
            True,
        ) from exc
    except ServerBusyError as exc:
        raise ApiError(
            409,
            "server_busy",
            "Server has active runs or pending approvals",
            True,
            {
                "active_run_ids": list(exc.state.active_run_ids),
                "pending_approval_ids": list(exc.state.pending_approval_ids),
            },
        ) from exc

    producer_registry.begin_close()
    request_shutdown()
    return ShutdownResponse(
        server_instance_id=identity.server_instance_id,
        status="stopping",
    )
