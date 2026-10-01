"""聚合仓储：从事件流重放状态，并承载全部写侧业务规则。"""
from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from typing import Any

from .errors import (
    CaliberOverlap,
    ConcurrentPublish,
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
from .scoring import compute_scores, diff_rankings
from .store import EventStore, canonical_manifest, utcnow_iso


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Repository:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._txn_lock = threading.RLock()
        self._replay()

    # ------------------------------------------------------------------ replay

    def _replay(self) -> None:
        self.schools: dict[str, dict] = {}
        self.metrics: dict[str, Metric] = {}
        self.calibers: dict[str, dict] = {}
        self.signs: dict[tuple[str, str], dict] = {}
        self.submissions: dict[str, dict] = {}
        self.publications: dict[str, dict] = {}
        self.corrections: list[dict] = []
        self._event_ids: set[str] = set()
        for e in self.store.events:
            if e.id:
                self._event_ids.add(e.id)
            self._apply(e)

    def _apply(self, e) -> None:
        p = e.payload
        t = e.type
        if t == "SCHOOL_REGISTERED":
            self.schools[p["school_code"]] = {
                "school_code": p["school_code"], "name": p["name"],
                "school_type": SchoolType(p["school_type"]),
            }
        elif t == "METRIC_REGISTERED":
            self.metrics[p["code"]] = Metric(
                code=p["code"], name=p["name"], school_type=SchoolType(p["school_type"]),
                kind=MetricKind(p["kind"]), unit=p.get("unit", ""), source=p.get("source", ""),
                evidence_required=p.get("evidence_required", True), description=p.get("description", ""),
            )
        elif t == "CALIBER_CREATED":
            self.calibers[p["caliber_id"]] = {
                "id": p["caliber_id"], "school_type": SchoolType(p["school_type"]),
                "year": p["year"], "version": p["version"], "weights": dict(p["weights"]),
                "window": ValidityWindow(p["window"]["start_year"], p["window"].get("end_year")),
                "required_signers": p["required_signers"], "creator": p["creator"],
                "status": CaliberStatus.DRAFT, "created_seq": e.seq,
                "signers": [], "revoked_signers": [], "superseded_by": None,
            }
        elif t == "COSIGN_STARTED":
            self.calibers[p["caliber_id"]]["status"] = CaliberStatus.IN_COSIGN
        elif t == "CALIBER_SIGNED":
            cid, signer = p["caliber_id"], p["signer"]
            c = self.calibers[cid]
            c["status"] = CaliberStatus.IN_COSIGN
            if signer not in c["signers"]:
                c["signers"].append(signer)
            c["revoked_signers"] = [s for s in c["revoked_signers"] if s != signer]
            self.signs[(cid, signer)] = {"caliber_id": cid, "signer": signer,
                                         "signed_seq": e.seq, "revoked": False,
                                         "revoked_seq": None}
        elif t == "SIGN_REVOKED":
            cid, signer = p["caliber_id"], p["signer"]
            self.calibers[cid]["signers"] = [s for s in self.calibers[cid]["signers"] if s != signer]
            if signer not in self.calibers[cid]["revoked_signers"]:
                self.calibers[cid]["revoked_signers"].append(signer)
            if (cid, signer) in self.signs:
                self.signs[(cid, signer)]["revoked"] = True
                self.signs[(cid, signer)]["revoked_seq"] = e.seq
        elif t == "CALIBER_PUBLISHED":
            self.calibers[p["caliber_id"]]["status"] = CaliberStatus.ACTIVE
            self.calibers[p["caliber_id"]]["effective_seq"] = e.seq
        elif t == "CALIBER_SUPERSEDED":
            self.calibers[p["caliber_id"]]["status"] = CaliberStatus.SUPERSEDED
            self.calibers[p["caliber_id"]]["superseded_by"] = p["by_caliber_id"]
        elif t == "DATA_SUBMITTED":
            self.submissions[p["submission_id"]] = {
                "id": p["submission_id"], "year": p["year"],
                "school_code": p["school_code"], "metric_code": p["metric_code"],
                "value": p["value"], "evidence_ref": p["evidence_ref"],
                "status": SubmissionStatus(p["status"]), "seq": e.seq,
                "timestamp": e.timestamp,
            }
        elif t == "PUBLICATION_SEALED":
            self.publications[p["publication_id"]] = {
                "id": p["publication_id"], "year": p["year"],
                "school_type": SchoolType(p["school_type"]),
                "caliber_id": p["caliber_id"], "version": p["version"],
                "cutoff": p["cutoff"], "manifest": p["manifest"],
                "scores": p["scores"], "missing": p["missing"],
                "values_snapshot": p["values_snapshot"],
                "weight_snapshot": p["weight_snapshot"],
                "metric_codes": p["metric_codes"],
                "schools_snapshot": p["schools_snapshot"],
                "txn": p.get("txn", ""), "correction_of": p.get("correction_of"),
                "status": "已封存", "seq": e.seq,
            }
            self.calibers[p["caliber_id"]]["status"] = CaliberStatus.SEALED
        elif t == "PUBLICATION_CORRECTED":
            old = self.publications[p["old_publication_id"]]
            old["status"] = "已更正"
            old["corrected_by"] = p["new_publication_id"]
            self.corrections.append({
                "old_publication_id": p["old_publication_id"],
                "new_publication_id": p["new_publication_id"],
                "new_caliber_id": p["new_caliber_id"], "reason": p["reason"],
                "late_submission_ids": list(p["late_submission_ids"]),
                "revoked_signers": list(p.get("revoked_signers", [])),
                "diff": p["diff"], "seq": e.seq,
            })
        # PUBLICATION_BLOCKED 仅作审计留痕，不改变状态

    # ------------------------------------------------------------- helpers

    def _append(self, etype: str, payload: dict, actor: str, *,
                event_id: str = "", txn: str = "", timestamp: str | None = None):
        if event_id:
            if event_id in self._event_ids:
                raise ValidationFailed(f"重复请求：{event_id}")
            self._event_ids.add(event_id)
        event = self.store.append(etype, deepcopy(payload), actor,
                                  event_id=event_id, txn=txn, timestamp=timestamp)
        self._apply(event)
        return event

    def _calibers_for(self, school_type: SchoolType, year: int) -> list[dict]:
        return [c for c in self.calibers.values()
                if c["school_type"] is school_type and c["year"] == year]

    def _active_calibers(self, school_type: SchoolType) -> list[dict]:
        return [c for c in self.calibers.values()
                if c["school_type"] is school_type and c["status"] is CaliberStatus.ACTIVE]

    def _publications_for(self, year: int, school_type: SchoolType) -> list[dict]:
        return sorted((pub for pub in self.publications.values()
                       if pub["year"] == year and pub["school_type"] is school_type),
                      key=lambda x: x["seq"])

    def _latest_publication(self, year: int, school_type: SchoolType) -> dict | None:
        pubs = self._publications_for(year, school_type)
        return pubs[-1] if pubs else None

    def _require_caliber(self, cid: str) -> dict:
        c = self.calibers.get(cid)
        if not c:
            raise NotFound(f"口径不存在：{cid}")
        return c

    def _valid_signers(self, cid: str) -> list[str]:
        return [s for (c, s), rec in self.signs.items() if c == cid and not rec["revoked"]]

    # ------------------------------------------------------------- 主数据

    def register_school(self, actor: str, code: str, name: str,
                        school_type: SchoolType, *, event_id: str = "") -> dict:
        if code in self.schools:
            raise ValidationFailed(f"高校已登记：{code}")
        self._append("SCHOOL_REGISTERED",
                     {"school_code": code, "name": name, "school_type": school_type.value},
                     actor, event_id=event_id)
        return self.schools[code]

    def register_metric(self, actor: str, code: str, name: str,
                        school_type: SchoolType, kind: MetricKind, *,
                        unit: str = "", source: str = "", evidence_required: bool = True,
                        description: str = "", event_id: str = "") -> Metric:
        if code in self.metrics:
            raise ValidationFailed(f"指标编码已存在：{code}")
        if not source:
            raise ValidationFailed("必须登记数据来源")
        payload = {"code": code, "name": name, "school_type": school_type.value,
                   "kind": kind.value, "unit": unit, "source": source,
                   "evidence_required": evidence_required, "description": description}
        self._append("METRIC_REGISTERED", payload, actor, event_id=event_id)
        return self.metrics[code]

    # ------------------------------------------------------------- 口径会签

    def create_caliber(self, actor: str, school_type: SchoolType, year: int,
                       weights: dict[str, float], window: ValidityWindow,
                       required_signers: int, *, event_id: str = "") -> dict:
        if required_signers < 2:
            raise ValidationFailed("新口径必须多人会签（至少 2 人）")
        if abs(sum(weights.values()) - 1.0) > 1e-6:
            raise ValidationFailed("权重之和必须为 1")
        for code, w in weights.items():
            if code not in self.metrics:
                raise ValidationFailed(f"指标不存在：{code}")
            if self.metrics[code].school_type is not school_type:
                raise ValidationFailed(f"指标 {code} 不属于该办学类型")
            if not 0 < w <= 1:
                raise ValidationFailed("单项权重必须落在 (0,1]")
        if not window.covers(year):
            raise ValidationFailed("生效区间必须覆盖考核年度")
        version = max((c["version"] for c in self._calibers_for(school_type, year)), default=0) + 1
        cid = _new_id("cal")
        self._append("CALIBER_CREATED", {
            "caliber_id": cid, "school_type": school_type.value, "year": year,
            "version": version, "weights": weights,
            "window": {"start_year": window.start_year, "end_year": window.end_year},
            "required_signers": required_signers, "creator": actor,
        }, actor, event_id=event_id)
        return self.calibers[cid]

    def start_cosign(self, actor: str, caliber_id: str) -> None:
        c = self._require_caliber(caliber_id)
        if c["status"] is not CaliberStatus.DRAFT:
            raise WorkflowError(f"口径当前为 {c['status'].value}，不能发起会签")
        self._append("COSIGN_STARTED", {"caliber_id": caliber_id}, actor)

    def sign_caliber(self, actor: str, caliber_id: str, signer: str | None = None) -> None:
        signer = signer or actor
        c = self._require_caliber(caliber_id)
        if c["status"] not in (CaliberStatus.DRAFT, CaliberStatus.IN_COSIGN):
            raise WorkflowError(f"口径当前为 {c['status'].value}，不能签署")
        if (caliber_id, signer) in self.signs and not self.signs[(caliber_id, signer)]["revoked"]:
            raise ValidationFailed("已签署，请勿重复")
        self._append("CALIBER_SIGNED", {"caliber_id": caliber_id, "signer": signer}, actor)

    def revoke_sign(self, actor: str, caliber_id: str, signer: str, reason: str = "") -> None:
        c = self._require_caliber(caliber_id)
        rec = self.signs.get((caliber_id, signer))
        if not rec or rec["revoked"]:
            raise NotFound("未找到该有效签署")
        if c["status"] not in (CaliberStatus.IN_COSIGN, CaliberStatus.ACTIVE, CaliberStatus.SEALED):
            raise WorkflowError(f"口径当前为 {c['status'].value}，不能撤销签署")
        self._append("SIGN_REVOKED",
                     {"caliber_id": caliber_id, "signer": signer, "reason": reason}, actor)

    def publish_caliber(self, actor: str, caliber_id: str, *,
                        supersedes: str | None = None) -> dict:
        """会签通过 → 生效。supersedes 用于更正版本原子替代旧口径。"""
        with self._txn_lock:
            c = self._require_caliber(caliber_id)
            if c["status"] is not CaliberStatus.IN_COSIGN:
                raise WorkflowError(f"口径当前为 {c['status'].value}，不能生效")
            valid = self._valid_signers(caliber_id)
            if len(set(valid)) < c["required_signers"]:
                raise QuorumNotMet(
                    f"会签人数不足：{len(set(valid))}/{c['required_signers']}")
            if supersedes:
                old = self._require_caliber(supersedes)
                if old["status"] not in (CaliberStatus.ACTIVE, CaliberStatus.SEALED):
                    raise WorkflowError("被替代口径必须处于生效或封存状态")
                if old["status"] is CaliberStatus.SEALED and not self._latest_publication(
                        old["year"], old["school_type"]):
                    raise WorkflowError("封存口径缺少对应发布，不能被替代")
                if old["school_type"] is not c["school_type"] or old["year"] != c["year"]:
                    raise ValidationFailed("只能替代同年度同类型口径")
                self._append("CALIBER_SUPERSEDED",
                             {"caliber_id": supersedes, "by_caliber_id": caliber_id}, actor)
            else:
                for other in self._active_calibers(c["school_type"]):
                    if self._windows_overlap(c["window"], other["window"]):
                        raise CaliberOverlap(
                            f"与生效口径 {other['id']}（v{other['version']}）生效区间重叠")
            self._append("CALIBER_PUBLISHED", {"caliber_id": caliber_id}, actor)
            return self.calibers[caliber_id]

    @staticmethod
    def _windows_overlap(a: ValidityWindow, b: ValidityWindow) -> bool:
        a_end = a.end_year if a.end_year is not None else 10**6
        b_end = b.end_year if b.end_year is not None else 10**6
        return a.start_year < b_end and b.start_year < a_end

    # ------------------------------------------------------------- 数据填报

    def submit_data(self, actor: str, year: int, school_code: str, metric_code: str,
                    value: float | None, evidence_ref: str, *,
                    event_id: str = "", timestamp: str | None = None) -> dict:
        """填报数据。幂等键必填；封存后到达自动记为迟到，缺证据单列。"""
        if not event_id:
            raise ValidationFailed("填报必须提供幂等键")
        if event_id in self._event_ids:
            ev = next(e for e in self.store.events if e.id == event_id)
            if ev.type == "DATA_SUBMITTED":
                return self.submissions[ev.payload["submission_id"]]
            raise ValidationFailed(f"重复请求：{event_id}")
        if school_code not in self.schools:
            raise NotFound(f"高校未登记：{school_code}")
        metric = self.metrics.get(metric_code)
        if not metric:
            raise NotFound(f"指标不存在：{metric_code}")
        school_type = self.schools[school_code]["school_type"]
        if metric.school_type is not school_type:
            raise ValidationFailed("指标与高校办学类型不符")
        sealed = self._latest_publication(year, school_type)
        sid = _new_id("sub")
        if not evidence_ref:
            status = SubmissionStatus.EVIDENCE_MISSING
            late = bool(sealed)
        elif sealed:
            status = SubmissionStatus.LATE
            late = True
        else:
            status = SubmissionStatus.INCLUDED
            late = False
        self._append("DATA_SUBMITTED", {
            "submission_id": sid, "year": year, "school_code": school_code,
            "metric_code": metric_code, "value": value,
            "evidence_ref": evidence_ref, "status": status.value,
            "arrived_after_seal": late,
        }, actor, event_id=event_id, timestamp=timestamp)
        return self.submissions[sid]

    # ------------------------------------------------------------- 封存发布

    def _snapshot_inputs(self, year: int, school_type: SchoolType, before_seq: int,
                         include_late: bool) -> tuple[dict, list[str], list[str]]:
        values: dict[str, dict[str, float]] = {}
        late_ids: list[str] = []
        for s in self.submissions.values():
            if s["year"] != year or s["seq"] >= before_seq:
                continue
            if self.schools[s["school_code"]]["school_type"] is not school_type:
                continue
            if s["status"] is SubmissionStatus.EVIDENCE_MISSING or not s["evidence_ref"]:
                continue
            if s["status"] is SubmissionStatus.LATE:
                late_ids.append(s["id"])
                if not include_late:
                    continue
            values.setdefault(s["school_code"], {})[s["metric_code"]] = s["value"]
        schools = sorted(code for code, sch in self.schools.items()
                         if sch["school_type"] is school_type)
        return values, schools, late_ids

    def seal_publication(self, actor: str, caliber_id: str, *,
                         txn: str = "", timestamp: str | None = None) -> dict:
        """阶段一事务：锁定 → 复核生效口径与并发 → 封存口径、分数、缺失证据、清单指纹。"""
        with self._txn_lock:
            c = self._require_caliber(caliber_id)
            year, stype = c["year"], c["school_type"]
            existing = self._latest_publication(year, stype)
            if existing and existing["status"] == "已封存":
                self._append("PUBLICATION_BLOCKED", {
                    "year": year, "school_type": stype.value, "caliber_id": caliber_id,
                    "reason": f"已有封存发布 {existing['id']}，并发/重复发布被拒绝",
                    "existing_publication_id": existing["id"],
                }, actor, txn=txn, timestamp=timestamp)
                raise ConcurrentPublish("该年度类型已封存，并发发布被拒绝；只能走更正版本")
            if c["status"] is not CaliberStatus.ACTIVE:
                raise WorkflowError(f"口径当前为 {c['status'].value}，只有生效口径可封存")

            cutoff = timestamp or utcnow_iso()
            seq_before = len(self.store.events) + 1
            values, schools, _ = self._snapshot_inputs(year, stype, seq_before, include_late=False)
            metrics = {code: self.metrics[code] for code in c["weights"]}
            ranked, missing = compute_scores(metrics, c["weights"], values, schools)
            scores = [r.__dict__ for r in ranked]
            missing_d = [m.__dict__ for m in missing]
            manifest_items = [
                {"kind": "caliber", "caliber_id": caliber_id, "version": c["version"],
                 "weights": c["weights"], "window": {"start_year": c["window"].start_year,
                                                     "end_year": c["window"].end_year}},
                {"kind": "cutoff", "cutoff": cutoff},
                {"kind": "schools", "schools": schools},
                {"kind": "scores", "scores": scores},
                {"kind": "missing", "missing": missing_d},
                {"kind": "values", "values": values},
            ]
            manifest = canonical_manifest(manifest_items)
            pid = _new_id("pub")
            version = len(self._publications_for(year, stype)) + 1
            self._append("PUBLICATION_SEALED", {
                "publication_id": pid, "year": year, "school_type": stype.value,
                "caliber_id": caliber_id, "version": version, "cutoff": cutoff,
                "manifest": manifest, "scores": scores, "missing": missing_d,
                "values_snapshot": values, "weight_snapshot": dict(c["weights"]),
                "metric_codes": sorted(c["weights"]), "schools_snapshot": schools,
                "correction_of": existing["id"] if existing else None,
            }, actor, txn=txn, timestamp=timestamp)
            return self.publications[pid]

    def correct_publication(self, actor: str, old_publication_id: str,
                            new_caliber_id: str, reason: str, *,
                            txn: str = "", timestamp: str | None = None) -> dict:
        """已公布结果不得修改：以更正版本重算，旧版标记"已更正"并完整保留。"""
        with self._txn_lock:
            old = self.publications.get(old_publication_id)
            if not old:
                raise NotFound(f"发布不存在：{old_publication_id}")
            if old["status"] != "已封存":
                raise WorkflowError("只能更正当前封存版本；该版本已是历史更正记录")
            latest = self._latest_publication(old["year"], old["school_type"])
            if latest is not old:
                raise WorkflowError("存在更新的封存版本，只能对最新版本发起更正")
            year, stype = old["year"], old["school_type"]
            c = self._require_caliber(new_caliber_id)
            if c["id"] == old["caliber_id"]:
                raise ValidationFailed("更正版本必须使用新口径")
            if c["school_type"] is not stype or c["year"] != year:
                raise ValidationFailed("更正口径必须同年度同类型")
            if c["status"] is not CaliberStatus.ACTIVE:
                raise WorkflowError("更正口径必须先完成会签并生效")

            cutoff = timestamp or utcnow_iso()
            seq_before = len(self.store.events) + 1
            values, schools, late_ids = self._snapshot_inputs(
                year, stype, seq_before, include_late=True)
            metrics = {code: self.metrics[code] for code in c["weights"]}
            ranked, missing = compute_scores(metrics, c["weights"], values, schools)
            scores = [r.__dict__ for r in ranked]
            missing_d = [m.__dict__ for m in missing]
            manifest = canonical_manifest([
                {"kind": "caliber", "caliber_id": new_caliber_id, "version": c["version"],
                 "weights": c["weights"]},
                {"kind": "cutoff", "cutoff": cutoff},
                {"kind": "schools", "schools": schools},
                {"kind": "scores", "scores": scores},
                {"kind": "missing", "missing": missing_d},
                {"kind": "values", "values": values},
                {"kind": "corrects", "publication_id": old_publication_id,
                 "old_manifest": old["manifest"]},
            ])
            pid = _new_id("pub")
            version = len(self._publications_for(year, stype)) + 1
            self._append("PUBLICATION_SEALED", {
                "publication_id": pid, "year": year, "school_type": stype.value,
                "caliber_id": new_caliber_id, "version": version, "cutoff": cutoff,
                "manifest": manifest, "scores": scores, "missing": missing_d,
                "values_snapshot": values, "weight_snapshot": dict(c["weights"]),
                "metric_codes": sorted(c["weights"]), "schools_snapshot": schools,
                "correction_of": old_publication_id,
            }, actor, txn=txn, timestamp=timestamp)
            new_pub = self.publications[pid]

            revoked_between = sorted({
                signer for (cid, signer), rec in self.signs.items()
                if rec["revoked"] and rec["revoked_seq"] is not None
                and old["seq"] <= rec["revoked_seq"] <= new_pub["seq"]
            })
            delta = diff_rankings([_row(r) for r in old["scores"]],
                                  [_row(r) for r in scores])
            self._append("PUBLICATION_CORRECTED", {
                "old_publication_id": old_publication_id,
                "new_publication_id": pid, "new_caliber_id": new_caliber_id,
                "reason": reason, "late_submission_ids": late_ids,
                "revoked_signers": revoked_between, "diff": delta,
            }, actor, txn=txn, timestamp=timestamp)
            return new_pub


def _row(r: dict):
    from .models import ScoreEntry
    return ScoreEntry(school_code=r["school_code"], score=r["score"], rank=r["rank"])
