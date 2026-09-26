"""核心领域流程测试：冻结、会签闸门、灰度归属、应急留痕、组合预警、
最小披露、反查、并发唯一正式版本。"""

import unittest

from src import alerts as alerts_mod
from src import impact
from src.api import APPROVER, AUDITOR, MERCHANT, OPERATOR, PermissionError_, Principal, RuleApi
from src.demo import (
    ENTRY_SEARCH, Clock, baseline_package, build_demo_store,
    candidate_package,
)
from src.ledger import Ledger
from src.models import RulePackage
from src.store import (
    APPROVED, DRAFT, GRAY, KILL_SWITCHED, PRODUCTION, REQUIRED_TEAMS,
    SUPERSEDED, RuleError, RuleStore,
)

OPS = Principal(OPERATOR, "ops-li")
AUD = Principal(AUDITOR, "auditor-chen")


def approve_all(store: RuleStore, h: str) -> None:
    for team in REQUIRED_TEAMS:
        store.approve(f"approver-{team}", team, h)


def notify_all(store: RuleStore, h: str, clock: Clock) -> None:
    past = clock.t - 9 * 86400
    for mid in store.affected_merchants(h):
        store.record_notification("ops-li", h, mid, ts=past)


class PackageFreezeTest(unittest.TestCase):
    def test_hash_changes_on_any_dimension(self) -> None:
        p1 = candidate_package()
        h1 = p1.content_hash
        d = p1.to_dict()
        d["subsidies"][0]["subsidy_rate"] = 0.20
        p2 = RulePackage.from_dict(d)
        self.assertNotEqual(h1, p2.content_hash)

    def test_frozen_object_is_immutable(self) -> None:
        p = candidate_package()
        with self.assertRaises(Exception):
            p.notes = "tamper"  # type: ignore[misc]

    def test_round_trip_preserves_hash(self) -> None:
        p = candidate_package()
        self.assertEqual(p.content_hash,
                         RulePackage.from_dict(p.to_dict()).content_hash)

    def test_duplicate_and_revision(self) -> None:
        store, _, _, ch = build_demo_store()
        with self.assertRaises(RuleError):
            store.create_package("ops", candidate_package())
        # 修订号必须递增
        d = candidate_package().to_dict()
        d["revision"] = 9
        with self.assertRaises(RuleError):
            store.create_package("ops", RulePackage.from_dict(d))


class ApprovalGateTest(unittest.TestCase):
    def test_cannot_activate_without_all_teams(self) -> None:
        store, _, _, ch = build_demo_store()
        store.approve("approver-合规", "合规", ch)
        with self.assertRaises(RuleError):
            store.activate("ops", ch)
        self.assertEqual(store.status[ch], DRAFT)

    def test_unknown_team_rejected(self) -> None:
        store, _, _, ch = build_demo_store()
        with self.assertRaises(RuleError):
            store.approve("x", "市场部", ch)

    def test_unfinished_notifications_block_launch(self) -> None:
        store, clock, _, ch = build_demo_store()
        approve_all(store, ch)
        report = store.preflight(ch)
        self.assertFalse(report["ready"])
        self.assertEqual(report["affected_merchant_count"], 6)
        self.assertEqual(len(report["outstanding_notifications"]), 6)
        with self.assertRaises(RuleError):
            store.activate("ops", ch)
        # 刚通知、提前期不足仍拦截
        first = report["affected_merchants"][0]
        store.record_notification("ops", ch, first)
        rep2 = store.preflight(ch)
        reasons = {n["merchant_id"]: n["reason"]
                   for n in rep2["outstanding_notifications"]}
        self.assertEqual(reasons[first], "LEAD_TIME_NOT_MET")

    def test_preflight_lists_affected_groups(self) -> None:
        store, _, _, ch = build_demo_store()
        report = store.preflight(ch)
        self.assertEqual(report["affected_segments"], ["A", "B"])
        self.assertEqual(set(report["missing_approval_teams"]),
                         set(REQUIRED_TEAMS))

    def test_gate_passes_after_approvals_and_timely_notice(self) -> None:
        store, clock, _, ch = build_demo_store()
        approve_all(store, ch)
        notify_all(store, ch, clock)
        self.assertTrue(store.preflight(ch)["ready"])


