"""只增事件账本。

所有有意义的动作都落成事件：创建、审批、灰度放量、曝光归属、上线闸门、
紧急止损、回滚、豁免、申诉、预警。事件按哈希链串联，任何删改都会使
``head`` 与后继 ``prev_hash`` 断裂，从而可被发现。

账本从不更新或删除既有事件；"回滚"不是覆盖，而是追加一条回滚事件并
激活被指向的旧包；"紧急止损/豁免"也只追加，历史条目原样保留。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .models import canonical, content_hash

GENESIS = "sha256:genesis"


@dataclass(frozen=True)
class Event:
    seq: int
    ts: float
    actor: str
    kind: str
    payload: dict[str, Any]
    prev_hash: str
    event_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "actor": self.actor,
            "kind": self.kind,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "event_hash": self.event_hash,
        }


def _hash_event(seq: int, ts: float, actor: str, kind: str,
                payload: dict[str, Any], prev_hash: str) -> str:
    body = [seq, round(ts, 6), actor, kind, payload, prev_hash]
    return content_hash(body)


class Ledger:
    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self._events: list[Event] = []

    # ---- 写入 ----------------------------------------------------------
    def append(self, kind: str, payload: dict[str, Any], actor: str) -> Event:
        seq = len(self._events)
        ts = self.clock()
        prev = self._events[-1].event_hash if self._events else GENESIS
        # 冻结载荷：后续对调用方字典的改动无法影响账本
        frozen_payload = json.loads(canonical(payload))
        eh = _hash_event(seq, ts, actor, kind, frozen_payload, prev)
        ev = Event(seq, ts, actor, kind, frozen_payload, prev, eh)
        self._events.append(ev)
        return ev

    # ---- 读取 ----------------------------------------------------------
    def events(self, kind: str | None = None) -> list[Event]:
        if kind is None:
            return list(self._events)
        return [e for e in self._events if e.kind == kind]

    def by_hash(self, event_hash: str) -> Event | None:
        for e in self._events:
            if e.event_hash == event_hash:
                return e
        return None

    def head(self) -> str:
        return self._events[-1].event_hash if self._events else GENESIS

    def verify(self) -> tuple[bool, str]:
        """重放哈希链，返回 (是否完好, 断裂位置说明)。"""
        prev = GENESIS
        for e in self._events:
            if e.prev_hash != prev:
                return False, f"事件#{e.seq} 前驱断裂"
            expect = _hash_event(e.seq, e.ts, e.actor, e.kind,
                                 e.payload, e.prev_hash)
            if expect != e.event_hash:
                return False, f"事件#{e.seq} 内容与哈希不符"
            prev = e.event_hash
        return True, "ok"

    # ---- 持久化 --------------------------------------------------------
    def dump(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._events]

    @classmethod
    def load(cls, raw: list[dict[str, Any]], clock=time.time) -> "Ledger":
        led = cls(clock=clock)
        for r in raw:
            led._events.append(Event(
                seq=r["seq"], ts=r["ts"], actor=r["actor"], kind=r["kind"],
                payload=r["payload"], prev_hash=r["prev_hash"],
                event_hash=r["event_hash"],
            ))
        ok, msg = led.verify()
        if not ok:
            raise ValueError(f"账本校验失败：{msg}")
        return led
