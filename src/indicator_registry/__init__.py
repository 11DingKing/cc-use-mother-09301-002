"""差异化评价指标库后端。

事件溯源 + 哈希链存证：
- 指标/口径/权重/数据来源/生效区间的多版本管理；
- 新口径多人会签后方可生效；
- 计算时封存口径快照、分数、缺失证据与清单指纹；
- 已公布结果只能以更正版本处理，旧版永不删除；
- 迟到数据、撤销签署、并发发布各自独立留痕，审计可复算任一年度。
"""
from .audit import AuditService
from .errors import (
    CaliberOverlap,
    ChainIntegrityError,
    ConcurrentPublish,
    DomainError,
    NotFound,
    QuorumNotMet,
    ValidationFailed,
    WorkflowError,
)
from .models import (
    CaliberStatus,
    Metric,
    MetricKind,
    SchoolType,
    SubmissionStatus,
    ValidityWindow,
)
from .repository import Repository
from .services import (
    ApplicationService,
    AuthorizationError,
    ROLE_ADMIN,
    ROLE_AUDITOR,
    ROLE_FILER,
)
from .store import EventStore

__all__ = [
    "ApplicationService", "AuditService", "AuthorizationError",
    "Repository", "EventStore",
    "Metric", "MetricKind", "SchoolType", "ValidityWindow",
    "CaliberStatus", "SubmissionStatus",
    "DomainError", "NotFound", "ValidationFailed", "WorkflowError",
    "QuorumNotMet", "ConcurrentPublish", "CaliberOverlap", "ChainIntegrityError",
    "ROLE_ADMIN", "ROLE_FILER", "ROLE_AUDITOR",
]
