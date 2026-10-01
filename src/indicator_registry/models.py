"""领域值对象与实体（全部不可变/冻结）。"""
from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any


class SchoolType(str, enum.Enum):
    """办学类型，决定差异化指标集。"""

    SKILL = "技能型"
    LOCAL_SERVICE = "地方服务型"
    RESEARCH = "基础研究型"


class MetricKind(str, enum.Enum):
    QUANTITATIVE = "定量"
    QUALITATIVE = "定性"


class CaliberStatus(str, enum.Enum):
    DRAFT = "草案"
    IN_COSIGN = "会签"
    ACTIVE = "生效"
    SEALED = "封存"
    SUPERSEDED = "被替代"  # 生效中被更正版本取代后的终态


class SubmissionStatus(str, enum.Enum):
    DRAFT = "草稿"
    SUBMITTED = "已提交"
    EVIDENCE_MISSING = "缺失证据"
    INCLUDED = "已采信"
    LATE = "迟到"
    REJECTED = "驳回"


class PublicationStatus(str, enum.Enum):
    SEALED = "已封存"
    CORRECTED = "已更正"  # 原版本被更正后打上的标记，原记录永不删除


@dataclass(frozen=True)
class Metric:
    """指标定义。"""

    code: str
    name: str
    school_type: SchoolType
    kind: MetricKind
    unit: str = ""
    source: str = ""  # 数据来源（系统/部门/佐证材料类型）
    evidence_required: bool = True
    description: str = ""


@dataclass(frozen=True)
class WeightedMetric:
    """口径内的指标权重项。"""

    metric_code: str
    weight: float

    def __post_init__(self) -> None:
        if not 0 <= self.weight <= 1:
            raise ValueError("权重必须落在 [0,1]")


@dataclass(frozen=True)
class ValidityWindow:
    """生效区间，半开区间 [start_year, end_year)，end_year=None 表示至今。"""

    start_year: int
    end_year: int | None = None

    def covers(self, year: int) -> bool:
        return self.start_year <= year and (self.end_year is None or year < self.end_year)


@dataclass(frozen=True)
class ScoreEntry:
    school_code: str
    score: float
    rank: int


@dataclass(frozen=True)
class MissingEvidence:
    """计算时封存的缺失证据记录。"""

    school_code: str
    metric_code: str
    reason: str
    raw_value: Any = None


@dataclass(frozen=True)
class Event:
    """追加日志中的一条不可变事件。"""

    seq: int
    timestamp: str
    actor: str
    type: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    id: str = ""
    txn: str = ""  # 并发发布时的事务分组标识

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
