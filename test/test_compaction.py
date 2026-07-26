import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.canonical_reader import messages_by_node
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


def _manager(tmp):
    storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
    prompts = PromptStorage(storage_dir=os.path.join(tmp, "prompts"))
    return ChatManager(CompactModelManager(), storage, prompts)


def _messages_for_node(manager, conversation_id, node_id):
    return messages_by_node(manager.chat_repository, conversation_id, [node_id]).get(node_id, [])


def _add_turn(manager, conv, user_content, assistant_content=None, *, import_files=None, parent_id=None, focus=True):
    parent_id = parent_id or conv.current_node_id
    node = NodeManager.create_node(parent_id=parent_id, model_id="fake-model")
    conv.add_node(node, parent_id, focus=focus)
    manager.chat_repository.save(conv)
    manager.chat_repository.ensure_branch(conv, node["id"], provider_id="fake", model_id="fake-model")
    metadata = {"import_files": import_files} if import_files else None
    manager.chat_repository.add_message(
        conv.metadata["id"],
        node["id"],
        role=Role.USER.value,
        content=user_content,
        metadata=metadata,
    )
    if assistant_content is not None:
        manager.chat_repository.add_message(
            conv.metadata["id"],
            node["id"],
            role=Role.ASSISTANT.value,
            content=assistant_content,
        )
    return node


def _add_compact_node(manager, conv, summary, *, trigger="manual", pre_tokens=100, messages_to_keep=0):
    parent_id = conv.current_node_id
    node = NodeManager.create_compact_node(parent_id=parent_id, model_id="fake-model")
    conv.add_node(node, parent_id)
    manager.chat_repository.save(conv)
    manager.chat_repository.ensure_branch(conv, node["id"], provider_id="fake", model_id="fake-model")
    metadata = {
        "trigger": trigger,
        "pre_tokens": pre_tokens,
        "messages_to_keep": messages_to_keep,
        "last_pre_compact_message_id": parent_id,
    }
    manager.chat_repository.add_message(
        conv.metadata["id"],
        node["id"],
        role=Role.SYSTEM.value,
        content="Conversation compacted",
        subtype="compact_boundary",
        hidden=True,
        metadata=metadata,
    )
    manager.chat_repository.add_message(
        conv.metadata["id"],
        node["id"],
        role=Role.ASSISTANT.value,
        content=summary,
        subtype="compact_summary",
        hidden=True,
        transcript_only=True,
        metadata=metadata,
    )
    return node


def test_compact_node_uses_claude_style_boundary_and_summary_message():
    node = NodeManager.create_compact_node(parent_id="parent-1", model_id="fake-model")

    assert node["parent_id"] == "parent-1"
    assert node["model_id"] == "fake-model"
    assert "system_message" not in node
    assert "user_message" not in node
    assert "assistant_message" not in node


def test_model_context_starts_at_latest_compact_summary():
    tmp = tempfile.mkdtemp(prefix="chattree_compact_projection_")
    try:
        manager = _manager(tmp)
        conv = manager.create_conversation("compact projection")
        conv.metadata["provider_id"] = "fake"
        conv.metadata["model_id"] = "fake-model"
        _add_turn(manager, conv, "old question", "old answer")
        _add_compact_node(manager, conv, "Summary:\nold question was answered", messages_to_keep=0)
        _add_turn(manager, conv, "new question")

        messages = manager._prepare_messages_for_api_with_conversation(conv)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    contents = [message["content"] for message in messages]
    assert any("This session is being continued" in content for content in contents)
    assert contents[-1] == "new question"
    assert "old question" not in contents
    assert "old answer" not in contents
    assert "Conversation compacted" not in contents


