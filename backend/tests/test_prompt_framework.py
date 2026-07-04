import unittest
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.dependencies import get_chat_manager, get_run_manager, get_subagent_executor, get_workflow_manager
from backend.api.routes import messages as messages_route
from backend.api.routes import runs as runs_route
from backend.api.routes.messages import SendMessageRequest, detached_stream_event_generator
from backend.core.agents.mailbox import AgentMailbox
from backend.core.agents.runtime import AgentRuntime
from backend.core.agents.subagent_executor import SubagentExecutor
from backend.core.agents.types import AgentSource
from backend.core.capabilities.agent_loader import load_agent_roots
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import (
    AgentDefinition,
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySource,
)
from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.node import NodeManager
from backend.core.config.types import Message, Role, StreamStatus
from backend.core.prompts import PromptBuilder
from backend.core.prompts import types as prompt_types
from backend.core.prompts.catalog import (
    PROMPT_SOURCES,
    load_prompt_template,
    validate_prompt_catalog,
)
from backend.core.prompts.types import PromptBuildRequest
from backend.core.runs import RunKind, RunManager, RunStatus, SyntheticInputQueue
from backend.core.runs.journal import RunJournal
from backend.core.tools.agent_tools import StartSubagentTool, StartWorkflowTool
from backend.core.plans import PlanLedger
from backend.core.tasks import TaskLedger, TaskStatus
from backend.core.workflows.workflow_manager import WorkflowManager
from backend.core.slash.dispatcher import SlashCommandDispatcher
from backend.core.slash.registry import SlashCommandRegistry
from backend.core.workflows.js_runner import WorkflowJsRunner
from backend.core.workflows.runtime_bridge import WorkflowRuntimeBridge


class PromptCatalogTests(unittest.TestCase):
    def test_core_prompt_loads_as_chattree_prompt(self):
        text = load_prompt_template("core")
        self.assertIn("ChatTree", text)
        self.assertNotIn("Claude Code", text)
        self.assertNotIn("Codex", text)
        self.assertGreaterEqual(len(text), 3000)

    def test_core_prompt_defines_command_tool_boundaries(self):
        text = load_prompt_template("core")
        self.assertIn("run_command", text)
        self.assertIn("start_background_command", text)
        self.assertIn("wait_command", text)
        self.assertIn("Use `run_command` for command execution that should start foreground", text)
        self.assertIn("Use `start_background_command` only for true background command work", text)
        self.assertIn("Use `wait_command` only when the current answer must join", text)
        self.assertIn("auto-background", text)
        self.assertIn("active shell declared by the command tool description", text)
        self.assertNotIn("start_terminal", text)
        self.assertNotIn("wait_terminal", text)
        self.assertIn("If the user explicitly asked for a background command and it fails", text)
        self.assertIn("do not describe the final result as completed by the background run", text)

    def test_core_prompt_distinguishes_fresh_subagents_from_forks(self):
        text = load_prompt_template("core")
        self.assertIn("Fresh subagents start without the current conversation context", text)
        self.assertIn("A fork inherits the current conversation context", text)

    def test_side_prompt_is_not_registered(self):
        self.assertNotIn("side", PROMPT_SOURCES)
        self.assertNotIn("btw", PROMPT_SOURCES)

    def test_required_templates_have_source_mappings(self):
        for name in ["core", "fork", "workflow", "review", "init"]:
            self.assertIn(name, PROMPT_SOURCES)
            self.assertTrue(PROMPT_SOURCES[name])

    def test_prompt_catalog_validation_can_skip_local_reference_files(self):
        with patch.dict(PROMPT_SOURCES, {"core": ("reference/does-not-exist.md",)}, clear=False):
            validate_prompt_catalog(require_source_files=False)

    def test_templates_are_not_short_summaries(self):
        minimums = {
            "core": 3000,
            "fork": 1200,
            "workflow": 9000,
            "review": 3000,
            "init": 8000,
            "agent:explorer": 600,
            "agent:planner": 600,
            "agent:implementer": 600,
            "agent:reviewer": 600,
            "agent:verifier": 600,
            "agent:workflow-worker": 600,
        }
        for name, minimum in minimums.items():
            with self.subTest(name=name):
                self.assertGreaterEqual(len(load_prompt_template(name)), minimum)

    def test_no_legacy_side_conversation_prompt_text_remains(self):
        forbidden = ["side conversation"]
        for name in PROMPT_SOURCES:
            text = load_prompt_template(name).lower()
            for phrase in forbidden:
                self.assertNotIn(phrase, text, name)

    def test_templates_have_no_unrendered_source_variables(self):
        forbidden_terms = ["AskUserQuestion", "SYSTEM_TAG_NAME", "AGENT_TOOL_NAME", "WORKFLOW_TOOL_NAME"]
        for name in PROMPT_SOURCES:
            text = load_prompt_template(name)
            with self.subTest(name=name):
                self.assertNotIn("<!--", text)
                self.assertNotRegex(text.lower(), r"(?m)^source:\s*$")
                self.assertNotIn("reference/claude", text.lower())
                self.assertNotIn("reference/codex", text.lower())
                for term in forbidden_terms:
                    self.assertNotIn(term, text)
                self.assertNotRegex(text, r"\$\{[A-Z0-9_]+\}")
                if name != "review":
                    self.assertNotIn("{{", text)
                    self.assertNotIn("}}", text)
                self.assertNotRegex(text, r"(?m)^\s*import\s+")


