import asyncio
from pathlib import Path

import sys

sys.path.insert(0, ".")
sys.path.insert(0, "test")

from backend.core.chat.chat_manager import ChatManager
from backend.core.config.types import StreamChunk, StreamController, StreamStatus
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.transcript import TranscriptProjection
from backend.core.plans import PlanLedger
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.storage.tool_result_storage import ToolResultStorage
from backend.core.tools.orchestrator import ToolOrchestrator
from backend.core.tools.security.approval import ApprovalManager
from backend.core.tools.security.logical_sandbox import LogicalSandbox
from backend.core.tools.security.permissions import PermissionEngine
from test_chat_manager_prompt_slash import (
    CapturingModelManager,
    PlanFinalWithoutExitProvider,
    PlanModeToolManager,
    collect_chunks,
)


def _make_manager(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path / "sqlite")
    persistence.initialize()
    repository = ChatRepository(persistence)
    projection = TranscriptProjection(persistence)
    model_manager = CapturingModelManager()
    try:
        manager = ChatManager(
            model_manager,
            ChatStorage(str(tmp_path / "conversations")),
            PromptStorage(str(tmp_path / "prompts")),
            chat_repository=repository,
            transcript_projection=projection,
        )
    except TypeError:
        manager = ChatManager(
            model_manager,
            ChatStorage(str(tmp_path / "conversations")),
            PromptStorage(str(tmp_path / "prompts")),
        )
        manager.chat_repository = repository
        manager.transcript_projection = projection
    return manager, repository, projection


def test_send_message_stream_writes_transcript_projection(tmp_path: Path):
    manager, _repository, projection = _make_manager(tmp_path)
    conversation = manager.create_conversation("sqlite transcript")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "hello sqlite",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    try:
        items = projection.list_for_branch(
            conversation.metadata["id"],
            reloaded.current_node_id,
        )
    except KeyError:
        items = []

    assert [item["item_type"] for item in items] == [
        "user_message",
        "assistant_answer",
    ]
    assert [item["preview"] for item in items] == ["hello sqlite", "ok"]


def test_send_message_stream_updates_run_draft_item(tmp_path: Path):
    manager, _repository, projection = _make_manager(tmp_path)
    conversation = manager.create_conversation("sqlite run draft")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "hello run",
                model_id="fake-model",
                run_id="run-chat-1",
            )
        )
    )

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    items = projection.list_for_branch(
        conversation.metadata["id"],
        reloaded.current_node_id,
    )

    run_items = [item for item in items if item["item_type"] == "run_draft"]
    assert len(run_items) == 1
    assert run_items[0]["run_id"] == "run-chat-1"
    assert run_items[0]["status"] == "completed"
    assert run_items[0]["preview"] == "ok"


def test_plan_control_turn_writes_process_and_plan_card_without_answer(tmp_path: Path):
    manager, _repository, projection = _make_manager(tmp_path)
    plan_ledger = PlanLedger()
    tool_manager = PlanModeToolManager(plan_ledger)
    manager.plan_ledger = plan_ledger
    manager.tool_manager = tool_manager
    manager.tool_orchestrator = ToolOrchestrator(
        tool_manager=tool_manager,
        permission_engine=PermissionEngine.default(),
        approval_manager=ApprovalManager(),
        logical_sandbox=LogicalSandbox.for_config({}, tmp_path),
    )
    manager.model_manager.provider = PlanFinalWithoutExitProvider()
    conversation = manager.create_conversation("sqlite plan")
    asyncio.run(
        plan_ledger.enter_plan_mode(
            conversation_id=conversation.metadata["id"],
            previous_permission_mode="modify_only",
        )
    )

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "写计划",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    items = projection.list_for_branch(
        conversation.metadata["id"],
        reloaded.current_node_id,
    )
    item_types = [item["item_type"] for item in items]
    assert "user_message" in item_types
    assert "assistant_process" in item_types
    assert "plan_card" in item_types
    assert "assistant_answer" not in item_types
    plan_card = next(item for item in items if item["item_type"] == "plan_card")
    assert plan_card["status"] == "awaiting_approval"
    assert "修改设置页" in plan_card["preview"]


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
                tool_calls=[
                    {
                        "id": "call_large_tool",
                        "type": "function",
                        "function": {
                            "name": "large_tool",
                            "arguments": "{\"value\":\"large\"}",
                        },
                    }
                ],
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
            usage_info={
                "input_tokens": 1,
                "output_tokens": 0,
                "total_tokens": 1,
                "source": "test",
                "raw": {},
            },
        )


class LargeToolManager:
    def __init__(self, tmp_path: Path):
        self.tool_result_store = ToolResultStorage(str(tmp_path / "tool-results"))

    def get_openai_tools(self, include_disabled=False):
        return [
            {
                "type": "function",
                "function": {
                    "name": "large_tool",
                    "description": "Return a large result",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        return "x" * 5000


def test_tool_results_are_copied_to_sqlite_blobs(tmp_path: Path):
    manager, repository, projection = _make_manager(tmp_path)
    tool_manager = LargeToolManager(tmp_path)
    manager.tool_manager = tool_manager
    tool_manager.tool_result_store.sqlite_repository = repository
    manager.model_manager.provider = ToolCallingProvider()
    conversation = manager.create_conversation("sqlite tools")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "run tool",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == "complete"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    items = projection.list_for_branch(
        conversation.metadata["id"],
        reloaded.current_node_id,
    )
    assert [item["item_type"] for item in items] == [
        "user_message",
        "assistant_process",
        "assistant_answer",
    ]
    with repository.persistence.connect() as conn:
        row = conn.execute(
            """
            SELECT output_preview, output_blob_id, output_size
            FROM tool_results
            WHERE conversation_id = ?
            """,
            (conversation.metadata["id"],),
        ).fetchone()
    assert row["output_preview"] == "x" * 4096
    assert row["output_blob_id"]
    assert row["output_size"] == 5000
