from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from .idempotency import (
    RunIdempotency,
    RunIdempotencyConflictError,
    RunReferenceConversationMismatchError,
    RunReferenceNotFoundError,
    RunStartResult,
)
from .run_manager import RunManager
from .types import FINISHED_RUN_STATUSES, RunKind, RunRecord, RunStatus

logger = logging.getLogger(__name__)

MAX_PROVISIONAL_RETRIES = 1
_SCHEDULING_ERROR = "producer scheduling failed"
_PRODUCER_FAILED_ERROR = "producer failed"
_PRODUCER_CANCELLED_ERROR = "producer cancelled during shutdown"

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


@dataclass(frozen=True)
class RunBootstrapOutcome:
    run: RunRecord | None = None
    error: RunStartSchedulingError | None = None
    conflict_run_id: str | None = None
    retry: bool = False

    def __post_init__(self) -> None:
        states = (
            int(self.run is not None)
            + int(self.error is not None)
            + int(self.conflict_run_id is not None)
            + int(self.retry)
        )
        if states != 1:
            raise ValueError("bootstrap outcome must contain exactly one state")


@dataclass
class RunBootstrapBarrier:
    request_fingerprint: str
    future: asyncio.Future[RunBootstrapOutcome]
    run_id: str | None = None
    owner_task: asyncio.Task[RunStartResult] | None = None


@dataclass(frozen=True)
class RunStartDrainResult:
    request_task_ids: tuple[str, ...] = ()
    producer_run_ids: tuple[str, ...] = ()
    pending_run_ids: tuple[str, ...] = ()

    @property
    def exhausted(self) -> bool:
        return bool(
            self.request_task_ids
            or self.producer_run_ids
            or self.pending_run_ids
        )


@dataclass(frozen=True)
class _ProducerTerminalizationTarget:
    status: RunStatus
    error: str


