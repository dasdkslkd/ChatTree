"""导入文件存储方法 + 路径穿越防护 验证。
从仓库根运行：  python test/test_import_files.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

from backend.core.storage.chat_storage import ChatStorage


def main():
    tmp = tempfile.mkdtemp(prefix="chattree_imp_")
    try:
        storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
        cid = "conv-1"

        # save + read
        storage.save_import_file(cid, "notes.txt", b"hello world")
        assert storage.read_import_file(cid, "notes.txt") == "hello world", "读回内容不符"

        # list
        listed = storage.list_import_files(cid)
        assert any(f["filename"] == "notes.txt" and f["size"] == 11 for f in listed), f"list 不正确: {listed}"

        # 路径穿越：read 越界返回 None
        assert storage.read_import_file(cid, "../../../main.py") is None, "路径穿越未被拦截 (read)"
        assert storage.read_import_file(cid, "..\\..\\..\\main.py") is None, "路径穿越未被拦截 (read win)"

        # 路径穿越：save 越界抛 ValueError
        try:
            storage.save_import_file(cid, "../escape.txt", b"x")
            raise AssertionError("路径穿越未被拦截 (save)")
        except ValueError:
            pass

        # delete：存在 -> True，再次 -> False
        assert storage.delete_import_file(cid, "notes.txt") is True, "首次删除应为 True"
        assert storage.delete_import_file(cid, "notes.txt") is False, "重复删除应为 False"
        assert storage.read_import_file(cid, "notes.txt") is None, "删除后应读不到"

        # 越界 delete 返回 False（不抛）
        assert storage.delete_import_file(cid, "../../../main.py") is False, "越界删除应为 False"

        print("PASS: 导入文件 CRUD + 路径穿越防护正确")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
