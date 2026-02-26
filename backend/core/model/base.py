# model/base.py - 基础提供商接口
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, AsyncIterator
from ..config.types import Message, StreamChunk, StreamController
from ..utils.logger import setup_logger

logger = setup_logger('Provider')

class BaseProvider(ABC):
    """基础模型提供商接口"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
    
    @abstractmethod
    def generate_response(
        self,
        model: str,
        messages: List[Message],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        **kwargs
    ) -> tuple[str, int]:
        """同步生成回复，返回(内容, token使用量)"""
        pass
    
    @abstractmethod
    async def generate_response_stream(
        self,
        model: str,
        messages: List[Message],
        stream_controller: Optional['StreamController'] = None,  # 前向引用
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """异步流式生成回复"""
        pass
    
    # @abstractmethod
    # def get_model_info(self) -> Dict[str, Any]:
    #     """获取模型信息"""
    #     pass
    
    @abstractmethod
    def list_models(self) -> List[str]:
        """获取可用模型列表"""
        pass
    
    # def validate_config(self) -> bool:
    #     """验证配置是否完整"""
    #     return bool(self.model)