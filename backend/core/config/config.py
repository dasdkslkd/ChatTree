# config.py - 更新配置管理
import os
import json
from typing import Dict, Any, Optional
from .types import ChatConfig, ModelProviderConfig, ModelProvider

DEFAULT_CONFIG: ChatConfig = {
    "save_history": True,
    "max_history_messages": 50,
    "system_prompt": ""
}

# 多模型默认配置
DEFAULT_MODELS_CONFIG: Dict[ModelProvider, ModelProviderConfig] = {
    ModelProvider.OPENAI: {
        'name': 'openai',
        'models': [],
        'api_key': '',
        'base_url': '',
        'organization': '',
        'project': '',
        'enabled': True,
        'default_model': 'gpt-3'
    },
    ModelProvider.OLLAMA: {
        'name': 'ollama',
        'models': [],
        'api_key': '',
        'base_url': '',
        'organization': '',
        'project': '',
        'enabled': True,
        'default_model': ''
    },
    ModelProvider.DEEPSEEK: {
        'name': 'deepseek',
        'models': [],
        'api_key': '',
        'base_url': '',
        'organization': '',
        'project': '',
        'enabled': True,
        'default_model': ''
    },

}

class Config:
    """配置管理器"""
    
    def __init__(self, config_path: str = "data/config.json"):
        self.config_path = config_path
        self.data = self._load_config()
            
    def _load_config(self) -> Dict[str, Any]:
        """加载配置"""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {'provider': DEFAULT_MODELS_CONFIG.copy(), 'default_provider': ModelProvider.OPENAI}
    
    def save(self):
        """保存配置"""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
    
    def get_model_config(self, model: ModelProvider) -> Optional[ModelProviderConfig]:
        """获取模型配置"""
        return self.data['provider'].get(model)
    
    def add_model_config(self, model: ModelProvider, config: ModelProviderConfig):
        """添加模型配置"""
        self.data['provider'][model] = config
        self.save()
    
    def set_default_model(self, provider: ModelProvider):
        """设置默认模型ID"""
        self.data['default_provider'] = provider
        self.save()

cfg = Config()