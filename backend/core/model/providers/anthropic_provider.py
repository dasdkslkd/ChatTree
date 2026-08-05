# model/providers/anthropic_provider.py - Anthropic HTTP 提供商（纯标准库）
import asyncio
import json
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from typing import List, Dict, Any, Optional, AsyncIterator
from ..base import BaseProvider, logger
from ..usage import estimated_usage, usage_from_anthropic, usage_total
from ...config.types import Message, ModelRoute, StreamChunk, StreamStatus, StreamController
from .retry import RetryPolicy, RetryableHTTPError, classify_retry_error, run_with_retries, sleep_before_retry
from .sse import StreamStopped, iter_sse_lines
from .multimodal import to_anthropic_content


class AnthropicHTTPError(RetryableHTTPError):
    pass


class AnthropicProvider(BaseProvider):
    """Anthropic API 提供商 — 纯 HTTP 实现，不依赖 anthropic SDK"""

    def __init__(self, config: Dict[str, Any], route: ModelRoute):
        super().__init__(config, route)

    def _api_base(self) -> str:
        base = self.config.get("base_url", "https://api.anthropic.com").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return base

    async def _iter_sse_events(
        self,
        body: Dict[str, Any],
        *,
        stream_controller: Optional[StreamController] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        async for line in iter_sse_lines(
            self._api_base() + self.route["endpoint"],
            body,
            self._headers(),
            self.config,
            AnthropicHTTPError,
            stream_controller,
        ):
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                logger.warning(f"Invalid Anthropic SSE payload ignored: {payload[:200]}")

    def _headers(self) -> Dict[str, str]:
        # Claude 订阅（CLI 凭据复用）走 Bearer + anthropic-beta
        auth = self.config.get("auth") or {}
        if auth.get("subscription") == "claude":
            from ...auth import get_valid_token_sync
            token, _ = get_valid_token_sync(auth)
            return {
                "Authorization": f"Bearer {token}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20",
                "content-type": "application/json",
                "User-Agent": self.config.get("custom_user_agent") or "ChatTree",
            }
        return {
            "x-api-key": self.config.get("api_key", ""),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "User-Agent": self.config.get("custom_user_agent") or "ChatTree",
        }

    def _convert_messages(self, messages: List[Message]):
        system_text = ""
        anthropic_messages: List[Dict[str, Any]] = []
        for msg in messages:
            native_items = [
                item
                for item in (msg.get("model_state_items") or [])
                if item.get("route_id") == self.route["route_id"]
                and item.get("kind") == "assistant_message"
                and isinstance(item.get("native_payload"), dict)
            ]
            if native_items:
                layout = (native_items[0]["native_payload"]).get("layout") or []
                tool_calls = {
                    str(call.get("id") or ""): call
                    for call in (msg.get("tool_calls") or [])
                }
                blocks: List[Dict[str, Any]] = []
                text_emitted = False
                for entry in layout:
                    if not isinstance(entry, dict):
                        continue
                    if isinstance(entry.get("state"), dict):
                        blocks.append(deepcopy(entry["state"]))
                    elif "text" in entry and not text_emitted and msg.get("content"):
                        blocks.append({"type": "text", "text": msg.get("content") or ""})
                        text_emitted = True
                    elif entry.get("tool_call_id") in tool_calls:
                        call = tool_calls[str(entry["tool_call_id"])]
                        fn = call.get("function") or {}
                        try:
                            tool_input = json.loads(fn.get("arguments") or "{}")
                        except json.JSONDecodeError:
                            tool_input = {"arguments": fn.get("arguments") or ""}
                        blocks.append({
                            "type": "tool_use",
                            "id": call.get("id"),
                            "name": fn.get("name", ""),
                            "input": tool_input,
                        })
                if msg.get("content") and not text_emitted:
                    blocks.append({"type": "text", "text": msg.get("content") or ""})
                anthropic_messages.append({"role": "assistant", "content": blocks})
                continue
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
                anthropic_messages.append({"role": role, "content": to_anthropic_content(content)})
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
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise AnthropicHTTPError(exc.code, error_body, dict(exc.headers or {})) from exc

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
        policy = RetryPolicy.from_config(self.config)
        result = run_with_retries(
            lambda: self._http_post(self.route["endpoint"], body),
            policy,
            label="Anthropic messages request",
            logger=logger,
        )
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
        finish_reason: Optional[str] = None
        try:
            system_text, api_messages = self._convert_messages(messages)
            body = self._build_body(model, api_messages, system_text, max_tokens or 4096, temperature, stream=True,
                                    reasoning_effort=reasoning_effort, thinking_enabled=thinking_enabled,
                                    tools=tools, tool_choice=tool_choice)

            tool_blocks: Dict[int, Dict[str, Any]] = {}
            native_blocks: Dict[int, Dict[str, Any]] = {}
            tool_call_started = False
            stream_policy = RetryPolicy.from_config(self.config)
            retry_failures = 0

            while True:
                attempt_had_output = False
                native_blocks = {}
                try:
                    async for event in self._iter_sse_events(
                        body,
                        stream_controller=stream_controller,
                    ):
                        etype = event.get("type", "")
                        if usage := event.get("usage"):
                            usage_info = usage_from_anthropic(usage)
                            total_tokens = usage_total(usage_info, total_tokens)
                        if message_usage := event.get("message", {}).get("usage"):
                            usage_info = usage_from_anthropic(message_usage)
                            total_tokens = usage_total(usage_info, total_tokens)
                        if etype == "content_block_start":
                            block = event.get("content_block") or {}
                            index = int(event.get("index", len(native_blocks)))
                            if isinstance(block, dict):
                                native_blocks[index] = deepcopy(block)
                            if block.get("type") == "tool_use":
                                if not tool_call_started:
                                    tool_call_started = True
                                    attempt_had_output = True
                                    yield StreamChunk(
                                        status=StreamStatus.CONTENT, content=None,
                                        node_id=stream_controller.node_id if stream_controller else None,
                                        conversation_id=stream_controller.conversation_id if stream_controller else None,
                                        error=None, tokens_used=0,
                                        event_type="tool_call_start",
                                    )
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
                                partial_json = delta.get("partial_json") or ""
                                tool_call["function"]["arguments"] += partial_json
                                native_block = native_blocks.setdefault(
                                    index,
                                    {"type": "tool_use", "id": tool_call["id"], "name": tool_call["function"]["name"]},
                                )
                                native_block["_partial_json"] = (
                                    str(native_block.get("_partial_json") or "") + partial_json
                                )
                                continue
                            # 思考增量：Anthropic 的 thinking_delta（携带 thinking 文本）
                            if delta.get("type") == "thinking_delta" or "thinking" in delta:
                                thinking = delta.get("thinking", "")
                                if thinking:
                                    native_block = native_blocks.setdefault(index, {"type": "thinking"})
                                    native_block["thinking"] = (
                                        str(native_block.get("thinking") or "") + thinking
                                    )
                                    if stream_controller and await stream_controller.is_stopped():
                                        yield StreamChunk(
                                            status=StreamStatus.STOPPED, content=None,
                                            node_id=stream_controller.node_id,
                                            conversation_id=stream_controller.conversation_id,
                                            error="用户手动终止", tokens_used=total_tokens,
                                            usage_info=usage_info,
                                        )
                                        return
                                    attempt_had_output = True
                                    yield StreamChunk(
                                        status=StreamStatus.CONTENT, content=None,
                                        node_id=stream_controller.node_id if stream_controller else None,
                                        conversation_id=stream_controller.conversation_id if stream_controller else None,
                                        error=None, tokens_used=0,
                                        event_type="reasoning", reasoning=thinking,
                                    )
                                continue
                            if delta.get("type") == "signature_delta" or "signature" in delta:
                                signature = str(delta.get("signature") or "")
                                native_block = native_blocks.setdefault(index, {"type": "thinking"})
                                native_block["signature"] = (
                                    str(native_block.get("signature") or "") + signature
                                )
                                continue
                            text = delta.get("text", "")
                            if text:
                                native_block = native_blocks.setdefault(index, {"type": "text"})
                                native_block["text"] = str(native_block.get("text") or "") + text
                                attempt_had_output = True
                                total_content += text
                                token_delta = int(len(text.split()) * 1.3)
                                total_tokens += token_delta
                                if stream_controller and await stream_controller.is_stopped():
                                    yield StreamChunk(
                                        status=StreamStatus.STOPPED, content=None,
                                        node_id=stream_controller.node_id,
                                        conversation_id=stream_controller.conversation_id,
                                        error="用户手动终止", tokens_used=total_tokens,
                                        usage_info=usage_info,
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
                            native_block = native_blocks.get(index)
                            if native_block and "_partial_json" in native_block:
                                partial_json = str(native_block.pop("_partial_json") or "")
                                try:
                                    native_block["input"] = json.loads(partial_json or "{}")
                                except json.JSONDecodeError:
                                    native_block["input"] = {"arguments": partial_json}
                            tool_call = tool_blocks.get(index)
                            if tool_call and tool_call.get("function", {}).get("name"):
                                if not tool_call["function"]["arguments"]:
                                    tool_call["function"]["arguments"] = "{}"
                                tool_calls = [
                                    dict(call)
                                    for _, call in sorted(tool_blocks.items(), key=lambda item: item[0])
                                    if call.get("function", {}).get("name")
                                ]
                                attempt_had_output = True
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
                            observed_finish_reason = (event.get("delta") or {}).get("stop_reason")
                            if observed_finish_reason:
                                raw_finish_reason = str(observed_finish_reason).strip().lower()
                                finish_reason = {
                                    "end_turn": "stop",
                                    "stop_sequence": "stop",
                                    "tool_use": "tool_calls",
                                    "max_tokens": "length",
                                }.get(raw_finish_reason, raw_finish_reason)
                            usage = event.get("usage", {})
                            if usage.get("output_tokens"):
                                usage_info = usage_from_anthropic(usage)
                                total_tokens = usage_total(usage_info, total_tokens)
                    break
                except StreamStopped:
                    yield StreamChunk(
                        status=StreamStatus.STOPPED, content=None,
                        node_id=stream_controller.node_id if stream_controller else None,
                        conversation_id=stream_controller.conversation_id if stream_controller else None,
                        error="用户手动终止", tokens_used=total_tokens,
                        usage_info=usage_info,
                    )
                    return
                except Exception as exc:
                    if stream_controller and await stream_controller.is_stopped():
                        yield StreamChunk(
                            status=StreamStatus.STOPPED, content=None,
                            node_id=stream_controller.node_id,
                            conversation_id=stream_controller.conversation_id,
                            error="用户手动终止", tokens_used=total_tokens,
                            usage_info=usage_info,
                        )
                        return
                    decision = classify_retry_error(exc, stream_policy)
                    if (
                        attempt_had_output
                        or not decision.retryable
                        or retry_failures >= stream_policy.max_retries(stream=True)
                    ):
                        raise
                    retry_failures += 1
                    delay = await sleep_before_retry(retry_failures, stream_policy, decision)
                    logger.warning(
                        f"Anthropic stream failed before output; retrying "
                        f"{retry_failures}/{stream_policy.max_retries(stream=True)} "
                        f"in {delay:.2f}s: {exc}"
                    )

            if usage_info is None:
                usage_info = estimated_usage(total_tokens)
            if tool_blocks and finish_reason in {None, "stop"}:
                finish_reason = "tool_calls"
            ordered_blocks = [
                block
                for _, block in sorted(native_blocks.items())
                if isinstance(block, dict)
            ]
            state_layout = []
            has_state = False
            for block in ordered_blocks:
                block_type = str(block.get("type") or "")
                if block_type in {"thinking", "redacted_thinking"}:
                    state_layout.append({"state": block})
                    has_state = True
                elif block_type == "text":
                    state_layout.append({"text": True})
                elif block_type == "tool_use":
                    state_layout.append({"tool_call_id": str(block.get("id") or "")})
                else:
                    state_layout.append({"state": block})
                    has_state = True
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=None,
                tokens_used=0,
                event_type="model_output_item",
                output_item={
                    "id": str(uuid.uuid4()),
                    "route_id": self.route["route_id"],
                    "index": 0,
                    "round_index": 0,
                    "kind": "assistant_message",
                    "display_text": total_content,
                    "tool_call_ids": [
                        str(block.get("id"))
                        for block in ordered_blocks
                        if block.get("type") == "tool_use" and block.get("id")
                    ],
                    "native_payload": {
                        "role": "assistant",
                        "content": ordered_blocks,
                    },
                    **(
                        {
                            "state_payload": {
                                "role": "assistant",
                                "layout": state_layout,
                            }
                        }
                        if has_state
                        else {}
                    ),
                },
            )
            yield StreamChunk(
                status=StreamStatus.COMPLETE, content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=None, tokens_used=total_tokens, usage_info=usage_info,
                metadata={"finish_reason": finish_reason or "unknown"},
            )

        except asyncio.CancelledError:
            yield StreamChunk(
                status=StreamStatus.STOPPED, content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error="任务被取消", tokens_used=total_tokens,
                usage_info=usage_info,
            )
        except Exception as e:
            logger.error(f"Anthropic stream error: {e}")
            yield StreamChunk(
                status=StreamStatus.ERROR, content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=str(e) or e.__class__.__name__, tokens_used=total_tokens,
                usage_info=usage_info,
            )

    def _openai_tool_to_anthropic_tool(self, tool: Dict[str, Any]) -> Dict[str, Any]:
        fn = tool.get("function") or {}
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        }

    # ── 模型列表 ──
    def list_models(self) -> List[str]:
        # 候选 URL 列表覆盖 Anthropic 兼容子路径（/api/anthropic、/claudecode 等）：
        # 先按 Bearer 尝试所有候选，再按 x-api-key 兜底。
        from .model_fetch import fetch_models
        api_key = self.config.get("api_key", "")
        kwargs = {
            "base_url": self._api_base(),
            "api_key": api_key,
            "models_url_override": self.config.get("models_url_override"),
            "custom_user_agent": self.config.get("custom_user_agent"),
        }
        try:
            models = fetch_models(**kwargs)
            return [m["id"] for m in models]
        except Exception as e:
            logger.warning(f"Bearer 候选列表失败: {e}")

        # x-api-key 兜底：仅当 Bearer 全部失败时
        try:
            models = fetch_models(
                base_url=self._api_base(),
                api_key="",  # 不发 Bearer
                models_url_override=self.config.get("models_url_override"),
                custom_user_agent=self.config.get("custom_user_agent"),
                extra_headers=self._headers(),  # 注入 x-api-key + anthropic-version
            )
            return [m["id"] for m in models]
        except Exception as e:
            logger.warning(f"x-api-key 候选列表失败: {e}")

        return []