class GrayAndExposureTest(unittest.TestCase):
    def _ready(self, store, clock, ch):
        approve_all(store, ch)
        notify_all(store, ch, clock)

    def test_gray_exposure_attribution_is_stable_and_replayable(self) -> None:
        store, clock, bh, ch = build_demo_store()
        self._ready(store, clock, ch)
        store.start_gray("ops", ch, 50, cohorts=())
        # 同一商家+入口的分桶确定：多次曝光归属一致
        r1 = store.resolve_exposure("ops", "m-a2", ENTRY_SEARCH)
        r2 = store.resolve_exposure("ops", "m-a2", ENTRY_SEARCH)
        self.assertEqual(r1["content_hash"], r2["content_hash"])
        self.assertEqual(r1["algorithm_version"], "algo-v2"
                         if r1["layer"] == "GRAY" else "algo-v1")
        # 灰度命中或正式命中必有其一，且记录含规则哈希与版本
        self.assertIn(r1["layer"], ("GRAY", "PRODUCTION"))
        self.assertTrue(r1["content_hash"])
        self.assertTrue(r1["decision_path"])

    def test_gray_100_covers_all_scope(self) -> None:
        store, clock, bh, ch = build_demo_store()
        self._ready(store, clock, ch)
        store.start_gray("ops", ch, 100)
        layers = {m: store.resolve_exposure("ops", m, ENTRY_SEARCH)["layer"]
                  for m in ("m-a1", "m-a2", "m-b1")}
        self.assertTrue(all(v == "GRAY" for v in layers.values()))

    def test_only_one_gray_and_one_production(self) -> None:
        store, clock, bh, ch = build_demo_store()
        self._ready(store, clock, ch)
        store.start_gray("ops", ch, 10)
        # 另起一个候选包，不允许并发第二条灰度
        d = candidate_package().to_dict()
        d["revision"] = 3
        d["notes"] = "并行候选"
        h3 = store.create_package("ops", RulePackage.from_dict(d))
        approve_all(store, h3)
        notify_all(store, h3, clock)
        with self.assertRaises(RuleError):
            store.start_gray("ops", h3, 10)
        # 全量上线后旧正式版本被取代，全局仅一个 PRODUCTION
        store.activate("ops", ch)
        prod = [h for h, s in store.status.items() if s == PRODUCTION]
        self.assertEqual(prod, [ch])
        self.assertEqual(store.status[bh], SUPERSEDED)

    def test_exposure_outside_scope_falls_back(self) -> None:
        store, clock, _, ch = build_demo_store()
        # 未灰度时全部归属正式基线包
        rec = store.resolve_exposure("ops", "m-b1", ENTRY_SEARCH)
        self.assertEqual(rec["layer"], "PRODUCTION")
        self.assertEqual(rec["algorithm_version"], "algo-v1")


