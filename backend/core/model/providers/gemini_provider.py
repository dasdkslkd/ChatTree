# model/providers/gemini_provider.py - Gemini provider over raw HTTP
import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Dict, Any, Optional, AsyncIterator, Tuple

from ..base import BaseProvider, logger
from ..usage import estimated_usage, usage_from_gemini, usage_total
from ...config.types import Message, StreamChunk, StreamStatus, StreamController

_SENTINEL = object()


class GeminiHTTPError(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


class GeminiProvider(BaseProvider):
    """Google Gemini API provider implemented with urllib."""

    _EFFORT_BUDGET = {
        "dynamic": -1,
        "low": 1024,
        "medium": 8192,
        "high": 24576,
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

    def _api_base(self) -> str:
        base = (self.config.get("base_url") or "https://generativelanguage.googleapis.com").rstrip("/")
        if base.endswith("/v1") or base.endswith("/v1beta"):
            return base
        return base + "/v1beta"

    def _headers(self, *, stream: bool = False) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.config.get("api_key", ""),
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
            raise GeminiHTTPError(exc.code, error_body) from exc

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
            raise GeminiHTTPError(exc.code, error_body) from exc

    def _stream_to_queue(
        self,
        path: str,
        body: Dict[str, Any],
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ):
        req = urllib.request.Request(
            self._url(path, {"alt": "sse"}),
            data=json.dumps(self._clean_payload(body)).encode("utf-8"),
            headers=self._headers(stream=True),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                buffer = ""
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.rstrip("\r")
                        if line:
                            loop.call_soon_threadsafe(queue.put_nowait, line)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            loop.call_soon_threadsafe(queue.put_nowait, GeminiHTTPError(exc.code, error_body))
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    def _convert_messages(self, messages: List[Message]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        system_prompt = ""
        gemini_messages: List[Dict[str, Any]] = []

        for msg in messages:
            role = str(msg["role"])
            content = msg.get("content") or ""
            if role == "system":
                system_prompt += content + "\n"
                continue
            if role == "tool":
                tool_name = msg.get("name") or msg.get("tool_call_id") or "tool"
                content = f"[{tool_name}]\n{content}"
                role = "user"
            gemini_messages.append({
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": content}],
            })

        return system_prompt.strip() or None, gemini_messages

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
        return body

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
        body = self._build_body(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            reasoning_effort=reasoning_effort,
            thinking_enabled=thinking_enabled,
            extra_kwargs=kwargs,
        )
        result = self._request_json(f"/models/{model}:generateContent", body)
        usage_info = usage_from_gemini(result.get("usageMetadata"))
        return self._extract_text(result), usage_total(usage_info, 0)

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

            body = self._build_body(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=None,
                reasoning_effort=reasoning_effort,
                thinking_enabled=thinking_enabled,
                extra_kwargs=kwargs,
            )

            async for event in self._iter_sse_events(f"/models/{model}:streamGenerateContent", body):
                if usage := event.get("usageMetadata"):
                    usage_info = usage_from_gemini(usage)
                    total_tokens = usage_total(usage_info, total_tokens)

                for reasoning in self._extract_reasoning_parts(event):
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
            logger.error(f"Gemini stream error: {e}")
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content=None,
                node_id=stream_controller.node_id if stream_controller else None,
                conversation_id=stream_controller.conversation_id if stream_controller else None,
                error=str(e),
                tokens_used=total_tokens,
            )

    async def _iter_sse_events(self, path: str, body: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, self._stream_to_queue, path, body, queue, loop)

        while True:
            item = await queue.get()
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
            data = self._request_get_json("/models")
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
