"""端到端演示：差异化口径、多人会签、缺失证据、迟到数据、撤销签署、并发发布与更正复算。

运行：python3 tools/demo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from assessment_backend import (
    Actor,
    AssessmentService,
    AuditService,
    ConcurrencyError,
    DIMENSION_SERVICE,
    DIMENSION_SKILL,
    DIMENSION_RESEARCH,
    EventStore,
    FixedRuntime,
    ROLE_AUDITOR,
    ROLE_FILER,
    ROLE_MANAGER,
)
from assessment_backend.projection import Projection

YEAR = 2025


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    store = EventStore(FixedRuntime(start="2025-11-01T09:00:00+00:00"))
    svc = AssessmentService(store, runtime=store._runtime, quorum=2)
    audit = AuditService(store)

    manager = Actor("赵管理", ROLE_MANAGER)
    auditor = Actor("钱审计", ROLE_AUDITOR)
    manager_b = Actor("孙评审", ROLE_MANAGER)
    filer = Actor("李填报", ROLE_FILER)

    # 1) 高校与差异化口径 ---------------------------------------------------
    section("1. 注册高校，按办学类型建立 2025 年度口径草案")
    svc.register_university(manager, "U1", "江北职业技术学院", "技能型")
    svc.register_university(manager, "U2", "河东理工学院", "教学研究型")
    svc.create_caliber(manager, "技能型", YEAR)

    cal_skill = [c for c in Projection.rebuild(store.all_events()).calibers.values()
                 if c.university_type == "技能型"][0].id

    svc.define_metric(manager, cal_skill, "SK1", "高级工以上培养比例", DIMENSION_SKILL, "人社厅", 0.45)
    svc.define_metric(manager, cal_skill, "SV1", "校企合作横向服务", DIMENSION_SERVICE, "学校台账", 0.30)
    svc.define_metric(manager, cal_skill, "RS1", "应用技术研究项目", DIMENSION_RESEARCH, "科技厅", 0.25)
    print("技能型口径：技能 45% / 地方服务 30% / 基础研究 25%")

    # 2) 多人会签 + 撤销签署 ------------------------------------------------
    section("2. 提交会签：钱审计签署后撤销，会签人数不足被生效拒绝")
    svc.submit_for_signing(manager, cal_skill)
    svc.sign_caliber(auditor, cal_skill)
    svc.revoke_signature(auditor, cal_skill, "钱审计")
    try:
        svc.effectivate_caliber(manager, cal_skill)
    except Exception as exc:
        print(f"生效被拒：{exc}")
    svc.sign_caliber(auditor, cal_skill)
    svc.sign_caliber(manager_b, cal_skill)
    svc.effectivate_caliber(manager, cal_skill)
    svc.seal_caliber(manager, cal_skill)
    print("重新达到 2 人会签，口径生效并封存（指标/权重/来源冻结）")

    # 封存后口径不可改
    try:
        svc.define_metric(manager, cal_skill, "X", "x", DIMENSION_SKILL, "x", 0.1)
    except Exception as exc:
        print(f"封存后改指标被拒：{exc}")

    # 3) 开立记录、填报（含缺失证据）与迟到数据 ----------------------------
    section("3. 填报数据：一项无数据登记缺失，一项有值缺证据计 0")
    svc.open_record(manager, "R1", "U1", YEAR)
    svc.record_value(filer, "R1", "SK1", 88.0, "EV-SK1-001")
    svc.record_value(filer, "R1", "SV1", None, note="横向合同尚未归档")
    svc.record_value(filer, "R1", "RS1", 70.0, None, note="仅有汇总表，原始凭证待补")
    score_before = svc.get_score("R1")
    print(f"当前总分 {score_before.total}；缺失 {score_before.missing_codes}；缺证据 {score_before.evidence_gaps}")

    seq_before_late = len(store.all_events())
    svc.record_value(filer, "R1", "SV1", 92.0, "EV-SV1-007", note="合同迟到归档")
    score_after = svc.get_score("R1")
    seq_after_late = len(store.all_events())
    print(f"迟到数据到达后总分 {score_after.total}（SK1 不变，SV1 由缺失转为有证据）")

    # 4) 并发发布 -----------------------------------------------------------
    section("4. 并发发布：两个发布请求基于同一序号，只有一个成功")
    seq_at_publish = len(store.all_events())
    svc.publish_record(manager, "R1", expected_seq=seq_at_publish)
    try:
        svc.publish_record(manager_b, "R1", expected_seq=seq_at_publish)
    except ConcurrencyError as exc:
        print(f"第二个发布被拒：{exc}")
    print(json.dumps(audit.inspect_publish_race("R1"), ensure_ascii=False, indent=2))

    # 公布后迟到数据被拒
    try:
        svc.record_value(filer, "R1", "RS1", 95.0, "EV-RS1-009")
    except Exception as exc:
        print(f"公布后改数据被拒：{exc}")

    # 5) 更正版本：新口径多人会签 → 封存 → 更正结果 -------------------------
    section("5. 已公布结果只能更正：开立更正口径并重新会签封存")
    ev = svc.open_correction_caliber(manager, cal_skill, note="提高基础研究权重")
    new_cal = ev.payload["caliber_id"]
    # 更正口径复制了旧指标；在草案期调整权重（旧封存口径保持不变）
    svc.adjust_metric(manager, new_cal, "SK1", "高级工以上培养比例", DIMENSION_SKILL, "人社厅", 0.40)
    svc.adjust_metric(manager, new_cal, "RS1", "应用技术研究项目", DIMENSION_RESEARCH, "科技厅", 0.30)
    print("更正口径权重：技能 40% / 地方服务 30% / 基础研究 30%（原口径 45/30/25 不变）")
    svc.submit_for_signing(manager, new_cal)
    svc.sign_caliber(auditor, new_cal)
    svc.sign_caliber(manager_b, new_cal)
    svc.effectivate_caliber(manager, new_cal)
    svc.seal_caliber(manager, new_cal)
    svc.open_correction_record(manager, "R1", new_cal, "R2")
    svc.record_value(filer, "R2", "SK1", 88.0, "EV-SK1-001")
    svc.record_value(filer, "R2", "SV1", 92.0, "EV-SV1-007")
    svc.record_value(filer, "R2", "RS1", 95.0, "EV-RS1-009", note="凭证补齐")
    svc.publish_record(manager, "R2")
    print("更正结果 R2 公布，原结果 R1 保留为历史，榜单只列现行版本")

    # 6) 审计复算 -----------------------------------------------------------
    section("6. 审计：2025 年度复算、封存核对、变更窗口归因")
    report = audit.replay_year(YEAR)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))

    section("封存快照核对（R1：以公布时刻数据复算）")
    print(json.dumps(audit.verify_record("R1"), ensure_ascii=False, indent=2))

    section("迟到数据 + 撤销签署窗口归因")
    print(json.dumps(audit.explain_window(0, seq_after_late), ensure_ascii=False, indent=2,
                     default=str))

    section("持久化：事件日志写入 data/events.jsonl，可随时重建复算")
    out = ROOT / "data" / "events.jsonl"
    out.parent.mkdir(exist_ok=True)
    store.save_jsonl(out)
    print(f"已写入 {out}（{len(store.all_events())} 个事件）")


if __name__ == "__main__":
    main()
