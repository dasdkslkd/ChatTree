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


class SideQuestionChatManager:
    async def send_message_stream(self, **kwargs):
        yield StreamChunk(
            status=StreamStatus.START,
            content=None,
            node_id=None,
            target_node_id=None,
            conversation_id=kwargs["conversation_id"],
            run_id=kwargs.get("run_id"),
            tokens_used=0,
        )
        yield StreamChunk(
            status=StreamStatus.CONTENT,
            content="aside",
            node_id=None,
            target_node_id=None,
            conversation_id=kwargs["conversation_id"],
            run_id=kwargs.get("run_id"),
            tokens_used=0,
        )
        yield StreamChunk(
            status=StreamStatus.COMPLETE,
            content=None,
            node_id=None,
            target_node_id=None,
            conversation_id=kwargs["conversation_id"],
            run_id=kwargs.get("run_id"),
            tokens_used=1,
        )


class FailingChatManager:
    async def send_message_stream(self, **kwargs):
        raise AssertionError("chat stream should not be used for direct-response slash commands")


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


def test_detached_btw_stream_uses_side_question_run_without_target_bind():
    async def run():
        manager = SideQuestionChatManager()
        run_manager = RunManager()
        request = messages_route.SendMessageRequest(
            content="/btw explain this",
            model_id="fake-model",
            node_id="node-anchor",
        )
        stream = messages_route.detached_stream_event_generator("conv-1", request, manager, run_manager)

        started_event = parse_sse(await anext(stream))
        assert started_event["type"] == "run_started"
        assert started_event["kind"] == "side_question"
        assert started_event["anchor_node_id"] == "node-anchor"
        assert started_event["target_node_id"] is None

        start_chunk = parse_sse(await anext(stream))
        content_chunk = parse_sse(await anext(stream))
        complete_chunk = parse_sse(await anext(stream))

        assert start_chunk["target_node_id"] is None
        assert content_chunk["content"] == "aside"
        assert content_chunk["target_node_id"] is None
        assert complete_chunk["status"] == "complete"
        assert complete_chunk["target_node_id"] is None
        assert run_manager.get_run(started_event["run_id"])["target_node_id"] is None

        done_event = await anext(stream)
        assert "[DONE]" in done_event

    asyncio.run(run())


def test_detached_direct_response_stream_creates_run_without_target_or_chat_node():
    async def run():
        run_manager = RunManager()
        request = messages_route.SendMessageRequest(
            content="/status",
            model_id="fake-model",
            node_id="node-anchor",
        )
        stream = messages_route.detached_stream_event_generator(
            "conv-1",
            request,
            FailingChatManager(),
            run_manager,
        )

        started_event = parse_sse(await anext(stream))
        assert started_event["type"] == "run_started"
        assert started_event["kind"] == "direct_response"
        assert started_event["anchor_node_id"] == "node-anchor"
        assert started_event["target_node_id"] is None

        start_chunk = parse_sse(await anext(stream))
        content_chunk = parse_sse(await anext(stream))
        complete_chunk = parse_sse(await anext(stream))

        assert start_chunk["status"] == "start"
        assert start_chunk["target_node_id"] is None
        assert content_chunk["status"] == "content"
        assert "ChatTree" in content_chunk["content"]
        assert content_chunk["target_node_id"] is None
        assert complete_chunk["status"] == "complete"
        assert complete_chunk["target_node_id"] is None
        assert run_manager.get_run(started_event["run_id"])["target_node_id"] is None

        done_event = await anext(stream)
        assert "[DONE]" in done_event

    asyncio.run(run())


def test_active_streams_include_direct_response_runs_without_target_node():
    async def run():
        run_manager = RunManager()
        direct = await run_manager.create_run(
            conversation_id="conv-1",
            kind="direct_response",
            anchor_node_id="node-anchor",
        )

        active_streams = await messages_route.get_all_active_streams(run_manager)

        assert active_streams == [
            {
                "run_id": direct.run_id,
                "conversation_id": "conv-1",
                "anchor_node_id": "node-anchor",
                "node_id": None,
                "target_node_id": None,
                "kind": "direct_response",
                "status": "running",
                "event_count": 1,
                "done": False,
                "created_at": direct.created_at,
                "updated_at": direct.updated_at,
            }
        ]

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
