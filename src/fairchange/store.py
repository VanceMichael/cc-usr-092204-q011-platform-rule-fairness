"""只增不改的事件日志。

紧急止损、回滚、豁免都以新事件追加，历史事件永不修改或删除，
因此任何时候都可以完整重放规则变更过程。
"""
from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class EventStore:
    """追加式事件存储。刻意不提供 update/delete 方法。"""

    def __init__(self) -> None:
        self._events: list[dict] = []

    def append(self, event_type: str, actor: str, payload: dict, ts: str | None = None) -> dict:
        event = {
            "seq": len(self._events) + 1,
            "type": event_type,
            "actor": actor,
            "ts": ts or utc_now(),
            "payload": payload,
        }
        self._events.append(event)
        return dict(event)

    def all(self) -> list[dict]:
        return [dict(e) for e in self._events]

    def for_package(self, package_id: str) -> list[dict]:
        return [dict(e) for e in self._events if e["payload"].get("package_id") == package_id]

    def of_type(self, *types: str) -> list[dict]:
        return [dict(e) for e in self._events if e["type"] in types]
