import sys

sys.path.insert(0, ".")

from backend.core.chat.conversation import Conversation
from backend.core.chat.node import NodeManager
from backend.core.chat import refer_context
from backend.core.chat.refer_context import (
    ReferContextError,
    build_refer_bundle,
    format_refer_context_message,
    parse_refer_prompt_args,
)
from backend.core.config.types import Message, Role


def _message(role, content):
    return Message({
        "id": f"{role}-{content}",
        "role": role,
        "content": content,
        "timestamp": 1,
    })


def _node(content, assistant=None):
    node = NodeManager.create_node(_message(Role.USER, content), model_id="fake-model")
    if assistant is not None:
        node["assistant_message"] = _message(Role.ASSISTANT, assistant)
    return node


def test_parse_refer_args_supports_multiple_selectors_and_inline_prompt():
    parsed = parse_refer_prompt_args("node:a compact:b truncated:c compare these branches")

    assert [item["raw"] for item in parsed["selectors"]] == ["node:a", "compact:b", "truncated:c"]
    assert parsed["prompt"] == "compare these branches"


def test_parse_refer_args_preserves_prompt_spacing_and_separator():
    parsed = parse_refer_prompt_args("node:a -- node:b  should stay\n  exactly enough")

    assert [item["raw"] for item in parsed["selectors"]] == ["node:a"]
    assert parsed["prompt"] == "node:b  should stay\n  exactly enough"


def test_refer_bundle_formats_node_prune_and_truncated_sources():
    conv = Conversation(title="refer")
    conv.initialize_with_system_message("system")
    root_id = conv.current_node_id
    first = _node("first historical turn", "first result")
    conv.add_node(first, root_id, focus=False)
    second = _node("truncated historical turn", "truncated result")
    conv.add_node(second, root_id, focus=False)
    conv.nodes[root_id]["context_summaries"] = [{
        "id": "summary-1",
        "type": "prune_summary",
        "parent_node_id": root_id,
        "summary": "summary durable fact",
        "covered_node_ids": [first["id"], second["id"]],
        "compact_node_ids": [],
        "truncated_node_ids": [second["id"]],
        "created_at": 1,
        "status": "completed",
    }]

    parsed = parse_refer_prompt_args(
        f"node:{first['id']} prune:summary-1 truncated:summary-1 inspect evidence"
    )
    bundle = build_refer_bundle(conv, parsed["selectors"])
    content, truncated = format_refer_context_message(bundle)

    assert truncated is False
    assert bundle["source_node_ids"] == [first["id"], second["id"]]
    assert "first historical turn" in content
    assert "summary durable fact" in content
    assert "truncated historical turn" in content
    assert "Do not treat different referenced branches as sequential history" in content


def test_refer_bundle_supports_compact_and_before_selectors():
    conv = Conversation(title="refer compact")
    conv.initialize_with_system_message("system")
    root_id = conv.current_node_id
    first = _node("first pre-compact turn", "first pre-compact result")
    conv.add_node(first, root_id)
    compact = NodeManager.create_compact_node(
        parent_id=first["id"],
        summary="Summary:\nfolded compact facts",
        model_id="fake-model",
        last_pre_compact_message_id=first["id"],
    )
    conv.add_node(compact, first["id"])

    parsed = parse_refer_prompt_args(f"before:{compact['id']} compact:{compact['id']} inspect folded history")
    bundle = build_refer_bundle(conv, parsed["selectors"])
    content, truncated = format_refer_context_message(bundle)

    assert truncated is False
    assert bundle["source_node_ids"] == [first["id"], compact["id"]]
    assert "first pre-compact turn" in content
    assert "folded compact facts" in content
    assert "Compact metadata" in content


def test_refer_bundle_deduplicates_repeated_node_selectors():
    conv = Conversation(title="refer dedupe")
    conv.initialize_with_system_message("system")
    root_id = conv.current_node_id
    node = _node("same evidence", "same result")
    conv.add_node(node, root_id, focus=False)

    parsed = parse_refer_prompt_args(f"node:{node['id']} node:{node['id']} compare once")
    bundle = build_refer_bundle(conv, parsed["selectors"])
    content, _ = format_refer_context_message(bundle)

    assert bundle["source_node_ids"] == [node["id"]]
    assert len(bundle["sources"]) == 1
    assert content.count("## Source") == 1


def test_refer_invalid_selector_fails_clearly():
    conv = Conversation(title="refer invalid")
    conv.initialize_with_system_message("system")
    parsed = parse_refer_prompt_args("node:missing-node inspect")

    try:
        build_refer_bundle(conv, parsed["selectors"])
    except ReferContextError as exc:
        assert "refer selector not found" in str(exc)
    else:
        raise AssertionError("expected ReferContextError")


def test_refer_context_total_budget_is_hard_limit(monkeypatch):
    conv = Conversation(title="refer budget")
    conv.initialize_with_system_message("system")
    root_id = conv.current_node_id
    node = _node("x" * 1_000, "y" * 1_000)
    conv.add_node(node, root_id, focus=False)
    monkeypatch.setattr(refer_context, "REFER_TOTAL_MAX_CHARS", 160)

    parsed = parse_refer_prompt_args(f"node:{node['id']} inspect budget")
    bundle = build_refer_bundle(conv, parsed["selectors"])
    content, truncated = format_refer_context_message(bundle)

    assert truncated is True
    assert len(content) <= 160
