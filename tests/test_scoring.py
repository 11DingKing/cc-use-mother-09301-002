"""计分、缺失数据与缺失证据留痕。"""
from __future__ import annotations

from _base import BackendTestCase, YEAR

from assessment_backend import WorkflowError
from assessment_backend.projection import Projection



class ScoringTest(BackendTestCase):
    def test_missing_value_scores_zero_and_is_recorded(self) -> None:
        self.seal_skill_caliber()
        self.svc.open_record(self.manager, "R1", "U1", YEAR)
        self.svc.record_value(self.filer, "R1", "SK1", 88.0, "EV-SK1")
        self.svc.record_value(self.filer, "R1", "SV1", None, note="合同未归档")
        self.svc.record_value(self.filer, "R1", "RS1", 70.0, "EV-RS1")
        score = self.svc.get_score("R1")
        self.assertEqual(score.missing_codes, ["SV1"])
        self.assertEqual(score.total, round(0.45 * 88 + 0.25 * 70, 9))

    def test_value_without_evidence_scores_zero_and_flagged(self) -> None:
        self.seal_skill_caliber()
        self.svc.open_record(self.manager, "R1", "U1", YEAR)
        self.svc.record_value(self.filer, "R1", "SK1", 88.0, "EV-SK1")
        self.svc.record_value(self.filer, "R1", "SV1", 90.0, "EV-SV1")
        self.svc.record_value(self.filer, "R1", "RS1", 70.0, None, note="凭证待补")
        score = self.svc.get_score("R1")
        self.assertEqual(score.evidence_gaps, ["RS1"])
        self.assertEqual(score.total, round(0.45 * 88 + 0.30 * 90, 9))

    def test_record_must_bind_sealed_caliber(self) -> None:
        self.svc.register_university(self.manager, "U1", "职院", "技能型")
        with self.assertRaises(WorkflowError):
            self.svc.open_record(self.manager, "R1", "U1", YEAR)

    def test_value_must_belong_to_caliber(self) -> None:
        self.seal_skill_caliber()
        self.svc.open_record(self.manager, "R1", "U1", YEAR)
        from assessment_backend import ValidationError
        with self.assertRaises(ValidationError):
            self.svc.record_value(self.filer, "R1", "NOPE", 1.0, "E")

    def test_publish_requires_all_metrics_filed(self) -> None:
        self.seal_skill_caliber()
        self.svc.open_record(self.manager, "R1", "U1", YEAR)
        self.svc.record_value(self.filer, "R1", "SK1", 88.0, "EV-SK1")
        from assessment_backend import ValidationError
        with self.assertRaises(ValidationError):
            self.svc.publish_record(self.manager, "R1")

    def test_full_score_and_snapshot(self) -> None:
        self.seal_skill_caliber()
        self.svc.open_record(self.manager, "R1", "U1", YEAR)
        self.svc.record_value(self.filer, "R1", "SK1", 100.0, "E1")
        self.svc.record_value(self.filer, "R1", "SV1", 100.0, "E2")
        self.svc.record_value(self.filer, "R1", "RS1", 100.0, "E3")
        self.svc.publish_record(self.manager, "R1")
        score = self.svc.get_score("R1")
        self.assertEqual(score.total, 100.0)
        self.assertEqual(score.missing_codes, [])
        self.assertEqual(score.evidence_gaps, [])
        proj = Projection.rebuild(self.store.all_events())
        self.assertTrue(proj.records["R1"].published)
