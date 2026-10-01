"""角色与状态常量，与 domain/contract.json 保持一致。"""
from __future__ import annotations

# 领域契约中的三类参与者
ROLE_MANAGER = "评价管理人员"
ROLE_FILER = "高校填报员"
ROLE_AUDITOR = "审计人员"

ALL_ROLES = (ROLE_MANAGER, ROLE_FILER, ROLE_AUDITOR)

# 口径版本生命周期状态
STATE_DRAFT = "草案"
STATE_SIGNING = "会签"
STATE_EFFECTIVE = "生效"
STATE_SEALED = "封存"
STATE_CORRECTED = "更正"

# 缺失证据原因
MISSING_NO_DATA = "NO_DATA"
MISSING_NO_EVIDENCE = "NO_EVIDENCE"

# 默认会签人数（多人会签下限）
DEFAULT_QUORUM = 2
