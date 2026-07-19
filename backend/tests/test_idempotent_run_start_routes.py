from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.errors import RequestBoundaryMiddleware, install_error_handlers
from backend.api.routes import agents as agent_routes
from backend.api.routes import workflows as workflow_routes
from backend.core.agents import AgentMailbox, AgentRuntime, SubagentExecutor
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import AgentDefinition
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.runs import (
    RunManager,
    RunStartCoordinator,
    RunStatus,
    fingerprint_run_request,
)
from backend.core.workflows import WorkflowManager
from backend.core.workflows.js_runner import WorkflowJsRunner


VALID_WORKFLOW = (
    "export default async function workflow(ctx) { return ctx.args ?? 1; }"
)
OTHER_WORKFLOW = (
    "export default async function workflow(ctx) { return ctx.args ?? 2; }"
)
DEFAULT_BUDGET = {
    "max_seconds": 600,
    "max_host_calls": 200,
    "max_parallel": 8,
}


class _FakeChatManager:
    def get_conversation(self, _conversation_id: str) -> Any:
        return SimpleNamespace(metadata={})


@dataclass
class _RouteHarness:
    app: FastAPI
    persistence: SQLitePersistence
    chat_repository: ChatRepository
    run_repository: SQLiteRunRepository
    run_manager: RunManager
    coordinator: RunStartCoordinator
    executor: SubagentExecutor
    agent_runtime: AgentRuntime
    workflow_manager: WorkflowManager
    conversation_id: str
    node_id: str
    alternate_node_id: str
    other_conversation_id: str
    other_node_id: str
    agent_calls: list[dict[str, Any]]
    workflow_calls: list[dict[str, Any]]
    agent_started: asyncio.Event
    workflow_started: asyncio.Event
    agent_release: asyncio.Event
    workflow_release: asyncio.Event

    async def close(self) -> None:
        self.agent_release.set()
        self.workflow_release.set()
        await asyncio.sleep(0)
        await self.coordinator.close(timeout=1)
        await self.coordinator.producer_registry.close(timeout=1)
        await self.run_manager.close()


