from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from client_launcher.http_errors import REQUEST_ID_RE, RequestBoundaryMiddleware
from client_launcher.models import EndpointLease
from client_launcher.proxy import (
    CONNECTION_LEASE_HEADER,
    ProxyEndpointUnavailable,
    ProxyHandler,
    ProxyRequestBodyTooLarge,
    ProxyStaleConnectionEpoch,
    ProxyUpstreamTransportError,
    create_proxy_router,
)


PROFILE_ID = "local"
SERVER_A = "11111111-1111-4111-8111-111111111111"
LEASE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LEASE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _request(
    method: str,
    target: str,
    *,
    chunks: tuple[bytes, ...] = (b"",),
    headers: tuple[tuple[bytes, bytes], ...] = (),
    receive_override: Callable[[], Awaitable[dict[str, Any]]] | None = None,
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
    return Request(scope, receive_override or receive)


def _lease(
    *,
    endpoint: str = "http://upstream.test",
    invalidated: asyncio.Event | None = None,
) -> EndpointLease:
    return EndpointLease(
        endpoint=endpoint,
        profile_id=PROFILE_ID,
        server_instance_id=SERVER_A,
        connection_epoch=7,
        connection_lease_id=LEASE_A,
        invalidated=invalidated or asyncio.Event(),
    )


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
        app.add_middleware(RequestBoundaryMiddleware)

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


def test_invalid_resolved_endpoint_error_carries_captured_connection_lease() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None))
        handler = ProxyHandler(
            lambda _profile_id: _lease(endpoint="not-an-http-endpoint"),
            http_client=client,
            require_connection_lease=True,
        )
        request = _request(
            "GET",
            "/p/local/api/v1/health",
            headers=((b"x-chattree-connection-lease-id", LEASE_A.encode("ascii")),),
        )

        with pytest.raises(ProxyEndpointUnavailable) as captured:
            await handler(request, PROFILE_ID, "health")

        await client.aclose()
        assert captured.value.connection_lease_id == LEASE_A

    _run(scenario())


@pytest.mark.parametrize(
    "lease_headers",
    (
        (
            (b"x-chattree-connection-lease-id", LEASE_A.encode("ascii")),
            (b"X-ChatTree-Connection-Lease-ID", LEASE_A.encode("ascii")),
        ),
        ((b"x-chattree-connection-lease-id", f"{LEASE_A},{LEASE_A}".encode("ascii")),),
        ((b"x-chattree-connection-lease-id", LEASE_A.upper().encode("ascii")),),
        ((b"x-chattree-connection-lease-id", b"not-a-uuid"),),
        ((b"x-chattree-connection-lease-id", b"\xff"),),
        ((b"x-chattree-connection-lease-id", LEASE_B.encode("ascii")),),
    ),
    ids=("duplicate", "comma-joined", "noncanonical", "invalid", "non-ascii", "stale"),
)
def test_proxy_rejects_invalid_raw_lease_header_before_body_or_upstream(
    lease_headers: tuple[tuple[bytes, bytes], ...],
) -> None:
    async def scenario() -> None:
        body_reads = 0
        upstream_calls = 0

        async def receive() -> dict[str, Any]:
            nonlocal body_reads
            body_reads += 1
            return {"type": "http.request", "body": b"payload", "more_body": False}

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal upstream_calls
            upstream_calls += 1
            return httpx.Response(204)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: _lease(),
            http_client=client,
            require_connection_lease=False,
        )
        request = _request(
            "POST",
            "/p/local/api/v1/upload",
            headers=lease_headers,
            receive_override=receive,
        )

        with pytest.raises(ProxyStaleConnectionEpoch) as captured:
            await handler(request, PROFILE_ID, "upload")

        await client.aclose()
        assert captured.value.code == "stale_connection_epoch"
        assert captured.value.status_code == 409
        assert captured.value.retryable is False
        assert captured.value.details == {"current_connection_epoch": 7}
        assert captured.value.connection_lease_id == LEASE_A
        assert body_reads == 0
        assert upstream_calls == 0

    _run(scenario())


