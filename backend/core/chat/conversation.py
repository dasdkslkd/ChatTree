# chat/conversation.py - 重构为基于节点的对话管理
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any
from ..config.types import ConversationData, ConversationTreeNode, ConversationMetadata, Message

class Conversation:
    """基于节点的树形对话类"""
    
    def __init__(self, model: str, conversation_id: str = '', title: str = ''):
        self.metadata: ConversationMetadata = {
            "id": conversation_id or str(uuid.uuid4()),
            "title": title,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "model": model,
            "total_tokens": {}
        }
        self.nodes: Dict[str, ConversationTreeNode] = {}
        self.root_node_id: Optional[str] = None
        self.current_node_id: Optional[str] = None
    
    def initialize_with_system_message(self, system_prompt: str):
        """初始化系统消息作为根节点"""
        from .node import NodeManager
        root_node = NodeManager.create_root_node(system_prompt)
        self.add_node(root_node, is_root=True)
    
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
        
        self.metadata["updated_at"] = datetime.now().isoformat()

    def del_node(self, node_id: str):
        """删除节点及其子节点"""
        if node_id not in self.nodes:
            return
        
        # 递归删除子节点
        def _delete_recursive(n_id: str):
            node = self.nodes[n_id]
            for child_id in node["children_ids"]:
                _delete_recursive(child_id)
            del self.nodes[n_id]
        
        parent_id = self.nodes[node_id].get("parent_id")
        _delete_recursive(node_id)
        
        # 更新父节点的子节点列表
        if parent_id and parent_id in self.nodes:
            self.nodes[parent_id]["children_ids"].remove(node_id)
        
        # 如果删除的是当前节点，切换到父节点
        if self.current_node_id == node_id:
            self.current_node_id = parent_id
        
        self.metadata["updated_at"] = datetime.now().isoformat()
    
    def get_node_chain(self, node_id: Optional[str] = None) -> List[ConversationTreeNode]:
        """获取从根节点到指定节点的完整路径"""
        target_id = node_id or self.current_node_id
        if not target_id or target_id not in self.nodes:
            return []
        
        chain = []
        current_id = target_id
        
        # 向前回溯到根节点
        while current_id:
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
        """
        node_chain = self.get_node_chain(node_id)
        messages = []
        
        for node in node_chain:
            # 根节点可能有system消息
            if node["system_message"]:
                messages.append(node["system_message"])
            
            # 添加用户消息
            if node["user_message"]:
                messages.append(node["user_message"])
            
            # 添加助手消息（如果存在）
            if node["assistant_message"]:
                messages.append(node["assistant_message"])
            
            # 添加工具消息（如果有）
            messages.extend(node["tool_messages"])
        
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
        if node["user_message"]:
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
            "root_node_id": self.root_node_id
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Conversation":
        """从字典创建对话"""
        metadata = data["metadata"]
        conv = cls(
            model=metadata["model"],
            conversation_id=metadata["id"],
            title=metadata.get("title", "")
        )
        conv.metadata = metadata
        conv.nodes = {node["id"]: node for node in data["nodes"]}
        conv.current_node_id = data.get("current_node_id")
        conv.root_node_id = data.get("root_node_id")
        return conv
    
    def clear(self):
        """清空对话"""
        self.nodes.clear()
        self.root_node_id = None
        self.current_node_id = None
        self.metadata["updated_at"] = datetime.now().isoformat()