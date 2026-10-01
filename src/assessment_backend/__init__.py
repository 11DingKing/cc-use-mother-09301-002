"""差异化评价指标库后端：事件溯源的口径管理、结果封存、更正与审计复算。"""
from __future__ import annotations

from .actors import (
    ALL_ROLES,
    ROLE_AUDITOR,
    ROLE_FILER,
    ROLE_MANAGER,
)
from .audit import AuditService
from .errors import (
    ConcurrencyError,
    DomainError,
    NotFoundError,
    PermissionError,
    ValidationError,
    WorkflowError,
)
from .models import (
    DIMENSION_RESEARCH,
    DIMENSION_SERVICE,
    DIMENSION_SKILL,
)
from .runtime import FixedRuntime, SystemRuntime
from .service import Actor, AssessmentService
from .storage import EventStore

__all__ = [
    "ALL_ROLES",
    "DIMENSION_RESEARCH",
    "DIMENSION_SERVICE",
    "DIMENSION_SKILL",
    "ROLE_AUDITOR",
    "ROLE_FILER",
    "ROLE_MANAGER",
    "Actor",
    "AssessmentService",
    "AuditService",
    "ConcurrencyError",
    "DomainError",
    "EventStore",
    "FixedRuntime",
    "NotFoundError",
    "PermissionError",
    "SystemRuntime",
    "ValidationError",
    "WorkflowError",
]
