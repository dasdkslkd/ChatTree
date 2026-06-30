from __future__ import annotations


INIT_PROMPT = """Set up an AGENTS.md file for this repository.

Analyze the codebase before writing. Include only instructions that future
coding agents would get wrong without repository-specific guidance:
- non-obvious build, test, and run commands
- architecture and module boundaries that require reading multiple files
- coding conventions that differ from language defaults
- workflow, environment, or tool quirks that affect day-to-day edits

If AGENTS.md already exists, read it first and propose focused edits instead of
silently replacing it. Avoid generic advice, long file inventories, and details
that can be discovered from manifests or README files."""


def review_prompt(args: str) -> str:
    custom = args.strip()
    target = custom or "the current workspace changes"
    return f"""Review {target}.

Use a code-review stance:
- prioritize bugs, regressions, security issues, data loss risks, and missing tests
- cite concrete files and lines when possible
- keep findings ordered by severity
- avoid broad style commentary unless it hides a real risk

If there is no diff or the requested target cannot be inspected, say that
clearly and describe what evidence is missing."""


def btw_prompt(args: str) -> str:
    question = args.strip()
    return f"""<system-reminder>This is a side question from the user. You must answer this question directly in a single response.

IMPORTANT CONTEXT:
- You are a separate, lightweight agent spawned to answer this one question.
- The main agent is not interrupted and continues independently.
- You share the conversation context but are a separate instance.
- Do not reference being interrupted or what you were previously doing.

CRITICAL CONSTRAINTS:
- You have NO tools available. You cannot read files, run commands, search, or take any actions.
- This is a one-off response. There will be no follow-up turns.
- You can only provide information based on the conversation context already provided.
- Never say "let me check", "I'll run", "I'll inspect", or promise to take an action.
- If you do not know the answer from the available context, say so directly.

Simply answer the question with the information you have.</system-reminder>

{question}"""
