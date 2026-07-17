import asyncio
import logging
from types import SimpleNamespace

import pytest

from backend.core.agents import AgentMailbox, AgentRuntime, AgentSource, SubagentExecutor
from backend.core.agents.subagent_executor import DEFAULT_MAX_TOOL_ROUNDS, DEFAULT_MAX_TURNS
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import (
    AgentDefinition,
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySource,
)
from backend.core.config.types import StreamStatus
from backend.core.runs import (
    RunIdempotency,
    RunIdempotencyConflictError,
    RunManager,
    RunStartCoordinator,
    RunStartValidationError,
)
from backend.core.tasks import ActiveTaskConflictError, ActiveTaskService


class FakeRegistry:
    def __init__(self, agent: AgentDefinition):
        self.agent = agent

    def get_agent(self, name: str):
        return self.agent if name == self.agent.name else None


class FakeToolManager:
    def get_openai_tools(self, **_kwargs):
        return [
            {"function": {"name": "read"}},
            {"function": {"name": "shell"}},
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
    assert [tool["function"]["name"] for tool in executor._filter_tools(None)] == [
        "read",
        "shell",
    ]
    assert [tool["function"]["name"] for tool in executor._filter_tools(["*"])] == [
        "read",
        "shell",
    ]
    assert [tool["function"]["name"] for tool in executor._filter_tools(["*"], disallowed_names=["shell"])] == [
        "read",
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


def test_idempotent_subagent_start_schedules_and_notifies_once_without_blocking(
    monkeypatch,
    caplog,
):
    agent = AgentDefinition(
        name="implementer",
        input_schema={
            "type": "object",
            "required": ["path", "depth"],
            "properties": {
                "path": {"type": "string"},
                "depth": {"type": "integer"},
            },
        },
    )
    executor, run_manager = make_executor(agent, FakeProvider([]))
    executor.run_start_coordinator = RunStartCoordinator(run_manager)
    produced = asyncio.Event()
    release_producer = asyncio.Event()
    notification_started = asyncio.Event()
    release_notification = asyncio.Event()
    produce_calls = 0
    notification_calls = 0
    anchor_calls = 0
    bind_calls = []
    produced_inputs = []
    produced_parents = []

    async def fake_produce(**kwargs):
        nonlocal produce_calls
        produce_calls += 1
        produced_inputs.append(kwargs["input_data"])
        produced_parents.append(kwargs["parent_node_id"])
        produced.set()
        await release_producer.wait()

    async def fake_notification(_run_id, *, agent_name):
        nonlocal notification_calls
        assert agent_name == "implementer"
        notification_calls += 1
        notification_started.set()
        await release_notification.wait()
        raise RuntimeError("notification boom")

    async def fake_anchor_factory(_run):
        nonlocal anchor_calls
        anchor_calls += 1
        return "winner-anchor"

    async def fake_bind_anchor(run_id, anchor_node_id):
        bind_calls.append((run_id, anchor_node_id))
        record = run_manager._runs[run_id]
        record.anchor_node_id = anchor_node_id
        return record

    monkeypatch.setattr(executor, "_produce", fake_produce)
    monkeypatch.setattr(executor, "_register_task_notification", fake_notification)
    monkeypatch.setattr(run_manager, "bind_anchor_node", fake_bind_anchor, raising=False)
    idempotency = RunIdempotency("op_agent", "a" * 64)
    input_data = {"path": "src", "depth": 2}

    async def run():
        caplog.set_level(logging.ERROR, logger="backend.core.agents.subagent_executor")
        first, second = await asyncio.wait_for(
            asyncio.gather(
                executor.start_idempotent(
                    conversation_id="conv-1",
                    agent_name="implementer",
                    input_data=input_data,
                    parent_node_id="original-anchor",
                    idempotency=idempotency,
                    request_id="req_agent_1",
                    winner_anchor_factory=fake_anchor_factory,
                ),
                executor.start_idempotent(
                    conversation_id="conv-1",
                    agent_name="implementer",
                    input_data=input_data,
                    parent_node_id="original-anchor",
                    idempotency=idempotency,
                    request_id="req_agent_2",
                    winner_anchor_factory=fake_anchor_factory,
                ),
            ),
            timeout=1,
        )
        await asyncio.wait_for(produced.wait(), timeout=1)
        await asyncio.wait_for(notification_started.wait(), timeout=1)

        assert first.run.run_id == second.run.run_id
        assert {first.created, second.created} == {True, False}
        assert produce_calls == 1
        assert notification_calls == 1
        assert anchor_calls == 1
        assert bind_calls == [(first.run.run_id, "winner-anchor")]
        assert produced_inputs == [input_data]
        assert produced_inputs[0] is input_data
        assert produced_parents == ["winner-anchor"]
        assert list(executor._tasks) == [first.run.run_id]

        release_notification.set()
        for _ in range(20):
            if not executor._notification_tasks:
                break
            await asyncio.sleep(0)
        assert not executor._notification_tasks
        assert sum(
            "Failed to register subagent notification" in record.message
            for record in caplog.records
        ) == 1

        release_producer.set()
        await asyncio.gather(*executor._tasks.values())
        executor._tasks.clear()

    asyncio.run(run())


def test_idempotent_subagent_replay_skips_changed_schema_and_new_key_still_validates(
    monkeypatch,
):
    agent = AgentDefinition(
        name="implementer",
        input_schema={
            "type": "object",
            "required": ["task"],
            "properties": {"task": {"type": "string"}},
        },
    )
    executor, run_manager = make_executor(agent, FakeProvider([]))
    coordinator = RunStartCoordinator(run_manager)
    executor.run_start_coordinator = coordinator
    producer_started = asyncio.Event()
    release_producer = asyncio.Event()
    produce_calls = 0

    async def fake_produce(**_kwargs):
        nonlocal produce_calls
        produce_calls += 1
        producer_started.set()
        await release_producer.wait()

    monkeypatch.setattr(executor, "_produce", fake_produce)
    idempotency = RunIdempotency("op-schema-replay", "1" * 64)

    async def run():
        first = await executor.start_idempotent(
            conversation_id="conv-schema-replay",
            agent_name="implementer",
            input_data={"task": "inspect"},
            idempotency=idempotency,
            request_id="request-schema-first",
        )
        await asyncio.wait_for(producer_started.wait(), timeout=1)
        run_id = first.run.run_id
        events_before = list(run_manager.read_events(run_id))

        agent.input_schema = {
            "type": "object",
            "required": ["task", "new_required_field"],
            "properties": {
                "task": {"type": "string"},
                "new_required_field": {"type": "string"},
            },
        }

        replay_lookups = 0
        real_replay_existing = coordinator.replay_existing

        async def miss_once_then_replay(canonical_idempotency):
            nonlocal replay_lookups
            replay_lookups += 1
            if replay_lookups == 1:
                return None
            return await real_replay_existing(canonical_idempotency)

        monkeypatch.setattr(
            coordinator,
            "replay_existing",
            miss_once_then_replay,
        )

        replay = await executor.start_idempotent(
            conversation_id="conv-schema-replay",
            agent_name="implementer",
            input_data={"task": "inspect"},
            idempotency=idempotency,
            request_id="request-schema-replay",
        )

        assert replay.created is False
        assert replay.run.run_id == run_id
        assert replay_lookups == 2
        assert produce_calls == 1
        assert list(run_manager.read_events(run_id)) == events_before

        with pytest.raises(RunStartValidationError, match="input_schema"):
            await executor.start_idempotent(
                conversation_id="conv-schema-replay",
                agent_name="implementer",
                input_data={"task": "inspect"},
                idempotency=RunIdempotency("op-schema-new", "2" * 64),
                request_id="request-schema-new",
            )
        assert [item["run_id"] for item in run_manager.list_runs("conv-schema-replay")] == [
            run_id
        ]
        assert list(run_manager.read_events(run_id)) == events_before

        release_producer.set()
        await asyncio.gather(*executor._tasks.values())

    asyncio.run(run())


def test_idempotent_subagent_active_barrier_replay_and_conflict_skip_removed_agent(
    monkeypatch,
):
    agent = AgentDefinition(name="implementer")
    executor, run_manager = make_executor(agent, FakeProvider([]))
    executor.run_start_coordinator = RunStartCoordinator(run_manager)
    anchor_entered = asyncio.Event()
    release_anchor = asyncio.Event()
    producer_started = asyncio.Event()
    release_producer = asyncio.Event()

    async def blocking_anchor(_run):
        anchor_entered.set()
        await release_anchor.wait()
        return None

    async def fake_produce(**_kwargs):
        producer_started.set()
        await release_producer.wait()

    monkeypatch.setattr(executor, "_produce", fake_produce)
    idempotency = RunIdempotency("op-registry-replay", "3" * 64)

    async def run():
        winner_task = asyncio.create_task(
            executor.start_idempotent(
                conversation_id="conv-registry-replay",
                agent_name="implementer",
                input_data="inspect",
                idempotency=idempotency,
                request_id="request-registry-winner",
                winner_anchor_factory=blocking_anchor,
            )
        )
        await asyncio.wait_for(anchor_entered.wait(), timeout=1)
        executor.capability_registry.agent = AgentDefinition(name="removed")

        replay_task = asyncio.create_task(
            executor.start_idempotent(
                conversation_id="conv-registry-replay",
                agent_name="implementer",
                input_data="inspect",
                idempotency=idempotency,
                request_id="request-registry-replay",
            )
        )
        conflict_task = asyncio.create_task(
            executor.start_idempotent(
                conversation_id="conv-registry-replay",
                agent_name="implementer",
                input_data="inspect",
                idempotency=RunIdempotency("op-registry-replay", "4" * 64),
                request_id="request-registry-conflict",
            )
        )
        await asyncio.sleep(0)
        assert not replay_task.done()
        assert not conflict_task.done()

        release_anchor.set()
        winner = await asyncio.wait_for(winner_task, timeout=1)
        replay = await asyncio.wait_for(replay_task, timeout=1)
        with pytest.raises(RunIdempotencyConflictError) as raised:
            await asyncio.wait_for(conflict_task, timeout=1)

        assert replay.created is False
        assert replay.run.run_id == winner.run.run_id
        assert raised.value.existing_run_id == winner.run.run_id
        await asyncio.wait_for(producer_started.wait(), timeout=1)
        assert list(executor._tasks) == [winner.run.run_id]

        with pytest.raises(KeyError, match="implementer"):
            await executor.start_idempotent(
                conversation_id="conv-registry-replay",
                agent_name="implementer",
                input_data="inspect",
                idempotency=RunIdempotency("op-registry-new", "5" * 64),
                request_id="request-registry-new",
            )
        assert [item["run_id"] for item in run_manager.list_runs("conv-registry-replay")] == [
            winner.run.run_id
        ]

        release_producer.set()
        await asyncio.gather(*executor._tasks.values())

    asyncio.run(run())


def test_subagent_coordinator_shutdown_interrupts_owned_producer():
    class BlockingProvider:
        def __init__(self):
            self.started = asyncio.Event()

        async def generate_response_stream(self, **_kwargs):
            self.started.set()
            await asyncio.Event().wait()
            yield {"status": StreamStatus.COMPLETE}

    async def run():
        provider = BlockingProvider()
        agent = AgentDefinition(name="implementer", tools=[])
        executor, run_manager = make_executor(agent, provider)
        coordinator = RunStartCoordinator(run_manager)
        executor.run_start_coordinator = coordinator
        started = await executor.start_idempotent(
            conversation_id="conv-shutdown",
            agent_name="implementer",
            input_data="inspect",
            idempotency=RunIdempotency("op-agent-shutdown", "f" * 64),
            request_id="request-agent-shutdown",
        )
        await asyncio.wait_for(provider.started.wait(), timeout=1)

        drained = await coordinator.close(timeout=1)

        run_id = started.run.run_id
        state = run_manager.get_run(run_id)
        assert drained.exhausted is False
        assert state["status"] == "interrupted"
        finished = [
            event
            for event in run_manager.read_events(run_id)
            if event.get("type") == "run_finished"
        ]
        assert len(finished) == 1
        assert finished[0]["status"] == "interrupted"
        assert not [
            event
            for event in run_manager.read_events(run_id)
            if event.get("event_type") == "subagent_result"
        ]

    asyncio.run(run())


def test_subagent_user_stop_keeps_owned_producer_cancelled():
    class BlockingProvider:
        def __init__(self):
            self.started = asyncio.Event()

        async def generate_response_stream(self, **_kwargs):
            self.started.set()
            await asyncio.Event().wait()
            yield {"status": StreamStatus.COMPLETE}

    async def run():
        provider = BlockingProvider()
        agent = AgentDefinition(name="implementer", tools=[])
        executor, run_manager = make_executor(agent, provider)
        coordinator = RunStartCoordinator(run_manager)
        executor.run_start_coordinator = coordinator
        started = await executor.start_idempotent(
            conversation_id="conv-stop-owned",
            agent_name="implementer",
            input_data="inspect",
            idempotency=RunIdempotency("op-agent-stop", "0" * 64),
            request_id="request-agent-stop",
        )
        await asyncio.wait_for(provider.started.wait(), timeout=1)

        assert await executor.stop(started.run.run_id) is True
        state, _events = await wait_terminal(
            run_manager,
            "conv-stop-owned",
            started.run.run_id,
        )

        assert state["status"] == "cancelled"
        assert (await coordinator.close(timeout=1)).exhausted is False
        assert run_manager.get_run(started.run.run_id)["status"] == "cancelled"

    asyncio.run(run())


def test_subagent_close_serializes_with_internal_start_and_rejects_new_work(monkeypatch):
    class BlockingProvider:
        async def generate_response_stream(self, **_kwargs):
            await asyncio.Event().wait()
            yield {"status": StreamStatus.COMPLETE}

    async def run():
        agent = AgentDefinition(name="implementer", tools=[])
        executor, run_manager = make_executor(agent, BlockingProvider())
        create_entered = asyncio.Event()
        release_create = asyncio.Event()
        real_create_run = run_manager.create_run

        async def blocking_create_run(**kwargs):
            create_entered.set()
            await release_create.wait()
            return await real_create_run(**kwargs)

        monkeypatch.setattr(run_manager, "create_run", blocking_create_run)
        start_task = asyncio.create_task(
            executor.start(
                conversation_id="conv-close-race",
                agent_name="implementer",
                input_data="inspect",
            )
        )
        await asyncio.wait_for(create_entered.wait(), timeout=1)

        close_task = asyncio.create_task(executor.close(timeout=1))
        await asyncio.sleep(0)
        assert not close_task.done()

        release_create.set()
        started = await asyncio.wait_for(start_task, timeout=1)
        assert await asyncio.wait_for(close_task, timeout=1) == ()
        run_id = started["run_id"]
        assert run_manager.get_run(run_id)["status"] == "cancelled"
        assert executor._tasks == {}

        with pytest.raises(RuntimeError, match="subagent executor is closing"):
            await executor.start(
                conversation_id="conv-close-rejected",
                agent_name="implementer",
                input_data="inspect",
            )

        unscheduled = await real_create_run(
            conversation_id="conv-schedule-rejected",
            kind="subagent",
        )
        with pytest.raises(RuntimeError, match="subagent executor is closing"):
            await executor.schedule_existing(
                run=unscheduled,
                conversation_id="conv-schedule-rejected",
                agent_name="implementer",
                input_data="inspect",
                parent_node_id=None,
                created_by_run_id=None,
                cancellation_parent_run_id=None,
                provider_id=None,
                model_id=None,
                permission_mode=None,
                workspace=None,
                context_mode="fresh",
            )
        await run_manager.finish_run(unscheduled.run_id, "cancelled")

    asyncio.run(run())


def test_subagent_close_deadline_covers_lifecycle_lock(monkeypatch):
    class BlockingProvider:
        async def generate_response_stream(self, **_kwargs):
            await asyncio.Event().wait()
            yield {"status": StreamStatus.COMPLETE}

    agent = AgentDefinition(name="implementer", tools=[])
    executor, run_manager = make_executor(agent, BlockingProvider())
    create_entered = asyncio.Event()
    release_create = asyncio.Event()
    real_create_run = run_manager.create_run

    async def blocking_create_run(**kwargs):
        create_entered.set()
        await release_create.wait()
        return await real_create_run(**kwargs)

    monkeypatch.setattr(run_manager, "create_run", blocking_create_run)

    async def run():
        start_task = asyncio.create_task(
            executor.start(
                conversation_id="conv-close-lock-deadline",
                agent_name="implementer",
                input_data="inspect",
            )
        )
        await asyncio.wait_for(create_entered.wait(), timeout=1)
        try:
            assert await asyncio.wait_for(executor.close(timeout=0.01), timeout=0.2) == (
                "subagent-lifecycle-lock",
            )
        finally:
            release_create.set()

        started = await asyncio.wait_for(start_task, timeout=1)
        with pytest.raises(RuntimeError, match="subagent executor is closing"):
            await executor.start(
                conversation_id="conv-close-lock-rejected",
                agent_name="implementer",
                input_data="inspect",
            )

        assert await executor.close(timeout=1) == ()
        assert run_manager.get_run(started["run_id"])["status"] == "cancelled"

    asyncio.run(run())


def test_subagent_close_terminalizes_producer_done_before_snapshot_callback(monkeypatch):
    agent = AgentDefinition(name="implementer", tools=[])
    executor, run_manager = make_executor(agent, FakeProvider([]))

    async def run():
        record = await run_manager.create_run(
            conversation_id="conv-producer-done-before-close",
            kind="subagent",
        )

        async def failed_producer():
            raise RuntimeError("producer failed before close snapshot")

        producer_task = asyncio.create_task(
            failed_producer(),
            name=f"subagent-producer:{record.run_id}",
        )
        with pytest.raises(RuntimeError, match="producer failed before close snapshot"):
            await producer_task
        assert producer_task.done()
        executor._tasks[record.run_id] = producer_task

        assert await executor.close(timeout=1) == ()
        assert executor._tasks == {}
        assert run_manager.get_run(record.run_id)["status"] == "cancelled"

    asyncio.run(run())


def test_subagent_close_sees_nonterminal_producer_after_done_callback(monkeypatch):
    agent = AgentDefinition(name="implementer", tools=[])
    executor, run_manager = make_executor(agent, FakeProvider([]))
    producer_callback_ran = asyncio.Event()
    original_consume = executor._consume_producer_task

    async def producer(**_kwargs):
        raise RuntimeError("producer failed without terminalizing")

    def consume(run_id, task):
        original_consume(run_id, task)
        producer_callback_ran.set()

    monkeypatch.setattr(executor, "_produce", producer)
    monkeypatch.setattr(executor, "_consume_producer_task", consume)

    async def run():
        started = await executor.start(
            conversation_id="conv-nonterminal-done-callback",
            agent_name="implementer",
            input_data="inspect",
        )
        run_id = started["run_id"]
        producer_task = executor._tasks[run_id]
        await asyncio.wait_for(producer_callback_ran.wait(), timeout=1)
        assert producer_task.done()
        assert executor._tasks.get(run_id) is producer_task
        assert run_manager.get_run(run_id)["status"] == "running"

        assert await executor.close(timeout=1) == ()
        assert executor._tasks == {}
        assert run_manager.get_run(run_id)["status"] == "cancelled"

    asyncio.run(run())


def test_subagent_close_reports_producer_that_swallows_cancellation():
    class StubbornProvider:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancellation_swallowed = asyncio.Event()
            self.release = asyncio.Event()

        async def generate_response_stream(self, **_kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellation_swallowed.set()
                await self.release.wait()
                raise
            yield {"status": StreamStatus.COMPLETE}

    async def run():
        provider = StubbornProvider()
        agent = AgentDefinition(name="implementer", tools=[])
        executor, run_manager = make_executor(agent, provider)
        started = await executor.start(
            conversation_id="conv-producer-stubborn",
            agent_name="implementer",
            input_data="inspect",
        )
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        run_id = started["run_id"]
        producer_task = executor._tasks[run_id]
        task_name = f"subagent-producer:{run_id}"
        try:
            assert await executor.close(timeout=0.01) == (task_name,)
            assert provider.cancellation_swallowed.is_set()

            provider.release.set()
            for _ in range(100):
                if producer_task.done():
                    break
                await asyncio.sleep(0)
            assert producer_task.done()
            assert await executor.close(timeout=1) == ()
            assert executor._tasks == {}
            assert run_manager.get_run(run_id)["status"] == "cancelled"
        finally:
            provider.release.set()

    asyncio.run(run())


def test_subagent_close_retries_late_internal_producer_terminalization(monkeypatch):
    agent = AgentDefinition(name="implementer", tools=[])
    executor, run_manager = make_executor(agent, FakeProvider([]))
    producer_started = asyncio.Event()
    cancellation_swallowed = asyncio.Event()
    producer_release = asyncio.Event()

    async def producer(**_kwargs):
        producer_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_swallowed.set()
            await producer_release.wait()
            raise RuntimeError("producer failed after cancellation")

    monkeypatch.setattr(executor, "_produce", producer)

    async def run():
        loop_errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        started = await executor.start(
            conversation_id="conv-producer-late",
            agent_name="implementer",
            input_data="inspect",
        )
        run_id = started["run_id"]
        producer_task = executor._tasks[run_id]
        await asyncio.wait_for(producer_started.wait(), timeout=1)
        task_name = f"subagent-producer:{run_id}"
        try:
            assert await executor.close(timeout=0.01) == (task_name,)
            assert cancellation_swallowed.is_set()

            producer_release.set()
            for _ in range(100):
                if producer_task.done():
                    break
                await asyncio.sleep(0)
            assert producer_task.done()
            assert executor._tasks.get(run_id) is producer_task
            assert run_manager.get_run(run_id)["status"] == "running"

            assert await executor.close(timeout=1) == ()
            assert executor._tasks == {}
            assert run_manager.get_run(run_id)["status"] == "cancelled"
            await asyncio.sleep(0)
            assert loop_errors == []
        finally:
            producer_release.set()

    asyncio.run(run())


def test_subagent_close_deadline_covers_retryable_late_terminalization(monkeypatch):
    agent = AgentDefinition(name="implementer", tools=[])
    executor, run_manager = make_executor(agent, FakeProvider([]))
    producer_started = asyncio.Event()
    producer_release = asyncio.Event()
    finish_entered = asyncio.Event()
    finish_release = asyncio.Event()
    finish_attempts = 0
    real_finish_run = run_manager.finish_run

    async def producer(**_kwargs):
        producer_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await producer_release.wait()
            raise RuntimeError("producer failed after cancellation")

    async def blocking_then_successful_finish_run(*args, **kwargs):
        nonlocal finish_attempts
        finish_attempts += 1
        if finish_attempts == 1:
            finish_entered.set()
            await finish_release.wait()
            raise RuntimeError("transient terminalization failure")
        return await real_finish_run(*args, **kwargs)

    monkeypatch.setattr(executor, "_produce", producer)

    async def run():
        started = await executor.start(
            conversation_id="conv-terminalization-deadline",
            agent_name="implementer",
            input_data="inspect",
        )
        run_id = started["run_id"]
        producer_task = executor._tasks[run_id]
        producer_name = f"subagent-producer:{run_id}"
        terminalize_name = f"subagent-terminalize:{run_id}"
        await asyncio.wait_for(producer_started.wait(), timeout=1)
        try:
            assert await executor.close(timeout=0.01) == (producer_name,)
            producer_release.set()
            for _ in range(100):
                if producer_task.done():
                    break
                await asyncio.sleep(0)
            assert producer_task.done()

            monkeypatch.setattr(run_manager, "finish_run", blocking_then_successful_finish_run)
            assert await asyncio.wait_for(executor.close(timeout=0.01), timeout=0.2) == (
                terminalize_name,
            )
            await asyncio.wait_for(finish_entered.wait(), timeout=1)
            assert await executor.close(timeout=0.01) == (terminalize_name,)
            assert finish_attempts == 1

            finish_release.set()
            for _ in range(100):
                if executor._shutdown_terminalization_tasks[run_id].done():
                    break
                await asyncio.sleep(0)
            assert executor._shutdown_terminalization_tasks[run_id].done()

            assert await executor.close(timeout=1) == ()
            assert finish_attempts == 2
            assert executor._tasks == {}
            assert run_manager.get_run(run_id)["status"] == "cancelled"
        finally:
            producer_release.set()
            finish_release.set()

    asyncio.run(run())


def test_subagent_close_drains_blocked_notification_and_consumes_error(monkeypatch):
    agent = AgentDefinition(name="implementer", tools=[])
    executor, run_manager = make_executor(agent, FakeProvider([]))
    producer_release = asyncio.Event()
    notification_started = asyncio.Event()
    notification_cancelled = asyncio.Event()

    async def producer(**kwargs):
        await producer_release.wait()
        await run_manager.finish_run(kwargs["run_id"])

    async def notification(_run_id, *, agent_name):
        assert agent_name == "implementer"
        notification_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            notification_cancelled.set()
            raise RuntimeError("notification failed after cancellation")

    monkeypatch.setattr(executor, "_produce", producer)
    monkeypatch.setattr(executor, "_register_task_notification", notification)

    async def run():
        loop_errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        started = await executor.start(
            conversation_id="conv-notification-close",
            agent_name="implementer",
            input_data="inspect",
        )
        await asyncio.wait_for(notification_started.wait(), timeout=1)
        assert [task.get_name() for task in executor._notification_tasks] == [
            f"subagent-notification:{started['run_id']}"
        ]

        assert await executor.close(timeout=1) == ()
        assert notification_cancelled.is_set()
        assert executor._notification_tasks == set()
        await asyncio.sleep(0)
        assert loop_errors == []

        tasks = list(executor._tasks.values())
        producer_release.set()
        await asyncio.gather(*tasks)
        executor._tasks.clear()

    asyncio.run(run())


def test_subagent_close_reports_notification_that_swallows_cancellation(monkeypatch):
    agent = AgentDefinition(name="implementer", tools=[])
    executor, run_manager = make_executor(agent, FakeProvider([]))
    producer_release = asyncio.Event()
    notification_started = asyncio.Event()
    cancellation_swallowed = asyncio.Event()
    notification_release = asyncio.Event()

    async def producer(**kwargs):
        await producer_release.wait()
        await run_manager.finish_run(kwargs["run_id"])

    async def notification(_run_id, *, agent_name):
        assert agent_name == "implementer"
        notification_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_swallowed.set()
            await notification_release.wait()

    monkeypatch.setattr(executor, "_produce", producer)
    monkeypatch.setattr(executor, "_register_task_notification", notification)

    async def run():
        started = await executor.start(
            conversation_id="conv-notification-stubborn",
            agent_name="implementer",
            input_data="inspect",
        )
        await asyncio.wait_for(notification_started.wait(), timeout=1)
        task_name = f"subagent-notification:{started['run_id']}"
        try:
            assert [task.get_name() for task in executor._notification_tasks] == [
                task_name
            ]
            assert await executor.close(timeout=0.01) == (task_name,)
            assert cancellation_swallowed.is_set()

            notification_release.set()
            for _ in range(100):
                if not executor._notification_tasks:
                    break
                await asyncio.sleep(0)
            assert not executor._notification_tasks
            assert await executor.close(timeout=1) == ()
        finally:
            notification_release.set()
            tasks = list(executor._tasks.values())
            producer_release.set()
            await asyncio.gather(*tasks)
            executor._tasks.clear()

    asyncio.run(run())


def test_internal_subagent_start_remains_non_idempotent(monkeypatch):
    agent = AgentDefinition(name="implementer", tools=[])
    executor, _run_manager = make_executor(agent, FakeProvider([]))
    release_producers = asyncio.Event()

    async def fake_produce(**_kwargs):
        await release_producers.wait()

    monkeypatch.setattr(executor, "_produce", fake_produce)

    async def run():
        first = await executor.start(
            conversation_id="conv-1",
            agent_name="implementer",
            input_data="first",
        )
        second = await executor.start(
            conversation_id="conv-1",
            agent_name="implementer",
            input_data="first",
        )

        assert first["run_id"] != second["run_id"]
        release_producers.set()
        await asyncio.gather(*executor._tasks.values())
        executor._tasks.clear()

    asyncio.run(run())


def test_idempotent_subagent_validation_and_configuration_fail_before_reservation():
    agent = AgentDefinition(
        name="schema-agent",
        input_schema={
            "type": "object",
            "required": ["task"],
            "properties": {"task": {"type": "string"}},
        },
    )
    executor, run_manager = make_executor(agent, FakeProvider([]))
    idempotency = RunIdempotency("op_agent", "a" * 64)

    async def run():
        with pytest.raises(RunStartValidationError, match="input_schema"):
            await executor.start_idempotent(
                conversation_id="conv",
                agent_name="schema-agent",
                input_data={"task": 123},
                idempotency=idempotency,
                request_id="req-invalid",
            )
        with pytest.raises(RuntimeError, match="coordinator"):
            await executor.start_idempotent(
                conversation_id="conv",
                agent_name="schema-agent",
                input_data={"task": "valid"},
                idempotency=idempotency,
                request_id="req-unconfigured",
            )
        assert run_manager.list_runs("conv") == []

    asyncio.run(run())


def test_idempotent_subagent_rejects_non_json_input_before_reservation():
    executor, run_manager = make_executor(
        AgentDefinition(name="implementer"),
        FakeProvider([]),
    )
    executor.run_start_coordinator = RunStartCoordinator(run_manager)
    idempotency = RunIdempotency("op_non_json", "c" * 64)

    async def run():
        for invalid_input in (object(), {"value": float("nan")}):
            with pytest.raises(RunStartValidationError, match="finite JSON"):
                await executor.start_idempotent(
                    conversation_id="conv",
                    agent_name="implementer",
                    input_data=invalid_input,
                    idempotency=idempotency,
                    request_id="req-non-json",
                )
        assert run_manager.list_runs("conv") == []

    asyncio.run(run())


def test_agent_runtime_idempotent_start_rejects_blank_input_before_reservation():
    executor, run_manager = make_executor(
        AgentDefinition(name="implementer"),
        FakeProvider([]),
    )
    executor.run_start_coordinator = RunStartCoordinator(run_manager)
    runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=AgentMailbox(),
        subagent_executor=executor,
        capability_registry=executor.capability_registry,
    )

    async def run():
        with pytest.raises(RunStartValidationError, match="input is required"):
            await runtime.spawn_agent_idempotent(
                source=AgentSource(
                    conversation_id="conv",
                    run_id="",
                    run_kind="chat",
                ),
                agent_name="implementer",
                input_data="   ",
                idempotency=RunIdempotency("op_blank", "d" * 64),
                request_id="req-blank",
            )
        assert run_manager.list_runs("conv") == []

    asyncio.run(run())


def test_agent_runtime_idempotent_start_persists_metadata_before_loser_returns(
    monkeypatch,
):
    input_data = {"path": "src", "depth": 2}
    agent = AgentDefinition(
        name="implementer",
        input_schema={
            "type": "object",
            "required": ["path", "depth"],
            "properties": {
                "path": {"type": "string"},
                "depth": {"type": "integer"},
            },
        },
    )
    executor, run_manager = make_executor(agent, FakeProvider([]))
    executor.run_start_coordinator = RunStartCoordinator(run_manager)
    runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=AgentMailbox(),
        subagent_executor=executor,
        capability_registry=executor.capability_registry,
    )
    anchor_entered = asyncio.Event()
    release_anchor = asyncio.Event()
    producer_started = asyncio.Event()
    release_producer = asyncio.Event()
    produced_inputs = []

    async def blocking_anchor(_run):
        anchor_entered.set()
        await release_anchor.wait()
        return None

    async def losing_anchor(_run):
        raise AssertionError("loser must not invoke winner anchor factory")

    async def fake_produce(**kwargs):
        produced_inputs.append(kwargs["input_data"])
        producer_started.set()
        await release_producer.wait()

    monkeypatch.setattr(executor, "_produce", fake_produce)
    idempotency = RunIdempotency("op_runtime", "b" * 64)
    source = AgentSource(
        conversation_id="conv-1",
        run_id="   ",
        run_kind="chat",
        anchor_node_id=None,
        root_run_id=None,
    )

    async def run():
        winner = asyncio.create_task(
            runtime.spawn_agent_idempotent(
                source=source,
                agent_name="implementer",
                input_data=input_data,
                idempotency=idempotency,
                request_id="req-runtime-winner",
                winner_anchor_factory=blocking_anchor,
            )
        )
        await asyncio.wait_for(anchor_entered.wait(), timeout=1)
        loser = asyncio.create_task(
            runtime.spawn_agent_idempotent(
                source=source,
                agent_name="implementer",
                input_data=input_data,
                idempotency=idempotency,
                request_id="req-runtime-loser",
                winner_anchor_factory=losing_anchor,
            )
        )
        await asyncio.sleep(0)
        assert not loser.done()

        winner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await winner
        release_anchor.set()
        result = await asyncio.wait_for(loser, timeout=1)
        await asyncio.wait_for(producer_started.wait(), timeout=1)

        persisted = run_manager.get_run(result.run.run_id)
        assert persisted is not None
        assert persisted["created_by_run_id"] is None
        assert persisted["metadata"]["agent_name"] == "implementer"
        assert persisted["metadata"]["context_mode"] == "fresh"
        assert persisted["metadata"]["delivery_policy"] == "auto"
        assert persisted["metadata"]["source_run_id"] is None
        assert persisted["metadata"]["root_run_id"] is None
        assert isinstance(persisted["metadata"]["task"], str)
        assert "src" in persisted["metadata"]["task"]
        assert persisted["metadata"]["delegated_task"] == input_data
        assert produced_inputs == [input_data]
        assert produced_inputs[0] is input_data

        release_producer.set()
        await asyncio.gather(*executor._tasks.values())
        executor._tasks.clear()

    asyncio.run(run())


def test_agent_runtime_task_bound_idempotent_start_replays_active_and_finished(
    monkeypatch,
):
    executor, run_manager = make_executor(
        AgentDefinition(name="implementer"),
        FakeProvider([]),
    )
    executor.run_start_coordinator = RunStartCoordinator(run_manager)
    task_service = ActiveTaskService(run_manager=run_manager)
    run_manager.task_service = task_service
    runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=AgentMailbox(),
        subagent_executor=executor,
        capability_registry=executor.capability_registry,
        task_service=task_service,
    )
    producer_started = asyncio.Event()
    release_producer = asyncio.Event()

    async def fake_produce(**kwargs):
        producer_started.set()
        await release_producer.wait()
        await run_manager.finish_run(kwargs["run_id"])

    monkeypatch.setattr(executor, "_produce", fake_produce)
    idempotency = RunIdempotency("op-task-bound", "6" * 64)
    source = AgentSource(
        conversation_id="conv-task-bound",
        run_id="",
        run_kind="chat",
    )

    async def run():
        task = await task_service.create_task(
            conversation_id=source.conversation_id,
            title="Bound task",
            steps=[{"title": "Implement"}],
        )
        start_kwargs = {
            "source": source,
            "agent_name": "implementer",
            "input_data": "implement",
            "idempotency": idempotency,
            "step": 1,
            "task_generation_id": task.generation_id,
            "task_revision": task.revision,
        }

        winner = await runtime.spawn_agent_idempotent(
            **start_kwargs,
            request_id="request-task-bound-winner",
        )
        await asyncio.wait_for(producer_started.wait(), timeout=1)

        active_replay = await runtime.spawn_agent_idempotent(
            **start_kwargs,
            request_id="request-task-bound-active-replay",
        )
        assert active_replay.created is False
        assert active_replay.run.run_id == winner.run.run_id

        with pytest.raises(
            ActiveTaskConflictError,
            match="active task already has a running step",
        ):
            await runtime.spawn_agent_idempotent(
                **{
                    **start_kwargs,
                    "idempotency": RunIdempotency("op-task-bound-new", "7" * 64),
                },
                request_id="request-task-bound-new-key",
            )

        producer_tasks = list(executor._tasks.values())
        release_producer.set()
        await asyncio.gather(*producer_tasks)

        finished_replay = await runtime.spawn_agent_idempotent(
            **start_kwargs,
            request_id="request-task-bound-finished-replay",
        )
        assert finished_replay.created is False
        assert finished_replay.run.run_id == winner.run.run_id
        assert finished_replay.run.status.value == "completed"
        assert len(run_manager.list_runs(source.conversation_id)) == 1

    asyncio.run(run())


