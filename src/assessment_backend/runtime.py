"""时钟与 ID 生成，可在测试与演示中替换为确定性实现。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional


class SystemRuntime:
    """生产环境使用的真实时钟与随机 ID。"""

    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:10]}"


class FixedRuntime:
    """确定性时钟：从固定起点开始，每次取时间自动前进固定步长。"""

    def __init__(self, start: str = "2025-11-01T09:00:00+00:00", step_seconds: int = 60) -> None:
        self._cursor = datetime.fromisoformat(start)
        self._step = timedelta(seconds=step_seconds)
        self._counters: dict[str, int] = {}

    def now(self) -> str:
        value = self._cursor.isoformat()
        self._cursor += self._step
        return value

    def new_id(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{prefix}-{n:04d}"


def current_year(at: Optional[str] = None) -> int:
    """从 ISO 时间戳解析年份（供测试断言，业务年度由命令显式给出）。"""
    stamp = at or datetime.now(timezone.utc).isoformat()
    return datetime.fromisoformat(stamp).year
