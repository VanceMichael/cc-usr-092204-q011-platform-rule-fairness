import copy
import json
import unittest
from pathlib import Path

from src.fairchange.analysis import AlertPolicy, analyze_competition
from src.fairchange.disclosure import disclose_package, reviewer_recompute
from src.fairchange.service import ConflictError, FairChangeService, StateError

OPERATOR = {"id": "op-1", "role": "operator"}
APPROVER = {"id": "ap-1", "role": "approver"}
REVIEWER = {"id": "rev-1", "role": "reviewer"}
AUDITOR = {"id": "aud-1", "role": "auditor"}

T0 = "2026-09-19T00:00:00+00:00"
FAR_FUTURE = "2027-01-01T00:00:00+00:00"

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "release_package.json"


def sample_content(percent: int = 100) -> dict:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    content = fixture["content"]
    content["rollout"] = {"stages": [{"name": "灰度", "percent": percent}]}
    return content


def make_service() -> FairChangeService:
    return FairChangeService()


def approve_all(service: FairChangeService, package_id: str) -> None:
    for team in service.required_teams:
        service.submit_approval(APPROVER, package_id, team, "approve",
                                f"{team}评审通过", now=T0)


def publish_ready(service: FairChangeService, package_id: str,
                  percent: int = 100) -> None:
    service.create_draft(OPERATOR, package_id, f"规则包{package_id}",
                         sample_content(percent), now=T0)
    service.freeze(OPERATOR, package_id, now=T0)
    approve_all(service, package_id)
    for item in service.preflight(package_id)["pending_notifications"]:
        service.record_notification(OPERATOR, package_id, item["group"],
                                    item["channel"], now=T0)
    service.publish(OPERATOR, package_id, now=T0)


class LifecycleTest(unittest.TestCase):
    def test_freeze_makes_content_immutable(self) -> None:
        svc = make_service()
        svc.create_draft(OPERATOR, "P1", "规则包P1", sample_content(), now=T0)
        updated = sample_content()
        updated["algorithm_version"] = "rank-v2.3.2"
        svc.update_draft(OPERATOR, "P1", updated, now=T0)
        view = svc.freeze(OPERATOR, "P1", now=T0)
        self.assertTrue(view["content_hash"])
        with self.assertRaises(StateError):
            svc.update_draft(OPERATOR, "P1", sample_content(), now=T0)
        with self.assertRaises(StateError):
            svc.freeze(OPERATOR, "P1", now=T0)

    def test_publish_requires_full_approval_and_notifications(self) -> None:
        svc = make_service()
        svc.create_draft(OPERATOR, "P1", "规则包P1", sample_content(), now=T0)
        svc.freeze(OPERATOR, "P1", now=T0)
        for team in svc.required_teams[:-1]:
            svc.submit_approval(APPROVER, "P1", team, "approve", "通过", now=T0)
        with self.assertRaises(StateError):
            svc.publish(OPERATOR, "P1", now=T0)
        svc.submit_approval(APPROVER, "P1", svc.required_teams[-1], "approve",
                            "通过", now=T0)
        with self.assertRaises(StateError) as ctx:
            svc.publish(OPERATOR, "P1", now=T0)
        self.assertIn("未完成通知", str(ctx.exception))
        for item in svc.preflight("P1")["pending_notifications"]:
            svc.record_notification(OPERATOR, "P1", item["group"], item["channel"], now=T0)
        svc.publish(OPERATOR, "P1", now=T0)
        self.assertEqual(svc.status_of("P1"), "published")

    def test_preflight_lists_affected_groups_and_pending_notifications(self) -> None:
        svc = make_service()
        svc.create_draft(OPERATOR, "P1", "规则包P1", sample_content(), now=T0)
        check = svc.preflight("P1")
        self.assertEqual(check["affected_groups"], ["餐饮商家", "生鲜商家"])
        self.assertEqual(check["affected_merchants_estimate"], 1280)
        self.assertEqual(len(check["pending_notifications"]), 2)
        self.assertFalse(check["ready"])
        svc.record_notification(OPERATOR, "P1", "餐饮商家", "站内信", now=T0)
        svc.record_notification(OPERATOR, "P1", "生鲜商家", "站内信", now=T0)
        self.assertTrue(svc.preflight("P1")["ready"])

    def test_emergency_publish_is_recorded_not_silent(self) -> None:
        svc = make_service()
        svc.create_draft(OPERATOR, "P1", "规则包P1", sample_content(), now=T0)
        svc.freeze(OPERATOR, "P1", now=T0)
        approve_all(svc, "P1")
        with self.assertRaises(StateError):
            svc.publish(OPERATOR, "P1", emergency=True, now=T0)  # 无理由不允许
        event = svc.publish(OPERATOR, "P1", emergency=True,
                            reason="竞品价格战，需立即上线", now=T0)
        self.assertTrue(event["payload"]["emergency"])
        self.assertEqual(len(event["payload"]["skipped_notifications"]), 2)

    def test_only_one_official_version(self) -> None:
        svc = make_service()
        publish_ready(svc, "P1")
        publish_ready(svc, "P2")
        svc.promote(OPERATOR, "P1", now=T0)
        self.assertEqual(svc.current_official(), "P1")
        with self.assertRaises(ConflictError):
            svc.promote(OPERATOR, "P2", now=T0)
        svc.promote(OPERATOR, "P2", supersedes="P1", now=T0)
        self.assertEqual(svc.current_official(), "P2")
        self.assertEqual(svc.status_of("P1"), "superseded")