class PromptBuilderFrameworkTests(unittest.TestCase):
    def test_default_core_prompt_injected_when_no_custom_system_prompt(self):
        messages = PromptBuilder().build(
            PromptBuildRequest(
                base_messages=[
                    {"role": "user", "content": "hello"},
                ],
                include_core_prompt=True,
            )
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("ChatTree", messages[0]["content"])
        self.assertEqual(messages[1]["role"], "user")

    def test_core_prompt_injected_after_existing_system_messages(self):
        messages = PromptBuilder().build(
            PromptBuildRequest(
                base_messages=[
                    {"role": "system", "content": "user selected system"},
                    {"role": "user", "content": "hello"},
                ],
                include_core_prompt=True,
            )
        )
        self.assertEqual(messages[0]["content"], "user selected system")
        self.assertEqual(messages[1]["role"], "system")
        self.assertIn("ChatTree", messages[1]["content"])
        self.assertEqual(messages[2]["role"], "user")

    def test_custom_system_prompt_override_replaces_core_prompt(self):
        messages = PromptBuilder().build(
            PromptBuildRequest(
                base_messages=[
                    {"role": "user", "content": "hello"},
                ],
                include_core_prompt=True,
                custom_system_prompt="user selected system",
                custom_system_prompt_mode="override",
            )
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "user selected system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertFalse(any("ChatTree" in message.get("content", "") for message in messages))

    def test_custom_system_prompt_append_keeps_core_prompt_then_custom_prompt(self):
        messages = PromptBuilder().build(
            PromptBuildRequest(
                base_messages=[
                    {"role": "user", "content": "hello"},
                ],
                include_core_prompt=True,
                custom_system_prompt="user selected system",
                custom_system_prompt_mode="append",
            )
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("ChatTree", messages[0]["content"])
        self.assertEqual(messages[1]["role"], "system")
        self.assertEqual(messages[1]["content"], "user selected system")
        self.assertEqual(messages[2]["role"], "user")

    def test_runtime_context_order_with_override_capabilities_skills_and_history(self):
        self.assertTrue(hasattr(prompt_types, "RuntimePromptContext"))
        runtime_context = prompt_types.RuntimePromptContext(
            name="main",
            content="Runtime mode: main chat",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_path = Path(temp_dir) / "review" / "SKILL.md"
            skill_path.parent.mkdir()
            skill_path.write_text("# Review\n\n检查代码。", encoding="utf-8")
            registry = CapabilityRegistry()
            registry.add_capabilities([
                CapabilityDefinition(
                    name="review",
                    kind=CapabilityKind.SKILL,
                    source=CapabilitySource.PROJECT,
                    description="Review skill",
                    path=skill_path,
                )
            ])

            messages = PromptBuilder(registry).build(
                PromptBuildRequest(
                    base_messages=[{"role": "user", "content": "hello"}],
                    active_skill_names=["review"],
                    custom_system_prompt="override prompt",
                    custom_system_prompt_mode="override",
                    runtime_context=runtime_context,
                )
            )

        self.assertEqual(messages[0]["content"], "override prompt")
        self.assertIn("Runtime mode: main chat", messages[1]["content"])
        self.assertIn("## Available Capabilities", messages[2]["content"])
        self.assertIn("<name>review</name>", messages[3]["content"])
        self.assertEqual(messages[4]["content"], "hello")
        self.assertFalse(any("# ChatTree Core Prompt" in message.get("content", "") for message in messages))

    def test_runtime_context_order_with_append_custom_prompt(self):
        self.assertTrue(hasattr(prompt_types, "RuntimePromptContext"))
        messages = PromptBuilder().build(
            PromptBuildRequest(
                base_messages=[{"role": "user", "content": "hello"}],
                custom_system_prompt="append prompt",
                custom_system_prompt_mode="append",
                runtime_context=prompt_types.RuntimePromptContext(
                    name="main",
                    content="Runtime mode: main chat",
                ),
            )
        )

        self.assertIn("# ChatTree Core Prompt", messages[0]["content"])
        self.assertIn("Runtime mode: main chat", messages[1]["content"])
        self.assertEqual(messages[2]["content"], "append prompt")
        self.assertEqual(messages[3]["content"], "hello")


class ChatManagerPromptSelectionTests(unittest.TestCase):
    class FakeStorage:
        def __init__(self):
            self.saved = None

        def save(self, data):
            self.saved = data

    class FakePromptStorage:
        def load(self, prompt_id):
            self.loaded_id = prompt_id
            return "snapshot prompt body"

    def test_create_conversation_snapshots_selected_prompt_metadata_without_root_system_message(self):
        storage = self.FakeStorage()
        prompts = self.FakePromptStorage()
        manager = ChatManager(model_manager=None, storage=storage, prompts=prompts)

        conversation = manager.create_conversation(
            "title",
            prompt_id="prompt-1",
            prompt_mode="append",
        )

        self.assertEqual(prompts.loaded_id, "prompt-1")
        self.assertEqual(
            conversation.metadata["selected_system_prompt"],
            {
                "id": "prompt-1",
                "mode": "append",
                "content": "snapshot prompt body",
            },
        )
        root = conversation.nodes[conversation.root_node_id]
        self.assertIsNone(root["system_message"])
        self.assertEqual(
            storage.saved["metadata"]["selected_system_prompt"]["content"],
            "snapshot prompt body",
        )


class ChatManagerRuntimeContextTests(unittest.IsolatedAsyncioTestCase):
    class FakeStorage:
        def __init__(self):
            self.saved = None

        def save(self, data):
            self.saved = data

        def load(self, conversation_id):
            if self.saved and self.saved["metadata"]["id"] == conversation_id:
                return self.saved
            return None

    class FakePromptStorage:
        def load(self, prompt_id):
            return None

    class CapturingProvider:
        def __init__(self):
            self.messages = None

        async def generate_response_stream(self, **kwargs):
            self.messages = kwargs["messages"]
            yield {"status": StreamStatus.COMPLETE, "content": "", "tokens_used": 0}

    def test_main_chat_builds_main_runtime_context(self):
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
        )
        conversation = manager.create_conversation("title")

        messages = manager._build_prompt_messages(conversation, [])

        self.assertIn("# ChatTree Core Prompt", messages[0]["content"])
        self.assertIn("Runtime mode: main chat", messages[1]["content"])
        self.assertNotIn("start_subagent", messages[1]["content"])
        self.assertNotIn("spawn_agent", messages[1]["content"])

    def test_main_runtime_context_includes_plan_mode_rules_when_enabled(self):
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
            plan_ledger=PlanLedger(),
        )
        conversation = manager.create_conversation("title")

        messages = manager._build_prompt_messages(conversation, [])

        self.assertIn("Plan mode rules:", messages[1]["content"])
        self.assertIn("Use `enter_plan_mode` only when", messages[1]["content"])
        self.assertIn("genuine ambiguity", messages[1]["content"])
        self.assertIn("Do not enter plan mode merely because the task is large", messages[1]["content"])
        self.assertIn("When the user asks you to implement now", messages[1]["content"])
        self.assertIn("call `exit_plan_mode` with the plan", messages[1]["content"])

    def test_active_plan_mode_prompt_requires_structured_exit_or_question(self):
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
            plan_ledger=PlanLedger(),
        )
        conversation = manager.create_conversation("title")
        root = conversation.nodes[conversation.current_node_id]
        root["tool_permission_mode"] = "plan"

        messages = manager._build_prompt_messages(conversation, [])

        self.assertIn("Plan mode is active:", messages[1]["content"])
        self.assertIn("read-only planning phase", messages[1]["content"])
        self.assertIn("must end with exactly one structured plan-mode action", messages[1]["content"])
        self.assertIn("Do not ask whether the plan is acceptable in text", messages[1]["content"])

    async def test_main_runtime_context_lists_open_tasks(self):
        task_ledger = TaskLedger()
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
            task_ledger=task_ledger,
        )
        conversation = manager.create_conversation("title")
        task = await task_ledger.create_task(
            conversation_id=conversation.metadata["id"],
            title="检查 reference 中的 plan 模式",
            detail="x" * 260,
            created_by_run_id="run-1",
        )
        await task_ledger.bind_run(
            conversation_id=conversation.metadata["id"],
            task_id=task.task_id,
            run_id="run-subagent",
            owner_type="subagent",
        )

        messages = manager._build_prompt_messages(conversation, [])

        self.assertIn("Open Tasks:", messages[1]["content"])
        self.assertIn(task.task_id, messages[1]["content"])
        self.assertIn("run-subagent", messages[1]["content"])
        self.assertIn("Before final response", messages[1]["content"])
        self.assertNotIn("x" * 200, messages[1]["content"])

    async def test_task_completion_guard_requires_nudge_for_unresolved_task(self):
        task_ledger = TaskLedger()
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
            task_ledger=task_ledger,
        )
        conversation = manager.create_conversation("title")
        await task_ledger.create_task(
            conversation_id=conversation.metadata["id"],
            title="检查实现",
            created_by_run_id="run-1",
        )

        needs_nudge, open_tasks = await manager._needs_task_completion_nudge(
            conversation.metadata["id"],
            "已经完成。",
        )

        self.assertTrue(needs_nudge)
        self.assertEqual(len(open_tasks), 1)

    async def test_task_completion_guard_allows_reported_blocked_tasks(self):
        task_ledger = TaskLedger()
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
            task_ledger=task_ledger,
        )
        conversation = manager.create_conversation("title")
        task = await task_ledger.create_task(
            conversation_id=conversation.metadata["id"],
            title="联网验证",
            created_by_run_id="run-1",
        )
        await task_ledger.update_task(
            conversation_id=conversation.metadata["id"],
            task_id=task.task_id,
            status=TaskStatus.BLOCKED,
            evidence_summary="searxng connection refused",
        )

        needs_nudge, open_tasks = await manager._needs_task_completion_nudge(
            conversation.metadata["id"],
            f"{task.task_id} 已阻塞：searxng connection refused",
        )

        self.assertFalse(needs_nudge)
        self.assertEqual(len(open_tasks), 1)

    async def test_task_completion_guard_allows_blocked_tasks_with_evidence_without_restatement(self):
        task_ledger = TaskLedger()
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
            task_ledger=task_ledger,
        )
        conversation = manager.create_conversation("title")
        task = await task_ledger.create_task(
            conversation_id=conversation.metadata["id"],
            title="联网验证",
            created_by_run_id="run-1",
        )
        await task_ledger.update_task(
            conversation_id=conversation.metadata["id"],
            task_id=task.task_id,
            status=TaskStatus.BLOCKED,
            evidence_summary="searxng connection refused",
        )

        needs_nudge, open_tasks = await manager._needs_task_completion_nudge(
            conversation.metadata["id"],
            "该验证无法继续，已按失败路径收口。",
        )

        self.assertFalse(needs_nudge)
        self.assertEqual(len(open_tasks), 1)

    def test_explicit_subagent_request_builds_multi_agent_runtime_context(self):
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
        )
        conversation = manager.create_conversation("title")
        conversation.nodes[conversation.current_node_id]["user_message"] = {
            "role": "user",
            "content": "请开 subagent 检查这个问题",
        }

        messages = manager._build_prompt_messages(conversation, [])

        self.assertIn("spawn_agent", messages[1]["content"])
        self.assertIn("wait_agent", messages[1]["content"])
        self.assertIn("Do not replace an explicit subagent request", messages[1]["content"])
        self.assertNotIn("start_subagent", messages[1]["content"])

    def test_create_conversation_persists_initial_multi_agent_mode(self):
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
        )

        conversation = manager.create_conversation("title", multi_agent_mode="proactive")

        self.assertEqual(conversation.metadata["multi_agent_mode"], "proactive")
        messages = manager._build_prompt_messages(conversation, [])
        self.assertIn("You may proactively delegate", messages[1]["content"])

    def test_clarification_turn_inherits_explicit_subagent_request(self):
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
        )
        conversation = manager.create_conversation("title")
        first = NodeManager.create_node(
            Message({"role": Role.USER, "content": "开 subagent 计算几个积分"}),
            parent_id=conversation.current_node_id,
        )
        conversation.add_node(first, parent_id=conversation.current_node_id)
        second = NodeManager.create_node(
            Message({"role": Role.USER, "content": "计算几个随机的积分"}),
            parent_id=conversation.current_node_id,
        )
        conversation.add_node(second, parent_id=conversation.current_node_id)

        messages = manager._build_prompt_messages(conversation, [])
        inherited_text = manager._multi_agent_intent_text(conversation, "计算几个随机的积分")
        mode = manager._resolve_multi_agent_mode(inherited_text, conversation.metadata)
        tools = manager._filter_agent_tools_for_mode(
            [
                {"type": "function", "function": {"name": "spawn_agent"}},
                {"type": "function", "function": {"name": "run_command"}},
            ],
            mode,
        )

        self.assertIn("spawn_agent", messages[1]["content"])
        self.assertEqual(mode, "explicit_request_only")
        self.assertEqual([tool["function"]["name"] for tool in tools], ["spawn_agent", "run_command"])

    def test_short_confirmation_turn_inherits_explicit_subagent_request(self):
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
        )
        conversation = manager.create_conversation("title")
        first = NodeManager.create_node(
            Message({"role": Role.USER, "content": "请用 subagent 检查这个实现"}),
            parent_id=conversation.current_node_id,
        )
        conversation.add_node(first, parent_id=conversation.current_node_id)
        first["assistant_message"] = Message({"role": Role.ASSISTANT, "content": "可以，具体检查什么？"})
        second = NodeManager.create_node(
            Message({"role": Role.USER, "content": "继续"}),
            parent_id=conversation.current_node_id,
        )
        conversation.add_node(second, parent_id=conversation.current_node_id)

        inherited_text = manager._multi_agent_intent_text(conversation, "继续")
        mode = manager._resolve_multi_agent_mode(inherited_text, conversation.metadata)
        tools = manager._filter_agent_tools_for_mode(
            [
                {"type": "function", "function": {"name": "spawn_agent"}},
                {"type": "function", "function": {"name": "run_command"}},
            ],
            mode,
        )

        self.assertEqual(mode, "explicit_request_only")
        self.assertEqual([tool["function"]["name"] for tool in tools], ["spawn_agent", "run_command"])

    async def test_conversation_multi_agent_mode_can_be_set_to_proactive(self):
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
        )
        conversation = manager.create_conversation("title")

        ok = await manager.update_conversation_multi_agent_mode(
            conversation.metadata["id"],
            "proactive",
        )
        loaded = manager.get_conversation(conversation.metadata["id"])

        self.assertTrue(ok)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.metadata["multi_agent_mode"], "proactive")
        messages = manager._build_prompt_messages(loaded, [])
        self.assertIn("You may proactively delegate", messages[1]["content"])
        tools = manager._filter_agent_tools_for_mode(
            [
                {"type": "function", "function": {"name": "spawn_agent"}},
                {"type": "function", "function": {"name": "run_command"}},
            ],
            manager._resolve_multi_agent_mode("普通问题", loaded.metadata),
        )
        self.assertEqual([tool["function"]["name"] for tool in tools], ["spawn_agent", "run_command"])

    async def test_btw_builds_side_question_runtime_context(self):
        manager = ChatManager(
            model_manager=None,
            storage=self.FakeStorage(),
            prompts=self.FakePromptStorage(),
        )
        conversation = manager.create_conversation("title")
        provider = self.CapturingProvider()

        async for _ in manager._send_side_question_stream(
            conversation=conversation,
            content="summarize this",
            provider=provider,
            target_model="model",
            eff_effort=None,
            eff_thinking=None,
            run_id="run-1",
        ):
            pass

        self.assertIsNotNone(provider.messages)
        self.assertIn("# ChatTree Core Prompt", provider.messages[0]["content"])
        self.assertIn("Runtime mode: side question (/btw)", provider.messages[1]["content"])
        self.assertEqual(provider.messages[-1]["content"], "summarize this")


