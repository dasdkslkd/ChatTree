# backend/api/routes/openai_proxy.py - 反向代理 OpenAI 兼容端点
# 暴露本地 provider 能力给远程 server，使其无需配置 API Key 即可复用本地 provider。
import json
import os
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...core.config.config import cfg
from ...core.config.types import Message, Role, StreamStatus
from ...core.model.model_manager import ModelManager
from ..dependencies import get_model_manager

router = APIRouter(prefix="/proxy")

_PROXY_TOKEN_ENV = "CHATTREE_PROXY_TOKEN"


def _verify_token(authorization: Optional[str]) -> None:
    expected = os.environ.get(_PROXY_TOKEN_ENV, "").strip()
    # 未配置 token 时开放访问（开发模式或由 launcher 注入到可信回环链路）
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization[7:].strip() != expected:
        raise HTTPException(status_code=401, detail="invalid token")


def _find_provider_for_model(model_manager: ModelManager, model: str) -> str:
    for provider_id, models in model_manager.model_list.items():
        if model in models:
            return provider_id
    providers = cfg.get_all_providers()
    for provider_id, pc in providers.items():
        if model in (pc.get("models") or []):
            return provider_id
    raise HTTPException(
        status_code=404,
        detail=f"model {model} not found in any enabled provider",
    )


def _to_internal_messages(openai_messages: List[Dict[str, Any]]) -> List[Message]:
    now = int(time.time() * 1000)
    converted: List[Message] = []
    for raw in openai_messages:
        role_str = str(raw.get("role", "user"))
        try:
            role = Role(role_str)
        except ValueError:
            role = Role.USER
        msg: Message = {
            "id": str(uuid.uuid4()),
            "role": role,
            "content": raw.get("content", ""),
            "timestamp": now,
        }
        if raw.get("tool_calls"):
            msg["tool_calls"] = raw["tool_calls"]
        if raw.get("tool_call_id"):
            msg["tool_call_id"] = raw["tool_call_id"]
        if raw.get("name"):
            msg["name"] = raw["name"]
        converted.append(msg)
    return converted