def test_proxy_matching_lease_is_stripped_and_response_spoof_is_replaced() -> None:
    async def scenario() -> None:
        resolve_calls = 0
        upstream_lease_headers: list[str] = []
        upstream_idempotency_keys: list[str] = []

        def resolve_endpoint(profile_id: str) -> EndpointLease:
            nonlocal resolve_calls
            resolve_calls += 1
            assert profile_id == PROFILE_ID
            return _lease()

        async def upstream(request: httpx.Request) -> httpx.Response:
            upstream_lease_headers.extend(
                request.headers.get_list("x-chattree-connection-lease-id")
            )
            upstream_idempotency_keys.extend(
                request.headers.get_list("idempotency-key")
            )
            return httpx.Response(
                409,
                headers=[
                    (CONNECTION_LEASE_HEADER, LEASE_B),
                    (CONNECTION_LEASE_HEADER, LEASE_B),
                    ("X-Upstream", "kept"),
                ],
                content=b"backend-error",
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            resolve_endpoint,
            http_client=client,
            require_connection_lease=True,
        )
        request = _request(
            "POST",
            "/p/local/api/v1/widgets",
            chunks=(b"{}",),
            headers=(
                (b"x-chattree-connection-lease-id", LEASE_A.encode("ascii")),
                (b"idempotency-key", b"idem-tree-1"),
            ),
        )

        response = await handler(request, PROFILE_ID, "widgets")
        body = b"".join([chunk async for chunk in response.body_iterator])
        response_leases = [
            value.decode("ascii")
            for name, value in response.raw_headers
            if name.lower() == b"x-chattree-connection-lease-id"
        ]
        if response.background is not None:
            await response.background()
        await client.aclose()

        assert resolve_calls == 1
        assert upstream_lease_headers == []
        assert upstream_idempotency_keys == ["idem-tree-1"]
        assert response.status_code == 409
        assert body == b"backend-error"
        assert [
            value
            for name, value in response.raw_headers
            if name.lower() == b"x-upstream"
        ] == [b"kept"]
        assert response_leases == [LEASE_A]

    _run(scenario())


def test_proxy_missing_lease_is_only_allowed_by_explicit_transition_switch() -> None:
    async def scenario() -> None:
        upstream_calls = 0
        body_reads = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal upstream_calls
            upstream_calls += 1
            return httpx.Response(204)

        async def receive() -> dict[str, Any]:
            nonlocal body_reads
            body_reads += 1
            return {"type": "http.request", "body": b"", "more_body": False}

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        transitional = ProxyHandler(
            lambda _profile_id: _lease(),
            http_client=client,
            require_connection_lease=False,
        )
        allowed = await transitional(
            _request("GET", "/p/local/api/v1/health"),
            PROFILE_ID,
            "health",
        )
        if allowed.background is not None:
            await allowed.background()

        strict = ProxyHandler(
            lambda _profile_id: _lease(),
            http_client=client,
            require_connection_lease=True,
        )
        with pytest.raises(ProxyStaleConnectionEpoch) as captured:
            await strict(
                _request(
                    "POST",
                    "/p/local/api/v1/upload",
                    receive_override=receive,
                ),
                PROFILE_ID,
                "upload",
            )

        await client.aclose()
        assert upstream_calls == 1
        assert body_reads == 0
        assert captured.value.details == {"current_connection_epoch": 7}

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


def test_proxy_replaces_incoming_request_id_with_launcher_canonical_id() -> None:
    async def scenario() -> None:
        seen_request_ids: list[str] = []

        async def upstream(request: httpx.Request) -> httpx.Response:
            seen_request_ids.extend(request.headers.get_list("x-request-id"))
            return httpx.Response(204)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: "http://upstream.test",
            http_client=client,
        )
        request = _request(
            "GET",
            "/p/local/api/v1/health",
            headers=((b"x-request-id", b"invalid-or-overlong"),),
        )
        request.state.request_id = "req-canonical"
        response = await handler(request, "local", "health")
        iterator = response.body_iterator.__aiter__()
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        if response.background is not None:
            await response.background()

        await client.aclose()
        assert seen_request_ids == ["req-canonical"]

    _run(scenario())


