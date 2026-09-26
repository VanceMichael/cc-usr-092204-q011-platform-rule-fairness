"""竞争影响预警。

规则组合造成异常集中降权、被迫跟价或退出困难时，产生带证据与
解释文本的预警，并以 alert_raised 事件追加进日志，供事后复核。
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from .service import FairChangeService
from .store import parse_ts, utc_now


class AlertPolicy:
    """预警阈值，可按监管要求调整。"""

    def __init__(self, demotion_share: float = 0.6, demotion_min_count: int = 3,
                 follow_price_min_merchants: int = 3,
                 follow_price_window_hours: int = 72,
                 exit_delay_days: int = 7, exit_min_count: int = 2) -> None:
        self.demotion_share = demotion_share
        self.demotion_min_count = demotion_min_count
        self.follow_price_min_merchants = follow_price_min_merchants
        self.follow_price_window_hours = follow_price_window_hours
        self.exit_delay_days = exit_delay_days
        self.exit_min_count = exit_min_count


def analyze_competition(service: FairChangeService, policy: AlertPolicy | None = None,
                        now: str | None = None) -> list[dict]:
    policy = policy or AlertPolicy()
    metrics = service.metrics()
    alerts: list[dict] = []
    alerts.extend(_concentrated_demotion(service, metrics, policy))
    alerts.extend(_forced_price_following(service, metrics, policy))
    alerts.extend(_exit_difficulty(service, metrics, policy))
    for i, alert in enumerate(alerts, 1):
        alert["alert_id"] = f"ALT-{len(service.store.of_type('alert_raised')) + i:06d}"
        alert["raised_at"] = now or utc_now()
        service.store.append("alert_raised", "system", alert, ts=now)
    return alerts


def _concentrated_demotion(service: FairChangeService, metrics: list[dict],
                           policy: AlertPolicy) -> list[dict]:
    """异常集中降权：同一发布包生效期间，降权过度集中于某一商家群体。"""
    by_package: dict[str, list[dict]] = {}
    for m in metrics:
        if m.get("kind") != "demotion":
            continue
        pid = service.attribute_metric(m)
        if pid:
            by_package.setdefault(pid, []).append(m)
    alerts = []
    for pid, items in by_package.items():
        total = len(items)
        group, n = Counter(m.get("group", "未知群体") for m in items).most_common(1)[0]
        share = n / total
        if n >= policy.demotion_min_count and share >= policy.demotion_share:
            alerts.append({
                "kind": "concentrated_demotion",
                "package_id": pid,
                "affected_group": group,
                "evidence": {
                    "group_demotions": n,
                    "total_demotions": total,
                    "share": round(share, 4),
                    "share_threshold": policy.demotion_share,
                },
                "explanation": (
                    f"发布包 {pid} 生效期间共记录 {total} 次降权，其中「{group}」"
                    f"群体 {n} 次（占 {share:.0%}），超过集中阈值 "
                    f"{policy.demotion_share:.0%}，疑似规则组合造成异常集中降权，"
                    "请复核流量入口与价格约束的叠加效果。"
                ),
            })
    return alerts


def _forced_price_following(service: FairChangeService, metrics: list[dict],
                            policy: AlertPolicy) -> list[dict]:
    """被迫跟价：发布包上线后窗口期内，适用商家集中向价格约束边界调价。"""
    alerts = []
    window = timedelta(hours=policy.follow_price_window_hours)
    for pkg in service.packages():
        pid = pkg["package_id"]
        published = service.publish_time(pid)
        if published is None:
            continue
        constraints = pkg["content"].get("price_constraints", {})
        if "max_price" in constraints:
            direction, bound = "down", constraints["max_price"]
        elif "min_price" in constraints:
            direction, bound = "up", constraints["min_price"]
        else:
            continue
        merchants = set()
        for m in metrics:
            if m.get("kind") != "price_change" or m.get("direction") != direction:
                continue
            ts = m.get("ts")
            if not ts or not (published <= parse_ts(ts) <= published + window):
                continue
            if not service.scope_matches(pid, m["merchant_id"], m.get("groups", ())):
                continue
            merchants.add(m["merchant_id"])
        if len(merchants) >= policy.follow_price_min_merchants:
            alerts.append({
                "kind": "forced_price_following",
                "package_id": pid,
                "evidence": {
                    "merchants": sorted(merchants),
                    "merchant_count": len(merchants),
                    "direction": direction,
                    "bound": bound,
                    "window_hours": policy.follow_price_window_hours,
                },
                "explanation": (
                    f"发布包 {pid} 上线后 {policy.follow_price_window_hours} 小时内，"
                    f"{len(merchants)} 家适用商家向约束边界 {bound} 调价"
                    f"（方向 {direction}），疑似价格约束与补贴条件叠加导致被迫跟价。"
                ),
            })
    return alerts


def _exit_difficulty(service: FairChangeService, metrics: list[dict],
                     policy: AlertPolicy) -> list[dict]:
    """退出困难：适用商家退出被拦截或拖延超过阈值天数。"""
    by_package: dict[str, list[dict]] = {}
    for m in metrics:
        if m.get("kind") != "exit_request":
            continue
        blocked = m.get("status") == "blocked" or m.get("delay_days", 0) > policy.exit_delay_days
        if not blocked:
            continue
        pid = service.attribute_metric(m)
        if pid:
            by_package.setdefault(pid, []).append(m)
    alerts = []
    for pid, items in by_package.items():
        if len(items) >= policy.exit_min_count:
            merchants = sorted({m["merchant_id"] for m in items})
            alerts.append({
                "kind": "exit_difficulty",
                "package_id": pid,
                "evidence": {
                    "blocked_exits": len(items),
                    "merchants": merchants,
                    "delay_threshold_days": policy.exit_delay_days,
                },
                "explanation": (
                    f"发布包 {pid} 适用范围内 {len(items)} 起退出请求被拦截或拖延超过 "
                    f"{policy.exit_delay_days} 天（涉及商家 {merchants}），"
                    "疑似规则组合抬高商家退出成本。"
                ),
            })
    return alerts
