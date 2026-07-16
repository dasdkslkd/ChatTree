from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from client_launcher.models import (
    ConnectionErrorInfo,
    LauncherError,
    ServerSession,
)
from client_launcher.profiles import ProfileStore


class SessionManager:
    def __init__(self, profiles: ProfileStore, connector: Any) -> None:
        self.profiles = profiles
        self.connector = connector
        self._sessions: dict[str, ServerSession] = {}
        self._endpoints: dict[str, str] = {}
        self._attempt_generation: dict[str, int] = {}
        self._connect_tasks: dict[str, asyncio.Task[ServerSession]] = {}
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

    def resolve_endpoint(self, profile_id: str) -> str:
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
        return endpoint

    def mark_error(self, profile_id: str, error: LauncherError) -> None:
        self.profiles.get(profile_id)
        session = self._session(profile_id)
        session.status = "error"
        session.phase = None
        session.server_instance_id = None
        session.error = ConnectionErrorInfo(
            code=error.code,
            message=error.message,
            retryable=error.retryable,
        )
        self._endpoints.pop(profile_id, None)

    async def start(self) -> None:
        for profile in self.profiles.list():
            if not profile.auto_connect:
                continue
            task = asyncio.create_task(self.connect(profile.id))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_done)

    async def connect(
        self,
        profile_id: str,
        *,
        rebind: bool = False,
        expected_server_instance_id: str | None = None,
    ) -> ServerSession:
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

        async with self._guard:
            self.profiles.get(profile_id)
            current = self._session(profile_id)
            if current.status == "ready":
                return replace(current)
            task = self._connect_tasks.get(profile_id)
            if task is not None and task.done():
                self._connect_tasks.pop(profile_id, None)
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
                    )
                )
                self._connect_tasks[profile_id] = task

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

    async def disconnect(self, profile_id: str) -> ServerSession:
        task: asyncio.Task[ServerSession] | None
        async with self._guard:
            self.profiles.get(profile_id)
            session = self._session(profile_id)
            self._attempt_generation[profile_id] = (
                self._attempt_generation.get(profile_id, 0) + 1
            )
            task = self._connect_tasks.pop(profile_id, None)
            if task is not None and not task.done():
                task.cancel()
            session.status = "disconnected"
            session.phase = None
            session.server_instance_id = None
            session.error = None
            self._endpoints.pop(profile_id, None)

        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return replace(session)

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
    ) -> ServerSession:
        profile = self.profiles.get(profile_id)

        def set_phase(phase: str) -> None:
            if self._attempt_generation.get(profile_id) != generation:
                return
            session = self._sessions.get(profile_id)
            if session is not None and session.status == "connecting":
                session.phase = phase

        try:
            connected = await self.connector.connect(profile, set_phase)
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
                        )
                    bound_profile = self.profiles.rebind(
                        profile_id,
                        connected.server_instance_id,
                    )
                else:
                    bound_profile = self.profiles.bind(
                        profile_id,
                        connected.server_instance_id,
                    )

                session = self._session(profile_id)
                session.status = "ready"
                session.phase = None
                session.connection_epoch += 1
                session.server_instance_id = bound_profile.bound_server_instance_id
                session.error = None
                self._endpoints[profile_id] = connected.endpoint
                return replace(session)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error = self._launcher_error(exc)
            async with self._guard:
                if self._attempt_generation.get(profile_id) == generation:
                    session = self._session(profile_id)
                    session.status = "error"
                    session.error = ConnectionErrorInfo(
                        code=error.code,
                        message=error.message,
                        retryable=error.retryable,
                    )
                    session.server_instance_id = None
                    self._endpoints.pop(profile_id, None)
            raise error from exc
        finally:
            async with self._guard:
                current = asyncio.current_task()
                if self._connect_tasks.get(profile_id) is current:
                    self._connect_tasks.pop(profile_id, None)

    @staticmethod
    def _launcher_error(exc: BaseException) -> LauncherError:
        if isinstance(exc, LauncherError):
            return exc
        return LauncherError(
            str(getattr(exc, "code", "server_connection_failed")),
            str(getattr(exc, "message", str(exc) or "Server connection failed")),
            retryable=bool(getattr(exc, "retryable", True)),
            status_code=int(getattr(exc, "status_code", 502)),
        )

    def _background_done(self, task: asyncio.Task[object]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        task.exception()
