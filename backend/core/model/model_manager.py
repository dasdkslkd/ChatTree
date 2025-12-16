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
        self.model_instances: Dict[ModelProvider, BaseProvider] = {}
        self.model_list: Dict[ModelProvider, List[str]] = {}
        self.current_model: ModelProvider = cfg.data.get('default_provider', ModelProvider.OPENAI)
        self.model_list = cfg.data.get('provider', {}).get(self.current_model, {}).get('models', [])
    
    def get_model(self, provider: Optional[ModelProvider] = None) -> Optional[BaseProvider]:
        """获取模型实例（延迟加载）"""
        provider = provider or self.current_model
        if provider in self.model_instances:
            return self.model_instances[provider]
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
        self.model_instances[provider] = instance
        return instance
    
    def set_current_model(self, provider: ModelProvider) -> bool:
        """设置默认模型"""
        if provider in self.PROVIDER_MAP:
            self.current_model = provider
            cfg.set_default_model(provider)
            return True
        return False
    
    def list_available_models(self, provider: ModelProvider) -> List[str]:
        """获取指定提供商的可用模型列表"""
        model = self.get_model(provider)
        if model:
            available_models = model.list_models()
            self.model_list[provider] = available_models
            cfg.data['provider'][provider]['models'] = available_models
            return available_models
        return []