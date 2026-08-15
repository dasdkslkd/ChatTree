"""本地模型连接的透明反向代理。

代理只解析模型路由并替换认证；请求字段、响应状态与 SSE 字节流不做协议转换。
"""
import json
import os
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...core.config.config import cfg
from ...core.config.types import ModelProtocol
from ...core.model.model_manager import ModelManager
from ...core.model.model_metadata import ModelRouteError
from ..dependencies import get_model_manager

router = APIRouter(prefix="/proxy")

_PROXY_TOKEN_ENV = "CHATTREE_PROXY_TOKEN"
_GEMINI_PATH = re.compile(
    r"^(?:v1beta/)?models/(?P<model>.+):(?P<method>streamGenerateContent|generateContent)$"
)
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _verify_token(authorization: Optional[str]) -> None:
    expected = os.environ.get(_PROXY_TOKEN_ENV, "").strip()
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization[7:].strip() != expected:
        raise HTTPException(status_code=401, detail="invalid token")


def _split_proxy_model(model: str) -> Tuple[str, str]:
    provider_id, separator, model_id = model.partition("/")
    if not separator or not provider_id or not model_id:
        raise HTTPException(
            status_code=400,
            detail="proxy model must use provider_id/model_id",
        )
    return provider_id, model_id


def _request_protocol(path: str) -> str:
    normalized = path.strip("/")
    if normalized in {"chat/completions", "v1/chat/completions"}:
        return ModelProtocol.OPENAI_CHAT_COMPLETIONS.value
    if normalized in {"responses", "v1/responses"}:
        return ModelProtocol.OPENAI_RESPONSES.value
    if normalized in {"messages", "v1/messages"}:
        return ModelProtocol.ANTHROPIC_MESSAGES.value
    if _GEMINI_PATH.match(normalized):
        return ModelProtocol.GEMINI_GENERATE_CONTENT.value
    raise HTTPException(status_code=404, detail=f"unsupported model protocol path: {path}")


def _target_for_adapter(
    adapter: Any,
    protocol: str,
    model_id: str,
    path: str,
    request: Request,
    stream: bool,
) -> Tuple[str, Dict[str, str]]:
    if protocol == ModelProtocol.OPENAI_CHAT_COMPLETIONS.value:
        return adapter._url(adapter.route["endpoint"]), adapter._headers(stream=stream)
    if protocol == ModelProtocol.OPENAI_RESPONSES.value:
        return adapter._url(adapter.route["endpoint"]), adapter._headers(stream=stream)
    if protocol == ModelProtocol.ANTHROPIC_MESSAGES.value:
        return adapter._api_base() + adapter.route["endpoint"], adapter._headers()

    match = _GEMINI_PATH.match(path.strip("/"))
    if match is None:
        raise HTTPException(status_code=404, detail="invalid Gemini path")
    method = match.group("method")
    params = {key: value for key, value in request.query_params.items()}
    return (
        adapter._url(
            adapter.route["endpoint"].format(model=model_id).replace(
                ":generateContent",
                f":{method}",
            ),
            params,
        ),
        adapter._headers(stream=method == "streamGenerateContent"),
    )


async def _raw_response_body(
    response: httpx.Response,
    client: httpx.AsyncClient,
) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await response.aclose()
        await client.aclose()


@router.get("/models")
async def list_proxy_models(
    authorization: Optional[str] = Header(default=None),
    model_manager: ModelManager = Depends(get_model_manager),
) -> Dict[str, Any]:
    _verify_token(authorization)
    data: List[Dict[str, Any]] = []
    for provider_id, models in model_manager.model_list.items():
        provider = cfg.get_provider_config(provider_id) or {}
        if not provider.get("enabled", False):
            continue
        hidden = set(provider.get("hidden_models") or [])
        for model_id in models:
            if model_id in hidden:
                continue
            try:
                route = model_manager.get_route(provider_id, model_id)
            except ModelRouteError:
                continue
            data.append({
                "id": f"{provider_id}/{model_id}",
                "object": "model",
                "owned_by": provider.get("name") or provider_id,
                "routes": [{
                    "protocol": route["protocol"],
                    "endpoint": route["endpoint"],
                    "reasoning_profile": route.get("reasoning_profile") or {},
                    "capabilities": route.get("capabilities") or {},
                    "preferred": True,
                }],
            })
    return {"object": "list", "data": data}


@router.post("/{path:path}")
async def proxy_model_protocol(
    path: str,
    request: Request,
    model_manager: ModelManager = Depends(get_model_manager),
    authorization: Optional[str] = Header(default=None),
):
    _verify_token(authorization)
    protocol = _request_protocol(path)
    raw_body = await request.body()
    try:
        body = json.loads(raw_body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON request body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")

    if protocol == ModelProtocol.GEMINI_GENERATE_CONTENT.value:
        match = _GEMINI_PATH.match(path.strip("/"))
        proxy_model = match.group("model") if match else ""
    else:
        proxy_model = str(body.get("model") or "")
    provider_id, model_id = _split_proxy_model(proxy_model)

    try:
        adapter = model_manager.get_model(provider_id, model_id, is_async=True)
        route = model_manager.get_route(provider_id, model_id)
        if route["protocol"] != protocol:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"route protocol mismatch: model uses {route['protocol']}, "
                    f"request used {protocol}"
                ),
            )
    except ModelRouteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if adapter is None:
        raise HTTPException(status_code=503, detail="model adapter unavailable")

    if protocol != ModelProtocol.GEMINI_GENERATE_CONTENT.value:
        body["model"] = model_id
    target_url, target_headers = _target_for_adapter(
        adapter,
        protocol,
        model_id,
        path,
        request,
        bool(body.get("stream")),
    )
    if protocol != ModelProtocol.GEMINI_GENERATE_CONTENT.value and request.query_params:
        target_url = str(
            httpx.URL(target_url).copy_merge_params(
                request.query_params.multi_items()
            )
        )
    forwarded_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {
            *_HOP_BY_HOP,
            "authorization",
            "content-length",
            "host",
            "x-api-key",
            "x-goog-api-key",
        }
    }
    forwarded_headers.update(target_headers)
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))
    upstream_request = client.build_request(
        "POST",
        target_url,
        headers=forwarded_headers,
        content=payload,
    )
    try:
        response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    response_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _HOP_BY_HOP
    }
    return StreamingResponse(
        _raw_response_body(response, client),
        status_code=response.status_code,
        headers=response_headers,
        media_type=response.headers.get("content-type"),
    )
