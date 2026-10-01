"""HTTP 接口与真实线程并发测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from indicator_registry.api import serve
from indicator_registry import MetricKind, SchoolType, ValidityWindow

import urllib.parse

ADMIN_HEADERS = {"X-Actor-Role": "admin",
                 "X-Actor-Name": urllib.parse.quote("管理员")}
FILER_HEADERS = {"X-Actor-Role": "filer",
                 "X-Actor-Name": urllib.parse.quote("填报员甲")}
AUDITOR_HEADERS = {"X-Actor-Role": "auditor",
                   "X-Actor-Name": urllib.parse.quote("审计员")}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd, cls.api = serve("127.0.0.1", 0, None)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def setUp(self) -> None:
        # 每个测试换一个全新的内存存储
        from indicator_registry.api import Api
        from indicator_registry.repository import Repository
        from indicator_registry.services import ApplicationService
        self.api.repo = Repository(self.api.store.__class__())
        self.api.svc = ApplicationService(self.api.repo)

    def _req(self, method: str, path: str, body: dict | None = None,
             headers: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read().decode("utf-8"))

    def _seed(self) -> None:
        self._req("POST", "/schools",
                  {"code": "SK1", "name": "技能职院", "school_type": "技能型"}, ADMIN_HEADERS)
        self._req("POST", "/metrics",
                  {"code": "SK-PRAC", "name": "实训达标率", "school_type": "技能型",
                   "source": "教务系统"}, ADMIN_HEADERS)
        self._req("POST", "/metrics",
                  {"code": "SK-CERT", "name": "证书获取率", "school_type": "技能型",
                   "source": "人社接口"}, ADMIN_HEADERS)
        code, body = self._req("POST", "/calibers", {
            "school_type": "技能型", "year": 2026,
            "weights": {"SK-PRAC": 0.6, "SK-CERT": 0.4},
            "window": {"start_year": 2026, "end_year": 2030},
            "required_signers": 2}, ADMIN_HEADERS)
        self.cid = body["caliber_id"]
        self._req("POST", f"/calibers/{self.cid}/cosign/start", {}, ADMIN_HEADERS)
        for s in ("张三", "李四"):
            self._req("POST", f"/calibers/{self.cid}/cosign/sign",
                      {"signer": s},
                      {**ADMIN_HEADERS, "X-Actor-Name": urllib.parse.quote(s)})
        self._req("POST", f"/calibers/{self.cid}/publish", {}, ADMIN_HEADERS)

    def test_full_flow_over_http(self) -> None:
        self._seed()
        # 填报员提交数据
        code, _ = self._req("POST", "/submissions",
                            {"year": 2026, "school_code": "SK1", "metric_code": "SK-PRAC",
                             "value": 90, "evidence_ref": "e1"},
                            {**FILER_HEADERS, "X-Idempotency-Key": "k1"})
        self.assertEqual(code, 201)
        # 同一幂等键重放：不产生第二条事件
        self._req("POST", "/submissions",
                  {"year": 2026, "school_code": "SK1", "metric_code": "SK-PRAC",
                   "value": 90, "evidence_ref": "e1"},
                  {**FILER_HEADERS, "X-Idempotency-Key": "k1"})
        self._req("POST", "/submissions",
                  {"year": 2026, "school_code": "SK1", "metric_code": "SK-CERT",
                   "value": 80, "evidence_ref": "e2"},
                  {**FILER_HEADERS, "X-Idempotency-Key": "k2"})
        # 无证据提交：标记缺失
        code, body = self._req("POST", "/submissions",
                               {"year": 2026, "school_code": "SK1", "metric_code": "SK-CERT",
                                "value": None, "evidence_ref": ""},
                               {**FILER_HEADERS, "X-Idempotency-Key": "k3"})
        self.assertEqual(body["status"], "缺失证据")

        code, sealed = self._req("POST", f"/calibers/{self.cid}/seal", {}, ADMIN_HEADERS)
        self.assertEqual(code, 201)
        pid = sealed["publication_id"]

        # 审计：复算与哈希链
        code, rec = self._req(
            "GET", "/audit/recompute?year=2026&school_type=" + urllib.parse.quote("技能型"),
            headers=AUDITOR_HEADERS)
        self.assertEqual(code, 200)
        self.assertTrue(all(r["scores_match"] and r["manifest_match"] for r in rec))
        code, chain = self._req("GET", "/audit/verify", headers=AUDITOR_HEADERS)
        self.assertTrue(chain["intact"])

        # 已封存后重复/并发发布被拒
        code, blocked = self._req("POST", f"/calibers/{self.cid}/seal", {},
                                  {**ADMIN_HEADERS, "X-Txn": "txn-dup"})
        self.assertEqual(code, 409)
        self.assertEqual(blocked["error"], "concurrent_publish")

        # 审计可见并发拦截
        code, changes = self._req(
            "GET", "/audit/changes?year=2026&school_type=" + urllib.parse.quote("技能型"),
            headers=AUDITOR_HEADERS)
        self.assertEqual(len(changes["concurrent_publish_blocks"]), 1)

        # 发布详情：旧版始终可查
        code, detail = self._req("GET", f"/publications/{pid}")
        self.assertEqual(code, 200)
        self.assertEqual(detail["status"], "已封存")

    def test_authorization_enforced(self) -> None:
        self._seed()
        code, body = self._req("POST", "/schools",
                               {"code": "X", "name": "x", "school_type": "技能型"}, FILER_HEADERS)
        self.assertEqual(code, 403)
        code, body = self._req(
            "GET", "/audit/verify", headers=FILER_HEADERS)
        self.assertEqual(code, 403)


class ConcurrentSealRaceTest(unittest.TestCase):
    def test_two_threads_only_one_seal_wins(self) -> None:
        from indicator_registry.api import Api
        from indicator_registry import (
            ApplicationService, EventStore, Repository,
        )
        api = Api(None)
        svc = api.svc
        repo = svc.repo
        repo.register_school("管理员", "SK1", "技能职院", SchoolType.SKILL)
        repo.register_metric("管理员", "SK-PRAC", "实训", SchoolType.SKILL,
                             MetricKind.QUANTITATIVE, source="系统")
        repo.register_metric("管理员", "SK-CERT", "证书", SchoolType.SKILL,
                             MetricKind.QUANTITATIVE, source="部门")
        c = repo.create_caliber("管理员", SchoolType.SKILL, 2026,
                                {"SK-PRAC": 0.5, "SK-CERT": 0.5},
                                ValidityWindow(2026, 2030), required_signers=2)
        repo.start_cosign("管理员", c["id"])
        repo.sign_caliber("张三", c["id"], "张三")
        repo.sign_caliber("李四", c["id"], "李四")
        repo.publish_caliber("管理员", c["id"])
        repo.submit_data("填报员", 2026, "SK1", "SK-PRAC", 90, "e1", event_id="a")
        repo.submit_data("填报员", 2026, "SK1", "SK-CERT", 80, "e2", event_id="b")

        results: list[object] = []

        def seal(txn: str) -> None:
            try:
                results.append(("ok", svc.seal_publication(
                    "评价管理人员", "管理员", c["id"], txn=txn)["id"]))
            except Exception as ex:  # noqa: BLE001
                results.append(("err", type(ex).__name__))

        t1 = threading.Thread(target=seal, args=("txn-1",))
        t2 = threading.Thread(target=seal, args=("txn-2",))
        t1.start(); t2.start(); t1.join(); t2.join()

        winners = [r for r in results if r[0] == "ok"]
        losers = [r for r in results if r[0] == "err"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0][1], "ConcurrentPublish")


if __name__ == "__main__":
    unittest.main()
