from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from client_launcher.models import (
    ConnectionErrorInfo,
    LauncherError,
    LocalTarget,
    ServerProfile,
    ServerSession,
    SshTarget,
    ssh_profile_id,
)
from client_launcher.profiles import ProfileStore
from client_launcher.settings import (
    DEFAULT_LOCAL_PROFILE_ID,
    DEFAULT_LOCAL_SERVER_PORT,
    LauncherSettings,
    PROFILES_SCHEMA_VERSION,
    resolve_client_home,
)


LEASE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _profile(
    profile_id: str,
    home: Path,
    *,
    label: str | None = None,
    bound_server_instance_id: str | None = None,
    port: int = 8100,
) -> ServerProfile:
    return ServerProfile(
        id=profile_id,
        label=label or profile_id,
        kind="local",
        auto_connect=False,
        bound_server_instance_id=bound_server_instance_id,
        local=LocalTarget(server_home=str(home), server_port=port),
    )


def _write_document(path: Path, profiles: list[dict], *, version: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": version, "profiles": profiles}),
        encoding="utf-8",
    )


def test_resolve_client_home_uses_override_env_and_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    configured = tmp_path / "configured"
    monkeypatch.setenv("CHATTREE_CLIENT_HOME", str(configured))
    assert resolve_client_home() == configured.resolve()

    explicit = tmp_path / "explicit"
    assert resolve_client_home(explicit) == explicit.resolve()

    monkeypatch.delenv("CHATTREE_CLIENT_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    assert resolve_client_home() == (tmp_path / "user" / ".chattree-client").resolve()


def test_launcher_settings_from_env_has_local_defaults_and_typed_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setenv("CHATTREE_CLIENT_HOME", str(tmp_path / "client"))
    monkeypatch.setenv("CHATTREE_CLIENT_PORT", "18000")
    monkeypatch.setenv("CHATTREE_LOCAL_SERVER_PORT", "18001")
    monkeypatch.setenv("CHATTREE_SERVER_PYTHON", str(tmp_path / "python"))
    monkeypatch.setenv("CHATTREE_CLIENT_CONNECT_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("CHATTREE_CLIENT_ALLOWED_ORIGINS", "http://one.test, http://two.test")

    settings = LauncherSettings.from_env(project_root=tmp_path / "project")

    assert settings.client_home == (tmp_path / "client").resolve()
    assert settings.project_root == (tmp_path / "project").resolve()
    assert settings.server_python == str(tmp_path / "python")
    assert settings.host == "127.0.0.1"
    assert settings.port == 18000
    assert settings.default_local_server_port == 18001
    assert settings.connect_timeout_seconds == 1.5
    assert settings.start_timeout_seconds > 0
    assert settings.poll_interval_seconds > 0
    assert settings.max_request_body_bytes > 0
    assert settings.proxy_idle_timeout_seconds > 0
    assert settings.allowed_origins == ("http://one.test", "http://two.test")

    monkeypatch.delenv("CHATTREE_SERVER_PYTHON")
    monkeypatch.delenv("CHATTREE_CLIENT_ALLOWED_ORIGINS")
    defaults = LauncherSettings.from_env(project_root=tmp_path)
    assert defaults.server_python == sys.executable
    assert defaults.allowed_origins == (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )


@pytest.mark.parametrize(
    ("name", "invalid_value"),
    [
        ("CHATTREE_CLIENT_CONNECT_TIMEOUT_SECONDS", "nan"),
        ("CHATTREE_CLIENT_CONNECT_TIMEOUT_SECONDS", "inf"),
        ("CHATTREE_CLIENT_CONNECT_TIMEOUT_SECONDS", "60.1"),
        ("CHATTREE_CLIENT_START_TIMEOUT_SECONDS", "nan"),
        ("CHATTREE_CLIENT_START_TIMEOUT_SECONDS", "inf"),
        ("CHATTREE_CLIENT_START_TIMEOUT_SECONDS", "600.1"),
        ("CHATTREE_CLIENT_POLL_INTERVAL_SECONDS", "nan"),
        ("CHATTREE_CLIENT_POLL_INTERVAL_SECONDS", "inf"),
        ("CHATTREE_CLIENT_POLL_INTERVAL_SECONDS", "10.1"),
        ("CHATTREE_CLIENT_PROXY_IDLE_TIMEOUT_SECONDS", "nan"),
        ("CHATTREE_CLIENT_PROXY_IDLE_TIMEOUT_SECONDS", "inf"),
        ("CHATTREE_CLIENT_PROXY_IDLE_TIMEOUT_SECONDS", "3600.1"),
    ],
)
def test_launcher_settings_reject_non_finite_or_excessive_float_timeouts(
    tmp_path: Path,
    name: str,
    invalid_value: str,
):
    with pytest.raises(ValueError, match=name):
        LauncherSettings.from_env(
            project_root=tmp_path,
            environ={name: invalid_value},
        )


def test_launcher_settings_accept_normal_float_timeouts(tmp_path: Path):
    settings = LauncherSettings.from_env(
        project_root=tmp_path,
        environ={
            "CHATTREE_CLIENT_CONNECT_TIMEOUT_SECONDS": "5.5",
            "CHATTREE_CLIENT_START_TIMEOUT_SECONDS": "120",
            "CHATTREE_CLIENT_POLL_INTERVAL_SECONDS": "1.5",
            "CHATTREE_CLIENT_PROXY_IDLE_TIMEOUT_SECONDS": "900",
        },
    )

    assert settings.connect_timeout_seconds == 5.5
    assert settings.start_timeout_seconds == 120.0
    assert settings.poll_interval_seconds == 1.5
    assert settings.proxy_idle_timeout_seconds == 900.0


def test_missing_store_seeds_and_persists_default_local_profile(tmp_path: Path):
    path = tmp_path / "profiles.json"
    server_home = tmp_path / "server-home"

    store = ProfileStore(path, default_server_home=server_home)

    profiles = store.list()
    assert len(profiles) == 1
    default = profiles[0]
    assert default == ServerProfile(
        id=DEFAULT_LOCAL_PROFILE_ID,
        label="Local",
        kind="local",
        auto_connect=True,
        bound_server_instance_id=None,
        local=LocalTarget(
            server_home=str(server_home.resolve()),
            server_port=DEFAULT_LOCAL_SERVER_PORT,
        ),
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": PROFILES_SCHEMA_VERSION,
        "profiles": [default.to_dict()],
    }


def test_profile_and_session_models_have_stable_wire_shapes(tmp_path: Path):
    profile = _profile("work", tmp_path / "work", bound_server_instance_id="server-a")
    assert profile.to_dict() == {
        "id": "work",
        "label": "work",
        "kind": "local",
        "auto_connect": False,
        "bound_server_instance_id": "server-a",
        "local": {
            "server_home": str((tmp_path / "work").resolve()),
            "server_port": 8100,
        },
    }

    session = ServerSession(
        profile_id="work",
        status="error",
        phase="handshake",
        connection_epoch=3,
        connection_lease_id=LEASE_A,
        server_instance_id="server-a",
        error=ConnectionErrorInfo(
            code="protocol_mismatch",
            message="Expected protocol 1",
            retryable=False,
        ),
    )
    assert session.to_dict() == {
        "profile_id": "work",
        "status": "error",
        "phase": "handshake",
        "connection_epoch": 3,
        "connection_lease_id": LEASE_A,
        "server_instance_id": "server-a",
        "error": {
            "code": "protocol_mismatch",
            "message": "Expected protocol 1",
            "retryable": False,
        },
    }

    with pytest.raises(ValueError, match="connection_lease_id"):
        ServerSession(
            profile_id="work",
            connection_lease_id=LEASE_A.upper(),
        )


def test_crud_normalizes_home_and_update_never_changes_binding(tmp_path: Path):
    store = ProfileStore(
        tmp_path / "profiles.json",
        default_server_home=tmp_path / "default",
    )
    original = _profile(
        "work",
        tmp_path / "nested" / ".." / "work",
        bound_server_instance_id="server-a",
    )

    created = store.create(original)
    assert created.local.server_home == str((tmp_path / "work").resolve())
    assert store.get("work") == created

    updated = store.update(
        "work",
        label="Workstation",
        auto_connect=True,
        local=LocalTarget(str(tmp_path / "moved"), 8200),
    )
    assert updated.label == "Workstation"
    assert updated.auto_connect is True
    assert updated.local.server_home == str((tmp_path / "moved").resolve())
    assert updated.bound_server_instance_id == "server-a"

    assert store.delete("work") == updated
    with pytest.raises(LauncherError) as exc_info:
        store.get("work")
    assert exc_info.value.code == "profile_not_found"


def test_create_rejects_local_port_owned_by_another_home(tmp_path: Path):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path, default_server_home=tmp_path / "default")
    store.create(_profile("first", tmp_path / "first", port=8100))
    before_profiles = store.list()
    before_bytes = path.read_bytes()

    with pytest.raises(LauncherError) as exc_info:
        store.create(_profile("second", tmp_path / "second", port=8100))

    assert exc_info.value.code == "profile_port_duplicate"
    assert store.list() == before_profiles
    assert path.read_bytes() == before_bytes


def test_update_rejects_local_port_owned_by_another_home(tmp_path: Path):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path, default_server_home=tmp_path / "default")
    store.create(_profile("first", tmp_path / "first", port=8100))
    original = store.create(_profile("second", tmp_path / "second", port=8101))
    before_bytes = path.read_bytes()

    with pytest.raises(LauncherError) as exc_info:
        store.update(
            "second",
            local=LocalTarget(str(tmp_path / "second"), 8100),
        )

    assert exc_info.value.code == "profile_port_duplicate"
    assert store.get("second") == original
    assert path.read_bytes() == before_bytes


