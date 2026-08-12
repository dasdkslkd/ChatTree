from __future__ import annotations

from dataclasses import dataclass, field, replace
import fnmatch
import re
from typing import Any, Iterable, Optional

from .base import BaseTool


CANONICAL_CODING_TOOLS = {
    "glob",
    "grep",
    "read",
    "edit",
    "shell",
    "agent",
    "web",
    "memory",
    "enter_plan_mode",
    "ask_user_question",
    "exit_plan_mode",
}

DISCOVERY_TOOLS = {"tools"}

PLAN_MUTATING_TOOLS = {"agent", "edit", "memory", "shell"}

INTERNAL_MODEL_TOOL_NAMES = {
    "create_task",
    "set_task_step",
    "cancel_task",
}


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    kind: str = "builtin"
    category: str = "utility"
    capabilities: frozenset[str] = field(default_factory=frozenset)
    exposure_tags: frozenset[str] = field(default_factory=frozenset)
    model_visible: bool = True
    internal: bool = False
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolExposureContext:
    profile: Optional[str] = None
    run_kind: str = "chat"
    permission_mode: str = "default"
    allowed_tools: Optional[tuple[str, ...]] = None
    disallowed_tools: tuple[str, ...] = ()
    include_mcp: bool = False


@dataclass(frozen=True)
class ToolSpec:
    raw: str
    tool_pattern: str
    argument_pattern: Optional[str] = None

    @property
    def has_argument_pattern(self) -> bool:
        return self.argument_pattern is not None