def _sse_chunk(
    *,
    chat_id: str,
    model: str,
    delta: Dict[str, Any],
    finish_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> str:
    payload: Dict[str, Any] = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _stream_openai(
    model_manager: ModelManager,
    provider_id: str,
    model: str,
    messages: List[Message],
    body: Dict[str, Any],
) -> AsyncIterator[str]:
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    provider = model_manager.get_model(provider_id, is_async=True)
    if provider is None:
        yield _sse_chunk(chat_id=chat_id, model=model, delta={}, finish_reason="stop")
        yield "data: [DONE]\n\n"
        return

    kwargs: Dict[str, Any] = {}
    if body.get("max_tokens") is not None:
        kwargs["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        kwargs["temperature"] = body["temperature"]
    if body.get("tools"):
        kwargs["tools"] = body["tools"]
    if body.get("tool_choice"):
        kwargs["tool_choice"] = body["tool_choice"]
    if body.get("reasoning_effort"):
        kwargs["reasoning_effort"] = body["reasoning_effort"]
    if body.get("thinking_enabled") is not None:
        kwargs["thinking_enabled"] = body["thinking_enabled"]

    # 本地 provider 产出的 tool_calls 是「全量快照」（每次包含已累积的完整 arguments），
    # 而 OpenAI 流式协议要求「增量 delta」（每次只含新增的 arguments 片段）。
    # 这里维护每个 index 已发送的 arguments 前缀，把快照转成真正的 delta，避免下游重复拼接。
    emitted_tool_args: Dict[int, str] = {}

    try:
        async for chunk in provider.generate_response_stream(
            model=model,
            messages=messages,
            stream_controller=None,
            **kwargs,
        ):
            status = chunk.get("status")
            if status == StreamStatus.CONTENT:
                event_type = chunk.get("event_type")
                if event_type == "reasoning" and chunk.get("reasoning"):
                    yield _sse_chunk(
                        chat_id=chat_id,
                        model=model,
                        delta={"reasoning_content": chunk["reasoning"]},
                    )
                elif event_type in ("tool_call", "tool_call_start") and chunk.get("tool_calls"):
                    for idx, tc in enumerate(chunk["tool_calls"]):
                        fn = tc.get("function") or {}
                        args = fn.get("arguments", "") or ""
                        prev = emitted_tool_args.get(idx)
                        if prev is None:
                            yield _sse_chunk(
                                chat_id=chat_id,
                                model=model,
                                delta={"tool_calls": [{
                                    "index": idx,
                                    "id": tc.get("id", ""),
                                    "type": tc.get("type", "function"),
                                    "function": {
                                        "name": fn.get("name", ""),
                                        "arguments": args,
                                    },
                                }]},
                            )
                        else:
                            suffix = args[len(prev):] if args.startswith(prev) else args
                            if suffix:
                                yield _sse_chunk(
                                    chat_id=chat_id,
                                    model=model,
                                    delta={"tool_calls": [{
                                        "index": idx,
                                        "function": {"arguments": suffix},
                                    }]},
                                )
                        emitted_tool_args[idx] = args
                elif chunk.get("content"):
                    yield _sse_chunk(
                        chat_id=chat_id,
                        model=model,
                        delta={"content": chunk["content"]},
                    )
            elif status == StreamStatus.COMPLETE:
                usage_info = chunk.get("usage_info") or {}
                prompt = int(usage_info.get("input_tokens", 0) or 0)
                completion = int(usage_info.get("output_tokens", 0) or 0)
                usage_payload = {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                }
                yield _sse_chunk(
                    chat_id=chat_id,
                    model=model,
                    delta={},
                    finish_reason="tool_calls" if emitted_tool_args else "stop",
                    usage=usage_payload,
                )
            elif status == StreamStatus.ERROR:
                err = chunk.get("error") or "stream error"
                yield _sse_chunk(
                    chat_id=chat_id,
                    model=model,
                    delta={"content": f"[error] {err}"},
                    finish_reason="stop",
                )
            elif status == StreamStatus.STOPPED:
                yield _sse_chunk(
                    chat_id=chat_id,
                    model=model,
                    delta={},
                    finish_reason="stop",
                )
    except Exception as exc:
        yield _sse_chunk(
            chat_id=chat_id,
            model=model,
            delta={"content": f"[error] {exc}"},
            finish_reason="stop",
        )
    yield "data: [DONE]\n\n"


@router.get("/models")
async def list_proxy_models(
    authorization: Optional[str] = Header(default=None),
    model_manager: ModelManager = Depends(get_model_manager),
) -> Dict[str, Any]:
    """返回本地所有已启用 provider 的模型汇总列表（OpenAI 兼容格式）。"""
    _verify_token(authorization)
    data: List[Dict[str, Any]] = []
    for provider_id in model_manager.model_list:
        pc = cfg.get_provider_config(provider_id) or {}
        if not pc.get("enabled", False):
            continue
        owner = pc.get("name") or provider_id
        hidden = set(pc.get("hidden_models") or [])
        for model_id in model_manager.model_list[provider_id]:
            if model_id in hidden:
                continue
            data.append({"id": model_id, "object": "model", "owned_by": owner})
    return {"object": "list", "data": data}


@router.post("/chat/completions")
async def proxy_chat_completions(
    request: Request,
    model_manager: ModelManager = Depends(get_model_manager),
    authorization: Optional[str] = Header(default=None),
):
    """OpenAI 兼容的反向代理端点，按 model 字段路由到本地 provider。"""
    _verify_token(authorization)
    body = await request.json()
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    provider_id = _find_provider_for_model(model_manager, model)
    messages = _to_internal_messages(body.get("messages") or [])
    stream = bool(body.get("stream", False))

    if stream:
        return StreamingResponse(
            _stream_openai(model_manager, provider_id, model, messages, body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：复用流式实现聚合完整内容
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    content_parts: List[str] = []
    tool_call_acc: Dict[int, Dict[str, Any]] = {}
    had_tool_calls = False
    usage_payload: Dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    async for line in _stream_openai(
        model_manager, provider_id, model, messages, {**body, "stream": True}
    ):
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            continue
        try:
            payload = json.loads(line[6:].strip())
        except json.JSONDecodeError:
            continue
        choices = payload.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content_parts.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            idx = int(tc.get("index", 0))
            current = tool_call_acc.setdefault(
                idx,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if tc.get("id"):
                current["id"] = tc["id"]
            if tc.get("type"):
                current["type"] = tc["type"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                current["function"]["name"] = fn["name"]
            if fn.get("arguments"):
                current["function"]["arguments"] += fn["arguments"]
            had_tool_calls = True
        if choice.get("finish_reason") and payload.get("usage"):
            usage_payload = payload["usage"]

    tool_calls = [
        tool_call_acc[i] for i in sorted(tool_call_acc)
        if tool_call_acc[i].get("function", {}).get("name")
    ]
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if had_tool_calls else "stop",
            }
        ],
        "usage": usage_payload,
    }
