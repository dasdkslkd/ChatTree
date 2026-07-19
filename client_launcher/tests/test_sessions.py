import asyncio
from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from client_launcher.http_errors import REQUEST_ID_RE
from client_launcher.local_server import (
    ConnectedServer,
    LocalServerShutdownUncertainError,
    LocalServerStopTimeoutError,
)
from client_launcher.models import LauncherError, LocalTarget, ServerProfile
from client_launcher.profiles import ProfileStore
from client_launcher.sessions import SessionManager


SERVER_A = "11111111-1111-4111-8111-111111111111"
SERVER_B = "22222222-2222-4222-8222-222222222222"


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        return False


class FakeConnector:
    def __init__(self, instance_id=SERVER_A):
        self.instance_id = instance_id
        self.calls = 0
        self.request_ids: list[str] = []
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.ignore_cancellation = False
        self.shutdown_calls: list[tuple[str, str, str | None]] = []
        self.disconnect_calls: list[str] = []
        self.shutdown_error: BaseException | None = None
        self.wait_stopped_calls: list[tuple[str, object, float]] = []
        self.wait_stopped_error: BaseException | None = None
        self.shutdown_started = asyncio.Event()
        self.shutdown_release = asyncio.Event()
        self.shutdown_release.set()
        self.wait_stopped_started = asyncio.Event()
        self.wait_stopped_release = asyncio.Event()
        self.wait_stopped_release.set()

    async def connect(
        self,
        profile,
        phase_callback,
        *,
        request_id: str | None = None,
    ):
        self.calls += 1
        assert request_id is not None
        self.request_ids.append(request_id)
        phase_callback("handshake")
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            if not self.ignore_cancellation:
                raise
        return ConnectedServer(
            endpoint=(
                f"http://127.0.0.1:{profile.local.server_port}"
                if profile.local is not None
                else "http://127.0.0.1:19081"
            ),
            server_instance_id=self.instance_id,
            handshake={
                "server_instance_id": self.instance_id,
                "protocol_version": 1,
            },
        )

    async def disconnect(self, profile):
        self.disconnect_calls.append(profile.id)

    async def request_shutdown(
        self,
        profile,
        expected_server_instance_id: str,
        *,
        request_id: str | None = None,
    ):
        self.shutdown_calls.append(
            (profile.id, expected_server_instance_id, request_id)
        )
        self.shutdown_started.set()
        await self.shutdown_release.wait()
        if self.shutdown_error is not None:
            raise self.shutdown_error
        return (
            f"http://127.0.0.1:{profile.local.server_port}",
            profile.local.server_home,
        )

    def shutdown_target(self, profile):
        return (
            f"http://127.0.0.1:{profile.local.server_port}",
            profile.local.server_home,
        )

    async def wait_stopped(self, endpoint, server_home, *, timeout: float):
        self.wait_stopped_calls.append((endpoint, server_home, timeout))
        self.wait_stopped_started.set()
        await self.wait_stopped_release.wait()
        if self.wait_stopped_error is not None:
            raise self.wait_stopped_error

    async def close(self):
        self.closed = True


def _store(tmp_path):
    return ProfileStore(
        tmp_path / "profiles.json",
        default_server_home=tmp_path / "server-home",
    )


def test_disconnected_epoch_zero_has_a_canonical_connection_lease(tmp_path):
    manager = SessionManager(_store(tmp_path), FakeConnector())

    status = manager.status("local")

    assert status.status == "disconnected"
    assert status.connection_epoch == 0
    assert _is_canonical_uuid(status.connection_lease_id)


async def _concurrent_connect_is_singleflight_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    manager = SessionManager(store, connector)
    initial_lease_id = manager.status("local").connection_lease_id

    first = asyncio.create_task(
        manager.connect("local", request_id="creator-tree")
    )
    await connector.started.wait()
    second = asyncio.create_task(
        manager.connect("local", request_id="joiner-tree")
    )
    await asyncio.sleep(0)
    connector.release.set()

    first_status, second_status = await asyncio.gather(first, second)

    assert connector.calls == 1
    assert connector.request_ids == ["creator-tree"]
    assert first_status.status == "ready"
    assert second_status.status == "ready"
    assert first_status.connection_epoch == 1
    assert second_status.connection_epoch == 1
    assert first_status.connection_lease_id == second_status.connection_lease_id
    assert first_status.connection_lease_id != initial_lease_id
    assert _is_canonical_uuid(first_status.connection_lease_id)
    assert store.get("local").bound_server_instance_id == SERVER_A


