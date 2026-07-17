from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from http import HTTPStatus
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger(__name__)

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", re.ASCII)
GENERIC_5XX_MESSAGE = "服务暂时不可用，请稍后重试"

_SAFE_EXCEPTION_HEADERS = frozenset(
    {"allow", "retry-after", "www-authenticate", "etag"}
)


def _validate_json_value(value: object, path: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError(f"{path} must contain only finite numbers")
    if isinstance(value, list):
        return [
            _validate_json_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} must contain only string keys")
            normalized[key] = _validate_json_value(item, f"{path}.{key}")
        return normalized
    raise ValueError(f"{path} must contain only JSON-compatible values")


def validate_json_object(
    details: dict[str, object] | None,
) -> dict[str, object] | None:
    if details is None:
        return None
    if not isinstance(details, dict):
        raise ValueError("details must be a JSON object")
    normalized = _validate_json_value(details, "details")
    if not normalized:
        return None
    return normalized  # type: ignore[return-value]


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = validate_json_object(details)


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool
    request_id: str
    details: dict[str, object] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def canonical_request_id(value: str | None) -> str:
    if value is not None and REQUEST_ID_RE.fullmatch(value):
        return value
    return uuid4().hex


def _request_id(request: Request) -> str:
    request_id = canonical_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("x-request-id")
    )
    request.state.request_id = request_id
    return request_id


def _safe_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {
        name: value
        for name, value in headers.items()
        if name.lower() in _SAFE_EXCEPTION_HEADERS
    }


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    details: dict[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    request_id = _request_id(request)
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            retryable=retryable,
            request_id=request_id,
            details=validate_json_object(details),
        )
    )
    response_headers = _safe_headers(headers)
    if request.method == "HEAD":
        response: Response = Response(
            content=b"",
            status_code=status_code,
            headers=response_headers,
            media_type="application/json",
        )
    else:
        response = JSONResponse(
            content=envelope.model_dump(exclude_none=True),
            status_code=status_code,
            headers=response_headers,
        )
    response.headers["X-Request-ID"] = request_id
    return response


def _status_contract(status_code: int) -> tuple[str, bool]:
    if status_code == 400:
        return "invalid_request", False
    if status_code == 405:
        return "method_not_allowed", False
    if status_code == 429:
        return "rate_limited", True
    if status_code in {502, 503, 504}:
        return "service_unavailable", True
    if 500 <= status_code < 600:
        return "internal_error", False
    return "http_error", False


def _legacy_http_message(status_code: int, detail: object) -> str:
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "HTTP error"


def _log_server_error(request: Request, exc: Exception) -> None:
    request_id = _request_id(request)
    logger.error(
        "request failed request_id=%s",
        request_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


async def _api_error_handler(request: Request, exc: ApiError) -> Response:
    message = exc.message
    details = exc.details
    if 500 <= exc.status_code < 600:
        _log_server_error(request, exc)
        message = GENERIC_5XX_MESSAGE
        details = None
    return error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=message,
        retryable=exc.retryable,
        details=details,
    )


async def _validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> Response:
    issues = []
    for issue in exc.errors():
        location = issue.get("loc", ())
        issues.append(
            {
                "path": ".".join(str(part) for part in location),
                "code": str(issue.get("type", "validation_error")),
                "message": "输入值无效",
            }
        )
    return error_response(
        request,
        status_code=422,
        code="invalid_request",
        message="请求参数无效",
        retryable=False,
        details={"issues": issues},
    )


async def _http_error_handler(request: Request, exc: HTTPException) -> Response:
    code, retryable = _status_contract(exc.status_code)
    message = _legacy_http_message(exc.status_code, exc.detail)
    if 500 <= exc.status_code < 600:
        _log_server_error(request, exc)
        message = GENERIC_5XX_MESSAGE
    return error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        retryable=retryable,
        headers=exc.headers,
    )


async def _unknown_error_handler(request: Request, exc: Exception) -> Response:
    _log_server_error(request, exc)
    return error_response(
        request,
        status_code=500,
        code="internal_error",
        message=GENERIC_5XX_MESSAGE,
        retryable=False,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(HTTPException, _http_error_handler)
    app.add_exception_handler(Exception, _unknown_error_handler)


class RequestBoundaryMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origins: tuple[str, ...] = (),
        allowed_origin_pattern: str | None = None,
    ) -> None:
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)
        self.allowed_origin_pattern = (
            re.compile(allowed_origin_pattern)
            if allowed_origin_pattern is not None
            else None
        )

    def _origin_allowed(self, origin: str) -> bool:
        if origin in self.allowed_origins:
            return True
        return bool(
            self.allowed_origin_pattern is not None
            and self.allowed_origin_pattern.fullmatch(origin)
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = canonical_request_id(headers.get("x-request-id"))
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        origin = headers.get("origin")
        if origin is not None and not self._origin_allowed(origin):
            response = error_response(
                Request(scope, receive=receive),
                status_code=403,
                code="origin_not_allowed",
                message="请求来源不受信任",
                retryable=False,
            )
            await response(scope, receive, send)
            return

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                current_request_id = state.get("request_id")
                response_request_id = canonical_request_id(
                    current_request_id
                    if isinstance(current_request_id, str)
                    else None
                )
                state["request_id"] = response_request_id
                message = dict(message)
                response_headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                response_headers.append(
                    (b"x-request-id", response_request_id.encode("ascii"))
                )
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, receive, send_with_request_id)
