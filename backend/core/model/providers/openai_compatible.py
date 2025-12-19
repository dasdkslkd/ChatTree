# model/providers/openai_compatible.py - OpenAI兼容提供商
from typing import List, Dict, Any, Optional, AsyncIterator
import openai
from ..base import BaseProvider
from ...config.types import Message

class OpenAICompatibleProvider(BaseProvider):
    """兼容OpenAI格式的提供商"""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.client = self._create_client()
    
    def _create_client(self) -> openai.OpenAI:
        """创建OpenAI兼容客户端"""
        kwargs = {"api_key": self.config.get("api_key", "ollama")}
        
        if base_url := self.config.get("base_url"):
            kwargs["base_url"] = base_url
        
        return openai.OpenAI(timeout=5, **kwargs)
    
    def generate_response(
        self,
        model: str,
        messages: List[Message],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        **kwargs
    ) -> tuple[str, int]:
        """同步生成回复"""
        api_messages = self._convert_messages(messages)
        
        response = self.client.chat.completions.create(
            model=model,# type: ignore
            messages=api_messages,# type: ignore
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=False,
            **kwargs
        )
        
        content = response.choices[0].message.content or ""
        tokens = response.usage.total_tokens if response.usage else 0
        return content, tokens
    
    async def generate_response_stream(# type: ignore
        self,
        model: str,
        messages: List[Message],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        **kwargs
    ) -> AsyncIterator[str]:
        """流式生成回复"""
        api_messages = self._convert_messages(messages)
        
        stream = self.client.chat.completions.create(
            model=model,# type: ignore
            messages=api_messages,# type: ignore
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=True,
            **kwargs
        )
        
        for chunk in stream:
            if content := chunk.choices[0].delta.content:
                yield content
    
    def _convert_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """转换消息格式到OpenAI格式"""
        return [
            {
                "role": msg["role"],
                "content": msg["content"],
                "name": msg.get("name"),
                "tool_calls": msg.get("tool_calls"),
                "tool_call_id": msg.get("tool_call_id"),
            }
            for msg in messages
        ]
    
    # def get_model_info(self) -> Dict[str, Any]:
    #     """获取模型信息"""
    #     return {
    #         "provider": self.config.get("provider"),
    #         "model": self.model,
    #         "name": self.config.get("name", self.model),
    #         "type": "openai-compatible"
    #     }
    
    def list_models(self) -> List[str]:
        """获取可用模型列表"""
        try:
            models = self.client.models.list()
            return [model.id for model in models.data]
        except Exception as e:
            print(f"获取模型列表失败: {e}")
            return self.config.get("models", [])
    
    # def validate_config(self) -> bool:
    #     """验证配置"""
    #     return super().validate_config() and bool(self.config.get("api_key") or self.config.get("base_url"))