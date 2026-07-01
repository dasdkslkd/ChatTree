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
from backend.api.routes.messages import SendMessageRequest, detached_stream_event_generator
from backend.core.agents.subagent_executor import SubagentExecutor
from backend.core.capabilities.agent_loader import load_agent_roots
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import (
    AgentDefinition,
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySource,
)
from backend.core.chat.chat_manager import ChatManager
from backend.core.config.types import StreamStatus
from backend.core.prompts import PromptBuilder
from backend.core.prompts import types as prompt_types
from backend.core.prompts.catalog import (
    PROMPT_SOURCES,
    load_prompt_template,
    validate_prompt_catalog,
)
from backend.core.prompts.types import PromptBuildRequest
from backend.core.runs import RunKind, RunManager, SyntheticInputQueue
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


class SlashRuntimeDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def _collect_events(self, generator):
        events = []
        async for event in generator:
            events.append(event)
        return events

    async def test_fork_slash_starts_subagent_run_without_chat_run(self):
        class FakeChatManager:
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
        events = await self._collect_events(detached_stream_event_generator(
            "conversation-1",
            SendMessageRequest(content="/fork inspect prompts", node_id="node-1"),
            FakeChatManager(),
            FakeRunManager(),
            subagent,
            None,
        ))
        self.assertEqual(subagent.kwargs["agent_name"], "implementer")
        self.assertEqual(subagent.kwargs["input_data"], "inspect prompts")
        self.assertTrue(events[-1].strip().endswith("[DONE]"))

    async def test_workflow_slash_starts_workflow_run_without_chat_run(self):
        class FakeChatManager:
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
        events = await self._collect_events(detached_stream_event_generator(
            "conversation-1",
            SendMessageRequest(content="/workflow return 1", node_id="node-1"),
            FakeChatManager(),
            FakeRunManager(),
            None,
            workflow,
        ))
        self.assertEqual(workflow.kwargs["script"], "return 1")
        self.assertEqual(workflow.kwargs["parent_node_id"], "node-1")
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

    def _executor(self, run_manager, provider):
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
        )

    async def test_completed_subagent_enqueues_pending_task_notification_once(self):
        run_manager = RunManager()
        executor = self._executor(run_manager, self.FakeProvider())
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
        await executor._enqueue_synthetic_task_notification(run.run_id, "completed", "duplicate")

        pending = run_manager.synthetic_inputs.list_pending("conversation-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "task_notification")
        self.assertEqual(pending[0]["anchor_node_id"], "node-1")
        self.assertEqual(pending[0]["source_run_id"], run.run_id)
        self.assertEqual(pending[0]["source_run_kind"], "subagent")
        self.assertEqual(pending[0]["metadata"]["origin"], "task_notification")
        self.assertEqual(pending[0]["metadata"]["source_status"], "completed")
        self.assertIn("subagent answer", pending[0]["content"])

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
        self.assertIn("workflow answer", pending[0]["content"])

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
        self.assertNotIn("# ChatTree Core Prompt", system_text)


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
