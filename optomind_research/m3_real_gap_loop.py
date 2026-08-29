"""M2.5/M3-real bridge: run targeted OA gap expansion for weak claims.

This module does not convert newly retrieved papers into final evidence chunks.
It creates traceable OA candidate packages that can be handed to the canonical
full-text acquisition and ReviewKnowledgeBase update pipeline.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from optomind_research.gap_oa_expander import GapOAEvidenceExpander, normalize_doi, safe_slug
from optomind_research.domain_config_loader import load_domain_config, get_m3_defaults, get_topic_context
from optomind_research.m3_kb_ingest import KBIngester
from optomind_research.m3_gap_classifier import classify_gap


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "m3_real_gap_loop"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compact(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def classification_retrieval_ready(classification: dict[str, Any]) -> bool:
    """Return the single, explicit paper-retrieval routing decision.

    ``implementation_status`` describes the complete route and may truthfully
    say that extraction or comparison is still pending.  It must not suppress
    a retrieval step that is already implemented.
    """
    explicit = classification.get("retrieval_ready")
    if explicit is not None:
        return bool(explicit)
    return bool(
        classification.get("gap_type")
        in {"retrievable", "direct_retrievable", "mechanism_component"}
        or classification.get("action") in {"retrieve", "retrieve_mechanism"}
    )


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_ingested_chunk_rows(sqlite_path: Path, chunk_ids: list[str]) -> list[dict[str, Any]]:
    """Load newly ingested chunks in the planner's candidate-anchor shape."""
    ids = list(dict.fromkeys(str(x) for x in chunk_ids if x))
    if not ids or not Path(sqlite_path).exists():
        return []
    con = sqlite3.connect(str(sqlite_path))
    con.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = con.execute(
            f"""SELECT t.chunk_id,t.paper_id,t.doi,t.title,t.section_path,t.text,t.search_text,
                       t.evidence_level,t.source_kind,t.provenance_json,p.year AS publication_year
                FROM text_chunks t LEFT JOIN papers p ON p.paper_id=t.paper_id
                WHERE t.chunk_id IN ({placeholders})""",
            ids,
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "chunk_id": row["chunk_id"],
            "paper_id": row["paper_id"],
            "source_paper_id": row["paper_id"],
            "doi": row["doi"],
            "title": row["title"],
            "section_path": row["section_path"],
            "text_preview": compact(row["text"] or row["search_text"], 1800),
            "publication_year": row["publication_year"],
            "evidence_level": row["evidence_level"] or "fulltext",
            "source_kind": row["source_kind"] or "fulltext",
            "provenance": json.loads(row["provenance_json"] or "{}") if row["provenance_json"] else {},
            "retrieval_query": "m3_real_gap_ingest",
        }
        for row in rows
    ]


def load_supporting_dois(sqlite_path: Path | None, chunk_ids: list[str]) -> set[str]:
    """Resolve DOI identities already supporting a claim from the canonical KB.

    Filesystem-safe chunk IDs cannot be losslessly converted back into DOI
    strings.  Resolving through SQLite lets external M3 avoid downloading the
    same paper again; already available full text belongs to the internal-search
    route, while external retrieval should preferentially add independent work.
    """
    ids = list(dict.fromkeys(str(value) for value in chunk_ids if value))
    if sqlite_path is None or not ids or not Path(sqlite_path).exists():
        return set()
    connection = sqlite3.connect(str(sqlite_path))
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT DISTINCT doi FROM text_chunks "
            f"WHERE chunk_id IN ({placeholders}) AND doi IS NOT NULL AND doi != ''",
            ids,
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        connection.close()
    return {
        normalized
        for row in rows
        if row and (normalized := normalize_doi(str(row[0] or "")))
    }


def iter_claims(blueprint: dict[str, Any]):
    for section in blueprint.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for claim in section.get("claims") or []:
            if isinstance(claim, dict):
                yield section, claim