def test_manual_compact_saves_boundary_summary_and_moves_current_node():
    tmp = tempfile.mkdtemp(prefix="chattree_compact_")
    try:
        manager = _manager(tmp)
        conv = manager.create_conversation("manual compact")
        conv.metadata["provider_id"] = "fake"
        conv.metadata["model_id"] = "fake-model"
        _add_turn(manager, conv, "old question", "old answer")

        result = asyncio.run(manager.compact_conversation(conv.metadata["id"]))

        reloaded = manager.get_conversation(conv.metadata["id"])
        current = reloaded.nodes[reloaded.current_node_id]
        assert result["node_id"] == reloaded.current_node_id
        messages = _messages_for_node(manager, conv.metadata["id"], current["id"])
        boundary = next(message for message in messages if message.get("subtype") == "compact_boundary")
        summary = next(message for message in messages if message.get("subtype") == "compact_summary")
        assert boundary["trigger"] == "manual"
        assert summary["is_visible_in_transcript_only"] is True
        assert "Primary Request and Intent" in summary["content"]
        assert "<analysis>" not in summary["content"]

        compact_call = manager.model_manager.provider.calls[-1]
        assert compact_call["tools"] is None
        assert compact_call["tool_choice"] is None
        assert compact_call["max_tokens"] == 20000
        assert compact_call["temperature"] == 0
        assert compact_call["messages"][-1]["role"] == "user"
        assert "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools." in compact_call["messages"][-1]["content"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compact_restores_recent_import_file_context_after_summary():
    tmp = tempfile.mkdtemp(prefix="chattree_compact_files_")
    try:
        manager = _manager(tmp)
        conv = manager.create_conversation("file restore")
        conv.metadata["provider_id"] = "fake"
        conv.metadata["model_id"] = "fake-model"
        manager.storage.save_import_file(conv.metadata["id"], "notes.txt", "important file facts".encode("utf-8"))
        _add_turn(
            manager,
            conv,
            "'''USER MENTIONED FILES: notes.txt '''\n\n<file>\nstale inline copy\n</file>\n\n---\n\nuse this file",
            "read it",
        )

        asyncio.run(manager.compact_conversation(conv.metadata["id"], messages_to_keep=0))

        reloaded = manager.get_conversation(conv.metadata["id"])
        compact_node = reloaded.nodes[reloaded.current_node_id]
        boundary = next(
            message for message in _messages_for_node(manager, conv.metadata["id"], compact_node["id"])
            if message.get("subtype") == "compact_boundary"
        )
        restored = boundary["restored_files"]
        assert restored == [{"filename": "notes.txt", "content": "important file facts", "truncated": False}]

        messages = manager._prepare_messages_for_api_with_conversation(reloaded)
        contents = [message["content"] for message in messages]
        summary_index = next(i for i, content in enumerate(contents) if "This session is being continued" in content)
        restored_index = next(i for i, content in enumerate(contents) if "Restored file context" in content)
        assert restored_index > summary_index
        assert "notes.txt" in contents[restored_index]
        assert "important file facts" in contents[restored_index]
        assert "stale inline copy" not in contents[restored_index]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_import_file_references_are_injected_as_model_context_without_mutating_user_text():
    tmp = tempfile.mkdtemp(prefix="chattree_import_context_")
    try:
        manager = _manager(tmp)
        conv = manager.create_conversation("file references")
        manager.storage.save_import_file(conv.metadata["id"], "notes with space.txt", "important file facts".encode("utf-8"))
        _add_turn(
            manager,
            conv,
            "请根据这个文件回答",
            import_files=[{"filename": "notes with space.txt"}],
        )

        messages = manager._prepare_messages_for_api_with_conversation(conv)

        assert messages[-2]["role"] == "user"
        assert messages[-2]["content"] == "请根据这个文件回答"
        assert "import_files" not in messages[-2]
        assert messages[-1]["role"] == "system"
        assert "User attached file `notes with space.txt`" in messages[-1]["content"]
        assert "important file facts" in messages[-1]["content"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compact_restores_structured_import_file_references_after_summary():
    tmp = tempfile.mkdtemp(prefix="chattree_compact_structured_files_")
    try:
        manager = _manager(tmp)
        conv = manager.create_conversation("structured file restore")
        conv.metadata["provider_id"] = "fake"
        conv.metadata["model_id"] = "fake-model"
        manager.storage.save_import_file(conv.metadata["id"], "notes with space.txt", "important file facts".encode("utf-8"))
        _add_turn(
            manager,
            conv,
            "use this file",
            "read it",
            import_files=[{"filename": "notes with space.txt"}],
        )

        asyncio.run(manager.compact_conversation(conv.metadata["id"], messages_to_keep=0))

        reloaded = manager.get_conversation(conv.metadata["id"])
        compact_node = reloaded.nodes[reloaded.current_node_id]
        boundary = next(
            message for message in _messages_for_node(manager, conv.metadata["id"], compact_node["id"])
            if message.get("subtype") == "compact_boundary"
        )
        restored = boundary["restored_files"]
        assert restored == [{"filename": "notes with space.txt", "content": "important file facts", "truncated": False}]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_messages_to_keep_preserves_latest_original_turn_after_summary():
    tmp = tempfile.mkdtemp(prefix="chattree_compact_keep_")
    try:
        manager = _manager(tmp)
        conv = manager.create_conversation("compact keep")
        conv.metadata["provider_id"] = "fake"
        conv.metadata["model_id"] = "fake-model"
        _add_turn(manager, conv, "first old question", "first old answer")
        _add_turn(manager, conv, "latest kept question", "latest kept answer")
        _add_compact_node(manager, conv, "Summary:\nolder work", messages_to_keep=1)
        contents = [message["content"] for message in manager._prepare_messages_for_api_with_conversation(conv)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    assert any("This session is being continued" in content for content in contents)
    assert "latest kept question" in contents
    assert "latest kept answer" in contents
    assert "first old question" not in contents
    assert "first old answer" not in contents


def test_microcompact_shortens_large_tool_results_without_touching_user_text():
    messages = [
        {"role": "user", "content": "u" * 12000},
        {"role": "tool", "content": "x" * 12000, "tool_call_id": "call-1", "name": "shell"},
    ]

    compacted = microcompact_messages(messages, max_tool_content_chars=2000)

    assert compacted[0]["content"] == "u" * 12000
    assert len(compacted[1]["content"]) < 2600
    assert "[microcompact]" in compacted[1]["content"]
    assert "12000 chars" in compacted[1]["content"]


def test_send_message_auto_compacts_when_context_usage_reaches_90_percent():
    tmp = tempfile.mkdtemp(prefix="chattree_auto_compact_")
    try:
        manager = _manager(tmp)
        conv = manager.create_conversation("auto compact")
        conv.metadata["provider_id"] = "fake"
        conv.metadata["model_id"] = "fake-model"

        old = _add_turn(manager, conv, "old question", "old answer")
        old["usage"]["active_context_usage"] = {
            "input_tokens": 179999,
            "output_tokens": 1,
            "total_tokens": 180000,
            "source": "api",
        }
        target_parent_id = old["id"]
        _add_turn(manager, conv, "other branch", "other answer", parent_id=conv.root_node_id, focus=False)
        manager.chat_repository.save(conv)

        asyncio.run(_drain(manager.send_message_stream(
            conv.metadata["id"],
            "new question",
            model_id="fake-model",
            parent_node_id=target_parent_id,
        )))

        reloaded = manager.get_conversation(conv.metadata["id"])
        chain = reloaded.get_node_chain(reloaded.current_node_id)
        compact_nodes = [
            node for node in chain
            if any(message.get("subtype") == "compact_boundary" for message in _messages_for_node(manager, conv.metadata["id"], node["id"]))
        ]
        assert compact_nodes
        assert compact_nodes[-1]["id"] == chain[-2]["id"]
        boundary = next(
            message for message in _messages_for_node(manager, conv.metadata["id"], chain[-2]["id"])
            if message.get("subtype") == "compact_boundary"
        )
        assert boundary["trigger"] == "auto"
        assert chain[-2]["parent_id"] == target_parent_id
        user_message = next(
            message for message in _messages_for_node(manager, conv.metadata["id"], chain[-1]["id"])
            if message.get("role") == Role.USER
        )
        assert user_message["content"] == "new question"
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
