import asyncio
from pathlib import Path
import sys

sys.path.insert(0, ".")

from backend.core.capabilities import CapabilitySource, load_skill_roots
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.chat.chat_manager import ChatManager
from backend.core.config.types import StreamChunk, StreamController, StreamStatus
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage


class CapturingProvider:
    def __init__(self):
        self.message_calls = []

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.message_calls.append(messages)
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


class CapturingModelManager:
    def __init__(self):
        self.model_list = {"ustc": ["deepseek-v4-pro"]}
        self.provider = CapturingProvider()

    def get_model(self, provider, is_async=False):
        return self.provider

    def get_model_metadata(self, provider_id, model_name):
        return {}


async def drain(stream):
    async for _ in stream:
        pass


def make_manager(tmp_path: Path) -> ChatManager:
    model_manager = CapturingModelManager()
    manager = ChatManager(
        model_manager,
        ChatStorage(str(tmp_path / "conversations")),
        PromptStorage(str(tmp_path / "prompts.json")),
    )
    manager.capability_registry = make_registry(tmp_path)
    return manager


def make_registry(tmp_path: Path) -> CapabilityRegistry:
    root = tmp_path / "skills"
    skill_path = root / "kimi-webbridge" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: kimi-webbridge
description: Kimi WebBridge lets AI control the user's real browser.
aliases:
  - webbridge
  - browser
---

# Kimi WebBridge

Health check first.
""",
        encoding="utf-8",
    )
    registry = CapabilityRegistry()
    registry.add_capabilities(
        load_skill_roots([root], source=CapabilitySource.PROJECT)
    )
    return registry


def messages_contain_skill(messages) -> bool:
    return any(
        message.get("role") == "system"
        and "<name>kimi-webbridge</name>" in str(message.get("content") or "")
        for message in messages
    )


def user_active_skill_names(manager, conversation_id, node_id):
    messages = manager._canonical_messages_by_node(conversation_id, [node_id]).get(node_id, [])
    user_messages = [message for message in messages if message.get("role") == "user"]
    assert user_messages
    return user_messages[-1].get("active_skill_names")


def test_skill_injection_uses_turn_intent_and_inherits_active_skill(tmp_path):
    manager = make_manager(tmp_path)
    conversation = manager.create_conversation("skills")
    conversation_id = conversation.metadata["id"]

    asyncio.run(
        drain(
            manager.send_message_stream(
                conversation_id,
                "打开 bilibili",
                model_id="deepseek-v4-pro",
                provider_id="ustc",
                parent_node_id=conversation.current_node_id,
            )
        )
    )
    reloaded = manager.get_conversation(conversation_id)
    assert reloaded is not None
    first_node = reloaded.nodes[reloaded.current_node_id]
    assert user_active_skill_names(manager, conversation_id, first_node["id"]) == ["kimi-webbridge"]
    assert messages_contain_skill(manager.model_manager.provider.message_calls[-1])

    asyncio.run(
        drain(
            manager.send_message_stream(
                conversation_id,
                "截图一下",
                model_id="deepseek-v4-pro",
                provider_id="ustc",
                parent_node_id=reloaded.current_node_id,
            )
        )
    )
    reloaded = manager.get_conversation(conversation_id)
    assert reloaded is not None
    second_node = reloaded.nodes[reloaded.current_node_id]
    assert user_active_skill_names(manager, conversation_id, second_node["id"]) == ["kimi-webbridge"]
    assert messages_contain_skill(manager.model_manager.provider.message_calls[-1])