@pytest.mark.parametrize(
    "incoming_headers",
    (
        (("X-Request-ID", "not valid!"),),
        (
            ("X-Request-ID", "first-valid"),
            ("X-Request-ID", "second-valid"),
        ),
    ),
    ids=("invalid", "duplicate"),
)
def test_proxy_sends_one_canonical_id_and_preserves_idempotency_key(
    incoming_headers: tuple[tuple[str, str], ...],
) -> None:
    async def scenario() -> None:
        seen_request_ids: list[str] = []
        seen_idempotency_keys: list[str] = []

        async def upstream(request: httpx.Request) -> httpx.Response:
            seen_request_ids.extend(request.headers.get_list("x-request-id"))
            seen_idempotency_keys.extend(
                request.headers.get_list("idempotency-key")
            )
            return httpx.Response(204)

        upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream)
        )
        inner = FastAPI()
        inner.include_router(
            create_proxy_router(
                lambda _profile_id: "http://upstream.test",
                upstream_client,
            )
        )
        app = RequestBoundaryMiddleware(inner)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://launcher.test",
        ) as launcher_client:
            response = await launcher_client.get(
                "/p/local/api/v1/health",
                headers=[
                    *incoming_headers,
                    ("Idempotency-Key", "idem-tree-1"),
                ],
            )

        await upstream_client.aclose()
        assert len(seen_request_ids) == 1
        assert REQUEST_ID_RE.fullmatch(seen_request_ids[0])
        assert seen_request_ids[0].startswith("req_")
        assert seen_request_ids[0] not in {
            value for name, value in incoming_headers if name == "X-Request-ID"
        }
        assert seen_idempotency_keys == ["idem-tree-1"]
        assert response.headers.get_list("X-Request-ID") == seen_request_ids

    _run(scenario())


def test_proxy_passes_upstream_error_envelope_through_byte_for_byte() -> None:
    async def scenario() -> None:
        upstream_body = (
            b'{"error":{"code":"active_runs_present","message":"blocked",'
            b'"retryable":true,"request_id":"proxy-tree","details":'
            b'{"active_run_ids":["run-1","run-2"]}}}'
        )

        async def upstream(request: httpx.Request) -> httpx.Response:
            assert request.headers.get_list("x-request-id") == ["proxy-tree"]
            return httpx.Response(
                409,
                headers=[
                    ("Content-Type", "application/json"),
                    ("X-Request-ID", "proxy-tree"),
                ],
                content=upstream_body,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: "http://upstream.test",
            http_client=client,
        )
        request = _request("DELETE", "/p/local/api/v1/conversations/branch")
        request.state.request_id = "proxy-tree"
        response = await handler(request, "local", "conversations/branch")
        body = b"".join([chunk async for chunk in response.body_iterator])
        upstream_request_ids = [
            value
            for name, value in response.raw_headers
            if name.lower() == b"x-request-id"
        ]
        if response.background is not None:
            await response.background()
        await client.aclose()

        assert response.status_code == 409
        assert body == upstream_body
        assert body.count(b'"error"') == 1
        assert b'"active_run_ids":["run-1","run-2"]' in body
        assert upstream_request_ids == [b"proxy-tree"]

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


def test_proxy_rechecks_endpoint_invalidation_after_slow_body() -> None:
    async def scenario() -> None:
        invalidated = asyncio.Event()
        waiting_for_final_chunk = asyncio.Event()
        release_final_chunk = asyncio.Event()
        upstream_calls = 0
        receive_calls = 0

        async def receive() -> dict[str, Any]:
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return {
                    "type": "http.request",
                    "body": b"first-",
                    "more_body": True,
                }
            waiting_for_final_chunk.set()
            await release_final_chunk.wait()
            return {
                "type": "http.request",
                "body": b"second",
                "more_body": False,
            }

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal upstream_calls
            upstream_calls += 1
            return httpx.Response(204)

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: _lease(invalidated=invalidated),
            http_client=client,
            require_connection_lease=True,
        )
        request = _request(
            "POST",
            "/p/local/api/v1/upload",
            headers=((b"x-chattree-connection-lease-id", LEASE_A.encode("ascii")),),
            receive_override=receive,
        )
        proxy_task = asyncio.create_task(handler(request, PROFILE_ID, "upload"))
        await asyncio.wait_for(waiting_for_final_chunk.wait(), timeout=0.25)

        invalidated.set()
        release_final_chunk.set()

        with pytest.raises(ProxyStaleConnectionEpoch) as captured:
            await asyncio.wait_for(proxy_task, timeout=0.25)

        await client.aclose()
        assert captured.value.details == {"expected_connection_epoch": 7}
        assert receive_calls == 2
        assert upstream_calls == 0

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


