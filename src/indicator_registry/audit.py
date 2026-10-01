"""读模型与审计复算：任一年度可复算，并解释三类变更来源。"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .models import CaliberStatus, ScoreEntry, SchoolType
from .scoring import compute_scores, diff_rankings
from .store import canonical_manifest, GENESIS

from .repository import Repository


def _row(r: dict) -> ScoreEntry:
    return ScoreEntry(school_code=r["school_code"], score=r["score"], rank=r["rank"])


def build_manifest(pub: dict, old_pub: dict | None = None) -> str:
    """按封存时的同一规则重建清单指纹。"""
    items: list[dict[str, Any]] = []
    if old_pub is None:
        items += [
            {"kind": "caliber", "caliber_id": pub["caliber_id"], "version": pub["version"],
             "weights": pub["weight_snapshot"],
             "window": _caliber_window(pub)},
        ]
    else:
        items += [
            {"kind": "caliber", "caliber_id": pub["caliber_id"], "version": pub["version"],
             "weights": pub["weight_snapshot"]},
        ]
    items += [
        {"kind": "cutoff", "cutoff": pub["cutoff"]},
        {"kind": "schools", "schools": pub["schools_snapshot"]},
        {"kind": "scores", "scores": pub["scores"]},
        {"kind": "missing", "missing": pub["missing"]},
        {"kind": "values", "values": pub["values_snapshot"]},
    ]
    if old_pub is not None:
        items.append({"kind": "corrects", "publication_id": old_pub["id"],
                      "old_manifest": old_pub["manifest"]})
    return canonical_manifest(items)


def _caliber_window(pub: dict) -> dict:
    # 首次封存时窗口信息只存在口径实体上；审计复算以权重/数据快照为准，
    # 窗口通过事件流里的 CALIBER_CREATED 重建（由调用方注入）。
    return pub.get("_window", {"start_year": pub["year"], "end_year": None})


class AuditService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    # ------------------------------------------------------------- 读模型

    def list_calibers(self, school_type: SchoolType | None = None) -> list[dict]:
        out = []
        for c in self.repo.calibers.values():
            if school_type and c["school_type"] is not school_type:
                continue
            out.append({
                "id": c["id"], "school_type": c["school_type"].value, "year": c["year"],
                "version": c["version"], "status": c["status"].value,
                "weights": c["weights"],
                "window": {"start_year": c["window"].start_year, "end_year": c["window"].end_year},
                "required_signers": c["required_signers"],
                "signers": self.repo._valid_signers(c["id"]),
                "revoked_signers": c["revoked_signers"],
            })
        return sorted(out, key=lambda x: (x["school_type"], x["year"], x["version"]))

    def publication_history(self, year: int, school_type: SchoolType) -> list[dict]:
        return [self._publication_view(p) for p in self.repo._publications_for(year, school_type)]

    def _publication_view(self, pub: dict) -> dict:
        return {
            "id": pub["id"], "year": pub["year"],
            "school_type": pub["school_type"].value, "caliber_id": pub["caliber_id"],
            "version": pub["version"], "status": pub["status"], "cutoff": pub["cutoff"],
            "manifest": pub["manifest"], "correction_of": pub.get("correction_of"),
            "scores": pub["scores"], "missing_count": len(pub["missing"]),
            "missing": pub["missing"],
        }

    # ----------------------------------------------------- 哈希链与复算

    def verify_chain(self) -> dict:
        events = self.repo.store.events
        self.repo.store.verify()
        return {"events": len(events),
                "head": self.repo.store.head_hash,
                "genesis": GENESIS,
                "intact": True}

    def recompute(self, publication_id: str) -> dict:
        """用封存快照重算分数并复核清单指纹，审计人员可独立复现。"""
        pub = self.repo.publications.get(publication_id)
        if not pub:
            raise KeyError(publication_id)
        ranked, missing = compute_scores(
            {}, pub["weight_snapshot"], pub["values_snapshot"], pub["schools_snapshot"])
        recomputed_scores = [asdict(r) for r in ranked]
        recomputed_missing = [asdict(m) for m in missing]
        scores_match = recomputed_scores == pub["scores"]
        missing_match = recomputed_missing == pub["missing"]

        old_pub = self.repo.publications.get(pub["correction_of"]) if pub.get("correction_of") else None
        rebuilt = build_manifest(self._manifest_view(pub), old_pub)
        return {
            "publication_id": publication_id,
            "scores_match": scores_match,
            "missing_match": missing_match,
            "manifest_match": rebuilt == pub["manifest"],
            "manifest_stored": pub["manifest"],
            "manifest_rebuilt": rebuilt,
            "recomputed_scores": recomputed_scores,
            "stored_scores": pub["scores"],
            "recomputed_missing": recomputed_missing,
        }

    def _manifest_view(self, pub: dict) -> dict:
        view = dict(pub)
        cal = self.repo.calibers[pub["caliber_id"]]
        view["_window"] = {"start_year": cal["window"].start_year,
                           "end_year": cal["window"].end_year}
        return view

    def recompute_year(self, year: int, school_type: SchoolType) -> list[dict]:
        return [self.recompute(p["id"]) for p in
                self.repo._publications_for(year, school_type)]

    # --------------------------------------- 变更解释：迟到/撤销/并发

    def explain_changes(self, year: int, school_type: SchoolType) -> dict:
        pubs = self.repo._publications_for(year, school_type)
        corrections: list[dict] = []
        for corr in self.repo.corrections:
            old = self.repo.publications[corr["old_publication_id"]]
            if old["year"] != year or old["school_type"] is not school_type:
                continue
            new = self.repo.publications[corr["new_publication_id"]]
            corrections.append(self._explain_one(old, new, corr))
        blocked = [
            {"seq": e.seq, "timestamp": e.timestamp, "actor": e.actor,
             "reason": e.payload["reason"],
             "caliber_id": e.payload.get("caliber_id"),
             "existing_publication_id": e.payload.get("existing_publication_id")}
            for e in self.repo.store.events
            if e.type == "PUBLICATION_BLOCKED"
            and e.payload["year"] == year
            and e.payload["school_type"] == school_type.value
        ]
        return {
            "year": year, "school_type": school_type.value,
            "publication_versions": [{"id": p["id"], "version": p["version"],
                                      "status": p["status"],
                                      "caliber_id": p["caliber_id"],
                                      "cutoff": p["cutoff"]} for p in pubs],
            "concurrent_publish_blocks": blocked,
            "corrections": corrections,
        }

    def _explain_one(self, old: dict, new: dict, corr: dict) -> dict:
        late_detail = []
        for sid in corr["late_submission_ids"]:
            sub = self.repo.submissions.get(sid)
            if not sub:
                continue
            late_detail.append({
                "submission_id": sid, "school_code": sub["school_code"],
                "metric_code": sub["metric_code"], "value": sub["value"],
                "submitted_at": sub["timestamp"], "sealed_cutoff": old["cutoff"],
            })

        # 迟到数据的独立影响：新口径权重下，纳入 vs 剔除迟到数据
        # （反事实快照按"截止前数据"规则重建，迟到提交整笔剔除）。
        values_with_late = new["values_snapshot"]
        values_without_late, _, _ = self.repo._snapshot_inputs(
            new["year"], new["school_type"], new["seq"], include_late=False)
        with_late, _ = compute_scores(
            {}, new["weight_snapshot"], values_with_late, new["schools_snapshot"])
        without_late, _ = compute_scores(
            {}, new["weight_snapshot"], values_without_late, new["schools_snapshot"])
        late_effect = [d for d in diff_rankings(without_late, with_late)
                       if d["score_delta"] not in (None, 0) or d["rank_delta"] not in (None, 0)]

        revoked = []
        for signer in corr["revoked_signers"]:
            recs = [rec for (cid, s), rec in self.repo.signs.items()
                    if s == signer and rec["revoked"]
                    and rec["revoked_seq"] is not None
                    and old["seq"] <= rec["revoked_seq"] <= new["seq"]]
            for rec in recs:
                revoked.append({"signer": signer, "caliber_id": rec["caliber_id"],
                                "revoked_seq": rec["revoked_seq"]})

        return {
            "old_publication_id": old["id"], "new_publication_id": new["id"],
            "new_caliber_id": corr["new_caliber_id"], "reason": corr["reason"],
            "overall_ranking_diff": corr["diff"],
            "late_data": {"count": len(late_detail), "items": late_detail,
                          "independent_score_effect": late_effect},
            "revoked_signatures": revoked,
            "note": ("迟到数据按更正时点快照纳入；撤销签署不回改已封存版本，"
                     "新口径按其自身会签法定人数生效；旧版本完整保留为已更正。"),
        }
