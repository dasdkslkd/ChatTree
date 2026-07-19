import asyncio
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, ".")

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.conversation import Conversation
from backend.core.chat.node import NodeManager
from backend.core.chat.prune_summary import build_prune_packets
from backend.core.config.types import Message, Role
from backend.core.runs import RunKind, RunManager
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.slash import SlashCommandDispatcher, SlashDispatchKind
from backend.api.routes.messages import (
    SendMessageRequest,
    _parse_prune_summary_args,
    _produce_prune_summary,
    _subscribe_sse,
)


async def _prune_summary_stream(conversation_id, request, chat_manager):
    run_manager = RunManager()
    slash_result = SlashCommandDispatcher().dispatch(request.content)
    target_node_id, _ = _parse_prune_summary_args(
        slash_result.args,
        request.parent_node_id,
    )
    run = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.DIRECT_RESPONSE,
        anchor_node_id=target_node_id or request.parent_node_id,
        summary=request.content[:80],
    )
    producer = asyncio.create_task(
        _produce_prune_summary(
            run=run,
            conversation_id=conversation_id,
            request=request,
            slash_result=slash_result,
            chat_manager=chat_manager,
            run_manager=run_manager,
        )
    )
    async for event in _subscribe_sse(run_manager, run.run_id, from_event=1):
        yield event
    await producer


class PruneProvider:
    def __init__(self):
        self.calls = []

    def generate_response(self, model, messages, max_tokens=None, temperature=None, tools=None, tool_choice=None, **kwargs):
        self.calls.append({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": tools,
            "tool_choice": tool_choice,
        })
        return (
            "<summary>\n"
            "1. Parent Context:\n"
            "   Parent work.\n\n"
            "2. Branch Results:\n"
            "   Branches found durable fact.\n\n"
            "4. Durable Context To Inject:\n"
            "   Durable fact from child branches.\n"
            "</summary>",
            17,
        )


class PruneModelManager:
    def __init__(self):
        self.provider = PruneProvider()
        self.model_list = {"fake": ["fake-model"]}

    def get_model(self, provider, is_async=False):
        return self.provider

    def get_model_metadata(self, provider_id, model_name):
        return {"context_length": 200000}


class SlowPruneProvider(PruneProvider):
    def generate_response(self, *args, **kwargs):
        time.sleep(0.35)
        return super().generate_response(*args, **kwargs)


class SlowPruneModelManager(PruneModelManager):
    def __init__(self):
        self.provider = SlowPruneProvider()
        self.model_list = {"fake": ["fake-model"]}


def _message(role, content):
    return Message({
        "id": f"{role}-{content}",
        "role": role,
        "content": content,
        "timestamp": 1,
    })


def _node(content, assistant=None):
    node = NodeManager.create_node(_message(Role.USER, content), model_id="fake-model")
    if assistant is not None:
        node["assistant_message"] = _message(Role.ASSISTANT, assistant)
    return node


def _make_manager():
    tmp = tempfile.mkdtemp(prefix="chattree_prune_")
    storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
    prompts = PromptStorage(storage_dir=os.path.join(tmp, "prompts"))
    return tmp, ChatManager(PruneModelManager(), storage, prompts)


def _make_tree(manager):
    conv = manager.create_conversation("prune")
    root_id = conv.current_node_id
    parent = _node("parent task", "parent answer")
    conv.add_node(parent, root_id)
    child_a = _node("branch a", "result a")
    conv.add_node(child_a, parent["id"], focus=False)
    child_b = _node("branch b", "result b")
    conv.add_node(child_b, parent["id"], focus=False)
    conv.switch_to_node(parent["id"])
    manager._save(conv)
    return conv, parent, child_a, child_b


