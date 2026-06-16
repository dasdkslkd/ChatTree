"""对话完整性校验 + schema 版本 验证。
从仓库根运行：  python test/test_integrity.py
"""
import sys
from time import time

sys.path.insert(0, ".")

from backend.core.chat.conversation import Conversation, CorruptConversationError
from backend.core.config.types import SCHEMA_VERSION


def _root(nid="root"):
    return {
        "id": nid, "parent_id": "None", "children_ids": [],
        "user_message": None, "assistant_message": None, "tool_messages": [],
        "system_message": None, "timestamp": int(time()), "model_id": None, "total_tokens": 0,
    }


def _child(nid, parent):
    return {
        "id": nid, "parent_id": parent, "children_ids": [],
        "user_message": {"id": "m", "role": "user", "content": "hi", "timestamp": int(time())},
        "assistant_message": None, "tool_messages": [],
        "system_message": None, "timestamp": int(time()), "model_id": None, "total_tokens": 0,
    }


def base(nodes, root="root", current=None):
    return {
        "metadata": {"id": "c1", "title": "t", "created_at": 1, "updated_at": 1,
                     "total_tokens": {}, "schema_version": SCHEMA_VERSION},
        "nodes": nodes, "root_node_id": root, "current_node_id": current or root,
    }


def expect_raise(data, label):
    try:
        Conversation.from_dict(data)
        raise AssertionError(f"{label}: 应抛 CorruptConversationError 但没有")
    except CorruptConversationError:
        pass


def main():
    # 1. 缺 id -> raise
    d = base([_root()]); d["metadata"].pop("id")
    expect_raise(d, "缺 metadata.id")

    # 2. 缺 root -> raise
    expect_raise(base([_root()], root="missing"), "root 不存在")

    # 3. schema_version 过新 -> raise
    d = base([_root()]); d["metadata"]["schema_version"] = SCHEMA_VERSION + 1
    expect_raise(d, "版本过新")

    # 4. 节点缺 id -> 跳过，不致命
    bad = _child("c", "root"); bad.pop("id")
    conv = Conversation.from_dict(base([_root(), bad]))
    assert len(conv.nodes) == 1, "缺 id 节点应被跳过"

    # 5. 悬空 parent -> 跳过该节点；current 落在被跳过集合 -> 重置为 root
    r = _root(); r["children_ids"] = ["c1node"]
    dangling = _child("c1node", "ghost-parent")
    conv = Conversation.from_dict(base([r, dangling], current="c1node"))
    assert "c1node" not in conv.nodes, "悬空 parent 节点应被剔除"
    assert conv.current_node_id == conv.root_node_id, "current 应重置为 root"

    # 6. current 非法 -> 重置为 root（正常节点保留）
    r = _root(); r["children_ids"] = ["ok"]
    conv = Conversation.from_dict(base([r, _child("ok", "root")], current="nonexistent"))
    assert conv.current_node_id == conv.root_node_id, "非法 current 应重置为 root"
    assert "ok" in conv.nodes, "合法节点不应被剔除"

    # 7. 缺失 schema_version -> 接受（无迁移）
    d = base([_root()]); d["metadata"].pop("schema_version")
    Conversation.from_dict(d)  # 不应抛

    print("PASS: 完整性校验与版本检查行为正确")


if __name__ == "__main__":
    main()
