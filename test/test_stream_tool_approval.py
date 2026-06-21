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
from backend.core.tools.orchestrator import ToolOrchestrator
from backend.core.tools.security.approval import ApprovalManager
from backend.core.tools.security.logical_sandbox import LogicalSandbox
from backend.core.tools.security.permissions import PermissionEngine


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


class ApprovalPersistenceProvider:
    def __init__(self):
        self.calls = 0

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.calls += 1
        yield StreamChunk(
            status=StreamStatus.START,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=0,
        )
        if self.calls == 1:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=0,
                tool_calls=[make_tool_call("filesystem__write_file", {"path": "notes.txt"})],
            )
        else:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="done",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=0,
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
                "output_tokens": 1,
                "total_tokens": 2,
                "source": "test",
            },
        )


class PlainAssistantProvider:
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
            content="plain response",
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=0,
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
                "output_tokens": 1,
                "total_tokens": 2,
                "source": "test",
            },
        )


class FakeModelManager:
    def __init__(self, provider=None):
        self.model_list = {"fake-provider": ["fake-model"]}
        self.provider = provider or FakeProvider()

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

    async def execute_tool(self, name, arguments):
        return "streaming-tool-result"


class StopAwareApprovalProvider:
    def __init__(self):
        self.calls = 0

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.calls += 1
        if await stream_controller.is_stopped():
            yield StreamChunk(
                status=StreamStatus.STOPPED,
                content="",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=0,
            )
            return

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


class ApprovalPersistenceOrchestrator:
    async def execute_tool_call(self, tool_call, conversation_id, node_id, emit_event=None):
        events = [
            {
                "event_type": "tool_approval_request",
                "approval": {
                    "id": "approval-42",
                    "status": "pending",
                    "grant_scope": "once",
                    "tool_name": tool_call["function"]["name"],
                    "tool_call_id": tool_call["id"],
                    "conversation_id": conversation_id,
                    "node_id": node_id,
                },
            },
            {
                "event_type": "tool_approval_result",
                "approval": {
                    "id": "approval-42",
                    "status": "approved",
                    "grant_scope": "once",
                    "tool_name": tool_call["function"]["name"],
                    "tool_call_id": tool_call["id"],
                    "conversation_id": conversation_id,
                    "node_id": node_id,
                },
            },
            {
                "event_type": "tool_approval_reused",
                "approval": {
                    "conversation_id": conversation_id,
                    "node_id": node_id,
                    "tool_call_id": tool_call["id"],
                    "tool_name": tool_call["function"]["name"],
                    "grant_scope": "session",
                    "reason": "Session approval grant reused.",
                },
            },
        ]
        for event in events:
            result = emit_event(event)
            if inspect.isawaitable(result):
                await result
        return {
            "id": str(uuid.uuid4()),
            "role": Role.TOOL,
            "content": "write ok",
            "name": tool_call["function"]["name"],
            "tool_calls": None,
            "tool_call_id": tool_call["id"],
        }


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
    payload = json.loads(tool_messages[0]["content"])
    assert payload == {
        "tool_result_id": "result-1",
        "total_chars": 23,
        "truncated": False,
        "preview": "raw orchestrator result",
    }
    assert tool_messages[0]["raw_content"] == "raw orchestrator result"
    assert tool_messages[0]["model_visible_content"] == tool_messages[0]["content"]
    assert tool_messages[0]["tool_result_id"] == "result-1"
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


async def _stream_persists_tool_approval_events_on_assistant_node(tmp_path):
    chat_manager = ChatManager(
        FakeModelManager(provider=ApprovalPersistenceProvider()),
        ChatStorage(storage_dir=str(tmp_path / "conversations")),
        PromptStorage(storage_dir=str(tmp_path / "prompts")),
        tool_manager=FakeStreamingToolManager(),
    )
    chat_manager.tool_orchestrator = ApprovalPersistenceOrchestrator()
    conversation = chat_manager.create_conversation("approval persistence")

    chunks = []
    async for chunk in chat_manager.send_message_stream(
        conversation.metadata["id"],
        "write notes",
        model_id="fake-model",
    ):
        chunks.append(chunk)

    streamed_approval_events = [
        chunk for chunk in chunks if chunk.get("event_type", "").startswith("tool_approval_")
    ]
    assert [event["event_type"] for event in streamed_approval_events] == [
        "tool_approval_request",
        "tool_approval_result",
        "tool_approval_reused",
    ]

    saved = chat_manager.get_conversation(conversation.metadata["id"])
    assert saved is not None
    node_id = streamed_approval_events[0]["node_id"]
    assistant_message = saved.nodes[node_id]["assistant_message"]
    assert assistant_message is not None
    assert assistant_message["approval_events"] == [
        {
            "event_type": "tool_approval_request",
            "approval": {
                "id": "approval-42",
                "status": "pending",
                "grant_scope": "once",
                "tool_name": "filesystem__write_file",
                "tool_call_id": "call-1",
                "conversation_id": conversation.metadata["id"],
                "node_id": node_id,
            },
        },
        {
            "event_type": "tool_approval_result",
            "approval": {
                "id": "approval-42",
                "status": "approved",
                "grant_scope": "once",
                "tool_name": "filesystem__write_file",
                "tool_call_id": "call-1",
                "conversation_id": conversation.metadata["id"],
                "node_id": node_id,
            },
        },
        {
            "event_type": "tool_approval_reused",
            "approval": {
                "conversation_id": conversation.metadata["id"],
                "node_id": node_id,
                "tool_call_id": "call-1",
                "tool_name": "filesystem__write_file",
                "grant_scope": "session",
                "reason": "Session approval grant reused.",
            },
        },
    ]


