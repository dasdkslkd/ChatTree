# model/providers/gemini_provider.py - Gemini 专用提供商
import asyncio
from typing import List, Dict, Any, Optional, AsyncIterator
import google.generativeai as genai
from ..base import BaseProvider, logger
from ...config.types import Message, StreamChunk, StreamStatus, StreamController


class GeminiProvider(BaseProvider):
    """Google Gemini 专用提供商"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        genai.configure(api_key=config.get("api_key", ""))
        # Gemini SDK 需要在调用时指定模型，不在初始化时绑定
        self._genai = genai

    def _convert_messages(self, messages: List[Message]):
        """提取 system prompt 并转换消息为 Gemini 格式"""
        system_prompt = ""
        gemini_messages: List[Dict[str, Any]] = []

        for msg in messages:
            role = str(msg["role"])
            content = msg.get("content") or ""
            if role == "system":
                system_prompt += content + "\n"
            else:
                gemini_messages.append({
                    "role": "model" if role == "assistant" else "user",
                    "parts": [content],
                })

        return system_prompt.strip() or None, gemini_messages

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
        system_prompt, gemini_messages = self._convert_messages(messages)

        generation_config = genai.types.GenerationConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        )

        genai_model = self._genai.GenerativeModel(
            model_name=model,
            system_instruction=system_prompt,
        )

        chat = genai_model.start_chat(history=gemini_messages[:-1] if gemini_messages else [])
        last_msg = gemini_messages[-1]["parts"][0] if gemini_messages else ""
        response = chat.send_message(last_msg, generation_config=generation_config)

        return response.text, 0

    async def generate_response_stream(
        self,
        model: str,
        messages: List[Message],
        stream_controller: Optional[StreamController] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = 0.7,
        **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """流式生成回复"""
        total_content = ""
        total_tokens = 0

        try:
            yield StreamChunk(
                status=StreamStatus.START,
                content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=None,
                tokens_used=0,
            )

            system_prompt, gemini_messages = self._convert_messages(messages)

            generation_config = genai.types.GenerationConfig(
                max_output_tokens=max_tokens,
                temperature=temperature,
            )

            genai_model = self._genai.GenerativeModel(
                model_name=model,
                system_instruction=system_prompt,
            )

            chat = genai_model.start_chat(history=gemini_messages[:-1] if gemini_messages else [])
            last_msg = gemini_messages[-1]["parts"][0] if gemini_messages else ""

            # Gemini SDK 的 stream=True
            loop = asyncio.get_event_loop()
            response_iter = await loop.run_in_executor(
                None,
                lambda: chat.send_message(last_msg, generation_config=generation_config, stream=True)
            )

            for chunk in response_iter:
                if stream_controller and await stream_controller.is_stopped():
                    yield StreamChunk(
                        status=StreamStatus.STOPPED,
                        content=None,
                        node_id=stream_controller.node_id,
                        conversation_id=stream_controller.conversation_id,
                        error="用户手动终止",
                        tokens_used=total_tokens,
                    )
                    return

                if not chunk.text:
                    continue

                total_content += chunk.text
                token_delta = int(len(chunk.text.split()) * 1.3)
                total_tokens += token_delta

                yield StreamChunk(
                    status=StreamStatus.CONTENT,
                    content=chunk.text,
                    node_id=stream_controller.node_id if stream_controller else None,
                    conversation_id=stream_controller.conversation_id if stream_controller else None,
                    error=None,
                    tokens_used=token_delta,
                )

            assert stream_controller is not None
            yield StreamChunk(
                status=StreamStatus.COMPLETE,
                content=None,
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=total_tokens,
            )

        except asyncio.CancelledError:
            assert stream_controller is not None
            yield StreamChunk(
                status=StreamStatus.STOPPED,
                content=None,
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error="任务被取消",
                tokens_used=total_tokens,
            )
        except Exception as e:
            assert stream_controller is not None
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content=None,
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=str(e),
                tokens_used=total_tokens,
            )
            logger.error(f"Gemini stream error: {e}")

    def list_models(self) -> List[str]:
        """获取可用模型列表"""
        try:
            models = genai.list_models()
            return [
                model.name.split('/')[-1]
                for model in models
                if 'generateContent' in model.supported_generation_methods
            ]
        except Exception as e:
            logger.error(f"获取 Gemini 模型列表失败: {e}")
            raise RuntimeError(f"获取 Gemini 模型列表失败: {e}")
