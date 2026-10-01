"""迟到数据：公布前可更新并标记，公布后只能走更正。"""
from __future__ import annotations

from _base import BackendTestCase, YEAR

from assessment_backend import WorkflowError
from assessment_backend.projection import Projection



class LateDataTest(BackendTestCase):
    def test_late_value_before_publish_changes_total(self) -> None:
        self.seal_skill_caliber()
        self.svc.open_record(self.manager, "R1", "U1", YEAR)
        self.svc.record_value(self.filer, "R1", "SK1", 80.0, "E1")
        self.svc.record_value(self.filer, "R1", "SV1", None, note="缺")
        self.svc.record_value(self.filer, "R1", "RS1", 60.0, "E3")
        before = self.svc.get_score("R1").total

        seq0 = self.seq()
        self.svc.record_value(self.filer, "R1", "SV1", 90.0, "E2", note="迟到合同")
        after = self.svc.get_score("R1").total

        self.assertGreater(after, before)
        proj = Projection.rebuild(self.store.all_events())
        mv = proj.records["R1"].values["SV1"]
        self.assertTrue(mv.corrected)
        self.assertEqual(mv.value, 90.0)

        window = self.audit.explain_window(seq0, self.seq())
        self.assertEqual(len(window["late_data"]), 1)
        item = window["late_data"][0]
        self.assertEqual(item["total_after"] - item["total_before"], round(0.30 * 90, 9))

    def test_late_value_after_publish_rejected(self) -> None:
        self.seal_skill_caliber()
        self.fill_and_publish_r1(sv=90.0, rs=70.0, rs_evidence="E3")
        with self.assertRaises(WorkflowError):
            self.svc.record_value(self.filer, "R1", "RS1", 99.0, "E9")

    def test_published_record_stays_immutable_through_correction(self) -> None:
        self.seal_skill_caliber()
        self.fill_and_publish_r1(sv=90.0, rs=70.0, rs_evidence="E3")
        old_total = self.svc.get_score("R1").total

        cal = list(Projection.rebuild(self.store.all_events()).calibers.values())[0]
        new_cal = self.svc.open_correction_caliber(self.manager, cal.id)
        new_cid = new_cal.payload["caliber_id"]
        self.svc.submit_for_signing(self.manager, new_cid)
        self.svc.sign_caliber(self.auditor, new_cid)
        self.svc.sign_caliber(self.manager_b, new_cid)
        self.svc.effectivate_caliber(self.manager, new_cid)
        self.svc.seal_caliber(self.manager, new_cid)
        self.svc.open_correction_record(self.manager, "R1", new_cid, "R2")
        self.svc.record_value(self.filer, "R2", "RS1", 99.0, "E9")

        # 新记录的数据与公布不影响 R1
        self.assertEqual(self.svc.get_score("R1").total, old_total)
        proj = Projection.rebuild(self.store.all_events())
        self.assertEqual(proj.records["R1"].superseded_by_record, "R2")
