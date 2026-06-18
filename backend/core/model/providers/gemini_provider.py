# model/providers/gemini_provider.py - Gemini 专用提供商
import asyncio
from typing import List, Dict, Any, Optional, AsyncIterator
import google.generativeai as genai
from ..base import BaseProvider, logger
from ..usage import estimated_usage, usage_from_gemini, usage_total
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

    # 抽象推理档位 -> Gemini thinking_budget 整数预算。
    # dynamic=-1 让模型自动决定；thinking_enabled False 时强制 0（关闭思考）。
    _EFFORT_BUDGET = {
        "dynamic": -1,
        "low": 1024,
        "medium": 8192,
        "high": 24576,
    }

    def _build_thinking_config(self, reasoning_effort, thinking_enabled):
        """构造 Gemini ThinkingConfig；SDK 版本不支持时返回 None（守护降级）。"""
        # 关闭思考优先：thinking_enabled False -> budget 0。
        if thinking_enabled is False:
            budget = 0
        elif reasoning_effort:
            budget = self._EFFORT_BUDGET.get(reasoning_effort)
        elif thinking_enabled is True:
            budget = -1  # 开启但未指定档位 -> 动态预算
        else:
            return None
        if budget is None:
            return None
        try:
            return genai.types.ThinkingConfig(thinking_budget=budget)
        except Exception as e:  # SDK 版本无 ThinkingConfig / 参数名不符
            logger.warning(f"Gemini ThinkingConfig 不可用，忽略推理参数: {e}")
            return None

    def generate_response(
        self,
        model: str,
        messages: List[Message],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
        **kwargs
    ) -> tuple[str, int]:
        """同步生成回复"""
        system_prompt, gemini_messages = self._convert_messages(messages)

        gen_kwargs: Dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
        }
        thinking_config = self._build_thinking_config(reasoning_effort, thinking_enabled)
        if thinking_config is not None:
            gen_kwargs["thinking_config"] = thinking_config
        generation_config = genai.types.GenerationConfig(**gen_kwargs)

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
        reasoning_effort: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
        **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """流式生成回复"""
        total_content = ""
        total_tokens = 0
        usage_info = None

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

            gen_kwargs: Dict[str, Any] = {
                "max_output_tokens": max_tokens,
                "temperature": temperature,
            }
            thinking_config = self._build_thinking_config(reasoning_effort, thinking_enabled)
            if thinking_config is not None:
                gen_kwargs["thinking_config"] = thinking_config
            generation_config = genai.types.GenerationConfig(**gen_kwargs)

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
                if getattr(chunk, "usage_metadata", None):
                    usage_info = usage_from_gemini(chunk.usage_metadata)
                    total_tokens = usage_total(usage_info, total_tokens)
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

            if getattr(response_iter, "usage_metadata", None):
                usage_info = usage_from_gemini(response_iter.usage_metadata)
                total_tokens = usage_total(usage_info, total_tokens)
            if usage_info is None:
                usage_info = estimated_usage(total_tokens)
            assert stream_controller is not None
            yield StreamChunk(
                status=StreamStatus.COMPLETE,
                content=None,
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=None,
                tokens_used=total_tokens,
                usage_info=usage_info,
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
