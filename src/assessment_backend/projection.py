"""事件投影：从日志重建领域状态、计算得分、支持任一时点复算。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .events import Event
from .hashing import canonical, digest
from .models import (
    AssessmentRecord,
    CaliberVersion,
    MetricDef,
    MetricValue,
    Signing,
    University,
)

# ---- 事件类型 ----
EV_UNIVERSITY_REGISTERED = "UniversityRegistered"
EV_CALIBER_CREATED = "CaliberCreated"
EV_METRIC_DEFINED = "MetricDefined"
EV_METRIC_ADJUSTED = "MetricAdjusted"
EV_CALIBER_SUBMITTED = "CaliberSubmitted"
EV_CALIBER_SIGNED = "CaliberSigned"
EV_SIGNATURE_REVOKED = "SignatureRevoked"
EV_CALIBER_EFFECTIVATED = "CaliberEffectivated"
EV_CALIBER_SEALED = "CaliberSealed"
EV_RECORD_OPENED = "AssessmentRecordOpened"
EV_VALUE_RECORDED = "MetricValueRecorded"
EV_RECORD_PUBLISHED = "RecordPublished"
EV_RECORD_CORRECTION_OPENED = "RecordCorrectionOpened"


@dataclass
class ScoreLine:
    metric_code: str
    metric_name: str
    dimension: str
    weight: float
    raw_value: Optional[float]
    scored: bool
    reason: str
    contribution: float


@dataclass
class ScoreResult:
    record_id: str
    caliber_id: str
    year: int
    lines: list[ScoreLine]
    total: float
    missing_codes: list[str]
    evidence_gaps: list[str]
    caliber_digest: str
    input_digest: str

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "caliber_id": self.caliber_id,
            "year": self.year,
            "total": self.total,
            "missing_codes": self.missing_codes,
            "evidence_gaps": self.evidence_gaps,
            "caliber_digest": self.caliber_digest,
            "input_digest": self.input_digest,
            "lines": [
                {
                    "metric_code": ln.metric_code,
                    "metric_name": ln.metric_name,
                    "dimension": ln.dimension,
                    "weight": ln.weight,
                    "raw_value": ln.raw_value,
                    "scored": ln.scored,
                    "reason": ln.reason,
                    "contribution": ln.contribution,
                }
                for ln in self.lines
            ],
        }


class Projection:
    """把事件流折叠为当前（或截至某序号的）领域状态。"""

    def __init__(self) -> None:
        self.universities: dict[str, University] = {}
        self.calibers: dict[str, CaliberVersion] = {}
        self.records: dict[str, AssessmentRecord] = {}
        # 每个办学类型+年度的口径版本序列（含草案与更正）
        self.type_year_calibers: dict[tuple[str, int], list[str]] = {}
        self.last_seq = 0

    def apply(self, event: Event) -> None:
        p = event.payload
        self.last_seq = event.seq
        t = event.type
        if t == EV_UNIVERSITY_REGISTERED:
            self.universities[p["university_id"]] = University(
                id=p["university_id"], name=p["name"], university_type=p["university_type"]
            )
        elif t == EV_CALIBER_CREATED:
            cid = p["caliber_id"]
            self.calibers[cid] = CaliberVersion(
                id=cid,
                university_type=p["university_type"],
                year=p["year"],
                sequence=p["sequence"],
                created_by=event.actor_id,
                created_at=event.at,
                supersedes=p.get("supersedes"),
                note=p.get("note", ""),
            )
            self.type_year_calibers.setdefault((p["university_type"], p["year"]), []).append(cid)
        elif t == EV_METRIC_DEFINED:
            cal = self.calibers[p["caliber_id"]]
            cal.metrics[p["code"]] = MetricDef(
                code=p["code"],
                name=p["name"],
                dimension=p["dimension"],
                source=p["source"],
                weight=float(p["weight"]),
            )
        elif t == EV_METRIC_ADJUSTED:
            # 仅草案期可调整；封存口径永不产生此事件
            cal = self.calibers[p["caliber_id"]]
            m = cal.metrics[p["code"]]
            m.name = p["name"]
            m.dimension = p["dimension"]
            m.source = p["source"]
            m.weight = float(p["weight"])
        elif t == EV_CALIBER_SUBMITTED:
            cal = self.calibers[p["caliber_id"]]
            cal.state = "会签"
            cal.submitted_at = event.at
        elif t == EV_CALIBER_SIGNED:
            cal = self.calibers[p["caliber_id"]]
            cal.signings.append(
                Signing(signer_id=p["signer_id"], signer_role=p["signer_role"], at=event.at)
            )
        elif t == EV_SIGNATURE_REVOKED:
            cal = self.calibers[p["caliber_id"]]
            for s in cal.signings:
                if s.signer_id == p["signer_id"] and s.active:
                    s.revoked = True
                    s.revoked_at = event.at
                    break
        elif t == EV_CALIBER_EFFECTIVATED:
            cal = self.calibers[p["caliber_id"]]
            cal.state = "生效"
            cal.effective_at = event.at
        elif t == EV_CALIBER_SEALED:
            cal = self.calibers[p["caliber_id"]]
            cal.state = "封存"
            cal.sealed_at = event.at
        elif t == EV_RECORD_OPENED:
            rid = p["record_id"]
            self.records[rid] = AssessmentRecord(
                id=rid,
                year=p["year"],
                university_id=p["university_id"],
                caliber_id=p["caliber_id"],
                created_by=event.actor_id,
                created_at=event.at,
            )
        elif t == EV_VALUE_RECORDED:
            rec = self.records[p["record_id"]]
            existed = p["code"] in rec.values
            rec.values[p["code"]] = MetricValue(
                metric_code=p["code"],
                value=p.get("value"),
                evidence_ref=p.get("evidence_ref"),
                reason=p.get("reason", ""),
                note=p.get("note", ""),
                recorded_by=event.actor_id,
                recorded_at=event.at,
                corrected=existed,
            )
        elif t == EV_RECORD_PUBLISHED:
            rec = self.records[p["record_id"]]
            rec.published = True
            rec.published_at = event.at
            rec.published_by = event.actor_id
            rec.publish_seq = event.seq
        elif t == EV_RECORD_CORRECTION_OPENED:
            old = self.records[p["old_record_id"]]
            old.superseded_by_record = p["new_record_id"]
            new = AssessmentRecord(
                id=p["new_record_id"],
                year=old.year,
                university_id=old.university_id,
                caliber_id=p["new_caliber_id"],
                created_by=event.actor_id,
                created_at=event.at,
            )
            self.records[p["new_record_id"]] = new

    @classmethod
    def rebuild(cls, events: list[Event], as_of_seq: Optional[int] = None) -> "Projection":
        proj = cls()
        for event in events:
            if as_of_seq is not None and event.seq > as_of_seq:
                break
            proj.apply(event)
        return proj


# ---- 计分（纯函数：封存口径 + 当时数据 → 确定结果） ----


def caliber_fingerprint(cal: CaliberVersion) -> str:
    """封存口径指纹：指标、权重、数据来源与生效区间的内容哈希。"""
    body = {
        "university_type": cal.university_type,
        "year": cal.year,
        "sequence": cal.sequence,
        "metrics": [
            {
                "code": m.code,
                "name": m.name,
                "dimension": m.dimension,
                "source": m.source,
                "weight": m.weight,
            }
            for code, m in sorted(cal.metrics.items())
        ],
        "effective_at": cal.effective_at,
        "sealed_at": cal.sealed_at,
    }
    return digest(body)


def score_record(cal: CaliberVersion, record: AssessmentRecord) -> ScoreResult:
    """按口径权重对记录计分。

    - 有值且有证据：贡献 = 权重 × 标准化取值（取值约定在 0~100）。
    - 无数据 / 有值无证据：该指标计 0，并分别登记到 missing_codes / evidence_gaps，
      随结果一并封存，缺失证据不会被静默吞掉。
    """
    lines: list[ScoreLine] = []
    missing_codes: list[str] = []
    evidence_gaps: list[str] = []
    total = 0.0
    for code in sorted(cal.metrics):
        metric = cal.metrics[code]
        mv = record.values.get(code)
        if mv is None or mv.value is None:
            reason = (mv.reason if mv is not None and mv.reason else "NO_DATA")
            lines.append(
                ScoreLine(code, metric.name, metric.dimension, metric.weight, None, False, reason, 0.0)
            )
            missing_codes.append(code)
        elif not mv.evidence_ref:
            lines.append(
                ScoreLine(
                    code, metric.name, metric.dimension, metric.weight, mv.value, False, "NO_EVIDENCE", 0.0
                )
            )
            evidence_gaps.append(code)
        else:
            contribution = round(metric.weight * float(mv.value), 9)
            total += contribution
            lines.append(
                ScoreLine(
                    code, metric.name, metric.dimension, metric.weight, mv.value, True, "", contribution
                )
            )
    input_body = [
        {
            "code": ln.metric_code,
            "raw_value": ln.raw_value,
            "scored": ln.scored,
            "reason": ln.reason,
        }
        for ln in lines
    ]
    return ScoreResult(
        record_id=record.id,
        caliber_id=cal.id,
        year=record.year,
        lines=lines,
        total=round(total, 9),
        missing_codes=missing_codes,
        evidence_gaps=evidence_gaps,
        caliber_digest=caliber_fingerprint(cal),
        input_digest=digest(input_body),
    )
