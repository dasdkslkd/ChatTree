from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from contextlib import suppress
from inspect import isawaitable
from typing import Any, Callable, Dict, Optional

from backend.core.config.types import Message, Role
from backend.core.tools.security.approval import ApprovalManager, ApprovalRequest
from backend.core.tools.security.command_policy import CommandPolicy
from backend.core.tools.security.logical_sandbox import LogicalSandbox, SandboxViolation
from backend.core.tools.security.permissions import PermissionContext, PermissionDecision, PermissionEngine


MUTATING_TOOL_NAME_TOKENS = (
    "write",
    "delete",
    "move",
    "remove",
    "edit",
    "create",
    "mkdir",
    "append",
    "patch",
    "rename",
    "copy",
)

PATH_ARGUMENT_KEYS = {
    "path",
    "file",
    "filepath",
    "file_path",
    "target",
    "destination",
    "dest",
    "output",
    "source",
    "paths",
    "files",
    "cwd",
}

COMMAND_TOOL_NAME_TOKENS = {
    "command",
    "shell",
    "bash",
    "powershell",
    "terminal",
    "exec",
    "run",
}

COMMAND_ARGUMENT_KEYS = {
    "command",
    "cmd",
    "script",
}


class ToolOrchestrator:
    def __init__(
        self,
        tool_manager: Any,
        permission_engine: PermissionEngine,
        approval_manager: ApprovalManager,
        logical_sandbox: LogicalSandbox,
    ):
        self.tool_manager = tool_manager
        self.permission_engine = permission_engine
        self.approval_manager = approval_manager
        self.logical_sandbox = logical_sandbox

    async def execute_tool_call(
        self,
        tool_call: Dict[str, Any],
        conversation_id: str,
        node_id: str,
        emit_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> Message:
        name = _tool_name(tool_call)
        arguments = _parse_arguments(tool_call.get("function", {}).get("arguments"))
        tool_call_id = str(tool_call.get("id") or "")

        context = PermissionContext(
            conversation_id=conversation_id,
            node_id=node_id,
            tool_call_id=tool_call_id,
            tool_name=name,
            arguments=arguments,
            source="model",
        )
        command_decision = _command_policy_decision(name, arguments)
        if command_decision and command_decision.behavior == "deny":
            return _tool_message(
                name=name,
                tool_call_id=tool_call_id,
                content=_permission_denied_content(
                    tool_name=name,
                    reason=command_decision.reason,
                    message="Tool execution requires permission.",
                ),
            )

        decision = self.permission_engine.evaluate(context)
        if decision.behavior == "deny":
            return _tool_message(
                name=name,
                tool_call_id=tool_call_id,
                content=_permission_denied_content(
                    tool_name=name,
                    reason=decision.reason,
                    message="Tool execution requires permission.",
                ),
            )

        if command_decision and command_decision.behavior == "ask":
            decision = PermissionDecision(
                "ask",
                f"Command approval required: {command_decision.reason}",
                [],
            )

        if decision.behavior == "ask" and not _session_allow_applies(
            decision,
            self.approval_manager,
            conversation_id,
            name,
        ):
            if emit_event is None:
                return _tool_message(
                    name=name,
                    tool_call_id=tool_call_id,
                    content=_permission_denied_content(
                        tool_name=name,
                        reason=f"{decision.reason}; approval UI unavailable",
                        message="Tool execution requires permission.",
                    ),
                )

            approval_request = ApprovalRequest(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                node_id=node_id,
                tool_call_id=tool_call_id,
                tool_name=name,
                arguments_preview=_arguments_preview(arguments),
                risk_level="medium",
                reason=decision.reason,
                suggested_actions=["allow_once", "allow_session", "deny"],
            )

            approval_wait_task = self.approval_manager.begin_request(approval_request)
            try:
                await _maybe_await(
                    emit_event(
                        {
                            "event_type": "tool_approval_request",
                            "approval": approval_request.to_payload(),
                        }
                    )
                )
                approval = await approval_wait_task
            except BaseException:
                if not approval_wait_task.done():
                    approval_wait_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await approval_wait_task
                raise
            await _maybe_await(
                emit_event(
                    {
                        "event_type": "tool_approval_result",
                        "approval": {
                            "id": approval_request.id,
                            "status": approval.status,
                            "grant_scope": approval.scope,
                        },
                    }
                )
            )

            if approval.status != "approved":
                return _tool_message(
                    name=name,
                    tool_call_id=tool_call_id,
                    content=_permission_denied_content(
                        tool_name=name,
                        reason=f"Tool approval status: {approval.status}",
                        message="Tool approval was not granted.",
                    ),
                )

        try:
            for target in _filesystem_write_targets(name, arguments):
                self.logical_sandbox.check_filesystem_write(target)
        except SandboxViolation as exc:
            return _tool_message(
                name=name,
                tool_call_id=tool_call_id,
                content=_permission_denied_content(
                    tool_name=name,
                    reason=str(exc),
                    message="Tool execution violates logical sandbox.",
                ),
            )

        content = await self.tool_manager.execute_tool(name, arguments)
        return _tool_message(name=name, tool_call_id=tool_call_id, content=content)


def _tool_name(tool_call: Dict[str, Any]) -> str:
    return str(tool_call.get("function", {}).get("name") or "")


def _parse_arguments(raw_arguments: Any) -> Dict[str, Any]:
    if raw_arguments is None or raw_arguments == "":
        return {}
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {"arguments": raw_arguments}
        if isinstance(parsed, dict):
            return parsed
        return {}
    return {}


def _arguments_preview(arguments: Dict[str, Any]) -> str:
    return json.dumps(arguments, ensure_ascii=False, default=str)[:1000]


async def _maybe_await(result: Any) -> None:
    if isawaitable(result):
        await result


def _is_write_like_tool(tool_name: str) -> bool:
    lowered = tool_name.lower()
    return any(token in lowered for token in MUTATING_TOOL_NAME_TOKENS)


def _filesystem_write_targets(tool_name: str, arguments: Dict[str, Any]) -> list[Any]:
    if not _is_write_like_tool(tool_name):
        return []

    targets: list[Any] = []
    for key, value in arguments.items():
        if key.lower() not in PATH_ARGUMENT_KEYS:
            continue
        targets.extend(_path_values(value))
    return targets


def _path_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item is not None]
    return [value]


