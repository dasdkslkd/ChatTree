import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.api.routes.server as server_routes
from backend.api.errors import install_error_handlers
from backend.core.runs import ProducerRegistry
from backend.core.server.admission import MutationAdmission
from backend.core.server.identity import ServerIdentity
from backend.core.server.protocol import PROTOCOL_FEATURES


SERVER_ID = "5fb0d7cc-785e-40c2-875d-218447b15583"


def _client(
    config_data=None,
    *,
    active_runs: list[dict] | None = None,
    pending_approvals: list[dict] | None = None,
    shutdown_calls: list[str] | None = None,
) -> TestClient:
    default_config = {
        "provider": {},
        "default_provider": "",
        "default_model": "",
        "projects": {},
    }
    app = FastAPI()
    app.state.server_identity = ServerIdentity(
        server_instance_id=SERVER_ID
    )
    app.state.config_manager = SimpleNamespace(
        data=default_config if config_data is None else config_data
    )
    app.state.mutation_admission = MutationAdmission()
    run_manager = SimpleNamespace(
        list_active=lambda: list(active_runs or [])
    )
    app.state.run_manager = run_manager
    app.state.producer_registry = ProducerRegistry(run_manager)
    app.state.approval_manager = SimpleNamespace(
        list_pending=lambda: list(pending_approvals or [])
    )
    calls = shutdown_calls if shutdown_calls is not None else []
    app.state.request_shutdown = lambda: calls.append("shutdown")
    install_error_handlers(app)
    app.include_router(server_routes.router, prefix="/api/v1")
    return TestClient(app)


def test_health_is_ready_without_provider(monkeypatch):
    monkeypatch.setattr(server_routes.clock, "time", lambda: 1784112000.9)

    response = _client().get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "server_instance_id": SERVER_ID,
        "time": 1784112000,
    }


def test_handshake_returns_exact_protocol_contract():
    response = _client().get("/api/v1/handshake")

    assert response.status_code == 200
    payload = response.json()
    assert payload["server_instance_id"] == SERVER_ID
    assert payload["protocol_version"] == 1
    assert payload["server_version"] == "0.1.0"
    assert payload["platform"] in {"windows", "macos", "linux"}
    assert payload["features"] == list(PROTOCOL_FEATURES)
    assert "error_envelope_v1" in payload["features"]
    assert "idempotency" not in payload["features"]
    assert "lifecycle" not in payload["features"]
    assert payload["provider_configured"] is False
    UUID(payload["server_instance_id"])


@pytest.mark.parametrize(
    "config_data",
    [
        {"provider": {}, "default_provider": ""},
        {
            "provider": {
                "remote": {"enabled": True},
            },
            "default_provider": "",
        },
        {
            "provider": {
                "remote": {"enabled": False},
            },
            "default_provider": "remote",
        },
        {
            "provider": {},
            "default_provider": "missing",
        },
    ],
)
def test_handshake_reports_provider_not_configured(config_data):
    response = _client(config_data).get("/api/v1/handshake")

    assert response.status_code == 200
    assert response.json()["provider_configured"] is False


def test_handshake_reports_enabled_default_provider():
    config_data = {
        "provider": {
            "remote": {"enabled": True},
        },
        "default_provider": "remote",
    }

    response = _client(config_data).get("/api/v1/handshake")

    assert response.status_code == 200
    assert response.json()["provider_configured"] is True


@pytest.mark.parametrize("path", ["/api/v1/health", "/api/v1/handshake"])
def test_server_routes_fail_closed_before_identity_is_initialized(path):
    app = FastAPI()
    app.state.config_manager = SimpleNamespace(data={})
    app.include_router(server_routes.router, prefix="/api/v1")

    response = TestClient(app).get(path)

    assert response.status_code == 503


