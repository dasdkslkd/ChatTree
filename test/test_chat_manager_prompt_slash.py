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
from backend.core.plans import PlanLedger
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.slash import SlashCommandDispatcher, SlashDispatchKind
from backend.core.tasks import TaskLedger
from backend.core.tools.orchestrator import ToolOrchestrator
from backend.core.tools.plan_tools import register_plan_tools
from backend.core.tools.security.approval import ApprovalManager
from backend.core.tools.security.logical_sandbox import LogicalSandbox
from backend.core.tools.security.permissions import PermissionEngine


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


class EnterPlanThenWriteProvider(CapturingProvider):
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
        if call_number == 1:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
                tool_calls=[
                    {
                        "id": "call_enter_plan",
                        "type": "function",
                        "function": {"name": "enter_plan_mode", "arguments": "{}"},
                    },
                    {
                        "id": "call_write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "x.txt", "content": "nope"}),
                        },
                    },
                ],
            )
        else:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="planning only",
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
            usage_info={"input_tokens": 1, "output_tokens": 0, "total_tokens": 1, "source": "test", "raw": {}},
        )


class PlanFinalWithoutExitProvider(CapturingProvider):
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
        if call_number == 1:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="我已经探索完了，这里是普通文本计划，但没有调用 exit_plan_mode。",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
            )
        else:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="",
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=1,
                tool_calls=[
                    {
                        "id": "call_update_plan",
                        "type": "function",
                        "function": {
                            "name": "update_plan",
                            "arguments": json.dumps({
                                "mode": "replace",
                                "content": "1. 修改设置页\n2. 增加验证",
                            }, ensure_ascii=False),
                        },
                    },
                    {
                        "id": "call_exit_plan",
                        "type": "function",
                        "function": {
                            "name": "exit_plan_mode",
                            "arguments": "{}",
                        },
                    }
                ],
            )
        yield StreamChunk(
            status=StreamStatus.COMPLETE,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=1,
            usage_info={"input_tokens": 1, "output_tokens": 0, "total_tokens": 1, "source": "test", "raw": {}},
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


class PlanModeToolManager:
    def __init__(self, plan_ledger):
        self.tools = {}
        register_plan_tools(self, plan_ledger)

    def register(self, tool):
        self.tools[tool.name] = tool

    def get_openai_tools(self, include_disabled=False):
        tools = [tool.to_openai_tool() for tool in self.tools.values()]
        tools.append({
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            },
        })
        return tools

    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        if name in self.tools:
            return await self.tools[name].execute(**arguments, _runtime_context=runtime_context)
        return json.dumps({"ok": True, "name": name}, ensure_ascii=False)


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


def test_send_message_stream_enter_plan_mode_blocks_same_round_write(tmp_path: Path):
    manager, model_manager = make_stream_manager(tmp_path)
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
    model_manager.provider = EnterPlanThenWriteProvider()
    conversation = manager.create_conversation("plan guard")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "先做计划再实现",
                model_id="fake-model",
            )
        )
    )

    tool_results = [chunk["tool_call"] for chunk in chunks if chunk.get("event_type") == "tool_result"]
    write_result = next(item for item in tool_results if item["name"] == "write_file")
    assert "permission_denied" in write_result["content"]
    assert "plan mode" in write_result["content"]
    reloaded = manager.get_conversation(conversation.metadata["id"])
    current = reloaded.nodes[reloaded.current_node_id]
    assert current["tool_permission_mode"] == "plan"