class SlashPromptPolicyTests(unittest.TestCase):
    def test_btw_command_is_registered_as_side_question(self):
        commands = SlashCommandRegistry.builtins()
        command = commands.get("btw")
        self.assertIsNotNone(command)
        self.assertEqual(command.dispatch_kind.value, "side_question")
        self.assertEqual(command.tool_policy.value, "disabled")
        self.assertEqual(command.persistence_policy.value, "side_run")
        self.assertEqual(command.run_kind, "side_question")
        self.assertFalse(command.blocks_main_thread)

    def test_btw_dispatches_side_question_prompt(self):
        result = SlashCommandDispatcher().dispatch("/btw summarize context")
        self.assertEqual(result.kind.value, "side_question")
        self.assertEqual(result.run_kind, "side_question")
        self.assertIn("summarize context", result.model_input)
        self.assertTrue(result.disable_tools)

    def test_status_dispatches_direct_response_without_prompt(self):
        result = SlashCommandDispatcher().dispatch("/status")
        self.assertEqual(result.kind.value, "direct_response")
        self.assertEqual(result.run_kind, "direct_response")
        self.assertIsNone(result.model_input)
        self.assertTrue(result.disable_tools)


class SlashTemplateTests(unittest.TestCase):
    def test_init_uses_template_prompt(self):
        result = SlashCommandDispatcher().dispatch("/init")
        self.assertIn("AGENTS.md", result.model_input)
        self.assertIn("ChatTree", result.model_input)
        self.assertGreaterEqual(len(result.model_input), 8000)

    def test_review_uses_template_prompt_with_target(self):
        result = SlashCommandDispatcher().dispatch("/review current branch")
        self.assertIn("current branch", result.model_input)
        self.assertIn("findings", result.model_input.lower())
        self.assertGreaterEqual(len(result.model_input), 3000)
        self.assertNotIn("{{REVIEW_TARGET}}", result.model_input)
        self.assertNotIn("${", result.model_input)


class StreamChunkRoutingTests(unittest.TestCase):
    def test_child_run_started_chunk_preserves_side_run_fields(self):
        chunk = {
            "status": StreamStatus.CONTENT,
            "content": "",
            "node_id": "node-1",
            "target_node_id": "node-1",
            "run_id": "run-parent",
            "conversation_id": "conversation-1",
            "event_type": "child_run_started",
            "child_run_id": "run-child",
            "child_kind": RunKind.SUBAGENT.value,
            "child_status": "running",
            "child_summary": "检查实现",
            "payload": {
                "run_id": "run-child",
                "kind": RunKind.SUBAGENT.value,
            },
        }

        payload = messages_route.build_stream_chunk_data(chunk, "conversation-1")

        self.assertEqual(payload["event_type"], "child_run_started")
        self.assertEqual(payload["child_run_id"], "run-child")
        self.assertEqual(payload["child_kind"], RunKind.SUBAGENT.value)
        self.assertEqual(payload["child_status"], "running")
        self.assertEqual(payload["child_summary"], "检查实现")
        self.assertEqual(payload["payload"]["run_id"], "run-child")


class SlashRuntimeDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def _collect_events(self, generator):
        events = []
        async for event in generator:
            events.append(event)
        return events

    async def test_fork_slash_starts_subagent_run_without_chat_run(self):
        class FakeChatManager:
            def __init__(self):
                self.anchor_kwargs = None

            async def create_visible_user_anchor_node(self, **kwargs):
                self.anchor_kwargs = kwargs
                return "slash-node-1"

            async def send_message_stream(self, **kwargs):
                raise AssertionError("chat stream should not be used for /fork")

        class FakeSubagentExecutor:
            def __init__(self):
                self.kwargs = None

            async def start(self, **kwargs):
                self.kwargs = kwargs
                return {"run_id": "subagent-1"}

        class FakeRunManager:
            async def subscribe(self, run_id, from_event):
                yield {"status": "complete", "run_id": run_id}

        subagent = FakeSubagentExecutor()
        chat = FakeChatManager()
        events = await self._collect_events(detached_stream_event_generator(
            "conversation-1",
            SendMessageRequest(
                content="/fork inspect prompts",
                node_id="node-1",
                tool_permission_mode="modify_only",
            ),
            chat,
            FakeRunManager(),
            subagent,
            None,
        ))
        self.assertEqual(chat.anchor_kwargs["content"], "/fork inspect prompts")
        self.assertEqual(chat.anchor_kwargs["parent_node_id"], "node-1")
        self.assertEqual(chat.anchor_kwargs["tool_permission_mode"], "modify_only")
        self.assertEqual(subagent.kwargs["agent_name"], "implementer")
        self.assertEqual(subagent.kwargs["input_data"], "inspect prompts")
        self.assertEqual(subagent.kwargs["parent_node_id"], "slash-node-1")
        self.assertEqual(subagent.kwargs["permission_mode"], "modify_only")
        self.assertTrue(events[-1].strip().endswith("[DONE]"))

    async def test_workflow_slash_starts_workflow_run_without_chat_run(self):
        class FakeChatManager:
            def __init__(self):
                self.anchor_kwargs = None

            async def create_visible_user_anchor_node(self, **kwargs):
                self.anchor_kwargs = kwargs
                return "slash-node-1"

            async def send_message_stream(self, **kwargs):
                raise AssertionError("chat stream should not be used for /workflow")

        class FakeWorkflowManager:
            def __init__(self):
                self.kwargs = None

            async def start(self, **kwargs):
                self.kwargs = kwargs
                return {"run_id": "workflow-1"}

        class FakeRunManager:
            async def subscribe(self, run_id, from_event):
                yield {"status": "complete", "run_id": run_id}

        workflow = FakeWorkflowManager()
        chat = FakeChatManager()
        events = await self._collect_events(detached_stream_event_generator(
            "conversation-1",
            SendMessageRequest(
                content="/workflow return 1",
                node_id="node-1",
                tool_permission_mode="ask_always",
            ),
            chat,
            FakeRunManager(),
            None,
            workflow,
        ))
        self.assertEqual(chat.anchor_kwargs["content"], "/workflow return 1")
        self.assertEqual(chat.anchor_kwargs["parent_node_id"], "node-1")
        self.assertEqual(workflow.kwargs["script"], "return 1")
        self.assertEqual(workflow.kwargs["parent_node_id"], "slash-node-1")
        self.assertEqual(workflow.kwargs["permission_mode"], "ask_always")
        self.assertTrue(events[-1].strip().endswith("[DONE]"))

    async def test_btw_slash_runs_as_side_question_chat_run(self):
        class FakeChatManager:
            def __init__(self):
                self.kwargs = None

            async def send_message_stream(self, **kwargs):
                self.kwargs = kwargs
                yield {
                    "status": "complete",
                    "content": "side answer",
                    "node_id": None,
                    "target_node_id": None,
                    "run_id": kwargs["run_id"],
                }

        chat = FakeChatManager()
        run_manager = RunManager()
        events = await self._collect_events(detached_stream_event_generator(
            "conversation-1",
            SendMessageRequest(content="/btw summarize context", node_id="node-1"),
            chat,
            run_manager,
            None,
            None,
        ))
        runs = run_manager.list_runs("conversation-1")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["kind"], "side_question")
        self.assertIsNone(runs[0]["target_node_id"])
        self.assertIn("summarize context", chat.kwargs["content"])
        self.assertEqual(chat.kwargs["node_id"], "node-1")
        self.assertTrue(any('"target_node_id": null' in event for event in events))
        self.assertTrue(events[-1].strip().endswith("[DONE]"))

    async def test_status_slash_runs_as_direct_response_without_chat_stream(self):
        class FakeChatManager:
            async def send_message_stream(self, **kwargs):
                raise AssertionError("chat stream should not be used for /status")

        run_manager = RunManager()
        events = await self._collect_events(detached_stream_event_generator(
            "conversation-1",
            SendMessageRequest(content="/status", node_id="node-1"),
            FakeChatManager(),
            run_manager,
            None,
            None,
        ))
        runs = run_manager.list_runs("conversation-1")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["kind"], "direct_response")
        self.assertIsNone(runs[0]["target_node_id"])
        self.assertTrue(any('"status": "content"' in event for event in events))
        self.assertTrue(any("ChatTree" in event for event in events))
        self.assertTrue(events[-1].strip().endswith("[DONE]"))


class SyntheticInputQueueTests(unittest.TestCase):
    def test_enqueue_list_pending_and_consume_task_notification(self):
        queue = SyntheticInputQueue()

        item = queue.enqueue(
            kind="task_notification",
            conversation_id="conversation-1",
            anchor_node_id="node-1",
            source_run_id="run-1",
            source_run_kind="subagent",
            status="pending",
            summary="implementer completed",
            content="result text",
            metadata={"origin": "task_notification", "source_status": "completed"},
        )
        duplicate = queue.enqueue(
            kind="task_notification",
            conversation_id="conversation-1",
            anchor_node_id="node-1",
            source_run_id="run-1",
            source_run_kind="subagent",
            status="pending",
            summary="implementer completed again",
            content="duplicate",
            metadata={"origin": "task_notification", "source_status": "completed"},
        )

        self.assertEqual(item.input_id, duplicate.input_id)
        pending = queue.list_pending("conversation-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "task_notification")
        self.assertEqual(pending[0]["status"], "pending")
        self.assertEqual(pending[0]["source_run_kind"], "subagent")
        self.assertEqual(pending[0]["metadata"]["origin"], "task_notification")
        self.assertEqual(pending[0]["metadata"]["source_status"], "completed")
        self.assertIsNone(pending[0]["consumed_at"])

        consumed = queue.mark_consumed("conversation-1", item.input_id)

        self.assertIsNotNone(consumed)
        self.assertEqual(consumed["status"], "consumed")
        self.assertIsNotNone(consumed["consumed_at"])
        self.assertEqual(queue.list_pending("conversation-1"), [])


class RunLifecycleContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_child_run_emits_standard_parent_child_event(self):
        run_manager = RunManager()
        parent = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.CHAT,
            target_node_id="assistant-node-1",
            summary="parent",
        )

        child = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="assistant-node-1",
            parent_run_id=parent.run_id,
            summary="reviewer: inspect",
            metadata={"agent_name": "reviewer"},
        )

        parent_events = [
            event["payload"]
            for event in run_manager.journal.read_events("conversation-1", parent.run_id)
        ]
        child_events = [
            payload for payload in parent_events
            if payload.get("event_type") == "child_run_started"
        ]

        self.assertEqual(len(child_events), 1)
        self.assertEqual(child_events[0]["child_run_id"], child.run_id)
        self.assertEqual(child_events[0]["child_kind"], RunKind.SUBAGENT.value)
        self.assertEqual(child_events[0]["payload"]["parent_run_id"], parent.run_id)
        self.assertEqual(child_events[0]["payload"]["metadata"]["agent_name"], "reviewer")

    async def test_legacy_start_subagent_tool_preserves_parent_run_id(self):
        class FakeSubagentExecutor:
            def __init__(self):
                self.kwargs = None

            async def start(self, **kwargs):
                self.kwargs = kwargs
                return {"run_id": "run-child", "kind": RunKind.SUBAGENT.value, "status": "running"}

        executor = FakeSubagentExecutor()
        tool = StartSubagentTool(subagent_executor=executor)

        await tool.execute(
            task="检查实现",
            agent_name="reviewer",
            _runtime_context={
                "run_id": "run-parent",
                "run_kind": RunKind.CHAT.value,
                "conversation_id": "conversation-1",
                "node_id": "assistant-node-1",
            },
        )

        self.assertEqual(executor.kwargs["parent_run_id"], "run-parent")

    async def test_legacy_start_workflow_tool_preserves_parent_run_id(self):
        class FakeWorkflowManager:
            def __init__(self):
                self.kwargs = None

            async def start(self, **kwargs):
                self.kwargs = kwargs
                return {"run_id": "run-workflow", "kind": RunKind.WORKFLOW.value, "status": "running"}

        manager = FakeWorkflowManager()
        tool = StartWorkflowTool(workflow_manager=manager)

        await tool.execute(
            script="log('hello')",
            _runtime_context={
                "run_id": "run-parent",
                "run_kind": RunKind.CHAT.value,
                "conversation_id": "conversation-1",
                "node_id": "assistant-node-1",
            },
        )

        self.assertEqual(manager.kwargs["parent_run_id"], "run-parent")


