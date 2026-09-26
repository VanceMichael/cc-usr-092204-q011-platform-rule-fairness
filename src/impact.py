"""影响复算。

审查人员仅凭"冻结发布包 + 商家目录 + 市场观测"即可确定性重放：
- 新旧算法下的排序与名次变化（降权）；
- 价格约束与补贴条件叠加产生的跟价压力；
- 退出成本相对旧包的变化。

全部计算无随机数、无外部服务；并列时按 merchant_id 破并，保证可复算。
"""

from __future__ import annotations

from typing import Any

from .models import RulePackage
from .store import RuleStore


def _rank(pkg: RulePackage, merchants: list[dict[str, Any]]
          ) -> dict[str, int]:
    ordered = sorted(
        merchants,
        key=lambda m: (-pkg.algorithm.score(m), m["merchant_id"]),
    )
    return {m["merchant_id"]: i + 1 for i, m in enumerate(ordered)}


def _lowest_price(merchants: list[dict[str, Any]]) -> float | None:
    prices = [m.get("price") for m in merchants if m.get("price") is not None]
    return min(prices) if prices else None


def compare(store: RuleStore, new_hash: str,
            base_hash: str | None = None) -> dict[str, Any]:
    """对比新包与基线包，逐商家产出可复算行与分层汇总。"""
    new = store.package(new_hash)
    base = store.package(base_hash) if base_hash else None

    rows: list[dict[str, Any]] = []
    for mid in sorted(store.merchants):
        m = store.merchants[mid]
        if not new.scope.matches(m):
            continue
        row: dict[str, Any] = {
            "merchant_id": mid,
            "segment": m.get("segment"),
        }
        # --- 排序复算 ---
        if base:
            peers = [x for x in store.merchants.values()
                     if x.get("segment") == m.get("segment")]
            old_rank = _rank(base, peers).get(mid)
            new_rank = _rank(new, peers).get(mid)
            row["old_rank"] = old_rank
            row["new_rank"] = new_rank
            row["rank_delta"] = (new_rank or 0) - (old_rank or 0)
            row["old_score"] = round(base.algorithm.score(m), 6)
            row["new_score"] = round(new.algorithm.score(m), 6)
            row["demoted"] = row["rank_delta"] > 0
        else:
            row["old_rank"] = None
            row["new_rank"] = None
            row["rank_delta"] = None
            row["demoted"] = False

        # --- 跟价 / 补贴压力复算 ---
        peers = [x for x in store.merchants.values()
                 if x.get("segment") == m.get("segment")]
        lowest = _lowest_price(peers)
        price = m.get("price")
        follow_constraints = [
            c for c in new.price_constraints
            if c.kind == "follow_lowest"
            and (not c.applies_segments or m.get("segment")
                 in c.applies_segments)]
        row["must_follow_price"] = None
        row["follow_gap"] = 0.0
        if follow_constraints and price is not None and lowest is not None \
                and price > lowest:
            row["must_follow_price"] = lowest
            row["follow_gap"] = round(price - lowest, 6)

        subsidy_pressure: list[dict[str, Any]] = []
        for s in new.subsidies:
            if m.get("segment") not in s.applies_segments:
                continue
            ref = m.get("category_ref_price")
            if price is not None and ref is not None:
                cap = ref * s.max_price_ratio
                loses = price > cap
                subsidy_pressure.append({
                    "subsidy_id": s.condition_id,
                    "price_cap": round(cap, 6),
                    "would_lose_subsidy": loses,
                    "withdrawn_when_violated": s.withdrawn_when_violated,
                })
        row["subsidy_pressure"] = subsidy_pressure

        # --- 退出成本变化 ---
        if base:
            row["lock_in_delta_days"] = (
                new.exit_terms.lock_in_days - base.exit_terms.lock_in_days)
            row["penalty_delta"] = round(
                new.exit_terms.penalty_ratio - base.exit_terms.penalty_ratio,
                6)
        else:
            row["lock_in_delta_days"] = new.exit_terms.lock_in_days
            row["penalty_delta"] = new.exit_terms.penalty_ratio
        rows.append(row)

    # --- 分层汇总 ---
    segments: dict[str, dict[str, Any]] = {}
    for r in rows:
        seg = r["segment"]
        agg = segments.setdefault(seg, {
            "segment": seg, "merchants": 0, "demoted": 0,
            "follow_pressured": 0, "subsidy_at_risk": 0,
            "exit_harder": 0,
        })
        agg["merchants"] += 1
        if r["demoted"]:
            agg["demoted"] += 1
        if r["follow_gap"] > 0:
            agg["follow_pressured"] += 1
        if any(s["would_lose_subsidy"] for s in r["subsidy_pressure"]):
            agg["subsidy_at_risk"] += 1
        if r["lock_in_delta_days"] > 0 or r["penalty_delta"] > 0:
            agg["exit_harder"] += 1
    for agg in segments.values():
        n = agg["merchants"] or 1
        agg["demotion_rate"] = round(agg["demoted"] / n, 6)
        agg["follow_rate"] = round(agg["follow_pressured"] / n, 6)

    return {
        "content_hash": new_hash,
        "baseline_hash": base_hash,
        "algorithm": {"old": base.algorithm.version if base else None,
                      "new": new.algorithm.version},
        "weight_change": (
            {k: [round(base.algorithm.ranking_weights.get(k, 0.0), 6),
                 round(new.algorithm.ranking_weights.get(k, 0.0), 6)]
             for k in set(base.algorithm.ranking_weights)
             | set(new.algorithm.ranking_weights)}
            if base else {}),
        "rows": rows,
        "segments": sorted(segments.values(), key=lambda a: a["segment"]),
    }
