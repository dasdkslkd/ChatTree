from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.api.routes.server as server_routes
from backend.core.server.identity import ServerIdentity
from backend.core.server.protocol import PROTOCOL_FEATURES


SERVER_ID = "5fb0d7cc-785e-40c2-875d-218447b15583"


def _client(config_data=None) -> TestClient:
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
