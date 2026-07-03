from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, ".")

from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import (
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySource,
)
from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.conversation import Conversation
from backend.core.config.types import StreamChunk, StreamController, StreamStatus
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.slash import SlashCommandDispatcher, SlashDispatchKind
from backend.core.tasks import TaskLedger


def make_manager(registry=None):
    manager = ChatManager.__new__(ChatManager)
    manager.capability_registry = registry
    manager.slash_dispatcher = SlashCommandDispatcher()
    return manager


def test_chat_manager_dispatches_builtin_slash_to_main_prompt():
    manager = make_manager()

    result = manager._dispatch_slash_content("/review focus on state bugs")

    assert result.kind == SlashDispatchKind.MAIN_PROMPT
    assert result.model_input is not None
    assert "focus on state bugs" in result.model_input


def test_chat_manager_treats_removed_side_command_as_plain_text():
    manager = make_manager()
    result = manager._dispatch_slash_content("/side quick question")

    assert result.kind == SlashDispatchKind.PASSTHROUGH
    assert result.model_input == "/side quick question"


def test_chat_manager_build_prompt_messages_uses_unified_builder(tmp_path: Path):
    skill_path = tmp_path / "review" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# Review\n\n检查代码。", encoding="utf-8")
    registry = CapabilityRegistry()
    registry.add_capabilities(
        [
            CapabilityDefinition(
                name="review",
                kind=CapabilityKind.SKILL,
                source=CapabilitySource.PROJECT,
                description="Review skill",
                path=skill_path,
            )
        ]
    )
    manager = make_manager(registry)
    conversation = Conversation(title="test")
    conversation.initialize_with_system_message("base system")

    messages = manager._build_prompt_messages(conversation, ["review"])

    assert messages[0]["content"] == "base system"
    contents = [str(message.get("content") or "") for message in messages]
    assert any("## Available Capabilities" in content for content in contents)
    assert any("<name>review</name>" in content for content in contents)
    assert any("检查代码" in content for content in contents)


class CapturingProvider:
    def __init__(self):
        self.messages = None
        self.kwargs = None
        self.calls = []

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.messages = messages
        self.kwargs = kwargs
        self.calls.append({"messages": messages, "kwargs": kwargs})
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
            usage_info={
                "input_tokens": 1,
                "output_tokens": 0,
                "total_tokens": 1,
                "source": "test",
                "raw": {},
            },
        )


class CapturingModelManager:
    def __init__(self):
        self.model_list = {"fake": ["fake-model"]}
        self.provider = CapturingProvider()
        self.get_model_calls = []

    def get_model(self, provider, is_async=False):
        self.get_model_calls.append((provider, is_async))
        return self.provider

    def get_model_metadata(self, provider_id, model_name):
        return {}


class FakeToolManager:
    def __init__(self, task_ledger=None):
        self.task_ledger = task_ledger

    def get_openai_tools(self, include_disabled=False):
        return [
            {
                "type": "function",
                "function": {
                    "name": "update_task",
                    "description": "Update a task",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        if name == "update_task" and self.task_ledger is not None:
            task = await self.task_ledger.update_task(
                conversation_id=runtime_context["conversation_id"],
                task_id=arguments["task_id"],
                status=arguments.get("status"),
                evidence_summary=arguments.get("evidence_summary"),
                evidence_run_id=runtime_context.get("run_id"),
            )
            return json.dumps({"task_id": task.task_id, "status": task.status.value}, ensure_ascii=False)
        return json.dumps({"ok": True}, ensure_ascii=False)


class ResolvingAfterGuardProvider(CapturingProvider):
    def __init__(self, task_id):
        super().__init__()
        self.task_id = task_id

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.messages = messages
        self.kwargs = kwargs
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        call_number = len(self.calls)
        if call_number < 3:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content=f"premature final {call_number}",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
            )
        elif call_number == 3:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
                tool_calls=[
                    {
                        "id": "call_update_task",
                        "type": "function",
                        "function": {
                            "name": "update_task",
                            "arguments": json.dumps({
                                "task_id": self.task_id,
                                "status": "completed",
                                "evidence_summary": "resolved after TaskLedger reminder",
                            }),
                        },
                    }
                ],
            )
        else:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="task resolved",
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


async def collect_chunks(stream):
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    return chunks


def make_stream_manager(tmp_path: Path):
    model_manager = CapturingModelManager()
    manager = ChatManager(
        model_manager,
        ChatStorage(str(tmp_path / "conversations")),
        PromptStorage(str(tmp_path / "prompts")),
    )
    return manager, model_manager


def test_send_message_stream_expands_review_slash_prompt(tmp_path: Path):
    manager, model_manager = make_stream_manager(tmp_path)
    conversation = manager.create_conversation("slash review")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "/review focus on auth",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == StreamStatus.COMPLETE
    assert model_manager.provider.messages is not None
    sent_user_messages = [
        message
        for message in model_manager.provider.messages
        if message.get("role") == "user"
    ]
    assert "Review target: focus on auth" in sent_user_messages[-1]["content"]
    reloaded = manager.get_conversation(conversation.metadata["id"])
    current = reloaded.nodes[reloaded.current_node_id]
    assert current["user_message"]["slash_command"]["command"] == "review"
    assert current["user_message"]["slash_command"]["original_input"] == "/review focus on auth"


def test_send_message_stream_task_guard_suppresses_unresolved_final_text(tmp_path: Path):
    manager, _model_manager = make_stream_manager(tmp_path)
    task_ledger = TaskLedger()
    manager.task_ledger = task_ledger
    conversation = manager.create_conversation("task guard")
    asyncio.run(task_ledger.create_task(
        conversation_id=conversation.metadata["id"],
        title="未完成任务",
    ))

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "直接回答",
                model_id="fake-model",
            )
        )
    )

    content_chunks = [chunk.get("content") for chunk in chunks if chunk.get("content")]
    assert "ok" not in content_chunks
    assert any("仍有未完成任务" in str(content) for content in content_chunks)
    reloaded = manager.get_conversation(conversation.metadata["id"])
    assistant = reloaded.nodes[reloaded.current_node_id]["assistant_message"]
    assert "仍有未完成任务" in assistant["content"]
    assert assistant["generation_info"]["task_guard"]["open_task_count"] == 1


