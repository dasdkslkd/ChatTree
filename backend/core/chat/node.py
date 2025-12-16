# chat/node.py - 节点管理器
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from ..config.types import Message, Role, ConversationTreeNode

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
                "timestamp": datetime.now().isoformat()
            })
        
        return {
            "id": node_id,
            "parent_id": 'None',
            "children_ids": [],
            "user_message": None,
            "assistant_message": None,
            "tool_messages": [],
            "system_message": system_msg,
            "timestamp": datetime.now().isoformat(),
            "model_id": None,
            "total_tokens": 0
        }
    
    @staticmethod
    def create_node(
        user_message: Message,
        parent_id: Optional[str] = None,
        model_id: Optional[str] = None
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
            "timestamp": datetime.now().isoformat(),
            "model_id": model_id,
            "total_tokens": 0
        }
    
    @staticmethod
    def add_assistant_message(node: ConversationTreeNode, message: Message):
        """添加助手回复到节点"""
        node["assistant_message"] = message
        node["timestamp"] = datetime.now().isoformat()
    
    @staticmethod
    def add_tool_messages(node: ConversationTreeNode, messages: List[Message]):
        """添加工具调用结果到节点"""
        node["tool_messages"].extend(messages)
        node["timestamp"] = datetime.now().isoformat()
    
    @staticmethod
    def mark_as_branch_point(node: ConversationTreeNode, child_id: str):
        """标记节点为分支点"""
        if child_id not in node["children_ids"]:
            node["children_ids"].append(child_id)