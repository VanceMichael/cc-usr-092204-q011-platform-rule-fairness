"""反查：从投诉或竞争指标定位到具体规则与批准依据。

支持两类入口：
1. 投诉：给 merchant_id（可选 entry_id / 时间窗），先定位其当时曝光所归属
   的发布包，再回溯该包的规则组件、批准团队与审批意见、相关豁免与申诉；
2. 竞争指标：给分层与时间窗，汇总该窗口内生效过的规则包及预警，给出
   "异常可能由哪条规则组合造成"的证据链。

输出是一条完整证据链：曝光 -> 发布包 -> 规则组件 -> 审批 -> 应急/豁免/
申诉 -> 预警。所有节点引用只增账本中的事件哈希，可逐条核验。
"""

from __future__ import annotations

from typing import Any

from .models import to_jsonable


def _event_ref(e) -> dict[str, Any]:
    return {"seq": e.seq, "kind": e.kind, "actor": e.actor, "ts": e.ts,
            "event_hash": e.event_hash, "payload": e.payload}


def trace_complaint(store, merchant_id: str,
                    entry_id: str | None = None,
                    start_ts: float | None = None,
                    end_ts: float | None = None) -> dict[str, Any]:
    """从一则商家投诉反查其遭遇的规则与平台依据。"""
    if merchant_id not in store.merchants:
        raise KeyError(f"商家不存在：{merchant_id}")

    exposures = [
        x for x in store.exposures
        if x["merchant_id"] == merchant_id
        and (entry_id is None or x["entry_id"] == entry_id)
        and (start_ts is None or x["ts"] >= start_ts)
        and (end_ts is None or x["ts"] <= end_ts)
    ]
    if not exposures:
        return {"merchant_id": merchant_id, "found": False,
                "reason": "时间窗内无该商家的曝光归属记录"}

    chains: list[dict[str, Any]] = []
    package_hashes = {x["content_hash"] for x in exposures}
    for h in sorted(package_hashes):
        chains.append(_package_chain(store, h, merchant_id))

    return {
        "found": True,
        "merchant_id": merchant_id,
        "exposure_count": len(exposures),
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "exposures": exposures,
        "rule_chains": chains,
    }


def _package_chain(store, package_hash: str,
                   merchant_id: str | None = None) -> dict[str, Any]:
    pkg = store.package(package_hash)
    created = next((e for e in store.ledger.events("PACKAGE_CREATED")
                    if e.payload.get("content_hash") == package_hash), None)
    approvals = [
        {"team": e.payload["team"], "approver": e.payload["approver"],
         "ts": e.ts, "comment": e.payload.get("comment", ""),
         "event_hash": e.event_hash}
        for e in store.ledger.events("PACKAGE_APPROVED")
        if e.payload.get("content_hash") == package_hash
    ]
    lifecycle = [
        _event_ref(e) for e in store.ledger.events()
        if e.payload.get("content_hash") == package_hash
        and e.kind in ("GRAY_STARTED", "GRAY_ADJUSTED",
                       "PRODUCTION_ACTIVATED", "EMERGENCY_STOP",
                       "ALERT_RAISED")
        or (e.kind == "ROLLBACK" and
            (e.payload.get("to_hash") == package_hash or
             e.payload.get("from_hash") == package_hash))
    ]
    exemptions = [
        {"exemption_id": e.exemption_id, "constraint_ids":
            list(e.constraint_ids), "reason": e.reason,
            "grantor": e.grantor, "ts": e.ts, "revoked": e.revoked}
        for e in store.exemptions
        if e.package_hash == package_hash
        and (merchant_id is None or e.merchant_id == merchant_id)
    ]
    appeals = [
        {"appeal_id": a.appeal_id, "merchant_id": a.merchant_id,
         "exposure_id": a.exposure_id, "reason": a.reason,
         "status": a.status, "resolution": a.resolution,
         "ts": a.ts, "resolved_ts": a.resolved_ts}
        for a in store.appeals.values()
        if a.package_hash == package_hash
        and (merchant_id is None or a.merchant_id == merchant_id)
    ]
    alerts = [_event_ref(e) for e in store.ledger.events("ALERT_RAISED")
              if e.payload.get("content_hash") == package_hash]

    return {
        "content_hash": package_hash,
        "package_id": pkg.package_id,
        "revision": pkg.revision,
        "algorithm_version": pkg.algorithm.version,
        "rule_components": {
            "algorithm": to_jsonable(pkg.algorithm),
            "scope": to_jsonable(pkg.scope),
            "entries": [e.entry_id for e in pkg.entries],
            "subsidies": [s.condition_id for s in pkg.subsidies],
            "price_constraints":
                [c.constraint_id for c in pkg.price_constraints],
            "exit_terms": to_jsonable(pkg.exit_terms),
        },
        "created_event": _event_ref(created) if created else None,
        "approval_basis": approvals,
        "lifecycle_events": lifecycle,
        "exemptions": exemptions,
        "appeals": appeals,
        "alerts": alerts,
    }


def trace_metric(store, segment: str,
                 start_ts: float | None = None,
                 end_ts: float | None = None) -> dict[str, Any]:
    """从竞争指标异常（分层 + 时间窗）反查窗口内生效过的规则与组合预警。"""
    active_hashes: set[str] = set()
    timeline: list[dict[str, Any]] = []
    for e in store.ledger.events():
        if start_ts is not None and e.ts < start_ts:
            continue
        if end_ts is not None and e.ts > end_ts:
            continue
        h = e.payload.get("content_hash")
        if e.kind == "PRODUCTION_ACTIVATED":
            active_hashes.add(e.payload["content_hash"])
            timeline.append({"ts": e.ts, "event": "PRODUCTION_ACTIVATED",
                             "content_hash": e.payload["content_hash"]})
        elif e.kind == "GRAY_STARTED":
            active_hashes.add(e.payload["content_hash"])
            timeline.append({"ts": e.ts, "event": "GRAY_STARTED",
                             "content_hash": e.payload["content_hash"],
                             "percent": e.payload.get("percent")})
        elif e.kind == "EMERGENCY_STOP":
            timeline.append({"ts": e.ts, "event": "EMERGENCY_STOP",
                             "content_hash": h,
                             "reason": e.payload.get("reason")})
        elif e.kind == "ROLLBACK":
            timeline.append({"ts": e.ts, "event": "ROLLBACK",
                             "from_hash": e.payload.get("from_hash"),
                             "to_hash": e.payload.get("to_hash"),
                             "reason": e.payload.get("reason")})
            if e.payload.get("to_hash"):
                active_hashes.add(e.payload["to_hash"])
        elif e.kind == "ALERT_RAISED":
            active_hashes.add(e.payload["content_hash"])

    # 仅保留作用于该分层的包
    seg_hashes = []
    for h in sorted(active_hashes):
        pkg = store.package(h)
        seg_merchants = [m for m, d in store.merchants.items()
                         if d.get("segment") == segment and pkg.scope.matches(d)]
        if seg_merchants:
            seg_hashes.append(h)

    alerts = []
    for e in store.ledger.events("ALERT_RAISED"):
        if e.payload.get("segment") != segment:
            continue
        if start_ts is not None and e.ts < start_ts:
            continue
        if end_ts is not None and e.ts > end_ts:
            continue
        alerts.append(_event_ref(e))

    return {
        "found": bool(seg_hashes or alerts),
        "segment": segment,
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "active_packages": [_package_chain(store, h) for h in seg_hashes],
        "alerts": alerts,
        "timeline": timeline,
    }
