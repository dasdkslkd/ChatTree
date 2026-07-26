"""并发流式保存竞态 + token 统计 验证。
从仓库根运行：  python test/test_concurrent_stream.py
"""
import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.canonical_reader import messages_by_node
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.config.types import StreamChunk, StreamStatus, StreamController


class FakeProvider:
    """假 provider：在 CONTENT chunk 间插入 sleep，放大并发窗口；COMPLETE 带 token 总量。"""
    def __init__(self, total_tokens=42, deltas=("Hello", " ", "world")):
        self._total = total_tokens
        self._deltas = deltas

    async def generate_response_stream(self, model, messages, stream_controller: StreamController = None, **kw):
        yield StreamChunk(status=StreamStatus.START, content=None,
                          node_id=stream_controller.node_id,
                          conversation_id=stream_controller.conversation_id,
                          error=None, tokens_used=0)
        for d in self._deltas:
            await asyncio.sleep(0.05)
            yield StreamChunk(status=StreamStatus.CONTENT, content=d,
                              node_id=stream_controller.node_id,
                              conversation_id=stream_controller.conversation_id,
                              error=None, tokens_used=1)
        yield StreamChunk(status=StreamStatus.COMPLETE, content=None,
                          node_id=stream_controller.node_id,
                          conversation_id=stream_controller.conversation_id,
                          error=None, tokens_used=self._total,
                          usage_info={
                              "input_tokens": 10,
                              "output_tokens": self._total - 10,
                              "total_tokens": self._total,
                              "source": "api",
                              "raw": {"total_tokens": self._total},
                           })


class CapturingProvider(FakeProvider):
    def __init__(self):
        super().__init__(total_tokens=1, deltas=("ok",))
        self.calls = []

    async def generate_response_stream(self, model, messages, stream_controller: StreamController = None, **kw):
        self.calls.append({
            "node_id": stream_controller.node_id,
            "messages": [(m.get("role"), m.get("content")) for m in messages],
        })
        async for chunk in super().generate_response_stream(model, messages, stream_controller, **kw):
            yield chunk


class FakeModelManager:
    def __init__(self, provider=None):
        self.model_list = {"fake": ["fake-model"]}
        self._p = provider or FakeProvider()

    def get_model(self, provider, is_async=False):
        return self._p


async def drain(stream):
    async for _ in stream:
        pass


def _role_value(role):
    return getattr(role, "value", role)


def _node_messages(manager, conversation_id, node_id):
    return messages_by_node(manager.chat_repository, conversation_id, [node_id]).get(node_id, [])


def test_concurrent_streams_with_explicit_parent_create_sibling_nodes():
    async def run():
        tmp = tempfile.mkdtemp(prefix="chattree_test_")
        try:
            provider = CapturingProvider()
            storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
            prompts = PromptStorage(storage_dir=os.path.join(tmp, "prompts"))
            cm = ChatManager(FakeModelManager(provider), storage, prompts)

            conv = cm.create_conversation("race parent test")
            cid = conv.metadata["id"]
            root_id = conv.root_node_id

            await asyncio.gather(
                drain(cm.send_message_stream(cid, "msg A", model_id="fake-model", parent_node_id=root_id)),
                drain(cm.send_message_stream(cid, "msg B", model_id="fake-model", parent_node_id=root_id)),
            )

            reloaded = cm.get_conversation(cid)
            non_root = [node for nid, node in reloaded.nodes.items() if nid != root_id]
            assert len(non_root) == 2
            assert {node["parent_id"] for node in non_root} == {root_id}
            user_contents = {
                message["content"]
                for node in non_root
                for message in _node_messages(cm, cid, node["id"])
                if _role_value(message.get("role")) == "user"
            }
            assert user_contents == {"msg A", "msg B"}
            for call in provider.calls:
                visible_user_messages = [
                    content for role, content in call["messages"] if _role_value(role) == "user"
                ]
                assert visible_user_messages in (["msg A"], ["msg B"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    asyncio.run(run())


if __name__ == "__main__":
    test_concurrent_streams_with_explicit_parent_create_sibling_nodes()