def _route_harness(tmp_path: Path) -> _RouteHarness:
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat_repository = ChatRepository(persistence)
    run_repository = SQLiteRunRepository(persistence)
    conversation_id = chat_repository.create_conversation(title="Route contracts")
    node_id = chat_repository.create_node(conversation_id, parent_id=None)
    alternate_node_id = chat_repository.create_node(conversation_id, parent_id=node_id)
    other_conversation_id = chat_repository.create_conversation(title="Other")
    other_node_id = chat_repository.create_node(other_conversation_id, parent_id=None)

    run_manager = RunManager(repository=run_repository)
    coordinator = RunStartCoordinator(run_manager)
    registry = CapabilityRegistry()
    registry.add_agents(
        [
            AgentDefinition(name="explorer", tools=[]),
            AgentDefinition(name="reviewer", tools=[]),
            AgentDefinition(
                name="structured",
                tools=[],
                input_schema={
                    "type": "object",
                    "required": ["path", "depth"],
                    "properties": {
                        "path": {"type": "string"},
                        "depth": {"type": "integer"},
                    },
                },
            ),
        ]
    )
    mailbox = AgentMailbox()
    executor = SubagentExecutor(
        chat_manager=_FakeChatManager(),
        run_manager=run_manager,
        capability_registry=registry,
        mailbox=mailbox,
        run_start_coordinator=coordinator,
    )
    workflow_manager = WorkflowManager(
        run_manager=run_manager,
        subagent_executor=executor,
        runner=WorkflowJsRunner(),
        mailbox=mailbox,
        run_start_coordinator=coordinator,
    )
    agent_runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=mailbox,
        subagent_executor=executor,
        workflow_manager=workflow_manager,
        capability_registry=registry,
    )
    workflow_manager.agent_runtime = agent_runtime

    agent_calls: list[dict[str, Any]] = []
    workflow_calls: list[dict[str, Any]] = []
    agent_started = asyncio.Event()
    workflow_started = asyncio.Event()
    agent_release = asyncio.Event()
    workflow_release = asyncio.Event()

    async def fake_agent_produce(**kwargs: Any) -> None:
        agent_calls.append(dict(kwargs))
        agent_started.set()
        try:
            await agent_release.wait()
            await run_manager.finish_run(kwargs["run_id"], RunStatus.COMPLETED)
        finally:
            executor._tasks.pop(kwargs["run_id"], None)

    async def fake_workflow_produce(**kwargs: Any) -> None:
        workflow_calls.append(dict(kwargs))
        workflow_started.set()
        try:
            await workflow_release.wait()
            await run_manager.finish_run(kwargs["run_id"], RunStatus.COMPLETED)
        finally:
            workflow_manager._tasks.pop(kwargs["run_id"], None)

    executor._produce = fake_agent_produce  # type: ignore[method-assign]
    workflow_manager._produce = fake_workflow_produce  # type: ignore[method-assign]

    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestBoundaryMiddleware)
    app.include_router(agent_routes.router, prefix="/api/v1")
    app.include_router(workflow_routes.router, prefix="/api/v1")
    app.state.run_manager = run_manager
    app.state.run_start_coordinator = coordinator
    app.state.agent_runtime = agent_runtime
    app.state.workflow_manager = workflow_manager

    return _RouteHarness(
        app=app,
        persistence=persistence,
        chat_repository=chat_repository,
        run_repository=run_repository,
        run_manager=run_manager,
        coordinator=coordinator,
        executor=executor,
        agent_runtime=agent_runtime,
        workflow_manager=workflow_manager,
        conversation_id=conversation_id,
        node_id=node_id,
        alternate_node_id=alternate_node_id,
        other_conversation_id=other_conversation_id,
        other_node_id=other_node_id,
        agent_calls=agent_calls,
        workflow_calls=workflow_calls,
        agent_started=agent_started,
        workflow_started=workflow_started,
        agent_release=agent_release,
        workflow_release=workflow_release,
    )


async def _post(
    harness: _RouteHarness,
    path: str,
    *,
    body: dict[str, Any],
    key: str | None,
):
    headers = {"Idempotency-Key": key} if key is not None else None
    transport = ASGITransport(app=harness.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, json=body, headers=headers)


def _run_counts(harness: _RouteHarness) -> tuple[int, int]:
    with harness.persistence.connect() as connection:
        run_count = int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        event_count = int(
            connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
        )
    return run_count, event_count


def test_agent_start_requires_key_and_returns_minimal_replay_response(tmp_path):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        path = (
            f"/api/v1/conversations/{harness.conversation_id}"
            "/agents/explorer/runs"
        )
        body = {"input": "inspect", "parent_node_id": harness.node_id}
        try:
            missing = await _post(harness, path, body=body, key=None)
            created = await _post(harness, path, body=body, key="op_agent_route")
            repeated = await _post(harness, path, body=body, key="op_agent_route")
            await asyncio.wait_for(harness.agent_started.wait(), timeout=1)

            assert missing.status_code == 428
            assert missing.json()["error"]["code"] == "idempotency_key_required"
            assert created.status_code == 202
            assert repeated.status_code == 200
            assert created.json()["run_id"] == repeated.json()["run_id"]
            assert set(created.json()) == {"run_id", "created", "status"}
            assert created.json()["created"] is True
            assert repeated.json()["created"] is False
            assert len(harness.agent_calls) == 1
        finally:
            await harness.close()

    asyncio.run(case())


def test_agent_start_rejects_malformed_and_duplicate_keys_before_reservation(
    tmp_path,
):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        path = (
            f"/api/v1/conversations/{harness.conversation_id}"
            "/agents/explorer/runs"
        )
        body = {"input": "inspect", "parent_node_id": harness.node_id}
        transport = ASGITransport(app=harness.app, raise_app_exceptions=False)
        try:
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                malformed = await client.post(
                    path,
                    json=body,
                    headers={"Idempotency-Key": "bad key"},
                )
                duplicate = await client.post(
                    path,
                    json=body,
                    headers=[
                        ("Idempotency-Key", "op_duplicate"),
                        ("Idempotency-Key", "op_duplicate"),
                    ],
                )

            assert malformed.status_code == 422
            assert malformed.json()["error"]["code"] == "invalid_idempotency_key"
            assert duplicate.status_code == 422
            assert duplicate.json()["error"]["code"] == "invalid_idempotency_key"
            assert _run_counts(harness) == (0, 0)
            assert harness.agent_calls == []
        finally:
            await harness.close()

    asyncio.run(case())