def test_send_message_stream_consumes_approved_plan_context_and_restores_permission(tmp_path: Path):
    manager, model_manager = make_stream_manager(tmp_path)
    plan_ledger = PlanLedger()
    manager.plan_ledger = plan_ledger
    conversation = manager.create_conversation("approved plan")
    active = asyncio.run(plan_ledger.enter_plan_mode(
        conversation_id=conversation.metadata["id"],
        previous_permission_mode="modify_only",
    ))
    awaiting = asyncio.run(plan_ledger.submit_plan(
        conversation_id=conversation.metadata["id"],
        plan="1. Update backend\n2. Run tests",
    ))
    assert awaiting.plan_id == active.plan_id
    asyncio.run(plan_ledger.approve_plan(
        conversation_id=conversation.metadata["id"],
        plan_id=awaiting.plan_id,
    ))

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "继续实现已批准的计划。",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == StreamStatus.COMPLETE
    full_prompt = "\n\n".join(str(message.get("content") or "") for message in model_manager.provider.messages)
    assert "Approved plan for this conversation" in full_prompt
    assert "Update backend" in full_prompt
    assert "Continue with the approved plan" in full_prompt
    reloaded = manager.get_conversation(conversation.metadata["id"])
    current = reloaded.nodes[reloaded.current_node_id]
    assert current["tool_permission_mode"] == "modify_only"
    assert asyncio.run(plan_ledger.consume_pending_context(conversation.metadata["id"])) == []


def test_send_message_stream_plan_mode_retries_until_exit_or_question_tool(tmp_path: Path):
    manager, model_manager = make_stream_manager(tmp_path)
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
    model_manager.provider = PlanFinalWithoutExitProvider()
    conversation = manager.create_conversation("plan guard final")
    asyncio.run(plan_ledger.enter_plan_mode(
        conversation_id=conversation.metadata["id"],
        previous_permission_mode="modify_only",
    ))

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "设置页增加项目栏",
                model_id="fake-model",
            )
        )
    )

    content_chunks = [chunk.get("content") for chunk in chunks if chunk.get("content")]
    assert "我已经探索完了，这里是普通文本计划，但没有调用 exit_plan_mode。" not in content_chunks
    assert len(model_manager.provider.calls) == 2
    reminder_text = "\n".join(
        str(message.get("content") or "")
        for call in model_manager.provider.calls
        for message in call["messages"]
        if message.get("role") == "system"
    )
    assert "Plan mode final response was discarded" in reminder_text
    assert "exit_plan_mode" in reminder_text
    current_plan = asyncio.run(plan_ledger.get_active_or_awaiting(conversation.metadata["id"]))
    assert current_plan is not None
    assert current_plan.status.value == "awaiting_approval"
    reloaded = manager.get_conversation(conversation.metadata["id"])
    assistant = reloaded.nodes[reloaded.current_node_id]["assistant_message"]
    assert "普通文本计划" not in assistant["content"]


def test_continue_plan_question_answer_stream_uses_hidden_control_response(tmp_path: Path):
    manager, model_manager = make_stream_manager(tmp_path)
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
    model_manager.provider = PlanFinalWithoutExitProvider()
    conversation = manager.create_conversation("plan question answer")
    asyncio.run(plan_ledger.enter_plan_mode(
        conversation_id=conversation.metadata["id"],
        previous_permission_mode="modify_only",
    ))
    asyncio.run(plan_ledger.ask_user_question(
        conversation_id=conversation.metadata["id"],
        question="项目栏是否默认显示？",
        options=[{"label": "默认显示", "description": "进入页面直接看到"}],
        tool_call_id="call-question",
    ))

    chunks = asyncio.run(
        collect_chunks(
            manager.continue_plan_action_stream(
                conversation_id=conversation.metadata["id"],
                content="默认显示",
                model_id="fake-model",
                message_subtype="plan_question_response",
            )
        )
    )

    assert chunks[-1]["status"] == StreamStatus.COMPLETE
    first_prompt = "\n\n".join(str(message.get("content") or "") for message in model_manager.provider.calls[0]["messages"])
    assert "The user answered your plan-mode clarification question." in first_prompt
    assert "项目栏是否默认显示？" in first_prompt
    assert "默认显示" in first_prompt
    reloaded = manager.get_conversation(conversation.metadata["id"])
    visible_messages = [
        msg for msg in reloaded.get_message_chain_from_node()
        if not msg.get("is_hidden_from_transcript")
    ]
    assert all(msg.get("content") != "默认显示" for msg in visible_messages)


