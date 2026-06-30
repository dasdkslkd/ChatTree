import sys

sys.path.insert(0, ".")

from backend.core.slash.dispatcher import SlashCommandDispatcher
from backend.core.slash.parser import parse_slash_command
from backend.core.slash.registry import SlashCommandRegistry
from backend.core.slash.types import (
    SlashDispatchKind,
    SlashPersistencePolicy,
    SlashToolPolicy,
)


def test_parse_slash_command_supports_inline_args():
    parsed = parse_slash_command("/review check auth flow")

    assert parsed is not None
    assert parsed.name == "review"
    assert parsed.args == "check auth flow"


def test_parse_slash_command_requires_leading_command_boundary():
    assert parse_slash_command("please /review this") is None
    assert parse_slash_command("/review/thing") is None
    assert parse_slash_command("/") is None


def test_dispatch_init_converts_to_main_prompt():
    result = SlashCommandDispatcher().dispatch("/init")

    assert result.kind == SlashDispatchKind.MAIN_PROMPT
    assert result.canonical_name == "init"
    assert result.model_input
    assert "AGENTS.md" in result.model_input
    assert result.tool_policy == SlashToolPolicy.INHERIT
    assert result.persistence_policy == SlashPersistencePolicy.MAIN_THREAD
    assert result.run_kind == "chat"


def test_dispatch_review_keeps_custom_instructions():
    result = SlashCommandDispatcher().dispatch("/review focus on regressions")

    assert result.kind == SlashDispatchKind.MAIN_PROMPT
    assert result.canonical_name == "review"
    assert "focus on regressions" in result.model_input


def test_dispatch_unsupported_inline_args_falls_back_to_plain_message():
    result = SlashCommandDispatcher().dispatch("/init with extra words")

    assert result.kind == SlashDispatchKind.PASSTHROUGH
    assert result.model_input == "/init with extra words"


def test_dispatch_btw_fork_workflow_are_structured_results():
    dispatcher = SlashCommandDispatcher()

    btw = dispatcher.dispatch("/btw summarize context")
    fork = dispatcher.dispatch("/fork inspect backend")
    workflow = dispatcher.dispatch("/workflow run deep review")

    assert btw.kind == SlashDispatchKind.SIDE_QUESTION
    assert btw.canonical_name == "btw"
    assert btw.args == "summarize context"
    assert btw.model_input is not None
    assert "summarize context" in btw.model_input
    assert btw.tool_policy == SlashToolPolicy.DISABLED
    assert btw.persistence_policy == SlashPersistencePolicy.SIDE_RUN
    assert btw.run_kind == "side_question"
    assert fork.kind == SlashDispatchKind.SUBAGENT
    assert fork.canonical_name == "fork"
    assert fork.args == "inspect backend"
    assert workflow.kind == SlashDispatchKind.WORKFLOW
    assert workflow.canonical_name == "workflow"
    assert workflow.args == "run deep review"


def test_dispatch_empty_btw_returns_usage_error():
    result = SlashCommandDispatcher().dispatch("/btw")

    assert result.kind == SlashDispatchKind.ERROR
    assert result.canonical_name == "btw"
    assert result.error == "用法: /btw <旁路问题>"


def test_side_command_is_not_registered():
    result = SlashCommandDispatcher().dispatch("/side summarize context")

    assert result.kind == SlashDispatchKind.PASSTHROUGH
    assert result.canonical_name is None
    assert result.model_input == "/side summarize context"


def test_builtin_registry_lists_five_commands_without_side():
    registry = SlashCommandRegistry.builtins()
    names = [definition.name for definition in registry.list()]

    assert names == ["init", "review", "btw", "fork", "workflow"]
    assert registry.get("side") is None
    assert registry.get("btw").stream_target_policy == "anchor_only"
    assert registry.get("review").stream_target_policy == "target_node"


def test_dispatch_unknown_command_is_plain_message():
    result = SlashCommandDispatcher().dispatch("/does-not-exist keep literal")

    assert result.kind == SlashDispatchKind.PASSTHROUGH
    assert result.command is None
    assert result.model_input == "/does-not-exist keep literal"
