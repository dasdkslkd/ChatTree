import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "test")

from backend.core.chat.chat_manager import ChatManager
from backend.core.config.types import Message, Role, StreamChunk, StreamController, StreamStatus
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.tools.security.capabilities import ToolCapability
from backend.core.transcript import TranscriptAssembler
from test_chat_manager_prompt_slash import CapturingModelManager, collect_chunks


def _make_manager(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path / "sqlite")
    persistence.initialize()
    repository = ChatRepository(persistence)
    manager = ChatManager(
        CapturingModelManager(),
        ChatStorage(str(tmp_path / "conversations")),
        PromptStorage(str(tmp_path / "prompts")),
        chat_repository=repository,
    )
    return manager, repository, persistence


def _items(persistence: SQLitePersistence, conversation_id: str, node_id: str):
    return TranscriptAssembler(persistence).snapshot(conversation_id, node_id)["items"]


async def _drain_stream(stream, chunks: list[dict]):
    async for chunk in stream:
        chunks.append(dict(chunk))


class FailingUserMessageRepository(ChatRepository):
    def add_message(self, conversation_id, node_id, role, content, subtype=None, **kwargs):
        if str(role) == "user":
            raise RuntimeError("user canonical write failed")
        return super().add_message(
            conversation_id,
            node_id,
            role,
            content,
            subtype=subtype,
            **kwargs,
        )


class FailingAssistantMessageRepository(ChatRepository):
    def add_message(self, conversation_id, node_id, role, content, subtype=None, **kwargs):
        if subtype == "assistant_answer":
            raise RuntimeError("assistant canonical write failed")
        return super().add_message(
            conversation_id,
            node_id,
            role,
            content,
            subtype=subtype,
            **kwargs,
        )


class StaticProvider:
    def __init__(self, *, content: str, reasoning: str = ""):
        self.content = content
        self.reasoning = reasoning

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        if self.reasoning:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content=None,
                reasoning=self.reasoning,
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
            )
        yield StreamChunk(
            status=StreamStatus.CONTENT,
            content=self.content,
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
        )


class CompactingProvider:
    def generate_response(self, model, messages, **kwargs):
        return "Summary:\ncanonical compact facts", 7


def test_create_empty_conversation_writes_sqlite_conversation(tmp_path: Path):
    manager, repository, _persistence = _make_manager(tmp_path)

    conversation = manager.create_conversation("empty sqlite")

    stored = repository.get_conversation(conversation.metadata["id"])
    assert stored["title"] == "empty sqlite"


def test_send_message_stream_writes_canonical_rows_for_transcript_assembler(tmp_path: Path):
    manager, _repository, persistence = _make_manager(tmp_path)
    conversation = manager.create_conversation("sqlite transcript")

    chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "hello sqlite",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    )))

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    assert [(item["type"], item.get("content")) for item in _items(
        persistence,
        conversation.metadata["id"],
        reloaded.current_node_id,
    )] == [
        ("user_message", "hello sqlite"),
        ("assistant_answer", "ok"),
    ]


def test_user_canonical_write_failure_aborts_stream(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path / "sqlite")
    persistence.initialize()
    repository = FailingUserMessageRepository(persistence)
    manager = ChatManager(
        CapturingModelManager(),
        ChatStorage(str(tmp_path / "conversations")),
        PromptStorage(str(tmp_path / "prompts")),
        chat_repository=repository,
    )
    conversation = manager.create_conversation("sqlite user failure")
    chunks: list[dict] = []

    with pytest.raises(RuntimeError, match="user canonical write failed"):
        asyncio.run(_drain_stream(manager.send_message_stream(
            conversation.metadata["id"],
            "will fail",
            model_id="fake-model",
            parent_node_id=conversation.current_node_id,
        ), chunks))

    assert chunks == []


def test_assistant_canonical_write_failure_is_not_swallowed(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path / "sqlite")
    persistence.initialize()
    repository = FailingAssistantMessageRepository(persistence)
    manager = ChatManager(
        CapturingModelManager(),
        ChatStorage(str(tmp_path / "conversations")),
        PromptStorage(str(tmp_path / "prompts")),
        chat_repository=repository,
    )
    conversation = manager.create_conversation("sqlite assistant failure")
    chunks: list[dict] = []

    with pytest.raises(RuntimeError, match="assistant canonical write failed"):
        asyncio.run(_drain_stream(manager.send_message_stream(
            conversation.metadata["id"],
            "will fail late",
            model_id="fake-model",
            parent_node_id=conversation.current_node_id,
        ), chunks))

    assert not any(chunk.get("status") == StreamStatus.COMPLETE for chunk in chunks)
    assert manager._active_controllers == {}


