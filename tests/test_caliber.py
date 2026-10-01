"""口径版本：差异化指标、权重校验与封存冻结。"""
from __future__ import annotations

from _base import BackendTestCase, YEAR

from assessment_backend import (
    Actor,
    DIMENSION_RESEARCH,
    DIMENSION_SERVICE,
    DIMENSION_SKILL,
    ROLE_FILER,
    PermissionError,
    ValidationError,
    WorkflowError,
)
from assessment_backend.projection import Projection, caliber_fingerprint



def calibers(store):
    return list(Projection.rebuild(store.all_events()).calibers.values())


class CaliberTest(BackendTestCase):
    def test_differentiated_calibers_per_type(self) -> None:
        self.svc.register_university(self.manager, "U1", "职业技术学院", "技能型")
        self.svc.register_university(self.manager, "U2", "理工学院", "教学研究型")
        self.svc.create_caliber(self.manager, "技能型", YEAR)
        self.svc.create_caliber(self.manager, "教学研究型", YEAR)
        cs = calibers(self.store)
        self.assertEqual({c.university_type for c in cs}, {"技能型", "教学研究型"})
        self.assertTrue(all(c.sequence == 1 for c in cs))

    def test_define_metric_validation(self) -> None:
        self.svc.create_caliber(self.manager, "技能型", YEAR)
        cid = calibers(self.store)[0].id
        with self.assertRaises(ValidationError):
            self.svc.define_metric(self.manager, cid, "X", "x", "不存在的维度", "人社厅", 0.5)
        with self.assertRaises(ValidationError):
            self.svc.define_metric(self.manager, cid, "X", "x", DIMENSION_SKILL, "人社厅", 0)
        with self.assertRaises(ValidationError):
            self.svc.define_metric(self.manager, cid, "X", "x", DIMENSION_SKILL, "", 0.5)

    def test_duplicate_metric_rejected(self) -> None:
        self.svc.create_caliber(self.manager, "技能型", YEAR)
        cid = calibers(self.store)[0].id
        self.svc.define_metric(self.manager, cid, "SK1", "a", DIMENSION_SKILL, "人社厅", 0.5)
        with self.assertRaises(ValidationError):
            self.svc.define_metric(self.manager, cid, "SK1", "b", DIMENSION_SKILL, "人社厅", 0.5)

    def test_weight_sum_and_dimension_coverage(self) -> None:
        self.svc.create_caliber(self.manager, "技能型", YEAR)
        cid = calibers(self.store)[0].id
        self.svc.define_metric(self.manager, cid, "SK1", "a", DIMENSION_SKILL, "人社厅", 0.6)
        self.svc.define_metric(self.manager, cid, "SV1", "b", DIMENSION_SERVICE, "台账", 0.4)
        # 缺基础研究维度
        with self.assertRaises(ValidationError):
            self.svc.submit_for_signing(self.manager, cid)
        self.svc.define_metric(self.manager, cid, "RS1", "c", DIMENSION_RESEARCH, "科技厅", 0.2)
        # 权重和 1.2 != 1.0
        with self.assertRaises(ValidationError):
            self.svc.submit_for_signing(self.manager, cid)

    def test_adjust_metric_only_in_draft(self) -> None:
        cid = self.seal_skill_caliber()
        with self.assertRaises(WorkflowError):
            self.svc.adjust_metric(self.manager, cid, "SK1", "x", DIMENSION_SKILL, "人社厅", 0.4)
        with self.assertRaises(WorkflowError):
            self.svc.define_metric(self.manager, cid, "NEW", "x", DIMENSION_SKILL, "人社厅", 0.1)

    def test_filer_cannot_manage_caliber(self) -> None:
        with self.assertRaises(PermissionError):
            self.svc.create_caliber(Actor("李填报", ROLE_FILER), "技能型", YEAR)

    def test_sealed_fingerprint_stable(self) -> None:
        cid = self.seal_skill_caliber()
        proj = Projection.rebuild(self.store.all_events())
        digest1 = caliber_fingerprint(proj.calibers[cid])
        # 更多无关事件不改变封存口径指纹
        self.svc.register_university(self.manager, "U9", "另一个学校", "技能型")
        proj2 = Projection.rebuild(self.store.all_events())
        self.assertEqual(digest1, caliber_fingerprint(proj2.calibers[cid]))
