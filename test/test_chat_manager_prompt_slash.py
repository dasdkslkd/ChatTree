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
from backend.core.chat.canonical_reader import messages_by_node
from backend.core.chat.conversation import Conversation
from backend.core.chat.node import NodeManager
from backend.core.config.types import Message, Role, StreamChunk, StreamController, StreamStatus
from backend.core.plans import PlanLedger
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.plan_repository import SQLitePlanRepository
from backend.core.persistence.repository import ChatRepository
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.slash import SlashCommandDispatcher, SlashDispatchKind
from backend.core.tools.orchestrator import ToolOrchestrator
from backend.core.tools.plan_tools import register_plan_tools
from backend.core.tools.security.capabilities import ToolCapability, capabilities_for_tool
from backend.core.tools.security.approval import ApprovalManager
from backend.core.tools.security.logical_sandbox import LogicalSandbox
from backend.core.tools.security.permissions import PermissionEngine


def make_manager(registry=None):
    manager = ChatManager.__new__(ChatManager)
    manager.capability_registry = registry
    manager.chat_repository = None
    manager.slash_dispatcher = SlashCommandDispatcher()
    return manager


def node_messages(manager, conversation_id, node_id):
    return messages_by_node(manager.chat_repository, conversation_id, [node_id]).get(node_id, [])


def latest_node_message(manager, conversation_id, node_id, role):
    messages = [message for message in node_messages(manager, conversation_id, node_id) if message.get("role") == role]
    assert messages
    return messages[-1]


def test_chat_manager_dispatches_builtin_slash_to_main_prompt():
    manager = make_manager()

    result = manager._dispatch_slash_content("/review focus on state bugs")

    assert result.kind == SlashDispatchKind.MAIN_PROMPT
    assert result.model_input is not None
    assert "focus on state bugs" in result.model_input


def test_chat_manager_dispatches_refer_to_refer_prompt():
    manager = make_manager()

    result = manager._dispatch_slash_content("/refer node:abc inspect the prior result")

    assert result.kind == SlashDispatchKind.REFER_PROMPT
    assert result.model_input is None
    assert result.args == "node:abc inspect the prior result"
    assert result.tool_policy.value == "inherit"


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

    contents = [str(message.get("content") or "") for message in messages]
    assert contents[0].startswith("# ChatTree Core Prompt")
    assert "base system" not in contents
    assert any("## Available Capabilities" in content for content in contents)
    assert any("<name>review</name>" in content for content in contents)
    assert any("检查代码" in content for content in contents)


def test_refer_prompt_injects_history_and_persists_only_inline_prompt(tmp_path: Path):
    async def scenario():
        manager, model_manager = make_stream_manager(tmp_path)
        manager.tool_manager = FakeToolManager()
        conversation = manager.create_conversation("refer")
        root_id = conversation.current_node_id
        old_node = NodeManager.create_node(parent_id=root_id, model_id="fake-model")
        conversation.add_node(old_node, root_id, focus=False)
        conversation.switch_to_node(root_id)
        manager.chat_repository.save(conversation)
        manager.chat_repository.ensure_branch(
            conversation,
            old_node["id"],
            provider_id="fake",
            model_id="fake-model",
        )
        manager.chat_repository.add_message(
            conversation.metadata["id"],
            old_node["id"],
            role=Role.USER.value,
            content="old branch failed because config path was wrong",
            message_id="old-user",
        )
        manager.chat_repository.add_message(
            conversation.metadata["id"],
            old_node["id"],
            role=Role.ASSISTANT.value,
            content="The failure came from using the project-local config root.",
            message_id="old-assistant",
        )
        manager.chat_repository.add_tool_call(
            conversation.metadata["id"],
            old_node["id"],
            tool_call_id="tool-1",
            name="shell",
            arguments="{}",
            call_index=0,
        )
        manager.chat_repository.add_tool_result(
            conversation.metadata["id"],
            old_node["id"],
            tool_result_id="old-tool",
            tool_call_id="tool-1",
            output="stderr: missing C:\\Users\\xyz\\.chattree\\config.json",
        )

        chunks = await collect_chunks(manager.send_message_stream(
            conversation.metadata["id"],
            f"/refer node:{old_node['id']} analyze the failure now",
            model_id="fake-model",
            provider_id="fake",
            parent_node_id=root_id,
            tool_permission_mode="ask_always",
        ))

        assert not [chunk for chunk in chunks if chunk.get("status") == StreamStatus.ERROR]
        stored = manager.get_conversation(conversation.metadata["id"])
        new_nodes = []
        for node in stored.nodes.values():
            messages = node_messages(manager, conversation.metadata["id"], node["id"])
            if any(message.get("role") == Role.USER and message.get("content") == "analyze the failure now" for message in messages):
                new_nodes.append(node)
        assert len(new_nodes) == 1
        new_user = latest_node_message(manager, conversation.metadata["id"], new_nodes[0]["id"], Role.USER)
        assert new_user["content"] == "analyze the failure now"
        assert "/refer" not in new_user["content"]

        sent_contents = [str(message.get("content") or "") for message in model_manager.provider.messages]
        refer_index = next(index for index, content in enumerate(sent_contents) if "Explicit /refer context" in content)
        prompt_index = next(index for index, content in enumerate(sent_contents) if content == "analyze the failure now")
        assert refer_index < prompt_index
        assert any("old branch failed because config path was wrong" in content for content in sent_contents)
        assert any("missing C:\\Users\\xyz\\.chattree\\config.json" in content for content in sent_contents)
        assert model_manager.provider.kwargs["tools"]

    asyncio.run(scenario())


