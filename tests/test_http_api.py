"""HTTP 端到端集成测试：真实启动服务，用 urllib 走网络验证角色权限、
上线闸门、灰度归属、止损留痕、审查只读与磁盘持久化。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

_tmp = tempfile.mkdtemp(prefix="rule-state-")
os.environ["RULE_STATE_FILE"] = os.path.join(_tmp, "state.json")

from src import server  # noqa: E402
from src.demo import (  # noqa: E402
    MERCHANTS, baseline_package, build_demo_store, candidate_package,
)
from src.models import to_jsonable  # noqa: E402
from src.server import TEAM_CODES  # noqa: E402


def _free_packages():
    """演示基线/候选包的 package_id 与服务全局已有的可能冲突，
    用带后缀的独立 id 构造 HTTP 场景。"""
    base = baseline_package()
    cand = candidate_package()
    bd = to_jsonable(base)
    cd = to_jsonable(cand)
    bd["package_id"] = "HTTP-RULE"
    cd["package_id"] = "HTTP-RULE"
    return bd, cd


class HttpIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def call(self, method: str, path: str, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_full_lifecycle_over_http(self) -> None:
        ops = {"X-Role": "OPERATOR", "X-User": "ops-http"}
        aud = {"X-Role": "AUDITOR", "X-User": "aud-http"}

        # 健康检查与领域资料
        self.assertEqual(self.call("GET", "/health")[0], 200)
        self.assertEqual(self.call("GET", "/context")[1]["project"],
                         "平台规则公平变更")

        # 录入商家
        for m in MERCHANTS:
            self.assertEqual(
                self.call("POST", "/admin/merchants", m, ops)[0], 200)

        bd, cd = _free_packages()
        # 基线包
        _, created = self.call("POST", "/packages", bd, ops)
        bh = created if isinstance(created, str) else None
        # create_package 返回字符串时 json 为 "..."
        bh = json.loads(json.dumps(created))
        self.assertTrue(bh.startswith("sha256:"))
        for code in TEAM_CODES:
            h = {"X-Role": "APPROVER", "X-User": f"ap-{code}",
                 "X-Team": code}
            self.assertEqual(
                self.call("POST", f"/packages/{bh}/approve", {}, h)[0], 200)

        # 通知提前期：直接给一个很早的 ts
        for m in MERCHANTS:
            self.call("POST", f"/packages/{bh}/notify",
                      {"merchant_id": m["merchant_id"],
                       "ts": 1_699_000_000.0}, ops)
        # 闸门通过 -> 上线 -> 标记基线
        pre = self.call("GET", f"/packages/{bh}/preflight", headers=ops)[1]
        self.assertTrue(pre["ready"])
        self.assertEqual(
            self.call("POST", f"/packages/{bh}/activate", {}, ops)[0], 200)
        self.assertEqual(
            self.call("POST", f"/packages/{bh}/baseline", {}, ops)[0], 200)

        # 候选包：先看上线前清单（缺会签、未通知）
        _, ch = self.call("POST", "/packages", cd, ops)
        pre = self.call("GET", f"/packages/{ch}/preflight", headers=ops)[1]
        self.assertFalse(pre["ready"])
        self.assertTrue(pre["missing_approval_teams"])
        self.assertEqual(pre["affected_merchant_count"], len(MERCHANTS))

        # 会签 + 通知 + 灰度
        for code in TEAM_CODES:
            h = {"X-Role": "APPROVER", "X-User": f"ap2-{code}",
                 "X-Team": code}
            self.call("POST", f"/packages/{ch}/approve", {}, h)
        for m in MERCHANTS:
            self.call("POST", f"/packages/{ch}/notify",
                      {"merchant_id": m["merchant_id"],
                       "ts": 1_699_000_000.0}, ops)
        self.assertEqual(
            self.call("POST", f"/packages/{ch}/gray",
                      {"percent": 100}, ops)[0], 200)

        # 灰度曝光逐次归属
        code, exp = self.call("POST", "/exposures",
                              {"merchant_id": "m-a3",
                               "entry_id": "search_home"}, ops)
        self.assertEqual(code, 200)
        self.assertEqual(exp["layer"], "GRAY")
        self.assertEqual(exp["content_hash"], ch)
        self.assertEqual(exp["algorithm_version"], "algo-v2")

        # 组合预警（审查人可评估，只读不落库）
        code, alert_resp = self.call(
            "GET", f"/packages/{ch}/alerts?base={bh}", headers=aud)
        self.assertEqual(code, 200)
        codes = {a["code"] for a in alert_resp}
        self.assertIn("FORCED_PRICE_FOLLOWING", codes)
        # 审查人看到的是假名商家
        forced = next(a for a in alert_resp
                      if a["code"] == "FORCED_PRICE_FOLLOWING")
        self.assertTrue(all(x.startswith("M-")
                            for x in forced["affected_merchants"]))

        # 审查人能复算影响
        code, impact_resp = self.call(
            "GET", f"/packages/{ch}/impact?base={bh}", headers=aud)
        self.assertEqual(code, 200)
        self.assertNotIn("[REDACTED", json.dumps(impact_resp["weight_change"]))

        # 审查人不能改生产规则
        for path, body in [
            (f"/packages/{ch}/stop", {"reason": "x"}),
            ("/rollback", {"target_hash": bh, "reason": "x"}),
            (f"/packages/{ch}/exemptions",
             {"merchant_id": "m-a1", "reason": "x"}),
        ]:
            code, err = self.call("POST", path, body, aud)
            self.assertEqual(code, 403, path)

        # 运营紧急止损：历史保留，正式版本回落基线
        self.assertEqual(
            self.call("POST", f"/packages/{ch}/stop",
                      {"reason": "监控异常"}, ops)[0], 200)
        hist = self.call("GET", f"/packages/{ch}/history", headers=ops)[1]
        kinds = {e["kind"] for e in hist}
        self.assertIn("GRAY_STARTED", kinds)
        self.assertIn("EMERGENCY_STOP", kinds)
        # 止损只追加事件，灰度曝光归属记录仍在
        self.assertIn("EXPOSURE_RESOLVED", kinds)

        # 回滚基线
        self.assertEqual(
            self.call("POST", "/rollback",
                      {"target_hash": bh, "reason": "恢复"}, ops)[0], 200)

        # 从投诉反查：能定位规则、批准依据与止损事件
        code, traced = self.call(
            "GET", "/trace/complaint?merchant_id=m-a3", headers=aud)
        self.assertEqual(code, 200)
        self.assertTrue(traced["found"])
        chain = next(c for c in traced["rule_chains"]
                     if c["content_hash"] == ch)
        self.assertEqual(len(chain["approval_basis"]), len(TEAM_CODES))
        self.assertTrue(any(
            e["kind"] == "EMERGENCY_STOP"
            for e in chain["lifecycle_events"]))

        # 从竞争指标反查
        code, metric = self.call(
            "GET", "/trace/metric?segment=A", headers=aud)
        self.assertEqual(code, 200)
        self.assertTrue(metric["active_packages"])

        # 账本完好
        verify = self.call("GET", "/ledger/verify", headers=aud)[1]
        self.assertTrue(verify["intact"])

        # 持久化文件已写出且可解析
        self.assertTrue(os.path.exists(os.environ["RULE_STATE_FILE"]))
        with open(os.environ["RULE_STATE_FILE"], encoding="utf-8") as f:
            saved = json.load(f)
        self.assertIn("events", saved)


if __name__ == "__main__":
    unittest.main()
