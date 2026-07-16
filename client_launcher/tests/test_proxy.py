from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from client_launcher.models import EndpointLease
from client_launcher.proxy import (
    ProxyEndpointUnavailable,
    ProxyHandler,
    ProxyRequestBodyTooLarge,
    ProxyUpstreamTransportError,
    create_proxy_router,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _request(
    method: str,
    target: str,
    *,
    chunks: tuple[bytes, ...] = (b"",),
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> Request:
    path, _, query = target.partition("?")
    index = 0

    async def receive() -> dict[str, Any]:
        nonlocal index
        if index >= len(chunks):
            return {"type": "http.disconnect"}
        body = chunks[index]
        index += 1
        return {
            "type": "http.request",
            "body": body,
            "more_body": index < len(chunks),
        }

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "headers": list(headers),
        "client": ("127.0.0.1", 12345),
        "server": ("launcher.test", 80),
        "root_path": "",
    }
    return Request(scope, receive)


def test_proxy_preserves_http_semantics_and_filters_hop_by_hop_headers() -> None:
    async def scenario() -> None:
        seen: dict[str, Any] = {}

        async def upstream(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["raw_path"] = request.url.raw_path
            seen["headers"] = request.headers
            seen["body"] = await request.aread()
            seen["timeout"] = request.extensions["timeout"]
            return httpx.Response(
                207,
                headers=[
                    ("Content-Type", "application/json"),
                    ("X-Upstream", "kept"),
                    ("Connection", "X-Response-Hop, keep-alive"),
                    ("X-Response-Hop", "removed"),
                    ("Keep-Alive", "timeout=5"),
                    ("Set-Cookie", "one=1"),
                    ("Set-Cookie", "two=2"),
                ],
                content=b'{"ok":true}',
            )

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = FastAPI()

        async def resolve_endpoint(profile_id: str) -> str | None:
            return "http://upstream.test:8443" if profile_id == "local" else None

        app.include_router(
            create_proxy_router(
                resolve_endpoint,
                http_client=upstream_client,
                connect_timeout=1.25,
                read_timeout=9.5,
            )
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://launcher.test",
        ) as launcher_client:
            response = await launcher_client.post(
                "/p/local/api/v1/widgets?from_event=42&raw=a%2Fb",
                headers={
                    "Connection": "X-Request-Hop, keep-alive",
                    "Keep-Alive": "timeout=5",
                    "X-Request-Hop": "removed",
                    "Proxy-Authorization": "Basic hidden",
                    "Idempotency-Key": "idem-123",
                    "X-Request-ID": "req-123",
                    "X-Business": "kept",
                },
                content=b'{"value":1}',
            )

        await upstream_client.aclose()

        assert response.status_code == 207
        assert response.content == b'{"ok":true}'
        assert response.headers["x-upstream"] == "kept"
        assert response.headers.get_list("set-cookie") == ["one=1", "two=2"]
        assert "connection" not in response.headers
        assert "keep-alive" not in response.headers
        assert "x-response-hop" not in response.headers

        assert seen["method"] == "POST"
        assert seen["raw_path"] == b"/api/v1/widgets?from_event=42&raw=a%2Fb"
        assert seen["body"] == b'{"value":1}'
        assert seen["headers"]["host"] == "upstream.test:8443"
        assert seen["headers"]["content-length"] == str(len(b'{"value":1}'))
        assert seen["headers"]["idempotency-key"] == "idem-123"
        assert seen["headers"]["x-request-id"] == "req-123"
        assert seen["headers"]["x-business"] == "kept"
        assert "connection" not in seen["headers"]
        assert "keep-alive" not in seen["headers"]
        assert "x-request-hop" not in seen["headers"]
        assert "proxy-authorization" not in seen["headers"]
        assert seen["timeout"] == {
            "connect": 1.25,
            "read": 9.5,
            "write": 1.25,
            "pool": 1.25,
        }

    _run(scenario())


def test_proxy_rejects_non_ready_profile_without_contacting_upstream() -> None:
    async def scenario() -> None:
        calls = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(lambda _profile_id: None, http_client=client)
        request = _request("GET", "/p/offline/api/v1/health")

        with pytest.raises(ProxyEndpointUnavailable) as captured:
            await handler(request, "offline", "health")

        await client.aclose()
        assert captured.value.code == "profile_not_ready"
        assert captured.value.status_code == 503
        assert calls == 0

    _run(scenario())


def test_proxy_forwards_launcher_generated_request_id() -> None:
    async def scenario() -> None:
        seen_request_id: str | None = None

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal seen_request_id
            seen_request_id = request.headers.get("x-request-id")
            return httpx.Response(204)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: "http://upstream.test",
            http_client=client,
        )
        request = _request("GET", "/p/local/api/v1/health")
        request.state.request_id = "req-generated"
        response = await handler(request, "local", "health")
        iterator = response.body_iterator.__aiter__()
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        if response.background is not None:
            await response.background()

        await client.aclose()
        assert seen_request_id == "req-generated"

    _run(scenario())


def test_proxy_enforces_body_limit_while_reading_chunked_body() -> None:
    async def scenario() -> None:
        calls = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: "http://upstream.test",
            http_client=client,
            max_body_bytes=5,
        )
        request = _request(
            "POST",
            "/p/local/api/v1/upload",
            chunks=(b"abc", b"def"),
        )

        with pytest.raises(ProxyRequestBodyTooLarge) as captured:
            await handler(request, "local", "upload")

        await client.aclose()
        assert captured.value.code == "request_body_too_large"
        assert captured.value.status_code == 413
        assert captured.value.limit_bytes == 5
        assert calls == 0

    _run(scenario())


