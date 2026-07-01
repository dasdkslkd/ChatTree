from __future__ import annotations

import re
from pathlib import Path

TEMPLATE_ROOT = Path(__file__).resolve().parent / "templates"
_LEADING_HTML_COMMENT_RE = re.compile(r"^\ufeff?\s*<!--.*?-->\s*", re.DOTALL)

PROMPT_SOURCES: dict[str, tuple[str, ...]] = {
    "core": (
        "reference/claude-code-cli/constants/prompts.ts",
    ),
    "fork": (
        "reference/claude-code-system-prompts/system-prompts/system-prompt-forked-agent-guidance.md",
        "reference/claude-code-system-prompts/system-prompts/system-prompt-fork-usage-guidelines.md",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-worker-fork.md",
    ),
    "workflow": (
        "reference/claude-code-system-prompts/system-prompts/tool-description-workflow.md",
    ),
    "review": (
        "reference/claude-code-cli/commands/review.ts",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-code-review-part-1-base-finder-angles.md",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-code-review-part-2-low-effort-mode.md",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-code-review-part-3-extra-high-and-maximum-effort-modes.md",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-code-review-part-4-three-state-verification-phase.md",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-code-review-part-5-recall-biased-verification-phase.md",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-code-review-part-6-medium-effort-mode.md",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-code-review-part-7-high-effort-mode.md",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-code-review-part-8-github-comment-posting.md",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-code-review-part-9-fix-application.md",
    ),
    "init": (
        "reference/claude-code-cli/commands/init.ts",
        "reference/claude-code-system-prompts/system-prompts/skill-init-claudemd-and-skill-setup-new-version.md",
    ),
    "agent:explorer": (
        "reference/claude-code-cli/tools/AgentTool/prompt.ts",
        "reference/claude-code-cli/tools/AgentTool/built-in/exploreAgent.ts",
    ),
    "agent:planner": (
        "reference/claude-code-cli/tools/AgentTool/prompt.ts",
        "reference/claude-code-cli/tools/AgentTool/built-in/planAgent.ts",
    ),
    "agent:implementer": (
        "reference/claude-code-cli/tools/AgentTool/prompt.ts",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-worker-fork.md",
    ),
    "agent:reviewer": (
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-code-review-part-1-base-finder-angles.md",
    ),
    "agent:verifier": (
        "reference/claude-code-cli/tools/AgentTool/built-in/verificationAgent.ts",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-code-review-part-4-three-state-verification-phase.md",
    ),
    "agent:workflow-worker": (
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-workflow-subagent-structured-output.md",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-workflow-subagent-plain-text-output.md",
        "reference/claude-code-system-prompts/system-prompts/agent-prompt-worker-fork.md",
    ),
}


def load_prompt_template(name: str) -> str:
    path = _template_path(name)
    raw = path.read_text(encoding="utf-8")
    return _LEADING_HTML_COMMENT_RE.sub("", raw, count=1).strip()


def validate_prompt_catalog(*, require_source_files: bool = False) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    for name, sources in PROMPT_SOURCES.items():
        text = load_prompt_template(name)
        if "Claude Code" in text or "Codex" in text:
            raise ValueError(f"template {name} still contains source product names")
        if name in {"side", "btw"}:
            raise ValueError(f"side template is not allowed: {name}")
        if not require_source_files:
            continue
        for source in sources:
            if not (repo_root / source).exists():
                raise FileNotFoundError(f"prompt source for {name} is missing: {source}")


def _template_path(name: str) -> Path:
    if name.startswith("agent:"):
        return TEMPLATE_ROOT / "agents" / f"{name.split(':', 1)[1]}.md"
    return TEMPLATE_ROOT / f"{name}.md"
