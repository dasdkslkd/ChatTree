# types.py - 更新类型定义
from typing import List, Optional, Dict, Any, Union
from enum import Enum
import asyncio
from typing_extensions import TypedDict, Required

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
    NVIDIA = "nvidia"

class GenerationInfo(TypedDict, total=False):
    """消息生成信息"""
    duration_ms: int  # 生成用时（毫秒）
    status: str  # 生成状态：completed, error, stopped
    error_message: Optional[str]  # 错误信息
    tokens_used: int  # 使用的token数


class Message(TypedDict, total=False):
    """基础消息类型"""
    id: Required[str]
    role: Required[Role]
    content: Required[str]
    node_id: Optional[str]  # 所在对话树节点ID
    parent_node_id: Optional[str]  # 父节点ID
    name: Optional[str]
    tool_calls: Optional[List[Dict[str, Any]]]
    tool_call_id: Optional[str]
    timestamp: Required[int]
    generation_info: Optional[GenerationInfo]  # 生成信息（仅助手消息有，可选）

class ConversationTreeNode(TypedDict):
    """对话树节点 - 一轮完整交互"""
    id: str
    parent_id: Optional[str]
    children_ids: List[str]
    user_message: Optional[Message]
    assistant_message: Optional[Message]
    tool_messages: List[Message]
    system_message: Optional[Message]  # 仅根节点有
    timestamp: int
    model_id: Optional[str]
    total_tokens: int

class ConversationMetadata(TypedDict):
    """对话元数据"""
    id: str
    title: str
    created_at: int
    updated_at: int
    total_tokens: Dict[ModelProvider, int]

class ConversationData(TypedDict):
    """对话数据类型"""
    metadata: ConversationMetadata
    nodes: List[ConversationTreeNode]
    current_node_id: Optional[str]
    root_node_id: Optional[str]

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

class StreamStatus(str, Enum):
    """流状态枚举"""
    START = "start"
    CONTENT = "content" 
    COMPLETE = "complete"
    ERROR = "error"
    STOPPED = "stopped"

class StreamController:
    """流控制器，用于终止和管理活跃流"""
    def __init__(self, node_id: str, conversation_id: str):
        self.node_id = node_id
        self.conversation_id = conversation_id
        self._is_stopped = False
        self._lock = asyncio.Lock()
    
    async def stop(self):
        """标记为停止"""
        async with self._lock:
            self._is_stopped = True
    
    async def is_stopped(self) -> bool:
        """检查是否已停止"""
        async with self._lock:
            return self._is_stopped

# 扩展StreamChunk，添加token统计
class StreamChunk(TypedDict):
    """流式数据块"""
    status: StreamStatus
    content: Optional[str]
    node_id: Optional[str]
    conversation_id: Optional[str]
    error: Optional[str]
    tokens_used: int  # 新增：当前chunk的token数

class StreamResult(TypedDict):
    """流最终结果"""
    content: str
    node_id: str
    conversation_id: str
    is_stopped: bool  # 是否被手动终止