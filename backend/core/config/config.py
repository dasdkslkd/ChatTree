# config.py - 配置管理（动态提供商）
import os
import json
from pathlib import Path
from typing import Dict, Any, Optional, List
from backend.core.persistence.home import resolve_chattree_home
from .types import ModelProviderConfig, APIFormat

# 旧的预设提供商ID列表，用于迁移检测
_LEGACY_PROVIDER_IDS = {
    'openai', 'azure', 'gemini', 'ollama', 'deepseek',
    'anthropic', 'groq', 'local', 'nvidia'
}

# 新提供商的默认配置模板
_DEFAULT_PROVIDER_TEMPLATE: ModelProviderConfig = {
    'name': '',
    'models': [],
    'api_key': '',
    'base_url': '',
    'organization': '',
    'project': '',
    'api_format': APIFormat.CHAT_COMPLETIONS,
    'hidden_models': [],
    'enabled': False,
    'default_model': '',
}

class Config:
    """配置管理器"""

    def __init__(self, config_path: str | None = None):
        self.config_path = str(
            Path(config_path)
            if config_path is not None
            else resolve_chattree_home() / "config.json"
        )
        self.data = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """加载配置，如果检测到旧格式则重置"""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # 检测旧格式（含有预设提供商枚举key）并重置
            if self._is_legacy_config(data):
                data = self._fresh_config()
                self._save_data(data)
            return data
        return self._fresh_config()

    def _is_legacy_config(self, data: Dict[str, Any]) -> bool:
        """检测是否为旧的枚举预设格式"""
        providers = data.get('provider', {})
        return any(pid in _LEGACY_PROVIDER_IDS for pid in providers.keys())

    @staticmethod
    def _fresh_config() -> Dict[str, Any]:
        """返回空白配置"""
        return {'provider': {}, 'default_provider': ''}

    def _save_data(self, data: Dict[str, Any]):
        """直接写入指定数据"""
        os.makedirs(os.path.dirname(self.config_path) or '.', exist_ok=True)
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def save(self):
        """保存当前配置到磁盘"""
        self._save_data(self.data)

    def get_provider_config(self, provider: str) -> Optional[ModelProviderConfig]:
        """获取提供商配置，不存在则返回 None"""
        return self.data['provider'].get(provider)

    def get_all_providers(self) -> Dict[str, ModelProviderConfig]:
        """获取所有已配置的提供商"""
        return self.data.get('provider', {})

    def add_provider(self, provider_id: str, config: Dict[str, Any]):
        """添加新提供商"""
        # 合并默认模板，确保所有字段存在
        merged = {**_DEFAULT_PROVIDER_TEMPLATE, **config}
        self.data['provider'][provider_id] = merged
        self.save()

    def delete_provider(self, provider_id: str) -> bool:
        """删除提供商，返回是否成功"""
        if provider_id not in self.data['provider']:
            return False
        del self.data['provider'][provider_id]
        # 如果删除的是默认提供商，清空默认
        if self.data.get('default_provider') == provider_id:
            providers = list(self.data['provider'].keys())
            self.data['default_provider'] = providers[0] if providers else ''
        self.save()
        return True

    def set_default_provider(self, provider_id: str):
        """设置默认提供商"""
        self.data['default_provider'] = provider_id
        self.save()

cfg = Config()
