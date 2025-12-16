# chat/chat_manager.py - 适配延迟加载
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime
from .conversation import Conversation
from .node import NodeManager
from ..config.types import Message, Role, ConversationTreeNode, ChatConfig
from ..storage.base import StorageInterface
from ..model.model_manager import ModelManager
from ..utils.logger import setup_logger

logger = setup_logger('ChatManager')

class ChatManager:
    """延迟加载模型的聊天管理器"""
    
    def __init__(self, model_manager: ModelManager, storage: StorageInterface, config: ChatConfig):
        self.model_manager = model_manager
        self.storage = storage
        self.config = config
        self.current_conversation: Optional[Conversation] = None
    
    def create_conversation(self, title: str = '') -> Conversation:
        """
        创建新对话（不实例化模型，只保存配置ID）
        """
        # 创建对话，只保存model_id字符串引用
        conversation = Conversation(model=self.model_manager.current_model, title=title)
        
        # 初始化系统消息
        system_prompt = self.config.get("system_prompt")
        if system_prompt:
            conversation.initialize_with_system_message(system_prompt)
        
        self.current_conversation = conversation
        logger.info(f"对话创建成功，将使用模型: {self.model_manager.current_model}")
        return conversation
    
    def load_conversation(self, conversation_id: str) -> bool:
        """加载对话"""
        data = self.storage.load(conversation_id)
        if data:
            self.current_conversation = Conversation.from_dict(data)
            return True
        return False
    
    def save_conversation(self):
        """保存当前对话"""
        if self.current_conversation:
            self.storage.save(self.current_conversation.to_dict())
    
    def list_conversations(self) -> List[Dict[str, str]]:
        """列出所有对话"""
        return self.storage.list()
    
    def delete_conversation(self, conversation_id: str):
        """删除对话"""
        self.storage.delete(conversation_id)
        if self.current_conversation and self.current_conversation.metadata["id"] == conversation_id:
            self.current_conversation = None
    
    def send_message(
        self,
        content: str,
        model_id: Optional[str] = None
    ) -> str:
        """
        发送消息（在此首次实例化模型）
        """
        if not self.current_conversation:
            logger.error("没有加载的对话，无法发送消息")
            return ""
        
        # 确定使用的模型ID
        target_model = model_id or self.current_conversation.metadata.get("model")
        
        if not target_model:
            logger.error("未指定模型ID，无法发送消息")
            return ""
        
        # 获取或创建模型实例（延迟加载的核心）
        # 第一次调用时会实例化模型并缓存
        target_provider = None
        for provider, models in self.model_manager.model_list.items():
            if target_model in models:
                target_provider = provider
                break
        use_model = self.model_manager.get_model(target_provider)
        if not use_model:
            logger.error(f"无法找到模型实例: {target_model}")
            return ""
        
        # 创建用户消息
        user_msg = Message({
            "id": str(uuid.uuid4()),
            "role": Role.USER,
            "content": content,
            "name": None,
            "tool_calls": None,
            "tool_call_id": None,
            "timestamp": datetime.now().isoformat()
        })
        
        # 创建新节点
        current_node_id = self.current_conversation.current_node_id
        new_node = NodeManager.create_node(
            user_message=user_msg,
            parent_id=current_node_id,
            model_id=target_model
        )
        
        # 添加节点到对话树
        self.current_conversation.add_node(new_node, parent_id=current_node_id)
        
        # 准备消息链用于API调用
        messages = self._prepare_messages_for_api()
        
        # 调用模型生成回复
        response_content = ""
        tool_calls = []
        
        try:
            # 使用模型的统一接口生成回复
            # 这行代码会触发实际的API调用
            response_content, tokens = use_model.generate_response(
                messages=messages,
                # max_tokens=self.config.get("max_tokens"),
                # temperature=self.config.get("temperature"),
                # top_p=self.config.get("top_p"),
                # frequency_penalty=self.config.get("frequency_penalty"),
                # presence_penalty=self.config.get("presence_penalty")
            )
            
            # 创建助手消息
            assistant_msg = Message({
                "id": str(uuid.uuid4()),
                "role": Role.ASSISTANT,
                "content": response_content,
                "name": None,
                "tool_calls": tool_calls if tool_calls else None,
                "tool_call_id": None,
                "timestamp": datetime.now().isoformat()
            })
            
            # 添加到当前节点
            NodeManager.add_assistant_message(new_node, assistant_msg)
            
            # 处理工具调用等...
            # if tool_calls:
            #     self._process_tool_calls(new_node, tool_calls, use_model)
            
            # 更新token统计
            self._update_token_stats(use_model, tokens)
            
            # 保存对话
            if self.config.get("save_history", True):
                self.save_conversation()
            
            return response_content
            
        except Exception as e:
            logger.error(e)
            return ''
    
    def _prepare_messages_for_api(self) -> List[Message]:
        """准备API调用的消息列表"""
        if not self.current_conversation:
            return []
        
        messages = self.current_conversation.get_message_chain_from_node(self.current_conversation.current_node_id)
        
        return messages
    
    def _update_token_stats(self, model, tokens: int):
        """更新token统计"""
        model_key = model.get_model_info().get("id")
        assert self.current_conversation is not None
        if model_key:
            if model_key not in self.current_conversation.metadata["total_tokens"]:
                self.current_conversation.metadata["total_tokens"][model_key] = 0
            
            # 粗略估算token数（实际应从API响应获取）
            self.current_conversation.metadata["total_tokens"][model_key] += tokens
    
    def get_conversation_history(self) -> List[Message]:
        """获取对话历史"""
        if self.current_conversation:
            return self.current_conversation.get_message_chain_from_node(self.current_conversation.current_node_id)
        return []
    
    def delelte_conversation(self, conversation_id: str):
        """删除对话"""
        self.storage.delete(conversation_id)
        if self.current_conversation and self.current_conversation.metadata["id"] == conversation_id:
            self.current_conversation = None
        