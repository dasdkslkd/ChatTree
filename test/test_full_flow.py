import asyncio
import sys

sys.path.insert(0, ".")

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.canonical_reader import messages_by_node
from backend.core.config.types import Role, StreamChunk, StreamController, StreamStatus
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from model_route_support import fake_model_route


class FullFlowProvider:
    def __init__(self):
        self.calls = []

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.calls.append([(message.get("role"), message.get("content")) for message in messages])
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
            content="assistant answer",
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
            tokens_used=5,
            usage_info={
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
                "source": "test",
            },
        )


class FullFlowModelManager:
    def __init__(self):
        self.model_list = {"fake": ["fake-model"]}
        self.provider = FullFlowProvider()

    def get_route(self, provider, model):
        return fake_model_route(provider, model)

    def get_model(self, provider, model, is_async=False):
        return self.provider

    def get_model_metadata(self, provider, model):
        return self.get_route(provider, model)["capabilities"]


async def _drain(stream):
    async for _ in stream:
        pass


def _role_value(role):
    return getattr(role, "value", role)


def test_stream_round_persists_and_replays_canonical_messages(tmp_path):
    manager = ChatManager(
        FullFlowModelManager(),
        ChatStorage(storage_dir=str(tmp_path / "conversations")),
        PromptStorage(storage_dir=str(tmp_path / "prompts")),
    )
    conversation = manager.create_conversation("full flow")

    asyncio.run(_drain(manager.send_message_stream(
        conversation.metadata["id"],
        "hello",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    )))

    reloaded = manager.get_conversation(conversation.metadata["id"])
    assert reloaded is not None
    node_id = reloaded.current_node_id
    stored = messages_by_node(manager.chat_repository, conversation.metadata["id"], [node_id])[node_id]
    assert [(_role_value(message.get("role")), message.get("content")) for message in stored] == [
        (Role.USER.value, "hello"),
        (Role.ASSISTANT.value, "assistant answer"),
    ]

    prompt_messages = manager._prepare_messages_for_api_with_conversation(reloaded)
    assert prompt_messages[-2]["content"] == "hello"
    assert prompt_messages[-1]["content"] == "assistant answer"
