"""Phase 3 argument and material orchestration.

This module is the bridge between section-level literature coverage and any
later writing stage.  It deliberately does not write prose.  It turns a
section into an argument contract, reuses the existing M2a/M2b components,
selects a compact evidence portfolio, and emits executable requests when the
current material is not enough.

The default path is deterministic and offline.  A caller may opt into one
bounded ClaimDecomposer call for a single section, or provide a Phase-2
coverage callback for a finite, affected-section-only retry.  No synthetic
claim is created merely to make a section appear ready.
"""

from __future__ import annotations

import json
import hashlib
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from optomind_research.argument_dag_builder import (
    ArgumentDAGBuilder,
    _claim_can_enter_dag,
)
from optomind_research.claim_decomposer import ClaimDecomposer
from optomind_research.review_blueprint_planner import build_evidence_digest
from .evidence_portfolio_selector import select_evidence_portfolio
from optomind_research.review_mentor_agent import ReviewMentorAgent
from .section_authoring_assets import CanonicalAssetGraph, build_canonical_asset_graph
from .section_asset_overlay import build_section_asset_overlay
from .section_coverage_orchestrator import (
    SectionCoverageOrchestrator,
    SectionCoverageOrchestratorConfig,
)
from .coverage_atlas import build_coverage_atlas
from .semantic_relation_classifier import revalidate_legacy_relation_edges
from .synthesis_bundle import build_synthesis_bundle
from .argument_quality_policy import (
    DISCOVERY,
    FACTUAL,
    QUALIFIED,
    evidence_ceiling,
    normalize_importance,
)
from .cost_ledger import estimate_call_cost_cny
from .fresh_evidence_reconciliation import (
    apply_semantic_judge_batch,
    audit_fresh_components,
    normalize_residuals,
    normalize_support_state,
)
from .fresh_evidence_semantic_judge import QwenFreshEvidenceSemanticJudge
from .artifact_store import atomic_write_json
from .r3_production_handoff import (
    R3_HANDOFF_FILENAME,
    build_canonical_identity_resolver,
    build_r3_production_handoff_from_phase3,
    write_r3_production_handoff,
)

logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARGUMENT_RELATION_TYPES = (
    "depends_on",
    "supports",
    "motivates",
    "extends",
    "contrasts_with",
    "qualifies",
    "limits",
    "constrains",
    "applies_to",
)

# These values are deliberately small and closed.  A claim can carry many
# provenance/status fields, but downstream stages should make one explicit
# decision about the strongest language the current material permits.
CLAIM_CLASSIFICATIONS = ("supported", "qualified", "open_question")
SECTION_OUTCOMES = (
    "ready",
    "ready_with_limits",
    "merge_required",
    "needs_more_literature",
)
_OPEN_CLAIM_STATES = frozenset({
    "open_question",
    "uncertain",
    "contested",
    "insufficient",
    "unresolved",
    "unverified",
    "unsupported",
    "needs_more_literature",
})
_QUALIFIED_CLAIM_STATES = frozenset({
    "partial",
    "partially_grounded",
    "qualified",
    "conditional",
})


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not Path(path).exists():
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _text(value: Any, limit: int = 1200) -> str:
    return str(value or "").strip()[:limit]


def _clean_text(value: Any) -> str:
    """Normalize whitespace without truncating an argument task."""
    return " ".join(str(value or "").split()).strip()


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _as_sequence(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _section_sources_from_graph(
    graph: CanonicalAssetGraph,
    section_id: str,
) -> list[dict[str, Any]]:
    """Serialize only ownership already validated in the active graph."""

    rows: list[dict[str, Any]] = []
    for paper_id, paper in graph.papers.items():
        chunk_ids = [
            chunk_id for chunk_id, chunk in graph.chunks.items()
            if chunk.paper_id == paper_id
        ]
        rows.append({
            "paper_id": paper_id,
            "title": paper.title,
            "year": paper.year,
            "literature_role": paper.literature_role,
            "scope_fit": paper.scope_fit,
            "use_permission": paper.use_permission,
            "content_depth": paper.content_depth,
            "acquisition_status": paper.acquisition_status,
            "discovery_route": paper.discovery_route,
            "materialization_route": paper.materialization_route,
            "allowed_claim_kinds": list(paper.allowed_claim_kinds),
            "canonical_chunk_ids": chunk_ids,
            "section_id": section_id,
        })
    return rows


def _merge_and_validate_section_sources(
    *,
    section_id: str,
    previous_sources: Iterable[dict[str, Any]],
    incoming_sources: Iterable[dict[str, Any]],
    kb_paths: Iterable[Path],
) -> dict[str, Any]:
    """Merge section ownership and verify every ID against active SQLite.

    A source-ledger declaration is necessary but not sufficient. Papers must
    exist in an active KB or own at least one verified chunk, and every chunk
    must resolve to exactly the paper declared by the section source row.
    """

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    for origin, values in (
        ("previous_validated_graph", previous_sources),
        ("incoming_source_ledger", incoming_sources),
    ):
        for raw in values:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            paper_id = str(row.get("paper_id") or "").strip()
            row_section = str(row.get("section_id") or "").strip()
            if not paper_id:
                continue
            if row_section and row_section != section_id:
                rejected.append({
                    "id_type": "paper_id",
                    "id": paper_id,
                    "reason": "section_id_mismatch",
                    "declared_section_id": row_section,
                    "expected_section_id": section_id,
                    "origin": origin,
                })
                continue
            role = str(row.get("literature_role") or "")
            key = (paper_id, role)
            chunk_ids = _unique(row.get("canonical_chunk_ids") or [])
            if key not in merged:
                row["canonical_chunk_ids"] = chunk_ids
                row["section_id"] = section_id
                row["ownership_origins"] = [origin]
                merged[key] = row
            else:
                existing = merged[key]
                existing["canonical_chunk_ids"] = _unique([
                    *existing.get("canonical_chunk_ids", []), *chunk_ids,
                ])
                existing["ownership_origins"] = _unique([
                    *existing.get("ownership_origins", []), origin,
                ])
                # The current Phase-2 ledger is authoritative for refreshed
                # section policy, while prior verified chunk membership is
                # retained until ownership is rechecked below.
                if origin == "incoming_source_ledger":
                    for field, value in row.items():
                        if field not in {"canonical_chunk_ids", "section_id"} and value not in (None, ""):
                            existing[field] = value

    requested_papers = {key[0] for key in merged}
    requested_chunks = {
        str(chunk_id)
        for row in merged.values()
        for chunk_id in row.get("canonical_chunk_ids") or []
        if str(chunk_id)
    }
    known_papers: set[str] = set()
    owners_by_chunk: dict[str, set[str]] = {
        chunk_id: set() for chunk_id in requested_chunks
    }

    def batches(values: set[str], size: int = 400) -> Iterable[list[str]]:
        ordered = sorted(values)
        for index in range(0, len(ordered), size):
            yield ordered[index:index + size]

    active_paths: list[str] = []
    for raw_path in kb_paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        active_paths.append(str(path))
        try:
            with sqlite3.connect(str(path)) as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "papers" in tables:
                    paper_columns = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(papers)").fetchall()
                    }
                    if "paper_id" in paper_columns:
                        for group in batches(requested_papers):
                            marks = ",".join("?" for _ in group)
                            known_papers.update(
                                str(row[0]) for row in conn.execute(
                                    f"SELECT paper_id FROM papers WHERE paper_id IN ({marks})",
                                    tuple(group),
                                ).fetchall()
                                if row and row[0]
                            )
                if "text_chunks" not in tables:
                    continue
                chunk_columns = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(text_chunks)"
                    ).fetchall()
                }
                if not {"chunk_id", "paper_id"}.issubset(chunk_columns):
                    continue
                for group in batches(requested_chunks):
                    marks = ",".join("?" for _ in group)
                    for chunk_id, paper_id in conn.execute(
                        f"SELECT chunk_id, paper_id FROM text_chunks WHERE chunk_id IN ({marks})",
                        tuple(group),
                    ).fetchall():
                        cid = str(chunk_id or "")
                        pid = str(paper_id or "")
                        if cid in owners_by_chunk and pid:
                            owners_by_chunk[cid].add(pid)
                            known_papers.add(pid)
        except sqlite3.Error as exc:
            rejected.append({
                "id_type": "kb_path",
                "id": str(path),
                "reason": "sqlite_ownership_check_failed",
                "error": f"{type(exc).__name__}: {exc}",
            })

    validated: list[dict[str, Any]] = []
    for (paper_id, _role), row in merged.items():
        valid_chunks: list[str] = []
        for chunk_id in row.get("canonical_chunk_ids") or []:
            owners = owners_by_chunk.get(str(chunk_id), set())
            if owners == {paper_id}:
                valid_chunks.append(str(chunk_id))
                continue
            rejected.append({
                "id_type": "chunk_id",
                "id": str(chunk_id),
                "paper_id": paper_id,
                "reason": (
                    "unknown_chunk_id"
                    if not owners
                    else "chunk_owner_mismatch"
                    if paper_id not in owners
                    else "ambiguous_chunk_owner"
                ),
                "observed_paper_ids": sorted(owners),
            })
        if paper_id not in known_papers and not valid_chunks:
            rejected.append({
                "id_type": "paper_id",
                "id": paper_id,
                "reason": "unknown_paper_id",
            })
            continue
        clean = dict(row)
        clean["canonical_chunk_ids"] = valid_chunks
        clean["section_id"] = section_id
        validated.append(clean)

    return {
        "schema_version": "research_harness.phase3_validated_section_ownership.v1",
        "section_id": section_id,
        "sources": validated,
        "active_kb_paths": active_paths,
        "validated_paper_count": len({
            str(item.get("paper_id")) for item in validated
            if item.get("paper_id")
        }),
        "validated_chunk_count": len({
            str(chunk_id)
            for item in validated
            for chunk_id in item.get("canonical_chunk_ids") or []
        }),
        "rejected_ids": rejected,
        "rejected_id_count": len(rejected),
    }


def _is_real_claim(claim: dict[str, Any]) -> bool:
    value = _text(claim.get("statement") or claim.get("claim"), 2000)
    if len(value) < 20:
        return False
    lowered = value.casefold()
    return not any(
        marker in lowered
        for marker in (
            "formulate the supported points",
            "material inventory is available",
            "no claim-level",
            "additional candidates remain available",
        )
    )


def _as_claim_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        try:
            result = value.to_dict()
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    return {}


def _graph_record(graph: CanonicalAssetGraph, chunk_id: str) -> dict[str, Any]:
    chunk = graph.chunks[chunk_id]
    normalized_text = str(chunk.normalized_text or "")
    source_kind = str(chunk.source_kind or chunk.evidence_level or "").casefold()
    content_depth = str(chunk.content_depth or "metadata").casefold()
    # Some legacy SQLite rows carry a conservative paper-level depth while
    # the chunk itself is an actual full-text passage.  Preserve the raw
    # route, but use the chunk-level fact for downstream permission checks.
    if (
        content_depth in {"", "metadata", "unknown"}
        and source_kind in {"fulltext", "publisher_html", "pdf", "html_markdown"}
        and len(normalized_text) >= 40
    ):
        content_depth = "fulltext"
    return {
        "chunk_id": chunk.chunk_id,
        "paper_id": chunk.paper_id,
        "paper_title": chunk.paper_title,
        "title": chunk.paper_title,
        "paper_year": chunk.paper_year,
        "normalized_text": normalized_text,
        "ordinal": chunk.ordinal,
        "section_path": chunk.section_path,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "source_locator": dict(chunk.source_locator or {}),
        # Existing M2a/M2b consumers use ``text``/``text_preview`` while the
        # canonical graph uses ``normalized_text``.  Expose both names at this
        # boundary; otherwise a real claim verifier receives empty anchors
        # even though the SQLite chunk contains full text.
        "text": normalized_text,
        "text_preview": normalized_text[:1400],
        "search_text": normalized_text[:4000],
        "scope_fit": chunk.scope_fit,
        "use_permission": chunk.use_permission,
        "content_depth": content_depth,
        "context_complete": chunk.context_complete,
        "source_kind": source_kind,
        "literature_roles": [chunk.literature_role] if chunk.literature_role else [],
        "relation_roles": [str(item) for item in (getattr(chunk, "relation_roles", ()) or ())],
        "discovery_route": chunk.discovery_route,
        "materialization_route": chunk.materialization_route,
        "retrieval_role": getattr(chunk, "retrieval_role", "")
        or (chunk.route_provenance or {}).get("retrieval_role", ""),
        "allowed_claim_kinds": list(chunk.allowed_claim_kinds),
        "route_provenance": dict(chunk.route_provenance or {}),
        "provenance": dict(chunk.route_provenance or {}),
    }


def _component_support_state(value: Any) -> str:
    """Normalize legacy and refreshed component labels to three states."""

    return normalize_support_state(value)


