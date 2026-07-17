from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

import main
from backend.api import dependencies
from backend.core.server import ServerHomeInUseError, ServerHomeLock


def _request(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 123),
            "scheme": "http",
            "root_path": "",
            "http_version": "1.1",
        }
    )


def test_run_start_coordinator_dependency_rejects_missing_and_none():
    missing_app = FastAPI()
    with pytest.raises(HTTPException) as missing:
        dependencies.get_run_start_coordinator(_request(missing_app))
    assert missing.value.status_code == 500

    cleared_app = FastAPI()
    cleared_app.state.run_start_coordinator = None
    with pytest.raises(HTTPException) as cleared:
        dependencies.get_run_start_coordinator(_request(cleared_app))
    assert cleared.value.status_code == 500

    coordinator = object()
    ready_app = FastAPI()
    ready_app.state.run_start_coordinator = coordinator
    assert dependencies.get_run_start_coordinator(_request(ready_app)) is coordinator


def test_shutdown_closes_background_owners_before_manager_and_tools(monkeypatch):
    order: list[str] = []

    class Coordinator:
        async def close(self):
            order.append("coordinator")
            return SimpleNamespace(exhausted=False)

    class RunManager:
        async def close(self):
            order.append("run_manager")
            return SimpleNamespace(exhausted_run_ids=())

    class WorkflowManager:
        async def close(self):
            order.append("workflow_manager")
            return ()

    class SubagentExecutor:
        async def close(self):
            order.append("subagent_executor")
            return ()

    class ToolManager:
        async def close(self):
            order.append("tool_manager")

    coordinator = Coordinator()
    workflow_manager = WorkflowManager()
    subagent_executor = SubagentExecutor()
    run_manager = RunManager()
    tool_manager = ToolManager()
    monkeypatch.setattr(
        main.app.state,
        "run_start_coordinator",
        coordinator,
        raising=False,
    )
    monkeypatch.setattr(
        main.app.state,
        "workflow_manager",
        workflow_manager,
        raising=False,
    )
    monkeypatch.setattr(
        main.app.state,
        "subagent_executor",
        subagent_executor,
        raising=False,
    )
    monkeypatch.setattr(main.app.state, "run_manager", run_manager, raising=False)
    monkeypatch.setattr(main.app.state, "tool_manager", tool_manager, raising=False)
    monkeypatch.setattr(main.app.state, "server_home_lock", None, raising=False)

    asyncio.run(main.shutdown_event())

    assert order == [
        "coordinator",
        "workflow_manager",
        "subagent_executor",
        "run_manager",
        "tool_manager",
    ]
    assert main.app.state.run_start_coordinator is None
    assert main.app.state.workflow_manager is None
    assert main.app.state.subagent_executor is None
    assert main.app.state.run_manager is None
    assert main.app.state.tool_manager is None


