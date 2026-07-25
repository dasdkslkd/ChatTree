import asyncio

from backend.core.config.types import StreamStatus
from backend.core.model.providers.anthropic_provider import AnthropicHTTPError, AnthropicProvider
from backend.core.model.providers.gemini_provider import GeminiHTTPError, GeminiProvider
from backend.core.model.providers.openai_compatible import OpenAICompatibleProvider, ProviderHTTPError


def _retry_config(max_request_retries=2, max_stream_retries=1):
    return {
        "api_key": "test",
        "retry": {
            "max_request_retries": max_request_retries,
            "max_stream_retries": max_stream_retries,
            "base_delay_seconds": 0,
            "max_delay_seconds": 0,
            "jitter_fraction": 0,
        },
    }


def test_openai_non_stream_retries_503_then_succeeds(monkeypatch):
    provider = OpenAICompatibleProvider(_retry_config(max_request_retries=2))
    calls = []

    def fake_request_json(path, body, timeout=120):
        calls.append((path, body))
        if len(calls) < 3:
            raise ProviderHTTPError(503, "busy")
        return {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 7}}

    monkeypatch.setattr(provider, "_request_json", fake_request_json)

    content, tokens = provider.generate_response("demo", [{"role": "user", "content": "hi"}])

    assert content == "ok"
    assert tokens == 7
    assert len(calls) == 3


def test_openai_non_stream_does_not_retry_400(monkeypatch):
    provider = OpenAICompatibleProvider(_retry_config(max_request_retries=2))
    calls = []

    def fake_request_json(path, body, timeout=120):
        calls.append(path)
        raise ProviderHTTPError(400, "bad request")

    monkeypatch.setattr(provider, "_request_json", fake_request_json)

    try:
        provider.generate_response("demo", [{"role": "user", "content": "hi"}])
    except ProviderHTTPError as exc:
        assert exc.status == 400
    else:
        raise AssertionError("400 should not be retried or swallowed")

    assert len(calls) == 1


def test_openai_stream_retries_before_any_output(monkeypatch):
    provider = OpenAICompatibleProvider(_retry_config(max_stream_retries=1))
    calls = []

    async def fake_iter_sse_events(path, body, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise ProviderHTTPError(503, "busy")
        yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]}

    monkeypatch.setattr(provider, "_iter_sse_events", fake_iter_sse_events)

    async def run():
        return [
            chunk
            async for chunk in provider.generate_response_stream(
                "demo",
                [{"role": "user", "content": "hi"}],
            )
        ]

    chunks = asyncio.run(run())

    assert len(calls) == 2
    assert [chunk["status"] for chunk in chunks] == [StreamStatus.CONTENT, StreamStatus.COMPLETE]
    assert chunks[0]["content"] == "ok"


def test_openai_stream_does_not_retry_after_output(monkeypatch):
    provider = OpenAICompatibleProvider(_retry_config(max_stream_retries=1))
    calls = []

    async def fake_iter_sse_events(path, body, **kwargs):
        calls.append(path)
        yield {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}
        raise ProviderHTTPError(503, "dropped")

    monkeypatch.setattr(provider, "_iter_sse_events", fake_iter_sse_events)

    async def run():
        return [
            chunk
            async for chunk in provider.generate_response_stream(
                "demo",
                [{"role": "user", "content": "hi"}],
            )
        ]

    chunks = asyncio.run(run())

    assert len(calls) == 1
    assert chunks[-1]["status"] == StreamStatus.ERROR
    assert "503" in chunks[-1]["error"]


def test_openai_responses_stream_retries_before_any_output(monkeypatch):
    config = _retry_config(max_stream_retries=1)
    config["api_format"] = "responses"
    provider = OpenAICompatibleProvider(config)
    calls = []

    async def fake_iter_sse_events(path, body, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise ProviderHTTPError(503, "busy")
        yield {"type": "response.output_text.delta", "delta": "responses ok"}

    monkeypatch.setattr(provider, "_iter_sse_events", fake_iter_sse_events)

    async def run():
        return [
            chunk
            async for chunk in provider.generate_response_stream(
                "demo",
                [{"role": "user", "content": "hi"}],
            )
        ]

    chunks = asyncio.run(run())

    assert len(calls) == 2
    assert chunks[0]["content"] == "responses ok"
    assert chunks[-1]["status"] == StreamStatus.COMPLETE


def test_gemini_non_stream_uses_shared_retry_policy(monkeypatch):
    provider = GeminiProvider(_retry_config(max_request_retries=1))
    calls = []

    def fake_request_json(path, body, timeout=120):
        calls.append(path)
        if len(calls) == 1:
            raise GeminiHTTPError(503, "busy")
        return {
            "candidates": [{"content": {"parts": [{"text": "gemini ok"}]}}],
            "usageMetadata": {"total_token_count": 4},
        }

    monkeypatch.setattr(provider, "_request_json", fake_request_json)

    content, tokens = provider.generate_response("gemini-demo", [{"role": "user", "content": "hi"}])

    assert content == "gemini ok"
    assert tokens == 4
    assert len(calls) == 2


def test_anthropic_non_stream_uses_shared_retry_policy(monkeypatch):
    provider = AnthropicProvider(_retry_config(max_request_retries=1))
    calls = []

    def fake_http_post(path, body):
        calls.append(path)
        if len(calls) == 1:
            raise AnthropicHTTPError(529, "overloaded")
        return {"content": [{"type": "text", "text": "anthropic ok"}], "usage": {"input_tokens": 1, "output_tokens": 2}}

    monkeypatch.setattr(provider, "_http_post", fake_http_post)

    content, tokens = provider.generate_response("claude-demo", [{"role": "user", "content": "hi"}])

    assert content == "anthropic ok"
    assert tokens == 3
    assert len(calls) == 2
