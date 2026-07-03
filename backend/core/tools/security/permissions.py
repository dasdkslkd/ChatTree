from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
import re
from typing import Any, Dict, List, Literal, Optional

PermissionBehavior = Literal["allow", "ask", "deny"]
PermissionMode = Literal[
    "default",
    "dont_ask",
    "bypass_permissions",
    "auto_approve",
    "modify_only",
    "ask_always",
]
RuleTargetType = Literal["tool", "mcp_server", "mcp_tool", "filesystem", "network", "command"]
RuleSource = Literal["default", "user", "session", "project"]


@dataclass(frozen=True)
class PermissionRule:
    id: str
    behavior: PermissionBehavior
    target_type: RuleTargetType
    pattern: str
    conditions: Dict[str, Any] = field(default_factory=dict)
    source: RuleSource = "user"
    enabled: bool = True


@dataclass(frozen=True)
class PermissionContext:
    conversation_id: str
    node_id: str
    tool_call_id: str
    tool_name: str
    arguments: Dict[str, Any]
    source: Literal["model", "user", "system"] = "model"
    mode: PermissionMode = "default"
    turn_grants: List[PermissionRule] = field(default_factory=list)
    session_grants: List[PermissionRule] = field(default_factory=list)
    run_id: Optional[str] = None
    run_kind: Optional[str] = None
    parent_run_id: Optional[str] = None
    root_run_id: Optional[str] = None
    agent_name: Optional[str] = None
    task_summary: Optional[str] = None


@dataclass(frozen=True)
class PermissionDecision:
    behavior: PermissionBehavior
    reason: str
    matched_rules: List[PermissionRule] = field(default_factory=list)
    suggested_grants: List[PermissionRule] = field(default_factory=list)
    sandbox_checks: List[str] = field(default_factory=list)


