"""平台规则公平变更后台 —— HTTP 入口（标准库，零三方依赖）。

角色通过请求头声明：
    X-Role: OPERATOR | APPROVER | AUDITOR | MERCHANT
    X-User: 操作人名称
    X-Team: 审批团队（APPROVER 必填）
    X-Merchant-Id: 商家本人编号（MERCHANT 必填）

状态默认保存在内存；设置环境变量 RULE_STATE_FILE 后，每次写操作会把
快照（含哈希链账本）落盘，重启自动恢复。
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from src.api import APPROVER, PermissionError_, Principal, RuleApi
from src.catalog import load_context
from src.store import RuleError, RuleStore

_STATE_FILE = os.environ.get("RULE_STATE_FILE")

# 审批团队 HTTP 代号 -> 领域内团队名（HTTP 头只能用拉丁字符）
TEAM_CODES = {
    "governance": "规则治理",
    "algorithm": "算法",
    "compliance": "合规",
    "ecosystem": "商家生态",
}


class Service:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.store = self._load()
        self.api = RuleApi(self.store)

    def _load(self) -> RuleStore:
        if _STATE_FILE and os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, encoding="utf-8") as f:
                return RuleStore.restore(json.load(f))
        return RuleStore()

    def persist(self) -> None:
        if _STATE_FILE:
            tmp = _STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.store.snapshot(), f, ensure_ascii=False)
            os.replace(tmp, _STATE_FILE)


SERVICE = Service()

# 需要落盘的方法名前缀
_MUTATING = {
    "register_merchant", "create_package", "approve", "notify", "start_gray",
    "adjust_gray", "activate", "emergency_stop", "rollback", "mark_baseline",
    "grant_exemption", "revoke_exemption", "file_appeal", "resolve_appeal",
    "resolve_exposure",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音默认访问日志
        pass

    # ---- 工具 ----------------------------------------------------------
    def _principal(self) -> Principal:
        role = self.headers.get("X-Role", "PUBLIC")
        team_code = self.headers.get("X-Team")
        return Principal(
            role=role,
            name=self.headers.get("X-User", role),
            team=TEAM_CODES.get(team_code, team_code) if team_code else None,
            merchant_id=self.headers.get("X-Merchant-Id"),
        )

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise RuleError("请求体不是合法 JSON")
        return data if isinstance(data, dict) else {}

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method_name: str, *args, **kwargs):
        p = self._principal()
        with SERVICE.lock:
            fn = getattr(SERVICE.api, method_name)
            result = fn(p, *args, **kwargs)
            if method_name in _MUTATING:
                SERVICE.persist()
            return result

    def _run(self, method_name: str, *args, **kwargs):
        try:
            result = self._dispatch(method_name, *args, **kwargs)
        except RuleError as e:
            return self._send(400, {"error": str(e)})
        except KeyError as e:
            return self._send(404, {"error": f"对象不存在：{e.args[0]}"})
        except PermissionError_ as e:
            return self._send(403, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._send(400, {"error": str(e)})
        self._send(200, {} if result is None else result)

    # ---- 路由 ----------------------------------------------------------
    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if path == "/health":
            return self._send(200, {"status": "ok"})
        if path == "/context":
            return self._send(200, load_context())

        parts = [x for x in path.split("/") if x]
        # /packages/<hash>/...
        if len(parts) >= 2 and parts[0] == "packages":
            h = parts[1]
            if len(parts) == 2:
                return self._run("get_package", h)
            sub = parts[2]
            if sub == "preflight":
                return self._run("preflight", h)
            if sub == "impact":
                return self._run("impact", h, q.get("base"))
            if sub == "alerts":
                thresholds = None
                return self._run(
                    "evaluate_alerts", h, q.get("base"), thresholds,
                    q.get("record") == "1")
            if sub == "history":
                return self._run("history", h)
            return self._send(404, {"error": "未知子资源"})

        if path == "/packages":
            return self._run("list_packages")
        if path == "/ledger/verify":
            return self._run("verify_ledger")
        if path == "/trace/complaint":
            if "merchant_id" not in q:
                return self._send(400, {"error": "需要 merchant_id"})
            return self._run(
                "trace_complaint", q["merchant_id"], q.get("entry_id"),
                _f(q.get("start_ts")), _f(q.get("end_ts")))
        if path == "/trace/metric":
            if "segment" not in q:
                return self._send(400, {"error": "需要 segment"})
            return self._run(
                "trace_metric", q["segment"],
                _f(q.get("start_ts")), _f(q.get("end_ts")))
        return self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        body = self._read_json()
        parts = [x for x in path.split("/") if x]

        if path == "/admin/merchants":
            return self._run("register_merchant", body)
        if path == "/packages":
            return self._run("create_package", body)
        if path == "/exposures":
            return self._run("resolve_exposure",
                             body.get("merchant_id", ""),
                             body.get("entry_id", ""),
                             body.get("cohort", ""))
        if path == "/rollback":
            return self._run("rollback", body.get("target_hash", ""),
                             body.get("reason", ""))
        if path == "/gray/adjust":
            return self._run("adjust_gray", int(body.get("percent", 0)))
        if path == "/appeals":
            return self._run("file_appeal", body.get("package_hash", ""),
                             body.get("exposure_id", ""),
                             body.get("reason", ""),
                             body.get("merchant_id"))

        if len(parts) >= 3 and parts[0] == "packages":
            h = parts[1]
            action = parts[2]
            if action == "approve":
                return self._run("approve", h, body.get("comment", ""))
            if action == "notify":
                return self._run("notify", h, body.get("merchant_id", ""),
                                 body.get("channel"),
                                 _f(body.get("ts")))
            if action == "gray":
                return self._run("start_gray", h,
                                 int(body.get("percent", 0)),
                                 body.get("cohorts"))
            if action == "activate":
                return self._run("activate", h)
            if action == "stop":
                return self._run("emergency_stop", h, body.get("reason", ""))
            if action == "baseline":
                return self._run("mark_baseline", h)
            if action == "exemptions":
                return self._run("grant_exemption", h,
                                 body.get("merchant_id", ""),
                                 body.get("reason", ""),
                                 body.get("constraint_ids"))
        if len(parts) == 3 and parts[0] == "exemptions":
            return self._run("revoke_exemption", parts[1],
                             body.get("reason", ""))
        if len(parts) == 3 and parts[0] == "appeals":
            return self._run("resolve_appeal", parts[1],
                             bool(body.get("uphold")),
                             body.get("resolution", ""))
        return self.send_error(404)


def _f(value):
    return float(value) if value not in (None, "") else None


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
