"""确定性评分：给定口径快照与数据清单，任何人都能复算出同一结果。"""
from __future__ import annotations

from typing import Any

from .models import Metric, MissingEvidence, ScoreEntry

# 浮点比较与权重归一容差
EPS = 1e-9


def compute_scores(
    metrics: dict[str, Metric],
    weights: dict[str, float],
    values_by_school: dict[str, dict[str, float]],
    schools: list[str],
) -> tuple[list[ScoreEntry], list[MissingEvidence]]:
    """加权平均。缺证据的指标不计入该校，权重在已到证据指标间再归一。

    返回按 (分数降序, 学校代码升序) 稳定排名的榜单与缺失证据清单。
    """
    missing: list[MissingEvidence] = []
    rows: list[ScoreEntry] = []
    for school in sorted(schools):
        vals = values_by_school.get(school, {})
        present: dict[str, float] = {}
        for code, w in weights.items():
            if code in vals and vals[code] is not None:
                present[code] = vals[code]
            else:
                missing.append(MissingEvidence(
                    school_code=school, metric_code=code,
                    reason="未在截止前取得有效佐证数据",
                    raw_value=vals.get(code),
                ))
        if present:
            total_w = sum(weights[c] for c in present)
            score = sum(weights[c] * float(present[c]) for c in present) / total_w
        else:
            score = 0.0
        rows.append(ScoreEntry(school_code=school, score=round(score, 6), rank=0))

    ordered = sorted(rows, key=lambda r: (-r.score, r.school_code))
    ranked = [
        ScoreEntry(school_code=r.school_code, score=r.score, rank=i + 1)
        for i, r in enumerate(ordered)
    ]
    missing.sort(key=lambda m: (m.school_code, m.metric_code))
    return ranked, missing


def entries_to_map(entries: list[ScoreEntry]) -> dict[str, ScoreEntry]:
    return {e.school_code: e for e in entries}


def diff_rankings(old: list[ScoreEntry], new: list[ScoreEntry]) -> list[dict[str, Any]]:
    """对比两版榜单，逐校给出分数与名次变化。"""
    a = entries_to_map(old)
    b = entries_to_map(new)
    out: list[dict[str, Any]] = []
    for code in sorted(set(a) | set(b)):
        o, n = a.get(code), b.get(code)
        out.append({
            "school_code": code,
            "old_score": o.score if o else None,
            "new_score": n.score if n else None,
            "score_delta": round(n.score - o.score, 6) if o and n else None,
            "old_rank": o.rank if o else None,
            "new_rank": n.rank if n else None,
            "rank_delta": (o.rank - n.rank) if o and n else None,
        })
    return out