def test_build_prune_packets_preserves_branch_boundaries_and_compact_summary():
    conv = Conversation(title="packets")
    conv.initialize_with_system_message("system")
    root_id = conv.current_node_id
    parent = _node("parent", "parent answer")
    conv.add_node(parent, root_id)
    child = _node("child before compact", "old result")
    conv.add_node(child, parent["id"])
    compact = NodeManager.create_compact_node(
        parent_id=child["id"],
        summary="Summary:\nfolded history",
        model_id="fake-model",
        last_pre_compact_message_id=child["id"],
    )
    conv.add_node(compact, child["id"])
    after = _node("after compact", "new result")
    conv.add_node(after, compact["id"])

    bundle = build_prune_packets(conv, parent["id"])
    packets = bundle["branch_packets"]

    assert len(packets) == 1
    segments = packets[0]["segments"]
    assert [segment["type"] for segment in segments] == ["compact_summary", "raw_turns"]
    assert compact["id"] in bundle["coverage"]["compact_node_ids"]
    assert child["id"] in packets[0]["coverage"]["included_node_ids"]
    assert child["id"] in packets[0]["coverage"]["folded_node_ids"]
    assert child["id"] not in segments[0].get("node_ids", [])
    assert after["id"] in packets[0]["coverage"]["included_node_ids"]