class SyntheticTaskNotificationTests(unittest.IsolatedAsyncioTestCase):
    class FakeConversation:
        current_provider = "fake"
        current_model = "model"
        metadata = {}

    class FakeProvider:
        def __init__(self, *, fail: bool = False):
            self.fail = fail

        async def generate_response_stream(self, **kwargs):
            if self.fail:
                yield {"status": StreamStatus.ERROR, "error": "provider failed"}
                return
            yield {"status": StreamStatus.CONTENT, "content": "subagent answer"}
            yield {"status": StreamStatus.COMPLETE, "content": "", "tokens_used": 0}

    class FakeModelManager:
        def __init__(self, provider):
            self.provider = provider
            self.model_list = {"fake": ["model"]}

        def get_model(self, provider_id, stream=False):
            return self.provider

    class FakeChatManager:
        tool_manager = None

        def __init__(self, provider):
            self.model_manager = SyntheticTaskNotificationTests.FakeModelManager(provider)

        def get_conversation(self, conversation_id):
            return SyntheticTaskNotificationTests.FakeConversation()

        def _provider_for_model(self, model_id):
            return "fake"

    def _executor(self, run_manager, provider, mailbox=None):
        registry = CapabilityRegistry()
        registry.add_agents([
            AgentDefinition(
                name="implementer",
                system_prompt="Implementer body",
                provider_id="fake",
                model_id="model",
            )
        ])
        return SubagentExecutor(
            chat_manager=self.FakeChatManager(provider),
            run_manager=run_manager,
            capability_registry=registry,
            mailbox=mailbox,
        )

    async def test_completed_subagent_enqueues_pending_task_notification_once(self):
        run_manager = RunManager()
        executor = self._executor(run_manager, self.FakeProvider())
        run = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="node-1",
            summary="implementer: inspect",
            metadata={
                "agent_name": "implementer",
                "delegated_task": "inspect",
                "task_id": "task_abc123",
                "slash_command": {"original_input": "/fork inspect"},
            },
        )

        await executor._produce(
            run_id=run.run_id,
            conversation_id="conversation-1",
            agent_name="implementer",
            input_data="inspect",
            parent_node_id="node-1",
            parent_run_id=None,
            provider_id=None,
            model_id=None,
            permission_mode=None,
            workspace=None,
        )
        await executor._enqueue_synthetic_task_notification(run.run_id, "completed", "duplicate")

        pending = run_manager.synthetic_inputs.list_pending("conversation-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "task_notification")
        self.assertEqual(pending[0]["anchor_node_id"], "node-1")
        self.assertEqual(pending[0]["source_run_id"], run.run_id)
        self.assertEqual(pending[0]["source_run_kind"], "subagent")
        self.assertEqual(pending[0]["metadata"]["origin"], "task_notification")
        self.assertEqual(pending[0]["metadata"]["source_status"], "completed")
        self.assertEqual(pending[0]["metadata"]["delegated_task"], "inspect")
        self.assertEqual(pending[0]["metadata"]["task_id"], "task_abc123")
        self.assertEqual(pending[0]["metadata"]["original_slash_input"], "/fork inspect")
        self.assertIn("subagent answer", pending[0]["content"])
        wrapped = messages_route.build_task_notification_content(pending[0])
        self.assertIn('"delegated_task": "inspect"', wrapped)
        self.assertIn('"task_id": "task_abc123"', wrapped)
        self.assertIn('"/fork inspect"', wrapped)
        request = messages_route.build_synthetic_message_request(pending[0])
        self.assertEqual(request.synthetic_metadata["task_id"], "task_abc123")

    async def test_failed_subagent_enqueues_failed_task_notification(self):
        run_manager = RunManager()
        executor = self._executor(run_manager, self.FakeProvider(fail=True))
        run = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="node-1",
            summary="implementer: inspect",
            metadata={"agent_name": "implementer"},
        )

        await executor._produce(
            run_id=run.run_id,
            conversation_id="conversation-1",
            agent_name="implementer",
            input_data="inspect",
            parent_node_id="node-1",
            parent_run_id=None,
            provider_id=None,
            model_id=None,
            permission_mode=None,
            workspace=None,
        )

        pending = run_manager.synthetic_inputs.list_pending("conversation-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["metadata"]["source_status"], "failed")
        self.assertIn("provider failed", pending[0]["content"])

    async def test_workflow_child_subagent_does_not_enqueue_notification(self):
        run_manager = RunManager()
        executor = self._executor(run_manager, self.FakeProvider())
        run = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="node-1",
            parent_run_id="workflow-1",
            summary="workflow-worker: inspect",
            metadata={"agent_name": "workflow-worker"},
        )

        await executor._produce(
            run_id=run.run_id,
            conversation_id="conversation-1",
            agent_name="implementer",
            input_data="inspect",
            parent_node_id="node-1",
            parent_run_id="workflow-1",
            provider_id=None,
            model_id=None,
            permission_mode=None,
            workspace=None,
        )

        self.assertEqual(run_manager.synthetic_inputs.list_pending("conversation-1"), [])

    async def test_parented_wait_subagent_publishes_mailbox_without_synthetic_notification(self):
        run_manager = RunManager()
        mailbox = AgentMailbox()
        executor = self._executor(run_manager, self.FakeProvider(), mailbox=mailbox)
        run = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="node-1",
            parent_run_id="chat-1",
            summary="implementer: inspect",
            metadata={"agent_name": "implementer", "delivery_policy": "wait"},
        )

        await executor._produce(
            run_id=run.run_id,
            conversation_id="conversation-1",
            agent_name="implementer",
            input_data="inspect",
            parent_node_id="node-1",
            parent_run_id="chat-1",
            provider_id=None,
            model_id=None,
            permission_mode=None,
            workspace=None,
        )

        messages = await mailbox.wait_for_run(
            conversation_id="conversation-1",
            run_id=run.run_id,
            timeout_seconds=0.01,
        )
        self.assertEqual(run_manager.synthetic_inputs.list_pending("conversation-1"), [])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "subagent answer")

    async def test_parented_auto_subagent_keeps_mailbox_and_synthetic_notification(self):
        run_manager = RunManager()
        mailbox = AgentMailbox()
        executor = self._executor(run_manager, self.FakeProvider(), mailbox=mailbox)
        parent = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.CHAT,
            anchor_node_id="node-1",
            summary="chat",
        )
        run = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="node-1",
            parent_run_id=parent.run_id,
            summary="implementer: inspect",
            metadata={"agent_name": "implementer", "delivery_policy": "auto"},
        )

        await executor._produce(
            run_id=run.run_id,
            conversation_id="conversation-1",
            agent_name="implementer",
            input_data="inspect",
            parent_node_id="node-1",
            parent_run_id=parent.run_id,
            provider_id=None,
            model_id=None,
            permission_mode=None,
            workspace=None,
        )

        messages = await mailbox.wait_for_run(
            conversation_id="conversation-1",
            run_id=run.run_id,
            timeout_seconds=0.01,
        )
        pending = run_manager.synthetic_inputs.list_pending("conversation-1")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "subagent answer")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["source_run_id"], run.run_id)
        self.assertEqual(pending[0]["metadata"]["mailbox_message_id"], messages[0]["message_id"])

    async def test_workflow_completion_enqueues_task_notification(self):
        class FakeRunner:
            async def run(self, **kwargs):
                return "workflow answer"

        run_manager = RunManager()
        manager = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=self._executor(run_manager, self.FakeProvider()),
            runner=FakeRunner(),
        )
        run = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.WORKFLOW,
            anchor_node_id="node-1",
            summary="Dynamic workflow",
            metadata={
                "delegated_task": "return 1",
                "slash_command": {"original_input": "/workflow return 1"},
            },
        )

        await manager._produce(
            run_id=run.run_id,
            conversation_id="conversation-1",
            script="return 1",
            args={},
            parent_node_id="node-1",
            parent_run_id=None,
            budget={"max_seconds": 10, "max_parallel": 1},
        )

        pending = run_manager.synthetic_inputs.list_pending("conversation-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["source_run_kind"], "workflow")
        self.assertEqual(pending[0]["metadata"]["source_status"], "completed")
        self.assertEqual(pending[0]["metadata"]["delegated_task"], "return 1")
        self.assertEqual(pending[0]["metadata"]["original_slash_input"], "/workflow return 1")
        self.assertIn("workflow answer", pending[0]["content"])

    async def test_subagent_stop_interrupts_pending_tool_approval_wait(self):
        approval_requested = asyncio.Event()
        tool_wait_cancelled = asyncio.Event()

        class ToolCallingProvider:
            async def generate_response_stream(self, **kwargs):
                yield {
                    "status": StreamStatus.COMPLETE,
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "run_command", "arguments": "{}"},
                        }
                    ],
                }

        class WaitingToolChatManager(self.FakeChatManager):
            def __init__(self, provider):
                super().__init__(provider)
                self.tool_manager = type(
                    "FakeToolManager",
                    (),
                    {
                        "get_openai_tools": lambda _self: [
                            {"type": "function", "function": {"name": "run_command"}}
                        ]
                    },
                )()

            def _merge_tool_call_lists(self, existing, incoming):
                return list(existing) + list(incoming)

            async def _execute_tool_calls(self, tool_calls, **kwargs):
                await kwargs["emit_event"]({
                    "event_type": "tool_approval_request",
                    "approval": {"id": "approval-1", "status": "pending"},
                })
                approval_requested.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    tool_wait_cancelled.set()
                    raise

            def _apply_round_tool_result_budget(self, tool_messages):
                return tool_messages

            def _tool_event_stream_chunk(self, event, node_id, conversation_id):
                payload = dict(event)
                payload.update({
                    "status": "content",
                    "node_id": node_id,
                    "conversation_id": conversation_id,
                })
                return payload

        run_manager = RunManager()
        registry = CapabilityRegistry()
        registry.add_agents([
            AgentDefinition(
                name="implementer",
                system_prompt="Implementer body",
                provider_id="fake",
                model_id="model",
                tools=["run_command"],
            )
        ])
        executor = SubagentExecutor(
            chat_manager=WaitingToolChatManager(ToolCallingProvider()),
            run_manager=run_manager,
            capability_registry=registry,
        )
        run = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="slash-node-1",
            summary="implementer: inspect",
            metadata={"agent_name": "implementer"},
        )
        task = asyncio.create_task(executor._produce(
            run_id=run.run_id,
            conversation_id="conversation-1",
            agent_name="implementer",
            input_data="inspect",
            parent_node_id="slash-node-1",
            parent_run_id=None,
            provider_id=None,
            model_id=None,
            permission_mode="ask_always",
            workspace=None,
        ))
        try:
            await asyncio.wait_for(approval_requested.wait(), timeout=1)
            await executor.stop(run.run_id)
            await asyncio.wait_for(task, timeout=0.5)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self.assertTrue(tool_wait_cancelled.is_set())
        self.assertEqual(run_manager.get_run(run.run_id)["status"], "cancelled")

    async def test_subagent_marks_run_waiting_during_tool_approval(self):
        approval_requested = asyncio.Event()
        release_tools = asyncio.Event()
        statuses: list[str] = []

        class ToolCallingProvider:
            async def generate_response_stream(self, **kwargs):
                yield {
                    "status": StreamStatus.COMPLETE,
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "run_command", "arguments": "{}"},
                        }
                    ],
                }

        class WaitingToolChatManager(self.FakeChatManager):
            def __init__(self, provider, run_manager, run_id_getter):
                super().__init__(provider)
                self.run_manager = run_manager
                self.run_id_getter = run_id_getter
                self.tool_manager = type(
                    "FakeToolManager",
                    (),
                    {
                        "get_openai_tools": lambda _self: [
                            {"type": "function", "function": {"name": "run_command"}}
                        ]
                    },
                )()

            def _merge_tool_call_lists(self, existing, incoming):
                return list(existing) + list(incoming)

            async def _execute_tool_calls(self, tool_calls, **kwargs):
                await kwargs["emit_event"]({
                    "event_type": "tool_approval_request",
                    "approval": {"id": "approval-1", "status": "pending"},
                })
                statuses.append(self.run_manager.get_run(self.run_id_getter())["status"])
                approval_requested.set()
                await release_tools.wait()
                await kwargs["emit_event"]({
                    "event_type": "tool_approval_result",
                    "approval": {"id": "approval-1", "status": "denied"},
                })
                statuses.append(self.run_manager.get_run(self.run_id_getter())["status"])
                return [{
                    "role": "tool",
                    "name": "run_command",
                    "tool_call_id": "call-1",
                    "content": "{}",
                }]

            def _apply_round_tool_result_budget(self, tool_messages):
                return tool_messages

            def _tool_event_stream_chunk(self, event, node_id, conversation_id):
                payload = dict(event)
                payload.update({
                    "status": "content",
                    "node_id": node_id,
                    "conversation_id": conversation_id,
                })
                return payload

        run_manager = RunManager()
        run_id_holder: dict[str, str] = {}
        registry = CapabilityRegistry()
        registry.add_agents([
            AgentDefinition(
                name="implementer",
                system_prompt="Implementer body",
                provider_id="fake",
                model_id="model",
                tools=["run_command"],
                max_tool_rounds=1,
            )
        ])
        executor = SubagentExecutor(
            chat_manager=WaitingToolChatManager(
                ToolCallingProvider(),
                run_manager,
                lambda: run_id_holder["run_id"],
            ),
            run_manager=run_manager,
            capability_registry=registry,
        )
        run = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="slash-node-1",
            summary="implementer: inspect",
            metadata={"agent_name": "implementer"},
        )
        run_id_holder["run_id"] = run.run_id
        task = asyncio.create_task(executor._produce(
            run_id=run.run_id,
            conversation_id="conversation-1",
            agent_name="implementer",
            input_data="inspect",
            parent_node_id="slash-node-1",
            parent_run_id=None,
            provider_id=None,
            model_id=None,
            permission_mode="ask_always",
            workspace=None,
        ))
        await asyncio.wait_for(approval_requested.wait(), timeout=1)
        release_tools.set()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(statuses[:2], ["waiting_approval", "running"])

    async def test_btw_and_status_do_not_enqueue_task_notifications(self):
        class FakeChatManager:
            async def send_message_stream(self, **kwargs):
                yield {
                    "status": "complete",
                    "content": "side answer",
                    "node_id": None,
                    "target_node_id": None,
                    "run_id": kwargs["run_id"],
                }

        run_manager = RunManager()
        await SlashRuntimeDispatchTests()._collect_events(detached_stream_event_generator(
            "conversation-1",
            SendMessageRequest(content="/btw summarize context", node_id="node-1"),
            FakeChatManager(),
            run_manager,
            None,
            None,
        ))
        await SlashRuntimeDispatchTests()._collect_events(detached_stream_event_generator(
            "conversation-1",
            SendMessageRequest(content="/status", node_id="node-1"),
            FakeChatManager(),
            run_manager,
            None,
            None,
        ))

        self.assertEqual(run_manager.synthetic_inputs.list_pending("conversation-1"), [])


