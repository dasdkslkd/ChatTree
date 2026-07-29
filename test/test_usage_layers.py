import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.conversation import Conversation
from backend.core.chat.node import NodeManager
from backend.core.config.types import StreamChunk, StreamController, StreamStatus
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage


class SequencedUsageProvider:
    def __init__(self, totals):
        self._totals = list(totals)

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        total = self._totals.pop(0)
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
            content=f"ok {total}",
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
            tokens_used=total,
            usage_info={
                "input_tokens": 3,
                "output_tokens": total - 3,
                "total_tokens": total,
                "source": "api",
                "raw": {"total_tokens": total},
            },
        )


class SequencedUsageModelManager:
    def __init__(self):
        self.model_list = {"fake": ["fake-model"]}
        self._provider = SequencedUsageProvider([7, 11])

    def get_model(self, provider, is_async=False):
        return self._provider


async def drain(stream):
    async for _ in stream:
        pass


def test_new_nodes_start_with_layered_usage():
    root = NodeManager.create_root_node()
    child = NodeManager.create_node(
        parent_id=root["id"],
        model_id="fake-model",
    )

    for node in (root, child):
        usage = node["usage"]
        assert usage["turn_usage"]["total_tokens"] == 0
        assert usage["branch_usage"]["total_tokens"] == 0
        assert usage["active_context_usage"]["total_tokens"] == 0
        assert node["branch_usage_info"]["total_tokens"] == 0
        assert node["total_tokens"] == 0


def test_aggregate_usage_is_not_reused_as_active_context():
    node = NodeManager.create_node(parent_id="parent", model_id="fake-model")
    node["usage"] = {
        "turn_usage": {"total_tokens": 11, "source": "api"},
        "branch_usage": {"total_tokens": 18, "source": "aggregate"},
        "active_context_usage": {"total_tokens": 18, "source": "aggregate"},
        "model_context_window": 200_000,
    }

    Conversation._ensure_node_usage(node)

    assert node["usage"]["active_context_usage"] == node["usage"]["turn_usage"]
    assert "model_context_window" not in node["usage"]


def test_streaming_updates_each_node_layered_usage_to_that_point():
    tmp = tempfile.mkdtemp(prefix="chattree_usage_layers_")
    try:
        storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
        prompts = PromptStorage(storage_dir=os.path.join(tmp, "prompts"))
        cm = ChatManager(SequencedUsageModelManager(), storage, prompts)
        conv = cm.create_conversation("usage layers")
        cid = conv.metadata["id"]

        async def run():
            await drain(cm.send_message_stream(
                cid,
                "first",
                model_id="fake-model",
                parent_node_id=conv.root_node_id,
            ))
            current = cm.get_conversation(cid)
            assert current is not None
            await drain(cm.send_message_stream(
                cid,
                "second",
                model_id="fake-model",
                parent_node_id=current.current_node_id,
            ))

        asyncio.run(run())

        reloaded = cm.get_conversation(cid)
        chain = reloaded.get_node_chain(reloaded.current_node_id)
        non_root = [node for node in chain if node["id"] != reloaded.root_node_id]
        assert len(non_root) == 2

        first, second = non_root
        assert first["usage"]["turn_usage"]["total_tokens"] == 7
        assert first["usage"]["branch_usage"]["total_tokens"] == 7
        assert first["usage"]["active_context_usage"]["total_tokens"] == 7
        assert first["total_tokens"] == 7
        assert first["branch_usage_info"]["total_tokens"] == 7

        assert second["usage"]["turn_usage"]["total_tokens"] == 11
        assert second["usage"]["branch_usage"]["total_tokens"] == 18
        assert second["usage"]["active_context_usage"]["total_tokens"] == 11
        assert second["total_tokens"] == 18
        assert second["branch_usage_info"]["total_tokens"] == 18

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
