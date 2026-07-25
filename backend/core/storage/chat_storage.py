# storage/chat_storage.py - 修改为多文件存储
import os
from typing import List, Dict, Any, Optional
from backend.core.persistence.home import resolve_chattree_home
from .atomic import atomic_write_bytes
from ..utils.logger import setup_logger

logger = setup_logger('ChatStorage')

class ChatStorage:
    """对话导入文件存储；conversation history 只在 canonical SQLite。"""

    def __init__(self, storage_dir: str | None = None):
        self.storage_dir = str(storage_dir or resolve_chattree_home() / "conversations")
        os.makedirs(self.storage_dir, exist_ok=True)
        logger.info(f"Chat存储初始化完成，目录: {self.storage_dir}")

    def _get_conversation_dir(self, conversation_id: str) -> str:
        """获取对话目录路径"""
        return os.path.join(self.storage_dir, conversation_id)

    # ── 导入文件管理 ──
    # 文件存放于 ChatTree home 的 conversations/{id}/imports/。路由使用 {filename:path}，
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

    def read_import_file_bytes(self, conversation_id: str, filename: str) -> Optional[bytes]:
        """读取导入文件原始字节；不存在或越界返回 None。"""
        target = self._safe_import_path(conversation_id, filename)
        if target is None or not os.path.isfile(target):
            return None
        with open(target, 'rb') as f:
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