def test_append_to_existing_node_updates_existing_sqlite_assistant_rows(tmp_path: Path):
    manager, repository, _persistence = _make_manager(tmp_path)
    manager.model_manager.provider = StaticProvider(content="first", reasoning="think-1")
    conversation = manager.create_conversation("sqlite continuation")

    first_chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "start",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    )))
    node_id = first_chunks[0]["node_id"]
    manager.model_manager.provider = StaticProvider(content="+second", reasoning="+think-2")

    continuation_chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "",
        model_id="fake-model",
        parent_node_id=node_id,
        suppress_user_message=True,
        append_to_existing_node=True,
    )))

    assert continuation_chunks[-1]["status"] == "complete"
    with repository.persistence.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, subtype, content_inline
            FROM messages
            WHERE conversation_id = ? AND node_id = ? AND role = 'assistant'
            ORDER BY subtype, id
            """,
            (conversation.metadata["id"], node_id),
        ).fetchall()

    by_subtype = {}
    for row in rows:
        by_subtype.setdefault(row["subtype"], []).append(row)
    assert [row["content_inline"] for row in by_subtype["assistant_answer"]] == ["first+second"]
    assert [row["content_inline"] for row in by_subtype["assistant_process_reasoning"]] == ["think-1+think-2"]


def test_continuation_merge_keeps_process_without_json_tool_history(tmp_path: Path):
    manager, _repository, _persistence = _make_manager(tmp_path)
    existing = Message({
        "id": "assistant",
        "role": Role.ASSISTANT,
        "content": "old",
        "process_parts": [
            {"type": "reasoning", "content": "old reasoning", "order": 0},
            {"type": "content", "content": "old content", "order": 1},
        ],
        "timestamp": 1,
    })
    continuation = Message({
        "id": "assistant-new",
        "role": Role.ASSISTANT,
        "content": "new",
        "process_parts": [
            {"type": "reasoning", "content": "new reasoning", "order": 0},
            {"type": "content", "content": "new content", "order": 1},
        ],
        "timestamp": 2,
    })

    merged = manager._merge_existing_node_assistant_continuation(existing, continuation)

    assert [part["order"] for part in merged["process_parts"]] == [0, 1, 2, 3]
    assert [part["order"] for part in continuation["process_parts"]] == [2, 3]
    assert "tool_calls" not in merged
    assert "tool_results" not in merged


def test_continuation_sqlite_persists_only_current_run_tool_facts(tmp_path: Path):
    manager, repository, persistence = _make_manager(tmp_path)
    conversation = manager.create_conversation("sqlite continuation tool ownership")
    node = conversation.nodes[conversation.current_node_id]
    run_repository = SQLiteRunRepository(persistence)
    old_run = run_repository.create_run(
        conversation.metadata["id"],
        kind="chat",
        target_node_id=node["id"],
        summary="old",
    )
    new_run = run_repository.create_run(
        conversation.metadata["id"],
        kind="chat",
        target_node_id=node["id"],
        summary="new",
    )
    old = Message({
        "id": "assistant",
        "role": Role.ASSISTANT,
        "content": "old",
        "process_parts": [
            {"type": "reasoning", "content": "old reasoning", "order": 0},
            {"type": "content", "content": "old content", "order": 1},
        ],
        "tool_calls": [
            {"id": "call-old", "function": {"name": "old_tool", "arguments": "{}"}, "call_index": 2},
        ],
        "timestamp": 1,
    })
    repository.persist_assistant_turn(
        conversation=conversation,
        node=node,
        assistant_msg=old,
        provider_id="fake",
        model_id="fake-model",
        run_id=old_run,
        tool_messages=[],
        tool_calls=list(old["tool_calls"] or []),
    )
    continuation = Message({
        "id": "assistant-new",
        "role": Role.ASSISTANT,
        "content": "new",
        "process_parts": [
            {"type": "reasoning", "content": "new reasoning", "order": 0},
            {"type": "content", "content": "new content", "order": 1},
        ],
        "tool_calls": [
            {"id": "call-new", "function": {"name": "new_tool", "arguments": "{}"}, "call_index": 2},
        ],
        "timestamp": 2,
    })
    merged = manager._merge_existing_node_assistant_continuation(old, continuation)
    transcript_msg = Message(deepcopy(merged))
    transcript_msg["process_parts"] = continuation.get("process_parts")

    repository.persist_assistant_turn(
        conversation=conversation,
        node=node,
        assistant_msg=transcript_msg,
        provider_id="fake",
        model_id="fake-model",
        run_id=new_run,
        tool_messages=[],
        tool_calls=list(continuation.get("tool_calls") or []),
    )

    with repository.persistence.connect() as conn:
        calls = conn.execute(
            """
            SELECT id, run_id, call_index
            FROM tool_calls
            WHERE conversation_id = ?
            ORDER BY id
            """,
            (conversation.metadata["id"],),
        ).fetchall()
        process_rows = conn.execute(
            """
            SELECT id, metadata_json
            FROM messages
            WHERE conversation_id = ?
              AND subtype IN ('assistant_process_reasoning', 'assistant_process_content')
            ORDER BY id
            """,
            (conversation.metadata["id"],),
        ).fetchall()

    assert [(row["id"], row["run_id"], row["call_index"]) for row in calls] == [
        ("call-new", new_run, 2),
        ("call-old", old_run, 2),
    ]
    metadata_by_id = {
        row["id"]: json.loads(row["metadata_json"])
        for row in process_rows
    }
    assert metadata_by_id["assistant:reasoning:0"]["run_id"] == old_run
    assert metadata_by_id["assistant:content:1"]["run_id"] == old_run
    assert metadata_by_id["assistant:reasoning:2"]["run_id"] == new_run
    assert metadata_by_id["assistant:content:3"]["run_id"] == new_run


def test_compact_conversation_writes_canonical_compact_messages(tmp_path: Path):
    manager, _repository, persistence = _make_manager(tmp_path)
    manager.model_manager.provider = CompactingProvider()
    conversation = manager.create_conversation("sqlite compact")
    conversation.metadata["provider_id"] = "fake"
    conversation.metadata["model_id"] = "fake-model"
    parent_id = conversation.current_node_id
    manager.chat_repository.save(conversation)

    result = asyncio.run(manager.compact_conversation(conversation.metadata["id"]))
    items = _items(persistence, conversation.metadata["id"], result["node_id"])

    assert [item["type"] for item in items] == ["compact"]
    assert items[0]["content"] == "Summary:\ncanonical compact facts"
    assert items[0]["trigger"] == "manual"
    assert items[0]["messages_to_keep"] == 1
    with persistence.connect() as conn:
        rows = conn.execute(
            """
            SELECT role, subtype, content_inline
            FROM messages
            WHERE conversation_id = ? AND node_id = ?
            ORDER BY subtype
            """,
            (conversation.metadata["id"], result["node_id"]),
        ).fetchall()
    assert [(row["role"], row["subtype"], row["content_inline"]) for row in rows] == [
        ("system", "compact_boundary", "Conversation compacted"),
        ("assistant", "compact_summary", "Summary:\ncanonical compact facts"),
    ]
    assert manager.get_conversation(conversation.metadata["id"]).nodes[result["node_id"]]["parent_id"] == parent_id


def test_delete_conversation_removes_canonical_transcript_source_rows(tmp_path: Path):
    manager, _repository, persistence = _make_manager(tmp_path)
    conversation = manager.create_conversation("delete sqlite")
    chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "to delete",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    )))
    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    assert _items(persistence, conversation.metadata["id"], reloaded.current_node_id)

    manager.delete_conversation(conversation.metadata["id"])

    try:
        TranscriptAssembler(persistence).snapshot(conversation.metadata["id"], reloaded.current_node_id)
    except KeyError:
        pass
    else:
        raise AssertionError("deleted conversation canonical transcript rows should not remain")


class ToolCallingProvider:
    def __init__(self):
        self.calls = []

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        if len(self.calls) == 1:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
                tool_calls=[{
                    "id": "call_large_tool",
                    "type": "function",
                    "function": {
                        "name": "large_tool",
                        "arguments": "{\"value\":\"large\"}",
                    },
                }],
            )
        else:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="done",
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
        )


class PreambleToolCallingProvider:
    def __init__(self):
        self.calls = 0

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="I will inspect first. ",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
            )
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content=None,
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=0,
                tool_calls=[{
                    "id": "call_large_tool",
                    "type": "function",
                    "function": {
                        "name": "large_tool",
                        "arguments": "{}",
                    },
                }],
            )
        else:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="done",
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
        )


class LargeToolManager:
    def __init__(self, tmp_path: Path):
        pass

    def get_openai_tools(self, include_disabled=False):
        return [{
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": "Return a large result",
                "parameters": {"type": "object", "properties": {}},
            },
        }]

    def capabilities_for(self, name, workspace=None):
        return {ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE}

    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        return "x" * 5000


class FailingToolManager(LargeToolManager):
    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        raise RuntimeError("tool exploded")


class SlowToolManager(LargeToolManager):
    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        await asyncio.sleep(5)
        return "late"


def test_tool_results_are_written_once_with_run_id_and_transcript_process(tmp_path: Path):
    manager, repository, persistence = _make_manager(tmp_path)
    tool_manager = LargeToolManager(tmp_path)
    manager.tool_manager = tool_manager
    manager.model_manager.provider = ToolCallingProvider()
    conversation = manager.create_conversation("sqlite tools")
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation.metadata["id"],
        kind="chat",
        target_node_id=conversation.current_node_id,
        summary="large tool",
    )

    chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "run tool",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
        run_id=run_id,
    )))

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    items = _items(persistence, conversation.metadata["id"], reloaded.current_node_id)
    assert [item["type"] for item in items] == [
        "user_message",
        "assistant_process",
        "assistant_answer",
    ]
    process = next(item for item in items if item["type"] == "assistant_process")
    assert process["blocks"][0]["tool_name"] == "large_tool"
    assert process["blocks"][0]["result_preview"] == "x" * 4096
    with repository.persistence.connect() as conn:
        row = conn.execute(
            """
            SELECT output_preview, output_blob_id, output_size, metadata_json, run_id
            FROM tool_results
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchone()
    assert row["output_preview"] == "x" * 4096
    assert row["output_blob_id"]
    assert row["output_size"] == 5000
    assert row["run_id"] == run_id
    metadata = json.loads(row["metadata_json"])
    assert metadata["tool_name"] == "large_tool"
    assert metadata["tool_result_id"]
    assert "model_visible_content" not in metadata