def test_persisted_profiles_with_different_homes_on_same_port_fail_closed(
    tmp_path: Path,
):
    path = tmp_path / "profiles.json"
    _write_document(
        path,
        [
            _profile(
                DEFAULT_LOCAL_PROFILE_ID,
                tmp_path / "default",
                port=8100,
            ).to_dict(),
            _profile("other", tmp_path / "other", port=8100).to_dict(),
        ],
    )

    with pytest.raises(LauncherError) as exc_info:
        ProfileStore(path, default_server_home=tmp_path / "default")

    assert exc_info.value.code == "profile_port_duplicate"


def test_default_local_profile_cannot_be_deleted(tmp_path: Path):
    store = ProfileStore(
        tmp_path / "profiles.json",
        default_server_home=tmp_path / "default",
    )

    with pytest.raises(LauncherError) as exc_info:
        store.delete(DEFAULT_LOCAL_PROFILE_ID)

    assert exc_info.value.code == "default_profile_required"
    assert store.get(DEFAULT_LOCAL_PROFILE_ID).kind == "local"


def test_bind_is_idempotent_and_requires_explicit_rebind(tmp_path: Path):
    store = ProfileStore(
        tmp_path / "profiles.json",
        default_server_home=tmp_path / "default",
    )

    first = store.bind(DEFAULT_LOCAL_PROFILE_ID, "server-a")
    assert first.bound_server_instance_id == "server-a"
    assert store.bind(DEFAULT_LOCAL_PROFILE_ID, "server-a") == first

    with pytest.raises(LauncherError) as exc_info:
        store.bind(DEFAULT_LOCAL_PROFILE_ID, "server-b")
    assert exc_info.value.code == "server_identity_changed"
    assert store.get(DEFAULT_LOCAL_PROFILE_ID) == first

    rebound = store.rebind(DEFAULT_LOCAL_PROFILE_ID, "server-b")
    assert rebound.bound_server_instance_id == "server-b"


