from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from .run_manager import RunManager
from .types import FINISHED_RUN_STATUSES, RunStatus


logger = logging.getLogger(__name__)


class ProducerRegistryClosingError(RuntimeError):
    pass


class ProducerRegistry:
    """The single owner of run producers and related background tasks."""

    def __init__(self, run_manager: RunManager) -> None:
        self.run_manager = run_manager
        self._producers: dict[str, asyncio.Task[Any]] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._terminalizers: dict[str, asyncio.Task[Any]] = {}
        self._pending_terminalizations: dict[
            str,
            tuple[RunStatus, str],
        ] = {}
        self._closing = False
        self._close_task: asyncio.Task[tuple[str, ...]] | None = None

    @classmethod
    def for_run_manager(cls, run_manager: RunManager) -> "ProducerRegistry":
        existing = getattr(run_manager, "producer_registry", None)
        if isinstance(existing, cls):
            return existing
        registry = cls(run_manager)
        run_manager.producer_registry = registry
        return registry

    def create(
        self,
        run_id: str,
        producer: Coroutine[Any, Any, Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        if self._closing:
            producer.close()
            raise ProducerRegistryClosingError("producer registry is closing")
        if run_id in self._producers:
            producer.close()
            raise RuntimeError(f"producer already registered for run {run_id}")
        try:
            task = asyncio.create_task(producer, name=name)
        except BaseException:
            producer.close()
            raise
        return self.register(run_id, task)

    def register(
        self,
        run_id: str,
        task: asyncio.Task[Any],
    ) -> asyncio.Task[Any]:
        if self._closing:
            task.cancel()
            raise ProducerRegistryClosingError("producer registry is closing")
        existing = self._producers.get(run_id)
        if existing is not None and existing is not task:
            task.cancel()
            raise RuntimeError(f"producer already registered for run {run_id}")
        if existing is task:
            return task
        self._producers[run_id] = task
        task.add_done_callback(
            lambda completed, current_run_id=run_id: self._producer_done(
                current_run_id,
                completed,
            )
        )
        return task

    def create_background(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str,
    ) -> asyncio.Task[Any] | None:
        if self._closing:
            coroutine.close()
            return None
        try:
            task = asyncio.create_task(coroutine, name=name)
        except BaseException:
            coroutine.close()
            raise
        self._background.add(task)
        task.add_done_callback(self._background_done)
        return task

    def begin_close(self) -> None:
        """Synchronously reject new work and cancel work not yet terminalizing."""
        if self._closing:
            return
        self._closing = True
        for task in [*self._producers.values(), *self._background]:
            if not task.done():
                task.cancel()

    def task_for(self, run_id: str) -> asyncio.Task[Any] | None:
        return self._producers.get(run_id)

    def cancel(self, run_id: str) -> bool:
        task = self._producers.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def _producer_done(self, run_id: str, task: asyncio.Task[Any]) -> None:
        if self._producers.get(run_id) is not task:
            return
        self._producers.pop(run_id, None)
        error: BaseException | None = None
        if not task.cancelled():
            try:
                error = task.exception()
            except asyncio.CancelledError:
                pass
        status = RunStatus.CANCELLED if task.cancelled() else RunStatus.FAILED
        reason = (
            "producer cancelled"
            if task.cancelled()
            else str(error) if error is not None else "producer exited without terminalizing run"
        )
        run = self.run_manager.get_run(run_id)
        if run is None or RunStatus(str(run["status"])) in FINISHED_RUN_STATUSES:
            return
        self._pending_terminalizations[run_id] = (status, reason)
        self._schedule_terminalizer(run_id, status, reason)

    def _schedule_terminalizer(
        self,
        run_id: str,
        status: RunStatus,
        reason: str,
    ) -> asyncio.Task[Any]:
        existing = self._terminalizers.get(run_id)
        if existing is not None and not existing.done():
            return existing
        terminalizer = asyncio.create_task(
            self.run_manager.finish_run(run_id, status, reason),
            name=f"producer-terminalize:{run_id}",
        )
        self._terminalizers[run_id] = terminalizer
        terminalizer.add_done_callback(
            lambda completed, current_run_id=run_id: self._terminalizer_done(
                current_run_id,
                completed,
            )
        )
        return terminalizer

    async def terminalize(
        self,
        run_id: str,
        status: RunStatus,
        reason: str,
    ) -> None:
        if self._is_durably_terminal(run_id):
            return
        self._pending_terminalizations[run_id] = (status, reason)
        task = self._schedule_terminalizer(run_id, status, reason)
        try:
            await asyncio.shield(task)
        finally:
            if self._is_durably_terminal(run_id):
                self._pending_terminalizations.pop(run_id, None)

    def _terminalizer_done(self, run_id: str, task: asyncio.Task[Any]) -> None:
        if self._terminalizers.get(run_id) is task:
            self._terminalizers.pop(run_id, None)
        if self._is_durably_terminal(run_id):
            self._pending_terminalizations.pop(run_id, None)
            return
        if task.cancelled():
            logger.error("producer terminalization cancelled for run %s", run_id)
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "producer terminalization failed for run %s",
                run_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _is_durably_terminal(self, run_id: str) -> bool:
        try:
            run = self.run_manager.repository.get_run(run_id)
        except Exception:
            return False
        return bool(
            run is not None
            and RunStatus(str(run.get("status"))) in FINISHED_RUN_STATUSES
        )

    def _background_done(self, task: asyncio.Task[Any]) -> None:
        self._background.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "background producer task failed: %s",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    async def close(self, timeout: float = 5.0) -> tuple[str, ...]:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(
                self._close_once(timeout),
                name="producer-registry-close",
            )
        return await asyncio.shield(self._close_task)

    async def _close_once(self, timeout: float) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        tracked_producers = dict(self._producers)
        tracked_background = list(self._background)
        tracked = [*tracked_producers.values(), *tracked_background]
        for task in tracked:
            if not task.done():
                task.cancel()
        if tracked:
            await asyncio.wait(tracked, timeout=max(0.0, deadline - loop.time()))
        for run_id, task in tracked_producers.items():
            if task.done() and self._producers.get(run_id) is task:
                self._producer_done(run_id, task)
        terminalizers = list(self._terminalizers.values())
        if terminalizers:
            await asyncio.wait(
                terminalizers,
                timeout=max(0.0, deadline - loop.time()),
            )
        await asyncio.sleep(0)
        retry_specs = dict(self._pending_terminalizations)
        for run_id, (status, reason) in retry_specs.items():
            if self._is_durably_terminal(run_id):
                self._pending_terminalizations.pop(run_id, None)
                continue
            self._schedule_terminalizer(run_id, status, reason)
        retry_tasks = [
            self._terminalizers[run_id]
            for run_id in retry_specs
            if run_id in self._terminalizers
        ]
        if retry_tasks:
            await asyncio.wait(
                retry_tasks,
                timeout=max(0.0, deadline - loop.time()),
            )
        await asyncio.sleep(0)
        unresolved = {
            task.get_name()
            for task in [
                *self._producers.values(),
                *self._background,
                *self._terminalizers.values(),
            ]
            if not task.done()
        }
        for run_id, task in tracked_producers.items():
            if not task.done() or not self._is_durably_terminal(run_id):
                unresolved.add(run_id)
        for run_id in list(self._pending_terminalizations):
            if self._is_durably_terminal(run_id):
                self._pending_terminalizations.pop(run_id, None)
            else:
                unresolved.add(run_id)
        return tuple(sorted(unresolved))


__all__ = [
    "ProducerRegistry",
    "ProducerRegistryClosingError",
]
