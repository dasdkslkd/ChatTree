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
from backend.core.chat.conversation import Conversation
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
                          error=None, tokens_used=self._total)


class FakeModelManager:
    def __init__(self):
        self.model_list = {"fake": ["fake-model"]}
        self._p = FakeProvider()

    def get_model(self, provider, is_async=False):
        return self._p


async def drain(stream):
    async for _ in stream:
        pass


async def main():
    tmp = tempfile.mkdtemp(prefix="chattree_test_")
    try:
        storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
        prompts = PromptStorage(storage_dir=os.path.join(tmp, "prompts"))
        cm = ChatManager(FakeModelManager(), storage, prompts)

        conv = cm.create_conversation("race test")
        cid = conv.metadata["id"]

        # 两路并发流式，同一对话
        await asyncio.gather(
            drain(cm.send_message_stream(cid, "msg A", model_id="fake-model")),
            drain(cm.send_message_stream(cid, "msg B", model_id="fake-model")),
        )

        # 断言：两个新节点都在盘上（root + 2），无节点文件被并发保存删除
        nodes_dir = storage._get_nodes_dir(cid)
        node_files = [f for f in os.listdir(nodes_dir) if f.endswith(".json")]
        assert len(node_files) == 3, f"期望 3 个节点文件(root+2)，实际 {len(node_files)}"

        # 断言：可重新加载，且两个非根节点都从 root 可达（树连通、无孤儿）
        reloaded = Conversation.from_dict(storage.load(cid))
        assert len(reloaded.nodes) == 3, f"加载后应有 3 个节点，实际 {len(reloaded.nodes)}"
        non_root = [nid for nid in reloaded.nodes if nid != reloaded.root_node_id]
        assert len(non_root) == 2, f"应有 2 个非根节点，实际 {len(non_root)}"
        for nid in non_root:
            chain = reloaded.get_node_chain(nid)
            assert chain and chain[0]["id"] == reloaded.root_node_id, f"节点 {nid} 未能从 root 可达"
            # 每个新节点都应带 assistant 消息
            assert reloaded.nodes[nid]["assistant_message"] is not None, f"节点 {nid} 缺少助手消息"

        # 断言：token 统计正确（两轮各 42，累加 84）
        total = reloaded.metadata["total_tokens"].get("fake", 0)
        assert total == 84, f"total_tokens 应为 84，实际 {total}"

        # 断言：每个 assistant 节点的 generation_info.tokens_used == 42
        for nid in non_root:
            gi = reloaded.nodes[nid]["assistant_message"]["generation_info"]
            assert gi["tokens_used"] == 42, f"节点 token 应为 42，实际 {gi['tokens_used']}"

        print("PASS: 并发不丢节点，树连通，token 统计正确")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
