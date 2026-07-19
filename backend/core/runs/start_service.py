from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from .idempotency import RunIdempotency, RunIdempotencyConflictError, RunStartResult
from .producer_registry import ProducerRegistry
from .run_manager import RunManager
from .types import FINISHED_RUN_STATUSES, RunKind, RunRecord, RunStatus


logger = logging.getLogger(__name__)

RunBootstrap = Callable[[RunRecord], Awaitable[asyncio.Task[Any]]]


@dataclass(frozen=True)
class RunStartSpec:
    conversation_id: str
    kind: RunKind
    anchor_node_id: Optional[str]
    summary: str
    idempotency: RunIdempotency
    request_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    target_node_id: Optional[str] = None
    created_by_run_id: Optional[str] = None
    cancellation_parent_run_id: Optional[str] = None
    task_binding: Optional[Dict[str, Any]] = None


class RunStartSchedulingError(RuntimeError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"producer scheduling failed for run {run_id}")
        self.run_id = run_id


class RunStartReservationError(RuntimeError):
    """Reservation failed before any canonical run ID was committed."""


class RunStartValidationError(ValueError):
    pass


@dataclass
class _BootstrapBarrier:
    request_fingerprint: str
    future: asyncio.Future[RunRecord]
    run_id: str | None = None


class RunStartCoordinator:
    """Owns only the reserve-to-producer-registration window."""

    def __init__(
        self,
        run_manager: RunManager,
        producer_registry: ProducerRegistry | None = None,
    ) -> None:
        self.run_manager = run_manager
        self.producer_registry = producer_registry or ProducerRegistry.for_run_manager(
            run_manager
        )
        self._barriers: dict[str, _BootstrapBarrier] = {}
        self._request_tasks: dict[asyncio.Task[RunStartResult], str] = {}
        self._pending_terminalizations: dict[
            str,
            tuple[RunStatus, str],
        ] = {}
        self._state_lock = asyncio.Lock()
        self._closing = False
        self._close_task: asyncio.Task[tuple[str, ...]] | None = None

    async def replay_existing(
        self,
        idempotency: RunIdempotency,
    ) -> RunStartResult | None:
        self._validate_idempotency(idempotency)
        while True:
            async with self._state_lock:
                self._ensure_open()
                barrier = self._barriers.get(idempotency.key)
            if barrier is None:
                record = await self.run_manager.get_idempotent_run(
                    idempotency_key=idempotency.key,
                    request_fingerprint=idempotency.request_fingerprint,
                )
                if record is not None:
                    await self._settle_pending_terminalization(record.run_id)
                    record = await self.run_manager.get_idempotent_run(
                        idempotency_key=idempotency.key,
                        request_fingerprint=idempotency.request_fingerprint,
                    )
                return RunStartResult(record, False) if record is not None else None

            if idempotency.request_fingerprint != barrier.request_fingerprint:
                try:
                    winner = await asyncio.shield(barrier.future)
                except BaseException:
                    await asyncio.sleep(0)
                    continue
                raise RunIdempotencyConflictError(winner.run_id)

            winner = await asyncio.shield(barrier.future)
            refreshed = await self.run_manager.get_idempotent_run(
                idempotency_key=idempotency.key,
                request_fingerprint=idempotency.request_fingerprint,
            )
            return RunStartResult(refreshed or winner, False)

    async def start(
        self,
        spec: RunStartSpec,
        bootstrap: RunBootstrap,
    ) -> RunStartResult:
        self._validate_idempotency(spec.idempotency)
        async with self._state_lock:
            self._ensure_open()
            task = asyncio.create_task(
                self._start_once(spec, bootstrap),
                name=f"run-start:{spec.request_id}",
            )
            self._request_tasks[task] = spec.request_id
        task.add_done_callback(self._request_done)
        return await asyncio.shield(task)

    async def _start_once(
        self,
        spec: RunStartSpec,
        bootstrap: RunBootstrap,
    ) -> RunStartResult:
        key = spec.idempotency.key
        while True:
            async with self._state_lock:
                barrier = self._barriers.get(key)
                if barrier is None:
                    future: asyncio.Future[RunRecord] = (
                        asyncio.get_running_loop().create_future()
                    )
                    future.add_done_callback(self._consume_future_error)
                    barrier = _BootstrapBarrier(
                        request_fingerprint=spec.idempotency.request_fingerprint,
                        future=future,
                    )
                    self._barriers[key] = barrier
                    owner = True
                else:
                    owner = False

            if owner:
                try:
                    return await self._bootstrap_owner(spec, bootstrap, barrier)
                finally:
                    async with self._state_lock:
                        if self._barriers.get(key) is barrier:
                            self._barriers.pop(key, None)

            if spec.idempotency.request_fingerprint != barrier.request_fingerprint:
                try:
                    winner = await asyncio.shield(barrier.future)
                except BaseException:
                    await asyncio.sleep(0)
                    continue
                raise RunIdempotencyConflictError(winner.run_id)

            winner = await asyncio.shield(barrier.future)
            refreshed = await self.run_manager.get_idempotent_run(
                idempotency_key=key,
                request_fingerprint=spec.idempotency.request_fingerprint,
            )
            return RunStartResult(refreshed or winner, False)

    async def _bootstrap_owner(
        self,
        spec: RunStartSpec,
        bootstrap: RunBootstrap,
        barrier: _BootstrapBarrier,
    ) -> RunStartResult:
        record: RunRecord | None = None
        created = False
        try:
            record, created = await self.run_manager.reserve_or_get_run(
                conversation_id=spec.conversation_id,
                kind=spec.kind,
                idempotency_key=spec.idempotency.key,
                request_fingerprint=spec.idempotency.request_fingerprint,
                anchor_node_id=spec.anchor_node_id,
                target_node_id=spec.target_node_id,
                created_by_run_id=spec.created_by_run_id,
                cancellation_parent_run_id=spec.cancellation_parent_run_id,
                summary=spec.summary,
                metadata=spec.metadata,
                task_binding=spec.task_binding,
                on_reserved=lambda run_id: setattr(barrier, "run_id", run_id),
            )
            barrier.run_id = record.run_id
            if created:
                task = await bootstrap(record)
                if not isinstance(task, asyncio.Task):
                    raise TypeError("run bootstrap must return an asyncio.Task")
                self.producer_registry.register(record.run_id, task)
            else:
                await self._settle_pending_terminalization(record.run_id)
                refreshed = await self.run_manager.get_idempotent_run(
                    idempotency_key=spec.idempotency.key,
                    request_fingerprint=spec.idempotency.request_fingerprint,
                )
                if refreshed is not None:
                    record = refreshed
            if not barrier.future.done():
                barrier.future.set_result(record)
            return RunStartResult(record, created)
        except RunIdempotencyConflictError as exc:
            barrier.run_id = exc.existing_run_id
            if not barrier.future.done():
                barrier.future.set_exception(exc)
            raise
        except RunStartSchedulingError as exc:
            if not barrier.future.done():
                barrier.future.set_exception(exc)
            raise
        except BaseException as exc:
            error: BaseException = exc
            canonical_run_id = record.run_id if record is not None and created else None
            if record is None and barrier.run_id is not None:
                reserved = self.run_manager.get_run(barrier.run_id)
                if reserved is not None:
                    canonical_run_id = barrier.run_id
            if canonical_run_id is not None:
                try:
                    await asyncio.shield(
                        self.run_manager.finish_run(
                            canonical_run_id,
                            RunStatus.INTERRUPTED,
                            "producer scheduling failed",
                        )
                    )
                except BaseException as terminalization_error:
                    logger.error(
                        "failed to terminalize run after bootstrap failure: %s",
                        canonical_run_id,
                        exc_info=(
                            type(terminalization_error),
                            terminalization_error,
                            terminalization_error.__traceback__,
                        ),
                    )
                    if not self._is_durably_terminal(canonical_run_id):
                        self._pending_terminalizations[canonical_run_id] = (
                            RunStatus.INTERRUPTED,
                            "producer scheduling failed",
                        )
                if not isinstance(exc, asyncio.CancelledError):
                    error = RunStartSchedulingError(canonical_run_id)
            elif not isinstance(exc, asyncio.CancelledError):
                error = RunStartReservationError(str(exc))
            if not barrier.future.done():
                barrier.future.set_exception(error)
            raise error

    async def _settle_pending_terminalization(self, run_id: str) -> None:
        spec = self._pending_terminalizations.get(run_id)
        if spec is None:
            return
        await self._retry_terminalization(run_id, *spec)
        if run_id in self._pending_terminalizations:
            raise RunStartSchedulingError(run_id)

    async def _retry_terminalization(
        self,
        run_id: str,
        status: RunStatus,
        reason: str,
    ) -> None:
        try:
            await self.run_manager.finish_run(run_id, status, reason)
        except BaseException as exc:
            logger.error(
                "failed to retry run terminalization: %s",
                run_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        if self._is_durably_terminal(run_id):
            self._pending_terminalizations.pop(run_id, None)

    def _is_durably_terminal(self, run_id: str) -> bool:
        try:
            run = self.run_manager.repository.get_run(run_id)
        except Exception:
            return False
        return bool(
            run is not None
            and RunStatus(str(run.get("status"))) in FINISHED_RUN_STATUSES
        )

    def _request_done(self, task: asyncio.Task[RunStartResult]) -> None:
        self._request_tasks.pop(task, None)
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _consume_future_error(future: asyncio.Future[RunRecord]) -> None:
        if future.cancelled():
            return
        try:
            future.exception()
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _validate_idempotency(idempotency: RunIdempotency) -> None:
        if not isinstance(idempotency, RunIdempotency):
            raise RunStartValidationError("validated run idempotency is required")

    def _ensure_open(self) -> None:
        if self._closing:
            raise RunStartReservationError("run start coordinator is closing")

    async def close(self, timeout: float = 5.0) -> tuple[str, ...]:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(
                self._close_once(timeout),
                name="run-start-coordinator-close",
            )
        return await asyncio.shield(self._close_task)

    async def _close_once(self, timeout: float) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        tasks = list(self._request_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=max(0.0, deadline - loop.time()))
        await asyncio.sleep(0)
        retry_tasks = {
            run_id: asyncio.create_task(
                self._retry_terminalization(run_id, status, reason),
                name=f"run-start-terminalize:{run_id}",
            )
            for run_id, (status, reason) in dict(
                self._pending_terminalizations
            ).items()
        }
        if retry_tasks:
            await asyncio.wait(
                retry_tasks.values(),
                timeout=max(0.0, deadline - loop.time()),
            )
        unresolved = {
            self._request_tasks[task]
            for task in tasks
            if not task.done() and task in self._request_tasks
        }
        unresolved.update(
            run_id
            for run_id, task in retry_tasks.items()
            if not task.done()
        )
        for run_id in list(self._pending_terminalizations):
            if self._is_durably_terminal(run_id):
                self._pending_terminalizations.pop(run_id, None)
            else:
                unresolved.add(run_id)
        return tuple(sorted(unresolved))


__all__ = [
    "RunBootstrap",
    "RunStartCoordinator",
    "RunStartReservationError",
    "RunStartSchedulingError",
    "RunStartSpec",
    "RunStartValidationError",
]
