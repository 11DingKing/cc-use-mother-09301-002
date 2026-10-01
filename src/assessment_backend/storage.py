"""事件日志：追加写入、乐观并发控制与 JSONL 持久化。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable, Optional

from .errors import ConcurrencyError
from .events import Event
from .runtime import SystemRuntime


class EventStore:
    """线程安全的追加事件日志。

    - 事件一旦追加不可修改（审计完整性）。
    - expected_seq 实现乐观并发：提交方基于读到的最大序号提交，
      期间被其他写入者抢先追加则抛出 ConcurrencyError。
    - 可持久化到 JSONL：每行一个事件对象。
    """

    def __init__(self, runtime: Optional[SystemRuntime] = None) -> None:
        self._events: list[Event] = []
        self._runtime = runtime or SystemRuntime()
        self._lock = threading.RLock()

    # ---- 读取 ----

    def all_events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def events_for(self, stream_id: str) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.payload.get("stream_id") == stream_id]

    def next_seq(self) -> int:
        with self._lock:
            return len(self._events) + 1

    # ---- 写入 ----

    def append(
        self,
        event_type: str,
        payload: dict,
        actor_id: str,
        actor_role: str,
        expected_seq: Optional[int] = None,
    ) -> Event:
        with self._lock:
            current = len(self._events)
            if expected_seq is not None and expected_seq != current:
                raise ConcurrencyError(
                    f"并发冲突：期望日志序号 {expected_seq}，实际 {current}，请重读后重试"
                )
            event = Event(
                seq=current + 1,
                id=self._runtime.new_id("EVT"),
                at=self._runtime.now(),
                type=event_type,
                actor_id=actor_id,
                actor_role=actor_role,
                payload=payload,
            )
            self._events.append(event)
            return event

    # ---- 持久化 ----

    def save_jsonl(self, path: str | Path) -> None:
        with self._lock:
            text = "\n".join(json.dumps(e.to_dict(), ensure_ascii=False) for e in self._events)
        Path(path).write_text(text + ("\n" if text else ""), encoding="utf-8")

    def load_jsonl(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            return
        loaded: list[Event] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                loaded.append(Event.from_dict(json.loads(line)))
        with self._lock:
            self._events = loaded

    def replay_into(self, events: Iterable[Event]) -> None:
        """从外部事件集合重建日志（用于审计复算的隔离环境）。"""
        with self._lock:
            self._events = list(events)