def test_binding_and_rebinding_reject_instance_owned_by_another_profile(
    tmp_path: Path,
):
    store = ProfileStore(
        tmp_path / "profiles.json",
        default_server_home=tmp_path / "default",
    )
    store.create(_profile("other", tmp_path / "other"))
    store.bind(DEFAULT_LOCAL_PROFILE_ID, "server-a")

    with pytest.raises(LauncherError) as bind_error:
        store.bind("other", "server-a")
    assert bind_error.value.code == "server_instance_already_bound"

    store.bind("other", "server-b")
    with pytest.raises(LauncherError) as rebind_error:
        store.rebind("other", "server-a")
    assert rebind_error.value.code == "server_instance_already_bound"
    assert store.get("other").bound_server_instance_id == "server-b"


def test_ensure_ssh_profile_uses_stable_alias_id_and_reuses_existing_profile(
    tmp_path: Path,
):
    store = ProfileStore(
        tmp_path / "profiles.json",
        default_server_home=tmp_path / "default",
    )

    created = store.ensure_ssh_profile("gpu-box")
    reused = store.ensure_ssh_profile("gpu-box")

    assert created == reused
    assert created.id == ssh_profile_id("gpu-box")
    assert created.kind == "ssh"
    assert created.ssh == SshTarget(config_host="gpu-box")
    assert created.local is None
    assert created.auto_connect is False


def test_v2_ssh_profiles_load_from_disk(tmp_path: Path):
    path = tmp_path / "profiles.json"
    local = _profile(DEFAULT_LOCAL_PROFILE_ID, tmp_path / "default")
    ssh = ServerProfile(
        id=ssh_profile_id("gpu-box"),
        label="SSH: gpu-box",
        kind="ssh",
        auto_connect=False,
        bound_server_instance_id=None,
        ssh=SshTarget(config_host="gpu-box"),
    )
    _write_document(path, [local.to_dict(), ssh.to_dict()], version=2)

    store = ProfileStore(path, default_server_home=tmp_path / "default")

    assert store.get(ssh.id) == ssh


