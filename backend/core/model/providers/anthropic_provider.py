# model/providers/anthropic_provider.py - Anthropic HTTP 提供商（纯标准库）
import asyncio
import json
import urllib.request
from typing import List, Dict, Any, Optional, AsyncIterator
from ..base import BaseProvider, logger
from ..usage import estimated_usage, usage_from_anthropic, usage_total
from ...config.types import Message, StreamChunk, StreamStatus, StreamController
from .sse import iter_decoded_sse_lines

_SENTINEL = object()  # 队列结束标记


class AnthropicProvider(BaseProvider):
    """Anthropic API 提供商 — 纯 HTTP 实现，不依赖 anthropic SDK"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

    def _api_base(self) -> str:
        base = self.config.get("base_url", "https://api.anthropic.com").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return base

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.config.get("api_key", ""),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _convert_messages(self, messages: List[Message]):
        system_text = ""
        anthropic_messages: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg["role"] if isinstance(msg["role"], str) else msg["role"].value
            content = msg.get("content") or ""
            if role == "system":
                system_text += content + "\n"
            elif role == "assistant" and msg.get("tool_calls"):
                blocks: List[Dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tool_call in msg.get("tool_calls") or []:
                    fn = tool_call.get("function") or {}
                    try:
                        tool_input = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        tool_input = {"arguments": fn.get("arguments") or ""}
                    blocks.append({
                        "type": "tool_use",
                        "id": tool_call.get("id") or fn.get("name") or "tool_call",
                        "name": fn.get("name", ""),
                        "input": tool_input,
                    })
                anthropic_messages.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                anthropic_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id") or msg.get("name") or "tool",
                        "content": content,
                    }],
                })
            else:
                anthropic_messages.append({"role": role, "content": content})
        merged: List[Dict[str, Any]] = []
        for m in anthropic_messages:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] = self._merge_content_blocks(merged[-1]["content"], m["content"])
            else:
                merged.append(dict(m))
        return system_text.strip() or None, merged

    def _merge_content_blocks(self, left: Any, right: Any) -> Any:
        left_blocks = left if isinstance(left, list) else [{"type": "text", "text": str(left)}]
        right_blocks = right if isinstance(right, list) else [{"type": "text", "text": str(right)}]
        return left_blocks + right_blocks

    def _build_body(self, model: str, messages: List[Dict[str, Any]],
                    system: Optional[str], max_tokens: int,
                    temperature: Optional[float], stream: bool,
                    reasoning_effort: Optional[str] = None,
                    thinking_enabled: Optional[bool] = None,
                    tools: Optional[List[Dict[str, Any]]] = None,
                    tool_choice: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "stream": stream,
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        # 思考模式：开启 → adaptive 且请求可见摘要（display=summarized）。
        # Opus 4.7/4.8 默认 display=omitted——思考照常进行但 thinking 字段为空，
        # 流里收不到 thinking_delta，UI 的“思考过程”块就空着。必须显式要 summarized
        # 才能拿到可见思考文本（Claude Code 正是这样请求的）。关闭 → disabled；
        # None → 不发送（用 API 默认）。
        if thinking_enabled is True:
            body["thinking"] = {"type": "adaptive", "display": "summarized"}
        elif thinking_enabled is False:
            body["thinking"] = {"type": "disabled"}
        # 推理强度：Anthropic 走 output_config.effort（low/medium/high/xhigh/max）。
        if reasoning_effort:
            body["output_config"] = {"effort": reasoning_effort}
        if tools:
            body["tools"] = [self._openai_tool_to_anthropic_tool(tool) for tool in tools]
            if tool_choice and tool_choice != "auto":
                body["tool_choice"] = {"type": "tool", "name": tool_choice}
        return body

    def _http_post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        url = self._api_base() + path
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())

    # ── 同步生成 ──
    def generate_response(self, model: str, messages: List[Message],
                          max_tokens: Optional[int] = None,
                          temperature: Optional[float] = None,
                          top_p: Optional[float] = None,
                          tools: Optional[List[Dict[str, Any]]] = None,
                          tool_choice: Optional[str] = None,
                          **kwargs) -> tuple[str, int]:
        system_text, api_messages = self._convert_messages(messages)
        body = self._build_body(
            model, api_messages, system_text, max_tokens or 4096, temperature,
            stream=False, tools=tools, tool_choice=tool_choice
        )
        result = self._http_post("/v1/messages", body)
        content = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")
        usage = result.get("usage", {})
        tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        return content, tokens

    # ── 流式生成（真流式：边读边输出） ──
    async def generate_response_stream(self, model: str, messages: List[Message],
                                       stream_controller: Optional[StreamController] = None,
                                       max_tokens: Optional[int] = None,
                                       temperature: Optional[float] = 0.7,
                                       tools: Optional[List[Dict[str, Any]]] = None,
                                       tool_choice: Optional[str] = None,
                                       reasoning_effort: Optional[str] = None,
                                       thinking_enabled: Optional[bool] = None,
                                       **kwargs) -> AsyncIterator[StreamChunk]:
        total_content = ""
        total_tokens = 0
        usage_info = None
        try:
            yield StreamChunk(
                status=StreamStatus.START, content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=None, tokens_used=0,
            )

            system_text, api_messages = self._convert_messages(messages)
            body = self._build_body(model, api_messages, system_text, max_tokens or 4096, temperature, stream=True,
                                    reasoning_effort=reasoning_effort, thinking_enabled=thinking_enabled,
                                    tools=tools, tool_choice=tool_choice)

            # 用 asyncio.Queue 实现真流式：线程读 HTTP → 放入队列 → 异步取出并 yield
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, self._stream_to_queue, body, queue, loop)
            tool_blocks: Dict[int, Dict[str, Any]] = {}

            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    raise item

                line = item
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")
                if usage := event.get("usage"):
                    usage_info = usage_from_anthropic(usage)
                    total_tokens = usage_total(usage_info, total_tokens)
                if message_usage := event.get("message", {}).get("usage"):
                    usage_info = usage_from_anthropic(message_usage)
                    total_tokens = usage_total(usage_info, total_tokens)
                if etype == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        index = int(event.get("index", len(tool_blocks)))
                        tool_blocks[index] = {
                            "id": block.get("id", f"toolu_{index}"),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)
                                if block.get("input") else "",
                            },
                        }
                    continue
                if etype == "content_block_delta":
                    delta = event.get("delta", {})
                    index = int(event.get("index", 0))
                    if delta.get("type") == "input_json_delta":
                        tool_call = tool_blocks.setdefault(
                            index,
                            {"id": f"toolu_{index}", "type": "function", "function": {"name": "", "arguments": ""}},
                        )
                        tool_call["function"]["arguments"] += delta.get("partial_json") or ""
                        continue
                    # 思考增量：Anthropic 的 thinking_delta（携带 thinking 文本）
                    if delta.get("type") == "thinking_delta" or "thinking" in delta:
                        thinking = delta.get("thinking", "")
                        if thinking:
                            if stream_controller and await stream_controller.is_stopped():
                                yield StreamChunk(
                                    status=StreamStatus.STOPPED, content=None,
                                    node_id=stream_controller.node_id,
                                    conversation_id=stream_controller.conversation_id,
                                    error="用户手动终止", tokens_used=total_tokens,
                                )
                                return
                            yield StreamChunk(
                                status=StreamStatus.CONTENT, content=None,
                                node_id=stream_controller.node_id if stream_controller else None,
                                conversation_id=stream_controller.conversation_id if stream_controller else None,
                                error=None, tokens_used=0,
                                event_type="reasoning", reasoning=thinking,
                            )
                        continue
                    text = delta.get("text", "")
                    if text:
                        total_content += text
                        token_delta = int(len(text.split()) * 1.3)
                        total_tokens += token_delta
                        if stream_controller and await stream_controller.is_stopped():
                            yield StreamChunk(
                                status=StreamStatus.STOPPED, content=None,
                                node_id=stream_controller.node_id,
                                conversation_id=stream_controller.conversation_id,
                                error="用户手动终止", tokens_used=total_tokens,
                            )
                            return
                        yield StreamChunk(
                            status=StreamStatus.CONTENT, content=text,
                            node_id=stream_controller.node_id if stream_controller else None,
                            conversation_id=stream_controller.conversation_id if stream_controller else None,
                            error=None, tokens_used=token_delta,
                        )
                elif etype == "content_block_stop":
                    index = int(event.get("index", 0))
                    tool_call = tool_blocks.get(index)
                    if tool_call and tool_call.get("function", {}).get("name"):
                        if not tool_call["function"]["arguments"]:
                            tool_call["function"]["arguments"] = "{}"
                        tool_calls = [tool_call]
                        yield StreamChunk(
                            status=StreamStatus.CONTENT, content=None,
                            node_id=stream_controller.node_id if stream_controller else None,
                            conversation_id=stream_controller.conversation_id if stream_controller else None,
                            error=None, tokens_used=0,
                            event_type="tool_call",
                            tool_call={"tool_calls": tool_calls},
                            tool_calls=tool_calls,
                        )
                elif etype == "message_delta":
                    usage = event.get("usage", {})
                    if usage.get("output_tokens"):
                        usage_info = usage_from_anthropic(usage)
                        total_tokens = usage_total(usage_info, total_tokens)

            if usage_info is None:
                usage_info = estimated_usage(total_tokens)
            yield StreamChunk(
                status=StreamStatus.COMPLETE, content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=None, tokens_used=total_tokens, usage_info=usage_info,
            )

        except asyncio.CancelledError:
            yield StreamChunk(
                status=StreamStatus.STOPPED, content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error="任务被取消", tokens_used=total_tokens,
            )
        except Exception as e:
            logger.error(f"Anthropic stream error: {e}")
            yield StreamChunk(
                status=StreamStatus.ERROR, content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=str(e), tokens_used=total_tokens,
            )

    def _openai_tool_to_anthropic_tool(self, tool: Dict[str, Any]) -> Dict[str, Any]:
        fn = tool.get("function") or {}
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        }

    def _stream_to_queue(self, body: Dict[str, Any], queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        """线程池中执行：逐行读取 SSE 并放入 asyncio Queue"""
        url = self._api_base() + "/v1/messages"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for line in iter_decoded_sse_lines(resp):
                    if line:
                        loop.call_soon_threadsafe(queue.put_nowait, line)
        except Exception as e:
            logger.error(f"Anthropic HTTP error: {type(e).__name__}: {e}")
            loop.call_soon_threadsafe(queue.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    # ── 模型列表 ──
    def list_models(self) -> List[str]:
        try:
            url = self._api_base() + "/v1/models"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self.config.get('api_key', '')}",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return [m["id"] for m in data.get("data", [])]
        except Exception as e:
            logger.warning(f"Bearer /v1/models 失败: {e}")

        try:
            url = self._api_base() + "/v1/models"
            req = urllib.request.Request(url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return [m["id"] for m in data.get("data", [])]
        except Exception as e:
            logger.warning(f"x-api-key /v1/models 失败: {e}")

        return []