class PermissionEngine:
    def __init__(self, rules: Optional[List[PermissionRule]] = None):
        self._rules = list(rules or [])

    @classmethod
    def default(cls) -> "PermissionEngine":
        return cls(default_permission_rules())

    def evaluate(self, context: PermissionContext) -> PermissionDecision:
        mode = normalize_permission_mode(context.mode)
        rules = [
            *context.turn_grants,
            *context.session_grants,
            *self._rules,
        ]
        matched = [rule for rule in rules if rule.enabled and self._matches(rule, context)]

        deny = self._first_behavior(matched, "deny")
        if deny:
            return PermissionDecision("deny", f"Denied by rule {deny.id}", [deny])

        if _is_agent_management_tool(context.tool_name):
            allow = self._first_behavior(matched, "allow")
            return PermissionDecision(
                "allow",
                self._allow_reason(allow) if allow else "Agent management tool allowed",
                [allow] if allow else matched,
            )

        if mode == "ask_always":
            return PermissionDecision("ask", "ask_always mode requires approval for every tool call", matched)

        if mode == "auto_approve":
            if _is_explicit_delete(context.tool_name, context.arguments):
                return PermissionDecision("ask", "auto_approve mode requires approval for explicit delete/remove operations", matched)
            return PermissionDecision("allow", "auto_approve mode allows non-delete tool calls", matched)

        if mode == "modify_only":
            if _is_explicit_delete(context.tool_name, context.arguments):
                return PermissionDecision("ask", "modify_only mode requires approval for explicit delete/remove operations", matched)
            if _is_mutating_tool_call(context.tool_name, context.arguments):
                return PermissionDecision("allow", "modify_only mode allows modifying tool calls", matched)
            return PermissionDecision("ask", "modify_only mode requires approval for non-modifying tool calls", matched)

        explicit_ask = self._first_behavior(
            [rule for rule in matched if not self._is_default_ask(rule)],
            "ask",
        )
        allow = self._first_behavior(matched, "allow")
        default_ask = self._first_behavior(
            [rule for rule in matched if self._is_default_ask(rule)],
            "ask",
        )

        if explicit_ask:
            if mode == "dont_ask":
                return PermissionDecision("deny", f"dont_ask mode converted ask rule {explicit_ask.id} to deny", [explicit_ask])
            return PermissionDecision("ask", self._ask_reason(explicit_ask), [explicit_ask])

        if mode == "bypass_permissions":
            return PermissionDecision("allow", "bypass_permissions mode allows non-denied tool calls", matched)

        if allow:
            return PermissionDecision("allow", self._allow_reason(allow), [allow])

        if default_ask:
            if mode == "dont_ask":
                return PermissionDecision("deny", f"dont_ask mode converted ask rule {default_ask.id} to deny", [default_ask])
            return PermissionDecision("ask", self._ask_reason(default_ask), [default_ask])

        fallback = PermissionDecision("ask", "No matching allow rule; approval required", matched)
        if mode == "dont_ask":
            return PermissionDecision("deny", "dont_ask mode denied unclassified tool call", matched)
        if mode == "bypass_permissions":
            return PermissionDecision("allow", "bypass_permissions mode allows unclassified tool call", matched)
        return fallback

    def _matches(self, rule: PermissionRule, context: PermissionContext) -> bool:
        if rule.target_type == "tool":
            return fnmatch(context.tool_name, rule.pattern)
        if rule.target_type == "mcp_tool":
            return any(fnmatch(variant, rule.pattern) for variant in self._mcp_tool_name_variants(context.tool_name))
        if rule.target_type == "mcp_server":
            parts = self._mcp_parts(context.tool_name)
            return bool(parts and fnmatch(parts[0], rule.pattern))
        if rule.target_type in ("filesystem", "network"):
            return False
        if rule.target_type == "command":
            command = str(context.arguments.get("command") or "")
            return fnmatch(command, rule.pattern)
        return fnmatch(context.tool_name, rule.pattern)

    def _first_behavior(self, rules: List[PermissionRule], behavior: PermissionBehavior) -> Optional[PermissionRule]:
        for rule in rules:
            if rule.behavior == behavior:
                return rule
        return None

    def _is_default_ask(self, rule: PermissionRule) -> bool:
        return rule.behavior == "ask" and rule.source == "default"

    def _mcp_tool_name_variants(self, tool_name: str) -> List[str]:
        parts = self._mcp_parts(tool_name)
        if not parts:
            return []

        _, _, route_name = parts
        alias_name = f"mcp__{route_name}"
        if tool_name.startswith("mcp__"):
            return [tool_name, route_name]
        return [tool_name, alias_name]

    def _mcp_parts(self, tool_name: str) -> Optional[tuple[str, str, str]]:
        route_name = tool_name[5:] if tool_name.startswith("mcp__") else tool_name
        if "__" not in route_name:
            return None

        server_name, tool_part = route_name.split("__", 1)
        if not server_name or not tool_part:
            return None
        return server_name, tool_part, route_name

    def _ask_reason(self, rule: PermissionRule) -> str:
        if rule.target_type == "mcp_tool":
            return f"MCP approval required by rule {rule.id}"
        return f"Approval required by rule {rule.id}"

    def _allow_reason(self, rule: PermissionRule) -> str:
        if rule.source == "default" and rule.target_type == "tool":
            return f"Allowed by default built-in read tool rule {rule.id}"
        return f"Allowed by rule {rule.id}"


