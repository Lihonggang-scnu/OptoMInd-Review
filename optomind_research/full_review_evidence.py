"""Evidence-side adapters for the full-review S10-S12 stages.

This module turns a selected review blueprint into an evidence-bearing blueprint
without confusing retrieval candidates with verified support.  It deliberately
reuses the mature M2/M3/M4 components already present in OptoMind and adds the
stage-level contracts needed by :mod:`full_review_orchestrator`.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


def compact(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def classify_visual_creation(argument_role: str) -> str:
    """Separate explanatory schematics from visuals that assert data."""
    lower = compact(argument_role, 1200).lower()
    empirical_phrases = (
        "measured", "experimental", "source data", "data-derived", "quantitative",
        "numerical", "spectrum", "spectral curve", "spectral overlay", "scatter plot",
        "distribution", "benchmark", "timeline", "radar chart", "heat map",
        "trl vs", "performance matrix", "projected versus measured",
        "same material's performance", "research roadmap",
    )
    if any(phrase in lower for phrase in empirical_phrases):
        return "source_data_replot_or_verified_source_figure"
    conceptual_phrases = (
        "conceptual diagram", "conceptual schematic", "framework diagram",
        "mechanism schematic", "pathway schematic", "heat flux pathways",
        "flow diagram", "knowledge structure", "integration points",
    )
    if any(phrase in lower for phrase in conceptual_phrases):
        return "author_synthesized_conceptual_schematic"
    if any(token in lower for token in ("matrix", "taxonomy", "most vulnerable")):
        return "source_data_replot_or_verified_source_figure"
    return "author_synthesized_conceptual_schematic"


def resolve_kb_sqlite(value: Path | str | None) -> Path | None:
    """Resolve a ReviewKnowledgeBase SQLite file from a file or directory."""
    if not value:
        return None
    path = Path(value)
    if path.is_file() and path.suffix.lower() in {".sqlite", ".db"}:
        return path
    if path.is_dir():
        for name in ("review_knowledge_base.sqlite", "knowledge_base.sqlite", "kb.sqlite"):
            candidate = path / name
            if candidate.exists():
                return candidate
        candidates = sorted(path.glob("*.sqlite")) + sorted(path.glob("*.db"))
        return candidates[0] if candidates else None
    if path.is_file() and path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        keys = (
            "review_knowledge_base_sqlite",
            "knowledge_base_sqlite",
            "sqlite_path",
            "kb_sqlite",
            "kb_path",
        )
        queue: list[Any] = [payload]
        while queue:
            item = queue.pop(0)
            if isinstance(item, dict):
                for key in keys:
                    if item.get(key):
                        candidate_value = Path(str(item[key]))
                        if not candidate_value.is_absolute():
                            candidate_value = path.parent / candidate_value
                        resolved = resolve_kb_sqlite(candidate_value)
                        if resolved:
                            return resolved
                queue.extend(item.values())
            elif isinstance(item, list):
                queue.extend(item)
        return None
    return None


def _tokens(text: str) -> set[str]:
    stop = {
        "about", "after", "also", "among", "and", "are", "based", "between",
        "from", "have", "into", "more", "review", "section", "should", "study",
        "that", "their", "these", "this", "those", "through", "using", "what",
        "when", "where", "which", "with", "within", "would",
    }
    return {
        token for token in re.findall(r"[a-z][a-z0-9-]{3,}", str(text or "").lower())
        if token not in stop
    }


def _section_title(section: dict[str, Any]) -> str:
    return compact(section.get("title") or section.get("section_title"), 220)


def _planned_thesis(section: dict[str, Any], contract: dict[str, Any]) -> str:
    planned = section.get("planned_thesis")
    if isinstance(planned, dict):
        planned = planned.get("text")
    central = section.get("central_claim")
    if isinstance(central, dict):
        central = central.get("text")
    return compact(
        planned
        or central
        or contract.get("central_thesis")
        or section.get("argument_role"),
        1400,
    )


def _merge_contract(section: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(section)
    merged["section_id"] = str(
        merged.get("section_id") or contract.get("section_id") or ""
    )
    merged["title"] = _section_title(merged) or compact(contract.get("section_title"), 220)
    merged["section_title"] = merged["title"]
    merged["argument_role"] = compact(
        merged.get("argument_role") or contract.get("section_purpose"), 900
    )
    merged["key_questions"] = list(
        merged.get("key_questions") or contract.get("open_questions") or []
    )[:8]
    merged["required_claim_kinds"] = list(
        merged.get("required_claim_kinds") or contract.get("required_evidence_roles") or []
    )[:8]
    merged["scope_guardrails"] = list(
        merged.get("scope_guardrails") or contract.get("forbidden_overclaims") or []
    )[:8]
    merged["expected_visual_arguments"] = list(
        merged.get("expected_visual_arguments") or contract.get("expected_visual_roles") or []
    )[:8]
    merged["transition_from_previous"] = compact(
        merged.get("transition_from_previous") or contract.get("transition_in"), 700
    )
    merged["transition_to_next"] = compact(
        merged.get("transition_to_next") or contract.get("transition_out"), 700
    )
    thesis = _planned_thesis(merged, contract)
    # The selected blueprint is authoritative for the planned thesis.  Some
    # older S9 artifacts clipped this field at a fixed character boundary,
    # leaving the writer with a half sentence even though retrieval still saw
    # the complete blueprint thesis.  Repair that hand-off generically.
    merged_contract = copy.deepcopy(contract)
    if thesis:
        merged_contract["central_thesis"] = thesis
        merged_contract["central_thesis_source"] = "selected_blueprint_planned_thesis"
    merged["section_contract"] = merged_contract

    existing_graph = merged.get("claim_graph_seed")
    existing_seeds = (
        existing_graph.get("central_claim_candidates")
        if isinstance(existing_graph, dict)
        else []
    ) or []
    if not existing_seeds and thesis:
        existing_seeds = [{
            "claim_seed": thesis,
            "supporting_text_chunk_ids": [],
            "supporting_visual_chunk_ids": [],
            "seed_status": "planned_requires_posthoc_binding",
        }]
    merged["claim_graph_seed"] = {
        "central_claim_candidates": existing_seeds[:4],
        "relation_types_to_check": [
            "supports", "motivates", "constrains", "applies_to", "qualifies", "contrasts_with"
        ],
        "claim_binding_rule": (
            "A planning proposition is not evidence. Every factual claim must be "
            "bound to exact candidate chunks and independently verified."
        ),
    }
    return merged


def _section_queries(section: dict[str, Any]) -> list[str]:
    contract = section.get("section_contract") or {}
    thesis = _planned_thesis(section, contract)
    values: list[str] = [
        _section_title(section),
        thesis,
        compact(section.get("argument_role"), 500),
    ]
    # Evidence-role queries come before open questions so a long contract does
    # not silently truncate the very evidence obligations that control writing.
    values.extend(
        compact(x, 420) for x in (contract.get("required_evidence_roles") or [])[:8]
    )
    values.extend(compact(x, 320) for x in (section.get("key_questions") or [])[:5])
    values.extend(compact(x, 360) for x in (contract.get("argument_sequence") or [])[:5])
    queries: list[str] = []
    for value in values:
        terms = sorted(token for token in _tokens(value) if len(token) >= 4)
        query = " ".join(terms[:14])
        if query and query not in queries:
            queries.append(query)
    return queries[:14]


def _retrieve_candidates(
    sqlite_path: Path | None,
    section: dict[str, Any],
    *,
    top_k_per_query: int = 10,
    candidate_limit: int = 28,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if sqlite_path is None:
        return [], {"queries": _section_queries(section), "reason": "kb_unavailable"}
    from optomind_research.review_knowledge_base import query_kb

    queries = _section_queries(section)
    by_id: dict[str, dict[str, Any]] = {}
    appearances: dict[str, int] = {}
    query_counts: dict[str, int] = {}
    rows_by_query: dict[str, list[dict[str, Any]]] = {}
    for query in queries:
        try:
            rows = query_kb(sqlite_path, query, top_k=top_k_per_query).get("text_chunks") or []
        except Exception:
            rows = []
        query_counts[query] = len(rows)
        rows_by_query[query] = []
        query_tokens = _tokens(query)
        for rank, row in enumerate(rows):
            if not isinstance(row, dict) or not row.get("chunk_id"):
                continue
            chunk_id = str(row["chunk_id"])
            text = " ".join(
                str(row.get(key) or "")
                for key in ("title", "section_path", "text_preview")
            )
            overlap = len(query_tokens & _tokens(text))
            if overlap == 0:
                continue
            appearances[chunk_id] = appearances.get(chunk_id, 0) + 1
            candidate = dict(row)
            candidate["retrieval_query"] = query
            candidate["retrieval_rank"] = rank + 1
            candidate["lexical_overlap"] = overlap
            existing = by_id.get(chunk_id)
            if existing is None or (overlap, -rank) > (
                int(existing.get("lexical_overlap") or 0),
                -int(existing.get("retrieval_rank") or 999),
            ):
                by_id[chunk_id] = candidate
            rows_by_query[query].append(candidate)

    ranked = sorted(
        by_id.values(),
        key=lambda row: (
            appearances.get(str(row.get("chunk_id")), 0),
            int(row.get("lexical_overlap") or 0),
            -int(row.get("retrieval_rank") or 999),
        ),
        reverse=True,
    )
    # Diversity-aware selection: no paper may crowd out the rest of the corpus.
    selected: list[dict[str, Any]] = []
    per_paper: dict[str, int] = {}
    selected_ids: set[str] = set()

    # Coverage pass: retain up to two strong candidates per contract query in
    # round-robin order before global relevance ranking.  This prevents the
    # central thesis vocabulary from crowding out equation, comparison,
    # boundary-condition, or deployment evidence required by the contract.
    for round_index in range(2):
        for query in queries:
            query_rows = rows_by_query.get(query) or []
            candidates = sorted(
                query_rows,
                key=lambda row: (
                    int(row.get("lexical_overlap") or 0),
                    -int(row.get("retrieval_rank") or 999),
                ),
                reverse=True,
            )
            available = [
                row for row in candidates
                if str(row.get("chunk_id") or "") not in selected_ids
                and per_paper.get(
                    str(row.get("paper_id") or row.get("doi") or "unknown"), 0
                ) < 4
            ]
            if not available:
                continue
            row = available[0]
            chunk_id = str(row.get("chunk_id") or "")
            paper_id = str(row.get("paper_id") or row.get("doi") or "unknown")
            selected.append(row)
            selected_ids.add(chunk_id)
            per_paper[paper_id] = per_paper.get(paper_id, 0) + 1
            if len(selected) >= candidate_limit:
                break
        if len(selected) >= candidate_limit:
            break

    for row in ranked:
        if len(selected) >= candidate_limit:
            break
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id in selected_ids:
            continue
        paper_id = str(row.get("paper_id") or row.get("doi") or "unknown")
        if per_paper.get(paper_id, 0) >= 4:
            continue
        selected.append(row)
        selected_ids.add(chunk_id)
        per_paper[paper_id] = per_paper.get(paper_id, 0) + 1
    # Do not leave a section empty solely because a diversity cap was reached.
    if len(selected) < min(6, candidate_limit):
        for row in ranked:
            if str(row.get("chunk_id")) in selected_ids:
                continue
            selected.append(row)
            if len(selected) >= min(6, candidate_limit):
                break
    return selected, {
        "queries": queries,
        "query_candidate_counts": query_counts,
        "unique_candidates": len(by_id),
        "selected_candidates": len(selected),
        "selected_papers": len({str(row.get("paper_id") or "") for row in selected}),
        "policy": "multi_query_fts_plus_paper_diversity",
    }


def _mock_gap_claims(section: dict[str, Any]) -> list[dict[str, Any]]:
    """Create honest mock claims without inventing source identifiers."""
    from optomind_research.claim_schema import Claim, infer_claim_kind_from_statement

    seed_rows = (section.get("claim_graph_seed") or {}).get("central_claim_candidates") or []
    seed = compact(seed_rows[0].get("claim_seed") if seed_rows else section.get("argument_role"), 900)
    if seed and not re.search(r"[.!?]$", seed):
        seed += "."
    statements = [seed or f"The argument planned for {section.get('section_id')} requires evidence."]
    questions = [compact(x, 500) for x in (section.get("key_questions") or []) if compact(x, 500)]
    if questions:
        question = questions[0]
        if not question.endswith("?"):
            question += "?"
        statements.append(question)
    else:
        statements.append("Which evidence is required to establish this section's central proposition?")
    claims: list[dict[str, Any]] = []
    for index, statement in enumerate(statements[:2], 1):
        claim = Claim(
            claim_id=f"{section.get('section_id', 'S00')}-C{index:02d}",
            statement=statement,
            evidence_type="mechanism",
            claim_kind=(
                "frontier_uncertainty" if statement.endswith("?")
                else infer_claim_kind_from_statement(statement)
            ),
            claim_state="open_question",
            saturation_score=0.0,
            load_bearing=index == 1,
            critic_flags=["mock_no_kb_evidence"],
            evidence_binding_status="insufficient",
            evidence_binding_confidence="low",
            evidence_binding_reason="No knowledge-base evidence was supplied in mock mode.",
            section_fit="central" if index == 1 else "supporting",
            evidence_requirement="factual" if index == 1 else "open_question",
            closure_disposition="open_question",
        )
        claims.append(claim.to_dict())
    return claims


def _section_meta(sections: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for section in sections:
        text_chunks = section.get("candidate_text_chunks") or []
        visual_chunks = section.get("candidate_visual_chunks") or []
        result[str(section.get("section_id") or "")] = {
            "title": _section_title(section),
            "argument_role": section.get("argument_role", ""),
            "review_mentor_advice": section.get("review_mentor_advice", {}),
            "text_chunk_map": {
                str(row.get("chunk_id")): str(row.get("text_preview") or row.get("text") or "")
                for row in text_chunks if isinstance(row, dict) and row.get("chunk_id")
            },
            "text_chunk_meta": {
                str(row.get("chunk_id")): dict(row)
                for row in text_chunks if isinstance(row, dict) and row.get("chunk_id")
            },
            "visual_chunk_map": {
                str(row.get("chunk_id")): dict(row)
                for row in visual_chunks if isinstance(row, dict) and row.get("chunk_id")
            },
        }
    return result


def rebuild_argument_dag(
    blueprint: dict[str, Any],
    *,
    real_llm: bool,
    scope_definition: str = "",
) -> dict[str, Any]:
    from optomind_research.argument_dag_builder import ArgumentDAGBuilder

    sections = blueprint.get("sections") or []
    claims: list[dict[str, Any]] = []
    for section in sections:
        sid = str(section.get("section_id") or "")
        for claim in section.get("claims") or []:
            if isinstance(claim, dict):
                claim["section_id"] = sid
                claims.append(claim)
    builder = ArgumentDAGBuilder(
        real_llm=real_llm,
        model_tier="advanced_model",
        max_layer4_candidates=80,
        layer4_workers=6,
    )
    dag = builder.build(
        claims,
        [str(section.get("section_id") or "") for section in sections],
        section_meta=_section_meta(sections),
        enable_scope_check=real_llm,
        enable_readiness_check=True,
        scope_definition=scope_definition,
    )
    dag.propagate_saturation()
    registry = dag.claims_registry
    for section in sections:
        section["claims"] = [
            registry.get(str(claim.get("claim_id")), claim)
            for claim in (section.get("claims") or [])
            if str(claim.get("claim_id")) in registry
        ]
    blueprint["argument_dag"] = dag.to_dict()
    return blueprint


def refresh_argument_dag_evidence_state(
    blueprint: dict[str, Any],
    *,
    target_claim_ids: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Refresh grounding metadata without re-judging unchanged logical edges.

    Targeted gap resolution changes evidence bindings, not the semantic content
    of claims. Re-running every Proposer/Critic pair is both wasteful and less
    stable. A full DAG rebuild remains mandatory when claims, evidence types,
    section membership, or the blueprint architecture itself changes.
    """
    from optomind_research.argument_dag_builder import _claim_can_enter_dag, _edge_readiness

    dag = copy.deepcopy(blueprint.get("argument_dag") or {})
    if not isinstance(dag.get("edges"), list):
        return rebuild_argument_dag(blueprint, real_llm=False)
    registry: dict[str, dict[str, Any]] = {}
    for section in blueprint.get("sections") or []:
        sid = str(section.get("section_id") or "")
        for claim in section.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            claim["section_id"] = sid
            cid = str(claim.get("claim_id") or "")
            if cid and _claim_can_enter_dag(claim):
                registry[cid] = claim
    refreshed_edges: list[dict[str, Any]] = []
    for raw in dag.get("edges") or []:
        if not isinstance(raw, dict):
            continue
        source_id = str(raw.get("source_claim_id") or "")
        target_id = str(raw.get("target_claim_id") or "")
        if source_id not in registry or target_id not in registry:
            continue
        edge = copy.deepcopy(raw)
        source, target = registry[source_id], registry[target_id]
        readiness, followup = _edge_readiness(source, target)
        edge["source_evidence_type"] = source.get("evidence_type") or "mechanism"
        edge["target_evidence_type"] = target.get("evidence_type") or "mechanism"
        edge["edge_readiness"] = readiness
        edge["requires_evidence_followup"] = followup
        refreshed_edges.append(edge)
    dag["nodes"] = list(registry.values())
    dag["claims"] = list(registry.values())
    dag["edges"] = refreshed_edges
    dag["edge_count"] = len(refreshed_edges)
    dag["cross_section_edge_count"] = sum(
        str(edge.get("source_section_id") or "") != str(edge.get("target_section_id") or "")
        for edge in refreshed_edges
    )
    dag["grounded_edge_count"] = sum(
        edge.get("edge_readiness") == "grounded" for edge in refreshed_edges
    )
    dag["provisional_edge_count"] = sum(
        edge.get("edge_readiness") == "provisional" for edge in refreshed_edges
    )
    pruning = dict(dag.get("pruning_stats") or {})
    pruning["evidence_state_refresh"] = {
        "mode": "targeted_evidence_refresh_without_semantic_edge_rebuild",
        "target_claim_ids": sorted({str(value) for value in (target_claim_ids or []) if str(value)}),
        "claim_count": len(registry),
        "edge_count": len(refreshed_edges),
    }
    dag["pruning_stats"] = pruning
    blueprint["argument_dag"] = dag
    return blueprint


