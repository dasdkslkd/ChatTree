# chat/node.py - 节点管理器
import uuid
from time import time
from typing import Optional, List, Dict, Any, Literal
from ..config.types import Message, Role, ConversationTreeNode
from .compact import get_compact_user_summary_message
from ..model.usage import estimated_usage


def _empty_node_usage() -> Dict[str, Any]:
    usage = estimated_usage(0)
    return {
        "turn_usage": dict(usage),
        "branch_usage": dict(usage),
        "active_context_usage": dict(usage),
        "model_context_window": None,
    }

class NodeManager:
    """对话树节点管理器"""

    @staticmethod
    def create_root_node(system_prompt: Optional[str] = None) -> ConversationTreeNode:
        """创建根节点（仅包含系统消息）"""
        node_id = str(uuid.uuid4())

        system_msg = None
        if system_prompt:
            system_msg = Message({
                "id": str(uuid.uuid4()),
                "role": Role.SYSTEM,
                "content": system_prompt,
                "name": None,
                "tool_calls": None,
                "tool_call_id": None,
                "timestamp": int(time())
            })

        return {
            "id": node_id,
            "parent_id": 'None',
            "children_ids": [],
            "user_message": None,
            "assistant_message": None,
            "tool_messages": [],
            "system_message": system_msg,
            "timestamp": int(time()),
            "model_id": None,
            "tool_permission_mode": None,
            "task_context_mode": "attached",
            "total_tokens": 0,
            "branch_usage_info": estimated_usage(0),
            "usage": _empty_node_usage(),
        }

    @staticmethod
    def create_node(
        user_message: Message,
        parent_id: Optional[str] = None,
        model_id: Optional[str] = None,
        tool_permission_mode: Optional[str] = None,
        task_context_mode: str = "attached",
    ) -> ConversationTreeNode:
        """创建新节点（一轮交互）"""
        node_id = str(uuid.uuid4())

        return {
            "id": node_id,
            "parent_id": parent_id,
            "children_ids": [],
            "user_message": user_message,
            "assistant_message": None,
            "tool_messages": [],
            "system_message": None,
            "timestamp": int(time()),
            "model_id": model_id,
            "tool_permission_mode": tool_permission_mode,
            "task_context_mode": task_context_mode,
            "total_tokens": 0,
            "branch_usage_info": estimated_usage(0),
            "usage": _empty_node_usage(),
        }

    @staticmethod
    def create_compact_node(
        parent_id: Optional[str],
        summary: str,
        trigger: Literal["manual", "auto"] = "manual",
        pre_tokens: int = 0,
        model_id: Optional[str] = None,
        last_pre_compact_message_id: Optional[str] = None,
        messages_to_keep: int = 1,
        restored_files: Optional[List[Dict[str, Any]]] = None,
        suppress_follow_up_questions: bool = True,
        task_context_mode: str = "attached",
    ) -> ConversationTreeNode:
        """创建 Claude Code 风格 compact boundary + summary 节点。"""
        node_id = str(uuid.uuid4())
        now = int(time())
        compact_metadata: Dict[str, Any] = {
            "trigger": trigger,
            "pre_tokens": int(pre_tokens or 0),
            "messages_to_keep": max(int(messages_to_keep or 0), 0),
        }
        if last_pre_compact_message_id:
            compact_metadata["last_pre_compact_message_id"] = last_pre_compact_message_id
        if restored_files:
            compact_metadata["restored_files"] = restored_files

        boundary_msg = Message({
            "id": str(uuid.uuid4()),
            "role": Role.SYSTEM,
            "subtype": "compact_boundary",
            "content": "Conversation compacted",
            "compact_metadata": compact_metadata,
            "timestamp": now,
        })
        summary_msg = Message({
            "id": str(uuid.uuid4()),
            "role": Role.USER,
            "content": get_compact_user_summary_message(
                summary,
                suppress_follow_up_questions=suppress_follow_up_questions,
            ),
            "is_compact_summary": True,
            "is_visible_in_transcript_only": True,
            "timestamp": now,
        })

        return {
            "id": node_id,
            "parent_id": parent_id,
            "children_ids": [],
            "user_message": summary_msg,
            "assistant_message": None,
            "tool_messages": [],
            "system_message": boundary_msg,
            "timestamp": now,
            "model_id": model_id,
            "tool_permission_mode": None,
            "task_context_mode": task_context_mode,
            "total_tokens": 0,
            "branch_usage_info": estimated_usage(0),
            "usage": _empty_node_usage(),
        }

    @staticmethod
    def add_assistant_message(node: ConversationTreeNode, message: Message):
        """添加助手回复到节点"""
        node["assistant_message"] = message
        node["timestamp"] = int(time())

    @staticmethod
    def add_tool_messages(node: ConversationTreeNode, messages: List[Message]):
        """添加工具调用结果到节点"""
        node["tool_messages"].extend(messages)
        node["timestamp"] = int(time())

    @staticmethod
    def mark_as_branch_point(node: ConversationTreeNode, child_id: str):
        """标记节点为分支点"""
        if child_id not in node["children_ids"]:
            node["children_ids"].append(child_id)
