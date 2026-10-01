"""后端领域测试的公共夹具。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from assessment_backend import (
    Actor,
    AssessmentService,
    AuditService,
    DIMENSION_RESEARCH,
    DIMENSION_SERVICE,
    DIMENSION_SKILL,
    EventStore,
    FixedRuntime,
    ROLE_AUDITOR,
    ROLE_FILER,
    ROLE_MANAGER,
)

YEAR = 2025


class BackendTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = FixedRuntime(start="2025-11-01T09:00:00+00:00", step_seconds=37)
        self.store = EventStore(self.runtime)
        self.svc = AssessmentService(self.store, runtime=self.runtime, quorum=2)
        self.audit = AuditService(self.store)
        self.manager = Actor("赵管理", ROLE_MANAGER)
        self.manager_b = Actor("孙评审", ROLE_MANAGER)
        self.auditor = Actor("钱审计", ROLE_AUDITOR)
        self.filer = Actor("李填报", ROLE_FILER)
        self.seq = lambda: len(self.store.all_events())

    def seal_skill_caliber(self, year: int = YEAR) -> str:
        """建立并封存一套技能型口径，返回口径 ID。"""
        self.svc.register_university(self.manager, "U1", "江北职业技术学院", "技能型")
        self.svc.create_caliber(self.manager, "技能型", year)
        from assessment_backend.projection import Projection
        cal = [c for c in Projection.rebuild(self.store.all_events()).calibers.values()
               if c.university_type == "技能型" and c.year == year][0]
        cid = cal.id
        self.svc.define_metric(self.manager, cid, "SK1", "高级工培养", DIMENSION_SKILL, "人社厅", 0.45)
        self.svc.define_metric(self.manager, cid, "SV1", "横向服务", DIMENSION_SERVICE, "学校台账", 0.30)
        self.svc.define_metric(self.manager, cid, "RS1", "应用研究", DIMENSION_RESEARCH, "科技厅", 0.25)
        self.svc.submit_for_signing(self.manager, cid)
        self.svc.sign_caliber(self.auditor, cid)
        self.svc.sign_caliber(self.manager_b, cid)
        self.svc.effectivate_caliber(self.manager, cid)
        self.svc.seal_caliber(self.manager, cid)
        return cid

    def fill_and_publish_r1(self, sk=88.0, sv=None, rs=70.0, rs_evidence=None) -> None:
        self.svc.open_record(self.manager, "R1", "U1", YEAR)
        self.svc.record_value(self.filer, "R1", "SK1", sk, "EV-SK1")
        if sv is None:
            self.svc.record_value(self.filer, "R1", "SV1", None, note="缺失")
        else:
            self.svc.record_value(self.filer, "R1", "SV1", sv, "EV-SV1")
        self.svc.record_value(self.filer, "R1", "RS1", rs, rs_evidence, note="")
        self.svc.publish_record(self.manager, "R1")
