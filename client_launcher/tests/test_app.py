from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from starlette.responses import StreamingResponse

from client_launcher.app import create_app
from client_launcher.http_errors import (
    GENERIC_5XX_MESSAGE,
    REQUEST_ID_RE,
    ErrorEnvelope,
    RequestBoundaryMiddleware,
)
from client_launcher.local_server import ConnectedServer
from client_launcher.models import LauncherError
from client_launcher.profiles import ProfileStore
from client_launcher.proxy import CONNECTION_LEASE_HEADER, ProxyError
from client_launcher.settings import LauncherSettings


SERVER_A = "11111111-1111-4111-8111-111111111111"


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        return False


class ImmediateConnector:
    def __init__(self):
        self.instance_id = SERVER_A
        self.connect_calls = 0
        self.request_ids: list[str] = []
        self.closed = False

    async def connect(
        self,
        profile,
        phase_callback,
        *,
        request_id: str | None = None,
    ):
        self.connect_calls += 1
        assert request_id is not None
        self.request_ids.append(request_id)
        phase_callback("health")
        phase_callback("handshake")
        return ConnectedServer(
            endpoint=f"http://127.0.0.1:{profile.local.server_port}",
            server_instance_id=self.instance_id,
            handshake={
                "server_instance_id": self.instance_id,
                "protocol_version": 1,
            },
        )

    async def close(self):
        self.closed = True


def _settings(tmp_path: Path) -> LauncherSettings:
    return LauncherSettings(
        client_home=tmp_path / "client",
        project_root=Path(__file__).resolve().parents[2],
        server_python="python",
        port=18100,
    )


def _app(
    tmp_path: Path,
    *,
    proxy_client: httpx.AsyncClient | None = None,
    require_connection_lease: bool = False,
    max_request_body_bytes: int | None = None,
):
    settings = _settings(tmp_path)
    if max_request_body_bytes is not None:
        settings = replace(
            settings,
            max_request_body_bytes=max_request_body_bytes,
        )
    store = ProfileStore(
        settings.client_home / "profiles.json",
        default_server_home=tmp_path / "default-server",
    )
    store.update("local", auto_connect=False)
    connector = ImmediateConnector()
    return (
        create_app(
            settings=settings,
            profiles=store,
            connector=connector,
            proxy_client=proxy_client,
            require_connection_lease=require_connection_lease,
        ),
        store,
        connector,
    )


def test_independent_launcher_apps_issue_distinct_epoch_zero_leases(tmp_path: Path):
    first_app, _, _ = _app(tmp_path / "first")
    second_app, _, _ = _app(tmp_path / "second")

    with TestClient(first_app) as first_client:
        first_status = first_client.get(
            "/client/v1/profiles/local/status"
        ).json()
    with TestClient(second_app) as second_client:
        second_status = second_client.get(
            "/client/v1/profiles/local/status"
        ).json()

    assert first_status["status"] == second_status["status"] == "disconnected"
    assert first_status["connection_epoch"] == second_status["connection_epoch"] == 0
    assert _is_canonical_uuid(first_status["connection_lease_id"])
    assert _is_canonical_uuid(second_status["connection_lease_id"])
    assert first_status["connection_lease_id"] != second_status["connection_lease_id"]


