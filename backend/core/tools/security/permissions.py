from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Dict, List, Literal, Optional

PermissionBehavior = Literal["allow", "ask", "deny"]
PermissionMode = Literal["default", "dont_ask", "bypass_permissions"]
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
        rules = [
            *context.turn_grants,
            *context.session_grants,
            *self._rules,
        ]
        matched = [rule for rule in rules if rule.enabled and self._matches(rule, context)]

        deny = self._first_behavior(matched, "deny")
        if deny:
            return PermissionDecision("deny", f"Denied by rule {deny.id}", [deny])

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
            if context.mode == "dont_ask":
                return PermissionDecision("deny", f"dont_ask mode converted ask rule {explicit_ask.id} to deny", [explicit_ask])
            return PermissionDecision("ask", self._ask_reason(explicit_ask), [explicit_ask])

        if context.mode == "bypass_permissions":
            return PermissionDecision("allow", "bypass_permissions mode allows non-denied tool calls", matched)

        if allow:
            return PermissionDecision("allow", self._allow_reason(allow), [allow])

        if default_ask:
            if context.mode == "dont_ask":
                return PermissionDecision("deny", f"dont_ask mode converted ask rule {default_ask.id} to deny", [default_ask])
            return PermissionDecision("ask", self._ask_reason(default_ask), [default_ask])

        fallback = PermissionDecision("ask", "No matching allow rule; approval required", matched)
        if context.mode == "dont_ask":
            return PermissionDecision("deny", "dont_ask mode denied unclassified tool call", matched)
        if context.mode == "bypass_permissions":
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
        PermissionRule("default-ask-mcp", "ask", "mcp_tool", "*", source="default"),
    ]
