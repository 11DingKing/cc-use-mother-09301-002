"""端到端领域测试：差异化指标、会签、封存、迟到数据、撤销签署、并发发布与更正复算。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from indicator_registry import (
    ApplicationService,
    CaliberStatus,
    ConcurrentPublish,
    EventStore,
    MetricKind,
    NotFound,
    QuorumNotMet,
    Repository,
    ROLE_ADMIN,
    ROLE_AUDITOR,
    ROLE_FILER,
    SchoolType,
    SubmissionStatus,
    ValidationFailed,
    ValidityWindow,
    WorkflowError,
)
from indicator_registry.errors import ChainIntegrityError, CaliberOverlap
from indicator_registry.services import AuthorizationError


ADMIN = ROLE_ADMIN


def bootstrap(svc: ApplicationService) -> dict:
    """注册三类高校、每类两项指标，并返回指标编码。"""
    repo = svc.repo
    repo.register_school("管理员", "SK1", "技能职院", SchoolType.SKILL)
    repo.register_school("管理员", "SK2", "技工高专", SchoolType.SKILL)
    repo.register_school("管理员", "LC1", "地方学院", SchoolType.LOCAL_SERVICE)
    repo.register_school("管理员", "RD1", "研究大学", SchoolType.RESEARCH)

    codes = {}
    codes["skill_practice"] = repo.register_metric(
        "管理员", "SK-PRAC", "实训课时达标率", SchoolType.SKILL, MetricKind.QUANTITATIVE,
        unit="%", source="教务系统导出").code
    codes["skill_cert"] = repo.register_metric(
        "管理员", "SK-CERT", "职业资格证书获取率", SchoolType.SKILL, MetricKind.QUANTITATIVE,
        unit="%", source="人社部门数据接口").code
    repo.register_metric(
        "管理员", "LC-SERV", "地方服务项目数", SchoolType.LOCAL_SERVICE, MetricKind.QUANTITATIVE,
        unit="项", source="横向合同台账")
    repo.register_metric(
        "管理员", "LC-GOV", "决策咨询采纳数", SchoolType.LOCAL_SERVICE, MetricKind.QUANTITATIVE,
        unit="项", source="政府办公厅回执")
    repo.register_metric(
        "管理员", "RD-PAPER", "高水平论文", SchoolType.RESEARCH, MetricKind.QUANTITATIVE,
        unit="篇", source="文献数据库核验")
    repo.register_metric(
        "管理员", "RD-GRANT", "基础研究项目", SchoolType.RESEARCH, MetricKind.QUANTITATIVE,
        unit="项", source="基金委立项清单")
    return codes


def make_service(path: str | None = None) -> ApplicationService:
    return ApplicationService(Repository(EventStore(path)))


def publish_caliber(repo: Repository, cid: str, signers: list[str]) -> None:
    repo.start_cosign("管理员", cid)
    for s in signers:
        repo.sign_caliber(s, cid, s)
    repo.publish_caliber("管理员", cid)


class CaliberCosignTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        self.codes = bootstrap(self.svc)
        self.repo = self.svc.repo

    def test_weights_source_window_validation(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.repo.create_caliber(
                "管理员", SchoolType.SKILL, 2026,
                {"SK-PRAC": 0.6, "SK-CERT": 0.3},
                ValidityWindow(2026, 2030), required_signers=2)  # 权重和 != 1
        with self.assertRaises(ValidationFailed):
            self.repo.create_caliber(
                "管理员", SchoolType.SKILL, 2026,
                {"SK-PRAC": 0.5, "RD-GRANT": 0.5},  # 跨办学类型
                ValidityWindow(2026, 2030), required_signers=2)
        with self.assertRaises(ValidationFailed):
            self.repo.create_caliber(
                "管理员", SchoolType.SKILL, 2026,
                {"SK-PRAC": 0.5, "SK-CERT": 0.5},
                ValidityWindow(2027, 2030), required_signers=2)  # 区间不覆盖年度
        with self.assertRaises(ValidationFailed):
            self.repo.create_caliber(
                "管理员", SchoolType.SKILL, 2026,
                {"SK-PRAC": 0.5, "SK-CERT": 0.5},
                ValidityWindow(2026, 2030), required_signers=1)  # 必须多人会签

    def test_revocation_breaks_quorum(self) -> None:
        c = self.repo.create_caliber(
            "管理员", SchoolType.SKILL, 2026,
            {"SK-PRAC": 0.5, "SK-CERT": 0.5},
            ValidityWindow(2026, 2030), required_signers=3)
        cid = c["id"]
        self.repo.start_cosign("管理员", cid)
        self.repo.sign_caliber("张三", cid, "张三")
        self.repo.sign_caliber("李四", cid, "李四")
        with self.assertRaises(QuorumNotMet):
            self.repo.publish_caliber("管理员", cid)
        self.repo.sign_caliber("王五", cid, "王五")
        self.repo.revoke_sign("李四", cid, "李四", reason="对实训口径有异议")
        with self.assertRaises(QuorumNotMet):
            self.repo.publish_caliber("管理员", cid)
        self.repo.sign_caliber("李四", cid, "李四")  # 重新签署
        pub = self.repo.publish_caliber("管理员", cid)
        self.assertEqual(pub["status"], CaliberStatus.ACTIVE)
        self.assertEqual(pub["version"], 1)

    def test_overlapping_active_window_rejected(self) -> None:
        c1 = self.repo.create_caliber(
            "管理员", SchoolType.SKILL, 2026,
            {"SK-PRAC": 0.5, "SK-CERT": 0.5},
            ValidityWindow(2026, 2030), required_signers=2)
        publish_caliber(self.repo, c1["id"], ["张三", "李四"])
        c2 = self.repo.create_caliber(
            "管理员", SchoolType.SKILL, 2026,
            {"SK-PRAC": 0.7, "SK-CERT": 0.3},
            ValidityWindow(2026, 2031), required_signers=2)  # 与 [2026,2030) 重叠
        self.repo.start_cosign("管理员", c2["id"])
        self.repo.sign_caliber("张三", c2["id"], "张三")
        self.repo.sign_caliber("李四", c2["id"], "李四")
        with self.assertRaises(CaliberOverlap):
            self.repo.publish_caliber("管理员", c2["id"])

    def test_role_boundaries(self) -> None:
        c = self.repo.create_caliber(
            "管理员", SchoolType.SKILL, 2026,
            {"SK-PRAC": 0.5, "SK-CERT": 0.5},
            ValidityWindow(2026, 2030), required_signers=2)
        with self.assertRaises(AuthorizationError):
            self.svc.start_cosign(ROLE_FILER, "填报员甲", c["id"])
        with self.assertRaises(AuthorizationError):
            self.svc.seal_publication(ROLE_AUDITOR, "审计员", c["id"])
        self.svc.submit_data(
            ROLE_FILER, "填报员甲", year=2026, school_code="SK1",
            metric_code="SK-PRAC", value=90, evidence_ref="ev-1",
            event_id="idem-1")
        with self.assertRaises(AuthorizationError):
            self.svc.register_metric(ROLE_FILER, "填报员甲", code="X", name="x",
                                     school_type=SchoolType.SKILL,
                                     kind=MetricKind.QUANTITATIVE, source="s")


class SealingAndLateDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        bootstrap(self.svc)
        self.repo = self.svc.repo
        c = self.repo.create_caliber(
            "管理员", SchoolType.SKILL, 2026,
            {"SK-PRAC": 0.6, "SK-CERT": 0.4},
            ValidityWindow(2026, 2030), required_signers=2)
        self.cid = c["id"]
        publish_caliber(self.repo, self.cid, ["张三", "李四"])
        # 截止前数据：SK1 两项齐备，SK2 缺证书佐证
        self.repo.submit_data("填报员", 2026, "SK1", "SK-PRAC", 90.0, "ev-sk1-p", event_id="i1")
        self.repo.submit_data("填报员", 2026, "SK1", "SK-CERT", 80.0, "ev-sk1-c", event_id="i2")
        self.repo.submit_data("填报员", 2026, "SK2", "SK-PRAC", 70.0, "ev-sk2-p", event_id="i3")
        self.repo.submit_data("填报员", 2026, "SK2", "SK-CERT", None, "", event_id="i4")

    def test_seal_freezes_scores_and_missing_evidence(self) -> None:
        pub = self.repo.seal_publication("管理员", self.cid)
        scores = {r["school_code"]: r for r in pub["scores"]}
        # SK1: 0.6*90 + 0.4*80 = 86；SK2 缺证书，按可得权重归一：70
        self.assertAlmostEqual(scores["SK1"]["score"], 86.0, places=6)
        self.assertAlmostEqual(scores["SK2"]["score"], 70.0, places=6)
        self.assertEqual(scores["SK1"]["rank"], 1)
        missing = {(m["school_code"], m["metric_code"]) for m in pub["missing"]}
        self.assertIn(("SK2", "SK-CERT"), missing)

        audit = self.svc.audit_view(ROLE_AUDITOR)
        result = audit.recompute(pub["id"])
        self.assertTrue(result["scores_match"])
        self.assertTrue(result["missing_match"])
        self.assertTrue(result["manifest_match"])

    def test_late_submission_is_marked_and_not_mutating_sealed(self) -> None:
        pub = self.repo.seal_publication("管理员", self.cid, timestamp="2027-03-01T00:00:00+00:00")
        late = self.repo.submit_data(
            "填报员", 2026, "SK2", "SK-CERT", 95.0, "ev-sk2-c-late",
            event_id="i5", timestamp="2027-03-05T10:00:00+00:00")
        self.assertEqual(late["status"], SubmissionStatus.LATE)
        # 已封存结果不变
        again = self.repo.publications[pub["id"]]
        self.assertAlmostEqual(
            next(r for r in again["scores"] if r["school_code"] == "SK2")["score"], 70.0)

    def test_concurrent_publish_blocked_and_recorded(self) -> None:
        self.repo.seal_publication("管理员", self.cid)
        with self.assertRaises(ConcurrentPublish):
            self.repo.seal_publication("管理员", self.cid, txn="txn-B")
        blocked = [e for e in self.repo.store.events if e.type == "PUBLICATION_BLOCKED"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].txn, "txn-B")


class CorrectionWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        bootstrap(self.svc)
        self.repo = self.svc.repo
        c1 = self.repo.create_caliber(
            "管理员", SchoolType.SKILL, 2026,
            {"SK-PRAC": 0.6, "SK-CERT": 0.4},
            ValidityWindow(2026, 2030), required_signers=2)
        self.c1 = c1["id"]
        publish_caliber(self.repo, self.c1, ["张三", "李四"])
        self.repo.submit_data("填报员", 2026, "SK1", "SK-PRAC", 90.0, "ev-1", event_id="i1")
        self.repo.submit_data("填报员", 2026, "SK1", "SK-CERT", 80.0, "ev-2", event_id="i2")
        self.repo.submit_data("填报员", 2026, "SK2", "SK-PRAC", 70.0, "ev-3", event_id="i3")
        self.old = self.repo.seal_publication(
            "管理员", self.c1, timestamp="2027-03-01T00:00:00+00:00")
        # 迟到数据：SK2 补齐证书
        self.repo.submit_data("填报员", 2026, "SK2", "SK-CERT", 95.0, "ev-late",
                              event_id="i4", timestamp="2027-03-10T00:00:00+00:00")
        # 更正口径 v2：加大实训权重
        c2 = self.repo.create_caliber(
            "管理员", SchoolType.SKILL, 2026,
            {"SK-PRAC": 0.8, "SK-CERT": 0.2},
            ValidityWindow(2026, 2030), required_signers=2)
        self.c2 = c2["id"]

    def _activate_c2_with_revocation(self) -> None:
        self.repo.start_cosign("管理员", self.c2)
        self.repo.sign_caliber("张三", self.c2, "张三")
        self.repo.sign_caliber("王五", self.c2, "王五")
        # 王五在更正发布前撤销签署，改由赵六签署——撤销必须留痕
        self.repo.revoke_sign("王五", self.c2, "王五", reason="回避")
        self.repo.sign_caliber("赵六", self.c2, "赵六")
        self.repo.publish_caliber("管理员", self.c2, supersedes=self.c1)

    def test_correction_keeps_old_and_explains_all_three_changes(self) -> None:
        self._activate_c2_with_revocation()
        new_pub = self.repo.correct_publication(
            "管理员", self.old["id"], self.c2,
            "迟到佐证补齐 + 技能类提高实训权重", txn="txn-correct")

        self.assertEqual(self.repo.publications[self.old["id"]]["status"], "已更正")
        self.assertEqual(new_pub["status"], "已封存")
        self.assertEqual(new_pub["correction_of"], self.old["id"])
        self.assertEqual(self.repo.calibers[self.c1]["status"], CaliberStatus.SUPERSEDED)

        scores = {r["school_code"]: r for r in new_pub["scores"]}
        # 迟到数据已纳入：SK2 = 0.8*70 + 0.2*95 = 75
        self.assertAlmostEqual(scores["SK2"]["score"], 75.0, places=6)
        self.assertAlmostEqual(scores["SK1"]["score"], 88.0, places=6)

        audit = self.svc.audit_view(ROLE_AUDITOR)
        # 两个版本均可独立复算
        for r in audit.recompute_year(2026, SchoolType.SKILL):
            self.assertTrue(r["scores_match"])
            self.assertTrue(r["manifest_match"])

        explanation = audit.explain_changes(2026, SchoolType.SKILL)
        corr = explanation["corrections"][0]
        self.assertEqual(len(corr["late_data"]["items"]), 1)
        self.assertEqual(corr["late_data"]["items"][0]["school_code"], "SK2")
        # 撤销签署被记录
        self.assertIn("王五", [r["signer"] for r in corr["revoked_signatures"]])
        # 总差异：SK2 从 70 → 75
        sk2 = next(d for d in corr["overall_ranking_diff"] if d["school_code"] == "SK2")
        self.assertAlmostEqual(sk2["score_delta"], 5.0, places=6)
        # 迟到数据的独立贡献
        late_only = {d["school_code"]: d for d in corr["late_data"]["independent_score_effect"]}
        self.assertIn("SK2", late_only)

        history = audit.publication_history(2026, SchoolType.SKILL)
        self.assertEqual([h["version"] for h in history], [1, 2])

    def test_cannot_correct_with_same_or_inactive_caliber(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.repo.correct_publication("管理员", self.old["id"], self.c1, "同口径")
        self.repo.start_cosign("管理员", self.c2)
        self.repo.sign_caliber("张三", self.c2, "张三")
        self.repo.sign_caliber("李四", self.c2, "李四")
        with self.assertRaises(WorkflowError):
            self.repo.correct_publication("管理员", self.old["id"], self.c2, "未生效口径")


class PersistenceAndChainTest(unittest.TestCase):
    def test_jsonl_replay_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.jsonl")
            svc = make_service(path)
            bootstrap(svc)
            repo = svc.repo
            c = repo.create_caliber(
                "管理员", SchoolType.RESEARCH, 2026,
                {"RD-PAPER": 0.5, "RD-GRANT": 0.5},
                ValidityWindow(2026, 2031), required_signers=2)
            publish_caliber(repo, c["id"], ["张三", "李四"])
            repo.submit_data("填报员", 2026, "RD1", "RD-PAPER", 12.0, "e1", event_id="p1")
            repo.submit_data("填报员", 2026, "RD1", "RD-GRANT", 3.0, "e2", event_id="p2")
            pub = repo.seal_publication("管理员", c["id"])

            # 重新打开：事件流重放后状态一致、复算一致
            svc2 = make_service(path)
            audit = svc2.audit_view(ROLE_AUDITOR)
            r = audit.recompute(pub["id"])
            self.assertTrue(r["scores_match"])
            self.assertTrue(r["manifest_match"])
            chain = audit.verify_chain()
            self.assertTrue(chain["intact"])

            # 篡改日志：哈希链立即断裂
            lines = Path(path).read_text(encoding="utf-8").splitlines()
            import json
            evil = json.loads(lines[0])
            evil["payload"]["name"] = "被篡改的大学"
            lines[0] = json.dumps(evil, ensure_ascii=False)
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(ChainIntegrityError):
                make_service(path)


if __name__ == "__main__":
    unittest.main()
