"""规则中枢：发布包登记、多团队审批、灰度放量、逐次曝光归属、
紧急止损 / 回滚 / 豁免 / 申诉。

设计约束：
- 发布包一经登记不可修改，改规则只能发新修订；
- 并发审批用锁串行化，同一时刻全平台只有一个正式（PRODUCTION）版本；
- 所有状态变迁写入只增账本，状态本身是事件的投影，可整体重放重建。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .ledger import Ledger
from .models import (
    RulePackage,
    to_jsonable,
)

DRAFT = "DRAFT"
APPROVED = "APPROVED"
GRAY = "GRAY"
PRODUCTION = "PRODUCTION"
KILL_SWITCHED = "KILL_SWITCHED"
SUPERSEDED = "SUPERSEDED"

# 上线一个发布包需要会签的团队；多团队并行审批，缺一不可
REQUIRED_TEAMS = ("规则治理", "算法", "合规", "商家生态")


class RuleError(Exception):
    """业务规则违例（未满足上线闸门、版本冲突等）。"""


@dataclass
class Rollout:
    package_hash: str
    percent: int
    cohorts: tuple[str, ...]
    started_ts: float


@dataclass
class Exemption:
    exemption_id: str
    package_hash: str
    merchant_id: str
    constraint_ids: tuple[str, ...]  # ("*",) 表示该商家豁免包内全部约束
    reason: str
    grantor: str
    ts: float
    revoked: bool = False
    revoke_ts: float | None = None


@dataclass
class Appeal:
    appeal_id: str
    merchant_id: str
    package_hash: str
    exposure_id: str
    reason: str
    ts: float
    status: str = "PENDING"  # PENDING / UPHELD / REJECTED
    resolution: str = ""
    resolved_ts: float | None = None


class RuleStore:
    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self.ledger = Ledger(clock=clock)
        self._lock = threading.RLock()
        # 发布包快照：content_hash -> 冻结结构
        self.packages: dict[str, dict[str, Any]] = {}
        self.index: dict[str, list[str]] = {}   # package_id -> [content_hash] 按修订
        self.merchants: dict[str, dict[str, Any]] = {}
        self.baseline_hash: str | None = None
        # 审批：hash -> {team: (approver, ts, verdict, comment)}
        self.approvals: dict[str, dict[str, dict[str, Any]]] = {}
        self.status: dict[str, str] = {}
        self.gray: Rollout | None = None
        self.production_hash: str | None = None
        self.notifications: dict[str, dict[str, dict[str, Any]]] = {}
        self.exemptions: list[Exemption] = []
        self.appeals: dict[str, Appeal] = {}
        self.exposures: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 商家目录（复算输入）
    # ------------------------------------------------------------------ #
    def register_merchant(self, actor: str, merchant: dict[str, Any]) -> None:
        with self._lock:
            self.merchants[merchant["merchant_id"]] = merchant
            self.ledger.append("MERCHANT_REGISTERED",
                               {"merchant_id": merchant["merchant_id"],
                                "segment": merchant.get("segment")}, actor)

    def package(self, package_hash: str) -> RulePackage:
        return RulePackage.from_dict(self.packages[package_hash])

    def latest_revision(self, package_id: str) -> int:
        hashes = self.index.get(package_id, [])
        if not hashes:
            return 0
        return max(self.packages[h]["revision"] for h in hashes)

    # ------------------------------------------------------------------ #
    # 1. 创建并冻结发布包
    # ------------------------------------------------------------------ #
    def create_package(self, actor: str, pkg: RulePackage) -> str:
        with self._lock:
            snap = to_jsonable(pkg)
            h = pkg.content_hash
            if h in self.packages:
                raise RuleError("内容相同的发布包已存在，禁止重复登记")
            expect_rev = self.latest_revision(pkg.package_id) + 1
            if pkg.revision != expect_rev:
                raise RuleError(
                    f"修订号必须递增：期望 {expect_rev}，收到 {pkg.revision}")
            self.packages[h] = snap
            self.index.setdefault(pkg.package_id, []).append(h)
            self.approvals[h] = {}
            self.status[h] = DRAFT
            self.notifications[h] = {}
            self.ledger.append("PACKAGE_CREATED", {
                "package_id": pkg.package_id,
                "revision": pkg.revision,
                "content_hash": h,
                "algorithm_version": pkg.algorithm.version,
                "segments": list(pkg.scope.segments),
                "entries": [e.entry_id for e in pkg.entries],
                "constraint_ids": [c.constraint_id for c in pkg.price_constraints],
                "subsidy_ids": [s.condition_id for s in pkg.subsidies],
                "lock_in_days": pkg.exit_terms.lock_in_days,
                "penalty_ratio": pkg.exit_terms.penalty_ratio,
                "notification": snap["notification"],
                "appeal": snap["appeal"],
                "created_by": pkg.created_by,
            }, actor)
            return h

    # ------------------------------------------------------------------ #
    # 2. 多团队会签
    # ------------------------------------------------------------------ #
    def approve(self, actor: str, team: str, package_hash: str,
                comment: str = "") -> None:
        with self._lock:
            if team not in REQUIRED_TEAMS:
                raise RuleError(f"未知会签团队：{team}")
            if package_hash not in self.packages:
                raise RuleError("发布包不存在")
            if self.status[package_hash] != DRAFT:
                raise RuleError("仅 DRAFT 状态可审批")
            self.approvals[package_hash][team] = {
                "approver": actor, "ts": self.clock(),
                "verdict": "APPROVE", "comment": comment,
            }
            self.ledger.append("PACKAGE_APPROVED", {
                "content_hash": package_hash, "team": team,
                "approver": actor, "comment": comment,
            }, actor)
            if set(self.approval_basis(package_hash)) >= set(REQUIRED_TEAMS):
                self.status[package_hash] = APPROVED
                self.ledger.append("PACKAGE_APPROVAL_COMPLETE", {
                    "content_hash": package_hash,
                    "teams": list(REQUIRED_TEAMS),
                }, actor)

    def approval_basis(self, package_hash: str) -> dict[str, dict[str, Any]]:
        """批准依据：团队 -> 审批人/时间/意见，供事后反查。"""
        return dict(self.approvals.get(package_hash, {}))

    # ------------------------------------------------------------------ #
    # 3. 受影响群体与通知闸门
    # ------------------------------------------------------------------ #
    def affected_merchants(self, package_hash: str) -> list[str]:
        pkg = self.package(package_hash)
        return [m for m, d in sorted(self.merchants.items())
                if pkg.scope.matches(d)]

    def _notification_requirements(self, package_hash: str) -> list[str]:
        """按通知规则受众分层，列出必须通知到的商家。"""
        pkg = self.package(package_hash)
        audiences = set(pkg.notification.audiences)
        return [m for m in self.affected_merchants(package_hash)
                if self.merchants[m].get("segment") in audiences]

    def record_notification(self, actor: str, package_hash: str,
                            merchant_id: str, channel: str | None = None,
                            ts: float | None = None) -> None:
        with self._lock:
            if package_hash not in self.packages:
                raise RuleError("发布包不存在")
            if merchant_id not in self.merchants:
                raise RuleError("商家不存在")
            when = ts if ts is not None else self.clock()
            pkg = self.package(package_hash)
            self.notifications[package_hash][merchant_id] = {
                "ts": when,
                "channel": channel or pkg.notification.channel,
            }
            self.ledger.append("NOTIFICATION_SENT", {
                "content_hash": package_hash,
                "merchant_id": merchant_id,
                "channel": channel or pkg.notification.channel,
                "ts": when,
            }, actor)

    def outstanding_notifications(self, package_hash: str,
                                  now: float | None = None
                                  ) -> list[dict[str, Any]]:
        """未通知或提前期不足的商家，均视为未完成通知。"""
        now = self.clock() if now is None else now
        pkg = self.package(package_hash)
        lead = pkg.notification.lead_days * 86400
        out: list[dict[str, Any]] = []
        for m in self._notification_requirements(package_hash):
            rec = self.notifications[package_hash].get(m)
            if rec is None:
                out.append({"merchant_id": m, "reason": "NOT_NOTIFIED"})
            elif now - rec["ts"] < lead:
                out.append({"merchant_id": m, "reason": "LEAD_TIME_NOT_MET",
                            "ready_ts": rec["ts"] + lead})
        return out

    def preflight(self, package_hash: str) -> dict[str, Any]:
        """上线前清单：受影响群体 + 未完成通知 + 审批缺口。"""
        with self._lock:
            affected = self.affected_merchants(package_hash)
            segments = sorted({self.merchants[m].get("segment")
                               for m in affected})
            missing_teams = [t for t in REQUIRED_TEAMS
                             if t not in self.approval_basis(package_hash)]
            outstanding = self.outstanding_notifications(package_hash)
            return {
                "content_hash": package_hash,
                "package_id": self.packages[package_hash]["package_id"],
                "revision": self.packages[package_hash]["revision"],
                "affected_segments": segments,
                "affected_merchant_count": len(affected),
                "affected_merchants": affected,
                "missing_approval_teams": missing_teams,
                "outstanding_notifications": outstanding,
                "ready": not missing_teams and not outstanding,
            }

    def _enforce_gate(self, package_hash: str) -> None:
        report = self.preflight(package_hash)
        blockers: list[str] = []
        if report["missing_approval_teams"]:
            blockers.append("缺少会签：" + ",".join(report["missing_approval_teams"]))
        if report["outstanding_notifications"]:
            blockers.append(
                f"未完成通知 {len(report['outstanding_notifications'])} 家")
        if blockers:
            raise RuleError("上线闸门未通过：" + "；".join(blockers))

    # ------------------------------------------------------------------ #
    # 4. 灰度与正式发布（唯一正式版本）
    # ------------------------------------------------------------------ #
    def start_gray(self, actor: str, package_hash: str, percent: int,
                   cohorts: tuple[str, ...] = ()) -> None:
        with self._lock:
            if not 0 < percent <= 100:
                raise RuleError("灰度比例须在 1~100")
            if self.status.get(package_hash) not in (APPROVED, GRAY):
                raise RuleError("仅会签完成的发布包可灰度")
            self._enforce_gate(package_hash)
            if self.gray and self.gray.package_hash != package_hash:
                raise RuleError("存在另一进行中的灰度，同一时刻只允许一条灰度")
            self.gray = Rollout(package_hash, percent, tuple(cohorts),
                                self.clock())
            self.status[package_hash] = GRAY
            self.ledger.append("GRAY_STARTED", {
                "content_hash": package_hash, "percent": percent,
                "cohorts": list(cohorts),
            }, actor)

    def adjust_gray(self, actor: str, percent: int) -> None:
        with self._lock:
            if not self.gray:
                raise RuleError("没有进行中的灰度")
            if not 0 < percent <= 100:
                raise RuleError("灰度比例须在 1~100")
            old = self.gray
            self.gray = Rollout(old.package_hash, percent, old.cohorts,
                                self.clock())
            self.ledger.append("GRAY_ADJUSTED", {
                "content_hash": old.package_hash,
                "old_percent": old.percent, "new_percent": percent,
            }, actor)

    def activate(self, actor: str, package_hash: str) -> None:
        """全量上线：闸门通过后成为唯一正式版本，旧正式版本标记被取代。"""
        with self._lock:
            if self.status.get(package_hash) not in (APPROVED, GRAY):
                raise RuleError("仅会签完成/灰度中的发布包可上线")
            self._enforce_gate(package_hash)
            previous = self.production_hash
            self.production_hash = package_hash
            self.status[package_hash] = PRODUCTION
            self.gray = None
            if previous and previous != package_hash:
                self.status[previous] = SUPERSEDED
            self.ledger.append("PRODUCTION_ACTIVATED", {
                "content_hash": package_hash,
                "superseded": previous,
            }, actor)

    # ------------------------------------------------------------------ #
    # 5. 紧急止损 / 回滚（只追加，不抹历史）
    # ------------------------------------------------------------------ #
    def emergency_stop(self, actor: str, package_hash: str,
                       reason: str) -> None:
        with self._lock:
            if self.status.get(package_hash) not in (GRAY, PRODUCTION):
                raise RuleError("仅灰度/正式中的版本可紧急止损")
            if self.gray and self.gray.package_hash == package_hash:
                self.gray = None
            if self.production_hash == package_hash:
                # 正式版本止损后回落到基线，直到显式回滚到旧版
                self.production_hash = self.baseline_hash
            self.status[package_hash] = KILL_SWITCHED
            self.ledger.append("EMERGENCY_STOP", {
                "content_hash": package_hash, "reason": reason,
            }, actor)

    def rollback(self, actor: str, target_hash: str, reason: str) -> None:
        """回滚到历史发布包：追加回滚事件并重新激活目标包。

        被止损/取代的历史记录原样保留；目标包自身的修订与审批链不被改写。
        """
        with self._lock:
            if target_hash not in self.packages:
                raise RuleError("回滚目标不存在")
            if self.status.get(target_hash) == GRAY:
                raise RuleError("不能回滚到灰度版本")
            current = self.production_hash
            self.production_hash = target_hash
            self.status[target_hash] = PRODUCTION
            self.gray = None
            if current and current != target_hash and \
                    self.status.get(current) == PRODUCTION:
                self.status[current] = SUPERSEDED
            self.ledger.append("ROLLBACK", {
                "from_hash": current, "to_hash": target_hash,
                "reason": reason,
            }, actor)

    def mark_baseline(self, actor: str, package_hash: str) -> None:
        with self._lock:
            self.baseline_hash = package_hash
            self.ledger.append("BASELINE_MARKED",
                               {"content_hash": package_hash}, actor)

    # ------------------------------------------------------------------ #
    # 6. 豁免（只增；撤销同样留痕）
    # ------------------------------------------------------------------ #
    def grant_exemption(self, actor: str, package_hash: str, merchant_id: str,
                        reason: str, constraint_ids: tuple[str, ...] = ("*",)
                        ) -> str:
        with self._lock:
            if package_hash not in self.packages:
                raise RuleError("发布包不存在")
            if merchant_id not in self.merchants:
                raise RuleError("商家不存在")
            eid = "EX-" + hashlib.sha1(
                f"{package_hash}|{merchant_id}|{self.clock()}".encode()
            ).hexdigest()[:10]
            ex = Exemption(eid, package_hash, merchant_id,
                           tuple(constraint_ids), reason, actor, self.clock())
            self.exemptions.append(ex)
            self.ledger.append("EXEMPTION_GRANTED", {
                "exemption_id": eid, "content_hash": package_hash,
                "merchant_id": merchant_id,
                "constraint_ids": list(constraint_ids),
                "reason": reason, "grantor": actor,
            }, actor)
            return eid

    def revoke_exemption(self, actor: str, exemption_id: str,
                         reason: str) -> None:
        with self._lock:
            for ex in self.exemptions:
                if ex.exemption_id == exemption_id and not ex.revoked:
                    ex.revoked = True
                    ex.revoke_ts = self.clock()
                    self.ledger.append("EXEMPTION_REVOKED", {
                        "exemption_id": exemption_id,
                        "content_hash": ex.package_hash,
                        "merchant_id": ex.merchant_id, "reason": reason,
                    }, actor)
                    return
            raise RuleError("有效豁免不存在")

    def active_exemptions(self, package_hash: str, merchant_id: str
                          ) -> list[Exemption]:
        return [e for e in self.exemptions
                if not e.revoked and e.package_hash == package_hash
                and e.merchant_id == merchant_id]

    # ------------------------------------------------------------------ #
    # 7. 申诉与待裁冻结
    # ------------------------------------------------------------------ #
    def file_appeal(self, actor: str, merchant_id: str, package_hash: str,
                    exposure_id: str, reason: str) -> str:
        with self._lock:
            aid = "AP-" + hashlib.sha1(
                f"{merchant_id}|{exposure_id}|{self.clock()}".encode()
            ).hexdigest()[:10]
            appeal = Appeal(aid, merchant_id, package_hash, exposure_id,
                            reason, self.clock())
            self.appeals[aid] = appeal
            self.ledger.append("APPEAL_FILED", {
                "appeal_id": aid, "merchant_id": merchant_id,
                "content_hash": package_hash,
                "exposure_id": exposure_id, "reason": reason,
            }, actor)
            return aid

    def resolve_appeal(self, actor: str, appeal_id: str, uphold: bool,
                       resolution: str) -> None:
        with self._lock:
            appeal = self.appeals.get(appeal_id)
            if not appeal or appeal.status != "PENDING":
                raise RuleError("申诉不存在或已裁决")
            appeal.status = "UPHELD" if uphold else "REJECTED"
            appeal.resolution = resolution
            appeal.resolved_ts = self.clock()
            self.ledger.append("APPEAL_RESOLVED", {
                "appeal_id": appeal_id,
                "merchant_id": appeal.merchant_id,
                "content_hash": appeal.package_hash,
                "verdict": appeal.status, "resolution": resolution,
            }, actor)

    def pending_appeal_freeze(self, package_hash: str, merchant_id: str
                              ) -> list[str]:
        """该商家在该包下处于待裁状态的申诉；申诉规则要求冻结执行时，
        返回的申诉将使降权/扣款等执行暂缓。"""
        pkg = self.package(package_hash)
        if not pkg.appeal.freeze_pending:
            return []
        return [a.appeal_id for a in self.appeals.values()
                if a.status == "PENDING" and a.package_hash == package_hash
                and a.merchant_id == merchant_id]

    # ------------------------------------------------------------------ #
    # 8. 灰度期间逐次曝光归属
    # ------------------------------------------------------------------ #
    @staticmethod
    def _bucket(merchant_id: str, entry_id: str) -> int:
        digest = hashlib.sha256(f"{merchant_id}|{entry_id}".encode()).hexdigest()
        return int(digest[:8], 16) % 100

    def resolve_exposure(self, actor: str, merchant_id: str, entry_id: str,
                         cohort: str = "") -> dict[str, Any]:
        """一次曝光必须能对应到一份生效规则。返回归属记录并落账。"""
        with self._lock:
            if merchant_id not in self.merchants:
                raise RuleError("商家不存在")
            merchant = self.merchants[merchant_id]
            decision_path: list[str] = []
            chosen: str | None = None
            gray_hit = False

            if self.gray:
                gpkg = self.package(self.gray.package_hash)
                in_scope = gpkg.scope.matches(merchant)
                in_entry = entry_id in gpkg.entry_ids()
                in_cohort = not self.gray.cohorts or cohort in self.gray.cohorts
                bucket = self._bucket(merchant_id, entry_id)
                in_bucket = bucket < self.gray.percent
                decision_path.append(
                    f"灰度候选 {gpkg.package_id}@r{gpkg.revision}: "
                    f"适用商家={in_scope} 入口={in_entry} "
                    f"队列={in_cohort} 分桶={bucket}<{self.gray.percent}={in_bucket}")
                if in_scope and in_entry and in_cohort and in_bucket:
                    chosen = self.gray.package_hash
                    gray_hit = True

            if chosen is None:
                prod = self.production_hash
                if prod:
                    ppkg = self.package(prod)
                    if ppkg.scope.matches(merchant) and \
                            entry_id in ppkg.entry_ids():
                        chosen = prod
                        decision_path.append(
                            f"命中正式版本 {ppkg.package_id}@r{ppkg.revision}")
                    else:
                        decision_path.append(
                            "正式版本不覆盖该商家/入口，回落基线")
                if chosen is None and self.baseline_hash:
                    chosen = self.baseline_hash
                    decision_path.append("使用基线规则")

            if chosen is None:
                raise RuleError("该曝光无任何生效规则可归属，禁止放行")

            pkg = self.package(chosen)
            exemptions = self.active_exemptions(chosen, merchant_id)
            frozen_appeals = self.pending_appeal_freeze(chosen, merchant_id)
            # 该商家本次实际生效的价格约束（剔除豁免项；申诉冻结期整体暂缓执行）
            exempt_ids = {cid for e in exemptions for cid in e.constraint_ids}
            enforced: list[str] = []
            suspended: list[str] = []
            for c in pkg.price_constraints:
                if "*" in exempt_ids or c.constraint_id in exempt_ids:
                    continue
                if frozen_appeals:
                    suspended.append(c.constraint_id)
                else:
                    enforced.append(c.constraint_id)

            exposure_id = "EXPOSURE-" + hashlib.sha256(
                f"{merchant_id}|{entry_id}|{len(self.exposures)}|{self.clock()}"
                .encode()).hexdigest()[:12]
            record = {
                "exposure_id": exposure_id,
                "ts": self.clock(),
                "merchant_id": merchant_id,
                "entry_id": entry_id,
                "cohort": cohort,
                "layer": "GRAY" if gray_hit else "PRODUCTION",
                "package_id": pkg.package_id,
                "revision": pkg.revision,
                "content_hash": chosen,
                "algorithm_version": pkg.algorithm.version,
                "ranking_weights": dict(pkg.algorithm.ranking_weights),
                "enforced_constraints": enforced,
                "suspended_constraints": suspended,
                "exemption_ids": [e.exemption_id for e in exemptions],
                "frozen_appeal_ids": frozen_appeals,
                "decision_path": decision_path,
            }
            self.exposures.append(record)
            # 账本中只保存归属与权重指纹所需信息；商家机密字段不在此重复
            self.ledger.append("EXPOSURE_RESOLVED", {
                "exposure_id": exposure_id,
                "ts": record["ts"],
                "merchant_id": merchant_id,
                "entry_id": entry_id,
                "cohort": cohort,
                "layer": record["layer"],
                "content_hash": chosen,
                "algorithm_version": pkg.algorithm.version,
                "enforced_constraints": enforced,
                "suspended_constraints": suspended,
                "exemption_ids": record["exemption_ids"],
                "frozen_appeal_ids": frozen_appeals,
                "decision_path": decision_path,
            }, actor)
            return record

    # ------------------------------------------------------------------ #
    # 持久化：状态是事件账本的投影，整体落盘可重建
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        from dataclasses import asdict
        return {
            "packages": self.packages,
            "index": self.index,
            "merchants": self.merchants,
            "baseline_hash": self.baseline_hash,
            "approvals": self.approvals,
            "status": self.status,
            "gray": asdict(self.gray) if self.gray else None,
            "production_hash": self.production_hash,
            "notifications": self.notifications,
            "exemptions": [asdict(e) for e in self.exemptions],
            "appeals": {k: asdict(v) for k, v in self.appeals.items()},
            "exposures": self.exposures,
            "events": self.ledger.dump(),
        }

    @classmethod
    def restore(cls, data: dict[str, Any], clock=time.time) -> "RuleStore":
        store = cls(clock=clock)
        store.packages = data["packages"]
        store.index = data["index"]
        store.merchants = data["merchants"]
        store.baseline_hash = data.get("baseline_hash")
        store.approvals = data.get("approvals", {})
        store.status = data.get("status", {})
        g = data.get("gray")
        store.gray = Rollout(g["package_hash"], g["percent"],
                             tuple(g["cohorts"]), g["started_ts"]) if g else None
        store.production_hash = data.get("production_hash")
        store.notifications = data.get("notifications", {})
        store.exemptions = [Exemption(**e) for e in data.get("exemptions", [])]
        store.appeals = {k: Appeal(**v)
                         for k, v in data.get("appeals", {}).items()}
        store.exposures = data.get("exposures", [])
        store.ledger = Ledger.load(data["events"], clock=clock)
        return store

    # ------------------------------------------------------------------ #
    # 重放重建
    # ------------------------------------------------------------------ #
    def history(self, package_hash: str) -> list:
        """某发布包的完整事件轨迹（含止损/回滚/豁免/申诉），用于反查。
        返回账本事件对象；API 层负责 JSON 化。"""
        pid = self.packages[package_hash]["package_id"]
        out = []
        for e in self.ledger.events():
            hit = e.payload.get("content_hash") == package_hash
            if not hit and e.kind == "ROLLBACK" and \
                    (e.payload.get("to_hash") == package_hash or
                     e.payload.get("from_hash") == package_hash):
                hit = True
            if hit:
                out.append(e)
        return out