def test_committed_tool_call_persists_when_tool_execution_fails(tmp_path: Path):
    manager, repository, _persistence = _make_manager(tmp_path)
    manager.tool_manager = FailingToolManager(tmp_path)
    manager.model_manager.provider = ToolCallingProvider()
    conversation = manager.create_conversation("sqlite failed tool")

    chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "run failing tool",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    )))

    assert any(chunk.get("status") == StreamStatus.ERROR for chunk in chunks)
    with repository.persistence.connect() as conn:
        call = conn.execute(
            """
            SELECT id, name, status
            FROM tool_calls
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchone()
        result_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM tool_results
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchone()["count"]
    assert dict(call) == {
        "id": "call_large_tool",
        "name": "large_tool",
        "status": "error",
    }
    assert result_count == 0


def test_committed_tool_call_persists_when_stream_stops_during_tool(tmp_path: Path):
    manager, repository, _persistence = _make_manager(tmp_path)
    manager.tool_manager = SlowToolManager(tmp_path)
    manager.model_manager.provider = ToolCallingProvider()
    conversation = manager.create_conversation("sqlite stopped tool")
    chunks: list[dict] = []

    async def scenario():
        async for chunk in manager.send_message_stream(
            conversation.metadata["id"],
            "run slow tool",
            model_id="fake-model",
            parent_node_id=conversation.current_node_id,
        ):
            chunks.append(dict(chunk))
            if chunk.get("event_type") == "tool_calls_committed":
                await manager.stop_stream(chunk["node_id"])

    asyncio.run(scenario())

    assert any(chunk.get("status") == StreamStatus.STOPPED for chunk in chunks)
    with repository.persistence.connect() as conn:
        call = conn.execute(
            """
            SELECT id, name, status
            FROM tool_calls
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchone()
        result_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM tool_results
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchone()["count"]
    assert dict(call) == {
        "id": "call_large_tool",
        "name": "large_tool",
        "status": "stopped",
    }
    assert result_count == 0


def test_committed_tool_call_persists_when_client_closes_stream(tmp_path: Path):
    manager, repository, _persistence = _make_manager(tmp_path)
    manager.tool_manager = SlowToolManager(tmp_path)
    manager.model_manager.provider = ToolCallingProvider()
    conversation = manager.create_conversation("sqlite closed tool")

    async def scenario():
        stream = manager.send_message_stream(
            conversation.metadata["id"],
            "run slow tool then close",
            model_id="fake-model",
            parent_node_id=conversation.current_node_id,
        )
        try:
            while True:
                chunk = await anext(stream)
                if chunk.get("event_type") == "tool_calls_committed":
                    await stream.aclose()
                    return
        finally:
            await stream.aclose()

    asyncio.run(scenario())

    with repository.persistence.connect() as conn:
        call = conn.execute(
            """
            SELECT id, name, status
            FROM tool_calls
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchone()
        result_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM tool_results
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchone()["count"]
    assert dict(call) == {
        "id": "call_large_tool",
        "name": "large_tool",
        "status": "error",
    }
    assert result_count == 0


def test_tool_preamble_content_persists_as_process_content(tmp_path: Path):
    manager, repository, _persistence = _make_manager(tmp_path)
    tool_manager = LargeToolManager(tmp_path)
    manager.tool_manager = tool_manager
    manager.model_manager.provider = PreambleToolCallingProvider()
    conversation = manager.create_conversation("sqlite tool preamble")

    chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "run tool with preamble",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    )))

    assert any(chunk.get("content") == "I will inspect first. " for chunk in chunks)
    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    assert "assistant_message" not in reloaded.nodes[reloaded.current_node_id]

    with repository.persistence.connect() as conn:
        rows = conn.execute(
            """
            SELECT subtype, content_inline
            FROM messages
            WHERE conversation_id = ? AND node_id = ?
            ORDER BY created_at, id
            """,
            (conversation.metadata["id"], reloaded.current_node_id),
        ).fetchall()
    by_subtype = {row["subtype"]: row["content_inline"] for row in rows}
    assert by_subtype["assistant_process_content"] == "I will inspect first. "
    assert by_subtype["assistant_answer"] == "done"


class TwoToolCallingProvider:
    def __init__(self):
        self.calls = []

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        if len(self.calls) > 1:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="done",
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
            )
            return
        yield StreamChunk(
            status=StreamStatus.CONTENT,
            content="",
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=1,
            tool_calls=[
                {"id": "call_first", "type": "function", "function": {"name": "first_tool", "arguments": "{\"a\":1}"}},
                {"id": "call_second", "type": "function", "function": {"name": "second_tool", "arguments": "{\"b\":2}"}},
            ],
        )
        yield StreamChunk(
            status=StreamStatus.COMPLETE,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=1,
        )


class TwoToolManager:
    def __init__(self, tmp_path: Path):
        self.runtime_contexts = []

    def get_openai_tools(self, include_disabled=False):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Run {name}",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in ("first_tool", "second_tool")
        ]

    def capabilities_for(self, name, workspace=None):
        return {ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE}

    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        self.runtime_contexts.append(dict(runtime_context or {}))
        return json.dumps({"tool": name, "arguments": arguments}, sort_keys=True)


class InterleavedToolCallingProvider:
    def __init__(self):
        self.calls = 0

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content=None,
                reasoning="reasoning one",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
            )
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="preamble one",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
            )
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content=None,
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
                tool_calls=[{
                    "id": "call_first",
                    "type": "function",
                    "function": {"name": "first_tool", "arguments": "{}"},
                }],
            )
        elif self.calls == 2:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content=None,
                reasoning="reasoning two",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
            )
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="preamble two",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
            )
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content=None,
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
                tool_calls=[{
                    "id": "call_second",
                    "type": "function",
                    "function": {"name": "second_tool", "arguments": "{}"},
                }],
            )
        else:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="done",
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
        )


def test_tool_result_persistence_preserves_tool_call_arguments_and_index(tmp_path: Path):
    manager, repository, _persistence = _make_manager(tmp_path)
    tool_manager = TwoToolManager(tmp_path)
    manager.tool_manager = tool_manager
    manager.model_manager.provider = TwoToolCallingProvider()
    conversation = manager.create_conversation("sqlite two tools")

    chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "run two tools",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    )))

    assert chunks[-1]["status"] == "complete"
    with repository.persistence.connect() as conn:
        call_rows = conn.execute(
            """
            SELECT id, call_index, args_inline
            FROM tool_calls
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchall()
        result_rows = conn.execute(
            """
            SELECT tool_call_id
            FROM tool_results
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchall()

    calls = {row["id"]: row for row in call_rows}
    assert set(calls) == {"call_first", "call_second"}
    assert calls["call_first"]["call_index"] == 0
    assert json.loads(calls["call_first"]["args_inline"]) == {"a": 1}
    assert calls["call_second"]["call_index"] == 1
    assert json.loads(calls["call_second"]["args_inline"]) == {"b": 2}
    assert sorted(row["tool_call_id"] for row in result_rows) == [
        "call_first",
        "call_second",
    ]


def test_tool_history_is_sqlite_canonical_not_node_json(tmp_path: Path):
    manager, repository, _persistence = _make_manager(tmp_path)
    manager.tool_manager = LargeToolManager(tmp_path)
    provider = ToolCallingProvider()
    manager.model_manager.provider = provider
    conversation = manager.create_conversation("canonical tool context")

    first_chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "run tool",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    )))

    assert first_chunks[-1]["status"] == "complete"
    first_node = manager.get_conversation(conversation.metadata["id"]).nodes[first_chunks[-1]["node_id"]]
    assert "assistant_message" not in first_node
    assert "tool_messages" not in first_node

    second_chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "use prior result",
        model_id="fake-model",
        parent_node_id=first_node["id"],
    )))

    assert second_chunks[-1]["status"] == "complete"
    second_prompt = provider.calls[-1]["messages"]
    tool_call_messages = [
        message
        for message in second_prompt
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    tool_result_messages = [
        message
        for message in second_prompt
        if message.get("role") == "tool" and message.get("tool_call_id") == "call_large_tool"
    ]
    final_answers = [
        message
        for message in second_prompt
        if message.get("role") == "assistant" and message.get("content") == "done"
    ]
    assert tool_call_messages[0]["tool_calls"][0]["id"] == "call_large_tool"
    assert tool_call_messages[0]["tool_calls"][0]["function"]["arguments"] == "{\"value\":\"large\"}"
    assert len(tool_result_messages) == 1
    assert "tool_result_id" in tool_result_messages[0]["content"]
    assert final_answers and all("tool_calls" not in message for message in final_answers)
    with repository.persistence.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE conversation_id = ?",
            (conversation.metadata["id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM tool_results WHERE conversation_id = ? AND tool_call_id = ?",
            (conversation.metadata["id"], "call_large_tool"),
        ).fetchone()[0] == 1


def test_tool_round_process_segments_keep_stream_order_in_sqlite_transcript(tmp_path: Path):
    manager, repository, persistence = _make_manager(tmp_path)
    tool_manager = TwoToolManager(tmp_path)
    manager.tool_manager = tool_manager
    manager.model_manager.provider = InterleavedToolCallingProvider()
    conversation = manager.create_conversation("sqlite interleaved process")

    chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "run interleaved tools",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    )))

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    items = _items(persistence, conversation.metadata["id"], reloaded.current_node_id)
    process = next(item for item in items if item["type"] == "assistant_process")
    assert [(block["type"], block.get("content"), block.get("tool_call_id")) for block in process["blocks"]] == [
        ("reasoning", "reasoning one", None),
        ("content", "preamble one", None),
        ("tool_call", None, "call_first"),
        ("reasoning", "reasoning two", None),
        ("content", "preamble two", None),
        ("tool_call", None, "call_second"),
    ]
    with repository.persistence.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, call_index
            FROM tool_calls
            WHERE conversation_id = ?
            ORDER BY call_index
            """,
            (conversation.metadata["id"],),
        ).fetchall()
    assert [(row["id"], row["call_index"]) for row in rows] == [
        ("call_first", 2),
        ("call_second", 5),
    ]


def test_task_context_mode_is_stored_per_node_and_inherited_by_children(tmp_path: Path):
    manager, repository, _persistence = _make_manager(tmp_path)
    conversation = manager.create_conversation("task context branches")
    root_id = conversation.current_node_id

    detached_chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "explore separately",
        model_id="fake-model",
        parent_node_id=root_id,
        task_context_mode="detached",
    )))
    detached_id = detached_chunks[0]["node_id"]
    assert detached_chunks[0]["task_context_mode"] == "detached"

    inherited_chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "continue exploration",
        model_id="fake-model",
        parent_node_id=detached_id,
    )))
    inherited_id = inherited_chunks[0]["node_id"]
    assert inherited_chunks[0]["task_context_mode"] == "detached"

    attached_chunks = asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "coding branch",
        model_id="fake-model",
        parent_node_id=root_id,
    )))
    attached_id = attached_chunks[0]["node_id"]
    assert attached_chunks[0]["task_context_mode"] == "attached"

    with repository.persistence.connect() as conn:
        rows = conn.execute(
            "SELECT id, task_context_mode FROM nodes WHERE id IN (?, ?, ?)",
            (detached_id, inherited_id, attached_id),
        ).fetchall()
    modes = {row["id"]: row["task_context_mode"] for row in rows}
    assert modes == {
        detached_id: "detached",
        inherited_id: "detached",
        attached_id: "attached",
    }
