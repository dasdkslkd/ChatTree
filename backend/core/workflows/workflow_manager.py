from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Dict, Optional

from backend.core.agents import SubagentExecutor
from backend.core.agents.types import AgentDeliveryPolicy
from backend.core.runs import (
    FINISHED_RUN_STATUSES,
    RunIdempotency,
    RunKind,
    RunManager,
    RunRecord,
    RunStartCoordinator,
    RunStartResult,
    RunStartSpec,
    RunStartValidationError,
    RunStatus,
)
from .js_runner import WorkflowJsRunner, WorkflowScriptError
from .runtime_bridge import WorkflowRuntimeBridge


logger = logging.getLogger(__name__)

_DEFAULT_WORKFLOW_BUDGET = {
    "max_seconds": 600,
    "max_host_calls": 200,
    "max_parallel": 8,
}
_MAX_WORKFLOW_BUDGET_VALUE = 2_147_483_647


def normalize_workflow_budget(
    budget: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    if budget is None:
        supplied: Dict[str, Any] = {}
    elif isinstance(budget, Mapping):
        supplied = dict(budget)
    else:
        raise RunStartValidationError("budget must be an object")

    for field in _DEFAULT_WORKFLOW_BUDGET:
        if field not in supplied:
            continue
        value = supplied[field]
        if (
            type(value) is not int
            or value < 1
            or value > _MAX_WORKFLOW_BUDGET_VALUE
        ):
            raise RunStartValidationError(
                f"budget.{field} must be an integer between 1 and 2147483647"
            )

    return {**_DEFAULT_WORKFLOW_BUDGET, **supplied}


class WorkflowManager:
    def __init__(
        self,
        *,
        run_manager: RunManager,
        subagent_executor: SubagentExecutor,
        runner: Optional[WorkflowJsRunner] = None,
        mailbox: Any = None,
        agent_runtime: Any = None,
        run_start_coordinator: RunStartCoordinator | None = None,
    ) -> None:
        self.run_manager = run_manager
        self.subagent_executor = subagent_executor
        self.runner = runner or WorkflowJsRunner()
        self.mailbox = mailbox
        self.agent_runtime = agent_runtime
        self.run_start_coordinator = run_start_coordinator
        self._tasks: dict[str, asyncio.Task] = {}
        self._coordinator_owned_run_ids: set[str] = set()
        self._shutdown_cancelled_run_ids: set[str] = set()
        self._notification_tasks: set[asyncio.Task[Any]] = set()
        self._shutdown_terminalization_tasks: dict[str, asyncio.Task[Any]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._closing = False

    def validate(self, script: str) -> Dict[str, Any]:
        self.runner.validate_script(script)
        return {"valid": True}

    async def start(
        self,
        *,
        conversation_id: str,
        script: str,
        args: Optional[Dict[str, Any]] = None,
        parent_node_id: Optional[str] = None,
        created_by_run_id: Optional[str] = None,
        cancellation_parent_run_id: Optional[str] = None,
        budget: Optional[Dict[str, Any]] = None,
        permission_mode: Optional[str] = None,
        delegated_task: Any = None,
        original_slash_input: Optional[str] = None,
        delivery_policy: str = "auto",
        task_binding: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        async with self._lifecycle_lock:
            self._ensure_open_locked()
            normalized_args, normalized_budget, metadata = self._prepare_start(
                script=script,
                args=args,
                budget=budget,
                permission_mode=permission_mode,
                delegated_task=delegated_task,
                original_slash_input=original_slash_input,
                delivery_policy=delivery_policy,
            )
            run = await self.run_manager.create_run(
                conversation_id=conversation_id,
                kind=RunKind.WORKFLOW,
                anchor_node_id=parent_node_id,
                created_by_run_id=created_by_run_id,
                cancellation_parent_run_id=cancellation_parent_run_id,
                summary="Dynamic workflow",
                metadata=metadata,
                task_binding=task_binding,
            )
            self._schedule_existing_locked(
                run=run,
                conversation_id=conversation_id,
                script=script,
                args=normalized_args,
                parent_node_id=parent_node_id,
                created_by_run_id=created_by_run_id,
                cancellation_parent_run_id=cancellation_parent_run_id,
                budget=normalized_budget,
                permission_mode=permission_mode,
            )
        return run.to_dict()

    async def start_idempotent(
        self,
        *,
        conversation_id: str,
        script: str,
        args: Optional[Dict[str, Any]] = None,
        parent_node_id: Optional[str] = None,
        created_by_run_id: Optional[str] = None,
        cancellation_parent_run_id: Optional[str] = None,
        budget: Optional[Dict[str, Any]] = None,
        permission_mode: Optional[str] = None,
        delegated_task: Any = None,
        original_slash_input: Optional[str] = None,
        delivery_policy: str = "auto",
        task_binding: Optional[Dict[str, Any]] = None,
        idempotency: RunIdempotency,
        request_id: str,
        winner_anchor_factory: Callable[
            [RunRecord], Awaitable[str | None]
        ] | None = None,
    ) -> RunStartResult:
        coordinator = self.run_start_coordinator
        if coordinator is not None:
            replay = await coordinator.replay_existing(idempotency)
            if replay is not None:
                return replay
        try:
            normalized_args, normalized_budget, metadata = self._prepare_start(
                script=script,
                args=args,
                budget=budget,
                permission_mode=permission_mode,
                delegated_task=delegated_task,
                original_slash_input=original_slash_input,
                delivery_policy=delivery_policy,
            )
        except Exception:
            if coordinator is not None:
                replay = await coordinator.replay_existing(idempotency)
                if replay is not None:
                    return replay
            raise
        if coordinator is None:
            raise RuntimeError("run start coordinator is not configured")
        spec = RunStartSpec(
            conversation_id=conversation_id,
            kind=RunKind.WORKFLOW,
            anchor_node_id=parent_node_id,
            created_by_run_id=created_by_run_id,
            cancellation_parent_run_id=cancellation_parent_run_id,
            summary="Dynamic workflow",
            metadata=metadata,
            task_binding=task_binding,
            idempotency=idempotency,
            request_id=request_id,
        )

        async def bootstrap(run: RunRecord) -> asyncio.Task[Any]:
            effective_run = run
            if winner_anchor_factory is not None:
                winner_anchor = await winner_anchor_factory(run)
                if winner_anchor is not None and winner_anchor != run.anchor_node_id:
                    effective_run = await self.run_manager.bind_anchor_node(
                        run.run_id,
                        winner_anchor,
                    )
            return await self.schedule_existing(
                run=effective_run,
                conversation_id=conversation_id,
                script=script,
                args=normalized_args,
                parent_node_id=effective_run.anchor_node_id,
                created_by_run_id=created_by_run_id,
                cancellation_parent_run_id=cancellation_parent_run_id,
                budget=normalized_budget,
                permission_mode=permission_mode,
                _coordinator_owned=True,
            )

        return await coordinator.start(spec, bootstrap)

    def _prepare_start(
        self,
        *,
        script: str,
        args: Optional[Dict[str, Any]],
        budget: Optional[Dict[str, Any]],
        permission_mode: Optional[str],
        delegated_task: Any,
        original_slash_input: Optional[str],
        delivery_policy: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        try:
            self.runner.validate_script(script)
        except WorkflowScriptError as exc:
            raise RunStartValidationError(str(exc)) from exc
        normalized_args = dict(args or {})
        normalized_budget = normalize_workflow_budget(budget)
        normalized_delivery_policy = AgentDeliveryPolicy(
            str(delivery_policy or "auto")
        ).value
        metadata = {
            "args": normalized_args,
            "budget": normalized_budget,
            "permission_mode": permission_mode,
            "delegated_task": delegated_task if delegated_task is not None else script,
            "original_slash_input": original_slash_input,
            "delivery_policy": normalized_delivery_policy,
        }
        return normalized_args, normalized_budget, metadata

    async def schedule_existing(
        self,
        *,
        run: RunRecord,
        conversation_id: str,
        script: str,
        args: Dict[str, Any],
        parent_node_id: Optional[str],
        created_by_run_id: Optional[str],
        cancellation_parent_run_id: Optional[str],
        budget: Dict[str, Any],
        permission_mode: Optional[str] = None,
        _coordinator_owned: bool = False,
    ) -> asyncio.Task[Any]:
        async with self._lifecycle_lock:
            self._ensure_open_locked()
            return self._schedule_existing_locked(
                run=run,
                conversation_id=conversation_id,
                script=script,
                args=args,
                parent_node_id=parent_node_id,
                created_by_run_id=created_by_run_id,
                cancellation_parent_run_id=cancellation_parent_run_id,
                budget=budget,
                permission_mode=permission_mode,
                _coordinator_owned=_coordinator_owned,
            )

    def _ensure_open_locked(self) -> None:
        if self._closing:
            raise RuntimeError("workflow manager is closing")

    def _schedule_existing_locked(
        self,
        *,
        run: RunRecord,
        conversation_id: str,
        script: str,
        args: Dict[str, Any],
        parent_node_id: Optional[str],
        created_by_run_id: Optional[str],
        cancellation_parent_run_id: Optional[str],
        budget: Dict[str, Any],
        permission_mode: Optional[str] = None,
        _coordinator_owned: bool = False,
    ) -> asyncio.Task[Any]:
        producer_coro = self._produce(
            run_id=run.run_id,
            conversation_id=conversation_id,
            script=script,
            args=args,
            parent_node_id=parent_node_id,
            created_by_run_id=created_by_run_id,
            cancellation_parent_run_id=cancellation_parent_run_id,
            budget=budget,
            permission_mode=permission_mode,
            _coordinator_owned=_coordinator_owned,
        )
        try:
            task = asyncio.create_task(
                producer_coro,
                name=f"workflow-producer:{run.run_id}",
            )
        except BaseException:
            producer_coro.close()
            raise
        self._tasks[run.run_id] = task
        if _coordinator_owned:
            self._coordinator_owned_run_ids.add(run.run_id)
        else:
            self._coordinator_owned_run_ids.discard(run.run_id)
        task.add_done_callback(
            lambda completed, run_id=run.run_id: self._consume_producer_task(
                run_id,
                completed,
            )
        )

        notification_coro = self._register_task_notification(run.run_id)
        try:
            notification_task = asyncio.create_task(
                notification_coro,
                name=f"workflow-notification:{run.run_id}",
            )
        except Exception:
            notification_coro.close()
            logger.exception(
                "Failed to schedule workflow notification for %s",
                run.run_id,
            )
        else:
            self._notification_tasks.add(notification_task)
            notification_task.add_done_callback(self._consume_notification_task)
        return task

    def _consume_producer_task(
        self,
        run_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        if not task.cancelled():
            try:
                task.exception()
            except asyncio.CancelledError:
                pass
        if self._tasks.get(run_id) is not task:
            return
        if run_id not in self._coordinator_owned_run_ids:
            run = self.run_manager.get_run(run_id)
            if (
                run is not None
                and RunStatus(str(run["status"])) not in FINISHED_RUN_STATUSES
            ):
                self._shutdown_cancelled_run_ids.add(run_id)
                return
        self._discard_producer_task(run_id, task)

    def _discard_producer_task(
        self,
        run_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        if self._tasks.get(run_id) is not task:
            return
        self._tasks.pop(run_id, None)
        self._coordinator_owned_run_ids.discard(run_id)
        self._shutdown_cancelled_run_ids.discard(run_id)

    def _consume_notification_task(self, task: asyncio.Task[Any]) -> None:
        if task not in self._notification_tasks:
            return
        self._notification_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("Failed to register workflow notification")

    async def close(self, timeout: float = 5.0) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        self._closing = True
        if not await self._acquire_lifecycle_lock_until(deadline):
            return ("workflow-lifecycle-lock",)
        try:
            self._shutdown_cancelled_run_ids.update(
                run_id
                for run_id in self._tasks
                if run_id not in self._coordinator_owned_run_ids
            )
            owned_tasks = {
                task: (f"workflow-producer:{run_id}", run_id)
                for run_id, task in self._tasks.items()
                if not task.done()
            }
            internal_run_ids = {
                task: run_id
                for run_id, task in self._tasks.items()
                if run_id in self._shutdown_cancelled_run_ids
            }
            owned_tasks.update(
                {
                    task: (task.get_name(), None)
                    for task in self._notification_tasks
                    if not task.done()
                }
            )
        finally:
            self._lifecycle_lock.release()

        for task in owned_tasks:
            task.cancel()
        if owned_tasks:
            remaining = max(0.0, deadline - loop.time())
            if remaining > 0:
                await asyncio.wait(owned_tasks, timeout=remaining)

        for task in tuple(self._notification_tasks):
            if task.done():
                self._consume_notification_task(task)

        terminalization_tasks: dict[asyncio.Task[Any], tuple[str, str]] = {}
        for run_id, task in tuple(self._shutdown_terminalization_tasks.items()):
            if task.done():
                self._reap_shutdown_terminalization_task(run_id, task)
            else:
                terminalization_tasks[task] = (
                    f"workflow-terminalize:{run_id}",
                    run_id,
                )

        terminalization_start_failures: set[str] = set()
        for task, run_id in internal_run_ids.items():
            if not task.done() or self._tasks.get(run_id) is not task:
                continue
            if run_id in self._shutdown_terminalization_tasks:
                continue
            run = self.run_manager.get_run(run_id)
            if (
                run is None
                or RunStatus(str(run["status"])) in FINISHED_RUN_STATUSES
            ):
                self._discard_producer_task(run_id, task)
                continue
            terminalization_task = self._start_shutdown_terminalization(run_id)
            if terminalization_task is None:
                terminalization_start_failures.add(
                    f"workflow-terminalize:{run_id}"
                )
                continue
            terminalization_tasks[terminalization_task] = (
                f"workflow-terminalize:{run_id}",
                run_id,
            )

        if terminalization_tasks:
            remaining = max(0.0, deadline - loop.time())
            if remaining > 0:
                await asyncio.wait(terminalization_tasks, timeout=remaining)

        terminalization_failures = set(terminalization_start_failures)
        for task, (label, run_id) in terminalization_tasks.items():
            if not task.done():
                terminalization_failures.add(label)
                continue
            if not self._reap_shutdown_terminalization_task(run_id, task):
                terminalization_failures.add(label)

        return tuple(
            sorted(
                {
                    label
                    for task, (label, _run_id) in owned_tasks.items()
                    if not task.done()
                }
                | terminalization_failures
            )
        )

    async def _acquire_lifecycle_lock_until(self, deadline: float) -> bool:
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if remaining <= 0:
            return False
        waiter = asyncio.create_task(
            self._lifecycle_lock.acquire(),
            name="workflow-lifecycle-lock",
        )
        try:
            done, _pending = await asyncio.wait({waiter}, timeout=remaining)
        except BaseException:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            if (
                not waiter.cancelled()
                and waiter.exception() is None
                and waiter.result()
            ):
                self._lifecycle_lock.release()
            raise
        if waiter not in done:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            if (
                not waiter.cancelled()
                and waiter.exception() is None
                and waiter.result()
            ):
                self._lifecycle_lock.release()
            return False
        return bool(waiter.result())

    def _start_shutdown_terminalization(
        self,
        run_id: str,
    ) -> asyncio.Task[Any] | None:
        terminalization_coro = self.run_manager.finish_run(
            run_id,
            RunStatus.CANCELLED,
        )
        try:
            task = asyncio.create_task(
                terminalization_coro,
                name=f"workflow-terminalize:{run_id}",
            )
        except Exception:
            terminalization_coro.close()
            logger.exception(
                "Failed to schedule workflow shutdown terminalization for %s",
                run_id,
            )
            return None
        self._shutdown_terminalization_tasks[run_id] = task
        task.add_done_callback(self._consume_shutdown_terminalization_task)
        return task

    @staticmethod
    def _consume_shutdown_terminalization_task(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    def _reap_shutdown_terminalization_task(
        self,
        run_id: str,
        task: asyncio.Task[Any],
    ) -> bool:
        if self._shutdown_terminalization_tasks.get(run_id) is not task:
            return True
        if not task.done():
            return False
        self._shutdown_terminalization_tasks.pop(run_id, None)
        if task.cancelled():
            logger.warning(
                "Workflow shutdown terminalization was cancelled for %s",
                run_id,
            )
            return False
        error = task.exception()
        if error is not None:
            logger.warning(
                "Workflow shutdown terminalization failed for %s: %s",
                run_id,
                error,
            )
            return False
        producer_task = self._tasks.get(run_id)
        if producer_task is not None and producer_task.done():
            self._discard_producer_task(run_id, producer_task)
        return True

    async def _register_task_notification(self, run_id: str) -> None:
        run = self.run_manager.get_run(run_id)
        if not run:
            return
        metadata = dict(run.get("metadata") or {})
        if str(metadata.get("delivery_policy") or "auto") == "silent":
            return
        service = getattr(self.run_manager, "notification_service", None)
        if service is None:
            return
        await service.register_run_notification(
            run_id=run_id,
            summary="Workflow running",
            payload={
                "delegated_task": metadata.get("delegated_task"),
                "original_slash_input": metadata.get("original_slash_input"),
            },
        )

    async def stop(self, run_id: str) -> bool:
        requested = await self.run_manager.request_stop(run_id)
        for child in self.run_manager.list_active_cancellation_children(cancellation_parent_run_id=run_id):
            if child.get("kind") in {RunKind.SUBAGENT.value, RunKind.WORKFLOW_STEP.value}:
                await self.subagent_executor.stop(str(child["run_id"]))
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            return True
        return requested

    async def _produce(
        self,
        *,
        run_id: str,
        conversation_id: str,
        script: str,
        args: Dict[str, Any],
        parent_node_id: Optional[str],
        created_by_run_id: Optional[str],
        cancellation_parent_run_id: Optional[str],
        budget: Dict[str, Any],
        permission_mode: Optional[str] = None,
        _coordinator_owned: bool = False,
    ) -> None:
        final_status = RunStatus.COMPLETED
        final_error = None
        notification_payload: Optional[Dict[str, Any]] = None
        defer_cancel_terminalization = False
        try:
            bridge = WorkflowRuntimeBridge(
                workflow_run_id=run_id,
                conversation_id=conversation_id,
                parent_node_id=parent_node_id,
                run_manager=self.run_manager,
                subagent_executor=self.subagent_executor,
                agent_runtime=self.agent_runtime,
                max_parallel=int(budget.get("max_parallel") or 8),
                permission_mode=permission_mode,
            )
            await self.run_manager.append_event(run_id, {
                "status": "start",
                "event_type": "workflow_start",
                "content": None,
            })
            result = await asyncio.wait_for(
                self.runner.run(script=script, args=args, budget=budget, bridge=bridge),
                timeout=int(budget.get("max_seconds") or 600),
            )
            if await self.run_manager.is_stop_requested(run_id):
                final_status = RunStatus.CANCELLED
                notification_payload = {
                    "status": "stopped",
                    "event_type": "workflow_cancelled",
                    "content": "",
                }
                await self.run_manager.append_event(run_id, notification_payload)
                return
            notification_payload = {
                "status": "complete",
                "event_type": "workflow_result",
                "content": _result_preview(result),
                "result": result,
            }
            await self.run_manager.append_event(run_id, notification_payload)
        except asyncio.CancelledError:
            if _coordinator_owned:
                defer_cancel_terminalization = True
                if not await self.run_manager.is_stop_requested(run_id):
                    raise
                defer_cancel_terminalization = False
            final_status = RunStatus.CANCELLED
            notification_payload = {
                "status": "stopped",
                "event_type": "workflow_cancelled",
                "content": "",
            }
            await self.run_manager.append_event(run_id, notification_payload)
        except Exception as exc:
            final_status = RunStatus.FAILED
            final_error = str(exc) or exc.__class__.__name__
            notification_payload = {
                "status": "error",
                "event_type": "workflow_error",
                "content": "",
                "error": final_error,
            }
            await self.run_manager.append_event(run_id, notification_payload)
        finally:
            if not defer_cancel_terminalization:
                await self.run_manager.finish_run(run_id, final_status, final_error)
                if notification_payload and final_status in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }:
                    source_status = {
                        RunStatus.COMPLETED: "completed",
                        RunStatus.FAILED: "failed",
                        RunStatus.CANCELLED: "cancelled",
                    }[final_status]
                    try:
                        await self._publish_task_notification(
                            run_id,
                            source_status,
                            notification_payload.get("content") or notification_payload.get("error") or "",
                            event_payload=notification_payload,
                        )
                    except Exception:
                        logger.exception("Failed to publish workflow notification for %s", run_id)

    async def _publish_task_notification(
        self,
        run_id: str,
        source_status: str,
        content: str,
        *,
        event_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        run = self.run_manager.get_run(run_id)
        if not run:
            return None
        if run.get("kind") != RunKind.WORKFLOW.value:
            return None
        metadata = dict(run.get("metadata") or {})
        slash_metadata = metadata.get("slash_command") if isinstance(metadata.get("slash_command"), dict) else {}
        original_slash_input = metadata.get("original_slash_input") or slash_metadata.get("original_input")
        event_payload = dict(event_payload or {})
        delivery_policy = str(metadata.get("delivery_policy") or "auto")
        if delivery_policy == "silent":
            return None
        service = getattr(self.run_manager, "notification_service", None)
        if service is None:
            return None
        return await service.publish_run_notification(
            run_id=run_id,
            source_status=source_status,
            summary=f"Workflow {source_status}",
            content=content,
            payload={
                "event_type": event_payload.get("event_type"),
                "delegated_task": metadata.get("delegated_task"),
                "original_slash_input": original_slash_input,
            },
        )


def _result_preview(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
