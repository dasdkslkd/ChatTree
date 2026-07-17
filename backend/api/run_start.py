from __future__ import annotations

import re
from typing import Annotated

from fastapi import Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.api.errors import GENERIC_5XX_MESSAGE, ApiError, ErrorEnvelope
from backend.core.runs import (
    RunIdempotencyConflictError,
    RunReferenceConversationMismatchError,
    RunReferenceNotFoundError,
    RunRequestFingerprintError,
    RunStartResult,
)
from backend.core.runs.start_service import (
    RunStartReservationError,
    RunStartSchedulingError,
    RunStartValidationError,
)


IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", re.ASCII)


class RunStartResponse(BaseModel):
    run_id: str
    created: bool
    status: str


def run_start_openapi_responses() -> dict[int, dict]:
    return {
        200: {"model": RunStartResponse},
        202: {"model": RunStartResponse},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        428: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    }


def require_idempotency_key(
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> str:
    raw_values = request.headers.getlist("Idempotency-Key")
    if not raw_values:
        raise ApiError(
            428,
            "idempotency_key_required",
            "缺少 Idempotency-Key",
            False,
        )
    if len(raw_values) != 1:
        raise ApiError(
            422,
            "invalid_idempotency_key",
            "Idempotency-Key 必须且只能出现一次",
            False,
        )
    raw_value = raw_values[0]
    # The raw ASGI list is authoritative so equal duplicate headers stay invalid.
    if (
        idempotency_key != raw_value
        or IDEMPOTENCY_KEY_RE.fullmatch(raw_value) is None
    ):
        raise ApiError(
            422,
            "invalid_idempotency_key",
            "Idempotency-Key 格式非法",
            False,
        )
    return raw_value


def run_start_response(result: RunStartResult) -> JSONResponse:
    body = RunStartResponse(
        run_id=result.run.run_id,
        created=result.created,
        status=result.run.status.value,
    )
    return JSONResponse(
        status_code=202 if result.created else 200,
        content=body.model_dump(mode="json"),
    )


def run_start_api_error(exc: Exception) -> ApiError:
    if isinstance(exc, RunIdempotencyConflictError):
        return ApiError(
            409,
            "idempotency_key_conflict",
            "Idempotency-Key 已用于不同请求",
            False,
            {"existing_run_id": exc.existing_run_id},
        )
    if isinstance(exc, (RunStartReservationError, RunStartSchedulingError)):
        return ApiError(
            500,
            "internal_error",
            GENERIC_5XX_MESSAGE,
            False,
            already_logged=True,
        )
    if isinstance(exc, RunReferenceNotFoundError):
        return ApiError(
            404,
            "run_reference_not_found",
            "请求引用的资源不存在",
            False,
            {
                "reference_kind": exc.reference_kind,
                "reference_id": exc.reference_id,
            },
        )
    if isinstance(exc, RunReferenceConversationMismatchError):
        return ApiError(
            422,
            "invalid_run_reference",
            "请求引用不属于当前会话",
            False,
            {
                "reference_kind": exc.reference_kind,
                "reference_id": exc.reference_id,
            },
        )
    if isinstance(exc, (RunRequestFingerprintError, RunStartValidationError)):
        return ApiError(422, "invalid_request", str(exc), False)
    raise exc
