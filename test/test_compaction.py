import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.compact import get_auto_compact_threshold, microcompact_messages
from backend.core.chat.conversation import Conversation
from backend.core.chat.node import NodeManager
from backend.core.config.types import Message, Role, StreamChunk, StreamController, StreamStatus
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.api.routes import conversations as conversation_routes


class CompactProvider:
    def __init__(self):
        self.calls = []
        self.stream_calls = []

    def generate_response(self, model, messages, max_tokens=None, temperature=None, tools=None, tool_choice=None, **kwargs):
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        return (
            "<analysis>draft notes</analysis>\n"
            "<summary>\n"
            "1. Primary Request and Intent:\n"
            "   Continue the current task.\n"
            "</summary>",
            42,
        )

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.stream_calls.append({"model": model, "messages": messages, **kwargs})
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
            content="streamed answer",
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=1,
        )
        yield StreamChunk(
            status=StreamStatus.COMPLETE,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=9,
            usage_info={
                "input_tokens": 7,
                "output_tokens": 2,
                "total_tokens": 9,
                "source": "api",
                "raw": {"total_tokens": 9},
            },
        )


class CompactModelManager:
    def __init__(self):
        self.provider = CompactProvider()
        self.model_list = {"fake": ["fake-model"]}

    def get_model(self, provider, is_async=False):
        return self.provider

    def get_model_metadata(self, provider_id, model_name):
        return {"context_length": 200000}


def _message(role, content):
    return Message(
        {
            "id": f"{role}-{content}",
            "role": role,
            "content": content,
            "timestamp": 1,
        }
    )


async def _drain(stream):
    async for _ in stream:
        pass


def test_compact_node_uses_claude_style_boundary_and_summary_message():
    node = NodeManager.create_compact_node(
        parent_id="parent-1",
        summary="Summary:\nkept facts",
        trigger="manual",
        pre_tokens=123,
        model_id="fake-model",
        last_pre_compact_message_id="msg-9",
    )

    boundary = node["system_message"]
    assert boundary["role"] == Role.SYSTEM
    assert boundary["subtype"] == "compact_boundary"
    assert boundary["content"] == "Conversation compacted"
    assert boundary["compact_metadata"]["trigger"] == "manual"
    assert boundary["compact_metadata"]["pre_tokens"] == 123
    assert boundary["compact_metadata"]["last_pre_compact_message_id"] == "msg-9"

    summary = node["user_message"]
    assert summary["role"] == Role.USER
    assert summary["is_compact_summary"] is True
    assert summary["is_visible_in_transcript_only"] is True
    assert "This session is being continued from a previous conversation that ran out of context." in summary["content"]
    assert "Summary:\nkept facts" in summary["content"]


def test_model_context_starts_at_latest_compact_summary_and_keeps_root_system():
    conv = Conversation(title="compact projection")
    conv.initialize_with_system_message("root instructions")

    first = NodeManager.create_node(_message(Role.USER, "old question"), conv.current_node_id, "fake-model")
    first["assistant_message"] = _message(Role.ASSISTANT, "old answer")
    conv.add_node(first, conv.current_node_id)

    compact = NodeManager.create_compact_node(
        parent_id=conv.current_node_id,
        summary="Summary:\nold question was answered",
        trigger="manual",
        pre_tokens=100,
        model_id="fake-model",
        messages_to_keep=0,
    )
    conv.add_node(compact, conv.current_node_id)

    latest = NodeManager.create_node(_message(Role.USER, "new question"), conv.current_node_id, "fake-model")
    conv.add_node(latest, conv.current_node_id)

    manager = ChatManager(CompactModelManager(), ChatStorage(tempfile.mkdtemp()), PromptStorage(tempfile.mkdtemp()))
    try:
        messages = manager._prepare_messages_for_api_with_conversation(conv)
    finally:
        shutil.rmtree(manager.storage.storage_dir, ignore_errors=True)
        shutil.rmtree(manager.prompts.storage_dir, ignore_errors=True)

    contents = [message["content"] for message in messages]
    assert contents[0] == "root instructions"
    assert any("This session is being continued" in content for content in contents)
    assert contents[-1] == "new question"
    assert "old question" not in contents
    assert "old answer" not in contents
    assert "Conversation compacted" not in contents