def test_continue_plan_approval_stream_uses_hidden_control_response(tmp_path: Path):
    manager, model_manager = make_stream_manager(tmp_path)
    plan_ledger = PlanLedger()
    manager.plan_ledger = plan_ledger
    conversation = manager.create_conversation("plan approval answer")
    active = asyncio.run(plan_ledger.enter_plan_mode(
        conversation_id=conversation.metadata["id"],
        previous_permission_mode="modify_only",
    ))
    awaiting = asyncio.run(plan_ledger.submit_plan(
        conversation_id=conversation.metadata["id"],
        plan="1. 修改设置页\n2. 增加验证",
        tool_call_id="call-exit",
    ))
    assert awaiting.plan_id == active.plan_id

    chunks = asyncio.run(
        collect_chunks(
            manager.continue_plan_action_stream(
                conversation_id=conversation.metadata["id"],
                content="Plan approved. Continue with the approved implementation.",
                model_id="fake-model",
                message_subtype="plan_approval_response",
            )
        )
    )

    assert chunks[-1]["status"] == StreamStatus.COMPLETE
    first_prompt = "\n\n".join(str(message.get("content") or "") for message in model_manager.provider.calls[0]["messages"])
    assert "User has approved your plan" in first_prompt
    assert "## Approved Plan:" in first_prompt
    assert "修改设置页" in first_prompt
    assert "start coding" in first_prompt
    assert asyncio.run(plan_ledger.get_active_or_awaiting(conversation.metadata["id"])) is None
    reloaded = manager.get_conversation(conversation.metadata["id"])
    current = reloaded.nodes[reloaded.current_node_id]
    assert current["tool_permission_mode"] == "modify_only"
    visible_messages = [
        msg for msg in reloaded.get_message_chain_from_node()
        if not msg.get("is_hidden_from_transcript")
    ]
    assert all(msg.get("content") != "继续实现已批准的计划。" for msg in visible_messages)
    assert all(msg.get("subtype") != "plan_approval_response" for msg in visible_messages)


def test_send_message_stream_does_not_auto_approve_pending_plan_from_user_text(tmp_path: Path):
    manager, _model_manager = make_stream_manager(tmp_path)
    plan_ledger = PlanLedger()
    manager.plan_ledger = plan_ledger
    conversation = manager.create_conversation("ordinary user text")
    active = asyncio.run(plan_ledger.enter_plan_mode(
        conversation_id=conversation.metadata["id"],
        previous_permission_mode="modify_only",
    ))
    awaiting = asyncio.run(plan_ledger.submit_plan(
        conversation_id=conversation.metadata["id"],
        plan="1. 修改设置页\n2. 增加验证",
    ))
    assert awaiting.plan_id == active.plan_id

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "继续实现已批准的计划。",
                model_id="fake-model",
            )
        )
    )

    assert chunks[-1]["status"] == StreamStatus.COMPLETE
    current_plan = asyncio.run(plan_ledger.get_active_or_awaiting(conversation.metadata["id"]))
    assert current_plan is not None
    assert current_plan.status.value == "awaiting_approval"


def test_send_message_stream_rejects_manual_plan_permission_without_session(tmp_path: Path):
    manager, _model_manager = make_stream_manager(tmp_path)
    manager.plan_ledger = PlanLedger()
    conversation = manager.create_conversation("manual plan permission")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "实现一个清晰的小改动",
                model_id="fake-model",
                tool_permission_mode="plan",
            )
        )
    )

    assert chunks[-1]["status"] == StreamStatus.ERROR
    assert "enter_plan_mode" in chunks[-1]["error"]
    reloaded = manager.get_conversation(conversation.metadata["id"])
    visible_messages = [
        msg for msg in reloaded.get_message_chain_from_node()
        if msg.get("role") == Role.USER
    ]
    assert all(msg.get("content") != "实现一个清晰的小改动" for msg in visible_messages)


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
