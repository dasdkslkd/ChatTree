# model/manager.py - 动态提供商管理器
from importlib import import_module
from typing import Dict, Optional, Any, List
from .base import BaseProvider
from .model_metadata import ModelMetadata, resolve_metadata, resolve_provider_metadata
from ..config.types import APIFormat
from ..config.config import cfg
from ..utils.logger import setup_logger

logger = setup_logger('ModelManager')

class ModelManager:
    """动态提供商管理器 — 根据 api_format 选择对应的 Provider 类"""

    # api_format → (module_path, class_name)
    API_FORMAT_MAP = {
        APIFormat.CHAT_COMPLETIONS: ('.providers.openai_compatible', 'OpenAICompatibleProvider'),
        APIFormat.RESPONSES:        ('.providers.openai_compatible', 'OpenAICompatibleProvider'),
        APIFormat.ANTHROPIC:        ('.providers.anthropic_provider', 'AnthropicProvider'),
        APIFormat.GEMINI:           ('.providers.gemini_provider', 'GeminiProvider'),
    }

    def __init__(self):
        self.provider_instances: Dict[str, BaseProvider] = {}
        self.model_list: Dict[str, List[str]] = {}
        for provider_id, provider_config in cfg.get_all_providers().items():
            if provider_config.get('enabled', False):
                self.model_list[provider_id] = provider_config.get('models', [])

    def get_model_metadata(self, provider_id: str, model_name: str) -> ModelMetadata:
        """解析单个模型的元数据（按 provider 的 api_format + 模型名 + 用户覆盖）。"""
        provider_config = cfg.get_provider_config(provider_id) or {}
        api_format = provider_config.get('api_format', APIFormat.CHAT_COMPLETIONS)
        if hasattr(api_format, 'value'):
            api_format = api_format.value
        overrides = cfg.data.get('model_metadata') if isinstance(cfg.data, dict) else None
        return resolve_metadata(model_name, api_format, overrides)

    def get_provider_metadata(self, provider_id: str) -> Dict[str, ModelMetadata]:
        """批量解析一个 provider 下所有模型的元数据，返回 model_name -> 元数据。

        模型列表优先取已配置的 models；为空时回退到运行时缓存的 model_list。
        """
        provider_config = cfg.get_provider_config(provider_id) or {}
        api_format = provider_config.get('api_format', APIFormat.CHAT_COMPLETIONS)
        if hasattr(api_format, 'value'):
            api_format = api_format.value
        models = provider_config.get('models') or self.model_list.get(provider_id, [])
        overrides = cfg.data.get('model_metadata') if isinstance(cfg.data, dict) else None
        return resolve_provider_metadata(models, api_format, overrides)

    def get_model(self, provider: str, is_async: bool = False) -> Optional[BaseProvider]:
        """获取模型实例（延迟加载）"""
        if provider in self.provider_instances:
            cached = self.provider_instances[provider]
            cached_async = getattr(cached, '_is_async', False)
            # async/sync 不匹配时需要重建
            if cached_async != is_async:
                self.provider_instances.pop(provider)
            else:
                # 同步最新 config（auth 可能被 token 刷新修改或从磁盘重新加载）
                latest = cfg.get_provider_config(provider) or {}
                cached.config.update(latest)
                cached.config['is_async'] = is_async
                return cached
        return self._create_model_instance(provider, is_async)

    def _create_model_instance(self, provider: str, is_async: bool = False) -> Optional[BaseProvider]:
        """创建并缓存模型实例"""
        provider_config = cfg.get_provider_config(provider)
        if provider_config is None:
            logger.warning(f"提供商 {provider} 不存在")
            return None

        if not provider_config.get('enabled', False):
            logger.warning(f"提供商 {provider} 未启用")
            return None

        # 注入运行时参数
        config = dict(provider_config)
        config['is_async'] = is_async

        provider_class = self._get_provider_class(config)
        if provider_class is None:
            logger.error(f"不支持的 API 格式: {config.get('api_format')}")
            return None

        instance = provider_class(config)
        instance._is_async = is_async
        self.provider_instances[provider] = instance
        return instance

    def _get_provider_class(self, config: Dict[str, Any]) -> Optional[type[BaseProvider]]:
        """根据 api_format 返回对应的 Provider 类"""
        api_format = config.get('api_format', APIFormat.CHAT_COMPLETIONS)
        entry = self.API_FORMAT_MAP.get(api_format)
        if entry is None:
            return None

        module_path, class_name = entry
        module = import_module(module_path, package=__package__)
        return getattr(module, class_name)

    def _fetch_models_via_http(self, provider: str) -> List[str]:
        """HTTP 回退：通过候选 URL 列表请求 /v1/models（适用于代理和兼容 API）"""
        from .providers.model_fetch import fetch_models

        provider_config = cfg.get_provider_config(provider)
        if provider_config is None:
            return []

        try:
            models = fetch_models(
                base_url=provider_config.get('base_url', ''),
                api_key=provider_config.get('api_key', ''),
                models_url_override=provider_config.get('models_url_override'),
                custom_user_agent=provider_config.get('custom_user_agent'),
            )
            ids = [m["id"] for m in models]
            if ids:
                self.model_list[provider] = ids
                provider_config['models'] = ids
            return ids
        except Exception as e:
            logger.error(f"HTTP 回退获取模型列表失败: {e}")
            return []

    def list_available_models(self, provider: str) -> List[str]:
        """获取指定提供商的可用模型列表"""
        try:
            model = self.get_model(provider)
            if model:
                available_models = model.list_models()
                self.model_list[provider] = available_models
                provider_config = cfg.get_provider_config(provider)
                if provider_config is not None:
                    provider_config['models'] = available_models
                return available_models
        except Exception as e:
            logger.error(f"通过 provider 获取模型列表失败: {e}")

        # provider 实例化失败或 list_models 失败时，尝试 HTTP 回退
        return self._fetch_models_via_http(provider)
