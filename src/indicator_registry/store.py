"""仅追加事件存储：每条事件携带前驱哈希，形成可独立验证的哈希链。"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import ChainIntegrityError
from .models import Event

GENESIS = "0" * 64


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def digest_event(prev_hash: str, timestamp: str, actor: str, etype: str, payload: dict) -> str:
    body = json.dumps(
        {"prev": prev_hash, "ts": timestamp, "actor": actor, "type": etype, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def canonical_manifest(entries: Iterable[dict]) -> str:
    """计算封存清单指纹（对输入项做规范化哈希，键序无关）。"""
    h = hashlib.sha256()
    for item in entries:
        h.update(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


class EventStore:
    """JSONL 追加日志。写操作在同一把锁内完成 seq 分配与落盘。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._events: list[Event] = []
        if self.path and self.path.exists():
            raw = self.path.read_text(encoding="utf-8")
            if raw.strip():
                for line in raw.splitlines():
                    self._events.append(self._from_dict(json.loads(line)))
            self.verify()

    @property
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    @staticmethod
    def _from_dict(d: dict) -> Event:
        return Event(
            seq=d["seq"],
            timestamp=d["timestamp"],
            actor=d["actor"],
            type=d["type"],
            payload=d.get("payload", {}),
            prev_hash=d["prev_hash"],
            hash=d["hash"],
            id=d.get("id", ""),
            txn=d.get("txn", ""),
        )

    def append(
        self,
        etype: str,
        payload: dict[str, Any],
        actor: str,
        *,
        event_id: str = "",
        txn: str = "",
        timestamp: str | None = None,
    ) -> Event:
        with self._lock:
            seq = len(self._events) + 1
            ts = timestamp or utcnow_iso()
            prev = self._events[-1].hash if self._events else GENESIS
            h = digest_event(prev, ts, actor, etype, payload)
            event = Event(seq=seq, timestamp=ts, actor=actor, type=etype, payload=payload,
                          prev_hash=prev, hash=h, id=event_id, txn=txn)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                    f.flush()
            self._events.append(event)
            return event

    def verify(self) -> None:
        """重算全链哈希，任何篡改立即报错。"""
        prev = GENESIS
        for e in self._events:
            if e.prev_hash != prev:
                raise ChainIntegrityError(f"事件 {e.seq} 前驱哈希不匹配")
            if digest_event(e.prev_hash, e.timestamp, e.actor, e.type, e.payload) != e.hash:
                raise ChainIntegrityError(f"事件 {e.seq} 内容哈希不匹配")
            prev = e.hash

    @property
    def head_hash(self) -> str:
        with self._lock:
            return self._events[-1].hash if self._events else GENESIS
