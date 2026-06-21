# storage/chat_storage.py - 修改为多文件存储
import os
import json
from typing import List, Dict, Any, Optional
from .base import StorageInterface
from .atomic import atomic_write_json, atomic_write_bytes
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
        atomic_write_json(self.index_file, self.index)
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
        """保存对话（多文件结构）

        非破坏式：只 **追加/覆盖** data["nodes"] 中的节点文件，并 **仅** 删除
        data["deleted_node_ids"] 中显式标记删除的节点。绝不根据 listdir 与内存
        节点集做 diff 来删除——那会在两路流并发保存同一对话时，把对方刚写入的
        新节点当成“孤儿”误删（数据丢失竞态）。
        """
        conversation_id = data["metadata"]["id"]
        conv_dir = self._get_conversation_dir(conversation_id)
        nodes_dir = self._get_nodes_dir(conversation_id)

        # 创建对话目录结构
        os.makedirs(conv_dir, exist_ok=True)
        os.makedirs(nodes_dir, exist_ok=True)

        # 1. 保存元数据（原子）
        metadata = {
            "metadata": data["metadata"],
            "root_node_id": data.get("root_node_id"),
            "current_node_id": data.get("current_node_id")
        }
        atomic_write_json(self._get_metadata_path(conversation_id), metadata)

        # 2. 仅删除显式标记删除的节点文件（不做 listdir diff）
        deleted_node_ids = data.get("deleted_node_ids", []) or []
        for node_id in deleted_node_ids:
            node_path = self._get_node_path(conversation_id, node_id)
            try:
                os.remove(node_path)
                logger.debug(f"删除节点文件: {node_id}")
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.error(f"删除节点文件失败 {node_id}: {e}")

        # 3. 追加/覆盖当前节点文件（原子）
        for node in data["nodes"]:
            node_path = self._get_node_path(conversation_id, node["id"])
            atomic_write_json(node_path, node)

        # 4. 更新主索引（原子）
        self.index[conversation_id] = {
            "id": conversation_id,
            "title": data["metadata"].get("title", ""),
            "updated_at": data["metadata"]["updated_at"],
            "node_count": len(data["nodes"]),
            "model_id": data["metadata"].get("model_id", ""),
            "provider_id": data["metadata"].get("provider_id", ""),
            "workspace": data["metadata"].get("workspace"),
            "current_node_id": data.get("current_node_id"),
        }
        self._save_index()

        logger.debug(f"对话 {conversation_id} 保存完成，共 {len(data['nodes'])} 个节点，删除 {len(deleted_node_ids)} 个节点")
    
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
                "node_count": str(info.get("node_count", 0)),
                "model_id": info.get("model_id", ""),
                "provider_id": info.get("provider_id", ""),
                "workspace": info.get("workspace"),
                "current_node_id": info.get("current_node_id"),
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

    # ── 导入文件管理 ──
    # 文件存放于 data/conversations/{id}/imports/。路由使用 {filename:path}，
    # filename 可能含 "/" 或 ".."，因此所有访问都先经 _safe_import_path 做
    # realpath 边界校验，阻断路径穿越（../../etc/passwd、绝对路径等）。

    def _get_imports_dir(self, conversation_id: str) -> str:
        """获取导入文件目录"""
        return os.path.join(self._get_conversation_dir(conversation_id), "imports")

    def _safe_import_path(self, conversation_id: str, filename: str) -> Optional[str]:
        """解析并校验导入文件的安全路径；越界返回 None。"""
        imports_dir = os.path.realpath(self._get_imports_dir(conversation_id))
        target = os.path.realpath(os.path.join(imports_dir, filename))
        try:
            if os.path.commonpath([imports_dir, target]) != imports_dir:
                return None
        except ValueError:
            # 不同盘符（Windows）等无法比较的情况，视为越界
            return None
        if target == imports_dir:
            return None
        return target

    def save_import_file(self, conversation_id: str, filename: str, data: bytes) -> None:
        """保存导入文件（原子写）"""
        target = self._safe_import_path(conversation_id, filename)
        if target is None:
            raise ValueError(f"非法文件名: {filename}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        atomic_write_bytes(target, data)
        logger.debug(f"导入文件已保存: {conversation_id}/{filename}")

    def read_import_file(self, conversation_id: str, filename: str) -> Optional[str]:
        """读取导入文件为 UTF-8 文本；不存在或越界返回 None。"""
        target = self._safe_import_path(conversation_id, filename)
        if target is None or not os.path.isfile(target):
            return None
        with open(target, 'r', encoding='utf-8') as f:
            return f.read()

    def list_import_files(self, conversation_id: str) -> List[Dict[str, Any]]:
        """列出对话的所有导入文件 [{filename, size}]。"""
        imports_dir = self._get_imports_dir(conversation_id)
        if not os.path.isdir(imports_dir):
            return []
        result: List[Dict[str, Any]] = []
        for name in os.listdir(imports_dir):
            path = os.path.join(imports_dir, name)
            if os.path.isfile(path):
                result.append({"filename": name, "size": os.path.getsize(path)})
        return result

    def delete_import_file(self, conversation_id: str, filename: str) -> bool:
        """删除导入文件；成功返回 True，文件不存在/越界返回 False。"""
        target = self._safe_import_path(conversation_id, filename)
        if target is None or not os.path.isfile(target):
            return False
        try:
            os.remove(target)
            return True
        except OSError:
            return False
