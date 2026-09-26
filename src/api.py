"""应用服务层：在 RuleStore 之上施加角色权限与输入装配。

角色与权限：
- OPERATOR 运营：建包、通知、灰度、上线、紧急止损、回滚、豁免；
- APPROVER 审批人（携带 team）：仅会签；
- AUDITOR 审查人员：只读 + 复算 + 反查 + 账本校验，**不能修改任何生产
  规则**（不能建包/审批/放量/止损/回滚/豁免）；
- MERCHANT 商家：查看与自身相关信息、提交申诉；
- 未认证调用仅可健康检查。

审批团队可并行调用 approve；临界区在 store 内串行，因此多团队同时审批
也只会产生一个正式版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import alerts as alerts_mod
from . import impact, trace as trace_mod
from .disclosure import (
    AUDITOR, MERCHANT, OPERATOR, PUBLIC, DisclosureView,
)
from .models import RulePackage
from .store import (
    REQUIRED_TEAMS, RuleError, RuleStore,
)

APPROVER = "APPROVER"


class PermissionError_(Exception):
    """角色无权执行该操作。"""


@dataclass
class Principal:
    role: str
    name: str
    team: str | None = None
    merchant_id: str | None = None


# 各类操作允许的角色
_MUTATE_RULES = {OPERATOR}
_APPROVE = {APPROVER}
_READ_INTERNAL = {OPERATOR, AUDITOR, APPROVER}


class RuleApi:
    def __init__(self, store: RuleStore) -> None:
        self.store = store

    # ------------------------------------------------------------------ #
    def _require(self, principal: Principal, allowed: set[str]) -> None:
        if principal.role not in allowed:
            raise PermissionError_(
                f"角色 {principal.role} 无权执行该操作")

    def _view(self, principal: Principal) -> DisclosureView:
        return DisclosureView(
            self.store, principal.role, principal.name,
            principal.merchant_id)

    # ---- 目录 ----------------------------------------------------------
    def register_merchant(self, p: Principal, merchant: dict) -> None:
        self._require(p, {OPERATOR})
        self.store.register_merchant(p.name, merchant)

    # ---- 发布包 --------------------------------------------------------
    def create_package(self, p: Principal, data: dict) -> str:
        self._require(p, _MUTATE_RULES)
        pkg = RulePackage.from_dict(data)
        return self.store.create_package(p.name, pkg)

    def get_package(self, p: Principal, package_hash: str) -> dict[str, Any]:
        self._require(p, _READ_INTERNAL | {MERCHANT})
        return self._view(p).package_view(package_hash)

    def list_packages(self, p: Principal) -> list[dict[str, Any]]:
        self._require(p, _READ_INTERNAL)
        out = []
        for pid, hashes in sorted(self.store.index.items()):
            for h in hashes:
                snap = self.store.packages[h]
                out.append({
                    "package_id": pid,
                    "revision": snap["revision"],
                    "content_hash": h,
                    "status": self.store.status.get(h),
                    "algorithm_version": snap["algorithm"]["version"],
                })
        return out

    # ---- 审批 ----------------------------------------------------------
    def approve(self, p: Principal, package_hash: str,
                comment: str = "") -> None:
        self._require(p, _APPROVE)
        if p.team not in REQUIRED_TEAMS:
            raise RuleError(f"审批人所属团队无效：{p.team}")
        self.store.approve(p.name, p.team, package_hash, comment)

    # ---- 上线闸门 ------------------------------------------------------
    def preflight(self, p: Principal, package_hash: str) -> dict[str, Any]:
        self._require(p, _READ_INTERNAL)
        report = self.store.preflight(package_hash)
        # 审查视图：商家名单假名化
        if p.role == AUDITOR:
            view = self._view(p)
            report = dict(report)
            report["affected_merchants"] = [
                view.redact_merchant(m) for m in report["affected_merchants"]]
            report["outstanding_notifications"] = [
                {**n, "merchant_id": view.redact_merchant(n["merchant_id"])}
                for n in report["outstanding_notifications"]]
        return report

    def notify(self, p: Principal, package_hash: str, merchant_id: str,
               channel: str | None = None, ts: float | None = None) -> None:
        self._require(p, _MUTATE_RULES)
        self.store.record_notification(p.name, package_hash, merchant_id,
                                       channel, ts)

    # ---- 灰度 / 上线 ---------------------------------------------------
    def start_gray(self, p: Principal, package_hash: str, percent: int,
                   cohorts: list[str] | None = None) -> None:
        self._require(p, _MUTATE_RULES)
        self.store.start_gray(p.name, package_hash, percent,
                              tuple(cohorts or ()))

    def adjust_gray(self, p: Principal, percent: int) -> None:
        self._require(p, _MUTATE_RULES)
        self.store.adjust_gray(p.name, percent)

    def activate(self, p: Principal, package_hash: str) -> None:
        self._require(p, _MUTATE_RULES)
        self.store.activate(p.name, package_hash)

    def emergency_stop(self, p: Principal, package_hash: str,
                       reason: str) -> None:
        self._require(p, _MUTATE_RULES)
        self.store.emergency_stop(p.name, package_hash, reason)

    def rollback(self, p: Principal, target_hash: str, reason: str) -> None:
        self._require(p, _MUTATE_RULES)
        self.store.rollback(p.name, target_hash, reason)

    def mark_baseline(self, p: Principal, package_hash: str) -> None:
        self._require(p, _MUTATE_RULES)
        self.store.mark_baseline(p.name, package_hash)

    # ---- 豁免 ----------------------------------------------------------
    def grant_exemption(self, p: Principal, package_hash: str,
                        merchant_id: str, reason: str,
                        constraint_ids: list[str] | None = None) -> str:
        self._require(p, _MUTATE_RULES)
        return self.store.grant_exemption(
            p.name, package_hash, merchant_id, reason,
            tuple(constraint_ids) if constraint_ids else ("*",))

    def revoke_exemption(self, p: Principal, exemption_id: str,
                         reason: str) -> None:
        self._require(p, _MUTATE_RULES)
        self.store.revoke_exemption(p.name, exemption_id, reason)

    # ---- 申诉 ----------------------------------------------------------
    def file_appeal(self, p: Principal, package_hash: str, exposure_id: str,
                    reason: str, merchant_id: str | None = None) -> str:
        self._require(p, {MERCHANT, OPERATOR})
        if p.role == MERCHANT:
            # 商家只能就自己的曝光申诉
            ex = next((x for x in self.store.exposures
                       if x["exposure_id"] == exposure_id), None)
            if ex is None or ex["merchant_id"] != p.merchant_id:
                raise PermissionError_("只能就本人的曝光记录申诉")
            mid = p.merchant_id
        else:
            if not merchant_id:
                raise RuleError("运营代提交申诉需提供 merchant_id")
            mid = merchant_id
        return self.store.file_appeal(p.name, mid, package_hash,
                                      exposure_id, reason)

    def resolve_appeal(self, p: Principal, appeal_id: str, uphold: bool,
                       resolution: str) -> None:
        self._require(p, _MUTATE_RULES)
        self.store.resolve_appeal(p.name, appeal_id, uphold, resolution)

    # ---- 曝光归属（生产遥测，仅运营侧写入） ----------------------------
    def resolve_exposure(self, p: Principal, merchant_id: str,
                         entry_id: str, cohort: str = "") -> dict[str, Any]:
        self._require(p, _MUTATE_RULES)
        return self.store.resolve_exposure(p.name, merchant_id, entry_id,
                                           cohort)

    # ---- 复算 / 预警（只读） -------------------------------------------
    def impact(self, p: Principal, new_hash: str,
               base_hash: str | None = None) -> dict[str, Any]:
        self._require(p, _READ_INTERNAL)
        report = impact.compare(self.store, new_hash, base_hash)
        return self._view(p).impact_view(report)

    def evaluate_alerts(self, p: Principal, new_hash: str,
                        base_hash: str | None = None,
                        thresholds: dict | None = None,
                        record: bool = False) -> list[dict[str, Any]]:
        self._require(p, _READ_INTERNAL)
        # 审查人员可触发复算与预警评估；预警是只增事实记录，记录动作仅运营
        actor = p.name if (record and p.role == OPERATOR) else None
        found = alerts_mod.evaluate(
            self.store, new_hash, base_hash, thresholds,
            record_actor=actor)
        if p.role == AUDITOR:
            view = self._view(p)
            for a in found:
                a["affected_merchants"] = [
                    view.redact_merchant(m) for m in a["affected_merchants"]]
        return found

    # ---- 反查（只读） --------------------------------------------------
    def trace_complaint(self, p: Principal, merchant_id: str,
                        entry_id: str | None = None,
                        start_ts: float | None = None,
                        end_ts: float | None = None) -> dict[str, Any]:
        self._require(p, _READ_INTERNAL | {MERCHANT})
        if p.role == MERCHANT:
            if merchant_id != p.merchant_id:
                raise PermissionError_("商家只能反查本人投诉")
        result = trace_mod.trace_complaint(
            self.store, merchant_id, entry_id, start_ts, end_ts)
        if p.role == AUDITOR:
            # 审查链路中出现的所有商家身份一律假名化，保留可关联性
            view = self._view(p)
            result["merchant_id"] = view.redact_merchant(result["merchant_id"])
            for ex in result.get("exposures", []):
                ex["merchant_id"] = view.redact_merchant(ex["merchant_id"])
            for chain in result.get("rule_chains", []):
                for e in chain.get("exemptions", []):
                    e["merchant_id"] = view.redact_merchant(e["merchant_id"])
                for a in chain.get("appeals", []):
                    a["merchant_id"] = view.redact_merchant(a["merchant_id"])
        return result

    def trace_metric(self, p: Principal, segment: str,
                     start_ts: float | None = None,
                     end_ts: float | None = None) -> dict[str, Any]:
        self._require(p, _READ_INTERNAL)
        result = trace_mod.trace_metric(self.store, segment, start_ts, end_ts)
        if p.role == AUDITOR:
            view = self._view(p)
            for ev in result.get("alerts", []):
                ids = ev.get("payload", {}).get("affected_merchants", [])
                ev["payload"]["affected_merchants"] = [
                    view.redact_merchant(m) for m in ids]
            for chain in result.get("active_packages", []):
                for e in chain.get("exemptions", []):
                    e["merchant_id"] = view.redact_merchant(e["merchant_id"])
                for a in chain.get("appeals", []):
                    a["merchant_id"] = view.redact_merchant(a["merchant_id"])
        return result

    # ---- 账本校验 ------------------------------------------------------
    def verify_ledger(self, p: Principal) -> dict[str, Any]:
        self._require(p, _READ_INTERNAL)
        ok, msg = self.store.ledger.verify()
        return {"intact": ok, "message": msg,
                "event_count": len(self.store.ledger.events()),
                "head": self.store.ledger.head()}

    def history(self, p: Principal, package_hash: str) -> list[dict[str, Any]]:
        self._require(p, _READ_INTERNAL)
        return [e.to_dict() for e in self.store.history(package_hash)]
