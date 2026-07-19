from __future__ import annotations

import logging

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


logger = logging.getLogger(__name__)


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    details: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
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
    )


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
        details: dict[str, object] | None = None,
        *,
        already_logged: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = validate_json_object(details)
        self.already_logged = bool(already_logged)


async def _api_error_handler(request: Request, exc: ApiError) -> Response:
    return build_error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        retryable=exc.retryable,
        details=exc.details,
        cause=exc,
        logger=logger,
        include_traceback=True,
        log_server_error=not exc.already_logged,
    )


async def _validation_error_handler(
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
        cause=exc,
        logger=logger,
    )


async def _http_error_handler(request: Request, exc: HTTPException) -> Response:
    code, retryable = status_contract(exc.status_code)
    return build_error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=http_exception_message(exc.status_code, exc.detail),
        retryable=retryable,
        headers=exc.headers,
        cause=exc,
        logger=logger,
        include_traceback=True,
    )


async def _unknown_error_handler(request: Request, exc: Exception) -> Response:
    return build_error_response(
        request,
        status_code=500,
        code="internal_error",
        message=str(exc) or type(exc).__name__,
        retryable=False,
        cause=exc,
        logger=logger,
        include_traceback=True,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(HTTPException, _http_error_handler)
    app.add_exception_handler(Exception, _unknown_error_handler)


__all__ = [
    "ApiError",
    "ErrorBody",
    "ErrorEnvelope",
    "GENERIC_5XX_MESSAGE",
    "REQUEST_ID_RE",
    "RequestBoundaryMiddleware",
    "canonical_request_id",
    "error_response",
    "install_error_handlers",
    "validate_json_object",
]