def _portfolio_summary(
    blueprint: dict[str, Any],
    packets: list[dict[str, Any]],
    sqlite_path: Path | None = None,
) -> dict[str, Any]:
    claims = [
        claim
        for section in (blueprint.get("sections") or [])
        for claim in (section.get("claims") or [])
        if isinstance(claim, dict)
    ]
    valid_ids = {
        str(row.get("chunk_id"))
        for packet in packets
        for row in (packet.get("evidence_packets") or [])
        if row.get("chunk_id")
    }
    referenced = {
        str(chunk_id)
        for claim in claims
        for chunk_id in (claim.get("supporting_text_chunk_ids") or [])
    }
    canonical_ids: set[str] = set()
    if sqlite_path is not None and referenced:
        connection = sqlite3.connect(str(sqlite_path))
        try:
            values = list(referenced)
            placeholders = ",".join("?" for _ in values)
            canonical_ids = {
                str(row[0]) for row in connection.execute(
                    f"SELECT chunk_id FROM text_chunks WHERE chunk_id IN ({placeholders})",
                    values,
                ).fetchall()
            }
        except Exception:
            canonical_ids = set()
        finally:
            connection.close()
    verified_claims = [
        claim for claim in claims
        if str(claim.get("evidence_binding_status") or "")
        in {"direct", "synthesized", "partial"}
        and bool(claim.get("supporting_text_chunk_ids"))
    ]
    states: dict[str, int] = {}
    for claim in claims:
        state = str(claim.get("claim_state") or "planned")
        states[state] = states.get(state, 0) + 1
    return {
        "section_count": len(blueprint.get("sections") or []),
        "claim_count": len(claims),
        "load_bearing_claim_count": sum(bool(c.get("load_bearing")) for c in claims),
        "claims_with_text_support": len(verified_claims),
        "claims_without_text_support": len(claims) - len(verified_claims),
        "claims_with_any_text_ids": sum(bool(c.get("supporting_text_chunk_ids")) for c in claims),
        "unverified_binding_claim_ids": [
            str(claim.get("claim_id") or "") for claim in claims
            if claim.get("supporting_text_chunk_ids")
            and str(claim.get("evidence_binding_status") or "")
            not in {"direct", "synthesized", "partial"}
        ],
        "claim_state_distribution": states,
        "verified_evidence_packet_count": sum(
            len(packet.get("evidence_packets") or []) for packet in packets
        ),
        "unknown_or_unloaded_chunk_ids": sorted(
            referenced - canonical_ids if sqlite_path is not None else referenced - valid_ids
        ),
        "canonical_supporting_ids_omitted_from_compact_packets": sorted(
            (referenced & canonical_ids) - valid_ids
        ),
        "dag_edge_count": int((blueprint.get("argument_dag") or {}).get("edge_count") or 0),
        "dag_provisional_edge_count": int(
            (blueprint.get("argument_dag") or {}).get("provisional_edge_count") or 0
        ),
    }


