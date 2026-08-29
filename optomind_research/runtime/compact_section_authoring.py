"""Bounded section-authoring workbench for production review runs.

The original ReAct workbench exposes every small authoring operation as a
separate tool.  That remains useful for difficult repairs, but it makes the
normal path repeatedly replay an ever-growing conversation.  This provider
keeps the same deterministic provenance, permission, citation, and package
validators while grouping the routine work into three coarse operations:

* prepare one compact, paper-diverse authoring workspace;
* submit one complete candidate (plan, evidence packet, prose, visuals);
* revise the prose once against the complete blocking audit batch.

Nothing in this module weakens an evidence gate.  It only moves deterministic
bookkeeping out of the model's conversation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from agentscope.tool import FunctionTool

from .argument_quality_policy import (
    BACKGROUND,
    DISCOVERY,
    FACTUAL,
    QUALIFIED,
    evidence_ceiling,
)
from .tool_provider import ToolProvider
from .section_authoring_tool_registry import (
    SectionAuthoringContext,
    SectionAuthoringToolProvider,
    _build_asset_graph,
    _make_build_evidence_packet,
    _make_inspect_material_package,
    _make_inspect_visual_assets,
    _make_load_authoring_context,
    _make_retrieve_chunk_text,
    _make_run_citation_audit,
    _make_submit_argument_plan,
    _make_submit_revision,
    _make_submit_section_draft,
    _make_submit_section_handoff_card,
    _make_submit_visual_placement,
    _legacy_fulltext_fixture,
    _normalize_writing_permission,
    _make_validate_authoring_package,
    _write_artifact,
    _persist_last_valid_section_candidate,
    _restore_last_valid_section_candidate,
)


COMPACT_SECTION_AUTHORING_TOOL_NAMES = [
    "prepare_authoring_workspace",
    "submit_authoring_candidate",
    "revise_authoring_candidate",
    "validate_authoring_package",
]


def _permissive_function_tool(func: Any) -> FunctionTool:
    """Allow harmless model envelope fields without weakening scientific gates."""

    tool = FunctionTool(func)
    schema = dict(tool.input_schema or {})
    # Qwen sometimes echoes fields such as ``section_id`` or ``claims_used``
    # beside the canonical candidate.  AgentScope validates the schema before
    # invoking the Python function; accepting those extras saves a ReAct turn.
    # The transaction below still validates every scientific field and simply
    # ignores unknown envelope metadata.
    schema["additionalProperties"] = True
    tool.input_schema = schema
    return tool

DEFAULT_CORE_CHUNK_LIMIT = 12
DEFAULT_CORE_CHUNK_MIN = 8
DEFAULT_CORE_CHUNK_MAX = 16
DEFAULT_WORKSPACE_TARGET_TOKENS = 25_000
DEFAULT_TOOL_RESULT_ALLOWANCE_TOKENS = 32_000

_CLAIM_HANDLE_RE = re.compile(r"\[\[claim:([A-Za-z0-9_:\-\.]+)\]\]")
_STRENGTH_PERMISSION = {
    "established": "factual_assertion",
    "qualified": "hedged_factual_assertion",
    "boundary": "interpretive_synthesis",
    "open": "evidence_gap_only",
}
_PERMISSION_RANK = {
    "evidence_gap_only": 1,
    "common_background": 2,
    "structural_transition": 2,
    "interpretive_synthesis": 2,
    "hedged_factual_assertion": 3,
    "factual_assertion": 4,
}
_VALID_WRITING_PERMISSIONS = frozenset(_PERMISSION_RANK)
_CLAIM_PERMISSION_BY_RANK = {
    1: "evidence_gap_only",
    2: "interpretive_synthesis",
    3: "hedged_factual_assertion",
    4: "factual_assertion",
}
_CEILING_PERMISSION = {
    FACTUAL: "factual_assertion",
    QUALIFIED: "hedged_factual_assertion",
    BACKGROUND: "interpretive_synthesis",
    DISCOVERY: "evidence_gap_only",
}


def _statement_priority(claim: Dict[str, Any]) -> str:
    """Canonical handoff statement priority, applied everywhere."""

    return str(
        claim.get("effective_statement")
        or claim.get("supported_rewrite")
        or claim.get("authoring_statement")
        or claim.get("statement")
        or ""
    ).strip()


def _asset_record(asset: Any) -> Dict[str, Any]:
    """Canonical asset dict for the shared evidence-ceiling semantics."""

    return {
        "use_permission": str(getattr(asset, "use_permission", "") or ""),
        "scope_fit": str(getattr(asset, "scope_fit", "") or ""),
        "content_depth": str(
            getattr(asset, "content_depth", "")
            or getattr(asset, "evidence_level", "")
            or ""
        ),
        "context_complete": bool(getattr(asset, "context_complete", False)),
        "source_kind": str(getattr(asset, "source_kind", "") or ""),
        "allowed_claim_kinds": list(getattr(asset, "allowed_claim_kinds", ()) or ()),
        "provenance": dict(getattr(asset, "route_provenance", {}) or {}),
    }


def _effective_asset_record(asset: Any) -> Dict[str, Any]:
    """Asset record with the registry's legacy-fulltext permission mapping."""

    record = _asset_record(asset)
    if _legacy_fulltext_fixture(asset):
        record["use_permission"] = "factual_support"
    return record


def _permission_from_ceiling(ceiling: str) -> str:
    return _CEILING_PERMISSION.get(ceiling, "evidence_gap_only")


def _loads(value: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _short_text(value: Any, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] if len(text) > limit else text


def _estimate_workspace_tokens(value: Any) -> int:
    """Conservative deterministic token estimate for serialized JSON.

    Calibrated against ResearchWorker's observed truncation boundary
    (~7,300 chars were held within the 1,800-token tool-result allowance,
    i.e. roughly 4 chars per token for dense scientific JSON).
    """

    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False)
    )
    return max(1, int(round(len(text) / 4.0)))


def _claim_support_chunk_ids(claim: Dict[str, Any], graph: Any) -> List[str]:
    """Canonical claim->chunk bindings present in the local asset graph."""

    result: List[str] = []
    for name in (
        "supporting_text_chunk_ids",
        "supporting_chunk_ids",
        "factual_support_chunk_ids",
        "contextual_support_chunk_ids",
        "core_chunk_ids",
    ):
        for raw in claim.get(name) or []:
            chunk_id = str(raw or "").strip()
            if chunk_id and chunk_id in graph.chunks and chunk_id not in result:
                result.append(chunk_id)

    # A contextual-fallback claim often carries one materialized abstract plus
    # several broader body snippets.  Treating the abstract as an equal peer
    # makes the later packet validator combine it with the whole paragraph's
    # claim text; a single inferred mechanism/comparison then invalidates the
    # entire candidate.  Prefer canonical body/structured chunks whenever any
    # are available.  An abstract is retained only for an explicitly
    # paper-reported/background-style claim, where the downstream permission
    # guard can still require attribution and lexical grounding.
    non_abstract = [
        chunk_id
        for chunk_id in result
        if str(getattr(graph.chunks[chunk_id], "content_depth", "") or "")
        .strip()
        .casefold()
        not in {"abstract", "abstract_only", "abstract_claim", "tldr", "metadata", "title", "snippet"}
        and not chunk_id.casefold().startswith("s2abstract:")
    ]
    if non_abstract:
        return non_abstract

    claim_kind = str(claim.get("claim_kind") or "").strip().casefold()
    support_classification = str(
        claim.get("support_classification")
        or claim.get("claim_classification")
        or ""
    ).strip().casefold()
    if claim_kind in {
        "paper_reported_claim",
        "background",
        "trend",
        "candidate_lead",
        "author_synthesis",
    } or support_classification in {"open_question", "background"}:
        return result
    # No body/structured evidence is available for this claim.  Returning an
    # empty relation lets the local synthesizer record it as uncovered instead
    # of attaching an abstract to an inferred or ungrounded assertion.
    return []


