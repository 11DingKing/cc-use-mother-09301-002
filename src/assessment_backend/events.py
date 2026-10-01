"""追加到事件日志中的不可变事件。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    seq: int
    id: str
    at: str
    type: str
    actor_id: str
    actor_role: str
    payload: dict

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "id": self.id,
            "at": self.at,
            "type": self.type,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "Event":
        return cls(
            seq=int(value["seq"]),
            id=value["id"],
            at=value["at"],
            type=value["type"],
            actor_id=value["actor_id"],
            actor_role=value["actor_role"],
            payload=value["payload"],
        )