class ToolRegistry:
    """Local tool registry with one descriptor per executable tool."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._descriptors: dict[str, ToolDescriptor] = {}

    def register(self, tool: BaseTool, descriptor: Optional[ToolDescriptor] = None) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' is already registered")
        schema = tool.parameters_schema()
        properties = schema.get("properties")
        if (
            schema.get("type") != "object"
            or schema.get("additionalProperties") is not False
            or not isinstance(properties, dict)
        ):
            raise ValueError(
                f"tool '{tool.name}' parameters must be an object schema "
                "with properties and additionalProperties=false"
            )
        pending: list[tuple[str, Any]] = [("parameters", schema)]
        while pending:
            path, value = pending.pop()
            if isinstance(value, list):
                pending.extend((path, item) for item in value)
                continue
            if not isinstance(value, dict):
                continue
            if "default" in value:
                raise ValueError(f"tool '{tool.name}' schema '{path}' must not declare default")
            nested_properties = value.get("properties")
            if nested_properties is not None:
                if (
                    value.get("type") != "object"
                    or value.get("additionalProperties") is not False
                    or not isinstance(nested_properties, dict)
                ):
                    raise ValueError(f"tool '{tool.name}' schema '{path}' must be a closed object")
                for property_name, property_schema in nested_properties.items():
                    if not isinstance(property_schema, dict) or not any(
                        key in property_schema
                        for key in ("type", "oneOf", "anyOf", "allOf", "$ref")
                    ):
                        raise ValueError(
                            f"tool '{tool.name}' property '{path}.{property_name}' has no schema type"
                        )
                    pending.append((f"{path}.{property_name}", property_schema))
            if value.get("type") == "array" and "items" not in value:
                raise ValueError(f"tool '{tool.name}' array '{path}' has no items schema")
            pending.extend(
                (path, child)
                for key, child in value.items()
                if key != "properties"
            )
        self._tools[tool.name] = tool
        self._descriptors[tool.name] = descriptor or descriptor_for_tool_name(tool.name)

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def items(self) -> Iterable[tuple[str, BaseTool]]:
        return self._tools.items()

    def descriptors(self) -> Iterable[ToolDescriptor]:
        return self._descriptors.values()

    def descriptor(self, name: str) -> Optional[ToolDescriptor]:
        return self._descriptors.get(name)


class ToolExposureResolver:
    def __init__(self, tools_config: dict[str, Any]):
        builtin_config = tools_config.get("builtin", {}) if isinstance(tools_config.get("builtin"), dict) else {}
        self._default_profile = str(
            builtin_config.get("exposure")
            or tools_config.get("builtin_exposure")
            or tools_config.get("exposure")
            or "coding"
        ).lower()
        self._explicit_visible = _optional_tuple(
            builtin_config.get("model_visible_tools", tools_config.get("model_visible_tools"))
        )
        self._hidden = set(_optional_tuple(builtin_config.get("hidden_tools", tools_config.get("hidden_tools", []))) or ())

    def context(self, **overrides: Any) -> ToolExposureContext:
        return ToolExposureContext(
            profile=str(overrides.get("profile") or self._default_profile or "coding").lower(),
            run_kind=str(overrides.get("run_kind") or "chat"),
            permission_mode=str(overrides.get("permission_mode") or "default"),
            allowed_tools=_optional_tuple(overrides.get("allowed_tools")),
            disallowed_tools=tuple(str(item) for item in (overrides.get("disallowed_tools") or ())),
            include_mcp=bool(overrides.get("include_mcp", False)),
        )

    def is_local_visible(self, descriptor: ToolDescriptor, context: Optional[ToolExposureContext] = None) -> bool:
        context = self._context(context)
        return descriptor.name in self.visible_local_names([descriptor], context)

    def visible_local_names(
        self,
        descriptors: Iterable[ToolDescriptor],
        context: Optional[ToolExposureContext] = None,
    ) -> set[str]:
        context = self._context(context)
        descriptor_by_name = {descriptor.name: descriptor for descriptor in descriptors}
        if self._explicit_visible is not None:
            visible = {name for name in self._explicit_visible if name in descriptor_by_name}
        else:
            visible = {
                name
                for name, descriptor in descriptor_by_name.items()
                if self._profile_allows(descriptor, context.profile)
            }

        if context.permission_mode in {"plan", "plan_mode"}:
            visible -= PLAN_MUTATING_TOOLS

        if context.run_kind in {"agent", "workflow", "workflow_step"} and not _patterns_include(context.allowed_tools, "agent"):
            visible.discard("agent")

        if context.allowed_tools is not None:
            requested = _names_matching(descriptor_by_name.keys(), context.allowed_tools)
            if not _patterns_include(context.allowed_tools, "*"):
                requested = {
                    name
                    for name in requested
                    if self._explicit_allows(descriptor_by_name[name])
                }
                visible |= requested
            visible &= requested

        if context.disallowed_tools:
            visible -= _names_matching(visible, context.disallowed_tools)

        visible -= self._hidden
        if context.run_kind != "chat" or context.permission_mode in {"plan", "plan_mode"}:
            visible.discard("memory")
        return visible

    def mcp_visible(self, context: Optional[ToolExposureContext] = None) -> bool:
        context = self._context(context)
        if context.include_mcp or context.profile == "internal":
            return True
        if context.allowed_tools is None:
            return False
        return any(_spec_may_match_mcp(spec) for spec in _parse_tool_specs(context.allowed_tools))

    def mcp_tool_visible(
        self,
        *,
        callable_name: str,
        original_name: str = "",
        server_name: str = "",
        context: Optional[ToolExposureContext] = None,
    ) -> bool:
        context = self._context(context)
        if not self.mcp_visible(context):
            return False
        variants = [
            callable_name,
            original_name,
            f"{server_name}.{original_name}" if server_name and original_name else "",
        ]
        if context.allowed_tools is not None:
            specs = _parse_tool_specs(context.allowed_tools)
            if not any(_mcp_spec_matches_variant(spec, variants) for spec in specs):
                return False
        if context.disallowed_tools:
            specs = _parse_tool_specs(context.disallowed_tools)
            if any(_mcp_spec_matches_variant(spec, variants) for spec in specs):
                return False
        return True

    def _profile_allows(self, descriptor: ToolDescriptor, profile: str) -> bool:
        if descriptor.internal or not descriptor.model_visible:
            return profile == "internal"
        if profile == "internal":
            return True
        if profile == "full":
            return descriptor.name not in INTERNAL_MODEL_TOOL_NAMES
        if profile == "minimal":
            return descriptor.name in {"glob", "grep", "read", "web"}
        return descriptor.name in CANONICAL_CODING_TOOLS

    def _explicit_allows(self, descriptor: ToolDescriptor) -> bool:
        return descriptor.model_visible and not descriptor.internal

    def _context(self, context: Optional[ToolExposureContext]) -> ToolExposureContext:
        if context is None:
            return self.context()
        if context.profile:
            return replace(context, profile=str(context.profile).lower())
        return replace(context, profile=self._default_profile or "coding")


def descriptor_for_tool_name(name: str) -> ToolDescriptor:
    if name in {"glob", "grep", "read"}:
        return ToolDescriptor(name=name, category="code", capabilities=frozenset({"read"}), exposure_tags=frozenset({"coding", "plan_safe"}))
    if name == "edit":
        return ToolDescriptor(name=name, category="code", capabilities=frozenset({"write"}), exposure_tags=frozenset({"coding"}))
    if name == "shell":
        return ToolDescriptor(name=name, category="code", capabilities=frozenset({"command"}), exposure_tags=frozenset({"coding"}))
    if name == "agent":
        return ToolDescriptor(name=name, category="agent", capabilities=frozenset({"agent"}), exposure_tags=frozenset({"coding"}))
    if name == "web":
        return ToolDescriptor(name=name, category="web", capabilities=frozenset({"network_read"}), exposure_tags=frozenset({"coding", "plan_safe"}))
    if name == "tools":
        return ToolDescriptor(name=name, category="utility", capabilities=frozenset({"read"}), exposure_tags=frozenset({"discovery"}))
    if name == "memory":
        return ToolDescriptor(name=name, category="housekeeping", capabilities=frozenset({"write"}), exposure_tags=frozenset({"housekeeping"}))
    if name in INTERNAL_MODEL_TOOL_NAMES:
        return ToolDescriptor(name=name, internal=True, model_visible=False)
    return ToolDescriptor(name=name)


def is_housekeeping_tool(name: str) -> bool:
    return descriptor_for_tool_name(name).category == "housekeeping"


def _optional_tuple(value: Any) -> Optional[tuple[str, ...]]:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value)
    return None


def _patterns_include(patterns: Optional[Iterable[str]], name: str) -> bool:
    if patterns is None:
        return False
    return name in _names_matching({name}, patterns)


def _names_matching(names: Iterable[str], patterns: Iterable[str]) -> set[str]:
    normalized_patterns = [spec.tool_pattern for spec in _parse_tool_specs(patterns)]
    matched: set[str] = set()
    for name in names:
        if any(pattern == "*" or fnmatch.fnmatch(name, pattern) for pattern in normalized_patterns):
            matched.add(name)
    return matched


def _tool_part(pattern: str) -> str:
    return parse_tool_spec(pattern).tool_pattern


def parse_tool_spec(value: Any) -> ToolSpec:
    raw = str(value).strip()
    stripped = raw
    if " " in stripped:
        prefix, rest = stripped.split(" ", 1)
        if prefix in {"allow", "deny", "disallow"}:
            stripped = rest.strip()
    match = re.match(r"^([^()]+)\((.*)\)$", stripped)
    if match:
        return ToolSpec(raw=raw, tool_pattern=match.group(1).strip(), argument_pattern=match.group(2).strip())
    return ToolSpec(raw=raw, tool_pattern=stripped or "*")


def tool_spec_matches(tool_name: str, specs: Iterable[str]) -> bool:
    return tool_name in _names_matching({tool_name}, specs)


def command_spec_matches(tool_name: str, command: str, spec_value: str) -> bool:
    spec = parse_tool_spec(spec_value)
    if not (spec.tool_pattern == "*" or fnmatch.fnmatch(tool_name, spec.tool_pattern)):
        return False
    if spec.argument_pattern is None:
        return True
    return fnmatch.fnmatch(command, spec.argument_pattern)


def _parse_tool_specs(patterns: Iterable[str]) -> list[ToolSpec]:
    return [parse_tool_spec(pattern) for pattern in patterns]


def _spec_may_match_mcp(spec: ToolSpec) -> bool:
    pattern = spec.tool_pattern
    return pattern.startswith("mcp") or "__" in pattern or "." in pattern


def _mcp_spec_matches_variant(spec: ToolSpec, variants: Iterable[str]) -> bool:
    if spec.has_argument_pattern:
        return False
    pattern = spec.tool_pattern
    if pattern == "*":
        return True
    return any(variant and fnmatch.fnmatch(variant, pattern) for variant in variants)
