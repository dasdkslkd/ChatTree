# model/manager.py - 支持延迟加载的模型管理器
from typing import Dict, Optional, Any, List
from .base import BaseProvider
from .providers.openai_compatible import OpenAICompatibleProvider
from .providers.gemini_provider import GeminiProvider
from ..config.types import ModelProvider
from ..config.config import cfg
from ..utils.logger import setup_logger

logger = setup_logger('ModelManager')

class ModelManager:
    """支持延迟加载的模型管理器"""
    
    # 直接映射类，避免字符串导入
    PROVIDER_MAP = {
        ModelProvider.OPENAI: OpenAICompatibleProvider,
        ModelProvider.AZURE: OpenAICompatibleProvider,
        ModelProvider.OLLAMA: OpenAICompatibleProvider,
        ModelProvider.DEEPSEEK: OpenAICompatibleProvider,
        ModelProvider.GEMINI: GeminiProvider,
        ModelProvider.GROQ: OpenAICompatibleProvider,
    }
    
    def __init__(self):
        self.provider_instances: Dict[ModelProvider, BaseProvider] = {}
        self.model_list: Dict[ModelProvider, List[str]] = {}
        for provider in ModelProvider:
            provider_config = cfg.get_provider_config(provider)
            if provider_config.get('enabled', True):
                self.model_list[provider] = provider_config.get('models', [])
    
    def get_model(self, provider: ModelProvider) -> Optional[BaseProvider]:
        """获取模型实例（延迟加载）"""
        if provider in self.provider_instances:
            return self.provider_instances[provider]
        return self._create_model_instance(provider)
    
    def _create_model_instance(self, provider: ModelProvider) -> Optional[BaseProvider]:
        """创建并缓存模型实例"""
        model_configs = cfg.data.get('provider', {})
        provider_config = model_configs.get(provider)
        
        if not provider_config or not provider_config.get('enabled', True):
            logger.warning(f"提供商 {provider} 未启用或配置缺失")
            return None
        
        provider_class = self.PROVIDER_MAP.get(provider)
        if not provider_class:
            logger.error(f"未知的提供商类型: {provider}")
            return None
        
        instance = provider_class(provider_config)
        self.provider_instances[provider] = instance
        return instance
    
    def list_available_models(self, provider: ModelProvider) -> List[str]:
        """获取指定提供商的可用模型列表"""
        model = self.get_model(provider)
        if model:
            available_models = model.list_models()
            self.model_list[provider] = available_models
            cfg.data['provider'][provider]['models'] = available_models
            return available_models
        return []