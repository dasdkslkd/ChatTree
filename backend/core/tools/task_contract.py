from __future__ import annotations

from typing import Any


TASK_RUNTIME_RULES = (
    "Task rules:",
    (
        "- The task state above is authoritative and conversation-wide; it may "
        "include work completed on sibling branches."
    ),
    "- Never infer that a step is pending merely because this branch does not contain its execution messages.",
    "- Report each step exactly as shown; never relabel a completed step as skipped or unexecuted.",
    "- Treat the current task snapshot and this turn's outcomes as authoritative over branch history.",
    (
        "- When an entire command, agent, or workflow run owns a numbered step, "
        "pass `step`; that run updates the step automatically, so do not call "
        "`set_task_step` for the same work."
    ),
    (
        "- Use `set_task_step` only for work completed directly in this chat "
        "without a bound run, or to mark a step blocked."
    ),
    "- Omit `step` for exploration, inspection, or bridge work between steps.",
    (
        "- A background launch does not reveal completion or output. Report those only from a terminal "
        "read/wait result or a delivered task notification."
    ),
)

TASK_STEP_BINDING_DESCRIPTION = (
    "Bind this entire run to one active-task step. Completion completes the step, "
    "failure blocks it, and cancellation only releases it; do not call set_task_step "
    "for the same work."
)

SET_TASK_STEP_DESCRIPTION = (
    "Record a directly completed or blocked active-task step only when no command, "
    "agent, or workflow run owns that work."
)


def task_step_parameter_schema() -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 1,
        "description": TASK_STEP_BINDING_DESCRIPTION,
    }