def default_permission_rules() -> List[PermissionRule]:
    return [
        PermissionRule("default-allow-web-search", "allow", "tool", "web_search", source="default"),
        PermissionRule("default-allow-fetch-url", "allow", "tool", "fetch_url", source="default"),
        PermissionRule("default-allow-read-tool-result", "allow", "tool", "read_tool_result", source="default"),
        PermissionRule("default-allow-list-tools", "allow", "tool", "list_available_tools", source="default"),
        PermissionRule("default-allow-wait-command", "allow", "tool", "wait_command", source="default"),
        PermissionRule("default-allow-read-command", "allow", "tool", "read_command", source="default"),
        PermissionRule("default-allow-stop-command", "allow", "tool", "stop_command", source="default"),
        PermissionRule("default-allow-spawn-agent", "allow", "tool", "spawn_agent", source="default"),
        PermissionRule("default-allow-wait-agent", "allow", "tool", "wait_agent", source="default"),
        PermissionRule("default-allow-list-agents", "allow", "tool", "list_agents", source="default"),
        PermissionRule("default-allow-send-agent-message", "allow", "tool", "send_message", source="default"),
        PermissionRule("default-allow-send-agent-input", "allow", "tool", "send_input", source="default"),
        PermissionRule("default-allow-followup-task", "allow", "tool", "followup_task", source="default"),
        PermissionRule("default-allow-resume-agent", "allow", "tool", "resume_agent", source="default"),
        PermissionRule("default-allow-close-agent", "allow", "tool", "close_agent", source="default"),
        PermissionRule("default-allow-interrupt-agent", "allow", "tool", "interrupt_agent", source="default"),
        PermissionRule("default-allow-list-files", "allow", "tool", "list_files", source="default"),
        PermissionRule("default-allow-read-file", "allow", "tool", "read_file", source="default"),
        PermissionRule("default-allow-search-files", "allow", "tool", "search_files", source="default"),
        PermissionRule("default-ask-run-command", "ask", "tool", "run_command", source="default"),
        PermissionRule("default-ask-start-background-command", "ask", "tool", "start_background_command", source="default"),
        PermissionRule("default-ask-edit-file", "ask", "tool", "edit_file", source="default"),
        PermissionRule("default-ask-write-file", "ask", "tool", "write_file", source="default"),
        PermissionRule("default-ask-apply-patch", "ask", "tool", "apply_patch", source="default"),
        PermissionRule("default-ask-mcp", "ask", "mcp_tool", "*", source="default"),
    ]


def normalize_permission_mode(value: Any) -> PermissionMode:
    if value in ("auto_approve", "auto", "bypass_permissions"):
        return "auto_approve"
    if value in ("modify_only", "ask_on_modify", "ask_modify", "modify", None, ""):
        return "modify_only"
    if value == "default":
        return "default"
    if value in ("ask_always", "ask_all", "all"):
        return "ask_always"
    if value == "dont_ask":
        return "dont_ask"
    return "ask_on_modify"


_MUTATING_NAME_TOKENS = {
    "write",
    "delete",
    "remove",
    "move",
    "edit",
    "create",
    "mkdir",
    "append",
    "patch",
    "rename",
    "copy",
    "run",
    "command",
    "shell",
    "exec",
}

_DELETE_NAME_TOKENS = {"delete", "remove", "rm", "unlink", "rmdir"}
_COMMAND_KEYS = {"command", "cmd", "script"}
_AGENT_MANAGEMENT_TOOLS = {
    "spawn_agent",
    "wait_agent",
    "list_agents",
    "send_message",
    "send_input",
    "followup_task",
    "resume_agent",
    "close_agent",
    "interrupt_agent",
}


def _is_agent_management_tool(tool_name: str) -> bool:
    return tool_name in _AGENT_MANAGEMENT_TOOLS


def _is_mutating_tool_call(tool_name: str, arguments: Dict[str, Any]) -> bool:
    parts = _tool_name_parts(tool_name)
    if any(part in _MUTATING_NAME_TOKENS for part in parts):
        return True
    return _command_text(arguments) is not None


def _is_explicit_delete(tool_name: str, arguments: Dict[str, Any]) -> bool:
    parts = _tool_name_parts(tool_name)
    if any(part in _DELETE_NAME_TOKENS for part in parts):
        return True
    command = _command_text(arguments)
    if not command:
        return False
    return bool(re.search(r"(?i)(^|[;&|]\s*)(rm|del|erase|rmdir|remove-item)\b", command))


def _tool_name_parts(tool_name: str) -> List[str]:
    normalized = tool_name[5:] if tool_name.startswith("mcp__") else tool_name
    return [part for part in re.split(r"[^a-zA-Z0-9]+", normalized.lower()) if part]


def _command_text(arguments: Dict[str, Any]) -> Optional[str]:
    for key, value in arguments.items():
        if key.lower() in _COMMAND_KEYS and isinstance(value, str):
            return value
    return None
