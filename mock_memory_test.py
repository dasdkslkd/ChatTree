"""本地 mock 数据测试：验证 memory 压缩注入(M3) 与 housekeeping 隐藏(H1) 机制的实际效果。

运行方式：
    python mock_memory_test.py
"""
import asyncio
import json
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, ".")

from backend.core.chat.chat_manager import ChatManager
from backend.core.config.config import cfg
from backend.core.config.types import Message, Role, StreamChunk, StreamController, StreamStatus
from backend.core.memory import MemoryStore
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.prompts.runtime_context import build_memory_section
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.tools.exposure import is_housekeeping_tool
from backend.core.tools.memory import MemoryTool
from backend.core.tools.tool_manager import ToolManager
from backend.core.transcript import TranscriptAssembler


# ---------------------------------------------------------------------------
# 可捕获的 Provider：记录发给模型的 messages，便于断言 memory 是否被注入
# ---------------------------------------------------------------------------
class CapturingProvider:
    def __init__(self):
        self.compact_messages = None
        self.chat_messages = None

    def generate_response(self, model, messages, **kwargs):
        # compact_conversation 走非流式 generate_response
        self.compact_messages = [dict(m) for m in messages]
        return "Summary:\ncanonical compact facts", 7

    async def generate_response_stream(self, model, messages, stream_controller=None, **kwargs):
        self.chat_messages = [dict(m) for m in messages]
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
            tokens_used=1,
            metadata={"finish_reason": "stop"},
        )


class StubModelManager:
    """最小 model manager：只解析路由并委托给可捕获的 provider。"""
    def __init__(self):
        self.provider = CapturingProvider()
        self.model_list = {"fake": ["fake-model"]}

    def get_route(self, provider_id, model_name):
        return {"provider": provider_id, "model": model_name, "route_id": "r", "protocol": "openai_chat"}

    def get_model(self, provider, model, is_async=False):
        return self.provider


def _make_manager(tmp: Path, store: MemoryStore, cfg_data: dict):
    persistence = SQLitePersistence(tmp / "sqlite")
    persistence.initialize()
    repository = ChatRepository(persistence)
    manager = ChatManager(
        StubModelManager(),
        ChatStorage(str(tmp / "conversations")),
        PromptStorage(str(tmp / "prompts")),
        chat_repository=repository,
        memory_store=store,
    )
    return manager, repository, persistence


def test_compact_injects_memory_section():
    print("\n=== [M3] compact 压缩消息注入 Memory PromptSection ===")
    tmp = Path(tempfile.mkdtemp())
    # 构造真实记忆文件
    project_id = "019c2d2f-27d8-7b8c-b246-e73fe95b42db"
    memories = tmp / "memories"
    (memories / "projects").mkdir(parents=True)
    (memories / "USER.md").write_text("# User Memory\n\n- prefers Chinese replies\n", encoding="utf-8")
    (memories / "MACHINE.md").write_text("# Machine Memory\n\n- corporate TLS enforcement\n", encoding="utf-8")
    (memories / "projects" / f"{project_id}.md").write_text(
        "# Project Memory\n\n- run tests with pytest\n",
        encoding="utf-8",
    )
    store = MemoryStore(tmp)
    cfg_data = {
        "memory": {"enabled": True},
        "projects": {
            "D:\\Workspace\\ChatTree": {
                "id": project_id,
                "roots": ["D:\\Workspace\\ChatTree"],
                "label": "ChatTree",
            }
        },
        "tools": {"builtin": {"enabled": True}},
    }
    previous = cfg.data
    cfg.data = cfg_data
    try:
        manager, _repo, _ = _make_manager(tmp, store, cfg_data)
        conversation = manager.create_conversation("compact memory test")
        conversation.metadata["provider_id"] = "fake"
        conversation.metadata["model_id"] = "fake-model"
        # 会话 workspace 绑定 project_id 与 cwd，使记忆文件被解析到
        conversation.metadata["workspace"] = {
            "project_id": project_id,
            "cwd": "D:\\Workspace\\ChatTree",
            "workspace_roots": ["D:\\Workspace\\ChatTree"],
        }
        manager.chat_repository.save(conversation)

        # 先直接验证 build_memory_section 产出
        section = build_memory_section(
            conversation.metadata["workspace"],
            cfg_data,
            store,
        )
        assert section is not None, "build_memory_section 应返回非空段落"
        print("Memory PromptSection priority =", section.priority)
        print("--- Memory 段落内容 ---")
        print(section.content)
        print("------------------------")

        asyncio.run(manager.compact_conversation(conversation.metadata["id"]))
        compact_msgs = manager.model_manager.provider.compact_messages
        assert compact_msgs is not None, "compact 应调用 provider.generate_response"
        system_contents = [m["content"] for m in compact_msgs if m["role"] == "system"]
        memory_injected = any("## Memory" in c and "- prefers Chinese replies" in c for c in system_contents)
        print(f"compact 请求消息数: {len(compact_msgs)}")
        print(f"compact 中 system 消息含 Memory 段落: {memory_injected}")
        assert memory_injected, "M3 失败：compact 请求中未注入 Memory PromptSection"

        # 关闭记忆后再压缩，应不注入
        cfg.data = {**cfg_data, "memory": {"enabled": False}}
        manager.model_manager.provider.compact_messages = None
        asyncio.run(manager.compact_conversation(conversation.metadata["id"]))
        compact_msgs2 = manager.model_manager.provider.compact_messages
        system2 = [m["content"] for m in compact_msgs2 if m["role"] == "system"]
        not_injected = not any("## Memory" in c for c in system2)
        print(f"关闭记忆后 compact 不再注入 Memory: {not_injected}")
        assert not_injected, "M3 失败：关闭 memory.enabled 后 compact 仍注入 Memory"
        print("M3 PASSED\n")
    finally:
        cfg.data = previous


