from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from http import HTTPStatus
from uuid import uuid4

from fastapi import Request
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send


REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", re.ASCII)
GENERIC_5XX_MESSAGE = "服务暂时不可用，请稍后重试"

_SAFE_EXCEPTION_HEADERS = frozenset(
    {"allow", "retry-after", "www-authenticate", "etag"}
)
_TRUSTED_VALIDATION_ROOTS = frozenset(
    {"body", "query", "path", "header", "cookie"}
)
_HTTP_ERROR_CONTRACTS = {
    400: ("invalid_request", False),
    401: ("unauthorized", False),
    403: ("forbidden", False),
    404: ("not_found", False),
    405: ("method_not_allowed", False),
    409: ("conflict", False),
    410: ("gone", False),
    412: ("precondition_failed", False),
    413: ("payload_too_large", False),
    415: ("unsupported_media_type", False),
    422: ("invalid_request", False),
    428: ("precondition_required", False),
    429: ("rate_limited", True),
    500: ("internal_error", False),
    502: ("service_unavailable", True),
    503: ("service_unavailable", True),
    504: ("service_unavailable", True),
}


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool
    request_id: str
    details: dict[str, object] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def canonical_request_id(value: object) -> str:
    if isinstance(value, str) and REQUEST_ID_RE.fullmatch(value):
        return value
    return f"req_{uuid4().hex}"


def incoming_request_id(scope: Scope) -> str:
    values = [
        value
        for name, value in scope.get("headers", ())
        if name.lower() == b"x-request-id"
    ]
    if len(values) != 1:
        return canonical_request_id(None)
    try:
        candidate: object = values[0].decode("ascii")
    except UnicodeDecodeError:
        candidate = None
    return canonical_request_id(candidate)


def request_id_for(request: Request) -> str:
    state = request.scope.setdefault("state", {})
    if "request_id" in state:
        request_id = canonical_request_id(state["request_id"])
    else:
        request_id = incoming_request_id(request.scope)
    request.state.request_id = request_id
    return request_id


def _validate_json_value(
    value: object,
    path: str,
    active_containers: set[int],
) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError(f"{path} must contain only finite numbers")
    if isinstance(value, (list, tuple)):
        container_id = id(value)
        if container_id in active_containers:
            raise ValueError(f"{path} must not contain circular references")
        active_containers.add(container_id)
        try:
            return [
                _validate_json_value(item, f"{path}[{index}]", active_containers)
                for index, item in enumerate(value)
            ]
        finally:
            active_containers.remove(container_id)
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_containers:
            raise ValueError(f"{path} must not contain circular references")
        active_containers.add(container_id)
        try:
            normalized: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{path} must contain only string keys")
                normalized[key] = _validate_json_value(
                    item,
                    f"{path}.{key}",
                    active_containers,
                )
            return normalized
        finally:
            active_containers.remove(container_id)
    raise ValueError(f"{path} must contain only JSON-compatible values")


def validate_json_object(
    details: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if details is None:
        return None
    normalized = _validate_json_value(details, "details", set())
    if not normalized:
        return None
    return normalized  # type: ignore[return-value]


def safe_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {
        name: value
        for name, value in headers.items()
        if name.lower() in _SAFE_EXCEPTION_HEADERS
    }


def status_contract(status_code: int) -> tuple[str, bool]:
    if status_code in _HTTP_ERROR_CONTRACTS:
        return _HTTP_ERROR_CONTRACTS[status_code]
    if 500 <= status_code < 600:
        return "internal_error", False
    return "http_error", False


def http_exception_message(status_code: int, detail: object) -> str:
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, Mapping):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "HTTP error"


def safe_validation_path(location: object) -> str:
    if not isinstance(location, (list, tuple)):
        return "*"
    safe_parts: list[str] = []
    for index, part in enumerate(location):
        if (
            index == 0
            and isinstance(part, str)
            and part in _TRUSTED_VALIDATION_ROOTS
        ):
            safe_parts.append(part)
        elif isinstance(part, int) and not isinstance(part, bool):
            safe_parts.append(str(part))
        else:
            safe_parts.append("*")
    return ".".join(safe_parts) or "*"


def build_error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    details: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
    logger: logging.Logger | None = None,
    log_label: str = "request failed",
    log_reason: object | None = None,
    cause: Exception | None = None,
    include_traceback: bool = False,
    log_server_error: bool = True,
) -> Response:
    request_id = request_id_for(request)
    if 500 <= status_code < 600:
        if logger is not None and log_server_error:
            logger.error(
                "%s request_id=%s reason=%s",
                log_label,
                request_id,
                message if log_reason is None else log_reason,
                exc_info=(type(cause), cause, cause.__traceback__)
                if include_traceback and cause is not None
                else None,
            )
        message = GENERIC_5XX_MESSAGE
        details = None

    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            retryable=retryable,
            request_id=request_id,
            details=validate_json_object(details),
        )
    )
    response_headers = safe_headers(headers)
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
    )


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

        request_id = incoming_request_id(scope)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        origin = Headers(scope=scope).get("origin")
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
                response_request_id = canonical_request_id(state.get("request_id"))
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