def test_concurrent_connect_is_singleflight(tmp_path):
    asyncio.run(_concurrent_connect_is_singleflight_case(tmp_path))


async def _concurrent_connect_rejects_conflicting_intent_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector(SERVER_A)
    manager = SessionManager(store, connector)

    first = asyncio.create_task(manager.connect("local"))
    await connector.started.wait()
    conflicting = asyncio.create_task(
        manager.connect(
            "local",
            rebind=True,
            expected_server_instance_id=SERVER_B,
        )
    )
    await asyncio.sleep(0)
    connector.release.set()

    first_status = await first
    with pytest.raises(LauncherError) as exc_info:
        await conflicting

    assert exc_info.value.code == "connection_intent_conflict"
    assert exc_info.value.status_code == 409
    assert connector.calls == 1
    assert first_status.server_instance_id == SERVER_A


def test_concurrent_connect_rejects_conflicting_intent(tmp_path):
    asyncio.run(_concurrent_connect_rejects_conflicting_intent_case(tmp_path))


async def _ready_connect_is_idempotent_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    connector.release.set()
    manager = SessionManager(store, connector)

    first = await manager.connect("local")
    second = await manager.connect("local")

    assert connector.calls == 1
    assert first.connection_epoch == second.connection_epoch == 1
    assert first.connection_lease_id == second.connection_lease_id


def test_ready_connect_is_idempotent(tmp_path):
    asyncio.run(_ready_connect_is_idempotent_case(tmp_path))


async def _stale_transport_error_does_not_break_reconnected_session_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    connector.release.set()
    manager = SessionManager(store, connector)

    first = await manager.connect("local")
    first_lease = manager.resolve_endpoint("local")
    assert first_lease.profile_id == "local"
    assert first_lease.server_instance_id == SERVER_A
    assert first_lease.connection_epoch == first.connection_epoch
    assert first_lease.connection_lease_id == first.connection_lease_id
    assert first_lease.invalidated is not None
    assert not first_lease.invalidated.is_set()
    disconnected = await manager.disconnect("local")
    assert disconnected.connection_lease_id != first.connection_lease_id
    assert _is_canonical_uuid(disconnected.connection_lease_id)
    assert first_lease.invalidated.is_set()
    with pytest.raises(FrozenInstanceError):
        first_lease.connection_epoch = 99
    second = await manager.connect("local")
    second_lease = manager.resolve_endpoint("local")
    assert second.connection_lease_id != disconnected.connection_lease_id
    assert second.connection_lease_id != first.connection_lease_id
    assert second_lease.connection_lease_id == second.connection_lease_id
    assert second_lease.invalidated is not None
    transport_error = LauncherError(
        "proxy_upstream_unavailable",
        "Unable to reach the Server",
        retryable=True,
        status_code=502,
    )

    manager.mark_error(
        "local",
        transport_error,
        connection_epoch=first.connection_epoch,
    )

    current = manager.status("local")
    assert current.status == "ready"
    assert current.connection_epoch == second.connection_epoch == 2
    assert not second_lease.invalidated.is_set()

    manager.mark_error(
        "local",
        transport_error,
        connection_epoch=second.connection_epoch,
    )
    errored = manager.status("local")
    assert errored.status == "error"
    assert _is_canonical_uuid(errored.connection_lease_id)
    assert second_lease.invalidated.is_set()


def test_stale_transport_error_does_not_break_reconnected_session(tmp_path):
    asyncio.run(
        _stale_transport_error_does_not_break_reconnected_session_case(tmp_path)
    )


async def _ready_rebind_rejects_expected_identity_mismatch_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector(SERVER_A)
    connector.release.set()
    manager = SessionManager(store, connector)

    await manager.connect("local")

    with pytest.raises(LauncherError) as exc_info:
        await manager.connect(
            "local",
            rebind=True,
            expected_server_instance_id=SERVER_B,
        )

    assert exc_info.value.code == "rebind_identity_mismatch"
    assert manager.status("local").server_instance_id == SERVER_A
    assert connector.calls == 1


