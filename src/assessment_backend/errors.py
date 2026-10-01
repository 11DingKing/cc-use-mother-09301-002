"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有领域错误的基类。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class ValidationError(DomainError):
    """命令参数不满足领域约束。"""


class WorkflowError(DomainError):
    """对象当前状态不允许该操作。"""


class ConcurrencyError(DomainError):
    """并发提交冲突（乐观锁失败）。"""


class PermissionError(DomainError):
    """当前角色无权执行该操作。"""
