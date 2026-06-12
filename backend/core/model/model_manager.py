# model/manager.py - 动态提供商管理器
from importlib import import_module
from typing import Dict, Optional, Any, List
from .base import BaseProvider
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

    def get_model(self, provider: str, is_async: bool = False) -> Optional[BaseProvider]:
        """获取模型实例（延迟加载）"""
        if provider in self.provider_instances:
            cached = self.provider_instances[provider]
            cached_async = getattr(cached, '_is_async', False)
            # async/sync 不匹配时需要重建
            if cached_async != is_async:
                self.provider_instances.pop(provider)
            else:
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
        """HTTP 回退：直接请求 /v1/models 获取模型列表（适用于代理和兼容 API）"""
        import urllib.request
        import json as _json

        provider_config = cfg.get_provider_config(provider)
        if provider_config is None:
            return []

        api_key = provider_config.get('api_key', '')
        base_url = provider_config.get('base_url', '')
        if not base_url:
            return []

        url = base_url.rstrip('/')
        if url.endswith('/v1'):
            url += '/models'
        else:
            url += '/v1/models'

        try:
            req = urllib.request.Request(url, headers={
                'Authorization': f'Bearer {api_key}',
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())
            models = [m['id'] for m in data.get('data', [])]
            if models:
                self.model_list[provider] = models
                provider_config['models'] = models
            return models
        except Exception as e:
            logger.error(f"HTTP 回退获取模型列表失败 ({url}): {e}")
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
