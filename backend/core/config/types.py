# types.py - 更新类型定义
from typing import TypedDict, List, Optional, Dict, Any, Union, Required
from enum import Enum
from datetime import datetime

class Role(str, Enum):
    """消息角色枚举"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

class ModelProvider(str, Enum):
    """模型提供商枚举"""
    OPENAI = "openai"
    AZURE = "azure"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    DEEPSEEK = "deepseek"
    ANTHROPIC = "anthropic"
    GROQ = "groq"
    LOCAL = "local"

class Message(TypedDict):
    """基础消息类型"""
    id: str
    role: Role
    content: str
    name: Optional[str]
    tool_calls: Optional[List[Dict[str, Any]]]
    tool_call_id: Optional[str]
    timestamp: str

class ConversationTreeNode(TypedDict):
    """对话树节点 - 一轮完整交互"""
    id: str
    parent_id: Optional[str]
    children_ids: List[str]
    user_message: Optional[Message]
    assistant_message: Optional[Message]
    tool_messages: List[Message]
    system_message: Optional[Message]  # 仅根节点有
    timestamp: str
    model_id: Optional[str]
    total_tokens: int

class ConversationMetadata(TypedDict):
    """对话元数据"""
    id: str
    title: str
    created_at: str
    updated_at: str
    total_tokens: Dict[ModelProvider, int]

class ConversationData(TypedDict):
    """对话数据类型"""
    metadata: ConversationMetadata
    nodes: List[ConversationTreeNode]
    current_node_id: Optional[str]
    root_node_id: Optional[str]

class ChatConfig(TypedDict):
    """聊天配置"""
    save_history: bool
    max_history_messages: int
    system_prompt: Optional[str]

class ModelProviderConfig(TypedDict, total=False):
    """单个模型配置"""
    name: Optional[str]
    models: List[str]
    api_key: str
    base_url: str
    organization: Optional[str]
    project: Optional[str]
    # max_tokens: Optional[int]
    # temperature: Optional[float]
    # top_p: Optional[float]
    # frequency_penalty: Optional[float]
    # presence_penalty: Optional[float]
    # is_default: bool
    enabled: bool
    default_model: str

# class MultiModelConfig(TypedDict, total=False):
#     """多模型配置"""
#     models: Dict[str, ModelConfig]
#     default_model: str
#     save_history: bool
#     max_history_messages: int