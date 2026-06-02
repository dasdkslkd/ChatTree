# model/providers/openai_compatible.py - OpenAI兼容提供商
from typing import List, Dict, Any, Optional, AsyncIterator, Tuple
import openai
import asyncio
from ..base import BaseProvider, logger
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
        
        logger.info(f"Creating async OpenAI client with kwargs: {kwargs}")
        return openai.AsyncOpenAI(timeout=5, **kwargs)
    
    def _create_client(self) -> openai.OpenAI:
        """创建OpenAI兼容客户端"""
        kwargs = {"api_key": self.config.get("api_key", "ollama")}

        if base_url := self.config.get("base_url"):
            kwargs["base_url"] = base_url
        logger.info(f"Creating OpenAI client with kwargs: {kwargs}")
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
        if self._use_responses_api():
            return self._generate_response_with_responses_api(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                **kwargs,
            )

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

            if self._use_responses_api():
                instructions, response_input = self._convert_messages_to_responses_input(messages)
                request_kwargs = self._build_responses_request_kwargs(
                    instructions=instructions,
                    response_input=response_input,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=None,
                    extra_kwargs=kwargs,
                )

                request_attempts = [request_kwargs]
                if self._should_retry_responses_without_temperature(request_kwargs):
                    retry_kwargs = dict(request_kwargs)
                    retry_kwargs.pop("temperature", None)
                    request_attempts.append(retry_kwargs)

                response = None
                for attempt_index, current_kwargs in enumerate(request_attempts):
                    try:
                        async with self.client.responses.stream(model=model, **current_kwargs) as stream:  # type: ignore[attr-defined]
                            async for event in stream:
                                if stream_controller and await stream_controller.is_stopped():
                                    yield StreamChunk(
                                        status=StreamStatus.STOPPED,
                                        content=None,
                                        node_id=stream_controller.node_id,
                                        conversation_id=stream_controller.conversation_id,
                                        error="用户手动终止",
                                        tokens_used=total_tokens
                                    )
                                    logger.warning(f"Stream stopped by user: {stream_controller.conversation_id} - {stream_controller.node_id}")
                                    return

                                if event.type != "response.output_text.delta" or not event.delta:
                                    continue

                                content = event.delta
                                total_content += content
                                token_delta = int(len(content.split()) * 1.3)
                                total_tokens += token_delta

                                yield StreamChunk(
                                    status=StreamStatus.CONTENT,
                                    content=content,
                                    node_id=stream_controller.node_id if stream_controller else None,
                                    conversation_id=stream_controller.conversation_id if stream_controller else None,
                                    error=None,
                                    tokens_used=token_delta,
                                )

                            response = await stream.get_final_response()
                        break
                    except openai.BadRequestError as exc:
                        if (
                            attempt_index + 1 < len(request_attempts)
                            and not total_content
                            and "temperature" in current_kwargs
                        ):
                            logger.warning(f"Responses stream rejected temperature, retrying without it: {exc}")
                            continue
                        raise

                if response is not None and response.usage:
                    total_tokens = response.usage.total_tokens
            else:
                api_messages = [
                    {"role": msg["role"], "content": msg["content"]}
                    for msg in messages
                ]

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
                            content=None,
                            node_id=stream_controller.node_id,
                            conversation_id=stream_controller.conversation_id,
                            error="用户手动终止",
                            tokens_used=total_tokens
                        )
                        logger.warning(f"Stream stopped by user: {stream_controller.conversation_id} - {stream_controller.node_id}")
                        return
                    assert stream_controller is not None, "stream_controller不能为空"
                    if chunk.choices and chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        total_content += content

                        token_delta = int(len(content.split()) * 1.3)
                        total_tokens += token_delta

                        yield StreamChunk(
                            status=StreamStatus.CONTENT,
                            content=content,
                            node_id=stream_controller.node_id,
                            conversation_id=stream_controller.conversation_id,
                            error=None,
                            tokens_used=token_delta
                        )
            
            # 完成
            assert stream_controller is not None, "stream_controller不能为空"
            yield StreamChunk(
                status=StreamStatus.COMPLETE,
                content=None,  # 完成时不再发送内容，避免重复
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
                content=None,  # 取消时不再发送内容，避免重复
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error="任务被取消",
                tokens_used=total_tokens
            )
            logger.warning(f"Stream cancelled: {stream_controller.conversation_id} - {stream_controller.node_id}")
        except Exception as e:
            assert stream_controller is not None, "stream_controller不能为空"
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content=None,  # 错误时不再发送内容，避免重复
                node_id=stream_controller.node_id,
                conversation_id=stream_controller.conversation_id,
                error=str(e),
                tokens_used=total_tokens
            )
            logger.error(f"Stream error: {e} - Conversation: {stream_controller.conversation_id} - Node: {stream_controller.node_id}")

    def _use_responses_api(self) -> bool:
        return self.config.get("name") == "openai"

    def _generate_response_with_responses_api(
        self,
        model: str,
        messages: List[Message],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        **kwargs
    ) -> tuple[str, int]:
        instructions, response_input = self._convert_messages_to_responses_input(messages)
        request_kwargs = self._build_responses_request_kwargs(
            instructions=instructions,
            response_input=response_input,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            extra_kwargs=kwargs,
        )

        response = self.client.responses.create(model=model, **request_kwargs)
        content = self._extract_responses_text(response)
        tokens = response.usage.total_tokens if response.usage else 0
        return content, tokens

    def _build_responses_request_kwargs(
        self,
        instructions: Optional[str],
        response_input: List[Dict[str, Any]],
        max_tokens: Optional[int],
        temperature: Optional[float],
        top_p: Optional[float],
        extra_kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        request_kwargs: Dict[str, Any] = {
            "input": response_input,
            **extra_kwargs,
        }

        if instructions:
            request_kwargs["instructions"] = instructions
        if max_tokens is not None:
            request_kwargs["max_output_tokens"] = max_tokens
        if temperature is not None:
            request_kwargs["temperature"] = temperature
        if top_p is not None:
            request_kwargs["top_p"] = top_p

        return request_kwargs

    def _should_retry_responses_without_temperature(self, request_kwargs: Dict[str, Any]) -> bool:
        return bool(self.config.get("base_url") and "temperature" in request_kwargs)

    def _convert_messages_to_responses_input(self, messages: List[Message]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        response_input: List[Dict[str, Any]] = []

        for msg in messages:
            role = str(msg["role"])
            content = msg.get("content") or ""

            if role == "system":
                role = "developer"

            if role == "tool":
                tool_name = msg.get("name") or msg.get("tool_call_id") or "tool"
                role = "assistant"
                content = f"[{tool_name}]\n{content}"

            if role not in {"user", "assistant", "developer"}:
                role = "user"

            response_input.append({
                "type": "message",
                "role": role,
                "content": content,
            })

        return None, response_input

    def _extract_responses_text(self, response: Any) -> str:
        if output_text := getattr(response, "output_text", None):
            return output_text

        text_parts: List[str] = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) != "message":
                continue
            for content in getattr(item, "content", []) or []:
                if getattr(content, "type", None) != "output_text":
                    continue
                if text := getattr(content, "text", None):
                    text_parts.append(text)

        return "".join(text_parts)
    
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
            logger.error(f"获取模型列表失败: {e}")
            raise RuntimeError(f"获取模型列表失败: {e}")
    
    # def validate_config(self) -> bool:
    #     """验证配置"""
    #     return super().validate_config() and bool(self.config.get("api_key") or self.config.get("base_url"))