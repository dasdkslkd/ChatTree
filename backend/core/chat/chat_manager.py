# chat/chat_manager.py - 适配延迟加载
from typing import List, Optional, Dict, AsyncIterator
import uuid
import asyncio  
from datetime import datetime
from .conversation import Conversation
from .node import NodeManager
from ..config.types import Message, Role, ModelProvider, ChatConfig, StreamChunk, StreamStatus, StreamController
from ..storage.chat_storage import ChatStorage
from ..model.model_manager import ModelManager
from ..utils.logger import setup_logger
from ..config.config import cfg

logger = setup_logger('ChatManager')

class ChatManager:
    """延迟加载模型的聊天管理器"""
    
    def __init__(self, model_manager: ModelManager, storage: ChatStorage, config: ChatConfig):
        self.model_manager = model_manager
        self.storage = storage
        self.config = config
        self.current_conversation: Optional[Conversation] = None
        self._active_controllers: Dict[str, StreamController] = {}  # node_id -> controller
    
    def create_conversation(self, title: str = '') -> Conversation:
        """
        创建新对话（不实例化模型，只保存配置ID）
        """
        # 创建对话，只保存model_id字符串引用
        conversation = Conversation(title=title)
        
        # 初始化系统消息
        system_prompt = self.config.get("system_prompt")
        if system_prompt:
            conversation.initialize_with_system_message(system_prompt)
        
        self.current_conversation = conversation
        logger.info(f"对话创建成功 id: {conversation.metadata['id']}")
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
        assert target_provider is not None, "无法找到模型对应的提供商"
        provider = self.model_manager.get_model(target_provider)
        if not provider:
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
            response_content, tokens = provider.generate_response(
                messages=messages,
                model=target_model,
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
            self._update_token_stats(target_provider, tokens)
            
            # 保存对话
            if self.config.get("save_history", True):
                self.save_conversation()
            
            return response_content
            
        except Exception as e:
            logger.error(e)
            return ''
    
    async def send_message_stream(
        self,
        content: str,
        model_id: Optional[str] = None
    ) -> AsyncIterator[StreamChunk]:
        """
        异步流式发送消息
        前端可以：for chunk in stream: 实时更新UI
        """
        if not self.current_conversation:
            logger.error("没有加载的对话")
            yield StreamChunk(
                status=StreamStatus.ERROR,
                node_id=None,
                conversation_id=None,
                content="",
                error="没有加载的对话",
                tokens_used=0
            )
            return
        # 确定模型
        target_model = model_id or self.current_conversation.metadata.get("model")
        if not target_model:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                node_id=None,
                conversation_id=None,
                content="",
                error="未指定模型ID",
                tokens_used=0
            )
            return
        
        # 获取提供商
        target_provider = None
        for provider, models in self.model_manager.model_list.items():
            if target_model in models:
                target_provider = provider
                break
        
        if not target_provider:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=None,
                conversation_id=None,
                error=f"无法找到模型 {target_model} 对应的提供商",
                tokens_used=0
            )
            return
        
        provider = self.model_manager.get_model(target_provider, True)
        if not provider:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=None,
                conversation_id=None,
                error=f"无法初始化提供商 {target_provider}",
                tokens_used=0
            )
            return
        
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
        
        # 添加到对话树
        self.current_conversation.add_node(new_node, parent_id=current_node_id)
        
        # 创建流控制器
        controller = StreamController(
            node_id=new_node["id"],
            conversation_id=self.current_conversation.metadata["id"]
        )
        self._active_controllers[new_node["id"]] = controller
        
        # 准备消息链
        messages = self._prepare_messages_for_api()
        
        total_content = ""
        
        try:
            # 流式生成
            async for chunk in provider.generate_response_stream(
                model=target_model,
                messages=messages,
                stream_controller=controller
            ): # type: ignore
                if data := chunk.get("content"):
                    total_content += data
                yield chunk
            
            # 保存助手消息到节点
            assistant_msg = Message({
                "id": str(uuid.uuid4()),
                "role": Role.ASSISTANT,
                "content": total_content,
                "name": None,
                "tool_calls": None,
                "tool_call_id": None,
                "timestamp": datetime.now().isoformat()
            })
            NodeManager.add_assistant_message(new_node, assistant_msg)
            
            # 更新token统计
            self._update_token_stats(target_provider, chunk.get("tokens_used", 0)) # type: ignore
            
            # 保存对话
            if self.config.get("save_history", True):
                self.save_conversation()
            
        finally:
            # 清理控制器
            if new_node["id"] in self._active_controllers:
                del self._active_controllers[new_node["id"]]

    def stop_stream(self, node_id: str) -> bool:
        """终止指定节点的流式生成"""
        if node_id in self._active_controllers:
            print("找到活跃的流控制器，正在终止...")
            asyncio.create_task(self._active_controllers[node_id].stop())
            logger.info(f"已请求终止节点 {node_id} 的流")
            return True
        return False
    
    def stop_all_streams(self):
        """终止所有活跃流"""
        for node_id in list(self._active_controllers.keys()):
            self.stop_stream(node_id)

    def _prepare_messages_for_api(self) -> List[Message]:
        """准备API调用的消息列表"""
        if not self.current_conversation:
            return []
        
        messages = self.current_conversation.get_message_chain_from_node(self.current_conversation.current_node_id)

        msg_dict = []
        for msg in messages:
            msg_dict.append({
                "role": msg["role"],
                "content": msg["content"],
            })
        
        return msg_dict
    
    def _update_token_stats(self, provider: ModelProvider, tokens: int):
        """更新token统计"""
        assert self.current_conversation is not None
        if provider not in self.current_conversation.metadata["total_tokens"]:
            self.current_conversation.metadata["total_tokens"][provider] = 0
            
            # 粗略估算token数（实际应从API响应获取）
            self.current_conversation.metadata["total_tokens"][provider] += tokens
    
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
        