class EmergencyAndRollbackTest(unittest.TestCase):
    def test_emergency_stop_and_rollback_keep_history(self) -> None:
        store, clock, bh, ch = build_demo_store()
        approve_all(store, ch)
        notify_all(store, ch, clock)
        store.activate("ops", ch)
        store.emergency_stop("ops-oncall", ch, "投诉量异常飙升")
        self.assertEqual(store.status[ch], KILL_SWITCHED)
        self.assertEqual(store.production_hash, bh)  # 回落基线
        # 历史仍可查到该包曾上线、被止损
        kinds = [e.kind for e in store.history(ch)]
        self.assertIn("PRODUCTION_ACTIVATED", kinds)
        self.assertIn("EMERGENCY_STOP", kinds)

        # 回滚到基线包：追加 ROLLBACK，不抹任何事件
        store.rollback("ops-oncall", bh, "止损后恢复基线")
        self.assertEqual(store.production_hash, bh)
        self.assertEqual(store.status[bh], PRODUCTION)
        rollback_events = store.ledger.events("ROLLBACK")
        self.assertEqual(len(rollback_events), 1)
        self.assertEqual(rollback_events[0].payload["to_hash"], bh)
        # 被止损的包状态未被回滚改写
        self.assertEqual(store.status[ch], KILL_SWITCHED)

    def test_cannot_rollback_to_gray(self) -> None:
        store, clock, bh, ch = build_demo_store()
        approve_all(store, ch)
        notify_all(store, ch, clock)
        store.start_gray("ops", ch, 10)
        with self.assertRaises(RuleError):
            store.rollback("ops", ch, "x")


class ExemptionAppealTest(unittest.TestCase):
    def test_exemption_removes_constraint_and_revoke_is_logged(self) -> None:
        store, clock, bh, ch = build_demo_store()
        approve_all(store, ch)
        notify_all(store, ch, clock)
        store.activate("ops", ch)
        eid = store.grant_exemption("ops", ch, "m-a2", "独家协议价格保护")
        rec = store.resolve_exposure("ops", "m-a2", ENTRY_SEARCH)
        self.assertEqual(rec["enforced_constraints"], [])
        self.assertIn(eid, rec["exemption_ids"])
        store.revoke_exemption("ops", eid, "保护期结束")
        rec2 = store.resolve_exposure("ops", "m-a2", ENTRY_SEARCH)
        self.assertEqual(rec2["enforced_constraints"], ["PC-FOLLOW"])
        # 授予与撤销都在账本中
        self.assertTrue(store.ledger.events("EXEMPTION_GRANTED"))
        self.assertTrue(store.ledger.events("EXEMPTION_REVOKED"))

    def test_pending_appeal_freezes_enforcement(self) -> None:
        store, clock, bh, ch = build_demo_store()
        approve_all(store, ch)
        notify_all(store, ch, clock)
        store.activate("ops", ch)
        rec = store.resolve_exposure("ops", "m-a3", ENTRY_SEARCH)
        aid = store.file_appeal("m-a3", "m-a3", ch,
                                rec["exposure_id"], "不认可降权")
        rec2 = store.resolve_exposure("ops", "m-a3", ENTRY_SEARCH)
        self.assertEqual(rec2["suspended_constraints"], ["PC-FOLLOW"])
        self.assertIn(aid, rec2["frozen_appeal_ids"])
        # 裁决后恢复执行
        store.resolve_appeal("arbiter", aid, False, "申诉不成立")
        rec3 = store.resolve_exposure("ops", "m-a3", ENTRY_SEARCH)
        self.assertEqual(rec3["enforced_constraints"], ["PC-FOLLOW"])
        self.assertEqual(rec3["suspended_constraints"], [])