def test_stream_persists_tool_approval_events_on_assistant_node(tmp_path):
    asyncio.run(_stream_persists_tool_approval_events_on_assistant_node(tmp_path))


async def _stream_updates_metadata_updated_at_after_tool_completion(tmp_path, monkeypatch):
    from backend.core.chat import chat_manager as chat_manager_module
    from backend.core.chat import conversation as conversation_module

    monkeypatch.setattr(conversation_module, "time", lambda: 1000)
    monkeypatch.setattr(chat_manager_module, "time", lambda: 2000)

    chat_manager = ChatManager(
        FakeModelManager(provider=ApprovalPersistenceProvider()),
        ChatStorage(storage_dir=str(tmp_path / "conversations")),
        PromptStorage(storage_dir=str(tmp_path / "prompts")),
        tool_manager=FakeStreamingToolManager(),
    )
    chat_manager.tool_orchestrator = ApprovalPersistenceOrchestrator()
    conversation = chat_manager.create_conversation("metadata tool completion")

    chunks = [
        chunk
        async for chunk in chat_manager.send_message_stream(
            conversation.metadata["id"],
            "write notes",
            model_id="fake-model",
        )
    ]

    tool_event = next(chunk for chunk in chunks if chunk.get("event_type") == "tool_approval_result")
    saved = chat_manager.get_conversation(conversation.metadata["id"])
    assert saved is not None
    assistant_message = saved.nodes[tool_event["node_id"]]["assistant_message"]
    assert assistant_message is not None
    assert saved.metadata["updated_at"] >= assistant_message["timestamp"]


def test_stream_updates_metadata_updated_at_after_tool_completion(tmp_path, monkeypatch):
    asyncio.run(_stream_updates_metadata_updated_at_after_tool_completion(tmp_path, monkeypatch))


async def _stream_updates_metadata_updated_at_after_plain_assistant_completion(tmp_path, monkeypatch):
    from backend.core.chat import chat_manager as chat_manager_module
    from backend.core.chat import conversation as conversation_module

    monkeypatch.setattr(conversation_module, "time", lambda: 1000)
    monkeypatch.setattr(chat_manager_module, "time", lambda: 2000)

    chat_manager = ChatManager(
        FakeModelManager(provider=PlainAssistantProvider()),
        ChatStorage(storage_dir=str(tmp_path / "conversations")),
        PromptStorage(storage_dir=str(tmp_path / "prompts")),
        tool_manager=None,
    )
    conversation = chat_manager.create_conversation("metadata plain completion")

    chunks = [
        chunk
        async for chunk in chat_manager.send_message_stream(
            conversation.metadata["id"],
            "hello",
            model_id="fake-model",
        )
    ]

    content_chunk = next(chunk for chunk in chunks if chunk.get("content") == "plain response")
    saved = chat_manager.get_conversation(conversation.metadata["id"])
    assert saved is not None
    assistant_message = saved.nodes[content_chunk["node_id"]]["assistant_message"]
    assert assistant_message is not None
    assert saved.metadata["updated_at"] >= assistant_message["timestamp"]


def test_stream_updates_metadata_updated_at_after_plain_assistant_completion(tmp_path, monkeypatch):
    asyncio.run(_stream_updates_metadata_updated_at_after_plain_assistant_completion(tmp_path, monkeypatch))


async def _stop_stream_cancels_pending_approval_and_stream_finishes(tmp_path):
    tool_manager = FakeStreamingToolManager()
    approval_manager = ApprovalManager(timeout_seconds=60)
    chat_manager = ChatManager(
        FakeModelManager(provider=StopAwareApprovalProvider()),
        ChatStorage(storage_dir=str(tmp_path / "conversations")),
        PromptStorage(storage_dir=str(tmp_path / "prompts")),
        tool_manager=tool_manager,
    )
    chat_manager.tool_orchestrator = ToolOrchestrator(
        tool_manager=tool_manager,
        permission_engine=PermissionEngine.default(),
        approval_manager=approval_manager,
        logical_sandbox=LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
    )
    conversation = chat_manager.create_conversation("stop pending approval")
    stream = chat_manager.send_message_stream(
        conversation.metadata["id"],
        "read notes",
        model_id="fake-model",
    )

    approval_id = None
    node_id = None
    tail = []

    try:
        while True:
            chunk = await asyncio.wait_for(stream.__anext__(), timeout=1)
            if chunk.get("event_type") == "tool_approval_request":
                approval_id = chunk["approval"]["id"]
                node_id = chunk["node_id"]
                break

        assert approval_id is not None
        assert node_id is not None
        assert await chat_manager.stop_stream(node_id)

        for _ in range(8):
            try:
                chunk = await asyncio.wait_for(stream.__anext__(), timeout=1)
            except StopAsyncIteration:
                break
            tail.append(chunk)
            if chunk.get("status") in {
                StreamStatus.STOPPED,
                StreamStatus.ERROR,
                StreamStatus.COMPLETE,
            }:
                break
    finally:
        await stream.aclose()

    assert approval_manager.get(approval_id) is None
    assert any(
        chunk.get("event_type") == "tool_approval_result"
        and chunk.get("approval", {}).get("status") == "cancelled"
        for chunk in tail
    )
    assert any(chunk.get("status") == StreamStatus.STOPPED for chunk in tail)


def test_stop_stream_cancels_pending_approval_and_stream_finishes(tmp_path):
    asyncio.run(_stop_stream_cancels_pending_approval_and_stream_finishes(tmp_path))
