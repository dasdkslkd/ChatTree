# model/providers/gemini_provider.py - Gemini专用提供商
from typing import List, Dict, Any, Optional, AsyncIterator
import google.generativeai as genai
from ..base import BaseProvider
from ...config.types import Message

class GeminiProvider(BaseProvider):
    """Google Gemini专用提供商"""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        genai.configure(api_key=config["api_key"])
        self.client = genai.GenerativeModel(self.model)
    
    def generate_response(
        self,
        messages: List[Message],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        **kwargs
    ) -> tuple[str, int]:
        """生成回复"""
        system_prompt = ""
        recent_messages = []
        
        for msg in messages:
            if msg["role"] == "system":
                system_prompt += msg["content"] + "\n"
            else:
                recent_messages.append({
                    "role": "model" if msg["role"] == "assistant" else "user",
                    "parts": [msg["content"]]
                })
        
        if system_prompt:
            # Gemini 1.5+ supports system prompt in generation config
            kwargs["system_instruction"] = system_prompt
        
        generation_config = genai.types.GenerationConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        )
        
        chat = self.client.start_chat(history=recent_messages[:-1])
        response = chat.send_message(
            recent_messages[-1]["parts"][0],
            generation_config=generation_config
        )
        
        # Gemini doesn't provide token usage in response, return 0
        return response.text, 0
    
    async def generate_response_stream(
        self,
        messages: List[Message],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        **kwargs
    ) -> AsyncIterator[str]:
        """流式生成回复"""
        # 实际异步实现
        import asyncio
        loop = asyncio.get_event_loop()
        
        # 使用线程池执行同步调用
        response = await loop.run_in_executor(
            None,
            lambda: self.generate_response(messages, max_tokens, temperature, top_p, **kwargs)
        )
        yield response[0]
    
    # def get_model_info(self) -> Dict[str, Any]:
    #     """获取模型信息"""
    #     return {
    #         "provider": "gemini",
    #         "model": self.model,
    #         "name": self.config.get("name", self.model),
    #         "type": "gemini"
    #     }
    
    def list_models(self) -> List[str]:
        """获取可用模型列表"""
        try:
            models = genai.list_models()
            return [model.name.split('/')[-1] for model in models if 'generateContent' in model.supported_generation_methods]
        except Exception as e:
            print(f"获取Gemini模型列表失败: {e}")
            raise RuntimeError(f"获取Gemini模型列表失败: {e}")
    
    def validate_config(self) -> bool:
        """验证配置"""
        return super().validate_config() and bool(self.config.get("api_key"))