class ImpactAndAlertTest(unittest.TestCase):
    def test_deterministic_recompute_ranks(self) -> None:
        store, _, bh, ch = build_demo_store()
        r1 = impact.compare(store, ch, bh)
        r2 = impact.compare(store, ch, bh)
        self.assertEqual(r1["rows"], r2["rows"])
        a3 = next(r for r in r1["rows"] if r["merchant_id"] == "m-a3")
        # 基线按 q：a3 排第2；新规则按 p：a3 排第3，下滑1名
        self.assertEqual((a3["old_rank"], a3["new_rank"]), (2, 3))
        self.assertTrue(a3["demoted"])
        # a3 售价13 > 最低价10，被要求跟价
        self.assertEqual(a3["must_follow_price"], 10)
        self.assertGreater(a3["follow_gap"], 0)

    def test_combination_alerts_fire_with_explanation(self) -> None:
        store, _, bh, ch = build_demo_store()
        found = alerts_mod.evaluate(store, ch, bh)
        codes = {a["code"] for a in found}
        self.assertEqual(
            codes,
            {"CONCENTRATED_DEMOTION", "FORCED_PRICE_FOLLOWING",
             "EXIT_DIFFICULTY"})
        for a in found:
            self.assertTrue(a["explanation"])
            self.assertTrue(a["rule_combination"])
            self.assertTrue(a["affected_merchants"])
        # 预警引用的规则组件确实来自候选包
        combo = next(a for a in found
                     if a["code"] == "FORCED_PRICE_FOLLOWING")
        self.assertEqual(combo["rule_combination"]["follow_lowest_constraints"],
                         ["PC-FOLLOW"])
        self.assertEqual(combo["rule_combination"]["withdrawn_subsidies"],
                         ["SUB-LOWPRICE"])

    def test_alert_recording_is_append_only(self) -> None:
        store, _, bh, ch = build_demo_store()
        alerts_mod.evaluate(store, ch, bh, record_actor="monitor")
        self.assertTrue(store.ledger.events("ALERT_RAISED"))

    def test_threshold_change_can_silence(self) -> None:
        store, _, bh, ch = build_demo_store()
        found = alerts_mod.evaluate(
            store, ch, bh,
            thresholds={"demotion": 0.99, "follow": 0.99, "exit": 0.99})
        self.assertEqual(found, [])


class LedgerIntegrityTest(unittest.TestCase):
    def test_chain_verifies_and_detects_tamper(self) -> None:
        store, _, _, _ = build_demo_store()
        ok, msg = store.ledger.verify()
        self.assertTrue(ok, msg)
        # 直接篡改一条历史载荷 -> 哈希链断裂
        ev = store.ledger.events("PACKAGE_APPROVED")[0]
        ev.payload["team"] = "冒充合规"
        ok, msg = store.ledger.verify()
        self.assertFalse(ok)

    def test_snapshot_restore_replays_state(self) -> None:
        store, clock, bh, ch = build_demo_store()
        approve_all(store, ch)
        notify_all(store, ch, clock)
        store.activate("ops", ch)
        store.emergency_stop("ops", ch, "演练止损")
        data = store.snapshot()
        restored = RuleStore.restore(data)
        self.assertEqual(restored.production_hash, store.production_hash)
        self.assertTrue(restored.ledger.verify()[0])
        self.assertEqual(len(restored.ledger.events()),
                         len(store.ledger.events()))


class DisclosureTest(unittest.TestCase):
    def test_auditor_can_recompute_but_not_mutate(self) -> None:
        store, _, bh, ch = build_demo_store()
        api = RuleApi(store)
        rep = api.impact(AUD, ch, bh)
        # 审查人可见模型权重用于复算
        self.assertNotEqual(rep["weight_change"], "[REDACTED:platform_model]")
        # 但任何修改生产规则的动作一律 403
        with self.assertRaises(PermissionError_):
            api.create_package(AUD, candidate_package().to_dict())
        with self.assertRaises(PermissionError_):
            api.activate(AUD, ch)
        with self.assertRaises(PermissionError_):
            api.emergency_stop(AUD, ch, "x")
        with self.assertRaises(PermissionError_):
            api.rollback(AUD, bh, "x")
        with self.assertRaises(PermissionError_):
            api.grant_exemption(AUD, ch, "m-a1", "x")
        with self.assertRaises(PermissionError_):
            api.approve(AUD, ch)

    def test_auditor_sees_pseudonyms_not_merchant_secrets(self) -> None:
        store, _, _, _ = build_demo_store()
        api = RuleApi(store)
        ch = _candidate_hash(api)
        view = api.preflight(AUD, ch)
        self.assertTrue(all(m.startswith("M-")
                            for m in view["affected_merchants"]))
        # 模型内部参数在发布包视图中对审查人可见，但商家名单假名化
        pkg = api.get_package(AUD, ch)
        self.assertNotIn("m-a1", str(pkg["scope"]["merchant_ids"]))

    def test_merchant_sees_only_self_and_no_model_internals(self) -> None:
        store, _, _, _ = build_demo_store()
        api = RuleApi(store)
        ch = _candidate_hash(api)
        mp = Principal(MERCHANT, "m-a2", merchant_id="m-a2")
        pkg = api.get_package(mp, ch)
        self.assertEqual(pkg["algorithm"]["ranking_weights"],
                         "[REDACTED:platform_model]")
        self.assertEqual(pkg["scope"]["merchant_ids"],
                         {"includes_self": False})
        with self.assertRaises(PermissionError_):
            api.trace_complaint(mp, "m-a3")

    def test_public_cannot_read_internals(self) -> None:
        api = RuleApi(build_demo_store()[0])
        anon = Principal("PUBLIC", "anon")
        with self.assertRaises(PermissionError_):
            api.list_packages(anon)


