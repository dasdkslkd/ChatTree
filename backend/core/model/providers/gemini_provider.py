# model/providers/gemini_provider.py - Gemini provider over raw HTTP
import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from copy import deepcopy
from typing import List, Dict, Any, Optional, AsyncIterator, Tuple

from ..base import BaseProvider, logger
from ..usage import estimated_usage, usage_from_gemini, usage_total
from ...config.types import Message, ModelRoute, StreamChunk, StreamStatus, StreamController
from .retry import RetryPolicy, RetryableHTTPError, classify_retry_error, run_with_retries, sleep_before_retry
from .sse import StreamStopped, iter_sse_lines
from .multimodal import to_gemini_parts


class GeminiHTTPError(RetryableHTTPError):
    pass


class GeminiProvider(BaseProvider):
    """Google Gemini API provider implemented with urllib."""

    _EFFORT_BUDGET = {
        "dynamic": -1,
        "low": 1024,
        "medium": 8192,
        "high": 24576,
    }

    def __init__(self, config: Dict[str, Any], route: ModelRoute):
        super().__init__(config, route)

    def _api_base(self) -> str:
        base = (self.config.get("base_url") or "https://generativelanguage.googleapis.com").rstrip("/")
        if base.endswith("/v1") or base.endswith("/v1beta"):
            return base
        return base + "/v1beta"

    def _headers(self, *, stream: bool = False) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.config.get("api_key", ""),
            "User-Agent": self.config.get("custom_user_agent") or "ChatTree",
        }
        if stream:
            headers["Accept"] = "text/event-stream"
        return headers

    def _url(self, path: str, params: Optional[Dict[str, str]] = None) -> str:
        query = dict(params or {})
        api_key = self.config.get("api_key", "")
        if api_key:
            query.setdefault("key", api_key)
        url = self._api_base() + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _request_json(self, path: str, body: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
        req = urllib.request.Request(
            self._url(path),
            data=json.dumps(self._clean_payload(body)).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise GeminiHTTPError(exc.code, error_body, dict(exc.headers or {})) from exc

    def _request_get_json(self, path: str, timeout: int = 30) -> Dict[str, Any]:
        req = urllib.request.Request(
            self._url(path),
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise GeminiHTTPError(exc.code, error_body, dict(exc.headers or {})) from exc

    def _convert_messages(self, messages: List[Message]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        system_prompt = ""
        gemini_messages: List[Dict[str, Any]] = []

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
                parts: List[Dict[str, Any]] = []
                text_emitted = False
                for entry in layout:
                    if not isinstance(entry, dict):
                        continue
                    if isinstance(entry.get("state"), dict):
                        parts.append(deepcopy(entry["state"]))
                    elif "text" in entry and not text_emitted and msg.get("content"):
                        parts.extend(to_gemini_parts(msg.get("content") or ""))
                        text_emitted = True
                    elif entry.get("tool_call_id") in tool_calls:
                        call = tool_calls[str(entry["tool_call_id"])]
                        fn = call.get("function") or {}
                        try:
                            args = json.loads(fn.get("arguments") or "{}")
                        except json.JSONDecodeError:
                            args = {"arguments": fn.get("arguments") or ""}
                        function_part = {
                            "functionCall": {
                                "name": fn.get("name", ""),
                                "args": args,
                            }
                        }
                        if entry.get("thoughtSignature"):
                            function_part["thoughtSignature"] = entry["thoughtSignature"]
                        parts.append(function_part)
                if msg.get("content") and not text_emitted:
                    parts.extend(to_gemini_parts(msg.get("content") or ""))
                gemini_messages.append({"role": "model", "parts": parts})
                continue
            role = msg["role"].value if hasattr(msg["role"], "value") else str(msg["role"])
            content = msg.get("content") or ""
            if role == "system":
                system_prompt += content + "\n"
                continue
            if role == "assistant" and msg.get("tool_calls"):
                parts = []
                if content:
                    parts.extend(to_gemini_parts(content))
                for tool_call in msg.get("tool_calls") or []:
                    fn = tool_call.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {"arguments": fn.get("arguments") or ""}
                    parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
                gemini_messages.append({"role": "model", "parts": parts})
                continue
            if role == "tool":
                tool_name = msg.get("name") or msg.get("tool_call_id") or "tool"
                try:
                    response = json.loads(content)
                except json.JSONDecodeError:
                    response = {"result": content}
                gemini_messages.append({
                    "role": "user",
                    "parts": [{"functionResponse": {"name": tool_name, "response": response}}],
                })
                continue
            gemini_messages.append({
                "role": "model" if role == "assistant" else "user",
                "parts": to_gemini_parts(content),
            })

        merged_messages: List[Dict[str, Any]] = []
        for message in gemini_messages:
            if (
                merged_messages
                and merged_messages[-1].get("role") == message.get("role")
            ):
                merged_messages[-1]["parts"].extend(message.get("parts") or [])
            else:
                merged_messages.append(deepcopy(message))
        return system_prompt.strip() or None, merged_messages

    def _build_generation_config(
        self,
        max_tokens: Optional[int],
        temperature: Optional[float],
        top_p: Optional[float],
        reasoning_effort: Optional[str],
        thinking_enabled: Optional[bool],
    ) -> Dict[str, Any]:
        config: Dict[str, Any] = {}
        if max_tokens is not None:
            config["maxOutputTokens"] = max_tokens
        if temperature is not None:
            config["temperature"] = temperature
        if top_p is not None:
            config["topP"] = top_p

        thinking_budget = self._thinking_budget(reasoning_effort, thinking_enabled)
        if thinking_budget is not None:
            config["thinkingConfig"] = {"thinkingBudget": thinking_budget}

        return config

    def _thinking_budget(
        self,
        reasoning_effort: Optional[str],
        thinking_enabled: Optional[bool],
    ) -> Optional[int]:
        if thinking_enabled is False:
            return 0
        if reasoning_effort:
            return self._EFFORT_BUDGET.get(reasoning_effort)
        if thinking_enabled is True:
            return -1
        return None

    def _build_body(
        self,
        messages: List[Message],
        max_tokens: Optional[int],
        temperature: Optional[float],
        top_p: Optional[float],
        reasoning_effort: Optional[str],
        thinking_enabled: Optional[bool],
        extra_kwargs: Dict[str, Any],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
    ) -> Dict[str, Any]:
        system_prompt, gemini_messages = self._convert_messages(messages)
        body: Dict[str, Any] = {
            "contents": gemini_messages,
            **extra_kwargs,
        }
        if system_prompt:
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        generation_config = self._build_generation_config(
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            reasoning_effort=reasoning_effort,
            thinking_enabled=thinking_enabled,
        )
        if generation_config:
            body["generationConfig"] = generation_config
        if tools:
            body["tools"] = [{
                "functionDeclarations": [
                    self._openai_tool_to_gemini_declaration(tool)
                    for tool in tools
                ]
            }]
            mode = "ANY" if tool_choice and tool_choice != "auto" else "AUTO"
            function_calling_config: Dict[str, Any] = {"mode": mode}
            if tool_choice and tool_choice != "auto":
                function_calling_config["allowedFunctionNames"] = [tool_choice]
            body["toolConfig"] = {"functionCallingConfig": function_calling_config}
        return body

    def generate_response(
        self,
        model: str,
        messages: List[Message],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
        **kwargs
    ) -> tuple[str, int]:
        body = self._build_body(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            reasoning_effort=reasoning_effort,
            thinking_enabled=thinking_enabled,
            extra_kwargs=kwargs,
            tools=tools,
            tool_choice=tool_choice,
        )
        policy = RetryPolicy.from_config(self.config)
        result = run_with_retries(
            lambda: self._request_json(
                self.route["endpoint"].format(model=model),
                body,
            ),
            policy,
            label="Gemini generateContent",
            logger=logger,
        )
        usage_info = usage_from_gemini(result.get("usageMetadata"))
        return self._extract_text(result), usage_total(usage_info, 0)

    async def generate_response_stream(
        self,
        model: str,
        messages: List[Message],
        stream_controller: Optional[StreamController] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
        **kwargs
    ) -> AsyncIterator[StreamChunk]:
        total_tokens = 0
        usage_info = None
        native_parts: List[Dict[str, Any]] = []
        native_tool_call_ids: List[str] = []
        finish_reason: Optional[str] = None

        try:
            body = self._build_body(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=None,
                reasoning_effort=reasoning_effort,
                thinking_enabled=thinking_enabled,
                extra_kwargs=kwargs,
                tools=tools,
                tool_choice=tool_choice,
            )

            stream_policy = RetryPolicy.from_config(self.config)
            retry_failures = 0
            tool_call_started = False
            while True:
                attempt_had_output = False
                native_parts = []
                native_tool_call_ids = []
                finish_reason = None
                try:
                    async for event in self._iter_sse_events(
                        self.route["endpoint"].format(model=model).replace(
                            ":generateContent",
                            ":streamGenerateContent",
                        ),
                        body,
                        stream_controller=stream_controller,
                    ):
                        for candidate in event.get("candidates") or []:
                            observed_finish_reason = candidate.get("finishReason")
                            if observed_finish_reason:
                                raw_finish_reason = str(observed_finish_reason).strip().lower()
                                finish_reason = {
                                    "stop": "stop",
                                    "max_tokens": "length",
                                }.get(raw_finish_reason, raw_finish_reason)
                            content = candidate.get("content") or {}
                            for part in content.get("parts") or []:
                                if isinstance(part, dict):
                                    native_parts.append(deepcopy(part))
                        if usage := event.get("usageMetadata"):
                            usage_info = usage_from_gemini(usage)
                            total_tokens = usage_total(usage_info, total_tokens)

                        tool_calls = self._extract_tool_calls(
                            event,
                            start_index=len(native_tool_call_ids),
                        )
                        if tool_calls:
                            native_tool_call_ids.extend(
                                str(call.get("id"))
                                for call in tool_calls
                                if call.get("id") and str(call.get("id")) not in native_tool_call_ids
                            )
                            if not tool_call_started:
                                tool_call_started = True
                                attempt_had_output = True
                                yield StreamChunk(
                                    status=StreamStatus.CONTENT,
                                    content=None,
                                    node_id=stream_controller.node_id if stream_controller else None,
                                    conversation_id=stream_controller.conversation_id if stream_controller else None,
                                    error=None,
                                    tokens_used=0,
                                    event_type="tool_call_start",
                                )
                            attempt_had_output = True
                            yield StreamChunk(
                                status=StreamStatus.CONTENT,
                                content=None,
                                node_id=stream_controller.node_id if stream_controller else None,
                                conversation_id=stream_controller.conversation_id if stream_controller else None,
                                error=None,
                                tokens_used=0,
                                event_type="tool_call",
                                tool_call={"tool_calls": tool_calls},
                                tool_calls=tool_calls,
                            )

                        for reasoning in self._extract_reasoning_parts(event):
                            if stream_controller and await stream_controller.is_stopped():
                                yield StreamChunk(
                                    status=StreamStatus.STOPPED,
                                    content=None,
                                    node_id=stream_controller.node_id,
                                    conversation_id=stream_controller.conversation_id,
                                    error="用户手动终止",
                                    tokens_used=total_tokens,
                                    usage_info=usage_info,
                                )
                                return
                            attempt_had_output = True
                            yield StreamChunk(
                                status=StreamStatus.CONTENT,
                                content=None,
                                node_id=stream_controller.node_id if stream_controller else None,
                                conversation_id=stream_controller.conversation_id if stream_controller else None,
                                error=None,
                                tokens_used=0,
                                event_type="reasoning",
                                reasoning=reasoning,
                            )

                        text = self._extract_text(event)
                        if not text:
                            continue
                        attempt_had_output = True
                        token_delta = int(len(text.split()) * 1.3)
                        total_tokens += token_delta

                        if stream_controller and await stream_controller.is_stopped():
                            yield StreamChunk(
                                status=StreamStatus.STOPPED,
                                content=None,
                                node_id=stream_controller.node_id,
                                conversation_id=stream_controller.conversation_id,
                                error="用户手动终止",
                                tokens_used=total_tokens,
                                usage_info=usage_info,
                            )
                            return

                        yield StreamChunk(
                            status=StreamStatus.CONTENT,
                            content=text,
                            node_id=stream_controller.node_id if stream_controller else None,
                            conversation_id=stream_controller.conversation_id if stream_controller else None,
                            error=None,
                            tokens_used=token_delta,
                        )
                    break
                except StreamStopped:
                    yield StreamChunk(
                        status=StreamStatus.STOPPED,
                        content=None,
                        node_id=stream_controller.node_id if stream_controller else None,
                        conversation_id=stream_controller.conversation_id if stream_controller else None,
                        error="用户手动终止",
                        tokens_used=total_tokens,
                        usage_info=usage_info,
                    )
                    return
                except Exception as exc:
                    if stream_controller and await stream_controller.is_stopped():
                        yield StreamChunk(
                            status=StreamStatus.STOPPED,
                            content=None,
                            node_id=stream_controller.node_id,
                            conversation_id=stream_controller.conversation_id,
                            error="用户手动终止",
                            tokens_used=total_tokens,
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
                        f"Gemini stream failed before output; retrying "
                        f"{retry_failures}/{stream_policy.max_retries(stream=True)} "
                        f"in {delay:.2f}s: {exc}"
                    )

            if usage_info is None:
                usage_info = estimated_usage(total_tokens)
            if native_tool_call_ids and finish_reason in {None, "stop"}:
                finish_reason = "tool_calls"
            state_layout = []
            has_state = False
            tool_position = 0
            for part in native_parts:
                if part.get("functionCall"):
                    tool_call_id = (
                        native_tool_call_ids[tool_position]
                        if tool_position < len(native_tool_call_ids)
                        else ""
                    )
                    tool_position += 1
                    entry = {"tool_call_id": tool_call_id}
                    if part.get("thoughtSignature"):
                        entry["thoughtSignature"] = part["thoughtSignature"]
                        has_state = True
                    state_layout.append(entry)
                elif part.get("thought") or part.get("thoughtSignature"):
                    state_layout.append({"state": part})
                    has_state = True
                elif "text" in part:
                    state_layout.append({"text": True})
                else:
                    state_layout.append({"state": part})
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
                    "display_text": "".join(
                        str(part.get("text") or "")
                        for part in native_parts
                        if not part.get("thought")
                    ),
                    "tool_call_ids": native_tool_call_ids,
                    "native_payload": {
                        "role": "model",
                        "parts": native_parts,
                    },
                    **(
                        {
                            "state_payload": {
                                "role": "model",
                                "layout": state_layout,
                            }
                        }
                        if has_state
                        else {}
                    ),
                },
            )
            yield StreamChunk(
                status=StreamStatus.COMPLETE,
                content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=None,
                tokens_used=total_tokens,
                usage_info=usage_info,
                metadata={"finish_reason": finish_reason or "unknown"},
            )

        except asyncio.CancelledError:
            yield StreamChunk(
                status=StreamStatus.STOPPED,
                content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error="任务被取消",
                tokens_used=total_tokens,
                usage_info=usage_info,
            )
        except Exception as e:
            logger.error(f"Gemini stream error: {e}")
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=str(e) or e.__class__.__name__,
                tokens_used=total_tokens,
                usage_info=usage_info,
                metadata={"retryable": classify_retry_error(e).retryable},
            )

    async def _iter_sse_events(
        self,
        path: str,
        body: Dict[str, Any],
        *,
        stream_controller: Optional[StreamController] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        async for line in iter_sse_lines(
            self._url(path, {"alt": "sse"}),
            self._clean_payload(body),
            self._headers(stream=True),
            self.config,
            GeminiHTTPError,
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
                logger.warning(f"Invalid Gemini SSE payload ignored: {payload[:200]}")

    def _extract_text(self, payload: Dict[str, Any]) -> str:
        chunks: List[str] = []
        for candidate in payload.get("candidates") or []:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                if part.get("thought"):
                    continue
                if text := part.get("text"):
                    chunks.append(text)
        return "".join(chunks)

    def _extract_reasoning_parts(self, payload: Dict[str, Any]) -> List[str]:
        chunks: List[str] = []
        for candidate in payload.get("candidates") or []:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                if part.get("thought") and part.get("text"):
                    chunks.append(part["text"])
        return chunks

    def _extract_tool_calls(
        self,
        payload: Dict[str, Any],
        start_index: int = 0,
    ) -> List[Dict[str, Any]]:
        tool_calls: List[Dict[str, Any]] = []
        for candidate in payload.get("candidates") or []:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                function_call = part.get("functionCall") or part.get("function_call")
                if not isinstance(function_call, dict):
                    continue
                name = str(function_call.get("name") or "")
                if not name:
                    continue
                args = function_call.get("args")
                tool_calls.append({
                    "id": str(
                        function_call.get("id")
                        or f"gemini_call_{start_index + len(tool_calls)}"
                    ),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False),
                    },
                })
        return tool_calls

    def _openai_tool_to_gemini_declaration(self, tool: Dict[str, Any]) -> Dict[str, Any]:
        fn = tool.get("function") or {}
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        }

    def _clean_payload(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self._clean_payload(child)
                for key, child in value.items()
                if child is not None
            }
        if isinstance(value, list):
            return [self._clean_payload(item) for item in value]
        return value

    def list_models(self) -> List[str]:
        try:
            policy = RetryPolicy.from_config(self.config)
            data = run_with_retries(
                lambda: self._request_get_json("/models"),
                policy,
                label="Gemini list models",
                logger=logger,
            )
            models: List[str] = []
            for model in data.get("models", []):
                methods = model.get("supportedGenerationMethods") or []
                if "generateContent" not in methods:
                    continue
                name = model.get("name", "")
                models.append(name.split("/")[-1])
            return models
        except Exception as e:
            logger.error(f"获取 Gemini 模型列表失败: {e}")
            raise RuntimeError(f"获取 Gemini 模型列表失败: {e}")
