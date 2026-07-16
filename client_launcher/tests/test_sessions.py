import asyncio

import pytest

from client_launcher.local_server import ConnectedServer
from client_launcher.models import LauncherError
from client_launcher.profiles import ProfileStore
from client_launcher.sessions import SessionManager


SERVER_A = "11111111-1111-4111-8111-111111111111"
SERVER_B = "22222222-2222-4222-8222-222222222222"


class FakeConnector:
    def __init__(self, instance_id=SERVER_A):
        self.instance_id = instance_id
        self.calls = 0
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.ignore_cancellation = False

    async def connect(self, profile, phase_callback):
        self.calls += 1
        phase_callback("health")
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            if not self.ignore_cancellation:
                raise
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


def _store(tmp_path):
    return ProfileStore(
        tmp_path / "profiles.json",
        default_server_home=tmp_path / "server-home",
    )


async def _concurrent_connect_is_singleflight_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    manager = SessionManager(store, connector)

    first = asyncio.create_task(manager.connect("local"))
    await connector.started.wait()
    second = asyncio.create_task(manager.connect("local"))
    await asyncio.sleep(0)
    connector.release.set()

    first_status, second_status = await asyncio.gather(first, second)

    assert connector.calls == 1
    assert first_status.status == "ready"
    assert second_status.status == "ready"
    assert first_status.connection_epoch == 1
    assert second_status.connection_epoch == 1
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


def test_ready_connect_is_idempotent(tmp_path):
    asyncio.run(_ready_connect_is_idempotent_case(tmp_path))


async def _stale_transport_error_does_not_break_reconnected_session_case(tmp_path):
    store = _store(tmp_path)
    connector = FakeConnector()
    connector.release.set()
    manager = SessionManager(store, connector)

    first = await manager.connect("local")
    await manager.disconnect("local")
    second = await manager.connect("local")
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

    manager.mark_error(
        "local",
        transport_error,
        connection_epoch=second.connection_epoch,
    )
    assert manager.status("local").status == "error"


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
    assert store.get("local").bound_server_instance_id == SERVER_A

    rebound = await manager.connect(
        "local",
        rebind=True,
        expected_server_instance_id=SERVER_B,
    )

    assert first.connection_epoch == 1
    assert rebound.connection_epoch == 2
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
