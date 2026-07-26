from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from client_launcher.app import create_app
from client_launcher.local_server import ConnectedServer
from client_launcher.models import SshTarget, ssh_profile_id
from client_launcher.profiles import ProfileStore
from client_launcher.settings import LauncherSettings
from client_launcher.ssh_config import SshConfigStore


SERVER_A = "11111111-1111-4111-8111-111111111111"


class ImmediateAnyConnector:
    def __init__(self):
        self.connect_profiles: list[str] = []
        self.disconnected_profiles: list[str] = []
        self.closed = False

    async def connect(self, profile, phase_callback, *, request_id=None):
        self.connect_profiles.append(profile.id)
        phase_callback("handshake")
        return ConnectedServer(
            endpoint=f"http://upstream.test/{profile.id}",
            server_instance_id=SERVER_A,
            handshake={"server_instance_id": SERVER_A, "protocol_version": 1},
        )

    async def disconnect(self, profile):
        self.disconnected_profiles.append(profile.id)

    def shutdown_target(self, profile):
        return f"http://upstream.test/{profile.id}", Path()

    async def request_shutdown(self, profile, expected_server_instance_id, *, request_id=None):
        return f"http://upstream.test/{profile.id}", Path()

    async def wait_stopped(self, endpoint, server_home, *, timeout: float):
        return None

    async def close(self):
        self.closed = True


def _settings(tmp_path: Path) -> LauncherSettings:
    return LauncherSettings(
        client_home=tmp_path / "client",
        project_root=tmp_path,
        server_python="python",
        port=18100,
    )


def _app(tmp_path: Path):
    settings = _settings(tmp_path)
    store = ProfileStore(
        settings.client_home / "profiles.json",
        default_server_home=tmp_path / "server",
    )
    store.update("local", auto_connect=False)
    ssh_config = SshConfigStore(tmp_path / ".ssh" / "config")
    connector = ImmediateAnyConnector()
    app = create_app(
        settings=settings,
        profiles=store,
        connector={"local": connector, "ssh": connector},
        ssh_config=ssh_config,
    )
    return app, store, ssh_config, connector


def test_ssh_config_and_hosts_routes(tmp_path: Path):
    app, _, _, _ = _app(tmp_path)

    with TestClient(app) as client:
        empty = client.get("/client/v1/ssh/config")
        saved = client.put(
            "/client/v1/ssh/config",
            json={"text": "Host gpu-box *.wild\n  HostName 10.0.0.8\n"},
        )
        hosts = client.get("/client/v1/ssh/hosts")

    assert empty.status_code == 200
    assert empty.json()["hosts"] == []
    assert saved.status_code == 200
    assert saved.json()["hosts"] == ["gpu-box"]
    assert hosts.json()["hosts"] == ["gpu-box"]


def test_connect_host_auto_creates_hidden_profile_and_uses_profile_route(
    tmp_path: Path,
):
    app, store, ssh_config, connector = _app(tmp_path)
    ssh_config.write("Host gpu-box\n  HostName 10.0.0.8\n")
    profile_id = ssh_profile_id("gpu-box")

    with TestClient(app) as client:
        connected = client.post("/client/v1/ssh/hosts/gpu-box/connect")
        profiles = client.get("/client/v1/profiles")
        status = client.get("/client/v1/ssh/hosts/gpu-box/status")
        stopped = client.post(
            f"/client/v1/profiles/{profile_id}/server/stop",
            json={"expected_server_instance_id": SERVER_A},
        )
        disconnected = client.post("/client/v1/ssh/hosts/gpu-box/disconnect")

    assert connected.status_code == 200
    assert connected.json()["profile_id"] == profile_id
    assert connected.json()["host_alias"] == "gpu-box"
    assert connected.json()["session"]["status"] == "ready"
    assert connector.connect_profiles == [profile_id]
    assert store.get(profile_id).ssh == SshTarget(config_host="gpu-box")
    assert profiles.json() == [store.get("local").to_dict()]
    assert status.json()["session"]["status"] == "ready"
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "disconnected"
    assert disconnected.status_code == 200
    assert disconnected.json()["session"]["status"] == "disconnected"
    assert profile_id in connector.disconnected_profiles


def test_unknown_host_is_rejected_without_creating_profile(tmp_path: Path):
    app, store, ssh_config, _ = _app(tmp_path)
    ssh_config.write("Host known\n")

    with TestClient(app) as client:
        response = client.post("/client/v1/ssh/hosts/missing/connect")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ssh_host_not_found"
    assert [profile.id for profile in store.list()] == ["local"]


def test_ssh_status_before_first_connect_is_disconnected(tmp_path: Path):
    app, _, ssh_config, _ = _app(tmp_path)
    ssh_config.write("Host gpu-box\n")

    with TestClient(app) as client:
        response = client.get("/client/v1/ssh/hosts/gpu-box/status")

    assert response.status_code == 200
    assert response.json()["profile_id"] == ssh_profile_id("gpu-box")
    assert response.json()["session"]["status"] == "disconnected"
