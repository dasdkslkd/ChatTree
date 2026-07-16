from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import TypeAlias
from urllib.parse import quote, urljoin, urlsplit

import httpx
from fastapi import APIRouter
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from client_launcher.models import EndpointLease


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
ResolvedEndpoint: TypeAlias = Endpoint | EndpointLease | None
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

    def __init__(
        self,
        profile_id: str,
        connection_epoch: int | None = None,
    ) -> None:
        self.profile_id = profile_id
        self.connection_epoch = connection_epoch
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
        endpoint, connection_epoch, invalidated = await self._ready_endpoint(
            profile_id
        )
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
            raise ProxyUpstreamTransportError(
                profile_id,
                connection_epoch,
            ) from exc

        closer = _UpstreamCloser(upstream_response)
        return _ClosingStreamingResponse(
            _response_body(upstream_response, closer, invalidated),
            status_code=upstream_response.status_code,
            raw_headers=_response_headers(
                upstream_response.headers.raw,
                target_url=target_url,
                profile_id=profile_id,
            ),
            closer=closer,
        )

    async def _ready_endpoint(
        self,
        profile_id: str,
    ) -> tuple[Endpoint, int | None, asyncio.Event | None]:
        resolved = self._resolve_endpoint(profile_id)
        if inspect.isawaitable(resolved):
            resolved = await resolved
        if isinstance(resolved, EndpointLease):
            return (
                resolved.endpoint,
                resolved.connection_epoch,
                resolved.invalidated,
            )
        if resolved is None or (isinstance(resolved, str) and not resolved.strip()):
            raise ProxyEndpointUnavailable(profile_id)
        return resolved, None, None


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
    request_id = getattr(request.state, "request_id", None)
    canonical_request_id = (
        request_id
        if isinstance(request_id, str)
        and request_id
        and len(request_id) <= 128
        and request_id.isascii()
        else None
    )
    excluded = {b"content-length", b"host"}
    if canonical_request_id is not None:
        excluded.add(b"x-request-id")
    headers = _filtered_headers(
        request.scope.get("headers", ()),
        extra_excluded=excluded,
    )
    headers.append((b"host", target_url.netloc))
    if body or request.method.upper() in _CONTENT_LENGTH_METHODS:
        headers.append((b"content-length", str(len(body)).encode("ascii")))
    if canonical_request_id is not None:
        headers.append((b"x-request-id", canonical_request_id.encode("ascii")))
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


def _response_headers(
    raw_headers: Iterable[RawHeader],
    *,
    target_url: httpx.URL,
    profile_id: str,
) -> list[RawHeader]:
    headers = _filtered_headers(raw_headers)
    return [
        (
            name,
            _rewrite_location(value, target_url, profile_id)
            if name.lower() == b"location"
            else value,
        )
        for name, value in headers
    ]


def _rewrite_location(
    value: bytes,
    target_url: httpx.URL,
    profile_id: str,
) -> bytes:
    try:
        location = value.decode("latin-1")
        target = urlsplit(str(target_url))
        resolved = urlsplit(urljoin(str(target_url), location))
        target_port = target.port or (443 if target.scheme == "https" else 80)
        resolved_port = resolved.port or (
            443 if resolved.scheme == "https" else 80
        )
    except (UnicodeError, ValueError):
        return value

    same_origin = (
        resolved.scheme.lower() == target.scheme.lower()
        and (resolved.hostname or "").lower() == (target.hostname or "").lower()
        and resolved_port == target_port
    )
    if not same_origin or not (
        resolved.path == "/api/v1" or resolved.path.startswith("/api/v1/")
    ):
        return value

    rewritten = f"/p/{quote(profile_id, safe='')}{resolved.path}"
    if resolved.query:
        rewritten += f"?{resolved.query}"
    if resolved.fragment:
        rewritten += f"#{resolved.fragment}"
    try:
        return rewritten.encode("latin-1")
    except UnicodeEncodeError:
        return value


async def _response_body(
    response: httpx.Response,
    closer: _UpstreamCloser,
    invalidated: asyncio.Event | None = None,
) -> AsyncIterator[bytes]:
    try:
        if invalidated is not None and invalidated.is_set():
            return
        if response.is_stream_consumed:
            if response.content:
                yield response.content
            return
        if invalidated is None:
            async for chunk in response.aiter_raw():
                yield chunk
            return

        iterator = response.aiter_raw().__aiter__()
        invalidated_task = asyncio.create_task(invalidated.wait())
        next_chunk_task: asyncio.Task[bytes] | None = None
        try:
            while True:
                next_chunk_task = asyncio.create_task(anext(iterator))
                done, _ = await asyncio.wait(
                    (next_chunk_task, invalidated_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if invalidated_task in done:
                    next_chunk_task.cancel()
                    await asyncio.gather(next_chunk_task, return_exceptions=True)
                    return
                try:
                    chunk = next_chunk_task.result()
                except StopAsyncIteration:
                    return
                next_chunk_task = None
                yield chunk
        finally:
            pending = [
                task
                for task in (next_chunk_task, invalidated_task)
                if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await closer()