class RunStartCoordinator:
    def __init__(self, run_manager: RunManager) -> None:
        self.run_manager = run_manager
        self._bootstrap_barriers: dict[str, RunBootstrapBarrier] = {}
        self._bootstrap_failures: dict[str, RunStartSchedulingError] = {}
        self._request_tasks: set[asyncio.Task[RunStartResult]] = set()
        self._request_task_ids: dict[asyncio.Task[RunStartResult], str] = {}
        self._producer_tasks: dict[str, asyncio.Task[Any]] = {}
        self._producer_callback_claims: set[
            tuple[str, asyncio.Task[Any]]
        ] = set()
        self._detached_producer_tasks: dict[asyncio.Task[Any], str] = {}
        self._producer_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._producer_cleanup_run_ids: dict[asyncio.Task[None], str] = {}
        self._producer_cleanup_targets: dict[
            asyncio.Task[None], _ProducerTerminalizationTarget
        ] = {}
        self._producer_terminalization_failures: dict[
            str, _ProducerTerminalizationTarget
        ] = {}
        self._close_recovery_tasks: dict[str, asyncio.Task[RunRecord]] = {}
        self._state_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closing = False

    async def start(
        self,
        spec: RunStartSpec,
        bootstrap: RunBootstrap,
    ) -> RunStartResult:
        if not isinstance(spec.idempotency, RunIdempotency):
            raise RunStartValidationError("validated run idempotency is required")
        async with self._state_lock:
            if self._closing:
                logger.error(
                    "run start rejected while coordinator is closing "
                    "request_id=%s",
                    spec.request_id,
                )
                raise RunStartReservationError("run start coordinator is closing")
            request_task = asyncio.create_task(
                self._start_once(spec, bootstrap),
                name=f"run-start:{spec.request_id}",
            )
            self._request_tasks.add(request_task)
            self._request_task_ids[request_task] = spec.request_id
        request_task.add_done_callback(self._consume_request_task)
        return await asyncio.shield(request_task)

    async def _start_once(
        self,
        spec: RunStartSpec,
        bootstrap: RunBootstrap,
    ) -> RunStartResult:
        retries = 0
        key = spec.idempotency.key
        while True:
            async with self._state_lock:
                await self._prune_missing_failures_locked()
                barrier = self._bootstrap_barriers.get(key)
                if barrier is None:
                    barrier = RunBootstrapBarrier(
                        request_fingerprint=spec.idempotency.request_fingerprint,
                        future=asyncio.get_running_loop().create_future(),
                        owner_task=asyncio.current_task(),
                    )
                    self._bootstrap_barriers[key] = barrier
                    owner = True
                else:
                    owner = False

            if owner:
                return await self._run_barrier_owner(spec, bootstrap, barrier)

            outcome = await asyncio.shield(barrier.future)
            if outcome.conflict_run_id is not None:
                if (
                    spec.idempotency.request_fingerprint
                    == barrier.request_fingerprint
                ):
                    raise RunIdempotencyConflictError(outcome.conflict_run_id)
                # This caller may match the durable canonical fingerprint even
                # though the barrier owner did not, so it must resolve afresh.
                continue
            if outcome.retry:
                retries += 1
                if retries > MAX_PROVISIONAL_RETRIES:
                    logger.error(
                        "run reservation retry budget exhausted request_id=%s key=%s",
                        spec.request_id,
                        key,
                    )
                    raise RunStartReservationError(
                        "run reservation retry budget exhausted"
                    )
                continue

            run_id = (
                outcome.run.run_id
                if outcome.run is not None
                else outcome.error.run_id
                if outcome.error is not None
                else outcome.conflict_run_id
                if outcome.conflict_run_id is not None
                else None
            )
            if spec.idempotency.request_fingerprint != barrier.request_fingerprint:
                if run_id is None:
                    raise RunStartReservationError(
                        "canonical run ID is missing from settled reservation"
                    )
                raise RunIdempotencyConflictError(run_id)
            if outcome.error is not None:
                raise outcome.error
            if outcome.run is None:
                raise RunStartReservationError(
                    "settled reservation did not contain a run"
                )
            return RunStartResult(run=outcome.run, created=False)

    async def _run_barrier_owner(
        self,
        spec: RunStartSpec,
        bootstrap: RunBootstrap,
        barrier: RunBootstrapBarrier,
    ) -> RunStartResult:
        outcome = RunBootstrapOutcome(retry=True)
        try:
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
                    on_reserved=lambda run_id: self._record_reserved_id(
                        barrier, run_id
                    ),
                )
            except RunIdempotencyConflictError as exc:
                outcome = RunBootstrapOutcome(
                    conflict_run_id=exc.existing_run_id
                )
                raise
            except (Exception, asyncio.CancelledError) as exc:
                if barrier.run_id is None:
                    self._raise_pre_reservation_failure(spec, exc)
                scheduling_error = await self._terminalize_scheduling_failure(
                    spec,
                    barrier.run_id,
                    exc,
                )
                outcome = RunBootstrapOutcome(error=scheduling_error)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise scheduling_error from exc

            if barrier.run_id is None:
                barrier.run_id = record.run_id

            if not created:
                record = await self._refresh_existing_record(record)
                failure = await self._failure_for_existing(record)
                if failure is not None:
                    outcome = RunBootstrapOutcome(error=failure)
                    raise failure
                outcome = RunBootstrapOutcome(run=record)
                return RunStartResult(run=record, created=False)

            try:
                published = await self.run_manager.publish_reserved_run(record.run_id)
                producer_task = await bootstrap(published)
                if not isinstance(producer_task, asyncio.Task):
                    if inspect.iscoroutine(producer_task):
                        producer_task.close()
                    raise TypeError("run bootstrap must return an asyncio.Task")
                await self._register_producer(published.run_id, producer_task)
            except (Exception, asyncio.CancelledError) as exc:
                scheduling_error = await self._terminalize_scheduling_failure(
                    spec,
                    record.run_id,
                    exc,
                )
                outcome = RunBootstrapOutcome(error=scheduling_error)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise scheduling_error from exc

            outcome = RunBootstrapOutcome(run=published)
            return RunStartResult(run=published, created=True)
        finally:
            await self._settle_barrier(spec.idempotency.key, barrier, outcome)

    @staticmethod
    def _record_reserved_id(barrier: RunBootstrapBarrier, run_id: str) -> None:
        barrier.run_id = run_id

    def _raise_pre_reservation_failure(
        self,
        spec: RunStartSpec,
        exc: BaseException,
    ) -> None:
        if isinstance(exc, asyncio.CancelledError):
            raise exc
        if isinstance(
            exc,
            (
                RunIdempotencyConflictError,
                RunReferenceNotFoundError,
                RunReferenceConversationMismatchError,
                RunStartValidationError,
            ),
        ):
            raise exc
        logger.error(
            "run reservation failed before canonical ID request_id=%s",
            spec.request_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        raise RunStartReservationError("run reservation failed") from exc

    async def _terminalize_scheduling_failure(
        self,
        spec: RunStartSpec,
        run_id: str,
        exc: BaseException,
    ) -> RunStartSchedulingError:
        scheduling_error = RunStartSchedulingError(run_id)
        logger.error(
            "run producer scheduling failed request_id=%s run_id=%s",
            spec.request_id,
            run_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        try:
            await self.run_manager.interrupt_reserved_run(
                run_id, _SCHEDULING_ERROR
            )
        except (Exception, asyncio.CancelledError) as cleanup_exc:
            logger.error(
                "run scheduling interruption failed request_id=%s run_id=%s",
                spec.request_id,
                run_id,
                exc_info=(
                    type(cleanup_exc),
                    cleanup_exc,
                    cleanup_exc.__traceback__,
                ),
            )
            async with self._state_lock:
                self._bootstrap_failures[run_id] = scheduling_error
        else:
            async with self._state_lock:
                self._bootstrap_failures.pop(run_id, None)
        return scheduling_error

    async def _failure_for_existing(
        self,
        record: RunRecord,
    ) -> RunStartSchedulingError | None:
        async with self._state_lock:
            failure = self._bootstrap_failures.get(record.run_id)
            producer_failure = self._producer_terminalization_failures.get(
                record.run_id
            )
            if record.status in FINISHED_RUN_STATUSES:
                self._bootstrap_failures.pop(record.run_id, None)
                self._producer_terminalization_failures.pop(
                    record.run_id, None
                )
                return None
            if failure is not None:
                return failure
            if producer_failure is not None:
                return RunStartSchedulingError(record.run_id)
            return None

    async def _refresh_existing_record(self, record: RunRecord) -> RunRecord:
        producer_task = self._producer_tasks.get(record.run_id)
        if producer_task is not None and producer_task.done():
            self._claim_completed_producer(
                record.run_id,
                producer_task,
                before_done_callback=True,
            )
        try:
            refreshed = await self.run_manager.get_run_for_recovery(record.run_id)
        except Exception as exc:
            logger.error(
                "completed producer canonical refresh failed run_id=%s",
                record.run_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            async with self._state_lock:
                self._bootstrap_failures.setdefault(
                    record.run_id, RunStartSchedulingError(record.run_id)
                )
            return record
        if refreshed is not None:
            return refreshed
        async with self._state_lock:
            self._bootstrap_failures.setdefault(
                record.run_id, RunStartSchedulingError(record.run_id)
            )
        return record

    async def _prune_missing_failures_locked(self) -> None:
        run_ids = set(self._bootstrap_failures)
        run_ids.update(self._producer_terminalization_failures)
        for run_id in run_ids:
            bootstrap_failure = self._bootstrap_failures.get(run_id)
            producer_failure = self._producer_terminalization_failures.get(run_id)
            try:
                record = await self.run_manager.get_run_for_recovery(run_id)
            except Exception:
                logger.exception(
                    "run scheduling tombstone lookup failed run_id=%s", run_id
                )
                continue
            if record is not None:
                continue
            if (
                bootstrap_failure is not None
                and self._bootstrap_failures.get(run_id) is bootstrap_failure
            ):
                self._bootstrap_failures.pop(run_id, None)
            if (
                producer_failure is not None
                and self._producer_terminalization_failures.get(run_id)
                is producer_failure
            ):
                self._producer_terminalization_failures.pop(run_id, None)

    async def _settle_barrier(
        self,
        key: str,
        barrier: RunBootstrapBarrier,
        outcome: RunBootstrapOutcome,
    ) -> None:
        async with self._state_lock:
            if self._bootstrap_barriers.get(key) is barrier:
                self._bootstrap_barriers.pop(key, None)
            if not barrier.future.done():
                barrier.future.set_result(outcome)

    async def _register_producer(
        self,
        run_id: str,
        producer_task: asyncio.Task[Any],
    ) -> None:
        try:
            async with self._state_lock:
                if self._closing:
                    raise RuntimeError("run start coordinator is closing")
                existing = self._producer_tasks.get(run_id)
                if existing is not None and existing is not producer_task:
                    raise RuntimeError(
                        f"producer task already registered for run {run_id}"
                    )
                self._producer_tasks[run_id] = producer_task
        except (Exception, asyncio.CancelledError):
            producer_task.cancel()
            self._detached_producer_tasks[producer_task] = run_id
            producer_task.add_done_callback(
                lambda completed, owned_run_id=run_id: (
                    self._consume_detached_producer_task(
                        owned_run_id, completed
                    )
                )
            )
            raise
        producer_task.add_done_callback(
            lambda completed, owned_run_id=run_id: self._consume_producer_task(
                owned_run_id, completed
            )
        )

    def _consume_request_task(
        self,
        task: asyncio.Task[RunStartResult],
    ) -> None:
        self._request_tasks.discard(task)
        self._request_task_ids.pop(task, None)
        if not task.cancelled():
            try:
                task.exception()
            except asyncio.CancelledError:
                pass

    def _consume_detached_producer_task(
        self,
        run_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        if not task.cancelled():
            try:
                task.exception()
            except asyncio.CancelledError:
                pass
        if self._detached_producer_tasks.get(task) == run_id:
            self._detached_producer_tasks.pop(task, None)

    def _consume_producer_task(
        self,
        run_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        claim = (run_id, task)
        if claim in self._producer_callback_claims:
            self._producer_callback_claims.discard(claim)
            return
        if self._claim_completed_producer(
            run_id,
            task,
            before_done_callback=False,
        ):
            return
        if task.done() and not task.cancelled():
            try:
                task.exception()
            except asyncio.CancelledError:
                pass

    def _claim_completed_producer(
        self,
        run_id: str,
        task: asyncio.Task[Any],
        *,
        before_done_callback: bool,
    ) -> bool:
        if self._producer_tasks.get(run_id) is not task or not task.done():
            return False
        self._producer_tasks.pop(run_id, None)
        if before_done_callback:
            self._producer_callback_claims.add((run_id, task))

        cancelled = task.cancelled()
        error: BaseException | None = None
        if not cancelled:
            try:
                error = task.exception()
            except asyncio.CancelledError:
                cancelled = True

        if not cancelled and error is None:
            return True

        shutting_down = self._closing
        if error is not None:
            logger.error(
                "run producer task failed run_id=%s",
                run_id,
                exc_info=(type(error), error, error.__traceback__),
            )
        else:
            logger.info(
                "run producer task cancelled run_id=%s shutting_down=%s",
                run_id,
                shutting_down,
            )
        interrupted = cancelled and shutting_down
        target = _ProducerTerminalizationTarget(
            status=RunStatus.INTERRUPTED if interrupted else RunStatus.FAILED,
            error=(
                _PRODUCER_CANCELLED_ERROR
                if interrupted
                else _PRODUCER_FAILED_ERROR
            ),
        )
        self._schedule_producer_terminalization(run_id, target)
        return True

    def _schedule_producer_terminalization(
        self,
        run_id: str,
        target: _ProducerTerminalizationTarget,
    ) -> asyncio.Task[None]:
        current = self._producer_terminalization_failures.get(run_id)
        if current is None or current is target:
            self._producer_terminalization_failures[run_id] = target
        cleanup_task = asyncio.create_task(
            self._terminalize_failed_producer(run_id, target=target),
            name=f"run-producer-cleanup:{run_id}",
        )
        self._producer_cleanup_tasks.add(cleanup_task)
        self._producer_cleanup_run_ids[cleanup_task] = run_id
        self._producer_cleanup_targets[cleanup_task] = target
        cleanup_task.add_done_callback(self._consume_producer_cleanup_task)
        return cleanup_task

    async def _terminalize_failed_producer(
        self,
        run_id: str,
        *,
        target: _ProducerTerminalizationTarget,
    ) -> None:
        try:
            record = await self.run_manager.get_run_for_recovery(run_id)
            if record is None or record.status in FINISHED_RUN_STATUSES:
                async with self._state_lock:
                    if (
                        self._producer_terminalization_failures.get(run_id)
                        is target
                    ):
                        self._producer_terminalization_failures.pop(run_id, None)
                return
            await self.run_manager.finish_run(
                run_id, target.status, target.error
            )
        except (Exception, asyncio.CancelledError) as exc:
            async with self._state_lock:
                current = self._producer_terminalization_failures.get(run_id)
                if current is None or current is target:
                    self._producer_terminalization_failures[run_id] = target
            logger.error(
                "run producer fail-closed terminalization failed run_id=%s",
                run_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise
        else:
            async with self._state_lock:
                if (
                    self._producer_terminalization_failures.get(run_id)
                    is target
                ):
                    self._producer_terminalization_failures.pop(run_id, None)

    def _consume_producer_cleanup_task(
        self,
        task: asyncio.Task[None],
    ) -> None:
        self._producer_cleanup_tasks.discard(task)
        self._producer_cleanup_run_ids.pop(task, None)
        self._producer_cleanup_targets.pop(task, None)
        if not task.cancelled():
            try:
                task.exception()
            except asyncio.CancelledError:
                pass

    async def close(self, timeout: float = 5.0) -> RunStartDrainResult:
        async with self._close_lock:
            return await self._close_once(max(0.0, float(timeout)))

    async def _close_once(self, timeout: float) -> RunStartDrainResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self._state_lock:
            self._closing = True
            await self._prune_missing_failures_locked()
            active_cleanup_run_ids = set(self._producer_cleanup_run_ids.values())
            for run_id, target in tuple(
                self._producer_terminalization_failures.items()
            ):
                if run_id not in active_cleanup_run_ids:
                    self._schedule_producer_terminalization(run_id, target)
            request_snapshot = set(self._request_tasks)

        grace = min(0.05, max(0.0, deadline - loop.time()) * 0.2)
        if request_snapshot and grace > 0:
            await asyncio.wait(request_snapshot, timeout=grace)

        live_requests = {task for task in request_snapshot if not task.done()}
        for task in live_requests:
            task.cancel()
        await asyncio.sleep(0)

        async with self._state_lock:
            barrier_snapshot = tuple(self._bootstrap_barriers.items())
            producer_snapshot = dict(self._producer_tasks)
            detached_producer_snapshot = dict(self._detached_producer_tasks)
        for task in producer_snapshot.values():
            if not task.done():
                task.cancel()
        for task in detached_producer_snapshot:
            if not task.done():
                task.cancel()

        async with self._state_lock:
            recovery_run_ids = {
                barrier.run_id
                for _key, barrier in barrier_snapshot
                if barrier.run_id is not None
            }
            recovery_run_ids.update(self._bootstrap_failures)
            recovery_tasks: dict[str, asyncio.Task[RunRecord]] = {}
            for run_id in recovery_run_ids:
                task = self._close_recovery_tasks.get(run_id)
                if task is None:
                    self._bootstrap_failures.setdefault(
                        run_id, RunStartSchedulingError(run_id)
                    )
                    task = asyncio.create_task(
                        self.run_manager.interrupt_reserved_run(
                            run_id, "run start coordinator is closing"
                        ),
                        name=f"run-start-close:{run_id}",
                    )
                    self._close_recovery_tasks[run_id] = task
                    task.add_done_callback(
                        lambda completed, owned_run_id=run_id: (
                            self._consume_close_recovery_task(
                                owned_run_id, completed
                            )
                        )
                    )
                recovery_tasks[run_id] = task

        remaining = max(0.0, deadline - loop.time())
        recovery_budget = remaining * 0.4
        completed_recoveries: set[asyncio.Task[RunRecord]] = set()
        if recovery_tasks and recovery_budget > 0:
            completed_recoveries, _ = await asyncio.wait(
                set(recovery_tasks.values()), timeout=recovery_budget
            )

        for run_id, task in recovery_tasks.items():
            if task in completed_recoveries:
                self._consume_close_recovery_task(run_id, task)

        async with self._state_lock:
            for key, barrier in barrier_snapshot:
                if self._bootstrap_barriers.get(key) is not barrier:
                    continue
                if barrier.owner_task is not None and not barrier.owner_task.done():
                    continue
                self._bootstrap_barriers.pop(key, None)
                if barrier.future.done():
                    continue
                if barrier.run_id is None:
                    barrier.future.set_result(RunBootstrapOutcome(retry=True))
                else:
                    barrier.future.set_result(
                        RunBootstrapOutcome(
                            error=RunStartSchedulingError(barrier.run_id)
                        )
                    )

        remaining = max(0.0, deadline - loop.time())
        if remaining > 0:
            await self._wait_for_owned_work(deadline)
        await asyncio.sleep(0)

        async with self._state_lock:
            request_ids = tuple(
                sorted(
                    {
                        self._request_task_ids.get(task, task.get_name())
                        for task in self._request_tasks
                        if not task.done()
                    }
                )
            )
            producer_ids = set(self._producer_tasks)
            producer_ids.update(
                run_id
                for task, run_id in self._detached_producer_tasks.items()
                if not task.done()
            )
            producer_ids.update(
                self._producer_cleanup_run_ids[task]
                for task in self._producer_cleanup_tasks
                if not task.done() and task in self._producer_cleanup_run_ids
            )
            producer_ids.update(self._producer_terminalization_failures)
            pending_ids = set(self._close_recovery_tasks)
            pending_ids.update(
                barrier.run_id
                for barrier in self._bootstrap_barriers.values()
                if barrier.run_id is not None
            )
            for run_id in self._bootstrap_failures:
                pending_ids.add(run_id)

        result = RunStartDrainResult(
            request_task_ids=request_ids,
            producer_run_ids=tuple(sorted(producer_ids)),
            pending_run_ids=tuple(sorted(pending_ids)),
        )
        if result.exhausted:
            logger.error(
                "run start coordinator drain incomplete request_task_ids=%s "
                "producer_run_ids=%s pending_run_ids=%s",
                result.request_task_ids,
                result.producer_run_ids,
                result.pending_run_ids,
            )
        return result

    async def _wait_for_owned_work(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        idle_passes = 0
        while True:
            await asyncio.sleep(0)
            tasks: set[asyncio.Task[Any]] = {
                task for task in self._request_tasks if not task.done()
            }
            tasks.update(
                task for task in self._producer_tasks.values() if not task.done()
            )
            tasks.update(
                task for task in self._detached_producer_tasks if not task.done()
            )
            tasks.update(
                task for task in self._producer_cleanup_tasks if not task.done()
            )
            tasks.update(
                task for task in self._close_recovery_tasks.values() if not task.done()
            )
            if not tasks:
                idle_passes += 1
                if idle_passes >= 2:
                    return
                continue
            idle_passes = 0
            remaining = max(0.0, deadline - loop.time())
            if remaining <= 0:
                return
            _done, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                return

    def _consume_close_recovery_task(
        self,
        run_id: str,
        task: asyncio.Task[RunRecord],
    ) -> None:
        error: BaseException | None = None
        if task.cancelled():
            error = asyncio.CancelledError()
        else:
            try:
                error = task.exception()
            except asyncio.CancelledError as exc:
                error = exc
        if self._close_recovery_tasks.get(run_id) is not task:
            return
        self._close_recovery_tasks.pop(run_id, None)
        if error is None:
            self._bootstrap_failures.pop(run_id, None)
            return
        self._bootstrap_failures.setdefault(
            run_id, RunStartSchedulingError(run_id)
        )
        logger.error(
            "run start close terminalization failed run_id=%s",
            run_id,
            exc_info=(type(error), error, error.__traceback__),
        )
