"""平台规则公平变更后台的核心服务。

设计原则：
- 发布包冻结后不可变，所有变更以事件追加，历史可完整重放；
- 灰度期间每次曝光都记录生效的发布包与内容哈希；
- 审查角色只能读取与复算，不能修改生产规则；
- 多团队会签可以并行进行，但正式版本全平台唯一。
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timedelta

from .model import (
    DEFAULT_REQUIRED_TEAMS,
    PACKAGE_CONTENT_FIELDS,
    RulePackage,
    canonical_hash,
)
from .store import EventStore, parse_ts, utc_now

WRITE_ROLES = frozenset({"operator"})            # 只有运营角色可以写入生产规则
APPROVE_ROLES = frozenset({"operator", "approver"})
METRIC_ROLES = frozenset({"operator", "analyst"})

ACTIVE_STATUSES = frozenset({"published", "official"})


class ConflictError(Exception):
    """违反唯一正式版本等约束。"""


class StateError(Exception):
    """当前状态不允许该操作。"""


class FairChangeService:
    """规则发布、灰度归属、止损回滚与追溯的领域服务。"""

    def __init__(self, store: EventStore | None = None,
                 required_teams=DEFAULT_REQUIRED_TEAMS) -> None:
        self.store = store or EventStore()
        self.required_teams = tuple(required_teams)
        self._packages: dict[str, RulePackage] = {}
        self._exposures: list[dict] = []
        self._complaints: list[dict] = []
        self._metrics: list[dict] = []

    # ---- 权限 ----
    @staticmethod
    def _require_write(actor: dict) -> None:
        if actor.get("role") not in WRITE_ROLES:
            raise PermissionError("审查人员只能复算影响，不能修改生产规则")

    @staticmethod
    def _require_approver(actor: dict) -> None:
        if actor.get("role") not in APPROVE_ROLES:
            raise PermissionError("只有审批团队代表可以提交审批意见")

    # ---- 发布包生命周期 ----
    def create_draft(self, actor: dict, package_id: str, title: str,
                     content: dict, now: str | None = None) -> dict:
        self._require_write(actor)
        if package_id in self._packages:
            raise ConflictError(f"发布包已存在: {package_id}")
        missing = [f for f in PACKAGE_CONTENT_FIELDS if f not in content]
        if missing:
            raise StateError(f"发布包缺少必备字段: {missing}")
        pkg = RulePackage(package_id, title, copy.deepcopy(content),
                          actor["id"], now or utc_now())
        self._packages[package_id] = pkg
        self.store.append("package_drafted", actor["id"],
                          {"package_id": package_id, "title": title}, ts=now)
        return self.package_view(package_id)

    def update_draft(self, actor: dict, package_id: str, content: dict,
                     now: str | None = None) -> dict:
        self._require_write(actor)
        pkg = self._get(package_id)
        if pkg.frozen():
            raise StateError("发布包已冻结，内容不可再修改；如需调整请新建版本")
        missing = [f for f in PACKAGE_CONTENT_FIELDS if f not in content]
        if missing:
            raise StateError(f"发布包缺少必备字段: {missing}")
        pkg.content = copy.deepcopy(content)
        self.store.append("package_updated", actor["id"],
                          {"package_id": package_id}, ts=now)
        return self.package_view(package_id)

    def freeze(self, actor: dict, package_id: str, now: str | None = None) -> dict:
        self._require_write(actor)
        pkg = self._get(package_id)
        if pkg.frozen():
            raise StateError("发布包已冻结")
        pkg.content_hash = canonical_hash(pkg.content)
        self.store.append("package_frozen", actor["id"], {
            "package_id": package_id,
            "content_hash": pkg.content_hash,
            "content": copy.deepcopy(pkg.content),
        }, ts=now)
        return self.package_view(package_id)

    # ---- 多团队会签 ----
    def submit_approval(self, actor: dict, package_id: str, team: str,
                        decision: str, basis: str, now: str | None = None) -> dict:
        self._require_approver(actor)
        pkg = self._get(package_id)
        if not pkg.frozen():
            raise StateError("发布包未冻结，不能进入审批")
        if team not in self.required_teams:
            raise StateError(f"未知审批团队: {team}")
        if decision not in ("approve", "reject"):
            raise StateError("审批结论只能是 approve 或 reject")
        if not basis:
            raise StateError("审批必须给出依据，便于事后反查")
        event = self.store.append("approval_submitted", actor["id"], {
            "package_id": package_id, "team": team,
            "decision": decision, "basis": basis,
        }, ts=now)
        return event

    def approval_status(self, package_id: str) -> dict:
        """各团队最新一次审批结论；多团队可以并行提交。"""
        latest: dict[str, dict] = {}
        for e in self.store.for_package(package_id):
            if e["type"] == "approval_submitted":
                latest[e["payload"]["team"]] = e["payload"] | {"actor": e["actor"], "ts": e["ts"]}
        approved = all(
            latest.get(team, {}).get("decision") == "approve"
            for team in self.required_teams
        )
        return {"teams": latest, "approved": approved}

    # ---- 通知与上线前检查 ----
    def record_notification(self, actor: dict, package_id: str, group: str,
                            channel: str, now: str | None = None) -> dict:
        self._require_write(actor)
        self._get(package_id)
        return self.store.append("notification_sent", actor["id"], {
            "package_id": package_id, "group": group, "channel": channel,
        }, ts=now)

    def preflight(self, package_id: str) -> dict:
        """上线前检查：列出受影响群体与未完成通知。"""
        view = self.package_view(package_id)
        scope = view["content"].get("merchant_scope", {})
        plan = view["content"].get("notification_plan", [])
        sent = {
            (e["payload"]["group"], e["payload"].get("channel"))
            for e in self.store.for_package(package_id)
            if e["type"] == "notification_sent"
        }
        pending = [item for item in plan
                   if (item.get("group"), item.get("channel")) not in sent]
        return {
            "package_id": package_id,
            "affected_groups": list(scope.get("groups", [])),
            "affected_merchants_estimate": scope.get("estimated_merchants"),
            "pending_notifications": pending,
            "ready": not pending,
        }

    # ---- 发布与唯一正式版本 ----
    def publish(self, actor: dict, package_id: str, now: str | None = None,
                emergency: bool = False, reason: str | None = None) -> dict:
        self._require_write(actor)
        pkg = self._get(package_id)
        if self.status_of(package_id) != "frozen":
            raise StateError("只有已冻结且未发布的发布包可以发布")
        if not self.approval_status(package_id)["approved"]:
            raise StateError("多团队会签未全部通过，不能发布")
        check = self.preflight(package_id)
        if check["pending_notifications"] and not emergency:
            raise StateError(
                f"存在未完成通知: {check['pending_notifications']}；"
                "紧急发布需显式 emergency=True 并给出理由"
            )
        if emergency and not reason:
            raise StateError("紧急发布必须记录理由")
        return self.store.append("package_published", actor["id"], {
            "package_id": package_id,
            "content_hash": pkg.content_hash,
            "rollout": copy.deepcopy(pkg.content.get("rollout", {})),
            "emergency": emergency,
            "reason": reason,
            "skipped_notifications": check["pending_notifications"],
        }, ts=now)

    def promote(self, actor: dict, package_id: str, now: str | None = None,
                supersedes: str | None = None) -> dict:
        """灰度转正式。全平台只允许一个正式版本，取代必须显式声明。"""
        self._require_write(actor)
        self._get(package_id)
        if self.status_of(package_id) != "published":
            raise StateError("只有灰度中的发布包可以转为正式版本")
        current = self.current_official()
        if current and current != package_id:
            if supersedes != current:
                raise ConflictError(
                    f"全平台只允许一个正式版本（当前为 {current}），"
                    "如需取代请显式传入 supersedes"
                )
            self.store.append("package_superseded", actor["id"], {
                "package_id": current, "superseded_by": package_id,
            }, ts=now)
        return self.store.append("package_promoted", actor["id"], {
            "package_id": package_id,
            "content_hash": self._get(package_id).content_hash,
            "supersedes": supersedes,
        }, ts=now)

    # ---- 止损、回滚、豁免（只追加事件，不抹历史） ----
    def rollback(self, actor: dict, package_id: str, reason: str,
                 to_package_id: str | None = None, now: str | None = None) -> dict:
        self._require_write(actor)
        self._get(package_id)
        if self.status_of(package_id) not in ACTIVE_STATUSES:
            raise StateError("只有在线上的发布包可以回滚")
        if not reason:
            raise StateError("回滚必须记录理由")
        if to_package_id is not None:
            self._get(to_package_id)
            if self.status_of(to_package_id) not in ("superseded", "rolled_back", "published"):
                raise StateError(f"回滚目标 {to_package_id} 不在可恢复状态")
        event = self.store.append("package_rolled_back", actor["id"], {
            "package_id": package_id, "reason": reason,
            "to_package_id": to_package_id,
        }, ts=now)
        if to_package_id is not None:
            self.store.append("package_promoted", actor["id"], {
                "package_id": to_package_id,
                "content_hash": self._get(to_package_id).content_hash,
                "via": "rollback",
                "rollback_of": package_id,
            }, ts=now)
        return event

    def emergency_stop(self, actor: dict, package_id: str, reason: str,
                       now: str | None = None) -> dict:
        self._require_write(actor)
        self._get(package_id)
        if self.status_of(package_id) == "stopped":
            raise StateError("发布包已处于止损状态")
        if not reason:
            raise StateError("紧急止损必须记录理由")
        return self.store.append("emergency_stopped", actor["id"], {
            "package_id": package_id, "reason": reason,
        }, ts=now)

    def grant_exemption(self, actor: dict, package_id: str, reason: str,
                        expires_at: str, merchant_ids=(), groups=(),
                        now: str | None = None) -> dict:
        self._require_write(actor)
        self._get(package_id)
        if not merchant_ids and not groups:
            raise StateError("豁免必须指明商家或群体")
        if not reason:
            raise StateError("豁免必须记录理由")
        return self.store.append("exemption_granted", actor["id"], {
            "package_id": package_id,
            "merchant_ids": list(merchant_ids),
            "groups": list(groups),
            "reason": reason,
            "expires_at": expires_at,
        }, ts=now)

    # ---- 灰度曝光归属 ----
    def record_exposure(self, actor: dict, merchant_id: str, entry: str,
                        merchant_groups=(), now: str | None = None) -> dict:
        """每次曝光解析并记录当时生效的发布包与内容哈希。"""
        now = now or utc_now()
        chosen = self._resolve_package(merchant_id, entry, merchant_groups, now)
        exposure = {
            "exposure_id": f"EXP-{len(self._exposures) + 1:06d}",
            "merchant_id": merchant_id,
            "entry": entry,
            "ts": now,
            "package_id": chosen.package_id if chosen else None,
            "content_hash": chosen.content_hash if chosen else None,
            "algorithm_version": (
                chosen.content["algorithm_version"] if chosen else "platform-default"
            ),
        }
        self._exposures.append(exposure)
        return dict(exposure)

    def _resolve_package(self, merchant_id: str, entry: str,
                         merchant_groups, now: str) -> RulePackage | None:
        # 灰度包优先（最新发布者优先），其次正式版本，最后平台默认
        grays = [p for p in self._packages.values()
                 if self.status_of(p.package_id) == "published"]
        grays.sort(key=lambda p: self._publish_ts(p.package_id), reverse=True)
        for pkg in grays:
            if not self._applies(pkg, merchant_id, entry, merchant_groups, now):
                continue
            percent = max(
                (s.get("percent", 0) for s in pkg.content.get("rollout", {}).get("stages", [])),
                default=0,
            )
            if self.gray_bucket(pkg.package_id, merchant_id) < percent:
                return pkg
        official = self.current_official()
        if official:
            pkg = self._packages[official]
            if self._applies(pkg, merchant_id, entry, merchant_groups, now):
                return pkg
        return None

    def _applies(self, pkg: RulePackage, merchant_id: str, entry: str,
                 merchant_groups, now: str) -> bool:
        if entry not in pkg.content.get("traffic_entries", []):
            return False
        if not self.scope_matches(pkg.package_id, merchant_id, merchant_groups):
            return False
        return not self._exempted(pkg.package_id, merchant_id, merchant_groups, now)

    def scope_matches(self, package_id: str, merchant_id: str, merchant_groups=()) -> bool:
        scope = self._get(package_id).content.get("merchant_scope", {})
        if merchant_id in scope.get("exclude_merchant_ids", []):
            return False
        ids = scope.get("merchant_ids", [])
        groups = scope.get("groups", [])
        if not ids and not groups:
            return True  # 未限定范围即全平台适用
        return merchant_id in ids or bool(set(merchant_groups) & set(groups))

    @staticmethod
    def gray_bucket(package_id: str, merchant_id: str) -> int:
        """稳定的灰度分桶：同一发布包同一商家永远落在同一桶。"""
        digest = hashlib.sha256(f"{package_id}:{merchant_id}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % 100

    def _exempted(self, package_id: str, merchant_id: str, merchant_groups, now: str) -> bool:
        now_dt = parse_ts(now)
        for e in self.store.for_package(package_id):
            if e["type"] != "exemption_granted":
                continue
            payload = e["payload"]
            if parse_ts(payload["expires_at"]) <= now_dt:
                continue
            if merchant_id in payload.get("merchant_ids", []):
                return True
            if set(merchant_groups) & set(payload.get("groups", [])):
                return True
        return False

    # ---- 投诉、指标与反查 ----
    def record_complaint(self, actor: dict, merchant_id: str, entry: str,
                         ts: str, category: str, detail: str) -> dict:
        complaint = {
            "complaint_id": f"CMP-{len(self._complaints) + 1:06d}",
            "merchant_id": merchant_id,
            "entry": entry,
            "ts": ts,
            "category": category,
            "detail": detail,
            "recorded_by": actor.get("id", "anonymous"),
        }
        self._complaints.append(complaint)
        return dict(complaint)

    def record_metrics(self, actor: dict, metrics: list[dict]) -> list[dict]:
        if actor.get("role") not in METRIC_ROLES:
            raise PermissionError("只有运营或分析角色可以写入竞争指标")
        recorded = []
        for m in metrics:
            metric = dict(m)
            metric["metric_id"] = f"MTX-{len(self._metrics) + 1:06d}"
            self._metrics.append(metric)
            recorded.append(dict(metric))
        return recorded

    def attribute_metric(self, metric: dict) -> str | None:
        """把指标归属到当时生效的发布包：优先曝光记录，其次指标自带归属。"""
        if metric.get("package_id"):
            return metric["package_id"]
        ts = metric.get("ts")
        candidates = [
            e for e in self._exposures
            if e["merchant_id"] == metric.get("merchant_id")
            and e["package_id"]
            and (not ts or e["ts"] <= ts)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda e: e["ts"])["package_id"]

    def trace_complaint(self, complaint_id: str) -> dict:
        """从投诉反查：曝光 → 生效发布包 → 审批依据与发布事件。"""
        complaint = next((c for c in self._complaints
                          if c["complaint_id"] == complaint_id), None)
        if complaint is None:
            raise KeyError(complaint_id)
        window = timedelta(hours=24)
        cts = parse_ts(complaint["ts"])
        exposures = [
            e for e in self._exposures
            if e["merchant_id"] == complaint["merchant_id"]
            and cts - window <= parse_ts(e["ts"]) <= cts + window
        ]
        package_ids = sorted({e["package_id"] for e in exposures if e["package_id"]})
        return {
            "complaint": complaint,
            "exposures": exposures,
            "rules": [self._trace_package(pid) for pid in package_ids],
        }

    def trace_metric(self, metric_id: str) -> dict:
        """从竞争指标反查具体规则与批准依据。"""
        metric = next((m for m in self._metrics
                       if m["metric_id"] == metric_id), None)
        if metric is None:
            raise KeyError(metric_id)
        package_id = self.attribute_metric(metric)
        return {
            "metric": metric,
            "rule": self._trace_package(package_id) if package_id else None,
        }

    def _trace_package(self, package_id: str) -> dict:
        pkg = self._get(package_id)
        events = self.store.for_package(package_id)
        approvals = [e for e in events if e["type"] == "approval_submitted"]
        release = [e for e in events
                   if e["type"] in ("package_published", "package_promoted",
                                    "package_rolled_back", "emergency_stopped")]
        return {
            "package_id": package_id,
            "title": pkg.title,
            "content_hash": pkg.content_hash,
            "algorithm_version": pkg.content.get("algorithm_version"),
            "status": self.status_of(package_id),
            "approvals": [{
                "team": e["payload"]["team"],
                "decision": e["payload"]["decision"],
                "basis": e["payload"]["basis"],
                "actor": e["actor"],
                "ts": e["ts"],
            } for e in approvals],
            "release_events": release,
        }

    # ---- 状态与查询 ----
    def status_of(self, package_id: str) -> str:
        status = "draft"
        for e in self.store.for_package(package_id):
            t = e["type"]
            if t == "package_frozen":
                status = "frozen"
            elif t == "package_published":
                status = "published"
            elif t == "package_promoted":
                status = "official"
            elif t == "package_superseded":
                status = "superseded"
            elif t == "package_rolled_back":
                status = "rolled_back"
            elif t == "emergency_stopped":
                status = "stopped"
        return status

    def current_official(self) -> str | None:
        for package_id in self._packages:
            if self.status_of(package_id) == "official":
                return package_id
        return None

    def package_view(self, package_id: str) -> dict:
        pkg = self._get(package_id)
        return {
            "package_id": pkg.package_id,
            "title": pkg.title,
            "content": copy.deepcopy(pkg.content),
            "content_hash": pkg.content_hash,
            "status": self.status_of(package_id),
            "created_by": pkg.created_by,
            "created_at": pkg.created_at,
        }

    def packages(self) -> list[dict]:
        return [self.package_view(pid) for pid in self._packages]

    def exposures(self) -> list[dict]:
        return [dict(e) for e in self._exposures]

    def metrics(self) -> list[dict]:
        return [dict(m) for m in self._metrics]

    def _publish_ts(self, package_id: str) -> str:
        events = [e for e in self.store.for_package(package_id)
                  if e["type"] == "package_published"]
        return events[-1]["ts"] if events else ""

    def publish_time(self, package_id: str) -> datetime | None:
        ts = self._publish_ts(package_id)
        return parse_ts(ts) if ts else None

    def _get(self, package_id: str) -> RulePackage:
        pkg = self._packages.get(package_id)
        if pkg is None:
            raise KeyError(package_id)
        return pkg