def test_ready_rebind_rejects_expected_identity_mismatch(tmp_path):
    asyncio.run(_ready_rebind_rejects_expected_identity_mismatch_case(tmp_path))


async def _disconnect_blocks_late_ready_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    connector.ignore_cancellation = True
    manager = SessionManager(store, connector)

    connect_task = asyncio.create_task(manager.connect("local"))
    await connector.started.wait()
    disconnected = await manager.disconnect("local")

    with pytest.raises(LauncherError) as exc_info:
        await connect_task

    assert exc_info.value.code == "connection_cancelled"
    assert disconnected.status == "disconnected"
    assert manager.status("local").status == "disconnected"
    assert store.get("local").bound_server_instance_id is None


def test_disconnect_blocks_late_ready_and_binding(tmp_path):
    asyncio.run(_disconnect_blocks_late_ready_case(tmp_path))


async def _disconnect_before_inner_task_starts_allows_reconnect_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    manager = SessionManager(store, connector)

    connect_task = asyncio.create_task(manager.connect("local"))
    await asyncio.sleep(0)
    await manager.disconnect("local")
    with pytest.raises(LauncherError) as exc_info:
        await connect_task
    assert exc_info.value.code == "connection_cancelled"

    connector.release.set()
    reconnected = await manager.connect("local")

    assert reconnected.status == "ready"
    assert reconnected.connection_epoch == 1


def test_disconnect_before_inner_task_starts_allows_reconnect(tmp_path):
    asyncio.run(_disconnect_before_inner_task_starts_allows_reconnect_case(tmp_path))


async def _auto_connect_start_then_immediate_disconnect_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    manager = SessionManager(store, connector)

    await manager.start()
    background_tasks = list(manager._background_tasks)
    disconnected = await manager.disconnect("local")
    connector.release.set()
    await asyncio.gather(*background_tasks, return_exceptions=True)

    assert disconnected.status == "disconnected"
    assert manager.status("local").status == "disconnected"
    assert store.get("local").bound_server_instance_id is None


def test_auto_connect_start_then_immediate_disconnect_stays_disconnected(tmp_path):
    asyncio.run(_auto_connect_start_then_immediate_disconnect_case(tmp_path))


async def _auto_connect_generates_a_new_tree_id_per_attempt_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    connector.release.set()
    manager = SessionManager(store, connector)

    await manager.start()
    first_tasks = list(manager._background_tasks)
    await asyncio.gather(*first_tasks)
    await manager.disconnect("local")
    await manager.start()
    second_tasks = list(manager._background_tasks)
    await asyncio.gather(*second_tasks)

    assert connector.calls == 2
    assert len(connector.request_ids) == 2
    assert connector.request_ids[0] != connector.request_ids[1]
    assert all(
        request_id.startswith("req_") and REQUEST_ID_RE.fullmatch(request_id)
        for request_id in connector.request_ids
    )


def test_auto_connect_generates_a_new_tree_id_per_attempt(tmp_path):
    asyncio.run(_auto_connect_generates_a_new_tree_id_per_attempt_case(tmp_path))


async def _delete_profile_serializes_against_connect_case(tmp_path):
    store = _store(tmp_path)
    store.create(
        ServerProfile(
            id="work",
            label="Work",
            kind="local",
            auto_connect=False,
            bound_server_instance_id=None,
            local=LocalTarget(str(tmp_path / "work-server"), 8100),
        )
    )
    connector = FakeConnector()
    manager = SessionManager(store, connector)

    first_connect = asyncio.create_task(manager.connect("work"))
    await connector.started.wait()
    delete_task = asyncio.create_task(manager.delete_profile("work"))
    await asyncio.sleep(0)
    late_connect = asyncio.create_task(manager.connect("work"))
    connector.release.set()

    deleted = await delete_task
    with pytest.raises(LauncherError) as first_error:
        await first_connect
    with pytest.raises(LauncherError) as late_error:
        await late_connect

    assert deleted.id == "work"
    assert first_error.value.code == "connection_cancelled"
    assert late_error.value.code == "profile_not_found"
    assert connector.calls == 1
    assert [profile.id for profile in store.list()] == ["local"]


def test_delete_profile_serializes_against_connect(tmp_path):
    asyncio.run(_delete_profile_serializes_against_connect_case(tmp_path))


