from __future__ import annotations

from typing import Any

from .types import SlashDispatchResult


def build_direct_response_text(
    slash_result: SlashDispatchResult,
    command_definitions: list[dict[str, Any]],
    runtime_context: dict[str, Any] | None = None,
) -> str:
    runtime_context = runtime_context or {}
    command_name = slash_result.canonical_name or slash_result.command_name or ""
    if command_name == "status":
        return _status_text(runtime_context)
    if command_name == "help":
        return _help_text(command_definitions)
    if command_name == "capabilities":
        return _capabilities_text(command_definitions, runtime_context)
    return f"Unknown direct-response command: /{command_name or 'unknown'}"


def _status_text(runtime_context: dict[str, Any]) -> str:
    lines = [
        "ChatTree status",
        "",
        "- Backend: running",
        "- Slash direct-response lifecycle: available",
        "- These commands do not call the model or create a chat-tree node.",
    ]
    for label, key in [
        ("Conversation", "conversation_id"),
        ("Anchor node", "anchor_node_id"),
        ("Provider/model", "provider_model"),
        ("Workspace cwd", "workspace_cwd"),
        ("Selected system prompt mode", "prompt_mode"),
        ("Tool permission mode", "tool_permission_mode"),
    ]:
        value = runtime_context.get(key)
        if value not in (None, ""):
            lines.append(f"- {label}: {value}")
    active_runs = runtime_context.get("active_runs")
    if isinstance(active_runs, list):
        if active_runs:
            lines.append("- Active runs:")
            for run in active_runs[:6]:
                lines.append(
                    "  - {kind} {run_id} status={status} target={target}".format(
                        kind=run.get("kind") or "unknown",
                        run_id=str(run.get("run_id") or "")[:12],
                        status=run.get("status") or "unknown",
                        target=run.get("target_node_id") or run.get("anchor_node_id") or "none",
                    )
                )
        else:
            lines.append("- Active runs: none")
    return "\n".join(lines)


def _help_text(command_definitions: list[dict[str, Any]]) -> str:
    lines = [
        "ChatTree slash commands",
        "",
    ]
    for command in command_definitions:
        lines.append(f"- /{command['name']}: {command['description']}")
    return "\n".join(lines)


def _capabilities_text(
    command_definitions: list[dict[str, Any]],
    runtime_context: dict[str, Any],
) -> str:
    lines = [
        "ChatTree slash capabilities",
        "",
    ]
    capability_counts = runtime_context.get("capability_counts")
    if isinstance(capability_counts, dict):
        lines.extend([
            f"- Skills: {capability_counts.get('skills', 0)}",
            f"- Agents: {capability_counts.get('agents', 0)}",
            f"- Plugins: {capability_counts.get('plugins', 0)}",
            "",
        ])
    for command in command_definitions:
        lines.append(
            "- /{name}: dispatch={dispatch_kind}, run={run_kind}, target={stream_target_policy}".format(
                name=command["name"],
                dispatch_kind=command["dispatch_kind"],
                run_kind=command["run_kind"] or "none",
                stream_target_policy=command["stream_target_policy"],
            )
        )
    return "\n".join(lines)
