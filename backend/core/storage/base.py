# storage/base.py - 存储接口
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class StorageInterface(ABC):
    """存储接口"""
    
    @abstractmethod
    def save(self, data: Dict[str, Any]):
        """保存数据"""
        pass
    
    @abstractmethod
    def load(self, id: str) -> Optional[Any]:
        """加载数据"""
        pass
    
    @abstractmethod
    def list(self) -> List[Dict[str, str]]:
        """列出"""
        pass
    
    @abstractmethod
    def delete(self, id: str):
        """删除"""
        pass

    @abstractmethod
    def exists(self, id: str) -> bool:
        """检查存在"""
        pass