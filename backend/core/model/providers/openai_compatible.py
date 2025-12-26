# model/providers/openai_compatible.py - OpenAI兼容提供商
from typing import List, Dict, Any, Optional, AsyncIterator
import openai
import asyncio
from ..base import BaseProvider
from ...config.types import Message, StreamChunk, StreamStatus, StreamController

class OpenAICompatibleProvider(BaseProvider):
    """兼容OpenAI格式的提供商"""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        if self.config.get("is_async", False):
            self.client = self._create_client_async()
        else:
            self.client = self._create_client()
    
    def _create_client_async(self) -> openai.AsyncOpenAI:
        """创建OpenAI兼容客户端"""
        kwargs = {"api_key": self.config.get("api_key", "ollama")}
        
        if base_url := self.config.get("base_url"):
            kwargs["base_url"] = base_url
        
        return openai.AsyncOpenAI(timeout=5, **kwargs)
    
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
    
    async def generate_response_stream( # type: ignore
        self,
        model: str,
        messages: List[Message],
        stream_controller: Optional[StreamController] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = 0.7,
        **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """流式生成实现"""
        # import pdb; pdb.set_trace()
        # 准备消息格式
        api_messages = [
            {"role": msg["role"], "content": msg["content"]}
            for msg in messages
        ]
        
        total_content = ""
        total_tokens: int = 0
        
        try:
            # 发送初始状态
            yield StreamChunk(
                status=StreamStatus.START,
                content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=None,
                tokens_used=0
            )
            
            # 调用API
            stream = await self.client.chat.completions.create(
                model=model,
                messages=api_messages, # type: ignore
                stream=True,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs
            ) # type: ignore
            
            async for chunk in stream:
                # 检查是否被终止
                if stream_controller and await stream_controller.is_stopped():
                    yield StreamChunk(
                        status=StreamStatus.STOPPED,
                        content=total_content,
                        node_id=stream_controller.node_id,
                        conversation_id=stream_controller.conversation_id,
                        error="用户手动终止",
                        tokens_used=total_tokens
                    )
                    return
                assert stream_controller is not None, "stream_controller不能为空"
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    total_content += content
                    
                    # 估算token（实际应从API获取）
                    total_tokens += int(len(content.split()) * 1.3)
                    
                    yield StreamChunk(
                        status=StreamStatus.CONTENT,
                        content=content,
                        node_id=stream_controller.node_id,
                        conversation_id=stream_controller.conversation_id,
                        error=None,
                        tokens_used=int(len(content.split()) * 1.3)
                    )
            
            # 完成
            assert stream_controller is not None, "stream_controller不能为空"
            yield StreamChunk(
                status=StreamStatus.COMPLETE,
                content=total_content,
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=total_tokens
            )
            
        except asyncio.CancelledError:
            # 任务被取消
            assert stream_controller is not None, "stream_controller不能为空"
            yield StreamChunk(
                status=StreamStatus.STOPPED,
                content=total_content,
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error="任务被取消",
                tokens_used=total_tokens
            )
        except Exception as e:
            assert stream_controller is not None, "stream_controller不能为空"
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content=total_content,
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=str(e),
                tokens_used=total_tokens
            )
    
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
            tmp_client = self._create_client()
            models = tmp_client.models.list()
            return [model.id for model in models.data]
        except Exception as e:
            print(f"获取模型列表失败: {e}")
            return self.config.get("models", [])
    
    # def validate_config(self) -> bool:
    #     """验证配置"""
    #     return super().validate_config() and bool(self.config.get("api_key") or self.config.get("base_url"))