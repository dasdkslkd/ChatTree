import asyncio
import json

from backend.api.routes import messages as messages_route
from backend.core.config.types import StreamChunk, StreamStatus
from backend.core.runs import RunManager


def parse_sse(event: str):
    assert event.startswith("data: ")
    return json.loads(event.removeprefix("data: ").strip())


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


class DelayedStartChatManager:
    def __init__(self):
        self.allow_start = asyncio.Event()
        self.stopped = asyncio.Event()
        self.stop_calls = []

    async def send_message_stream(self, **kwargs):
        await self.allow_start.wait()
        yield StreamChunk(
            status=StreamStatus.START,
            content=None,
            node_id="node-early",
            conversation_id=kwargs["conversation_id"],
            run_id=kwargs.get("run_id"),
            tokens_used=0,
        )
        yield StreamChunk(
            status=StreamStatus.CONTENT,
            content="late",
            node_id="node-early",
            conversation_id=kwargs["conversation_id"],
            run_id=kwargs.get("run_id"),
            tokens_used=0,
        )

    async def stop_stream(self, node_id: str):
        self.stop_calls.append(node_id)
        self.stopped.set()
        return True


def test_detached_stream_continues_after_client_disconnect():
    async def run():
        manager = FakeChatManager()
        request = messages_route.SendMessageRequest(content="hello", model_id="fake-model")
        stream = messages_route.detached_stream_event_generator("conv-1", request, manager)

        started_event = await anext(stream)
        assert parse_sse(started_event)["type"] == "run_started"

        bound_event = await anext(stream)
        assert parse_sse(bound_event)["type"] == "run_target_bound"

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

        started_event = await anext(stream)
        assert parse_sse(started_event)["type"] == "run_started"
        bound_event = await anext(stream)
        assert parse_sse(bound_event)["type"] == "run_target_bound"
        first_event = await anext(stream)
        assert "first" in first_event

        session = messages_route._STREAM_SESSIONS["node-1"]
        active_streams = await messages_route.get_all_active_streams()
        assert active_streams[0]["conversation_id"] == "conv-1"
        assert active_streams[0]["node_id"] == "node-1"
        attached = session.subscribe(0)

        replayed_started = await anext(attached)
        assert replayed_started == started_event
        replayed_bound = await anext(attached)
        assert replayed_bound == bound_event
        replayed_first = await anext(attached)
        assert replayed_first == first_event

        manager.allow_continue.set()
        live_event = await anext(attached)
        assert "second" in live_event

        done_event = await anext(attached)
        assert "[DONE]" in done_event

        await asyncio.wait_for(manager.completed.wait(), timeout=1)
        await stream.aclose()

    asyncio.run(run())


def test_stop_before_target_bind_is_applied_when_node_arrives():
    async def run():
        manager = DelayedStartChatManager()
        run_manager = RunManager()
        request = messages_route.SendMessageRequest(content="hello", model_id="fake-model")
        stream = messages_route.detached_stream_event_generator("conv-1", request, manager, run_manager)

        started_event = await anext(stream)
        started = parse_sse(started_event)
        assert started["type"] == "run_started"
        await run_manager.request_stop(started["run_id"])

        manager.allow_start.set()
        await asyncio.wait_for(manager.stopped.wait(), timeout=1)
        assert manager.stop_calls == ["node-early"]
        await stream.aclose()

    asyncio.run(run())
