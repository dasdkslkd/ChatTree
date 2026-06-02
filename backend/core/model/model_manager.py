# model/manager.py - 支持延迟加载的模型管理器
from importlib import import_module
from typing import Dict, Optional, Any, List
from .base import BaseProvider
from ..config.types import ModelProvider
from ..config.config import cfg
from ..utils.logger import setup_logger

logger = setup_logger('ModelManager')

class ModelManager:
    """支持延迟加载的模型管理器"""
    
    # 映射到模块和类，只有实际实例化时才导入 provider。
    PROVIDER_MAP = {
        ModelProvider.OPENAI: ('.providers.openai_compatible', 'OpenAICompatibleProvider'),
        ModelProvider.AZURE: ('.providers.openai_compatible', 'OpenAICompatibleProvider'),
        ModelProvider.OLLAMA: ('.providers.openai_compatible', 'OpenAICompatibleProvider'),
        ModelProvider.DEEPSEEK: ('.providers.openai_compatible', 'OpenAICompatibleProvider'),
        ModelProvider.GEMINI: ('.providers.gemini_provider', 'GeminiProvider'),
        ModelProvider.GROQ: ('.providers.openai_compatible', 'OpenAICompatibleProvider'),
        ModelProvider.ANTHROPIC: ('.providers.openai_compatible', 'OpenAICompatibleProvider'),
        ModelProvider.LOCAL: ('.providers.openai_compatible', 'OpenAICompatibleProvider'),
        ModelProvider.NVIDIA: ('.providers.openai_compatible', 'OpenAICompatibleProvider'),
    }
    
    def __init__(self):
        self.provider_instances: Dict[ModelProvider, BaseProvider] = {}
        self.model_list: Dict[ModelProvider, List[str]] = {}
        for provider in ModelProvider:
            provider_config = cfg.get_provider_config(provider)
            if provider_config.get('enabled', True):
                self.model_list[provider] = provider_config.get('models', [])
    
    def get_model(self, provider: ModelProvider, is_async: bool = False) -> Optional[BaseProvider]:
        """获取模型实例（延迟加载）"""
        if provider in self.provider_instances:
            if is_async and not getattr(self.provider_instances[provider], 'client', None).__class__.__name__.startswith('Async'):
                # 如果需要异步实例但现有实例是同步的，重新创建
                self.provider_instances.pop(provider)
            else:
                return self.provider_instances[provider]
        return self._create_model_instance(provider, is_async)
    
    def _create_model_instance(self, provider: ModelProvider, is_async: bool = False) -> Optional[BaseProvider]:
        """创建并缓存模型实例"""
        model_configs = cfg.data.get('provider', {})
        provider_config = model_configs.get(provider)
        if provider_config is not None:
            provider_config['is_async'] = is_async
        
        if not provider_config or not provider_config.get('enabled', True):
            logger.warning(f"提供商 {provider} 未启用或配置缺失")
            return None
        
        provider_class = self._get_provider_class(provider)
        if provider_class is None:
            logger.error(f"未知的提供商类型: {provider}")
            return None
        
        instance = provider_class(provider_config)
        self.provider_instances[provider] = instance
        return instance

    def _get_provider_class(self, provider: ModelProvider) -> Optional[type[BaseProvider]]:
        provider_entry = self.PROVIDER_MAP.get(provider)
        if provider_entry is None:
            return None

        module_path, class_name = provider_entry
        module = import_module(module_path, package=__package__)
        return getattr(module, class_name)
    
    def list_available_models(self, provider: ModelProvider) -> List[str]:
        """获取指定提供商的可用模型列表"""
        model = self.get_model(provider)
        if model:
            available_models = model.list_models()
            self.model_list[provider] = available_models
            cfg.data['provider'][provider]['models'] = available_models
            return available_models
        return []