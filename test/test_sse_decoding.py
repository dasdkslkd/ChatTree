import asyncio

import pytest

from backend.core.model.providers.retry import RetryableHTTPError
from backend.core.model.providers.sse import iter_sse_lines


class FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, lines):
        self.lines = lines

    def aiter_lines(self):
        return self.lines()

    async def aclose(self):
        pass


class FakeClient:
    response = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def build_request(self, *_args, **_kwargs):
        return object()

    async def send(self, *_args, **_kwargs):
        return self.response


def _config(**overrides):
    return {
        "model_transport": {
            "connect_timeout_seconds": 1,
            "first_event_timeout_seconds": 1,
            "stream_idle_timeout_seconds": 1,
            **overrides,
        }
    }


def test_first_sse_event_uses_its_own_timeout(monkeypatch):
    async def lines():
        await asyncio.sleep(3600)
        yield ""

    FakeClient.response = FakeResponse(lines)
    monkeypatch.setattr("backend.core.model.providers.sse.httpx.AsyncClient", FakeClient)

    async def run():
        stream = iter_sse_lines(
            "https://example.test/stream",
            {},
            {},
            _config(first_event_timeout_seconds=0.01),
            RetryableHTTPError,
        )
        with pytest.raises(TimeoutError, match="first SSE event"):
            await anext(stream)

    asyncio.run(run())


def test_sse_switches_to_idle_timeout_after_first_line(monkeypatch):
    async def lines():
        yield 'data: {"ok":true}'
        await asyncio.sleep(3600)
        yield ""

    FakeClient.response = FakeResponse(lines)
    monkeypatch.setattr("backend.core.model.providers.sse.httpx.AsyncClient", FakeClient)

    async def run():
        stream = iter_sse_lines(
            "https://example.test/stream",
            {},
            {},
            _config(stream_idle_timeout_seconds=0.01),
            RetryableHTTPError,
        )
        assert await anext(stream) == 'data: {"ok":true}'
        with pytest.raises(TimeoutError, match="SSE idle"):
            await anext(stream)

    asyncio.run(run())