def test_duplicate_ssh_host_in_persisted_file_fails_closed(tmp_path: Path):
    path = tmp_path / "profiles.json"
    local = _profile(DEFAULT_LOCAL_PROFILE_ID, tmp_path / "default")
    first = ServerProfile(
        id="ssh:first",
        label="SSH one",
        kind="ssh",
        auto_connect=False,
        bound_server_instance_id=None,
        ssh=SshTarget(config_host="gpu-box"),
    )
    second = ServerProfile(
        id="ssh:second",
        label="SSH two",
        kind="ssh",
        auto_connect=False,
        bound_server_instance_id=None,
        ssh=SshTarget(config_host="gpu-box"),
    )
    _write_document(
        path,
        [local.to_dict(), first.to_dict(), second.to_dict()],
        version=2,
    )

    with pytest.raises(LauncherError) as exc_info:
        ProfileStore(path, default_server_home=tmp_path / "default")

    assert exc_info.value.code == "ssh_host_duplicate"


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda document: document.update(schema_version=99), "profiles_version_unsupported"),
        (lambda document: document["profiles"].append(document["profiles"][0]), "profile_id_duplicate"),
        (
            lambda document: document["profiles"].append(
                {
                    **document["profiles"][0],
                    "id": "duplicate-home",
                }
            ),
            "profile_home_duplicate",
        ),
        (
            lambda document: document["profiles"].append(
                {
                    **document["profiles"][0],
                    "id": "duplicate-instance",
                    "local": {
                        "server_home": "other-home",
                        "server_port": 8101,
                    },
                }
            ),
            "server_instance_already_bound",
        ),
        (lambda document: document.update(extra=True), "profiles_invalid"),
    ],
)
def test_invalid_persisted_documents_fail_closed(
    tmp_path: Path,
    mutate,
    expected_code: str,
):
    path = tmp_path / "profiles.json"
    profile = _profile(
        DEFAULT_LOCAL_PROFILE_ID,
        tmp_path / "default",
        bound_server_instance_id="server-a",
    )
    document = {
        "schema_version": PROFILES_SCHEMA_VERSION,
        "profiles": [profile.to_dict()],
    }
    mutate(document)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(LauncherError) as exc_info:
        ProfileStore(path, default_server_home=tmp_path / "default")

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize("payload", ["{", "[]", "null"])
def test_malformed_profile_files_fail_closed(tmp_path: Path, payload: str):
    path = tmp_path / "profiles.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(LauncherError) as exc_info:
        ProfileStore(path, default_server_home=tmp_path / "default")

    assert exc_info.value.code == "profiles_invalid"


def test_missing_default_profile_in_existing_file_fails_closed(tmp_path: Path):
    path = tmp_path / "profiles.json"
    _write_document(path, [_profile("other", tmp_path / "other").to_dict()])

    with pytest.raises(LauncherError) as exc_info:
        ProfileStore(path, default_server_home=tmp_path / "default")

    assert exc_info.value.code == "default_profile_required"


def test_failed_atomic_write_preserves_disk_and_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path, default_server_home=tmp_path / "default")
    before_profiles = store.list()
    before_bytes = path.read_bytes()

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("client_launcher.profiles.atomic_write_json", fail_write)
    with pytest.raises(LauncherError) as exc_info:
        store.create(_profile("work", tmp_path / "work"))

    assert exc_info.value.code == "profiles_write_failed"
    assert exc_info.value.retryable is True
    assert isinstance(exc_info.value.__cause__, OSError)
    assert store.list() == before_profiles
    assert path.read_bytes() == before_bytes


def test_concurrent_creates_are_serialized_without_lost_updates(tmp_path: Path):
    store = ProfileStore(
        tmp_path / "profiles.json",
        default_server_home=tmp_path / "default",
    )

    def create(index: int) -> None:
        store.create(
            _profile(
                f"profile-{index}",
                tmp_path / f"home-{index}",
                port=8100 + index,
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(create, range(20)))

    assert {profile.id for profile in store.list()} == {
        DEFAULT_LOCAL_PROFILE_ID,
        *(f"profile-{index}" for index in range(20)),
    }
    reloaded = ProfileStore(path=tmp_path / "profiles.json")
    assert reloaded.list() == store.list()


@pytest.mark.parametrize("port", [0, 65536, True, "8001"])
def test_local_target_rejects_invalid_ports(tmp_path: Path, port):
    with pytest.raises(ValueError, match="server_port"):
        LocalTarget(server_home=str(tmp_path), server_port=port)


def test_launcher_error_exposes_stable_transport_fields():
    error = LauncherError(
        code="profile_conflict",
        message="Profile already exists",
        retryable=False,
        status_code=409,
    )
    assert str(error) == "Profile already exists"
    assert error.code == "profile_conflict"
    assert error.retryable is False
    assert error.status_code == 409