def test_manual_compact_saves_boundary_summary_and_moves_current_node():
    tmp = tempfile.mkdtemp(prefix="chattree_compact_")
    try:
        storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
        prompts = PromptStorage(storage_dir=os.path.join(tmp, "prompts"))
        manager = ChatManager(CompactModelManager(), storage, prompts)
        conv = manager.create_conversation("manual compact")
        conv.metadata["provider_id"] = "fake"
        conv.metadata["model_id"] = "fake-model"

        first = NodeManager.create_node(_message(Role.USER, "old question"), conv.current_node_id, "fake-model")
        first["assistant_message"] = _message(Role.ASSISTANT, "old answer")
        conv.add_node(first, conv.current_node_id)
        manager._save(conv)

        result = asyncio.run(manager.compact_conversation(conv.metadata["id"]))

        reloaded = Conversation.from_dict(storage.load(conv.metadata["id"]))
        current = reloaded.nodes[reloaded.current_node_id]
        assert result["node_id"] == reloaded.current_node_id
        assert current["system_message"]["subtype"] == "compact_boundary"
        assert current["user_message"]["is_compact_summary"] is True
        assert "Summary:" in current["user_message"]["content"]
        assert "<analysis>" not in current["user_message"]["content"]

        compact_call = manager.model_manager.provider.calls[-1]
        assert compact_call["tools"] is None
        assert compact_call["tool_choice"] is None
        assert compact_call["max_tokens"] == 20000
        assert compact_call["temperature"] == 0
        assert compact_call["messages"][-1]["role"] == "user"
        assert "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools." in compact_call["messages"][-1]["content"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_messages_to_keep_preserves_latest_original_turn_after_summary():
    conv = Conversation(title="compact keep")
    conv.initialize_with_system_message("root instructions")

    first = NodeManager.create_node(_message(Role.USER, "first old question"), conv.current_node_id, "fake-model")
    first["assistant_message"] = _message(Role.ASSISTANT, "first old answer")
    conv.add_node(first, conv.current_node_id)

    second = NodeManager.create_node(_message(Role.USER, "latest kept question"), conv.current_node_id, "fake-model")
    second["assistant_message"] = _message(Role.ASSISTANT, "latest kept answer")
    conv.add_node(second, conv.current_node_id)

    compact = NodeManager.create_compact_node(
        parent_id=conv.current_node_id,
        summary="Summary:\nolder work",
        trigger="manual",
        pre_tokens=100,
        model_id="fake-model",
        messages_to_keep=1,
    )
    conv.add_node(compact, conv.current_node_id)

    manager = ChatManager(CompactModelManager(), ChatStorage(tempfile.mkdtemp()), PromptStorage(tempfile.mkdtemp()))
    try:
        contents = [message["content"] for message in manager._prepare_messages_for_api_with_conversation(conv)]
    finally:
        shutil.rmtree(manager.storage.storage_dir, ignore_errors=True)
        shutil.rmtree(manager.prompts.storage_dir, ignore_errors=True)

    assert any("This session is being continued" in content for content in contents)
    assert "latest kept question" in contents
    assert "latest kept answer" in contents
    assert "first old question" not in contents
    assert "first old answer" not in contents


def test_microcompact_shortens_large_tool_results_without_touching_user_text():
    messages = [
        {"role": "user", "content": "u" * 12000},
        {"role": "tool", "content": "x" * 12000, "tool_call_id": "call-1", "name": "run_command"},
    ]

    compacted = microcompact_messages(messages, max_tool_content_chars=2000)

    assert compacted[0]["content"] == "u" * 12000
    assert len(compacted[1]["content"]) < 2600
    assert "[microcompact]" in compacted[1]["content"]
    assert "12000 chars" in compacted[1]["content"]


def test_send_message_auto_compacts_when_context_usage_reaches_90_percent():
    tmp = tempfile.mkdtemp(prefix="chattree_auto_compact_")
    try:
        storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
        prompts = PromptStorage(storage_dir=os.path.join(tmp, "prompts"))
        manager = ChatManager(CompactModelManager(), storage, prompts)
        conv = manager.create_conversation("auto compact")
        conv.metadata["provider_id"] = "fake"
        conv.metadata["model_id"] = "fake-model"

        old = NodeManager.create_node(_message(Role.USER, "old question"), conv.current_node_id, "fake-model")
        old["assistant_message"] = _message(Role.ASSISTANT, "old answer")
        conv.add_node(old, conv.current_node_id)
        old["usage"]["active_context_usage"] = {
            "input_tokens": 179999,
            "output_tokens": 1,
            "total_tokens": 180000,
            "source": "api",
        }
        manager._save(conv)

        asyncio.run(_drain(manager.send_message_stream(conv.metadata["id"], "new question", model_id="fake-model")))

        reloaded = Conversation.from_dict(storage.load(conv.metadata["id"]))
        chain = reloaded.get_node_chain(reloaded.current_node_id)
        assert any((node.get("system_message") or {}).get("subtype") == "compact_boundary" for node in chain)
        assert chain[-2]["system_message"]["compact_metadata"]["trigger"] == "auto"
        assert chain[-1]["user_message"]["content"] == "new question"
        assert chain[-1]["parent_id"] == chain[-2]["id"]
        assert manager.model_manager.provider.calls
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compact_route_delegates_to_chat_manager():
    class FakeManager:
        def __init__(self):
            self.called_with = None

        async def compact_conversation(self, conversation_id, **kwargs):
            self.called_with = (conversation_id, kwargs)
            return {"conversation_id": conversation_id, "node_id": "compact-1"}

    async def run():
        manager = FakeManager()
        response = await conversation_routes.compact_conversation(
            "conv-1",
            conversation_routes.ConversationCompactRequest(
                custom_instructions="focus on files",
                model_id="fake-model",
                provider_id="fake",
            ),
            manager,
        )
        assert response == {"conversation_id": "conv-1", "node_id": "compact-1"}
        assert manager.called_with == (
            "conv-1",
            {
                "custom_instructions": "focus on files",
                "model_id": "fake-model",
                "provider_id": "fake",
                "trigger": "manual",
                "messages_to_keep": 1,
            },
        )

    asyncio.run(run())


def test_auto_compact_threshold_uses_90_percent_of_current_model_window():
    assert get_auto_compact_threshold(200000) == 180000
    assert get_auto_compact_threshold(32000) == 28800
