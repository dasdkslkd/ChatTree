# model/providers/openai_compatible.py - OpenAI compatible provider over raw HTTP
import asyncio
import json
import urllib.error
import urllib.request
from copy import deepcopy
from typing import List, Dict, Any, Optional, AsyncIterator, Tuple

from ..base import BaseProvider, logger
from ..usage import estimated_usage, usage_from_openai, usage_total
from ...config.types import Message, StreamChunk, StreamStatus, StreamController
from .retry import RetryPolicy, RetryableHTTPError, classify_retry_error, run_with_retries, sleep_before_retry
from .sse import iter_decoded_sse_lines
from .multimodal import to_openai_responses_content
from .stream_queue import StreamStopped, get_queue_item_or_stop

_SENTINEL = object()


class ProviderHTTPError(RetryableHTTPError):
    pass


class OpenAICompatibleProvider(BaseProvider):
    """OpenAI-compatible API provider implemented with urllib."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

    def _api_base(self) -> str:
        return (self.config.get("base_url") or "https://api.openai.com/v1").rstrip("/")

    def _subscription_auth(self):
        """返回 (token, extra_headers)；无订阅时返回 (None, {})."""
        auth = self.config.get("auth") or {}
        if not auth.get("subscription"):
            return None, {}
        from ...auth import get_valid_token_sync
        return get_valid_token_sync(auth)

    def _headers(self, *, stream: bool = False) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token, extra = self._subscription_auth()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers.update(extra)
        else:
            headers["Authorization"] = f"Bearer {self.config.get('api_key', 'ollama')}"
        if stream:
            headers["Accept"] = "text/event-stream"
        if organization := self.config.get("organization"):
            headers["OpenAI-Organization"] = organization
        if project := self.config.get("project"):
            headers["OpenAI-Project"] = project
        # Codex 订阅要求 session-id header（codex CLI / opencode 均会发送）
        auth = self.config.get("auth") or {}
        if auth.get("subscription") == "codex":
            import uuid
            headers["session-id"] = str(uuid.uuid4())
        return headers

    def _url(self, path: str) -> str:
        auth = self.config.get("auth") or {}
        sub = auth.get("subscription")
        # Codex 订阅强制走 chatgpt.com 后端（/v1/responses、/v1/chat/completions 都重写）
        if sub == "codex" and path in ("/responses", "/chat/completions", "/v1/responses", "/v1/chat/completions"):
            return "https://chatgpt.com/backend-api/codex/responses"
        # Copilot 订阅强制走 api.githubcopilot.com
        if sub == "copilot":
            enterprise = auth.get("enterprise_domain", "")
            base = (
                f"https://copilot-api.{enterprise_domain.lower()}"
                if enterprise
                else "https://api.githubcopilot.com"
            )
            return base + path
        return self._api_base() + path

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
            raise ProviderHTTPError(exc.code, error_body, dict(exc.headers or {})) from exc
        except urllib.error.URLError as exc:
            reason = str(exc.reason) if exc.reason else str(exc)
            if "timed out" in reason.lower() or "timeout" in reason.lower():
                raise ProviderHTTPError(0, f"连接超时：{reason}", {"network": "timeout"}) from exc
            if "ssl" in reason.lower() or "certificate" in reason.lower():
                raise ProviderHTTPError(0, f"SSL 握手失败，可能需要配置代理：{reason}", {"network": "ssl"}) from exc
            raise ProviderHTTPError(0, f"网络错误：{reason}", {"network": "error"}) from exc

    def _stream_to_queue(
        self,
        path: str,
        body: Dict[str, Any],
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ):
        req = urllib.request.Request(
            self._url(path),
            data=json.dumps(self._clean_payload(body)).encode("utf-8"),
            headers=self._headers(stream=True),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for line in iter_decoded_sse_lines(resp):
                    if line:
                        loop.call_soon_threadsafe(queue.put_nowait, line)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            loop.call_soon_threadsafe(queue.put_nowait, ProviderHTTPError(exc.code, error_body, dict(exc.headers or {})))
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    def generate_response(
        self,
        model: str,
        messages: List[Message],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs
    ) -> tuple[str, int]:
        if self._use_responses_api():
            return self._generate_response_with_responses_api(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                tools=tools,
                tool_choice=tool_choice,
                **kwargs,
            )

        body = self._build_chat_request_kwargs(
            model=model,
            messages=self._convert_messages(messages),
            stream=False,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            extra_kwargs=kwargs,
            tools=tools,
            tool_choice=tool_choice,
        )
        policy = RetryPolicy.from_config(self.config)
        response = run_with_retries(
            lambda: self._request_json("/chat/completions", body),
            policy,
            label="OpenAI chat completion",
            logger=logger,
        )
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        usage = response.get("usage")
        usage_info = usage_from_openai(usage)
        return content, usage_total(usage_info, 0)

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
        total_content = ""
        total_tokens = 0
        usage_info = None

        try:
            if self._use_responses_api():
                async for chunk in self._stream_responses_api(
                    model=model,
                    messages=messages,
                    stream_controller=stream_controller,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tools=tools,
                    tool_choice=tool_choice,
                    reasoning_effort=reasoning_effort,
                    extra_kwargs=kwargs,
                ):
                    if chunk["status"] == StreamStatus.CONTENT and chunk.get("content"):
                        total_content += chunk["content"] or ""
                    if chunk["status"] == StreamStatus.COMPLETE:
                        usage_info = chunk.get("usage_info")
                        total_tokens = chunk.get("tokens_used", total_tokens)
                    yield chunk
                return

            api_messages = self._convert_messages(messages)
            request_kwargs = self._build_chat_request_kwargs(
                model=model,
                messages=api_messages,
                stream=True,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=None,
                extra_kwargs=kwargs,
                reasoning_effort=reasoning_effort,
                tools=tools,
                tool_choice=tool_choice,
            )
            request_kwargs["stream_options"] = {"include_usage": True}
            if thinking_enabled is not None and self._supports_enable_thinking(model):
                request_kwargs["extra_body"] = {
                    "enable_thinking": thinking_enabled,
                    "chat_template_kwargs": {"enable_thinking": thinking_enabled},
                }

            attempts = [request_kwargs]
            if self.config.get("base_url") and temperature is not None:
                retry_kwargs = dict(request_kwargs)
                retry_kwargs.pop("temperature", None)
                attempts.append(retry_kwargs)
            no_stream_usage = dict(attempts[-1])
            no_stream_usage.pop("stream_options", None)
            attempts.append(no_stream_usage)
            if reasoning_effort or (
                thinking_enabled is not None and self._supports_enable_thinking(model)
            ):
                no_reasoning = dict(attempts[-1])
                no_reasoning.pop("reasoning_effort", None)
                no_reasoning.pop("extra_body", None)
                attempts.append(no_reasoning)

            last_error: Optional[Exception] = None
            tool_call_accumulator: Dict[int, Dict[str, Any]] = {}
            last_emitted_tool_calls = ""
            stream_policy = RetryPolicy.from_config(self.config, stream=True)
            for attempt_index, current_kwargs in enumerate(attempts):
                retry_failures = 0
                fallback_to_next_params = False
                while True:
                    attempt_had_output = False
                    try:
                        tool_call_started = False
                        async for event in self._iter_sse_events(
                            "/chat/completions",
                            current_kwargs,
                            stream_controller=stream_controller,
                        ):
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

                            if usage := event.get("usage"):
                                usage_info = usage_from_openai(usage)
                                total_tokens = usage_total(usage_info, total_tokens)

                            choice = (event.get("choices") or [{}])[0]
                            delta = choice.get("delta") or {}
                            delta_tool_calls = delta.get("tool_calls") or []
                            if delta_tool_calls and not tool_call_started:
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
                            for tool_call in delta_tool_calls:
                                self._merge_openai_tool_call_delta(tool_call_accumulator, tool_call)
                            if delta_tool_calls:
                                tool_calls = self._finalize_openai_tool_calls(tool_call_accumulator)
                                if tool_calls:
                                    serialized_tool_calls = json.dumps(tool_calls, ensure_ascii=False, sort_keys=True)
                                    if serialized_tool_calls != last_emitted_tool_calls:
                                        last_emitted_tool_calls = serialized_tool_calls
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
                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if reasoning:
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

                            content = delta.get("content") or ""
                            if content:
                                attempt_had_output = True
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
                            finish_reason = choice.get("finish_reason")
                            if finish_reason == "tool_calls" and tool_call_accumulator:
                                tool_calls = self._finalize_openai_tool_calls(tool_call_accumulator)
                                serialized_tool_calls = json.dumps(tool_calls, ensure_ascii=False, sort_keys=True)
                                if serialized_tool_calls != last_emitted_tool_calls:
                                    last_emitted_tool_calls = serialized_tool_calls
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
                        break
                    except StreamStopped:
                        yield StreamChunk(
                            status=StreamStatus.STOPPED,
                            content=None,
                            node_id=stream_controller.node_id if stream_controller else None,
                            conversation_id=stream_controller.conversation_id if stream_controller else None,
                            error="用户手动终止",
                            tokens_used=total_tokens,
                        )
                        return
                    except ProviderHTTPError as exc:
                        last_error = exc
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
                        if attempt_index + 1 < len(attempts) and exc.status == 400 and not attempt_had_output:
                            logger.warning(f"Chat completions request rejected, retrying with fewer params: {exc}")
                            fallback_to_next_params = True
                            break
                        decision = classify_retry_error(exc, stream_policy)
                        if (
                            attempt_had_output
                            or not decision.retryable
                            or retry_failures >= stream_policy.max_retries(stream=True)
                        ):
                            raise
                        retry_failures += 1
                        delay = await sleep_before_retry(
                            retry_failures,
                            stream_policy,
                            decision,
                        )
                        logger.warning(
                            f"OpenAI stream failed before output; retrying "
                            f"{retry_failures}/{stream_policy.max_retries(stream=True)} "
                            f"in {delay:.2f}s: {exc}"
                        )
                        continue
                    except Exception as exc:
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
                        decision = classify_retry_error(exc, stream_policy)
                        if (
                            attempt_had_output
                            or not decision.retryable
                            or retry_failures >= stream_policy.max_retries(stream=True)
                        ):
                            raise
                        retry_failures += 1
                        delay = await sleep_before_retry(
                            retry_failures,
                            stream_policy,
                            decision,
                        )
                        logger.warning(
                            f"OpenAI stream failed before output; retrying "
                            f"{retry_failures}/{stream_policy.max_retries(stream=True)} "
                            f"in {delay:.2f}s: {exc}"
                        )
                        continue
                if fallback_to_next_params:
                    continue
                break

            if last_error and not total_content and usage_info is None and not attempts:
                raise last_error

            if usage_info is None:
                usage_info = estimated_usage(total_tokens)
            yield StreamChunk(
                status=StreamStatus.COMPLETE,
                content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=None,
                tokens_used=total_tokens,
                usage_info=usage_info,
            )

        except asyncio.CancelledError:
            yield StreamChunk(
                status=StreamStatus.STOPPED,
                content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error="任务被取消",
                tokens_used=total_tokens,
            )
        except Exception as e:
            logger.error(
                f"Stream error: {e} - Conversation: "
                f"{stream_controller.conversation_id if stream_controller else None} - "
                f"Node: {stream_controller.node_id if stream_controller else None}"
            )
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=str(e),
                tokens_used=total_tokens,
            )

    async def _stream_responses_api(
        self,
        model: str,
        messages: List[Message],
        stream_controller: Optional[StreamController],
        max_tokens: Optional[int],
        temperature: Optional[float],
        tools: Optional[List[Dict[str, Any]]],
        tool_choice: Optional[str],
        reasoning_effort: Optional[str],
        extra_kwargs: Dict[str, Any],
    ) -> AsyncIterator[StreamChunk]:
        total_tokens = 0
        usage_info = None
        instructions, response_input = self._convert_messages_to_responses_input(messages)
        request_kwargs = self._build_responses_request_kwargs(
            instructions=instructions,
            response_input=response_input,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=None,
            reasoning_effort=reasoning_effort,
            extra_kwargs=extra_kwargs,
            tools=tools,
            tool_choice=tool_choice,
        )
        request_kwargs["model"] = model
        request_kwargs["stream"] = True

        attempts = [request_kwargs]
        if self._should_retry_responses_without_temperature(request_kwargs):
            retry_kwargs = dict(request_kwargs)
            retry_kwargs.pop("temperature", None)
            attempts.append(retry_kwargs)

        function_calls: Dict[str, Dict[str, Any]] = {}
        last_emitted_tool_calls = ""
        stream_policy = RetryPolicy.from_config(self.config, stream=True)
        for attempt_index, current_kwargs in enumerate(attempts):
            retry_failures = 0
            fallback_to_next_params = False
            while True:
                total_content = ""
                function_calls = {}
                tool_call_started = False
                last_emitted_tool_calls = ""
                attempt_had_output = False
                try:
                    async for event in self._iter_sse_events(
                        "/responses",
                        current_kwargs,
                        stream_controller=stream_controller,
                    ):
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

                        event_type = event.get("type", "")
                        if event_type == "response.completed":
                            response = event.get("response") or {}
                            if usage := response.get("usage"):
                                usage_info = usage_from_openai(usage)
                                total_tokens = usage_total(usage_info, total_tokens)
                            completed_text = self._extract_responses_text(response)
                            if completed_text and completed_text != total_content:
                                content = (
                                    completed_text[len(total_content):]
                                    if total_content and completed_text.startswith(total_content)
                                    else completed_text
                                    if not total_content
                                    else ""
                                )
                                if content:
                                    attempt_had_output = True
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
                            completed_tool_calls = self._extract_responses_tool_calls(response)
                            if completed_tool_calls:
                                function_calls.clear()
                                serialized_tool_calls = json.dumps(completed_tool_calls, ensure_ascii=False, sort_keys=True)
                                if serialized_tool_calls != last_emitted_tool_calls:
                                    last_emitted_tool_calls = serialized_tool_calls
                                    attempt_had_output = True
                                    yield StreamChunk(
                                        status=StreamStatus.CONTENT,
                                        content=None,
                                        node_id=stream_controller.node_id if stream_controller else None,
                                        conversation_id=stream_controller.conversation_id if stream_controller else None,
                                        error=None,
                                        tokens_used=0,
                                        event_type="tool_call",
                                        tool_call={"tool_calls": completed_tool_calls},
                                        tool_calls=completed_tool_calls,
                                    )
                            continue

                        if event_type == "response.output_item.added":
                            item = event.get("item") or {}
                            if item.get("type") == "function_call":
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
                                key = str(event.get("output_index", item.get("id") or item.get("call_id") or len(function_calls)))
                                function_calls[key] = {
                                    "id": item.get("call_id") or item.get("id") or key,
                                    "type": "function",
                                    "function": {
                                        "name": item.get("name", ""),
                                        "arguments": item.get("arguments", "") or "",
                                    },
                                }
                                tool_calls = self._finalize_response_function_calls(function_calls)
                                if tool_calls:
                                    serialized_tool_calls = json.dumps(tool_calls, ensure_ascii=False, sort_keys=True)
                                    if serialized_tool_calls != last_emitted_tool_calls:
                                        last_emitted_tool_calls = serialized_tool_calls
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
                            continue

                        if event_type == "response.function_call_arguments.delta":
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
                            key = str(event.get("output_index", "0"))
                            call = function_calls.setdefault(
                                key,
                                {"id": key, "type": "function", "function": {"name": "", "arguments": ""}},
                            )
                            call["function"]["arguments"] += event.get("delta") or ""
                            tool_calls = self._finalize_response_function_calls(function_calls)
                            if tool_calls:
                                serialized_tool_calls = json.dumps(tool_calls, ensure_ascii=False, sort_keys=True)
                                if serialized_tool_calls != last_emitted_tool_calls:
                                    last_emitted_tool_calls = serialized_tool_calls
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
                            continue

                        if event_type == "response.output_item.done":
                            item = event.get("item") or {}
                            if item.get("type") == "function_call":
                                key = str(event.get("output_index", item.get("id") or item.get("call_id") or "0"))
                                function_calls[key] = {
                                    "id": item.get("call_id") or item.get("id") or key,
                                    "type": "function",
                                    "function": {
                                        "name": item.get("name", ""),
                                        "arguments": item.get("arguments", "") or "",
                                    },
                                }
                            continue

                        if event_type == "response.reasoning_summary_text.delta":
                            reasoning = event.get("delta") or ""
                            if reasoning:
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
                            continue

                        if event_type != "response.output_text.delta":
                            continue

                        content = event.get("delta") or ""
                        if not content:
                            continue
                        attempt_had_output = True
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
                    break
                except StreamStopped:
                    yield StreamChunk(
                        status=StreamStatus.STOPPED,
                        content=None,
                        node_id=stream_controller.node_id if stream_controller else None,
                        conversation_id=stream_controller.conversation_id if stream_controller else None,
                        error="用户手动终止",
                        tokens_used=total_tokens,
                    )
                    return
                except ProviderHTTPError as exc:
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
                    if (
                        attempt_index + 1 < len(attempts)
                        and exc.status == 400
                        and not attempt_had_output
                        and "temperature" in current_kwargs
                    ):
                        logger.warning(f"Responses stream rejected temperature, retrying without it: {exc}")
                        fallback_to_next_params = True
                        break
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
                        f"Responses stream failed before output; retrying "
                        f"{retry_failures}/{stream_policy.max_retries(stream=True)} "
                        f"in {delay:.2f}s: {exc}"
                    )
                    continue
                except Exception as exc:
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
                        f"Responses stream failed before output; retrying "
                        f"{retry_failures}/{stream_policy.max_retries(stream=True)} "
                        f"in {delay:.2f}s: {exc}"
                    )
                    continue
            if fallback_to_next_params:
                continue
            break

        if function_calls:
            tool_calls = self._finalize_response_function_calls(function_calls)
            if tool_calls:
                serialized_tool_calls = json.dumps(tool_calls, ensure_ascii=False, sort_keys=True)
                if serialized_tool_calls != last_emitted_tool_calls:
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

        if usage_info is None:
            usage_info = estimated_usage(total_tokens)
        yield StreamChunk(
            status=StreamStatus.COMPLETE,
            content=None,
            node_id=stream_controller.node_id if stream_controller else None,
            conversation_id=stream_controller.conversation_id if stream_controller else None,
            error=None,
            tokens_used=total_tokens,
            usage_info=usage_info,
        )

    async def _iter_sse_events(
        self,
        path: str,
        body: Dict[str, Any],
        *,
        stream_controller: Optional[StreamController] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, self._stream_to_queue, path, body, queue, loop)

        while True:
            item = await get_queue_item_or_stop(queue, stream_controller)
            if item is _SENTINEL:
                break
            if isinstance(item, Exception):
                raise item
            line = str(item)
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                logger.warning(f"Invalid SSE payload ignored: {payload[:200]}")

    def _use_responses_api(self) -> bool:
        return self.config.get("api_format") == "responses"

    def _generate_response_with_responses_api(
        self,
        model: str,
        messages: List[Message],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
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
            tools=tools,
            tool_choice=tool_choice,
        )
        request_kwargs["model"] = model
        policy = RetryPolicy.from_config(self.config)
        response = run_with_retries(
            lambda: self._request_json("/responses", request_kwargs),
            policy,
            label="OpenAI responses request",
            logger=logger,
        )
        content = self._extract_responses_text(response)
        usage_info = usage_from_openai(response.get("usage"))
        return content, usage_total(usage_info, 0)

    def _build_chat_request_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        stream: bool,
        max_tokens: Optional[int],
        temperature: Optional[float],
        top_p: Optional[float],
        extra_kwargs: Dict[str, Any],
        reasoning_effort: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            **extra_kwargs,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        return body

    def _build_responses_request_kwargs(
        self,
        instructions: Optional[str],
        response_input: List[Dict[str, Any]],
        max_tokens: Optional[int],
        temperature: Optional[float],
        top_p: Optional[float],
        extra_kwargs: Dict[str, Any],
        reasoning_effort: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
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
        if reasoning_effort:
            request_kwargs["reasoning"] = {"effort": reasoning_effort}
        if tools:
            request_kwargs["tools"] = [self._openai_tool_to_responses_tool(tool) for tool in tools]
            if tool_choice and tool_choice != "auto":
                request_kwargs["tool_choice"] = tool_choice
        # Codex 订阅要求 store=false（参考 codex CLI），且不支持 max_output_tokens 和 temperature（参考 opencode）
        auth = self.config.get("auth") or {}
        if auth.get("subscription") == "codex":
            request_kwargs["store"] = False
            request_kwargs.pop("max_output_tokens", None)
            request_kwargs.pop("temperature", None)
        return request_kwargs

    def _should_retry_responses_without_temperature(self, request_kwargs: Dict[str, Any]) -> bool:
        return bool(self.config.get("base_url") and "temperature" in request_kwargs)

    def _supports_enable_thinking(self, model: str) -> bool:
        lowered = model.lower()
        return any(marker in lowered for marker in ("qwen", "qwq"))

    def _convert_messages_to_responses_input(self, messages: List[Message]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        response_input: List[Dict[str, Any]] = []

        for msg in messages:
            raw_role = msg["role"]
            role = raw_role.value if hasattr(raw_role, "value") else str(raw_role)
            content = msg.get("content") or ""
            responses_content = to_openai_responses_content(content)

            if role == "system":
                role = "developer"
            if role == "assistant" and msg.get("tool_calls"):
                if content:
                    response_input.append({
                        "type": "message",
                        "role": "assistant",
                        "content": responses_content,
                    })
                for tool_call in msg.get("tool_calls") or []:
                    fn = tool_call.get("function") or {}
                    response_input.append({
                        "type": "function_call",
                        "call_id": tool_call.get("id"),
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments") or "{}",
                    })
                continue
            if role == "tool":
                response_input.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id") or msg.get("name") or "tool",
                    "output": content,
                })
                continue
            if role not in {"user", "assistant", "developer"}:
                role = "user"

            response_input.append({
                "type": "message",
                "role": role,
                "content": responses_content,
            })

        return None, response_input

    def _merge_openai_tool_call_delta(
        self,
        accumulator: Dict[int, Dict[str, Any]],
        tool_call: Dict[str, Any],
    ):
        index = int(tool_call.get("index", 0))
        current = accumulator.setdefault(
            index,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if tool_call.get("id"):
            current["id"] = tool_call["id"]
        if tool_call.get("type"):
            current["type"] = tool_call["type"]
        function = tool_call.get("function") or {}
        if function.get("name"):
            current["function"]["name"] = function["name"]
        if function.get("arguments"):
            current["function"]["arguments"] += function["arguments"]

    def _finalize_openai_tool_calls(self, accumulator: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            deepcopy(call) for _, call in sorted(accumulator.items(), key=lambda item: item[0])
            if call.get("function", {}).get("name")
        ]

    def _finalize_response_function_calls(self, function_calls: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            deepcopy(call) for _, call in sorted(function_calls.items(), key=lambda item: item[0])
            if call.get("function", {}).get("name")
        ]

    def _openai_tool_to_responses_tool(self, tool: Dict[str, Any]) -> Dict[str, Any]:
        fn = tool.get("function") or {}
        return {
            "type": "function",
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        }

    def _extract_responses_tool_calls(self, response: Dict[str, Any]) -> List[Dict[str, Any]]:
        tool_calls: List[Dict[str, Any]] = []
        for item in response.get("output") or []:
            if item.get("type") != "function_call":
                continue
            call_id = item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}"
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "") or "{}",
                },
            })
        return tool_calls

    def _extract_responses_text(self, response: Dict[str, Any]) -> str:
        if output_text := response.get("output_text"):
            return output_text

        text_parts: List[str] = []
        for item in response.get("output") or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if content.get("type") != "output_text":
                    continue
                if text := content.get("text"):
                    text_parts.append(text)
        return "".join(text_parts)

    def _convert_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        for msg in messages:
            item = {
                "role": msg["role"].value if hasattr(msg["role"], "value") else str(msg["role"]),
                "content": msg["content"],
                "name": msg.get("name"),
                "tool_calls": msg.get("tool_calls"),
                "tool_call_id": msg.get("tool_call_id"),
            }
            converted.append(self._clean_payload(item))
        return converted

    def _clean_payload(self, value: Any) -> Any:
        if isinstance(value, dict):
            cleaned: Dict[str, Any] = {}
            extra_body = value.get("extra_body")
            for key, child in value.items():
                if key == "extra_body":
                    continue
                if child is None:
                    continue
                cleaned[key] = self._clean_payload(child)
            if isinstance(extra_body, dict):
                cleaned.update(self._clean_payload(extra_body))
            return cleaned
        if isinstance(value, list):
            return [self._clean_payload(item) for item in value]
        return value

    def list_models(self) -> List[str]:
        # 订阅 provider 走 subscription.fetch_models_sync 动态发现
        auth = self.config.get("auth") or {}
        if auth.get("subscription"):
            from ...auth import fetch_models_sync
            try:
                models = fetch_models_sync(auth)
                return [m["id"] for m in models]
            except Exception as e:
                logger.error(f"获取订阅模型列表失败: {e}")
                raise RuntimeError(f"获取订阅模型列表失败: {e}")

        from .model_fetch import fetch_models
        try:
            models = fetch_models(
                base_url=self._api_base(),
                api_key=self.config.get("api_key", ""),
                models_url_override=self.config.get("models_url_override"),
                custom_user_agent=self.config.get("custom_user_agent"),
            )
            return [m["id"] for m in models]
        except Exception as e:
            logger.error(f"获取模型列表失败: {e}")
            raise RuntimeError(f"获取模型列表失败: {e}")
