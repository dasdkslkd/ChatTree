"""运行时提示上下文构建：将对话状态转换为模型可见的 prompt 段落。"""
from __future__ import annotations

from typing import Any, List, Optional

from ..config.config import cfg
from ..instructions import build_agents_instruction_section
from ..tasks import TaskContextMode, TaskOutcome, TaskTurnContext
from ..tools.task_contract import TASK_RUNTIME_RULES
from ..workspace import build_default_workspace, normalize_workspace
from .types import RuntimePromptContext


def normalize_selected_system_prompt_mode(mode: str) -> str:
    return mode if mode in {"override", "append"} else "override"


def selected_system_prompt(conversation) -> tuple[Optional[str], str]:
    prompt = conversation.metadata.get("selected_system_prompt") or {}
    if not isinstance(prompt, dict):
        return None, "override"
    content = prompt.get("content")
    if not isinstance(content, str) or not content.strip():
        return None, "override"
    return content, normalize_selected_system_prompt_mode(
        str(prompt.get("mode") or "override")
    )


def runtime_context_details(
    conversation,
    permission_mode: str,
    task_context_mode: str,
) -> list[str]:
    if conversation is None:
        return []
    metadata = conversation.metadata or {}
    prompt = metadata.get("selected_system_prompt") or {}
    workspace = normalize_workspace(
        metadata.get("workspace"),
        build_default_workspace(cfg.data if isinstance(cfg.data, dict) else None),
    )
    workspace_roots = workspace.get("workspace_roots") or []
    cwd = workspace.get("cwd") or ""
    mode = normalize_selected_system_prompt_mode(str(prompt.get("mode") or "override")) if prompt else "none"
    return [
        f"- Conversation id: {metadata.get('id') or ''}",
        f"- Current node id: {conversation.current_node_id or ''}",
        f"- Current tool permission mode: {permission_mode}",
        f"- Current task context mode: {task_context_mode}",
        f"- Provider/model: {(metadata.get('provider_id') or conversation.current_provider or '')}/{(metadata.get('model_id') or conversation.current_model or '')}",
        f"- Workspace cwd: {cwd}",
        f"- Workspace roots: {', '.join(map(str, workspace_roots[:3])) if workspace_roots else 'none'}",
        f"- Selected system prompt mode: {mode}",
    ]


def plan_mode_runtime_lines(permission_mode: str, plan_ledger) -> list[str]:
    if plan_ledger is None:
        return []
    if permission_mode == "plan":
        return [
            "",
            "Plan mode is active:",
            "- You are in a read-only planning phase. Inspect, search, compare approaches, and reason only with read-only tools.",
            "- Do not edit files, run implementation commands, start implementation work, change configuration, commit, or claim changes were made.",
            "- Use `ask_user_question` only when a genuine user decision is required to continue planning.",
            "- Use `exit_plan_mode` with a concrete plan when planning is complete and user approval is required.",
            "- If the user changes direction while you are in plan mode, stay in plan mode instead of implementing.",
        ]
    return [
        "",
        "Plan mode rules:",
        "- Use `enter_plan_mode` only when the user explicitly asks for planning/exploration before implementation, or when the implementation approach has genuine ambiguity and user sign-off would prevent significant rework.",
        "- Do not enter plan mode merely because the task is large. If the path is clear, even across multiple files, proceed with implementation using the existing codebase patterns.",
        "- When the user asks you to implement now, directly execute, or complete the change, start working instead of planning unless continuing would violate safety or permission rules.",
        "- Prefer direct implementation for small fixes, clear bug fixes after diagnosis, specific instructions, and features that follow an obvious existing pattern.",
        "- Use `ask_user_question` in plan mode only for genuine user decisions that block planning.",
        "- Use `exit_plan_mode` in plan mode only after producing a concrete plan.",
    ]


def agents_instruction_sections(conversation) -> list[Any]:
    if conversation is None:
        return []
    workspace = normalize_workspace(
        conversation.metadata.get("workspace"),
        build_default_workspace(cfg.data if isinstance(cfg.data, dict) else None),
    )
    section = build_agents_instruction_section(
        workspace,
        cfg.data if isinstance(cfg.data, dict) else None,
    )
    return [section] if section is not None else []


