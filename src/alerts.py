"""可解释竞争预警。

预警只针对"规则组合"而非单一商家选择：当多份规则组件叠加，使某一分层
异常集中地降权、被迫跟价或退出困难时触发。每条预警都给出：

- 触发的规则组件组合（算法权重 / 价格约束 / 补贴条件 / 退出条款的具体编号）；
- 受影响分层与商家；
- 可复算的指标值与阈值；
- 自然语言解释。

评估结果追加为只增 ``ALERT_RAISED`` 事件；预警可被 ``RESOLVED`` 但不会
被删除。
"""

from __future__ import annotations

from typing import Any

from .impact import compare
from .models import RulePackage
from .store import RuleStore

# 默认阈值（可按场景覆盖）
DEMOTION_RATE_THRESHOLD = 0.5
FOLLOW_RATE_THRESHOLD = 0.5
EXIT_HARD_RATE_THRESHOLD = 0.3


def _component_combo(pkg: RulePackage) -> dict[str, list[str]]:
    return {
        "algorithm_version": [pkg.algorithm.version],
        "price_constraints": [c.constraint_id for c in pkg.price_constraints],
        "subsidies": [s.condition_id for s in pkg.subsidies],
        "exit_terms": (
            [f"lock_in={pkg.exit_terms.lock_in_days}d",
             f"penalty={pkg.exit_terms.penalty_ratio}"]
            if pkg.exit_terms.lock_in_days or pkg.exit_terms.penalty_ratio
            else []),
    }