class SyntheticInputRouteTests(unittest.TestCase):
    class FakeChatManager:
        def __init__(self):
            self.contents: list[str] = []
            self.kwargs: list[dict[str, Any]] = []

        async def send_message_stream(self, **kwargs):
            self.kwargs.append(kwargs)
            self.contents.append(kwargs["content"])
            yield {
                "status": "complete",
                "content": "synthetic answer",
                "node_id": "assistant-node-1",
                "target_node_id": "assistant-node-1",
                "run_id": kwargs["run_id"],
            }

    def _client(self, run_manager: RunManager, chat_manager: Any | None = None):
        app = FastAPI()
        app.include_router(messages_route.router)
        app.dependency_overrides[get_run_manager] = lambda: run_manager
        app.dependency_overrides[get_chat_manager] = lambda: chat_manager or self.FakeChatManager()
        app.dependency_overrides[get_subagent_executor] = lambda: object()
        app.dependency_overrides[get_workflow_manager] = lambda: object()
        return TestClient(app)

    def test_pending_and_consume_synthetic_inputs(self):
        run_manager = RunManager()
        item = run_manager.synthetic_inputs.enqueue(
            kind="task_notification",
            conversation_id="conversation-1",
            anchor_node_id="node-1",
            source_run_id="run-1",
            source_run_kind="subagent",
            status="pending",
            summary="subagent completed",
            content="result text",
            metadata={"origin": "task_notification", "source_status": "completed"},
        )
        client = self._client(run_manager)

        pending_response = client.get("/conversations/conversation-1/synthetic-inputs/pending")
        consume_response = client.post(f"/conversations/conversation-1/synthetic-inputs/{item.input_id}/consume")

        self.assertEqual(pending_response.status_code, 200)
        self.assertEqual(len(pending_response.json()), 1)
        self.assertEqual(pending_response.json()[0]["input_id"], item.input_id)
        self.assertEqual(consume_response.status_code, 200)
        self.assertEqual(consume_response.json()["status"], "consumed")
        self.assertEqual(run_manager.synthetic_inputs.list_pending("conversation-1"), [])

    def test_active_streams_include_subagent_runs(self):
        run_manager = RunManager()
        run = asyncio.run(run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="node-1",
            summary="subagent inspect",
        ))

        active = asyncio.run(messages_route.get_active_streams("conversation-1", run_manager))

        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["run_id"], run.run_id)
        self.assertEqual(active[0]["kind"], RunKind.SUBAGENT.value)

    def test_start_synthetic_input_stream_wraps_notification_and_marks_origin(self):
        run_manager = RunManager()
        chat_manager = self.FakeChatManager()
        item = run_manager.synthetic_inputs.enqueue(
            kind="task_notification",
            conversation_id="conversation-1",
            anchor_node_id="node-1",
            source_run_id="run-1",
            source_run_kind="workflow",
            status="pending",
            summary="workflow completed",
            content="workflow result",
            metadata={"origin": "task_notification", "source_status": "completed"},
        )
        client = self._client(run_manager, chat_manager)

        response = client.post(f"/conversations/conversation-1/synthetic-inputs/{item.input_id}/stream")

        self.assertEqual(response.status_code, 200)
        self.assertIn("[DONE]", response.text)
        self.assertEqual(len(chat_manager.contents), 1)
        self.assertIn("<task-notification>", chat_manager.contents[0])
        self.assertIn("workflow result", chat_manager.contents[0])
        self.assertEqual(chat_manager.kwargs[0]["message_subtype"], "task_notification")
        self.assertEqual(run_manager.synthetic_inputs.list_pending("conversation-1"), [])
        chat_runs = [
            run for run in run_manager.list_runs("conversation-1")
            if run["kind"] == "chat"
        ]
        self.assertEqual(len(chat_runs), 1)
        self.assertEqual(chat_runs[0]["metadata"]["origin"], "task_notification")
        self.assertEqual(chat_runs[0]["metadata"]["synthetic_input"]["input_id"], item.input_id)
        self.assertEqual(chat_runs[0]["metadata"]["synthetic_input"]["kind"], "task_notification")

    def test_synthetic_input_stream_keeps_pending_when_followup_cannot_bind_node(self):
        class FailingBeforeNodeChatManager(self.FakeChatManager):
            async def send_message_stream(self, **kwargs):
                self.kwargs.append(kwargs)
                self.contents.append(kwargs["content"])
                yield {
                    "status": "error",
                    "content": "",
                    "node_id": None,
                    "target_node_id": None,
                    "run_id": kwargs["run_id"],
                    "error": "model unavailable",
                }

        run_manager = RunManager()
        chat_manager = FailingBeforeNodeChatManager()
        item = run_manager.synthetic_inputs.enqueue(
            kind="task_notification",
            conversation_id="conversation-1",
            anchor_node_id="node-1",
            source_run_id="run-1",
            source_run_kind="workflow",
            status="pending",
            summary="workflow completed",
            content="workflow result",
            metadata={"origin": "task_notification", "source_status": "completed"},
        )
        client = self._client(run_manager, chat_manager)

        response = client.post(f"/conversations/conversation-1/synthetic-inputs/{item.input_id}/stream")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(run_manager.synthetic_inputs.list_pending("conversation-1")), 1)
        self.assertEqual(run_manager.synthetic_inputs.list_pending("conversation-1")[0]["input_id"], item.input_id)

    def test_synthetic_followup_scheduler_starts_followup_without_frontend_trigger(self):
        from backend.api.routes.messages import SyntheticFollowupScheduler

        run_manager = RunManager()
        chat_manager = self.FakeChatManager()
        scheduler = SyntheticFollowupScheduler(
            chat_manager=chat_manager,
            run_manager=run_manager,
        )
        scheduler.install()
        run_manager.synthetic_inputs.enqueue(
            kind="task_notification",
            conversation_id="conversation-1",
            anchor_node_id="node-1",
            source_run_id="run-1",
            source_run_kind="subagent",
            status="pending",
            summary="subagent completed",
            content="subagent result",
            metadata={"origin": "task_notification", "source_status": "completed"},
        )

        async def run_scheduler():
            await scheduler.drain("conversation-1")
            await asyncio.sleep(0.05)

        asyncio.run(run_scheduler())

        self.assertEqual(run_manager.synthetic_inputs.list_pending("conversation-1"), [])
        chat_runs = [
            run for run in run_manager.list_runs("conversation-1")
            if run["kind"] == "chat"
        ]
        self.assertEqual(len(chat_runs), 1)
        self.assertEqual(chat_runs[0]["metadata"]["origin"], "task_notification")
        self.assertIn("subagent result", chat_manager.contents[0])

    def test_synthetic_followup_scheduler_skips_integrated_mailbox_notification(self):
        from backend.api.routes.messages import SyntheticFollowupScheduler

        async def scenario():
            run_manager = RunManager()
            mailbox = AgentMailbox()
            run_manager.agent_mailbox = mailbox
            chat_manager = self.FakeChatManager()
            scheduler = SyntheticFollowupScheduler(
                chat_manager=chat_manager,
                run_manager=run_manager,
            )
            mailbox_item = await mailbox.publish(
                conversation_id="conversation-1",
                source_run_id="run-1",
                source_run_kind="subagent",
                message_type="result",
                content="subagent result",
                delivery_policy="both",
            )
            await mailbox.mark_integrated("conversation-1", mailbox_item.message_id)
            run_manager.synthetic_inputs.enqueue(
                kind="task_notification",
                conversation_id="conversation-1",
                anchor_node_id="node-1",
                source_run_id="run-1",
                source_run_kind="subagent",
                status="pending",
                summary="subagent completed",
                content="subagent result",
                metadata={
                    "origin": "task_notification",
                    "source_status": "completed",
                    "mailbox_message_id": mailbox_item.message_id,
                },
            )

            await scheduler.drain("conversation-1")

            self.assertEqual(run_manager.synthetic_inputs.list_pending("conversation-1"), [])
            self.assertEqual(chat_manager.contents, [])
            self.assertEqual(
                [
                    run for run in run_manager.list_runs("conversation-1")
                    if run["kind"] == "chat"
                ],
                [],
            )

        asyncio.run(scenario())


