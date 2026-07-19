from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Dict, Optional

from backend.core.agents import SubagentExecutor
from backend.core.agents.types import AgentDeliveryPolicy
from backend.core.runs import (
    ProducerRegistry,
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
        producer_registry: ProducerRegistry | None = None,
    ) -> None:
        self.run_manager = run_manager
        self.subagent_executor = subagent_executor
        self.runner = runner or WorkflowJsRunner()
        self.mailbox = mailbox
        self.agent_runtime = agent_runtime
        self.run_start_coordinator = run_start_coordinator
        self.producer_registry = producer_registry or ProducerRegistry.for_run_manager(
            run_manager
        )

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
        try:
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
        except BaseException:
            try:
                await self.producer_registry.terminalize(
                    run.run_id,
                    RunStatus.INTERRUPTED,
                    "producer scheduling failed",
                )
            except BaseException:
                logger.exception(
                    "failed to terminalize unscheduled workflow run %s",
                    run.run_id,
                )
            raise
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
    ) -> asyncio.Task[Any]:
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
        )

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
        )
        task = self.producer_registry.create(
            run.run_id,
            producer_coro,
            name=f"workflow-producer:{run.run_id}",
        )

        notification_coro = self._register_task_notification(run.run_id)
        try:
            self.producer_registry.create_background(
                notification_coro,
                name=f"workflow-notification:{run.run_id}",
            )
        except Exception:
            notification_coro.close()
            logger.exception(
                "Failed to schedule workflow notification for %s",
                run.run_id,
            )
        return task

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
        failures: list[BaseException] = []
        requested = False
        try:
            requested = await self.run_manager.request_stop(run_id)
        except BaseException as exc:
            failures.append(exc)
        try:
            children = self.run_manager.list_active_cancellation_children(
                cancellation_parent_run_id=run_id
            )
        except BaseException as exc:
            failures.append(exc)
            children = self.run_manager.list_cached_active_cancellation_children(
                cancellation_parent_run_id=run_id
            )
        for child in children:
            if child.get("kind") not in {
                RunKind.SUBAGENT.value,
                RunKind.WORKFLOW_STEP.value,
            }:
                continue
            try:
                await self.subagent_executor.stop(str(child["run_id"]))
            except BaseException as exc:
                failures.append(exc)
        stopped = self.producer_registry.cancel(run_id) or requested
        if failures:
            raise failures[0]
        return stopped

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
    ) -> None:
        final_status = RunStatus.COMPLETED
        final_error = None
        notification_payload: Optional[Dict[str, Any]] = None
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