def evaluate(store: RuleStore, new_hash: str,
             base_hash: str | None = None,
             thresholds: dict[str, float] | None = None,
             record_actor: str | None = None) -> list[dict[str, Any]]:
    th = {
        "demotion": DEMOTION_RATE_THRESHOLD,
        "follow": FOLLOW_RATE_THRESHOLD,
        "exit": EXIT_HARD_RATE_THRESHOLD,
    }
    if thresholds:
        th.update(thresholds)

    report = compare(store, new_hash, base_hash)
    pkg = store.package(new_hash)
    rows = report["rows"]
    by_segment = {a["segment"]: a for a in report["segments"]}
    alerts: list[dict[str, Any]] = []

    exit_tightened = (pkg.exit_terms.lock_in_days > 0
                      or pkg.exit_terms.penalty_ratio > 0)

    for seg, agg in by_segment.items():
        seg_rows = [r for r in rows if r["segment"] == seg]
        combo = _component_combo(pkg)

        def applies(c) -> bool:
            return not c.applies_segments or seg in c.applies_segments

        follow_ids = [c.constraint_id for c in pkg.price_constraints
                      if c.kind == "follow_lowest" and applies(c)]
        demotion_enf = [c.constraint_id for c in pkg.price_constraints
                        if c.enforcement == "demotion" and applies(c)]
        withdraw_ids = [s.condition_id for s in pkg.subsidies
                        if seg in s.applies_segments and
                        s.withdrawn_when_violated]

        # 1) 异常集中降权：算法权重变化 + 降权执行约束，集中作用于同一分层
        if agg["merchants"] >= 2 and agg["demotion_rate"] >= th["demotion"]:
            victims = [r["merchant_id"] for r in seg_rows if r["demoted"]]
            weight_shifts = {
                k: v for k, v in report["weight_change"].items()
                if v[0] != v[1]
            }
            alerts.append({
                "code": "CONCENTRATED_DEMOTION",
                "segment": seg,
                "severity": "high" if agg["demotion_rate"] >= 0.8 else "medium",
                "metric": {"demotion_rate": agg["demotion_rate"],
                           "threshold": th["demotion"],
                           "demoted": agg["demoted"],
                           "merchants": agg["merchants"]},
                "rule_combination": {
                    **combo,
                    "ranking_weight_changes": weight_shifts,
                    "demotion_enforcement": demotion_enf,
                },
                "affected_merchants": victims,
                "explanation": (
                    f"分层[{seg}] {agg['demoted']}/{agg['merchants']} 家名次下滑"
                    f"（降权率 {agg['demotion_rate']:.0%} ≥ {th['demotion']:.0%}）。"
                    f"成因为算法版本 {pkg.algorithm.version} 的权重变化"
                    f"（{', '.join(weight_shifts) or '排序因子结构调整'}）"
                    f"与价格约束 {demotion_enf or follow_ids} 的降权执行叠加，"
                    "需判定是否构成对特定商家群体的异常集中降权。"),
            })

        # 2) 被迫跟价：跟价约束 + 违反即撤补贴，同时压向同一分层
        pressured = [r for r in seg_rows if r["follow_gap"] > 0]
        at_risk = [r for r in seg_rows
                   if any(s["would_lose_subsidy"] for s in r["subsidy_pressure"])]
        squeezed = [r["merchant_id"] for r in pressured
                    if any(s["would_lose_subsidy"]
                           for s in r["subsidy_pressure"])]
        if follow_ids and withdraw_ids and agg["merchants"] >= 2 and \
                len(squeezed) / agg["merchants"] >= th["follow"]:
            alerts.append({
                "code": "FORCED_PRICE_FOLLOWING",
                "segment": seg,
                "severity": "high",
                "metric": {"squeezed_rate": round(
                               len(squeezed) / agg["merchants"], 6),
                           "threshold": th["follow"],
                           "follow_pressured": len(pressured),
                           "subsidy_at_risk": len(at_risk),
                           "merchants": agg["merchants"]},
                "rule_combination": {
                    **combo,
                    "follow_lowest_constraints": follow_ids,
                    "withdrawn_subsidies": withdraw_ids,
                },
                "affected_merchants": squeezed,
                "explanation": (
                    f"分层[{seg}] 有 {len(squeezed)} 家同时被跟价约束 "
                    f"{follow_ids} 要求追随最低价，且一旦不达标即被补贴条件 "
                    f"{withdraw_ids} 撤销补贴。'必须降价'与'不降价即失补贴'"
                    "双向夹击，易诱发低价内耗，应核查该组合是否被迫跟价。"),
            })

        # 3) 退出困难：退出成本上升的同一批商家又正承受降权/撤补贴
        harder = [r for r in seg_rows
                  if r["lock_in_delta_days"] > 0 or r["penalty_delta"] > 0]
        trapped = [r["merchant_id"] for r in harder
                   if r["demoted"] or
                   any(s["would_lose_subsidy"] for s in r["subsidy_pressure"])]
        if exit_tightened and agg["merchants"] >= 2 and \
                len(trapped) / agg["merchants"] >= th["exit"]:
            alerts.append({
                "code": "EXIT_DIFFICULTY",
                "segment": seg,
                "severity": "high",
                "metric": {"trapped_rate": round(
                               len(trapped) / agg["merchants"], 6),
                           "threshold": th["exit"],
                           "exit_harder": len(harder),
                           "trapped": len(trapped),
                           "merchants": agg["merchants"]},
                "rule_combination": {
                    **combo,
                    "lock_in_days": pkg.exit_terms.lock_in_days,
                    "penalty_ratio": pkg.exit_terms.penalty_ratio,
                },
                "affected_merchants": trapped,
                "explanation": (
                    f"分层[{seg}] {len(trapped)} 家商家退出成本上升"
                    f"（锁定期 {pkg.exit_terms.lock_in_days} 天、违约金比例 "
                    f"{pkg.exit_terms.penalty_ratio}），同期又遭遇降权或补贴"
                    "流失。'走不掉'与'留下即受损'叠加，构成退出困难风险。"),
            })

    if record_actor:
        for a in alerts:
            store.ledger.append("ALERT_RAISED", {
                "content_hash": new_hash,
                "baseline_hash": base_hash,
                "code": a["code"],
                "severity": a["severity"],
                "segment": a["segment"],
                "metric": a["metric"],
                "rule_combination": a["rule_combination"],
                "affected_merchants": a["affected_merchants"],
                "explanation": a["explanation"],
            }, record_actor)

    return alerts