def test_refer_prompt_requires_inline_prompt(tmp_path: Path):
    async def scenario():
        manager, _ = make_stream_manager(tmp_path)
        conversation = manager.create_conversation("refer")
        root_id = conversation.current_node_id
        old_node = NodeManager.create_node(parent_id=root_id, model_id="fake-model")
        conversation.add_node(old_node, root_id, focus=False)
        conversation.switch_to_node(root_id)
        manager.chat_repository.save(conversation)
        manager.chat_repository.ensure_branch(conversation, old_node["id"], provider_id="fake", model_id="fake-model")
        manager.chat_repository.add_message(
            conversation.metadata["id"],
            old_node["id"],
            role=Role.USER.value,
            content="old",
            message_id="old-user",
        )

        chunks = await collect_chunks(manager.send_message_stream(
            conversation.metadata["id"],
            f"/refer node:{old_node['id']}",
            model_id="fake-model",
            provider_id="fake",
            parent_node_id=root_id,
        ))
        return chunks

    chunks = asyncio.run(scenario())

    errors = [chunk for chunk in chunks if chunk.get("status") == StreamStatus.ERROR]
    assert errors
    assert "用法: /refer" in errors[0].get("error")


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
    def capabilities_for(self, name, workspace=None):
        if name == "test_tool":
            return {ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE}
        return capabilities_for_tool(name)

    def get_openai_tools(self, include_disabled=False):
        return [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "Test tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        return json.dumps({"ok": True}, ensure_ascii=False)


class FakeWorkflowToolManager(FakeToolManager):
    def get_openai_tools(self, include_disabled=False):
        return [
            {
                "type": "function",
                "function": {
                    "name": "start_workflow",
                    "description": "Start a real workflow",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "shell",
                    "description": "Run a command",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]


class WorkflowToolThenFinalProvider(CapturingProvider):
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
                        "id": "call_start_workflow",
                        "type": "function",
                        "function": {
                            "name": "start_workflow",
                            "arguments": json.dumps({
                                "script": "export default async function workflow(ctx) { return 1; }"
                            }),
                        },
                    }
                ],
            )
        else:
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content="workflow done",
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
                            "name": "write",
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


async def collect_chunks(stream):
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    return chunks


def make_stream_manager(tmp_path: Path):
    model_manager = CapturingModelManager()
    persistence = SQLitePersistence(tmp_path / "sqlite")
    persistence.initialize()
    repository = ChatRepository(persistence)
    manager = ChatManager(
        model_manager,
        ChatStorage(str(tmp_path / "conversations")),
        PromptStorage(str(tmp_path / "prompts")),
        chat_repository=repository,
    )
    return manager, model_manager


