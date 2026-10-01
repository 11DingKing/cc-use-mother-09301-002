"""应用服务：在领域仓储之上施加角色边界（与领域契约的三类角色对齐）。"""
from __future__ import annotations

from .errors import DomainError, ValidationFailed
from .models import MetricKind, SchoolType, ValidityWindow
from .repository import Repository
from .audit import AuditService

ROLE_ADMIN = "评价管理人员"
ROLE_FILER = "高校填报员"
ROLE_AUDITOR = "审计人员"

# 命令 -> 允许的角色
_PERMISSIONS = {
    "register_school": {ROLE_ADMIN},
    "register_metric": {ROLE_ADMIN},
    "create_caliber": {ROLE_ADMIN},
    "start_cosign": {ROLE_ADMIN},
    "sign_caliber": {ROLE_ADMIN},          # 会签人须为考核办授权人员
    "revoke_sign": {ROLE_ADMIN},
    "publish_caliber": {ROLE_ADMIN},
    "submit_data": {ROLE_FILER, ROLE_ADMIN},
    "seal_publication": {ROLE_ADMIN},
    "correct_publication": {ROLE_ADMIN},
    # 审计人员只拥有只读/复算权限，由 AuditService 直接提供
}


class AuthorizationError(DomainError):
    status = 403
    code = "forbidden"


class ApplicationService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.audit = AuditService(repo)

    def _authorize(self, command: str, role: str) -> None:
        allowed = _PERMISSIONS.get(command, set())
        if role not in allowed:
            raise AuthorizationError(f"角色 {role} 无权执行 {command}（允许：{sorted(allowed)}）")

    # 每个命令统一以 actor_role 鉴权；审计动作不走此门。

    def register_school(self, actor_role: str, actor: str, **kw):
        self._authorize("register_school", actor_role)
        return self.repo.register_school(actor, **kw)

    def register_metric(self, actor_role: str, actor: str, **kw):
        self._authorize("register_metric", actor_role)
        return self.repo.register_metric(actor, **kw)

    def create_caliber(self, actor_role: str, actor: str, *, school_type: SchoolType,
                       year: int, weights: dict, window: tuple[int, int | None],
                       required_signers: int, event_id: str = ""):
        self._authorize("create_caliber", actor_role)
        return self.repo.create_caliber(
            actor, school_type, year, weights,
            ValidityWindow(window[0], window[1]), required_signers, event_id=event_id)

    def start_cosign(self, actor_role: str, actor: str, caliber_id: str):
        self._authorize("start_cosign", actor_role)
        return self.repo.start_cosign(actor, caliber_id)

    def sign_caliber(self, actor_role: str, actor: str, caliber_id: str, signer: str | None = None):
        self._authorize("sign_caliber", actor_role)
        return self.repo.sign_caliber(actor, caliber_id, signer or actor)

    def revoke_sign(self, actor_role: str, actor: str, caliber_id: str, signer: str, reason: str = ""):
        self._authorize("revoke_sign", actor_role)
        return self.repo.revoke_sign(actor, caliber_id, signer, reason)

    def publish_caliber(self, actor_role: str, actor: str, caliber_id: str,
                        supersedes: str | None = None):
        self._authorize("publish_caliber", actor_role)
        return self.repo.publish_caliber(actor, caliber_id, supersedes=supersedes)

    def submit_data(self, actor_role: str, actor: str, **kw):
        self._authorize("submit_data", actor_role)
        return self.repo.submit_data(actor, **kw)

    def seal_publication(self, actor_role: str, actor: str, caliber_id: str,
                         txn: str = "", timestamp: str | None = None):
        self._authorize("seal_publication", actor_role)
        return self.repo.seal_publication(actor, caliber_id, txn=txn, timestamp=timestamp)

    def correct_publication(self, actor_role: str, actor: str, *,
                            old_publication_id: str, new_caliber_id: str, reason: str,
                            txn: str = "", timestamp: str | None = None):
        self._authorize("correct_publication", actor_role)
        return self.repo.correct_publication(
            actor, old_publication_id, new_caliber_id, reason, txn=txn, timestamp=timestamp)

    # ----------------------------------------------------- 审计（只读）

    def audit_view(self, actor_role: str) -> AuditService:
        if actor_role != ROLE_AUDITOR:
            raise AuthorizationError("仅审计人员可复算与查看变更解释")
        return self.audit
