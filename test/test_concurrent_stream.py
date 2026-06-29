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


def test_concurrent_streams_without_explicit_node_id_use_same_start_parent():
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
                drain(cm.send_message_stream(cid, "msg A", model_id="fake-model")),
                drain(cm.send_message_stream(cid, "msg B", model_id="fake-model")),
            )

            reloaded = Conversation.from_dict(storage.load(cid))
            non_root = [node for nid, node in reloaded.nodes.items() if nid != root_id]
            assert len(non_root) == 2
            assert {node["parent_id"] for node in non_root} == {root_id}
            assert {node["user_message"]["content"] for node in non_root} == {"msg A", "msg B"}
            for call in provider.calls:
                visible_user_messages = [
                    content for role, content in call["messages"] if _role_value(role) == "user"
                ]
                assert visible_user_messages in (["msg A"], ["msg B"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    asyncio.run(run())


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
        for nid in non_root:
            gi = reloaded.nodes[nid]["assistant_message"]["generation_info"]
            assert gi["usage_info"]["total_tokens"] == 42, "usage_info should preserve turn usage"
            expected_branch_total = 42 * sum(
                1 for n in reloaded.get_node_chain(nid) if n.get("assistant_message")
            )
            if not reloaded.nodes[nid]["children_ids"]:
                assert reloaded.nodes[nid]["total_tokens"] == expected_branch_total, "leaf should preserve branch total tokens"
                assert reloaded.nodes[nid]["branch_usage_info"]["total_tokens"] == expected_branch_total, "leaf should preserve branch usage"

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
