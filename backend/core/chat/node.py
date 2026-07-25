# chat/node.py - 节点管理器
import uuid
from time import time
from typing import Optional, Dict, Any
from ..config.types import ConversationTreeNode
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
        """创建根节点。消息事实写入 canonical SQLite，不写入 node JSON。"""
        node_id = str(uuid.uuid4())

        return {
            "id": node_id,
            "parent_id": 'None',
            "children_ids": [],
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
        parent_id: Optional[str] = None,
        model_id: Optional[str] = None,
        tool_permission_mode: Optional[str] = None,
        task_context_mode: str = "attached",
    ) -> ConversationTreeNode:
        """创建新节点。节点只表达树结构，消息事实另存 canonical tables。"""
        node_id = str(uuid.uuid4())

        return {
            "id": node_id,
            "parent_id": parent_id,
            "children_ids": [],
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
        model_id: Optional[str] = None,
        task_context_mode: str = "attached",
    ) -> ConversationTreeNode:
        """创建 compact 节点；boundary/summary 事实写入 canonical messages。"""
        node_id = str(uuid.uuid4())
        now = int(time())

        return {
            "id": node_id,
            "parent_id": parent_id,
            "children_ids": [],
            "timestamp": now,
            "model_id": model_id,
            "tool_permission_mode": None,
            "task_context_mode": task_context_mode,
            "total_tokens": 0,
            "branch_usage_info": estimated_usage(0),
            "usage": _empty_node_usage(),
        }

    @staticmethod
    def mark_as_branch_point(node: ConversationTreeNode, child_id: str):
        """标记节点为分支点"""
        if child_id not in node["children_ids"]:
            node["children_ids"].append(child_id)
