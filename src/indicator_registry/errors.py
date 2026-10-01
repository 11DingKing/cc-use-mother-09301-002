"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则违反的基类。"""

    status = 400
    code = "domain_error"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class ValidationFailed(DomainError):
    status = 422
    code = "validation_failed"


class WorkflowError(DomainError):
    status = 409
    code = "workflow_conflict"


class QuorumNotMet(DomainError):
    status = 422
    code = "quorum_not_met"


class ConcurrentPublish(WorkflowError):
    code = "concurrent_publish"


class CaliberOverlap(WorkflowError):
    code = "caliber_overlap"


class ChainIntegrityError(DomainError):
    status = 500
    code = "chain_integrity_broken"
