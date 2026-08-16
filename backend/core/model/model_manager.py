# model/manager.py - 模型级协议路由与适配器管理
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

from .base import BaseProvider
from .model_metadata import (
    ModelMetadata,
    ModelRouteError,
    resolve_metadata,
    resolve_provider_metadata,
    resolve_route,
)
from .providers.anthropic_provider import AnthropicProvider
from .providers.gemini_provider import GeminiProvider
from .providers.openai_compatible import OpenAICompatibleProvider
from ..config.config import cfg
from ..config.types import ModelProtocol, ModelRoute
from ..perf import get_profiler
from ..utils.logger import setup_logger

logger = setup_logger("ModelManager")


class ModelManager:
    """把供应商连接与具体模型的线协议分开管理。"""

    PROTOCOL_ADAPTERS = {
        ModelProtocol.OPENAI_CHAT_COMPLETIONS.value: OpenAICompatibleProvider,
        ModelProtocol.OPENAI_RESPONSES.value: OpenAICompatibleProvider,
        ModelProtocol.ANTHROPIC_MESSAGES.value: AnthropicProvider,
        ModelProtocol.GEMINI_GENERATE_CONTENT.value: GeminiProvider,
    }

    _PROTOCOL_ENDPOINTS = {
        ModelProtocol.OPENAI_CHAT_COMPLETIONS.value: "/chat/completions",
        ModelProtocol.OPENAI_RESPONSES.value: "/responses",
        ModelProtocol.ANTHROPIC_MESSAGES.value: "/v1/messages",
        ModelProtocol.GEMINI_GENERATE_CONTENT.value: "/models/{model}:generateContent",
    }

    def __init__(self):
        self.provider_instances: Dict[Tuple[str, str, bool], BaseProvider] = {}
        self._detected_routes: Dict[
            Tuple[str, str, str], Tuple[ModelRoute, str]
        ] = {}
        self.model_list: Dict[str, List[str]] = {}
        for provider_id, provider_config in cfg.get_all_providers().items():
            if provider_config.get("enabled", False):
                self.model_list[provider_id] = provider_config.get("models", [])

    def get_route(self, provider_id: str, model_name: str) -> ModelRoute:
        provider_config = cfg.get_provider_config(provider_id)
        if provider_config is None:
            raise ModelRouteError(f"供应商 {provider_id} 不存在")
        if not provider_config.get("enabled", False):
            raise ModelRouteError(f"供应商 {provider_id} 未启用")
        base_url = str(provider_config.get("base_url") or "").rstrip("/")
        detected = self._detected_routes.get((provider_id, model_name, base_url))
        if detected:
            return detected[0]
        route, resolved_base = self._resolve_route(provider_config, provider_id, model_name)
        self._detected_routes[(provider_id, model_name, base_url)] = (route, resolved_base)
        return route

    def get_model_metadata(self, provider_id: str, model_name: str) -> ModelMetadata:
        return resolve_metadata(self.get_route(provider_id, model_name))

    def get_provider_metadata(self, provider_id: str) -> Dict[str, ModelMetadata]:
        provider_config = cfg.get_provider_config(provider_id) or {}
        models = provider_config.get("models") or self.model_list.get(provider_id, [])
        routes = [self.get_route(provider_id, model) for model in models]
        return resolve_provider_metadata(routes)

    def get_model(
        self,
        provider: str,
        model: str,
        is_async: bool = False,
    ) -> Optional[BaseProvider]:
        profiler = get_profiler()
        provider_config = cfg.get_provider_config(provider)
        if provider_config is None:
            raise ModelRouteError(f"供应商 {provider} 不存在")
        if not provider_config.get("enabled", False):
            raise ModelRouteError(f"供应商 {provider} 未启用")
        base_url = str(provider_config.get("base_url") or "").rstrip("/")
        cache_key = (provider, model, base_url)
        with profiler.span("model.route_resolve"):
            detected = self._detected_routes.get(cache_key)
            if detected:
                route, resolved_base = detected
            else:
                route, resolved_base = self._resolve_route(provider_config, provider, model)
                self._detected_routes[cache_key] = (route, resolved_base)
        cache_key = (provider, route["route_id"], is_async)
        if cache_key in self.provider_instances:
            with profiler.span("model.instance_cached"):
                cached = self.provider_instances[cache_key]
                latest = dict(cfg.get_provider_config(provider) or {})
                cached.config.clear()
                cached.config.update(latest)
                if resolved_base:
                    cached.config["base_url"] = resolved_base
                cached.config["model_transport"] = dict(cfg.data["model_transport"])
                cached.config["is_async"] = is_async
                cached.route = route
                return cached
        with profiler.span("model.instance_create"):
            return self._create_model_instance(provider, route, is_async, resolved_base)

    def _route_from_entry(
        self,
        provider: str,
        model: str,
        entry: Dict[str, object],
    ) -> ModelRoute:
        """从持久化的探测结果重建协议路由。"""
        route = resolve_route(provider, model)
        protocol = str(entry["protocol"])
        endpoint = str(entry.get("endpoint") or self._PROTOCOL_ENDPOINTS[protocol]).format(
            model=urllib.parse.quote(model, safe=""),
        )
        detected = dict(route)
        detected["protocol"] = protocol
        detected["endpoint"] = endpoint
        if protocol != route["protocol"] or endpoint != route["endpoint"]:
            detected["route_id"] = f"{route['route_id']}:detected:{protocol}:{endpoint}"
        return detected

    def _resolve_route(
        self,
        provider_config: Dict[str, object],
        provider: str,
        model: str,
    ) -> Tuple[ModelRoute, str]:
        """路由优先级：持久化探测结果 > 供应商级 api_format > 模型元数据默认。"""
        entry = (provider_config.get("model_routes") or {}).get(model)
        if isinstance(entry, dict) and entry.get("protocol"):
            route = self._route_from_entry(provider, model, entry)
            resolved_base = str(entry.get("base_url") or provider_config.get("base_url") or "")
        else:
            route = resolve_route(provider, model)
            api_format = provider_config.get("api_format")
            if isinstance(api_format, str) and api_format in self._PROTOCOL_ENDPOINTS:
                route = self._route_from_entry(provider, model, {"protocol": api_format})
            resolved_base = str(provider_config.get("base_url") or "")
        return route, resolved_base

    def _probe_route(
        self,
        provider: str,
        model: str,
        route: ModelRoute,
    ) -> Tuple[ModelRoute, str]:
        """真实请求协议失败时探测正确格式并持久化到供应商配置。"""
        provider_config = cfg.get_provider_config(provider)
        if provider_config is None:
            return route, ""
        return self._probe_api_format(provider_config, route, model)

    def _probe_api_format(
        self,
        provider_config: Dict[str, object],
        route: ModelRoute,
        model: str,
    ) -> Tuple[ModelRoute, str]:
        """按元数据默认协议优先，用短请求确认真实 URL 和 API format。"""
        configured_base = str(provider_config.get("base_url") or "").rstrip("/")
        if not configured_base:
            return route, configured_base

        root_base = (
            configured_base[:-3]
            if configured_base.endswith("/v1")
            else configured_base
        )
        protocols = [
            route["protocol"],
            *(
                protocol
                for protocol in self._PROTOCOL_ENDPOINTS
                if protocol != route["protocol"]
            ),
        ]
        timeout = int(float((cfg.data.get("model_transport") or {}).get(
            "connect_timeout_seconds", 10,
        )))
        api_key = str(provider_config.get("api_key") or "ollama")

        for protocol in protocols:
            endpoint_template = (
                route["endpoint"]
                if protocol == route["protocol"]
                else self._PROTOCOL_ENDPOINTS[protocol]
            )
            endpoint = endpoint_template.format(
                model=urllib.parse.quote(model, safe=""),
            )
            if protocol == ModelProtocol.ANTHROPIC_MESSAGES.value:
                candidates = [(root_base, endpoint)]
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                }
                body = {
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                }
            elif protocol == ModelProtocol.GEMINI_GENERATE_CONTENT.value:
                gemini_base = (
                    configured_base
                    if configured_base.endswith(("/v1", "/v1beta"))
                    else f"{root_base}/v1beta"
                )
                candidates = [(gemini_base, endpoint)]
                headers = {"x-goog-api-key": api_key}
                body = {
                    "contents": [{
                        "role": "user",
                        "parts": [{"text": "ping"}],
                    }],
                    "generationConfig": {"maxOutputTokens": 1},
                }
            else:
                candidates = [(configured_base, endpoint)]
                if configured_base == root_base:
                    candidates.append((f"{root_base}/v1", endpoint))
                headers = {"Authorization": f"Bearer {api_key}"}
                body = (
                    {
                        "model": model,
                        "input": "ping",
                        "stream": False,
                        "max_output_tokens": 1,
                    }
                    if protocol == ModelProtocol.OPENAI_RESPONSES.value
                    else {
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "stream": False,
                        "max_tokens": 1,
                    }
                )
            headers.update({
                "Content-Type": "application/json",
                "User-Agent": provider_config.get("custom_user_agent") or "ChatTree",
            })

            for resolved_base, resolved_endpoint in candidates:
                request = urllib.request.Request(
                    f"{resolved_base}{resolved_endpoint}",
                    data=json.dumps(body).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        content_type = str(response.headers.get("Content-Type") or "").lower()
                        raw = response.read()
                    if "json" not in content_type:
                        continue
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    valid = {
                        ModelProtocol.OPENAI_CHAT_COMPLETIONS.value:
                            isinstance(payload.get("choices"), list),
                        ModelProtocol.OPENAI_RESPONSES.value:
                            "output" in payload or "status" in payload,
                        ModelProtocol.ANTHROPIC_MESSAGES.value:
                            isinstance(payload.get("content"), list),
                        ModelProtocol.GEMINI_GENERATE_CONTENT.value:
                            isinstance(payload.get("candidates"), list),
                    }[protocol]
                    if not valid:
                        continue
                    detected = dict(route)
                    detected["protocol"] = protocol
                    detected["endpoint"] = resolved_endpoint
                    if protocol != route["protocol"] or resolved_endpoint != route["endpoint"]:
                        detected["route_id"] = (
                            f"{route['route_id']}:detected:{protocol}:{resolved_endpoint}"
                        )
                    logger.info(
                        "Model API format resolved: provider=%s model=%s protocol=%s base=%s",
                        route["provider_id"],
                        model,
                        protocol,
                        resolved_base,
                    )
                    provider_config.setdefault("model_routes", {})[model] = {
                        "protocol": protocol,
                        "endpoint": resolved_endpoint,
                        "base_url": resolved_base,
                    }
                    if resolved_base != configured_base:
                        provider_config["base_url"] = resolved_base
                    cfg.save()
                    return detected, resolved_base
                except urllib.error.HTTPError as exc:
                    error_body = exc.read().decode("utf-8", errors="replace")
                    if exc.code in (401, 403, 429):
                        if resolved_base != configured_base:
                            provider_config["base_url"] = resolved_base
                            cfg.save()
                        return route, resolved_base
                    if exc.code in (404, 405):
                        continue
                    if "protocol_not_supported" not in error_body.lower():
                        if 400 <= exc.code < 500:
                            return route, resolved_base
                        continue
                except urllib.error.URLError:
                    return route, configured_base

        return route, configured_base

    def _create_model_instance(
        self,
        provider: str,
        route: ModelRoute,
        is_async: bool,
        resolved_base: str = "",
    ) -> Optional[BaseProvider]:
        provider_config = cfg.get_provider_config(provider)
        if provider_config is None or not provider_config.get("enabled", False):
            return None
        provider_class = self.PROTOCOL_ADAPTERS.get(route["protocol"])
        if provider_class is None:
            raise ModelRouteError(f"未实现的模型协议: {route['protocol']}")

        config = dict(provider_config)
        if resolved_base:
            config["base_url"] = resolved_base
        config["model_transport"] = dict(cfg.data["model_transport"])
        config["is_async"] = is_async
        instance = provider_class(config, route)
        instance._route_probe = lambda current_route, model_name: self._probe_route(
            provider, model_name, current_route,
        )
        instance._is_async = is_async
        self.provider_instances[(provider, route["route_id"], is_async)] = instance
        return instance

    def _fetch_models_via_http(self, provider: str) -> List[str]:
        from .providers.model_fetch import fetch_models

        provider_config = cfg.get_provider_config(provider)
        if provider_config is None:
            return []
        try:
            models = fetch_models(
                base_url=provider_config.get("base_url", ""),
                api_key=provider_config.get("api_key", ""),
                models_url_override=provider_config.get("models_url_override"),
                custom_user_agent=provider_config.get("custom_user_agent"),
            )
            ids = [item["id"] for item in models]
            if ids:
                self.model_list[provider] = ids
                provider_config["models"] = ids
            return ids
        except Exception as exc:
            logger.error(f"HTTP 获取模型目录失败: {exc}")
            return []

    def list_available_models(self, provider: str) -> List[str]:
        provider_config = cfg.get_provider_config(provider) or {}
        auth = provider_config.get("auth") or {}
        if auth.get("subscription"):
            from ..auth import fetch_models_sync

            models = fetch_models_sync(auth)
            ids = [item["id"] for item in models]
            self.model_list[provider] = ids
            provider_config["models"] = ids
            return ids

        configured = provider_config.get("models") or []
        for model in configured:
            try:
                adapter = self.get_model(provider, model)
            except ModelRouteError:
                continue
            if adapter is not None:
                try:
                    models = adapter.list_models()
                    self.model_list[provider] = models
                    provider_config["models"] = models
                    return models
                except Exception as exc:
                    logger.warning(f"通过协议适配器获取模型目录失败: {exc}")
                break
        return self._fetch_models_via_http(provider)
