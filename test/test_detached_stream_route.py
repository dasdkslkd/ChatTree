import asyncio

from backend.api.routes import messages as messages_route
from backend.core.config.types import StreamChunk, StreamStatus


class FakeChatManager:
    def __init__(self):
        self.allow_continue = asyncio.Event()
        self.completed = asyncio.Event()
        self.cancelled = False

    async def send_message_stream(self, **kwargs):
        try:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="first",
                node_id="node-1",
                conversation_id=kwargs["conversation_id"],
                tokens_used=0,
            )
            await self.allow_continue.wait()
            yield StreamChunk(
                status=StreamStatus.COMPLETE,
                content="second",
                node_id="node-1",
                conversation_id=kwargs["conversation_id"],
                tokens_used=1,
            )
            self.completed.set()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_detached_stream_continues_after_client_disconnect():
    async def run():
        manager = FakeChatManager()
        request = messages_route.SendMessageRequest(content="hello", model_id="fake-model")
        stream = messages_route.detached_stream_event_generator("conv-1", request, manager)

        first_event = await anext(stream)
        assert "first" in first_event

        await stream.aclose()
        manager.allow_continue.set()

        await asyncio.wait_for(manager.completed.wait(), timeout=1)
        assert manager.cancelled is False

    asyncio.run(run())


def test_detached_stream_attach_replays_buffer_and_continues_live():
    async def run():
        manager = FakeChatManager()
        request = messages_route.SendMessageRequest(content="hello", model_id="fake-model")
        stream = messages_route.detached_stream_event_generator("conv-1", request, manager)

        first_event = await anext(stream)
        assert "first" in first_event

        session = messages_route._STREAM_SESSIONS["node-1"]
        active_streams = await messages_route.get_all_active_streams()
        assert active_streams[0]["conversation_id"] == "conv-1"
        assert active_streams[0]["node_id"] == "node-1"
        attached = session.subscribe(0)

        replayed = await anext(attached)
        assert replayed == first_event

        manager.allow_continue.set()
        live_event = await anext(attached)
        assert "second" in live_event

        done_event = await anext(attached)
        assert "[DONE]" in done_event

        await asyncio.wait_for(manager.completed.wait(), timeout=1)
        await stream.aclose()

    asyncio.run(run())
