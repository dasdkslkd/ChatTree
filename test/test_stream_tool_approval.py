import asyncio
import inspect
import json
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.api.routes.messages import build_stream_chunk_data
from backend.core.chat.chat_manager import ChatManager
from backend.core.config.types import Role, StreamChunk, StreamController, StreamStatus
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage


def make_tool_call(name="web_search", arguments=None):
    return {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {"query": "ChatTree"}, ensure_ascii=False),
        },
    }


class FakeToolResultStore:
    def __init__(self):
        self.saved = []

    def save_result(self, **kwargs):
        self.saved.append(kwargs)
        return {"id": "result-1"}


class FakeToolManager:
    def __init__(self):
        self.tool_result_store = FakeToolResultStore()
        self.calls = []

    async def execute_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return "direct-result"


class FakeOrchestrator:
    def __init__(self):
        self.calls = []

    async def execute_tool_call(self, tool_call, conversation_id, node_id, emit_event=None):
        self.calls.append((tool_call, conversation_id, node_id))
        if emit_event:
            result = emit_event(
                {
                    "event_type": "tool_approval_request",
                    "approval": {"id": "approval-1", "status": "pending"},
                }
            )
            if inspect.isawaitable(result):
                await result
        return {
            "id": str(uuid.uuid4()),
            "role": Role.TOOL,
            "content": "raw orchestrator result",
            "name": tool_call["function"]["name"],
            "tool_calls": None,
            "tool_call_id": tool_call["id"],
        }


class FakeProvider:
    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        yield StreamChunk(
            status=StreamStatus.START,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=0,
        )
        yield StreamChunk(
            status=StreamStatus.CONTENT,
            content="",
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=0,
            tool_calls=[make_tool_call("filesystem__read_file", {"path": "notes.txt"})],
        )
        yield StreamChunk(
            status=StreamStatus.COMPLETE,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=1,
            usage_info={
                "input_tokens": 1,
                "output_tokens": 0,
                "total_tokens": 1,
                "source": "test",
            },
        )


class FakeModelManager:
    def __init__(self):
        self.model_list = {"fake-provider": ["fake-model"]}
        self.provider = FakeProvider()

    def get_model(self, provider, is_async=False):
        return self.provider


class FakeStreamingToolManager:
    def get_openai_tools(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "filesystem__read_file",
                    "description": "read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]


class BlockingOrchestrator:
    def __init__(self):
        self.cancelled = asyncio.Event()
        self.task = None

    async def execute_tool_call(self, tool_call, conversation_id, node_id, emit_event=None):
        self.task = asyncio.current_task()
        try:
            await emit_event(
                {
                    "event_type": "tool_approval_request",
                    "approval": {"id": "approval-blocking", "status": "pending"},
                }
            )
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def test_build_stream_chunk_data_preserves_tool_approval_request():
    data = build_stream_chunk_data(
        StreamChunk(
            status=StreamStatus.CONTENT,
            content=None,
            node_id="node-1",
            conversation_id="conv-1",
            error=None,
            tokens_used=0,
            event_type="tool_approval_request",
            approval={"id": "approval-1", "status": "pending"},
        ),
        conversation_id="fallback-conv",
    )

    assert data["event_type"] == "tool_approval_request"
    assert data["approval"]["id"] == "approval-1"


async def _execute_tool_calls_uses_orchestrator_and_keeps_events_separate():
    tool_manager = FakeToolManager()
    orchestrator = FakeOrchestrator()
    chat_manager = ChatManager(
        model_manager=None,
        storage=None,
        prompts=None,
        tool_manager=tool_manager,
    )
    chat_manager.tool_orchestrator = orchestrator
    events = []

    tool_messages = await chat_manager._execute_tool_calls(
        [make_tool_call("web_search", {"query": "中文"})],
        node_id="node-1",
        conversation_id="conv-1",
        emit_event=events.append,
    )

    assert tool_manager.calls == []
    assert len(orchestrator.calls) == 1
    assert events == [
        {
            "event_type": "tool_approval_request",
            "approval": {"id": "approval-1", "status": "pending"},
        }
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0]["role"] == Role.TOOL
    assert tool_messages[0]["name"] == "web_search"
    assert json.loads(tool_messages[0]["content"]) == {"preview": "raw orchestrator result"}
    assert tool_manager.tool_result_store.saved[0]["content"] == "raw orchestrator result"


def test_execute_tool_calls_uses_orchestrator_and_keeps_events_separate():
    asyncio.run(_execute_tool_calls_uses_orchestrator_and_keeps_events_separate())


async def _closing_stream_cancels_pending_tool_execution(tmp_path):
    chat_manager = ChatManager(
        FakeModelManager(),
        ChatStorage(storage_dir=str(tmp_path / "conversations")),
        PromptStorage(storage_dir=str(tmp_path / "prompts")),
        tool_manager=FakeStreamingToolManager(),
    )
    orchestrator = BlockingOrchestrator()
    chat_manager.tool_orchestrator = orchestrator
    conversation = chat_manager.create_conversation("tool approval cancellation")
    stream = chat_manager.send_message_stream(
        conversation.metadata["id"],
        "read notes",
        model_id="fake-model",
    )

    try:
        while True:
            chunk = await stream.__anext__()
            if chunk.get("event_type") == "tool_approval_request":
                assert chunk["approval"]["id"] == "approval-blocking"
                break

        await stream.aclose()
        await asyncio.wait_for(orchestrator.cancelled.wait(), timeout=1)

        assert orchestrator.task is not None
        assert orchestrator.task.done()
    finally:
        await stream.aclose()


def test_closing_stream_cancels_pending_tool_execution(tmp_path):
    asyncio.run(_closing_stream_cancels_pending_tool_execution(tmp_path))