def test_shutdown_stops_before_run_manager_when_notification_drain_exhausts(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    order: list[str] = []

    class Coordinator:
        async def close(self):
            order.append("coordinator")
            return SimpleNamespace(exhausted=False)

    class WorkflowManager:
        async def close(self):
            order.append("workflow_manager")
            return ("workflow-notification:run-1",)

    class SubagentExecutor:
        async def close(self):
            order.append("subagent_executor")
            return ()

    class RunManager:
        async def close(self):
            order.append("run_manager")
            return SimpleNamespace(exhausted_run_ids=())

    class ToolManager:
        async def close(self):
            order.append("tool_manager")

    coordinator = Coordinator()
    workflow_manager = WorkflowManager()
    subagent_executor = SubagentExecutor()
    run_manager = RunManager()
    tool_manager = ToolManager()
    home_lock = ServerHomeLock(tmp_path)
    home_lock.acquire()
    monkeypatch.setattr(
        main.app.state,
        "run_start_coordinator",
        coordinator,
        raising=False,
    )
    monkeypatch.setattr(
        main.app.state,
        "workflow_manager",
        workflow_manager,
        raising=False,
    )
    monkeypatch.setattr(
        main.app.state,
        "subagent_executor",
        subagent_executor,
        raising=False,
    )
    monkeypatch.setattr(main.app.state, "run_manager", run_manager, raising=False)
    monkeypatch.setattr(main.app.state, "tool_manager", tool_manager, raising=False)
    monkeypatch.setattr(main.app.state, "server_home_lock", home_lock, raising=False)

    with pytest.raises(RuntimeError, match="workflow_manager drain incomplete"):
        asyncio.run(main.shutdown_event())

    assert order == ["coordinator", "workflow_manager"]
    assert main.app.state.run_start_coordinator is None
    assert main.app.state.workflow_manager is workflow_manager
    assert main.app.state.subagent_executor is subagent_executor
    assert main.app.state.run_manager is run_manager
    assert main.app.state.tool_manager is tool_manager
    assert main.app.state.server_home_lock is home_lock
    try:
        with pytest.raises(ServerHomeInUseError):
            with ServerHomeLock(tmp_path):
                pass
    finally:
        home_lock.release()
        main.app.state.server_home_lock = None


def test_shutdown_retains_home_lock_when_internal_producer_drain_exhausts(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    order: list[str] = []

    class Resource:
        def __init__(self, name, report=None):
            self.name = name
            self.report = report

        async def close(self):
            order.append(self.name)
            return self.report

    coordinator = Resource("coordinator", SimpleNamespace(exhausted=False))
    workflow_manager = Resource("workflow_manager", ())
    subagent_executor = Resource(
        "subagent_executor",
        ("subagent-producer:run-1",),
    )
    run_manager = Resource("run_manager", SimpleNamespace(exhausted_run_ids=()))
    tool_manager = Resource("tool_manager")
    home_lock = ServerHomeLock(tmp_path)
    home_lock.acquire()
    monkeypatch.setattr(
        main.app.state,
        "run_start_coordinator",
        coordinator,
        raising=False,
    )
    monkeypatch.setattr(
        main.app.state,
        "workflow_manager",
        workflow_manager,
        raising=False,
    )
    monkeypatch.setattr(
        main.app.state,
        "subagent_executor",
        subagent_executor,
        raising=False,
    )
    monkeypatch.setattr(main.app.state, "run_manager", run_manager, raising=False)
    monkeypatch.setattr(main.app.state, "tool_manager", tool_manager, raising=False)
    monkeypatch.setattr(main.app.state, "server_home_lock", home_lock, raising=False)

    with pytest.raises(RuntimeError, match="subagent_executor drain incomplete"):
        asyncio.run(main.shutdown_event())

    assert order == ["coordinator", "workflow_manager", "subagent_executor"]
    assert main.app.state.run_start_coordinator is None
    assert main.app.state.workflow_manager is None
    assert main.app.state.subagent_executor is subagent_executor
    assert main.app.state.run_manager is run_manager
    assert main.app.state.tool_manager is tool_manager
    assert main.app.state.server_home_lock is home_lock
    try:
        with pytest.raises(ServerHomeInUseError):
            with ServerHomeLock(tmp_path):
                pass
    finally:
        home_lock.release()
        main.app.state.server_home_lock = None


def test_startup_rollback_after_coordinator_construction_uses_shutdown_order(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    order: list[str] = []
    constructed_with: list[object] = []

    class Coordinator:
        def __init__(self, run_manager):
            constructed_with.append(run_manager)

        async def close(self):
            order.append("coordinator")
            return SimpleNamespace(exhausted=False)

    class RunManager:
        async def close(self):
            order.append("run_manager")
            return SimpleNamespace(exhausted_run_ids=())

    class ToolManager:
        async def close(self):
            order.append("tool_manager")

    async def fail_after_coordinator_construction():
        run_manager = RunManager()
        main.app.state.run_manager = run_manager
        main.app.state.run_start_coordinator = main.RunStartCoordinator(run_manager)
        main.app.state.tool_manager = ToolManager()
        raise RuntimeError("startup failed after coordinator construction")

    monkeypatch.setattr(main, "RunStartCoordinator", Coordinator)
    monkeypatch.setattr(
        main,
        "_initialize_server",
        fail_after_coordinator_construction,
    )

    with pytest.raises(RuntimeError, match="after coordinator construction"):
        asyncio.run(main.startup_event())

    assert order == ["coordinator", "run_manager", "tool_manager"]
    assert len(constructed_with) == 1
    assert main.app.state.run_start_coordinator is None
    assert main.app.state.run_manager is None
    assert main.app.state.tool_manager is None
    assert main.app.state.server_home_lock is None
    with ServerHomeLock(tmp_path):
        pass


def test_startup_rollback_exhaustion_retains_home_lock_and_resources(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    order: list[str] = []

    class Coordinator:
        async def close(self):
            order.append("coordinator")
            return SimpleNamespace(
                exhausted=True,
                request_task_ids=("request-1",),
                producer_run_ids=(),
                pending_run_ids=(),
            )

    class RunManager:
        async def close(self):
            order.append("run_manager")
            return SimpleNamespace(exhausted_run_ids=())

    class ToolManager:
        async def close(self):
            order.append("tool_manager")

    coordinator = Coordinator()
    run_manager = RunManager()
    tool_manager = ToolManager()

    async def fail_after_coordinator_construction():
        main.app.state.run_start_coordinator = coordinator
        main.app.state.run_manager = run_manager
        main.app.state.tool_manager = tool_manager
        raise RuntimeError("startup failed after coordinator construction")

    monkeypatch.setattr(
        main,
        "_initialize_server",
        fail_after_coordinator_construction,
    )

    with pytest.raises(RuntimeError, match="after coordinator construction"):
        asyncio.run(main.startup_event())

    home_lock = main.app.state.server_home_lock
    assert order == ["coordinator"]
    assert home_lock is not None
    assert main.app.state.run_start_coordinator is coordinator
    assert main.app.state.run_manager is run_manager
    assert main.app.state.tool_manager is tool_manager
    try:
        with pytest.raises(ServerHomeInUseError):
            with ServerHomeLock(tmp_path):
                pass
    finally:
        home_lock.release()
        main.app.state.server_home_lock = None
        main.app.state.run_start_coordinator = None
        main.app.state.run_manager = None
        main.app.state.tool_manager = None


def test_shutdown_stops_before_dependencies_for_exhausted_coordinator(
    monkeypatch,
    caplog,
    tmp_path,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    order: list[str] = []

    class Coordinator:
        def __init__(self):
            self.close_calls = 0

        async def close(self):
            order.append("coordinator")
            self.close_calls += 1
            return SimpleNamespace(
                exhausted=self.close_calls == 1,
                request_task_ids=("request-1",) if self.close_calls == 1 else (),
                producer_run_ids=(),
                pending_run_ids=(),
            )

    class RunManager:
        async def close(self):
            order.append("run_manager")
            return SimpleNamespace(
                pending_run_ids=("observed-only",),
                exhausted_run_ids=(),
            )

    class ToolManager:
        async def close(self):
            order.append("tool_manager")

    coordinator = Coordinator()
    run_manager = RunManager()
    tool_manager = ToolManager()
    home_lock = ServerHomeLock(tmp_path)
    home_lock.acquire()
    monkeypatch.setattr(
        main.app.state,
        "run_start_coordinator",
        coordinator,
        raising=False,
    )
    monkeypatch.setattr(main.app.state, "run_manager", run_manager, raising=False)
    monkeypatch.setattr(main.app.state, "tool_manager", tool_manager, raising=False)
    monkeypatch.setattr(main.app.state, "server_home_lock", home_lock, raising=False)

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="run_start_coordinator drain incomplete"):
            asyncio.run(main.shutdown_event())

    assert order == ["coordinator"]
    assert "resource cleanup failed" in caplog.text
    assert main.app.state.run_start_coordinator is coordinator
    assert main.app.state.run_manager is run_manager
    assert main.app.state.tool_manager is tool_manager
    assert main.app.state.server_home_lock is home_lock
    with pytest.raises(ServerHomeInUseError):
        with ServerHomeLock(tmp_path):
            pass

    asyncio.run(main.shutdown_event())
    assert order == [
        "coordinator",
        "coordinator",
        "run_manager",
        "tool_manager",
    ]
    assert main.app.state.run_start_coordinator is None
    assert main.app.state.run_manager is None
    assert main.app.state.tool_manager is None
    assert main.app.state.server_home_lock is None
    with ServerHomeLock(tmp_path):
        pass


@pytest.mark.parametrize("failure_layer", ["coordinator", "run_manager"])
def test_shutdown_close_failure_stops_before_lower_dependencies(
    monkeypatch,
    failure_layer,
    tmp_path,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    order: list[str] = []

    class Coordinator:
        async def close(self):
            order.append("coordinator")
            if failure_layer == "coordinator":
                raise RuntimeError("coordinator close failed")
            return SimpleNamespace(exhausted=False)

    class RunManager:
        async def close(self):
            order.append("run_manager")
            if failure_layer == "run_manager":
                raise RuntimeError("run_manager close failed")
            return SimpleNamespace(exhausted_run_ids=())

    class ToolManager:
        async def close(self):
            order.append("tool_manager")

    coordinator = Coordinator()
    run_manager = RunManager()
    tool_manager = ToolManager()
    home_lock = ServerHomeLock(tmp_path)
    home_lock.acquire()
    monkeypatch.setattr(
        main.app.state,
        "run_start_coordinator",
        coordinator,
        raising=False,
    )
    monkeypatch.setattr(main.app.state, "run_manager", run_manager, raising=False)
    monkeypatch.setattr(main.app.state, "tool_manager", tool_manager, raising=False)
    monkeypatch.setattr(main.app.state, "server_home_lock", home_lock, raising=False)

    with pytest.raises(RuntimeError, match=f"{failure_layer} close failed"):
        asyncio.run(main.shutdown_event())

    if failure_layer == "coordinator":
        assert order == ["coordinator"]
        assert main.app.state.run_start_coordinator is coordinator
    else:
        assert order == ["coordinator", "run_manager"]
        assert main.app.state.run_start_coordinator is None
    assert main.app.state.run_manager is run_manager
    assert main.app.state.tool_manager is tool_manager
    assert main.app.state.server_home_lock is home_lock
    try:
        with pytest.raises(ServerHomeInUseError):
            with ServerHomeLock(tmp_path):
                pass
    finally:
        home_lock.release()
        main.app.state.server_home_lock = None


def test_shutdown_surfaces_exhausted_run_manager_report(monkeypatch, caplog):
    order: list[str] = []

    class Coordinator:
        async def close(self):
            order.append("coordinator")
            return SimpleNamespace(exhausted=False)

    class RunManager:
        async def close(self):
            order.append("run_manager")
            return SimpleNamespace(
                pending_run_ids=("run-1",),
                exhausted_run_ids=("run-1",),
            )

    class ToolManager:
        async def close(self):
            order.append("tool_manager")

    coordinator = Coordinator()
    run_manager = RunManager()
    tool_manager = ToolManager()
    monkeypatch.setattr(
        main.app.state,
        "run_start_coordinator",
        coordinator,
        raising=False,
    )
    monkeypatch.setattr(main.app.state, "run_manager", run_manager, raising=False)
    monkeypatch.setattr(main.app.state, "tool_manager", tool_manager, raising=False)
    monkeypatch.setattr(main.app.state, "server_home_lock", None, raising=False)

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="run_manager drain incomplete"):
            asyncio.run(main.shutdown_event())

    assert order == ["coordinator", "run_manager"]
    assert "resource cleanup failed" in caplog.text
    assert main.app.state.run_start_coordinator is None
    assert main.app.state.run_manager is run_manager
    assert main.app.state.tool_manager is tool_manager
