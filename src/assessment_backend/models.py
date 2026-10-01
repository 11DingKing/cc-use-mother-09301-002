"""领域模型：高校、口径版本、签署记录、填报记录与指标取值。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .actors import (
    MISSING_NO_DATA,
    STATE_CORRECTED,
    STATE_DRAFT,
    STATE_EFFECTIVE,
    STATE_SEALED,
    STATE_SIGNING,
)

# 三大贡献维度（对应差异化评价的不同贡献）
DIMENSION_SKILL = "技能人才培养"
DIMENSION_SERVICE = "地方服务"
DIMENSION_RESEARCH = "基础研究"
ALL_DIMENSIONS = (DIMENSION_SKILL, DIMENSION_SERVICE, DIMENSION_RESEARCH)


@dataclass
class University:
    id: str
    name: str
    university_type: str  # 办学类型，如 技能型/教学研究型/研究型


@dataclass
class MetricDef:
    code: str
    name: str
    dimension: str
    source: str  # 数据来源，如 人社厅/科技厅/学校台账
    weight: float


@dataclass
class Signing:
    signer_id: str
    signer_role: str
    at: str
    revoked: bool = False
    revoked_at: Optional[str] = None

    @property
    def active(self) -> bool:
        return not self.revoked


@dataclass
class CaliberVersion:
    """某办学类型某年度的一套指标口径（指标 + 权重 + 数据来源）。"""

    id: str
    university_type: str
    year: int
    sequence: int
    state: str = STATE_DRAFT
    metrics: dict[str, MetricDef] = field(default_factory=dict)
    created_by: str = ""
    created_at: str = ""
    submitted_at: Optional[str] = None
    effective_at: Optional[str] = None
    sealed_at: Optional[str] = None
    signings: list[Signing] = field(default_factory=list)
    supersedes: Optional[str] = None  # 更正版本指向被更正的已封存版本
    note: str = ""

    @property
    def is_correction(self) -> bool:
        return self.supersedes is not None

    def active_signers(self) -> set[str]:
        return {s.signer_id for s in self.signings if s.active}

    def weight_sum(self) -> float:
        return round(sum(m.weight for m in self.metrics.values()), 9)


@dataclass
class MetricValue:
    metric_code: str
    value: Optional[float]
    evidence_ref: Optional[str]
    reason: str  # 有值时为 ""；缺失时为 NO_DATA / NO_EVIDENCE
    note: str
    recorded_by: str
    recorded_at: str
    corrected: bool = False  # 是否为迟到数据（曾被更新）


@dataclass
class AssessmentRecord:
    """某高校某年度的考核结果记录，绑定封存口径。"""

    id: str
    year: int
    university_id: str
    caliber_id: str
    values: dict[str, MetricValue] = field(default_factory=dict)
    published: bool = False
    published_at: Optional[str] = None
    published_by: Optional[str] = None
    publish_seq: Optional[int] = None  # 首次公布时的事件序号（复算锚点）
    superseded_by_record: Optional[str] = None  # 被更正结果替代
    created_by: str = ""
    created_at: str = ""

    def ordered_values(self) -> list[MetricValue]:
        return [self.values[code] for code in sorted(self.values)]
