# types.py - 更新类型定义
from typing import List, Optional, Dict, Any, Union
from enum import Enum
import asyncio
from typing_extensions import TypedDict, Required

# 持久化 schema 版本。新写入打此版本；加载时若数据版本高于此值则拒绝。
SCHEMA_VERSION = 1


class Role(str, Enum):
    """消息角色枚举"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

ModelProvider = str  # 自定义提供商ID，不再使用枚举

class APIFormat(str, Enum):
    """API 格式枚举"""
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"

class UsageInfo(TypedDict, total=False):
    """Unified token usage; raw keeps provider-specific fields."""
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    source: str
    raw: Dict[str, Any]

class NodeUsage(TypedDict, total=False):
    """Layered usage snapshot stored on each conversation node."""
    turn_usage: UsageInfo
    branch_usage: UsageInfo
    active_context_usage: UsageInfo
    model_context_window: Optional[int]

class GenerationInfo(TypedDict, total=False):
    """消息生成信息"""
    duration_ms: int  # 生成用时（毫秒）
    status: str  # 生成状态：completed, error, stopped
    error_message: Optional[str]  # 错误信息
    tokens_used: int  # 使用的token数


    usage_info: UsageInfo

class Message(TypedDict, total=False):
    """基础消息类型"""
    id: Required[str]
    role: Required[Role]
    content: Required[Any]
    subtype: Optional[str]  # 系统事件子类型，例如 compact_boundary
    node_id: Optional[str]  # 所在对话树节点ID
    parent_node_id: Optional[str]  # 父节点ID
    name: Optional[str]
    tool_calls: Optional[List[Dict[str, Any]]]
    tool_call_id: Optional[str]
    tool_results: Optional[List[Dict[str, Any]]]  # 本消息触发的工具调用结果（未来工具轮次填充）
    tool_interactions: Optional[List[Dict[str, Any]]]  # 工具轮次序列：assistant tool_call + tool result
    approval_events: Optional[List[Dict[str, Any]]]  # 本消息触发的工具审批请求/结果事件
    reasoning: Optional[str]  # 推理/思考轨迹（未来推理模型填充）
    timestamp: Required[int]
    generation_info: Optional[GenerationInfo]  # 生成信息（仅助手消息有，可选）
    context_usage: Optional[NodeUsage]  # 所在节点的分层上下文 usage 快照
    compact_metadata: Optional[Dict[str, Any]]  # compact boundary 元数据
    is_compact_summary: Optional[bool]  # Claude Code 风格 compact summary 标记
    is_visible_in_transcript_only: Optional[bool]  # UI/transcript 可见，语义上是恢复上下文
    import_files: Optional[List[Dict[str, Any]]]  # 用户显式引用的导入文件元数据
    image_refs: Optional[List[Dict[str, Any]]]  # 用户显式引用的图片附件元数据
    tool_permission_mode: Optional[str]  # 所在节点的工具审批模式

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
    tool_permission_mode: Optional[str]
    total_tokens: int

    branch_usage_info: UsageInfo
    usage: NodeUsage

class ConversationMetadata(TypedDict, total=False):
    """对话元数据"""
    id: str
    title: str
    created_at: int
    updated_at: int
    total_tokens: Dict[str, int]
    model_id: Optional[str]       # 当前对话使用的模型ID
    provider_id: Optional[str]    # 当前对话使用的提供商ID
    reasoning_effort: Optional[str]   # 当前对话的推理强度档位（None=不发送）
    thinking_enabled: Optional[bool]  # 当前对话的思考模式开关（None=不发送）
    schema_version: Optional[int] # 持久化 schema 版本
    workspace: Dict[str, Any] # 对话绑定的工作区快照

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
    api_format: str  # APIFormat 值: chat_completions, responses, anthropic, gemini
    hidden_models: List[str]  # 被隐藏的模型名称列表
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
    def __init__(self, node_id: str, conversation_id: str, run_id: Optional[str] = None):
        self.node_id = node_id
        self.conversation_id = conversation_id
        self.run_id = run_id
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
class StreamChunk(TypedDict, total=False):
    """流式数据块

    total=False：新增字段均为可选，保持向后兼容。当前文本路径只填写
    status/content/node_id/conversation_id/error/tokens_used；event_type/
    reasoning/tool_call/approval 留给推理、工具与审批事件，缺省时读取方按文本处理。
    """
    status: StreamStatus
    content: Optional[str]
    node_id: Optional[str]
    target_node_id: Optional[str]
    run_id: Optional[str]
    event_index: Optional[int]
    conversation_id: Optional[str]
    error: Optional[str]
    tokens_used: int  # 当前chunk的token数
    event_type: Optional[str]            # "text" | "reasoning" | "tool_call_start" | "tool_call" | "tool_result" | "tool_approval_request" | "tool_approval_result"，缺省按 text
    reasoning: Optional[str]             # 推理增量
    tool_call: Optional[Dict[str, Any]]  # 工具调用增量/完整载荷
    tool_calls: Optional[List[Dict[str, Any]]]  # 完整工具调用列表（provider 聚合后填充）
    approval: Optional[Dict[str, Any]]   # 工具审批请求/结果载荷

    usage_info: UsageInfo

class StreamResult(TypedDict):
    """流最终结果"""
    content: str
    node_id: str
    conversation_id: str
    is_stopped: bool  # 是否被手动终止
