"""已公布结果的更正版本流程：原结果封存保留，更正版走新口径新会签。"""
from __future__ import annotations

from _base import BackendTestCase, YEAR

from assessment_backend import (
    DIMENSION_RESEARCH,
    DIMENSION_SERVICE,
    DIMENSION_SKILL,
    ValidationError,
    WorkflowError,
)
from assessment_backend.projection import Projection, caliber_fingerprint



def _calibers(store):
    return sorted(Projection.rebuild(store.all_events()).calibers.values(),
                  key=lambda c: c.sequence)


class CorrectionTest(BackendTestCase):
    def _seal_correction(self, old_cid: str) -> str:
        ev = self.svc.open_correction_caliber(self.manager, old_cid, note="调整权重")
        new_cid = ev.payload["caliber_id"]
        self.svc.adjust_metric(self.manager, new_cid, "SK1", "高级工培养",
                               DIMENSION_SKILL, "人社厅", 0.40)
        self.svc.adjust_metric(self.manager, new_cid, "RS1", "应用研究",
                               DIMENSION_RESEARCH, "科技厅", 0.30)
        self.svc.submit_for_signing(self.manager, new_cid)
        self.svc.sign_caliber(self.auditor, new_cid)
        self.svc.sign_caliber(self.manager_b, new_cid)
        self.svc.effectivate_caliber(self.manager, new_cid)
        self.svc.seal_caliber(self.manager, new_cid)
        return new_cid

    def test_correction_caliber_copies_and_keeps_history(self) -> None:
        old_cid = self.seal_skill_caliber()
        new_cid = self._seal_correction(old_cid)
        proj = Projection.rebuild(self.store.all_events())
        old, new = proj.calibers[old_cid], proj.calibers[new_cid]
        self.assertEqual(old.state, "封存")
        self.assertTrue(new.is_correction)
        self.assertEqual(new.supersedes, old_cid)
        self.assertEqual(new.sequence, 2)
        self.assertEqual(set(new.metrics), set(old.metrics))
        self.assertNotEqual(caliber_fingerprint(old), caliber_fingerprint(new))

    def test_correction_requires_sealed_caliber(self) -> None:
        self.svc.create_caliber(self.manager, "技能型", YEAR)
        cid = _calibers(self.store)[0].id
        with self.assertRaises(WorkflowError):
            self.svc.open_correction_caliber(self.manager, cid)

    def test_published_result_only_via_correction(self) -> None:
        old_cid = self.seal_skill_caliber()
        self.fill_and_publish_r1(sv=90.0, rs=70.0, rs_evidence="E3")
        with self.assertRaises(WorkflowError):
            self.svc.open_correction_record(self.manager, "R1", old_cid, "RX")
        new_cid = self._seal_correction(old_cid)
        self.svc.open_correction_record(self.manager, "R1", new_cid, "R2")

        proj = Projection.rebuild(self.store.all_events())
        self.assertEqual(proj.records["R1"].superseded_by_record, "R2")
        self.assertEqual(proj.records["R2"].caliber_id, new_cid)

    def test_correction_caliber_must_match_original(self) -> None:
        old_cid = self.seal_skill_caliber()
        self.fill_and_publish_r1(sv=90.0, rs=70.0, rs_evidence="E3")
        # 另立一个不相关的封存更正口径
        self.svc.register_university(self.manager, "U2", "理工学院", "研究型")
        self.svc.create_caliber(self.manager, "研究型", YEAR)
        other = [c for c in Projection.rebuild(self.store.all_events()).calibers.values()
                 if c.university_type == "研究型"][0].id
        self.svc.define_metric(self.manager, other, "SK1", "a", DIMENSION_SKILL, "人社厅", 0.45)
        self.svc.define_metric(self.manager, other, "SV1", "b", DIMENSION_SERVICE, "台账", 0.30)
        self.svc.define_metric(self.manager, other, "RS1", "c", DIMENSION_RESEARCH, "科技厅", 0.25)
        self.svc.submit_for_signing(self.manager, other)
        self.svc.sign_caliber(self.auditor, other)
        self.svc.sign_caliber(self.manager_b, other)
        self.svc.effectivate_caliber(self.manager, other)
        self.svc.seal_caliber(self.manager, other)
        # other 不是更正口径，拒绝
        with self.assertRaises(WorkflowError):
            self.svc.open_correction_record(self.manager, "R1", other, "R2")

    def test_double_correction_rejected(self) -> None:
        old_cid = self.seal_skill_caliber()
        self.fill_and_publish_r1(sv=90.0, rs=70.0, rs_evidence="E3")
        new_cid = self._seal_correction(old_cid)
        self.svc.open_correction_record(self.manager, "R1", new_cid, "R2")
        with self.assertRaises(WorkflowError):
            self.svc.open_correction_record(self.manager, "R1", new_cid, "R3")

    def test_correction_changes_ranking_but_keeps_history(self) -> None:
        old_cid = self.seal_skill_caliber()
        self.fill_and_publish_r1(sv=90.0, rs=70.0, rs_evidence="E3")
        old_total = self.svc.get_score("R1").total
        new_cid = self._seal_correction(old_cid)
        self.svc.open_correction_record(self.manager, "R1", new_cid, "R2")
        self.svc.record_value(self.filer, "R2", "SK1", 88.0, "E1")
        self.svc.record_value(self.filer, "R2", "SV1", 90.0, "E2")
        self.svc.record_value(self.filer, "R2", "RS1", 95.0, "E9")
        self.svc.publish_record(self.manager, "R2")

        # 现行榜单用 R2；历史 R1 仍可复算
        ranking = self.audit.replay_year(YEAR).ranking_now
        self.assertEqual([r["record_id"] for r in ranking], ["R2"])
        report = self.audit.replay_year(YEAR)
        rows = {r.record_id: r for r in report.records}
        self.assertTrue(rows["R1"].superseded_by == "R2")
        self.assertNotEqual(old_total, rows["R2"].recomputed_total)
