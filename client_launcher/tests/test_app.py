from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from client_launcher.app import create_app
from client_launcher.local_server import ConnectedServer
from client_launcher.profiles import ProfileStore
from client_launcher.settings import LauncherSettings


SERVER_A = "11111111-1111-4111-8111-111111111111"


class ImmediateConnector:
    def __init__(self):
        self.instance_id = SERVER_A
        self.connect_calls = 0
        self.closed = False

    async def connect(self, profile, phase_callback):
        self.connect_calls += 1
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


def _app(tmp_path: Path):
    settings = _settings(tmp_path)
    store = ProfileStore(
        settings.client_home / "profiles.json",
        default_server_home=tmp_path / "default-server",
    )
    store.update("local", auto_connect=False)
    connector = ImmediateConnector()
    return create_app(settings=settings, profiles=store, connector=connector), store, connector


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


def test_rebind_requires_confirmed_instance_id(tmp_path: Path):
    app, _, connector = _app(tmp_path)

    with TestClient(app) as client:
        assert client.post("/client/v1/profiles/local/connect").status_code == 200
        assert client.post("/client/v1/profiles/local/disconnect").status_code == 200
        connector.instance_id = "22222222-2222-4222-8222-222222222222"

        changed = client.post("/client/v1/profiles/local/connect")
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "server_identity_changed"

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
                "expected_server_instance_id": connector.instance_id,
            },
        )
        assert rebound.status_code == 200
        assert rebound.json()["server_instance_id"] == connector.instance_id


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