def merge_incremental_material_packets(
    previous_packets: list[dict[str, Any]],
    refreshed_packets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Union old verified packets with refreshed packets for unchanged claims."""
    previous_by_section = {
        str(row.get("section_id") or ""): row
        for row in previous_packets if isinstance(row, dict)
    }
    merged: list[dict[str, Any]] = []
    preserved_count = 0
    added_count = 0
    for fresh_raw in refreshed_packets:
        if not isinstance(fresh_raw, dict):
            continue
        fresh = copy.deepcopy(fresh_raw)
        sid = str(fresh.get("section_id") or "")
        old = previous_by_section.get(sid) or {}
        fresh_claims = {
            str(row.get("claim_id") or ""): row
            for row in (fresh.get("claims") or []) if isinstance(row, dict)
        }
        old_claims = {
            str(row.get("claim_id") or ""): row
            for row in (old.get("claims") or []) if isinstance(row, dict)
        }

        def packet_allowed(packet: dict[str, Any]) -> bool:
            claim_id = str(packet.get("claim_id") or "")
            current = fresh_claims.get(claim_id)
            prior = old_claims.get(claim_id)
            if current is None or prior is None:
                return False
            if str(current.get("claim_state") or "").lower() in {"dropped", "contradicted"}:
                return False
            return compact(current.get("statement"), 1200) == compact(
                prior.get("statement"), 1200
            )

        packets: list[dict[str, Any]] = []
        seen_packets: set[tuple[str, str, str]] = set()
        for source, rows in (
            ("previous", old.get("evidence_packets") or []),
            ("refreshed", fresh.get("evidence_packets") or []),
        ):
            for row in rows:
                if not isinstance(row, dict) or not row.get("chunk_id"):
                    continue
                if source == "previous" and not packet_allowed(row):
                    continue
                key = (
                    str(row.get("claim_id") or ""),
                    str(row.get("chunk_id") or ""),
                    str(row.get("paper_id") or ""),
                )
                if key in seen_packets:
                    continue
                seen_packets.add(key)
                packets.append(copy.deepcopy(row))
                if source == "previous":
                    preserved_count += 1
                else:
                    added_count += 1
        fresh["evidence_packets"] = packets
        for field_name in (
            "contradictions", "open_questions", "visual_evidence", "visual_gap_plan"
        ):
            values: list[Any] = []
            seen: set[str] = set()
            for row in list(old.get(field_name) or []) + list(fresh.get(field_name) or []):
                key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                if key in seen:
                    continue
                seen.add(key)
                values.append(copy.deepcopy(row))
            fresh[field_name] = values
        # Coverage is a chapter-level source landscape rather than a claim
        # packet. Preserve it across evidence-only refreshes.
        old_coverage = old.get("literature_coverage") or {}
        fresh_coverage = fresh.get("literature_coverage") or {}
        coverage_sources: list[dict[str, Any]] = []
        seen_papers: set[str] = set()
        for source in list(old_coverage.get("sources") or []) + list(fresh_coverage.get("sources") or []):
            if not isinstance(source, dict):
                continue
            paper_id = str(source.get("paper_id") or "")
            if not paper_id or paper_id in seen_papers:
                continue
            seen_papers.add(paper_id)
            coverage_sources.append(copy.deepcopy(source))
        merged_coverage = copy.deepcopy(fresh_coverage or old_coverage)
        merged_coverage["sources"] = coverage_sources
        fresh["literature_coverage"] = merged_coverage
        merged.append(fresh)
    return merged, {
        "policy": "monotonic_union_for_unchanged_claims",
        "previous_packets_preserved": preserved_count,
        "refreshed_packets_added": added_count,
    }


def build_evidence_portfolios(
    blueprint: dict[str, Any],
    contracts: list[dict[str, Any]],
    *,
    kb_path: Path | str | None,
    real_llm: bool,
    scope_definition: str = "",
    mentor_advice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Implement S10: retrieve candidates, decompose claims, verify, and map packets."""
    from optomind_research.claim_decomposer import ClaimDecomposer
    from optomind_research.review_writer import SectionMaterialMapper
    from optomind_research.section_literature_coverage import (
        SectionLiteratureCoverageExpander,
        coverage_candidate_chunks,
        filter_candidate_chunks_by_coverage_scope,
    )

    bp = copy.deepcopy(blueprint)
    contract_index = {
        str(row.get("section_id") or ""): row for row in contracts if isinstance(row, dict)
    }
    sqlite_path = resolve_kb_sqlite(kb_path)
    retrieval_audit: list[dict[str, Any]] = []
    normalized_sections: list[dict[str, Any]] = []
    for raw in bp.get("sections") or []:
        sid = str(raw.get("section_id") or "")
        section = _merge_contract(raw, contract_index.get(sid, {}))
        section["review_mentor_advice"] = copy.deepcopy(mentor_advice or {})
        candidates, audit = _retrieve_candidates(sqlite_path, section)
        section["candidate_text_chunks"] = candidates
        section["candidate_text_chunk_ids"] = [str(row.get("chunk_id")) for row in candidates]
        section.setdefault("candidate_visual_chunks", [])
        retrieval_audit.append({"section_id": sid, **audit})
        normalized_sections.append(section)
    bp["sections"] = normalized_sections

    # Build the chapter-level literature landscape before claim decomposition.
    # These sources are inputs for comparison and synthesis, not automatic
    # proof of a precise factual proposition.
    coverage_expander = SectionLiteratureCoverageExpander(
        kb_path=sqlite_path,
        real_llm=real_llm,
        model_tier="premium_model",
        max_papers_per_role=5,
    )
    with ThreadPoolExecutor(max_workers=min(3, max(1, len(normalized_sections)))) as pool:
        coverage_results = list(pool.map(coverage_expander.expand, normalized_sections))
    with ThreadPoolExecutor(max_workers=min(3, max(1, len(normalized_sections)))) as pool:
        candidate_scope_audits = list(pool.map(
            lambda pair: coverage_expander.audit_candidate_chunks(
                pair[0],
                pair[1].get("plan") or {},
                list(pair[0].get("candidate_text_chunks") or []),
            ),
            zip(normalized_sections, coverage_results),
        ))
    literature_coverage_audit: list[dict[str, Any]] = []
    for section, coverage, candidate_scope_audit in zip(
        normalized_sections, coverage_results, candidate_scope_audits
    ):
        section["literature_coverage"] = coverage
        scoped_candidates, scope_filter_audit = filter_candidate_chunks_by_coverage_scope(
            list(section.get("candidate_text_chunks") or []),
            {"source_scope_audit": candidate_scope_audit},
        )
        scope_filter_audit["source_scope_audit"] = candidate_scope_audit
        section["candidate_scope_filter_audit"] = scope_filter_audit
        existing = {
            str(row.get("chunk_id") or ""): row
            for row in scoped_candidates
            if isinstance(row, dict) and row.get("chunk_id")
        }
        for row in coverage_candidate_chunks(coverage):
            chunk_id = str(row.get("chunk_id") or "")
            if chunk_id and chunk_id not in existing:
                existing[chunk_id] = row
        section["candidate_text_chunks"] = list(existing.values())[:60]
        section["candidate_text_chunk_ids"] = [
            str(row.get("chunk_id") or "")
            for row in section["candidate_text_chunks"] if row.get("chunk_id")
        ]
        literature_coverage_audit.append({
            "section_id": str(section.get("section_id") or ""),
            **copy.deepcopy(coverage.get("summary") or {}),
            "coverage_gaps": copy.deepcopy(coverage.get("coverage_gaps") or []),
        })

    claim_generation_audit: list[dict[str, Any]] = []
    if real_llm:
        def decompose(section: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            agent = ClaimDecomposer(model_tier="premium_model", real_llm=True)
            claims = [claim.to_dict() for claim in agent.decompose_section(section)]
            return claims, {
                "section_id": str(section.get("section_id") or ""),
                **copy.deepcopy(agent.last_audit),
            }

        with ThreadPoolExecutor(max_workers=min(3, max(1, len(normalized_sections)))) as pool:
            claim_batches = list(pool.map(decompose, normalized_sections))
        for section, (claims, audit) in zip(normalized_sections, claim_batches):
            section["claims"] = claims
            claim_generation_audit.append(audit)
    else:
        for section in normalized_sections:
            section["claims"] = _mock_gap_claims(section)

    # Initial claim generation changes the semantic graph and therefore always
    # requires a graph build. Evidence-only refresh is reserved for S11.
    bp = rebuild_argument_dag(bp, real_llm=real_llm, scope_definition=scope_definition)
    mapper = SectionMaterialMapper(kb_path=sqlite_path)
    packets = [mapper.map(section).to_dict() for section in bp.get("sections") or []]
    return {
        "schema_version": "full_review.evidence_portfolios.v1",
        "blueprint": bp,
        "evidence_portfolios": packets,
        "retrieval_audit": retrieval_audit,
        "literature_coverage_audit": literature_coverage_audit,
        "claim_generation_audit": claim_generation_audit,
        "kb_sqlite": str(sqlite_path or ""),
        "quality_summary": _portfolio_summary(bp, packets, sqlite_path),
        "candidate_vs_evidence_policy": (
            "Chapter literature sources may support context, comparison, and review synthesis. "
            "Precise measurements and source-specific factual claims still require claim-level "
            "verification; ordinary synthesis is not reduced to sentence-level proof."
        ),
    }


def _load_chunks_by_ids(sqlite_path: Path | None, chunk_ids: list[str]) -> list[dict[str, Any]]:
    if sqlite_path is None or not chunk_ids:
        return []
    connection = sqlite3.connect(str(sqlite_path))
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = connection.execute(
            f"SELECT chunk_id,paper_id,doi,title,section_path,text AS text_preview "
            f"FROM text_chunks WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        ).fetchall()
        by_id = {str(row["chunk_id"]): dict(row) for row in rows}
        return [by_id[cid] for cid in chunk_ids if cid in by_id]
    finally:
        connection.close()


def _verify_new_internal_support(
    blueprint: dict[str, Any],
    before_ids: dict[str, list[str]],
    sqlite_path: Path | None,
) -> list[dict[str, Any]]:
    """Re-verify keyword-retrieved chunks before they become factual support."""
    if sqlite_path is None:
        return []
    from optomind_research.claim_evidence_verifier import ClaimEvidenceVerifier
    from optomind_research.claim_schema import Claim

    jobs: list[tuple[dict[str, Any], dict[str, Any], list[str], list[str]]] = []
    for section in blueprint.get("sections") or []:
        for claim in section.get("claims") or []:
            claim_id = str(claim.get("claim_id") or "")
            original = list(before_ids.get(claim_id) or [])
            proposed = list(claim.get("supporting_text_chunk_ids") or [])
            new_ids = [chunk_id for chunk_id in proposed if chunk_id not in original]
            if new_ids:
                jobs.append((section, claim, original, new_ids))

    def verify_one(job: tuple[dict[str, Any], dict[str, Any], list[str], list[str]]) -> dict[str, Any]:
        section, claim, original, new_ids = job
        claim_id = str(claim.get("claim_id") or "")
        existing_candidates = {
            str(row.get("chunk_id")): row
            for row in (section.get("candidate_text_chunks") or [])
            if isinstance(row, dict) and row.get("chunk_id")
        }
        for row in _load_chunks_by_ids(sqlite_path, new_ids):
            existing_candidates[str(row["chunk_id"])] = row
        verifier_section = dict(section)
        priority = list(dict.fromkeys(original + new_ids + list(existing_candidates)))
        verifier_section["candidate_text_chunks"] = [
            existing_candidates[cid] for cid in priority if cid in existing_candidates
        ][:16]
        verifier_section["candidate_text_chunk_ids"] = [
            str(row["chunk_id"]) for row in verifier_section["candidate_text_chunks"]
        ]
        try:
            verifier = ClaimEvidenceVerifier(model_tier="premium_model")
            verified = verifier.verify_and_bind([Claim.from_dict(claim)], verifier_section)[0]
            claim.clear()
            claim.update(verified.to_dict())
            accepted_new = [
                cid for cid in claim.get("supporting_text_chunk_ids") or [] if cid in new_ids
            ]
            return {
                "claim_id": claim_id,
                "proposed_new_chunk_ids": new_ids,
                "verified_new_chunk_ids": accepted_new,
                "rejected_new_chunk_ids": [cid for cid in new_ids if cid not in accepted_new],
                "verifier_status": claim.get("evidence_binding_status", ""),
                "verifier_audit": verifier.last_audit,
            }
        except Exception as exc:
            claim["supporting_text_chunk_ids"] = original
            claim["gap_resolution_status"] = "verification_failed"
            claim.setdefault("critic_flags", []).append(
                f"internal_gap_verifier_error:{type(exc).__name__}"
            )
            return {
                "claim_id": claim_id,
                "proposed_new_chunk_ids": new_ids,
                "verified_new_chunk_ids": [],
                "rejected_new_chunk_ids": new_ids,
                "verifier_status": "error_fail_closed",
                "verifier_error": f"{type(exc).__name__}: {exc}",
            }

    if len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
            return list(pool.map(verify_one, jobs))
    return [verify_one(job) for job in jobs]


def resolve_evidence_gaps(
    evidence_bundle: dict[str, Any],
    *,
    kb_path: Path | str | None,
    real_llm: bool,
    scope_definition: str = "",
    enable_external_oa: bool = False,
    external_output_dir: Path | None = None,
    max_external_rounds: int = 2,
    max_external_claims: int = 6,
    external_download_top_n: int = 3,
    max_external_coverage_gaps: int = 6,
    external_coverage_download_top_n: int = 2,
    target_claim_ids: list[str] | set[str] | tuple[str, ...] | None = None,
    dag_update_mode: str = "auto",
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """Implement S11 with internal KB search and optional OA expansion."""
    from optomind_research.gap_resolution_agent import GapResolutionAgent
    from optomind_research.review_writer import SectionMaterialMapper

    bp = copy.deepcopy(evidence_bundle.get("blueprint") or {})
    sqlite_path = resolve_kb_sqlite(kb_path or evidence_bundle.get("kb_sqlite"))
    before_ids = {
        str(claim.get("claim_id") or ""): list(claim.get("supporting_text_chunk_ids") or [])
        for section in bp.get("sections") or []
        for claim in (section.get("claims") or [])
    }
    internal = GapResolutionAgent(
        real_llm=real_llm,
        model_tier="advanced_model",
        saturation_threshold=1.5,
        max_iterations=2,
        kb_path=sqlite_path,
    )
    if progress_callback is not None:
        progress_callback("internal_kb_search_started", {"target_claim_ids": list(target_claim_ids or [])})
    bp, internal_results = internal.resolve_blueprint(
        bp, target_claim_ids=target_claim_ids
    )
    verifier_audit = (
        _verify_new_internal_support(bp, before_ids, sqlite_path) if real_llm else []
    )

    external_report: dict[str, Any] = {
        "enabled": bool(enable_external_oa),
        "status": "not_requested",
    }
    if enable_external_oa and real_llm:
        from optomind_research.m3_real_gap_loop import run_m3_real_gap_loop

        output_dir = Path(external_output_dir or Path("outputs") / "m3_real_gap_loop")
        bp, report = run_m3_real_gap_loop(
            bp,
            output_dir=output_dir,
            max_rounds=max_external_rounds,
            max_claims=max_external_claims,
            download_top_n=external_download_top_n,
            citation_chase_top_n=2,
            kb_sqlite=sqlite_path,
            topic_context=scope_definition,
            adaptive_closure=True,
            target_claim_ids=list(target_claim_ids) if target_claim_ids is not None else None,
            progress_callback=progress_callback,
        )
        external_report = report
    elif enable_external_oa:
        external_report = {"enabled": True, "status": "mock_mode_not_executed"}

    section_coverage_oa_report: dict[str, Any] = {
        "enabled": bool(enable_external_oa),
        "status": "not_requested",
    }
    if enable_external_oa and real_llm:
        from optomind_research.section_coverage_oa_expander import (
            expand_section_coverage_gaps_oa,
        )

        coverage_dir = Path(
            external_output_dir or Path("outputs") / "m3_real_gap_loop"
        ) / "section_coverage"
        bp, section_coverage_oa_report = expand_section_coverage_gaps_oa(
            bp,
            kb_sqlite=sqlite_path,
            output_dir=coverage_dir,
            max_gaps=max_external_coverage_gaps,
            results_per_backend=10,
            download_top_n=external_coverage_download_top_n,
            progress_callback=progress_callback,
        )
    elif enable_external_oa:
        section_coverage_oa_report = {
            "enabled": True,
            "status": "mock_mode_not_executed",
        }

    if dag_update_mode not in {"auto", "refresh_existing_graph", "rebuild_semantic_graph"}:
        raise ValueError(f"Unsupported dag_update_mode: {dag_update_mode}")
    refresh_graph = bool(
        dag_update_mode == "refresh_existing_graph"
        or (
            dag_update_mode == "auto"
            and target_claim_ids is not None
            and (bp.get("argument_dag") or {}).get("edges") is not None
        )
    )
    if refresh_graph:
        bp = refresh_argument_dag_evidence_state(bp, target_claim_ids=target_claim_ids)
    else:
        bp = rebuild_argument_dag(bp, real_llm=real_llm, scope_definition=scope_definition)
    mapper = SectionMaterialMapper(kb_path=sqlite_path)
    refreshed_packets = [mapper.map(section).to_dict() for section in bp.get("sections") or []]
    packets, packet_merge_audit = merge_incremental_material_packets(
        list(
            evidence_bundle.get("material_packets")
            or evidence_bundle.get("evidence_portfolios")
            or []
        ),
        refreshed_packets,
    )
    load_bearing_factual = [
        claim
        for section in bp.get("sections") or []
        for claim in (section.get("claims") or [])
        if bool(claim.get("load_bearing"))
        and str(claim.get("evidence_requirement") or "factual") == "factual"
    ]
    unresolved = [
        str(claim.get("claim_id") or "")
        for claim in load_bearing_factual
        if str(claim.get("evidence_binding_status") or "")
        not in {"direct", "synthesized", "partial"}
    ]
    partially_supported = [
        str(claim.get("claim_id") or "")
        for claim in load_bearing_factual
        if str(claim.get("evidence_binding_status") or "") == "partial"
    ]
    remaining_missing_components = {
        str(claim.get("claim_id") or ""): list(claim.get("missing_evidence_components") or [])
        for section in bp.get("sections") or []
        for claim in (section.get("claims") or [])
        if claim.get("missing_evidence_components")
    }
    accepted_new = sum(
        len(row.get("verified_new_chunk_ids") or []) for row in verifier_audit
    )
    stop_reason = (
        "mock_evidence_not_evaluated"
        if not real_llm
        else
        "all_load_bearing_factual_claims_have_direct_or_synthesized_support"
        if not unresolved and not partially_supported
        else "minimum_partial_support_reached_with_explicit_residual_gaps"
        if not unresolved
        else "no_new_verified_internal_support"
        if real_llm and not accepted_new and not enable_external_oa
        else "external_oa_round_budget_reached"
        if enable_external_oa
        else "internal_only_completed"
    )
    return {
        "schema_version": "full_review.gap_history.v1",
        "blueprint": bp,
        "evidence_portfolios": packets,
        "kb_sqlite": str(sqlite_path or ""),
        "internal_gap_resolution": [row.to_dict() for row in internal_results],
        "internal_evidence_verifier_audit": verifier_audit,
        "external_oa_gap_resolution": external_report,
        "section_coverage_oa_expansion": section_coverage_oa_report,
        "argument_dag_update_mode": (
            "refresh_existing_graph" if refresh_graph else "rebuild_semantic_graph"
        ),
        "material_packet_merge_audit": packet_merge_audit,
        "unresolved_load_bearing_claim_ids": unresolved,
        "partially_supported_load_bearing_claim_ids": partially_supported,
        "remaining_missing_components": remaining_missing_components,
        "quality_summary": _portfolio_summary(bp, packets, sqlite_path),
        "stop_reason": stop_reason,
        "stop_policy": (
            "Stop on direct/synthesized support, or on minimum partial support when all residual "
            "components remain explicit and writing is forced to hedge/narrow; otherwise stop on "
            "no newly verified support or the configured retrieval-round budget."
        ),
    }


def plan_visual_evidence(
    gap_bundle: dict[str, Any],
    *,
    kb_path: Path | str | None,
    real_llm: bool,
    rerank_max_items: int | None = None,
    rerank_workers: int = 4,
    cache_path: Path | None = None,
    generate_conceptual_visuals: bool = False,
    generated_visual_output_dir: Path | None = None,
    max_generated_conceptual_visuals: int = 4,
) -> dict[str, Any]:
    """Implement S12 and promote only independently verified visual support."""
    from optomind_research.review_writer import SectionMaterialMapper
    from optomind_research.visual_argument_alignment import (
        VisualArgumentAligner,
        merge_verified_visual_support,
    )

    bp = copy.deepcopy(gap_bundle.get("blueprint") or {})
    sqlite_path = resolve_kb_sqlite(kb_path or gap_bundle.get("kb_sqlite"))
    aligner = VisualArgumentAligner()
    chunks = aligner.load_visual_chunks_from_sqlite(sqlite_path) if sqlite_path else []
    result = aligner.build_alignment_report(
        chunks,
        bp,
        auto_recommend=True,
        section_top_k=6,
        claim_top_k=3,
        rerank=True,
        rerank_real_llm=real_llm,
        rerank_model_tier="vision_model",
        rerank_workers=rerank_workers,
        rerank_cache=cache_path,
        rerank_max_items=rerank_max_items,
        rerank_load_bearing_only=True,
    )
    report = result.to_dict()
    bp = merge_verified_visual_support(bp, report)
    visual_gap_plan: list[dict[str, Any]] = []
    for section in bp.get("sections") or []:
        if section.get("verified_visual_chunk_ids"):
            section["visual_gap_plan"] = []
            continue
        sid = str(section.get("section_id") or "")
        expected_roles = [
            compact(role, 700) for role in (section.get("expected_visual_arguments") or [])
            if compact(role, 700)
        ]
        if not expected_roles:
            expected_roles = [
                "A synthesis figure that makes the section's central relationship easier to evaluate."
            ]
        section_plans: list[dict[str, Any]] = []
        for index, role in enumerate(expected_roles, 1):
            creation_class = classify_visual_creation(role)
            empirical = creation_class == "source_data_replot_or_verified_source_figure"
            plan = {
                "visual_plan_id": f"{sid}-VG{index:02d}",
                "section_id": sid,
                "argument_role": role,
                "asset_status": "missing_required_visual",
                "creation_class": creation_class,
                "permitted_creation_modes": (
                    ["replot_from_traceable_source_data", "reuse_verified_source_figure_with_permission"]
                    if empirical
                    else ["author_drawn_schematic", "ai_assisted_schematic_explicitly_labelled"]
                ),
                "prohibited_use": (
                    "Do not use a generated image as empirical evidence or invent numerical curves."
                ),
                "evidence_status": "not_evidence_until_asset_is_created_and_reviewed",
                "needs_human_review": True,
            }
            section_plans.append(plan)
            visual_gap_plan.append(plan)
        section["visual_gap_plan"] = section_plans
    if generate_conceptual_visuals and visual_gap_plan:
        from optomind_research.conceptual_visual_generator import (
            generate_conceptual_visual_gaps,
        )

        visual_gap_plan = generate_conceptual_visual_gaps(
            visual_gap_plan=visual_gap_plan,
            blueprint=bp,
            output_dir=Path(
                generated_visual_output_dir
                or Path("outputs") / "generated_conceptual_visuals"
            ),
            real_llm=real_llm,
            max_assets=max_generated_conceptual_visuals,
        )
        approval_path = Path(
            generated_visual_output_dir
            or Path("outputs") / "generated_conceptual_visuals"
        ) / "approvals.json"
        approvals: dict[str, Any] = {}
        if approval_path.exists():
            try:
                raw_approvals = json.loads(approval_path.read_text(encoding="utf-8"))
                approvals = (
                    raw_approvals.get("approvals")
                    if isinstance(raw_approvals, dict)
                    and isinstance(raw_approvals.get("approvals"), dict)
                    else raw_approvals if isinstance(raw_approvals, dict) else {}
                )
            except Exception:
                approvals = {}
        for row in visual_gap_plan:
            decision = str(
                approvals.get(str(row.get("visual_plan_id") or ""), "")
            ).strip().lower()
            model_approved = row.get("generation_status") == "model_approved_human_pending"
            row["human_review_decision"] = decision or "pending"
            row["human_approved"] = bool(model_approved and decision == "approve")
            if row["human_approved"]:
                row["needs_human_review"] = False
                row["asset_status"] = "approved_ai_conceptual_schematic"
            elif decision == "reject":
                row["asset_status"] = "human_rejected_generated_visual"
        generated_by_id = {
            str(row.get("visual_plan_id") or ""): row for row in visual_gap_plan
        }
        for section in bp.get("sections") or []:
            section["visual_gap_plan"] = [
                generated_by_id.get(str(row.get("visual_plan_id") or ""), row)
                for row in (section.get("visual_gap_plan") or [])
            ]
    mapper = SectionMaterialMapper(kb_path=sqlite_path)
    packets = [mapper.map(section).to_dict() for section in bp.get("sections") or []]
    verified_gap_sections = [
        str(section.get("section_id") or "")
        for section in (bp.get("sections") or [])
        if not (section.get("verified_visual_chunk_ids") or [])
    ]
    claims_without_verified_visuals = [
        str(claim.get("claim_id") or "")
        for section in (bp.get("sections") or [])
        for claim in (section.get("claims") or [])
        if bool(claim.get("load_bearing"))
        and not (claim.get("supporting_visual_chunk_ids") or [])
    ]
    return {
        "schema_version": "full_review.visual_plans.v1",
        "blueprint": bp,
        "evidence_portfolios": packets,
        "kb_sqlite": str(sqlite_path or ""),
        "alignment_report": report,
        "visual_gap_plan": visual_gap_plan,
        "conceptual_visual_approval_file": str(
            Path(generated_visual_output_dir) / "approvals.json"
            if generated_visual_output_dir else ""
        ),
        "quality_summary": {
            **_portfolio_summary(bp, packets, sqlite_path),
            "promoted_direct_visuals": int(
                (bp.get("visual_alignment_status") or {}).get("promoted_direct_visuals") or 0
            ),
            "provisional_visuals": int(
                (bp.get("visual_alignment_status") or {}).get("provisional_visuals") or 0
            ),
            "visual_gap_sections": list(report.get("visual_gap_sections") or []),
            "sections_without_verified_visual_support": verified_gap_sections,
            "load_bearing_claims_without_verified_visual_support": claims_without_verified_visuals,
            "missing_required_visual_plan_count": len(visual_gap_plan),
            "generated_conceptual_visual_count": sum(
                bool(row.get("local_image_path")) for row in visual_gap_plan
            ),
            "model_approved_conceptual_visual_count": sum(
                row.get("generation_status") == "model_approved_human_pending"
                for row in visual_gap_plan
            ),
            "empirical_visuals_left_for_source_or_replot": sum(
                row.get("creation_class") == "source_data_replot_or_verified_source_figure"
                for row in visual_gap_plan
            ),
        },
        "promotion_policy": (
            "Only image-inspected, direct, medium-or-strong, entity-aligned visuals without "
            "a human-review flag become supporting_visual_chunk_ids."
        ),
    }
