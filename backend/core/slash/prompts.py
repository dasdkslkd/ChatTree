from __future__ import annotations

from backend.core.prompts.catalog import load_prompt_template

INIT_PROMPT = load_prompt_template("init")


def btw_prompt(args: str) -> str:
    question = args.strip()
    return (
        "Answer this as a side question for ChatTree.\n"
        "Use the current conversation context, but do not modify the main conversation branch.\n"
        "Do not call tools. Do not edit files. Keep the answer concise and directly useful.\n\n"
        f"Side question: {question}"
    )


def review_prompt(args: str) -> str:
    custom = args.strip()
    target = custom or "the current workspace changes"
    return load_prompt_template("review").replace("{{REVIEW_TARGET}}", target)