def test_shutdown_accepts_only_matching_idle_server_and_closes_admission():
    shutdown_calls: list[str] = []
    client = _client(shutdown_calls=shutdown_calls)

    response = client.post(
        "/api/v1/server/shutdown",
        json={"expected_server_instance_id": SERVER_ID},
    )

    assert response.status_code == 202
    assert response.json() == {
        "server_instance_id": SERVER_ID,
        "status": "stopping",
    }
    assert shutdown_calls == ["shutdown"]
    assert client.app.state.mutation_admission.open is False


def test_shutdown_signal_is_issued_before_endpoint_returns():
    async def scenario() -> None:
        shutdown_calls: list[str] = []
        client = _client(shutdown_calls=shutdown_calls)
        request = SimpleNamespace(app=client.app)

        response = await server_routes.shutdown_server(
            server_routes.ShutdownRequest(
                expected_server_instance_id=SERVER_ID,
            ),
            request,
        )

        assert response.status == "stopping"
        assert shutdown_calls == ["shutdown"]
        assert client.app.state.mutation_admission.open is False

    asyncio.run(scenario())


def test_shutdown_closes_internal_producer_gate_before_exit_signal():
    async def scenario() -> None:
        order: list[str] = []
        client = _client(shutdown_calls=order)
        registry = client.app.state.producer_registry
        original_begin_close = registry.begin_close

        def begin_close() -> None:
            order.append("registry-close")
            original_begin_close()

        registry.begin_close = begin_close
        request = SimpleNamespace(app=client.app)

        response = await server_routes.shutdown_server(
            server_routes.ShutdownRequest(
                expected_server_instance_id=SERVER_ID,
            ),
            request,
        )

        assert response.status == "stopping"
        assert order == ["registry-close", "shutdown"]

        async def late_delivery() -> None:
            raise AssertionError("late internal work must not start")

        assert registry.create_background(
            late_delivery(),
            name="late-notification-delivery",
        ) is None
        assert await registry.close() == ()

    asyncio.run(scenario())


def test_shutdown_cancels_pending_internal_background_work():
    async def scenario() -> None:
        client = _client()
        registry = client.app.state.producer_registry
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending_delivery() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = registry.create_background(
            pending_delivery(),
            name="pending-notification-delivery",
        )
        assert task is not None
        await started.wait()

        await server_routes.shutdown_server(
            server_routes.ShutdownRequest(
                expected_server_instance_id=SERVER_ID,
            ),
            SimpleNamespace(app=client.app),
        )
        await asyncio.gather(task, return_exceptions=True)

        assert cancelled.is_set()
        assert await registry.close() == ()

    asyncio.run(scenario())


def test_shutdown_rejects_stale_server_identity_without_closing_admission():
    client = _client()

    response = client.post(
        "/api/v1/server/shutdown",
        headers={"X-Request-ID": "stale-stop-tree"},
        json={"expected_server_instance_id": "other-server"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "server_identity_mismatch"
    assert response.json()["error"]["request_id"] == "stale-stop-tree"
    assert client.app.state.mutation_admission.open is True


def test_shutdown_rejects_active_runs_and_pending_approvals():
    shutdown_calls: list[str] = []
    client = _client(
        active_runs=[{"run_id": "run-1"}],
        pending_approvals=[{"id": "approval-1"}],
        shutdown_calls=shutdown_calls,
    )

    response = client.post(
        "/api/v1/server/shutdown",
        json={"expected_server_instance_id": SERVER_ID},
    )

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "server_busy",
        "message": "Server has active runs or pending approvals",
        "retryable": True,
        "details": {
            "active_run_ids": ["run-1"],
            "pending_approval_ids": ["approval-1"],
        },
        "request_id": response.headers["X-Request-ID"],
    }
    assert shutdown_calls == []
    assert client.app.state.mutation_admission.open is True


def test_second_shutdown_request_is_rejected_without_second_callback():
    shutdown_calls: list[str] = []
    client = _client(shutdown_calls=shutdown_calls)
    payload = {"expected_server_instance_id": SERVER_ID}

    first = client.post("/api/v1/server/shutdown", json=payload)
    second = client.post("/api/v1/server/shutdown", json=payload)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "server_shutting_down"
    assert shutdown_calls == ["shutdown"]
