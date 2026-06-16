# storage/atomic.py - 原子文件写入辅助
"""
原子写入：先写同目录临时文件，再 os.replace 覆盖目标。

为什么同目录：os.replace 仅在同一文件系统上保证原子性。临时文件与目标
放在同一目录可确保同盘，Windows / POSIX 均按原子 rename 处理。

崩溃语义：进程在 os.replace 之前崩溃 → 目标文件保持旧内容（或不存在），
绝不会出现被截断的半成品。临时文件残留由下次写入或异常分支清理。

默认 fsync=False：data/ 目录可丢弃，重性能而非掉电持久性。需要落盘持久
保证的调用方可显式传 fsync=True。
"""
import json
import os
import uuid
from typing import Any


def _tmp_path(path: str) -> str:
    """同目录下生成唯一临时文件名。"""
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    return os.path.join(directory, f"{base}.tmp.{uuid.uuid4().hex}")


def _atomic_write(path: str, data: bytes, *, fsync: bool = False) -> None:
    """把 bytes 原子写入 path。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 失败时清理临时文件，避免残留（目标文件保持原状）
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: str, data: bytes, *, fsync: bool = False) -> None:
    """原子写入二进制内容。"""
    _atomic_write(path, data, fsync=fsync)


def atomic_write_text(path: str, text: str, *, fsync: bool = False) -> None:
    """原子写入 UTF-8 文本。"""
    _atomic_write(path, text.encode("utf-8"), fsync=fsync)


def atomic_write_json(path: str, obj: Any, *, fsync: bool = False) -> None:
    """原子写入 JSON（indent=2, ensure_ascii=False，与现有写法一致）。"""
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    _atomic_write(path, text.encode("utf-8"), fsync=fsync)