def test_prune_summary_saves_parent_attachment_and_uses_clean_no_tool_call():
    tmp, manager = _make_manager()
    try:
        conv, parent, child_a, _ = _make_tree(manager)

        result = asyncio.run(manager.prune_summary(conv.metadata["id"], parent["id"], custom_instructions="focus facts"))
        reloaded = manager.get_conversation(conv.metadata["id"])
        stored_parent = reloaded.nodes[parent["id"]]
        summaries = stored_parent["context_summaries"]
        provider_call = manager.model_manager.provider.calls[-1]

        assert result["parent_node_id"] == parent["id"]
        assert summaries[0]["type"] == "prune_summary"
        assert child_a["id"] in summaries[0]["covered_node_ids"]
        assert "Durable fact from child branches" in summaries[0]["summary"]
        assert provider_call["tools"] is None
        assert provider_call["tool_choice"] is None
        assert provider_call["messages"][0]["role"] == "system"
        assert "Do NOT call any tools" in provider_call["messages"][0]["content"]
        assert "focus facts" in provider_call["messages"][1]["content"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prune_summary_keeps_previous_parent_summaries():
    tmp, manager = _make_manager()
    try:
        conv, parent, _, _ = _make_tree(manager)

        first = asyncio.run(manager.prune_summary(conv.metadata["id"], parent["id"], custom_instructions="first"))
        second = asyncio.run(manager.prune_summary(conv.metadata["id"], parent["id"], custom_instructions="second"))
        reloaded = manager.get_conversation(conv.metadata["id"])
        summaries = reloaded.nodes[parent["id"]]["context_summaries"]

        assert [summary["id"] for summary in summaries] == [first["summary_id"], second["summary_id"]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prune_summary_uses_branch_digests_when_packet_exceeds_budget(monkeypatch):
    tmp, manager = _make_manager()
    try:
        conv, parent, _, _ = _make_tree(manager)
        monkeypatch.setattr("backend.core.chat.chat_manager.PRUNE_PACKET_BUDGET_CHARS", 1)

        result = asyncio.run(manager.prune_summary(conv.metadata["id"], parent["id"]))
        reloaded = manager.get_conversation(conv.metadata["id"])
        summary = reloaded.nodes[parent["id"]]["context_summaries"][0]

        assert len(manager.model_manager.provider.calls) == 3
        assert len(summary["branch_digests"]) == 2
        assert "超过全局预算" in "\n".join(summary["coverage_notes"])
        assert result["summary_id"] == summary["id"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prune_summary_sync_provider_does_not_block_event_loop():
    tmp = tempfile.mkdtemp(prefix="chattree_prune_nonblocking_")
    try:
        storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
        prompts = PromptStorage(storage_dir=os.path.join(tmp, "prompts"))
        manager = ChatManager(SlowPruneModelManager(), storage, prompts)
        conv, parent, _, _ = _make_tree(manager)

        async def probe():
            start = time.perf_counter()
            task = asyncio.create_task(manager.prune_summary(conv.metadata["id"], parent["id"]))
            await asyncio.sleep(0.05)
            elapsed = time.perf_counter() - start
            result = await task
            return elapsed, result

        elapsed, result = asyncio.run(probe())

        assert elapsed < 0.2
        assert result["summary_id"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prune_summary_injects_for_new_branch_but_not_covered_branch():
    tmp, manager = _make_manager()
    try:
        conv, parent, child_a, _ = _make_tree(manager)
        asyncio.run(manager.prune_summary(conv.metadata["id"], parent["id"]))

        reloaded = manager.get_conversation(conv.metadata["id"])
        new_child = _node("new branch after summary", "new answer")
        reloaded.add_node(new_child, parent["id"])
        reloaded.switch_to_node(new_child["id"])
        messages = manager._prepare_messages_for_api_with_conversation(reloaded)
        contents = [str(message.get("content") or "") for message in messages]
        assert any("Durable fact from child branches" in content for content in contents)

        reloaded.switch_to_node(child_a["id"])
        covered_messages = manager._prepare_messages_for_api_with_conversation(reloaded)
        covered_contents = [str(message.get("content") or "") for message in covered_messages]
        assert not any("Durable fact from child branches" in content for content in covered_contents)

        continued_old_branch = _node("continue covered branch", "covered continuation")
        reloaded.add_node(continued_old_branch, child_a["id"])
        reloaded.switch_to_node(continued_old_branch["id"])
        continued_messages = manager._prepare_messages_for_api_with_conversation(reloaded)
        continued_contents = [str(message.get("content") or "") for message in continued_messages]
        assert not any("Durable fact from child branches" in content for content in continued_contents)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prune_summary_slash_command_is_registered_as_direct_response():
    result = SlashCommandDispatcher().dispatch("/prune-summary node:abc focus storage")
    assert result.kind == SlashDispatchKind.DIRECT_RESPONSE
    assert result.canonical_name == "prune-summary"
    assert result.args == "node:abc focus storage"
    assert result.disable_tools is True


def test_prune_summary_slash_runtime_calls_special_handler():
    class FakeChatManager:
        def __init__(self):
            self.calls = []

        async def prune_summary(self, conversation_id, parent_node_id, **kwargs):
            self.calls.append((conversation_id, parent_node_id, kwargs))
            return {
                "conversation_id": conversation_id,
                "parent_node_id": parent_node_id,
                "summary_id": "summary-1",
                "covered_node_count": 3,
                "covered_direct_child_count": 2,
                "compact_node_ids": [],
                "truncated_node_ids": [],
                "coverage_notes": [],
                "summary_preview": "preview text",
            }

    async def collect():
        manager = FakeChatManager()
        events = []
        async for event in _prune_summary_stream(
            "conv-1",
            SendMessageRequest(content="/prune-summary node:target focus storage", parent_node_id="parent-1"),
            manager,
        ):
            events.append(event)
        return manager, events

    manager, events = asyncio.run(collect())

    assert manager.calls == [("conv-1", "target", {"custom_instructions": "focus storage", "model_id": None, "provider_id": None})]
    assert any("剪枝摘要已生成" in event for event in events)
    assert any("preview text" in event for event in events)


def test_prune_summary_slash_stream_yields_start_before_summary_finishes():
    class SlowFakeChatManager:
        async def prune_summary(self, conversation_id, parent_node_id, **kwargs):
            await asyncio.sleep(0.35)
            return {
                "conversation_id": conversation_id,
                "parent_node_id": parent_node_id,
                "summary_id": "summary-1",
                "covered_node_count": 3,
                "covered_direct_child_count": 2,
                "compact_node_ids": [],
                "truncated_node_ids": [],
                "coverage_notes": [],
                "summary_preview": "preview text",
            }

    async def collect():
        generator = _prune_summary_stream(
            "conv-1",
            SendMessageRequest(content="/prune-summary node:target", parent_node_id="parent-1"),
            SlowFakeChatManager(),
        )
        start = time.perf_counter()
        first_event = await generator.__anext__()
        elapsed = time.perf_counter() - start
        remaining_events = [event async for event in generator]
        return elapsed, [first_event] + remaining_events

    elapsed, events = asyncio.run(collect())

    assert elapsed < 0.2
    assert '"status": "start"' in events[0]
    assert any("剪枝摘要已生成" in event for event in events)
