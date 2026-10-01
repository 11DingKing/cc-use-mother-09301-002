"""审计复算：任一年度可复算、封存快照可核对，变更可归因。

审计人员只读事件日志，从不写入。三类关键变更的归因：

1. 迟到数据：同一记录同一指标被再次填报（MetricValueRecorded 覆盖旧值），
   审计窗口逐值对比，给出总分前后差异；已公布记录不允许迟到数据，只能更正。
2. 撤销签署：SignatureRevoked，给出撤销前后有效签署人数，以及是否曾阻断生效。
3. 并发发布：乐观锁保证只有一个发布事件落日志；落败方什么都没改变，
   审计可据序号证明"胜出发布"是唯一改变结果的事件。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import projection as P
from .errors import NotFoundError
from .events import Event
from .storage import EventStore


@dataclass
class RecordAudit:
    record_id: str
    university_id: str
    year: int
    caliber_id: str
    published: bool
    publish_seq: Optional[int]
    sealed_total: Optional[float]
    recomputed_total: Optional[float]
    match: Optional[bool]
    missing_codes: list[str] = field(default_factory=list)
    evidence_gaps: list[str] = field(default_factory=list)
    superseded_by: Optional[str] = None


@dataclass
class YearAuditReport:
    year: int
    as_of_seq: int
    calibers: list[dict]
    records: list[RecordAudit]
    ranking_now: list[dict]

    def to_dict(self) -> dict:
        return {
            "year": self.year,
            "as_of_seq": self.as_of_seq,
            "calibers": self.calibers,
            "records": [r.__dict__ for r in self.records],
            "ranking_now": self.ranking_now,
        }


class AuditService:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    # ---- 年度复算 ----

    def replay_year(self, year: int, as_of_seq: Optional[int] = None) -> YearAuditReport:
        events = self.store.all_events()
        head = as_of_seq if as_of_seq is not None else len(events)
        proj = P.Projection.rebuild(events, as_of_seq=head)

        calibers_view = []
        for cal in proj.calibers.values():
            if cal.year != year:
                continue
            calibers_view.append({
                "caliber_id": cal.id,
                "university_type": cal.university_type,
                "sequence": cal.sequence,
                "state": cal.state,
                "metrics": sorted(cal.metrics),
                "weight_sum": cal.weight_sum(),
                "digest": P.caliber_fingerprint(cal),
                "active_signers": sorted(cal.active_signers()),
                "supersedes": cal.supersedes,
            })

        records_view: list[RecordAudit] = []
        for rec in proj.records.values():
            if rec.year != year:
                continue
            cal = proj.calibers[rec.caliber_id]
            score = P.score_record(cal, rec)
            sealed_total = None
            match = None
            publish_event = self._publish_event(rec.id, head)
            if publish_event is not None:
                sealed_total = publish_event.payload["snapshot"]["total"]
                match = sealed_total == score.total
            records_view.append(RecordAudit(
                record_id=rec.id,
                university_id=rec.university_id,
                year=year,
                caliber_id=rec.caliber_id,
                published=rec.published,
                publish_seq=rec.publish_seq,
                sealed_total=sealed_total,
                recomputed_total=score.total,
                match=match,
                missing_codes=score.missing_codes,
                evidence_gaps=score.evidence_gaps,
                superseded_by=rec.superseded_by_record,
            ))

        ranking = self._ranking(proj, year)
        return YearAuditReport(year=year, as_of_seq=head,
                               calibers=calibers_view, records=records_view,
                               ranking_now=ranking)

    def _publish_event(self, record_id: str, as_of_seq: int) -> Optional[Event]:
        for e in self.store.all_events():
            if e.seq > as_of_seq:
                break
            if e.type == P.EV_RECORD_PUBLISHED and e.payload["record_id"] == record_id:
                return e
        return None

    def _ranking(self, proj: P.Projection, year: int) -> list[dict]:
        rows = []
        for rec in proj.records.values():
            if rec.year != year or not rec.published:
                continue
            if rec.superseded_by_record is not None:
                continue  # 已被更正替代的记录不进入现行榜单
            cal = proj.calibers[rec.caliber_id]
            score = P.score_record(cal, rec)
            uni = proj.universities[rec.university_id]
            rows.append({"university_id": rec.university_id, "name": uni.name,
                         "university_type": uni.university_type, "record_id": rec.id,
                         "total": score.total})
        return sorted(rows, key=lambda r: (-r["total"], r["university_id"]))

    # ---- 单记录封存核对 ----

    def verify_record(self, record_id: str) -> dict:
        """用公布时刻的原始数据复算，与封存快照逐行核对。"""
        events = self.store.all_events()
        proj_now = P.Projection.rebuild(events)
        rec = proj_now.records.get(record_id)
        if rec is None:
            raise NotFoundError(f"考核记录不存在：{record_id}")
        if not rec.published:
            return {"record_id": record_id, "published": False, "verified": None}
        # 复算锚点：公布事件序号（其后该记录数据被工作流禁止修改）
        proj_at = P.Projection.rebuild(events, as_of_seq=rec.publish_seq)
        rec_at = proj_at.records[record_id]
        cal_at = proj_at.calibers[rec_at.caliber_id]
        recomputed = P.score_record(cal_at, rec_at)
        publish = self._publish_event(record_id, rec.publish_seq)
        snap = publish.payload["snapshot"]
        line_matches = [
            snap_line["contribution"] == ln.contribution
            for snap_line, ln in zip(snap["lines"], recomputed.lines)
        ]
        return {
            "record_id": record_id,
            "published": True,
            "publish_seq": rec.publish_seq,
            "caliber_digest_sealed": snap["caliber_digest"],
            "caliber_digest_now": recomputed.caliber_digest,
            "input_digest_sealed": snap["input_digest"],
            "input_digest_now": recomputed.input_digest,
            "total_sealed": snap["total"],
            "total_recomputed": recomputed.total,
            "verified": (
                snap["total"] == recomputed.total
                and snap["caliber_digest"] == recomputed.caliber_digest
                and snap["input_digest"] == recomputed.input_digest
                and all(line_matches)
            ),
            "missing_codes": recomputed.missing_codes,
            "evidence_gaps": recomputed.evidence_gaps,
        }

    # ---- 变更归因 ----

    def explain_window(self, start_seq: int, end_seq: int) -> dict:
        """解释日志序号区间 (start_seq, end_seq] 内每类事件改变了什么。

        逐事件推进投影：每条变更都给出该事件发生前一刻与发生后的状态，
        窗口起点的选择不会让"前后对比"失真。
        """
        events = self.store.all_events()
        walk = P.Projection.rebuild(events, as_of_seq=start_seq)

        late_data, revocations, publishes, corrections, caliber_changes = [], [], [], [], []
        window = [e for e in events if start_seq < e.seq <= end_seq]
        for e in window:
            p = e.payload
            if e.type == P.EV_VALUE_RECORDED:
                rec = walk.records.get(p["record_id"])
                old_mv = rec.values[p["code"]] if rec is not None and p["code"] in rec.values else None
                total_before = None
                if rec is not None:
                    total_before = P.score_record(walk.calibers[rec.caliber_id], rec).total
                if old_mv is not None:
                    walk.apply(e)
                    total_after = P.score_record(walk.calibers[rec.caliber_id], rec).total
                    item = {
                        "seq": e.seq, "at": e.at, "record_id": p["record_id"],
                        "metric_code": p["code"],
                        "old_value": old_mv.value, "new_value": p["value"],
                        "old_reason": old_mv.reason, "new_reason": p.get("reason", ""),
                        "filed_by": e.actor_id,
                        "published_at_time": bool(rec.published),
                        "total_before": total_before, "total_after": total_after,
                        "score_delta": round(total_after - total_before, 9),
                    }
                    late_data.append(item)
                else:
                    walk.apply(e)
            elif e.type == P.EV_SIGNATURE_REVOKED:
                cal = walk.calibers[p["caliber_id"]]
                signers_before = sorted(cal.active_signers())
                walk.apply(e)
                signers_after = sorted(cal.active_signers())
                revocations.append({
                    "seq": e.seq, "at": e.at, "caliber_id": p["caliber_id"],
                    "signer_id": p["signer_id"],
                    "active_signers_before": signers_before,
                    "active_signers_after": signers_after,
                    "active_count_before": len(signers_before),
                    "active_count_after": len(signers_after),
                })
            elif e.type == P.EV_RECORD_PUBLISHED:
                walk.apply(e)
                rec = walk.records[p["record_id"]]
                publishes.append({
                    "seq": e.seq, "at": e.at, "record_id": p["record_id"],
                    "university_id": rec.university_id,
                    "caliber_id": rec.caliber_id,
                    "caliber_digest": p["snapshot"]["caliber_digest"],
                    "total": p["snapshot"]["total"],
                    "missing_codes": p["snapshot"]["missing_codes"],
                    "evidence_gaps": p["snapshot"]["evidence_gaps"],
                    "published_by": e.actor_id,
                })
            elif e.type == P.EV_RECORD_CORRECTION_OPENED:
                old_total = self._score_of(walk, p["old_record_id"])
                walk.apply(e)
                corrections.append({
                    "seq": e.seq, "at": e.at,
                    "old_record_id": p["old_record_id"],
                    "new_record_id": p["new_record_id"],
                    "new_caliber_id": p["new_caliber_id"],
                    "old_total": old_total,
                })
            elif e.type in (P.EV_CALIBER_CREATED, P.EV_METRIC_DEFINED, P.EV_METRIC_ADJUSTED,
                            P.EV_CALIBER_SUBMITTED, P.EV_CALIBER_EFFECTIVATED,
                            P.EV_CALIBER_SEALED):
                walk.apply(e)
                caliber_changes.append({"seq": e.seq, "at": e.at, "type": e.type,
                                        "payload": p})
            else:
                walk.apply(e)

        return {
            "window": {"start_seq": start_seq, "end_seq": end_seq},
            "late_data": late_data,
            "revoked_signatures": revocations,
            "publishes": publishes,
            "concurrent_publish_note": (
                "区间内每次发布仅对应一个成功事件；并发落败方在序号冲突时被拒绝，"
                "不产生事件、不改变任何状态。"
            ),
            "corrections": corrections,
            "caliber_changes": caliber_changes,
        }

    def _score_of(self, proj: P.Projection, record_id: str) -> Optional[float]:
        rec = proj.records.get(record_id)
        if rec is None:
            return None
        return P.score_record(proj.calibers[rec.caliber_id], rec).total

    # ---- 并发发布演示/核查 ----

    def inspect_publish_race(self, record_id: str) -> dict:
        """给出记录的发布事实，供审计判断并发发布中谁胜出、改变了什么。"""
        events = self.store.all_events()
        publish = None
        for e in events:
            if e.type == P.EV_RECORD_PUBLISHED and e.payload["record_id"] == record_id:
                publish = e
                break
        if publish is None:
            return {"record_id": record_id, "published": False}
        return {
            "record_id": record_id,
            "published": True,
            "winning_seq": publish.seq,
            "winning_actor": publish.actor_id,
            "at": publish.at,
            "total": publish.payload["snapshot"]["total"],
            "note": "该序号是唯一发布事件；同一时刻的其他发布请求因序号冲突被拒绝。",
        }
