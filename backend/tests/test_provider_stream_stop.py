import asyncio
from typing import Any

from backend.core.config.types import Message, Role, StreamController, StreamStatus
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
    })
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))


def test_openai_responses_stream_stop_does_not_wait_for_network_timeout():
    provider = _HangingOpenAIProvider({
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9/v1",
        "api_format": "responses",
    })
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))


def test_anthropic_stream_stop_does_not_wait_for_network_timeout():
    provider = _HangingAnthropicProvider({
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9",
    })
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))


def test_gemini_stream_stop_does_not_wait_for_network_timeout():
    provider = _HangingGeminiProvider({
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9/v1beta",
    })
    asyncio.run(_assert_stops_while_waiting_for_provider_queue(provider))
