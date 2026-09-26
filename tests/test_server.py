import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from src.fairchange.service import FairChangeService
from src.server import build_handler

OPERATOR_HEADERS = {"X-Actor-Id": "op-1", "X-Actor-Role": "operator"}
APPROVER_HEADERS = {"X-Actor-Id": "ap-1", "X-Actor-Role": "approver"}
REVIEWER_HEADERS = {"X-Actor-Id": "rev-1", "X-Actor-Role": "reviewer"}


def content() -> dict:
    return {
        "algorithm_version": "rank-v2.3.1",
        "merchant_scope": {"groups": ["餐饮商家"], "estimated_merchants": 100},
        "traffic_entries": ["搜索"],
        "subsidy_conditions": {"full_reduction": "满30减5"},
        "price_constraints": {"max_price": 59.9},
        "notification_plan": [{"group": "餐饮商家", "channel": "站内信"}],
        "appeal_policy": {"channel": "商家后台-申诉中心", "sla_days": 7},
        "rollout": {"stages": [{"name": "灰度", "percent": 100}]},
    }


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(FairChangeService()))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None,
             headers: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        payload = (json.dumps(body, ensure_ascii=False).encode("utf-8")
                   if body is not None else None)
        all_headers = dict(headers or {})
        if payload is not None:
            all_headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, all_headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_health(self) -> None:
        status, data = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_full_flow_and_permissions(self) -> None:
        # 审查角色不能创建发布包
        status, _ = self.call("POST", "/api/packages", {
            "package_id": "P1", "title": "规则包P1", "content": content(),
        }, REVIEWER_HEADERS)
        self.assertEqual(status, 403)

        status, _ = self.call("POST", "/api/packages", {
            "package_id": "P1", "title": "规则包P1", "content": content(),
        }, OPERATOR_HEADERS)
        self.assertEqual(status, 200)

        status, _ = self.call("POST", "/api/packages/P1/freeze", {}, OPERATOR_HEADERS)
        self.assertEqual(status, 200)

        # 通知未完成时发布被拦截，并列出待办
        for team in ("运营", "算法", "法务", "风控"):
            status, _ = self.call("POST", "/api/packages/P1/approvals", {
                "team": team, "decision": "approve", "basis": f"{team}评审通过",
            }, APPROVER_HEADERS)
            self.assertEqual(status, 200)
        status, data = self.call("POST", "/api/packages/P1/publish", {}, OPERATOR_HEADERS)
        self.assertEqual(status, 409)
        status, data = self.call("GET", "/api/packages/P1/preflight")
        self.assertEqual(status, 200)
        self.assertFalse(data["ready"])
        self.assertEqual(data["affected_groups"], ["餐饮商家"])

        self.call("POST", "/api/packages/P1/notifications",
                  {"group": "餐饮商家", "channel": "站内信"}, OPERATOR_HEADERS)
        status, _ = self.call("POST", "/api/packages/P1/publish", {}, OPERATOR_HEADERS)
        self.assertEqual(status, 200)

        # 灰度曝光归属到生效发布包
        status, data = self.call("POST", "/api/exposures", {
            "merchant_id": "M-1", "entry": "搜索", "merchant_groups": ["餐饮商家"],
        }, OPERATOR_HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(data["package_id"], "P1")
        self.assertTrue(data["content_hash"])

        # 事件日志仅审计可见
        status, _ = self.call("GET", "/api/events", headers=REVIEWER_HEADERS)
        self.assertEqual(status, 403)
        status, data = self.call("GET", "/api/events",
                                 headers={"X-Actor-Id": "aud-1", "X-Actor-Role": "auditor"})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data["events"]), 5)


if __name__ == "__main__":
    unittest.main()
