import unittest
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from backend.api.routes.messages import SendMessageRequest, detached_stream_event_generator
from backend.core.agents.subagent_executor import SubagentExecutor
from backend.core.capabilities.agent_loader import load_agent_roots
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import AgentDefinition, CapabilitySource
from backend.core.chat.chat_manager import ChatManager
from backend.core.prompts import PromptBuilder
from backend.core.prompts.catalog import (
    PROMPT_SOURCES,
    load_prompt_template,
    validate_prompt_catalog,
)
from backend.core.prompts.types import PromptBuildRequest
from backend.core.runs import RunManager
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
        system = messages[0]["content"]
        self.assertIn("ChatTree", system)
        self.assertIn("Worker body", system)
        self.assertIn("workflow", system.lower())
        self.assertIn("directive", system.lower())

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
        system = messages[0]["content"]
        self.assertIn("Custom worker body", system)
        self.assertIn("pipeline(", system)
        self.assertIn("workflow", system.lower())


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
    def test_validate_prompt_catalog_rejects_missing_sources(self):
        with patch.dict(PROMPT_SOURCES, {"core": ("reference/does-not-exist.md",)}, clear=False):
            with self.assertRaises(FileNotFoundError):
                validate_prompt_catalog()

    def test_prompt_framework_docs_exist(self):
        doc = Path("docs/prompt-framework.md").read_text(encoding="utf-8")
        self.assertIn("ChatTree Prompt Framework", doc)
        self.assertIn("`/btw` is supported as a side-question run", doc)


if __name__ == "__main__":
    unittest.main()
