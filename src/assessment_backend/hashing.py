"""规范化序列化与摘要哈希，用于封存快照与确定性复算比对。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(obj: Any) -> bytes:
    """对任意可 JSON 化对象产出字节级稳定的序列化结果。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(obj: Any) -> str:
    """返回对象的 SHA-256 摘要（取前 16 位，足够作为封存指纹）。"""
    return hashlib.sha256(canonical(obj)).hexdigest()[:16]