def _build_local_evidence_relations(
    ctx: SectionAuthoringContext,
    graph: Any,
    claims: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build claim -> chunk -> paper -> permission relations from the graph.

    Only records already present in the canonical asset graph are used; no
    identifier is invented.  The writing permission is the most conservative
    of the explicit upstream claim permission, the strength-derived ceiling,
    and every supporting chunk/paper permission ceiling.
    """

    claim_map: Dict[str, Dict[str, Any]] = {}
    chunk_map: Dict[str, Dict[str, Any]] = {}
    paper_map: Dict[str, Dict[str, Any]] = {}
    for raw in claims:
        if not isinstance(raw, dict):
            continue
        claim_id = str(raw.get("claim_id") or "").strip()
        if not claim_id:
            continue
        statement = _statement_priority(raw)
        support = _claim_support_chunk_ids(raw, graph)
        papers: List[str] = []
        chunk_ceiling_ranks: List[int] = []
        chunk_ceiling_by_paper: Dict[str, List[int]] = {}
        for chunk_id in support:
            paper_id = str(graph.chunks[chunk_id].paper_id or "")
            if paper_id and paper_id not in papers:
                papers.append(paper_id)
            ceiling, _ = evidence_ceiling(
                _effective_asset_record(graph.chunks[chunk_id])
            )
            ceiling_rank = _PERMISSION_RANK.get(
                _permission_from_ceiling(ceiling), 1
            )
            chunk_ceiling_ranks.append(ceiling_rank)
            if paper_id:
                chunk_ceiling_by_paper.setdefault(paper_id, []).append(
                    ceiling_rank
                )
            relation = chunk_map.setdefault(chunk_id, {
                "chunk_id": chunk_id,
                "paper_id": str(graph.chunks[chunk_id].paper_id or ""),
                "use_permission": str(
                    graph.chunks[chunk_id].use_permission or ""
                ),
                "scope_fit": str(graph.chunks[chunk_id].scope_fit or ""),
                "content_depth": str(
                    graph.chunks[chunk_id].content_depth or ""
                ),
                "context_complete": bool(
                    graph.chunks[chunk_id].context_complete
                ),
                "literature_role": str(
                    graph.chunks[chunk_id].literature_role or ""
                ),
                "evidence_level": str(
                    graph.chunks[chunk_id].evidence_level or ""
                ),
                "claim_ids": [],
            })
            if claim_id not in relation["claim_ids"]:
                relation["claim_ids"].append(claim_id)
        # A paper is only as strong as its best verified local chunk; the
        # paper-level legacy downgrade (metadata/discovery) must not mask a
        # fulltext legacy fixture chunk that the registry treats as factual.
        paper_ceiling_ranks: List[int] = [
            max(chunk_ceiling_by_paper.get(paper_id, [1]))
            for paper_id in papers
            if chunk_ceiling_by_paper.get(paper_id)
        ]
        for paper_id in papers:
            paper = graph.papers.get(paper_id)
            paper_map.setdefault(paper_id, {
                "paper_id": paper_id,
                "use_permission": str(
                    paper.use_permission if paper else "discovery_only"
                ),
                "scope_fit": str(paper.scope_fit if paper else "unreviewed"),
                "content_depth": str(
                    paper.content_depth if paper else "metadata"
                ),
                "literature_role": str(
                    paper.literature_role if paper else ""
                ),
            })
        strength = str(raw.get("strength") or "qualified").casefold().strip()
        if strength not in _STRENGTH_PERMISSION:
            strength = "qualified"
        strength_permission = _STRENGTH_PERMISSION[strength]
        explicit_permission = _normalize_writing_permission(
            raw.get("writing_permission") or ""
        )
        permission_ranks = [
            _PERMISSION_RANK[strength_permission],
            *chunk_ceiling_ranks,
            *paper_ceiling_ranks,
        ]
        if explicit_permission in _VALID_WRITING_PERMISSIONS:
            permission_ranks.append(
                _PERMISSION_RANK[explicit_permission]
            )
        local_permission = (
            "evidence_gap_only"
            if not support
            else _CLAIM_PERMISSION_BY_RANK[min(permission_ranks)]
        )
        claim_map[claim_id] = {
            "claim_id": claim_id,
            "statement": statement,
            "support_chunk_ids": support,
            "paper_ids": papers,
            "strength": strength,
            "writing_permission": local_permission,
            "claim_kind": str(raw.get("claim_kind") or "").strip().casefold(),
            "support_classification": str(
                raw.get("support_classification")
                or raw.get("claim_classification")
                or ""
            ).strip().casefold(),
        }
    return {"claims": claim_map, "chunks": chunk_map, "papers": paper_map}


def _select_adaptive_core_chunks(
    *,
    claims: List[Dict[str, Any]],
    graph: Any,
    portfolio_recommended: List[str],
    core_limit: int,
    min_limit: int,
    max_limit: int,
    minimum_synthesis_sources: int = 0,
) -> Dict[str, Any]:
    """Deterministically select the core chunk batch with 8..16 bounds.

    Selection starts from the shared paper-diverse portfolio, then expands up
    to ``max_limit`` only when uncovered claims or under-represented papers
    exist, and shrinks down to ``min_limit`` only while every claim stays
    covered and paper diversity is preserved.  Chunk IDs break ties so the
    result is stable across runs.
    """

    # Normalize bounds once: 1 <= min <= default <= max.
    min_limit = max(1, min(int(min_limit or 1), int(max_limit or 1)))
    max_limit = max(min_limit, int(max_limit or 1))
    core_limit = min(max(int(core_limit or 1), min_limit), max_limit)

    all_ids = [str(item) for item in portfolio_recommended if str(item)]
    selected = [
        chunk_id
        for chunk_id in dict.fromkeys(all_ids)
        if chunk_id in graph.chunks
    ][:core_limit]
    diversity_floor = max(0, int(minimum_synthesis_sources or 0))

    def claim_support(claim: Dict[str, Any]) -> List[str]:
        return _claim_support_chunk_ids(claim, graph)

    def coverage(selected_ids: List[str]) -> Dict[str, Any]:
        chosen = set(selected_ids)
        covered = 0
        covered_ids: List[str] = []
        for claim in claims:
            support = [item for item in claim_support(claim) if item in chosen]
            if support:
                covered += 1
                covered_ids.append(str(claim.get("claim_id") or ""))
        papers = {
            str(graph.chunks[chunk_id].paper_id)
            for chunk_id in selected_ids
            if chunk_id in graph.chunks
            and str(graph.chunks[chunk_id].paper_id)
        }
        return {
            "covered": covered,
            "covered_claim_ids": covered_ids,
            "papers": papers,
            "total": len(claims),
        }

    state = coverage(selected)
    required_papers = max(
        1,
        min(
            int(minimum_synthesis_sources or 0) or len(state["papers"]) or 1,
            len(state["papers"]) or 1,
        ),
    )

    # Expand only for real marginal coverage/diversity gain.
    while len(selected) < max_limit:
        candidates = [
            chunk_id
            for chunk_id in graph.chunks
            if chunk_id not in selected
        ]
        best: Optional[str] = None
        best_key: Optional[tuple] = None
        papers_need_diversity = (
            diversity_floor > 0 and len(state["papers"]) < diversity_floor
        )
        for chunk_id in sorted(candidates):
            trial = coverage([*selected, chunk_id])
            new_claims = len(
                set(trial["covered_claim_ids"])
                - set(state["covered_claim_ids"])
            )
            new_papers = len(trial["papers"] - state["papers"])
            diversity_gain = new_papers if papers_need_diversity else 0
            key = (new_claims, diversity_gain, chunk_id)
            if best_key is None or key > best_key:
                best_key, best = key, chunk_id
        if best is None or best_key[:2] == (0, 0):
            break
        selected.append(best)
        state = coverage(selected)

    # Shrink redundant chunks only when coverage and diversity survive.
    shrunk = 0
    while len(selected) > min_limit:
        removable: List[tuple] = []
        for chunk_id in selected:
            trial_ids = [item for item in selected if item != chunk_id]
            trial = coverage(trial_ids)
            if trial["covered"] != state["covered"]:
                continue
            if len(trial["papers"]) < required_papers:
                continue
            unique = sum(
                1
                for other in selected
                if other != chunk_id
                and graph.chunks[other].paper_id == graph.chunks[chunk_id].paper_id
            )
            removable.append((unique, chunk_id))
        if not removable:
            break
        removable.sort(key=lambda item: (item[0], item[1]))
        selected = [item for item in selected if item != removable[0][1]]
        state = coverage(selected)
        shrunk += 1

    reason = "default"
    if shrunk:
        reason = "shrunk_redundant_without_coverage_loss"
    elif len(selected) > core_limit:
        reason = "expanded_for_claim_or_paper_coverage"
    elif len(graph.chunks) < min_limit:
        reason = "available_less_than_min"
    return {
        "chunk_ids": selected,
        "reason": reason,
        "covered_claim_count": state["covered"],
        "total_claim_count": state["total"],
        "unique_paper_count": len(state["papers"]),
    }


def _compact_claim_card(
    claim: Dict[str, Any],
    relation: Dict[str, Any],
    *,
    statement_cap: int = 800,
    boundary_cap: int = 160,
) -> Dict[str, Any]:
    claim_id = str(claim.get("claim_id") or "")
    return {
        "claim_id": claim_id,
        "statement": _short_text(_statement_priority(claim), statement_cap),
        "citation_handle": f"[[claim:{claim_id}]]",
        "strength": relation.get("strength") or claim.get("strength") or "qualified",
        "writing_permission": (
            relation.get("writing_permission")
            or claim.get("writing_permission")
            or "hedged_factual_assertion"
        ),
        "importance": str(claim.get("importance") or ""),
        "evidence_type": str(claim.get("evidence_type") or ""),
        "claim_kind": str(claim.get("claim_kind") or ""),
        "evidence_chunk_ids": list(relation.get("support_chunk_ids") or [])[:6],
        "paper_ids": list(relation.get("paper_ids") or [])[:4],
        "writing_boundary": _short_text(
            claim.get("writing_boundary")
            or claim.get("policy")
            or "",
            boundary_cap,
        ),
    }


def _resolve_claim_citation_handles(
    text: str,
    relations: Dict[str, Any],
    authorable_claim_ids: set[str],
) -> tuple[str, List[str], List[str], List[str]]:
    """Resolve semantic ``[[claim:ID]]`` handles to canonical REF markers.

    Open, unknown, and unmapped claims have no canonical paper to cite: their
    handle is removed without inventing a marker or blocking the candidate.
    """

    errors: List[str] = []
    warnings: List[str] = []
    used: List[str] = []
    claim_map = relations.get("claims") or {}

    def replace(match: re.Match) -> str:
        claim_id = match.group(1)
        if claim_id not in authorable_claim_ids:
            return ""
        relation = claim_map.get(claim_id, {})
        papers = list(relation.get("paper_ids") or [])
        if not papers:
            return ""
        used.append(claim_id)
        return "".join(f"[REF:{paper_id}]" for paper_id in papers)

    resolved = _CLAIM_HANDLE_RE.sub(replace, str(text or ""))
    return resolved, errors, list(dict.fromkeys(used)), list(dict.fromkeys(warnings))


def _paragraph_derived_permission(
    key_claims: List[str],
    claim_map: Dict[str, Dict[str, Any]],
) -> str:
    """Most conservative writing permission allowed by every listed claim."""

    ranks = []
    for claim_id in key_claims:
        relation = claim_map.get(claim_id, {})
        permission = relation.get("writing_permission") or "hedged_factual_assertion"
        ranks.append(_PERMISSION_RANK.get(permission, 3))
    if not ranks:
        return "interpretive_synthesis"
    return _CLAIM_PERMISSION_BY_RANK.get(
        min(ranks), "interpretive_synthesis"
    )


def _synthesize_local_candidate(
    candidate: Dict[str, Any],
    relations: Dict[str, Any],
    graph: Any,
    authorable_claim_ids: set[str],
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], List[str]]:
    """Synthesize plan evidence fields and the evidence packet locally.

    The model supplies paragraph structure and claim IDs only; chunk/paper IDs
    and permission tables come from the canonical local relations. Unknown or
    unmapped claim IDs are omitted without a hard error.
    """

    errors: List[str] = []
    claim_map = relations.get("claims") or {}
    plan_in = candidate.get("argument_plan") or {}
    raw_paragraphs = plan_in.get("paragraphs") or []
    if not isinstance(raw_paragraphs, list) or not raw_paragraphs:
        return None, None, ["argument_plan.paragraphs must be a non-empty list"]
    paragraphs: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_paragraphs):
        if not isinstance(raw, dict):
            errors.append(f"paragraph[{index}] must be an object")
            continue
        requested_claims = list(dict.fromkeys(
            str(value) for value in raw.get("key_claims") or [] if str(value)
        ))
        key_claims = [
            claim_id
            for claim_id in requested_claims
            if (
                claim_id in authorable_claim_ids
                and claim_id in claim_map
                and (
                    claim_map[claim_id].get("support_chunk_ids")
                    or (
                        str(claim_map[claim_id].get("strength") or "").casefold()
                        == "open"
                        or str(
                            claim_map[claim_id].get("support_classification") or ""
                        ).casefold()
                        == "open_question"
                        or str(claim_map[claim_id].get("claim_kind") or "").casefold()
                        == "open_question"
                    )
                )
            )
        ]
        evidence_ids: List[str] = []
        for claim_id in key_claims:
            for chunk_id in claim_map.get(claim_id, {}).get(
                "support_chunk_ids", []
            ):
                if chunk_id not in evidence_ids:
                    evidence_ids.append(chunk_id)
        papers: List[str] = []
        for chunk_id in evidence_ids:
            paper_id = str(graph.chunks[chunk_id].paper_id or "")
            if paper_id and paper_id not in papers:
                papers.append(paper_id)
        local_paragraph_permission = _paragraph_derived_permission(
            key_claims, claim_map
        )
        supplied_permission = _normalize_writing_permission(
            raw.get("writing_permission") or ""
        )
        permission = local_paragraph_permission
        if supplied_permission in _VALID_WRITING_PERMISSIONS:
            # A model-supplied permission may only downgrade the locally
            # derived ceiling; it must never strengthen it.
            if (
                _PERMISSION_RANK[supplied_permission]
                < _PERMISSION_RANK[local_paragraph_permission]
            ):
                permission = supplied_permission
        paragraphs.append({
            "paragraph_index": int(raw.get("paragraph_index", index)),
            "function": _short_text(raw.get("function") or "evidence", 120),
            "topic_sentence": _short_text(raw.get("topic_sentence") or "", 400),
            "key_claims": key_claims,
            "evidence_chunk_ids": evidence_ids,
            "paper_ids": papers,
            "writing_permission": permission,
            "expected_word_count": int(raw.get("expected_word_count") or 0),
        })
    if errors:
        return None, None, errors

    plan = {
        "argument_flow": _short_text(plan_in.get("argument_flow") or "", 900),
        "paragraphs": paragraphs,
        "open_questions": list(plan_in.get("open_questions") or [])[:10],
    }

    items_by_chunk_paper: Dict[tuple, Dict[str, Any]] = {}
    for paragraph in paragraphs:
        paragraph_permission = str(paragraph.get("writing_permission") or "")
        for claim_id in paragraph.get("key_claims") or []:
            relation = claim_map.get(claim_id, {})
            claim_permission = relation.get(
                "writing_permission", "hedged_factual_assertion"
            )
            statement = relation.get("statement") or ""
            for chunk_id in relation.get("support_chunk_ids") or []:
                if chunk_id not in paragraph.get("evidence_chunk_ids", []):
                    continue
                chunk = graph.chunks[chunk_id]
                paper_id = str(chunk.paper_id or "")
                if not paper_id:
                    continue
                key = (chunk_id, paper_id)
                item = items_by_chunk_paper.get(key)
                if item is None:
                    item = {
                        "chunk_id": chunk_id,
                        "paper_id": paper_id,
                        "claim_ids": [],
                        "writing_permission": claim_permission,
                        "support_hint": _short_text(statement, 90),
                    }
                    items_by_chunk_paper[key] = item
                if claim_id not in item["claim_ids"]:
                    item["claim_ids"].append(claim_id)
                item_permission = min(
                    (
                        item["writing_permission"],
                        claim_permission,
                        paragraph_permission,
                    ),
                    key=lambda value: _PERMISSION_RANK.get(value, 3),
                )
                item["writing_permission"] = item_permission
    items = [
        items_by_chunk_paper[key]
        for key in sorted(items_by_chunk_paper)
    ]
    claims_with_evidence = {
        str(claim_id)
        for item in items
        for claim_id in item.get("claim_ids") or []
    }
    uncovered = [
        claim_id
        for claim_id in sorted(authorable_claim_ids)
        if claim_id not in claims_with_evidence
    ]
    packet = {"items": items, "uncovered_claim_ids": uncovered}
    return plan, packet, []


def _assemble_compact_workspace(
    *,
    ctx: SectionAuthoringContext,
    context_result: Dict[str, Any],
    material_result: Dict[str, Any],
    portfolio: Dict[str, Any],
    visual_assets: List[Any],
    retrieve,
    core_limit: int,
    min_limit: int,
    max_limit: int,
    target_tokens: int,
    allowance_tokens: int,
) -> Dict[str, Any]:
    """Assemble the structured compact workspace with diagnostics.

    Every authorable claim is preserved as a concise claim card.  Chunk text
    is exposed as bounded excerpts sized so the serialized workspace stays at
    or below the per-authoring tool-result allowance; claim cards are never
    dropped to fit.
    """

    graph = _build_asset_graph(ctx)
    claims = [
        item
        for item in context_result.get("claims") or []
        if isinstance(item, dict) and str(item.get("claim_id") or "")
    ]
    relations = _build_local_evidence_relations(ctx, graph, claims)
    selection = _select_adaptive_core_chunks(
        claims=claims,
        graph=graph,
        portfolio_recommended=list(
            portfolio.get("recommended_batch_chunk_ids") or []
        ),
        core_limit=core_limit,
        min_limit=min_limit,
        max_limit=max_limit,
        minimum_synthesis_sources=int(
            portfolio.get("minimum_synthesis_sources") or 0
        ),
    )
    selected_ids = selection["chunk_ids"]
    retrieved = (
        _loads(retrieve(json.dumps(selected_ids, ensure_ascii=False)))
        if selected_ids
        else {"status": "ok", "chunks": {}}
    )
    retrieved_chunks = retrieved.get("chunks") or {}
    selected_ids = [
        chunk_id for chunk_id in selected_ids if chunk_id in retrieved_chunks
    ]

    claim_map = relations.get("claims") or {}
    claim_cards = [
        _compact_claim_card(claim, claim_map.get(str(claim.get("claim_id")), {}))
        for claim in claims
    ]
    claim_evidence_map = []
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        support = list(
            dict.fromkeys(
                item
                for item in claim_map.get(claim_id, {}).get(
                    "support_chunk_ids", []
                )
                if item in selected_ids
            )
        )
        claim_evidence_map.append({
            "claim_id": claim_id,
            "effective_statement": _short_text(
                _statement_priority(claim),
                560,
            ),
            "recommended_chunk_ids": support,
            "claim_classification": str(
                claim.get("support_classification")
                or claim.get("claim_classification")
                or ""
            ),
            "missing_evidence_components": list(
                claim.get("missing_evidence_components") or []
            )[:3],
        })

    excerpt_caps = (
        4000, 3200, 2800, 2400, 2000, 1600, 1200, 900, 640, 400, 240, 160,
    )
    submission_contract = {
        "local_evidence_ownership": True,
        "coverage_policy": {
            "authorable_claim_count": len(claims),
            "use_all_distinct_claims_when_material_allows": True,
            "merge_duplicate_claims_in_one_paragraph": True,
            "list_omitted_claim_ids_in_uncovered_claim_ids": True,
            "preferred_word_budget_is_soft": True,
            "avoid_short_four_paragraph_default_when_more_claims_are_available": True,
        },
        "argument_plan": {
            "paragraph_fields": [
                "paragraph_index",
                "function",
                "topic_sentence",
                "key_claims",
                "expected_word_count",
            ],
            "note": (
                "Do not supply evidence_chunk_ids, paper_ids, or "
                "writing_permission tables.  The harness resolves "
                "claim->chunk->paper->permission locally from the "
                "canonical asset graph."
            ),
            "paragraphs": [{
                "paragraph_index": 0,
                "function": "one paragraph's argumentative job",
                "topic_sentence": "planned topic sentence",
                "key_claims": ["exact claim_id from claim_cards"],
                "expected_word_count": 250,
            }],
            "open_questions": [],
        },
        "draft_text": (
            "Complete English prose. Cite claims with [[claim:CLAIM_ID]] "
            "handles; the harness resolves them to canonical [REF:paper_id] "
            "markers. Open/evidence-gap claims carry no handle."
        ),
        "visual_placements": "optional canonical placement list",
        "handoff_card": "optional compact cross-section memory",
    }

    def assemble(excerpt_cap: int) -> Dict[str, Any]:
        chunks_payload: Dict[str, Any] = {}
        relations_payload: Dict[str, Any] = {}
        for chunk_id in selected_ids:
            raw = retrieved_chunks.get(chunk_id) or {}
            chunk = graph.chunks.get(chunk_id)
            text = _short_text(
                raw.get("text") or raw.get("normalized_text") or "",
                excerpt_cap,
            )
            chunks_payload[chunk_id] = {
                "text": text,
                "paper_id": str(raw.get("paper_id") or (chunk.paper_id if chunk else "")),
                "paper_title": str(
                    raw.get("paper_title") or (chunk.paper_title if chunk else "")
                ),
                "evidence_level": str(
                    raw.get("evidence_level") or (chunk.evidence_level if chunk else "")
                ),
            }
            relation = relations.get("chunks", {}).get(chunk_id, {})
            relations_payload[chunk_id] = {
                "paper_id": relation.get("paper_id") or chunks_payload[chunk_id]["paper_id"],
                "use_permission": relation.get("use_permission", ""),
                "scope_fit": relation.get("scope_fit", ""),
                "content_depth": relation.get("content_depth", ""),
                "context_complete": bool(relation.get("context_complete", False)),
                "literature_role": relation.get("literature_role", ""),
                "claim_ids": list(relation.get("claim_ids") or [])[:6],
            }
        return {
            "status": "ok",
            "protocol": "compact_section_authoring.v2",
            "context": _compact_context(context_result),
            "materials": _compact_material(material_result),
            "claim_cards": claim_cards,
            "claim_evidence_map": claim_evidence_map,
            "evidence_relations": {
                "chunks": relations_payload,
                "papers": dict(relations.get("papers") or {}),
            },
            "retrieved_chunks": chunks_payload,
            "retrieval_diagnostics": {
                "requested_ids": selected_ids,
                "claim_anchor_chunk_ids": list(
                    portfolio.get("claim_anchor_chunk_ids") or []
                ),
                "missing_ids": retrieved.get("missing") or [],
                "minimum_synthesis_sources": portfolio.get(
                    "minimum_synthesis_sources", 0
                ),
                "available_synthesis_sources": portfolio.get(
                    "available_synthesis_sources", 0
                ),
            },
            "eligible_visuals": list(visual_assets)[:8],
            "submission_contract": submission_contract,
            "workspace_diagnostics": {
                "schema_version": "compact_section_authoring.workspace.v2",
                "claim_count": len(claims),
                "selected_chunk_count": len(selected_ids),
                "unique_paper_count": selection["unique_paper_count"],
                "claim_coverage": selection["covered_claim_count"],
                "total_claim_count": selection["total_claim_count"],
                "coverage_ratio": (
                    round(
                        selection["covered_claim_count"]
                        / max(1, selection["total_claim_count"]),
                        4,
                    )
                ),
                "selection_bounds": {
                    "min": min_limit,
                    "default": core_limit,
                    "max": max_limit,
                    "selected": len(selected_ids),
                    "reason": selection["reason"],
                },
                "excerpt_chars": excerpt_cap,
                "target_tokens": target_tokens,
                "tool_result_allowance_tokens": allowance_tokens,
                "estimated_token_size": 0,
                "truncation_risk": True,
            },
        }

    payload: Dict[str, Any] = assemble(excerpt_caps[-1])
    chosen_cap = excerpt_caps[-1]
    for cap in excerpt_caps:
        candidate = assemble(cap)
        estimated = _estimate_workspace_tokens(candidate)
        if estimated <= target_tokens:
            payload = candidate
            chosen_cap = cap
            break
        payload = candidate
        chosen_cap = cap
    estimated = _estimate_workspace_tokens(payload)
    payload["workspace_diagnostics"]["excerpt_chars"] = chosen_cap
    payload["workspace_diagnostics"]["estimated_token_size"] = estimated
    payload["workspace_diagnostics"]["truncation_risk"] = (
        estimated > allowance_tokens
    )
    if estimated > allowance_tokens:
        return {
            "status": "workspace_too_large",
            "protocol": "compact_section_authoring.v2",
            "claim_cards": claim_cards,
            "submission_contract": submission_contract,
            "workspace_diagnostics": payload["workspace_diagnostics"],
            "instruction": (
                "The structured workspace exceeds the per-authoring "
                "tool-result allowance even at minimum excerpt size. "
                "Deterministic validation remains available; submit a "
                "candidate using claim IDs from the claim cards, or reduce "
                "the section scope. No semantic contract was silently dropped."
            ),
        }
    return payload


def _repair_premature_root_close(value: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Repair one unambiguous premature root-closing brace.

    The only accepted shape is: ``{...}}, "field": value...}`` where
    ``json.JSONDecoder.raw_decode`` returns the first valid dict and the
    remaining suffix starts with ``,`` and contains the original root members.
    We remove only the single closing brace immediately before that suffix.
    Ambiguous or arbitrary trailing JSON is not repaired.
    """

    stripped = value.strip()
    decoder = json.JSONDecoder()
    try:
        first, end = decoder.raw_decode(stripped)
    except Exception as exc:
        raise ValueError(f"candidate_json is not valid JSON: {exc}") from exc
    if not isinstance(first, dict):
        raise ValueError("candidate_json root must be an object")
    if end <= 0 or stripped[end - 1] != "}":
        raise ValueError("premature root-close repair boundary is ambiguous")
    suffix = stripped[end:].lstrip()
    if not suffix.startswith(","):
        raise ValueError("candidate_json has non-continuation trailing data")
    repaired = stripped[: end - 1] + stripped[end:]
    try:
        parsed = json.loads(repaired)
    except Exception as exc:
        raise ValueError(f"candidate_json repair produced invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("candidate_json repair root is not an object")
    return parsed, {
        "event": "premature_root_close_repaired",
        "first_object_end": end,
        "suffix_prefix": suffix[:80],
    }


def parse_candidate_json(value: str) -> tuple[Dict[str, Any], Dict[str, Any] | None]:
    """Parse candidate JSON with the narrow root-close repair."""

    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("object required")
        return parsed, None
    except json.JSONDecodeError as exc:
        if "Extra data" not in str(exc):
            raise
        return _repair_premature_root_close(value)


def _compact_context(value: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the intellectual contract without replaying complete inventories."""

    return {
        key: value.get(key)
        for key in (
            "section_id",
            "section_title",
            "chapter_argument",
            "coverage_status",
            "total_sources",
            "sources_by_role",
            "blocking_gaps_remain",
            "gap_summary",
            "claim_strength_policy",
            "section_contract",
            "minimum_synthesis_sources",
            "available_synthesis_sources",
            "scope_guardrails",
            "revision_mode",
            "revision_instructions",
            "existing_draft_text",
        )
        if value.get(key) not in (None, "", [], {})
    }


def _compact_material(value: Dict[str, Any]) -> Dict[str, Any]:
    role_detail = {}
    for role, raw in (value.get("role_detail") or {}).items():
        if not isinstance(raw, dict):
            continue
        role_detail[str(role)] = {
            "chunk_count": raw.get("chunk_count", 0),
            "paper_count": raw.get("paper_count", 0),
            "status": raw.get("status", "unknown"),
        }
    return {
        "coverage_status": value.get("coverage_status", "unknown"),
        "total_sources": value.get("total_sources", 0),
        "blocking_gaps_remain": bool(value.get("blocking_gaps_remain", False)),
        "role_detail": role_detail,
        "documented_gaps": list(value.get("documented_gaps") or [])[:5],
    }


def _blocking_audit(value: Dict[str, Any]) -> Dict[str, Any]:
    flags = [
        item
        for item in (value.get("flags_detail") or [])
        if isinstance(item, dict) and item.get("severity") == "blocking"
    ]
    return {
        "audit_passed": bool(value.get("audit_passed", False)),
        "total_flags": int(value.get("total_flags", 0) or 0),
        "blocking_flags": int(value.get("blocking_flags", 0) or 0),
        "blocking_batch": flags[:12],
        "papers_cited": list(value.get("papers_cited") or [])[:30],
    }


_HARD_AUDIT_FLAG_TYPES = frozenset({
    "unknown_ref",
    "missing_citation_mapping",
    "sentence_mapping_mismatch",
    "paper_chunk_mismatch",
    "scope_violation",
})


def _hard_audit_blockers(audit: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return unresolved blocking provenance failures from the full flag list.

    ``_blocking_audit`` deliberately caps the user-facing batch at twelve to
    bound message size.  The promotion gate must scan every blocking flag so a
    hard provenance failure is never hidden behind earlier soft blockers.
    """

    return [
        item
        for item in audit.get("flags_detail") or []
        if isinstance(item, dict)
        and str(item.get("severity") or "") == "blocking"
        and not bool(item.get("resolved"))
        and str(item.get("flag_type") or item.get("type") or "")
        in _HARD_AUDIT_FLAG_TYPES
    ]


_COMPACT_UNCOMMITTED_FILES = (
    "SECTION_DRAFT_EN.md",
    "SECTION_EVIDENCE_PACKET.json",
    "SECTION_CITATION_MAP.json",
    "SECTION_AUTHORING_AUDIT.json",
    "SECTION_AUTHORING_PACKAGE.json",
    "SECTION_REVISION_HISTORY.json",
    "SECTION_REVISION_CONTROL.json",
    "_audit_stale",
)


def _rollback_uncommitted_candidate(ctx: SectionAuthoringContext) -> bool:
    """Restore the pointer-selected candidate or clear the failed transaction."""

    if _restore_last_valid_section_candidate(ctx.work_dir):
        return True
    for name in _COMPACT_UNCOMMITTED_FILES:
        path = ctx.work_dir / name
        if path.is_file():
            path.unlink()
    return False


def _fill_candidate_evidence_defaults(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Fill omitted evidence fields from the same candidate's exact plan IDs.

    The compact transaction validates the evidence packet before persisting the
    argument plan.  Models frequently leave ``claim_ids`` blank even though the
    submitted plan already maps the exact chunk to canonical claims.  Requiring
    another model turn for that table-copy is wasteful; this deterministic fill
    never invents an ID, and the normal evidence/plan validators still reject
    unknown or mismatched values.
    """

    packet = candidate.get("evidence_packet")
    if not isinstance(packet, dict):
        return packet if isinstance(packet, dict) else {}
    defaults: Dict[str, Dict[str, Any]] = {}
    plan = candidate.get("argument_plan")
    for paragraph in (
        plan.get("paragraphs") or [] if isinstance(plan, dict) else []
    ):
        if not isinstance(paragraph, dict):
            continue
        claim_ids = [
            str(value)
            for value in paragraph.get("key_claims") or []
            if str(value)
        ]
        permission = str(paragraph.get("writing_permission") or "")
        for raw_chunk_id in paragraph.get("evidence_chunk_ids") or []:
            chunk_id = str(raw_chunk_id or "")
            if not chunk_id:
                continue
            row = defaults.setdefault(
                chunk_id, {"claim_ids": [], "writing_permission": ""}
            )
            row["claim_ids"] = list(
                dict.fromkeys([*row["claim_ids"], *claim_ids])
            )
            if permission and not row["writing_permission"]:
                row["writing_permission"] = permission
    normalized = dict(packet)
    items = []
    for raw in packet.get("items") or []:
        if not isinstance(raw, dict):
            items.append(raw)
            continue
        item = dict(raw)
        default = defaults.get(str(item.get("chunk_id") or ""), {})
        if not item.get("claim_ids") and default.get("claim_ids"):
            item["claim_ids"] = list(default["claim_ids"])
        if not item.get("writing_permission") and default.get(
            "writing_permission"
        ):
            item["writing_permission"] = default["writing_permission"]
        items.append(item)
    normalized["items"] = items
    return normalized


class CompactSectionAuthoringToolProvider(ToolProvider):
    """Expose a coarse, bounded authoring protocol backed by canonical tools."""

    def __init__(self, ctx: SectionAuthoringContext) -> None:
        self._ctx = ctx
        # Keep retrieval's served-ID memory for the lifetime of this provider.
        self._retrieve = _make_retrieve_chunk_text(ctx)
        self._workspace_prepared = False
        self._workspace: Optional[Dict[str, Any]] = None
        self._local_relations_cache: Optional[Dict[str, Any]] = None

    def _local_relations(self) -> Dict[str, Any]:
        """Claim -> chunk -> paper -> permission relations from the graph."""

        if self._local_relations_cache is None:
            graph = _build_asset_graph(self._ctx)
            claims = [
                item
                for item in (self._ctx.section_data or {}).get("claims") or []
                if isinstance(item, dict)
            ]
            self._local_relations_cache = _build_local_evidence_relations(
                self._ctx, graph, claims
            )
        return self._local_relations_cache

    def _authorable_claim_ids(self) -> set[str]:
        return {
            str(item.get("claim_id") or "")
            for item in (self._ctx.section_data or {}).get("claims") or []
            if isinstance(item, dict) and str(item.get("claim_id") or "")
        }

    def get_tools(self, work_dir: Path) -> List[FunctionTool]:
        ctx = self._ctx

        def prepare_authoring_workspace() -> str:
            """Load one compact writing contract and a bounded evidence batch.

            Call once.  The result contains the section job, diverse canonical
            chunks, exact IDs, permission boundaries, and eligible visuals.
            Do not call another discovery tool: write from this workspace.
            """

            if self._workspace_prepared:
                return json.dumps({
                    "status": "already_prepared",
                    "protocol": "compact_section_authoring.v2",
                    "instruction": (
                        "Use the complete workspace already returned in this "
                        "conversation and submit the authoring candidate now."
                    ),
                }, ensure_ascii=False)

            context_result = _loads(_make_load_authoring_context(ctx)())
            if context_result.get("status") == "error":
                return json.dumps(context_result, ensure_ascii=False)
            material_result = _loads(_make_inspect_material_package(ctx)())
            if material_result.get("status") == "error":
                return json.dumps(material_result, ensure_ascii=False)
            portfolio = material_result.get("evidence_portfolio") or {}
            section_data = ctx.section_data or {}
            raw_min = max(
                1,
                int(section_data.get("authoring_core_chunk_min") or 0)
                or DEFAULT_CORE_CHUNK_MIN,
            )
            raw_max = max(
                1,
                int(section_data.get("authoring_core_chunk_max") or 0)
                or DEFAULT_CORE_CHUNK_MAX,
            )
            raw_core = max(
                1,
                int(section_data.get("authoring_core_chunk_limit") or 0)
                or DEFAULT_CORE_CHUNK_LIMIT,
            )
            # Coherent bounds: 1 <= min <= default <= max.
            min_limit = min(raw_min, raw_max)
            max_limit = max(raw_min, raw_max)
            core_limit = min(max(raw_core, min_limit), max_limit)
            target_tokens = max(
                1,
                int(section_data.get("compact_workspace_target_tokens") or 0)
                or DEFAULT_WORKSPACE_TARGET_TOKENS,
            )
            allowance_tokens = max(
                1,
                int(section_data.get("compact_tool_result_limit") or 0)
                or DEFAULT_TOOL_RESULT_ALLOWANCE_TOKENS,
            )
            visuals = _loads(_make_inspect_visual_assets(ctx)())
            payload = _assemble_compact_workspace(
                ctx=ctx,
                context_result=context_result,
                material_result=material_result,
                portfolio=portfolio,
                visual_assets=list(visuals.get("visual_assets") or []),
                retrieve=self._retrieve,
                core_limit=core_limit,
                min_limit=min_limit,
                max_limit=max_limit,
                target_tokens=target_tokens,
                allowance_tokens=allowance_tokens,
            )
            self._workspace = payload
            self._workspace_prepared = payload.get("status") == "ok"
            return json.dumps(payload, ensure_ascii=False)

        def submit_authoring_candidate(
            candidate_json: str = "",
            draft_text: str = "",
            argument_plan: "dict | None" = None,
            evidence_packet: "dict | None" = None,
            visual_placements: "list | None" = None,
            handoff_card: "dict | None" = None,
            **extra: Any,
        ) -> str:
            """Submit plan, evidence packet and full prose in one transaction.

            Supply EITHER candidate_json (canonical form) OR the individual
            fields draft_text / argument_plan / evidence_packet /
            visual_placements / handoff_card (flat-field form).  Both forms
            are normalized into the same candidate transaction; all gates are
            applied identically regardless of which form was used.

            Args:
                candidate_json: JSON string containing argument_plan,
                    draft_text, and optional evidence_packet /
                    visual_placements / handoff_card.  Provide this OR the
                    individual fields below, not both.
                draft_text: Complete English prose.  Cite claims with
                    [[claim:CLAIM_ID]] handles; the harness resolves them to
                    canonical [REF:paper_id] markers.
                argument_plan: Paragraph structure object with keys
                    paragraph_index, function, topic_sentence, key_claims,
                    expected_word_count.  evidence_chunk_ids / paper_ids /
                    writing_permission are resolved locally.
                evidence_packet: Optional explicit evidence packet.  When
                    omitted the harness synthesizes it from the asset graph.
                visual_placements: Optional list of visual placement objects.
                handoff_card: Optional compact cross-section memory dict.
            """

            repair_event: Optional[Dict[str, Any]] = None
            if candidate_json.strip():
                # Canonical form: parse the JSON string.
                try:
                    candidate, repair_event = parse_candidate_json(candidate_json)
                except Exception as exc:
                    return json.dumps(
                        {"status": "error", "stage": "parse", "error": str(exc)},
                        ensure_ascii=False,
                    )
            elif draft_text or argument_plan is not None or extra:
                # Normalize completion-style envelope variants.  Canonical
                # parameters remain authoritative; aliases are used only when
                # the corresponding canonical field was omitted.
                if not draft_text:
                    for alias in (
                        "section_text",
                        "section_draft",
                        "draft",
                        "body",
                        "content",
                        "text",
                    ):
                        value = extra.get(alias)
                        if isinstance(value, str) and value.strip():
                            draft_text = value
                            break
                if argument_plan is None:
                    plan_alias = extra.get("paragraph_plan")
                    if isinstance(plan_alias, dict):
                        argument_plan = plan_alias
                    elif isinstance(extra.get("paragraphs"), list):
                        argument_plan = {
                            "argument_flow": str(
                                extra.get("argument_flow")
                                or extra.get("key_transition")
                                or ""
                            ),
                            "paragraphs": extra["paragraphs"],
                            "open_questions": list(
                                extra.get("open_questions") or []
                            ),
                        }
                if evidence_packet is None:
                    for alias in ("evidence", "evidence_items", "chunks_used"):
                        value = extra.get(alias)
                        if isinstance(value, (dict, list)):
                            evidence_packet = (
                                value
                                if isinstance(value, dict)
                                else {"items": value}
                            )
                            break
                # Flat-field form: assemble the canonical candidate dict from
                # individually-supplied parameters.  No gate is relaxed; the
                # assembled dict is processed identically to a parsed JSON string.
                candidate: Dict[str, Any] = {}
                if argument_plan is not None:
                    candidate["argument_plan"] = argument_plan
                if draft_text:
                    candidate["draft_text"] = draft_text
                if evidence_packet is not None:
                    candidate["evidence_packet"] = evidence_packet
                if visual_placements is not None:
                    candidate["visual_placements"] = visual_placements
                if handoff_card is not None:
                    candidate["handoff_card"] = handoff_card
            else:
                return json.dumps(
                    {
                        "status": "error",
                        "stage": "parse",
                        "error": (
                            "No candidate supplied: provide candidate_json or "
                            "at least draft_text / argument_plan."
                        ),
                    },
                    ensure_ascii=False,
                )
            if repair_event is not None:
                _write_artifact(
                    ctx.work_dir,
                    "SECTION_JSON_REPAIR_EVENTS.json",
                    {
                        "schema_version": "compact_section_authoring.json_repair.v1",
                        "section_id": ctx.section_id,
                        "events": [repair_event],
                    },
                )
            # Persist nonempty English prose before any relation synthesis.
            # Local binding is recoverable bookkeeping; it must never be able
            # to erase the first usable body returned by the model.
            provisional_text = str(candidate.get("draft_text") or "").strip()
            draft = _loads(
                _make_submit_section_draft(ctx, persist_last_valid=True)(
                    provisional_text, "compact provisional draft"
                )
            )
            if draft.get("status") not in {"ok", "revised", "already_completed"}:
                return json.dumps(
                    {"status": "repair_required", "stage": "draft", "detail": draft},
                    ensure_ascii=False,
                )
            last_valid_candidate = dict(draft.get("last_valid_candidate") or {})
            # Local evidence ownership: the model supplies paragraph structure
            # and claim IDs only.  When the candidate carries no explicit
            # evidence tables, synthesize the canonical plan evidence fields
            # and the evidence packet locally from the asset graph, and
            # resolve semantic citation handles to canonical REF markers.
            plan_supplied = candidate.get("argument_plan") or {}
            raw_paragraphs = plan_supplied.get("paragraphs") or []
            omitted_claim_ids: List[str] = []
            local_relation_warnings: List[str] = []
            prevalidated_plan: Optional[Dict[str, Any]] = None
            # Compact authoring is deliberately local-binding.  A model may
            # echo evidence_chunk_ids/paper_ids or an evidence_packet even
            # though the submission contract tells it not to.  Never let
            # valid model-supplied tables bypass the canonical claim→chunk→
            # paper relation graph: abstract-only chunks can otherwise be
            # promoted into support for ungrounded technical claims and cause
            # a long, non-converging repair loop.  Keep the legacy explicit
            # path only long enough for the normal validator to reject an
            # unknown chunk/paper ID; this preserves precise repair feedback
            # for malformed IDs without allowing known but unsafe abstract
            # evidence to bypass local synthesis.
            graph_for_explicit_check = _build_asset_graph(ctx)
            explicit_reference_errors = []
            for paragraph in raw_paragraphs:
                if not isinstance(paragraph, dict):
                    continue
                for raw_chunk_id in paragraph.get("evidence_chunk_ids") or []:
                    chunk_id = str(raw_chunk_id or "")
                    if chunk_id and chunk_id not in graph_for_explicit_check.chunks:
                        explicit_reference_errors.append(
                            f"unknown evidence_chunk_id: {chunk_id}"
                        )
                for raw_paper_id in paragraph.get("paper_ids") or []:
                    paper_id = str(raw_paper_id or "")
                    if paper_id and paper_id not in graph_for_explicit_check.papers:
                        explicit_reference_errors.append(
                            f"unknown paper_id: {paper_id}"
                        )
            supplied_packet = candidate.get("evidence_packet")
            if isinstance(supplied_packet, dict):
                for item in supplied_packet.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    chunk_id = str(item.get("chunk_id") or "")
                    paper_id = str(item.get("paper_id") or "")
                    if chunk_id and chunk_id not in graph_for_explicit_check.chunks:
                        explicit_reference_errors.append(
                            f"unknown evidence_packet chunk_id: {chunk_id}"
                        )
                    if paper_id and paper_id not in graph_for_explicit_check.papers:
                        explicit_reference_errors.append(
                            f"unknown evidence_packet paper_id: {paper_id}"
                        )
            has_explicit_relations = bool(explicit_reference_errors)
            if not has_explicit_relations:
                relations = self._local_relations()
                authorable_ids = self._authorable_claim_ids()
                resolved_draft, handle_errors, _used_claims, handle_warnings = (
                    _resolve_claim_citation_handles(
                        str(candidate.get("draft_text") or ""),
                        relations,
                        authorable_ids,
                    )
                )
                local_relation_warnings = list(handle_warnings)
                local_plan, local_packet, synth_errors = (
                    _synthesize_local_candidate(
                        candidate,
                        relations,
                        _build_asset_graph(ctx),
                        authorable_ids,
                    )
                )
                errors = [*handle_errors, *synth_errors]
                if errors:
                    return json.dumps(
                        {
                            "status": "repair_required",
                            "stage": "local_relations",
                            "errors": errors[:12],
                            "count": len(errors),
                            "provisional_draft_preserved": True,
                            "last_valid_candidate": last_valid_candidate,
                        },
                        ensure_ascii=False,
                    )
                candidate = dict(candidate)
                candidate["argument_plan"] = local_plan
                candidate["evidence_packet"] = local_packet
                candidate["draft_text"] = resolved_draft
                # Replace the working draft only after local resolution
                # succeeds; the earlier snapshot remains the recovery point.
                _make_submit_section_draft(ctx, persist_last_valid=False)(
                    resolved_draft, "locally resolved citation handles"
                )
                omitted_claim_ids = list(local_packet.get("uncovered_claim_ids") or [])
                _write_artifact(
                    ctx.work_dir,
                    "SECTION_OMITTED_CLAIMS.json",
                    {
                        "schema_version": "compact_section_authoring.omitted_claims.v1",
                        "section_id": ctx.section_id,
                        "omitted_claim_ids": omitted_claim_ids,
                        "count": len(omitted_claim_ids),
                    },
                )
                # Persist the locally synthesized plan before the citation
                # audit.  The audit may legitimately stop on a hard prose
                # flag; keeping the already-validated plan alongside the
                # draft/evidence/citation files makes the documented fail-open
                # handoff complete and resumable.
                prevalidated_plan = _loads(
                    _make_submit_argument_plan(ctx)(
                        json.dumps(candidate.get("argument_plan") or {}, ensure_ascii=False)
                    )
                )
                if prevalidated_plan.get("status") not in {
                    "ok",
                    "revised",
                    "already_completed",
                }:
                    return json.dumps(
                        {
                            "status": "repair_required",
                            "stage": "argument_plan",
                            "detail": prevalidated_plan,
                            "last_valid_candidate": last_valid_candidate,
                            "instruction": (
                                "Correct only the listed contract or provenance errors. "
                                "A usable draft has already been preserved; repair the "
                                "plan without discarding it."
                            ),
                        },
                        ensure_ascii=False,
                    )
            evidence = _loads(
                _make_build_evidence_packet(ctx)(
                    json.dumps(
                        _fill_candidate_evidence_defaults(candidate),
                        ensure_ascii=False,
                    )
                )
            )
            if evidence.get("status") not in {
                "ok",
                "extended",
                "already_completed",
            }:
                return json.dumps(
                    {
                        "status": "repair_required",
                        "stage": "evidence_packet",
                        "detail": evidence,
                        "provisional_draft_preserved": True,
                        "last_valid_candidate": last_valid_candidate,
                    },
                    ensure_ascii=False,
                )
            visual_result: Dict[str, Any] = {"status": "not_requested"}
            audit = _loads(_make_run_citation_audit(ctx)("[]"))
            audit_view = _blocking_audit(audit)
            hard_audit_blockers = _hard_audit_blockers(audit)
            if not hard_audit_blockers and audit.get("status") != "error":
                saved = _persist_last_valid_section_candidate(
                    ctx,
                    summary=(
                        "citation-audited candidate with soft limits"
                        if audit_view.get("blocking_flags")
                        else "citation-audited candidate"
                    ),
                    validation_level=(
                        "audited_with_limits"
                        if audit_view.get("blocking_flags")
                        else "citation_audited"
                    ),
                )
                if saved.get("saved"):
                    last_valid_candidate = saved
            if audit_view["blocking_flags"]:
                if hard_audit_blockers:
                    # Keep the provisional prose and mark only evidence as
                    # degraded.  A failed relation must not roll back the
                    # body that was already durably written above.
                    last_valid_candidate = {
                        **last_valid_candidate,
                        "reason": "hard_citation_audit_failure",
                        "provisional_draft_preserved": True,
                    }
                return json.dumps(
                    {
                        "status": "revision_required",
                        "stage": "citation_audit",
                        "audit": audit_view,
                        "visual": visual_result,
                        "last_valid_candidate": last_valid_candidate,
                        "instruction": (
                            "Revise the complete blocking batch in one pass; remove or "
                            "qualify unsupported technical detail rather than changing IDs."
                        ),
                    },
                    ensure_ascii=False,
                )
            plan = prevalidated_plan or _loads(
                _make_submit_argument_plan(ctx)(
                    json.dumps(candidate.get("argument_plan") or {}, ensure_ascii=False)
                )
            )
            if plan.get("status") not in {"ok", "revised", "already_completed"}:
                return json.dumps(
                    {
                        "status": "repair_required",
                        "stage": "argument_plan",
                        "detail": plan,
                        "last_valid_candidate": last_valid_candidate,
                        "instruction": (
                            "Correct only the listed contract or provenance errors. "
                            "A usable draft has already been preserved; repair the "
                            "plan without discarding it. If allowed_assets are present, "
                            "reuse those exact chunk/paper pairs."
                        ),
                    },
                    ensure_ascii=False,
                )
            # Include the now-accepted plan in the pointer-selected snapshot.
            saved = _persist_last_valid_section_candidate(
                ctx,
                summary="argument plan accepted",
                validation_level="plan_validated",
            )
            if saved.get("saved"):
                last_valid_candidate = saved
            placements = candidate.get("visual_placements")
            if isinstance(placements, list) and placements:
                visual_result = _loads(
                    _make_submit_visual_placement(ctx)(
                        json.dumps(placements, ensure_ascii=False)
                    )
                )
                if visual_result.get("status") not in {"ok", "already_completed"}:
                    # A visual must never block otherwise valid scientific prose.
                    visual_result["nonblocking"] = True
            handoff = candidate.get("handoff_card")
            handoff_result: Dict[str, Any] = {"status": "not_requested"}
            if isinstance(handoff, dict) and handoff:
                handoff_result = _loads(
                    _make_submit_section_handoff_card(ctx)(
                        json.dumps(handoff, ensure_ascii=False)
                    )
                )
            validation = _make_validate_authoring_package(ctx)()
            return json.dumps(
                {
                    "status": (
                        "completed" if "VALIDATION_PASSED" in validation else "repair_required"
                    ),
                    "stage": "validation",
                    "validation": validation,
                    "audit": audit_view,
                    "visual": visual_result,
                    "handoff": handoff_result,
                    "last_valid_candidate": last_valid_candidate,
                    "omitted_claim_ids": omitted_claim_ids,
                    "local_relation_warnings": local_relation_warnings,
                },
                ensure_ascii=False,
            )

        def revise_authoring_candidate(
            revised_text: str,
            flags_resolved_json: str = "[]",
            summary: str = "bounded audit repair",
        ) -> str:
            """Apply one full blocking-batch prose repair, re-audit and validate."""

            resolved_text, handle_errors, _used_claims, handle_warnings = (
                _resolve_claim_citation_handles(
                    revised_text,
                    self._local_relations(),
                    self._authorable_claim_ids(),
                )
            )
            if handle_errors:
                return json.dumps(
                    {
                        "status": "repair_required",
                        "stage": "local_relations",
                        "errors": handle_errors[:12],
                        "count": len(handle_errors),
                    },
                    ensure_ascii=False,
                )
            revised_text = resolved_text
            local_relation_warnings = list(handle_warnings)
            revision = _loads(
                _make_submit_revision(ctx)(
                    revised_text,
                    flags_resolved_json,
                    summary,
                )
            )
            if revision.get("status") not in {"ok", "revised", "already_completed"}:
                return json.dumps(
                    {"status": "repair_required", "stage": "revision", "detail": revision},
                    ensure_ascii=False,
                )
            audit = _loads(_make_run_citation_audit(ctx)("[]"))
            audit_view = _blocking_audit(audit)
            if audit_view["blocking_flags"]:
                return json.dumps(
                    {
                        "status": "revision_required",
                        "stage": "citation_audit",
                        "audit": audit_view,
                    },
                    ensure_ascii=False,
                )
            validation = _make_validate_authoring_package(ctx)()
            return json.dumps(
                {
                    "status": (
                        "completed" if "VALIDATION_PASSED" in validation else "repair_required"
                    ),
                    "stage": "validation",
                    "validation": validation,
                    "audit": audit_view,
                    "local_relation_warnings": local_relation_warnings,
                },
                ensure_ascii=False,
            )

        return [
            FunctionTool(prepare_authoring_workspace),
            _permissive_function_tool(submit_authoring_candidate),
            FunctionTool(revise_authoring_candidate),
            FunctionTool(_make_validate_authoring_package(ctx)),
        ]

    def get_allowed_tool_names(self) -> List[str]:
        return list(COMPACT_SECTION_AUTHORING_TOOL_NAMES)

    def try_auto_finalize(self) -> Optional[str]:
        return SectionAuthoringToolProvider(self._ctx).try_auto_finalize()