def test_structured_agent_input_and_anchor_cross_real_sqlite_boundary(tmp_path):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        path = (
            f"/api/v1/conversations/{harness.conversation_id}"
            "/agents/structured/runs"
        )
        body = {
            "input": {"path": "src", "depth": 2},
            "parent_node_id": harness.node_id,
        }
        try:
            response = await _post(harness, path, body=body, key="op_structured")
            await asyncio.wait_for(harness.agent_started.wait(), timeout=1)

            assert response.status_code == 202
            stored = harness.run_repository.get_run(response.json()["run_id"])
            assert stored is not None
            assert stored["anchor_node_id"] == harness.node_id
            assert stored["created_by_run_id"] is None
            assert harness.agent_calls[0]["input_data"] == body["input"]
            assert not isinstance(harness.agent_calls[0]["input_data"], str)
            expected_request = agent_routes.StartSubagentRequest(
                **body
            ).model_dump(mode="json")
            assert stored["request_fingerprint"] == fingerprint_run_request(
                operation="agent",
                conversation_id=harness.conversation_id,
                anchor_node_id=harness.node_id,
                payload={
                    "agent_name": "structured",
                    "request": expected_request,
                },
            )
        finally:
            await harness.close()

    asyncio.run(case())


def test_idempotency_key_conflict_is_global_between_agent_and_workflow(tmp_path):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        key = "op_cross_route"
        try:
            agent = await _post(
                harness,
                f"/api/v1/conversations/{harness.conversation_id}/agents/explorer/runs",
                body={"input": "inspect", "parent_node_id": harness.node_id},
                key=key,
            )
            await asyncio.wait_for(harness.agent_started.wait(), timeout=1)
            workflow = await _post(
                harness,
                f"/api/v1/conversations/{harness.conversation_id}/workflows/runs",
                body={"script": VALID_WORKFLOW, "parent_node_id": harness.node_id},
                key=key,
            )

            assert agent.status_code == 202
            assert workflow.status_code == 409
            error = workflow.json()["error"]
            assert error["code"] == "idempotency_key_conflict"
            assert error["details"]["existing_run_id"] == agent.json()["run_id"]
            assert harness.workflow_calls == []
        finally:
            await harness.close()

    asyncio.run(case())


@pytest.mark.parametrize(
    ("agent_name", "body_update"),
    [
        ("reviewer", {}),
        ("explorer", {"input": "changed"}),
        ("explorer", {"parent_node_id": "alternate"}),
        ("explorer", {"provider_id": "provider-2"}),
        ("explorer", {"model_id": "model-2"}),
        ("explorer", {"permission_mode": "plan"}),
        ("explorer", {"workspace": {"root": "D:/other"}}),
    ],
)
def test_changed_agent_request_conflicts(
    tmp_path,
    agent_name: str,
    body_update: dict[str, Any],
):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        base = {"input": "inspect", "parent_node_id": harness.node_id}
        changed = {**base, **body_update}
        if changed.get("parent_node_id") == "alternate":
            changed["parent_node_id"] = harness.alternate_node_id
        key = "op_agent_changed"
        try:
            first = await _post(
                harness,
                f"/api/v1/conversations/{harness.conversation_id}/agents/explorer/runs",
                body=base,
                key=key,
            )
            await asyncio.wait_for(harness.agent_started.wait(), timeout=1)
            second = await _post(
                harness,
                f"/api/v1/conversations/{harness.conversation_id}"
                f"/agents/{agent_name}/runs",
                body=changed,
                key=key,
            )
            assert first.status_code == 202
            assert second.status_code == 409
            assert second.json()["error"]["code"] == "idempotency_key_conflict"
            assert len(harness.agent_calls) == 1
        finally:
            await harness.close()

    asyncio.run(case())


