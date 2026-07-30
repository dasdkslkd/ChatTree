import asyncio
from typing import Any

from backend.core.config.types import Message, ModelRoute, Role, StreamController, StreamStatus
from backend.core.model.providers.anthropic_provider import AnthropicProvider
from backend.core.model.providers.gemini_provider import GeminiProvider
from backend.core.model.providers.openai_compatible import OpenAICompatibleProvider


class _HangingOpenAIProvider(OpenAICompatibleProvider):
    def _stream_to_queue(self, *args: Any, **kwargs: Any) -> None:
        return None


class _HangingAnthropicProvider(AnthropicProvider):
    def _stream_to_queue(self, *args: Any, **kwargs: Any) -> None:
        return None


class _HangingGeminiProvider(GeminiProvider):
    def _stream_to_queue(self, *args: Any, **kwargs: Any) -> None:
        return None


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


async def _assert_stops_while_waiting_for_provider_queue(provider: Any) -> None:
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


def test_openai_chat_stream_stop_does_not_wait_for_network_timeout():
    provider = _HangingOpenAIProvider({
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9/v1",
    }, _route("openai_chat_completions"))
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))


def test_openai_responses_stream_stop_does_not_wait_for_network_timeout():
    provider = _HangingOpenAIProvider({
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9/v1",
    }, _route("openai_responses"))
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))


def test_anthropic_stream_stop_does_not_wait_for_network_timeout():
    provider = _HangingAnthropicProvider({
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9",
    }, _route("anthropic_messages"))
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))


def test_gemini_stream_stop_does_not_wait_for_network_timeout():
    provider = _HangingGeminiProvider({
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9/v1beta",
    }, _route("gemini_generate_content"))
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))
