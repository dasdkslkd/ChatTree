# storage/json_storage.py - JSON文件存储实现
import os
import json
from typing import List, Dict, Any, Optional
from .base import StorageInterface
from ..utils.logger import setup_logger

logger = setup_logger('ChatStorage')

class ChatStorage(StorageInterface):
    """JSON文件存储"""
    
    def __init__(self, storage_dir: str = "data/conversations"):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)
        self.index_file = os.path.join(self.storage_dir, "index.json")
        self._load_index()
        logger.info(f"Chat存储初始化完成，目录: {self.storage_dir}")
    
    def _load_index(self):
        """加载索引"""
        if os.path.exists(self.index_file):
            with open(self.index_file, 'r', encoding='utf-8') as f:
                self.index = json.load(f)
        else:
            self.index = {}
    
    def _save_index(self):
        """保存索引"""
        with open(self.index_file, 'w', encoding='utf-8') as f:
            json.dump(self.index, f, indent=2, ensure_ascii=False)
        logger.debug("Chat索引保存完成")
    
    def save(self, data: Dict[str, Any]):
        """保存对话"""
        conversation_id = data["metadata"]["id"]
        file_path = os.path.join(self.storage_dir, f"{conversation_id}.json")
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        # 更新索引
        self.index[conversation_id] = {
            "id": conversation_id,
            "title": data["metadata"].get("title", ""),
            "updated_at": data["metadata"]["updated_at"]
        }
        self._save_index()
        logger.debug(f"对话 {conversation_id} 保存完成")
    
    def load(self, id: str) -> Optional[Dict[str, Any]]:
        """加载对话"""
        file_path = os.path.join(self.storage_dir, f"{id}.json")
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None
    
    def list(self) -> List[Dict[str, str]]:
        """列出所有对话"""
        self._load_index()
        return list(self.index.values())
    
    def delete(self, id: str):
        """删除对话"""
        file_path = os.path.join(self.storage_dir, f"{id}.json")
        if os.path.exists(file_path):
            os.remove(file_path)
        
        if id in self.index:
            del self.index[id]
            self._save_index()
        logger.info(f"对话 {id} 已删除")
    
    def exists(self, id: str) -> bool:
        """检查对话是否存在"""
        return id in self.index