def test_invalid_agent_schema_and_workflow_script_reserve_nothing(tmp_path):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        try:
            invalid_agent = await _post(
                harness,
                f"/api/v1/conversations/{harness.conversation_id}/agents/structured/runs",
                body={"input": "src", "parent_node_id": harness.node_id},
                key="op_bad_agent",
            )
            invalid_workflow = await _post(
                harness,
                f"/api/v1/conversations/{harness.conversation_id}/workflows/runs",
                body={"script": "return 1", "parent_node_id": harness.node_id},
                key="op_bad_workflow",
            )

            assert invalid_agent.status_code == 422
            assert invalid_agent.json()["error"]["code"] == "invalid_request"
            assert invalid_workflow.status_code == 422
            assert invalid_workflow.json()["error"]["code"] == "invalid_request"
            assert _run_counts(harness) == (0, 0)
            assert harness.agent_calls == []
            assert harness.workflow_calls == []
        finally:
            await harness.close()

    asyncio.run(case())


def test_missing_agent_is_a_typed_404_without_reservation(tmp_path):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        try:
            response = await _post(
                harness,
                f"/api/v1/conversations/{harness.conversation_id}/agents/missing/runs",
                body={"input": "inspect", "parent_node_id": harness.node_id},
                key="op_missing_agent",
            )
            assert response.status_code == 404
            error = response.json()["error"]
            assert error["code"] == "agent_not_found"
            assert error["details"] == {"agent_name": "missing"}
            assert _run_counts(harness) == (0, 0)
            assert harness.agent_calls == []
        finally:
            await harness.close()

    asyncio.run(case())