def test_profile_crud_and_stable_error_envelope(tmp_path: Path):
    app, store, _ = _app(tmp_path)

    with TestClient(app) as client:
        listed = client.get("/client/v1/profiles")
        assert listed.status_code == 200
        assert [profile["id"] for profile in listed.json()] == ["local"]

        created = client.post(
            "/client/v1/profiles",
            json={
                "label": "Work",
                "auto_connect": False,
                "server_home": str(tmp_path / "work-server"),
                "server_port": 18101,
            },
        )
        assert created.status_code == 201
        profile_id = created.json()["id"]
        assert created.json()["bound_server_instance_id"] is None

        patched = client.patch(
            f"/client/v1/profiles/{profile_id}",
            json={"label": "Workstation", "auto_connect": True},
        )
        assert patched.status_code == 200
        assert patched.json()["label"] == "Workstation"
        assert patched.json()["auto_connect"] is True

        duplicate = client.post(
            "/client/v1/profiles",
            headers={"X-Request-ID": "req-duplicate"},
            json={
                "label": "Duplicate",
                "server_home": str(tmp_path / "work-server"),
                "server_port": 18102,
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.headers["X-Request-ID"] == "req-duplicate"
        assert duplicate.json() == {
            "error": {
                "code": "profile_home_duplicate",
                "message": duplicate.json()["error"]["message"],
                "retryable": False,
                "request_id": "req-duplicate",
            }
        }

        deleted = client.delete(f"/client/v1/profiles/{profile_id}")
        assert deleted.status_code == 204
        assert store.list() == (store.get("local"),)


def test_create_profile_requires_explicit_unique_server_port(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        missing_port = client.post(
            "/client/v1/profiles",
            json={
                "label": "Work",
                "server_home": str(tmp_path / "work-server"),
            },
        )
        duplicate_port = client.post(
            "/client/v1/profiles",
            json={
                "label": "Work",
                "server_home": str(tmp_path / "work-server"),
                "server_port": 8001,
            },
        )

    assert missing_port.status_code == 422
    assert missing_port.json()["error"]["code"] == "invalid_request"
    assert duplicate_port.status_code == 409
    assert duplicate_port.json()["error"]["code"] == "profile_port_duplicate"


def test_rejected_endpoint_update_preserves_ready_session(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        created = client.post(
            "/client/v1/profiles",
            json={
                "label": "Work",
                "server_home": str(tmp_path / "work-server"),
                "server_port": 18101,
            },
        )
        profile_id = created.json()["id"]
        connected = client.post(f"/client/v1/profiles/{profile_id}/connect")

        rejected = client.patch(
            f"/client/v1/profiles/{profile_id}",
            json={"server_port": 8001},
        )
        status = client.get(f"/client/v1/profiles/{profile_id}/status")

    assert connected.status_code == 200
    assert connected.json()["status"] == "ready"
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "profile_port_duplicate"
    assert status.json()["status"] == "ready"
    assert status.json()["connection_epoch"] == 1


def test_noop_endpoint_update_preserves_ready_session(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        connected = client.post("/client/v1/profiles/local/connect")
        unchanged = client.patch(
            "/client/v1/profiles/local",
            json={"server_port": 8001},
        )
        status = client.get("/client/v1/profiles/local/status")

    assert connected.status_code == 200
    assert unchanged.status_code == 200
    assert status.json()["status"] == "ready"
    assert status.json()["connection_epoch"] == 1


def test_connect_disconnect_and_endpoint_change_reset_session(tmp_path: Path):
    app, store, connector = _app(tmp_path)

    with TestClient(app) as client:
        connected = client.post("/client/v1/profiles/local/connect")
        assert connected.status_code == 200
        assert connected.json()["status"] == "ready"
        assert connected.json()["connection_epoch"] == 1
        assert store.get("local").bound_server_instance_id == SERVER_A

        label_only = client.patch(
            "/client/v1/profiles/local",
            json={"label": "My local"},
        )
        assert label_only.status_code == 200
        assert client.get("/client/v1/profiles/local/status").json()["status"] == "ready"

        endpoint_change = client.patch(
            "/client/v1/profiles/local",
            json={"server_port": 18103},
        )
        assert endpoint_change.status_code == 200
        assert client.get("/client/v1/profiles/local/status").json()["status"] == "disconnected"

        reconnected = client.post("/client/v1/profiles/local/connect")
        assert reconnected.status_code == 200
        assert reconnected.json()["connection_epoch"] == 2
        assert connector.connect_calls == 2

        disconnected = client.post("/client/v1/profiles/local/disconnect")
        assert disconnected.status_code == 200
        assert disconnected.json()["status"] == "disconnected"

    assert connector.closed is True


def test_app_threads_strict_connection_lease_switch_through_proxy(tmp_path: Path):
    upstream_calls = 0
    forwarded_leases: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        forwarded_leases.extend(
            request.headers.get_list("x-chattree-connection-lease-id")
        )
        return httpx.Response(
            204,
            headers=[
                (CONNECTION_LEASE_HEADER, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                (CONNECTION_LEASE_HEADER, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            ],
        )

    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app, _, _ = _app(
        tmp_path,
        proxy_client=proxy_client,
        require_connection_lease=True,
    )

    with TestClient(app) as client:
        connected = client.post("/client/v1/profiles/local/connect")
        lease_id = connected.json()["connection_lease_id"]
        missing = client.get("/p/local/api/v1/health")
        matching = client.get(
            "/p/local/api/v1/health",
            headers={CONNECTION_LEASE_HEADER: lease_id},
        )

    asyncio.run(proxy_client.aclose())
    assert missing.status_code == 409
    assert missing.json()["error"] == {
        "code": "stale_connection_epoch",
        "message": missing.json()["error"]["message"],
        "retryable": False,
        "request_id": missing.headers["X-Request-ID"],
        "details": {"current_connection_epoch": 1},
    }
    assert missing.headers.get_list(CONNECTION_LEASE_HEADER) == [lease_id]
    assert matching.status_code == 204
    assert matching.headers.get_list(CONNECTION_LEASE_HEADER) == [lease_id]
    assert forwarded_leases == []
    assert upstream_calls == 1


def test_strict_proxy_body_limit_error_carries_captured_connection_lease(
    tmp_path: Path,
):
    upstream_calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(204)

    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app, _, _ = _app(
        tmp_path,
        proxy_client=proxy_client,
        require_connection_lease=True,
        max_request_body_bytes=5,
    )

    with TestClient(app) as client:
        connected = client.post("/client/v1/profiles/local/connect")
        lease_id = connected.json()["connection_lease_id"]
        response = client.post(
            "/p/local/api/v1/upload",
            headers={CONNECTION_LEASE_HEADER: lease_id},
            content=b"too-large",
        )

    asyncio.run(proxy_client.aclose())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_body_too_large"
    assert response.headers.get_list(CONNECTION_LEASE_HEADER) == [lease_id]
    assert upstream_calls == 0


def test_rebind_requires_confirmed_instance_id(tmp_path: Path):
    app, _, connector = _app(tmp_path)

    with TestClient(app) as client:
        assert client.post("/client/v1/profiles/local/connect").status_code == 200
        assert client.post("/client/v1/profiles/local/disconnect").status_code == 200
        connector.instance_id = "22222222-2222-4222-8222-222222222222"

        changed = client.post("/client/v1/profiles/local/connect")
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "server_identity_changed"
        observed_instance_id = changed.json()["error"]["details"][
            "observed_server_instance_id"
        ]
        assert observed_instance_id == connector.instance_id
        status = client.get("/client/v1/profiles/local/status").json()
        assert status["error"]["details"]["observed_server_instance_id"] == (
            observed_instance_id
        )

        missing_confirmation = client.post(
            "/client/v1/profiles/local/connect",
            json={"rebind": True},
        )
        assert missing_confirmation.status_code == 422
        assert missing_confirmation.json()["error"]["code"] == "rebind_confirmation_required"

        rebound = client.post(
            "/client/v1/profiles/local/connect",
            json={
                "rebind": True,
                "expected_server_instance_id": observed_instance_id,
            },
        )
        assert rebound.status_code == 200
        assert rebound.json()["server_instance_id"] == observed_instance_id


def test_duplicate_instance_connect_rolls_back_unbound_profile(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        assert client.post("/client/v1/profiles/local/connect").status_code == 200
        created = client.post(
            "/client/v1/profiles",
            json={
                "label": "Duplicate Instance",
                "server_home": str(tmp_path / "other-server"),
                "server_port": 18101,
            },
        )
        profile_id = created.json()["id"]

        duplicate = client.post(f"/client/v1/profiles/{profile_id}/connect")
        profiles = client.get("/client/v1/profiles").json()

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "server_instance_already_bound"
    assert duplicate.json()["error"]["details"] == {
        "existing_profile_id": "local",
        "observed_server_instance_id": SERVER_A,
        "unbound_profile_removed": True,
    }
    assert [profile["id"] for profile in profiles] == ["local"]


def test_profile_write_failure_uses_launcher_error_envelope(
    tmp_path: Path,
    monkeypatch,
):
    app, store, _ = _app(tmp_path)
    before = store.list()

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("client_launcher.profiles.atomic_write_json", fail_write)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/client/v1/profiles",
            headers={"X-Request-ID": "req-write-failed"},
            json={
                "label": "Work",
                "server_home": str(tmp_path / "write-failed-server"),
                "server_port": 18101,
            },
        )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "req-write-failed"
    assert response.json() == {
        "error": {
            "code": "profiles_write_failed",
            "message": response.json()["error"]["message"],
            "retryable": True,
            "request_id": "req-write-failed",
        }
    }
    assert store.list() == before


def test_new_local_profile_defaults_to_auto_connect(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/client/v1/profiles",
            json={
                "label": "Automatic",
                "server_home": str(tmp_path / "automatic-server"),
                "server_port": 18101,
            },
        )

    assert response.status_code == 201
    assert response.json()["auto_connect"] is True


def test_origin_and_validation_are_rejected_with_launcher_errors(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        forbidden = client.get(
            "/client/v1/profiles",
            headers={"Origin": "https://evil.example"},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "origin_not_allowed"

        allowed = client.get(
            "/client/v1/profiles",
            headers={"Origin": "http://localhost:5173"},
        )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"

        invalid = client.post(
            "/client/v1/profiles",
            json={"label": "", "server_home": "", "server_port": 0},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_request"
        assert invalid.json()["error"]["request_id"].startswith("req_")


def test_allowed_cors_exposes_connection_lease_and_request_id(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        response = client.get(
            "/client/v1/profiles/local/status",
            headers={"Origin": "http://localhost:5173"},
        )
        preflight = client.options(
            "/p/local/api/v1/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": (
                    f"{CONNECTION_LEASE_HEADER}, X-Request-ID"
                ),
            },
        )

    exposed = {
        value.strip().lower()
        for value in response.headers["Access-Control-Expose-Headers"].split(",")
    }
    allowed = {
        value.strip().lower()
        for value in preflight.headers["Access-Control-Allow-Headers"].split(",")
    }
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == (
        "http://localhost:5173"
    )
    assert exposed == {
        CONNECTION_LEASE_HEADER.lower(),
        "x-request-id",
    }
    assert preflight.status_code == 200
    assert preflight.headers["Access-Control-Allow-Origin"] == (
        "http://localhost:5173"
    )
    assert CONNECTION_LEASE_HEADER.lower() in allowed
    assert "x-request-id" in allowed


def test_profile_requests_reject_coercion_and_unknown_fields(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        boolean_port = client.post(
            "/client/v1/profiles",
            json={
                "label": "Boolean Port",
                "server_home": str(tmp_path / "boolean-port"),
                "server_port": True,
            },
        )
        binding_injection = client.patch(
            "/client/v1/profiles/local",
            json={"bound_server_instance_id": SERVER_A},
        )
        coerced_rebind = client.post(
            "/client/v1/profiles/local/connect",
            json={"rebind": 1, "expected_server_instance_id": SERVER_A},
        )

    for response in (boolean_port, binding_injection, coerced_rebind):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"


def test_default_profile_cannot_be_deleted(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        response = client.delete("/client/v1/profiles/local")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "default_profile_required"


def test_stale_proxy_failure_does_not_break_reconnected_session(tmp_path: Path):
    settings = _settings(tmp_path)
    store = ProfileStore(
        settings.client_home / "profiles.json",
        default_server_home=tmp_path / "default-server",
    )
    store.update("local", auto_connect=False)
    connector = ImmediateConnector()
    app = None

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert app is not None
        sessions = app.state.session_manager
        await sessions.disconnect("local")
        await sessions.connect("local")
        raise httpx.ConnectError("connection refused", request=request)

    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        settings=settings,
        profiles=store,
        connector=connector,
        proxy_client=proxy_client,
    )

    with TestClient(app) as client:
        assert client.post("/client/v1/profiles/local/connect").status_code == 200
        response = client.get("/p/local/api/v1/health")
        status = client.get("/client/v1/profiles/local/status")

    asyncio.run(proxy_client.aclose())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "proxy_upstream_unavailable"
    assert status.json()["status"] == "ready"
    assert status.json()["connection_epoch"] == 2


def test_deleted_profile_proxy_failure_preserves_transport_error(tmp_path: Path):
    settings = _settings(tmp_path)
    store = ProfileStore(
        settings.client_home / "profiles.json",
        default_server_home=tmp_path / "default-server",
    )
    store.update("local", auto_connect=False)
    connector = ImmediateConnector()
    profile_id = ""

    async def upstream(request: httpx.Request) -> httpx.Response:
        store.delete(profile_id)
        raise httpx.ConnectError("connection refused", request=request)

    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        settings=settings,
        profiles=store,
        connector=connector,
        proxy_client=proxy_client,
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        created = client.post(
            "/client/v1/profiles",
            json={
                "label": "Disposable",
                "auto_connect": False,
                "server_home": str(tmp_path / "disposable-server"),
                "server_port": 18101,
            },
        )
        profile_id = created.json()["id"]
        connected = client.post(f"/client/v1/profiles/{profile_id}/connect")
        assert connected.status_code == 200

        response = client.get(f"/p/{profile_id}/api/v1/health")

    asyncio.run(proxy_client.aclose())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "proxy_upstream_unavailable"


@pytest.mark.parametrize(
    ("headers", "preserved"),
    (
        ([('X-Request-ID', 'client.request:1')], 'client.request:1'),
        ([('X-Request-ID', 'contains space')], None),
        (
            [
                ('X-Request-ID', 'first-request'),
                ('X-Request-ID', 'second-request'),
            ],
            None,
        ),
        ([], None),
    ),
    ids=("valid", "invalid", "duplicate", "missing"),
)
def test_request_boundary_emits_one_canonical_id(
    tmp_path: Path,
    headers: list[tuple[str, str]],
    preserved: str | None,
):
    app, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        response = client.get("/client/v1/profiles", headers=headers)

    request_ids = response.headers.get_list("X-Request-ID")
    assert len(request_ids) == 1
    request_id = request_ids[0]
    assert REQUEST_ID_RE.fullmatch(request_id)
    assert request_id.startswith("req_") is (preserved is None)
    if preserved is None:
        assert request_id not in {value for _, value in headers}
    else:
        assert request_id == preserved


def test_connect_route_propagates_boundary_request_id(tmp_path: Path):
    app, _, connector = _app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/client/v1/profiles/local/connect",
            headers={"X-Request-ID": "connect-tree:1"},
        )

    assert response.status_code == 200
    assert connector.request_ids == ["connect-tree:1"]
    assert response.headers["X-Request-ID"] == "connect-tree:1"


@pytest.mark.parametrize(
    ("status_code", "code", "retryable"),
    (
        (400, "invalid_request", False),
        (401, "unauthorized", False),
        (403, "forbidden", False),
        (404, "not_found", False),
        (405, "method_not_allowed", False),
        (409, "conflict", False),
        (410, "gone", False),
        (412, "precondition_failed", False),
        (413, "payload_too_large", False),
        (415, "unsupported_media_type", False),
        (418, "http_error", False),
        (422, "invalid_request", False),
        (428, "precondition_required", False),
        (429, "rate_limited", True),
        (451, "http_error", False),
        (500, "internal_error", False),
        (502, "service_unavailable", True),
        (503, "service_unavailable", True),
        (504, "service_unavailable", True),
        (507, "internal_error", False),
    ),
)
def test_http_exception_statuses_use_complete_fallback_matrix(
    tmp_path: Path,
    status_code: int,
    code: str,
    retryable: bool,
):
    app, _, _ = _app(tmp_path)

    @app.get("/__contract__/status/{value}")
    async def status_error(value: int):
        raise HTTPException(value, "launcher status secret")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            f"/__contract__/status/{status_code}",
            headers={"X-Request-ID": f"status-{status_code}"},
        )

    error = response.json()["error"]
    assert response.status_code == status_code
    assert error["code"] == code
    assert error["retryable"] is retryable
    assert error["request_id"] == response.headers["X-Request-ID"]
    if status_code >= 500:
        assert error["message"] == GENERIC_5XX_MESSAGE
        assert "launcher status secret" not in response.text


def test_validation_issues_are_redacted_and_structured(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/client/v1/profiles",
            json={
                "label": "Sensitive",
                "server_home": "secret-home-value",
                "server_port": "secret-port-value",
                "credentials": {"token": "secret-token-value"},
            },
        )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert error["details"]["issues"]
    assert all(
        set(issue) == {"path", "code", "message"}
        for issue in error["details"]["issues"]
    )
    assert "secret-home-value" not in response.text
    assert "secret-port-value" not in response.text
    assert "secret-token-value" not in response.text


@pytest.mark.parametrize(
    ("path", "request_id", "secret", "code", "has_traceback"),
    (
        (
            "/__contract__/launcher-5xx",
            "launcher-5xx",
            "launcher original reason",
            "launcher_failed",
            False,
        ),
        (
            "/__contract__/proxy-5xx",
            "proxy-5xx",
            "proxy original reason",
            "proxy_error",
            False,
        ),
        (
            "/__contract__/http-5xx",
            "http-5xx",
            "http original reason",
            "internal_error",
            False,
        ),
        (
            "/__contract__/unknown-5xx",
            "unknown-5xx",
            "unknown original reason",
            "internal_error",
            True,
        ),
    ),
)
def test_launcher_5xx_sources_are_sanitized_and_logged(
    tmp_path: Path,
    caplog,
    path: str,
    request_id: str,
    secret: str,
    code: str,
    has_traceback: bool,
):
    app, _, _ = _app(tmp_path)

    @app.get("/__contract__/launcher-5xx")
    async def launcher_5xx():
        raise LauncherError(
            "launcher_failed",
            "launcher original reason",
            retryable=True,
            status_code=503,
            details={"secret": "launcher detail secret"},
        )

    @app.get("/__contract__/proxy-5xx")
    async def proxy_5xx():
        raise ProxyError("proxy original reason")

    @app.get("/__contract__/http-5xx")
    async def http_5xx():
        raise HTTPException(
            500,
            {"message": "http original reason", "secret": "http detail secret"},
        )

    @app.get("/__contract__/unknown-5xx")
    async def unknown_5xx():
        raise RuntimeError("unknown original reason")

    caplog.set_level("ERROR", logger="client_launcher.http_errors")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path, headers={"X-Request-ID": request_id})

    error = response.json()["error"]
    assert response.status_code >= 500
    assert error["code"] == code
    assert error["message"] == GENERIC_5XX_MESSAGE
    assert "details" not in error
    assert secret not in response.text
    assert "detail secret" not in response.text
    records = [
        record
        for record in caplog.records
        if record.name == "client_launcher.http_errors"
        and request_id in record.getMessage()
    ]
    assert len(records) == 1
    assert secret in records[0].getMessage()
    assert (records[0].exc_info is not None) is has_traceback


def test_boundary_wraps_server_errors_and_preserves_allowed_cors(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    @app.get("/__contract__/outermost-unknown")
    async def outermost_unknown():
        raise RuntimeError("outermost unknown reason")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/__contract__/outermost-unknown",
            headers={
                "Origin": "http://localhost:5173",
                "X-Request-ID": "outermost-unknown",
            },
        )

    assert isinstance(app.middleware_stack, RequestBoundaryMiddleware)
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "outermost-unknown"
    assert response.headers["Access-Control-Allow-Origin"] == (
        "http://localhost:5173"
    )
    assert response.json()["error"]["request_id"] == "outermost-unknown"


def test_proxy_preserves_a_different_valid_upstream_request_id(tmp_path: Path):
    upstream_body = (
        b'{"error":{"code":"active_runs_present","message":"blocked",'
        b'"retryable":true,"request_id":"upstream-tree","details":'
        b'{"active_run_ids":["run-1"]}}}'
    )

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers.get_list("X-Request-ID") == ["launcher-tree"]
        return httpx.Response(
            409,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": "upstream-tree",
            },
            content=upstream_body,
        )

    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app, _, _ = _app(tmp_path, proxy_client=proxy_client)

    with TestClient(app) as client:
        assert client.post("/client/v1/profiles/local/connect").status_code == 200
        response = client.delete(
            "/p/local/api/v1/conversations/branch",
            headers={"X-Request-ID": "launcher-tree"},
        )

    asyncio.run(proxy_client.aclose())
    assert response.status_code == 409
    assert response.content == upstream_body
    assert response.headers.get_list("X-Request-ID") == ["upstream-tree"]
    assert response.json()["error"]["request_id"] == "upstream-tree"


def test_safe_exception_and_cors_headers_survive(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    @app.get("/__contract__/headers")
    async def headers_error():
        raise HTTPException(
            401,
            "authenticate",
            headers={
                "Allow": "GET, HEAD",
                "Retry-After": "17",
                "WWW-Authenticate": 'Bearer realm="launcher"',
                "ETag": '"revision-1"',
                "Content-Type": "text/plain",
                "Content-Length": "9999",
                "X-Request-ID": "spoofed",
                "X-Unsafe": "drop",
            },
        )

    with TestClient(app) as client:
        response = client.get(
            "/__contract__/headers",
            headers={
                "Origin": "http://localhost:5173",
                "X-Request-ID": "safe-headers",
            },
        )
        invalid_preflight = client.options(
            "/client/v1/profiles",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        allowed_preflight = client.options(
            "/client/v1/profiles",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.headers["Allow"] == "GET, HEAD"
    assert response.headers["Retry-After"] == "17"
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="launcher"'
    assert response.headers["ETag"] == '"revision-1"'
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Content-Length"] != "9999"
    assert response.headers["X-Request-ID"] == "safe-headers"
    assert "X-Unsafe" not in response.headers
    assert response.headers["Access-Control-Allow-Origin"] == (
        "http://localhost:5173"
    )
    assert invalid_preflight.status_code == 403
    assert invalid_preflight.json()["error"]["code"] == "origin_not_allowed"
    assert "evil.example" not in invalid_preflight.text
    assert allowed_preflight.status_code == 200
    assert allowed_preflight.headers["Access-Control-Allow-Origin"] == (
        "http://localhost:5173"
    )


def test_launcher_openapi_uses_owned_error_envelope(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    with pytest.warns(UserWarning, match="Duplicate Operation ID"):
        schema = app.openapi()

    assert ErrorEnvelope.__module__ == "client_launcher.http_errors"
    assert "ErrorEnvelope" in schema["components"]["schemas"]
    validation_schema = schema["paths"]["/client/v1/profiles"]["post"][
        "responses"
    ]["422"]["content"]["application/json"]["schema"]
    assert validation_schema["$ref"].endswith("/ErrorEnvelope")


def test_initial_sse_response_has_one_canonical_request_id(tmp_path: Path):
    app, _, _ = _app(tmp_path)

    async def events():
        yield b"data: first\n\n"

    @app.get("/__contract__/events")
    async def stream_events():
        return StreamingResponse(events(), media_type="text/event-stream")

    with TestClient(app) as client:
        with client.stream(
            "GET",
            "/__contract__/events",
            headers={"X-Request-ID": "sse-header"},
        ) as response:
            assert response.status_code == 200
            assert response.headers.get_list("X-Request-ID") == ["sse-header"]
            assert next(response.iter_bytes()) == b"data: first\n\n"


def test_request_boundary_sends_first_stream_chunk_before_unblock():
    async def scenario() -> None:
        release = asyncio.Event()
        never_disconnect = asyncio.Event()

        async def events():
            yield b"data: first\n\n"
            await release.wait()
            yield b"data: second\n\n"

        app = FastAPI()

        @app.get("/events")
        async def stream_events():
            response = StreamingResponse(events(), media_type="text/event-stream")
            response.raw_headers.extend(
                [
                    (b"x-request-id", b"spoofed"),
                    (b"x-request-id", b"spoofed-again"),
                ]
            )
            return response

        app.add_middleware(RequestBoundaryMiddleware)
        sent: list[dict[str, Any]] = []
        first_recorded = asyncio.Event()

        async def receive():
            await never_disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if (
                message["type"] == "http.response.body"
                and message.get("body") == b"data: first\n\n"
            ):
                first_recorded.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/events",
            "raw_path": b"/events",
            "query_string": b"",
            "headers": [(b"x-request-id", b"send-level")],
            "client": ("127.0.0.1", 12345),
            "server": ("launcher.test", 80),
            "root_path": "",
        }
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(first_recorded.wait(), timeout=0.25)

        assert not task.done()
        assert sent[1] == {
            "type": "http.response.body",
            "body": b"data: first\n\n",
            "more_body": True,
        }
        response_headers = [
            value
            for name, value in sent[0]["headers"]
            if name.lower() == b"x-request-id"
        ]
        assert response_headers == [b"send-level"]

        release.set()
        await asyncio.wait_for(task, timeout=0.25)

    asyncio.run(scenario())
