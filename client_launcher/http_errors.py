from __future__ import annotations

import logging
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from chattree_protocol.http_errors import (
    GENERIC_5XX_MESSAGE,
    REQUEST_ID_RE,
    ErrorBody,
    ErrorEnvelope,
    RequestBoundaryMiddleware,
    build_error_response,
    canonical_request_id,
    http_exception_message,
    safe_validation_path,
    status_contract,
    validate_json_object,
)
from client_launcher.models import LauncherError


logger = logging.getLogger(__name__)


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    details: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    return build_error_response(
        request,
        status_code=status_code,
        code=code,
        message=message,
        retryable=retryable,
        details=details,
        headers=headers,
        logger=logger,
        log_label="launcher request failed",
    )


async def launcher_error_response(request: Request, exc: Exception) -> Response:
    raw_details = getattr(exc, "details", None)
    details = raw_details if isinstance(raw_details, Mapping) else None
    return build_error_response(
        request,
        status_code=int(getattr(exc, "status_code", 500)),
        code=str(getattr(exc, "code", "internal_error")),
        message=str(getattr(exc, "message", str(exc) or type(exc).__name__)),
        retryable=bool(getattr(exc, "retryable", False)),
        details=details,
        cause=exc,
        logger=logger,
        log_label="launcher request failed",
    )


async def validation_error_response(
    request: Request,
    exc: RequestValidationError,
) -> Response:
    issues = [
        {
            "path": safe_validation_path(issue.get("loc", ())),
            "code": "validation_error",
            "message": "输入值无效",
        }
        for issue in exc.errors()
    ]
    return build_error_response(
        request,
        status_code=422,
        code="invalid_request",
        message="请求参数无效",
        retryable=False,
        details={"issues": issues},
        logger=logger,
        log_label="launcher request failed",
    )


async def http_exception_response(
    request: Request,
    exc: HTTPException,
) -> Response:
    code, retryable = status_contract(exc.status_code)
    return build_error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=http_exception_message(exc.status_code, exc.detail),
        retryable=retryable,
        headers=exc.headers,
        log_reason=exc.detail,
        cause=exc,
        logger=logger,
        log_label="launcher request failed",
    )


async def unknown_exception_response(
    request: Request,
    exc: Exception,
) -> Response:
    return build_error_response(
        request,
        status_code=500,
        code="internal_error",
        message=str(exc) or type(exc).__name__,
        retryable=False,
        cause=exc,
        logger=logger,
        log_label="launcher request failed",
        include_traceback=True,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(LauncherError, launcher_error_response)
    app.add_exception_handler(RequestValidationError, validation_error_response)
    app.add_exception_handler(HTTPException, http_exception_response)
    app.add_exception_handler(Exception, unknown_exception_response)


__all__ = [
    "ErrorBody",
    "ErrorEnvelope",
    "GENERIC_5XX_MESSAGE",
    "REQUEST_ID_RE",
    "RequestBoundaryMiddleware",
    "canonical_request_id",
    "error_response",
    "install_error_handlers",
    "launcher_error_response",
    "unknown_exception_response",
    "validate_json_object",
    "validation_error_response",
]
