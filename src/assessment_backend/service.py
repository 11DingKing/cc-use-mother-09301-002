"""应用服务：命令处理、角色与工作流约束、结果封存与更正。

所有状态变更都通过这里追加事件；服务本身无状态，状态由事件投影重建。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import projection as P
from .actors import (
    ALL_ROLES,
    DEFAULT_QUORUM,
    MISSING_NO_DATA,
    MISSING_NO_EVIDENCE,
    ROLE_AUDITOR,
    ROLE_FILER,
    ROLE_MANAGER,
    STATE_DRAFT,
    STATE_EFFECTIVE,
    STATE_SEALED,
    STATE_SIGNING,
)
from .errors import ConcurrencyError, NotFoundError, PermissionError, ValidationError, WorkflowError
from .events import Event
from .models import ALL_DIMENSIONS
from .runtime import SystemRuntime
from .storage import EventStore

_WEIGHT_TOL = 1e-9


@dataclass
class Actor:
    actor_id: str
    role: str


class AssessmentService:
    def __init__(self, store: EventStore, runtime: Optional[SystemRuntime] = None,
                 quorum: int = DEFAULT_QUORUM) -> None:
        self.store = store
        self.runtime = runtime or SystemRuntime()
        self.quorum = quorum

    # ---- 内部工具 ----

    def _project(self) -> P.Projection:
        return P.Projection.rebuild(self.store.all_events())

    def _require_role(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionError(f"角色 {actor.role} 无权执行该操作（需要：{'、'.join(roles)}）")

    def _get_caliber(self, proj: P.Projection, caliber_id: str):
        cal = proj.calibers.get(caliber_id)
        if cal is None:
            raise NotFoundError(f"口径版本不存在：{caliber_id}")
        return cal

    def _get_record(self, proj: P.Projection, record_id: str):
        rec = proj.records.get(record_id)
        if rec is None:
            raise NotFoundError(f"考核记录不存在：{record_id}")
        return rec

    def _append(self, event_type: str, payload: dict, actor: Actor,
                expected_seq: Optional[int] = None) -> Event:
        return self.store.append(event_type, payload, actor.actor_id, actor.role, expected_seq)

    # ---- 高校 ----

    def register_university(self, actor: Actor, university_id: str, name: str,
                            university_type: str) -> Event:
        self._require_role(actor, ROLE_MANAGER)
        if not name or not university_type:
            raise ValidationError("高校名称与办学类型不能为空")
        proj = self._project()
        if university_id in proj.universities:
            raise ValidationError(f"高校已存在：{university_id}")
        return self._append(
            P.EV_UNIVERSITY_REGISTERED,
            {"stream_id": university_id, "university_id": university_id,
             "name": name, "university_type": university_type},
            actor,
        )

    # ---- 口径版本 ----

    def _next_sequence(self, proj: P.Projection, university_type: str, year: int) -> int:
        return len(proj.type_year_calibers.get((university_type, year), [])) + 1

    def create_caliber(self, actor: Actor, university_type: str, year: int,
                       note: str = "") -> Event:
        """为某办学类型某年度建立一套新口径草案。"""
        self._require_role(actor, ROLE_MANAGER)
        if not university_type or year < 1900:
            raise ValidationError("办学类型与年度不合法")
        proj = self._project()
        sequence = self._next_sequence(proj, university_type, year)
        caliber_id = self.runtime.new_id("CAL")
        return self._append(
            P.EV_CALIBER_CREATED,
            {"stream_id": caliber_id, "caliber_id": caliber_id,
             "university_type": university_type, "year": year, "sequence": sequence,
             "note": note},
            actor,
        )

    def open_correction_caliber(self, actor: Actor, sealed_caliber_id: str,
                                note: str = "") -> Event:
        """对已封存口径开启更正版本：复制指标与权重，可调整后重新走会签。

        旧口径保持封存不变，历史排名所依据的口径永不被原地修改。
        """
        self._require_role(actor, ROLE_MANAGER)
        proj = self._project()
        old = self._get_caliber(proj, sealed_caliber_id)
        if old.state != STATE_SEALED:
            raise WorkflowError("只有已封存口径才能开立更正版本")
        new_id = self.runtime.new_id("CAL")
        sequence = self._next_sequence(proj, old.university_type, old.year)
        event = self._append(
            P.EV_CALIBER_CREATED,
            {"stream_id": new_id, "caliber_id": new_id,
             "university_type": old.university_type, "year": old.year, "sequence": sequence,
             "supersedes": old.id, "note": note},
            actor,
        )
        # 复制旧口径全部指标作为更正版起点
        for code in sorted(old.metrics):
            m = old.metrics[code]
            self._append(
                P.EV_METRIC_DEFINED,
                {"stream_id": new_id, "caliber_id": new_id, "code": m.code, "name": m.name,
                 "dimension": m.dimension, "source": m.source, "weight": m.weight},
                actor,
            )
        return event

    def define_metric(self, actor: Actor, caliber_id: str, code: str, name: str,
                      dimension: str, source: str, weight: float) -> Event:
        self._require_role(actor, ROLE_MANAGER)
        if not code or not name or not source:
            raise ValidationError("指标编码、名称与数据来源不能为空")
        if dimension not in ALL_DIMENSIONS:
            raise ValidationError(f"指标维度必须是：{'、'.join(ALL_DIMENSIONS)}")
        if not isinstance(weight, (int, float)) or weight <= 0:
            raise ValidationError("权重必须为正数")
        proj = self._project()
        cal = self._get_caliber(proj, caliber_id)
        if cal.state != STATE_DRAFT:
            raise WorkflowError(f"口径处于{cal.state}状态，不能再定义/调整指标")
        if code in cal.metrics:
            raise ValidationError(f"指标编码在本口径内重复：{code}")
        return self._append(
            P.EV_METRIC_DEFINED,
            {"stream_id": caliber_id, "caliber_id": caliber_id, "code": code,
             "name": name, "dimension": dimension, "source": source, "weight": float(weight)},
            actor,
        )

    def adjust_metric(self, actor: Actor, caliber_id: str, code: str, name: str,
                      dimension: str, source: str, weight: float) -> Event:
        """在草案期调整指标（名称/维度/来源/权重）；历史版本不受影响。"""
        self._require_role(actor, ROLE_MANAGER)
        if dimension not in ALL_DIMENSIONS:
            raise ValidationError(f"指标维度必须是：{'、'.join(ALL_DIMENSIONS)}")
        if not name or not source or not isinstance(weight, (int, float)) or weight <= 0:
            raise ValidationError("名称、数据来源不能为空，权重必须为正数")
        proj = self._project()
        cal = self._get_caliber(proj, caliber_id)
        if cal.state != STATE_DRAFT:
            raise WorkflowError(f"口径处于{cal.state}状态，不能再调整指标")
        if code not in cal.metrics:
            raise NotFoundError(f"指标在本口径中不存在：{code}")
        return self._append(
            P.EV_METRIC_ADJUSTED,
            {"stream_id": caliber_id, "caliber_id": caliber_id, "code": code,
             "name": name, "dimension": dimension, "source": source, "weight": float(weight)},
            actor,
        )

    def submit_for_signing(self, actor: Actor, caliber_id: str) -> Event:
        self._require_role(actor, ROLE_MANAGER)
        proj = self._project()
        cal = self._get_caliber(proj, caliber_id)
        if cal.state != STATE_DRAFT:
            raise WorkflowError(f"只有草案可以提交会签，当前为{cal.state}")
        if not cal.metrics:
            raise ValidationError("口径至少要包含一个指标")
        for dimension in ALL_DIMENSIONS:
            if not any(m.dimension == dimension for m in cal.metrics.values()):
                raise ValidationError(f"口径缺少贡献维度：{dimension}")
        if abs(cal.weight_sum() - 1.0) > _WEIGHT_TOL:
            raise ValidationError(f"权重之和必须为 1.0，当前为 {cal.weight_sum():.6f}")
        return self._append(
            P.EV_CALIBER_SUBMITTED,
            {"stream_id": caliber_id, "caliber_id": caliber_id},
            actor,
        )

    def sign_caliber(self, actor: Actor, caliber_id: str) -> Event:
        """多人会签：签署人必须互不相同；评价管理与审计角色可签，填报员不可。"""
        self._require_role(actor, ROLE_MANAGER, ROLE_AUDITOR)
        proj = self._project()
        cal = self._get_caliber(proj, caliber_id)
        if cal.state != STATE_SIGNING:
            raise WorkflowError(f"只有会签中的口径可以签署，当前为{cal.state}")
        if actor.actor_id in cal.active_signers():
            raise ValidationError("同一签署人不能重复会签")
        return self._append(
            P.EV_CALIBER_SIGNED,
            {"stream_id": caliber_id, "caliber_id": caliber_id,
             "signer_id": actor.actor_id, "signer_role": actor.role},
            actor,
        )

    def revoke_signature(self, actor: Actor, caliber_id: str, signer_id: str) -> Event:
        """撤销签署只允许在会签阶段、由签署人本人操作。"""
        self._require_role(actor, ROLE_MANAGER, ROLE_AUDITOR)
        proj = self._project()
        cal = self._get_caliber(proj, caliber_id)
        if cal.state != STATE_SIGNING:
            raise WorkflowError(f"口径已进入{cal.state}阶段，签署不可撤销")
        if actor.actor_id != signer_id:
            raise PermissionError("只能撤销本人的签署")
        if signer_id not in cal.active_signers():
            raise NotFoundError("该签署人没有有效的会签记录")
        return self._append(
            P.EV_SIGNATURE_REVOKED,
            {"stream_id": caliber_id, "caliber_id": caliber_id, "signer_id": signer_id},
            actor,
        )

    def effectivate_caliber(self, actor: Actor, caliber_id: str) -> Event:
        self._require_role(actor, ROLE_MANAGER)
        proj = self._project()
        cal = self._get_caliber(proj, caliber_id)
        if cal.state != STATE_SIGNING:
            raise WorkflowError(f"只有会签中的口径可以生效，当前为{cal.state}")
        active = cal.active_signers()
        if len(active) < self.quorum:
            raise WorkflowError(f"会签人数不足：需要至少 {self.quorum} 人，当前 {len(active)} 人")
        return self._append(
            P.EV_CALIBER_EFFECTIVATED,
            {"stream_id": caliber_id, "caliber_id": caliber_id,
             "signers": sorted(active)},
            actor,
        )

    def seal_caliber(self, actor: Actor, caliber_id: str) -> Event:
        """封存生效口径：封存后指标、权重、数据来源与生效区间全部冻结。"""
        self._require_role(actor, ROLE_MANAGER)
        proj = self._project()
        cal = self._get_caliber(proj, caliber_id)
        if cal.state != STATE_EFFECTIVE:
            raise WorkflowError(f"只有已生效口径可以封存，当前为{cal.state}")
        if abs(cal.weight_sum() - 1.0) > _WEIGHT_TOL:
            raise ValidationError("权重之和不为 1.0，不能封存")
        fp = P.caliber_fingerprint(cal)
        return self._append(
            P.EV_CALIBER_SEALED,
            {"stream_id": caliber_id, "caliber_id": caliber_id,
             "caliber_digest": fp, "metrics": sorted(cal.metrics)},
            actor,
        )

    # ---- 考核记录与数据填报 ----

    def open_record(self, actor: Actor, record_id: str, university_id: str, year: int) -> Event:
        """为高校开立年度考核记录，绑定该高校办学类型在该年度的封存口径。"""
        self._require_role(actor, ROLE_MANAGER)
        proj = self._project()
        uni = proj.universities.get(university_id)
        if uni is None:
            raise NotFoundError(f"高校不存在：{university_id}")
        calibers = proj.type_year_calibers.get((uni.university_type, year), [])
        sealed = [cid for cid in calibers if proj.calibers[cid].state == STATE_SEALED]
        if not sealed:
            raise WorkflowError(f"{uni.university_type}{year}年度尚无封存口径，不能开立记录")
        caliber_id = sealed[-1]
        for rec in proj.records.values():
            if rec.university_id == university_id and rec.year == year and \
                    rec.superseded_by_record is None:
                raise ValidationError(f"高校 {university_id} {year}年度已有记录：{rec.id}")
        return self._append(
            P.EV_RECORD_OPENED,
            {"stream_id": record_id, "record_id": record_id, "year": year,
             "university_id": university_id, "caliber_id": caliber_id},
            actor,
        )

    def record_value(self, actor: Actor, record_id: str, code: str,
                     value: Optional[float], evidence_ref: Optional[str] = None,
                     note: str = "") -> Event:
        """填报单个指标。允许重复提交（迟到数据）；公布后禁止修改，必须走更正。

        - value 为 None：须登记缺失原因 NO_DATA；
        - 有值无证据：允许登记，但计分时按 NO_EVIDENCE 计 0 并留痕。
        """
        self._require_role(actor, ROLE_FILER)
        proj = self._project()
        rec = self._get_record(proj, record_id)
        if rec.published:
            raise WorkflowError("记录已公布，数据不能再改动；请申请更正版本")
        cal = self._get_caliber(proj, rec.caliber_id)
        if code not in cal.metrics:
            raise ValidationError(f"指标 {code} 不在封存口径 {cal.id} 中")
        if value is None:
            reason = MISSING_NO_DATA
            evidence_ref = None
        else:
            value = float(value)
            reason = "" if evidence_ref else MISSING_NO_EVIDENCE
        return self._append(
            P.EV_VALUE_RECORDED,
            {"stream_id": record_id, "record_id": record_id, "code": code,
             "value": value, "evidence_ref": evidence_ref, "reason": reason, "note": note},
            actor,
        )

    def publish_record(self, actor: Actor, record_id: str,
                       expected_seq: Optional[int] = None) -> Event:
        """公布结果：把按封存口径计算的总分、口径指纹与缺失证据一并写进事件。

        expected_seq 用于并发发布控制：两个发布者只有一个成功，
        另一个收到 ConcurrencyError 后必须重读再处理。
        """
        self._require_role(actor, ROLE_MANAGER)
        proj = self._project()
        # 乐观并发优先：调用方基于旧序号提交，直接判冲突，不看当前状态
        if expected_seq is not None and expected_seq != len(self.store.all_events()):
            raise ConcurrencyError(
                f"并发冲突：期望日志序号 {expected_seq}，实际 {len(self.store.all_events())}，请重读后重试"
            )
        rec = self._get_record(proj, record_id)
        if rec.published:
            raise WorkflowError("记录已公布；已公布结果只能以更正版本处理")
        cal = self._get_caliber(proj, rec.caliber_id)
        if cal.state != STATE_SEALED:
            raise WorkflowError("只能按封存口径公布结果")
        missing_inputs = sorted(set(cal.metrics) - set(rec.values))
        if missing_inputs:
            raise ValidationError("尚有指标未填报（含缺失登记）：" + "、".join(missing_inputs))
        score = P.score_record(cal, rec)
        snapshot = {
            "total": score.total,
            "caliber_digest": score.caliber_digest,
            "input_digest": score.input_digest,
            "missing_codes": score.missing_codes,
            "evidence_gaps": score.evidence_gaps,
            "lines": [
                {"code": ln.metric_code, "weight": ln.weight, "raw_value": ln.raw_value,
                 "scored": ln.scored, "reason": ln.reason, "contribution": ln.contribution}
                for ln in score.lines
            ],
        }
        return self._append(
            P.EV_RECORD_PUBLISHED,
            {"stream_id": record_id, "record_id": record_id, "snapshot": snapshot},
            actor, expected_seq=expected_seq,
        )

    def open_correction_record(self, actor: Actor, old_record_id: str,
                               new_caliber_id: str, new_record_id: str) -> Event:
        """已公布结果只能开立更正记录：绑定一份封存的更正口径。"""
        self._require_role(actor, ROLE_MANAGER)
        proj = self._project()
        old = self._get_record(proj, old_record_id)
        if not old.published:
            raise WorkflowError("只有已公布结果需要更正")
        if old.superseded_by_record is not None:
            raise WorkflowError("原结果已有更正版本，不能重复更正")
        new_cal = self._get_caliber(proj, new_caliber_id)
        if new_cal.state != STATE_SEALED or not new_cal.is_correction:
            raise WorkflowError("更正记录必须绑定封存的更正口径版本")
        if new_cal.supersedes != old.caliber_id:
            raise ValidationError("更正口径与原记录封存口径不匹配")
        uni = proj.universities[old.university_id]
        return self._append(
            P.EV_RECORD_CORRECTION_OPENED,
            {"stream_id": new_record_id, "new_record_id": new_record_id,
             "old_record_id": old_record_id, "new_caliber_id": new_caliber_id,
             "year": old.year, "university_id": uni.id},
            actor,
        )

    # ---- 查询 ----

    def get_score(self, record_id: str) -> P.ScoreResult:
        proj = self._project()
        rec = self._get_record(proj, record_id)
        return P.score_record(self._get_caliber(proj, rec.caliber_id), rec)
