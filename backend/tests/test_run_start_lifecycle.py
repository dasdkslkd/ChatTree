from __future__ import annotations

import asyncio

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


def test_run_start_coordinator_dependency_requires_initialized_state():
    missing_app = FastAPI()
    with pytest.raises(HTTPException) as missing:
        dependencies.get_run_start_coordinator(_request(missing_app))
    assert missing.value.status_code == 500

    missing_app.state.run_start_coordinator = None
    with pytest.raises(HTTPException) as cleared:
        dependencies.get_run_start_coordinator(_request(missing_app))
    assert cleared.value.status_code == 500

    coordinator = object()
    missing_app.state.run_start_coordinator = coordinator
    assert dependencies.get_run_start_coordinator(_request(missing_app)) is coordinator


class _Resource:
    def __init__(self, name: str, order: list[str], report: object = None):
        self.name = name
        self.order = order
        self.report = report

    async def close(self):
        self.order.append(self.name)
        return self.report

    def begin_close(self):
        self.order.append(f"{self.name}-begin")


def _install_resources(
    monkeypatch,
    order: list[str],
    *,
    coordinator_report=(),
    registry_report=(),
    command_report=(),
):
    resources = {
        "run_start_coordinator": _Resource("coordinator", order, coordinator_report),
        "producer_registry": _Resource("registry", order, registry_report),
        "command_executor": _Resource("command_executor", order, command_report),
        "run_manager": _Resource("run_manager", order),
        "tool_manager": _Resource("tool_manager", order),
    }
    for state_name, resource in resources.items():
        monkeypatch.setattr(main.app.state, state_name, resource, raising=False)
    return resources


def test_shutdown_closes_the_single_background_owner_chain_and_releases_home_lock(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    order: list[str] = []
    _install_resources(monkeypatch, order)
    home_lock = ServerHomeLock(tmp_path)
    home_lock.acquire()
    monkeypatch.setattr(main.app.state, "server_home_lock", home_lock, raising=False)

    asyncio.run(main.shutdown_event())

    assert order == [
        "coordinator",
        "registry-begin",
        "command_executor",
        "registry",
        "run_manager",
        "tool_manager",
    ]
    for state_name in (
        "run_start_coordinator",
        "producer_registry",
        "command_executor",
        "run_manager",
        "tool_manager",
        "server_home_lock",
    ):
        assert getattr(main.app.state, state_name) is None
    with ServerHomeLock(tmp_path):
        pass


@pytest.mark.parametrize(
    (
        "failing_owner",
        "coordinator_report",
        "registry_report",
        "command_report",
        "expected_order",
    ),
    [
        ("run_start_coordinator", ("run-1",), (), (), ["coordinator"]),
        (
            "producer_registry",
            (),
            ("run-1",),
            (),
            ["coordinator", "registry-begin", "command_executor", "registry"],
        ),
        (
            "command_executor",
            (),
            (),
            ("run-1",),
            ["coordinator", "registry-begin", "command_executor"],
        ),
    ],
)
def test_shutdown_stops_at_an_incomplete_background_drain_and_retains_home_lock(
    monkeypatch,
    tmp_path,
    failing_owner,
    coordinator_report,
    registry_report,
    command_report,
    expected_order,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    order: list[str] = []
    resources = _install_resources(
        monkeypatch,
        order,
        coordinator_report=coordinator_report,
        registry_report=registry_report,
        command_report=command_report,
    )
    home_lock = ServerHomeLock(tmp_path)
    home_lock.acquire()
    monkeypatch.setattr(main.app.state, "server_home_lock", home_lock, raising=False)

    with pytest.raises(RuntimeError, match=f"{failing_owner} drain incomplete"):
        asyncio.run(main.shutdown_event())

    assert order == expected_order
    assert getattr(main.app.state, failing_owner) is resources[failing_owner]
    assert main.app.state.run_manager is resources["run_manager"]
    assert main.app.state.tool_manager is resources["tool_manager"]
    with pytest.raises(ServerHomeInUseError):
        with ServerHomeLock(tmp_path):
            pass

    home_lock.release()
    main.app.state.server_home_lock = None


def test_startup_failure_rolls_back_resources_before_releasing_home_lock(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    order: list[str] = []

    async def fail_after_initialization():
        _install_resources(monkeypatch, order)
        raise RuntimeError("startup failed")

    monkeypatch.setattr(main, "_initialize_server", fail_after_initialization)

    with pytest.raises(RuntimeError, match="startup failed"):
        asyncio.run(main.startup_event())

    assert order == [
        "coordinator",
        "registry-begin",
        "command_executor",
        "registry",
        "run_manager",
        "tool_manager",
    ]
    assert main.app.state.server_home_lock is None
    with ServerHomeLock(tmp_path):
        pass


def test_startup_failure_retains_home_lock_when_rollback_is_incomplete(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    order: list[str] = []

    async def fail_after_initialization():
        _install_resources(monkeypatch, order, coordinator_report=("run-1",))
        raise RuntimeError("startup failed")

    monkeypatch.setattr(main, "_initialize_server", fail_after_initialization)

    with pytest.raises(RuntimeError, match="startup failed"):
        asyncio.run(main.startup_event())

    assert order == ["coordinator"]
    home_lock = main.app.state.server_home_lock
    with pytest.raises(ServerHomeInUseError):
        with ServerHomeLock(tmp_path):
            pass

    home_lock.release()
    main.app.state.server_home_lock = None
