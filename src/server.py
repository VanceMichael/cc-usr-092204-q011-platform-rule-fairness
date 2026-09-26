import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from src.catalog import load_context
from src.fairchange.analysis import analyze_competition
from src.fairchange.disclosure import disclose_package, reviewer_recompute
from src.fairchange.service import ConflictError, FairChangeService, StateError


def build_handler(service: FairChangeService):
    """把领域服务暴露为 JSON API。

    调用方身份通过请求头传入：X-Actor-Id、X-Actor-Role。
    缺省角色为 reviewer（只读），写生产规则必须显式使用 operator。
    """

    def actor_of(headers):
        return {
            "id": headers.get("X-Actor-Id", "anonymous"),
            "role": headers.get("X-Actor-Role", "reviewer"),
        }

    routes = []

    def route(method, pattern):
        def register(fn):
            routes.append((method, re.compile(f"^{pattern}$"), fn))
            return fn
        return register

    @route("GET", r"/health")
    def health(handler, match, query):
        return {"status": "ok"}

    @route("GET", r"/context")
    def context(handler, match, query):
        return load_context()

    # ---- 发布包生命周期 ----
    @route("GET", r"/api/packages")
    def list_packages(handler, match, query):
        return {"packages": service.packages()}

    @route("POST", r"/api/packages")
    def create_package(handler, match, query):
        body = handler.body
        return service.create_draft(
            actor_of(handler.headers), body["package_id"], body["title"], body["content"])

    @route("POST", r"/api/packages/([^/]+)/freeze")
    def freeze(handler, match, query):
        return service.freeze(actor_of(handler.headers), match.group(1))

    @route("POST", r"/api/packages/([^/]+)/approvals")
    def approve(handler, match, query):
        body = handler.body
        return service.submit_approval(
            actor_of(handler.headers), match.group(1),
            body["team"], body["decision"], body["basis"])

    @route("GET", r"/api/packages/([^/]+)/approvals")
    def approvals(handler, match, query):
        return service.approval_status(match.group(1))

    @route("POST", r"/api/packages/([^/]+)/notifications")
    def notify(handler, match, query):
        body = handler.body
        return service.record_notification(
            actor_of(handler.headers), match.group(1), body["group"], body["channel"])

    @route("GET", r"/api/packages/([^/]+)/preflight")
    def preflight(handler, match, query):
        return service.preflight(match.group(1))

    @route("POST", r"/api/packages/([^/]+)/publish")
    def publish(handler, match, query):
        body = handler.body
        return service.publish(actor_of(handler.headers), match.group(1),
                               emergency=body.get("emergency", False),
                               reason=body.get("reason"))

    @route("POST", r"/api/packages/([^/]+)/promote")
    def promote(handler, match, query):
        return service.promote(actor_of(handler.headers), match.group(1),
                               supersedes=handler.body.get("supersedes"))

    @route("POST", r"/api/packages/([^/]+)/rollback")
    def rollback(handler, match, query):
        body = handler.body
        return service.rollback(actor_of(handler.headers), match.group(1),
                                body["reason"], body.get("to_package_id"))

    @route("POST", r"/api/packages/([^/]+)/emergency-stop")
    def emergency_stop(handler, match, query):
        return service.emergency_stop(actor_of(handler.headers), match.group(1),
                                      handler.body["reason"])

    @route("POST", r"/api/packages/([^/]+)/exemptions")
    def exempt(handler, match, query):
        body = handler.body
        return service.grant_exemption(
            actor_of(handler.headers), match.group(1), body["reason"],
            body["expires_at"], body.get("merchant_ids", ()), body.get("groups", ()))

    # ---- 披露与复算 ----
    @route("GET", r"/api/packages/([^/]+)/disclosure")
    def disclosure(handler, match, query):
        role = query.get("role", ["merchant"])[0]
        merchant_id = query.get("merchant_id", [None])[0]
        return disclose_package(service.package_view(match.group(1)), role, merchant_id)

    @route("GET", r"/api/packages/([^/]+)/recompute")
    def recompute(handler, match, query):
        return reviewer_recompute(service, match.group(1))

    # ---- 曝光、投诉、指标、预警、反查 ----
    @route("POST", r"/api/exposures")
    def exposure(handler, match, query):
        body = handler.body
        return service.record_exposure(
            actor_of(handler.headers), body["merchant_id"], body["entry"],
            body.get("merchant_groups", ()), body.get("ts"))

    @route("GET", r"/api/exposures")
    def exposures(handler, match, query):
        return {"exposures": service.exposures()}

    @route("POST", r"/api/complaints")
    def complaint(handler, match, query):
        body = handler.body
        return service.record_complaint(
            actor_of(handler.headers), body["merchant_id"], body["entry"],
            body["ts"], body["category"], body["detail"])

    @route("POST", r"/api/metrics")
    def metrics(handler, match, query):
        return {"metrics": service.record_metrics(
            actor_of(handler.headers), handler.body.get("metrics", []))}

    @route("POST", r"/api/alerts/analyze")
    def analyze(handler, match, query):
        return {"alerts": analyze_competition(service)}

    @route("GET", r"/api/alerts")
    def alerts(handler, match, query):
        return {"alerts": service.store.of_type("alert_raised")}

    @route("GET", r"/api/trace/complaints/([^/]+)")
    def trace_complaint(handler, match, query):
        return service.trace_complaint(match.group(1))

    @route("GET", r"/api/trace/metrics/([^/]+)")
    def trace_metric(handler, match, query):
        return service.trace_metric(match.group(1))

    @route("GET", r"/api/events")
    def events(handler, match, query):
        if actor_of(handler.headers)["role"] != "auditor":
            raise PermissionError("完整事件日志仅审计人员可见")
        return {"events": service.store.all()}

    class Handler(BaseHTTPRequestHandler):
        body: dict

        def _dispatch(self, method: str) -> None:
            split = urlsplit(self.path)
            query = parse_qs(split.query)
            length = int(self.headers.get("Content-Length") or 0)
            self.body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            for route_method, pattern, fn in routes:
                if route_method != method:
                    continue
                match = pattern.match(split.path)
                if match:
                    try:
                        payload = fn(self, match, query)
                    except PermissionError as exc:
                        self._send({"error": str(exc)}, 403)
                    except (StateError, ConflictError) as exc:
                        self._send({"error": str(exc)}, 409)
                    except KeyError as exc:
                        self._send({"error": f"未找到: {exc.args[0]}"}, 404)
                    except (ValueError, TypeError) as exc:
                        self._send({"error": str(exc)}, 400)
                    else:
                        self._send(payload)
                    return
            self._send({"error": "未找到路径"}, 404)

        def _send(self, payload, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, format, *args):  # 保持测试输出安静
            pass

    return Handler


service = FairChangeService()
Handler = build_handler(service)

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
