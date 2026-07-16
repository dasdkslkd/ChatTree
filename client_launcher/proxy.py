from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import TypeAlias

import httpx
from fastapi import APIRouter
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


DEFAULT_MAX_BODY_BYTES = 64 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 60.0

_PROXY_METHODS = (
    "DELETE",
    "GET",
    "HEAD",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
    "TRACE",
)
_CONTENT_LENGTH_METHODS = frozenset({"PATCH", "POST", "PUT"})
_HOP_BY_HOP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)

Endpoint: TypeAlias = str | httpx.URL
ResolvedEndpoint: TypeAlias = Endpoint | None
EndpointResolverResult: TypeAlias = ResolvedEndpoint | Awaitable[ResolvedEndpoint]
EndpointResolver: TypeAlias = Callable[[str], EndpointResolverResult]
RawHeader: TypeAlias = tuple[bytes, bytes]


class ProxyError(RuntimeError):
    code = "proxy_error"
    status_code = 502
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProxyEndpointUnavailable(ProxyError):
    code = "profile_not_ready"
    status_code = 503
    retryable = True

    def __init__(self, profile_id: str) -> None:
        self.profile_id = profile_id
        super().__init__(f"Profile '{profile_id}' does not have a ready endpoint")


class ProxyRequestBodyTooLarge(ProxyError):
    code = "request_body_too_large"
    status_code = 413
    retryable = False

    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        super().__init__(f"Request body exceeds the {limit_bytes}-byte limit")


class ProxyUpstreamTransportError(ProxyError):
    code = "proxy_upstream_unavailable"
    status_code = 502
    retryable = True

    def __init__(self, profile_id: str) -> None:
        self.profile_id = profile_id
        super().__init__(f"Unable to reach the Server for profile '{profile_id}'")


class _UpstreamCloser:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._called = False

    async def __call__(self) -> None:
        if self._called:
            return
        self._called = True
        await self._response.aclose()


class _ClosingStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        status_code: int,
        raw_headers: list[RawHeader],
        closer: _UpstreamCloser,
    ) -> None:
        self._closer = closer
        super().__init__(
            content,
            status_code=status_code,
            background=BackgroundTask(closer),
        )
        self.raw_headers = raw_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._closer()


class ProxyHandler:
    """Forward requests to endpoints returned for ready profile sessions.

    The resolver is the readiness boundary: it must return an endpoint only while
    the profile is ready and return ``None`` for every other session state. Both
    synchronous and asynchronous resolvers are accepted.
    """

    def __init__(
        self,
        resolve_endpoint: EndpointResolver,
        http_client: httpx.AsyncClient,
        *,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        if max_body_bytes < 0:
            raise ValueError("max_body_bytes must be non-negative")
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")
        if read_timeout <= 0:
            raise ValueError("read_timeout must be positive")

        self._resolve_endpoint = resolve_endpoint
        self._http_client = http_client
        self._max_body_bytes = max_body_bytes
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=connect_timeout,
            pool=connect_timeout,
        )

    async def __call__(
        self,
        request: Request,
        profile_id: str,
        path: str,
    ) -> StreamingResponse:
        endpoint = await self._ready_endpoint(profile_id)
        target_url = _target_url(endpoint, request, profile_id, path)
        body = await _read_bounded_body(request, self._max_body_bytes)
        request_headers = _request_headers(request, target_url, body)
        upstream_request = httpx.Request(
            request.method,
            target_url,
            headers=request_headers,
            content=body,
            extensions={"timeout": self._timeout.as_dict()},
        )

        try:
            upstream_response = await self._http_client.send(
                upstream_request,
                stream=True,
                follow_redirects=False,
            )
        except httpx.TransportError as exc:
            raise ProxyUpstreamTransportError(profile_id) from exc

        closer = _UpstreamCloser(upstream_response)
        return _ClosingStreamingResponse(
            _response_body(upstream_response, closer),
            status_code=upstream_response.status_code,
            raw_headers=_filtered_headers(upstream_response.headers.raw),
            closer=closer,
        )

    async def _ready_endpoint(self, profile_id: str) -> Endpoint:
        resolved = self._resolve_endpoint(profile_id)
        if inspect.isawaitable(resolved):
            resolved = await resolved
        if resolved is None or (isinstance(resolved, str) and not resolved.strip()):
            raise ProxyEndpointUnavailable(profile_id)
        return resolved