class DetachedSlashStopRouteTests(unittest.TestCase):
    class FakeChatManager:
        storage = type("FakeStorage", (), {"index": {"conversation-1": True}})()

        def __init__(self):
            self.stopped_nodes: list[str] = []

        async def stop_stream(self, node_id: str):
            self.stopped_nodes.append(node_id)
            return False

    class FakeSubagentExecutor:
        def __init__(self):
            self.stopped_run_id = None
            self.stopped_run_ids: list[str] = []

        async def stop(self, run_id: str):
            self.stopped_run_id = run_id
            self.stopped_run_ids.append(run_id)
            return True

    class FakeWorkflowManager:
        def __init__(self):
            self.stopped_run_id = None
            self.stopped_run_ids: list[str] = []

        async def stop(self, run_id: str):
            self.stopped_run_id = run_id
            self.stopped_run_ids.append(run_id)
            return True

    def _client(self, run_manager, chat_manager, subagent_executor, workflow_manager):
        app = FastAPI()
        app.include_router(messages_route.router)
        app.dependency_overrides[get_run_manager] = lambda: run_manager
        app.dependency_overrides[get_chat_manager] = lambda: chat_manager
        app.dependency_overrides[get_subagent_executor] = lambda: subagent_executor
        app.dependency_overrides[get_workflow_manager] = lambda: workflow_manager
        return TestClient(app)

    def _runs_client(self, run_manager, chat_manager, subagent_executor, workflow_manager):
        app = FastAPI()
        app.include_router(runs_route.router)
        app.dependency_overrides[get_run_manager] = lambda: run_manager
        app.dependency_overrides[get_chat_manager] = lambda: chat_manager
        app.dependency_overrides[get_subagent_executor] = lambda: subagent_executor
        app.dependency_overrides[get_workflow_manager] = lambda: workflow_manager
        return TestClient(app)

    def test_stop_stream_message_stops_detached_subagent_by_anchor_node(self):
        run_manager = RunManager()
        run = asyncio.run(run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="slash-node-1",
            summary="implementer: inspect",
        ))
        subagent_executor = self.FakeSubagentExecutor()
        client = self._client(
            run_manager,
            self.FakeChatManager(),
            subagent_executor,
            self.FakeWorkflowManager(),
        )

        response = client.post("/conversations/conversation-1/messages/slash-node-1/stream/stop")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(subagent_executor.stopped_run_id, run.run_id)
        self.assertEqual(run_manager.get_run(run.run_id)["status"], "stopping")

    def test_stop_stream_message_stops_detached_workflow_by_anchor_node(self):
        run_manager = RunManager()
        run = asyncio.run(run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.WORKFLOW,
            anchor_node_id="slash-node-1",
            summary="Dynamic workflow",
        ))
        workflow_manager = self.FakeWorkflowManager()
        client = self._client(
            run_manager,
            self.FakeChatManager(),
            self.FakeSubagentExecutor(),
            workflow_manager,
        )

        response = client.post("/conversations/conversation-1/messages/slash-node-1/stream/stop")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(workflow_manager.stopped_run_id, run.run_id)
        self.assertEqual(run_manager.get_run(run.run_id)["status"], "stopping")

    def test_stop_stream_message_stops_workflow_step_by_anchor_node(self):
        run_manager = RunManager()
        run = asyncio.run(run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.WORKFLOW_STEP,
            anchor_node_id="slash-node-1",
            summary="step: inspect",
        ))
        subagent_executor = self.FakeSubagentExecutor()
        client = self._client(
            run_manager,
            self.FakeChatManager(),
            subagent_executor,
            self.FakeWorkflowManager(),
        )

        response = client.post("/conversations/conversation-1/messages/slash-node-1/stream/stop")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(subagent_executor.stopped_run_id, run.run_id)
        self.assertEqual(run_manager.get_run(run.run_id)["status"], "stopping")

    def test_stop_stream_message_stops_child_subagent_by_parent_run_id(self):
        run_manager = RunManager()
        parent = asyncio.run(run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.CHAT,
            target_node_id="assistant-node-1",
            summary="parent chat",
        ))
        child = asyncio.run(run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="assistant-node-1",
            parent_run_id=parent.run_id,
            summary="child agent",
        ))
        subagent_executor = self.FakeSubagentExecutor()
        client = self._client(
            run_manager,
            self.FakeChatManager(),
            subagent_executor,
            self.FakeWorkflowManager(),
        )

        response = client.post("/conversations/conversation-1/messages/assistant-node-1/stream/stop")

        self.assertEqual(response.status_code, 200)
        self.assertIn(child.run_id, subagent_executor.stopped_run_ids)
        self.assertEqual(run_manager.get_run(parent.run_id)["status"], "stopping")
        self.assertEqual(run_manager.get_run(child.run_id)["status"], "stopping")

    def test_stop_run_stops_child_subagent_by_parent_run_id(self):
        run_manager = RunManager()
        parent = asyncio.run(run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.CHAT,
            target_node_id="assistant-node-1",
            summary="parent chat",
        ))
        child = asyncio.run(run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="assistant-node-1",
            parent_run_id=parent.run_id,
            summary="child agent",
        ))
        chat_manager = self.FakeChatManager()
        subagent_executor = self.FakeSubagentExecutor()
        client = self._runs_client(
            run_manager,
            chat_manager,
            subagent_executor,
            self.FakeWorkflowManager(),
        )

        response = client.post(f"/runs/{parent.run_id}/stop")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(chat_manager.stopped_nodes, ["assistant-node-1"])
        self.assertIn(child.run_id, subagent_executor.stopped_run_ids)
        self.assertEqual(run_manager.get_run(parent.run_id)["status"], "stopping")
        self.assertEqual(run_manager.get_run(child.run_id)["status"], "stopping")


class AgentRolePromptTests(unittest.TestCase):
    def test_project_agents_are_role_specific(self):
        agents = {
            agent.name: agent
            for agent in load_agent_roots(
                [Path(".chattree/agents")],
                source=CapabilitySource.PROJECT,
            )
        }
        for name in ["explorer", "planner", "implementer", "reviewer", "verifier", "workflow-worker"]:
            with self.subTest(name=name):
                self.assertIn(name, agents)
                self.assertIn("ChatTree", agents[name].system_prompt)
                self.assertGreaterEqual(len(agents[name].system_prompt), 600)

    def test_workflow_worker_returns_data_not_user_facing_message(self):
        agents = {
            agent.name: agent
            for agent in load_agent_roots(
                [Path(".chattree/agents")],
                source=CapabilitySource.PROJECT,
            )
        }
        prompt = agents["workflow-worker"].system_prompt.lower()
        self.assertIn("return value", prompt)
        self.assertIn("workflow", prompt)

    def test_general_agent_is_compatibility_alias(self):
        agents = {
            agent.name: agent
            for agent in load_agent_roots(
                [Path(".chattree/agents")],
                source=CapabilitySource.PROJECT,
            )
        }
        prompt = agents["general"].system_prompt
        self.assertIn("role-specific explorer, planner, implementer, reviewer, verifier, or workflow-worker", prompt)
        self.assertEqual(agents["general"].permission_mode, "read_only")
        self.assertEqual(agents["general"].max_turns, 1)


class DummyChatManager:
    tool_manager = None


class DummyRunManager:
    pass


