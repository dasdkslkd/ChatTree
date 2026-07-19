import asyncio
import json

from backend.api.routes import messages as messages_route
from backend.core.config.types import StreamChunk, StreamStatus
from backend.core.runs import RunKind, RunManager
from backend.core.slash import SlashCommandDispatcher, SlashDispatchKind


def parse_sse(event: str):
    assert event.startswith("data: ")
    return json.loads(event.removeprefix("data: ").strip())


async def _start_test_producer(
    request: messages_route.SendMessageRequest,
    chat_manager,
    run_manager: RunManager,
):
    slash_result = SlashCommandDispatcher().dispatch(request.content)
    run_kind = RunKind(str(slash_result.run_kind or RunKind.CHAT.value))
    run = await run_manager.create_run(
        conversation_id="conv-1",
        kind=run_kind,
        anchor_node_id=request.parent_node_id,
        summary=request.content[:80],
        metadata=messages_route._message_run_metadata(request, slash_result),
    )
    if slash_result.kind == SlashDispatchKind.DIRECT_RESPONSE:
        producer = messages_route._produce_direct_response(
            run=run,
            conversation_id="conv-1",
            request=request,
            slash_result=slash_result,
            chat_manager=chat_manager,
            run_manager=run_manager,
        )
    else:
        producer = messages_route._produce_chat_run(
            run=run,
            conversation_id="conv-1",
            request=request,
            chat_manager=chat_manager,
            run_manager=run_manager,
        )
    return run, asyncio.create_task(producer)


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
        for status, content in (
            (StreamStatus.START, None),
            (StreamStatus.CONTENT, "aside"),
            (StreamStatus.COMPLETE, None),
        ):
            yield StreamChunk(
                status=status,
                content=content,
                node_id=None,
                target_node_id=None,
                conversation_id=kwargs["conversation_id"],
                run_id=kwargs.get("run_id"),
                tokens_used=1 if status == StreamStatus.COMPLETE else 0,
            )


class FailingChatManager:
    async def send_message_stream(self, **kwargs):
        raise AssertionError("chat stream must not run for direct responses")


def test_subscriber_disconnect_does_not_cancel_the_message_producer():
    async def scenario():
        manager = FakeChatManager()
        run_manager = RunManager()
        request = messages_route.SendMessageRequest(
            content="hello",
            parent_node_id="node-anchor",
            model_id="fake-model",
        )
        run, producer = await _start_test_producer(request, manager, run_manager)
        stream = messages_route._subscribe_sse(run_manager, run.run_id)

        assert parse_sse(await anext(stream))["type"] == "run_started"
        assert parse_sse(await anext(stream))["type"] == "run_target_bound"
        assert parse_sse(await anext(stream))["content"] == "first"
        await stream.aclose()

        manager.allow_continue.set()
        await asyncio.wait_for(producer, timeout=1)
        assert manager.completed.is_set()
        assert manager.cancelled is False

    asyncio.run(scenario())


def test_run_event_subscription_replays_then_continues_live():
    async def scenario():
        manager = FakeChatManager()
        run_manager = RunManager()
        request = messages_route.SendMessageRequest(
            content="hello",
            parent_node_id="node-anchor",
            model_id="fake-model",
        )
        run, producer = await _start_test_producer(request, manager, run_manager)
        first = messages_route._subscribe_sse(run_manager, run.run_id)
        replayed = [await anext(first) for _ in range(3)]
        await first.aclose()

        attached = messages_route._subscribe_sse(run_manager, run.run_id)
        assert [await anext(attached) for _ in range(3)] == replayed
        manager.allow_continue.set()
        assert parse_sse(await anext(attached))["content"] == "second"
        assert "[DONE]" in await anext(attached)
        await asyncio.wait_for(producer, timeout=1)

    asyncio.run(scenario())


def test_side_question_run_does_not_bind_a_target_node():
    async def scenario():
        run_manager = RunManager()
        request = messages_route.SendMessageRequest(
            content="/btw explain this",
            parent_node_id="node-anchor",
            model_id="fake-model",
        )
        run, producer = await _start_test_producer(
            request,
            SideQuestionChatManager(),
            run_manager,
        )
        await producer

        events = run_manager.read_events(run.run_id)
        assert events[0]["kind"] == "side_question"
        assert [event.get("content") for event in events if event.get("status") == "content"] == ["aside"]
        assert run_manager.get_run(run.run_id)["target_node_id"] is None

    asyncio.run(scenario())


def test_direct_response_run_never_calls_the_chat_stream():
    async def scenario():
        run_manager = RunManager()
        request = messages_route.SendMessageRequest(
            content="/status",
            parent_node_id="node-anchor",
            model_id="fake-model",
        )
        run, producer = await _start_test_producer(
            request,
            FailingChatManager(),
            run_manager,
        )
        await producer

        events = run_manager.read_events(run.run_id)
        assert events[0]["kind"] == "direct_response"
        assert any("ChatTree" in str(event.get("content") or "") for event in events)
        assert run_manager.get_run(run.run_id)["target_node_id"] is None

    asyncio.run(scenario())


def test_active_streams_include_targetless_direct_responses():
    async def scenario():
        run_manager = RunManager()
        direct = await run_manager.create_run(
            conversation_id="conv-1",
            kind="direct_response",
            anchor_node_id="node-anchor",
        )

        active_streams = await messages_route.get_all_active_streams(run_manager)

        assert len(active_streams) == 1
        assert active_streams[0]["run_id"] == direct.run_id
        assert active_streams[0]["kind"] == "direct_response"
        assert active_streams[0]["target_node_id"] is None

    asyncio.run(scenario())


def test_stop_requested_before_target_bind_is_applied_when_node_arrives():
    async def scenario():
        manager = DelayedStartChatManager()
        run_manager = RunManager()
        request = messages_route.SendMessageRequest(
            content="hello",
            parent_node_id="node-anchor",
            model_id="fake-model",
        )
        run, producer = await _start_test_producer(request, manager, run_manager)
        assert await run_manager.request_stop(run.run_id) is True

        manager.allow_start.set()
        await asyncio.wait_for(manager.stopped.wait(), timeout=1)
        await asyncio.wait_for(producer, timeout=1)
        assert manager.stop_calls == ["node-early"]

    asyncio.run(scenario())