async def _ssh_duplicate_instance_connect_disconnects_tunnel_case(tmp_path):
    store = _store(tmp_path)
    ssh_profile = store.ensure_ssh_profile("gpu-box")
    connector = FakeConnector(SERVER_A)
    connector.release.set()
    manager = SessionManager(store, {"local": connector, "ssh": connector})

    await manager.connect("local")

    with pytest.raises(LauncherError) as exc_info:
        await manager.connect(ssh_profile.id)

    assert exc_info.value.code == "server_instance_already_bound"
    assert connector.disconnect_calls == [ssh_profile.id]
    with pytest.raises(LauncherError) as profile_error:
        store.get(ssh_profile.id)
    assert profile_error.value.code == "profile_not_found"


def test_ssh_duplicate_instance_connect_disconnects_tunnel(tmp_path):
    asyncio.run(_ssh_duplicate_instance_connect_disconnects_tunnel_case(tmp_path))


async def _identity_change_requires_explicit_rebind_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector(SERVER_A)
    connector.release.set()
    manager = SessionManager(store, connector)

    first = await manager.connect("local")
    await manager.disconnect("local")
    connector.instance_id = SERVER_B

    with pytest.raises(LauncherError) as exc_info:
        await manager.connect("local")
    assert exc_info.value.code == "server_identity_changed"
    errored = manager.status("local")
    assert errored.status == "error"
    assert _is_canonical_uuid(errored.connection_lease_id)
    assert store.get("local").bound_server_instance_id == SERVER_A

    rebound = await manager.connect(
        "local",
        rebind=True,
        expected_server_instance_id=SERVER_B,
    )

    assert first.connection_epoch == 1
    assert rebound.connection_epoch == 2
    assert rebound.connection_lease_id != errored.connection_lease_id
    assert rebound.connection_lease_id != first.connection_lease_id
    assert rebound.server_instance_id == SERVER_B
    assert store.get("local").bound_server_instance_id == SERVER_B


def test_identity_change_requires_explicit_rebind(tmp_path):
    asyncio.run(_identity_change_requires_explicit_rebind_case(tmp_path))


async def _close_only_closes_connector_case(tmp_path):
    connector = FakeConnector()
    manager = SessionManager(_store(tmp_path), connector)

    await manager.close()

    assert connector.closed is True


def test_close_only_closes_launcher_connector(tmp_path):
    asyncio.run(_close_only_closes_connector_case(tmp_path))


async def _stop_invalidates_lease_after_server_accepts_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    connector.release.set()
    manager = SessionManager(store, connector)
    ready = await manager.connect("local")
    lease = manager.resolve_endpoint("local")

    stopped = await manager.stop(
        "local",
        expected_server_instance_id=SERVER_A,
        timeout=7,
        request_id="stop-tree",
    )

    assert stopped.status == "disconnected"
    assert lease.invalidated.is_set()
    assert manager.status("local").status == "disconnected"
    assert manager.status("local").connection_lease_id != ready.connection_lease_id
    assert connector.shutdown_calls == [("local", SERVER_A, "stop-tree")]
    assert connector.wait_stopped_calls == [
        (
            "http://127.0.0.1:8001",
            store.get("local").local.server_home,
            7,
        )
    ]


def test_stop_invalidates_lease_after_server_accepts(tmp_path):
    asyncio.run(_stop_invalidates_lease_after_server_accepts_case(tmp_path))


async def _stop_reconciles_lost_shutdown_response_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    connector.release.set()
    manager = SessionManager(store, connector)
    await manager.connect("local")
    lease = manager.resolve_endpoint("local")
    endpoint = "http://127.0.0.1:8001"
    server_home = store.get("local").local.server_home
    connector.shutdown_error = LocalServerShutdownUncertainError(
        endpoint,
        server_home,
        "shutdown response was lost",
    )

    stopped = await manager.stop(
        "local",
        expected_server_instance_id=SERVER_A,
        timeout=7,
    )

    assert stopped.status == "disconnected"
    assert manager.status("local").status == "disconnected"
    assert lease.invalidated.is_set()
    assert connector.wait_stopped_calls == [(endpoint, server_home, 7)]


def test_stop_reconciles_lost_shutdown_response(tmp_path):
    asyncio.run(_stop_reconciles_lost_shutdown_response_case(tmp_path))