def test_agent_runtime_task_binding_race_replays_after_preflight_conflict(
    monkeypatch,
):
    executor, run_manager = make_executor(
        AgentDefinition(name="implementer"),
        FakeProvider([]),
    )
    executor.run_start_coordinator = RunStartCoordinator(run_manager)
    task_service = ActiveTaskService(run_manager=run_manager)
    run_manager.task_service = task_service
    winner_runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=AgentMailbox(),
        subagent_executor=executor,
        capability_registry=executor.capability_registry,
        task_service=task_service,
    )
    replay_runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=AgentMailbox(),
        subagent_executor=executor,
        capability_registry=executor.capability_registry,
        task_service=task_service,
    )
    binding_preflight_entered = asyncio.Event()
    release_binding_preflight = asyncio.Event()
    release_producer = asyncio.Event()
    original_prepare_task_binding = replay_runtime._prepare_task_binding

    async def blocking_prepare_task_binding(**kwargs):
        binding_preflight_entered.set()
        await release_binding_preflight.wait()
        return await original_prepare_task_binding(**kwargs)

    async def fake_produce(**kwargs):
        await release_producer.wait()
        await run_manager.finish_run(kwargs["run_id"])

    monkeypatch.setattr(
        replay_runtime,
        "_prepare_task_binding",
        blocking_prepare_task_binding,
    )
    monkeypatch.setattr(executor, "_produce", fake_produce)
    idempotency = RunIdempotency("op-task-binding-race", "8" * 64)
    source = AgentSource(
        conversation_id="conv-task-binding-race",
        run_id="",
        run_kind="chat",
    )

    async def run():
        task = await task_service.create_task(
            conversation_id=source.conversation_id,
            title="Racing task",
            steps=[{"title": "Implement"}],
        )
        start_kwargs = {
            "source": source,
            "agent_name": "implementer",
            "input_data": "implement",
            "idempotency": idempotency,
            "step": 1,
            "task_generation_id": task.generation_id,
            "task_revision": task.revision,
        }

        replay_task = asyncio.create_task(
            replay_runtime.spawn_agent_idempotent(
                **start_kwargs,
                request_id="request-task-binding-race-replay",
            )
        )
        await asyncio.wait_for(binding_preflight_entered.wait(), timeout=1)

        winner = await winner_runtime.spawn_agent_idempotent(
            **start_kwargs,
            request_id="request-task-binding-race-winner",
        )
        release_binding_preflight.set()
        replay = await asyncio.wait_for(replay_task, timeout=1)

        assert winner.created is True
        assert replay.created is False
        assert replay.run.run_id == winner.run.run_id
        assert len(run_manager.list_runs(source.conversation_id)) == 1

        producer_tasks = list(executor._tasks.values())
        release_producer.set()
        await asyncio.gather(*producer_tasks)

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
    agent = AgentDefinition(name="limited", max_turns=1, tools=["read"])
    provider = FakeProvider([
        [
            {
                "status": StreamStatus.COMPLETE,
                "tool_calls": [
                    {"id": "call-1", "function": {"name": "read"}},
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
