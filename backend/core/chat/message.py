# chat/message.py - 更新消息管理
from time import time
from typing import Optional, List
import uuid
from ..config.types import Message, Role

class MessageManager:
    """消息管理器"""
    
    @staticmethod
    def create_message(
        role: Role,
        content: str,
        parent_id: Optional[str] = None,
        name: Optional[str] = None,
        tool_calls: Optional[List[dict]] = None,
        tool_call_id: Optional[str] = None,
        model_id: Optional[str] = None
    ) -> Message:
        """创建消息"""
        message_id = str(uuid.uuid4())
        return {
            "id": message_id,
            "role": role,
            "content": content,
            "name": name,
            "tool_calls": tool_calls,
            "tool_call_id": tool_call_id,
            "timestamp": int(time()),
        }
    
    @staticmethod
    def create_system_message(content: str) -> Message:
        """创建系统消息"""
        return MessageManager.create_message(Role.SYSTEM, content)
    
    @staticmethod
    def create_user_message(content: str, parent_id: Optional[str] = None) -> Message:
        """创建用户消息"""
        return MessageManager.create_message(Role.USER, content, parent_id=parent_id)
    
    @staticmethod
    def create_assistant_message(
        content: str,
        parent_id: Optional[str] = None,
        tool_calls: Optional[List[dict]] = None
    ) -> Message:
        """创建助手消息"""
        return MessageManager.create_message(
            Role.ASSISTANT,
            content,
            parent_id=parent_id,
            tool_calls=tool_calls
        )
    
    @staticmethod
    def create_tool_message(content: str, tool_call_id: str, parent_id: Optional[str] = None) -> Message:
        """创建工具消息"""
        return MessageManager.create_message(
            Role.TOOL,
            content,
            parent_id=parent_id,
            tool_call_id=tool_call_id
        )