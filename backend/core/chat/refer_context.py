# chat/refer_context.py - /refer historical evidence injection
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from ..config.types import Role


REFER_MAX_CONTENT_CHARS = 8_000
REFER_MAX_TOOL_CHARS = 6_000
REFER_TOTAL_MAX_CHARS = 80_000

_SELECTOR_KINDS = {"node", "compact", "before", "prune", "truncated"}
_BARE_NODE_ID_RE = re.compile(r"^[0-9a-fA-F]{8,}(?:-[0-9a-fA-F]{4,}){0,4}$")


class ReferContextError(ValueError):
    pass


def _role_value(role: Any) -> str:
    if isinstance(role, Role):
        return role.value
    value = str(role or "")
    if value.startswith("Role."):
        return value.split(".")[-1].lower()
    return value.lower()


def _clip(value: Any, max_chars: int) -> tuple[str, bool]:
    text = value if isinstance(value, str) else str(value or "")
    if len(text) <= max_chars:
        return text, False
    marker = f"\n\n[refer truncated {len(text) - max_chars} chars]\n\n"
    if max_chars <= len(marker):
        return marker[:max_chars], True
    visible_chars = max_chars - len(marker)
    head = visible_chars // 2
    tail = visible_chars - head
    return (
        text[:head].rstrip()
        + marker
        + text[-tail:].lstrip(),
        True,
    )


def _json_preview(value: Any, max_chars: int = 2_000) -> tuple[str, bool]:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        text = str(value)
    return _clip(text, max_chars)


def _selector_from_token(token: str) -> Optional[Dict[str, str]]:
    token = token.strip()
    if not token:
        return None
    if ":" in token:
        kind, value = token.split(":", 1)
        kind = kind.strip().lower()
        value = value.strip()
        if kind in _SELECTOR_KINDS and value:
            return {"kind": kind, "value": value, "raw": token}
        return None
    if _BARE_NODE_ID_RE.match(token):
        return {"kind": "node", "value": token, "raw": token}
    return None


def parse_refer_prompt_args(args: str) -> Dict[str, Any]:
    text = args or ""
    cursor = 0
    selectors: List[Dict[str, str]] = []
    while cursor < len(text):
        whitespace = re.match(r"\s*", text[cursor:])
        if whitespace:
            cursor += len(whitespace.group(0))
        if cursor >= len(text):
            break
        token_match = re.match(r"\S+", text[cursor:])
        if not token_match:
            break
        token = token_match.group(0)
        if token == "--":
            cursor += len(token)
            break
        selector = _selector_from_token(token)
        if selector is None:
            break
        selectors.append(selector)
        cursor += len(token)
    prompt = text[cursor:].lstrip()
    if not selectors:
        raise ReferContextError("用法: /refer <selector...> <本轮问题或指令>")
    if not prompt.strip():
        raise ReferContextError("用法: /refer <selector...> <本轮问题或指令>")
    return {"selectors": selectors, "prompt": prompt}


def _is_compact_boundary_node(node: Dict[str, Any]) -> bool:
    system_message = node.get("system_message") or {}
    return (
        _role_value(system_message.get("role")) == Role.SYSTEM.value
        and system_message.get("subtype") == "compact_boundary"
    )


def _find_prune_summary(conversation: Any, summary_id: str) -> Optional[Dict[str, Any]]:
    for node in (conversation.nodes or {}).values():
        for summary in node.get("context_summaries") or []:
            if isinstance(summary, dict) and str(summary.get("id") or "") == summary_id:
                return summary
    return None