def create_proxy_router(
    resolve_endpoint: EndpointResolver,
    http_client: httpx.AsyncClient,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
) -> APIRouter:
    handler = ProxyHandler(
        resolve_endpoint,
        http_client,
        max_body_bytes=max_body_bytes,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )
    router = APIRouter()

    async def proxy_request(
        request: Request,
        profile_id: str,
        path: str,
    ) -> StreamingResponse:
        return await handler(request, profile_id, path)

    router.add_api_route(
        "/p/{profile_id}/api/v1/{path:path}",
        proxy_request,
        methods=list(_PROXY_METHODS),
        name="proxy_server_api",
    )
    return router


async def _read_bounded_body(request: Request, limit_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > limit_bytes:
            raise ProxyRequestBodyTooLarge(limit_bytes)

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit_bytes:
            raise ProxyRequestBodyTooLarge(limit_bytes)
        body.extend(chunk)
    return bytes(body)


def _target_url(
    endpoint: Endpoint,
    request: Request,
    profile_id: str,
    path: str,
) -> httpx.URL:
    try:
        base_url = httpx.URL(endpoint)
    except (TypeError, httpx.InvalidURL) as exc:
        raise ProxyEndpointUnavailable(profile_id) from exc

    if base_url.scheme not in {"http", "https"} or not base_url.host:
        raise ProxyEndpointUnavailable(profile_id)

    marker = b"/api/v1/"
    incoming_raw_path = request.scope.get("raw_path")
    marker_index = (
        incoming_raw_path.find(marker)
        if isinstance(incoming_raw_path, bytes)
        else -1
    )
    if marker_index >= 0:
        target_raw_path = incoming_raw_path[marker_index:]
    else:
        target_raw_path = httpx.URL(f"/api/v1/{path}").raw_path

    query = request.scope.get("query_string", b"")
    if query:
        target_raw_path += b"?" + query
    return base_url.copy_with(raw_path=target_raw_path)


def _request_headers(
    request: Request,
    target_url: httpx.URL,
    body: bytes,
) -> list[RawHeader]:
    headers = _filtered_headers(
        request.scope.get("headers", ()),
        extra_excluded={b"content-length", b"host"},
    )
    headers.append((b"host", target_url.netloc))
    if body or request.method.upper() in _CONTENT_LENGTH_METHODS:
        headers.append((b"content-length", str(len(body)).encode("ascii")))
    if not any(name.lower() == b"x-request-id" for name, _value in headers):
        request_id = getattr(request.state, "request_id", None)
        if isinstance(request_id, str) and request_id.isascii():
            headers.append((b"x-request-id", request_id.encode("ascii")))
    return headers


def _filtered_headers(
    raw_headers: Iterable[RawHeader],
    *,
    extra_excluded: set[bytes] | None = None,
) -> list[RawHeader]:
    headers = list(raw_headers)
    excluded = set(_HOP_BY_HOP_HEADERS)
    if extra_excluded:
        excluded.update(extra_excluded)

    for name, value in headers:
        if name.lower() != b"connection":
            continue
        excluded.update(
            token.strip().lower()
            for token in value.split(b",")
            if token.strip()
        )

    return [
        (name, value)
        for name, value in headers
        if name.lower() not in excluded
    ]


async def _response_body(
    response: httpx.Response,
    closer: _UpstreamCloser,
) -> AsyncIterator[bytes]:
    try:
        if response.is_stream_consumed:
            if response.content:
                yield response.content
            return
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await closer()