def compact_task_text(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def format_task_snapshot_for_prompt(
    task: Any,
    *,
    heading: str,
    leading_blank: bool = True,
) -> list[str]:
    execution = f", {task.execution_state} step {task.active_step}" if task.active_step else ""
    lines = [""] if leading_blank else []
    lines.append(
        f"{heading} [{task.status}{execution}]: {compact_task_text(task.title, 120)}"
    )
    if task.detail:
        lines.append(f"Task detail: {compact_task_text(task.detail, 180)}")
    for step in task.steps:
        evidence = (
            f" evidence: {compact_task_text(step.evidence_summary, 80)}"
            if step.evidence_summary
            else ""
        )
        step_text = compact_task_text(step.title or step.detail, 100)
        if step.detail and step.detail != step.title:
            step_text += f"; detail: {compact_task_text(step.detail, 140)}"
        lines.append(f"{step.position}. [{step.status.value}] {step_text}{evidence}")
    return lines


def append_task_outcomes_for_prompt(
    lines: list[str],
    outcomes: list[TaskOutcome],
) -> None:
    if not outcomes:
        return
    lines.append("Authoritative Task Outcomes This Turn:")
    latest_snapshot_index = next(
        (index for index in range(len(outcomes) - 1, -1, -1) if outcomes[index].task_snapshot is not None),
        None,
    )
    for index, outcome in enumerate(outcomes):
        outcome_parts = []
        if outcome.step is not None:
            outcome_parts.append(f"step {outcome.step} -> {outcome.step_status or 'updated'}")
        outcome_parts.append(f"task -> {outcome.task_status.value}")
        if outcome.run_status:
            outcome_parts.append(f"run -> {outcome.run_status}")
        lines.append("- " + "; ".join(outcome_parts) + ".")
        if outcome.task_snapshot is not None and index == latest_snapshot_index:
            snapshot = outcome.task_snapshot
            lines.append(
                "Authoritative Task State After Outcome "
                f"[{outcome.task_status.value}]: {compact_task_text(snapshot.title, 120)}"
            )
            for step in snapshot.steps:
                step_text = compact_task_text(step.title, 100)
                lines.append(f"{step.position}. [{step.status.value}] {step_text}")


def format_task_turn_context_for_prompt(
    context: Optional[TaskTurnContext],
) -> list[str]:
    if context is None or context.mode != TaskContextMode.ATTACHED:
        return []
    baseline = context.baseline_task
    current = context.current_task
    observations = context.outcomes
    outcomes = [observed.outcome for observed in observations]
    latest_snapshot_observation = next(
        (observed for observed in reversed(observations) if observed.outcome.task_snapshot is not None),
        None,
    )
    baseline_superseded = (
        baseline is not None
        and current is None
        and latest_snapshot_observation is not None
        and latest_snapshot_observation.generation_id == baseline.generation_id
    )
    task_replaced = (
        baseline is not None
        and current is not None
        and baseline.generation_id != current.generation_id
    )
    if current is not None and not task_replaced:
        lines = format_task_snapshot_for_prompt(
            current,
            heading="Authoritative Active Conversation Task State",
        )
        append_task_outcomes_for_prompt(lines, outcomes)
        lines.extend(["", *TASK_RUNTIME_RULES])
        return lines
    if baseline is None and current is None and not outcomes:
        return [
            "",
            "Task context is attached; there is no active conversation task.",
            "",
            *TASK_RUNTIME_RULES,
        ]
    lines = [""]
    if baseline is not None and not baseline_superseded:
        lines.extend(format_task_snapshot_for_prompt(
            baseline,
            heading="Authoritative Task Snapshot At Turn Start",
            leading_blank=False,
        ))
    append_task_outcomes_for_prompt(lines, outcomes)
    if current is not None:
        lines.extend(format_task_snapshot_for_prompt(
            current,
            heading="Authoritative Active Conversation Task State Now",
            leading_blank=False,
        ))
    else:
        lines.append("There is no active conversation task now.")
    lines.extend(["", *TASK_RUNTIME_RULES])
    return lines


def runtime_prompt_context(
    runtime: str,
    conversation=None,
    *,
    latest_user_content: str = "",
    task_turn_context: Optional[TaskTurnContext] = None,
    multi_agent_mode: str = "none",
    permission_mode: str = "default",
    task_context_mode: str = "attached",
    plan_ledger=None,
) -> RuntimePromptContext:
    details = runtime_context_details(conversation, permission_mode, task_context_mode)
    if runtime == "side_question":
        return RuntimePromptContext(
            name="side_question",
            content="\n".join([
                "## Runtime Context",
                "",
                "Runtime mode: side question (/btw)",
                *details,
                "- Answer only the side question using the current conversation context.",
                "- Keep the run read-only: do not call tools, edit files, or create a main-branch response.",
                "- Preserve selected system prompt semantics: default core prompt, override custom prompt, or appended custom prompt.",
            ]),
            metadata={"runtime_mode": "side_question"},
        )
    multi_agent_lines: list[str] = []
    if multi_agent_mode != "none":
        multi_agent_lines = [
            "- Multi-agent tools are available in this conversation when tool schemas include `agent`.",
            "- If the user explicitly asks to use a subagent, agent, forked agent, or workflow, your first relevant action must be `agent` with the appropriate action.",
            "- Do not replace an explicit subagent request with direct shell commands, file tools, or a natural-language claim that a subagent was started.",
            "- Use `agent` with action `wait` when you need the delegated result before answering. Use notification delivery for background work.",
        ]
        if multi_agent_mode == "proactive":
            multi_agent_lines.append("- You may proactively delegate independent multi-step investigation or verification work to subagents.")
        else:
            multi_agent_lines.append("- Do not proactively spawn agents unless the user explicitly requested agent delegation.")
    task_lines = format_task_turn_context_for_prompt(task_turn_context)
    plan_lines = plan_mode_runtime_lines(permission_mode, plan_ledger)
    return RuntimePromptContext(
        name="main",
        content="\n".join([
            "## Runtime Context",
            "",
            "Runtime mode: main chat",
            *details,
            "- This is the primary persisted conversation branch.",
            "- Use tools only when they are provided for this call and follow the active permission mode.",
            *multi_agent_lines,
            *task_lines,
            *plan_lines,
            "- Preserve selected system prompt semantics: default core prompt, override custom prompt, or appended custom prompt.",
        ]),
        metadata={"runtime_mode": "main"},
    )
