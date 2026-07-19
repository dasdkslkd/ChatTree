from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Callable


class MutationAdmissionClosed(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerBusyState:
    active_run_ids: tuple[str, ...] = ()
    pending_approval_ids: tuple[str, ...] = ()

    @property
    def busy(self) -> bool:
        return bool(self.active_run_ids or self.pending_approval_ids)


class ServerBusyError(RuntimeError):
    def __init__(self, state: ServerBusyState) -> None:
        super().__init__("server has active runs or pending approvals")
        self.state = state


class MutationAdmission:
    """Serializes mutations and atomically closes admission for shutdown."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._open = True

    @property
    def open(self) -> bool:
        return self._open

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[None]:
        async with self._lock:
            if not self._open:
                raise MutationAdmissionClosed("server is shutting down")
            yield

    async def close_if_idle(
        self,
        inspect_busy: Callable[[], ServerBusyState],
    ) -> None:
        async with self._lock:
            if not self._open:
                raise MutationAdmissionClosed("server is shutting down")
            state = inspect_busy()
            if state.busy:
                raise ServerBusyError(state)
            self._open = False