def test_housekeeping_hidden_but_canonical():
    print("\n=== [H1] memory 工具 housekeeping：公开隐藏 / canonical 保留 ===")
    tmp = Path(tempfile.mkdtemp())
    persistence = SQLitePersistence(tmp / "sqlite")
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation(title="memory hidden")
    node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    from backend.core.persistence.run_repository import SQLiteRunRepository
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id, kind="chat", target_node_id=node_id, summary="memory",
    )
    # 写入一条 memory 工具调用 + 结果（模拟已发生的真实调用）
    repository.add_tool_call(
        conversation_id, node_id,
        tool_call_id="call-memory", name="memory",
        arguments={"action": "add", "scope": "user", "content": "value"},
        run_id=run_id,
    )
    repository.add_tool_result(
        conversation_id, node_id,
        tool_result_id="result-memory", tool_call_id="call-memory",
        output="ok", run_id=run_id,
    )

    assembler = TranscriptAssembler(persistence)
    snapshot = assembler.snapshot(conversation_id, node_id)
    public_leaks = [
        (item, block["tool_name"])
        for item in snapshot["items"]
        for block in item.get("blocks", [])
        if block.get("tool_name") == "memory"
    ]
    print(f"公开 transcript 中泄露 memory 的项数: {len(public_leaks)}")
    assert not public_leaks, "H1 失败：memory 出现在公开 transcript 中"

    with persistence.connect() as conn:
        calls = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE name='memory'").fetchone()[0]
        results = conn.execute(
            "SELECT COUNT(*) FROM tool_results WHERE tool_call_id='call-memory'"
        ).fetchone()[0]
    print(f"canonical SQLite 保留 memory tool_call: {calls}, tool_result: {results}")
    assert calls == 1 and results == 1, "H1 失败：canonical 历史未保留 memory 调用配对"

    # 实时流式事件过滤验证：内存中重放 tool_calls_committed 事件
    session = assembler.patch_session(run_id)
    patch = session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "event_type": "tool_calls_committed",
        "tool_calls": [{
            "id": "call-live-memory",
            "function": {"name": "memory", "arguments": '{"action":"add"}'},
        }],
    })
    print(f"包含 memory 的实时 tool_calls_committed 事件产生的公开 patch: {patch}")
    assert patch is None, "H1 失败：实时流式事件泄露 memory tool call"

    # is_housekeeping_tool 单一事实来源
    assert is_housekeeping_tool("memory") is True
    assert is_housekeeping_tool("read") is False
    print("is_housekeeping_tool(memory)=True, is_housekeeping_tool(read)=False")

    # ToolManager 暴露校验：plan/agent 不暴露，enabled 才暴露
    from backend.core.tools.exposure import ToolExposureContext
    mgr = ToolManager({
        "tools": {"enabled": True, "builtin": {"enabled": False}},
        "memory": {"enabled": True},
    })
    mgr.register(MemoryTool(MemoryStore(tmp)))
    def names(ctx):
        return {s["function"]["name"] for s in mgr.get_openai_tools(exposure_context=ctx)}
    assert "memory" in names(ToolExposureContext(run_kind="chat"))
    assert "memory" not in names(ToolExposureContext(run_kind="chat", permission_mode="plan"))
    assert "memory" not in names(ToolExposureContext(run_kind="agent"))
    mgr._config = {**mgr._config, "memory": {"enabled": False}}
    assert "memory" not in names(ToolExposureContext(run_kind="chat"))
    print("工具暴露控制：chat 暴露、plan/agent 不暴露、关闭后不暴露 —— 全部符合")
    print("H1 PASSED\n")


if __name__ == "__main__":
    test_compact_injects_memory_section()
    test_housekeeping_hidden_but_canonical()
    print("ALL MOCK TESTS PASSED")