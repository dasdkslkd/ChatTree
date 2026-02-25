# storage/chat_storage.py - 修改为多文件存储
import os
import json
from typing import List, Dict, Any, Optional
from .base import StorageInterface
from ..utils.logger import setup_logger

logger = setup_logger('ChatStorage')

class ChatStorage(StorageInterface):
    """多文件JSON存储 - 每个节点独立文件"""
    
    def __init__(self, storage_dir: str = "data/conversations"):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)
        self.index_file = os.path.join(self.storage_dir, "index.json")
        self._load_index()
        logger.info(f"Chat存储初始化完成，目录: {self.storage_dir}")
    
    def _load_index(self):
        """加载对话索引"""
        if os.path.exists(self.index_file):
            with open(self.index_file, 'r', encoding='utf-8') as f:
                self.index = json.load(f)
        else:
            self.index = {}
            self._save_index()
    
    def _save_index(self):
        """保存对话索引"""
        with open(self.index_file, 'w', encoding='utf-8') as f:
            json.dump(self.index, f, indent=2, ensure_ascii=False)
        logger.debug("对话索引保存完成")
    
    def _get_conversation_dir(self, conversation_id: str) -> str:
        """获取对话目录路径"""
        return os.path.join(self.storage_dir, conversation_id)
    
    def _get_nodes_dir(self, conversation_id: str) -> str:
        """获取节点存储目录"""
        return os.path.join(self._get_conversation_dir(conversation_id), "nodes")
    
    def _get_metadata_path(self, conversation_id: str) -> str:
        """获取元数据文件路径"""
        return os.path.join(self._get_conversation_dir(conversation_id), "metadata.json")
    
    def _get_node_path(self, conversation_id: str, node_id: str) -> str:
        """获取节点文件路径"""
        return os.path.join(self._get_nodes_dir(conversation_id), f"{node_id}.json")
    
    def save(self, data: Dict[str, Any]):
        """保存对话（多文件结构）"""
        conversation_id = data["metadata"]["id"]
        conv_dir = self._get_conversation_dir(conversation_id)
        nodes_dir = self._get_nodes_dir(conversation_id)
        
        # 创建对话目录结构
        os.makedirs(conv_dir, exist_ok=True)
        os.makedirs(nodes_dir, exist_ok=True)
        
        # 1. 保存元数据
        metadata = {
            "metadata": data["metadata"],
            "root_node_id": data.get("root_node_id"),
            "current_node_id": data.get("current_node_id")
        }
        with open(self._get_metadata_path(conversation_id), 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        
        # 2. 清理已删除的节点文件
        existing_files = os.listdir(nodes_dir)
        existing_node_ids = {f.replace('.json', '') for f in existing_files if f.endswith('.json')}
        current_node_ids = {node["id"] for node in data["nodes"]}
        
        # 找出需要删除的节点（存在文件中但不在当前数据中）
        nodes_to_delete = existing_node_ids - current_node_ids
        
        for node_id in nodes_to_delete:
            node_path = self._get_node_path(conversation_id, node_id)
            try:
                os.remove(node_path)
                logger.debug(f"删除已移除的节点文件: {node_id}")
            except Exception as e:
                logger.error(f"删除节点文件失败 {node_id}: {e}")
        
        # 3. 保存当前节点文件
        for node in data["nodes"]:
            node_path = self._get_node_path(conversation_id, node["id"])
            with open(node_path, 'w', encoding='utf-8') as f:
                json.dump(node, f, indent=2, ensure_ascii=False)
        
        # 4. 更新主索引
        self.index[conversation_id] = {
            "id": conversation_id,
            "title": data["metadata"].get("title", ""),
            "updated_at": data["metadata"]["updated_at"],
            "node_count": len(data["nodes"])
        }
        self._save_index()
        
        logger.debug(f"对话 {conversation_id} 保存完成，共 {len(data['nodes'])} 个节点，清理了 {len(nodes_to_delete)} 个旧节点")
    
    def load(self, id: str) -> Optional[Dict[str, Any]]:
        """加载对话"""
        if not self.exists(id):
            return None
                
        try:
            # 1. 加载元数据
            metadata_path = self._get_metadata_path(id)
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata_data = json.load(f)
            
            # 2. 加载所有节点
            nodes_dir = self._get_nodes_dir(id)
            nodes = []
            
            # 读取nodes目录下所有json文件
            if os.path.exists(nodes_dir):
                for filename in os.listdir(nodes_dir):
                    if filename.endswith('.json'):
                        node_path = os.path.join(nodes_dir, filename)
                        with open(node_path, 'r', encoding='utf-8') as f:
                            node = json.load(f)
                            nodes.append(node)
            
            return {
                "metadata": metadata_data["metadata"],
                "nodes": nodes,
                "current_node_id": metadata_data.get("current_node_id"),
                "root_node_id": metadata_data.get("root_node_id")
            }
            
        except Exception as e:
            logger.error(f"加载对话 {id} 失败: {e}", exc_info=True)
            return None
    
    def list(self) -> List[Dict[str, Any]]:
        """列出所有对话（从索引快速获取）"""
        self._load_index()
        # 返回索引的基本信息
        result = []
        for conv_id, info in self.index.items():
            result.append({
                "id": conv_id,
                "title": info.get("title", ""),
                "updated_at": info.get("updated_at", 0),
                "node_count": str(info.get("node_count", 0))
            })
        return result
    
    def delete(self, id: str):
        """删除对话"""
        if not self.exists(id):
            return
        
        # 删除整个对话目录及其所有文件
        import shutil
        conv_dir = self._get_conversation_dir(id)
        if os.path.exists(conv_dir):
            shutil.rmtree(conv_dir)
        
        # 从索引中移除
        if id in self.index:
            del self.index[id]
            self._save_index()
        
        logger.info(f"对话 {id} 及其所有节点已删除")
    
    def exists(self, id: str) -> bool:
        """检查对话是否存在"""
        return id in self.index and os.path.exists(self._get_conversation_dir(id))