async def _restart_reconciles_lost_shutdown_response_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    connector.release.set()
    manager = SessionManager(store, connector)
    first = await manager.connect("local")
    first_lease = manager.resolve_endpoint("local")
    connector.shutdown_error = LocalServerShutdownUncertainError(
        "http://127.0.0.1:8001",
        store.get("local").local.server_home,
        "shutdown response was lost",
    )

    restarted = await manager.restart(
        "local",
        expected_server_instance_id=SERVER_A,
        timeout=7,
    )

    assert restarted.status == "ready"
    assert restarted.connection_epoch == first.connection_epoch + 1
    assert first_lease.invalidated.is_set()
    assert connector.calls == 2


def test_restart_reconciles_lost_shutdown_response(tmp_path):
    asyncio.run(_restart_reconciles_lost_shutdown_response_case(tmp_path))


async def _lost_shutdown_response_timeout_leaves_disconnected_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    connector.release.set()
    manager = SessionManager(store, connector)
    await manager.connect("local")
    lease = manager.resolve_endpoint("local")
    endpoint = "http://127.0.0.1:8001"
    server_home = store.get("local").local.server_home
    connector.shutdown_error = LocalServerShutdownUncertainError(
        endpoint,
        server_home,
        "shutdown response was lost",
    )
    connector.wait_stopped_error = LocalServerStopTimeoutError(
        endpoint,
        server_home,
    )

    with pytest.raises(LocalServerStopTimeoutError):
        await manager.stop(
            "local",
            expected_server_instance_id=SERVER_A,
            timeout=1,
        )

    assert manager.status("local").status == "disconnected"
    assert lease.invalidated.is_set()
    with pytest.raises(LauncherError) as exc_info:
        manager.resolve_endpoint("local")
    assert exc_info.value.code == "profile_not_ready"


def test_lost_shutdown_response_timeout_leaves_disconnected(tmp_path):
    asyncio.run(_lost_shutdown_response_timeout_leaves_disconnected_case(tmp_path))


async def _cancelled_stop_request_reconciles_before_propagating_case(tmp_path):
    connector = FakeConnector()
    connector.release.set()
    connector.shutdown_release.clear()
    manager = SessionManager(_store(tmp_path), connector)
    await manager.connect("local")
    lease = manager.resolve_endpoint("local")

    stopping = asyncio.create_task(
        manager.stop(
            "local",
            expected_server_instance_id=SERVER_A,
            timeout=5,
        )
    )
    await connector.shutdown_started.wait()
    stopping.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert manager.status("local").status == "disconnected"
    assert lease.invalidated.is_set()
    assert connector.wait_stopped_calls == [
        (
            "http://127.0.0.1:8001",
            manager.profiles.get("local").local.server_home,
            5,
        )
    ]


def test_cancelled_stop_request_reconciles_before_propagating(tmp_path):
    asyncio.run(_cancelled_stop_request_reconciles_before_propagating_case(tmp_path))


async def _cancelled_restart_stops_without_spawning_replacement_case(tmp_path):
    connector = FakeConnector()
    connector.release.set()
    connector.shutdown_release.clear()
    manager = SessionManager(_store(tmp_path), connector)
    await manager.connect("local")
    lease = manager.resolve_endpoint("local")

    restarting = asyncio.create_task(
        manager.restart(
            "local",
            expected_server_instance_id=SERVER_A,
            timeout=5,
        )
    )
    await connector.shutdown_started.wait()
    restarting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await restarting

    assert manager.status("local").status == "disconnected"
    assert lease.invalidated.is_set()
    assert connector.calls == 1


def test_cancelled_restart_stops_without_spawning_replacement(tmp_path):
    asyncio.run(_cancelled_restart_stops_without_spawning_replacement_case(tmp_path))


async def _cancellation_during_stop_wait_does_not_abandon_reconciliation_case(
    tmp_path,
):
    connector = FakeConnector()
    connector.release.set()
    connector.wait_stopped_release.clear()
    manager = SessionManager(_store(tmp_path), connector)
    await manager.connect("local")
    lease = manager.resolve_endpoint("local")

    stopping = asyncio.create_task(
        manager.stop(
            "local",
            expected_server_instance_id=SERVER_A,
            timeout=5,
        )
    )
    await connector.wait_stopped_started.wait()
    stopping.cancel()
    await asyncio.sleep(0)

    assert not stopping.done()
    assert manager.status("local").status == "disconnected"
    assert lease.invalidated.is_set()

    connector.wait_stopped_release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping


