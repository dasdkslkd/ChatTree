import asyncio
from types import SimpleNamespace

import pytest

from backend.core.agents import SubagentExecutor
from backend.core.agents.subagent_executor import DEFAULT_MAX_TOOL_ROUNDS, DEFAULT_MAX_TURNS
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import (
    AgentDefinition,
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySource,
)
from backend.core.config.types import StreamStatus
from backend.core.runs import RunManager


class FakeRegistry:
    def __init__(self, agent: AgentDefinition):
        self.agent = agent

    def get_agent(self, name: str):
        return self.agent if name == self.agent.name else None


class FakeToolManager:
    def get_openai_tools(self):
        return [
            {"function": {"name": "read_file"}},
            {"function": {"name": "run_command"}},
        ]


class FakeProvider:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.call_count = 0

    async def generate_response_stream(self, **kwargs):
        self.call_count += 1
        round_items = self.rounds[min(self.call_count - 1, len(self.rounds) - 1)]
        for item in round_items:
            delay = item.pop("_delay", 0)
            if delay:
                await asyncio.sleep(delay)
            yield item


class FakeModelManager:
    def __init__(self, provider):
        self.provider = provider
        self.model_list = {"fake-provider": ["fake-model"]}

    def get_model(self, provider_id, stream=False):
        return self.provider


class FakeChatManager:
    def __init__(self, provider):
        self.model_manager = FakeModelManager(provider)
        self.tool_manager = FakeToolManager()
        self.conversation = SimpleNamespace(
            metadata={},
            current_provider="fake-provider",
            current_model="fake-model",
        )

    def get_conversation(self, conversation_id):
        return self.conversation

    def _provider_for_model(self, model):
        return "fake-provider"

    def _merge_tool_call_lists(self, existing, incoming):
        return [*existing, *incoming]

    async def _execute_tool_calls(self, tool_calls, **kwargs):
        return [
            {
                "tool_call_id": call.get("id"),
                "name": call.get("function", {}).get("name", "tool"),
                "content": "ok",
            }
            for call in tool_calls
        ]

    def _apply_round_tool_result_budget(self, tool_messages):
        return [{"role": "tool", "content": message["content"]} for message in tool_messages]

    def _tool_event_stream_chunk(self, event, node_id, conversation_id):
        return {
            "status": "content",
            "event_type": event.get("event_type", "tool_result"),
            "content": None,
            "node_id": node_id,
            "conversation_id": conversation_id,
        }


def make_executor(agent, provider):
    run_manager = RunManager()
    executor = SubagentExecutor(
        chat_manager=FakeChatManager(provider),
        run_manager=run_manager,
        capability_registry=FakeRegistry(agent),
    )
    return executor, run_manager


async def wait_terminal(run_manager, conversation_id, run_id):
    for _ in range(100):
        state = run_manager.get_run(run_id)
        if state["status"] in {"completed", "failed", "cancelled"}:
            events = [
                event["payload"]
                for event in run_manager.journal.read_events(conversation_id, run_id)
            ]
            return state, events
        await asyncio.sleep(0.02)
    raise AssertionError("run did not finish")


def test_empty_agent_tools_means_no_tools_and_star_allows_all_tools():
    agent = AgentDefinition(name="a", tools=[])
    executor, _ = make_executor(agent, FakeProvider([]))

    assert executor._filter_tools(agent.tools) == []
    assert [tool["function"]["name"] for tool in executor._filter_tools(["*"])] == [
        "read_file",
        "run_command",
    ]


def test_subagent_build_messages_uses_prompt_builder_for_agent_skills(tmp_path):
    skill_path = tmp_path / "review" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# Review\n\n检查代码。", encoding="utf-8")
    registry = CapabilityRegistry()
    registry.add_agents([AgentDefinition(name="a", system_prompt="Agent base", skills=["review"])])
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
    executor = SubagentExecutor(
        chat_manager=FakeChatManager(FakeProvider([])),
        run_manager=RunManager(),
        capability_registry=registry,
    )

    messages = executor._build_messages("a", "inspect", parent_node_id="node-1")

    assert "ChatTree Worker Fork Prompt" in messages[0]["content"]
    assert "Agent base" in messages[0]["content"]
    assert "Parent conversation node: node-1" in messages[0]["content"]
    assert "# ChatTree Core Prompt" not in messages[0]["content"]
    assert "Runtime mode: subagent worker" in messages[1]["content"]
    assert "<name>review</name>" in messages[2]["content"]
    assert "## Available Capabilities" not in messages[2]["content"]
    assert messages[3]["content"] == "inspect"


def test_subagent_rejects_invalid_input_schema_before_creating_run():
    agent = AgentDefinition(
        name="schema-agent",
        input_schema={
            "type": "object",
            "required": ["task"],
            "properties": {"task": {"type": "string"}},
        },
    )
    executor, run_manager = make_executor(agent, FakeProvider([]))

    async def run():
        with pytest.raises(ValueError, match="input_schema"):
            await executor.start(
                conversation_id="conv",
                agent_name="schema-agent",
                input_data={"task": 123},
            )
        assert run_manager.list_runs("conv") == []

    asyncio.run(run())


def test_subagent_timeout_seconds_fails_run():
    agent = AgentDefinition(name="slow", timeout_seconds=1)
    provider = FakeProvider([
        [{"status": "content", "content": "late", "_delay": 2}],
    ])
    executor, run_manager = make_executor(agent, provider)

    async def run():
        record = await executor.start(
            conversation_id="conv",
            agent_name="slow",
            input_data="hello",
        )
        state, events = await wait_terminal(run_manager, "conv", record["run_id"])
        assert state["status"] == "failed"
        assert "timeout" in state["metadata"]["error"].lower()
        assert any(event.get("event_type") == "subagent_error" for event in events)

    asyncio.run(run())


def test_subagent_max_turns_limits_model_rounds():
    agent = AgentDefinition(name="limited", max_turns=1, tools=["read_file"])
    provider = FakeProvider([
        [
            {
                "status": StreamStatus.COMPLETE,
                "tool_calls": [
                    {"id": "call-1", "function": {"name": "read_file"}},
                ],
            }
        ],
        [{"status": StreamStatus.COMPLETE, "content": "should not run"}],
    ])
    executor, run_manager = make_executor(agent, provider)

    async def run():
        record = await executor.start(
            conversation_id="conv",
            agent_name="limited",
            input_data="hello",
        )
        state, _ = await wait_terminal(run_manager, "conv", record["run_id"])
        assert state["status"] == "failed"
        assert provider.call_count == 1
        assert "max_turns" in state["metadata"]["error"]

    asyncio.run(run())


def test_default_subagent_tool_round_limit_is_500():
    assert DEFAULT_MAX_TOOL_ROUNDS == 500


def test_default_subagent_max_turns_is_1000():
    assert DEFAULT_MAX_TURNS == 1000


def test_subagent_output_schema_validates_json_result():
    agent = AgentDefinition(
        name="output-schema",
        output_schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )
    provider = FakeProvider([
        [
            {"status": "content", "content": "{\"ok\":\"yes\"}"},
            {"status": StreamStatus.COMPLETE},
        ],
    ])
    executor, run_manager = make_executor(agent, provider)

    async def run():
        record = await executor.start(
            conversation_id="conv",
            agent_name="output-schema",
            input_data="hello",
        )
        state, events = await wait_terminal(run_manager, "conv", record["run_id"])
        assert state["status"] == "failed"
        assert "output_schema" in state["metadata"]["error"]
        assert any(event.get("event_type") == "subagent_error" for event in events)

    asyncio.run(run())
