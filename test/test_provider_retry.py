import asyncio
import io
import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock

from backend.core.config.types import ModelRoute, StreamStatus
from backend.core.model.providers.anthropic_provider import AnthropicHTTPError, AnthropicProvider
from backend.core.model.providers.gemini_provider import GeminiHTTPError, GeminiProvider
from backend.core.model.providers.openai_compatible import OpenAICompatibleProvider, ProviderHTTPError


def _retry_config(max_request_retries=2, max_stream_retries=1):
    return {
        "api_key": "test",
        "model_transport": {
            "max_request_retries": max_request_retries,
            "max_stream_retries": max_stream_retries,
            "retry_base_delay_seconds": 0,
            "retry_max_delay_seconds": 0,
            "retry_jitter_fraction": 0,
        },
    }


def _route(protocol: str) -> ModelRoute:
    endpoints = {
        "openai_chat_completions": "/chat/completions",
        "openai_responses": "/responses",
        "anthropic_messages": "/v1/messages",
        "gemini_generate_content": "/models/{model}:generateContent",
    }
    return ModelRoute(
        route_id=f"test:model:{protocol}",
        provider_id="test",
        model_id="model",
        protocol=protocol,
        endpoint=endpoints[protocol],
        reasoning_profile={
            "name": "test",
            "carrier": "responses_items" if protocol == "openai_responses" else "none",
            "history_policy": "provider_state" if protocol == "openai_responses" else "drop",
            "strict": protocol == "openai_responses",
            "controls": {},
        },
    )


def test_packy_chat_probe_falls_back_to_v1(monkeypatch):
    provider = OpenAICompatibleProvider(
        {"base_url": "https://cf.api.fan", "api_key": "test"},
        _route("openai_chat_completions"),
    )
    calls = []
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"choices": []}'

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "missing",
                {},
                io.BytesIO(b"not found"),
            )
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert provider._probe_chat_endpoint("kimi-k3") == "https://cf.api.fan/v1"
    assert [request.full_url for request in calls] == [
        "https://cf.api.fan/chat/completions",
        "https://cf.api.fan/v1/chat/completions",
    ]
    assert json.loads(calls[0].data)["messages"] == [
        {"role": "user", "content": "ping"},
    ]
    assert provider._url("/chat/completions") == "https://cf.api.fan/v1/chat/completions"


def test_packy_chat_probe_skips_server_error_address(monkeypatch):
    provider = OpenAICompatibleProvider(
        {"base_url": "https://cf.api.fan", "api_key": "test"},
        _route("openai_chat_completions"),
    )
    calls = []
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"choices": []}'

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "server error",
                {},
                io.BytesIO(b"internal server error"),
            )
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert provider._probe_chat_endpoint("kimi-k3") == "https://cf.api.fan/v1"
    assert calls == [
        "https://cf.api.fan/chat/completions",
        "https://cf.api.fan/v1/chat/completions",
    ]


def test_packy_chat_probe_keeps_first_address_on_auth_error(monkeypatch):
    provider = OpenAICompatibleProvider(
        {"base_url": "https://cf.api.fan", "api_key": "test"},
        _route("openai_chat_completions"),
    )
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "unauthorized",
            {},
            io.BytesIO(b"unauthorized"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert provider._probe_chat_endpoint("kimi-k3") == "https://cf.api.fan"
    assert calls == ["https://cf.api.fan/chat/completions"]


def test_packy_chat_probe_rejects_html_homepage(monkeypatch):
    provider = OpenAICompatibleProvider(
        {"base_url": "https://www.packyapi.ai", "api_key": "test"},
        _route("openai_chat_completions"),
    )
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        response = MagicMock()
        response.__enter__.return_value = response
        if len(calls) == 1:
            response.headers = {"Content-Type": "text/html; charset=utf-8"}
            response.read.return_value = b"<!doctype html>"
        else:
            response.headers = {"Content-Type": "application/json"}
            response.read.return_value = b'{"choices": []}'
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert provider._probe_chat_endpoint("kimi-k3") == "https://www.packyapi.ai/v1"
    assert calls == [
        "https://www.packyapi.ai/chat/completions",
        "https://www.packyapi.ai/v1/chat/completions",
    ]


def test_morecode_versioned_base_is_not_duplicated(monkeypatch):
    provider = OpenAICompatibleProvider(
        {"base_url": "https://api.morecode.top/v1", "api_key": "test"},
        _route("openai_chat_completions"),
    )
    urlopen = MagicMock()
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert provider._chat_base_candidates() == ["https://api.morecode.top/v1"]
    assert provider._probe_chat_endpoint("kimi-k3") == "https://api.morecode.top/v1"
    urlopen.assert_not_called()
    assert provider._url("/chat/completions") == "https://api.morecode.top/v1/chat/completions"


def test_openai_non_stream_retries_503_then_succeeds(monkeypatch):
    provider = OpenAICompatibleProvider(
        _retry_config(max_request_retries=2),
        _route("openai_chat_completions"),
    )
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
    provider = OpenAICompatibleProvider(
        _retry_config(max_request_retries=2),
        _route("openai_chat_completions"),
    )
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
    provider = OpenAICompatibleProvider(
        _retry_config(max_stream_retries=1),
        _route("openai_chat_completions"),
    )
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
    assert [chunk["status"] for chunk in chunks] == [
        StreamStatus.CONTENT,
        StreamStatus.COMPLETE,
    ]
    assert chunks[0]["content"] == "ok"


def test_openai_stream_does_not_retry_after_output(monkeypatch):
    provider = OpenAICompatibleProvider(
        _retry_config(max_stream_retries=1),
        _route("openai_chat_completions"),
    )
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
    provider = OpenAICompatibleProvider(config, _route("openai_responses"))
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
    provider = GeminiProvider(
        _retry_config(max_request_retries=1),
        _route("gemini_generate_content"),
    )
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
    provider = AnthropicProvider(
        _retry_config(max_request_retries=1),
        _route("anthropic_messages"),
    )
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
