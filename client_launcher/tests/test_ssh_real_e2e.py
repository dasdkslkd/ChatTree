from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from client_launcher.app import create_app
from client_launcher.profiles import ProfileStore
from client_launcher.settings import LauncherSettings
from client_launcher.ssh_config import SshConfigStore


REAL_SSH_ENV = "CHATTREE_SSH_E2E_HOST"


def _real_ssh_host() -> str:
    return os.environ.get(REAL_SSH_ENV, "").strip()


def _require_real_ssh_host() -> str:
    host = _real_ssh_host()
    if not host:
        pytest.skip(f"set {REAL_SSH_ENV} to run real SSH E2E")
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            host,
            "chattree-server",
            "--version",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(
            f"real SSH E2E requires `ssh {host}` and remote chattree-server: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return host


def _settings(tmp_path: Path) -> LauncherSettings:
    return LauncherSettings(
        client_home=tmp_path / "client",
        project_root=tmp_path,
        server_python="python",
        port=18120,
        connect_timeout_seconds=5.0,
        start_timeout_seconds=30.0,
        poll_interval_seconds=0.1,
    )


def test_real_ssh_connect_proxy_and_sse_direct_response(tmp_path: Path):
    host = _require_real_ssh_host()
    settings = _settings(tmp_path)
    profiles = ProfileStore(
        settings.client_home / "profiles.json",
        default_server_home=tmp_path / "local-server",
    )
    profiles.update("local", auto_connect=False)
    ssh_config = SshConfigStore(tmp_path / ".ssh" / "config")
    ssh_config.write(f"Host {host}\n")
    app = create_app(
        settings=settings,
        profiles=profiles,
        ssh_config=ssh_config,
    )

    profile_id = ""
    lease_id = ""
    with TestClient(app) as client:
        connected = client.post(f"/client/v1/ssh/hosts/{quote(host, safe='')}/connect")
        assert connected.status_code == 200, connected.text
        profile_id = connected.json()["profile_id"]
        session = connected.json()["session"]
        lease_id = session["connection_lease_id"]
        assert session["status"] == "ready"
        assert session["server_instance_id"]

        encoded_profile = quote(profile_id, safe="")
        lease_headers = {"X-ChatTree-Connection-Lease-ID": lease_id}
        handshake = client.get(
            f"/p/{encoded_profile}/api/v1/handshake",
            headers=lease_headers,
        )
        assert handshake.status_code == 200
        assert handshake.json()["platform"] in {"linux", "macos", "windows"}

        conversation = client.post(
            f"/p/{encoded_profile}/api/v1/conversations",
            json={
                "title": f"real SSH E2E {uuid4()}",
                "multi_agent_mode": "none",
            },
            headers=lease_headers,
        )
        assert conversation.status_code == 200, conversation.text
        conversation_payload = conversation.json()

        started = client.post(
            (
                f"/p/{encoded_profile}/api/v1/conversations/"
                f"{conversation_payload['id']}/messages/runs"
            ),
            json={
                "content": "/help",
                "parent_node_id": conversation_payload["current_node_id"],
                "focus_new_node": True,
            },
            headers={
                **lease_headers,
                "Idempotency-Key": f"real-ssh-e2e-{uuid4()}",
            },
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]

        stream = client.get(
            f"/p/{encoded_profile}/api/v1/runs/{run_id}/attach?from_event=0",
            headers=lease_headers,
        )
        assert stream.status_code == 200, stream.text
        body = stream.text
        assert "data:" in body
        assert "available" in body.lower() or "slash" in body.lower()
        assert "data: [DONE]" in body

        disconnected = client.post(
            f"/client/v1/ssh/hosts/{quote(host, safe='')}/disconnect"
        )
        assert disconnected.status_code == 200
        assert disconnected.json()["session"]["status"] == "disconnected"
