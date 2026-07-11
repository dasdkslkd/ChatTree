import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core.chat.chat_manager import ChatManager
from backend.core.config.types import StreamChunk, StreamController, StreamStatus
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage


class RaisingProvider:
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
        raise RuntimeError("upstream quota exceeded")


class FakeModelManager:
    model_list = {"fake-provider": ["fake-model"]}

    def get_model(self, provider, is_async=False):
        return RaisingProvider()


async def _provider_exception_streams_and_persists_real_error(tmp_path):
    chat_manager = ChatManager(
        FakeModelManager(),
        ChatStorage(storage_dir=str(tmp_path / "conversations")),
        PromptStorage(storage_dir=str(tmp_path / "prompts")),
    )
    conversation = chat_manager.create_conversation("provider error")

    chunks = [
        chunk
        async for chunk in chat_manager.send_message_stream(
            conversation.metadata["id"],
            "hello",
            model_id="fake-model",
            parent_node_id=conversation.current_node_id,
        )
    ]

    error_chunk = next(chunk for chunk in chunks if chunk.get("status") == StreamStatus.ERROR)
    assert error_chunk["error"] == "upstream quota exceeded"
    assert error_chunk["node_id"]

    saved = chat_manager.get_conversation(conversation.metadata["id"])
    assistant_message = saved.nodes[error_chunk["node_id"]]["assistant_message"]
    assert assistant_message["generation_info"]["status"] == "error"
    assert assistant_message["generation_info"]["error_message"] == "upstream quota exceeded"


def test_provider_exception_streams_and_persists_real_error(tmp_path):
    asyncio.run(_provider_exception_streams_and_persists_real_error(tmp_path))
