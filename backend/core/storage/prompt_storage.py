import os
import json
from time import time
from typing import List, Dict, Any, Optional
from .base import StorageInterface
from ..utils.logger import setup_logger

logger = setup_logger('PromptStorage')

class PromptStorage(StorageInterface):
    def __init__(self, storage_dir: str = "data/prompts"):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)
        self.index_file = os.path.join(self.storage_dir, "index.json")
        self._load_index()
        logger.info(f"Prompt存储初始化完成，目录: {self.storage_dir}")

    def _load_index(self):
        """加载索引"""
        if os.path.exists(self.index_file):
            with open(self.index_file, 'r', encoding='utf-8') as f:
                self.index = json.load(f)
        else:
            self.index = {}
            self._save_index()

    def _save_index(self):
        """保存索引"""
        with open(self.index_file, 'w', encoding='utf-8') as f:
            json.dump(self.index, f, indent=2, ensure_ascii=False)
        logger.debug("Prompt索引保存完成")

    def save(self, data: Dict[str, Any]):
        """保存数据"""
        prompt_id = data["id"]
        file_path = os.path.join(self.storage_dir, f"{prompt_id}.txt")

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(data["content"])

        # 更新索引
        self.index[prompt_id] = {
            "id": prompt_id,
            "title": data.get("title", ""),
            "updated_at": int(time())
        }
        self._save_index()
        logger.debug(f"Prompt {prompt_id} 保存完成")

    def load(self, id: str) -> Optional[str]:
        """加载数据"""
        file_path = os.path.join(self.storage_dir, f"{id}.txt")
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return content
    
    def list(self) -> List[Dict[str, str]]:
        """列出"""
        self._load_index()
        return list(self.index.values())
    
    def delete(self, id: str):
        """删除数据"""
        file_path = os.path.join(self.storage_dir, f"{id}.txt")
        if os.path.exists(file_path):
            os.remove(file_path)
        
        if id in self.index:
            del self.index[id]
            self._save_index()
        logger.info(f"Prompt {id} 已删除")
    
    def exists(self, id: str) -> bool:
        """检查数据是否存在"""
        return id in self.index