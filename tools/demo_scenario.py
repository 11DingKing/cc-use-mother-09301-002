"""端到端场景演示：在 JSONL 事件库上跑通一个年度的完整生命周期并输出审计报告。

用法：
    python3 tools/demo_scenario.py [事件库路径，默认 data/demo_events.jsonl]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from indicator_registry import (  # noqa: E402
    ApplicationService, EventStore, MetricKind, Repository,
    ROLE_AUDITOR, SchoolType, ValidityWindow,
)


def main(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    svc = ApplicationService(Repository(EventStore(str(path))))
    repo = svc.repo

    if fresh:
        # --- 主数据：三类办学定位、差异化指标与数据来源 ---
        repo.register_school("考核办", "SK1", "江北职业技术学院", SchoolType.SKILL)
        repo.register_school("考核办", "SK2", "岭南技工高等专科学校", SchoolType.SKILL)
        repo.register_metric("考核办", "SK-PRAC", "实训课时达标率", SchoolType.SKILL,
                             MetricKind.QUANTITATIVE, unit="%", source="教务管理系统导出")
        repo.register_metric("考核办", "SK-CERT", "职业资格证书获取率", SchoolType.SKILL,
                             MetricKind.QUANTITATIVE, unit="%", source="人社部门数据接口")

        # --- 口径 v1：两人会签生效，生效区间 2026-2029 ---
        c1 = repo.create_caliber(
            "考核办", SchoolType.SKILL, 2026,
            {"SK-PRAC": 0.6, "SK-CERT": 0.4},
            ValidityWindow(2026, 2030),
            required_signers=2)
        repo.start_cosign("考核办", c1["id"])
        repo.sign_caliber("会签人-张", c1["id"], "会签人-张")
        repo.sign_caliber("会签人-李", c1["id"], "会签人-李")
        repo.publish_caliber("考核办", c1["id"])

        # --- 截止前填报；SK2 证书数据缺佐证 ---
        repo.submit_data("填报员", 2026, "SK1", "SK-PRAC", 90.0, "EV-2026-001", event_id="f1")
        repo.submit_data("填报员", 2026, "SK1", "SK-CERT", 80.0, "EV-2026-002", event_id="f2")
        repo.submit_data("填报员", 2026, "SK2", "SK-PRAC", 70.0, "EV-2026-003", event_id="f3")
        repo.submit_data("填报员", 2026, "SK2", "SK-CERT", None, "", event_id="f4")

        # --- 首次封存 ---
        repo.seal_publication("考核办", c1["id"], timestamp="2027-03-01T00:00:00+00:00")
        # 迟到数据：截止后才补齐佐证
        repo.submit_data("填报员", 2026, "SK2", "SK-CERT", 95.0, "EV-2026-099",
                         event_id="f5", timestamp="2027-03-12T09:30:00+00:00")

        # --- 口径 v2：提高实训权重，会签中一人撤销后改签，再替代 v1 生效 ---
        c2 = repo.create_caliber(
            "考核办", SchoolType.SKILL, 2026,
            {"SK-PRAC": 0.8, "SK-CERT": 0.2},
            ValidityWindow(2026, 2030),
            required_signers=2)
        repo.start_cosign("考核办", c2["id"])
        repo.sign_caliber("会签人-张", c2["id"], "会签人-张")
        repo.sign_caliber("会签人-王", c2["id"], "会签人-王")
        repo.revoke_sign("会签人-王", c2["id"], "会签人-王", reason="与该校有合作，主动回避")
        repo.sign_caliber("会签人-赵", c2["id"], "会签人-赵")
        repo.publish_caliber("考核办", c2["id"], supersedes=c1["id"])

        pubs = repo._publications_for(2026, SchoolType.SKILL)
        repo.correct_publication(
            "考核办", pubs[0]["id"], c2["id"],
            "迟到佐证补齐；技能人才培养提高实训课时权重",
            txn="annual-correction-2026")

    audit = svc.audit_view(ROLE_AUDITOR)
    report = {
        "存储": str(path),
        "事件总数": len(svc.repo.store.events),
        "哈希链": audit.verify_chain(),
        "口径清单": audit.list_calibers(SchoolType.SKILL),
        "发布历史": audit.publication_history(2026, SchoolType.SKILL),
        "逐版复算": audit.recompute_year(2026, SchoolType.SKILL),
        "变更解释": audit.explain_changes(2026, SchoolType.SKILL),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "demo_events.jsonl"
    main(target)
