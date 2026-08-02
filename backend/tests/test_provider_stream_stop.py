import asyncio

from backend.core.config.config import DEFAULT_MODEL_TRANSPORT
from backend.core.config.types import Message, ModelRoute, Role, StreamController, StreamStatus
from backend.core.model.providers.anthropic_provider import AnthropicProvider
from backend.core.model.providers.gemini_provider import GeminiProvider
from backend.core.model.providers.openai_compatible import OpenAICompatibleProvider


class _HangingClient:
    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def build_request(self, *_args, **_kwargs):
        return object()

    async def send(self, *_args, **_kwargs):
        await asyncio.sleep(3600)


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
        reasoning_profile={"name": "test", "carrier": "none", "history_policy": "drop", "strict": False},
    )


def _message() -> Message:
    return Message({
        "id": "msg-1",
        "role": Role.USER,
        "content": "hello",
        "timestamp": 1,
    })


async def _assert_stops_while_waiting_for_provider_queue(provider) -> None:
    controller = StreamController("node-1", "conv-1", "run-1")
    stream = provider.generate_response_stream(
        "test-model",
        [_message()],
        stream_controller=controller,
    )
    try:
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await controller.stop()
        stopped = await asyncio.wait_for(pending, timeout=0.5)
        assert stopped["status"] == StreamStatus.STOPPED
        assert stopped["node_id"] == "node-1"
        assert stopped["conversation_id"] == "conv-1"
    finally:
        await stream.aclose()


def test_openai_chat_stream_stop_does_not_wait_for_network_timeout(monkeypatch):
    monkeypatch.setattr("backend.core.model.providers.sse.httpx.AsyncClient", _HangingClient)
    provider = OpenAICompatibleProvider({
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9/v1",
        "model_transport": DEFAULT_MODEL_TRANSPORT,
    }, _route("openai_chat_completions"))
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))


def test_openai_responses_stream_stop_does_not_wait_for_network_timeout(monkeypatch):
    monkeypatch.setattr("backend.core.model.providers.sse.httpx.AsyncClient", _HangingClient)
    provider = OpenAICompatibleProvider({
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9/v1",
        "model_transport": DEFAULT_MODEL_TRANSPORT,
    }, _route("openai_responses"))
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))


def test_anthropic_stream_stop_does_not_wait_for_network_timeout(monkeypatch):
    monkeypatch.setattr("backend.core.model.providers.sse.httpx.AsyncClient", _HangingClient)
    provider = AnthropicProvider({
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9",
        "model_transport": DEFAULT_MODEL_TRANSPORT,
    }, _route("anthropic_messages"))
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))


def test_gemini_stream_stop_does_not_wait_for_network_timeout(monkeypatch):
    monkeypatch.setattr("backend.core.model.providers.sse.httpx.AsyncClient", _HangingClient)
    provider = GeminiProvider({
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9/v1beta",
        "model_transport": DEFAULT_MODEL_TRANSPORT,
    }, _route("gemini_generate_content"))
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))