def test_blank_agent_input_is_typed_and_reserves_nothing(tmp_path):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        try:
            response = await _post(
                harness,
                f"/api/v1/conversations/{harness.conversation_id}/agents/explorer/runs",
                body={"input": "   ", "parent_node_id": harness.node_id},
                key="op_blank_agent_input",
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_request"
            assert _run_counts(harness) == (0, 0)
            assert harness.agent_calls == []
        finally:
            await harness.close()

    asyncio.run(case())


def test_unrelated_key_error_uses_unified_500_instead_of_agent_404(tmp_path):
    async def case() -> None:
        harness = _route_harness(tmp_path)

        async def fail_start(**_kwargs: Any):
            raise KeyError("private_internal_field")

        harness.agent_runtime.spawn_agent_idempotent = fail_start  # type: ignore[method-assign]
        try:
            response = await _post(
                harness,
                f"/api/v1/conversations/{harness.conversation_id}/agents/explorer/runs",
                body={"input": "inspect", "parent_node_id": harness.node_id},
                key="op_internal_key_error",
            )
            assert response.status_code == 500
            error = response.json()["error"]
            assert error["code"] == "internal_error"
            assert "private_internal_field" not in str(response.json())
            assert _run_counts(harness) == (0, 0)
        finally:
            await harness.close()

    asyncio.run(case())


@pytest.mark.parametrize("field", tuple(DEFAULT_BUDGET))
@pytest.mark.parametrize("value", ["x", True, 0, -1, 2_147_483_648, 1.5])
def test_invalid_workflow_budget_is_typed_and_reserves_nothing(
    tmp_path,
    field: str,
    value: Any,
):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        try:
            response = await _post(
                harness,
                f"/api/v1/conversations/{harness.conversation_id}/workflows/runs",
                body={
                    "script": VALID_WORKFLOW,
                    "parent_node_id": harness.node_id,
                    "budget": {field: value},
                },
                key=f"op_budget_{field}",
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_request"
            assert _run_counts(harness) == (0, 0)
            assert harness.workflow_calls == []
        finally:
            await harness.close()

    asyncio.run(case())


def test_workflow_budget_default_forms_replay_the_same_run(tmp_path):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        path = f"/api/v1/conversations/{harness.conversation_id}/workflows/runs"
        base = {"script": VALID_WORKFLOW, "parent_node_id": harness.node_id}
        key = "op_budget_defaults"
        try:
            omitted = await _post(harness, path, body=base, key=key)
            empty = await _post(harness, path, body={**base, "budget": {}}, key=key)
            explicit = await _post(
                harness,
                path,
                body={**base, "budget": DEFAULT_BUDGET},
                key=key,
            )
            await asyncio.wait_for(harness.workflow_started.wait(), timeout=1)
            assert [omitted.status_code, empty.status_code, explicit.status_code] == [
                202,
                200,
                200,
            ]
            assert len({item.json()["run_id"] for item in (omitted, empty, explicit)}) == 1
            assert len(harness.workflow_calls) == 1
        finally:
            await harness.close()

    asyncio.run(case())


@pytest.mark.parametrize(
    "body_update",
    [
        {"script": OTHER_WORKFLOW},
        {"budget": {"max_seconds": 601}},
        {"parent_node_id": "alternate"},
    ],
)
def test_changed_workflow_request_conflicts(tmp_path, body_update: dict[str, Any]):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        path = f"/api/v1/conversations/{harness.conversation_id}/workflows/runs"
        base = {"script": VALID_WORKFLOW, "parent_node_id": harness.node_id}
        changed = {**base, **body_update}
        if changed.get("parent_node_id") == "alternate":
            changed["parent_node_id"] = harness.alternate_node_id
        try:
            first = await _post(harness, path, body=base, key="op_workflow_changed")
            await asyncio.wait_for(harness.workflow_started.wait(), timeout=1)
            second = await _post(
                harness,
                path,
                body=changed,
                key="op_workflow_changed",
            )
            assert first.status_code == 202
            assert second.status_code == 409
            assert second.json()["error"]["code"] == "idempotency_key_conflict"
            assert len(harness.workflow_calls) == 1
        finally:
            await harness.close()

    asyncio.run(case())


def test_concurrent_agent_replay_waits_for_winner_scheduling(tmp_path):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        path = (
            f"/api/v1/conversations/{harness.conversation_id}"
            "/agents/explorer/runs"
        )
        body = {"input": "inspect", "parent_node_id": harness.node_id}
        schedule_entered = asyncio.Event()
        release_schedule = asyncio.Event()
        schedule_calls = 0
        original_schedule = harness.executor.schedule_existing

        async def blocked_schedule(**kwargs: Any):
            nonlocal schedule_calls
            schedule_calls += 1
            schedule_entered.set()
            await release_schedule.wait()
            return await original_schedule(**kwargs)

        harness.executor.schedule_existing = blocked_schedule  # type: ignore[method-assign]
        try:
            first_task = asyncio.create_task(
                _post(harness, path, body=body, key="op_agent_concurrent")
            )
            await asyncio.wait_for(schedule_entered.wait(), timeout=1)
            second_task = asyncio.create_task(
                _post(harness, path, body=body, key="op_agent_concurrent")
            )
            await asyncio.sleep(0.05)
            assert not first_task.done()
            assert not second_task.done()

            release_schedule.set()
            first, second = await asyncio.wait_for(
                asyncio.gather(first_task, second_task),
                timeout=1,
            )
            await asyncio.wait_for(harness.agent_started.wait(), timeout=1)

            assert {first.status_code, second.status_code} == {200, 202}
            assert first.json()["run_id"] == second.json()["run_id"]
            assert {first.json()["created"], second.json()["created"]} == {
                True,
                False,
            }
            assert schedule_calls == 1
            assert len(harness.agent_calls) == 1
        finally:
            release_schedule.set()
            await harness.close()

    asyncio.run(case())


@pytest.mark.parametrize(
    ("conversation", "body", "status", "code", "reference_kind"),
    [
        ("missing", {"input": "inspect"}, 404, "run_reference_not_found", "conversation_id"),
        ("node", {"input": "inspect"}, 422, "invalid_run_reference", "conversation_id"),
        (
            "valid",
            {"input": "inspect", "parent_node_id": "missing-node"},
            404,
            "run_reference_not_found",
            "anchor_node_id",
        ),
        (
            "valid",
            {"input": "inspect", "parent_node_id": "other-node"},
            422,
            "invalid_run_reference",
            "anchor_node_id",
        ),
        (
            "valid",
            {"input": "inspect", "parent_node_id": "conversation-id"},
            422,
            "invalid_run_reference",
            "anchor_node_id",
        ),
        (
            "valid",
            {"input": "inspect", "created_by_run_id": "missing-run"},
            404,
            "run_reference_not_found",
            "created_by_run_id",
        ),
        (
            "valid",
            {"input": "inspect", "cancellation_parent_run_id": "missing-run"},
            404,
            "run_reference_not_found",
            "cancellation_parent_run_id",
        ),
        (
            "valid",
            {"input": "inspect", "created_by_run_id": "node-id"},
            422,
            "invalid_run_reference",
            "created_by_run_id",
        ),
        (
            "valid",
            {"input": "inspect", "cancellation_parent_run_id": "node-id"},
            422,
            "invalid_run_reference",
            "cancellation_parent_run_id",
        ),
    ],
)
def test_agent_reference_errors_are_typed_before_reservation(
    tmp_path,
    conversation: str,
    body: dict[str, Any],
    status: int,
    code: str,
    reference_kind: str,
):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        conversation_id = {
            "missing": "missing-conversation",
            "node": harness.node_id,
            "valid": harness.conversation_id,
        }[conversation]
        request_body = dict(body)
        replacements = {
            "other-node": harness.other_node_id,
            "conversation-id": harness.conversation_id,
            "node-id": harness.node_id,
        }
        for field in (
            "parent_node_id",
            "created_by_run_id",
            "cancellation_parent_run_id",
        ):
            value = request_body.get(field)
            if value in replacements:
                request_body[field] = replacements[value]
        try:
            response = await _post(
                harness,
                f"/api/v1/conversations/{conversation_id}/agents/explorer/runs",
                body=request_body,
                key=f"op_ref_{reference_kind}",
            )
            assert response.status_code == status
            assert response.json()["error"]["code"] == code
            assert response.json()["error"]["details"]["reference_kind"] == reference_kind
            assert _run_counts(harness) == (0, 0)
            assert harness.agent_calls == []
        finally:
            await harness.close()

    asyncio.run(case())


@pytest.mark.parametrize(
    ("conversation", "parent", "status", "code", "reference_kind"),
    [
        ("missing", "valid", 404, "run_reference_not_found", "conversation_id"),
        ("node", "valid", 422, "invalid_run_reference", "conversation_id"),
        ("valid", "other", 422, "invalid_run_reference", "anchor_node_id"),
        ("valid", "conversation", 422, "invalid_run_reference", "anchor_node_id"),
    ],
)
def test_workflow_reference_errors_are_typed_before_reservation(
    tmp_path,
    conversation: str,
    parent: str,
    status: int,
    code: str,
    reference_kind: str,
):
    async def case() -> None:
        harness = _route_harness(tmp_path)
        conversation_id = {
            "missing": "missing-conversation",
            "node": harness.node_id,
            "valid": harness.conversation_id,
        }[conversation]
        parent_node_id = {
            "other": harness.other_node_id,
            "conversation": harness.conversation_id,
            "valid": harness.node_id,
        }[parent]
        try:
            response = await _post(
                harness,
                f"/api/v1/conversations/{conversation_id}/workflows/runs",
                body={
                    "script": VALID_WORKFLOW,
                    "parent_node_id": parent_node_id,
                },
                key=f"op_workflow_ref_{conversation}_{parent}",
            )
            assert response.status_code == status
            assert response.json()["error"]["code"] == code
            assert response.json()["error"]["details"]["reference_kind"] == reference_kind
            assert _run_counts(harness) == (0, 0)
            assert harness.workflow_calls == []
        finally:
            await harness.close()

    asyncio.run(case())