def _candidate_hash(api: RuleApi) -> str:
    return [p["content_hash"] for p in api.list_packages(OPS)
            if p["revision"] == 2][0]


class TraceTest(unittest.TestCase):
    def test_trace_from_complaint_finds_rule_and_basis(self) -> None:
        from src import trace as trace_mod
        store, clock, bh, ch = build_demo_store()
        approve_all(store, ch)
        notify_all(store, ch, clock)
        store.activate("ops", ch)
        rec = store.resolve_exposure("ops", "m-a3", ENTRY_SEARCH)
        result = trace_mod.trace_complaint(store, "m-a3")
        self.assertTrue(result["found"])
        chain = next(c for c in result["rule_chains"]
                     if c["content_hash"] == rec["content_hash"])
        self.assertEqual(len(chain["approval_basis"]), len(REQUIRED_TEAMS))
        self.assertIn("algo-v2", chain["algorithm_version"])
        self.assertIn("PC-FOLLOW", chain["rule_components"]["price_constraints"])
        # 批准依据可定位到具体审批人
        teams = {a["team"] for a in chain["approval_basis"]}
        self.assertEqual(teams, set(REQUIRED_TEAMS))

    def test_trace_from_metric_finds_alerts_and_timeline(self) -> None:
        from src import trace as trace_mod
        store, clock, bh, ch = build_demo_store()
        approve_all(store, ch)
        notify_all(store, ch, clock)
        alerts_mod.evaluate(store, ch, bh, record_actor="monitor")
        store.activate("ops", ch)
        store.emergency_stop("ops", ch, "监控触发止损")
        result = trace_mod.trace_metric(store, "A")
        self.assertTrue(result["found"])
        codes = {e["payload"]["code"] for e in result["alerts"]}
        self.assertIn("FORCED_PRICE_FOLLOWING", codes)
        kinds = {t["event"] for t in result["timeline"]}
        self.assertIn("EMERGENCY_STOP", kinds)

    def test_trace_complaint_empty_window(self) -> None:
        from src import trace as trace_mod
        store, _, _, _ = build_demo_store()
        result = trace_mod.trace_complaint(
            store, "m-a3", start_ts=10_000_000_000)
        self.assertFalse(result["found"])


class ConcurrencyTest(unittest.TestCase):
    def test_parallel_approvals_yield_single_production(self) -> None:
        import threading
        store, clock, bh, ch = build_demo_store()
        notify_all(store, ch, clock)

        errors: list[Exception] = []

        def approve(team: str) -> None:
            try:
                store.approve(f"approver-{team}", team, ch)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=approve, args=(t,))
                   for t in REQUIRED_TEAMS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.assertEqual(store.status[ch], APPROVED)
        store.activate("ops", ch)
        self.assertEqual(len([h for h, s in store.status.items()
                              if s == PRODUCTION]), 1)


if __name__ == "__main__":
    unittest.main()
