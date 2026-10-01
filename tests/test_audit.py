"""审计复算：任一年度、任一时点、封存核对与变更窗口归因、日志持久化重建。"""
from __future__ import annotations

from _base import BackendTestCase, YEAR

import tempfile
from pathlib import Path

from assessment_backend import ConcurrencyError
from assessment_backend.projection import Projection



class AuditReplayTest(BackendTestCase):
    def _published(self) -> None:
        self.seal_skill_caliber()
        self.fill_and_publish_r1(sv=90.0, rs=70.0, rs_evidence="E3")

    def test_replay_year_matches_sealed_snapshot(self) -> None:
        self._published()
        report = self.audit.replay_year(YEAR)
        (row,) = report.records
        self.assertTrue(row.match)
        self.assertEqual(row.sealed_total, row.recomputed_total)
        verification = self.audit.verify_record("R1")
        self.assertTrue(verification["verified"])

    def test_replay_as_of_seq_is_stable(self) -> None:
        self.seal_skill_caliber()
        self.svc.open_record(self.manager, "R1", "U1", YEAR)
        seq_mid = self.seq()
        self.svc.record_value(self.filer, "R1", "SK1", 88.0, "E1")
        self.svc.record_value(self.filer, "R1", "SV1", 90.0, "E2")
        self.svc.record_value(self.filer, "R1", "RS1", 70.0, "E3")
        # 截至填报前：记录存在但无值
        mid = self.audit.replay_year(YEAR, as_of_seq=seq_mid)
        (row,) = mid.records
        self.assertEqual(row.recomputed_total, 0.0)
        self.assertEqual(row.missing_codes, ["RS1", "SK1", "SV1"])

    def test_explain_window_covers_all_three_changes(self) -> None:
        self.seal_skill_caliber()
        self.svc.open_record(self.manager, "R1", "U1", YEAR)
        self.svc.record_value(self.filer, "R1", "SK1", 88.0, "E1")
        self.svc.record_value(self.filer, "R1", "SV1", 80.0, "E2")
        self.svc.record_value(self.filer, "R1", "RS1", 70.0, "E3")
        start = self.seq()
        # 迟到数据
        self.svc.record_value(self.filer, "R1", "SV1", 100.0, "E2b", note="迟到")
        # 撤销签署发生在另一个新口径上
        self.svc.create_caliber(self.manager, "技能型", YEAR + 1)
        from assessment_backend.projection import EV_CALIBER_SUBMITTED
        cid2 = [c for c in Projection.rebuild(self.store.all_events()).calibers.values()
                if c.year == YEAR + 1][0].id
        from assessment_backend import DIMENSION_RESEARCH, DIMENSION_SERVICE, DIMENSION_SKILL
        self.svc.define_metric(self.manager, cid2, "SK1", "a", DIMENSION_SKILL, "人社厅", 0.45)
        self.svc.define_metric(self.manager, cid2, "SV1", "b", DIMENSION_SERVICE, "台账", 0.30)
        self.svc.define_metric(self.manager, cid2, "RS1", "c", DIMENSION_RESEARCH, "科技厅", 0.25)
        self.svc.submit_for_signing(self.manager, cid2)
        self.svc.sign_caliber(self.auditor, cid2)
        self.svc.sign_caliber(self.manager_b, cid2)
        self.svc.revoke_signature(self.manager_b, cid2, "孙评审")
        # 并发发布：一胜一败
        base = self.seq()
        self.svc.publish_record(self.manager, "R1", expected_seq=base)
        try:
            self.svc.publish_record(self.manager_b, "R1", expected_seq=base)
        except ConcurrencyError:
            pass
        end = self.seq()

        window = self.audit.explain_window(start, end)
        self.assertEqual(len(window["late_data"]), 1)
        self.assertEqual(window["late_data"][0]["old_value"], 80.0)
        self.assertEqual(window["late_data"][0]["new_value"], 100.0)
        self.assertEqual(len(window["revoked_signatures"]), 1)
        rev = window["revoked_signatures"][0]
        self.assertEqual(rev["signer_id"], "孙评审")
        self.assertNotIn("孙评审", rev["active_signers_after"])
        self.assertEqual(len(window["publishes"]), 1)
        self.assertEqual(window["publishes"][0]["published_by"], "赵管理")

    def test_jsonl_roundtrip_reproduces_state(self) -> None:
        self._published()
        expected = self.audit.replay_year(YEAR).to_dict()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.store.save_jsonl(path)

            from assessment_backend import EventStore, AuditService
            rebuilt_store = EventStore()
            rebuilt_store.load_jsonl(path)
            rebuilt = AuditService(rebuilt_store).replay_year(YEAR).to_dict()
            self.assertEqual(expected, rebuilt)
            self.assertEqual(len(rebuilt_store.all_events()), len(self.store.all_events()))

    def test_ranking_excludes_superseded_records(self) -> None:
        self._published()
        report = self.audit.replay_year(YEAR)
        self.assertEqual([r["record_id"] for r in report.ranking_now], ["R1"])