def test_send_message_stream_task_guard_continues_after_tool_retry(tmp_path: Path):
    manager, model_manager = make_stream_manager(tmp_path)
    task_ledger = TaskLedger()
    manager.tool_manager = FakeToolManager(task_ledger)
    manager.task_ledger = task_ledger
    conversation = manager.create_conversation("task guard retry")
    task = asyncio.run(task_ledger.create_task(
        conversation_id=conversation.metadata["id"],
        title="仍未完成",
    ))
    model_manager.provider = ResolvingAfterGuardProvider(task.task_id)

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "直接回答",
                model_id="fake-model",
            )
        )
    )

    assert len(model_manager.provider.calls) == 4
    content_chunks = [chunk.get("content") for chunk in chunks if chunk.get("content")]
    assert "premature final 1" not in content_chunks
    assert "premature final 2" not in content_chunks
    assert not any("仍有未完成任务" in str(content) for content in content_chunks)
    assert "task resolved" in content_chunks
    assert any(
        message.get("role") == "system"
        and "<system-reminder>" in str(message.get("content") or "")
        and "TaskLedger" in str(message.get("content") or "")
        for call in model_manager.provider.calls
        for message in call["messages"]
    )
    reminder_text = "\n".join(
        str(message.get("content") or "")
        for call in model_manager.provider.calls
        for message in call["messages"]
        if message.get("role") == "system" and "<system-reminder>" in str(message.get("content") or "")
    )
    assert "Previous final response was discarded: TaskLedger still has unresolved work." in reminder_text
    assert "Use tools to complete open tasks, or mark them blocked with evidence before replying." in reminder_text
    assert "Use the available tools to inspect, run commands, delegate, or update TaskLedger." not in reminder_text
    reloaded = manager.get_conversation(conversation.metadata["id"])
    assistant = reloaded.nodes[reloaded.current_node_id]["assistant_message"]
    assert assistant["content"] == "task resolved"
    assert assistant["generation_info"]["task_guard"]["nudged"] is True


def test_send_message_stream_btw_runs_isolated_side_question_without_tools(tmp_path: Path):
    skill_path = tmp_path / "tools" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# Tools\n\nThis injected skill mentions run_command.", encoding="utf-8")
    registry = CapabilityRegistry()
    registry.add_capabilities(
        [
            CapabilityDefinition(
                name="tools",
                kind=CapabilityKind.SKILL,
                source=CapabilitySource.PROJECT,
                description="Tool capability",
                path=skill_path,
            )
        ]
    )
    manager, model_manager = make_stream_manager(tmp_path)
    manager.capability_registry = registry
    conversation = manager.create_conversation("slash btw")
    original_current_node_id = conversation.current_node_id
    original_node_ids = set(conversation.nodes)

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "/btw what changed here?",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == StreamStatus.COMPLETE
    assert all(chunk.get("target_node_id") is None for chunk in chunks)
    assert all(chunk.get("node_id") is None for chunk in chunks)
    assert model_manager.provider.messages is not None
    assert model_manager.provider.kwargs["tools"] is None
    assert model_manager.provider.kwargs["tool_choice"] is None
    sent_user_messages = [
        message
        for message in model_manager.provider.messages
        if message.get("role") == "user"
    ]
    full_prompt = "\n\n".join(str(message.get("content") or "") for message in model_manager.provider.messages)
    assert "## Available Capabilities" not in full_prompt
    assert "This injected skill mentions run_command" not in full_prompt
    assert "Claude Code-style side question" in sent_user_messages[-1]["content"]
    assert "Do not call tools" in sent_user_messages[-1]["content"]
    assert "what changed here?" in sent_user_messages[-1]["content"]
    reloaded = manager.get_conversation(conversation.metadata["id"])
    assert reloaded.current_node_id == original_current_node_id
    assert set(reloaded.nodes) == original_node_ids


def test_send_message_stream_removed_side_command_is_plain_message(tmp_path: Path):
    manager, model_manager = make_stream_manager(tmp_path)
    conversation = manager.create_conversation("side plain")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "/side quick question",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == StreamStatus.COMPLETE
    assert model_manager.get_model_calls == [("fake", True)]
    reloaded = manager.get_conversation(conversation.metadata["id"])
    assert len(reloaded.nodes) == 2
    current = reloaded.nodes[reloaded.current_node_id]
    assert current["user_message"]["content"] == "/side quick question"
