# chat/prune_summary.py - 父节点子树剪枝摘要
from __future__ import annotations

import re
import uuid
from time import time
from typing import Any, Dict, List, Optional

from ..config.types import Role


PRUNE_SUMMARY_MAX_OUTPUT_TOKENS = 12_000
PRUNE_BRANCH_DIGEST_MAX_OUTPUT_TOKENS = 3_000
PRUNE_PACKET_MAX_CONTENT_CHARS = 6_000
PRUNE_PACKET_MAX_TOOL_CHARS = 4_000
PRUNE_PACKET_BUDGET_CHARS = 120_000

NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

You are summarizing structured ChatTree branch packets. The packet data is the
only factual source. Do not infer that sibling branches happened in one linear
conversation.

"""

PRUNE_SUMMARY_PROMPT = """Create a prune summary for continuing work from the selected parent node.

Your output must use this structure:

<summary>
1. Parent Context:
   - What the selected parent node was about.

2. Branch Results:
   - For each important child branch, summarize work done, conclusions, failures,
     files/commands/tools referenced, and unresolved questions.

3. Cross-Branch Synthesis:
   - Compare branch results. Mark contradictions or trade-offs with their source branch.

4. Durable Context To Inject:
   - Facts, decisions, constraints, paths, errors, user preferences, and next steps
     future turns should know when continuing from the parent node.

5. Coverage Notes:
   - Mention compact summaries, truncated content, or missing details explicitly.
</summary>