def test_cancellation_during_stop_wait_does_not_abandon_reconciliation(tmp_path):
    asyncio.run(
        _cancellation_during_stop_wait_does_not_abandon_reconciliation_case(
            tmp_path
        )
    )


async def _stop_rejects_stale_expected_identity_case(tmp_path):
    connector = FakeConnector()
    connector.release.set()
    manager = SessionManager(_store(tmp_path), connector)
    await manager.connect("local")

    with pytest.raises(LauncherError) as exc_info:
        await manager.stop(
            "local",
            expected_server_instance_id=SERVER_B,
            timeout=5,
        )

    assert exc_info.value.code == "server_identity_mismatch"
    assert connector.shutdown_calls == []
    assert manager.status("local").status == "ready"


def test_stop_rejects_stale_expected_identity(tmp_path):
    asyncio.run(_stop_rejects_stale_expected_identity_case(tmp_path))


async def _restart_is_stop_then_one_connect_case(tmp_path):
    connector = FakeConnector()
    connector.release.set()
    manager = SessionManager(_store(tmp_path), connector)
    first = await manager.connect("local")
    first_lease = manager.resolve_endpoint("local")

    restarted = await manager.restart(
        "local",
        expected_server_instance_id=SERVER_A,
        timeout=9,
        request_id="restart-tree",
    )

    assert first_lease.invalidated.is_set()
    assert restarted.status == "ready"
    assert restarted.server_instance_id == SERVER_A
    assert restarted.connection_epoch == first.connection_epoch + 1
    assert restarted.connection_lease_id != first.connection_lease_id
    assert connector.calls == 2
    assert connector.shutdown_calls == [("local", SERVER_A, "restart-tree")]
    assert connector.request_ids == [connector.request_ids[0], "restart-tree"]


def test_restart_is_stop_then_one_connect(tmp_path):
    asyncio.run(_restart_is_stop_then_one_connect_case(tmp_path))


async def _profile_lifecycle_lock_serializes_stop_case(tmp_path):
    connector = FakeConnector()
    connector.release.set()
    connector.shutdown_release.clear()
    manager = SessionManager(_store(tmp_path), connector)
    await manager.connect("local")

    first = asyncio.create_task(
        manager.stop(
            "local",
            expected_server_instance_id=SERVER_A,
            timeout=5,
        )
    )
    await connector.shutdown_started.wait()
    second = asyncio.create_task(
        manager.stop(
            "local",
            expected_server_instance_id=SERVER_A,
            timeout=5,
        )
    )
    await asyncio.sleep(0)
    assert len(connector.shutdown_calls) == 1

    connector.shutdown_release.set()
    await first
    with pytest.raises(LauncherError) as exc_info:
        await second

    assert exc_info.value.code == "profile_not_ready"
    assert len(connector.shutdown_calls) == 1


def test_profile_lifecycle_lock_serializes_stop(tmp_path):
    asyncio.run(_profile_lifecycle_lock_serializes_stop_case(tmp_path))


async def _profile_lifecycle_lock_blocks_connect_until_stop_finishes_case(tmp_path):
    connector = FakeConnector()
    connector.release.set()
    manager = SessionManager(_store(tmp_path), connector)
    await manager.connect("local")
    connector.wait_stopped_release.clear()

    stopping = asyncio.create_task(
        manager.stop(
            "local",
            expected_server_instance_id=SERVER_A,
            timeout=5,
        )
    )
    await connector.wait_stopped_started.wait()
    reconnecting = asyncio.create_task(manager.connect("local"))
    await asyncio.sleep(0)

    assert connector.calls == 1
    assert not reconnecting.done()

    connector.wait_stopped_release.set()
    stopped, reconnected = await asyncio.gather(stopping, reconnecting)

    assert stopped.status == "disconnected"
    assert reconnected.status == "ready"
    assert connector.calls == 2


def test_profile_lifecycle_lock_blocks_connect_until_stop_finishes(tmp_path):
    asyncio.run(
        _profile_lifecycle_lock_blocks_connect_until_stop_finishes_case(tmp_path)
    )
