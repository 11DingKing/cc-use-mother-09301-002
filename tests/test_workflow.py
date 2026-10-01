"""多人会签与撤销签署工作流。"""
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
from assessment_backend.projection import Projection



class SigningWorkflowTest(BackendTestCase):
    def _draft(self) -> str:
        self.svc.create_caliber(self.manager, "技能型", YEAR)
        cid = [c for c in Projection.rebuild(self.store.all_events()).calibers.values()
               if c.university_type == "技能型"][0].id
        self.svc.define_metric(self.manager, cid, "SK1", "a", DIMENSION_SKILL, "人社厅", 0.45)
        self.svc.define_metric(self.manager, cid, "SV1", "b", DIMENSION_SERVICE, "台账", 0.30)
        self.svc.define_metric(self.manager, cid, "RS1", "c", DIMENSION_RESEARCH, "科技厅", 0.25)
        self.svc.submit_for_signing(self.manager, cid)
        return cid

    def test_filer_cannot_sign(self) -> None:
        cid = self._draft()
        with self.assertRaises(PermissionError):
            self.svc.sign_caliber(Actor("李填报", ROLE_FILER), cid)

    def test_duplicate_signer_rejected(self) -> None:
        cid = self._draft()
        self.svc.sign_caliber(self.auditor, cid)
        with self.assertRaises(ValidationError):
            self.svc.sign_caliber(self.auditor, cid)

    def test_quorum_blocks_effectivation(self) -> None:
        cid = self._draft()
        self.svc.sign_caliber(self.auditor, cid)
        with self.assertRaises(WorkflowError):
            self.svc.effectivate_caliber(self.manager, cid)

    def test_revoke_lowers_quorum_and_blocks(self) -> None:
        cid = self._draft()
        self.svc.sign_caliber(self.auditor, cid)
        self.svc.sign_caliber(self.manager_b, cid)
        self.svc.revoke_signature(self.auditor, cid, "钱审计")
        with self.assertRaises(WorkflowError):
            self.svc.effectivate_caliber(self.manager, cid)
        # 撤销后可重新签署
        self.svc.sign_caliber(self.auditor, cid)
        self.svc.effectivate_caliber(self.manager, cid)
        proj = Projection.rebuild(self.store.all_events())
        self.assertEqual(proj.calibers[cid].state, "生效")

    def test_revoke_only_by_self(self) -> None:
        cid = self._draft()
        self.svc.sign_caliber(self.auditor, cid)
        with self.assertRaises(PermissionError):
            self.svc.revoke_signature(self.manager_b, cid, "钱审计")

    def test_revoke_only_in_signing_phase(self) -> None:
        cid = self.seal_skill_caliber()
        with self.assertRaises(WorkflowError):
            self.svc.revoke_signature(self.auditor, cid, "钱审计")

    def test_sign_only_in_signing_state(self) -> None:
        self.svc.create_caliber(self.manager, "技能型", YEAR)
        cid = [c for c in Projection.rebuild(self.store.all_events()).calibers.values()][0].id
        with self.assertRaises(WorkflowError):
            self.svc.sign_caliber(self.auditor, cid)