class _GatedStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.waiting_for_release = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"data: first\n\n"
        self.waiting_for_release.set()
        await self.release.wait()
        yield b"data: second\n\n"

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed.set()


def test_proxy_returns_first_sse_chunk_without_buffering_the_stream() -> None:
    async def scenario() -> None:
        stream = _GatedStream()

        async def upstream(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=stream,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: "http://upstream.test",
            http_client=client,
        )
        request = _request("GET", "/p/local/api/v1/runs/run-1/events")

        response = await asyncio.wait_for(
            handler(request, "local", "runs/run-1/events"),
            timeout=0.25,
        )
        iterator = response.body_iterator.__aiter__()
        first = await asyncio.wait_for(anext(iterator), timeout=0.25)
        second_task = asyncio.create_task(anext(iterator))
        await asyncio.wait_for(stream.waiting_for_release.wait(), timeout=0.25)

        assert first == b"data: first\n\n"
        assert not second_task.done()

        stream.release.set()
        assert await asyncio.wait_for(second_task, timeout=0.25) == b"data: second\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        if response.background is not None:
            await response.background()

        assert stream.closed.is_set()
        assert stream.close_calls == 1
        await client.aclose()

    _run(scenario())


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.never_release = asyncio.Event()
        self.close_calls = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await self.never_release.wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed.set()


def test_client_abort_closes_upstream_without_calling_stop() -> None:
    async def scenario() -> None:
        stream = _BlockingStream()
        calls: list[tuple[str, str]] = []

        async def upstream(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=stream,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: "http://upstream.test",
            http_client=client,
        )
        request = _request("GET", "/p/local/api/v1/runs/run-1/events")
        response = await handler(request, "local", "runs/run-1/events")
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, str]:
            await stream.started.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await asyncio.wait_for(response(request.scope, receive, send), timeout=0.5)

        assert any(message["type"] == "http.response.start" for message in sent)
        assert stream.closed.is_set()
        assert stream.close_calls == 1
        assert calls == [("GET", "/api/v1/runs/run-1/events")]
        assert all(not path.endswith("/stop") for _method, path in calls)
        await client.aclose()

    _run(scenario())


def test_transport_error_before_response_headers_is_typed() -> None:
    async def scenario() -> None:
        async def upstream(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: "http://upstream.test",
            http_client=client,
        )
        request = _request("GET", "/p/local/api/v1/health")

        with pytest.raises(ProxyUpstreamTransportError) as captured:
            await handler(request, "local", "health")

        await client.aclose()
        assert captured.value.code == "proxy_upstream_unavailable"
        assert captured.value.status_code == 502
        assert captured.value.retryable is True
        assert captured.value.connection_epoch is None
        assert isinstance(captured.value.__cause__, httpx.ConnectError)

    _run(scenario())


def test_transport_error_carries_resolved_connection_epoch() -> None:
    async def scenario() -> None:
        async def upstream(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: EndpointLease(
                endpoint="http://upstream.test",
                connection_epoch=7,
            ),
            http_client=client,
        )
        request = _request("GET", "/p/local/api/v1/health")

        with pytest.raises(ProxyUpstreamTransportError) as captured:
            await handler(request, "local", "health")

        await client.aclose()
        assert captured.value.connection_epoch == 7

    _run(scenario())
