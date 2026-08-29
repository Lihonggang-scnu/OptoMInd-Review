"""Targeted OA expansion for under-covered chapter literature roles.

This is deliberately separate from claim-level M3. It retrieves papers because
a chapter needs a foundation, mechanism, method, frontier, controversy, or
application landscape. Retrieved papers become KB material; they do not become
proof of a precise sentence merely by being relevant to the chapter.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from optomind_research.gap_oa_expander import GapOAEvidenceExpander, safe_slug
from optomind_research.m3_kb_ingest import KBIngester
from optomind_research.section_literature_coverage import (
    SectionLiteratureCoverageExpander,
    coverage_candidate_chunks,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RERANK_PROMPT = PROJECT_ROOT / "prompts" / "Section Coverage OA Reranker.txt"

ROLE_TO_EVIDENCE_TYPE = {
    "foundation": "mechanism",
    "mechanism": "mechanism",
    "method": "comparison",
    "frontier": "application",
    "controversy": "comparison",
    "application": "application",
}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def expand_section_coverage_gaps_oa(
    blueprint: dict[str, Any],
    *,
    kb_sqlite: Path | str | None,
    output_dir: Path,
    max_gaps: int = 6,
    results_per_backend: int = 10,
    download_top_n: int = 2,
    progress_callback: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fill the highest-value chapter-role gaps through legal OA routes."""
    bp = copy.deepcopy(blueprint)
    sqlite_path = Path(kb_sqlite) if kb_sqlite else None
    report: dict[str, Any] = {
        "schema_version": "section_coverage_oa_expansion.v1",
        "mode": "chapter_role_targeted_oa",
        "settings": {
            "max_gaps": max(0, int(max_gaps)),
            "results_per_backend": max(1, int(results_per_backend)),
            "download_top_n": max(0, int(download_top_n)),
        },
        "gaps_considered": 0,
        "gaps_processed": 0,
        "downloads_succeeded": 0,
        "kb_new_chunks": 0,
        "records": [],
        "status": "not_started",
    }
    if sqlite_path is None or not sqlite_path.exists():
        report["status"] = "kb_unavailable"
        return bp, report

    queue: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for section in bp.get("sections") or []:
        coverage = section.get("literature_coverage") or {}
        for gap in coverage.get("coverage_gaps") or []:
            if not isinstance(gap, dict):
                continue
            queue.append((section, gap))
    queue.sort(key=lambda pair: (
        0 if bool(pair[1].get("blocking")) else 1,
        -int(pair[1].get("missing_papers") or 0),
        str(pair[0].get("section_id") or ""),
    ))
    report["gaps_considered"] = len(queue)
    queue = queue[: max(0, int(max_gaps))]
    if not queue:
        report["status"] = "no_undercovered_roles"
        return bp, report

    expander = GapOAEvidenceExpander(
        max_queries=3,
        results_per_backend=results_per_backend,
        use_openalex=True,
        use_semantic_scholar=True,
        use_unpaywall=True,
        real_llm_queries=True,
        query_model_tier="advanced_model",
        real_llm_rerank=True,
        rerank_model_tier="premium_model",
        rerank_prompt_path=RERANK_PROMPT,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    impacted_sections: set[str] = set()
    for index, (section, gap) in enumerate(queue, start=1):
        section_id = str(section.get("section_id") or "")
        role = str(gap.get("role") or "mechanism")
        request_id = f"{section_id}-LIT-{role}"
        intended = str(gap.get("intended_synthesis") or "")
        question = str(gap.get("coverage_question") or "")
        planned_queries = [str(row) for row in (gap.get("queries") or []) if str(row).strip()]
        claim = {
            "claim_id": request_id,
            "statement": " ".join(value for value in (question, intended) if value).strip(),
            "evidence_type": ROLE_TO_EVIDENCE_TYPE.get(role, "mechanism"),
            "saturation_score": 0.0,
            "coverage_role": role,
            "planned_queries": planned_queries,
        }
        retrieval_section = dict(section)
        retrieval_section["_topic_context"] = " ".join([
            str(section.get("title") or ""),
            str(section.get("argument_role") or ""),
            " ".join(planned_queries),
        ])
        request_dir = output_dir / safe_slug(request_id, limit=64)
        if progress_callback is not None:
            progress_callback("chapter_coverage_gap_started", {
                "index": index, "count": len(queue), "request_id": request_id,
            })
        record: dict[str, Any] = {
            "request_id": request_id,
            "section_id": section_id,
            "role": role,
            "priority": gap.get("priority"),
            "planned_queries": planned_queries,
            "status": "started",
        }
        try:
            result = expander.expand_claim(
                claim,
                retrieval_section,
                top_k=max(12, int(gap.get("missing_papers") or 1) * 5),
                # Download only after the chapter scope/role audit below.
                download_top_n=0,
                download_dir=request_dir / "downloads",
                metadata_db=output_dir / "section_coverage_metadata.sqlite",
                citation_chase_top_n=2 if role == "foundation" else 0,
                references_per_seed=8,
            )
            _write_json(request_dir / "coverage_oa_report.json", result)
            raw_selected = [
                {**row, "coverage_role": role}
                for row in (result.get("selected_oa_candidates") or [])
                if isinstance(row, dict)
                and str(row.get("llm_scope_fit") or "") == "in_domain"
                and str(row.get("llm_retrieval_role") or "") == "evidence_candidate"
            ]
            predownload_scope_agent = SectionLiteratureCoverageExpander(
                kb_path=sqlite_path,
                real_llm=True,
            )
            candidate_scope_audit = predownload_scope_agent.audit_external_candidates(
                section,
                (section.get("literature_coverage") or {}).get("plan") or {},
                raw_selected,
            )
            kept_candidate_ids = {
                str(row.get("paper_id") or "")
                for row in (candidate_scope_audit.get("decisions") or [])
                if (
                    isinstance(row, dict)
                    and bool(row.get("keep"))
                    # Download is state-changing and potentially expensive.
                    # If the auditor is unavailable, preserve a visible gap
                    # instead of silently ingesting an unreviewed paper.
                    and str(row.get("scope_fit") or "") in {"direct", "adjacent"}
                    and str(row.get("decision_mode") or "") == "llm_scope_and_role_audit"
                    and role in (row.get("role_fit") or [])
                )
            }
            selected = [
                row for row in raw_selected
                if str(row.get("candidate_id") or "") in kept_candidate_ids
            ]
            ingest = None
            if selected and download_top_n > 0:
                ingest = KBIngester(
                    kb_sqlite=sqlite_path,
                    download_dir=request_dir / "kb_downloads",
                    require_scope_audit=True,
                ).ingest_oa_candidates(
                    selected,
                    claim,
                    max_successes=download_top_n,
                )
            ingest_dict = ingest.to_dict() if ingest is not None else {}
            stats = ingest_dict.get("stats") or {}
            new_chunks = list(ingest_dict.get("new_chunk_ids") or [])
            record.update({
                "status": "kb_ingested" if new_chunks else "candidates_only" if selected else "no_usable_candidate",
                "backend_stats": result.get("backend_stats") or {},
                "candidate_stats": result.get("candidate_stats") or {},
                "predownload_scope_audit": candidate_scope_audit,
                "selected_candidates": [
                    {key: row.get(key) for key in ("candidate_id", "title", "doi", "year", "venue", "download_status")}
                    for row in selected
                ],
                "download_summary": {
                    "enabled": bool(download_top_n),
                    "downloaded": int(stats.get("downloaded") or 0),
                    "source": "post_scope_audit_kb_ingest",
                },
                "kb_ingest": ingest_dict,
            })
            report["downloads_succeeded"] += int(stats.get("downloaded") or 0)
            report["kb_new_chunks"] += len(new_chunks)
            if selected:
                impacted_sections.add(section_id)
        except Exception as exc:
            record.update({
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            })
        report["records"].append(record)
        report["gaps_processed"] += 1
        if progress_callback is not None:
            progress_callback("chapter_coverage_gap_completed", {
                "request_id": request_id, "status": record["status"],
            })

    # Re-query the canonical KB with the original role plan. This keeps the
    # chapter landscape deterministic and ensures downstream writers receive
    # canonical chunk IDs rather than transient API records.
    local_expander = SectionLiteratureCoverageExpander(
        kb_path=sqlite_path,
        real_llm=True,
        max_papers_per_role=5,
    )
    for section in bp.get("sections") or []:
        section_id = str(section.get("section_id") or "")
        if section_id not in impacted_sections:
            continue
        old_coverage = section.get("literature_coverage") or {}
        refreshed = local_expander.expand(
            section,
            plan_override=old_coverage.get("plan") or {},
        )
        refreshed["external_expansion_history"] = [
            row for row in report["records"] if row.get("section_id") == section_id
        ]
        section["literature_coverage"] = refreshed
        existing = {
            str(row.get("chunk_id") or ""): row
            for row in (section.get("candidate_text_chunks") or [])
            if isinstance(row, dict) and row.get("chunk_id")
        }
        for row in coverage_candidate_chunks(refreshed):
            if row.get("chunk_id"):
                existing[str(row["chunk_id"])] = row
        section["candidate_text_chunks"] = list(existing.values())[:80]
        section["candidate_text_chunk_ids"] = list(existing)[:80]
    report["status"] = "completed"
    _write_json(output_dir / "section_coverage_oa_expansion_report.json", report)
    return bp, report