def resolve_refer_targets(conversation: Any, selectors: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()

    def add_target(target: Dict[str, Any]) -> None:
        key = str(target.get("key") or "")
        if key and key in seen_keys:
            return
        if key:
            seen_keys.add(key)
        targets.append(target)

    for selector in selectors:
        kind = selector["kind"]
        value = selector["value"]
        raw = selector["raw"]
        if kind == "node":
            if value not in conversation.nodes:
                raise ReferContextError(f"refer selector not found: {raw}")
            add_target({"key": f"node:{value}", "kind": "node", "selector": raw, "node_ids": [value]})
        elif kind == "compact":
            node = conversation.nodes.get(value)
            if not node or not _is_compact_boundary_node(node):
                raise ReferContextError(f"compact selector not found: {raw}")
            add_target({"key": f"compact:{value}", "kind": "compact", "selector": raw, "node_ids": [value]})
        elif kind == "before":
            node = conversation.nodes.get(value)
            if not node or not _is_compact_boundary_node(node):
                raise ReferContextError(f"compact selector not found: {raw}")
            chain = conversation.get_node_chain(value)
            node_ids = [
                str(item.get("id"))
                for item in chain[:-1]
                if item.get("id") and item.get("id") != conversation.root_node_id
            ]
            if not node_ids:
                raise ReferContextError(f"compact selector has no pre-compact nodes: {raw}")
            add_target({"key": f"before:{value}", "kind": "before", "selector": raw, "node_ids": node_ids})
        elif kind == "prune":
            summary = _find_prune_summary(conversation, value)
            if not summary:
                raise ReferContextError(f"prune selector not found: {raw}")
            add_target({"key": f"prune:{value}", "kind": "prune", "selector": raw, "summary": summary})
        elif kind == "truncated":
            summary = _find_prune_summary(conversation, value)
            if not summary:
                raise ReferContextError(f"prune selector not found: {raw}")
            node_ids = [str(node_id) for node_id in (summary.get("truncated_node_ids") or [])]
            node_ids = [node_id for node_id in node_ids if node_id in conversation.nodes]
            if not node_ids:
                raise ReferContextError(f"prune selector has no truncated nodes: {raw}")
            add_target({
                "key": f"truncated:{value}",
                "kind": "truncated",
                "selector": raw,
                "summary_id": value,
                "node_ids": node_ids,
            })
    return targets


def _message_section(label: str, message: Optional[Dict[str, Any]], max_chars: int) -> tuple[List[str], bool]:
    if not message:
        return [], False
    content, truncated = _clip(message.get("content") or "", max_chars)
    lines = [f"[{label}]"]
    if message.get("id"):
        lines.append(f"id: {message.get('id')}")
    if message.get("subtype"):
        lines.append(f"subtype: {message.get('subtype')}")
    if content.strip():
        lines.append(content)
    if message.get("tool_calls"):
        tool_calls, calls_truncated = _json_preview(message.get("tool_calls"), 3_000)
        truncated = truncated or calls_truncated
        lines.extend(["tool_calls:", tool_calls])
    return lines, truncated


def _tool_message_sections(messages: List[Dict[str, Any]]) -> tuple[List[str], bool]:
    lines: List[str] = []
    truncated = False
    for index, message in enumerate(messages or [], start=1):
        content, content_truncated = _clip(
            message.get("model_visible_content")
            if message.get("model_visible_content") is not None
            else message.get("content") or "",
            REFER_MAX_TOOL_CHARS,
        )
        truncated = truncated or content_truncated
        lines.append(f"[Tool result {index}]")
        if message.get("name"):
            lines.append(f"name: {message.get('name')}")
        if message.get("tool_call_id"):
            lines.append(f"tool_call_id: {message.get('tool_call_id')}")
        if content.strip():
            lines.append(content)
    return lines, truncated


def _node_packet(conversation: Any, node_id: str) -> Dict[str, Any]:
    node = conversation.nodes[node_id]
    truncated = False
    lines = [
        f"Node: {node_id}",
        f"Parent: {node.get('parent_id')}",
        f"Children: {', '.join(str(item) for item in (node.get('children_ids') or [])) or '(none)'}",
        f"Timestamp: {node.get('timestamp')}",
    ]
    if _is_compact_boundary_node(node):
        lines.append("Type: compact boundary")
        compact_meta, meta_truncated = _json_preview((node.get("system_message") or {}).get("compact_metadata") or {})
        truncated = truncated or meta_truncated
        lines.extend(["Compact metadata:", compact_meta])

    user_lines, user_truncated = _message_section("User", node.get("user_message"), REFER_MAX_CONTENT_CHARS)
    assistant_lines, assistant_truncated = _message_section(
        "Assistant",
        node.get("assistant_message"),
        REFER_MAX_CONTENT_CHARS,
    )
    truncated = truncated or user_truncated or assistant_truncated
    lines.extend(user_lines)
    assistant = node.get("assistant_message") or {}
    interactions = assistant.get("tool_interactions") or []
    if interactions:
        for index, interaction in enumerate(interactions, start=1):
            lines.append(f"[Tool interaction {index}]")
            interaction_assistant, ia_truncated = _message_section(
                "Assistant tool call",
                interaction.get("assistant"),
                REFER_MAX_CONTENT_CHARS,
            )
            truncated = truncated or ia_truncated
            lines.extend(interaction_assistant)
            tool_lines, tool_truncated = _tool_message_sections(interaction.get("tools") or [])
            truncated = truncated or tool_truncated
            lines.extend(tool_lines)
    else:
        tool_lines, tool_truncated = _tool_message_sections(node.get("tool_messages") or [])
        truncated = truncated or tool_truncated
        lines.extend(tool_lines)
    lines.extend(assistant_lines)
    return {
        "kind": "node",
        "node_id": node_id,
        "content": "\n".join(lines).strip(),
        "truncated": truncated,
    }


def _prune_packet(summary: Dict[str, Any]) -> Dict[str, Any]:
    content, truncated = _clip(summary.get("summary") or "", REFER_MAX_CONTENT_CHARS * 2)
    lines = [
        f"Prune summary: {summary.get('id')}",
        f"Parent node: {summary.get('parent_node_id')}",
        f"Created at: {summary.get('created_at')}",
        f"Covered nodes: {', '.join(str(item) for item in (summary.get('covered_node_ids') or [])) or '(none)'}",
        f"Compact nodes: {', '.join(str(item) for item in (summary.get('compact_node_ids') or [])) or '(none)'}",
        f"Truncated nodes: {', '.join(str(item) for item in (summary.get('truncated_node_ids') or [])) or '(none)'}",
        "[Summary]",
        content,
    ]
    return {
        "kind": "prune",
        "summary_id": summary.get("id"),
        "content": "\n".join(lines).strip(),
        "truncated": truncated,
    }


def build_refer_bundle(conversation: Any, selectors: List[Dict[str, str]]) -> Dict[str, Any]:
    targets = resolve_refer_targets(conversation, selectors)
    sources: List[Dict[str, Any]] = []
    source_node_ids: List[str] = []
    truncated_sources: List[str] = []
    seen_nodes: set[str] = set()

    for target in targets:
        packets: List[Dict[str, Any]] = []
        if target["kind"] == "prune":
            packets.append(_prune_packet(target["summary"]))
        else:
            for node_id in target.get("node_ids") or []:
                if node_id in seen_nodes:
                    continue
                seen_nodes.add(node_id)
                source_node_ids.append(node_id)
                packets.append(_node_packet(conversation, node_id))
        if any(packet.get("truncated") for packet in packets):
            truncated_sources.append(target["selector"])
        if packets:
            sources.append({
                "selector": target["selector"],
                "kind": target["kind"],
                "packets": packets,
            })

    return {
        "selectors": [item["raw"] for item in selectors],
        "sources": sources,
        "source_node_ids": source_node_ids,
        "truncated_sources": truncated_sources,
    }


def format_refer_context_message(bundle: Dict[str, Any]) -> tuple[str, bool]:
    lines = [
        "<system-reminder>",
        "Explicit /refer context requested by the user.",
        "This is historical evidence, not the current linear conversation.",
        "Do not treat different referenced branches as sequential history.",
        "",
    ]
    for index, source in enumerate(bundle.get("sources") or [], start=1):
        lines.append(f"## Source {index}: {source.get('selector')} ({source.get('kind')})")
        for packet in source.get("packets") or []:
            lines.append(packet.get("content") or "")
            lines.append("")
    if bundle.get("truncated_sources"):
        lines.append("Truncation notes:")
        for selector in bundle["truncated_sources"]:
            lines.append(f"- {selector} was truncated by /refer per-source budget.")
    lines.append("</system-reminder>")
    content = "\n".join(lines).strip()
    if len(content) <= REFER_TOTAL_MAX_CHARS:
        return content, bool(bundle.get("truncated_sources"))
    visible, _ = _clip(content, REFER_TOTAL_MAX_CHARS)
    return visible, True
