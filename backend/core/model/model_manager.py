# model/manager.py - 模型级协议路由与适配器管理
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

    def __init__(self):
        self.provider_instances: Dict[Tuple[str, str, bool], BaseProvider] = {}
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

        return resolve_route(provider_id, model_name)

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
        route = self.get_route(provider, model)
        cache_key = (provider, route["route_id"], is_async)
        if cache_key in self.provider_instances:
            cached = self.provider_instances[cache_key]
            latest = cfg.get_provider_config(provider) or {}
            cached.config.clear()
            cached.config.update(latest)
            cached.config["is_async"] = is_async
            cached.route = route
            return cached
        return self._create_model_instance(provider, route, is_async)

    def _create_model_instance(
        self,
        provider: str,
        route: ModelRoute,
        is_async: bool,
    ) -> Optional[BaseProvider]:
        provider_config = cfg.get_provider_config(provider)
        if provider_config is None or not provider_config.get("enabled", False):
            return None
        provider_class = self.PROTOCOL_ADAPTERS.get(route["protocol"])
        if provider_class is None:
            raise ModelRouteError(f"未实现的模型协议: {route['protocol']}")

        config = dict(provider_config)
        config["is_async"] = is_async
        instance = provider_class(config, route)
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