def collect_low_saturation_claims(
    blueprint: dict[str, Any],
    *,
    threshold: float = 1.5,
    max_claims: int = 3,
    claim_ids: list[str] | set[str] | tuple[str, ...] | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return claims eligible for M3 retrieval.

    ``claim_ids`` is optional for backwards compatibility.  When supplied it
    constrains selection to an already established target set; it is not a
    second opportunity to fill the batch from the rest of the blueprint.
    Claims whose readiness gate says ``proceed`` are complete even when their
    numeric saturation score is still below ``threshold``.
    """
    target_ids = None if claim_ids is None else {str(value) for value in claim_ids}
    rows: list[tuple[float, int, dict[str, Any], dict[str, Any]]] = []
    for section, claim in iter_claims(blueprint):
        if target_ids is not None and str(claim.get("claim_id", "")) not in target_ids:
            continue
        try:
            sat = float(claim.get("saturation_score", 0.0))
        except Exception:
            sat = 0.0
        readiness = claim.get("_readiness_check") if isinstance(claim.get("_readiness_check"), dict) else {}
        readiness_action = str(readiness.get("action") or "")
        if readiness_action == "proceed":
            continue
        if sat >= threshold and readiness_action not in {"block", "supplement"}:
            continue
        effective_sat = min(sat, 0.9 if readiness_action == "block" else 1.3 if readiness_action == "supplement" else sat)
        load_bearing_rank = 0 if claim.get("load_bearing") else 1
        rows.append((effective_sat, load_bearing_rank, section, claim))
    rows.sort(key=lambda x: (x[0], x[1], str(x[3].get("claim_id", ""))))
    return [(section, claim) for _, _, section, claim in rows[: max(0, max_claims)]]


def select_m3_target_claim_ids(
    blueprint: dict[str, Any],
    *,
    threshold: float = 1.5,
    max_claims: int = 3,
) -> list[str]:
    """Freeze the initial M3 target claim IDs for one run.

    This helper is intentionally small so callers/tests can verify targeting
    without constructing the OA expander or performing a retrieval.
    """
    selected = collect_low_saturation_claims(
        blueprint,
        threshold=threshold,
        max_claims=max_claims,
    )
    return list(dict.fromkeys(str(claim.get("claim_id", "")) for _, claim in selected))


def collect_m3_target_claims(
    blueprint: dict[str, Any],
    target_claim_ids: list[str] | tuple[str, ...] | set[str],
    *,
    threshold: float = 1.5,
    completed_claim_ids: set[str] | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return only still-active claims from the frozen target queue."""
    target_ids = list(dict.fromkeys(str(value) for value in target_claim_ids))
    completed = {str(value) for value in (completed_claim_ids or set())}
    active_ids = [claim_id for claim_id in target_ids if claim_id not in completed]
    return collect_low_saturation_claims(
        blueprint,
        threshold=threshold,
        max_claims=len(active_ids),
        claim_ids=active_ids,
    )


def topic_context_from_blueprint(blueprint: dict[str, Any], override: str = "") -> str:
    if override:
        return override
    ctx = blueprint.get("input_context") if isinstance(blueprint.get("input_context"), dict) else {}
    parts = [
        ctx.get("user_question", ""),
        ctx.get("problem_understanding", ""),
        ctx.get("scope_definition", ""),
        blueprint.get("review_thesis", ""),
    ]
    mentor = blueprint.get("review_mentor_advice") if isinstance(blueprint.get("review_mentor_advice"), dict) else {}
    parts.append(mentor.get("mentor_summary", ""))
    return compact(" ".join(str(x or "") for x in parts), 1500)


def run_m3_real_gap_loop(
    blueprint: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    metadata_db: Path | None = None,
    max_rounds: int = 1,
    max_claims: int | None = None,
    target_claim_ids: list[str] | tuple[str, ...] | None = None,
    saturation_threshold: float | None = None,
    max_queries: int | None = None,
    results_per_backend: int | None = None,
    top_k: int | None = None,
    from_year: int | None = None,
    download_top_n: int = 0,
    citation_chase_top_n: int = 0,
    references_per_seed: int | None = None,
    topic_context: str = "",
    use_openalex: bool = True,
    use_semantic_scholar: bool = True,
    use_unpaywall: bool = True,
    domain_config_path: Path | None = None,
    kb_sqlite: Path | None = None,
    adaptive_closure: bool = False,
    progress_callback: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run targeted OA expansion for low-saturation claims.

    Parameters without explicit values fall back to domain_config.yaml defaults.
    kb_sqlite: if provided, newly retrieved chunks are ingested into the KB (P1 回流).

    Returns a copied blueprint with per-claim M3 package metadata plus a report.
    """
    # ── 从 domain_config 读取默认值 ─────────────────────────
    _cfg = load_domain_config(domain_config_path)
    _m3 = get_m3_defaults(_cfg)
    if saturation_threshold is None:
        saturation_threshold = _m3["saturation_threshold"]
    if max_claims is None:
        max_claims = _m3["max_claims_per_loop"]
    if max_queries is None:
        max_queries = _m3["max_queries"]
    if results_per_backend is None:
        results_per_backend = _m3["results_per_backend"]
    if top_k is None:
        top_k = _m3["top_k"]
    if from_year is None:
        from_year = _m3["from_year"]
    if references_per_seed is None:
        references_per_seed = _m3["references_per_seed"]
    bp = deepcopy(blueprint)
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_db = metadata_db or (output_dir / "m3_real_gap_metadata.sqlite")
    # The live user/blueprint context outranks the repository's example domain
    # config.  This prevents a new optical topic from collapsing back to the
    # radiative-cooling test domain when no per-run config was supplied.
    context = (
        compact(topic_context, 1500)
        or topic_context_from_blueprint(bp, "")
        or compact(_m3["topic_context"], 1500)
    )

    expander = GapOAEvidenceExpander(
        max_queries=max_queries,
        results_per_backend=results_per_backend,
        use_openalex=use_openalex,
        use_semantic_scholar=use_semantic_scholar,
        use_unpaywall=use_unpaywall,
        from_year=from_year,
        query_boost_terms=_m3.get("query_boost_terms", []),
        real_llm_queries=True,
        query_model_tier="advanced_model",
    )
    available_claim_ids = {
        str(claim.get("claim_id", ""))
        for _, claim in iter_claims(bp)
        if str(claim.get("claim_id", ""))
    }
    if target_claim_ids is not None:
        target_claim_ids = list(
            dict.fromkeys(
                str(claim_id)
                for claim_id in target_claim_ids
                if str(claim_id) in available_claim_ids
            )
        )
        targeting_mode = "explicit"
    else:
        target_claim_ids = select_m3_target_claim_ids(
            bp,
            threshold=saturation_threshold,
            max_claims=max_claims,
        )
        targeting_mode = "automatic"

    # U3: Gap classification tracking
    gap_classifications: dict[str, dict[str, Any]] = {}
    skipped_claims: list[dict[str, Any]] = []

    report: dict[str, Any] = {
        "schema_version": "m3_real_gap_loop.v1",
        "created_at": utc_now(),
        "mode": "oa_only_targeted_gap_loop",
        "settings": {
            "max_rounds": max_rounds,
            "max_claims": max_claims,
            "saturation_threshold": saturation_threshold,
            "max_queries": max_queries,
            "results_per_backend": results_per_backend,
            "top_k": top_k,
            "from_year": from_year,
            "download_top_n": download_top_n,
            "citation_chase_top_n": citation_chase_top_n,
            "references_per_seed": references_per_seed,
        },
        # ``selected_claim_ids`` is retained for report compatibility; the
        # explicit target name documents that this queue is immutable per run.
        "selected_claim_ids": target_claim_ids,
        "target_claim_ids": target_claim_ids,
        "targeting_mode": targeting_mode,
        "round_reports": [],
        "summary": {
            "claims_processed": 0,
            "candidate_packages": 0,
            "selected_candidates": 0,
            "downloaded": 0,
            "abstract_fallback_candidates": 0,
            "abstract_chunks_written": 0,
            "abstract_fallback_skipped_reasons": {},
            "metadata_db": str(metadata_db),
            "kb_feedback_enabled": bool(kb_sqlite),
            "adaptive_closure_enabled": bool(adaptive_closure),
            "stop_reason": "",
        },
        "gap_classifications": gap_classifications,
        "skipped_claims": skipped_claims,
    }

    progress_path = output_dir / "m3_real_gap_progress.json"

    def emit_progress(event: str, **details: Any) -> None:
        payload = {
            "schema_version": "m3_real_gap.progress.v1",
            "updated_at": utc_now(),
            "event": event,
            "details": details,
        }
        write_json(progress_path, payload)
        if progress_callback is not None:
            try:
                progress_callback(event, details)
            except Exception:
                # Observability must never abort evidence retrieval.
                pass

    emit_progress(
        "targets_selected",
        target_claim_count=len(target_claim_ids),
        target_claim_ids=list(target_claim_ids),
        max_rounds=retrieval_rounds if "retrieval_rounds" in locals() else max_rounds,
    )

    seen_dois_by_claim: dict[str, set[str]] = {}
    stagnant_claim_ids: set[str] = set()
    processed_claim_ids: set[str] = set()
    skipped_claim_ids: set[str] = set()
    # ``max_rounds=0`` is a legitimate closure-only mode.  It must not be
    # coerced to one retrieval round: callers use it to re-assess existing
    # evidence without spending search/download quota.
    retrieval_rounds = max(0, int(max_rounds))
    for round_idx in range(1, retrieval_rounds + 1):
        selected_claims = collect_m3_target_claims(
            bp,
            target_claim_ids,
            threshold=saturation_threshold,
            completed_claim_ids=skipped_claim_ids | stagnant_claim_ids,
        )
        if not selected_claims:
            report["summary"]["stop_reason"] = (
                "no_initial_targets" if not target_claim_ids
                else "all_initial_targets_completed"
            )
            break
        emit_progress(
            "round_started",
            round=round_idx,
            total_rounds=retrieval_rounds,
            active_claim_count=len(selected_claims),
        )
        round_packages_before = int(report["summary"]["candidate_packages"])
        for claim_index, (section, claim) in enumerate(selected_claims, start=1):
            claim_id = str(claim.get("claim_id", "claim"))
            emit_progress(
                "claim_started",
                round=round_idx,
                claim_index=claim_index,
                claim_count=len(selected_claims),
                claim_id=claim_id,
                section_id=str(section.get("section_id") or ""),
            )
            processed_claim_ids.add(claim_id)
            section["_topic_context"] = context
            seen = seen_dois_by_claim.setdefault(claim_id, set())
            preexisting_support_dois = load_supporting_dois(
                Path(kb_sqlite) if kb_sqlite is not None else None,
                list(claim.get("supporting_text_chunk_ids") or []),
            )
            seen.update(preexisting_support_dois)
            claim_dir = output_dir / f"round-{round_idx:02d}" / safe_slug(claim_id, limit=40)

            # ── U3: Gap Classification before M3 retrieval ──
            if claim_id not in gap_classifications:
                classification = classify_gap(
                    claim_text=claim.get("statement") or claim.get("claim_text", ""),
                    supporting_chunk_ids=claim.get("supporting_text_chunk_ids") or [],
                    claim_context=" | ".join(
                        x for x in (section.get("title", ""), section.get("argument_role", "")) if x
                    ),
                    existing_chunks_summary=f"{len(claim.get('supporting_text_chunk_ids') or [])} chunks",
                    force_mock=False,
                )
                gap_classifications[claim_id] = classification
                claim["_gap_classification"] = classification

            classification = gap_classifications[claim_id]
            # Skip retrieval for gap types that are not retrieval-ready:
            # frontier_unknown (flag) and structural_or_writing (return_to_m2a).
            retrieval_ready = classification_retrieval_ready(classification)
            # ``retrieval_ready`` is the only paper-retrieval gate.  Some routes
            # can collect useful papers even when their specialised downstream
            # extraction or comparison step remains pending.
            if not retrieval_ready:
                if claim_id not in skipped_claim_ids:
                    skipped_claims.append({
                        "claim_id": claim_id,
                        "gap_type": classification["gap_type"],
                        "reasoning": classification["reasoning"],
                        "action": classification["action"],
                        "implementation_status": classification.get("implementation_status", ""),
                    })
                    skipped_claim_ids.add(claim_id)
                emit_progress(
                    "claim_skipped",
                    round=round_idx,
                    claim_index=claim_index,
                    claim_count=len(selected_claims),
                    claim_id=claim_id,
                    gap_type=str(classification.get("gap_type") or ""),
                    reason="gap_not_retrieval_ready",
                )
                continue

            result = expander.expand_claim(
                claim,
                section,
                top_k=top_k,
                download_top_n=download_top_n,
                download_dir=claim_dir / "downloads",
                metadata_db=metadata_db,
                citation_chase_top_n=citation_chase_top_n,
                references_per_seed=references_per_seed,
                exclude_dois=seen,
            )
            write_json(claim_dir / "gap_oa_expansion_report.json", result)
            selected = result.get("selected_oa_candidates") or []
            for cand in selected:
                doi = normalize_doi(cand.get("doi", ""))
                if doi:
                    seen.add(doi)

            # ── P1 KB 回流：把 OA 候选解析写入 ReviewKnowledgeBase ──
            kb_ingest_result: dict[str, Any] = {}
            evidence_verifier_audit: dict[str, Any] = {}
            factual_selected = [
                candidate for candidate in selected
                if str(candidate.get("llm_scope_fit") or "") == "in_domain"
                and str(candidate.get("llm_retrieval_role") or "") == "evidence_candidate"
            ]
            if kb_sqlite is not None and factual_selected and download_top_n > 0:
                ingester = KBIngester(
                    kb_sqlite=kb_sqlite,
                    download_dir=claim_dir / "kb_downloads",
                    require_scope_audit=True,
                )
                ingest = ingester.ingest_oa_candidates(
                    factual_selected,
                    claim,
                    max_successes=download_top_n,
                )
                available_chunk_ids = list(dict.fromkeys(
                    list(ingest.factual_candidate_chunk_ids)
                ))
                previous_support_ids = set(claim.get("supporting_text_chunk_ids") or [])
                if available_chunk_ids:
                    existing_candidates = {
                        str(row.get("chunk_id")): row
                        for row in (section.get("candidate_text_chunks") or [])
                        if isinstance(row, dict) and row.get("chunk_id")
                    }
                    for row in load_ingested_chunk_rows(Path(kb_sqlite), available_chunk_ids):
                        existing_candidates[row["chunk_id"]] = row
                    priority_ids = list(dict.fromkeys(
                        list(claim.get("supporting_text_chunk_ids") or [])
                        + list(ingest.candidate_bound_chunk_ids)
                        + list(existing_candidates.keys())
                    ))
                    section["candidate_text_chunks"] = [
                        existing_candidates[cid] for cid in priority_ids if cid in existing_candidates
                    ]
                    section["candidate_text_chunk_ids"] = list(existing_candidates.keys())
                    try:
                        from optomind_research.claim_evidence_verifier import ClaimEvidenceVerifier
                        from optomind_research.claim_schema import Claim

                        verifier_section = dict(section)
                        verifier_section["candidate_text_chunks"] = section["candidate_text_chunks"][:16]
                        verifier_section["candidate_text_chunk_ids"] = [
                            row["chunk_id"] for row in verifier_section["candidate_text_chunks"]
                        ]
                        verifier = ClaimEvidenceVerifier(model_tier="premium_model")
                        verified = verifier.verify_and_bind(
                            [Claim.from_dict(claim)],
                            verifier_section,
                        )[0]
                        evidence_verifier_audit = verifier.last_audit
                        claim.update(verified.to_dict())
                        claim["saturation_updated_by_m3"] = bool(
                            set(claim.get("supporting_text_chunk_ids") or []) - previous_support_ids
                        )
                    except Exception as exc:
                        provisional = list(dict.fromkeys(
                            list(claim.get("supporting_text_chunk_ids") or [])
                            + list(ingest.candidate_bound_chunk_ids)
                        ))
                        claim["supporting_text_chunk_ids"] = provisional
                        claim["saturation_score"] = min(1.0, ingest.new_saturation_score)
                        claim["evidence_binding_status"] = "unverified"
                        claim["evidence_binding_confidence"] = "low"
                        claim.setdefault("critic_flags", []).append(
                            f"m3_evidence_verifier_error: {type(exc).__name__}"
                        )
                    try:
                        from optomind_research.evidence_readiness_gate import evaluate_evidence_readiness
                        support_rows = [
                            existing_candidates[cid]
                            for cid in claim.get("supporting_text_chunk_ids") or []
                            if cid in existing_candidates
                        ]
                        claim["_readiness_check"] = evaluate_evidence_readiness(
                            claim_text=claim.get("statement", ""),
                            supporting_chunks=support_rows,
                            claim_type=claim.get("evidence_type", ""),
                            binding_status=claim.get("evidence_binding_status", ""),
                            binding_confidence=claim.get("evidence_binding_confidence", ""),
                            missing_components=claim.get("missing_evidence_components") or [],
                            load_bearing=bool(claim.get("load_bearing")),
                        )
                    except Exception:
                        pass
                novel_support_ids = sorted(
                    set(claim.get("supporting_text_chunk_ids") or []) - previous_support_ids
                )
                if not ingest.new_chunk_ids and not novel_support_ids and round_idx >= 2:
                    stagnant_claim_ids.add(claim_id)
                kb_ingest_result = ingest.to_dict()
                kb_ingest_result["novel_support_chunk_ids"] = novel_support_ids
                kb_ingest_result["diminishing_returns_stop"] = claim_id in stagnant_claim_ids

            package = {
                "round": round_idx,
                "output_dir": str(claim_dir),
                "candidate_stats": result.get("candidate_stats", {}),
                "download_summary": result.get("download_summary", {}),
                "citation_chase": result.get("citation_chase", {}),
                "selected_candidate_ids": [c.get("candidate_id") for c in selected],
                "selected_dois": [c.get("doi") for c in selected if c.get("doi")],
                "excluded_preexisting_support_dois_count": len(preexisting_support_dois),
                "metadata_updates": result.get("metadata_updates", {}),
                "kb_ingest": kb_ingest_result,
                "method_transfer_candidates_quarantined": max(0, len(selected) - len(factual_selected)),
                "evidence_verifier_audit": evidence_verifier_audit,
                "status": (
                    "kb_ingested" if kb_ingest_result.get("new_chunk_ids")
                    else "kb_reused_no_new_support" if kb_ingest_result.get("diminishing_returns_stop")
                    else "kb_reused_with_new_support" if kb_ingest_result.get("reused_chunk_ids")
                    else "candidate_package_generated" if selected
                    else "no_oa_candidates"
                ),
            }
            claim.setdefault("m3_real_gap_packages", []).append(package)
            report["round_reports"].append(
                {
                    "round": round_idx,
                    "section_id": section.get("section_id", ""),
                    "claim_id": claim_id,
                    **package,
                }
            )
            report["summary"]["candidate_packages"] += 1
            report["summary"]["selected_candidates"] += len(selected)
            report["summary"]["downloaded"] += max(
                int((result.get("download_summary") or {}).get("downloaded") or 0),
                int((kb_ingest_result.get("stats") or {}).get("downloaded") or 0),
            )
            ingest_stats = kb_ingest_result.get("stats") or {}
            report["summary"]["abstract_fallback_candidates"] += int(
                ingest_stats.get("abstract_fallback_candidates") or 0
            )
            report["summary"]["abstract_chunks_written"] += int(
                ingest_stats.get("abstract_chunks_written") or 0
            )
            for reason, count in (ingest_stats.get("abstract_fallback_skipped_reasons") or {}).items():
                summary_reasons = report["summary"]["abstract_fallback_skipped_reasons"]
                summary_reasons[str(reason)] = int(summary_reasons.get(str(reason), 0)) + int(count or 0)
            write_json(output_dir / "m3_real_gap_loop_report.partial.json", report)
            emit_progress(
                "claim_completed",
                round=round_idx,
                claim_index=claim_index,
                claim_count=len(selected_claims),
                claim_id=claim_id,
                status=package["status"],
                selected_candidate_count=len(selected),
                downloaded_count=max(
                    int((result.get("download_summary") or {}).get("downloaded") or 0),
                    int((kb_ingest_result.get("stats") or {}).get("downloaded") or 0),
                ),
                novel_support_count=len(
                    (kb_ingest_result.get("novel_support_chunk_ids") or [])
                ),
            )

        # Re-evaluate only the frozen target queue.  In particular, a
        # readiness ``proceed`` decision completes a target immediately even
        # if its numeric saturation score remains below the threshold.
        remaining_targets = collect_m3_target_claims(
            bp,
            target_claim_ids,
            threshold=saturation_threshold,
            completed_claim_ids=skipped_claim_ids | stagnant_claim_ids,
        )
        if not remaining_targets:
            report["summary"]["stop_reason"] = "all_initial_targets_completed"
            break
        if int(report["summary"]["candidate_packages"]) == round_packages_before:
            report["summary"]["stop_reason"] = "round_produced_no_candidate_packages"
            break
        emit_progress(
            "round_completed",
            round=round_idx,
            total_rounds=retrieval_rounds,
            candidate_packages=int(report["summary"]["candidate_packages"]),
            downloaded=int(report["summary"]["downloaded"]),
        )
    report["summary"]["claims_processed"] = len(processed_claim_ids)
    report["summary"]["stagnant_claim_ids"] = sorted(stagnant_claim_ids)
    if not report["summary"]["stop_reason"]:
        report["summary"]["stop_reason"] = (
            "retrieval_skipped_by_configuration"
            if retrieval_rounds == 0
            else "max_rounds_reached"
        )
    if adaptive_closure:
        try:
            emit_progress("adaptive_closure_started", claim_count=len(target_claim_ids))
            from optomind_research.claim_adaptation_agent import adapt_m3_claims

            # Closure decisions need to know whether a gap is retrievable,
            # structural, normative, or a frontier unknown even when no
            # retrieval round was requested.  Populate only missing entries;
            # normal retrieval runs already classified their processed claims.
            target_set = set(target_claim_ids)
            for section, claim in iter_claims(bp):
                claim_id = str(claim.get("claim_id") or "")
                if claim_id not in target_set or claim_id in gap_classifications:
                    continue
                try:
                    gap_classifications[claim_id] = classify_gap(
                        claim_text=claim.get("statement") or claim.get("claim_text", ""),
                        supporting_chunk_ids=claim.get("supporting_text_chunk_ids") or [],
                        claim_context=" | ".join(
                            value
                            for value in (
                                section.get("title", ""),
                                section.get("argument_role", ""),
                            )
                            if value
                        ),
                        existing_chunks_summary=(
                            f"{len(claim.get('supporting_text_chunk_ids') or [])} chunks"
                        ),
                        force_mock=False,
                    )
                except Exception as exc:
                    gap_classifications[claim_id] = {
                        "gap_type": "frontier_unknown",
                        "action": "flag",
                        "retrieval_ready": False,
                        "implementation_status": "classification_failed",
                        "reasoning": (
                            "Gap classification failed before adaptive closure: "
                            f"{type(exc).__name__}."
                        ),
                    }
            closure_results = adapt_m3_claims(
                bp,
                target_claim_ids=list(target_claim_ids),
                gap_classifications=gap_classifications,
                round_reports=report["round_reports"],
                real_llm=True,
            )
            report["adaptive_closure"] = closure_results
            report["summary"]["adaptive_closure_count"] = len(closure_results)
            emit_progress("adaptive_closure_completed", decision_count=len(closure_results))
        except Exception as exc:
            report["adaptive_closure"] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
    write_json(output_dir / "m3_real_gap_loop_report.json", report)
    emit_progress(
        "completed",
        stop_reason=str(report["summary"].get("stop_reason") or ""),
        claims_processed=int(report["summary"].get("claims_processed") or 0),
        candidate_packages=int(report["summary"].get("candidate_packages") or 0),
        downloaded=int(report["summary"].get("downloaded") or 0),
    )
    return bp, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run multi-round M3-real OA gap loop on a blueprint.")
    parser.add_argument("--blueprint", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--metadata-db", type=Path, default=None)
    parser.add_argument("--max-rounds", type=int, default=1)
    parser.add_argument("--max-claims", type=int, default=3)
    parser.add_argument(
        "--claim-id",
        action="append",
        default=[],
        help="Target one claim ID explicitly; repeat this option for multiple claims.",
    )
    parser.add_argument("--saturation-threshold", type=float, default=1.5)
    parser.add_argument("--max-queries", type=int, default=3)
    parser.add_argument("--results-per-backend", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--from-year", type=int, default=2014)
    parser.add_argument("--download-top-n", type=int, default=0)
    parser.add_argument("--citation-chase-top-n", type=int, default=0)
    parser.add_argument("--references-per-seed", type=int, default=8)
    parser.add_argument("--topic-context", default="")
    parser.add_argument("--no-openalex", action="store_true")
    parser.add_argument("--no-semantic-scholar", action="store_true")
    parser.add_argument("--no-unpaywall", action="store_true")
    parser.add_argument("--kb-sqlite", type=Path, default=None,
                        help="ReviewKnowledgeBase SQLite path for P1 KB回流（可选）")
    parser.add_argument("--domain-config", type=Path, default=None,
                        help="domain_config.yaml 路径（默认：项目根目录）")
    parser.add_argument(
        "--adaptive-closure",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="After retrieval, let a top-tier model keep, narrow, reframe, or drop unresolved claims",
    )
    args = parser.parse_args(argv)

    blueprint = load_json(args.blueprint)
    updated, report = run_m3_real_gap_loop(
        blueprint,
        output_dir=args.output_dir,
        metadata_db=args.metadata_db,
        max_rounds=args.max_rounds,
        max_claims=args.max_claims if args.max_claims != 3 else None,
        target_claim_ids=args.claim_id or None,
        saturation_threshold=args.saturation_threshold if args.saturation_threshold != 1.5 else None,
        max_queries=args.max_queries if args.max_queries != 3 else None,
        results_per_backend=args.results_per_backend if args.results_per_backend != 5 else None,
        top_k=args.top_k if args.top_k != 5 else None,
        from_year=args.from_year or None,
        download_top_n=args.download_top_n,
        citation_chase_top_n=args.citation_chase_top_n,
        references_per_seed=args.references_per_seed if args.references_per_seed != 8 else None,
        topic_context=args.topic_context,
        use_openalex=not args.no_openalex,
        use_semantic_scholar=not args.no_semantic_scholar,
        use_unpaywall=not args.no_unpaywall,
        kb_sqlite=args.kb_sqlite,
        domain_config_path=args.domain_config,
        adaptive_closure=bool(args.adaptive_closure),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    updated_path = args.output_dir / "blueprint.with_m3_real_gap_packages.json"
    write_json(updated_path, updated)
    print(
        json.dumps(
            {
                "ok": True,
                "updated_blueprint": str(updated_path),
                "report": str(args.output_dir / "m3_real_gap_loop_report.json"),
                "summary": report.get("summary", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