def _command_policy_decision(tool_name: str, arguments: Dict[str, Any]):
    if not _is_command_like_tool(tool_name):
        return None

    command = _command_from_arguments(arguments)
    if command is None:
        return None

    return CommandPolicy.default().classify(command)


def _is_command_like_tool(tool_name: str) -> bool:
    lowered = tool_name.lower()
    parts = [part for part in re.split(r"[^a-z0-9]+", lowered) if part]
    return any(part in COMMAND_TOOL_NAME_TOKENS for part in parts)


def _command_from_arguments(arguments: Dict[str, Any]) -> Optional[str]:
    for key, value in arguments.items():
        if key.lower() in COMMAND_ARGUMENT_KEYS and isinstance(value, str):
            return value
    return None


def _session_allow_applies(
    decision: PermissionDecision,
    approval_manager: ApprovalManager,
    conversation_id: str,
    tool_name: str,
) -> bool:
    if not approval_manager.is_session_allowed(conversation_id, tool_name):
        return False
    return any(
        rule.behavior == "ask" and rule.source == "default"
        for rule in decision.matched_rules
    )


def _permission_denied_content(tool_name: str, reason: str, message: str) -> str:
    return json.dumps(
        {
            "error": {
                "type": "permission_denied",
                "message": message,
                "reason": reason,
                "tool_name": tool_name,
            }
        },
        ensure_ascii=False,
    )


def _tool_message(name: str, tool_call_id: str, content: str) -> Message:
    return {
        "id": str(uuid.uuid4()),
        "role": Role.TOOL,
        "content": content,
        "name": name,
        "tool_calls": None,
        "tool_call_id": tool_call_id,
        "timestamp": int(time.time()),
    }
