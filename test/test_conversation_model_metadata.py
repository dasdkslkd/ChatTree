import asyncio
import sys
from pathlib import Path

sys.path.insert(0, ".")

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.conversation import Conversation
from backend.core.config.config import cfg
from backend.core.config.types import StreamChunk, StreamController, StreamStatus
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage


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
            content="ok",
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
            tokens_used=3,
            usage_info={
                "input_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 3,
                "source": "api",
                "raw": {},
            },
        )


class FakeModelManager:
    def __init__(self):
        self.model_list = {
            "packyapi-gpt": ["codex-auto-review"],
            "ustc": ["deepseek-v4-pro"],
            "first": ["shared-model"],
            "second": ["shared-model"],
        }
        self._provider = FakeProvider()
        self.get_model_calls = []

    def get_model(self, provider, is_async=False):
        self.get_model_calls.append((provider, is_async))
        return self._provider

    def get_model_metadata(self, provider_id, model_name):
        return {}


def make_chat_manager(tmp_path: Path) -> ChatManager:
    return ChatManager(
        FakeModelManager(),
        ChatStorage(str(tmp_path / "conversations")),
        PromptStorage(str(tmp_path / "prompts.json")),
    )


async def drain(stream):
    async for _ in stream:
        pass


async def collect_chunks(stream):
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    return chunks


def stream_from_current(manager, conversation_id, content, **kwargs):
    conversation = manager.get_conversation(conversation_id)
    assert conversation is not None
    return manager.send_message_stream(
        conversation_id,
        content,
        parent_node_id=conversation.current_node_id,
        **kwargs,
    )


def test_stream_persists_resolved_provider_and_model_to_conversation_metadata(tmp_path):
    manager = make_chat_manager(tmp_path)
    conversation = manager.create_conversation("metadata")
    conversation_id = conversation.metadata["id"]

    asyncio.run(
        drain(stream_from_current(manager,
            conversation_id,
            "hello",
            model_id="deepseek-v4-pro",
        ))
    )

    reloaded = manager.get_conversation(conversation_id)
    assert reloaded is not None
    assert reloaded.metadata["model_id"] == "deepseek-v4-pro"
    assert reloaded.metadata["provider_id"] == "ustc"

    listed = manager.list_conversations()
    assert listed[0]["model_id"] == "deepseek-v4-pro"
    assert listed[0]["provider_id"] == "ustc"


def test_stream_respects_request_provider_when_model_name_is_shared(tmp_path):
    manager = make_chat_manager(tmp_path)
    conversation = manager.create_conversation("shared model")
    conversation_id = conversation.metadata["id"]

    asyncio.run(
        drain(stream_from_current(manager,
            conversation_id,
            "hello",
            model_id="shared-model",
            provider_id="second",
        ))
    )

    reloaded = manager.get_conversation(conversation_id)
    assert reloaded is not None
    assert reloaded.metadata["model_id"] == "shared-model"
    assert reloaded.metadata["provider_id"] == "second"
    assert manager.model_manager.get_model_calls[-1] == ("second", True)


def test_stream_persists_tool_permission_mode_per_new_leaf_node(tmp_path):
    manager = make_chat_manager(tmp_path)
    conversation = manager.create_conversation("tool permission mode")
    conversation_id = conversation.metadata["id"]

    asyncio.run(
        drain(stream_from_current(manager,
            conversation_id,
            "first",
            model_id="deepseek-v4-pro",
            tool_permission_mode="auto_approve",
        ))
    )
    first_node_id = manager.get_conversation(conversation_id).current_node_id

    asyncio.run(
        drain(stream_from_current(manager,
            conversation_id,
            "second",
            model_id="deepseek-v4-pro",
            tool_permission_mode="modify_only",
        ))
    )
    second_node_id = manager.get_conversation(conversation_id).current_node_id

    reloaded = manager.get_conversation(conversation_id)
    assert reloaded is not None
    assert reloaded.nodes[first_node_id]["tool_permission_mode"] == "auto_approve"
    assert reloaded.nodes[second_node_id]["tool_permission_mode"] == "modify_only"


def test_stream_defaults_tool_permission_mode_to_auto_approve(tmp_path):
    manager = make_chat_manager(tmp_path)
    conversation = manager.create_conversation("tool permission default")
    conversation_id = conversation.metadata["id"]

    asyncio.run(
        drain(stream_from_current(manager,
            conversation_id,
            "first",
            model_id="deepseek-v4-pro",
        ))
    )
    first_node_id = manager.get_conversation(conversation_id).current_node_id

    reloaded = manager.get_conversation(conversation_id)
    assert reloaded is not None
    assert reloaded.nodes[first_node_id]["tool_permission_mode"] == "auto_approve"


def test_stream_uses_configured_default_tool_permission_mode(tmp_path):
    previous = dict(cfg.data)
    try:
        cfg.data = {**cfg.data, "tools": {**(cfg.data.get("tools") or {}), "default_permission_mode": "ask_always"}}
        manager = make_chat_manager(tmp_path)
        conversation = manager.create_conversation("tool permission configured default")
        conversation_id = conversation.metadata["id"]

        asyncio.run(
            drain(stream_from_current(
                manager,
                conversation_id,
                "first",
                model_id="deepseek-v4-pro",
            ))
        )
        first_node_id = manager.get_conversation(conversation_id).current_node_id

        reloaded = manager.get_conversation(conversation_id)
        assert reloaded is not None
        assert reloaded.nodes[first_node_id]["tool_permission_mode"] == "ask_always"
    finally:
        cfg.data = previous


def test_stream_inherits_tool_permission_mode_from_parent_node(tmp_path):
    manager = make_chat_manager(tmp_path)
    conversation = manager.create_conversation("tool permission inherit")
    conversation_id = conversation.metadata["id"]

    asyncio.run(
        drain(stream_from_current(manager,
            conversation_id,
            "first",
            model_id="deepseek-v4-pro",
            tool_permission_mode="ask_always",
        ))
    )
    first_node_id = manager.get_conversation(conversation_id).current_node_id

    asyncio.run(
        drain(stream_from_current(manager,
            conversation_id,
            "second",
            model_id="deepseek-v4-pro",
        ))
    )
    second_node_id = manager.get_conversation(conversation_id).current_node_id

    reloaded = manager.get_conversation(conversation_id)
    assert reloaded is not None
    assert reloaded.nodes[first_node_id]["tool_permission_mode"] == "ask_always"
    assert reloaded.nodes[second_node_id]["tool_permission_mode"] == "ask_always"