def make_plan_ledger(manager):
    return PlanLedger(repository=SQLitePlanRepository(manager.chat_repository.persistence))


class PlanModeToolManager:
    def __init__(self, plan_ledger):
        self.tools = {}
        register_plan_tools(self, plan_ledger)

    def register(self, tool):
        self.tools[tool.name] = tool

    def capabilities_for(self, name, workspace=None):
        return capabilities_for_tool(name)

    def get_openai_tools(self, include_disabled=False):
        tools = [tool.to_openai_tool() for tool in self.tools.values()]
        tools.append({
            "type": "function",
            "function": {
                "name": "write",
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
                parent_node_id=conversation.current_node_id,
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
    user_message = latest_node_message(manager, conversation.metadata["id"], current["id"], Role.USER)
    assert user_message["slash_command"]["command"] == "review"
    assert user_message["slash_command"]["original_input"] == "/review focus on auth"


def test_send_message_stream_enter_plan_mode_blocks_same_round_write(tmp_path: Path):
    manager, model_manager = make_stream_manager(tmp_path)
    plan_ledger = make_plan_ledger(manager)
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
                parent_node_id=conversation.current_node_id,
            )
        )
    )

    tool_results = [chunk["tool_call"] for chunk in chunks if chunk.get("event_type") == "tool_result"]
    write_result = next(item for item in tool_results if item["name"] == "write")
    assert "permission_denied" in write_result["content"]
    assert "plan mode" in write_result["content"]
    reloaded = manager.get_conversation(conversation.metadata["id"])
    current = reloaded.nodes[reloaded.current_node_id]
    assert current["tool_permission_mode"] == "plan"


def test_send_message_stream_rejects_manual_plan_permission_without_session(tmp_path: Path):
    manager, _model_manager = make_stream_manager(tmp_path)
    manager.plan_ledger = make_plan_ledger(manager)
    conversation = manager.create_conversation("manual plan permission")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "实现一个清晰的小改动",
                model_id="fake-model",
                tool_permission_mode="plan",
                parent_node_id=conversation.current_node_id,
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


def test_send_message_stream_allows_final_after_real_start_workflow(tmp_path: Path):
    manager, model_manager = make_stream_manager(tmp_path)
    manager.tool_manager = FakeWorkflowToolManager()
    model_manager.provider = WorkflowToolThenFinalProvider()
    conversation = manager.create_conversation("workflow real tool")

    chunks = asyncio.run(
        collect_chunks(
            manager.send_message_stream(
                conversation.metadata["id"],
                "启动一个 3 层 workflow 来验证积分结果",
                model_id="fake-model",
                parent_node_id=conversation.current_node_id,
            )
        )
    )

    content_chunks = [chunk.get("content") for chunk in chunks if chunk.get("content")]
    assert "workflow done" in content_chunks
    assert len(model_manager.provider.calls) == 2
    reloaded = manager.get_conversation(conversation.metadata["id"])
    assistant = latest_node_message(manager, conversation.metadata["id"], reloaded.current_node_id, Role.ASSISTANT)
    assert assistant["content"] == "workflow done"


def test_send_message_stream_btw_runs_isolated_side_question_without_tools(tmp_path: Path):
    skill_path = tmp_path / "tools" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# Tools\n\nThis injected skill mentions shell.", encoding="utf-8")
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
                parent_node_id=conversation.current_node_id,
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
    assert "This injected skill mentions shell" not in full_prompt
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
                parent_node_id=conversation.current_node_id,
            )
        )
    )

    assert chunks[-1]["status"] == StreamStatus.COMPLETE
    assert model_manager.get_model_calls == [("fake", True)]
    reloaded = manager.get_conversation(conversation.metadata["id"])
    assert len(reloaded.nodes) == 2
    current = reloaded.nodes[reloaded.current_node_id]
    user_message = latest_node_message(manager, conversation.metadata["id"], current["id"], Role.USER)
    assert user_message["content"] == "/side quick question"