def _fresh_component_audit(
    claims: Iterable[dict[str, Any]],
    records_by_id: dict[str, dict[str, Any]],
    fresh_chunk_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Rank fresh evidence using the domain-agnostic reconciliation engine."""

    return audit_fresh_components(claims, records_by_id, fresh_chunk_ids)


def _reconcile_fresh_claim_evidence(
    claim: dict[str, Any],
    audits: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Rebuild one effective claim from fresh component-level evidence.

    Fresh evidence can restore propositions that an older supported rewrite
    removed.  The original claim remains the audit source, while the writing
    statement is rebuilt only from supported or qualified component text.
    Stronger unsupported precision is retained as a narrow residual gap.
    """

    rows = [dict(item) for item in audits if isinstance(item, dict)]
    if not rows:
        return claim

    component_map = [
        dict(item) for item in claim.get("evidence_component_map") or []
        if isinstance(item, dict)
    ]
    missing = normalize_residuals(
        _clean_text(item)
        for item in (
            claim.get("missing_evidence_components")
            or claim.get("missing_components")
            or []
        )
        if _clean_text(item)
    )
    state_rows: list[dict[str, Any]] = []
    supported_rows: list[dict[str, Any]] = []

    def remove_component(values: list[str], requested: str) -> list[str]:
        target = requested.casefold()
        return [item for item in values if item.casefold() != target]

    for audit in rows:
        requested = _clean_text(audit.get("requested_component"))
        state = _component_support_state(
            audit.get("support_state") or audit.get("status")
        )
        supported_component = _clean_text(audit.get("supported_component"))
        chunk_ids = _unique(audit.get("chunk_ids") or [])
        residual = normalize_residuals(
            _clean_text(item)
            for item in audit.get("residual_components") or []
            if _clean_text(item)
        )
        state_rows.append({
            "requested_component": requested,
            "support_state": state,
            "supported_component": supported_component,
            "chunk_ids": chunk_ids,
            "residual_components": residual,
        })
        if state == "supported":
            missing = remove_component(missing, requested)
        elif state == "partially_supported":
            missing = remove_component(missing, requested)
            missing.extend(residual or [requested])
        elif requested and requested.casefold() not in {
            item.casefold() for item in missing
        }:
            missing.append(requested)

        if state == "unsupported" or not supported_component or not chunk_ids:
            continue
        supported_rows.append(audit)
        component_map.append({
            "component": supported_component,
            "chunk_ids": chunk_ids,
            "source": "phase3_fresh_chunk_rebinding",
            "requested_component": requested,
            "status": state,
            "support_state": state,
            "evidence_spans": list(audit.get("evidence_spans") or []),
        })
        claim["supporting_text_chunk_ids"] = _unique([
            *(claim.get("supporting_text_chunk_ids") or []),
            *chunk_ids,
        ])

    dedup_components: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for item in component_map:
        key = (
            _clean_text(item.get("component")),
            tuple(_unique(item.get("chunk_ids") or [])),
        )
        if key[0]:
            dedup_components[key] = item
    claim["evidence_component_map"] = list(dedup_components.values())
    claim["missing_evidence_components"] = normalize_residuals(missing)
    claim["fresh_evidence_component_states"] = state_rows

    if not supported_rows:
        claim["fresh_evidence_support_state"] = "unsupported"
        return claim

    original = _clean_text(
        claim.get("original_statement") or claim.get("statement")
    )
    old_rewrite = _clean_text(claim.get("supported_rewrite"))
    original_terms = _task_terms(original)
    rewrite_terms = _task_terms(old_rewrite)
    old_rewrite_relevant = bool(
        old_rewrite
        and len(original_terms & rewrite_terms) >= 2
        and any(
            (
                len(rewrite_terms & _term_tokens(_clean_text(item.get("requested_component"))))
                / max(1, len(_term_tokens(_clean_text(item.get("requested_component")))))
            ) >= 0.4
            for item in supported_rows
        )
    )

    parts: list[str] = []
    if old_rewrite_relevant:
        parts.append(old_rewrite)
    for audit in supported_rows:
        component = _clean_text(audit.get("supported_component"))
        if not component or component.casefold() in {
            item.casefold() for item in parts
        }:
            continue
        parts.append(component)

    if not old_rewrite and not claim["missing_evidence_components"] and original:
        effective = original
        reconciliation = "restored_original_statement"
    else:
        effective = " ".join(
            item if item.endswith((".", "!", "?")) else item + "."
            for item in parts
        ).strip()
        reconciliation = (
            "extended_supported_rewrite"
            if old_rewrite_relevant
            else "replaced_stale_supported_rewrite"
            if old_rewrite
            else "built_from_component_support"
        )
    if effective:
        claim["original_statement"] = original
        if old_rewrite and not old_rewrite_relevant:
            claim["superseded_supported_rewrite"] = old_rewrite
        claim["supported_rewrite"] = effective
        claim["effective_statement"] = effective
        claim["fresh_evidence_reconciliation"] = reconciliation
    claim["fresh_evidence_support_state"] = (
        "partially_supported"
        if claim["missing_evidence_components"]
        else "supported"
    )
    return claim


def _paper_ids(graph: CanonicalAssetGraph) -> list[str]:
    return list(graph.papers.keys())


def _chunk_ids(graph: CanonicalAssetGraph) -> list[str]:
    return list(graph.chunks.keys())


def _phase3_identity_inventory(states: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Expose graph identity/provenance fields to the R3 resolver."""

    papers: dict[str, dict[str, Any]] = {}
    chunks: dict[str, dict[str, Any]] = {}
    for state in states:
        graph = state.get("graph")
        if graph is None:
            continue
        for identifier, asset in getattr(graph, "papers", {}).items():
            row = asdict(asset) if hasattr(asset, "__dataclass_fields__") else dict(asset)
            row["paper_id"] = str(row.get("paper_id") or identifier)
            papers.setdefault(str(identifier), row)
        for identifier, asset in getattr(graph, "chunks", {}).items():
            row = asdict(asset) if hasattr(asset, "__dataclass_fields__") else dict(asset)
            row["chunk_id"] = str(row.get("chunk_id") or identifier)
            chunks.setdefault(str(identifier), row)
    return {"papers": papers, "chunks": chunks, "visuals": {}}


def _relation_basis_ids(edge: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    for field_name in (
        "relation_basis_chunk_ids",
        "basis_chunk_ids",
        "relation_basis_chunk_id",
        "basis_chunk_id",
        "source_chunk_ids",
        "target_chunk_ids",
        "source_chunk_id",
        "target_chunk_id",
    ):
        value = edge.get(field_name)
        if isinstance(value, (list, tuple)):
            values.extend(value)
        elif value not in (None, ""):
            values.append(value)
    return _unique(values)


def _section_relation_edges(
    edges: Iterable[dict[str, Any]], graph: CanonicalAssetGraph
) -> list[dict[str, Any]]:
    papers = set(graph.papers)
    chunks = set(graph.chunks)
    selected: list[dict[str, Any]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if str(edge.get("source_paper_id") or "") not in papers:
            continue
        if str(edge.get("target_paper_id") or "") not in papers:
            continue
        basis = _relation_basis_ids(edge)
        if not basis or any(item not in chunks for item in basis):
            continue
        selected.append(dict(edge))
    return selected


def _term_tokens(text: str) -> set[str]:
    return {
        item.casefold()
        for item in __import__("re").findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text or "")
        if item.casefold() not in {
            "the", "and", "for", "with", "from", "that", "this", "using",
            "section", "claim", "paper", "study", "method", "review",
        }
    }


def _task_terms(text: str) -> set[str]:
    """Return discriminative terms for task-to-claim matching.

    Generic discourse words identify the task form rather than its scientific
    proposition, so they must not make every claim appear to cover every task.
    """
    generic_topic = {
        "conventional", "effect", "effects", "how", "mechanism", "mechanisms",
        "point", "points", "relationship", "role", "section", "system", "systems",
        "what", "which", "does", "distinguishes", "distinguish", "characterizes",
        "characterize", "compares", "compare", "comparison",
    }
    return _term_tokens(text) - generic_topic


def _english_words(text: Any) -> list[str]:
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "into", "using",
        "section", "chapter", "review", "paper", "study", "about", "which",
        "their", "these", "those", "than", "also", "between", "through",
    }
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]*", str(text or ""))
    return [word.casefold() for word in words if word.casefold() not in stop and len(word) > 2]


def _normalise_guidance(raw: Any) -> list[str]:
    """Convert legacy mentor guidance shapes into a stable compact list."""

    if isinstance(raw, str):
        return [_clean_text(raw)] if _clean_text(raw) else []
    if isinstance(raw, (list, tuple)):
        return _unique(_clean_text(item) for item in raw if _clean_text(item))
    if isinstance(raw, dict):
        values: list[str] = []
        for key in (
            "planning_principles",
            "m2a_claim_decomposition_advice",
            "m2b_argument_dag_advice",
            "guidance",
            "advice",
            "summary",
        ):
            value = raw.get(key)
            if isinstance(value, (list, tuple)):
                values.extend(_clean_text(item) for item in value if _clean_text(item))
            elif _clean_text(value):
                values.append(_clean_text(value))
        return _unique(values)
    return []


def _normalise_word_range(raw: Any) -> list[int]:
    """Read legacy word-range forms without inventing a target."""

    values: list[Any] = []
    if isinstance(raw, dict):
        values = [raw.get("min"), raw.get("max")]
    elif isinstance(raw, (list, tuple)):
        values = list(raw[:2])
    elif raw not in (None, ""):
        values = [raw]
    result: list[int] = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result.append(number)
    if len(result) == 2 and result[0] > result[1]:
        result.reverse()
    return result


def _normalise_visual_slots(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw[:12]:
        if isinstance(item, dict):
            result.append(dict(item))
        elif _clean_text(item):
            result.append({"description": _clean_text(item)})
    return result


_ARGUMENT_RELATION_ROLES = (
    "support",
    "counterevidence",
    "boundary_condition",
    "background_context",
    "open_gap",
)


def _normalise_axis_assignments(raw: Any) -> list[dict[str, Any]]:
    """Keep planner axis ownership explicit and compact in the contract."""

    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw[:24]:
        if not isinstance(item, Mapping):
            continue
        axis_id = _clean_text(item.get("axis_id"))
        if not axis_id or axis_id in seen:
            continue
        seen.add(axis_id)
        result.append({
            "axis_id": axis_id,
            "label": _clean_text(item.get("label"))[:240],
            "assignment_basis": _clean_text(
                item.get("assignment_basis") or item.get("basis")
            )[:120],
            "fit": _clean_text(item.get("fit"))[:80],
            "question_function": _clean_text(item.get("question_function"))[:120],
        })
    return result


def _normalise_argument_structure(raw: Any) -> dict[str, Any]:
    """Normalize the planner's relation contract without inventing claims."""

    source = dict(raw) if isinstance(raw, Mapping) else {}
    required = [
        _clean_text(value)[:80]
        for value in _as_sequence(source.get("required_relation_roles") or _ARGUMENT_RELATION_ROLES)
        if _clean_text(value)
    ]
    required = list(dict.fromkeys(required))
    sequence = [
        _clean_text(value)[:160]
        for value in _as_sequence(source.get("writing_sequence"))
        if _clean_text(value)
    ]
    result = {
        "composition_mode": _clean_text(
            source.get("composition_mode") or "multi_axis_claim_centered"
        )[:120],
        "required_relation_roles": required or list(_ARGUMENT_RELATION_ROLES),
        "writing_sequence": list(dict.fromkeys(sequence)),
        "relation_types_to_check": [
            _clean_text(value)[:80]
            for value in _as_sequence(source.get("relation_types_to_check"))
            if _clean_text(value)
        ][:12],
        "role_binding_rule": _clean_text(source.get("role_binding_rule"))[:900],
    }
    decision_framework = source.get("decision_framework")
    if isinstance(decision_framework, Mapping):
        result["decision_framework"] = dict(decision_framework)
    return result


def _normalise_candidate_material_pool(raw: Any) -> dict[str, Any]:
    """Serialize the complete candidate inventory separately from served chunks."""

    source = dict(raw) if isinstance(raw, Mapping) else {}
    chunk_ids = _unique(_as_sequence(source.get("chunk_ids") or source.get("candidate_chunk_ids")))
    paper_ids = _unique(_as_sequence(source.get("paper_ids") or source.get("candidate_paper_ids")))
    served_chunk_ids = _unique(
        _as_sequence(
            source.get("served_chunk_ids")
            or source.get("served_candidate_chunk_ids")
            or source.get("m2a_served_chunk_ids")
        )
    )
    served_paper_ids = _unique(
        _as_sequence(
            source.get("served_paper_ids")
            or source.get("m2a_served_paper_ids")
        )
    )
    served_claim_pool_chunk_ids = _unique(
        _as_sequence(source.get("served_claim_pool_chunk_ids"))
    )
    served_claim_pool_paper_ids = _unique(
        _as_sequence(source.get("served_claim_pool_paper_ids"))
    )
    def count_or_default(value: Any, default: int) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default

    result = {
        "schema_version": str(
            source.get("schema_version")
            or "research_harness.candidate_material_pool.v1"
        ),
        "complete_inventory": bool(source.get("complete_inventory", bool(chunk_ids or paper_ids))),
        "chunk_ids": chunk_ids,
        "paper_ids": paper_ids,
        "served_chunk_ids": served_chunk_ids,
        "served_paper_ids": served_paper_ids,
        "served_claim_pool_chunk_ids": served_claim_pool_chunk_ids,
        "served_claim_pool_paper_ids": served_claim_pool_paper_ids,
        "inventory_chunk_count": count_or_default(source.get("inventory_chunk_count"), len(chunk_ids)),
        "inventory_paper_count": count_or_default(source.get("inventory_paper_count"), len(paper_ids)),
        "served_chunk_count": count_or_default(source.get("served_chunk_count"), len(served_chunk_ids)),
        "served_paper_count": count_or_default(source.get("served_paper_count"), len(served_paper_ids)),
        "served_claim_pool_chunk_count": count_or_default(
            source.get("served_claim_pool_chunk_count"),
            len(served_claim_pool_chunk_ids),
        ),
        "served_claim_pool_paper_count": count_or_default(
            source.get("served_claim_pool_paper_count"),
            len(served_claim_pool_paper_ids),
        ),
        "compression_strategy": dict(source.get("compression_strategy") or {}),
        "ref": _clean_text(source.get("ref"))[:180],
    }
    return result


_MODEL_HIDDEN_POOL_ID_FIELDS = frozenset({
    "chunk_ids",
    "paper_ids",
    "served_chunk_ids",
    "served_paper_ids",
    "served_claim_pool_chunk_ids",
    "served_claim_pool_paper_ids",
    "core_chunk_ids",
    "core_paper_ids",
    "candidate_chunk_ids",
    "candidate_paper_ids",
})


def _model_candidate_material_pool(raw: Any) -> dict[str, Any]:
    """Return inventory metadata without leaking the full ID ledger to M2a."""

    source = dict(raw) if isinstance(raw, Mapping) else {}
    return {
        key: value
        for key, value in source.items()
        if key not in _MODEL_HIDDEN_POOL_ID_FIELDS
    }


def _select_diverse_claim_pool_records(
    records: Iterable[Mapping[str, Any]],
    *,
    preferred_chunk_ids: Iterable[Any] = (),
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Select content-bearing records by relevance order and paper rotation."""

    rows_by_id: dict[str, dict[str, Any]] = {}
    input_order: list[str] = []
    for raw in records:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        chunk_id = str(row.get("chunk_id") or "").strip()
        content = _clean_text(
            row.get("normalized_text")
            or row.get("text")
            or row.get("text_preview")
            or row.get("search_text")
        )
        if not chunk_id or not content or chunk_id in rows_by_id:
            continue
        rows_by_id[chunk_id] = row
        input_order.append(chunk_id)

    ordered_ids = _unique([*preferred_chunk_ids, *input_order])
    buckets: dict[str, list[dict[str, Any]]] = {}
    paper_order: list[str] = []
    for chunk_id in ordered_ids:
        row = rows_by_id.get(str(chunk_id))
        if row is None:
            continue
        paper_id = str(row.get("paper_id") or f"__chunk__:{chunk_id}")
        if paper_id not in buckets:
            buckets[paper_id] = []
            paper_order.append(paper_id)
        buckets[paper_id].append(row)

    selected: list[dict[str, Any]] = []
    cap = max(1, int(limit or 200))
    while len(selected) < cap:
        advanced = False
        for paper_id in paper_order:
            bucket = buckets[paper_id]
            if not bucket:
                continue
            selected.append(bucket.pop(0))
            advanced = True
            if len(selected) >= cap:
                break
        if not advanced:
            break
    return selected


def _decision_framework_contract(section: Mapping[str, Any]) -> dict[str, Any]:
    """Provide domain-neutral comparison duties for decision-oriented sections."""

    combined = " ".join(
        _clean_text(value)
        for value in (
            section.get("title"),
            section.get("argument_role"),
            section.get("core_question"),
            section.get("central_judgment"),
            section.get("synthesis_task"),
        )
        if _clean_text(value)
    ).casefold()
    decision_markers = (
        "decision", "choose", "choice", "select", "selection", "trade-off",
        "tradeoff", "which method", "which approach", "决策", "选择", "权衡",
    )
    comparison_markers = ("compare", "comparison", "alternative", "比较", "对比")
    framework_markers = ("framework", "matrix", "框架", "矩阵")
    active = any(marker in combined for marker in decision_markers) or (
        any(marker in combined for marker in comparison_markers)
        and any(marker in combined for marker in framework_markers)
    )
    if not active:
        return {}
    decision_question = _clean_text(
        section.get("core_question")
        or next(iter(_as_sequence(section.get("key_questions"))), "")
        or section.get("synthesis_task")
    )[:900]
    return {
        "required": True,
        "decision_question": decision_question,
        "alternative_policy": (
            "Use only alternatives explicitly named by the section contract or "
            "supplied evidence; never invent an option to complete the matrix."
        ),
        "comparison_dimensions": [
            "applicability_conditions",
            "cost_and_resource_demands",
            "performance_boundaries",
            "evidence_type_and_strength",
        ],
        "criteria": [
            {
                "criterion": "applicability_conditions",
                "required_fields": [
                    "definition", "hard_or_soft_constraint", "applicable_range"
                ],
            },
            {
                "criterion": "cost_and_resource_demands",
                "required_fields": [
                    "definition", "unit_or_qualitative_scale", "preference_direction"
                ],
            },
            {
                "criterion": "performance_boundaries",
                "required_fields": [
                    "metric", "value_or_range", "conditions", "preference_direction"
                ],
            },
            {
                "criterion": "evidence_type_and_strength",
                "required_fields": [
                    "theory_simulation_or_experiment", "confidence", "scope_limit"
                ],
            },
        ],
        "matrix_cell_contract": {
            "required_fields": [
                "alternative", "criterion", "value_or_bounded_judgment",
                "conditions", "confidence", "supporting_claim_or_chunk_ids",
            ],
            "empty_cell_policy": (
                "Unknown or conflicting cells remain explicit gap records and "
                "must not be converted into negative recommendations."
            ),
        },
        "conditional_rule_contract": {
            "form": (
                "If the stated conditions hold, prefer or avoid a named alternative "
                "because of an explicit trade-off and evidence basis."
            ),
            "required_fields": [
                "conditions", "preferred_or_avoided_alternative", "tradeoff",
                "supporting_claim_or_chunk_ids", "confidence",
            ],
        },
        "required_outputs": [
            "comparison_matrix_claims",
            "conditional_decision_rules",
            "unknown_or_conflicting_cells",
        ],
        "upstream_dependency_policy": (
            "Reuse supplied and prior-section authorable claims when available; "
            "do not create new scientific facts merely to make the framework complete."
        ),
    }


def _partition_claim_lanes(
    claims: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate cautious writing inputs from evidence-gap records."""

    authorable: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for raw in claims:
        if not isinstance(raw, Mapping):
            continue
        claim = dict(raw)
        classification = str(
            claim.get("support_classification")
            or claim.get("claim_classification")
            or "open_question"
        )
        if classification in {"supported", "qualified"}:
            authorable.append(claim)
        else:
            gaps.append(claim)
    return authorable, gaps


def _expand_section_graph_for_claim_pool(
    section_graph: CanonicalAssetGraph,
    inventory_graph: CanonicalAssetGraph,
    selected_chunk_ids: Iterable[Any],
    *,
    overlay_path: Path | None,
) -> dict[str, Any]:
    """Legally add shared-ledger candidates while retaining explicit overrides."""

    overlay = _read_json(overlay_path)
    paper_overrides = overlay.get("paper_overrides") or {}
    chunk_overrides = overlay.get("chunk_overrides") or {}
    before_chunks = set(section_graph.chunks)
    before_papers = set(section_graph.papers)
    added_chunks: list[str] = []
    added_papers: list[str] = []
    missing: list[str] = []
    for raw_chunk_id in selected_chunk_ids:
        chunk_id = str(raw_chunk_id or "")
        source_chunk = inventory_graph.chunks.get(chunk_id)
        if source_chunk is None:
            missing.append(chunk_id)
            continue
        paper_id = str(source_chunk.paper_id or "")
        source_paper = inventory_graph.papers.get(paper_id)
        if source_paper is None:
            missing.append(chunk_id)
            continue
        paper_override = paper_overrides.get(paper_id)
        if isinstance(paper_override, Mapping):
            source_paper = replace(
                source_paper,
                scope_fit=str(paper_override.get("scope_fit") or source_paper.scope_fit),
                use_permission=str(
                    paper_override.get("use_permission") or source_paper.use_permission
                ),
                literature_role=str(
                    paper_override.get("literature_role") or source_paper.literature_role
                ),
                discovery_route=str(
                    paper_override.get("discovery_route") or source_paper.discovery_route
                ),
                materialization_route=str(
                    paper_override.get("materialization_route")
                    or source_paper.materialization_route
                ),
            )
        chunk_override = chunk_overrides.get(chunk_id)
        if isinstance(chunk_override, Mapping):
            source_chunk = replace(
                source_chunk,
                scope_fit=str(chunk_override.get("scope_fit") or source_chunk.scope_fit),
                use_permission=str(
                    chunk_override.get("use_permission") or source_chunk.use_permission
                ),
                literature_role=str(
                    chunk_override.get("literature_role") or source_chunk.literature_role
                ),
            )
        if paper_id not in section_graph.papers:
            section_graph.papers[paper_id] = source_paper
            added_papers.append(paper_id)
        if chunk_id not in section_graph.chunks:
            section_graph.chunks[chunk_id] = source_chunk
            added_chunks.append(chunk_id)
    section_graph.expected_chunk_ids.update(
        chunk_id for chunk_id in added_chunks if chunk_id
    )
    if added_chunks:
        section_graph.diagnostics.append(
            f"claim_pool_global_expansion_added_{len(added_chunks)}_chunks"
        )
    return {
        "schema_version": "research_harness.claim_pool_global_expansion.v1",
        "enabled": True,
        "overlay_path": str(overlay_path or ""),
        "overlay_chunk_count": len(before_chunks),
        "overlay_paper_count": len(before_papers),
        "shared_inventory_chunk_count": len(inventory_graph.chunks),
        "shared_inventory_paper_count": len(inventory_graph.papers),
        "selected_chunk_count": len(_unique(selected_chunk_ids)),
        "added_chunk_count": len(added_chunks),
        "added_paper_count": len(added_papers),
        "added_chunk_ids": added_chunks,
        "added_paper_ids": added_papers,
        "missing_selected_chunk_ids": missing,
        "permission_policy": (
            "shared-ledger canonical permissions with explicit section overlay "
            "paper/chunk overrides retained"
        ),
    }


def _candidate_material_pool_audit(
    section: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
    *,
    served_records: Iterable[Mapping[str, Any]],
    portfolio: Any,
) -> dict[str, Any]:
    """Create a durable full-inventory reference for downstream writing."""

    rows = [row for row in records if isinstance(row, Mapping)]
    served = [row for row in served_records if isinstance(row, Mapping)]
    chunk_ids = _unique(row.get("chunk_id") for row in rows)
    paper_ids = _unique(row.get("paper_id") for row in rows)
    served_chunk_ids = _unique(row.get("chunk_id") for row in served)
    served_paper_ids = _unique(row.get("paper_id") for row in served)
    existing = _normalise_candidate_material_pool(section.get("candidate_material_pool"))
    return {
        **existing,
        "schema_version": "research_harness.candidate_material_pool.v1",
        "complete_inventory": True,
        "chunk_ids": chunk_ids,
        "paper_ids": paper_ids,
        "served_chunk_ids": served_chunk_ids,
        "served_paper_ids": served_paper_ids,
        "inventory_chunk_count": len(chunk_ids),
        "inventory_paper_count": len(paper_ids),
        "served_chunk_count": len(served_chunk_ids),
        "served_paper_count": len(served_paper_ids),
        "core_chunk_ids": list(getattr(portfolio, "core_chunk_ids", []) or []),
        "core_paper_ids": list(getattr(portfolio, "core_paper_ids", []) or []),
        "candidate_chunk_ids": list(getattr(portfolio, "candidate_chunk_ids", []) or []),
        "candidate_paper_ids": list(getattr(portfolio, "candidate_paper_ids", []) or []),
        "ref": f"section_candidate_pool:{_clean_text(section.get('section_id'))}",
        "compression_strategy": {
            "mode": "bounded_m2a_view_with_full_inventory_audit",
            "served_records_are_subset_of_inventory": True,
            "max_served_records": len(served_chunk_ids),
            "reason": "Model context is compacted, but the complete candidate IDs remain available to retrieval and audit stages.",
        },
    }


def _compile_targeted_query(
    *,
    section: dict[str, Any],
    component: str,
    role: str = "",
    relation: str = "",
) -> str:
    """Compile a compact scientific query, not a description of the workflow."""

    forbidden = {
        "scientific", "evidence", "peer", "reviewed", "literature", "workflow",
        "load", "bearing", "claim", "claims", "section", "chapter", "request",
        "candidate", "coverage", "support", "supporting", "material", "role",
        "missing", "query", "paper", "study", "review", "internal", "label",
        "explicit", "statement", "attribution", "attributed", "establish",
        "establishes", "formal", "definition", "where", "only", "contains",
        "contain", "least", "one",
    }

    def terms(value: Any, limit: int) -> list[str]:
        return [
            word for word in _english_words(value)
            if word not in forbidden
        ][:limit]

    words: list[str] = []
    for word in (
        terms(component, 8)
        + terms(role or relation, 3)
        + terms(section.get("title", ""), 5)
        + terms(section.get("argument_role", ""), 4)
    ):
        if word not in words:
            words.append(word)
    # Very short or non-English input still receives an executable scientific
    # query.  These are domain-neutral scientific terms, not workflow labels.
    for word in ("optical", "mechanism", "characterization", "theory", "comparison", "experiment"):
        if len(words) >= 6:
            break
        if word not in words:
            words.append(word)
    return " ".join(words[:15])


def compile_coverage_queries(
    *,
    section: dict[str, Any],
    missing_roles: Iterable[str] = (),
    missing_claims: Iterable[dict[str, Any]] = (),
    missing_relations: Iterable[str] = (),
    breadth_shortfall: bool = False,
) -> list[str]:
    """Compile three-to-five clustered scientific retrieval queries.

    The old one-query-per-component strategy exposed internal gap structure to
    Phase 2 and produced a long list of near-duplicate requests.  Components
    are now grouped by meaningful lexical overlap; the query itself contains
    only scientific concepts and bounded section context.
    """

    gap_items: list[dict[str, str]] = []
    for role in _unique(missing_roles):
        gap_items.append({"component": _clean_text(role), "role": _clean_text(role), "relation": ""})
    for claim in missing_claims:
        if not isinstance(claim, dict):
            continue
        components = [
            _clean_text(item)
            for item in (claim.get("missing_evidence_components") or claim.get("missing_components") or [])
            if _clean_text(item)
        ] or [_clean_text(claim.get("statement"))]
        for component in components:
            gap_items.append({
                "component": component,
                "role": _clean_text(claim.get("evidence_type")),
                "relation": "",
            })
    for relation in _unique(missing_relations):
        gap_items.append({"component": _clean_text(relation), "role": "", "relation": _clean_text(relation)})
    if breadth_shortfall and not gap_items:
        gap_items.append({
            "component": "independent mechanisms comparative performance",
            "role": "cross-platform comparison",
            "relation": "",
        })
    if not gap_items:
        return []

    # Greedy lexical clustering is deterministic, domain-agnostic, and keeps
    # closely related components together without asking an LLM to invent the
    # retrieval plan.
    clusters: list[dict[str, Any]] = []
    for item in gap_items:
        tokens = set(_english_words(item["component"]))
        best = None
        best_overlap = 0
        for cluster in clusters:
            overlap = len(tokens & cluster["tokens"])
            if overlap > best_overlap:
                best = cluster
                best_overlap = overlap
        if best is not None and best_overlap >= 1:
            best["items"].append(item)
            best["tokens"].update(tokens)
        else:
            clusters.append({"items": [item], "tokens": set(tokens)})

    # Keep the request bounded.  If there are more than five clusters, merge
    # the smallest ones into the five largest scientific neighborhoods.
    while len(clusters) > 5:
        smallest_index = min(range(len(clusters)), key=lambda index: len(clusters[index]["items"]))
        smallest = clusters.pop(smallest_index)
        target = min(clusters, key=lambda cluster: len(cluster["items"]))
        target["items"].extend(smallest["items"])
        target["tokens"].update(smallest["tokens"])

    def make_query(cluster: dict[str, Any]) -> str:
        items = cluster["items"]
        component = " ".join(dict.fromkeys(
            item["component"] for item in items if item.get("component")
        ))
        role = " ".join(dict.fromkeys(item["role"] for item in items if item.get("role")))
        relation = " ".join(dict.fromkeys(item["relation"] for item in items if item.get("relation")))
        return _compile_targeted_query(
            section=section,
            component=component,
            role=role,
            relation=relation,
        )

    queries = [make_query(cluster) for cluster in clusters]
    # A single broad gap benefits from a small, fixed set of scientific views
    # rather than an expensive sequence of nearly identical searches.
    facet_components = (
        "fundamental mechanism theoretical model",
        "measurement characterization comparative performance",
        "boundary conditions limitations applicability",
    )
    for facet in facet_components:
        if len(queries) >= 3:
            break
        queries.append(_compile_targeted_query(section=section, component=facet, role=""))
    return list(dict.fromkeys(
        query for query in queries
        if 6 <= len(_english_words(query)) <= 15
    ))[:5]


def _claim_role_chunk_ids(claim: Mapping[str, Any]) -> dict[str, list[str]]:
    """Separate positive, author-reported, counter, boundary, and context IDs."""

    def values(*fields: str) -> list[str]:
        out: list[Any] = []
        for field_name in fields:
            raw = claim.get(field_name)
            if isinstance(raw, (list, tuple, set, frozenset)):
                out.extend(raw)
            elif raw not in (None, ""):
                out.append(raw)
        return _unique(out)

    return {
        "positive_support": values(
            "supporting_text_chunk_ids", "supporting_chunk_ids",
            "support_chunk_ids", "direct_support_chunk_ids",
            "factual_support_chunk_ids", "context_text_chunk_ids",
            "contextual_support_chunk_ids", "context_support_chunk_ids",
        ),
        "author_reported_support": values(
            "author_reported_support_chunk_ids", "author_reported_chunk_ids",
        ),
        "counterevidence": values(
            "counterevidence_text_chunk_ids", "counterevidence_chunk_ids",
            "counterevidence_support_chunk_ids",
        ),
        "boundary": values(
            "boundary_text_chunk_ids", "boundary_chunk_ids",
            "qualification_text_chunk_ids", "qualification_chunk_ids",
        ),
        "background_context": values(
            "background_text_chunk_ids", "background_chunk_ids",
            "background_context_chunk_ids",
        ),
    }


def _claim_permission_status(
    claim: dict[str, Any],
    records_by_id: dict[str, dict[str, Any]],
) -> tuple[str, list[str], list[str]]:
    """Return status plus factual/contextual IDs, never discovery-only IDs."""

    evidence_type = _text(claim.get("evidence_type"), 80).casefold()
    required = "factual_support" if evidence_type in {
        "measurement", "result", "comparison", "method",
    } else "contextual_or_qualified_support"
    role_ids = _claim_role_chunk_ids(claim)
    raw_ids = role_ids["positive_support"]
    factual: list[str] = []
    contextual: list[str] = []
    for chunk_id in raw_ids:
        record = records_by_id.get(chunk_id)
        if not record:
            continue
        permission, _ = evidence_ceiling(record)
        if permission == FACTUAL:
            factual.append(chunk_id)
        elif permission == QUALIFIED:
            contextual.append(chunk_id)
    if contextual:
        # A mixed packet inherits the lower permission ceiling.  Factual
        # chunks remain available, but the claim must be written with
        # qualification while any accepted support is contextual/adjacent.
        return "qualified_only", factual, contextual
    if factual:
        return "bound", factual, contextual
    return "unbound", factual, contextual


def _declared_claim_support_ids(claim: Mapping[str, Any]) -> list[str]:
    """Return only explicitly declared support references.

    Candidate/core portfolio IDs are intentionally excluded.  A candidate is
    useful for retrieval and a provenance audit, but it is not evidence until
    the claim explicitly binds to it.
    """

    values: list[Any] = []
    for field_name in (
        "supporting_text_chunk_ids",
        "supporting_chunk_ids",
        "support_chunk_ids",
        "context_text_chunk_ids",
        "contextual_support_chunk_ids",
        "context_support_chunk_ids",
        "factual_support_chunk_ids",
        "direct_support_chunk_ids",
        "author_reported_support_chunk_ids",
        "author_reported_chunk_ids",
        "counterevidence_text_chunk_ids",
        "counterevidence_chunk_ids",
        "boundary_text_chunk_ids",
        "boundary_chunk_ids",
        "qualification_text_chunk_ids",
        "qualification_chunk_ids",
        "background_text_chunk_ids",
        "background_chunk_ids",
        "background_context_chunk_ids",
    ):
        raw = claim.get(field_name)
        if isinstance(raw, (list, tuple, set, frozenset)):
            values.extend(raw)
        elif raw not in (None, ""):
            values.append(raw)
    return _unique(values)


def classify_claim_support(
    claim: Mapping[str, Any],
    records_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify one claim without upgrading an evidence permission.

    The returned audit separates declared IDs from eligible IDs.  This is the
    important distinction for partial coverage: metadata/discovery references
    remain visible as rejected provenance, but can never be counted as factual
    support.  The function is pure and has no discovery or network side effect.
    """

    role_ids = _claim_role_chunk_ids(claim)
    declared = _unique(
        chunk_id
        for values in role_ids.values()
        for chunk_id in values
    )
    factual: list[str] = []
    qualified: list[str] = []
    rejected: list[str] = []
    source_permissions: dict[str, dict[str, str]] = {}
    role_factual: dict[str, list[str]] = {role: [] for role in role_ids}
    role_qualified: dict[str, list[str]] = {role: [] for role in role_ids}
    role_rejected: dict[str, list[str]] = {role: [] for role in role_ids}
    for role, role_chunk_ids in role_ids.items():
        for chunk_id in role_chunk_ids:
            record = records_by_id.get(str(chunk_id))
            if not isinstance(record, Mapping):
                role_rejected[role].append(str(chunk_id))
                source_permissions[str(chunk_id)] = {
                    "evidence_ceiling": DISCOVERY,
                    "reason": "chunk_not_in_canonical_inventory",
                    "argument_role": role,
                }
                continue
            ceiling, reason = evidence_ceiling(dict(record))
            source_permissions[str(chunk_id)] = {
                "evidence_ceiling": ceiling,
                "reason": reason,
                "use_permission": _text(record.get("use_permission"), 120),
                "content_depth": _text(record.get("content_depth"), 120),
                "scope_fit": _text(record.get("scope_fit"), 120),
                "argument_role": role,
            }
            if ceiling == FACTUAL:
                role_factual[role].append(str(chunk_id))
            elif ceiling == QUALIFIED:
                role_qualified[role].append(str(chunk_id))
            else:
                role_rejected[role].append(str(chunk_id))
        if role == "positive_support":
            factual.extend(role_factual[role])
            qualified.extend(role_qualified[role])
            rejected.extend(role_rejected[role])

    state = _text(
        claim.get("claim_state")
        or claim.get("evidence_binding_status")
        or claim.get("status"),
        120,
    ).casefold()
    permission = _text(claim.get("permission_status"), 120).casefold()
    flags = " ".join(_text(item, 180).casefold() for item in claim.get("critic_flags") or [])
    missing = _unique(
        list(claim.get("missing_evidence_components") or [])
        + list(claim.get("missing_components") or [])
    )
    explicit_open = (
        state in _OPEN_CLAIM_STATES
        or permission in {"unbound", "unresolved", "needs_more_literature", "discovery_only"}
        or _text(claim.get("claim_classification"), 80).casefold() == "open_question"
    )
    qualified_signal = (
        bool(qualified)
        or permission in {"qualified_only", QUALIFIED}
        or state in _QUALIFIED_CLAIM_STATES
        or bool(missing)
        or any(token in flags for token in ("partial", "qualified", "conditional", "missing", "uncertain"))
    )
    if explicit_open and not factual and not qualified:
        classification = "open_question"
    elif not factual and not qualified:
        classification = "open_question"
    elif explicit_open and not factual:
        classification = "open_question"
    elif qualified_signal:
        classification = "qualified"
    else:
        classification = "supported"

    reasons: list[str] = []
    if rejected:
        reasons.append("metadata_or_discovery_support_rejected")
    if role_ids.get("background_context"):
        reasons.append("background_context_separated_from_positive_support")
    if role_ids.get("counterevidence") or role_ids.get("boundary"):
        reasons.append("counterevidence_or_boundary_separated_from_positive_support")
    if role_ids.get("author_reported_support") and not factual and not qualified:
        reasons.append("author_reported_support_not_counted_as_positive_support")
    if not factual and not qualified:
        reasons.append("no_permission_eligible_support")
    if qualified_signal and classification == "qualified":
        reasons.append("support_or_claim_scope_requires_qualification")
    if explicit_open and classification == "open_question":
        reasons.append("claim_marked_unresolved_or_unbound")
    return {
        "classification": classification,
        "declared_support_chunk_ids": declared,
        "eligible_support_chunk_ids": _unique([*factual, *qualified]),
        "factual_support_chunk_ids": _unique(factual),
        "qualified_support_chunk_ids": _unique(qualified),
        "rejected_support_chunk_ids": _unique(rejected),
        "role_chunk_ids": {
            role: list(values) for role, values in role_ids.items()
        },
        "role_factual_chunk_ids": {
            role: _unique(values) for role, values in role_factual.items()
        },
        "role_qualified_chunk_ids": {
            role: _unique(values) for role, values in role_qualified.items()
        },
        "role_rejected_chunk_ids": {
            role: _unique(values) for role, values in role_rejected.items()
        },
        "author_reported_support_chunk_ids": _unique(
            [*role_factual["author_reported_support"], *role_qualified["author_reported_support"]]
        ),
        "counterevidence_chunk_ids": _unique(
            [*role_factual["counterevidence"], *role_qualified["counterevidence"]]
        ),
        "boundary_chunk_ids": _unique(
            [*role_factual["boundary"], *role_qualified["boundary"]]
        ),
        "background_context_chunk_ids": _unique(
            [*role_factual["background_context"], *role_qualified["background_context"]]
        ),
        "source_permissions": source_permissions,
        "reasons": list(dict.fromkeys(reasons)),
    }


def _open_question_statement(statement: Any) -> str:
    """Turn an unsupported proposition into an explicit non-factual gap."""

    text = _clean_text(statement)[:1800]
    if not text:
        return "Open question: the available material does not establish the section proposition."
    if text.casefold().startswith(("open question:", "unresolved:", "evidence gap:")):
        return text
    if text.endswith("?"):
        return f"Open question: {text}"
    return f"Open question: the available material does not establish whether {text[0].lower() + text[1:]}"


def _merge_recommendation_from_section(section: Mapping[str, Any]) -> dict[str, Any]:
    """Read an explicit merge hint without inferring one from weak evidence."""

    raw = (
        section.get("merge_recommendation")
        or section.get("merge_required_with")
        or section.get("merge_with_section_ids")
        or section.get("merge_candidate_section_ids")
        or section.get("recommended_merge_section_ids")
    )
    if isinstance(raw, Mapping):
        recommendation = dict(raw)
        candidates = raw.get("section_ids") or raw.get("target_section_ids") or raw.get("merge_with")
    else:
        recommendation = {}
        candidates = raw
    if isinstance(candidates, str):
        candidates = [candidates]
    candidate_ids = _unique(candidates or [])
    required = bool(
        section.get("merge_required")
        or section.get("requires_merge")
        or candidate_ids
        or recommendation.get("required")
    )
    if not required:
        return {}
    recommendation.setdefault("action", "merge_recommendation")
    recommendation.setdefault("required", True)
    recommendation["target_section_ids"] = candidate_ids
    recommendation.setdefault(
        "reason",
        "The section declares overlapping or inseparable argument scope; preserve the gap until a human merges the section contract.",
    )
    return recommendation


def adapt_claim_for_partial_coverage(
    claim: Mapping[str, Any],
    records_by_id: Mapping[str, Mapping[str, Any]],
    *,
    section_id: str = "",
) -> dict[str, Any]:
    """Apply the reusable, provenance-preserving claim adaptation policy.

    This layer is intentionally bounded.  It can narrow an already supplied
    rewrite or expose a gap, but it never invents a factual proposition from a
    candidate, metadata row, or discovery lead.
    """

    adapted = dict(claim)
    adapted.setdefault("section_id", section_id)
    original = _clean_text(
        adapted.get("original_statement") or adapted.get("statement") or adapted.get("claim"),
    )[:1800]
    if original:
        adapted["original_statement"] = original
    audit = classify_claim_support(adapted, records_by_id)
    classification = str(audit["classification"])
    prior_effective = _clean_text(adapted.get("effective_statement"))[:1800]
    prior_rewrite = _clean_text(adapted.get("supported_rewrite"))[:1800]

    provenance = adapted.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    provenance = dict(provenance)
    provenance["phase3_dynamic_adaptation"] = {
        "schema_version": "research_harness.phase3_dynamic_adaptation.v1",
        "section_id": _text(adapted.get("section_id") or section_id),
        "classification": classification,
        "declared_support_chunk_ids": list(audit["declared_support_chunk_ids"]),
        "eligible_support_chunk_ids": list(audit["eligible_support_chunk_ids"]),
        "rejected_support_chunk_ids": list(audit["rejected_support_chunk_ids"]),
        "role_chunk_ids": dict(audit.get("role_chunk_ids") or {}),
        "author_reported_support_chunk_ids": list(
            audit.get("author_reported_support_chunk_ids") or []
        ),
        "counterevidence_chunk_ids": list(audit.get("counterevidence_chunk_ids") or []),
        "boundary_chunk_ids": list(audit.get("boundary_chunk_ids") or []),
        "background_context_chunk_ids": list(
            audit.get("background_context_chunk_ids") or []
        ),
        "source_permissions": dict(audit["source_permissions"]),
        "reasons": list(audit["reasons"]),
    }
    adapted["provenance"] = provenance
    adapted["claim_provenance"] = {
        "declared_support_chunk_ids": list(audit["declared_support_chunk_ids"]),
        "eligible_support_chunk_ids": list(audit["eligible_support_chunk_ids"]),
        "rejected_support_chunk_ids": list(audit["rejected_support_chunk_ids"]),
        "role_chunk_ids": dict(audit.get("role_chunk_ids") or {}),
        "source_permissions": dict(audit["source_permissions"]),
    }
    adapted["support_classification"] = classification
    adapted["claim_classification"] = classification
    adapted["declared_support_chunk_ids"] = list(audit["declared_support_chunk_ids"])
    adapted["rejected_support_chunk_ids"] = list(audit["rejected_support_chunk_ids"])
    adapted["source_permissions"] = dict(audit["source_permissions"])
    adapted["factual_support_chunk_ids"] = list(audit["factual_support_chunk_ids"])
    adapted["contextual_support_chunk_ids"] = list(audit["qualified_support_chunk_ids"])
    adapted["supporting_text_chunk_ids"] = list(audit["eligible_support_chunk_ids"])
    adapted["supporting_chunk_ids"] = list(audit["eligible_support_chunk_ids"])
    adapted["context_text_chunk_ids"] = list(audit["qualified_support_chunk_ids"])
    adapted["author_reported_support_chunk_ids"] = list(
        audit.get("author_reported_support_chunk_ids") or []
    )
    adapted["counterevidence_text_chunk_ids"] = list(
        audit.get("counterevidence_chunk_ids") or []
    )
    adapted["boundary_text_chunk_ids"] = list(
        audit.get("boundary_chunk_ids") or []
    )
    adapted["background_text_chunk_ids"] = list(
        audit.get("background_context_chunk_ids") or []
    )
    adapted["evidence_role_bindings"] = [
        {
            "role": role,
            "text_chunk_ids": list(audit.get("role_chunk_ids", {}).get(role) or []),
            "factual_text_chunk_ids": list(audit.get("role_factual_chunk_ids", {}).get(role) or []),
            "qualified_text_chunk_ids": list(audit.get("role_qualified_chunk_ids", {}).get(role) or []),
            "rejected_text_chunk_ids": list(audit.get("role_rejected_chunk_ids", {}).get(role) or []),
        }
        for role in (
            "positive_support", "author_reported_support", "counterevidence",
            "boundary", "background_context",
        )
        if audit.get("role_chunk_ids", {}).get(role)
    ]

    if classification == "supported":
        adapted.setdefault("evidence_binding_status", "bound")
        adapted.setdefault("permission_status", "bound")
        adapted.setdefault("claim_state", "grounded")
        adapted["adaptation_action"] = "retain_supported_claim"
        adapted["adaptation_recommendation"] = {
            "action": "retain_supported_claim",
            "bounded": True,
        }
        if prior_effective:
            adapted["effective_statement"] = prior_effective
        elif prior_rewrite:
            adapted["effective_statement"] = prior_rewrite
        else:
            adapted["effective_statement"] = original
        adapted["authoring_statement"] = _clean_text(
            adapted.get("effective_statement") or original,
        )[:1800]
        adapted["statement"] = adapted["authoring_statement"]
    elif classification == "qualified":
        adapted.setdefault("evidence_binding_status", "qualified")
        adapted.setdefault("permission_status", "qualified_only")
        adapted.setdefault("claim_state", "partially_grounded")
        adapted["adaptation_action"] = "bounded_qualified_language"
        adapted["adaptation_recommendation"] = {
            "action": "bounded_supported_rewrite" if prior_rewrite or prior_effective else "bounded_qualified_language",
            "bounded": True,
            "reason": "The available support permits only qualified language or a narrower supplied rewrite.",
        }
        effective = prior_effective or prior_rewrite or original
        adapted["effective_statement"] = effective
        adapted["authoring_statement"] = effective
        adapted["statement"] = effective
    else:
        adapted.setdefault("evidence_binding_status", "open_question")
        adapted.setdefault("permission_status", "unbound")
        adapted["claim_state"] = "open_question"
        adapted["missing_evidence_components"] = _unique(
            list(adapted.get("missing_evidence_components") or [])
            + (["permission-eligible support for the stated proposition"] if not adapted.get("missing_evidence_components") else [])
        )
        open_statement = _open_question_statement(original)
        adapted["effective_statement"] = open_statement
        adapted["authoring_statement"] = open_statement
        adapted["statement"] = open_statement
        # Keep a previously supplied rewrite visible, but never let it become
        # the active authoring proposition after support has been rejected.
        if prior_rewrite:
            adapted["superseded_supported_rewrite"] = prior_rewrite
        adapted["supported_rewrite_eligible"] = False
        importance = normalize_importance(adapted)
        if importance == "load_bearing":
            adapted["adaptation_action"] = "targeted_coverage_request"
            adapted["adaptation_recommendation"] = {
                "action": "targeted_coverage_request",
                "bounded": True,
                "missing_evidence_components": list(adapted["missing_evidence_components"]),
                "reason": "The load-bearing proposition is not supported by permission-eligible material.",
            }
        else:
            adapted["adaptation_action"] = "declare_optional_gap"
            adapted["adaptation_recommendation"] = {
                "action": "declare_optional_gap",
                "bounded": True,
                "reason": "The optional proposition remains visible as an open question.",
            }

    adapted["support_audit"] = audit
    adapted["importance"] = normalize_importance(adapted)
    adapted["load_bearing"] = adapted["importance"] == "load_bearing"
    return adapted


def _seed_open_question_claims(
    section: Mapping[str, Any],
    contract: "SectionArgumentContract",
) -> list[dict[str, Any]]:
    """Create auditable question records when decomposition has no claims."""

    tasks = [item for item in contract.argument_tasks if isinstance(item, dict)]
    if not tasks:
        fallback = _clean_text(
            section.get("core_question")
            or section.get("central_judgment")
            or section.get("chapter_argument")
            or section.get("title"),
        )[:1200]
        tasks = [{
            "task_id": f"{_text(section.get('section_id'))}:open_question:01",
            "description": fallback or "What remains to be established for this section?",
            "required": True,
            "kind": "core_open_question",
        }]
    claims: list[dict[str, Any]] = []
    for index, task in enumerate(tasks, start=1):
        description = _clean_text(
            task.get("description") or task.get("question") or task.get("task"),
        )[:1200]
        if not description:
            continue
        sid = _text(section.get("section_id"), 80)
        task_id = _text(task.get("task_id") or f"{sid}:task:{index:02d}", 120)
        importance = "load_bearing" if bool(task.get("required", True)) else "optional"
        claims.append({
            "claim_id": f"{sid}:open_question:{index:02d}",
            "section_id": sid,
            "statement": _open_question_statement(description),
            "original_statement": description,
            "effective_statement": _open_question_statement(description),
            "claim_kind": "open_question",
            "claim_classification": "open_question",
            "support_classification": "open_question",
            "evidence_binding_status": "open_question",
            "permission_status": "unbound",
            "claim_state": "open_question",
            "importance": importance,
            "load_bearing": importance == "load_bearing",
            "evidence_type": "synthesis",
            "missing_evidence_components": [description],
            "adaptation_action": "targeted_coverage_request" if importance == "load_bearing" else "declare_optional_gap",
            "adaptation_recommendation": {
                "action": "targeted_coverage_request" if importance == "load_bearing" else "declare_optional_gap",
                "bounded": True,
                "source_task_id": task_id,
            },
            "provenance": {
                "phase3_dynamic_adaptation": {
                    "schema_version": "research_harness.phase3_dynamic_adaptation.v1",
                    "source_type": "blueprint_argument_task",
                    "source_task_id": task_id,
                    "fact_claim": False,
                    "classification": "open_question",
                }
            },
        })
    return claims


def _build_argument_task_coverage(
    contract: "SectionArgumentContract",
    claims: Iterable[dict[str, Any]],
    bindings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Map each contract task to effective claims and unresolved components.

    This is deliberately a task-level audit, not a sentence-level citation
    rule.  A supported rewrite may narrow a claim so far that it no longer
    performs the task it was meant to perform; that loss must remain visible
    unless another effective claim covers the same task.
    """
    claim_rows = [item for item in claims if isinstance(item, dict)]
    binding_rows = bindings.get("claims", {}) if isinstance(bindings, dict) else {}
    result: list[dict[str, Any]] = []
    for index, task in enumerate(contract.argument_tasks or [], start=1):
        if not isinstance(task, dict):
            continue
        task_id = _text(task.get("task_id") or f"{contract.section_id}:task:{index:02d}", 120)
        description = _clean_text(task.get("description") or task.get("question") or task.get("task"))
        task_terms = _task_terms(description) or _term_tokens(description)
        required_overlap = max(1, (2 * len(task_terms) + 4) // 5)
        effective_claim_ids: list[str] = []
        supported_components: list[dict[str, Any]] = []
        missing_components: list[str] = []
        qualified = False
        has_material = False
        rewrite_removed = False
        for claim in claim_rows:
            claim_id = _text(claim.get("claim_id"), 120)
            original = _clean_text(claim.get("original_statement") or claim.get("statement"))
            effective = _clean_text(
                claim.get("effective_statement")
                or claim.get("supported_rewrite")
                or claim.get("statement")
            )
            original_overlap = len(task_terms & _term_tokens(original))
            effective_overlap = len(task_terms & _term_tokens(effective))
            original_matches = original_overlap >= required_overlap
            effective_matches = effective_overlap >= required_overlap
            if not claim_id or (not original_matches and not effective_matches):
                continue
            if effective_matches:
                effective_claim_ids.append(claim_id)
            elif original_matches:
                rewrite_removed = True
            binding = binding_rows.get(claim_id) if isinstance(binding_rows, dict) else {}
            binding = binding if isinstance(binding, dict) else {}
            supported_ids = list(binding.get("supporting_chunk_ids") or claim.get("supporting_text_chunk_ids") or [])
            if effective_matches:
                has_material = has_material or bool(supported_ids)
            if effective_matches and binding.get("permission_status") == "qualified_only":
                qualified = True
            for component in (claim.get("evidence_component_map") or []):
                if not isinstance(component, dict):
                    continue
                component_text = _clean_text(component.get("component"))
                component_ids = _unique(component.get("chunk_ids") or [])
                component_state = _component_support_state(
                    component.get("support_state") or component.get("status") or "supported"
                )
                if component_state == "partially_supported" and effective_matches:
                    qualified = True
                if component_text and effective_matches and component_state != "unsupported":
                    supported_components.append({
                        "claim_id": claim_id,
                        "component": component_text,
                        "chunk_ids": component_ids,
                        "permission_status": binding.get("permission_status", ""),
                        "support_state": component_state,
                    })
            for missing in (
                claim.get("missing_evidence_components")
                or binding.get("missing_evidence_components")
                or []
            ):
                value = _clean_text(missing)
                if value:
                    missing_components.append(value)
        if rewrite_removed and not effective_claim_ids:
            missing_components.append("Required task component removed by supported rewrite: " + description)
        if not effective_claim_ids:
            missing_components.append("No effective claim covers this argument task")
        if effective_claim_ids and not has_material:
            missing_components.append("Usable supporting material is not attached to the effective claim")
        missing_components = _unique(missing_components)
        if not effective_claim_ids or (effective_claim_ids and not has_material):
            status = "gap"
            support_state = "unsupported"
        elif missing_components:
            status = "partially_supported"
            support_state = "partially_supported"
        elif qualified:
            status = "qualified"
            support_state = "partially_supported"
        elif effective_claim_ids and supported_components:
            status = "covered"
            support_state = "supported"
        else:
            status = "inventory_only"
            support_state = "unsupported"
        result.append({
            "task_id": task_id,
            "description": description,
            "required": bool(task.get("required", True)),
            "effective_claim_ids": _unique(effective_claim_ids),
            "supported_components": supported_components,
            "missing_components": missing_components,
            "status": status,
            "support_state": support_state,
        })
    return result


def _llm_audit_summary(states: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize observed Qwen calls without assuming a vendor usage shape."""

    call_count = 0
    input_tokens = 0
    output_tokens = 0
    usage_records: list[dict[str, Any]] = []
    per_model: dict[str, dict[str, Any]] = {}
    estimated_input_tokens_total = 0
    estimated_output_tokens_total = 0
    provider_usage_seen = False
    estimated_usage_seen = False
    per_model_token_sources: dict[str, set[str]] = {}

    provider_input_keys = {"input_tokens", "prompt_tokens", "input_token_count"}
    provider_output_keys = {"output_tokens", "completion_tokens", "output_token_count"}
    estimated_input_keys = {"estimated_input_tokens"}
    estimated_output_keys = {"estimated_output_tokens"}

    def add_usage(raw: Any, attempt: dict[str, Any] | None = None) -> None:
        nonlocal input_tokens, output_tokens, provider_usage_seen, estimated_usage_seen
        nonlocal estimated_input_tokens_total, estimated_output_tokens_total
        record = dict(raw) if isinstance(raw, dict) else {}
        if isinstance(attempt, dict):
            # Preserve a failed/no-usage attempt in the summary instead of
            # silently dropping it.  This makes the final ledger answer which
            # model was called, whether a retry occurred, and whether usage
            # was unavailable.
            record.setdefault("model", attempt.get("model") or attempt.get("model_tier", ""))
            record.setdefault("model_tier", attempt.get("model_tier", ""))
            record.setdefault("retries", attempt.get("retries", 0))
            record.setdefault("failed", bool(attempt.get("failed")))
            for key in ("batch", "anchor_refs_sent", "requested_claim_ids", "max_tokens"):
                if attempt.get(key) is not None:
                    record.setdefault(key, attempt.get(key))
            for key in ("estimated_input_tokens", "estimated_output_tokens"):
                if attempt.get(key) is not None:
                    record.setdefault(key, attempt.get(key))
            if attempt.get("error"):
                record.setdefault("error_type", attempt.get("error"))
        if not record:
            return
        usage_records.append(record)
        try:
            estimated_input_tokens_total += int(record.get("estimated_input_tokens") or 0)
        except (TypeError, ValueError):
            pass
        try:
            estimated_output_tokens_total += int(record.get("estimated_output_tokens") or 0)
        except (TypeError, ValueError):
            pass
        if any(record.get(key) is not None for key in provider_input_keys | provider_output_keys):
            provider_usage_seen = True
        if any(record.get(key) is not None for key in estimated_input_keys | estimated_output_keys):
            estimated_usage_seen = True
        for key in (
            "input_tokens",
            "prompt_tokens",
            "input_token_count",
            "estimated_input_tokens",
        ):
            if record.get(key) in (None, ""):
                continue
            try:
                input_tokens += int(record.get(key))
                break
            except (TypeError, ValueError):
                continue
        for key in (
            "output_tokens",
            "completion_tokens",
            "output_token_count",
            "estimated_output_tokens",
        ):
            if record.get(key) in (None, ""):
                continue
            try:
                output_tokens += int(record.get(key))
                break
            except (TypeError, ValueError):
                continue

        model_name = str(
            record.get("model_name")
            or record.get("model")
            or record.get("model_tier")
            or "unknown"
        )
        model_row = per_model.setdefault(model_name, {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "failed_calls": 0,
            "estimated_cost_cny": 0.0,
        })
        input_source = (
            "provider_reported"
            if any(record.get(key) not in (None, "") for key in provider_input_keys)
            else "estimated"
            if any(record.get(key) not in (None, "") for key in estimated_input_keys)
            else "unavailable"
        )
        output_source = (
            "provider_reported"
            if any(record.get(key) not in (None, "") for key in provider_output_keys)
            else "estimated"
            if any(record.get(key) not in (None, "") for key in estimated_output_keys)
            else "unavailable"
        )
        source_row = per_model_token_sources.setdefault(model_name, set())
        source_row.update({f"input:{input_source}", f"output:{output_source}"})
        model_row["calls"] += 1
        call_input_tokens = next(
            (int(record.get(key)) for key in (
                "input_tokens", "prompt_tokens", "input_token_count", "estimated_input_tokens"
            ) if str(record.get(key, "")).strip().isdigit()),
            0,
        )
        call_output_tokens = next(
            (int(record.get(key)) for key in (
                "output_tokens", "completion_tokens", "output_token_count", "estimated_output_tokens"
            ) if str(record.get(key, "")).strip().isdigit()),
            0,
        )
        model_row["input_tokens"] += call_input_tokens
        model_row["output_tokens"] += call_output_tokens
        if record.get("failed"):
            model_row["failed_calls"] += 1
        try:
            model_row["estimated_cost_cny"] += estimate_call_cost_cny(
                model_name,
                call_input_tokens,
                call_output_tokens,
            )
        except Exception:
            pass

    for state in states:
        audit = state.get("llm_audit") or {}
        claim_pool_attempts = audit.get("claim_pool_generation_attempts") or []
        call_count += len(claim_pool_attempts)
        for item in claim_pool_attempts:
            if isinstance(item, dict):
                add_usage(item.get("usage"), item)
        generation = audit.get("generation_attempts") or []
        call_count += len(generation)
        for item in generation:
            if isinstance(item, dict):
                add_usage(item.get("usage"), item)
        for key in ("evidence_verifier_initial", "evidence_verifier_repair"):
            verifier = audit.get(key) or {}
            attempts = verifier.get("attempts") or []
            call_count += len(attempts)
            for item in attempts:
                if isinstance(item, dict):
                    add_usage(item.get("usage"), item)
        arbiter = audit.get("arbiter") or {}
        arbiter_attempts = arbiter.get("attempts") or []
        call_count += len(arbiter_attempts)
        for item in arbiter_attempts:
            if isinstance(item, dict):
                add_usage(item.get("usage"), item)
        try:
            legacy_arbiter_count = int(audit.get("arbiter_call_count") or 0)
            if not arbiter_attempts:
                call_count += legacy_arbiter_count
        except (TypeError, ValueError):
            pass
        semantic_judge = (
            (state.get("fresh_chunk_rebinding") or {}).get("semantic_judge")
            or {}
        )
        try:
            semantic_api_calls = max(
                0, int(semantic_judge.get("api_call_count") or 0)
            )
        except (TypeError, ValueError):
            semantic_api_calls = 0
        call_count += semantic_api_calls
        if semantic_api_calls:
            semantic_usage = (
                dict(semantic_judge.get("usage"))
                if isinstance(semantic_judge.get("usage"), dict)
                else {}
            )
            semantic_usage.setdefault(
                "model_name",
                semantic_judge.get("actual_model")
                or semantic_judge.get("model_tier")
                or "unknown",
            )
            if not any(
                semantic_usage.get(key) not in (None, "")
                for key in (
                    "input_tokens", "prompt_tokens", "input_token_count",
                    "estimated_input_tokens",
                )
            ):
                semantic_usage["estimated_input_tokens"] = (
                    semantic_judge.get("input_tokens", 0)
                )
            if not any(
                semantic_usage.get(key) not in (None, "")
                for key in (
                    "output_tokens", "completion_tokens", "output_token_count",
                    "estimated_output_tokens",
                )
            ):
                semantic_usage["estimated_output_tokens"] = (
                    semantic_judge.get("output_tokens", 0)
                )
            semantic_usage.setdefault(
                "estimated_cost_cny",
                semantic_judge.get("estimated_cost_cny", 0.0),
            )
            semantic_usage["failed"] = bool(
                semantic_usage.get("failed")
                or semantic_judge.get("fallback_used")
                or semantic_judge.get("error")
            )
            if semantic_judge.get("error"):
                semantic_usage["error_type"] = semantic_judge.get("error")
            semantic_usage.setdefault(
                "batch", "fresh_evidence_semantic_judge"
            )
            add_usage(semantic_usage)
    return {
        "calls_observed_or_estimated": call_count,
        "input_tokens_observed": input_tokens,
        "output_tokens_observed": output_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usage_records": usage_records,
        "per_model": {
            key: {
                **value,
                "estimated_cost_cny": round(float(value.get("estimated_cost_cny", 0.0)), 6),
                "input_token_source": (
                    "mixed"
                    if len({item.split(":", 1)[1] for item in per_model_token_sources.get(key, set()) if item.startswith("input:")}) > 1
                    else next((item.split(":", 1)[1] for item in per_model_token_sources.get(key, set()) if item.startswith("input:")), "unavailable")
                ),
                "output_token_source": (
                    "mixed"
                    if len({item.split(":", 1)[1] for item in per_model_token_sources.get(key, set()) if item.startswith("output:")}) > 1
                    else next((item.split(":", 1)[1] for item in per_model_token_sources.get(key, set()) if item.startswith("output:")), "unavailable")
                ),
                "cost_source": "estimated",
            }
            for key, value in per_model.items()
        },
        "estimated_cost_cny": round(
            sum(float(value.get("estimated_cost_cny", 0.0)) for value in per_model.values()),
            6,
        ),
        "estimated_input_tokens_total": estimated_input_tokens_total,
        "estimated_output_tokens_total": estimated_output_tokens_total,
        "max_batch_estimated_input_tokens": max(
            [
                int(record.get("estimated_input_tokens") or 0)
                for record in usage_records
                if record.get("batch") is not None
            ]
            or [0]
        ),
        "usage_is_provider_reported": provider_usage_seen,
        "metric_provenance": {
            "input_tokens": (
                "mixed" if provider_usage_seen and estimated_usage_seen
                else "provider_reported" if provider_usage_seen
                else "estimated" if estimated_usage_seen else "unavailable"
            ),
            "output_tokens": (
                "mixed" if provider_usage_seen and estimated_usage_seen
                else "provider_reported" if provider_usage_seen
                else "estimated" if estimated_usage_seen else "unavailable"
            ),
            "cost_cny": "estimated",
        },
        "token_count_source": (
            "provider_reported"
            if provider_usage_seen
            else "estimated"
            if estimated_usage_seen
            else "unavailable"
        ),
    }


def _fresh_semantic_judge_summary(
    states: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    sections: dict[str, dict[str, Any]] = {}
    for state in states:
        section_id = str((state.get("section") or {}).get("section_id") or "")
        telemetry = (
            (state.get("fresh_chunk_rebinding") or {}).get("semantic_judge")
            or {}
        )
        if section_id:
            sections[section_id] = dict(telemetry)
    called = [item for item in sections.values() if item.get("called")]
    token_sources = {
        str(item.get("token_provenance") or "unavailable")
        for item in called
    }
    cost_sources = {
        str(item.get("cost_provenance") or "unavailable")
        for item in called
    }
    errors = {
        section_id: str(item.get("error"))
        for section_id, item in sections.items()
        if item.get("error")
    }
    return {
        "enabled": any(item.get("enabled") for item in sections.values()),
        "sections": sections,
        "sections_enabled": sorted(
            section_id
            for section_id, item in sections.items()
            if item.get("enabled")
        ),
        "sections_called": sorted(
            section_id
            for section_id, item in sections.items()
            if item.get("called")
        ),
        "batch_count": sum(int(item.get("batch_count") or 0) for item in called),
        "call_count": sum(int(item.get("call_count") or 0) for item in called),
        "api_call_count": sum(
            int(item.get("api_call_count") or 0) for item in called
        ),
        "actual_models": sorted({
            str(item.get("actual_model"))
            for item in called
            if item.get("actual_model")
        }),
        "providers": sorted({
            str(item.get("provider"))
            for item in called
            if item.get("provider")
        }),
        "input_tokens": sum(int(item.get("input_tokens") or 0) for item in called),
        "output_tokens": sum(int(item.get("output_tokens") or 0) for item in called),
        "token_provenance": (
            next(iter(token_sources))
            if len(token_sources) == 1
            else "mixed" if token_sources else "unavailable"
        ),
        "estimated_cost_cny": round(sum(
            float(item.get("estimated_cost_cny") or 0.0) for item in called
        ), 6),
        "cost_provenance": (
            next(iter(cost_sources))
            if len(cost_sources) == 1
            else "mixed" if cost_sources else "unavailable"
        ),
        "fallback_used": any(item.get("fallback_used") for item in called),
        "errors": errors,
        "one_batch_invariant": all(
            bool(item.get("one_batch_invariant", True))
            and int(item.get("batch_count") or 0) <= 1
            and int(item.get("api_call_count") or 0) <= 1
            for item in called
        ),
        "included_once_in_llm_aggregate": True,
    }


@dataclass(slots=True)
class SectionArgumentContract:
    schema_version: str
    section_id: str
    core_question: str
    central_judgment: str
    argument_role: str
    argument_tasks: list[dict[str, Any]] = field(default_factory=list)
    material_requirements: list[dict[str, Any]] = field(default_factory=list)
    predecessor_section_id: str = ""
    following_section_id: str = ""
    mentor_guidance: list[str] = field(default_factory=list)
    synthesis_task: str = ""
    transition_from_previous: str = ""
    transition_to_next: str = ""
    target_word_range: list[int] = field(default_factory=list)
    visual_argument_slots: list[dict[str, Any]] = field(default_factory=list)
    status: str = "contract_ready"
    unresolved_items: list[str] = field(default_factory=list)
    source_fields: dict[str, str] = field(default_factory=dict)
    # These fields are the canonical M2a contract.  The legacy
    # ``section_argument_contract`` name remains serialized as an alias.
    key_questions: list[str] = field(default_factory=list)
    scope_guardrails: list[str] = field(default_factory=list)
    transitions: dict[str, str] = field(default_factory=dict)
    argument_sequence: list[Any] = field(default_factory=list)
    paragraph_functions: list[Any] = field(default_factory=list)
    axis_assignments: list[dict[str, Any]] = field(default_factory=list)
    argument_structure: dict[str, Any] = field(default_factory=dict)
    candidate_material_pool: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Compatibility aliases expected by the original M2a contract
        # consumer.  They are aliases, not a second source of truth.
        payload["central_thesis"] = self.central_judgment
        payload["section_role"] = self.argument_role
        payload["required_evidence_roles"] = [
            item.get("role") for item in self.material_requirements
            if isinstance(item, dict) and item.get("role")
        ]
        payload["forbidden_overclaims"] = list(self.scope_guardrails)
        payload["open_questions"] = list(self.unresolved_items)
        payload["axis_assignments"] = [dict(item) for item in self.axis_assignments]
        payload["argument_structure"] = dict(self.argument_structure)
        payload["candidate_material_pool"] = dict(self.candidate_material_pool)
        return payload


@dataclass(slots=True)
class CoverageRequest:
    request_id: str
    section_id: str
    iteration: int
    priority: str
    trigger: str
    missing_claim_ids: list[str] = field(default_factory=list)
    missing_roles: list[str] = field(default_factory=list)
    missing_relation_tasks: list[str] = field(default_factory=list)
    non_blocking_gaps: list[dict[str, Any]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    # Explicit target metadata is generated together with each query.  The
    # legacy string list remains for compatibility with older callers, but
    # production Phase 2 must consume this list rather than infer ownership
    # from query vocabulary later.
    query_targets: list[dict[str, Any]] = field(default_factory=list)
    expected_new_papers: int = 1
    per_wave_paper_budget: int = 3
    target_total_new_papers: int = 1
    stop_condition: dict[str, Any] = field(default_factory=dict)
    affected_section_ids: list[str] = field(default_factory=list)
    status: str = "pending"
    execution_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Phase3ArgumentOrchestrator:
    """Coordinate contracts, claims, argument graph, binding, and gap loops."""

    def __init__(
        self,
        *,
        blueprint: dict[str, Any] | Path,
        scope_map: dict[str, Any] | Path | None = None,
        coverage_atlas: dict[str, Any] | Path | None = None,
        synthesis_bundles: dict[str, Any] | Path | None = None,
        relation_graph: dict[str, Any] | Path | None = None,
        shared_ledger_path: Path | None = None,
        claim_pool_inventory_ledger_path: Path | None = None,
        shared_kb_paths: Iterable[Path] = (),
        overlay_paths: dict[str, Path] | None = None,
        mentor_advice: dict[str, Any] | None = None,
        mentor_library_path: Path | None = None,
        output_dir: Path | None = None,
        max_iterations: int = 2,
        real_llm_claims: bool = False,
        claim_pool_enabled: bool | None = None,
        claim_model_tier: str = "cheap_model",
        real_llm_dag: bool = False,
        dag_model_tier: str = "cheap_model",
        max_m2a_input_tokens: int = 8_000,
        max_m2a_records: int = 24,
        max_dag_candidates: int = 80,
        dag_claims_per_section: int = 16,
        dag_total_claims: int = 128,
        claim_pool_served_limit: int = 200,
        claim_pool_target_range: list[int] | None = None,
        claim_pool_shortlist_limit: int = 32,
        authoring_core_chunk_limit: int = 12,
        runtime_failures: Mapping[str, Any] | None = None,
        execute_coverage: bool = False,
        section_ids_to_process: Iterable[str] | None = None,
        enable_fresh_evidence_semantic_judge: bool = False,
        fresh_evidence_semantic_judge: Callable[[dict[str, Any]], Any] | None = None,
        fresh_evidence_semantic_judge_model_tier: str = "cheap_model",
    ) -> None:
        self.blueprint = _read_json(blueprint) if isinstance(blueprint, Path) else dict(blueprint or {})
        self.scope_map = (
            _read_json(scope_map) if isinstance(scope_map, Path)
            else dict(scope_map or self.blueprint.get("review_scope_map") or {})
        )
        self.coverage_atlas = (
            _read_json(coverage_atlas) if isinstance(coverage_atlas, Path)
            else dict(coverage_atlas or {})
        )
        self.input_synthesis_bundles = (
            _read_json(synthesis_bundles) if isinstance(synthesis_bundles, Path)
            else dict(synthesis_bundles or {})
        )
        self.relation_graph = (
            _read_json(relation_graph) if isinstance(relation_graph, Path)
            else dict(relation_graph or {})
        )
        self.shared_ledger_path = Path(shared_ledger_path) if shared_ledger_path else None
        self.claim_pool_inventory_ledger_path = (
            Path(claim_pool_inventory_ledger_path)
            if claim_pool_inventory_ledger_path
            else None
        )
        self.shared_kb_paths = [Path(item) for item in shared_kb_paths if item]
        self.overlay_paths = {str(key): Path(value) for key, value in (overlay_paths or {}).items()}
        self.mentor_advice = dict(mentor_advice or {})
        self.mentor_library_path = Path(mentor_library_path) if mentor_library_path else None
        self.output_dir = Path(output_dir or PROJECT_ROOT / "outputs" / "phase3_argument_orchestration")
        self.max_iterations = max(1, int(max_iterations))
        self.real_llm_claims = bool(real_llm_claims)
        # The top-level production harness passes True explicitly.  Direct
        # offline/legacy fixtures retain the historical single-call behavior
        # unless they opt into the strong pool themselves.
        self.claim_pool_enabled = bool(claim_pool_enabled)
        self.claim_model_tier = claim_model_tier
        self.real_llm_dag = bool(real_llm_dag)
        self.dag_model_tier = dag_model_tier
        self.max_m2a_input_tokens = max(1000, int(max_m2a_input_tokens or 8000))
        self.max_m2a_records = max(1, int(max_m2a_records or 24))
        self.max_dag_candidates = max(1, int(max_dag_candidates or 80))
        self.dag_claims_per_section = max(
            4, int(dag_claims_per_section or 16)
        )
        self.dag_total_claims = max(16, int(dag_total_claims or 128))
        self.claim_pool_served_limit = max(
            12, int(claim_pool_served_limit or 200)
        )
        raw_target = list(claim_pool_target_range or [80, 120])
        if len(raw_target) < 2:
            raw_target = [80, 120]
        try:
            target_low, target_high = int(raw_target[0]), int(raw_target[1])
        except (TypeError, ValueError):
            target_low, target_high = 80, 120
        self.claim_pool_target_range = [
            max(1, min(target_low, target_high)),
            max(1, max(target_low, target_high)),
        ]
        self.claim_pool_shortlist_limit = max(
            1, int(claim_pool_shortlist_limit or 32)
        )
        self.authoring_core_chunk_limit = max(
            4, int(authoring_core_chunk_limit or 12)
        )
        self.runtime_failures = {
            str(key): dict(value)
            for key, value in (runtime_failures or {}).items()
            if str(key).strip() and isinstance(value, Mapping)
        }
        self.execute_coverage = bool(execute_coverage)
        self.enable_fresh_evidence_semantic_judge = bool(
            enable_fresh_evidence_semantic_judge
        )
        self.fresh_evidence_semantic_judge_model_tier = str(
            fresh_evidence_semantic_judge_model_tier or "cheap_model"
        )
        self.fresh_evidence_semantic_judge = fresh_evidence_semantic_judge
        if (
            self.enable_fresh_evidence_semantic_judge
            and self.fresh_evidence_semantic_judge is None
        ):
            self.fresh_evidence_semantic_judge = QwenFreshEvidenceSemanticJudge(
                model_tier=self.fresh_evidence_semantic_judge_model_tier,
            )
        self.section_ids_to_process = {
            str(item).strip() for item in (section_ids_to_process or ()) if str(item).strip()
        }
        self._mentor_cache: dict[str, Any] | None = None
        self._m2a_compact_context: set[str] = set()
        self._m2a_minimal_context: set[str] = set()
        self._m2a_budget_audit: dict[str, dict[str, Any]] = {}

    def _mentor(self) -> dict[str, Any]:
        if self.mentor_advice:
            return self.mentor_advice
        if self._mentor_cache is not None:
            return self._mentor_cache
        if self.mentor_library_path and self.mentor_library_path.exists():
            try:
                agent = ReviewMentorAgent(
                    active_library_path=self.mentor_library_path,
                    real_llm=False,
                )
                self._mentor_cache = agent.build_advice(
                    user_question=_text(self.scope_map.get("user_question")),
                    problem_understanding=_text(self.scope_map.get("problem_understanding")),
                    scope_definition="; ".join(self.scope_map.get("inclusion_boundaries") or []),
                )
                return self._mentor_cache
            except Exception:
                pass
        return {}

    def _section_row_from_atlas(self, section_id: str) -> dict[str, Any]:
        for row in self.coverage_atlas.get("sections") or []:
            if isinstance(row, dict) and str(row.get("section_id")) == section_id:
                return row
        return {}

    def _build_contract(
        self,
        section: dict[str, Any],
        index: int,
        sections: list[dict[str, Any]],
    ) -> SectionArgumentContract:
        section_id = _text(section.get("section_id"), 80)
        title = _text(section.get("title") or section.get("section_title"), 300)
        raw_questions = section.get("key_questions") or []
        if isinstance(raw_questions, str):
            raw_questions = [raw_questions]
        key_questions = _unique(_clean_text(item) for item in raw_questions)
        question = _clean_text(
            section.get("core_question")
            or section.get("key_question")
            or (key_questions[0] if key_questions else "")
            or section.get("chapter_argument")
            or title
        )
        judgment = _clean_text(
            section.get("central_judgment")
            or section.get("central_thesis")
            or section.get("review_thesis")
            or section.get("chapter_argument")
        )
        role = _text(section.get("argument_role") or section.get("chapter_argument"), 900)
        raw_guardrails = section.get("scope_guardrails") or []
        if isinstance(raw_guardrails, str):
            raw_guardrails = [raw_guardrails]
        scope_guardrails = _unique(_clean_text(item) for item in raw_guardrails)
        raw_paragraph_functions = section.get("paragraph_functions") or []
        if isinstance(raw_paragraph_functions, str):
            raw_paragraph_functions = [raw_paragraph_functions]
        paragraph_functions = list(raw_paragraph_functions)
        raw_sequence = section.get("argument_sequence") or []
        if isinstance(raw_sequence, str):
            raw_sequence = [raw_sequence]
        argument_sequence = list(raw_sequence)
        transition_raw = section.get("transitions") or section.get("transition_contract") or {}
        transitions = dict(transition_raw) if isinstance(transition_raw, dict) else {}
        transition_from_previous = _clean_text(
            section.get("transition_from_previous")
            or transitions.get("from_previous")
            or section.get("preceding_section_conclusion")
        )
        transition_to_next = _clean_text(
            section.get("transition_to_next")
            or transitions.get("to_next")
            or section.get("following_section_role")
        )
        if transition_from_previous:
            transitions.setdefault("from_previous", transition_from_previous)
        if transition_to_next:
            transitions.setdefault("to_next", transition_to_next)
        synthesis_task = _clean_text(
            section.get("synthesis_task")
            or section.get("synthesis_instruction")
            or section.get("section_synthesis_task")
        )
        target_word_range = _normalise_word_range(
            section.get("target_word_range")
            or section.get("word_range")
            or section.get("target_word_count")
        )
        visual_argument_slots = _normalise_visual_slots(
            section.get("visual_argument_slots")
            or section.get("visual_slots")
        )
        axis_assignments = _normalise_axis_assignments(
            section.get("axis_assignments")
        )
        argument_structure = _normalise_argument_structure(
            section.get("argument_structure")
            or (
                (section.get("claim_graph_seed") or {}).get("argument_structure")
                if isinstance(section.get("claim_graph_seed"), Mapping)
                else {}
            )
        )
        decision_framework = _decision_framework_contract(section)
        if decision_framework:
            argument_structure["decision_framework"] = decision_framework
        candidate_material_pool = _normalise_candidate_material_pool(
            section.get("candidate_material_pool")
        )
        mentor = self._mentor()
        section_mentor = (
            section.get("mentor_guidance")
            if section.get("mentor_guidance") not in (None, "", [])
            else section.get("review_mentor_advice")
            or mentor
        )
        guidance: list[str] = _normalise_guidance(section_mentor)
        guidance.extend(_text(item, 600) for item in self.scope_map.get("m1_architecture_guidance") or [] if _text(item, 600))

        tasks: list[dict[str, Any]] = []
        task_descriptions: set[str] = set()

        def add_task(raw: Any, *, kind: str, source: str, required: bool = True) -> None:
            if isinstance(raw, dict):
                task = dict(raw)
                description = _clean_text(
                    task.get("description") or task.get("question")
                    or task.get("task") or task.get("claim_seed")
                )
            else:
                description = _clean_text(raw)
                task = {}
            if not description or description.casefold() in task_descriptions:
                return
            task_descriptions.add(description.casefold())
            task.setdefault("task_id", f"{section_id}:task:{len(tasks)+1:02d}")
            task.setdefault("kind", kind)
            task.setdefault("description", description)
            task.setdefault("required", required)
            task.setdefault("source", source)
            if scope_guardrails:
                task.setdefault("scope_guardrails", scope_guardrails)
            tasks.append(task)

        for raw in section.get("argument_tasks") or []:
            add_task(raw, kind="argument_task", source="blueprint.argument_tasks")
        for raw in argument_sequence:
            add_task(raw, kind="argument_step", source="blueprint.argument_sequence")
        for raw in key_questions:
            add_task(raw, kind="key_question", source="blueprint.key_questions")
        for raw in paragraph_functions:
            add_task(raw, kind="paragraph_function", source="blueprint.paragraph_functions", required=False)
        for raw in section.get("scope_guardrail_tasks") or []:
            add_task(raw, kind="scope_guardrail_task", source="blueprint.scope_guardrail_tasks", required=False)
        if not tasks and judgment:
            add_task(judgment, kind="core_judgment", source="blueprint.central_judgment")

        requirements: list[dict[str, Any]] = []
        role_targets = section.get("role_source_targets") if isinstance(section.get("role_source_targets"), dict) else {}
        roles = _unique(
            list(section.get("required_roles") or [])
            + list(section.get("optional_roles") or [])
            + list(section.get("literature_roles") or [])
        )
        for role_name in roles:
            try:
                target = int(role_targets.get(role_name) or 0)
            except (TypeError, ValueError):
                target = 0
            if not target:
                target = 2 if role_name in set(section.get("required_roles") or []) else 1
            requirements.append({
                "requirement_id": f"{section_id}:role:{role_name}",
                "kind": "literature_role",
                "role": role_name,
                "minimum_papers": target,
                "allowed_permissions": ["factual_support", "contextual_or_qualified_support"],
                "source": "blueprint",
            })
        for task in self.scope_map.get("relation_tasks") or section.get("relationship_tasks") or []:
            task_name = _text(task, 80)
            if task_name:
                requirements.append({
                    "requirement_id": f"{section_id}:relation:{task_name}",
                    "kind": "semantic_relation",
                    "relation_task": task_name,
                    "minimum_edges": 1,
                    "allowed_permissions": ["factual_support", "contextual_or_qualified_support"],
                "source": "scope_map_or_blueprint",
                })

        for relation_role in argument_structure.get("required_relation_roles") or []:
            role_name = _text(relation_role, 100)
            if not role_name:
                continue
            requirement_id = f"{section_id}:argument_role:{role_name}"
            if any(item.get("requirement_id") == requirement_id for item in requirements):
                continue
            requirements.append({
                "requirement_id": requirement_id,
                "kind": "argument_relation_role",
                "role": role_name,
                "minimum_bindings": 1 if role_name in {"support", "counterevidence", "boundary_condition"} else 0,
                "source": "section.argument_structure",
            })

        unresolved: list[str] = []
        if not question:
            unresolved.append("section_core_question_missing")
        if not judgment:
            unresolved.append("section_central_judgment_missing")
        if not tasks:
            unresolved.append("section_argument_tasks_missing")
        previous = str(sections[index - 1].get("section_id")) if index > 0 else ""
        following = str(sections[index + 1].get("section_id")) if index + 1 < len(sections) else ""
        return SectionArgumentContract(
            schema_version="research_harness.section_argument_contract.v1",
            section_id=section_id,
            core_question=question,
            central_judgment=judgment,
            argument_role=role,
            argument_tasks=tasks,
            material_requirements=requirements,
            predecessor_section_id=previous,
            following_section_id=following,
            mentor_guidance=list(dict.fromkeys(guidance)),
            synthesis_task=synthesis_task,
            transition_from_previous=transition_from_previous,
            transition_to_next=transition_to_next,
            target_word_range=target_word_range,
            visual_argument_slots=visual_argument_slots,
            status="contract_ready" if not unresolved else "needs_contract_input",
            unresolved_items=unresolved,
            source_fields={
                "core_question": "section.core_question/key_questions/chapter_argument/title",
                "central_judgment": "section.central_judgment/central_thesis/review_thesis/chapter_argument",
                "mentor_guidance": "section.mentor_guidance/review_mentor_advice/scope_map/m1_architecture_guidance",
                "synthesis_task": "section.synthesis_task/synthesis_instruction/section_synthesis_task",
                "transition_from_previous": "section.transition_from_previous/transitions.from_previous/preceding_section_conclusion",
                "transition_to_next": "section.transition_to_next/transitions.to_next/following_section_role",
                "target_word_range": "section.target_word_range/word_range/target_word_count",
                "visual_argument_slots": "section.visual_argument_slots/visual_slots",
                "key_questions": "section.key_questions",
                "scope_guardrails": "section.scope_guardrails",
                "transitions": "section.transitions/transition_contract/neighbor fields",
                "axis_assignments": "section.axis_assignments/concept-map and material bindings",
                "argument_structure": "section.argument_structure/claim_graph_seed.argument_structure",
                "candidate_material_pool": "section.candidate_material_pool/phase3 canonical graph inventory",
            },
            key_questions=key_questions,
            scope_guardrails=scope_guardrails,
            transitions=transitions,
            argument_sequence=argument_sequence,
            paragraph_functions=paragraph_functions,
            axis_assignments=axis_assignments,
            argument_structure=argument_structure,
            candidate_material_pool=candidate_material_pool,
        )

    def _m2a_section_view(
        self,
        section: dict[str, Any],
        records: list[dict[str, Any]],
        *,
        compact_context: bool = False,
    ) -> dict[str, Any]:
        """Build the bounded view actually sent to M2a.

        The full section remains in the Phase-3 state and handoff.  Only the
        model-facing view is compacted, so a token budget can never silently
        erase the audit/context record kept for later writing.
        """

        view = dict(section)
        selected = [dict(item) for item in records if isinstance(item, dict)]
        view["candidate_text_chunks"] = selected
        view["candidate_text_chunk_ids"] = [
            str(item.get("chunk_id")) for item in selected if item.get("chunk_id")
        ]
        view["candidate_pool_ids"] = list(view["candidate_text_chunk_ids"])
        # Full inventory IDs are local audit state, not scientific context.
        # Keeping counts and the durable ref is enough for the model; each
        # strong-pool call receives only its current batch IDs and summaries.
        view["candidate_material_pool"] = _model_candidate_material_pool(
            view.get("candidate_material_pool")
        )
        for contract_key in ("section_contract", "section_argument_contract"):
            raw_model_contract = view.get(contract_key)
            if not isinstance(raw_model_contract, Mapping):
                continue
            model_contract = dict(raw_model_contract)
            model_contract["candidate_material_pool"] = _model_candidate_material_pool(
                model_contract.get("candidate_material_pool")
            )
            view[contract_key] = model_contract
        if not compact_context:
            return view

        section_id = str(view.get("section_id") or "")
        raw_contract = view.get("section_contract") or view.get("section_argument_contract") or {}
        if section_id in self._m2a_minimal_context:
            contract = dict(raw_contract) if isinstance(raw_contract, dict) else {}
            minimal_contract = {
                key: contract.get(key)
                for key in (
                    "core_question",
                    "central_thesis",
                    "argument_tasks",
                    "required_evidence_roles",
                    "forbidden_overclaims",
                    "axis_assignments",
                    "argument_structure",
                    "candidate_material_pool",
                    "word_budget",
                )
                if contract.get(key) not in (None, "", [], {})
            }
            for key in (
                "argument_tasks", "required_evidence_roles", "forbidden_overclaims",
                "axis_assignments",
            ):
                value = minimal_contract.get(key)
                if isinstance(value, list):
                    minimal_contract[key] = [
                        _clean_text(item)[:180] if not isinstance(item, dict) else {
                            str(k): _clean_text(v)[:180]
                            for k, v in item.items()
                        }
                        for item in value[:3]
                    ]
            return {
                "section_id": view.get("section_id", ""),
                "title": _clean_text(view.get("title"))[:160],
                "argument_role": _clean_text(view.get("argument_role"))[:240],
                "section_contract": minimal_contract,
                "section_argument_contract": minimal_contract,
                "candidate_text_chunks": selected,
                "candidate_text_chunk_ids": list(view["candidate_text_chunk_ids"]),
                "candidate_pool_ids": list(view["candidate_pool_ids"]),
                "candidate_visual_chunks": [],
                "claim_graph_seed": {},
                "review_mentor_advice": {},
                "review_scope_map": {},
            }
        if isinstance(raw_contract, dict):
            contract = dict(raw_contract)
            for key in (
                "core_question", "central_thesis", "synthesis_task",
                "transition_from_previous", "transition_to_next",
            ):
                if contract.get(key) not in (None, ""):
                    contract[key] = _clean_text(contract[key])[:420]
            for key in (
                "argument_tasks", "argument_sequence", "paragraph_functions",
                "key_questions", "scope_guardrails", "mentor_guidance",
                "visual_argument_slots", "required_evidence_roles",
                "forbidden_overclaims", "open_questions", "axis_assignments",
            ):
                raw = contract.get(key)
                if isinstance(raw, list):
                    compact_items = []
                    for item in raw[:5]:
                        if isinstance(item, dict):
                            compact_items.append({
                                key: _clean_text(value)[:240]
                                if isinstance(value, str) else value
                                for key, value in item.items()
                            })
                        else:
                            compact_items.append(_clean_text(item)[:240])
                    contract[key] = compact_items
            transitions = contract.get("transitions")
            if isinstance(transitions, dict):
                contract["transitions"] = {
                    str(key): _clean_text(value)[:260]
                    for key, value in list(transitions.items())[:4]
                }
            if isinstance(contract.get("argument_structure"), dict):
                structure = dict(contract["argument_structure"])
                for key in ("required_relation_roles", "writing_sequence", "relation_types_to_check"):
                    if isinstance(structure.get(key), list):
                        structure[key] = [
                            _clean_text(item)[:180]
                            for item in structure[key][:8]
                            if _clean_text(item)
                        ]
                contract["argument_structure"] = structure
            if isinstance(contract.get("candidate_material_pool"), dict):
                contract["candidate_material_pool"] = _model_candidate_material_pool(
                    contract["candidate_material_pool"]
                )
            view["section_contract"] = contract
            view["section_argument_contract"] = contract
        # Mentor advice and the broad scope map are already represented by the
        # contract for M2a.  Omitting their duplicate copies is a cheap,
        # topic-generic way to stay within the cap.
        view["review_mentor_advice"] = {}
        view["review_scope_map"] = {}
        return view

    def _estimate_m2a_input_tokens(
        self,
        section: dict[str, Any],
        records: list[dict[str, Any]],
        *,
        compact_context: bool = False,
    ) -> int:
        view = self._m2a_section_view(
            section,
            records,
            compact_context=compact_context,
        )
        payload = ClaimDecomposer(real_llm=False)._build_input_payload(view)
        return max(1, len(json.dumps(payload, ensure_ascii=False)) // 4)

    def _select_m2a_input(
        self_or_section: "Phase3ArgumentOrchestrator | dict[str, Any]",
        section_or_contract: "dict[str, Any] | SectionArgumentContract",
        contract_or_records: "SectionArgumentContract | list[dict[str, Any]]",
        records_or_graph: "list[dict[str, Any]] | CanonicalAssetGraph",
        graph: CanonicalAssetGraph | None = None,
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Select a bounded, diverse M2a portfolio before claim generation."""

        # Keep the historical class-level helper contract alive.  A few
        # downstream/offline callers used ``Class._select_m2a_input(section,
        # contract, records, graph)`` before the live harness gained per-run
        # budgets.  The production instance path receives ``graph`` through
        # normal method binding and applies the configured caps; the legacy
        # path keeps the old 24-record diversity behavior without any model
        # call or invented data.
        owner: "Phase3ArgumentOrchestrator | None"
        if graph is None:
            owner = None
            section = self_or_section
            contract = section_or_contract
            records = contract_or_records
            graph = records_or_graph
        else:
            owner = self_or_section  # type: ignore[assignment]
            section = section_or_contract
            contract = contract_or_records
            records = records_or_graph

        if not isinstance(section, dict):
            raise TypeError("section must be a mapping")
        if not isinstance(contract, SectionArgumentContract):
            raise TypeError("contract must be a SectionArgumentContract")
        if not isinstance(records, list):
            raise TypeError("records must be a list")
        if graph is None:
            raise TypeError("graph is required")

        contract_payload = contract.to_dict()
        selector_section = {
            **section,
            "section_contract": contract_payload,
            "argument_tasks": contract.argument_tasks,
            "key_questions": contract.key_questions,
            "scope_guardrails": contract.scope_guardrails,
        }
        portfolio = select_evidence_portfolio(
            section=selector_section,
            candidates=records,
            claims=(),
            relation_edges=(),
            allowed_paper_ids=graph.papers,
            allowed_chunk_ids=graph.chunks,
            max_core_chunks=16,
            max_core_chunks_per_paper=2,
        )
        record_by_id = {str(item.get("chunk_id")): item for item in records}
        selected_ids = list(portfolio.core_chunk_ids)
        selected_papers = {
            str(record_by_id[cid].get("paper_id") or "")
            for cid in selected_ids
            if cid in record_by_id
        }
        all_usable_papers = {
            str(row.get("paper_id") or "")
            for row in records
            if isinstance(row, dict)
            and row.get("paper_id")
            and evidence_ceiling(row)[0] != DISCOVERY
        }
        # Core selection already enforces two chunks per paper.  The extra
        # context view must not undo that work by appending twenty chunks from
        # one highly productive paper.  When several papers are available,
        # first give uncovered papers one slot, then use a small per-paper cap.
        multi_paper_cap = 4 if len(all_usable_papers) > 1 else 24
        paper_counts = {
            pid: sum(
                1 for cid in selected_ids
                if cid in record_by_id
                and str(record_by_id[cid].get("paper_id") or "") == pid
            )
            for pid in selected_papers
        }
        candidate_rows = [
            record_by_id[str(cid)]
            for cid in portfolio.candidate_chunk_ids
            if str(cid) in record_by_id
            and evidence_ceiling(record_by_id[str(cid)])[0] != DISCOVERY
        ]
        # Diversity pass: add one relevant chunk for each paper not already in
        # the core before spending the remaining context budget on score order.
        for row in candidate_rows:
            if len(selected_ids) >= 24:
                break
            pid = str(row.get("paper_id") or "")
            if not pid or pid in paper_counts:
                continue
            selected_ids.append(str(row["chunk_id"]))
            paper_counts[pid] = 1
        for row in candidate_rows:
            if len(selected_ids) >= 24:
                break
            cid = str(row.get("chunk_id") or "")
            pid = str(row.get("paper_id") or "")
            if not cid or cid in selected_ids or not pid:
                continue
            if paper_counts.get(pid, 0) >= multi_paper_cap:
                continue
            selected_ids.append(cid)
            paper_counts[pid] = paper_counts.get(pid, 0) + 1
        selected_ids = _unique(selected_ids)
        selected_records = [record_by_id[cid] for cid in selected_ids if cid in record_by_id]
        section_id = str(section.get("section_id") or "")
        compact_context = bool(owner and section_id in owner._m2a_compact_context)
        max_m2a_records = owner.max_m2a_records if owner else 24
        max_m2a_input_tokens = owner.max_m2a_input_tokens if owner else 8_000

        def estimate_input(rows: list[dict[str, Any]], *, compact: bool) -> int:
            if owner:
                return owner._estimate_m2a_input_tokens(
                    section,
                    rows,
                    compact_context=compact,
                )
            # The class-level compatibility path intentionally does not
            # impose a new model budget.  Returning zero preserves its old
            # deterministic selector semantics and avoids constructing a
            # temporary orchestrator with filesystem state.
            return 0

        while selected_records and (
            len(selected_records) > max_m2a_records
            or estimate_input(selected_records, compact=compact_context)
            > max_m2a_input_tokens
        ):
            if len(selected_records) > 1:
                selected_records.pop()
                continue
            if not compact_context:
                compact_context = True
                if owner:
                    owner._m2a_compact_context.add(section_id)
                continue
            if owner and section_id not in owner._m2a_minimal_context:
                owner._m2a_minimal_context.add(section_id)
                continue
            selected_records.pop()
        if owner:
            estimated = estimate_input(
                selected_records,
                compact=section_id in owner._m2a_compact_context,
            )
            owner._m2a_budget_audit[section_id] = {
                "max_input_tokens": owner.max_m2a_input_tokens,
                "estimated_input_tokens": estimated,
                "max_records": owner.max_m2a_records,
                "record_count": len(selected_records),
                "compact_context": section_id in owner._m2a_compact_context,
                "minimal_context": section_id in owner._m2a_minimal_context,
                "candidate_pool_count": len(portfolio.candidate_chunk_ids),
            }
        return portfolio, selected_records

    @staticmethod
    def _context_source_values(
        section: dict[str, Any],
    ) -> dict[str, Any]:
        """Capture values supplied by legacy blueprint fields for audit."""

        raw_mentor = (
            section.get("mentor_guidance")
            if section.get("mentor_guidance") not in (None, "", [])
            else section.get("review_mentor_advice")
        )
        transitions = section.get("transitions") or section.get("transition_contract") or {}
        if not isinstance(transitions, dict):
            transitions = {}
        return {
            "mentor_guidance": _normalise_guidance(raw_mentor),
            "synthesis_task": _clean_text(
                section.get("synthesis_task")
                or section.get("synthesis_instruction")
                or section.get("section_synthesis_task")
            ),
            "transition_from_previous": _clean_text(
                section.get("transition_from_previous")
                or transitions.get("from_previous")
                or section.get("preceding_section_conclusion")
            ),
            "transition_to_next": _clean_text(
                section.get("transition_to_next")
                or transitions.get("to_next")
                or section.get("following_section_role")
            ),
            "target_word_range": _normalise_word_range(
                section.get("target_word_range")
                or section.get("word_range")
                or section.get("target_word_count")
            ),
            "visual_argument_slots": _normalise_visual_slots(
                section.get("visual_argument_slots")
                or section.get("visual_slots")
            ),
            "axis_assignments": _normalise_axis_assignments(
                section.get("axis_assignments")
            ),
            "argument_structure": _normalise_argument_structure(
                section.get("argument_structure")
            ),
        }

    @staticmethod
    def _context_handoff_audit(state: dict[str, Any]) -> dict[str, Any]:
        """Compare source values, serialized contract values, and M2a values."""

        contract = state.get("contract").to_dict() if state.get("contract") else {}
        payload_contract = (state.get("m2a_input_payload") or {}).get("section_contract") or {}
        source = state.get("context_source_values") or {}
        contract_values = {
            key: contract.get(key)
            for key in source
        }
        payload_values = {
            key: payload_contract.get(key)
            for key in source
        }
        checks: dict[str, bool] = {}
        normalization_notes: list[dict[str, Any]] = []

        def guidance_compatible(
            source_item: Any,
            actual_items: list[Any],
        ) -> tuple[bool, str]:
            source_text = _clean_text(source_item)
            source_tokens = source_text.split()
            best_reason = "mismatch"
            for actual in actual_items:
                actual_text = _clean_text(actual)
                if not source_text or not actual_text:
                    continue
                if actual_text == source_text:
                    return True, "exact"
                actual_tokens = actual_text.split()
                has_space_tokens = len(source_tokens) > 1 or len(actual_tokens) > 1
                if not has_space_tokens:
                    coverage = (
                        len(actual_text) / max(1, len(source_text))
                        if actual_text in source_text
                        else (
                            len(source_text) / max(1, len(actual_text))
                            if source_text in actual_text
                            else 0.0
                        )
                    )
                    meaningful = (
                        len(actual_text) >= 8
                        and len(actual_text) >= 4
                        and coverage >= 0.4
                    )
                    if actual_text in source_text:
                        if meaningful:
                            return True, "contained_in_source"
                        best_reason = "too_short_or_low_coverage"
                        continue
                    if source_text in actual_text:
                        if meaningful:
                            return True, "source_contained_in_actual"
                        best_reason = "too_short_or_low_coverage"
                        continue
                    continue

                token_coverage = (
                    len(actual_tokens) / max(1, len(source_tokens))
                    if actual_text in source_text
                    else (
                        len(source_tokens) / max(1, len(actual_tokens))
                        if source_text in actual_text
                        else 0.0
                    )
                )
                meaningful_length = (
                    len(actual_text) >= 24
                    and len(actual_tokens) >= 3
                )
                if actual_text in source_text:
                    if meaningful_length and token_coverage >= 0.4:
                        return True, "contained_in_source"
                    best_reason = "too_short_or_low_coverage"
                    continue
                if source_text in actual_text:
                    if len(source_text) >= 24 and token_coverage >= 0.4:
                        return True, "source_contained_in_actual"
                    best_reason = "too_short_or_low_coverage"
                    continue
                actual_token_set = set(actual_tokens)
                source_token_set = set(source_tokens)
                if (
                    actual_token_set
                    and actual_token_set <= source_token_set
                    and meaningful_length
                    and len(actual_token_set)
                    >= max(3, int(len(source_token_set) * 0.4))
                ):
                    return True, "bounded_token_subset"
            return False, best_reason

        def field_compatible(
            key: str,
            expected: Any,
            actual_contract: Any,
            actual_payload: Any,
            *,
            compare_payload: bool,
        ) -> tuple[bool, list[dict[str, str]]]:
            if key == "mentor_guidance":
                expected_items = list(expected or [])
                actual_contract_items = list(actual_contract or [])
                actual_payload_items = list(actual_payload or [])
                if not expected_items:
                    passed = not actual_contract_items and (
                        not compare_payload or not actual_payload_items
                    )
                    notes = [
                        {
                            "contract_match_modes": (
                                "empty_expected"
                                if not actual_contract_items
                                else "unexpected_nonempty_contract"
                            ),
                            "payload_match_modes": (
                                "not_applicable"
                                if not compare_payload
                                else (
                                    "empty_expected"
                                    if not actual_payload_items
                                    else "unexpected_nonempty_payload"
                                )
                            ),
                        }
                    ]
                    return passed, notes
                contract_results = [
                    guidance_compatible(item, actual_contract_items)
                    for item in expected_items
                ]
                payload_results = (
                    [
                        guidance_compatible(item, actual_payload_items)
                        for item in expected_items
                    ]
                    if compare_payload
                    else []
                )
                passed = bool(
                    contract_results
                    and all(result[0] for result in contract_results)
                    and (
                        not compare_payload
                        or (
                            payload_results
                            and all(result[0] for result in payload_results)
                        )
                    )
                )
                notes = [
                    {
                        "contract_match_modes": ",".join(
                            result[1] for result in contract_results
                        ),
                        "payload_match_modes": (
                            ",".join(result[1] for result in payload_results)
                            if compare_payload
                            else "not_applicable"
                        ),
                    }
                ]
                return passed, notes
            if key == "argument_structure" and isinstance(expected, Mapping):
                expected_mapping = dict(expected)

                def contains_expected(actual: Any) -> bool:
                    return bool(
                        isinstance(actual, Mapping)
                        and all(
                            actual.get(field_name) == field_value
                            for field_name, field_value in expected_mapping.items()
                        )
                    )

                contract_passed = contains_expected(actual_contract)
                payload_passed = (
                    True
                    if not compare_payload
                    else contains_expected(actual_payload)
                )
                return contract_passed and payload_passed, [{
                    "contract_match_modes": (
                        "source_mapping_preserved_with_additive_contract"
                        if contract_passed
                        else "source_mapping_changed"
                    ),
                    "payload_match_modes": (
                        "not_applicable"
                        if not compare_payload
                        else (
                            "source_mapping_preserved_with_additive_contract"
                            if payload_passed
                            else "source_mapping_changed"
                        )
                    ),
                }]
            passed = actual_contract == expected and (
                not compare_payload or actual_payload == expected
            )
            return passed, []

        reused_claims = (
            str(state.get("claim_status") or "") == "existing_claims_reused"
        )
        for key, expected in source.items():
            actual_contract = contract_values.get(key)
            actual_payload = payload_values.get(key)
            passed, notes = field_compatible(
                key,
                expected,
                actual_contract,
                actual_payload,
                compare_payload=not reused_claims,
            )
            checks[key] = passed
            if key == "mentor_guidance":
                normalization_notes.append(
                    {"field": key, **notes[0]}
                )
        return {
            "source_values": source,
            "contract_values": contract_values,
            "m2a_payload_values": (
                {} if reused_claims else payload_values
            ),
            "payload_audit_status": (
                "not_applicable" if reused_claims else "applicable"
            ),
            "payload_audit_reason": (
                "existing_claims_reused_and_m2a_not_called"
                if reused_claims
                else ""
            ),
            "checks": checks,
            "normalization_notes": normalization_notes,
            "passed": all(checks.values()) if checks else True,
        }

    def _prepare_section(
        self,
        section: dict[str, Any],
        index: int,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        section_id = _text(section.get("section_id"), 80)
        overlay = self.overlay_paths.get(section_id)
        graph = build_canonical_asset_graph(
            material_package_path=None,
            source_ledger_path=self.shared_ledger_path,
            work_dir=self.output_dir / "graph" / section_id,
            kb_paths=self.shared_kb_paths,
            overlay_path=overlay,
        )
        inventory_graph = graph
        if self.claim_pool_enabled:
            inventory_graph = build_canonical_asset_graph(
                material_package_path=None,
                source_ledger_path=(
                    self.claim_pool_inventory_ledger_path
                    or self.shared_ledger_path
                ),
                work_dir=self.output_dir / "graph" / f"{section_id}_shared_inventory",
                kb_paths=self.shared_kb_paths,
                overlay_path=None,
            )
        inventory_records = [
            _graph_record(inventory_graph, chunk_id)
            for chunk_id in _chunk_ids(inventory_graph)
        ]
        contract = self._build_contract(section, index, sections)
        m2a_portfolio, m2a_records = self._select_m2a_input(
            section, contract, inventory_records, inventory_graph
        )
        preliminary_claim_pool_records = _select_diverse_claim_pool_records(
            inventory_records,
            preferred_chunk_ids=[
                *list(m2a_portfolio.core_chunk_ids),
                *list(m2a_portfolio.candidate_chunk_ids),
            ],
            limit=self.claim_pool_served_limit,
        )
        expansion_audit = {
            "schema_version": "research_harness.claim_pool_global_expansion.v1",
            "enabled": False,
            "reason": "strong_claim_pool_disabled",
        }
        if self.claim_pool_enabled:
            expansion_audit = _expand_section_graph_for_claim_pool(
                graph,
                inventory_graph,
                [item.get("chunk_id") for item in preliminary_claim_pool_records],
                overlay_path=overlay,
            )
        records = [_graph_record(graph, chunk_id) for chunk_id in _chunk_ids(graph)]
        bound_record_by_id = {
            str(item.get("chunk_id")): item for item in records if item.get("chunk_id")
        }
        claim_pool_records = [
            bound_record_by_id[str(item.get("chunk_id"))]
            for item in preliminary_claim_pool_records
            if str(item.get("chunk_id")) in bound_record_by_id
        ]
        m2a_records = [
            bound_record_by_id[str(item.get("chunk_id"))]
            for item in m2a_records
            if str(item.get("chunk_id")) in bound_record_by_id
        ]
        candidate_evidence_digest = build_evidence_digest(
            claim_pool_records,
            batch_size=12,
        )
        contract.candidate_material_pool = _candidate_material_pool_audit(
            section,
            inventory_records,
            served_records=m2a_records,
            portfolio=m2a_portfolio,
        )
        contract.candidate_material_pool.update({
            "served_claim_pool_chunk_ids": _unique(
                item.get("chunk_id") for item in claim_pool_records
            ),
            "served_claim_pool_paper_ids": _unique(
                item.get("paper_id") for item in claim_pool_records
            ),
            "served_claim_pool_chunk_count": len(claim_pool_records),
            "served_claim_pool_paper_count": len({
                str(item.get("paper_id"))
                for item in claim_pool_records
                if item.get("paper_id")
            }),
        })
        contract_payload = contract.to_dict()
        data = dict(section)
        previous_section = sections[index - 1] if index > 0 else {}
        following_section = sections[index + 1] if index + 1 < len(sections) else {}
        data["preceding_section_context"] = {
            "section_id": _text(previous_section.get("section_id"), 80),
            "title": _text(previous_section.get("title"), 180),
            "conclusion": _text(
                previous_section.get("conclusion")
                or previous_section.get("section_conclusion")
                or previous_section.get("central_judgment"),
                600,
            ),
            "role": _text(previous_section.get("argument_role"), 350),
        } if previous_section else {}
        data["following_section_context"] = {
            "section_id": _text(following_section.get("section_id"), 80),
            "title": _text(following_section.get("title"), 180),
            "role": _text(following_section.get("argument_role"), 350),
            "question": _text(
                following_section.get("core_question")
                or following_section.get("chapter_argument"),
                600,
            ),
        } if following_section else {}
        data.update(
            {
                "section_id": section_id,
                # M2a receives the selected view.  The complete records remain
                # in state["records"] and the shared candidate pool for later
                # on-demand retrieval.
                "candidate_text_chunks": m2a_records,
                "candidate_text_chunk_ids": [item["chunk_id"] for item in m2a_records],
                "allowed_paper_ids": _paper_ids(graph),
                "allowed_chunk_ids": [item["chunk_id"] for item in records],
                "section_argument_contract": contract_payload,
                "section_contract": contract_payload,
                "argument_input_portfolio": m2a_portfolio.to_dict(),
                "candidate_pool_ref": f"section_candidate_pool:{section_id}",
                "candidate_pool_ids": list(m2a_portfolio.candidate_chunk_ids),
                "candidate_material_pool": dict(contract.candidate_material_pool),
                "candidate_evidence_digest": candidate_evidence_digest,
                "review_scope_map": self.scope_map,
                "coverage_atlas_section": self._section_row_from_atlas(section_id),
                "review_mentor_advice": section.get("review_mentor_advice") or self._mentor(),
                "runtime_failure": dict(self.runtime_failures.get(section_id) or {}),
            }
        )
        model_records = claim_pool_records if self.claim_pool_enabled else m2a_records
        m2a_input_payload = ClaimDecomposer(real_llm=False)._build_input_payload(
            self._m2a_section_view(
                data,
                model_records,
                compact_context=section_id in self._m2a_compact_context,
            )
        )
        return {
            "section": data,
            "graph": graph,
            "records": records,
            "m2a_records": m2a_records,
            "m2a_portfolio": m2a_portfolio,
            "claim_pool_records": claim_pool_records,
            "candidate_evidence_digest": candidate_evidence_digest,
            "claim_pool_runtime_audit": {},
            "claim_pool_global_expansion": expansion_audit,
            "m2a_input_payload": m2a_input_payload,
            "context_source_values": self._context_source_values(section),
            "contract": contract,
            "claims": [],
            "claim_status": "not_run",
            "claim_errors": [],
            "llm_audit": {},
            "bindings": {},
            "bundle": {},
            "status": "needs_more_literature",
            "section_outcome": "needs_more_literature",
            "adaptation_actions": [],
            "declared_limits": [],
            "open_questions": [],
            "merge_recommendation": _merge_recommendation_from_section(section),
            "overlay_path": overlay,
            "validated_section_sources": _section_sources_from_graph(
                graph, section_id
            ),
            "active_kb_paths": list(self.shared_kb_paths),
            "runtime_failure": dict(self.runtime_failures.get(section_id) or {}),
            "ownership_refresh_audit": [],
        }

    def _refresh_state_from_coverage_patch(
        self,
        state: dict[str, Any],
        patch: dict[str, Any],
    ) -> None:
        """Refresh only one section from a Phase-2 material bundle.

        A Phase-2 worker may write a new section ledger and a supplemental
        SQLite.  Rebuilding the affected canonical graph here makes the
        returned material visible to M2a/M2b without copying a database for
        every section.  A patch with no material paths is intentionally a
        no-op and remains auditable as an unfulfilled request.
        """

        section_id = str(state["section"]["section_id"])
        ledger_raw = patch.get("source_ledger_path") or ""
        ledger_path = Path(ledger_raw) if ledger_raw else self.shared_ledger_path
        kb_paths = [
            Path(value) for value in (
                state.get("active_kb_paths") or self.shared_kb_paths
            )
            if Path(value).exists()
        ]
        for shared in self.shared_kb_paths:
            if shared.exists() and shared not in kb_paths:
                kb_paths.append(shared)
        for raw in (patch.get("kb_sqlite"), patch.get("staging_kb_sqlite")):
            if raw:
                candidate = Path(raw)
                if candidate.exists() and candidate not in kb_paths:
                    kb_paths.append(candidate)
        if not ledger_path or not ledger_path.exists() or not kb_paths:
            return

        ledger = _read_json(ledger_path)
        sources = [
            item for item in ledger.get("sources") or []
            if isinstance(item, dict) and str(item.get("paper_id") or "").strip()
        ]
        ownership = _merge_and_validate_section_sources(
            section_id=section_id,
            previous_sources=state.get("validated_section_sources") or [],
            incoming_sources=sources,
            kb_paths=kb_paths,
        )
        validated_sources = list(ownership.get("sources") or [])
        if not validated_sources:
            state.setdefault("ownership_refresh_audit", []).append(ownership)
            return
        section_refresh_dir = self.output_dir / "coverage_requests" / section_id
        validated_ledger_path = (
            section_refresh_dir / "VALIDATED_SECTION_SOURCE_LEDGER.json"
        )
        atomic_write_json(validated_ledger_path, {
            "schema_version": "research_harness.phase3_validated_section_source_ledger.v1",
            "section_id": section_id,
            "sources": validated_sources,
            "ownership_validation": {
                key: value for key, value in ownership.items()
                if key != "sources"
            },
        })
        overlay_path = section_refresh_dir / "SECTION_ASSET_OVERLAY.json"
        build_section_asset_overlay(
            section_id=section_id,
            sources=validated_sources,
            shared_kb_paths=kb_paths,
            output_path=overlay_path,
        )
        graph = build_canonical_asset_graph(
            material_package_path=None,
            source_ledger_path=validated_ledger_path,
            work_dir=self.output_dir / "graph" / section_id / "refreshed",
            kb_paths=kb_paths,
            overlay_path=overlay_path,
        )
        records = [_graph_record(graph, chunk_id) for chunk_id in _chunk_ids(graph)]
        previous_chunk_ids = {
            str(item.get("chunk_id"))
            for item in state.get("records") or []
            if isinstance(item, dict) and item.get("chunk_id")
        }
        fresh_chunk_ids = [
            item["chunk_id"] for item in records
            if item["chunk_id"] not in previous_chunk_ids
        ]
        fresh_audit = _fresh_component_audit(
            state.get("claims") or state.get("section", {}).get("claims") or [],
            {item["chunk_id"]: item for item in records},
            fresh_chunk_ids,
        )
        judge_callable = (
            self.fresh_evidence_semantic_judge
            if self.enable_fresh_evidence_semantic_judge
            else None
        )
        fresh_audit, semantic_judge_telemetry = apply_semantic_judge_batch(
            fresh_audit,
            judge_callable,
            section_id=section_id,
        )
        if fresh_audit:
            claims_by_id = {
                str(item.get("claim_id")): item
                for item in state.get("claims") or []
                if isinstance(item, dict) and item.get("claim_id")
            }
            audits_by_claim: dict[str, list[dict[str, Any]]] = {}
            for audit in fresh_audit:
                audits_by_claim.setdefault(str(audit.get("claim_id") or ""), []).append(audit)
            for claim_id, claim_audits in audits_by_claim.items():
                claim = claims_by_id.get(claim_id)
                if not claim:
                    continue
                _reconcile_fresh_claim_evidence(claim, claim_audits)
                claim.setdefault("fresh_component_audit", []).extend(claim_audits)
            state["claims"] = list(claims_by_id.values())
            state["section"]["claims"] = state["claims"]
        state["fresh_chunk_rebinding"] = {
            "fresh_chunk_ids": list(fresh_chunk_ids),
            "eligible_fresh_chunk_ids": sorted({
                str(chunk_id)
                for audit in fresh_audit
                for chunk_id in audit.get("chunk_ids") or []
            }),
            "inspected_chunk_count": len(fresh_chunk_ids),
            "component_audit": fresh_audit,
            "semantic_judge": semantic_judge_telemetry,
            "scientific_components_closed": [
                {
                    "claim_id": item.get("claim_id"),
                    "requested_component": item.get("requested_component"),
                    "supported_component": item.get("supported_component"),
                    "chunk_ids": item.get("chunk_ids", []),
                }
                for item in fresh_audit
                if _component_support_state(item.get("status")) == "supported"
            ],
            "scientific_components_supported": [
                {
                    "claim_id": item.get("claim_id"),
                    "requested_component": item.get("requested_component"),
                    "support_state": _component_support_state(item.get("status")),
                    "supported_component": item.get("supported_component"),
                    "residual_components": list(item.get("residual_components") or []),
                    "chunk_ids": item.get("chunk_ids", []),
                }
                for item in fresh_audit
                if _component_support_state(item.get("status"))
                in {"supported", "partially_supported"}
            ],
        }
        inventory_graph = graph
        if self.claim_pool_enabled:
            inventory_graph = build_canonical_asset_graph(
                material_package_path=None,
                source_ledger_path=(
                    self.claim_pool_inventory_ledger_path
                    or validated_ledger_path
                ),
                work_dir=self.output_dir / "graph" / section_id / "refreshed_shared_inventory",
                kb_paths=kb_paths,
                overlay_path=None,
            )
        inventory_records = [
            _graph_record(inventory_graph, chunk_id)
            for chunk_id in _chunk_ids(inventory_graph)
        ]
        m2a_portfolio, m2a_records = self._select_m2a_input(
            state["section"], state["contract"], inventory_records, inventory_graph
        )
        preliminary_claim_pool_records = _select_diverse_claim_pool_records(
            inventory_records,
            preferred_chunk_ids=[
                *list(m2a_portfolio.core_chunk_ids),
                *list(m2a_portfolio.candidate_chunk_ids),
            ],
            limit=self.claim_pool_served_limit,
        )
        expansion_audit = {
            "schema_version": "research_harness.claim_pool_global_expansion.v1",
            "enabled": False,
            "reason": "strong_claim_pool_disabled",
        }
        if self.claim_pool_enabled:
            expansion_audit = _expand_section_graph_for_claim_pool(
                graph,
                inventory_graph,
                [item.get("chunk_id") for item in preliminary_claim_pool_records],
                overlay_path=overlay_path,
            )
        records = [_graph_record(graph, chunk_id) for chunk_id in _chunk_ids(graph)]
        bound_record_by_id = {
            str(item.get("chunk_id")): item for item in records if item.get("chunk_id")
        }
        claim_pool_records = [
            bound_record_by_id[str(item.get("chunk_id"))]
            for item in preliminary_claim_pool_records
            if str(item.get("chunk_id")) in bound_record_by_id
        ]
        m2a_records = [
            bound_record_by_id[str(item.get("chunk_id"))]
            for item in m2a_records
            if str(item.get("chunk_id")) in bound_record_by_id
        ]
        candidate_evidence_digest = build_evidence_digest(
            claim_pool_records,
            batch_size=12,
        )
        state["contract"].candidate_material_pool = _candidate_material_pool_audit(
            state["section"],
            inventory_records,
            served_records=m2a_records,
            portfolio=m2a_portfolio,
        )
        state["contract"].candidate_material_pool.update({
            "served_claim_pool_chunk_ids": _unique(
                item.get("chunk_id") for item in claim_pool_records
            ),
            "served_claim_pool_paper_ids": _unique(
                item.get("paper_id") for item in claim_pool_records
            ),
            "served_claim_pool_chunk_count": len(claim_pool_records),
            "served_claim_pool_paper_count": len({
                str(item.get("paper_id"))
                for item in claim_pool_records
                if item.get("paper_id")
            }),
        })
        state["graph"] = graph
        state["records"] = records
        state["m2a_records"] = m2a_records
        state["m2a_portfolio"] = m2a_portfolio
        state["claim_pool_records"] = claim_pool_records
        state["candidate_evidence_digest"] = candidate_evidence_digest
        state["claim_pool_global_expansion"] = expansion_audit
        state["overlay_path"] = overlay_path
        state["validated_section_sources"] = validated_sources
        state["active_kb_paths"] = kb_paths
        state.setdefault("ownership_refresh_audit", []).append(ownership)
        self.overlay_paths[section_id] = overlay_path
        state["section"]["candidate_text_chunks"] = m2a_records
        state["section"]["candidate_text_chunk_ids"] = [item["chunk_id"] for item in m2a_records]
        state["section"]["argument_input_portfolio"] = m2a_portfolio.to_dict()
        state["section"]["candidate_pool_ids"] = list(m2a_portfolio.candidate_chunk_ids)
        state["section"]["candidate_material_pool"] = dict(
            state["contract"].candidate_material_pool
        )
        state["section"]["candidate_evidence_digest"] = candidate_evidence_digest

        state["section"]["section_argument_contract"] = state["contract"].to_dict()
        state["section"]["section_contract"] = state["contract"].to_dict()
        state["section"]["allowed_paper_ids"] = _paper_ids(graph)
        state["section"]["allowed_chunk_ids"] = _chunk_ids(graph)
        # Keep the serialized M2a handoff synchronized with the refreshed
        # graph.  Without this, a Phase-2 feedback pass could correctly add
        # new chunks to the canonical graph while the next cached-claim pass
        # still exposed the pre-retrieval payload to audit and downstream
        # consumers.
        model_records = claim_pool_records if self.claim_pool_enabled else m2a_records
        state["m2a_input_payload"] = ClaimDecomposer(real_llm=False)._build_input_payload(
            self._m2a_section_view(
                state["section"],
                model_records,
                compact_context=section_id in self._m2a_compact_context,
            )
        )

    @staticmethod
    def _state_evidence_fingerprint(state: dict[str, Any]) -> str:
        sources = [
            {
                "paper_id": str(item.get("paper_id") or ""),
                "canonical_chunk_ids": sorted(
                    str(value)
                    for value in item.get("canonical_chunk_ids") or []
                ),
                "scope_fit": str(item.get("scope_fit") or ""),
                "content_depth": str(item.get("content_depth") or ""),
                "use_permission": str(item.get("use_permission") or ""),
                "acquisition_status": str(item.get("acquisition_status") or ""),
                "materialization_route": str(
                    item.get("materialization_route") or ""
                ),
            }
            for item in state.get("validated_section_sources") or []
            if isinstance(item, dict) and item.get("paper_id")
        ]
        chunks = [
            {
                "chunk_id": str(item.get("chunk_id") or ""),
                "paper_id": str(item.get("paper_id") or ""),
                "content_depth": str(item.get("content_depth") or ""),
                "use_permission": str(item.get("use_permission") or ""),
                "context_complete": bool(item.get("context_complete")),
                "source_kind": str(item.get("source_kind") or ""),
                "permission_ceiling": str(item.get("permission_ceiling") or ""),
                "text_sha256": hashlib.sha256(
                    str(item.get("text") or "").encode("utf-8")
                ).hexdigest(),
            }
            for item in state.get("records") or []
            if isinstance(item, dict) and item.get("chunk_id")
        ]
        rebinding = state.get("fresh_chunk_rebinding") or {}
        closed = rebinding.get("scientific_components_closed") or []
        return hashlib.sha256(
            json.dumps(
                {
                    "validated_sources": sorted(
                        sources,
                        key=lambda item: json.dumps(
                            item, sort_keys=True, default=str
                        ),
                    ),
                    "record_chunks": sorted(
                        chunks,
                        key=lambda item: json.dumps(
                            item, sort_keys=True, default=str
                        ),
                    ),
                    "scientific_components_closed": json.dumps(
                        closed,
                        sort_keys=True,
                        default=str,
                    ),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    def _decompose_claims(self, state: dict[str, Any]) -> None:
        section = state["section"]

        def normalize_claim(claim: dict[str, Any]) -> dict[str, Any]:
            fit = str(claim.get("section_fit") or "").casefold()
            importance = normalize_importance(claim)
            if fit in {"boundary", "off_scope"} and importance == "load_bearing":
                importance = "supporting"
            claim["importance"] = importance
            claim["load_bearing"] = importance == "load_bearing"
            if claim.get("supported_rewrite") and not claim.get("original_statement"):
                claim["original_statement"] = claim.get("statement", "")
            return claim

        existing = [
            _as_claim_dict(item)
            for item in section.get("claims") or []
            if _is_real_claim(_as_claim_dict(item))
        ]
        if existing:
            for claim in existing:
                claim.setdefault("section_id", section["section_id"])
                normalize_claim(claim)
            state["claims"] = existing
            state["claim_status"] = "existing_claims_reused"
            self._update_claim_pool_runtime_audit(state)
            return
        if not self.real_llm_claims:
            state["claims"] = _seed_open_question_claims(section, state["contract"])
            state["claim_status"] = (
                "open_questions_seeded_from_argument_tasks"
                if state["claims"]
                else "deferred_offline_no_valid_claims"
            )
            self._update_claim_pool_runtime_audit(state)
            return
        decomposer: ClaimDecomposer | None = None
        model_view: dict[str, Any] = {}
        try:
            decomposer = ClaimDecomposer(
                model_tier=self.claim_model_tier,
                real_llm=True,
                claim_pool_enabled=self.claim_pool_enabled,
                claim_pool_batch_size=12,
                claim_pool_target_range=self.claim_pool_target_range,
                final_claim_selection_limit=self.claim_pool_shortlist_limit,
                verify_candidate_pool_claims=False,
                claim_pool_progress_path=(
                    self.output_dir / "CLAIM_POOL_PROGRESS.jsonl"
                ),
            )
            model_records = (
                state.get("claim_pool_records")
                if self.claim_pool_enabled
                else state.get("m2a_records")
            ) or section.get("candidate_text_chunks") or []
            model_view = self._m2a_section_view(
                section,
                model_records,
                compact_context=(
                    str(section.get("section_id") or "")
                    in self._m2a_compact_context
                ),
            )
            if self.claim_pool_enabled:
                model_view["candidate_evidence_digest"] = dict(
                    state.get("candidate_evidence_digest") or {}
                )
            claims = decomposer.decompose_section(model_view)
            if decomposer.last_input_payload:
                state["m2a_input_payload"] = dict(decomposer.last_input_payload)
            if isinstance(model_view.get("candidate_claim_pool"), dict):
                state["section"]["candidate_claim_pool"] = dict(
                    model_view["candidate_claim_pool"]
                )
            if isinstance(model_view.get("candidate_claim_pool_audit"), dict):
                state["section"]["candidate_claim_pool_audit"] = dict(
                    model_view["candidate_claim_pool_audit"]
                )
            if isinstance(
                model_view.get("candidate_claim_pool_shortlist_audit"), dict
            ):
                state["section"]["candidate_claim_pool_shortlist_audit"] = dict(
                    model_view["candidate_claim_pool_shortlist_audit"]
                )
            normalized = []
            for item in claims:
                claim = _as_claim_dict(item)
                if _is_real_claim(claim):
                    claim.setdefault("section_id", section["section_id"])
                    normalize_claim(claim)
                    normalized.append(claim)
            state["claims"] = normalized
            state["llm_audit"] = dict(decomposer.last_audit)
            if normalized:
                state["claim_status"] = (
                    "real_llm_claim_pool_decomposed"
                    if self.claim_pool_enabled
                    else "real_llm_decomposed"
                )
            else:
                # An empty/invalid real response is a model/parser failure,
                # not a scientific literature gap.  Do not seed open
                # questions here: that would trigger an expensive coverage
                # request for a failure that occurred before claim formation.
                state["claims"] = []
                state["claim_status"] = "real_llm_parse_failure"
                self._record_phase3_runtime_failure(
                    state,
                    component="M2a",
                    error_type="parse_failure",
                    reason="Real M2a returned no valid claims.",
                )
        except Exception as exc:
            state["claim_status"] = "real_llm_runtime_failure"
            state["claim_errors"].append(f"{type(exc).__name__}: {exc}")
            state["claims"] = []
            self._record_phase3_runtime_failure(
                state,
                component="M2a",
                error_type=type(exc).__name__,
                reason=str(exc),
            )
        if decomposer is not None:
            state["llm_audit"] = dict(decomposer.last_audit)
            if decomposer.last_input_payload:
                state["m2a_input_payload"] = dict(decomposer.last_input_payload)
        if isinstance(model_view.get("candidate_claim_pool"), dict):
            state["section"]["candidate_claim_pool"] = dict(
                model_view["candidate_claim_pool"]
            )
        if isinstance(model_view.get("candidate_claim_pool_audit"), dict):
            state["section"]["candidate_claim_pool_audit"] = dict(
                model_view["candidate_claim_pool_audit"]
            )
        if isinstance(model_view.get("candidate_claim_pool_shortlist_audit"), dict):
            state["section"]["candidate_claim_pool_shortlist_audit"] = dict(
                model_view["candidate_claim_pool_shortlist_audit"]
            )
        self._update_claim_pool_runtime_audit(state)

    def _update_claim_pool_runtime_audit(self, state: dict[str, Any]) -> None:
        """Persist what the model actually read, independent of inventory size."""

        llm_audit = dict(state.get("llm_audit") or {})
        section = state.get("section") or {}
        pool = section.get("candidate_claim_pool") or {}
        pool = dict(pool) if isinstance(pool, Mapping) else {}
        pool_audit = (
            llm_audit.get("candidate_claim_pool_audit")
            or section.get("candidate_claim_pool_audit")
            or pool.get("audit")
            or {}
        )
        pool_audit = dict(pool_audit) if isinstance(pool_audit, Mapping) else {}
        shortlist = section.get("candidate_claim_pool_shortlist_audit") or {}
        shortlist = dict(shortlist) if isinstance(shortlist, Mapping) else {}
        parsed_batches = [
            dict(item)
            for item in pool_audit.get("batches") or []
            if isinstance(item, Mapping)
        ]
        planned_batches = [
            dict(item)
            for item in (
                (state.get("candidate_evidence_digest") or {}).get("batches")
                or []
            )
            if isinstance(item, Mapping)
        ]
        planned_by_id = {
            str(item.get("batch_id") or ""): item
            for item in planned_batches
            if str(item.get("batch_id") or "")
        }
        attempts = [
            dict(item)
            for item in pool_audit.get("attempts") or []
            if isinstance(item, Mapping)
        ]
        successful_call_batch_ids = _unique(
            item.get("batch_id")
            for item in attempts
            if not bool(item.get("failed")) and item.get("batch_id")
        )
        parsed_batch_ids = _unique(
            item.get("batch_id") for item in parsed_batches if item.get("batch_id")
        )
        productive_batch_ids = _unique(
            item.get("batch_id")
            for item in parsed_batches
            if int(item.get("claim_count") or 0) > 0 and item.get("batch_id")
        )
        submitted_to_successful_calls_ids = _unique(
            chunk_id
            for batch_id in successful_call_batch_ids
            for chunk_id in (planned_by_id.get(str(batch_id), {}).get("chunk_ids") or [])
        )
        parsed_batch_chunk_ids = _unique(
            chunk_id
            for batch in parsed_batches
            for chunk_id in batch.get("chunk_ids") or []
        )
        candidate_claims = [
            dict(item)
            for item in pool.get("claims") or []
            if isinstance(item, Mapping)
        ]
        candidate_cited_chunk_ids = _unique(
            chunk_id
            for claim in candidate_claims
            for chunk_id in _declared_claim_support_ids(claim)
        )
        failed_batch_ids = _unique(
            item.get("batch_id")
            for item in attempts
            if bool(item.get("failed")) and item.get("batch_id")
        )
        unpresented_batch_ids = [
            batch_id
            for batch_id in planned_by_id
            if batch_id not in set(successful_call_batch_ids)
        ]
        material_pool = (
            state.get("contract").candidate_material_pool
            if state.get("contract") else {}
        )
        inventory_count = int(
            (material_pool or {}).get("inventory_chunk_count")
            or len(state.get("records") or [])
        )
        served_claim_pool_count = len(state.get("claim_pool_records") or [])
        pool_expected = bool(
            self.real_llm_claims
            and self.claim_pool_enabled
            and state.get("claim_status") != "existing_claims_reused"
        )
        legacy_used = bool(llm_audit.get("legacy_single_call_used"))
        violations: list[str] = []
        if pool_expected and legacy_used:
            violations.append("strong_pool_fell_back_to_legacy_single_call")
        if (
            pool_expected
            and served_claim_pool_count
            and not submitted_to_successful_calls_ids
        ):
            violations.append("strong_pool_had_material_but_model_read_zero_chunks")
        runtime_audit = {
            "schema_version": "research_harness.phase3_claim_pool_runtime_audit.v1",
            "claim_pool_enabled": self.claim_pool_enabled,
            "pool_expected": pool_expected,
            "inventory_chunk_count": inventory_count,
            "served_claim_pool_chunk_count": served_claim_pool_count,
            "served_claim_pool_paper_count": len({
                str(item.get("paper_id"))
                for item in state.get("claim_pool_records") or []
                if isinstance(item, Mapping) and item.get("paper_id")
            }),
            "claim_pool_batch_count": int(pool_audit.get("batch_count") or 0),
            "planned_batch_count": len(planned_batches),
            "attempted_batch_count": len(attempts),
            "successful_call_batch_count": len(successful_call_batch_ids),
            "successful_call_batch_ids": successful_call_batch_ids,
            "parsed_batch_count": len(parsed_batch_ids),
            "completed_batch_count": len(parsed_batch_ids),
            "parsed_batch_ids": parsed_batch_ids,
            "productive_batch_count": len(productive_batch_ids),
            "productive_batch_ids": productive_batch_ids,
            "failed_batch_ids": failed_batch_ids,
            "unpresented_batch_ids": unpresented_batch_ids,
            "chunks_submitted_to_successful_calls_count": len(
                submitted_to_successful_calls_ids
            ),
            "chunks_submitted_to_successful_calls_ids": (
                submitted_to_successful_calls_ids
            ),
            "chunks_in_parsed_batches_count": len(parsed_batch_chunk_ids),
            "chunks_in_parsed_batches_ids": parsed_batch_chunk_ids,
            "chunks_cited_by_candidate_claims_count": len(
                candidate_cited_chunk_ids
            ),
            "chunks_cited_by_candidate_claims_ids": candidate_cited_chunk_ids,
            # Backward-compatible alias with an explicit, honest definition.
            "actual_model_read_chunk_count": len(
                submitted_to_successful_calls_ids
            ),
            "actual_model_read_chunk_ids": submitted_to_successful_calls_ids,
            "actual_model_read_definition": (
                "unique chunks presented in provider-successful batch calls; "
                "the model's internal attention cannot be observed"
            ),
            "candidate_claim_count": int(
                pool_audit.get("claims_after_merge")
                or len(pool.get("claims") or [])
            ),
            "selected_claim_count": int(
                shortlist.get("selected_count")
                or llm_audit.get("claim_pool_claims_selected")
                or 0
            ),
            "legacy_single_call_used": legacy_used,
            "authorable_claim_count": len(state.get("authorable_claims") or []),
            "evidence_gap_claim_count": len(state.get("evidence_gap_claims") or []),
            "integrity_violations": violations,
            "integrity_passed": not violations,
            "global_expansion": dict(state.get("claim_pool_global_expansion") or {}),
        }
        state["claim_pool_runtime_audit"] = runtime_audit
        if isinstance(section, dict):
            section["claim_pool_runtime_audit"] = dict(runtime_audit)

    @staticmethod
    def _record_phase3_runtime_failure(
        state: dict[str, Any],
        *,
        component: str,
        error_type: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record an M2 failure without turning it into a scientific gap."""
        existing = dict(state.get("runtime_failure") or {})
        failures = list(existing.get("failures") or [])
        if existing and not failures:
            failures.append({
                "phase": existing.get("phase", "coverage"),
                "component": existing.get("component", "coverage"),
                "error_type": existing.get("error_type", ""),
                "reason": existing.get("reason", ""),
                "source": existing.get("source", ""),
            })
        failure = {
            "phase": "phase3",
            "component": component,
            "error_type": error_type,
            "reason": str(reason or "phase-3 runtime failure"),
            "source": "phase3_argument_orchestration",
            "scientific_gap": False,
        }
        failures.append(failure)
        merged = {
            **existing,
            "section_id": str(state.get("section", {}).get("section_id") or ""),
            "kind": "runtime_failure",
            "phase": "phase3",
            "component": component,
            "error_type": error_type,
            "reason": failure["reason"],
            "source": "phase3_argument_orchestration",
            "scientific_gap": False,
            "failures": failures,
        }
        state["runtime_failure"] = merged
        state["declared_limits"] = _unique(
            list(state.get("declared_limits") or [])
            + ["runtime_failure:" + failure["reason"]]
        )
        return merged

    @staticmethod
    def _close_section_after_runtime_failure(state: dict[str, Any]) -> None:
        """Keep failed sections out of R4 while preserving other sections."""
        state["status"] = "needs_more_literature"
        state["section_outcome"] = "needs_more_literature"
        bindings = state.get("bindings")
        if isinstance(bindings, dict):
            bindings["status"] = "needs_more_literature"
            bindings["section_outcome"] = "needs_more_literature"
        bundle = state.get("bundle")
        if isinstance(bundle, dict):
            bundle["status"] = "needs_more_literature"
            bundle["readiness_status"] = "needs_more_literature"
            bundle["section_outcome"] = "needs_more_literature"
            bundle["r4_handoff_allowed"] = False
            bundle["runtime_failure"] = dict(state.get("runtime_failure") or {})

    def _project_claims_for_global_dag(
        self,
        states: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
        """Project chapter shortlists into a bounded global-DAG view.

        The global relation pass sees already selected claims, never the
        central material inventory.  Per-section quotas preserve coverage;
        unsupported/open claims remain in the full Phase-3 handoff but are not
        spent as relation-model context.
        """

        projected: list[dict[str, Any]] = []
        omitted: list[str] = []
        section_stats: dict[str, Any] = {}
        for state in states:
            section = state.get("section") or {}
            section_id = str(section.get("section_id") or "")
            claims = [
                dict(claim)
                for claim in (state.get("claims") or [])
                if isinstance(claim, Mapping) and claim.get("claim_id")
            ]
            eligible = [claim for claim in claims if _claim_can_enter_dag(claim)]
            ineligible = [
                str(claim.get("claim_id"))
                for claim in claims
                if not _claim_can_enter_dag(claim)
            ]
            # Keep load-bearing and supported claims first, then fill with
            # qualified claims. All ties are deterministic by claim ID.
            eligible.sort(
                key=lambda claim: (
                    0 if claim.get("load_bearing") else 1,
                    0
                    if str(
                        claim.get("support_classification")
                        or claim.get("claim_classification")
                        or ""
                    )
                    == "supported"
                    else 1,
                    -float(claim.get("saturation_score") or 0.0),
                    str(claim.get("claim_id") or ""),
                )
            )
            chosen = eligible[: self.dag_claims_per_section]
            chosen_ids = {str(claim.get("claim_id")) for claim in chosen}
            omitted.extend(
                str(claim.get("claim_id"))
                for claim in claims
                if str(claim.get("claim_id")) not in chosen_ids
            )
            for claim in chosen:
                claim["dag_projected"] = True
                projected.append(claim)
            omitted.extend(ineligible)
            section_stats[section_id] = {
                "input_claim_count": len(claims),
                "eligible_claim_count": len(eligible),
                "projected_claim_count": len(chosen),
                "omitted_claim_count": max(0, len(claims) - len(chosen)),
                "projected_claim_ids": [
                    str(claim.get("claim_id")) for claim in chosen
                ],
            }
        # A total cap protects a topic with many chapters. Keep each chapter's
        # first projected claims and trim only the deterministic tail.
        if len(projected) > self.dag_total_claims:
            overflow = projected[self.dag_total_claims :]
            projected = projected[: self.dag_total_claims]
            omitted.extend(
                str(claim.get("claim_id")) for claim in overflow
            )
        omitted_ids = list(dict.fromkeys(item for item in omitted if item))
        return projected, omitted_ids, {
            "schema_version": "research_harness.global_dag_projection.v1",
            "claims_per_section": self.dag_claims_per_section,
            "total_claims": self.dag_total_claims,
            "projected_claim_count": len(projected),
            "omitted_claim_count": len(omitted_ids),
            "sections": section_stats,
            "input_role": "chapter_shortlist_only",
            "central_inventory_included": False,
        }

    def _build_claim_graph(self, states: list[dict[str, Any]]) -> dict[str, Any]:
        all_claims: list[dict[str, Any]] = []
        section_meta: dict[str, dict[str, Any]] = {}
        section_order: list[str] = []
        for state in states:
            section = state["section"]
            section_id = section["section_id"]
            section_order.append(section_id)
            section_meta[section_id] = {
                "title": section.get("title", section_id),
                "argument_role": section.get("argument_role", ""),
            }
            all_claims.extend(state["claims"])
        if not all_claims:
            return {
                "schema_version": "research_harness.claim_graph.v1",
                "status": "deferred_no_valid_claims",
                "nodes": [],
                "claims": [],
                "edges": [],
                "relation_types": list(ARGUMENT_RELATION_TYPES),
                "validation_errors": ["No real section-level claims were available."],
            }
        dag_claims, omitted_claim_ids, projection = (
            self._project_claims_for_global_dag(states)
        )
        try:
            builder = ArgumentDAGBuilder(
                real_llm=self.real_llm_dag,
                model_tier=self.dag_model_tier,
                global_critic=self.real_llm_dag,
                max_layer4_candidates=self.max_dag_candidates,
            )
            dag = builder.build(
                dag_claims,
                section_order,
                section_meta=section_meta,
            )
            payload = dag.to_dict()
            # Keep omitted chapter claims available to downstream audits even
            # though they were intentionally excluded from relation-model
            # context. They carry no DAG edges.
            projected_ids = {
                str(claim.get("claim_id")) for claim in dag_claims
            }
            omitted_claims = [
                {**claim, "dag_projected": False}
                for claim in all_claims
                if str(claim.get("claim_id")) not in projected_ids
            ]
            payload["nodes"] = [
                *list(payload.get("nodes") or []),
                *omitted_claims,
            ]
            payload["claims"] = [
                *list(payload.get("claims") or []),
                *omitted_claims,
            ]
            payload.update(
                {
                    "schema_version": "research_harness.claim_graph.v1",
                    "status": "built",
                    "relation_types": list(ARGUMENT_RELATION_TYPES),
                    "validation_errors": dag.validate(),
                    "real_llm": self.real_llm_dag,
                    "global_dag_projection": projection,
                    "omitted_claim_ids": omitted_claim_ids,
                }
            )
            return payload
        except Exception as exc:
            for state in states:
                self._record_phase3_runtime_failure(
                    state,
                    component="M2b",
                    error_type=type(exc).__name__,
                    reason=str(exc),
                )
                self._close_section_after_runtime_failure(state)
            return {
                "schema_version": "research_harness.claim_graph.v1",
                "status": "failed_closed",
                "nodes": all_claims,
                "claims": all_claims,
                "edges": [],
                "relation_types": list(ARGUMENT_RELATION_TYPES),
                "validation_errors": [f"{type(exc).__name__}: {exc}"],
                "global_dag_projection": projection,
                "omitted_claim_ids": omitted_claim_ids,
            }

    def _bind_section(self, state: dict[str, Any], migrated_edges: list[dict[str, Any]]) -> None:
        section = state["section"]
        graph: CanonicalAssetGraph = state["graph"]
        records = state["records"]
        records_by_id = {item["chunk_id"]: item for item in records}
        # Re-run the pure adaptation layer after every coverage refresh.  This
        # keeps the claim object, binding, and later R3/R4 views on the same
        # classification while preserving all declared/rejected provenance.
        state["claims"] = [
            adapt_claim_for_partial_coverage(
                claim,
                records_by_id,
                section_id=str(section.get("section_id") or ""),
            )
            for claim in state.get("claims") or []
            if isinstance(claim, Mapping)
        ]
        state["section"]["claims"] = list(state["claims"])
        state["adaptation_actions"] = _unique(
            claim.get("adaptation_action")
            for claim in state["claims"]
            if claim.get("adaptation_action")
        )
        state["open_questions"] = [
            {
                "claim_id": str(claim.get("claim_id") or ""),
                "statement": _text(claim.get("effective_statement") or claim.get("statement"), 1800),
                "importance": normalize_importance(claim),
            }
            for claim in state["claims"]
            if claim.get("support_classification") == "open_question"
        ]
        (
            state["authorable_claims"],
            state["evidence_gap_claims"],
        ) = _partition_claim_lanes(state["claims"])
        state["section"]["claim_lanes"] = {
            "authorable_claim_ids": [
                str(claim.get("claim_id") or "")
                for claim in state["authorable_claims"]
                if claim.get("claim_id")
            ],
            "evidence_gap_claim_ids": [
                str(claim.get("claim_id") or "")
                for claim in state["evidence_gap_claims"]
                if claim.get("claim_id")
            ],
            "policy": (
                "supported and qualified claims may enter cautious writing; "
                "open questions remain evidence-gap records"
            ),
        }
        self._update_claim_pool_runtime_audit(state)
        local_edges = _section_relation_edges(migrated_edges, graph)
        # One section-level portfolio is shared by all claims.  This prevents
        # each claim from receiving a fresh top-ranked slice of the same
        # paper and keeps the candidate inventory out of every claim object.
        portfolio = select_evidence_portfolio(
            section={**section, "claims": state["claims"]},
            candidates=records,
            claims=state["claims"],
            relation_edges=local_edges,
            allowed_paper_ids=graph.papers,
            allowed_chunk_ids=graph.chunks,
            max_core_chunks=self.authoring_core_chunk_limit,
            max_core_chunks_per_paper=2,
        )
        state["portfolio"] = portfolio
        bindings: dict[str, Any] = {}
        for claim in state["claims"]:
            claim_id = _text(claim.get("claim_id"), 120)
            if not claim_id:
                continue
            permission_status, factual_ids, contextual_ids = _claim_permission_status(
                claim, records_by_id
            )
            role_ids = _claim_role_chunk_ids(claim)
            supporting_ids = [
                item for item in _unique(role_ids.get("positive_support") or [])
                if item in records_by_id
                and evidence_ceiling(records_by_id[item])[0] in {FACTUAL, QUALIFIED}
            ]
            # The selector's core portfolio is a ranked candidate set, not a
            # proof that the claim is supported.  Treating a selected
            # candidate as evidence silently promoted load-bearing claims to
            # ready in the old path.  Only explicitly attached, permission-
            # eligible IDs count as bound material; the portfolio remains
            # available for author review and gap requests.
            missing = not supporting_ids
            importance = normalize_importance(claim)
            if str(claim.get("section_fit") or "").casefold() in {"boundary", "off_scope"}:
                importance = "supporting" if importance == "load_bearing" else importance
            claim["importance"] = importance
            claim["load_bearing"] = importance == "load_bearing"
            load_bearing = importance == "load_bearing"
            classification = str(
                claim.get("support_classification")
                or claim.get("claim_classification")
                or ("open_question" if missing else "supported")
            )
            if classification == "open_question" or missing:
                write_status = "needs_more_literature" if load_bearing else "write_with_declared_gap"
            elif classification == "qualified" or permission_status == "qualified_only":
                write_status = "write_with_qualified_support"
            else:
                write_status = "bound"
            adaptation_action = _text(claim.get("adaptation_action"), 120)
            adaptation_recommendation = dict(claim.get("adaptation_recommendation") or {})
            if (
                load_bearing
                and classification == "open_question"
                and state.get("merge_recommendation")
            ):
                adaptation_action = "merge_recommendation"
                adaptation_recommendation = {
                    **dict(state.get("merge_recommendation") or {}),
                    "action": "merge_recommendation",
                    "bounded": True,
                    "claim_id": _text(claim.get("claim_id"), 120),
                }
            core_for_claim = [
                chunk_id for chunk_id in portfolio.core_chunk_ids
                if chunk_id in supporting_ids
            ]
            _used_fallback_core = not bool(core_for_claim)
            _fallback_contextual_ids: list = []
            if _used_fallback_core:
                # Fallback chunks are contextual support only — do NOT promote
                # them into core_for_claim (that silently binds weak support).
                logger.warning(
                    "phase3 fallback: no core chunks matched claim %s; "
                    "routing first4 portfolio chunks to contextual support only.",
                    _text(claim.get("claim_id"), 120) or "?",
                )
                _fallback_contextual_ids = list(portfolio.core_chunk_ids[:4])
                # core_for_claim stays [] — intentional
            effective_statement = _text(
                claim.get("effective_statement")
                or claim.get("supported_rewrite")
                or claim.get("statement"),
                1800,
            )
            bindings[claim_id] = {
                "claim_id": claim_id,
                "statement": _text(claim.get("statement"), 1800),
                "effective_statement": effective_statement,
                "evidence_type": _text(claim.get("evidence_type"), 80),
                "importance": importance,
                "load_bearing": load_bearing,
                "supporting_chunk_ids": supporting_ids,
                "factual_support_chunk_ids": factual_ids,
                "contextual_support_chunk_ids": list(dict.fromkeys(contextual_ids + _fallback_contextual_ids)),
                "author_reported_support_chunk_ids": [
                    item for item in role_ids.get("author_reported_support") or []
                    if item in records_by_id
                ],
                "counterevidence_chunk_ids": [
                    item for item in role_ids.get("counterevidence") or []
                    if item in records_by_id
                ],
                "boundary_chunk_ids": [
                    item for item in role_ids.get("boundary") or []
                    if item in records_by_id
                ],
                "background_context_chunk_ids": [
                    item for item in role_ids.get("background_context") or []
                    if item in records_by_id
                ],
                "relation_roles": list(claim.get("relation_roles") or []),
                "counterevidence_query": _text(claim.get("counterevidence_query"), 500),
                "boundary_conditions": [
                    _text(item, 500)
                    for item in (claim.get("boundary_conditions") or [])
                    if _text(item, 500)
                ][:8],
                "axis_assignments": [
                    dict(item) for item in (claim.get("axis_assignments") or [])
                    if isinstance(item, dict)
                ][:8],
                "evidence_role_bindings": [
                    dict(item) for item in (claim.get("evidence_role_bindings") or [])
                    if isinstance(item, dict)
                ],
                "core_chunk_ids": core_for_claim,
                "evidence_binding_status": (
                    "contextual_fallback" if _used_fallback_core else "matched"
                ),
                "core_paper_ids": _unique(
                    records_by_id[item]["paper_id"] for item in core_for_claim
                    if item in records_by_id
                ),
                "permission_status": "qualified_only" if (_used_fallback_core and permission_status == "bound") else permission_status,
                "write_status": write_status,
                "missing_material": missing,
                "claim_classification": classification,
                "support_classification": classification,
                "declared_support_chunk_ids": list(
                    claim.get("declared_support_chunk_ids") or []
                ),
                "rejected_support_chunk_ids": list(
                    claim.get("rejected_support_chunk_ids") or []
                ),
                "source_permissions": dict(claim.get("source_permissions") or {}),
                "claim_provenance": dict(claim.get("claim_provenance") or {}),
                "adaptation_action": adaptation_action,
                "adaptation_recommendation": adaptation_recommendation,
                "missing_evidence_components": [
                    _text(item, 500)
                    for item in (claim.get("missing_evidence_components") or claim.get("missing_components") or [])
                    if _text(item, 500)
                ],
                "claim_state": _text(claim.get("claim_state"), 80),
                "critic_flags": (list(claim.get("critic_flags") or []) + (["contextual_fallback"] if _used_fallback_core else []))[:8],
                "supported_rewrite": _text(claim.get("supported_rewrite"), 1800),
                "fresh_evidence_support_state": _text(
                    claim.get("fresh_evidence_support_state"), 80
                ),
                "fresh_evidence_component_states": list(
                    claim.get("fresh_evidence_component_states") or []
                ),
                "fresh_evidence_reconciliation": _text(
                    claim.get("fresh_evidence_reconciliation"), 120
                ),
                "superseded_supported_rewrite": _text(
                    claim.get("superseded_supported_rewrite"), 1800
                ),
                "selector_diagnostics": portfolio.diagnostics,
            }
        state["bindings"] = {
            "section_id": section["section_id"],
            "claims": bindings,
            "candidate_pool": {
                "ref": f"section_candidate_pool:{section['section_id']}",
                "complete_inventory": True,
                "all_chunk_ids": list(
                    (state.get("contract").candidate_material_pool if state.get("contract") else {}).get("chunk_ids") or []
                ),
                "all_paper_ids": list(
                    (state.get("contract").candidate_material_pool if state.get("contract") else {}).get("paper_ids") or []
                ),
                "served_chunk_ids": list(
                    (state.get("contract").candidate_material_pool if state.get("contract") else {}).get("served_chunk_ids") or []
                ),
                "served_paper_ids": list(
                    (state.get("contract").candidate_material_pool if state.get("contract") else {}).get("served_paper_ids") or []
                ),
                "chunk_ids": list(portfolio.candidate_chunk_ids),
                "paper_ids": list(portfolio.candidate_paper_ids),
                "count": len(portfolio.candidate_chunk_ids),
                "compression_strategy": dict(
                    (state.get("contract").candidate_material_pool if state.get("contract") else {}).get("compression_strategy") or {}
                ),
            },
            "core_portfolio": portfolio.to_dict(),
            "fresh_chunk_rebinding": state.get("fresh_chunk_rebinding", {
                "fresh_chunk_ids": [],
                "eligible_fresh_chunk_ids": [],
                "inspected_chunk_count": 0,
                "component_audit": [],
                "semantic_judge": {
                    "enabled": False,
                    "called": False,
                    "batch_count": 0,
                },
                "scientific_components_closed": [],
                "scientific_components_supported": [],
            }),
            "section_relation_edge_ids": [str(item.get("edge_id") or "") for item in local_edges],
            "status": "needs_more_literature",
        }

    def _write_updated_coverage_atlas(
        self,
        states: list[dict[str, Any]],
        migrated_edges: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build CoverageAtlas from the post-validation graph and overlays.

        CoverageAtlas is intentionally rebuilt from a temporary *view* of
        the shared material, not from the old Phase-2 files.  Each section
        receives a small source ledger, while the actual text remains in the
        shared SQLite database.  This prevents stale semantic edges and stale
        per-section permissions from leaking into the Phase-3 report.
        """

        coverage_root = self.output_dir / "coverage_snapshot"
        sections_root = coverage_root / "sections"
        sections_root.mkdir(parents=True, exist_ok=True)
        for state in states:
            section_id = str(state["section"]["section_id"])
            graph: CanonicalAssetGraph = state["graph"]
            rows: list[dict[str, Any]] = []
            for paper_id, paper in graph.papers.items():
                chunk_ids = [
                    chunk_id
                    for chunk_id, chunk in graph.chunks.items()
                    if chunk.paper_id == paper_id
                ]
                rows.append(
                    {
                        "paper_id": paper.paper_id,
                        "title": paper.title,
                        "year": paper.year,
                        "literature_role": paper.literature_role,
                        "scope_fit": paper.scope_fit,
                        "use_permission": paper.use_permission,
                        "content_depth": paper.content_depth,
                        "acquisition_status": paper.acquisition_status,
                        "discovery_route": paper.discovery_route,
                        "materialization_route": paper.materialization_route,
                        "allowed_claim_kinds": list(paper.allowed_claim_kinds),
                        "canonical_chunk_ids": chunk_ids,
                        "metadata_conflicts": list(paper.metadata_conflicts),
                        "section_id": section_id,
                    }
                )
            _write_json(
                sections_root / section_id / "SECTION_SOURCE_LEDGER.json",
                {
                    "schema_version": "research_harness.phase3_section_source_ledger.v1",
                    "section_id": section_id,
                    "sources": rows,
                    "source_of_truth": "phase3_canonical_asset_graph_with_section_overlay",
                },
            )

        # Force the atlas loader to read the freshly migrated graph instead of
        # an embedded legacy graph copied into the original blueprint.
        _write_json(
            coverage_root / "RELATION_GRAPH.json",
            {
                "schema_version": "research_harness.phase3_relation_graph.v1",
                "edges": migrated_edges,
                "source": "phase3_relation_revalidation",
            },
        )
        atlas_blueprint = dict(self.blueprint)
        atlas_blueprint.pop("relation_graph", None)
        atlas_blueprint.pop("literature_relation_graph", None)
        atlas = build_coverage_atlas(
            blueprint=atlas_blueprint,
            coverage_root=coverage_root,
            scope_map=self.scope_map,
        )
        atlas["source"] = {
            "relation_graph": str(coverage_root / "RELATION_GRAPH.json"),
            "section_ledgers": str(sections_root),
            "shared_kb_paths": [str(item) for item in self.shared_kb_paths],
            "overlay_paths": {
                key: str(value) for key, value in self.overlay_paths.items()
            },
        }
        _write_json(self.output_dir / "COVERAGE_ATLAS.json", atlas)
        return atlas

    @staticmethod
    def _derive_section_outcome(state: dict[str, Any]) -> str:
        """Derive an independent section outcome from the adapted claims."""

        claims = [item for item in state.get("claims") or [] if isinstance(item, dict)]
        bindings = state.get("bindings") or {}
        claim_bindings = bindings.get("claims") if isinstance(bindings, dict) else {}
        claim_bindings = claim_bindings if isinstance(claim_bindings, dict) else {}
        if not claims:
            return "needs_more_literature"

        load_open: list[str] = []
        limits: list[str] = []
        for claim in claims:
            claim_id = _text(claim.get("claim_id"), 120)
            importance = normalize_importance(claim)
            binding = claim_bindings.get(claim_id, {})
            classification = str(
                binding.get("claim_classification")
                or claim.get("support_classification")
                or claim.get("claim_classification")
                or "open_question"
            )
            if classification == "open_question" and importance == "load_bearing":
                load_open.append(claim_id)
            elif classification != "supported" or binding.get("missing_evidence_components"):
                limits.append(
                    f"{claim_id}:{classification or 'open_question'}"
                )

        merge = dict(state.get("merge_recommendation") or {})
        authorable_claim_count = len(state.get("authorable_claims") or [])
        if merge and load_open and not authorable_claim_count:
            state["declared_limits"] = _unique(
                [
                    *limits,
                    "merge_required:" + ",".join(merge.get("target_section_ids") or []),
                ]
            )
            return "merge_required"
        if load_open:
            state["declared_limits"] = _unique(
                [*limits, *[f"load_bearing_gap:{item}" for item in load_open]]
            )
            # A claim-level evidence gap is retained as an explicit excluded
            # lane.  It must not discard a section that still has other
            # permission-eligible, authorable claims; R4 can write the usable
            # claims while the gap remains available for later supplementary
            # retrieval.  Only a section with no authorable backbone stays
            # closed.
            return (
                "ready_with_limits"
                if authorable_claim_count
                else "needs_more_literature"
            )

        section = state.get("section") or {}
        atlas = section.get("coverage_atlas_section") or {}
        breadth = atlas.get("breadth_shortfall") if isinstance(atlas.get("breadth_shortfall"), dict) else {}
        if atlas.get("missing_literature_roles"):
            limits.extend(
                f"missing_literature_role:{_text(item, 120)}"
                for item in atlas.get("missing_literature_roles") or []
            )
        if any(int(value or 0) > 0 for value in breadth.values()):
            limits.append("coverage_breadth_shortfall")
        runtime_failure = state.get("runtime_failure") or {}
        if isinstance(runtime_failure, dict) and runtime_failure:
            limits.append(
                "runtime_failure:" + _text(runtime_failure.get("reason") or "section coverage failed", 180)
            )
        relation_tasks = (atlas.get("relationship_coverage") or {}).get(
            "missing_semantic_relation_tasks"
        )
        limits.extend(
            f"missing_relation_task:{_text(item, 160)}"
            for item in relation_tasks or []
        )
        state["declared_limits"] = _unique(limits)
        return "ready_with_limits" if limits else "ready"

    def _build_bundle(self, state: dict[str, Any], migrated_edges: list[dict[str, Any]]) -> None:
        section = state["section"]
        graph: CanonicalAssetGraph = state["graph"]
        local_edges = _section_relation_edges(migrated_edges, graph)
        # A narrowed supported_rewrite is the downstream writing statement;
        # the original wording remains in CLAIM_GRAPH/MATERIAL_BINDINGS for
        # audit.  Do not make SynthesisBundle rediscover that distinction.
        bundle_claims: list[dict[str, Any]] = []
        for claim in state["claims"]:
            item = dict(claim)
            binding = (state.get("bindings", {}).get("claims", {}) or {}).get(
                str(item.get("claim_id") or ""), {}
            )
            if isinstance(binding, dict):
                # Keep lifecycle and permission decisions adjacent to the
                # effective statement so the bundle classifier cannot fall
                # back to saturation alone.
                for key in (
                    "permission_status", "write_status", "missing_material",
                    "missing_evidence_components", "effective_statement",
                    "claim_classification", "support_classification",
                    "declared_support_chunk_ids", "rejected_support_chunk_ids",
                    "source_permissions", "claim_provenance", "adaptation_action",
                    "adaptation_recommendation",
                    "author_reported_support_chunk_ids", "counterevidence_chunk_ids",
                    "boundary_chunk_ids", "background_context_chunk_ids",
                    "relation_roles", "counterevidence_query", "boundary_conditions",
                    "axis_assignments", "evidence_role_bindings",
                ):
                    if binding.get(key) not in (None, ""):
                        item[key] = binding.get(key)
            if item.get("supported_rewrite") and item.get("supported_rewrite_eligible", True):
                item["original_statement"] = item.get("original_statement") or item.get("statement", "")
                item["effective_statement"] = item["supported_rewrite"]
                item["statement"] = item["supported_rewrite"]
            bundle_claims.append(item)
        task_coverage = _build_argument_task_coverage(
            state["contract"], state["claims"], state.get("bindings", {})
        )
        state["argument_task_coverage"] = task_coverage
        if isinstance(state.get("bindings"), dict):
            state["bindings"]["argument_task_coverage"] = task_coverage
        bundle = build_synthesis_bundle(
            section=section,
            claims=bundle_claims,
            relation_edges=local_edges,
            source_permissions={key: value.use_permission for key, value in graph.papers.items()},
            chunk_permissions={key: value.use_permission for key, value in graph.chunks.items()},
            allowed_paper_ids=list(graph.papers),
            allowed_chunk_ids=list(graph.chunks),
            chunk_to_paper={key: value.paper_id for key, value in graph.chunks.items()},
            chunk_records=list(graph.chunks.values()),
            max_core_chunks=self.authoring_core_chunk_limit,
            preselected_portfolio=state.get("portfolio"),
            argument_task_coverage=task_coverage,
            paper_content_depth_summary={
                str(key): str(value.content_depth)
                for key, value in graph.papers.items()
            },
        ).to_dict()
        outcome = self._derive_section_outcome(state)
        state["section_outcome"] = outcome
        claim_by_id = {
            str(item.get("claim_id")): item
            for item in state.get("claims") or []
            if isinstance(item, dict) and item.get("claim_id")
        }
        assignments: list[dict[str, Any]] = []
        for raw_assignment in bundle.get("claim_category_assignments") or []:
            assignment = dict(raw_assignment) if isinstance(raw_assignment, dict) else {}
            claim_id = str(assignment.get("claim_id") or "")
            claim = claim_by_id.get(claim_id, {})
            classification = str(
                claim.get("support_classification")
                or claim.get("claim_classification")
                or "open_question"
            )
            assignment["classification"] = classification
            assignment["effective_statement"] = _text(
                claim.get("effective_statement") or claim.get("statement"),
                1800,
            )
            assignment["original_statement"] = _text(
                claim.get("original_statement") or claim.get("statement"),
                1800,
            )
            assignment["supported_rewrite"] = _text(
                claim.get("supported_rewrite"),
                1800,
            )
            assignment["adaptation_action"] = _text(
                claim.get("adaptation_action"),
                120,
            )
            assignments.append(assignment)
        bundle["claim_category_assignments"] = assignments
        bundle["classification_counts"] = {
            value: sum(
                1
                for claim in state.get("claims") or []
                if str(
                    claim.get("support_classification")
                    or claim.get("claim_classification")
                    or "open_question"
                ) == value
            )
            for value in CLAIM_CLASSIFICATIONS
        }
        legacy_status = {
            "ready": "material_ready",
            "ready_with_limits": "ready_with_limits",
            "merge_required": "merge_required",
            "needs_more_literature": "needs_more_literature",
        }[outcome]
        state["bindings"]["section_outcome"] = outcome
        state["bindings"]["status"] = legacy_status
        bundle["section_overlay_path"] = str(
            state.get("overlay_path") or self.overlay_paths.get(section["section_id"], "")
        )
        bundle["claim_binding_status"] = legacy_status
        bundle["section_outcome"] = outcome
        bundle["declared_limits"] = list(state.get("declared_limits") or [])
        bundle["open_questions"] = list(state.get("open_questions") or [])
        bundle["authorable_claims"] = [
            dict(item) for item in state.get("authorable_claims") or []
        ]
        bundle["evidence_gap_claims"] = [
            dict(item) for item in state.get("evidence_gap_claims") or []
        ]
        bundle["authorable_claim_ids"] = [
            str(item.get("claim_id") or "")
            for item in state.get("authorable_claims") or []
            if item.get("claim_id")
        ]
        bundle["evidence_gap_claim_ids"] = [
            str(item.get("claim_id") or "")
            for item in state.get("evidence_gap_claims") or []
            if item.get("claim_id")
        ]
        bundle["claim_pool_runtime_audit"] = dict(
            state.get("claim_pool_runtime_audit") or {}
        )
        bundle["claim_pool_global_expansion"] = dict(
            state.get("claim_pool_global_expansion") or {}
        )
        bundle["adaptation_actions"] = list(state.get("adaptation_actions") or [])
        bundle["merge_recommendation"] = dict(state.get("merge_recommendation") or {})
        bundle["runtime_failure"] = dict(state.get("runtime_failure") or {})
        bundle["candidate_material_pool"] = dict(
            state.get("contract").candidate_material_pool
            if state.get("contract") else {}
        )
        bundle["r4_handoff_allowed"] = outcome in {"ready", "ready_with_limits"}
        state["bundle"] = bundle
        if outcome == "ready":
            state["status"] = "material_ready"
            bundle["r4_handoff_allowed"] = True
            bundle["readiness_status"] = "ready_for_authoring"
            bundle["status"] = "material_ready"
        elif outcome == "ready_with_limits":
            state["status"] = "ready_with_limits"
            bundle["readiness_status"] = "ready_with_limits"
            bundle["status"] = "ready_with_limits"
        elif outcome == "merge_required":
            state["status"] = "merge_required"
            bundle["readiness_status"] = "merge_required"
            bundle["status"] = "merge_required"
        else:
            state["status"] = "needs_more_literature"
            bundle["readiness_status"] = "needs_more_literature"
            bundle["status"] = "needs_more_literature"
        # SynthesisBundle's selector assesses whether useful material exists;
        # Phase 3 additionally requires that the current claim set has an
        # explicit, permission-eligible binding.  Keep candidate material,
        # but never let the bundle advertise authoring readiness when the
        # section gate is still open.
        if not bundle["r4_handoff_allowed"]:
            bundle["readiness_status"] = "needs_more_literature"
            bundle["status"] = state["status"]
            bundle["unresolved_claim_ids"] = [
                str(claim_id)
                for claim_id, item in state["bindings"].get("claims", {}).items()
                if item.get("write_status") not in {"bound", "write_with_qualified_support"}
            ]

    def _make_requests(
        self,
        states: list[dict[str, Any]],
        iteration: int,
    ) -> list[CoverageRequest]:
        requests: list[CoverageRequest] = []
        for state in states:
            if state.get("runtime_failure"):
                # A worker/runtime failure is not silently converted into a
                # scientific retrieval request.  The failure remains in the
                # phase ledger for a retry controller or operator.
                continue
            section = state["section"]
            sid = section["section_id"]
            atlas = section.get("coverage_atlas_section") or {}
            required_roles = _unique(section.get("required_roles") or [])
            atlas_missing_roles = _unique(atlas.get("missing_literature_roles") or [])
            portfolio_missing_roles = _unique(
                (state.get("portfolio") or state.get("m2a_portfolio")).missing_roles
                if getattr(state.get("portfolio") or state.get("m2a_portfolio"), "missing_roles", None)
                else []
            )
            missing_roles = [
                role for role in _unique(atlas_missing_roles + portfolio_missing_roles)
                if not required_roles or role in required_roles
            ]
            breadth = atlas.get("breadth_shortfall") if isinstance(atlas.get("breadth_shortfall"), dict) else {}
            # Only necessary load-bearing claims can trigger an expensive
            # request.  Other gaps are preserved in the request audit but do
            # not block a section or reopen the search loop.
            missing_claims: list[dict[str, Any]] = []
            non_blocking_gaps: list[dict[str, Any]] = []
            for claim_id, binding in state["bindings"].get("claims", {}).items():
                component_gap = bool(binding.get("missing_evidence_components"))
                if not binding.get("missing_material") and not component_gap:
                    continue
                item = {
                    "claim_id": claim_id,
                    "statement": binding.get("effective_statement") or binding.get("statement", ""),
                    "importance": binding.get("importance", "supporting"),
                    "missing_evidence_components": list(binding.get("missing_evidence_components") or []),
                    "claim_state": binding.get("claim_state", ""),
                    "gap_kind": "unbound_material" if binding.get("missing_material") else "missing_component",
                    "classification": binding.get("claim_classification", "open_question"),
                    "adaptation_action": binding.get("adaptation_action", ""),
                }
                if (
                    binding.get("importance") == "load_bearing"
                    and binding.get("adaptation_action") != "merge_recommendation"
                ):
                    missing_claims.append(item)
                else:
                    non_blocking_gaps.append({**item, "reason": "non_blocking_claim_gap"})
            missing_relations = list(
                (atlas.get("relationship_coverage") or {}).get("missing_semantic_relation_tasks") or []
            )
            triggers: list[str] = []
            if not state["claims"]:
                triggers.append("missing_claim_decomposition")
            if missing_claims:
                triggers.append("load_bearing_or_unbound_claim_material")
            if missing_roles or any(int(value or 0) > 0 for value in breadth.values()):
                triggers.append("section_breadth_or_role_shortfall")
            if missing_relations:
                triggers.append("missing_section_relation_tasks")
            if not triggers:
                continue
            query_list = compile_coverage_queries(
                section=section,
                missing_roles=missing_roles,
                missing_claims=missing_claims,
                missing_relations=missing_relations,
                breadth_shortfall=any(int(value or 0) > 0 for value in breadth.values()),
            )
            if not query_list:
                # A non-English or underspecified section still receives an
                # executable, bounded query instead of a silently empty one.
                query_list = [_compile_targeted_query(
                    section=section,
                    component="targeted mechanism characterization",
                    role="optical comparison",
                )]
            component_pairs: list[dict[str, Any]] = []
            for item in missing_claims:
                claim_id = str(item.get("claim_id") or "")
                for index, component in enumerate(item.get("missing_evidence_components") or []):
                    component_pairs.append({
                        "claim_id": claim_id,
                        "missing_component_id": f"{claim_id}::component_{index + 1}",
                        "missing_component": str(component),
                    })
            query_targets = []
            for index, query in enumerate(query_list):
                targets = [
                    component_pairs[index % len(component_pairs)]
                ] if component_pairs else []
                query_targets.append({
                    "query": query,
                    "claim_ids": list(dict.fromkeys(
                        [item["claim_id"] for item in targets]
                    )),
                    "missing_component_ids": [
                        item["missing_component_id"] for item in targets
                    ],
                    "missing_components": [
                        item["missing_component"] for item in targets
                    ],
                })
            desired = max(
                1,
                int(breadth.get("unique_sources") or 0),
                len(missing_roles),
                len(missing_claims),
            )
            # Phase 2 currently materializes at most three papers per section
            # in one execution.  Keep the request internally executable and
            # express a larger ambition as a bounded multi-wave target.
            per_wave_budget = 3
            max_waves = min(self.max_iterations, 2)
            expected = min(per_wave_budget, desired)
            target_total = min(desired, per_wave_budget * max_waves)
            digest = hashlib.sha1(f"{sid}|{iteration}|{'|'.join(triggers)}".encode("utf-8")).hexdigest()[:12]
            requests.append(
                CoverageRequest(
                    request_id=f"coverage:{sid}:{iteration}:{digest}",
                    section_id=sid,
                    iteration=iteration,
                    priority="load_bearing" if missing_claims else "breadth",
                    trigger=";".join(dict.fromkeys(triggers)),
                    missing_claim_ids=[item["claim_id"] for item in missing_claims],
                    missing_roles=list(dict.fromkeys(missing_roles)),
                    missing_relation_tasks=list(dict.fromkeys(missing_relations)),
                    queries=query_list,
                    query_targets=query_targets,
                    expected_new_papers=expected,
                    per_wave_paper_budget=per_wave_budget,
                    target_total_new_papers=target_total,
                    non_blocking_gaps=non_blocking_gaps,
                    stop_condition={
                        "target_missing_claim_ids": [item["claim_id"] for item in missing_claims],
                        "target_missing_roles": list(dict.fromkeys(missing_roles)),
                        "expected_new_papers": expected,
                        "per_wave_paper_budget": per_wave_budget,
                        "target_total_new_papers": target_total,
                        "max_waves": max_waves,
                        "stop_when": ["requested load-bearing component or role is addressed", "no new relevant material in two bounded waves"],
                        "do_not_stop_only_because": ["metadata_only_candidate_exists"],
                        "max_iterations": max_waves,
                    },
                    affected_section_ids=[sid],
                )
            )
        return requests

    def _execute_phase2_requests(self, requests: list[CoverageRequest]) -> dict[str, Any]:
        if not self.execute_coverage or not requests:
            return {
                "status": "not_run",
                "reason": "offline_or_execute_coverage_disabled",
                "sections": [],
            }
        blueprint_path = self.output_dir / "PHASE3_BLUEPRINT_INPUT.json"
        _write_json(blueprint_path, self.blueprint)
        config = SectionCoverageOrchestratorConfig(
            blueprint_path=blueprint_path,
            base_kb_sqlite=self.shared_kb_paths[0] if self.shared_kb_paths else None,
            output_root=self.output_dir / "coverage_requests",
            max_iters_per_section=12,
            token_budget_per_section=80_000,
            cost_budget_per_section_cny=1.0,
            stage_cost_budget_cny=4.0,
            max_materialized_papers_per_section=3,
            coverage_requests_by_section={
                item.section_id: item.to_dict() for item in requests
            },
            shared_kb_sqlite_paths=list(self.shared_kb_paths),
            source_ledger_path=self.shared_ledger_path,
            section_overlay_paths={
                str(section.get("section_id")): self.overlay_paths[str(section.get("section_id"))]
                for section in self.blueprint.get("sections") or []
                if str(section.get("section_id")) in self.overlay_paths
            },
            selected_paper_ids_by_section={
                str(section.get("section_id")): list(
                    (section.get("phase3_material_context") or {}).get("selected_paper_ids") or []
                )
                for section in self.blueprint.get("sections") or []
                if isinstance(section, dict)
            },
            selected_chunk_ids_by_section={
                str(section.get("section_id")): list(
                    (section.get("phase3_material_context") or {}).get("selected_chunk_ids") or []
                )
                for section in self.blueprint.get("sections") or []
                if isinstance(section, dict)
            },
        )
        result = SectionCoverageOrchestrator(config).run(
            section_ids=sorted({item.section_id for item in requests})
        )
        return {
            "status": result.status,
            "sections": sorted({item.section_id for item in requests}),
            "sections_completed": result.sections_completed,
            "sections_needing_more_literature": result.sections_needing_more_literature,
            "total_cost_cny": result.total_cost_cny,
            "work_dir": str(result.work_dir),
            "patches": {
                section_id: {
                    "source_ledger_path": str(bundle.source_ledger_path),
                    "kb_sqlite": str(bundle.kb_sqlite) if bundle.kb_sqlite else "",
                    "staging_kb_sqlite": (
                        str(bundle.staging_kb_sqlite)
                        if bundle.staging_kb_sqlite else ""
                    ),
                    "material_package_path": str(bundle.material_package_path),
                    "synthesis_bundle_path": (
                        str(bundle.synthesis_bundle_path)
                        if bundle.synthesis_bundle_path else ""
                    ),
                }
                for section_id, bundle in result.material_bundles.items()
            },
        }

    def build_production_handoff(
        self,
        *,
        states: Iterable[dict[str, Any]],
        coverage_atlas: dict[str, Any],
        claim_graph: dict[str, Any],
        relation_graph: dict[str, Any],
        coverage_requests: Iterable[CoverageRequest] = (),
        phase_run: dict[str, Any] | None = None,
        acceptance: dict[str, Any] | None = None,
    ) -> Any:
        """Adapt the final in-memory Phase-3 state into the R3 handoff.

        This public seam is the only producer API that a later top-level
        orchestrator needs to call.  ``run`` calls it automatically after the
        final Phase-3 acceptance audit and writes ``R3_PRODUCTION_HANDOFF.json``.
        """

        return build_r3_production_handoff_from_phase3(
            blueprint=self.blueprint,
            states=states,
            coverage_atlas=coverage_atlas,
            claim_graph=claim_graph,
            relation_graph=relation_graph,
            coverage_requests=coverage_requests,
            phase_run=phase_run,
            acceptance=acceptance,
            output_dir=self.output_dir,
        )

    def run(
        self,
        *,
        coverage_executor: Callable[[list[dict[str, Any]], int], dict[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        run_started = time.perf_counter()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        raw_sections = [
            dict(item) for item in self.blueprint.get("sections") or []
            if isinstance(item, dict) and _text(item.get("section_id"), 80)
        ]
        states = [
            self._prepare_section(section, index, raw_sections)
            for index, section in enumerate(raw_sections)
            if not self.section_ids_to_process
            or str(section.get("section_id")) in self.section_ids_to_process
        ]
        raw_edges = [item for item in self.relation_graph.get("edges") or [] if isinstance(item, dict)]
        identity_resolver = build_canonical_identity_resolver(
            _phase3_identity_inventory(states)
        )
        canonical_raw_edges, identity_edge_audit = identity_resolver.map_relation_endpoints(
            raw_edges
        )
        for edge in canonical_raw_edges:
            basis = _relation_basis_ids(edge)
            if basis:
                # Normalize an already supplied basis alias; never derive one
                # from endpoint papers or claim bindings.
                edge["relation_basis_chunk_ids"] = basis
        active_papers = set(identity_resolver.active_paper_ids)
        active_chunks = set(item for state in states for item in state["graph"].chunks)
        migrated_edges, relation_audit = revalidate_legacy_relation_edges(
            canonical_raw_edges,
            active_paper_ids=active_papers,
            active_chunk_ids=active_chunks,
        )
        relation_audit["identity_resolution"] = identity_edge_audit
        _write_json(self.output_dir / "RELATION_GRAPH_MIGRATED.json", {
            **self.relation_graph,
            "edges": migrated_edges,
            "phase3_relation_revalidation": relation_audit,
        })

        iteration_records: list[dict[str, Any]] = []
        all_requests: list[CoverageRequest] = []
        coverage_runs: list[dict[str, Any]] = []
        recomputed_sections: set[str] = set()
        pending_rebind_sections: set[str] = set()
        coverage_waves_executed = 0
        for iteration in range(1, self.max_iterations + 1):
            sections_to_process = (
                {state["section"]["section_id"] for state in states}
                if iteration == 1
                else set(pending_rebind_sections)
            )
            iteration_states = [
                state
                for state in states
                if state["section"]["section_id"] in sections_to_process
            ]
            claim_pool_workers = 1
            if (
                self.real_llm_claims
                and self.claim_pool_enabled
                and len(iteration_states) > 1
            ):
                claim_pool_workers = min(4, len(iteration_states))
                with ThreadPoolExecutor(
                    max_workers=claim_pool_workers,
                    thread_name_prefix="phase3-claim-pool",
                ) as executor:
                    list(executor.map(self._decompose_claims, iteration_states))
            else:
                for state in iteration_states:
                    self._decompose_claims(state)
            # Binding and bundle writes stay deterministic and section ordered.
            for state in iteration_states:
                self._bind_section(state, migrated_edges)
                self._build_bundle(state, migrated_edges)
            pending_rebind_sections.difference_update(sections_to_process)
            requests = self._make_requests(states, iteration)
            all_requests.extend(requests)
            iteration_records.append({
                "iteration": iteration,
                "sections_processed": sorted(sections_to_process),
                "request_count": len(requests),
                "affected_sections": sorted({item.section_id for item in requests}),
                "new_claim_count": sum(len(state["claims"]) for state in states),
                "claim_pool_workers": claim_pool_workers,
            })
            if not requests:
                break
            if coverage_waves_executed >= 1:
                by_id = {
                    state["section"]["section_id"]: state for state in states
                }
                for item in requests:
                    state = by_id.get(str(item.section_id))
                    if state is not None:
                        state["coverage_retrieval_skipped"] = True
                        state.setdefault("adaptation_actions", [])
                        state["adaptation_actions"] = _unique(
                            list(state.get("adaptation_actions") or [])
                            + ["coverage_retrieval_limit_reached"]
                        )
                    item.status = "not_executed"
                    item.execution_note = (
                        "Fresh coverage retrieval already executed once; "
                        "claims revised from existing bound material only."
                    )
                break
            patches: dict[str, dict[str, Any]] = {}
            if coverage_executor is not None:
                coverage_waves_executed += 1
                patches = coverage_executor([item.to_dict() for item in requests], iteration) or {}
            elif self.execute_coverage:
                coverage_waves_executed += 1
                coverage_result = self._execute_phase2_requests(requests)
                patches = dict(coverage_result.pop("patches", {}) or {})
                coverage_result["patch_count"] = len(patches)
                coverage_runs.append(coverage_result)
            if not patches:
                for item in requests:
                    item.status = "not_executed"
                    item.execution_note = "No coverage executor was supplied; request is ready for Phase 2."
                break
            iteration_recomputed: list[str] = []
            by_id = {state["section"]["section_id"]: state for state in states}
            for sid, patch in patches.items():
                state = by_id.get(str(sid))
                if not state or not isinstance(patch, dict):
                    continue
                if patch.get("claims") is not None:
                    state["section"]["claims"] = list(patch.get("claims") or [])
                before_fingerprint = self._state_evidence_fingerprint(state)
                if patch.get("source_ledger_path"):
                    self._refresh_state_from_coverage_patch(state, patch)
                if patch.get("candidate_text_chunks"):
                    existing = {item["chunk_id"]: item for item in state["records"]}
                    existing.update({str(item.get("chunk_id")): dict(item) for item in patch["candidate_text_chunks"] if isinstance(item, dict) and item.get("chunk_id")})
                    state["records"] = list(existing.values())
                    m2a_portfolio, m2a_records = self._select_m2a_input(
                        state["section"], state["contract"], state["records"], state["graph"]
                    )
                    state["m2a_portfolio"] = m2a_portfolio
                    state["m2a_records"] = m2a_records
                    state["section"]["candidate_text_chunks"] = m2a_records
                    state["section"]["candidate_text_chunk_ids"] = [item["chunk_id"] for item in m2a_records]
                    state["section"]["candidate_pool_ids"] = list(m2a_portfolio.candidate_chunk_ids)
                    state["m2a_input_payload"] = ClaimDecomposer(real_llm=False)._build_input_payload(
                        self._m2a_section_view(
                            state["section"],
                            state.get("m2a_records") or [],
                            compact_context=str(sid) in self._m2a_compact_context,
                        )
                    )
                made_evidence_delta = (
                    self._state_evidence_fingerprint(state)
                    != before_fingerprint
                )
                if made_evidence_delta:
                    iteration_recomputed.append(str(sid))
                    recomputed_sections.add(str(sid))
                    pending_rebind_sections.add(str(sid))
                else:
                    notes = patch.get("notes") or patch.get("reviewer_notes")
                    if isinstance(notes, dict):
                        state["section"].setdefault(
                            "reviewer_notes", {}
                        ).update(dict(notes))
            for item in requests:
                if item.section_id in iteration_recomputed:
                    item.status = "executed"
                    item.execution_note = "Affected section recomputed from returned coverage material."
                else:
                    item.execution_note = (
                        "No material evidence delta; reviewer notes preserved."
                    )
            if not iteration_recomputed:
                break

        # A bounded run may consume its final iteration while applying a
        # Phase-2 patch.  Rebind once after the loop so the final artifacts
        # expose the fresh graph and fresh component audit; otherwise the new
        # chunks are visible in the graph but MATERIAL_BINDINGS still reflects
        # the pre-patch iteration.
        if pending_rebind_sections:
            for state in states:
                if state["section"]["section_id"] in pending_rebind_sections:
                    self._decompose_claims(state)
                    self._bind_section(state, migrated_edges)
                    self._build_bundle(state, migrated_edges)

        claims_by_section = {
            state["section"]["section_id"]: state["claims"] for state in states
        }
        contracts = [state["contract"].to_dict() for state in states]
        claim_graph = self._build_claim_graph(states)
        material_bindings = {
            state["section"]["section_id"]: state["bindings"] for state in states
        }
        bundles = [state["bundle"] for state in states]
        statuses = {state["section"]["section_id"]: state["status"] for state in states}
        outcomes = {
            state["section"]["section_id"]: str(
                state.get("section_outcome") or "needs_more_literature"
            )
            for state in states
        }
        r4_ready_section_ids = sorted(
            sid for sid, outcome in outcomes.items()
            if outcome in {"ready", "ready_with_limits"}
        )
        if not states:
            phase_status = "failed_closed"
        elif all(value == "ready" for value in outcomes.values()):
            phase_status = "completed"
        elif r4_ready_section_ids:
            phase_status = "completed_with_limits"
        else:
            phase_status = "needs_more_literature"
        updated_atlas = self._write_updated_coverage_atlas(states, migrated_edges)
        llm_summary = _llm_audit_summary(states)
        fresh_semantic_judge_summary = _fresh_semantic_judge_summary(states)
        phase_run = {
            "schema_version": "research_harness.phase3_run.v1",
            "phase": "Phase 3 - Argument and Material Orchestration",
            "r4_entered": False,
            "status": phase_status,
            "elapsed_seconds": round(time.perf_counter() - run_started, 3),
            "iterations": iteration_records,
            "coverage_runs": coverage_runs,
            "coverage_waves_executed": coverage_waves_executed,
            "recomputed_sections": sorted(recomputed_sections),
            "fresh_chunk_rebinding": {
                state["section"]["section_id"]: state.get("fresh_chunk_rebinding", {
                    "fresh_chunk_ids": [],
                    "eligible_fresh_chunk_ids": [],
                    "inspected_chunk_count": 0,
                    "component_audit": [],
                    "semantic_judge": {
                        "enabled": False,
                        "called": False,
                        "batch_count": 0,
                    },
                    "scientific_components_closed": [],
                    "scientific_components_supported": [],
                })
                for state in states
            },
            "fresh_evidence_semantic_judge": fresh_semantic_judge_summary,
            "section_ownership_refresh": {
                state["section"]["section_id"]: list(
                    state.get("ownership_refresh_audit") or []
                )
                for state in states
            },
            "section_statuses": statuses,
            "section_outcomes": outcomes,
            "r4_ready_section_ids": r4_ready_section_ids,
            "partial_handoff_allowed": bool(r4_ready_section_ids),
            "claim_statuses": {state["section"]["section_id"]: state["claim_status"] for state in states},
            "claim_pool_audit": {
                state["section"]["section_id"]: dict(
                    state.get("claim_pool_runtime_audit") or {}
                )
                for state in states
            },
            "relation_revalidation": relation_audit,
            "coverage_atlas_path": str(self.output_dir / "COVERAGE_ATLAS.json"),
            "candidate_claim_pool_path": str(
                self.output_dir / "CANDIDATE_CLAIM_POOLS.json"
            ),
            "r3_production_handoff_path": str(self.output_dir / R3_HANDOFF_FILENAME),
            "updated_coverage_relation_counts": updated_atlas.get("relation_graph", {}),
            "llm": llm_summary,
            "phase2_executor_available": coverage_executor is not None or self.execute_coverage,
            "runtime_options": {
                "real_llm_claims": self.real_llm_claims,
                "claim_pool_enabled": self.claim_pool_enabled,
                "real_llm_dag": self.real_llm_dag,
                "claim_model_tier": self.claim_model_tier,
                "dag_model_tier": self.dag_model_tier,
                "max_m2a_input_tokens": self.max_m2a_input_tokens,
                "max_m2a_records": self.max_m2a_records,
                "max_dag_candidates": self.max_dag_candidates,
                "execute_coverage": self.execute_coverage,
            },
            "runtime_failures": {
                state["section"]["section_id"]: dict(state.get("runtime_failure") or {})
                for state in states
                if state.get("runtime_failure")
            },
            "m2a_budget": dict(self._m2a_budget_audit),
            "blueprint_context": {
                "input_section_count": len(raw_sections),
                "processed_section_ids": [state["section"]["section_id"] for state in states],
                "section_ids_to_process": sorted(self.section_ids_to_process),
                "full_blueprint_preserved": len(raw_sections) >= len(states),
                "preserved_context_fields": [
                    "mentor_guidance", "review_mentor_advice", "synthesis_task",
                    "transition_from_previous", "transition_to_next",
                    "preceding_section_conclusion", "following_section_role",
                    "transition_contract", "target_word_range", "visual_argument_slots",
                    "visual_requirements",
                ],
                "context_value_handoff": [
                    self._context_handoff_audit(state)
                    for state in states
                ],
            },
            "stop_reason": "all_sections_material_ready" if phase_status == "completed" else (
                "partial_sections_ready_with_declared_limits"
                if r4_ready_section_ids
                else "one_or_more_sections_require_material_or_claim_expansion"
            ),
        }

        _write_json(self.output_dir / "SECTION_ARGUMENT_CONTRACTS.json", {"contracts": contracts})
        _write_json(
            self.output_dir / "CANDIDATE_CLAIM_POOLS.json",
            {
                "schema_version": "research_harness.candidate_claim_pools.v1",
                "sections": {
                    state["section"]["section_id"]: {
                        "candidate_claim_pool": dict(
                            state["section"].get("candidate_claim_pool") or {}
                        ),
                        "candidate_claim_pool_audit": dict(
                            state["section"].get("candidate_claim_pool_audit")
                            or {}
                        ),
                        "shortlist_audit": dict(
                            state["section"].get(
                                "candidate_claim_pool_shortlist_audit"
                            )
                            or {}
                        ),
                        "claim_lanes": dict(
                            state["section"].get("claim_lanes") or {}
                        ),
                        "runtime_audit": dict(
                            state.get("claim_pool_runtime_audit") or {}
                        ),
                    }
                    for state in states
                },
            },
        )
        _write_json(self.output_dir / "CLAIM_GRAPH.json", claim_graph)
        _write_json(self.output_dir / "MATERIAL_BINDINGS.json", {"sections": material_bindings})
        _write_json(self.output_dir / "COVERAGE_REQUESTS.json", {"requests": [item.to_dict() for item in all_requests]})
        _write_json(self.output_dir / "SYNTHESIS_BUNDLES.json", {"bundles": bundles})
        _write_json(
            self.output_dir / "M2A_INPUT_PAYLOADS.json",
            {
                "schema_version": "research_harness.m2a_input_payloads.v1",
                "sections": {
                    state["section"]["section_id"]: state.get("m2a_input_payload") or {}
                    for state in states
                },
            },
        )
        _write_json(self.output_dir / "PHASE3_RUN.json", phase_run)
        acceptance = self._acceptance(
            states=states,
            requests=all_requests,
            claim_graph=claim_graph,
            relation_audit=relation_audit,
            phase_run=phase_run,
            coverage_atlas=updated_atlas,
            llm_summary=llm_summary,
        )
        production_handoff = self.build_production_handoff(
            states=states,
            coverage_atlas=updated_atlas,
            claim_graph=claim_graph,
            relation_graph={
                "schema_version": "research_harness.phase3_relation_graph.v1",
                "edges": migrated_edges,
            },
            coverage_requests=all_requests,
            phase_run=phase_run,
            acceptance=acceptance,
        )
        handoff_report = write_r3_production_handoff(
            self.output_dir / R3_HANDOFF_FILENAME,
            production_handoff,
        )
        acceptance["r3_production_handoff"] = {
            "path": str(self.output_dir / R3_HANDOFF_FILENAME),
            "schema_version": production_handoff.schema_version,
            "validation_status": handoff_report.status,
            "validation_errors": list(handoff_report.errors),
            "global_readiness": dict(handoff_report.global_readiness),
        }
        _write_json(self.output_dir / "PHASE3_ACCEPTANCE.json", acceptance)
        self._write_markdown(acceptance, phase_run)
        return acceptance

    @staticmethod
    def _acceptance(
        *,
        states: list[dict[str, Any]],
        requests: list[CoverageRequest],
        claim_graph: dict[str, Any],
        relation_audit: dict[str, Any],
        phase_run: dict[str, Any],
        coverage_atlas: dict[str, Any],
        llm_summary: dict[str, Any],
    ) -> dict[str, Any]:
        all_ids_valid = all(
            not bundle.get("invalid_chunk_ids") and not bundle.get("invalid_paper_ids")
            for state in states for bundle in [state["bundle"]]
        )
        no_generic = all(
            _is_real_claim(claim)
            for state in states for claim in state["claims"]
        )
        request_traceable = all(
            item.section_id
            and item.queries
            and item.affected_section_ids == [item.section_id]
            and 3 <= len(item.queries) <= 5
            and all(6 <= len(_english_words(query)) <= 15 for query in item.queries)
            and all(
                not any(term in {"load", "bearing", "claim", "section", "evidence", "literature", "workflow", "peer", "reviewed"}
                        for term in _english_words(query))
                for query in item.queries
            )
            and len({_clean_text(query).casefold() for query in item.queries}) == len(item.queries)
            and int(item.expected_new_papers or 0) <= int(item.per_wave_paper_budget or 0)
            and int(item.target_total_new_papers or 0) <= int(item.per_wave_paper_budget or 0) * int(item.stop_condition.get("max_waves") or 1)
            and int(item.stop_condition.get("expected_new_papers") or 0) == int(item.expected_new_papers or 0)
            and all(
                claim_id in set(item.missing_claim_ids)
                for claim_id in item.stop_condition.get("target_missing_claim_ids", [])
            )
            for item in requests
        )
        atlas_relation_counts = coverage_atlas.get("relation_graph") or {}
        atlas_semantic_count = sum(
            int(value or 0)
            for value in (atlas_relation_counts.get("semantic_relation_counts") or {}).values()
        )
        relation_atlas_consistent = atlas_semantic_count == int(
            relation_audit.get("output_semantic_edges", 0)
        )
        status_counts: dict[str, int] = {}
        for state in states:
            value = state["status"]
            status_counts[value] = status_counts.get(value, 0) + 1
        outcome_by_section = {
            state["section"]["section_id"]: str(
                state.get("section_outcome") or "needs_more_literature"
            )
            for state in states
        }
        r4_ready_section_ids = sorted(
            sid for sid, outcome in outcome_by_section.items()
            if outcome in {"ready", "ready_with_limits"}
        )
        handoff_ready = bool(r4_ready_section_ids)
        contract_flow = all(
            isinstance(state.get("section", {}).get("section_contract"), dict)
            and isinstance(state.get("section", {}).get("section_argument_contract"), dict)
            and state.get("section", {}).get("section_contract")
            == state.get("section", {}).get("section_argument_contract")
            for state in states
        )
        task_coverage_passed = True
        effective_statement_propagation = True
        duplicate_bundle_categories = False
        for state in states:
            contract_obj = state.get("contract")
            contract_tasks = {
                str(item.get("task_id"))
                for item in (contract_obj.argument_tasks if contract_obj is not None else [])
                if isinstance(item, dict) and item.get("task_id")
            }
            task_map = state.get("argument_task_coverage") or state.get("bundle", {}).get("argument_task_coverage") or []
            mapped_tasks = {str(item.get("task_id")) for item in task_map if isinstance(item, dict)}
            if contract_tasks != mapped_tasks:
                task_coverage_passed = False
            for task in task_map:
                if not isinstance(task, dict):
                    task_coverage_passed = False
                    continue
                if task.get("status") == "gap" and not task.get("missing_components"):
                    task_coverage_passed = False
            assignments = state.get("bundle", {}).get("claim_category_assignments") or []
            seen_claim_ids: set[str] = set()
            for assignment in assignments:
                if not isinstance(assignment, dict):
                    duplicate_bundle_categories = True
                    continue
                claim_id = str(assignment.get("claim_id") or "")
                if claim_id and claim_id in seen_claim_ids:
                    duplicate_bundle_categories = True
                if claim_id:
                    seen_claim_ids.add(claim_id)
                claim = next(
                    (item for item in state.get("claims", []) if str(item.get("claim_id")) == claim_id),
                    {},
                )
                expected_statement = str(
                    claim.get("effective_statement")
                    or claim.get("supported_rewrite")
                    or claim.get("statement")
                    or ""
                ).strip()
                if str(assignment.get("effective_statement") or "").strip() != expected_statement:
                    effective_statement_propagation = False
            category_lists = [
                state.get("bundle", {}).get("established_points") or [],
                state.get("bundle", {}).get("conditional_points") or [],
                state.get("bundle", {}).get("conflicts_or_boundaries") or [],
            ]
            if len(set().union(*[set(values) for values in category_lists])) != sum(len(values) for values in category_lists):
                duplicate_bundle_categories = True
        depth_aggregation_passed = True
        for state in states:
            graph = state.get("graph")
            if graph is None:
                continue
            for paper_id, paper in graph.papers.items():
                best_depth = max(
                    [
                        str(chunk.content_depth or "metadata")
                        for chunk in graph.chunks.values()
                        if chunk.paper_id == paper_id and str(chunk.normalized_text or "").strip()
                    ]
                    or [str(paper.content_depth or "metadata")],
                    key=lambda value: {"metadata": 0, "abstract": 1, "structured_snippet": 2, "fulltext": 3}.get(value, 0),
                )
                if {"metadata": 0, "abstract": 1, "structured_snippet": 2, "fulltext": 3}.get(str(paper.content_depth), 0) < {"metadata": 0, "abstract": 1, "structured_snippet": 2, "fulltext": 3}.get(best_depth, 0):
                    depth_aggregation_passed = False
        claim_pool_required = bool(
            (phase_run.get("runtime_options") or {}).get("claim_pool_enabled")
        )
        claim_pool_audits = phase_run.get("claim_pool_audit") or {}
        claim_pool_integrity_passed = bool(
            not claim_pool_required
            or (
                len(claim_pool_audits) == len(states)
                and all(
                    isinstance(audit, Mapping)
                    and audit.get("integrity_passed") is True
                    for audit in claim_pool_audits.values()
                )
            )
        )
        blueprint_context = phase_run.get("blueprint_context") or {}
        full_blueprint_context_passed = bool(
            blueprint_context.get("full_blueprint_preserved")
            and blueprint_context.get("preserved_context_fields")
            and all(
                item.get("passed") is True
                for item in blueprint_context.get("context_value_handoff") or []
            )
        )
        context_value_handoff_passed = all(
            item.get("passed") is True
            for item in blueprint_context.get("context_value_handoff") or []
        )
        input_budget_limit = max(50_000, max(1, len(states)) * 25_000)
        input_budget_passed = (
            int(llm_summary.get("estimated_input_tokens_total") or 0)
            <= input_budget_limit
        )
        verifier_batch_budget_passed = int(llm_summary.get("max_batch_estimated_input_tokens") or 0) <= 8_000
        # An empty section is an honest inventory_only result, not a claim
        # quality pass.  Do not let Python's vacuous ``all([])`` turn a
        # missing decomposition into a green acceptance flag.
        claim_quality_passed = bool(states) and all(state.get("claims") for state in states) and no_generic and all(
            normalize_importance(claim) in {"load_bearing", "supporting", "optional"}
            and str(
                claim.get("support_classification")
                or claim.get("claim_classification")
                or ""
            ) in CLAIM_CLASSIFICATIONS
            and not (
                str(claim.get("section_fit") or "").casefold() in {"boundary", "off_scope"}
                and bool(claim.get("load_bearing"))
            )
            for state in states for claim in state.get("claims", [])
        )
        evidence_permission_passed = True
        permission_audit: list[dict[str, Any]] = []
        for state in states:
            record_by_id = {str(row.get("chunk_id")): row for row in state.get("records", [])}
            for claim_id, binding in (state.get("bindings", {}).get("claims", {}) or {}).items():
                for chunk_id in binding.get("supporting_chunk_ids", []):
                    row = record_by_id.get(str(chunk_id))
                    ceiling, reason = evidence_ceiling(row)
                    if ceiling == DISCOVERY or not row:
                        evidence_permission_passed = False
                        permission_audit.append({
                            "claim_id": claim_id,
                            "chunk_id": chunk_id,
                            "ceiling": ceiling,
                            "reason": reason,
                        })
                    elif ceiling == QUALIFIED and binding.get("permission_status") == "bound":
                        evidence_permission_passed = False
                        permission_audit.append({
                            "claim_id": claim_id,
                            "chunk_id": chunk_id,
                            "ceiling": ceiling,
                            "reason": "qualified_material_marked_bound",
                        })
        engineering_passed = (
            all_ids_valid
            and contract_flow
            and bool(relation_audit.get("passed"))
            and relation_atlas_consistent
            and len(phase_run.get("iterations") or []) <= 2
            and full_blueprint_context_passed
            and depth_aggregation_passed
            and claim_pool_integrity_passed
        )
        coverage_request_quality_passed = request_traceable and not duplicate_bundle_categories
        overall_passed = bool(states) and all(
            (
                engineering_passed,
                claim_quality_passed,
                evidence_permission_passed,
                coverage_request_quality_passed,
                task_coverage_passed,
                effective_statement_propagation,
                verifier_batch_budget_passed,
                handoff_ready,
            )
        )
        input_budget_status = (
            "passed"
            if input_budget_passed
            else "warning_exceeds_aggregate_observability_budget"
        )
        return {
            "schema_version": "research_harness.phase3_acceptance.v1",
            "status": "passed" if overall_passed else "failed",
            "r4_entered": False,
            "r4_handoff_ready": handoff_ready and overall_passed,
            "partial_handoff_allowed": handoff_ready and overall_passed,
            "r4_ready_section_ids": r4_ready_section_ids if overall_passed else [],
            "section_outcomes": outcome_by_section,
            "engineering_passed": engineering_passed,
            "claim_quality_passed": claim_quality_passed,
            "evidence_permission_passed": evidence_permission_passed,
            "coverage_request_quality_passed": coverage_request_quality_passed,
            "argument_task_coverage_passed": task_coverage_passed,
            "effective_statement_propagation_passed": effective_statement_propagation,
            "duplicate_bundle_categories_detected": duplicate_bundle_categories,
            "full_blueprint_context_passed": full_blueprint_context_passed,
            "context_value_handoff_passed": context_value_handoff_passed,
            "content_depth_aggregation_passed": depth_aggregation_passed,
            "claim_pool_integrity_passed": claim_pool_integrity_passed,
            "claim_pool_audit": dict(claim_pool_audits),
            "input_budget_passed": input_budget_passed,
            "input_budget_limit": input_budget_limit,
            "input_budget_status": input_budget_status,
            "input_budget_warning": not input_budget_passed,
            "verifier_batch_budget_passed": verifier_batch_budget_passed,
            "r4_handoff_ready_explicit": handoff_ready and overall_passed,
            "permission_audit": permission_audit,
            "engineering_safety": {
                "all_ids_traceable": all_ids_valid,
                "relation_revalidation_passed": bool(relation_audit.get("passed")),
                "old_semantic_edges_downgraded_or_revalidated": (
                    int(relation_audit.get("downgraded_discovery_lead", 0))
                    + int(relation_audit.get("downgraded_unverified_legacy", 0))
                    + int(relation_audit.get("semantic_retained", 0))
                ) == int(relation_audit.get("input_edges", 0)) - int(relation_audit.get("observed_preserved", 0)),
                "coverage_atlas_uses_migrated_relation_graph": relation_atlas_consistent,
                "loop_has_finite_budget": len(phase_run.get("iterations") or []) <= 2,
                "context_value_handoff": blueprint_context.get("context_value_handoff", []),
                "claim_pool_integrity_passed": claim_pool_integrity_passed,
                "passes": engineering_passed,
            },
            "material_quality": {
                "section_status_counts": status_counts,
                "generic_claims_detected": not no_generic,
                "coverage_request_count": len(requests),
                "requests_are_executable_and_section_scoped": request_traceable,
                "query_quality": {
                    "workflow_terms_forbidden": True,
                    "scientific_term_range": "6-15",
                },
                "argument_task_coverage": [
                    {
                        "section_id": state["section"]["section_id"],
                        "tasks": state.get("argument_task_coverage") or [],
                    }
                    for state in states
                ],
                "material_ready_sections": [sid for sid, value in phase_run.get("section_statuses", {}).items() if value == "material_ready"],
                "needs_more_literature_sections": [sid for sid, value in phase_run.get("section_statuses", {}).items() if value == "needs_more_literature"],
                "ready_with_limits_sections": [sid for sid, value in outcome_by_section.items() if value == "ready_with_limits"],
                "merge_required_sections": [sid for sid, value in outcome_by_section.items() if value == "merge_required"],
                "r4_ready_sections": r4_ready_section_ids,
                "handoff_ready": handoff_ready,
                "passes": claim_quality_passed and coverage_request_quality_passed,
            },
            "claim_graph": {
                "status": claim_graph.get("status"),
                "claim_count": len(claim_graph.get("claims") or claim_graph.get("nodes") or []),
                "edge_count": len(claim_graph.get("edges") or []),
                "relation_types": list(ARGUMENT_RELATION_TYPES),
            },
            "coverage_atlas": {
                "path": phase_run.get("coverage_atlas_path", ""),
                "semantic_relation_counts": atlas_relation_counts.get("semantic_relation_counts", {}),
                "semantic_relation_edge_count": atlas_semantic_count,
                "uses_migrated_relation_graph": relation_atlas_consistent,
            },
            "phase2_gap_policy": {
                "minor_gap_policy": "write_with_declared_gap",
                "load_bearing_gap_policy": "fail_open_when_authorable_backbone_exists",
                "r4_handoff_rule": "sections with any authorable claims may enter R4 with declared claim-level gaps; sections without an authorable backbone remain explicit gaps",
            },
            "cost": {
                "s2_calls": 0,
                "qwen_calls": int(llm_summary.get("calls_observed_or_estimated", 0)),
                "input_tokens_observed": int(llm_summary.get("input_tokens_observed", 0)),
                "output_tokens_observed": int(llm_summary.get("output_tokens_observed", 0)),
                "estimated_input_tokens_total": int(llm_summary.get("estimated_input_tokens_total", 0)),
                "estimated_output_tokens_total": int(llm_summary.get("estimated_output_tokens_total", 0)),
                "estimated_cost_cny": float(llm_summary.get("estimated_cost_cny", 0.0) or 0.0),
                "per_model": llm_summary.get("per_model", {}),
                "max_batch_estimated_input_tokens": int(llm_summary.get("max_batch_estimated_input_tokens", 0)),
                "usage_is_provider_reported": bool(llm_summary.get("usage_is_provider_reported")),
                "token_count_source": llm_summary.get("token_count_source", "unavailable"),
                "metric_provenance": llm_summary.get("metric_provenance", {
                    "input_tokens": "unavailable",
                    "output_tokens": "unavailable",
                    "cost_cny": "estimated",
                }),
                "offline_run": not bool(llm_summary.get("calls_observed_or_estimated", 0)),
                "runtime_failure_count": sum(
                    1 for state in states if state.get("runtime_failure")
                ),
            },
        }

    def _write_markdown(self, acceptance: dict[str, Any], phase_run: dict[str, Any]) -> None:
        lines = [
            "# Phase 3 Argument and Material Orchestration Acceptance",
            "",
            f"- Status: **{acceptance.get('status')}**",
            "- R4 entered: **no**",
            f"- R4 handoff ready: **{acceptance.get('r4_handoff_ready')}**",
            f"- Engineering passed: **{acceptance.get('engineering_passed')}**",
            f"- Claim quality passed: **{acceptance.get('claim_quality_passed')}**",
            f"- Evidence permission passed: **{acceptance.get('evidence_permission_passed')}**",
            f"- Coverage request quality passed: **{acceptance.get('coverage_request_quality_passed')}**",
            f"- Engineering safety: **{acceptance.get('engineering_safety', {}).get('passes')}**",
            f"- Material quality: **{acceptance.get('material_quality', {}).get('passes')}**",
            f"- Sections: `{json.dumps(phase_run.get('section_statuses', {}), ensure_ascii=False)}`",
            f"- Coverage requests: `{acceptance.get('material_quality', {}).get('coverage_request_count', 0)}`",
            f"- Claim graph: `{acceptance.get('claim_graph', {}).get('status')}`",
            "",
            "The phase stops before writing. Sections without real claims or load-bearing material remain needs_more_literature; they are not promoted by placeholder text.",
        ]
        (self.output_dir / "PHASE3_ACCEPTANCE.md").write_text("\n".join(lines), encoding="utf-8")


__all__ = [
    "ARGUMENT_RELATION_TYPES",
    "CLAIM_CLASSIFICATIONS",
    "SECTION_OUTCOMES",
    "CoverageRequest",
    "adapt_claim_for_partial_coverage",
    "classify_claim_support",
    "compile_coverage_queries",
    "Phase3ArgumentOrchestrator",
    "SectionArgumentContract",
]