Rules:
- Preserve branch boundaries. Do not merge branches into a fake timeline.
- If two branches conflict, keep both and identify the branch source.
- Treat existing compact summaries as folded history unless packet coverage says otherwise.
- User guidance may adjust emphasis, but packet data remains the source of truth.
"""


def _role_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    if isinstance(value, str) and value.startswith("Role."):
        return value.split(".")[-1].lower()
    return str(value or "")


def _truncate_text(value: Any, max_chars: int) -> tuple[str, bool]:
    text = value if isinstance(value, str) else str(value or "")
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + f"\n\n[truncated from {len(text)} chars]", True


def _message_packet(message: Optional[Dict[str, Any]], max_chars: int) -> tuple[Optional[Dict[str, Any]], bool]:
    if not message:
        return None, False
    content, truncated = _truncate_text(message.get("content") or "", max_chars)
    packet: Dict[str, Any] = {
        "id": message.get("id"),
        "role": _role_value(message.get("role")),
        "content": content,
        "timestamp": message.get("timestamp"),
    }
    if message.get("subtype"):
        packet["subtype"] = message.get("subtype")
    if message.get("tool_calls"):
        packet["tool_calls"] = message.get("tool_calls")
    if message.get("tool_call_id"):
        packet["tool_call_id"] = message.get("tool_call_id")
    if message.get("name"):
        packet["name"] = message.get("name")
    if message.get("import_files"):
        packet["import_files"] = message.get("import_files")
    if message.get("image_refs"):
        packet["image_refs"] = message.get("image_refs")
    return packet, truncated


def _tool_packets(tool_messages: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], bool]:
    packets: List[Dict[str, Any]] = []
    truncated_any = False
    for message in tool_messages or []:
        packet, truncated = _message_packet(message, PRUNE_PACKET_MAX_TOOL_CHARS)
        if packet:
            packets.append(packet)
        truncated_any = truncated_any or truncated
    return packets, truncated_any


def _raw_turn_packet(node: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    user, user_truncated = _message_packet(node.get("user_message"), PRUNE_PACKET_MAX_CONTENT_CHARS)
    assistant, assistant_truncated = _message_packet(node.get("assistant_message"), PRUNE_PACKET_MAX_CONTENT_CHARS)
    tools, tools_truncated = _tool_packets(node.get("tool_messages") or [])
    packet = {
        "node_id": node.get("id"),
        "parent_id": node.get("parent_id"),
        "children_ids": list(node.get("children_ids") or []),
        "timestamp": node.get("timestamp"),
        "model_id": node.get("model_id"),
        "user": user,
        "assistant": assistant,
        "tool_messages": tools,
    }
    return packet, user_truncated or assistant_truncated or tools_truncated


def is_compact_boundary_node(node: Dict[str, Any]) -> bool:
    system_message = node.get("system_message") or {}
    return (
        _role_value(system_message.get("role")) == Role.SYSTEM.value
        and system_message.get("subtype") == "compact_boundary"
    )


def _compact_segment(node: Dict[str, Any]) -> Dict[str, Any]:
    summary_message = node.get("user_message") or {}
    content, truncated = _truncate_text(summary_message.get("content") or "", PRUNE_PACKET_MAX_CONTENT_CHARS)
    system_message = node.get("system_message") or {}
    return {
        "type": "compact_summary",
        "compact_node_id": node.get("id"),
        "parent_id": node.get("parent_id"),
        "content": content,
        "compact_metadata": system_message.get("compact_metadata") or {},
        "truncated": truncated,
    }


def _ancestor_ids_until(conversation: Any, node_id: str, stop_node_id: str) -> List[str]:
    ancestor_ids: List[str] = []
    current = (conversation.nodes.get(node_id) or {}).get("parent_id")
    while current and current != "None" and current != stop_node_id:
        if current not in conversation.nodes:
            break
        ancestor_ids.append(current)
        current = conversation.nodes[current].get("parent_id")
    return ancestor_ids


def _flush_raw_segment(segments: List[Dict[str, Any]], raw_turns: List[Dict[str, Any]]) -> None:
    if not raw_turns:
        return
    segments.append({
        "type": "raw_turns",
        "node_ids": [turn.get("node_id") for turn in raw_turns if turn.get("node_id")],
        "turns": list(raw_turns),
    })
    raw_turns.clear()


def _subtree_order(conversation: Any, start_node_id: str) -> List[str]:
    ordered: List[str] = []
    stack = [start_node_id]
    while stack:
        node_id = stack.pop()
        if node_id not in conversation.nodes:
            continue
        ordered.append(node_id)
        children = list(conversation.nodes[node_id].get("children_ids") or [])
        children.sort(key=lambda child_id: conversation.nodes.get(child_id, {}).get("timestamp") or 0, reverse=True)
        stack.extend(children)
    return ordered


def build_prune_packets(conversation: Any, parent_node_id: str) -> Dict[str, Any]:
    if parent_node_id not in conversation.nodes:
        raise ValueError("父节点不存在")

    parent = conversation.nodes[parent_node_id]
    child_ids = list(parent.get("children_ids") or [])
    if not child_ids:
        raise ValueError("该节点没有可摘要的子分支")

    current_chain_ids = {
        node.get("id")
        for node in conversation.get_node_chain(conversation.current_node_id)
        if node.get("id")
    }
    child_ids.sort(
        key=lambda child_id: (
            0 if child_id in current_chain_ids else 1,
            conversation.nodes.get(child_id, {}).get("timestamp") or 0,
        )
    )

    parent_user, parent_user_truncated = _message_packet(parent.get("user_message"), PRUNE_PACKET_MAX_CONTENT_CHARS)
    parent_assistant, parent_assistant_truncated = _message_packet(parent.get("assistant_message"), PRUNE_PACKET_MAX_CONTENT_CHARS)
    packets: List[Dict[str, Any]] = []
    covered_node_ids: List[str] = []
    compact_node_ids: List[str] = []
    truncated_node_ids: List[str] = []
    coverage_notes: List[str] = []

    for branch_order, child_id in enumerate(child_ids, start=1):
        segments: List[Dict[str, Any]] = []
        raw_turns: List[Dict[str, Any]] = []
        branch_node_ids = _subtree_order(conversation, child_id)
        folded_node_ids: List[str] = []
        for node_id in branch_node_ids:
            node = conversation.nodes[node_id]
            covered_node_ids.append(node_id)
            if is_compact_boundary_node(node):
                folded = set(_ancestor_ids_until(conversation, node_id, parent_node_id))
                if folded:
                    folded_node_ids.extend(node_id for node_id in folded if node_id not in folded_node_ids)
                    raw_turns = [
                        turn for turn in raw_turns
                        if turn.get("node_id") not in folded
                    ]
                _flush_raw_segment(segments, raw_turns)
                segment = _compact_segment(node)
                segment["folded_node_ids"] = list(folded)
                segments.append(segment)
                compact_node_ids.append(node_id)
                if segment.get("truncated"):
                    truncated_node_ids.append(node_id)
                continue
            turn, truncated = _raw_turn_packet(node)
            raw_turns.append(turn)
            if truncated:
                truncated_node_ids.append(node_id)
        _flush_raw_segment(segments, raw_turns)
        packets.append({
            "direct_child_node_id": child_id,
            "branch_order": branch_order,
            "is_current_branch": child_id in current_chain_ids,
            "segments": segments,
            "coverage": {
                "included_node_ids": branch_node_ids,
                "folded_node_ids": folded_node_ids,
                "used_existing_compact": any(
                    segment.get("type") == "compact_summary" for segment in segments
                ),
                "truncated": any(node_id in truncated_node_ids for node_id in branch_node_ids),
            },
        })

    if parent_user_truncated or parent_assistant_truncated:
        coverage_notes.append("父节点 anchor 内容被截断。")
    if compact_node_ids:
        coverage_notes.append("部分分支使用已有 compact summary 作为折叠历史。")
    if truncated_node_ids:
        coverage_notes.append("部分节点内容因长度限制被截断，可通过 /refer 取回原始轮次。")

    return {
        "parent": {
            "node_id": parent_node_id,
            "parent_id": parent.get("parent_id"),
            "timestamp": parent.get("timestamp"),
            "user": parent_user,
            "assistant": parent_assistant,
        },
        "branch_packets": packets,
        "coverage": {
            "covered_node_ids": covered_node_ids,
            "covered_direct_child_ids": child_ids,
            "compact_node_ids": compact_node_ids,
            "truncated_node_ids": truncated_node_ids,
            "coverage_notes": coverage_notes,
        },
    }


def build_prune_summary_messages(packet_bundle: Dict[str, Any], custom_instructions: Optional[str] = None) -> List[Dict[str, Any]]:
    parts = [
        "Selected parent and child branch packets follow as JSON.",
        "Treat this JSON as structured evidence, not as a linear transcript.",
    ]
    if custom_instructions and custom_instructions.strip():
        parts.append(f"\nUser guidance:\n{custom_instructions.strip()}")
    parts.append("\nPacket bundle:")
    parts.append(json_dumps(packet_bundle))
    parts.append("\nReturn only the requested <summary> block.")
    return [
        {"role": "system", "content": NO_TOOLS_PREAMBLE + PRUNE_SUMMARY_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def build_branch_digest_messages(
    parent_packet: Dict[str, Any],
    branch_packet: Dict[str, Any],
    custom_instructions: Optional[str] = None,
) -> List[Dict[str, Any]]:
    parts = [
        "Summarize this single ChatTree child branch packet into a branch digest.",
        "Preserve conclusions, failures, files/tools/commands, unresolved questions, compact dependencies, and truncation notes.",
    ]
    if custom_instructions and custom_instructions.strip():
        parts.append(f"\nUser guidance:\n{custom_instructions.strip()}")
    parts.append("\nParent packet:")
    parts.append(json_dumps(parent_packet))
    parts.append("\nBranch packet:")
    parts.append(json_dumps(branch_packet))
    parts.append("\nReturn only a concise branch digest in plain text.")
    return [
        {"role": "system", "content": NO_TOOLS_PREAMBLE + "You create faithful branch digests from structured packets."},
        {"role": "user", "content": "\n".join(parts)},
    ]


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2)


def format_prune_summary(summary: str) -> str:
    formatted = re.sub(r"<analysis>[\s\S]*?</analysis>", "", summary or "").strip()
    match = re.search(r"<summary>([\s\S]*?)</summary>", formatted)
    if match:
        formatted = match.group(1).strip()
    formatted = re.sub(r"\n\n+", "\n\n", formatted)
    return formatted.strip()


def build_prune_context_message(summary: Dict[str, Any]) -> str:
    coverage_notes = summary.get("coverage_notes") or []
    lines = [
        "The following is a summarized context from child branches previously explored under this parent node.",
        "Use it as background for continuing from this parent, but do not treat the listed branches as a single linear conversation.",
        "",
        str(summary.get("summary") or "").strip(),
    ]
    if coverage_notes:
        lines.append("")
        lines.append("Coverage notes:")
        for note in coverage_notes:
            lines.append(f"- {note}")
    covered = summary.get("covered_node_ids") or []
    compact = summary.get("compact_node_ids") or []
    truncated = summary.get("truncated_node_ids") or []
    if covered or compact or truncated:
        lines.append("")
        lines.append("Refer anchors:")
        if covered:
            lines.append(f"- Covered nodes: {', '.join(str(node_id) for node_id in covered[:20])}{' ...' if len(covered) > 20 else ''}")
        if compact:
            lines.append(f"- Compact nodes: {', '.join(str(node_id) for node_id in compact)}")
        if truncated:
            lines.append(f"- Truncated nodes: {', '.join(str(node_id) for node_id in truncated)}")
    return "\n".join(lines).strip()


def create_prune_summary_record(
    *,
    parent_node_id: str,
    summary: str,
    packet_bundle: Dict[str, Any],
    model_id: Optional[str],
    provider_id: Optional[str],
    custom_instructions: Optional[str],
    tokens_used: int,
    branch_digests: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    coverage = packet_bundle.get("coverage") or {}
    return {
        "id": str(uuid.uuid4()),
        "type": "prune_summary",
        "parent_node_id": parent_node_id,
        "created_at": int(time()),
        "model_id": model_id,
        "provider_id": provider_id,
        "user_instructions": custom_instructions,
        "summary": format_prune_summary(summary),
        "branch_digests": list(branch_digests or []),
        "covered_node_ids": list(coverage.get("covered_node_ids") or []),
        "covered_direct_child_ids": list(coverage.get("covered_direct_child_ids") or []),
        "compact_node_ids": list(coverage.get("compact_node_ids") or []),
        "truncated_node_ids": list(coverage.get("truncated_node_ids") or []),
        "coverage_notes": list(coverage.get("coverage_notes") or []),
        "tokens_used": int(tokens_used or 0),
        "status": "completed",
    }
