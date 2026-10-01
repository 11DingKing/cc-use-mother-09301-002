"""HTTP 接口（标准库实现，无第三方依赖）。

鉴权：请求头 X-Actor-Role / X-Actor-Name。
所有写命令支持 X-Idempotency-Key；封存/更正支持 X-Txn。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .errors import DomainError
from .models import MetricKind, SchoolType
from .repository import Repository
from .services import (
    ApplicationService, ROLE_ADMIN, ROLE_AUDITOR, ROLE_FILER,
)
from .store import EventStore

STYPE = {t.value: t for t in SchoolType}

# HTTP 头只能携带 latin-1，角色使用 ASCII 别名；姓名用百分号编码。
ROLE_ALIASES = {
    "admin": ROLE_ADMIN, "评价管理人员": ROLE_ADMIN,
    "filer": ROLE_FILER, "高校填报员": ROLE_FILER,
    "auditor": ROLE_AUDITOR, "审计人员": ROLE_AUDITOR,
}


class Api:
    def __init__(self, store_path: str | None = None) -> None:
        self.store = EventStore(store_path)
        self.repo = Repository(self.store)
        self.svc = ApplicationService(self.repo)

    def reset_for_tests(self) -> None:
        self.repo = Repository(self.store)
        self.svc = ApplicationService(self.repo)


def _enum_or_400(value: str, mapping: dict, name: str):
    if value not in mapping:
        raise DomainError(f"非法{name}：{value}")
    return mapping[value]


def create_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "IndicatorRegistry/1.0"

        def log_message(self, fmt, *args):  # 静音测试输出
            pass

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise DomainError("请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise DomainError("请求体必须是 JSON 对象")
            return data

        def _role(self) -> str:
            raw = self.headers.get("X-Actor-Role", "")
            return ROLE_ALIASES.get(raw, raw)

        def _ctx(self) -> tuple[str, str, str, str]:
            role = self._role()
            raw_name = self.headers.get("X-Actor-Name", "")
            actor = unquote(raw_name) if raw_name else (role or "匿名")
            return role, actor, self.headers.get("X-Idempotency-Key", ""), self.headers.get("X-Txn", "")

        # ---------------------------------------------------------- routing

        def do_GET(self) -> None:
            try:
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                role = self._role()
                if url.path == "/health":
                    self._send(200, {"ok": True, "events": len(api.store.events)})
                elif url.path == "/metrics":
                    items = [m.__dict__ | {"school_type": m.school_type.value,
                                           "kind": m.kind.value}
                             for m in api.repo.metrics.values()]
                    self._send(200, items)
                elif url.path == "/calibers":
                    st = _enum_or_400(q["school_type"], STYPE, "办学类型") if "school_type" in q else None
                    self._send(200, AuditServiceLister(api.repo).list_calibers(st))
                elif url.path.startswith("/publications/"):
                    self._publication_detail(url.path.rsplit("/", 1)[-1])
                elif url.path == "/publications":
                    st = _enum_or_400(q["school_type"], STYPE, "办学类型")
                    year = int(q["year"])
                    self._send(200, api.svc.audit_view(role).publication_history(year, st))
                elif url.path == "/audit/verify":
                    self._send(200, api.svc.audit_view(role).verify_chain())
                elif url.path == "/audit/recompute":
                    st = _enum_or_400(q["school_type"], STYPE, "办学类型")
                    year = int(q["year"])
                    self._send(200, api.svc.audit_view(role).recompute_year(year, st))
                elif url.path == "/audit/changes":
                    st = _enum_or_400(q["school_type"], STYPE, "办学类型")
                    year = int(q["year"])
                    self._send(200, api.svc.audit_view(role).explain_changes(year, st))
                else:
                    self._send(404, {"error": "not_found", "message": self.path})
            except DomainError as ex:
                self._send(ex.status, {"error": ex.code, "message": str(ex)})
            except (KeyError, ValueError) as ex:
                self._send(400, {"error": "bad_request", "message": str(ex)})

        def do_POST(self) -> None:
            try:
                role, actor, idem, txn = self._ctx()
                data = self._body()
                url = urlparse(self.path).path
                svc = api.svc

                if url == "/schools":
                    r = svc.register_school(
                        role, actor, code=data["code"], name=data["name"],
                        school_type=_enum_or_400(data["school_type"], STYPE, "办学类型"),
                        event_id=idem)
                    self._send(201, {"school_code": r["school_code"]})

                elif url == "/metrics":
                    m = svc.register_metric(
                        role, actor, code=data["code"], name=data["name"],
                        school_type=_enum_or_400(data["school_type"], STYPE, "办学类型"),
                        kind=_enum_or_400(data.get("kind", "定量"),
                                          {k.value: k for k in MetricKind}, "指标类型"),
                        unit=data.get("unit", ""), source=data["source"],
                        evidence_required=data.get("evidence_required", True),
                        description=data.get("description", ""), event_id=idem)
                    self._send(201, {"code": m.code})

                elif url == "/calibers":
                    win = data["window"]
                    c = svc.create_caliber(
                        role, actor,
                        school_type=_enum_or_400(data["school_type"], STYPE, "办学类型"),
                        year=int(data["year"]),
                        weights={k: float(v) for k, v in data["weights"].items()},
                        window=(int(win["start_year"]),
                                win.get("end_year") and int(win["end_year"])),
                        required_signers=int(data["required_signers"]),
                        event_id=idem)
                    self._send(201, {"caliber_id": c["id"], "version": c["version"]})

                elif url.endswith("/cosign/start"):
                    cid = url.split("/")[2]
                    svc.start_cosign(role, actor, cid)
                    self._send(200, {"caliber_id": cid, "status": "会签"})

                elif url.endswith("/cosign/sign"):
                    cid = url.split("/")[2]
                    svc.sign_caliber(role, actor, cid, data.get("signer"))
                    self._send(200, {"caliber_id": cid, "signed": data.get("signer") or actor})

                elif url.endswith("/cosign/revoke"):
                    cid = url.split("/")[2]
                    svc.revoke_sign(role, actor, cid, data["signer"], data.get("reason", ""))
                    self._send(200, {"caliber_id": cid, "revoked": data["signer"]})

                elif url.endswith("/publish"):
                    cid = url.split("/")[2]
                    c = svc.publish_caliber(role, actor, cid, data.get("supersedes"))
                    self._send(200, {"caliber_id": cid, "status": c["status"].value})

                elif url == "/submissions":
                    s = svc.submit_data(
                        role, actor, year=int(data["year"]),
                        school_code=data["school_code"], metric_code=data["metric_code"],
                        value=data.get("value"), evidence_ref=data.get("evidence_ref", ""),
                        event_id=idem, timestamp=data.get("timestamp"))
                    self._send(201, {"submission_id": s["id"], "status": s["status"].value})

                elif url.endswith("/seal"):
                    cid = url.split("/")[2]
                    pub = svc.seal_publication(role, actor, cid, txn=txn,
                                               timestamp=data.get("timestamp"))
                    self._send(201, {"publication_id": pub["id"], "version": pub["version"],
                                     "manifest": pub["manifest"],
                                     "missing_count": len(pub["missing"])})

                elif url == "/publications/correct":
                    pub = svc.correct_publication(
                        role, actor, old_publication_id=data["old_publication_id"],
                        new_caliber_id=data["new_caliber_id"], reason=data["reason"],
                        txn=txn, timestamp=data.get("timestamp"))
                    self._send(201, {"new_publication_id": pub["id"], "version": pub["version"],
                                     "manifest": pub["manifest"]})
                else:
                    self._send(404, {"error": "not_found", "message": url})
            except DomainError as ex:
                self._send(ex.status, {"error": ex.code, "message": str(ex)})
            except (KeyError, ValueError) as ex:
                self._send(400, {"error": "bad_request", "message": str(ex)})

        def _publication_detail(self, pid: str) -> None:
            pub = api.repo.publications.get(pid)
            if not pub:
                self._send(404, {"error": "not_found", "message": pid})
                return
            self._send(200, AuditServiceLister(api.repo)._publication_view(pub))

    return Handler


def AuditServiceLister(repo: Repository):
    from .audit import AuditService
    return AuditService(repo)


def serve(host: str = "127.0.0.1", port: int = 8080,
          store_path: str | None = None) -> tuple[ThreadingHTTPServer, Api]:
    api = Api(store_path)
    httpd = ThreadingHTTPServer((host, port), create_handler(api))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, api


if __name__ == "__main__":
    import sys
    p = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    h = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    path = sys.argv[3] if len(sys.argv) > 3 else None
    httpd, _ = serve(h, p, path)
    print(f"listening on http://{h}:{p}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()