class ExposureTest(unittest.TestCase):
    def test_gray_exposure_attributes_effective_package(self) -> None:
        svc = make_service()
        publish_ready(svc, "BASE")
        svc.promote(OPERATOR, "BASE", now=T0)
        publish_ready(svc, "GRAY", percent=100)
        ts = "2026-09-20T10:00:00+00:00"
        exposure = svc.record_exposure(OPERATOR, "M-101", "搜索", ["餐饮商家"], now=ts)
        self.assertEqual(exposure["package_id"], "GRAY")
        self.assertEqual(exposure["content_hash"],
                         svc.package_view("GRAY")["content_hash"])
        self.assertEqual(exposure["algorithm_version"], "rank-v2.3.1")

    def test_exemption_falls_back_without_erasing_rule(self) -> None:
        svc = make_service()
        publish_ready(svc, "BASE")
        svc.promote(OPERATOR, "BASE", now=T0)
        publish_ready(svc, "GRAY", percent=100)
        svc.grant_exemption(OPERATOR, "GRAY", "商家申诉成立，暂缓适用",
                            FAR_FUTURE, merchant_ids=["M-101"], now=T0)
        ts = "2026-09-20T10:00:00+00:00"
        exposure = svc.record_exposure(OPERATOR, "M-101", "搜索", ["餐饮商家"], now=ts)
        self.assertEqual(exposure["package_id"], "BASE")
        other = svc.record_exposure(OPERATOR, "M-102", "搜索", ["餐饮商家"], now=ts)
        self.assertEqual(other["package_id"], "GRAY")

    def test_merchant_outside_gray_bucket_uses_official(self) -> None:
        svc = make_service()
        publish_ready(svc, "BASE")
        svc.promote(OPERATOR, "BASE", now=T0)
        publish_ready(svc, "GRAY", percent=0)
        ts = "2026-09-20T10:00:00+00:00"
        exposure = svc.record_exposure(OPERATOR, "M-101", "搜索", ["餐饮商家"], now=ts)
        self.assertEqual(exposure["package_id"], "BASE")


class HistoryTest(unittest.TestCase):
    def test_rollback_and_emergency_stop_preserve_history(self) -> None:
        svc = make_service()
        publish_ready(svc, "P1")
        svc.promote(OPERATOR, "P1", now=T0)
        svc.rollback(OPERATOR, "P1", "投诉激增，回滚观察", now=T0)
        self.assertEqual(svc.status_of("P1"), "rolled_back")
        svc.emergency_stop(OPERATOR, "P1", "监管要求立即止损", now=T0)
        self.assertEqual(svc.status_of("P1"), "stopped")
        types = [e["type"] for e in svc.store.for_package("P1")]
        # 止损与回滚是追加事件，发布与转正历史仍在
        self.assertIn("package_published", types)
        self.assertIn("package_promoted", types)
        self.assertIn("package_rolled_back", types)
        self.assertIn("emergency_stopped", types)
        self.assertFalse(hasattr(svc.store, "delete"))
        self.assertFalse(hasattr(svc.store, "update"))

    def test_stopped_package_no_longer_resolves(self) -> None:
        svc = make_service()
        publish_ready(svc, "P1")
        svc.promote(OPERATOR, "P1", now=T0)
        svc.emergency_stop(OPERATOR, "P1", "紧急止损", now=T0)
        exposure = svc.record_exposure(OPERATOR, "M-101", "搜索", ["餐饮商家"],
                                     now="2026-09-20T10:00:00+00:00")
        self.assertIsNone(exposure["package_id"])
        self.assertEqual(exposure["algorithm_version"], "platform-default")


class PermissionTest(unittest.TestCase):
    def test_reviewer_cannot_modify_production_rules(self) -> None:
        svc = make_service()
        with self.assertRaises(PermissionError):
            svc.create_draft(REVIEWER, "P1", "规则包P1", sample_content(), now=T0)
        publish_ready(svc, "P1")
        with self.assertRaises(PermissionError):
            svc.rollback(REVIEWER, "P1", "试图回滚", now=T0)
        with self.assertRaises(PermissionError):
            svc.emergency_stop(REVIEWER, "P1", "试图止损", now=T0)
        # 但审查可以读取与复算
        self.assertTrue(reviewer_recompute(svc, "P1")["package_id"] == "P1")

    def test_approval_requires_approver_role_and_basis(self) -> None:
        svc = make_service()
        svc.create_draft(OPERATOR, "P1", "规则包P1", sample_content(), now=T0)
        svc.freeze(OPERATOR, "P1", now=T0)
        with self.assertRaises(PermissionError):
            svc.submit_approval(REVIEWER, "P1", "运营", "approve", "通过", now=T0)
        with self.assertRaises(StateError):
            svc.submit_approval(APPROVER, "P1", "运营", "approve", "", now=T0)


class DisclosureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        publish_ready(self.svc, "P1")
        self.view = self.svc.package_view("P1")

    def test_merchant_view_hides_model_and_secrets(self) -> None:
        shown = disclose_package(self.view, "merchant", "M-101")
        blob = json.dumps(shown, ensure_ascii=False)
        self.assertNotIn("ranking_weights", blob)
        self.assertNotIn("margin_floor", blob)
        self.assertIn("appeal_policy", blob)

    def test_reviewer_view_aggregates_merchant_secrets(self) -> None:
        shown = disclose_package(self.view, "reviewer")
        blob = json.dumps(shown, ensure_ascii=False)
        self.assertNotIn("ranking_weights", blob)
        self.assertNotIn("margin_floor", blob)
        self.assertIn("merchant_scope", blob)  # 可见群体范围，可复算影响

    def test_auditor_sees_full_content(self) -> None:
        shown = disclose_package(self.view, "auditor")
        blob = json.dumps(shown, ensure_ascii=False)
        self.assertIn("ranking_weights", blob)
        self.assertIn("margin_floor", blob)


class AlertTest(unittest.TestCase):
    def _scenario(self) -> FairChangeService:
        svc = make_service()
        publish_ready(svc, "P1")
        svc.promote(OPERATOR, "P1", now=T0)
        ts = "2026-09-20T09:00:00+00:00"
        for mid in ("M-101", "M-102", "M-103", "M-201"):
            svc.record_exposure(OPERATOR, mid, "搜索", ["餐饮商家"], now=ts)
        metrics = json.loads(
            (Path(__file__).resolve().parents[1] / "fixtures" / "metrics.json")
            .read_text(encoding="utf-8"))
        svc.record_metrics({"id": "an-1", "role": "analyst"}, metrics)
        return svc

    def test_explainable_alerts_for_three_patterns(self) -> None:
        svc = self._scenario()
        alerts = analyze_competition(svc, now="2026-09-23T00:00:00+00:00")
        kinds = {a["kind"] for a in alerts}
        self.assertEqual(
            kinds,
            {"concentrated_demotion", "forced_price_following", "exit_difficulty"},
        )
        for alert in alerts:
            self.assertEqual(alert["package_id"], "P1")
            self.assertTrue(alert["explanation"])
            self.assertTrue(alert["evidence"])
        demotion = next(a for a in alerts if a["kind"] == "concentrated_demotion")
        self.assertEqual(demotion["affected_group"], "中小餐饮")
        self.assertIn("75%", demotion["explanation"])
        # 预警本身也进入只增日志
        self.assertEqual(len(svc.store.of_type("alert_raised")), 3)

    def test_below_threshold_no_alert(self) -> None:
        svc = make_service()
        publish_ready(svc, "P1")
        svc.record_metrics(OPERATOR, [
            {"kind": "demotion", "merchant_id": "M-1", "group": "中小餐饮",
             "package_id": "P1", "ts": "2026-09-20T10:00:00+00:00"},
        ])
        self.assertEqual(analyze_competition(svc), [])


class TraceTest(unittest.TestCase):
    def test_trace_complaint_back_to_rule_and_approval_basis(self) -> None:
        svc = make_service()
        publish_ready(svc, "P1")
        svc.promote(OPERATOR, "P1", now=T0)
        ts = "2026-09-20T10:00:00+00:00"
        svc.record_exposure(OPERATOR, "M-101", "搜索", ["餐饮商家"], now=ts)
        complaint = svc.record_complaint(
            {"id": "M-101", "role": "merchant"}, "M-101", "搜索",
            "2026-09-20T12:00:00+00:00", "流量异常", "曝光骤降")
        result = svc.trace_complaint(complaint["complaint_id"])
        self.assertEqual(len(result["rules"]), 1)
        rule = result["rules"][0]
        self.assertEqual(rule["package_id"], "P1")
        self.assertEqual(len(rule["approvals"]), len(svc.required_teams))
        for approval in rule["approvals"]:
            self.assertTrue(approval["basis"])
        self.assertTrue(rule["content_hash"])

    def test_trace_metric_back_to_rule(self) -> None:
        svc = make_service()
        publish_ready(svc, "P1")
        svc.record_metrics(OPERATOR, [
            {"kind": "demotion", "merchant_id": "M-1", "group": "中小餐饮",
             "package_id": "P1", "ts": "2026-09-20T10:00:00+00:00"},
        ])
        metric_id = svc.metrics()[0]["metric_id"]
        result = svc.trace_metric(metric_id)
        self.assertEqual(result["rule"]["package_id"], "P1")
        self.assertEqual(result["rule"]["status"], "published")


if __name__ == "__main__":
    unittest.main()
