from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from client_launcher.http_errors import canonical_request_id
from client_launcher.models import (
    ConnectionErrorInfo,
    EndpointLease,
    LauncherError,
    LocalTarget,
    ServerProfile,
    ServerSession,
)
from client_launcher.profiles import ProfileStore
from client_launcher.settings import DEFAULT_LOCAL_PROFILE_ID


class SessionManager:
    def __init__(self, profiles: ProfileStore, connector: Any) -> None:
        self.profiles = profiles
        self.connector = connector
        self._sessions: dict[str, ServerSession] = {}
        self._endpoints: dict[str, str] = {}
        self._lease_invalidations: dict[str, asyncio.Event] = {}
        self._attempt_generation: dict[str, int] = {}
        self._connect_tasks: dict[str, asyncio.Task[ServerSession]] = {}
        self._connect_intents: dict[str, tuple[bool, str | None]] = {}
        self._background_tasks: set[asyncio.Task[object]] = set()
        self._guard = asyncio.Lock()
        self._closed = False

    def status(self, profile_id: str) -> ServerSession:
        self.profiles.get(profile_id)
        return replace(self._session(profile_id))

    def _session(self, profile_id: str) -> ServerSession:
        return self._sessions.setdefault(
            profile_id,
            ServerSession(profile_id=profile_id),
        )

    def resolve_endpoint(self, profile_id: str) -> EndpointLease:
        self.profiles.get(profile_id)
        session = self._session(profile_id)
        endpoint = self._endpoints.get(profile_id)
        if session.status != "ready" or not endpoint:
            raise LauncherError(
                "profile_not_ready",
                f"Profile {profile_id} is not connected",
                retryable=True,
                status_code=503,
            )
        return EndpointLease(
            endpoint=endpoint,
            connection_epoch=session.connection_epoch,
            invalidated=self._lease_invalidations.get(profile_id),
        )

    def mark_error(
        self,
        profile_id: str,
        error: LauncherError,
        *,
        connection_epoch: int,
    ) -> bool:
        try:
            self.profiles.get(profile_id)
        except LauncherError as exc:
            if exc.code == "profile_not_found":
                return False
            raise
        session = self._sessions.get(profile_id)
        if (
            session is None
            or session.status != "ready"
            or session.connection_epoch != connection_epoch
        ):
            return False
        session.status = "error"
        session.phase = None
        session.server_instance_id = None
        session.error = ConnectionErrorInfo(
            code=error.code,
            message=error.message,
            retryable=error.retryable,
            details=error.details or None,
        )
        self._invalidate_lease(profile_id)
        self._endpoints.pop(profile_id, None)
        return True

    async def start(self) -> None:
        for profile in self.profiles.list():
            if not profile.auto_connect:
                continue
            prepared = await self._prepare_connect(
                profile.id,
                rebind=False,
                expected_server_instance_id=None,
                request_id=canonical_request_id(None),
            )
            if isinstance(prepared, ServerSession):
                continue
            if prepared not in self._background_tasks:
                self._background_tasks.add(prepared)
                prepared.add_done_callback(self._background_done)

    async def connect(
        self,
        profile_id: str,
        *,
        rebind: bool = False,
        expected_server_instance_id: str | None = None,
        request_id: str | None = None,
    ) -> ServerSession:
        request_id = canonical_request_id(request_id)
        prepared = await self._prepare_connect(
            profile_id,
            rebind=rebind,
            expected_server_instance_id=expected_server_instance_id,
            request_id=request_id,
        )
        if isinstance(prepared, ServerSession):
            return prepared
        task = prepared

        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise LauncherError(
                    "connection_cancelled",
                    f"Connection for Profile {profile_id} was cancelled",
                    retryable=True,
                    status_code=409,
                ) from None
            raise

    async def _prepare_connect(
        self,
        profile_id: str,
        *,
        rebind: bool,
        expected_server_instance_id: str | None,
        request_id: str,
    ) -> ServerSession | asyncio.Task[ServerSession]:
        if self._closed:
            raise LauncherError(
                "launcher_stopping",
                "Client Launcher is stopping",
                retryable=True,
                status_code=503,
            )
        if rebind and not expected_server_instance_id:
            raise LauncherError(
                "rebind_confirmation_required",
                "Explicit rebind requires expected_server_instance_id",
                retryable=False,
                status_code=422,
            )
        intent = (
            rebind,
            expected_server_instance_id if rebind else None,
        )

        async with self._guard:
            self.profiles.get(profile_id)
            current = self._session(profile_id)
            if current.status == "ready":
                if (
                    rebind
                    and expected_server_instance_id
                    != current.server_instance_id
                ):
                    raise LauncherError(
                        "rebind_identity_mismatch",
                        "Connected Server does not match the confirmed instance ID",
                        retryable=False,
                        status_code=409,
                        details={
                            "expected_server_instance_id": (
                                expected_server_instance_id
                            ),
                            "observed_server_instance_id": (
                                current.server_instance_id
                            ),
                        },
                    )
                return replace(current)
            task = self._connect_tasks.get(profile_id)
            if task is not None and task.done():
                self._connect_tasks.pop(profile_id, None)
                self._connect_intents.pop(profile_id, None)
                task = None
            if task is None:
                generation = self._attempt_generation.get(profile_id, 0) + 1
                self._attempt_generation[profile_id] = generation
                current.status = "connecting"
                current.phase = "health"
                current.error = None
                self._endpoints.pop(profile_id, None)
                task = asyncio.create_task(
                    self._run_connect(
                        profile_id,
                        generation,
                        rebind=rebind,
                        expected_server_instance_id=expected_server_instance_id,
                        request_id=request_id,
                    )
                )
                self._connect_tasks[profile_id] = task
                self._connect_intents[profile_id] = intent
            elif self._connect_intents.get(profile_id) != intent:
                raise LauncherError(
                    "connection_intent_conflict",
                    f"Profile {profile_id} already has a connection attempt "
                    "with a different binding intent",
                    retryable=True,
                    status_code=409,
                )
            return task

    async def disconnect(self, profile_id: str) -> ServerSession:
        task: asyncio.Task[ServerSession] | None
        async with self._guard:
            self.profiles.get(profile_id)
            snapshot, task = self._disconnect_locked(profile_id)

        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return snapshot

    async def update_profile(
        self,
        profile_id: str,
        *,
        label: str | None = None,
        auto_connect: bool | None = None,
        local: LocalTarget | None = None,
    ) -> ServerProfile:
        task: asyncio.Task[ServerSession] | None = None
        async with self._guard:
            current = self.profiles.get(profile_id)
            updated = self.profiles.update(
                profile_id,
                label=label,
                auto_connect=auto_connect,
                local=local,
            )
            if updated.local != current.local:
                _, task = self._disconnect_locked(profile_id)

        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return updated

    async def delete_profile(self, profile_id: str) -> ServerProfile:
        task: asyncio.Task[ServerSession] | None
        async with self._guard:
            deleted = self.profiles.delete(profile_id)
            _, task = self._disconnect_locked(profile_id)
            self._sessions.pop(profile_id, None)

        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return deleted

    def _disconnect_locked(
        self,
        profile_id: str,
    ) -> tuple[ServerSession, asyncio.Task[ServerSession] | None]:
        session = self._session(profile_id)
        self._attempt_generation[profile_id] = (
            self._attempt_generation.get(profile_id, 0) + 1
        )
        task = self._connect_tasks.pop(profile_id, None)
        self._connect_intents.pop(profile_id, None)
        if task is not None and not task.done():
            task.cancel()
        session.status = "disconnected"
        session.phase = None
        session.server_instance_id = None
        session.error = None
        self._invalidate_lease(profile_id)
        self._endpoints.pop(profile_id, None)
        return replace(session), task

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        profile_ids = {profile.id for profile in self.profiles.list()}
        profile_ids.update(self._connect_tasks)
        for profile_id in profile_ids:
            await self.disconnect(profile_id)
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await self.connector.close()

    async def _run_connect(
        self,
        profile_id: str,
        generation: int,
        *,
        rebind: bool,
        expected_server_instance_id: str | None,
        request_id: str,
    ) -> ServerSession:
        profile = self.profiles.get(profile_id)

        def set_phase(phase: str) -> None:
            if self._attempt_generation.get(profile_id) != generation:
                return
            session = self._sessions.get(profile_id)
            if session is not None and session.status == "connecting":
                session.phase = phase

        try:
            connected = await self.connector.connect(
                profile,
                set_phase,
                request_id=request_id,
            )
            async with self._guard:
                if self._attempt_generation.get(profile_id) != generation:
                    raise LauncherError(
                        "connection_cancelled",
                        f"Connection for Profile {profile_id} was cancelled",
                        retryable=True,
                        status_code=409,
                    )

                if rebind:
                    if expected_server_instance_id != connected.server_instance_id:
                        raise LauncherError(
                            "rebind_identity_mismatch",
                            "Connected Server does not match the confirmed instance ID",
                            retryable=False,
                            status_code=409,
                            details={
                                "expected_server_instance_id": (
                                    expected_server_instance_id
                                ),
                                "observed_server_instance_id": (
                                    connected.server_instance_id
                                ),
                            },
                        )
                    bound_profile = self.profiles.rebind(
                        profile_id,
                        connected.server_instance_id,
                    )
                else:
                    try:
                        bound_profile = self.profiles.bind(
                            profile_id,
                            connected.server_instance_id,
                        )
                    except LauncherError as exc:
                        if exc.code != "server_instance_already_bound":
                            raise
                        current_profile = self.profiles.get(profile_id)
                        if (
                            current_profile.id == DEFAULT_LOCAL_PROFILE_ID
                            or current_profile.bound_server_instance_id is not None
                        ):
                            raise
                        self.profiles.delete(profile_id)
                        details = dict(exc.details)
                        details["unbound_profile_removed"] = True
                        raise LauncherError(
                            exc.code,
                            exc.message,
                            exc.retryable,
                            exc.status_code,
                            details=details,
                        ) from exc

                session = self._session(profile_id)
                session.status = "ready"
                session.phase = None
                session.connection_epoch += 1
                session.server_instance_id = bound_profile.bound_server_instance_id
                session.error = None
                self._invalidate_lease(profile_id)
                self._lease_invalidations[profile_id] = asyncio.Event()
                self._endpoints[profile_id] = connected.endpoint
                return replace(session)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error = self._launcher_error(exc)
            async with self._guard:
                if self._attempt_generation.get(profile_id) == generation:
                    try:
                        self.profiles.get(profile_id)
                    except LauncherError as profile_error:
                        if profile_error.code != "profile_not_found":
                            raise
                        self._sessions.pop(profile_id, None)
                    else:
                        session = self._session(profile_id)
                        session.status = "error"
                        session.error = ConnectionErrorInfo(
                            code=error.code,
                            message=error.message,
                            retryable=error.retryable,
                            details=error.details or None,
                        )
                        session.server_instance_id = None
                    self._invalidate_lease(profile_id)
                    self._endpoints.pop(profile_id, None)
            raise error from exc
        finally:
            async with self._guard:
                current = asyncio.current_task()
                if self._connect_tasks.get(profile_id) is current:
                    self._connect_tasks.pop(profile_id, None)
                    self._connect_intents.pop(profile_id, None)

    @staticmethod
    def _launcher_error(exc: BaseException) -> LauncherError:
        if isinstance(exc, LauncherError):
            return exc
        raw_details = getattr(exc, "details", None)
        details = raw_details if isinstance(raw_details, Mapping) else None
        return LauncherError(
            str(getattr(exc, "code", "server_connection_failed")),
            str(getattr(exc, "message", str(exc) or "Server connection failed")),
            retryable=bool(getattr(exc, "retryable", True)),
            status_code=int(getattr(exc, "status_code", 502)),
            details=details,
        )

    def _invalidate_lease(self, profile_id: str) -> None:
        invalidated = self._lease_invalidations.pop(profile_id, None)
        if invalidated is not None:
            invalidated.set()

    def _background_done(self, task: asyncio.Task[object]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        task.exception()