class RuntimePolicyTests(unittest.TestCase):
    def test_subagent_build_messages_include_fork_policy(self):
        registry = CapabilityRegistry()
        registry.add_agents([
            AgentDefinition(name="workflow-worker", system_prompt="Worker body")
        ])
        executor = SubagentExecutor(
            chat_manager=DummyChatManager(),
            run_manager=DummyRunManager(),
            capability_registry=registry,
        )
        messages = executor._build_messages("workflow-worker", "task", None)
        system_text = "\n\n".join(message["content"] for message in messages if message["role"] == "system")
        self.assertIn("ChatTree", messages[0]["content"])
        self.assertIn("Worker body", messages[0]["content"])
        self.assertIn("Runtime mode: workflow worker", messages[1]["content"])
        self.assertIn("workflow", system_text.lower())
        self.assertIn("directive", system_text.lower())
        self.assertNotIn("# ChatTree Core Prompt", system_text)

    def test_subagent_build_messages_include_workflow_policy_for_metadata(self):
        registry = CapabilityRegistry()
        registry.add_agents([
            AgentDefinition(
                name="custom-worker",
                system_prompt="Custom worker body",
                metadata={"runtime": "workflow"},
            )
        ])
        executor = SubagentExecutor(
            chat_manager=DummyChatManager(),
            run_manager=DummyRunManager(),
            capability_registry=registry,
        )
        messages = executor._build_messages("custom-worker", "task", None)
        system_text = "\n\n".join(message["content"] for message in messages if message["role"] == "system")
        self.assertIn("Custom worker body", messages[0]["content"])
        self.assertIn("Runtime mode: workflow worker", messages[1]["content"])
        self.assertNotIn("pipeline(", system_text)
        self.assertIn("returned verbatim", system_text)
        self.assertIn("workflow", system_text.lower())
        self.assertNotIn("# ChatTree Core Prompt", system_text)

    def test_subagent_build_messages_include_subagent_runtime_context(self):
        registry = CapabilityRegistry()
        registry.add_agents([
            AgentDefinition(name="reviewer", system_prompt="Reviewer worker body")
        ])
        executor = SubagentExecutor(
            chat_manager=DummyChatManager(),
            run_manager=DummyRunManager(),
            capability_registry=registry,
        )

        messages = executor._build_messages("reviewer", "task", None)

        system_text = "\n\n".join(message["content"] for message in messages if message["role"] == "system")
        self.assertIn("Reviewer worker body", messages[0]["content"])
        self.assertIn("Runtime mode: subagent worker", messages[1]["content"])
        self.assertIn("do not write report/output files", system_text.lower())
        self.assertNotIn("# ChatTree Core Prompt", system_text)

    def test_fork_context_mode_includes_parent_conversation_context(self):
        registry = CapabilityRegistry()
        registry.add_agents([
            AgentDefinition(name="reviewer", system_prompt="Reviewer worker body")
        ])

        class ParentConversation:
            def get_node_chain(self, node_id):
                return [
                    {
                        "id": "root",
                        "user_message": None,
                        "assistant_message": None,
                    },
                    {
                        "id": "node-1",
                        "user_message": {"role": "user", "content": "父对话问题"},
                        "assistant_message": {"role": "assistant", "content": "父对话回答"},
                    },
                ]

        executor = SubagentExecutor(
            chat_manager=DummyChatManager(),
            run_manager=DummyRunManager(),
            capability_registry=registry,
        )

        messages = executor._build_messages(
            "reviewer",
            "检查上述结论",
            "node-1",
            conversation=ParentConversation(),
            context_mode="fork",
        )

        system_text = "\n\n".join(message["content"] for message in messages if message["role"] == "system")
        self.assertIn("Parent conversation context", system_text)
        self.assertIn("父对话问题", system_text)
        self.assertIn("父对话回答", system_text)
        self.assertEqual(messages[-1]["content"], "检查上述结论")

    def test_agent_runtime_passes_fork_context_mode_to_executor(self):
        async def run_case():
            registry = CapabilityRegistry()
            registry.add_agents([
                AgentDefinition(name="reviewer", system_prompt="Reviewer worker body")
            ])

            class FakeSubagentExecutor:
                def __init__(self):
                    self.kwargs = None

                async def start(self, **kwargs):
                    self.kwargs = kwargs
                    return {"run_id": "agent-run-1", "kind": "subagent", "status": "running", "metadata": {}}

            class FakeRunManager:
                def __init__(self):
                    self.metadata = None

                def add_finish_listener(self, listener):
                    self.listener = listener

                async def update_metadata(self, run_id, metadata):
                    self.metadata = {"run_id": run_id, "metadata": metadata}

            run_manager = FakeRunManager()
            executor = FakeSubagentExecutor()
            runtime = AgentRuntime(
                run_manager=run_manager,
                mailbox=AgentMailbox(),
                subagent_executor=executor,
                capability_registry=registry,
            )

            await runtime.spawn_agent(
                source=AgentSource(
                    conversation_id="conversation-1",
                    run_id="chat-run-1",
                    run_kind=RunKind.CHAT.value,
                    anchor_node_id="node-1",
                ),
                agent_name="reviewer",
                task="检查上述结论",
                context_mode="fork",
            )

            self.assertEqual(executor.kwargs["context_mode"], "fork")

        asyncio.run(run_case())

    def test_agent_runtime_notifies_parent_run_when_subagent_starts(self):
        async def run_case():
            with tempfile.TemporaryDirectory() as tmpdir:
                registry = CapabilityRegistry()
                registry.add_agents([
                    AgentDefinition(name="reviewer", system_prompt="Reviewer worker body")
                ])
                run_manager = RunManager(RunJournal(Path(tmpdir)))
                parent = await run_manager.create_run(
                    conversation_id="conversation-1",
                    kind=RunKind.CHAT,
                    anchor_node_id="node-1",
                    target_node_id="assistant-node-1",
                    summary="parent chat",
                )

                class FakeSubagentExecutor:
                    async def start(self, **kwargs):
                        child = await run_manager.create_run(
                            conversation_id=kwargs["conversation_id"],
                            kind=RunKind.SUBAGENT,
                            anchor_node_id=kwargs.get("parent_node_id"),
                            parent_run_id=kwargs.get("parent_run_id"),
                            summary=kwargs.get("delegated_task") or "",
                            metadata={"agent_name": kwargs.get("agent_name")},
                        )
                        return child.to_dict()

                runtime = AgentRuntime(
                    run_manager=run_manager,
                    mailbox=AgentMailbox(),
                    subagent_executor=FakeSubagentExecutor(),
                    capability_registry=registry,
                )

                result = await runtime.spawn_agent(
                    source=AgentSource(
                        conversation_id="conversation-1",
                        run_id=parent.run_id,
                        run_kind=RunKind.CHAT.value,
                        anchor_node_id="node-1",
                    ),
                    agent_name="reviewer",
                    task="检查实现",
                    context_mode="fresh",
                )

                payloads = [
                    event["payload"]
                    for event in run_manager.journal.read_events("conversation-1", parent.run_id)
                ]
                child_events = [
                    payload for payload in payloads
                    if payload.get("event_type") == "child_run_started"
                ]
                self.assertEqual(len(child_events), 1)
                self.assertEqual(child_events[0]["child_run_id"], result["run_id"])
                self.assertEqual(child_events[0]["child_kind"], RunKind.SUBAGENT.value)
                self.assertIsNone(child_events[0].get("parent_run_id"))
                self.assertEqual(child_events[0]["payload"]["parent_run_id"], parent.run_id)

        asyncio.run(run_case())

    def test_agent_runtime_auto_creates_and_completes_subagent_task(self):
        async def run_case():
            registry = CapabilityRegistry()
            registry.add_agents([
                AgentDefinition(name="reviewer", system_prompt="Reviewer worker body")
            ])
            run_manager = RunManager()
            task_ledger = TaskLedger()
            task_ledger.install_run_finish_listener(run_manager)

            class FakeSubagentExecutor:
                async def start(self, **kwargs):
                    return (await run_manager.create_run(
                        conversation_id=kwargs["conversation_id"],
                        kind=RunKind.SUBAGENT,
                        anchor_node_id=kwargs.get("parent_node_id"),
                        parent_run_id=kwargs.get("parent_run_id"),
                        summary=kwargs.get("delegated_task") or "",
                        metadata={"agent_name": kwargs.get("agent_name")},
                    )).to_dict()

            runtime = AgentRuntime(
                run_manager=run_manager,
                mailbox=AgentMailbox(),
                subagent_executor=FakeSubagentExecutor(),
                capability_registry=registry,
                task_ledger=task_ledger,
            )

            result = await runtime.spawn_agent(
                source=AgentSource(
                    conversation_id="conversation-1",
                    run_id="chat-run-1",
                    run_kind=RunKind.CHAT.value,
                    anchor_node_id="node-1",
                ),
                agent_name="reviewer",
                task="检查实现",
            )

            self.assertTrue(str(result.get("task_id") or "").startswith("task_"))
            task = await task_ledger.get_task("conversation-1", result["task_id"])
            self.assertEqual(task.status, TaskStatus.IN_PROGRESS)
            self.assertEqual(task.owner_run_id, result["run_id"])

            await run_manager.finish_run(result["run_id"], RunStatus.COMPLETED)
            await asyncio.sleep(0)
            completed = await task_ledger.get_task("conversation-1", result["task_id"])
            self.assertEqual(completed.status, TaskStatus.COMPLETED)
            self.assertEqual(completed.evidence_run_id, result["run_id"])

        asyncio.run(run_case())

    def test_agent_runtime_binds_existing_workflow_task_and_blocks_on_failure(self):
        async def run_case():
            registry = CapabilityRegistry()
            run_manager = RunManager()
            task_ledger = TaskLedger()
            task_ledger.install_run_finish_listener(run_manager)
            created = await task_ledger.create_task(
                conversation_id="conversation-1",
                title="运行 workflow",
                created_by_run_id="chat-run-1",
            )

            class FakeWorkflowManager:
                async def start(self, **kwargs):
                    return (await run_manager.create_run(
                        conversation_id=kwargs["conversation_id"],
                        kind=RunKind.WORKFLOW,
                        anchor_node_id=kwargs.get("parent_node_id"),
                        parent_run_id=kwargs.get("parent_run_id"),
                        summary="workflow",
                        metadata={"delegated_task": kwargs.get("delegated_task")},
                    )).to_dict()

            runtime = AgentRuntime(
                run_manager=run_manager,
                mailbox=AgentMailbox(),
                subagent_executor=object(),
                workflow_manager=FakeWorkflowManager(),
                capability_registry=registry,
                task_ledger=task_ledger,
            )

            result = await runtime.start_workflow(
                source=AgentSource(
                    conversation_id="conversation-1",
                    run_id="chat-run-1",
                    run_kind=RunKind.CHAT.value,
                    anchor_node_id="node-1",
                ),
                script="throw new Error('boom')",
                task_id=created.task_id,
            )

            self.assertEqual(result["task_id"], created.task_id)
            bound = await task_ledger.get_task("conversation-1", created.task_id)
            self.assertEqual(bound.status, TaskStatus.IN_PROGRESS)
            self.assertEqual(bound.owner_run_id, result["run_id"])

            await run_manager.finish_run(result["run_id"], RunStatus.FAILED, "boom")
            await asyncio.sleep(0)
            blocked = await task_ledger.get_task("conversation-1", created.task_id)
            self.assertEqual(blocked.status, TaskStatus.BLOCKED)
            self.assertIn("boom", blocked.evidence_summary)

        asyncio.run(run_case())

    def test_agent_runtime_rejects_missing_task_id_before_spawning(self):
        async def run_case():
            registry = CapabilityRegistry()
            registry.add_agents([
                AgentDefinition(name="reviewer", system_prompt="Reviewer worker body")
            ])
            run_manager = RunManager()
            task_ledger = TaskLedger()
            starts = 0

            class FakeSubagentExecutor:
                async def start(self, **kwargs):
                    nonlocal starts
                    starts += 1
                    return {"run_id": "agent-run-1", "kind": "subagent", "status": "running", "metadata": {}}

            runtime = AgentRuntime(
                run_manager=run_manager,
                mailbox=AgentMailbox(),
                subagent_executor=FakeSubagentExecutor(),
                capability_registry=registry,
                task_ledger=task_ledger,
            )

            with self.assertRaises(Exception):
                await runtime.spawn_agent(
                    source=AgentSource(
                        conversation_id="conversation-1",
                        run_id="chat-run-1",
                        run_kind=RunKind.CHAT.value,
                        anchor_node_id="node-1",
                    ),
                    agent_name="reviewer",
                    task="检查实现",
                    task_id="task_missing",
                )

            self.assertEqual(starts, 0)

        asyncio.run(run_case())

    def test_subagent_stop_cancels_registered_producer_task(self):
        async def run_case():
            registry = CapabilityRegistry()
            registry.add_agents([
                AgentDefinition(name="reviewer", system_prompt="Reviewer worker body")
            ])
            run_manager = RunManager()
            executor = SubagentExecutor(
                chat_manager=DummyChatManager(),
                run_manager=run_manager,
                capability_registry=registry,
            )
            run = await run_manager.create_run(
                conversation_id="conversation-1",
                kind=RunKind.SUBAGENT,
                anchor_node_id="node-1",
                summary="reviewer: inspect",
            )
            task = asyncio.create_task(asyncio.sleep(60))
            executor._tasks[run.run_id] = task

            stopped = await executor.stop(run.run_id)
            await asyncio.sleep(0)

            self.assertTrue(stopped)
            self.assertTrue(task.cancelled())
            self.assertEqual(run_manager.get_run(run.run_id)["status"], RunStatus.STOPPING.value)

        asyncio.run(run_case())

    def test_agent_runtime_close_interrupt_use_owner_stop(self):
        async def run_case():
            run_manager = RunManager()
            run = await run_manager.create_run(
                conversation_id="conversation-1",
                kind=RunKind.SUBAGENT,
                anchor_node_id="node-1",
                summary="reviewer: inspect",
            )

            class FakeSubagentExecutor:
                def __init__(self):
                    self.stopped: list[str] = []

                async def stop(self, run_id):
                    self.stopped.append(run_id)
                    await run_manager.request_stop(run_id)
                    return True

            executor = FakeSubagentExecutor()
            runtime = AgentRuntime(
                run_manager=run_manager,
                mailbox=AgentMailbox(),
                subagent_executor=executor,
                capability_registry=CapabilityRegistry(),
            )

            await runtime.close_agent(run_id=run.run_id)
            await runtime.interrupt_agent(run_id=run.run_id)

            self.assertEqual(executor.stopped, [run.run_id, run.run_id])

        asyncio.run(run_case())

    def test_agent_runtime_close_uses_subagent_owner_for_workflow_step(self):
        async def run_case():
            run_manager = RunManager()
            run = await run_manager.create_run(
                conversation_id="conversation-1",
                kind=RunKind.WORKFLOW_STEP,
                anchor_node_id="node-1",
                summary="workflow step",
            )

            class FakeSubagentExecutor:
                def __init__(self):
                    self.stopped: list[str] = []

                async def stop(self, run_id):
                    self.stopped.append(run_id)
                    await run_manager.request_stop(run_id)
                    return True

            executor = FakeSubagentExecutor()
            runtime = AgentRuntime(
                run_manager=run_manager,
                mailbox=AgentMailbox(),
                subagent_executor=executor,
                capability_registry=CapabilityRegistry(),
            )

            await runtime.close_agent(run_id=run.run_id)

            self.assertEqual(executor.stopped, [run.run_id])

        asyncio.run(run_case())

class WorkflowRuntimeBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unnamed_workflow_agent_defaults_to_workflow_worker(self):
        class FakeSubagentExecutor:
            def __init__(self):
                self.agent_name = None

            async def start(self, **kwargs):
                self.agent_name = kwargs["agent_name"]
                return {"run_id": "child-1"}

        class FakeRunManager:
            async def append_event(self, *args, **kwargs):
                return None

            async def subscribe(self, run_id, offset):
                yield {"type": "run_finished", "status": "completed"}

        subagent_executor = FakeSubagentExecutor()
        bridge = WorkflowRuntimeBridge(
            workflow_run_id="workflow-1",
            conversation_id="conversation-1",
            parent_node_id="node-1",
            run_manager=FakeRunManager(),
            subagent_executor=subagent_executor,
        )
        result = await bridge._agent({"input": "do the work"})
        self.assertEqual(subagent_executor.agent_name, "workflow-worker")
        self.assertEqual(result["status"], "completed")

    async def test_child_subagent_inherits_workflow_permission_mode(self):
        class FakeSubagentExecutor:
            def __init__(self):
                self.kwargs = None

            async def start(self, **kwargs):
                self.kwargs = kwargs
                return {"run_id": "child-1"}

        class FakeRunManager:
            async def append_event(self, *args, **kwargs):
                return None

            async def subscribe(self, run_id, offset):
                yield {"type": "run_finished", "status": "completed"}

        subagent_executor = FakeSubagentExecutor()
        bridge = WorkflowRuntimeBridge(
            workflow_run_id="workflow-1",
            conversation_id="conversation-1",
            parent_node_id="slash-node-1",
            run_manager=FakeRunManager(),
            subagent_executor=subagent_executor,
            permission_mode="modify_only",
        )

        await bridge._agent({"input": "do the work"})

        self.assertEqual(subagent_executor.kwargs["permission_mode"], "modify_only")
        self.assertEqual(subagent_executor.kwargs["parent_node_id"], "slash-node-1")

    async def test_cancelled_workflow_agent_call_stops_child_subagent(self):
        child_started = asyncio.Event()

        class FakeSubagentExecutor:
            def __init__(self):
                self.stopped_run_id = None

            async def start(self, **kwargs):
                child_started.set()
                return {"run_id": "child-1"}

            async def stop(self, run_id: str):
                self.stopped_run_id = run_id
                return True

        class FakeRunManager:
            async def append_event(self, *args, **kwargs):
                return None

            async def subscribe(self, run_id, offset):
                await asyncio.Event().wait()
                yield {"type": "run_finished", "status": "completed"}

        subagent_executor = FakeSubagentExecutor()
        bridge = WorkflowRuntimeBridge(
            workflow_run_id="workflow-1",
            conversation_id="conversation-1",
            parent_node_id="slash-node-1",
            run_manager=FakeRunManager(),
            subagent_executor=subagent_executor,
            permission_mode="ask_always",
        )
        task = asyncio.create_task(bridge._agent({"input": "do the work"}))
        await asyncio.wait_for(child_started.wait(), timeout=1)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        self.assertEqual(subagent_executor.stopped_run_id, "child-1")

    async def test_workflow_manager_stop_stops_active_child_subagents(self):
        run_manager = RunManager()
        workflow = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.WORKFLOW,
            anchor_node_id="node-1",
            summary="workflow",
        )
        child = await run_manager.create_run(
            conversation_id="conversation-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="node-1",
            parent_run_id=workflow.run_id,
            summary="child",
        )

        class FakeSubagentExecutor:
            def __init__(self):
                self.stopped: list[str] = []

            async def stop(self, run_id):
                self.stopped.append(run_id)
                await run_manager.request_stop(run_id)
                return True

        subagent_executor = FakeSubagentExecutor()
        manager = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=subagent_executor,
        )

        await manager.stop(workflow.run_id)

        self.assertEqual(subagent_executor.stopped, [child.run_id])
        self.assertEqual(run_manager.get_run(child.run_id)["status"], RunStatus.STOPPING.value)


class WorkflowRuntimeWorkerTests(unittest.TestCase):
    def test_pipeline_runtime_matches_workflow_prompt_contract(self):
        worker = Path("backend/workers/workflow_runtime.mjs").read_text(encoding="utf-8")
        workflow_prompt = load_prompt_template("workflow")
        self.assertIn("pipeline(items, stage1, stage2", workflow_prompt)
        self.assertIn("const pipeline = async (items, ...stages)", worker)
        self.assertIn("Promise.all(items.map(async (item, index)", worker)
        self.assertIn("stage(value, item, index)", worker)

    def test_runtime_supports_export_const_meta(self):
        class FakeBridge:
            async def handle_call(self, method, params):
                return {"method": method, "params": params}

        result = asyncio.run(WorkflowJsRunner().run(
            script="export const meta = { name: 'x', description: 'x' }; return meta.name;",
            args={},
            budget={"max_host_calls": 10},
            bridge=FakeBridge(),
        ))
        self.assertEqual(result, "x")

    def test_runtime_agent_supports_chattree_style_signature(self):
        class FakeBridge:
            async def handle_call(self, method, params):
                return {"method": method, "params": params}

        result = asyncio.run(WorkflowJsRunner().run(
            script="return await agent('inspect', {agentType: 'reviewer', label: 'r1'});",
            args={},
            budget={"max_host_calls": 10},
            bridge=FakeBridge(),
        ))
        self.assertEqual(result["method"], "agent")
        self.assertEqual(result["params"]["name"], "reviewer")
        self.assertEqual(result["params"]["input"], "inspect")

    def test_runtime_agent_supports_legacy_name_input_signature(self):
        class FakeBridge:
            async def handle_call(self, method, params):
                return {"method": method, "params": params}

        result = asyncio.run(WorkflowJsRunner().run(
            script="return await agent('reviewer', 'inspect');",
            args={},
            budget={"max_host_calls": 10},
            bridge=FakeBridge(),
        ))
        self.assertEqual(result["params"]["name"], "reviewer")
        self.assertEqual(result["params"]["input"], "inspect")

    def test_runtime_agent_supports_legacy_object_input_signature(self):
        class FakeBridge:
            async def handle_call(self, method, params):
                return {"method": method, "params": params}

        result = asyncio.run(WorkflowJsRunner().run(
            script="return await agent('reviewer', {topic: 'x'}, {label: 'legacy'});",
            args={},
            budget={"max_host_calls": 10},
            bridge=FakeBridge(),
        ))
        self.assertEqual(result["params"]["name"], "reviewer")
        self.assertEqual(result["params"]["input"], {"topic": "x"})
        self.assertEqual(result["params"]["options"], {"label": "legacy"})


class PromptDriftGuardTests(unittest.TestCase):
    def test_validate_prompt_catalog_does_not_require_local_reference_by_default(self):
        with patch.dict(PROMPT_SOURCES, {"core": ("reference/does-not-exist.md",)}, clear=False):
            validate_prompt_catalog()

    def test_validate_prompt_catalog_can_audit_missing_sources_explicitly(self):
        with patch.dict(PROMPT_SOURCES, {"core": ("reference/does-not-exist.md",)}, clear=False):
            with self.assertRaises(FileNotFoundError):
                validate_prompt_catalog(require_source_files=True)

    def test_prompt_framework_docs_exist(self):
        doc = Path("docs/prompt-framework.md").read_text(encoding="utf-8")
        self.assertIn("ChatTree Prompt Framework", doc)
        self.assertIn("`/btw` is supported as a side-question run", doc)


if __name__ == "__main__":
    unittest.main()