def test_proxy_send_delivers_first_sse_chunk_before_upstream_unblocks() -> None:
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
        request.state.request_id = "proxy-send-tree"
        response = await handler(request, "local", "runs/run-1/events")
        sent: list[dict[str, Any]] = []
        first_body_sent = asyncio.Event()
        never_disconnect = asyncio.Event()

        async def receive() -> dict[str, str]:
            await never_disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)
            if (
                message["type"] == "http.response.body"
                and message.get("body") == b"data: first\n\n"
            ):
                first_body_sent.set()

        response_task = asyncio.create_task(
            response(request.scope, receive, send)
        )
        await asyncio.wait_for(first_body_sent.wait(), timeout=0.25)

        assert not response_task.done()
        assert any(
            message["type"] == "http.response.start" for message in sent
        )
        assert any(
            message["type"] == "http.response.body"
            and message.get("body") == b"data: first\n\n"
            and message.get("more_body") is True
            for message in sent
        )

        stream.release.set()
        await asyncio.wait_for(response_task, timeout=0.25)
        assert stream.closed.is_set()
        assert stream.close_calls == 1
        await client.aclose()

    _run(scenario())


def test_proxy_stops_stream_when_endpoint_lease_is_invalidated() -> None:
    async def scenario() -> None:
        stream = _GatedStream()
        invalidated = asyncio.Event()

        async def upstream(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=stream,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: EndpointLease(
                endpoint="http://upstream.test",
                profile_id=PROFILE_ID,
                server_instance_id=SERVER_A,
                connection_epoch=1,
                connection_lease_id=LEASE_A,
                invalidated=invalidated,
            ),
            http_client=client,
        )
        request = _request("GET", "/p/local/api/v1/runs/run-1/events")
        response = await handler(request, "local", "runs/run-1/events")
        iterator = response.body_iterator.__aiter__()

        assert await asyncio.wait_for(anext(iterator), timeout=0.25) == (
            b"data: first\n\n"
        )
        second_task = asyncio.create_task(anext(iterator))
        await asyncio.wait_for(stream.waiting_for_release.wait(), timeout=0.25)
        invalidated.set()

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(second_task, timeout=0.25)
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
                profile_id=PROFILE_ID,
                server_instance_id=SERVER_A,
                connection_epoch=7,
                connection_lease_id=LEASE_A,
                invalidated=asyncio.Event(),
            ),
            http_client=client,
        )
        request = _request("GET", "/p/local/api/v1/health")

        with pytest.raises(ProxyUpstreamTransportError) as captured:
            await handler(request, "local", "health")

        await client.aclose()
        assert captured.value.connection_epoch == 7
        assert captured.value.connection_lease_id == LEASE_A

    _run(scenario())


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (
            "http://upstream.test/api/v1/conversations",
            "/p/local/api/v1/conversations",
        ),
        ("/api/v1/conversations?limit=5", "/p/local/api/v1/conversations?limit=5"),
        ("../../models", "/p/local/api/v1/models"),
        ("https://example.com/elsewhere", "https://example.com/elsewhere"),
    ],
)
def test_proxy_rewrites_same_upstream_redirects(
    location: str,
    expected: str,
) -> None:
    async def scenario() -> None:
        async def upstream(request: httpx.Request) -> httpx.Response:
            return httpx.Response(307, headers={"Location": location})

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        handler = ProxyHandler(
            lambda _profile_id: "http://upstream.test",
            http_client=client,
        )
        request = _request(
            "GET",
            "/p/local/api/v1/conversations/current/",
        )
        response = await handler(
            request,
            "local",
            "conversations/current/",
        )

        response_location = next(
            value.decode("latin-1")
            for name, value in response.raw_headers
            if name.lower() == b"location"
        )
        assert response_location == expected
        if response.background is not None:
            await response.background()
        await client.aclose()

    _run(scenario())
