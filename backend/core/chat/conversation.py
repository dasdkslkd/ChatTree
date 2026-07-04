# chat/conversation.py - 重构为基于节点的对话管理
import uuid
from time import time
from typing import List, Optional, Dict, Any
from ..config.types import ConversationTreeNode, ConversationMetadata, Message, ModelProvider, SCHEMA_VERSION
from ..model.usage import estimated_usage, usage_total
from ..utils.logger import setup_logger

logger = setup_logger('ConversationTree')


class CorruptConversationError(Exception):
    """对话数据结构性损坏，无法安全加载。"""


class Conversation:
    """基于节点的树形对话类"""

    def __init__(
        self,
        conversation_id: str = '',
        title: str = '',
        provider: Optional[ModelProvider] = None,
        model: Optional[str] = None,
        workspace: Optional[Dict[str, Any]] = None,
    ):
        self.metadata: ConversationMetadata = {
            "id": conversation_id or str(uuid.uuid4()),
            "title": title,
            "created_at": int(time()),
            "updated_at": int(time()),
            "total_tokens": {},
            "schema_version": SCHEMA_VERSION,
        }
        if workspace is not None:
            self.metadata["workspace"] = workspace
        self.nodes: Dict[str, ConversationTreeNode] = {}
        self.root_node_id: Optional[str] = None
        self.current_node_id: Optional[str] = None
        self.current_provider: Optional[ModelProvider] = provider
        self.current_model: Optional[str] = model
        # 本次会话生命周期内被显式删除的节点 id，供 storage.save 定向删除文件。
        # 保存成功后由调用方 clear()。
        self._deleted_node_ids: set[str] = set()
    
    def initialize_with_system_message(self, system_prompt: Optional[str] = None, force = False):
        """初始化系统消息作为根节点"""
        from .node import NodeManager
        if len(self.nodes) > 0 and not force:
            logger.warning("对话已初始化，跳过系统消息初始化")
            return  # 已初始化
        self.clear()
        root_node = NodeManager.create_root_node(system_prompt)
        self.add_node(root_node, is_root=True)

    def set_current_model(self, provider: ModelProvider, model: str):
        """设置当前使用的模型"""
        self.current_provider = provider
        self.current_model = model
        self.metadata["provider_id"] = provider
        self.metadata["model_id"] = model
    
    def add_node(self, node: ConversationTreeNode, parent_id: Optional[str] = None, is_root: bool = False):
        """添加节点到对话树"""
        node_id = node["id"]
        self.nodes[node_id] = node
        
        if is_root:
            self.root_node_id = node_id
            self.current_node_id = node_id
        elif parent_id and parent_id in self.nodes:
            # 建立父子关系
            node["parent_id"] = parent_id
            self.nodes[parent_id]["children_ids"].append(node_id)
            self.current_node_id = node_id
        
        self.metadata["updated_at"] = int(time())

    def del_node(self, node_id: str):
        """删除节点及其子节点"""
        if node_id not in self.nodes:
            return
        
        # 递归删除子节点
        def _delete_recursive(n_id: str):
            node = self.nodes[n_id]
            for child_id in list(node["children_ids"]):
                _delete_recursive(child_id)
            del self.nodes[n_id]
            self._deleted_node_ids.add(n_id)

        parent_id = self.nodes[node_id].get("parent_id")
        _delete_recursive(node_id)
        
        # 更新父节点的子节点列表
        if parent_id and parent_id in self.nodes:
            self.nodes[parent_id]["children_ids"].remove(node_id)
        
        # 如果删除的是当前节点，切换到父节点
        if self.current_node_id == node_id:
            self.current_node_id = parent_id
        
        self.metadata["updated_at"] = int(time())
    
    def get_node_chain(self, node_id: Optional[str] = None) -> List[ConversationTreeNode]:
        """获取从根节点到指定节点的完整路径"""
        target_id = node_id or self.current_node_id
        if not target_id or target_id not in self.nodes:
            return []
        
        chain = []
        current_id = target_id
        
        # 向前回溯到根节点
        while current_id != 'None' and current_id:
            node = self.nodes[current_id]
            chain.insert(0, node)
            current_id = node.get("parent_id")
        
        return chain
    
    def get_current_node_chain(self) -> List[ConversationTreeNode]:
        """获取当前分支的节点链"""
        return self.get_node_chain(self.current_node_id)
    
    def get_message_chain_from_node(self, node_id: Optional[str] = None) -> List[Message]:
        """
        从节点链提取消息链，用于API调用
        顺序: system(根) -> user -> assistant -> tools -> user -> ...
        每条消息会附带所在节点的 node_id，方便前端定位。
        """
        node_chain = self.get_node_chain(node_id)
        messages = []

        def attach_node_metadata(msg: Message, node: ConversationTreeNode) -> Message:
            msg["node_id"] = node["id"]
            msg["parent_node_id"] = node.get("parent_id")
            msg["branch_total_tokens"] = node.get("total_tokens", 0)
            if node.get("branch_usage_info") is not None:
                msg["branch_usage_info"] = node.get("branch_usage_info")
            if node.get("usage") is not None:
                msg["context_usage"] = node.get("usage")
            if node.get("tool_permission_mode") is not None:
                msg["tool_permission_mode"] = node.get("tool_permission_mode")
            return msg
        
        for node in node_chain:
            # 根节点可能有system消息
            if node["system_message"]:
                msg = dict(node["system_message"])
                messages.append(attach_node_metadata(msg, node))
            
            # 添加用户消息
            if node["user_message"]:
                msg = dict(node["user_message"])
                messages.append(attach_node_metadata(msg, node))
            
            # 添加助手消息（如果存在）
            if node["assistant_message"]:
                msg = dict(node["assistant_message"])
                messages.append(attach_node_metadata(msg, node))
            
            # 添加工具消息（如果有）
            for tool_msg in node["tool_messages"]:
                msg = dict(tool_msg)
                messages.append(attach_node_metadata(msg, node))
        
        return messages
    
    def get_node_tree(self, node_id: Optional[str] = None, level: int = 0) -> List[Dict[str, Any]]:
        """获取节点树形结构用于显示"""
        current_id = node_id or self.root_node_id
        if not current_id or current_id not in self.nodes:
            return []
        
        result = []
        node = self.nodes[current_id]
        
        # 构建节点显示信息
        display_info = {
            "id": node["id"],
            "level": level,
            "is_current": node["id"] == self.current_node_id,
            "has_children": len(node["children_ids"]) > 0,
            "children_count": len(node["children_ids"]),
            "timestamp": node["timestamp"],
            "model_id": node["model_id"]
        }
        
        # 添加消息摘要
        if node["user_message"] and not node["user_message"].get("is_hidden_from_transcript"):
            content = node["user_message"]["content"][:50] + "..."
            display_info["user_content"] = content
        
        if node["assistant_message"]:
            content = node["assistant_message"]["content"][:50] + "..."
            display_info["assistant_content"] = content
        
        result.append(display_info)
        
        # 递归添加子节点
        for child_id in node["children_ids"]:
            result.extend(self.get_node_tree(child_id, level + 1))
        
        return result
    
    def get_available_branches(self) -> List[Dict[str, Any]]:
        """获取所有可用分支信息"""
        branches = []
        for node_id, node in self.nodes.items():
            if len(node["children_ids"]) > 1:  # 有分支点
                for child_id in node["children_ids"]:
                    child_node = self.nodes.get(child_id)
                    if child_node:
                        branches.append({
                            "branch_id": child_id,
                            "title": f"从节点 {node_id[:8]} 分支",
                            "fork_node_id": node_id,
                            "message_count": self._count_nodes_in_branch(child_id)
                        })
        return branches
    
    def _count_nodes_in_branch(self, start_node_id: str) -> int:
        """统计分支中的节点数量"""
        count = 0
        stack = [start_node_id]
        
        while stack:
            node_id = stack.pop()
            if node_id in self.nodes:
                count += 1
                stack.extend(self.nodes[node_id]["children_ids"])
        
        return count

    def get_descendant_node_ids(self, node_id: str, include_self: bool = True) -> List[str]:
        """返回 node_id 子树内的节点 id。"""
        if node_id not in self.nodes:
            return []
        result: List[str] = []
        stack = [node_id]
        while stack:
            current = stack.pop()
            if include_self or current != node_id:
                result.append(current)
            stack.extend(reversed(self.nodes.get(current, {}).get("children_ids", [])))
        return result
    
    def switch_to_node(self, node_id: str) -> bool:
        """切换到指定节点继续对话"""
        if node_id in self.nodes:
            self.current_node_id = node_id
            return True
        return False
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "metadata": self.metadata,
            "nodes": list(self.nodes.values()),
            "current_node_id": self.current_node_id,
            "root_node_id": self.root_node_id,
            # 仅供 storage.save 消费的定向删除列表（不持久化进 metadata）
            "deleted_node_ids": list(self._deleted_node_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Conversation":
        """从字典创建对话，并做完整性校验（fail-safe）。

        结构性损坏（缺 id / 缺 root / 版本过新）→ raise CorruptConversationError。
        可恢复问题（缺 id 的节点、悬空 parent、非法 current）→ 跳过/重置并记日志。
        无旧格式迁移：缺失 schema_version 视为兼容，过新则拒绝。
        """
        metadata = data.get("metadata") or {}
        conv_id = metadata.get("id")
        if not isinstance(conv_id, str) or not conv_id:
            raise CorruptConversationError("metadata.id 缺失或非法")

        version = metadata.get("schema_version")
        if isinstance(version, int) and version > SCHEMA_VERSION:
            raise CorruptConversationError(
                f"schema_version {version} 高于本程序支持的 {SCHEMA_VERSION}")

        conv = cls(
            provider=metadata.get("provider_id"),
            model=metadata.get("model_id"),
            conversation_id=conv_id,
            title=metadata.get("title", "")
        )
        conv.metadata = metadata

        # 构建节点字典，跳过缺 id 的节点
        nodes: Dict[str, ConversationTreeNode] = {}
        for node in data.get("nodes", []):
            nid = node.get("id")
            if not isinstance(nid, str) or not nid:
                logger.error(f"对话 {conv_id}: 跳过缺少 id 的节点")
                continue
            cls._ensure_node_usage(node)
            nodes[nid] = node

        root_node_id = data.get("root_node_id")
        if not root_node_id or root_node_id not in nodes:
            raise CorruptConversationError(f"对话 {conv_id}: 根节点缺失或不存在")

        # 剔除悬空 parent 的节点及其子树（保持可加载）
        skipped = cls._prune_dangling(nodes, root_node_id, conv_id)

        conv.nodes = nodes
        conv.root_node_id = root_node_id

        current = data.get("current_node_id")
        if not current or current not in nodes or current in skipped:
            if current is not None:
                logger.warning(f"对话 {conv_id}: current_node_id 非法，重置为根节点")
            current = root_node_id
        conv.current_node_id = current

        return conv

    @staticmethod
    def _ensure_node_usage(node: ConversationTreeNode):
        """Backfill layered usage fields for old persisted nodes."""
        total = int(node.get("total_tokens") or 0)
        branch_usage = node.get("branch_usage_info") or estimated_usage(total)
        if not branch_usage.get("total_tokens") and total:
            branch_usage = estimated_usage(total)
        node["branch_usage_info"] = branch_usage
        node["total_tokens"] = usage_total(branch_usage, total)

        assistant = node.get("assistant_message") or {}
        generation_info = assistant.get("generation_info") or {}
        turn_usage = generation_info.get("usage_info")
        if not turn_usage:
            turn_usage = estimated_usage(int(generation_info.get("tokens_used") or 0))

        usage = node.get("usage") or {}
        usage["turn_usage"] = usage.get("turn_usage") or turn_usage
        usage["branch_usage"] = usage.get("branch_usage") or branch_usage
        usage["active_context_usage"] = usage.get("active_context_usage") or branch_usage
        usage["model_context_window"] = usage.get("model_context_window")
        node["usage"] = usage

    @staticmethod
    def _prune_dangling(nodes: Dict[str, ConversationTreeNode], root_id: str, conv_id: str) -> set:
        """删除 parent 悬空（不存在且非根）的节点及其子树，返回被删 id 集合。"""
        skipped: set = set()
        changed = True
        while changed:
            changed = False
            for nid in list(nodes.keys()):
                if nid == root_id:
                    continue
                parent_id = nodes[nid].get("parent_id")
                if parent_id != 'None' and parent_id not in nodes:
                    del nodes[nid]
                    skipped.add(nid)
                    changed = True
        if skipped:
            logger.error(f"对话 {conv_id}: 剔除 {len(skipped)} 个悬空节点")
            # 清理仍在 nodes 中的父节点对已删子节点的引用
            for node in nodes.values():
                node["children_ids"] = [c for c in node.get("children_ids", []) if c in nodes]
        return skipped
    
    def clear(self):
        """清空对话"""
        self.nodes.clear()
        self.root_node_id = None
        self.current_node_id = None
        self.metadata["updated_at"] = int(time())
