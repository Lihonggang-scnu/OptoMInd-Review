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

import ast
import json
import hashlib
import logging
import re
import sqlite3
import time
import uuid
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


def _mounting_load_receipts(path: Path) -> dict[str, dict[str, Any]]:
    """Read a scope-decision log into a document_id -> receipt mapping."""

    receipts: dict[str, dict[str, Any]] = {}
    if not Path(path).is_file():
        return receipts
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping):
            continue
        document_id = _text(row.get("document_id") or row.get("paper_id"))
        if document_id:
            receipts[document_id] = dict(row)
    return receipts


def _sha256_of_file(path: Path) -> str:
    """sha256 of a file's bytes, streamed so a large artifact is safe to hash."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


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
        # Document identity hints.  The canonical graph reads the KB row's own
        # DOI column; without it a served record cannot be joined to the scope
        # receipt or to a locally downloaded source, and the evidence-first
        # producer would have to reject it for lack of a source identity.
        "doi": _text(
            (chunk.source_locator or {}).get("doi")
            or (chunk.route_provenance or {}).get("doi")
            or "",
            200,
        ),
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
        generation_id: str | None = None,
        attempt_id: str | None = None,
        node_runtime_options: Mapping[str, Any] | None = None,
        scoped_runtime_kb: Path | None = None,
        source_base_kb: Path | None = None,
        evidence_first_atoms: bool = False,
        evidence_atom_record_limit: int = 0,
        evidence_artifact_dir: Path | None = None,
        material_acquisition_path: Path | None = None,
        evidence_atomiser_call_limit: int = 0,
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
        # Evidence-first atom production is opt-in: a caller that has not asked for
        # it gets explicit unavailable receipts instead of a silent empty result.
        self.evidence_first_atoms = bool(evidence_first_atoms)
        # The atom working set is a producer parameter, not a hidden constant: a
        # slice with more resolvable documents must be able to raise it, and the
        # value is fingerprinted with the node so the change is visible.
        self.evidence_atom_record_limit = int(evidence_atom_record_limit or 0)
        self._configured_evidence_artifact_dir = (
            Path(evidence_artifact_dir) if evidence_artifact_dir else None
        )
        # SM10 reopen: material may be acquired for a role by meaning as well as
        # read out of the ledger.  The acquisition is registered at the start of
        # the run and only when its manifest is true of the knowledge base it
        # names, so a manifest can never widen the material by assertion.
        self.material_acquisition_path = (
            Path(material_acquisition_path) if material_acquisition_path else None
        )
        # Zero means "derive it from the target": a caller only overrides this to
        # bound cost below what the target needs, never to raise it.
        self.evidence_atomiser_call_limit = int(evidence_atomiser_call_limit or 0)
        self._phase3_material_acquisition_state: dict[str, Any] = {
            "status": "not_requested"
        }
        self.overlay_paths = {str(key): Path(value) for key, value in (overlay_paths or {}).items()}
        self.mentor_advice = dict(mentor_advice or {})
        self.mentor_library_path = Path(mentor_library_path) if mentor_library_path else None
        self.output_dir = Path(output_dir or PROJECT_ROOT / "outputs" / "phase3_argument_orchestration")
        # Where the evidence-first producers write.  In a formal run the Phase-3
        # node commit owns the root filenames, so the producers write into a
        # staging directory and the committed node projection is the only writer
        # of the root.  A direct caller that is not driving a node keeps the old
        # behaviour by leaving this at the root.
        self.evidence_artifact_dir = (
            self._configured_evidence_artifact_dir or self.output_dir
        )
        self._configured_generation_id = str(generation_id or "").strip()
        self.generation_id = str(
            self._configured_generation_id
            or self.output_dir.parent.name
            or "phase3"
        )
        self._configured_attempt_id = str(attempt_id or "").strip()
        self.attempt_id = self._configured_attempt_id or ""
        self.node_runtime_options = dict(node_runtime_options or {})
        self.scoped_runtime_kb = (
            Path(scoped_runtime_kb) if scoped_runtime_kb else None
        )
        self.source_base_kb = Path(source_base_kb) if source_base_kb else None
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

    # ------------------------------------------------------------------ #
    #  Evidence-first P3A atom production (upgrade3 W-R13)                #
    # ------------------------------------------------------------------ #

    def _p3a_atomic_claim_payloads(self) -> dict[str, Any]:
        """Read the atomic-claim artifacts for this P3A node.

        Same contract as the atom artifacts: with the producer enabled a missing or
        tampered set fails the node closed; with it disabled the node commits
        explicit unavailable receipts so P3B can never mistake absence for input.
        """

        from optomind_research.runtime.upgrade3 import atomic_claims as _atomic_claims

        out = self.evidence_artifact_dir
        claims_path = out / _atomic_claims.CLAIMS_FILENAME
        rejections_path = out / _atomic_claims.REJECTIONS_FILENAME
        manifest_path = out / _atomic_claims.MANIFEST_FILENAME
        complete = (
            claims_path.is_file()
            and rejections_path.is_file()
            and manifest_path.is_file()
        )
        if not complete:
            if getattr(self, "evidence_first_atoms", False):
                raise RuntimeError("atomic_claim_artifacts_missing")
            return {
                _atomic_claims.CLAIMS_FILENAME: "",
                _atomic_claims.REJECTIONS_FILENAME: "",
                _atomic_claims.MANIFEST_FILENAME: {
                    "schema_version": _atomic_claims.MANIFEST_SCHEMA,
                    "status": "unavailable",
                    "reason": "atomic_claim_producer_disabled",
                    "authorable_claims": 0,
                    "binding_required": True,
                    "selected_claims": [],
                    "counts": {"selected_claims": 0, "rejections": 0},
                },
            }
        _atomic_claims.load_claim_manifest(manifest_path)
        return {
            _atomic_claims.CLAIMS_FILENAME: claims_path.read_text(encoding="utf-8"),
            _atomic_claims.REJECTIONS_FILENAME: rejections_path.read_text(encoding="utf-8"),
            _atomic_claims.MANIFEST_FILENAME: json.loads(
                manifest_path.read_text(encoding="utf-8")
            ),
        }

    def _atomic_claims_for_attempt(self) -> list[dict[str, Any]]:
        """The claims this attempt produced, for the P3B binder to consume."""

        from optomind_research.runtime.upgrade3 import atomic_claims as _atomic_claims

        path = self.evidence_artifact_dir / _atomic_claims.CLAIMS_FILENAME
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, Mapping):
                rows.append(dict(row))
        return rows

    def _phase3_scope_decisions_path(self) -> Path:
        """Locate this generation's scope-decision receipt log."""

        candidates = [
            self.output_dir.parent / "topic_scoped_kb" / "upgrade3_scope_decisions.jsonl",
            self.output_dir.parent / "s2_literature_intelligence" / "upgrade3_scope_decisions.jsonl",
            self.output_dir / "upgrade3_scope_decisions.jsonl",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0]

    def _phase3_resolver(self) -> Any:
        """Return the document resolver this attempt registers versions in."""

        resolver = getattr(self, "_evidence_resolver", None)
        if resolver is None:
            from optomind_research.runtime.upgrade3.source_resolver import (
                DocumentResolver,
            )

            work_dir = self.output_dir / "evidence"
            work_dir.mkdir(parents=True, exist_ok=True)
            resolver = DocumentResolver(
                str(work_dir / "DOCUMENT_VERSIONS.json"),
                str(work_dir / "SPAN_INDEX.json"),
            )
            self._evidence_resolver = resolver
        return resolver

    def _phase3_evidence_records(self, states: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collect the served material records the claim pool actually saw."""

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for state in states:
            section_id = str((state.get("section") or {}).get("section_id") or "")
            for record in state.get("claim_pool_records") or ():
                if not isinstance(record, Mapping):
                    continue
                chunk_id = str(record.get("chunk_id") or "")
                if not chunk_id or chunk_id in seen:
                    continue
                seen.add(chunk_id)
                row = dict(record)
                row["section_id"] = section_id
                records.append(row)
        return records

    def _evidence_policy_sha256(self) -> str:
        """The policy identity this attempt stamps on its evidence objects."""

        try:
            from optomind_research.runtime.upgrade3 import wiring as _wiring

            value = str(getattr(_wiring, "DEFAULT_POLICY_HASH", "") or "")
        except Exception:
            value = ""
        # The envelope contract requires a 64-hex sha256; a human-readable policy
        # label is folded to its digest rather than being passed through.
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _evidence_material_snapshot_hash(self) -> str:
        """Hash of the material this attempt is allowed to reason about.

        The snapshot is built from the actual input artifacts (blueprint, shared
        ledger, knowledge bases, overlays), so a material change moves the atom
        envelope instead of being invisible to it.
        """

        rows: list[dict[str, str]] = []
        for role, path in sorted(self.overlay_paths.items()):
            rows.append({"role": "overlay:%s" % role, "path": str(path)})
        for path in self.shared_kb_paths:
            rows.append({"role": "kb", "path": str(path)})
        if self.shared_ledger_path is not None:
            rows.append({"role": "shared_ledger", "path": str(self.shared_ledger_path)})
        digest = hashlib.sha256()
        for row in rows:
            path = Path(row["path"])
            digest.update(row["role"].encode("utf-8"))
            digest.update(b"\0")
            if path.is_file():
                file_digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1 << 20), b""):
                        file_digest.update(block)
                digest.update(file_digest.hexdigest().encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _phase3_ledger_source_index(self) -> dict[str, dict[str, Any]]:
        """paper_id -> the role/section/scope this run's own ledger registered.

        Read-only provenance: the ledger says what a paper IS, never what it is
        allowed to support.
        """

        index: dict[str, dict[str, Any]] = {}
        path = Path(self.shared_ledger_path) if self.shared_ledger_path else None
        if path is None or not path.is_file():
            return index
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return index
        for row in (payload or {}).get("sources") or ():
            if not isinstance(row, Mapping):
                continue
            paper_id = _text(row.get("paper_id"))
            if not paper_id:
                continue
            entry = index.setdefault(paper_id, {})
            for key in ("literature_role", "section_id", "scope_fit",
                        "use_permission", "content_depth",
                        "retrieval_backend", "retrieval_query"):
                if _text(row.get(key)) and not _text(entry.get(key)):
                    entry[key] = _text(row.get(key))
            # A paper may legitimately be registered under more than one role:
            # it can be acquired for a section that names one role and again for
            # a section that names another.  Keeping only the first role made the
            # paper un-hostable by the second section even though the material
            # for it was in the library.
            role = _text(row.get("literature_role"))
            roles = entry.setdefault("literature_roles", [])
            if role and role not in roles:
                roles.append(role)
            provenance = row.get("role_provenance")
            if isinstance(provenance, Mapping) and provenance:
                entry.setdefault("role_provenance", dict(provenance))
        return index

    def _register_material_acquisition(self) -> dict[str, Any]:
        """Admit an acquisition's knowledge base only if its manifest is true.

        A role-targeted acquisition is a claim about material: "these chunks
        carry this role in this section".  Before the knowledge base it names
        joins the run's inputs it must hold exactly those chunks, byte for byte.
        An unverified acquisition contributes nothing and is reported, so the
        semantic channel can widen what the review may read but never what it
        may assert.
        """

        path = self.material_acquisition_path
        if path is None:
            return {"status": "not_requested"}
        from optomind_research.runtime.upgrade3 import role_acquisition as _acquisition

        manifest = _acquisition.load_acquisition(path)
        if not manifest:
            return {"status": "manifest_missing", "path": str(path)}
        kb_block = manifest.get("knowledge_base")
        kb_path = Path(_text((kb_block or {}).get("path"))) if isinstance(
            kb_block, Mapping) else Path("")
        candidates = [kb_path] if str(kb_path) not in ("", ".") else []
        verification = _acquisition.verify_acquisition(manifest, kb_paths=candidates)
        registered = False
        if verification.get("verified") and kb_path.is_file():
            if kb_path not in self.shared_kb_paths:
                self.shared_kb_paths = [*self.shared_kb_paths, kb_path]
            registered = True
        return {
            "status": "registered" if registered else "refused",
            "path": str(path),
            "knowledge_base": str(kb_path) if str(kb_path) not in ("", ".") else "",
            "verification": verification,
            "sections": sorted({
                _text(row.get("section_id"))
                for row in manifest.get("assignments") or ()
                if _text(row.get("section_id"))
            }),
            "roles": sorted({
                "%s:%s" % (_text(row.get("section_id")), _text(row.get("role")))
                for row in manifest.get("assignments") or ()
            }),
            "unmet_roles": list(manifest.get("unmet_roles") or ()),
            "assignments": len(manifest.get("assignments") or ()),
            "topic": _text(manifest.get("topic")),
            "embedding_model": _text(manifest.get("embedding_model")),
            "representation_version": _text(manifest.get("representation_version")),
            "cache_units": manifest.get("cache_units"),
        }

    def _phase3_material_scope(self, states: list[dict[str, Any]]) -> dict[str, Any]:
        """The generation-global material, and the part of it a section served.

        The unmounted pool is a statement about the whole material set: a record
        that a section served is mounted by definition, so a pool built from the
        served records alone is provably empty.  The global set is therefore read
        from the registered knowledge bases, annotated with the ledger's own
        role/section provenance, and the served records are overlaid on top of it
        because they carry the richer runtime provenance.
        """

        from optomind_research.runtime.upgrade3 import evidence_mounting as _mounting

        served_records = self._phase3_evidence_records(states)
        served_chunk_ids = {_text(row.get("chunk_id")) for row in served_records}
        served_document_ids = {_text(row.get("paper_id")) for row in served_records}
        ledger_index = self._phase3_ledger_source_index()
        registered = _mounting.read_registered_chunks(
            self.shared_kb_paths, ledger_index=ledger_index
        )
        merged: dict[str, dict[str, Any]] = {}
        for row in registered:
            merged[_text(row.get("chunk_id"))] = dict(row)
        for row in served_records:
            merged[_text(row.get("chunk_id"))] = dict(row)
        records = [merged[key] for key in sorted(merged) if key]
        return {
            "records": records,
            "served_records": served_records,
            "served_chunk_ids": sorted(item for item in served_chunk_ids if item),
            "served_document_ids": sorted(item for item in served_document_ids if item),
            "registered_chunks": len(registered),
            "ledger_sources": len(ledger_index),
            "material_acquisition": dict(self._phase3_material_acquisition_state),
        }

    def _produce_mounting_artifacts(self, states: list[dict[str, Any]]) -> dict[str, Any]:
        """Domain admission, the global unmounted pool and mount proposals."""

        from optomind_research.runtime.upgrade3 import evidence_mounting as _mounting

        scope = self._phase3_material_scope(states)
        self._phase3_material_scope_cache = scope
        records = scope["records"]
        run_dir = self.output_dir.parent
        scope_log = self._phase3_scope_decisions_path()
        receipts = _mounting_load_receipts(scope_log)
        contract_path = run_dir / "DOMAIN_CONTRACT.json"
        contract_hash = ""
        if contract_path.is_file():
            try:
                payload = json.loads(contract_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            contract_hash = _text((payload.get("envelope") or {}).get("content_sha256"))
        sections = [
            {
                "section_id": _text((state.get("section") or {}).get("section_id")),
                "literature_roles": list(
                    (state.get("section") or {}).get("literature_roles") or []
                ),
                "required_roles": list(
                    (state.get("section") or {}).get("required_roles") or []
                ),
                "comparison_axes": list(
                    (state.get("section") or {}).get("comparison_axes") or []
                ),
            }
            for state in states
        ]
        admission = _mounting.admit_material(
            records,
            scope_receipts=receipts,
            domain_contract_hash=contract_hash,
            domain_contract_source=str(contract_path),
        )
        # Mounting is decided per chunk: a paper that a section used for some of
        # its text is not thereby "mounted" for the rest of it, and excluding a
        # whole document because one of its chunks was served would hide exactly
        # the material the pool exists to keep.
        pool = _mounting.build_unmounted_pool(
            admission["admitted"],
            mounted_chunk_ids=scope["served_chunk_ids"],
            generation_id=self.generation_id,
        )
        proposals = _mounting.propose_mounts(
            pool, sections=sections, generation_id=self.generation_id,
        )
        return _mounting.write_mounting_artifacts(
            output_dir=self.evidence_artifact_dir,
            pool=pool,
            proposals=proposals,
            generation_id=self.generation_id,
            material_authority={
                "name": "phase3_served_material",
                "identity_sha256": self._evidence_material_snapshot_hash(),
                "chunk_count": len(records),
            },
            material_derived={
                "name": "shared_kb_paths",
                "identity_sha256": ";".join(
                    sorted(str(Path(item)) for item in self.shared_kb_paths)
                ),
                "chunk_count": 0,
            },
            reconciliation={
                "schema_version": "optomind.upgrade3.material_reconciliation.v1",
                "note": "the served material is one revision; the KB comparison is"
                        " performed by the ticket that owns the snapshot audit",
                "unexplained_differences": [],
                "explained_percent": 100.0,
            },
            material_scope={
                "generation_global_records": len(records),
                "registered_chunks": scope["registered_chunks"],
                "ledger_sources": scope["ledger_sources"],
                "served_records": len(scope["served_records"]),
                "served_chunks": len(scope["served_chunk_ids"]),
                "served_documents": len(scope["served_document_ids"]),
                "admitted": admission["admitted_count"],
                "refused": admission["refused_count"],
            },
        )

    def _p3a_mounting_payloads(self) -> dict[str, Any]:
        """Read the mounting artifacts for this node, or explicit unavailable ones."""

        from optomind_research.runtime.upgrade3 import evidence_mounting as _mounting

        out = self.evidence_artifact_dir
        pool_path = out / _mounting.POOL_FILENAME
        proposals_path = out / _mounting.PROPOSALS_FILENAME
        manifest_path = out / _mounting.MANIFEST_FILENAME
        complete = (
            pool_path.is_file() and proposals_path.is_file()
            and manifest_path.is_file()
        )
        if not complete:
            if getattr(self, "evidence_first_atoms", False):
                raise RuntimeError("evidence_mounting_artifacts_missing")
            return {
                _mounting.POOL_FILENAME: "",
                _mounting.PROPOSALS_FILENAME: "",
                _mounting.MANIFEST_FILENAME: {
                    "schema_version": _mounting.POOL_MANIFEST_SCHEMA,
                    "status": "unavailable",
                    "reason": "evidence_mounting_disabled",
                    "counts": {"pool": 0, "proposals": 0},
                },
            }
        return {
            _mounting.POOL_FILENAME: pool_path.read_text(encoding="utf-8"),
            _mounting.PROPOSALS_FILENAME: proposals_path.read_text(encoding="utf-8"),
            _mounting.MANIFEST_FILENAME: json.loads(
                manifest_path.read_text(encoding="utf-8")
            ),
        }

    def _phase3_served_by_section(
        self, states: list[dict[str, Any]]
    ) -> dict[str, list[str]]:
        """Which chunks each section actually served into its claim pool."""

        served: dict[str, list[str]] = {}
        for state in states:
            section_id = _text((state.get("section") or {}).get("section_id"))
            if not section_id:
                continue
            bucket = served.setdefault(section_id, [])
            for record in state.get("claim_pool_records") or ():
                chunk_id = _text(record.get("chunk_id")) if isinstance(
                    record, Mapping) else ""
                if chunk_id and chunk_id not in bucket:
                    bucket.append(chunk_id)
        return served

    def _produce_blueprint_revision(self, states: list[dict[str, Any]]) -> dict[str, Any]:
        """The literature's half: one frozen blueprint revision per attempt.

        It consumes the committed mounting artifacts and the real served/global
        split, and it produces the three revision artifacts atomically.  It never
        grants a permission and it never removes a question-required slot.
        """

        from optomind_research.runtime.upgrade3 import blueprint_revision as _revision
        from optomind_research.runtime.upgrade3 import evidence_mounting as _mounting

        scope = getattr(self, "_phase3_material_scope_cache", None)
        if not isinstance(scope, Mapping):
            scope = self._phase3_material_scope(states)
            self._phase3_material_scope_cache = scope
        pool = _mounting.load_pool(
            self.evidence_artifact_dir / _mounting.POOL_FILENAME)
        proposals = _mounting.load_pool(
            self.evidence_artifact_dir / _mounting.PROPOSALS_FILENAME
        )
        contract: dict[str, Any] = {}
        contract_path = self.output_dir.parent / "DOMAIN_CONTRACT.json"
        if contract_path.is_file():
            try:
                loaded = json.loads(contract_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, Mapping):
                contract = dict(loaded)
        hard_slots = _revision.extract_hard_slots(
            blueprint=self.blueprint, domain_contract=contract
        )
        bundle = _revision.build_revision(
            blueprint=self.blueprint,
            hard_slots=hard_slots,
            pool=pool,
            proposals=proposals,
            mounted_chunk_ids=scope["served_chunk_ids"],
            mounted_document_ids=scope["served_document_ids"],
            served_by_section=self._phase3_served_by_section(states),
            generation_id=self.generation_id,
            domain_contract_hash=_text(
                (contract.get("envelope") or {}).get("content_sha256")
                if isinstance(contract.get("envelope"), Mapping)
                else ""
            ),
        )
        manifest = _revision.write_revision_artifacts(
            output_dir=self.evidence_artifact_dir,
            bundle=bundle,
            source_hashes={
                "pool": _sha256_of_file(
                    self.evidence_artifact_dir / _mounting.POOL_FILENAME)
                if (self.evidence_artifact_dir / _mounting.POOL_FILENAME).is_file()
                else "",
                "proposals": _sha256_of_file(
                    self.evidence_artifact_dir / _mounting.PROPOSALS_FILENAME)
                if (self.evidence_artifact_dir
                    / _mounting.PROPOSALS_FILENAME).is_file() else "",
                "blueprint": hashlib.sha256(
                    json.dumps(self.blueprint, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), default=str).encode("utf-8")
                ).hexdigest(),
            },
        )
        self._blueprint_revision_hash = _text(bundle.get("revision_hash"))
        return {"bundle": bundle, "manifest": manifest["manifest"]}

    def _phase3_ensure_scope_receipts(
        self, states: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Real scope receipts for the material BEFORE any admission decision.

        The pool, the revision and the mounts all ask "is this in domain?".  If the
        receipts are made later, admission falls back to a chunk's acquisition
        label and material the real judge would refuse can enter the pool.  The
        judgement therefore happens first, over the generation-global material, and
        a missing judge leaves the documents without a receipt rather than with a
        permissive one.
        """

        from optomind_research.runtime.upgrade3 import evidence_first as _evidence_first

        scope = getattr(self, "_phase3_material_scope_cache", None)
        if not isinstance(scope, Mapping):
            scope = self._phase3_material_scope(states)
            self._phase3_material_scope_cache = scope
        records = scope["records"]
        if not records:
            return {"status": "no_records", "judged": 0}
        contract: dict[str, Any] = {}
        try:
            from optomind_research.runtime.upgrade3.wiring import load_domain_contract

            loaded = load_domain_contract(self.output_dir.parent)
            if isinstance(loaded, Mapping):
                contract = dict(loaded)
        except Exception:
            contract = {}
        resolver = self._phase3_resolver()
        for kb_path in self.shared_kb_paths:
            path = Path(kb_path)
            if path.is_file():
                resolver.register_kb(path.parent.name or path.stem, str(path))
        try:
            judge = self._phase3_scope_judge()
        except Exception:
            judge = None
        return _evidence_first.ensure_scope_receipts(
            records,
            resolver=resolver,
            domain_contract=contract,
            scope_decisions_path=self._phase3_scope_decisions_path(),
            source_roots=[self.output_dir.parent],
            judge=judge,
            record_limit=self.evidence_atom_record_limit
            or _evidence_first.DEFAULT_ATOM_RECORD_LIMIT,
        )

    def _phase3_apply_revision_mounts(
        self, states: list[dict[str, Any]], bundle: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Serve the material the frozen revision mounted, before the claims.

        Only real, in-domain material is admitted, only into the sections the
        revision declares, and never past a section's own serving limit.  The
        decision is written next to the revision so the node carries it.
        """

        from optomind_research.runtime.upgrade3 import blueprint_revision as _revision

        scope = getattr(self, "_phase3_material_scope_cache", None)
        if not isinstance(scope, Mapping):
            scope = self._phase3_material_scope(states)
            self._phase3_material_scope_cache = scope
        # The domain decision is the run's own scope receipt, read from the same
        # log admission reads: the served library labelled a hydrology paper
        # "direct" while the real judge refused it.
        application = _revision.apply_revision_mounts(
            revision=bundle,
            served_chunk_ids_by_section=self._phase3_served_by_section(states),
            available_records=scope["records"],
            limit_per_section=self.claim_pool_served_limit,
            scope_receipts=_mounting_load_receipts(self._phase3_scope_decisions_path()),
        )
        if application.get("added_by_section"):
            by_chunk = {
                _text(row.get("chunk_id")): row for row in scope["records"]
            }
            served = application.get("served_chunk_ids_by_section") or {}
            for state in states:
                section_id = _text(
                    (state.get("section") or {}).get("section_id"))
                wanted = served.get(section_id)
                if not wanted:
                    continue
                existing = {
                    _text(row.get("chunk_id")): row
                    for row in state.get("claim_pool_records") or ()
                }
                rebuilt = []
                for chunk_id in wanted:
                    record = existing.get(chunk_id) or by_chunk.get(chunk_id)
                    if record is not None:
                        rebuilt.append(record)
                state["claim_pool_records"] = rebuilt
        self._revision_mount_application = application
        _write_json(
            self.evidence_artifact_dir / "REVISION_MOUNT_APPLICATION.json",
            application,
        )
        return application

    def _p3a_blueprint_revision_payloads(self) -> dict[str, Any]:
        """Read the revision artifacts, or the explicit unavailable receipt."""

        from optomind_research.runtime.upgrade3 import blueprint_revision as _revision

        names = (
            _revision.PROPOSAL_FILENAME,
            _revision.MOUNTING_FILENAME,
            _revision.REVISION_FILENAME,
        )
        paths = {name: self.evidence_artifact_dir / name for name in names}
        if not all(path.is_file() for path in paths.values()):
            if getattr(self, "evidence_first_atoms", False):
                raise RuntimeError("blueprint_revision_artifacts_missing")
            return _revision.unavailable_payloads(
                reason="blueprint_revision_disabled")
        payloads: dict[str, Any] = {}
        for name, path in paths.items():
            try:
                payloads[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "blueprint_revision_artifact_unreadable:%s:%s" % (name, exc)
                )
        application_path = (
            self.evidence_artifact_dir / "REVISION_MOUNT_APPLICATION.json"
        )
        if application_path.is_file():
            try:
                payloads["REVISION_MOUNT_APPLICATION.json"] = json.loads(
                    application_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "revision_mount_application_unreadable:%s" % exc)
        return payloads

    def _phase3_budget_task_id(self) -> str:
        """The budget task this run charges (env-overridable, handoff default)."""

        try:
            from optomind_research.runtime.upgrade3 import wiring as _wiring

            return str(_wiring.run_budget_task_id())
        except Exception:
            return "031"

    def _phase3_claim_atomiser(self) -> Any:
        """The bounded, real atomiser used to rewrite claim statements.

        It goes through the same production transport and budget ledger as every
        other paid call, takes one batch per logical call, and returns the parsed
        object verbatim: validation of what the model said happens in the claim
        producer, not here, so a bad rewrite is a rejection and never a repair.
        """

        atomiser = getattr(self, "_evidence_claim_atomiser", None)
        if atomiser is not None:
            return atomiser
        try:
            from optomind_research.runtime.upgrade3 import wiring as _wiring

            transport = _wiring.build_transport()
            keys = _wiring.qwen_key_candidates()
            base_url = _wiring.qwen_base_url()
        except Exception:
            return None
        if not keys:
            return None
        key_index = {"i": 0}

        def call(
            system_prompt,
            user_payload,
            *,
            task_id,
            logical_call_id,
            generation_id,
            attempt=1,
        ):
            import json as _json

            from optomind_research.runtime.upgrade3.scope_screening import (
                _strip_json_fence,
            )

            last_error: Exception | None = None
            for _pass in range(max(1, len(keys))):
                try:
                    response = transport.chat_completion(
                        task_id=task_id,
                        logical_call_id=logical_call_id,
                        generation_id=generation_id,
                        model="qwen3.7-flash",
                        system_prompt=system_prompt,
                        user_payload=user_payload,
                        api_key=keys[key_index["i"] % len(keys)],
                        base_url=base_url,
                        schema=user_payload.get("output_schema") or {},
                        # Bounds measured from the first real call: the model
                        # reads the atom slice and writes its reasoning plus the
                        # compact statement, so the output bound has to cover the
                        # reasoning tokens (measured 4626 completion tokens for a
                        # two-item batch).
                        max_input_tokens=4000,
                        max_output_tokens=8000,
                        temperature=0.0,
                    )
                except Exception as exc:
                    last_error = exc
                    key_index["i"] += 1
                    continue
                content = response.get("content")
                parsed = None
                if isinstance(content, str):
                    try:
                        parsed = _json.loads(_strip_json_fence(content))
                    except Exception:
                        parsed = None
                elif isinstance(content, Mapping):
                    parsed = dict(content)
                if not isinstance(parsed, Mapping):
                    raise RuntimeError("atomiser_response_unparseable")
                out = dict(parsed)
                out["receipt"] = dict(response.get("receipt") or {})
                return out
            if last_error is not None:
                raise last_error
            raise RuntimeError("atomiser_no_key_available")

        self._evidence_claim_atomiser = call
        return call

    def _phase3_entailment_judge(self) -> Any:
        """The bounded batch entailment judge used by the evidence-first P3B.

        One physical call per batch, through the production transport and the 005
        ledger.  It returns the parsed object verbatim: validating that the batch
        answers exactly the requested pair ids is the producer job, so a bad batch
        is rejected rather than repaired here.
        """

        judge = getattr(self, "_evidence_entailment_judge", None)
        if judge is not None:
            return judge
        try:
            from optomind_research.runtime.upgrade3 import wiring as _wiring

            transport = _wiring.build_transport()
            keys = _wiring.qwen_key_candidates()
            base_url = _wiring.qwen_base_url()
        except Exception:
            return None
        if not keys:
            return None
        key_index = {"i": 0}

        def call(
            system_prompt,
            user_payload,
            *,
            task_id,
            logical_call_id,
            generation_id,
        ):
            import json as _json

            from optomind_research.runtime.upgrade3.scope_screening import (
                _strip_json_fence,
            )

            last_error: Exception | None = None
            for _pass in range(max(1, len(keys))):
                try:
                    response = transport.chat_completion(
                        task_id=task_id,
                        logical_call_id=logical_call_id,
                        generation_id=generation_id,
                        model="qwen3.7-flash",
                        system_prompt=system_prompt,
                        user_payload=user_payload,
                        api_key=keys[key_index["i"] % len(keys)],
                        base_url=base_url,
                        schema=user_payload.get("output_schema") or {},
                        # Bounds measured on the first real batch: a 12-pair batch
                        # costs roughly 4k prompt tokens and up to 6k completion
                        # tokens for qwen3.7-flash, whose reasoning tokens bill as
                        # completion.
                        max_input_tokens=8000,
                        max_output_tokens=8000,
                        temperature=0.0,
                    )
                except Exception as exc:
                    last_error = exc
                    key_index["i"] += 1
                    continue
                content = response.get("content")
                parsed = None
                if isinstance(content, str):
                    try:
                        parsed = _json.loads(_strip_json_fence(content))
                    except Exception:
                        parsed = None
                elif isinstance(content, Mapping):
                    parsed = dict(content)
                if not isinstance(parsed, Mapping):
                    raise RuntimeError("entailment_response_unparseable")
                out = dict(parsed)
                out["receipt"] = dict(response.get("receipt") or {})
                return out
            if last_error is not None:
                raise last_error
            raise RuntimeError("entailment_no_key_available")

        self._evidence_entailment_judge = call
        return call

    def _phase3_scope_judge(self) -> Any:
        """Build the real scope judge this run uses for missing receipts.

        It is the same production :class:`ScopeAdmission` the topic-scoped KB stage
        uses, so a receipt produced here is a receipt the run really paid for and
        can be replayed through the scope-decision log.
        """

        judge = getattr(self, "_evidence_scope_judge", None)
        if judge is not None:
            return judge
        try:
            from optomind_research.runtime.upgrade3 import wiring as _wiring

            admission = _wiring.build_scope_admission(
                run_dir=self.output_dir.parent,
                work_dir=self.output_dir.parent / "topic_scoped_kb",
                task_id=_wiring.run_budget_task_id(),
                generation_id=self.generation_id,
                policy_hash=getattr(self, "upgrade3_policy_hash", "") or "upgrade3-default-policy-v1",
            )
        except Exception:
            admission = None
        if admission is None:
            raise RuntimeError("upgrade3_scope_admission_unavailable")
        self._evidence_scope_judge = admission
        return admission

    def _produce_p3a_evidence_atoms(
        self,
        states: list[dict[str, Any]],
        *,
        scope_judge: Any = None,
        atomiser: Any = None,
        record_limit: int | None = None,
    ) -> dict[str, Any]:
        """Build canonical Evidence Atoms for this P3A attempt.

        The atoms are the only evidence objects the atomic-claim producer may
        read.  Every receipt attached to an atom comes from an object of this
        generation: the frozen domain contract, the run's scope-decision log and
        the registered owner row.  A record that cannot supply all of them is
        rejected instead of downgraded.
        """

        from optomind_research.runtime.upgrade3 import evidence_first as _evidence_first

        records = self._phase3_evidence_records(states)
        run_dir = self.output_dir.parent
        resolver = self._phase3_resolver()
        for kb_path in self.shared_kb_paths:
            path = Path(kb_path)
            if path.is_file():
                resolver.register_kb(path.parent.name or path.stem, str(path))
        contract = _u3_contract = None
        try:
            from optomind_research.runtime.upgrade3.wiring import load_domain_contract

            contract = load_domain_contract(run_dir)
        except Exception:
            contract = None
        contract_path = run_dir / "DOMAIN_CONTRACT.json"
        scope_path = self._phase3_scope_decisions_path()
        limit = int(record_limit or _evidence_first.DEFAULT_ATOM_RECORD_LIMIT)
        # Roles come from the section source ledger, which assigns a literature
        # role to each admitted paper.  They are provenance for the atom, not a
        # permission: the permission ceiling is still computed from the source
        # depth, the scope receipt and the section overlay.
        role_by_paper: dict[str, list[str]] = {}
        ledger_path = (
            Path(self.shared_ledger_path) if self.shared_ledger_path else None
        )
        if ledger_path is not None and ledger_path.is_file():
            try:
                ledger_payload = json.loads(ledger_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                ledger_payload = {}
            for row in (ledger_payload or {}).get("sources") or ():
                if not isinstance(row, Mapping):
                    continue
                role_by_paper.setdefault(
                    _text(row.get("paper_id")), []
                ).append(_text(row.get("literature_role")))
        for state in states:
            section = state.get("section") or {}
            for item in section.get("sources") or ():
                if not isinstance(item, Mapping):
                    continue
                role_by_paper.setdefault(
                    str(item.get("paper_id") or ""), []
                ).append(str(item.get("literature_role") or ""))
        for record in records:
            record["roles"] = sorted({
                role for role in role_by_paper.get(
                    _text(record.get("paper_id")), []
                ) if role
            })
        if scope_judge is None and records:
            try:
                scope_judge = self._phase3_scope_judge()
            except Exception:
                # No judge means no receipt; the producer then rejects the affected
                # records instead of granting them permission.
                scope_judge = None
        if scope_judge is not None and records:
            _evidence_first.ensure_scope_receipts(
                records,
                resolver=resolver,
                domain_contract=contract or {},
                scope_decisions_path=scope_path,
                source_roots=[run_dir],
                judge=scope_judge,
                record_limit=limit,
            )
        manifest = _evidence_first.produce_evidence_atoms(
            output_dir=self.evidence_artifact_dir,
            records=records,
            resolver=resolver,
            domain_contract=contract or {},
            domain_contract_source=str(contract_path),
            scope_decisions_path=scope_path,
            run_id=str(getattr(self, "run_id", "") or run_dir.name),
            generation_id=self.generation_id,
            attempt_id=self.attempt_id,
            policy_sha256=self._evidence_policy_sha256(),
            material_snapshot_hash=self._evidence_material_snapshot_hash(),
            roles=(),
            record_limit=limit,
            role_by_paper=role_by_paper,
            source_roots=[run_dir],
        )
        try:
            from optomind_research.runtime.upgrade3 import atomic_claims as _atomic_claims

            eligible_ids = [
                _text(row.get("atom_id"))
                for row in (manifest.get("eligible_atoms") or [])
                if _text(row.get("atom_id"))
            ]
            # The atomiser budget is derived from the contract's own target and the
            # material actually eligible: a fixed three calls of four atoms covered
            # twelve atoms for the whole attempt while each section is asked for
            # sixteen to thirty-two claims, so the target was unreachable by
            # construction and every run reported exactly twelve.
            _atomiser_calls = _atomic_claims.plan_atomiser_calls(
                eligible_atoms=len(eligible_ids),
                sections=max(1, len(states)),
                batch_size=_atomic_claims.DEFAULT_BATCH_SIZE,
                hard_cap=int(self.evidence_atomiser_call_limit or 0)
                or _atomic_claims.ATOMISER_HARD_CALL_CAP,
            )
            claim_manifest = _atomic_claims.build_atomic_claims(
                output_dir=self.evidence_artifact_dir,
                max_atomiser_calls=_atomiser_calls,
                atoms=_atomic_claims.load_atoms_from_jsonl(
                    self.evidence_artifact_dir / _evidence_first.ATOMS_FILENAME
                ),
                eligible_atom_ids=eligible_ids,
                generation_id=self.generation_id,
                attempt_id=self.attempt_id,
                run_id=str(getattr(self, "run_id", "") or run_dir.name),
                policy_sha256=self._evidence_policy_sha256(),
                material_snapshot_hash=self._evidence_material_snapshot_hash(),
                atomiser=atomiser,
                atomiser_task_id=self._phase3_budget_task_id(),
                atomiser_generation_id=self.generation_id,
            )
            self._write_claim_factory_snapshot(manifest, claim_manifest)
        except Exception:
            if getattr(self, "evidence_first_atoms", False):
                raise
        return manifest

    def _write_claim_factory_snapshot(
        self,
        atom_manifest: Mapping[str, Any],
        claim_manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist the typed claim-factory decision this attempt just made."""

        from optomind_research.runtime.upgrade3 import (
            phase3_claim_factory_snapshot as _snapshot,
        )

        atom_rows = [
            json.loads(line)
            for line in (self.evidence_artifact_dir / "EVIDENCE_ATOMS.jsonl")
            .read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        claim_rows = [
            json.loads(line)
            for line in (self.evidence_artifact_dir / "ATOMIC_CLAIMS.jsonl")
            .read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        snapshot = _snapshot.build_claim_factory_snapshot(
            generation_id=self.generation_id,
            attempt_id=self.attempt_id,
            atom_manifest=atom_manifest,
            atom_rows=atom_rows,
            claim_manifest=claim_manifest,
            claim_rows=claim_rows,
            input_hashes=self._evidence_input_hashes(),
        )
        _snapshot.write_snapshot(
            self.evidence_artifact_dir / _snapshot.SNAPSHOT_FILENAME, snapshot
        )
        return snapshot

    def _evidence_input_hashes(self) -> dict[str, Any]:
        """The claim-factory inputs a resume has to compare against.

        Only the artifacts that can change what the factory decides are included:
        the frozen domain contract, the scope-decision log, the material snapshot,
        the policy, and the node input fingerprint when the harness wrote one.
        """

        fingerprint_path = self.output_dir / "PHASE3_INPUT_FINGERPRINT.json"
        hashes: dict[str, Any] = {
            "policy_sha256": self._evidence_policy_sha256(),
            "material_snapshot_hash": self._evidence_material_snapshot_hash(),
            "domain_contract_sha256": "",
            "scope_receipts_sha256": "",
            "phase3_input_fingerprint": "",
        }
        contract_path = self.output_dir.parent / "DOMAIN_CONTRACT.json"
        if contract_path.is_file():
            hashes["domain_contract_sha256"] = _sha256_of_file(contract_path)
        scope_path = self._phase3_scope_decisions_path()
        if scope_path.is_file():
            hashes["scope_receipts_sha256"] = _sha256_of_file(scope_path)
        if fingerprint_path.is_file():
            try:
                payload = json.loads(fingerprint_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            hashes["phase3_input_fingerprint"] = _text(
                (payload or {}).get("sha256")
            )
        return hashes

    def _p3a_atom_payloads(self) -> dict[str, Any]:
        """Read the artifacts the evidence-first producer wrote for this node.

        When the producer was enabled it must have produced a complete, self
        consistent artifact set: a missing or tampered file is an engineering
        failure, not an empty result, and the node fails closed.  When it was not
        enabled the node still commits explicit unavailable receipts so no
        downstream consumer can mistake their absence for permission - the
        eligible list is empty and the mode is marked disabled.
        """

        from optomind_research.runtime.upgrade3 import evidence_first as _evidence_first

        from optomind_research.runtime.upgrade3 import (
            phase3_claim_factory_snapshot as _factory_snapshot,
        )

        out = self.evidence_artifact_dir
        atoms_path = out / _evidence_first.ATOMS_FILENAME
        rejections_path = out / _evidence_first.REJECTIONS_FILENAME
        manifest_path = out / _evidence_first.MANIFEST_FILENAME
        snapshot_path = out / _factory_snapshot.SNAPSHOT_FILENAME
        complete = (
            atoms_path.is_file()
            and rejections_path.is_file()
            and manifest_path.is_file()
            and snapshot_path.is_file()
        )
        if not complete:
            if getattr(self, "evidence_first_atoms", False):
                raise RuntimeError("evidence_first_artifacts_missing")
            return {
                _evidence_first.ATOMS_FILENAME: "",
                _evidence_first.REJECTIONS_FILENAME: "",
                _evidence_first.MANIFEST_FILENAME: {
                    "schema_version": _evidence_first.MANIFEST_SCHEMA,
                    "status": "unavailable",
                    "reason": "evidence_first_producer_disabled",
                    "authorable_atoms": 0,
                    "binding_required": True,
                    "eligible_atoms": [],
                    "counts": {
                        "atoms": 0,
                        "eligible_for_claim_audit": 0,
                        "rejected": 0,
                    },
                },
            }
        # Validate through the consumer entry point before the node commits it.
        _evidence_first.load_atom_manifest(manifest_path)
        # The typed snapshot is validated on the way out as well, so a P3A node that
        # commits it has already proven the resume path can read it back.
        _factory_snapshot.load_snapshot(snapshot_path)
        return {
            _evidence_first.ATOMS_FILENAME: atoms_path.read_text(encoding="utf-8"),
            _evidence_first.REJECTIONS_FILENAME: rejections_path.read_text(encoding="utf-8"),
            _evidence_first.MANIFEST_FILENAME: json.loads(
                manifest_path.read_text(encoding="utf-8")
            ),
            _factory_snapshot.SNAPSHOT_FILENAME: json.loads(
                snapshot_path.read_text(encoding="utf-8")
            ),
        }

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
            "provisional_bindings": {},
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
            # The decomposer owns the prompt path actually used by this run.
            # Keep it for the node manifest instead of assuming the repository
            # default when a caller injects a prompt asset.
            _prompt_path = getattr(decomposer, "prompt_path", None)
            if _prompt_path:
                self._phase3_claim_prompt_path = Path(
                    _prompt_path
                ).expanduser().resolve(strict=False)
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
        bindings = state.get("provisional_bindings")
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
        state["provisional_bindings"] = {
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
        return atlas

    @staticmethod
    def _derive_section_outcome(state: dict[str, Any]) -> str:
        """Derive an independent section outcome from the adapted claims."""

        claims = [item for item in state.get("claims") or [] if isinstance(item, dict)]
        bindings = state.get("provisional_bindings") or {}
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
            binding = (state.get("provisional_bindings", {}).get("claims", {}) or {}).get(
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
            state["contract"], state["claims"], state.get("provisional_bindings", {})
        )
        state["argument_task_coverage"] = task_coverage
        if isinstance(state.get("provisional_bindings"), dict):
            state["provisional_bindings"]["argument_task_coverage"] = task_coverage
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
        state["provisional_bindings"]["section_outcome"] = outcome
        state["provisional_bindings"]["status"] = legacy_status
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
                for claim_id, item in state["provisional_bindings"].get("claims", {}).items()
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
            for claim_id, binding in state["provisional_bindings"].get("claims", {}).items():
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
        blueprint: Mapping[str, Any] | None = None,
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
            blueprint=dict(blueprint or self.blueprint),
            states=states,
            coverage_atlas=coverage_atlas,
            claim_graph=claim_graph,
            relation_graph=relation_graph,
            coverage_requests=coverage_requests,
            phase_run=phase_run,
            acceptance=acceptance,
            output_dir=self.output_dir,
        )

    def _execute_p3d_from_committed_p3c(
        self,
        p3c_manifest: Mapping[str, Any],
        attempt_id: str,
    ) -> dict[str, Any]:
        """Run the P3D consumer from a committed, persisted P3C snapshot.

        This method is intentionally the only P3D execution path.  Fresh
        runs and direct D-only resumes both validate the committed A/B/C
        manifests, rehydrate the node-local snapshot, and then use the same
        acceptance/handoff/commit sequence.
        """

        from optomind_research.runtime.upgrade3 import phase3_node_integration as _node_integration
        from optomind_research.runtime.upgrade3.phase3_nodes import (
            NODE_IDS as _PHASE3_NODE_IDS,
            load_and_validate_committed_node,
        )
        from optomind_research.runtime.upgrade3.phase3_state_snapshot import (
            rehydrated_p3d_inputs,
        )

        attempt_id = str(attempt_id or "").strip()
        if not attempt_id:
            raise RuntimeError("p3d_attempt_id_required")
        p3c_node_id = str(p3c_manifest.get("node_id") or "")
        if p3c_node_id != "P3C_COVERAGE":
            raise RuntimeError(f"p3d_requires_p3c_manifest:{p3c_node_id}")
        if str(p3c_manifest.get("state") or "") != "committed":
            raise RuntimeError("p3d_requires_committed_p3c")
        if str(p3c_manifest.get("generation_id") or "") != self.generation_id:
            raise RuntimeError("p3d_p3c_generation_mismatch")

        # Every resume reads the persisted manifests from disk; the manifest
        # argument is only an identity hint from the caller and cannot become
        # a second source of truth.
        committed_manifests: dict[str, dict[str, Any]] = {}
        root = self.output_dir
        for node_id in _PHASE3_NODE_IDS[:3]:
            committed_manifests[node_id] = load_and_validate_committed_node(
                root,
                node_id,
                expected_generation_id=self.generation_id,
            )
        p3c_manifest = committed_manifests["P3C_COVERAGE"]
        snapshot_output = next(
            (
                row
                for row in p3c_manifest.get("outputs") or []
                if isinstance(row, Mapping)
                and str(row.get("role") or "") == "P3C_STATE_SNAPSHOT.json"
            ),
            None,
        )
        if not isinstance(snapshot_output, Mapping):
            raise RuntimeError("p3c_snapshot_output_missing")
        snapshot_path = Path(str(snapshot_output.get("path") or "")).expanduser().resolve(
            strict=False
        )
        snapshot_output_hash = str(snapshot_output.get("sha256") or "")
        snapshot_raw = _read_json(snapshot_path)
        snapshot_body_hash = str(snapshot_raw.get("snapshot_sha256") or "")
        p3c_attempt_id = str(p3c_manifest.get("attempt_id") or "")
        p3d_inputs = rehydrated_p3d_inputs(
            snapshot_path,
            expected_generation_id=self.generation_id,
            expected_attempt_id=p3c_attempt_id,
        )
        if (p3d_inputs.get("phase_run") or {}).get("binding_authority_ready") is not True:
            raise RuntimeError("p3d_binding_authority_unavailable")
        d_blueprint = dict(p3d_inputs["blueprint"])
        d_states = list(p3d_inputs["states"])
        d_requests = list(p3d_inputs["coverage_requests"])
        d_claim_graph = dict(p3d_inputs["claim_graph"])
        d_relation_graph = dict(p3d_inputs["relation_graph"])
        d_relation_audit = dict(p3d_inputs["relation_audit"])
        d_coverage_atlas = dict(p3d_inputs["coverage_atlas"])
        d_phase_run = dict(p3d_inputs["phase_run"])
        d_llm_summary = dict(p3d_inputs["llm_summary"])
        d_blocked_sections = list(p3d_inputs["blocked_sections"])
        d_upgrade3_verdicts = dict(p3d_inputs["upgrade3_verdicts"])
        d_phase_run["attempt_id"] = attempt_id
        d_phase_run["phase3_nodes"] = {
            node_id: {
                "state": manifest.get("state"),
                "generation_id": manifest.get("generation_id"),
                "attempt_id": manifest.get("attempt_id"),
                "manifest_sha256": manifest.get("manifest_sha256"),
            }
            for node_id, manifest in committed_manifests.items()
        }
        d_phase_run["p3d_consumer"] = {
            "source": "P3C_STATE_SNAPSHOT.json",
            "snapshot_path": str(snapshot_path),
            "snapshot_output_sha256": snapshot_output_hash,
            "snapshot_body_sha256": snapshot_body_hash,
            "p3c_attempt_id": p3c_attempt_id,
            "p3d_attempt_id": attempt_id,
        }
        d_phase_run["binding_authority_ready"] = True

        base_acceptance = self._acceptance(
            states=d_states,
            requests=d_requests,
            claim_graph=d_claim_graph,
            relation_audit=d_relation_audit,
            phase_run=d_phase_run,
            coverage_atlas=d_coverage_atlas,
            llm_summary=d_llm_summary,
            upgrade3_blocked_sections=d_blocked_sections,
        )
        base_acceptance["binding_authority_ready"] = True
        base_acceptance["upgrade3_coverage_verdicts"] = {
            "schema_version": "optomind.upgrade3.section_coverage_verdicts.v1",
            "blocked_sections": sorted(d_blocked_sections),
            "verdicts": d_upgrade3_verdicts,
        }
        # SM09: the stage publishes the level its committed nodes actually proved,
        # so a downstream readiness consumer never has to trust a stage status.
        from optomind_research.runtime.upgrade3 import (
            validation_receipts as _validation_module,
        )

        _validation_gate = _validation_module.read_run_validation_gate(
            self.output_dir.parent, required_level="V3"
        )
        _validation_gate["node_gates"] = {
            node_id: _node_integration.node_validation_gate(manifest)
            for node_id, manifest in sorted(committed_manifests.items())
            if isinstance(manifest, Mapping)
        }
        _write_json(
            self.output_dir / "NODE_VALIDATION_GATES.json",
            _validation_gate,
        )
        base_acceptance["upgrade3_validation_gate"] = _validation_gate

        draft_handoff = self.build_production_handoff(
            blueprint=d_blueprint,
            states=d_states,
            coverage_atlas=d_coverage_atlas,
            claim_graph=d_claim_graph,
            relation_graph=d_relation_graph,
            coverage_requests=d_requests,
            phase_run=d_phase_run,
            acceptance=base_acceptance,
        )
        draft_report = draft_handoff.validate()
        handoff_path = self.output_dir / R3_HANDOFF_FILENAME
        final_acceptance = dict(base_acceptance)
        final_acceptance["r3_production_handoff"] = {
            "path": str(handoff_path),
            "schema_version": draft_handoff.schema_version,
            "validation_status": draft_report.status,
            "validation_errors": list(draft_report.errors),
            "validation_warnings": list(draft_report.warnings),
            "section_readiness": {
                key: dict(value)
                for key, value in draft_report.section_readiness.items()
            },
            "global_readiness": dict(draft_report.global_readiness),
            "validation_source": "p3d_draft_handoff",
        }
        final_acceptance["p3d_consumer_source"] = {
            "node_id": "P3C_COVERAGE",
            "snapshot_path": str(snapshot_path),
            "snapshot_output_sha256": snapshot_output_hash,
            "snapshot_body_sha256": snapshot_body_hash,
            "rehydrated": True,
            "p3c_attempt_id": p3c_attempt_id,
            "p3d_attempt_id": attempt_id,
        }
        final_handoff = self.build_production_handoff(
            blueprint=d_blueprint,
            states=d_states,
            coverage_atlas=d_coverage_atlas,
            claim_graph=d_claim_graph,
            relation_graph=d_relation_graph,
            coverage_requests=d_requests,
            phase_run=d_phase_run,
            acceptance=final_acceptance,
        )
        final_report = final_handoff.validate()
        draft_validation_identity = (
            draft_report.status,
            list(draft_report.errors),
            list(draft_report.warnings),
            draft_report.section_readiness,
            draft_report.global_readiness,
        )
        final_validation_identity = (
            final_report.status,
            list(final_report.errors),
            list(final_report.warnings),
            final_report.section_readiness,
            final_report.global_readiness,
        )
        if final_validation_identity != draft_validation_identity:
            raise RuntimeError("p3d_draft_final_handoff_validation_mismatch")

        p3d_phase_run = dict(d_phase_run)
        p3d_phase_run["phase3_nodes"] = dict(d_phase_run.get("phase3_nodes") or {})
        p3d_phase_run["phase3_nodes"]["P3D_ACCEPTANCE_HANDOFF"] = {
            "state": "committed",
            "generation_id": self.generation_id,
            "attempt_id": attempt_id,
        }
        p3d_policy_context = {
            "p3c_manifest_sha256": str(p3c_manifest.get("manifest_sha256") or ""),
            "p3c_snapshot_output_sha256": snapshot_output_hash,
            "p3c_snapshot_body_sha256": snapshot_body_hash,
            "admission_policy": "upgrade3.coverage_readiness.author_admission",
        }
        no_llm_contract = _node_integration.canonical_no_llm_contract()
        p3d_payloads = {
            "PHASE3_RUN.json": p3d_phase_run,
            "PHASE3_ACCEPTANCE.json": final_acceptance,
            "R3_PRODUCTION_HANDOFF.json": final_handoff.to_dict(),
        }
        p3d_fingerprint = _node_integration.build_node_fingerprint_details(
            node_id="P3D_ACCEPTANCE_HANDOFF",
            code_paths=[
                Path(__file__),
                Path(_node_integration.__file__),
                Path(__file__).resolve().parent
                / "upgrade3"
                / "phase3_nodes.py",
                Path(__file__).resolve().parent
                / "upgrade3"
                / "phase3_state_snapshot.py",
                Path(build_r3_production_handoff_from_phase3.__code__.co_filename),
                Path(__file__).resolve().parent
                / "upgrade3"
                / "coverage_readiness.py",
            ],
            prompt_context={
                "prompt_contract": no_llm_contract,
                "contract_hash": _node_integration.canonical_no_llm_contract_hash(),
            },
            prompt_paths=[],
            prompt_functions=[],
            model_context={
                "real_llm_claims": False,
                "execution_mode": "no_llm",
                "contract_hash": _node_integration.canonical_no_llm_contract_hash(),
                "model_fingerprint": _node_integration.canonical_no_llm_contract_hash(),
                "contract": no_llm_contract,
            },
            policy_context=p3d_policy_context,
            schema_context={
                "schema_version": "research_harness.phase3_node_outputs.v1",
                "outputs": sorted(p3d_payloads),
            },
        )
        p3d_manifest = _node_integration.commit_node_artifacts(
            self.output_dir,
            node_id="P3D_ACCEPTANCE_HANDOFF",
            generation_id=self.generation_id,
            attempt_id=attempt_id,
            inputs=_node_integration.input_rows_from_manifest(p3c_manifest),
            dependencies=[_node_integration.dependency_row(p3c_manifest)],
            fingerprints=p3d_fingerprint["fingerprints"],
            fingerprint_sources=p3d_fingerprint["sources"],
            outputs={
                role: {"filename": role, "payload": payload}
                for role, payload in p3d_payloads.items()
            },
            root_filenames={role: role for role in p3d_payloads},
            cost_receipt={"status": "phase3_node_zero_cost", "model_calls": 0},
            validation_receipt={
                "status": "phase3_p3d_payloads_validated",
                "consumer_source": "P3C_STATE_SNAPSHOT.json",
                "consumer_snapshot_sha256": snapshot_output_hash,
                "draft_final_validation_match": True,
                "p3c_manifest_sha256": str(p3c_manifest.get("manifest_sha256") or ""),
            },
        )
        return {
            "acceptance": final_acceptance,
            "phase_run": p3d_phase_run,
            "handoff": final_handoff,
            "manifest": p3d_manifest,
            "p3c_manifest": p3c_manifest,
            "snapshot_path": snapshot_path,
            "snapshot_output_sha256": snapshot_output_hash,
            "snapshot_body_sha256": snapshot_body_hash,
            "executed_nodes": ["P3D_ACCEPTANCE_HANDOFF"],
            "model_calls": 0,
            "reused_manifests": committed_manifests,
        }

    def resume_p3d_from_committed_p3c(
        self,
        attempt_id: str | None = None,
        *,
        repair_projection_only: bool = True,
    ) -> dict[str, Any]:
        """Resume P3D directly from the persisted A/B/C node graph.

        The method deliberately bypasses the producer side of Phase 3.  It
        validates the committed A/B/C manifests, optionally archives the old
        D attempt and its root projections, and invokes the same P3D consumer
        used by :meth:`run`.  A committed D with only a compatibility-root
        projection failure is repaired in place by default; callers must set
        ``repair_projection_only=False`` to rebuild that attempt.
        """

        from optomind_research.runtime.upgrade3 import phase3_node_integration as _node_integration
        from optomind_research.runtime.upgrade3.phase3_nodes import (
            NODE_IDS as _PHASE3_NODE_IDS,
            _manifest_hash,
            archive_node_attempt_for_rebuild,
            continue_archive_node_attempt,
            load_and_validate_committed_node,
            node_dir,
        )

        root = _node_integration.phase3_root(self.output_dir)

        def _d_only_usage_receipt() -> tuple[dict[str, Any], dict[str, Any]]:
            """Separate zero current D-only usage from prior run totals."""

            current = {
                "cost_cny": 0.0,
                "cost": 0.0,
                "input_tokens": 0,
                "input": 0,
                "output_tokens": 0,
                "output": 0,
                "model_calls": 0,
                "calls": 0,
                "source": "d_only_current_attempt",
            }
            historical = {
                "cost_cny": 0.0,
                "cost": 0.0,
                "input_tokens": 0,
                "input": 0,
                "output_tokens": 0,
                "output": 0,
                "model_calls": 0,
                "calls": 0,
                "source": "unavailable",
            }
            for source_name, payload in (
                ("PHASE3_RUN.json:llm", _read_json(root / "PHASE3_RUN.json").get("llm")),
                ("PHASE3_ACCEPTANCE.json:cost", _read_json(root / "PHASE3_ACCEPTANCE.json").get("cost")),
            ):
                if not isinstance(payload, Mapping):
                    continue
                cost = float(
                    payload.get("estimated_cost_cny", payload.get("cost_cny", 0.0))
                    or 0.0
                )
                input_tokens = max(
                    int(payload.get("input_tokens_observed", 0) or 0),
                    int(payload.get("input_tokens", 0) or 0),
                    int(payload.get("estimated_input_tokens_total", 0) or 0),
                )
                output_tokens = max(
                    int(payload.get("output_tokens_observed", 0) or 0),
                    int(payload.get("output_tokens", 0) or 0),
                    int(payload.get("estimated_output_tokens_total", 0) or 0),
                )
                calls = int(
                    payload.get("calls_observed_or_estimated", payload.get("qwen_calls", 0))
                    or 0
                )
                if cost or input_tokens or output_tokens or calls:
                    historical.update({
                        "cost_cny": round(cost, 6),
                        "cost": round(cost, 6),
                        "input_tokens": input_tokens,
                        "input": input_tokens,
                        "output_tokens": output_tokens,
                        "output": output_tokens,
                        "model_calls": calls,
                        "calls": calls,
                        "source": source_name,
                    })
                    break
            return current, historical

        current_attempt_usage, historical_cumulative = _d_only_usage_receipt()
        committed_manifests: dict[str, dict[str, Any]] = {}
        if not self._configured_generation_id:
            persisted_p3c = load_and_validate_committed_node(
                root,
                "P3C_COVERAGE",
            )
            self.generation_id = str(persisted_p3c.get("generation_id") or "")
            if not self.generation_id:
                raise RuntimeError("p3d_resume_generation_id_missing")
        for node_id in _PHASE3_NODE_IDS[:3]:
            committed_manifests[node_id] = load_and_validate_committed_node(
                root,
                node_id,
                expected_generation_id=self.generation_id,
            )
        p3c_manifest = committed_manifests["P3C_COVERAGE"]

        d_node_dir = node_dir(root, "P3D_ACCEPTANCE_HANDOFF")
        d_manifest_path = d_node_dir / "NODE_MANIFEST.json"
        d_archive_journal_path = d_node_dir / "ARCHIVE_TRANSACTION.json"
        d_manifest: dict[str, Any] | None = None
        d_archive_journal: dict[str, Any] | None = None
        archive_source_manifest: dict[str, Any] | None = None
        if d_manifest_path.is_file():
            try:
                raw_d_manifest = json.loads(d_manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"p3d_manifest_unreadable:{type(exc).__name__}"
                ) from exc
            if not isinstance(raw_d_manifest, Mapping):
                raise RuntimeError("p3d_manifest_invalid")
            d_manifest = dict(raw_d_manifest)
            if str(d_manifest.get("generation_id") or "") != self.generation_id:
                raise RuntimeError("p3d_manifest_generation_mismatch")
            if str(d_manifest.get("manifest_sha256") or "") != _manifest_hash(d_manifest):
                raise RuntimeError("p3d_manifest_hash_mismatch")
            if str(d_manifest.get("state") or "") not in {"committed", "failed"}:
                if str(d_manifest.get("state") or "") != "superseded":
                    raise RuntimeError(
                        f"p3d_manifest_state_invalid:{d_manifest.get('state')}"
                    )
        if d_archive_journal_path.is_file():
            try:
                archive_value = json.loads(
                    d_archive_journal_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"p3d_archive_journal_unreadable:{type(exc).__name__}"
                ) from exc
            if not isinstance(archive_value, Mapping):
                raise RuntimeError("p3d_archive_journal_invalid")
            d_archive_journal = dict(archive_value)
            raw_source_manifest = d_archive_journal.get("source_manifest")
            journal_hash = str(
                d_archive_journal.get("original_manifest_sha256")
                or d_archive_journal.get("source_manifest_sha256")
                or ""
            )
            if (
                str(d_archive_journal.get("node_id") or "")
                == "P3D_ACCEPTANCE_HANDOFF"
                and str(d_archive_journal.get("generation_id") or "")
                == self.generation_id
                and isinstance(raw_source_manifest, Mapping)
                and journal_hash
                and str(raw_source_manifest.get("manifest_sha256") or "") == journal_hash
                and _manifest_hash(raw_source_manifest) == journal_hash
                and str(d_archive_journal.get("state") or "") != "committed"
            ):
                archive_source_manifest = dict(raw_source_manifest)
                # A superseded/removed top manifest is an in-progress archive,
                # not a D attempt eligible for projection-only repair.
                if d_manifest is None or d_manifest.get("state") == "superseded":
                    d_manifest = dict(archive_source_manifest)
            elif (
                str(d_archive_journal.get("state") or "") == "committed"
                and isinstance(raw_source_manifest, Mapping)
                and journal_hash == str(raw_source_manifest.get("manifest_sha256") or "")
                and (
                    d_manifest is None
                    or str(d_manifest.get("manifest_sha256") or "") == journal_hash
                )
            ):
                archive_source_manifest = dict(raw_source_manifest)

        def reused_payloads() -> dict[str, dict[str, Any]]:
            result = {node_id: dict(manifest) for node_id, manifest in committed_manifests.items()}
            if d_manifest is not None:
                result["P3D_ACCEPTANCE_HANDOFF"] = dict(d_manifest)
            return result

        archive_in_progress = bool(
            d_archive_journal is not None
            and str(d_archive_journal.get("state") or "") != "committed"
            and archive_source_manifest is not None
        )
        projection_validation: dict[str, Any] | None = None
        if (
            d_manifest is not None
            and d_manifest.get("state") == "committed"
            and not archive_in_progress
        ):
            # A D attempt is complete only when its own outputs, every index
            # row, and every root byte all agree.  Missing rows/payloads are a
            # safe replay case; a conflicting byte/provenance raises before
            # any archive mutation.
            projection_validation = _node_integration.validate_committed_root_projection(
                root,
                d_manifest,
            )
            if repair_projection_only and projection_validation.get("valid"):
                return {
                    "status": "already_committed",
                    "executed_nodes": [],
                    "model_calls": 0,
                    "attempt_id": str(d_manifest.get("attempt_id") or ""),
                    "reused_manifests": reused_payloads(),
                    "manifest": dict(d_manifest),
                    "projection_validation": projection_validation,
                    "current_attempt_usage": current_attempt_usage,
                    "historical_cumulative": historical_cumulative,
                }
            if repair_projection_only and not projection_validation.get("valid"):
                _node_integration.replay_committed_node_projection(root, d_manifest)
                acceptance_path = root / "PHASE3_ACCEPTANCE.json"
                acceptance = _read_json(acceptance_path)
                phase_run = _read_json(root / "PHASE3_RUN.json")
                if acceptance and phase_run:
                    self._write_markdown(acceptance, phase_run)
                return {
                    "status": "projection_repaired",
                    "executed_nodes": [],
                    "projection_replayed_nodes": ["P3D_ACCEPTANCE_HANDOFF"],
                    "model_calls": 0,
                    "attempt_id": str(d_manifest.get("attempt_id") or ""),
                    "reused_manifests": reused_payloads(),
                    "manifest": dict(d_manifest),
                    "acceptance": acceptance,
                    "projection_validation": projection_validation,
                    "current_attempt_usage": current_attempt_usage,
                    "historical_cumulative": historical_cumulative,
                }

        new_attempt_id = str(attempt_id or uuid.uuid4().hex).strip()
        if not new_attempt_id:
            raise RuntimeError("p3d_resume_attempt_id_required")
        old_attempt_manifest = archive_source_manifest or d_manifest
        if old_attempt_manifest is not None and new_attempt_id == str(
            old_attempt_manifest.get("attempt_id") or ""
        ):
            # This comparison intentionally precedes all archive/move calls so
            # a rejected same-attempt rebuild cannot mutate D, root index, or
            # history.
            raise RuntimeError("p3d_resume_attempt_id_must_change")

        old_d_manifest_hash = (
            str(old_attempt_manifest.get("manifest_sha256") or "")
            if old_attempt_manifest
            else ""
        )
        archive_result: dict[str, Any] | None = None
        root_archive_result: dict[str, Any] | None = None
        if old_attempt_manifest is not None:
            if archive_in_progress:
                archive_result = continue_archive_node_attempt(
                    root,
                    "P3D_ACCEPTANCE_HANDOFF",
                    self.generation_id,
                    old_d_manifest_hash,
                )
            else:
                archive_result = archive_node_attempt_for_rebuild(
                    root,
                    "P3D_ACCEPTANCE_HANDOFF",
                    self.generation_id,
                    old_d_manifest_hash,
                )
            root_archive_result = _node_integration.archive_node_root_projections_for_rebuild(
                root,
                old_attempt_manifest,
                archive_result["history_path"],
            )

        self.attempt_id = new_attempt_id
        result = self._execute_p3d_from_committed_p3c(
            p3c_manifest,
            new_attempt_id,
        )
        self._write_markdown(result["acceptance"], result["phase_run"])
        result.update({
            "status": "resumed_committed",
            "attempt_id": new_attempt_id,
            "archive": archive_result,
            "root_projection_archive": root_archive_result,
            "current_attempt_usage": current_attempt_usage,
            "historical_cumulative": historical_cumulative,
        })
        return result

    @staticmethod
    def _finalize_upgrade3_bindings(
        states: list[dict[str, Any]],
        claim_bindings_by_section: Mapping[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        """Replace provisional bindings with the validated W4 authority.

        The helper is the single integration boundary between the W4 list
        envelope and final Phase-3 state.  It mutates only the state binding
        slot and its audit record; provisional binding payloads are removed
        before the state can reach acceptance or the production handoff.
        """

        from optomind_research.runtime.upgrade3.phase3_state_snapshot import (
            project_upgrade3_bindings_for_p3d,
        )

        section_ids = [
            str(state.get("section", {}).get("section_id") or "")
            for state in states
        ]
        if any(not section_id for section_id in section_ids):
            raise RuntimeError("upgrade3_binding_section_id_missing")
        if len(set(section_ids)) != len(section_ids):
            raise RuntimeError("upgrade3_binding_section_ids_not_unique")
        if set(claim_bindings_by_section) != set(section_ids):
            raise RuntimeError(
                "upgrade3_binding_section_keys_incomplete:"
                f"expected={sorted(section_ids)}:"
                f"actual={sorted(claim_bindings_by_section)}"
            )
        projected_by_section: dict[str, dict[str, Any]] = {}
        for state, section_id in zip(states, section_ids):
            projected = project_upgrade3_bindings_for_p3d(
                section_id,
                claim_bindings_by_section[section_id],
            )
            raw_claims = state.get("claims") or []
            if any(
                not isinstance(claim, Mapping)
                or not str(claim.get("claim_id") or "").strip()
                for claim in raw_claims
            ):
                raise RuntimeError(
                    f"upgrade3_binding_claim_id_missing:{section_id}"
                )
            state_claim_ids = {
                str(claim.get("claim_id")) for claim in raw_claims
            }
            projected_claim_ids = set(projected.get("claims") or {})
            if state_claim_ids != projected_claim_ids:
                raise RuntimeError(
                    f"upgrade3_binding_claim_ids_mismatch:{section_id}:"
                    f"claims={sorted(state_claim_ids)}:"
                    f"bindings={sorted(projected_claim_ids)}"
                )
            projected_by_section[section_id] = projected
        for state, section_id in zip(states, section_ids):
            incoming = state.get("provisional_bindings")
            state["bindings"] = projected_by_section[section_id]
            state["binding_replacement_audit"] = {
                "incoming_type": type(incoming).__name__,
                "incoming_authority": "provisional_internal_bindings",
                "replacement_type": "dict",
                "replacement_authority": "upgrade3_claim_bindings",
                "source_binding_hashes": list(
                    projected_by_section[section_id].get("source_binding_hashes", [])
                ),
                "action": "replaced_by_w4_projection",
            }
            state.pop("provisional_bindings", None)
        return projected_by_section

    @staticmethod
    def _mark_upgrade3_bindings_unavailable(
        states: list[dict[str, Any]],
        error: Mapping[str, Any],
        generation_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Discard provisional state and return explicit blocked verdicts.

        The replacement bundle intentionally contains only section/task audit
        fields and redacted evidence-gap claims.  It is never copied from the
        provisional bundle, so support IDs, permissions, write decisions and
        any provisional selector content cannot survive a W4 failure.
        """

        verdicts: dict[str, dict[str, Any]] = {}
        for state in states:
            section_id = str(state.get("section", {}).get("section_id") or "")
            incoming = state.get("provisional_bindings")
            contract = state.get("contract")
            task_rows: list[dict[str, Any]] = []
            for task in getattr(contract, "argument_tasks", ()) or ():
                if not isinstance(task, Mapping):
                    continue
                task_row = {
                    "task_id": str(task.get("task_id") or ""),
                    "description": str(
                        task.get("description")
                        or task.get("question")
                        or task.get("task")
                        or ""
                    ),
                    "required": bool(task.get("required", True)),
                    "status": "unbound",
                    "support_state": "unbound",
                    "missing_components": [
                        "upgrade3_claim_bindings_unavailable"
                    ],
                }
                if task_row["task_id"]:
                    task_rows.append(task_row)
            gap_claims: list[dict[str, Any]] = []
            for claim in state.get("claims") or []:
                if not isinstance(claim, Mapping):
                    continue
                claim_id = str(claim.get("claim_id") or "")
                if not claim_id:
                    continue
                gap_claims.append({
                    "claim_id": claim_id,
                    "statement": str(
                        claim.get("effective_statement")
                        or claim.get("supported_rewrite")
                        or claim.get("statement")
                        or ""
                    ),
                    "importance": str(claim.get("importance") or "supporting"),
                    "classification": "evidence_gap",
                    "support_classification": "open_question",
                    "binding_authority": "upgrade3_claim_bindings_unavailable",
                    "unresolved_reason": "upgrade3_claim_bindings_unavailable",
                })
            state["bindings"] = {
                "section_id": section_id,
                "binding_authority": "upgrade3_claim_bindings_unavailable",
                "source_binding_hashes": [],
                "claims": {},
                "error": dict(error),
            }
            state["status"] = "needs_more_literature"
            state["section_outcome"] = "needs_more_literature"
            state["authorable_claims"] = []
            state["authorable_claim_ids"] = []
            state["evidence_gap_claims"] = list(gap_claims)
            state["evidence_gap_claim_ids"] = [
                item["claim_id"] for item in gap_claims
            ]
            state["bundle"] = {
                "section_id": section_id,
                "binding_authority": "upgrade3_claim_bindings_unavailable",
                "status": "needs_evidence",
                "readiness_status": "needs_evidence",
                "section_outcome": "needs_more_literature",
                "claim_binding_status": "upgrade3_claim_bindings_unavailable",
                "r4_handoff_allowed": False,
                "argument_task_coverage": task_rows,
                "authorable_claims": [],
                "authorable_claim_ids": [],
                "evidence_gap_claims": gap_claims,
                "evidence_gap_claim_ids": [
                    item["claim_id"] for item in gap_claims
                ],
                "unresolved_claim_ids": [
                    item["claim_id"] for item in gap_claims
                ],
                "runtime_failure": dict(state.get("runtime_failure") or {}),
                "upgrade3_error": dict(error),
            }
            state["binding_replacement_audit"] = {
                "incoming_type": type(incoming).__name__,
                "incoming_authority": "provisional_internal_bindings",
                "replacement_type": "dict",
                "replacement_authority": "upgrade3_claim_bindings_unavailable",
                "source_binding_hashes": [],
                "action": "discarded_replaced",
                "error": dict(error),
            }
            state.pop("provisional_bindings", None)
            verdicts[section_id] = {
                "schema_version": "optomind.upgrade3.section_coverage_verdict.v1",
                "section_id": section_id,
                "generation_id": generation_id,
                "required_slots": [],
                "question_coverage": {
                    "required_total": 0,
                    "covered": 0,
                    "ratio": 0.0,
                },
                "required_role_coverage": {
                    "required": [], "covered": [], "missing": [],
                },
                "optional_gaps": [],
                "comparison_axis_gaps": [],
                "soft_diagnostics": {},
                "status": "needs_evidence",
                "scientific_ready": False,
                "structural_complete": False,
                "upgrade3_error": dict(error),
            }
        return verdicts

    def _node_input_runtime_options(self) -> dict[str, Any]:
        """Return the complete runtime option contract persisted by P3A.

        The harness owns the canonical option map and passes it through
        ``node_runtime_options``.  Direct library callers still receive a
        complete deterministic map assembled from the effective attributes,
        so a resume can compare the whole key set rather than silently
        accepting an option that was absent from an older snapshot.
        """

        options: dict[str, Any] = {
            "offline": False,
            "real_llm_claims": self.real_llm_claims,
            "claim_pool_enabled": self.claim_pool_enabled,
            "real_llm_dag": self.real_llm_dag,
            "claim_model_tier": self.claim_model_tier,
            "dag_model_tier": self.dag_model_tier,
            "max_m2a_input_tokens": self.max_m2a_input_tokens,
            "max_m2a_records": self.max_m2a_records,
            "max_dag_candidates": self.max_dag_candidates,
            "dag_claims_per_section": self.dag_claims_per_section,
            "dag_total_claims": self.dag_total_claims,
            "claim_pool_served_limit": self.claim_pool_served_limit,
            "claim_pool_target_range": list(self.claim_pool_target_range),
            "claim_pool_shortlist_limit": self.claim_pool_shortlist_limit,
            "authoring_core_chunk_limit": self.authoring_core_chunk_limit,
            "execute_coverage": self.execute_coverage,
            "section_ids_to_process": sorted(self.section_ids_to_process),
        }
        # Keep the harness values byte-for-byte comparable with its runtime
        # policy, including the offline marker and any future option added to
        # that policy.  Path boundaries are separate snapshot fields and are
        # therefore deliberately supplied through their own constructor args.
        options.update(self.node_runtime_options)
        return options

    def run(
        self,
        *,
        coverage_executor: Callable[[list[dict[str, Any]], int], dict[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        run_started = time.perf_counter()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.attempt_id = self._configured_attempt_id or uuid.uuid4().hex
        # Registered before the node input snapshot is fingerprinted: acquired
        # material that the fingerprint does not cover would be material the
        # attempt is not accountable for.
        self._phase3_material_acquisition_state = self._register_material_acquisition()
        from optomind_research.runtime.upgrade3.phase3_node_integration import (
            phase3_root as _phase3_node_root,
            write_node_input_snapshot as _write_node_input_snapshot,
        )
        _phase3_root_path = _phase3_node_root(self.output_dir)
        _input_fingerprint = self.output_dir / "PHASE3_INPUT_FINGERPRINT.json"
        if not _input_fingerprint.is_file():
            _candidate_fingerprint = _phase3_root_path / "PHASE3_INPUT_FINGERPRINT.json"
            _input_fingerprint = (
                _candidate_fingerprint
                if _candidate_fingerprint.is_file()
                else self.output_dir / "PHASE3_INPUT_FINGERPRINT.json"
            )
        _node_input_snapshot_path = _write_node_input_snapshot(
            self.output_dir,
            generation_id=self.generation_id,
            attempt_id=self.attempt_id,
            blueprint=self.blueprint,
            runtime_options=self._node_input_runtime_options(),
            shared_ledger_path=self.shared_ledger_path,
            shared_kb_paths=self.shared_kb_paths,
            overlay_paths=self.overlay_paths,
            extra_paths=(
                [_input_fingerprint]
                if _input_fingerprint.is_file()
                else []
            ),
            scoped_runtime_kb=self.scoped_runtime_kb,
            source_base_kb=self.source_base_kb,
        )
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
        relation_graph_migrated_payload = {
            **self.relation_graph,
            "edges": migrated_edges,
            "phase3_relation_revalidation": relation_audit,
        }

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

        # upgrade3 W4 (016) + W3 (017): bind every section's final claims
        # through the atomic ClaimBinder, then compute the load-bearing
        # coverage verdict.  Sections whose verdict is needs_evidence can
        # never enter R4, regardless of the legacy outcome.
        # The evidence-first artifacts must exist BEFORE the binder asks for them:
        # the P3B stage consumes the claims this attempt produced, so producing
        # them later (at payload-assembly time) left the binder reading a previous
        # attempt's files, or nothing at all.  Production happens here, once, and
        # the P3A node later commits the staged artifacts.
        if getattr(self, "evidence_first_atoms", False):
            self.evidence_artifact_dir = self.output_dir / "_evidence_staging"
            # The ticket's order is remount -> blueprint revision -> claims.  The
            # revision used to run after the claim factory, so the 228 chunks it
            # mounted were never seen by the attempt that accepted them: a mount
            # the claim factory cannot read is a plan, not a change.
            #
            # The scope judgement comes first: an admission decision made before
            # the receipts exist would fall back to an acquisition label.
            self._phase3_ensure_scope_receipts(states)
            self._produce_mounting_artifacts(states)
            _revision_result = self._produce_blueprint_revision(states)
            self._phase3_apply_revision_mounts(states, _revision_result["bundle"])
            self._produce_p3a_evidence_atoms(
                states,
                atomiser=self._phase3_claim_atomiser(),
                record_limit=self.evidence_atom_record_limit or None,
            )
        section_ids = [state["section"]["section_id"] for state in states]
        claim_bindings_by_section: dict[str, list[dict[str, Any]]] = {
            section_id: [] for section_id in section_ids
        }
        upgrade3_verdicts: dict[str, dict[str, Any]] = {}
        upgrade3_binding_audit: dict[str, Any] = {"enabled": False}
        authoritative_bindings: dict[str, dict[str, Any]] = {}
        binding_authority_ready = False
        try:
            from optomind_research.runtime.upgrade3 import wiring as _u3w
            _run_dir = self.output_dir.parent
            _gen_id = self.generation_id
            _task_id = _u3w.run_budget_task_id()
            _entail = None
            try:
                _transport = _u3w.build_transport()
                _entail = _u3w.production_entailment_fn(
                    _transport, task_id=_task_id, generation_id=_gen_id)
            except Exception:
                _entail = None
            _binder = _u3w.build_claim_binder(_run_dir, entailment_fn=_entail)
            if _binder is None:
                raise RuntimeError("upgrade3_binder_unavailable")
            # Evidence-first mode binds the atomic claims the P3A node produced,
            # whose spans were verified against the document version before the
            # claim existed.  The legacy free-claim binder stays available for the
            # compatibility path only.
            _atomic_claims_for_binding = (
                self._atomic_claims_for_attempt()
                if getattr(self, "evidence_first_atoms", False) else []
            )
            upgrade3_batched_entailment: dict[str, Any] = {
                "enabled": bool(_atomic_claims_for_binding),
                "planned_pairs": 0,
                "provider_calls": 0,
                "cache_hits": 0,
                "single_pair_equivalent_calls": 0,
            }
            _atoms_by_id: dict[str, dict[str, Any]] = {}
            if _atomic_claims_for_binding:
                from optomind_research.runtime.upgrade3 import atomic_claims as _atomic_module

                for _atom in _atomic_module.load_atoms_from_jsonl(
                    self.evidence_artifact_dir / "EVIDENCE_ATOMS.jsonl"
                ):
                    _atoms_by_id[_text(_atom.get("atom_id"))] = _atom
            # Which section a claim belongs to is not a free choice: it is the
            # section whose served pool contains the claim's paper.  Binding every
            # claim into every section would attribute evidence to a section that
            # never sourced it.
            _paper_to_sections: dict[str, set[str]] = {}
            for _state in states:
                _sid = str(_state["section"]["section_id"])
                for _record in _state.get("claim_pool_records") or ():
                    _paper = _text(_record.get("paper_id"))
                    if _paper:
                        _paper_to_sections.setdefault(_paper, set()).add(_sid)
            for state in states:
                section_id = state["section"]["section_id"]
                if _atomic_claims_for_binding:
                    section_claims = [
                        claim for claim in _atomic_claims_for_binding
                        if _text(claim.get("field_name")) != "conditions"
                        and section_id in (_paper_to_sections.get(
                            _text(claim.get("paper_id"))) or {section_id})
                    ]
                    # In evidence-first mode the atomic claims ARE this section's
                    # authoritative claim set.  Leaving the legacy free claims in
                    # the state would make the state and the W4 binding authority
                    # disagree, which the snapshot contract refuses (correctly).
                    _projected_claims = []
                    for _claim in section_claims:
                        _claim_id = _text(_claim.get("claim_id"))
                        if not _claim_id:
                            continue
                        _projected_claims.append({
                            "claim_id": _claim_id,
                            "statement": _text(
                                _claim.get("atomic_statement")
                                or _claim.get("statement")),
                            "section_id": section_id,
                            "importance": _text(
                                _claim.get("importance") or "supporting"),
                            "claim_type": "evidence_first_atomic_claim",
                            "roles": list(
                                (_claim.get("role_provenance") or {}).get("roles")
                                or []),
                            "atom_ids": list(_claim.get("atom_ids") or []),
                            "evidence_type": _text(_claim.get("evidence_type")),
                            "source": "evidence_first.atomic_claims",
                        })
                    if _projected_claims:
                        state["legacy_claims"] = list(state.get("claims") or [])
                        state["claims"] = _projected_claims

                    def _binder_factory(_verdict_fn, _run_dir=_run_dir):
                        return _u3w.build_claim_binder(
                            _run_dir, entailment_fn=_verdict_fn
                        )

                    batched_result = _u3w.bind_atomic_section_claims_batched(
                        section_id=section_id,
                        atomic_claims=section_claims,
                        atoms_by_id=_atoms_by_id,
                        binder_factory=_binder_factory,
                        generation_id=_gen_id,
                        cache_path=self.output_dir / "ENTAILMENT_VERDICTS.jsonl",
                        judge=self._phase3_entailment_judge(),
                        batch_size=12,
                        max_batch_calls=self.claim_pool_shortlist_limit,
                        policy_sha256=self._evidence_policy_sha256(),
                        material_snapshot_hash=self._evidence_material_snapshot_hash(),
                        task_id=_task_id,
                        max_claims=self.claim_pool_shortlist_limit,
                    )
                    claim_bindings_by_section[section_id] = batched_result["bindings"]
                    upgrade3_batched_entailment = batched_result
                    continue
                graph = state.get("graph")
                records: dict[str, dict[str, Any]] = {}
                if graph is not None:
                    for chunk_id in graph.chunks:
                        try:
                            records[chunk_id] = _graph_record(graph, chunk_id)
                        except Exception:
                            continue
                claim_bindings_by_section[section_id] = _u3w.bind_section_claims(
                    section_id=section_id, claims=state["claims"],
                    chunk_records_by_id=records, binder=_binder,
                    generation_id=_gen_id)
            authoritative_bindings = self._finalize_upgrade3_bindings(
                states,
                claim_bindings_by_section,
            )
            upgrade3_binding_audit = {
                "enabled": True,
                "binder": "upgrade3.claim_binding.ClaimBinder",
                "entailment_calls": (
                    getattr(_entail, "call_count", {}).get("n", 0)
                    if _entail is not None else 0),
                # The batched topology is part of the receipt: the planned pair set,
                # the provider calls actually made, the calls the old single-pair
                # topology would have needed, and the cache hits that saved them.
                "batched_entailment": {
                    "enabled": bool(upgrade3_batched_entailment.get("enabled")),
                    "planned_pairs": int(
                        upgrade3_batched_entailment.get("planned_pairs") or 0),
                    "provider_calls": int(
                        upgrade3_batched_entailment.get("provider_calls") or 0),
                    "single_pair_equivalent_calls": int(
                        upgrade3_batched_entailment.get(
                            "single_pair_equivalent_calls") or 0),
                    "cache_hits": int(
                        upgrade3_batched_entailment.get("cache_hits") or 0),
                    "skipped_pairs": len(
                        (upgrade3_batched_entailment.get("entailment_receipt")
                         or {}).get("skipped") or []),
                    "unevaluated_pairs": len(
                        (upgrade3_batched_entailment.get("entailment_receipt")
                         or {}).get("unevaluated") or {}),
                    "batch_receipts": list(
                        (upgrade3_batched_entailment.get("entailment_receipt")
                         or {}).get("batches") or []),
                },
                "sections": {
                    sid: {
                        "claims": len(items),
                        "writable": sum(1 for b in items if b.get("writable")),
                        "unresolved": sum(
                            1 for b in items if not b.get("writable")),
                    }
                    for sid, items in claim_bindings_by_section.items()
                },
            }
            # The frozen blueprint revision is the authority for a section's
            # role set: it is the only path by which literature changes the
            # structure, and Phase 3 reads it by hash.  The legacy
            # section_coverage adaptation file is only consulted when no
            # revision was produced, so there is exactly one producer.
            _demoted: dict = {}
            _adaptation_hash = ""
            from optomind_research.runtime.upgrade3 import (
                blueprint_revision as _bp_revision_module,
            )

            _revision_view: dict | None = None
            _revision_error = ""
            _revision_path = (
                self.evidence_artifact_dir
                / _bp_revision_module.REVISION_FILENAME
            )
            if _revision_path.is_file():
                try:
                    _revision_obj = _bp_revision_module.load_frozen_revision(
                        _revision_path, expected_generation_id=_gen_id
                    )
                    _revision_view = _bp_revision_module.revision_consumer_view(
                        _revision_obj)
                except Exception as _rev_exc:
                    # Fail closed: a revision that cannot be verified must never
                    # be silently replaced by the initial blueprint.
                    _revision_error = "%s:%s" % (
                        type(_rev_exc).__name__, str(_rev_exc)[:300])
                    raise RuntimeError(
                        "blueprint_revision_consumer_rejected:%s" % _revision_error)
            else:
                _adapt_path = (
                    self.output_dir.parent / "section_coverage"
                    / "BLUEPRINT_ADAPTATION.json"
                )
                if _adapt_path.is_file():
                    try:
                        _adapt_obj = _read_json(_adapt_path) or {}
                        _demoted = dict(_adapt_obj.get("demoted_roles") or {})
                        _adaptation_hash = str(
                            _adapt_obj.get("adaptation_hash") or "")
                    except Exception:
                        _demoted = {}
            upgrade3_verdicts = _u3w.build_coverage_verdicts(
                states=states, bindings_by_section=claim_bindings_by_section,
                generation_id=_gen_id,
                demoted_roles=_demoted,
                adaptation_hash=_adaptation_hash,
                revision_view=_revision_view)
            if set(upgrade3_verdicts) != set(section_ids):
                raise RuntimeError("upgrade3_verdict_section_keys_incomplete")
            binding_authority_ready = bool(
                states
                and set(authoritative_bindings) == set(section_ids)
                and set(upgrade3_verdicts) == set(section_ids)
            )
            if not binding_authority_ready:
                raise RuntimeError("upgrade3_binding_authority_incomplete")
        except Exception as _u3_exc:
            # Fail closed: a broken binding gate must never leave the
            # provisional internal binding as the final state or silently
            # produce an apparently usable handoff.
            _u3_error = {
                "error_type": type(_u3_exc).__name__,
                "reason": str(_u3_exc)[:500],
            }
            authoritative_bindings = {}
            binding_authority_ready = False
            claim_bindings_by_section = {
                section_id: [] for section_id in section_ids
            }
            upgrade3_binding_audit = {
                "enabled": True,
                "binder": "unavailable_or_projection_failed",
                "error": _u3_error,
                "sections": {},
            }
            upgrade3_verdicts = self._mark_upgrade3_bindings_unavailable(
                states,
                _u3_error,
                self.output_dir.name or "phase3",
            )
        material_bindings = {
            state["section"]["section_id"]: state["bindings"]
            for state in states
        }
        bundles = [state["bundle"] for state in states]
        statuses = {state["section"]["section_id"]: state["status"] for state in states}
        outcomes = {
            state["section"]["section_id"]: str(
                state.get("section_outcome") or "needs_more_literature"
            )
            for state in states
        }
        # the 017 consumer gate is the only blocking authority here
        from optomind_research.runtime.upgrade3.coverage_readiness import (
            author_admission as _u3_author_admission,
        )
        upgrade3_blocked_sections = sorted(
            sid for sid, verdict in upgrade3_verdicts.items()
            if not _u3_author_admission(verdict).get("admit")
        )

        r4_ready_section_ids = sorted(
            sid for sid, outcome in outcomes.items()
            if outcome in {"ready", "ready_with_limits"}
            and sid not in upgrade3_blocked_sections
        )
        if not binding_authority_ready:
            phase_status = "failed_closed"
        elif not states:
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
            "material_acquisition": dict(self._phase3_material_acquisition_state),
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
            "r3_production_handoff_path": (
                str(self.output_dir / R3_HANDOFF_FILENAME)
                if binding_authority_ready else ""
            ),
            "updated_coverage_relation_counts": updated_atlas.get("relation_graph", {}),
            "llm": llm_summary,
            "phase2_executor_available": coverage_executor is not None or self.execute_coverage,
            "runtime_options": self._node_input_runtime_options(),
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
            "stop_reason": "upgrade3_binding_authority_unavailable" if not binding_authority_ready else (
                "all_sections_material_ready" if phase_status == "completed" else (
                "partial_sections_ready_with_declared_limits"
                if r4_ready_section_ids
                else "one_or_more_sections_require_material_or_claim_expansion"
                )
            ),
        }
        phase_run["binding_authority_ready"] = binding_authority_ready

        phase_run["upgrade3_claim_binding"] = dict(
            upgrade3_binding_audit,
            blocked_sections=upgrade3_blocked_sections,
            verdict_summaries={
                sid: {"status": v.get("status"),
                      "scientific_ready": v.get("scientific_ready"),
                      "question_coverage": v.get("question_coverage")}
                for sid, v in upgrade3_verdicts.items()
            },
        )
        self._upgrade3_blocked_sections = set(upgrade3_blocked_sections)
        self._upgrade3_verdicts = upgrade3_verdicts

        section_contracts_payload = {"contracts": contracts}
        candidate_claim_pools_payload = {
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
        }
        m2a_input_payloads_payload = {
            "schema_version": "research_harness.m2a_input_payloads.v1",
            "sections": {
                state["section"]["section_id"]: state.get("m2a_input_payload") or {}
                for state in states
            },
        }
        claim_bindings_payload = {
            "schema_version": "optomind.upgrade3.claim_bindings.v1",
            "sections": claim_bindings_by_section,
        }
        verdicts_payload = {
            "schema_version": "optomind.upgrade3.section_coverage_verdicts.v1",
            "verdicts": upgrade3_verdicts,
        }
        material_bindings_payload = {"sections": material_bindings}
        coverage_requests_payload = {
            "requests": [item.to_dict() for item in all_requests]
        }
        bundles_payload = {"bundles": bundles}
        from optomind_research.runtime.upgrade3 import phase3_node_integration as _node_integration
        from optomind_research.runtime.upgrade3.phase3_state_snapshot import (
            build_phase3_state_snapshot,
        )
        import optomind_research.claim_decomposer as _claim_decomposer_module
        from optomind_research.runtime.upgrade3 import wiring as _wiring_for_fingerprint
        _project_root = Path(__file__).resolve().parents[2]
        _claim_decomposer_path = Path(
            ClaimDecomposer.__init__.__code__.co_filename
        ).resolve()
        _phase3_nodes_path = Path(__file__).resolve().parent / "upgrade3" / "phase3_nodes.py"
        _claim_schema_path = _project_root / "optomind_research" / "claim_schema.py"
        _argument_dag_builder_path = Path(
            ArgumentDAGBuilder.__init__.__code__.co_filename
        ).resolve()
        _section_assets_path = Path(
            build_canonical_asset_graph.__code__.co_filename
        ).resolve()
        _wiring_path = _project_root / "optomind_research" / "runtime" / "upgrade3" / "wiring.py"
        _claim_binding_path = _project_root / "optomind_research" / "runtime" / "upgrade3" / "claim_binding.py"
        _permission_path = _project_root / "optomind_research" / "runtime" / "upgrade3" / "evidence_permission.py"
        _source_resolver_path = _project_root / "optomind_research" / "runtime" / "upgrade3" / "source_resolver.py"
        _coverage_readiness_path = _project_root / "optomind_research" / "runtime" / "upgrade3" / "coverage_readiness.py"
        _synthesis_bundle_path = _project_root / "optomind_research" / "runtime" / "synthesis_bundle.py"
        _blueprint_adaptation_path = _project_root / "optomind_research" / "runtime" / "upgrade3" / "blueprint_adaptation.py"
        _snapshot_path = Path(build_phase3_state_snapshot.__code__.co_filename).resolve()
        _r3_handoff_path = Path(
            build_r3_production_handoff_from_phase3.__code__.co_filename
        ).resolve()
        _coverage_atlas_path = Path(
            build_coverage_atlas.__code__.co_filename
        ).resolve()
        _relation_processing_path = Path(
            revalidate_legacy_relation_edges.__code__.co_filename
        ).resolve()
        _section_overlay_path = Path(
            build_section_asset_overlay.__code__.co_filename
        ).resolve()
        _claim_prompt_path = Path(
            getattr(
                self,
                "_phase3_claim_prompt_path",
                getattr(
                    _claim_decomposer_module,
                    "DEFAULT_DECOMPOSER_PROMPT",
                    _project_root / "prompts" / "Claim Decomposer.txt",
                ),
            )
        ).expanduser().resolve(strict=False)
        _domain_contract_path = (self.output_dir.parent / "DOMAIN_CONTRACT.json").resolve(
            strict=False
        )
        _domain_contract_hash = (
            _node_integration.sha256_file(_domain_contract_path)
            if _domain_contract_path.is_file()
            else ""
        )
        _policy_hash = str(
            getattr(_wiring_for_fingerprint, "DEFAULT_POLICY_HASH", "") or ""
        )
        _budget_task_id = str(
            _wiring_for_fingerprint.run_budget_task_id()
        )
        try:
            _qwen_base_url = str(_wiring_for_fingerprint.qwen_base_url() or "")
        except Exception:
            # Configuration discovery is local and optional for an offline
            # run.  Do not turn a missing key/config into a model call.
            _qwen_base_url = ""

        def _prompt_source_anchor(
            path: Path,
            function_name: str,
        ) -> Any:
            """Expose source-backed prompt identity through the fingerprint API."""

            source_text = path.read_text(encoding="utf-8")
            function_source = source_text
            try:
                tree = ast.parse(source_text, filename=str(path))
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                        node.name == function_name
                    ):
                        function_source = ast.get_source_segment(source_text, node) or source_text
                        break
            except (SyntaxError, ValueError):
                # The file hash remains in the code/prompt path rows; retaining
                # the full source is the conservative source identity.
                pass

            def source_anchor() -> None:
                return None

            source_anchor.__phase3_prompt_source__ = function_source
            source_anchor.__phase3_prompt_source_path__ = str(path)
            source_anchor.__phase3_prompt_qualname__ = function_name
            return source_anchor

        _entailment_fn = getattr(
            _wiring_for_fingerprint, "production_entailment_fn", None
        )
        _entailment_prompt_function = _entailment_fn
        _entailment_injected_prompt_function = None
        if not callable(_entailment_prompt_function) or not str(
            getattr(_entailment_prompt_function, "__qualname__", "")
        ).endswith("production_entailment_fn"):
            # Test/fixture dependency injection can replace the callable with
            # an anonymous lambda.  The node still fingerprints the real
            # production prompt source used by the wiring module.
            _entailment_prompt_function = _prompt_source_anchor(
                _wiring_path,
                "production_entailment_fn",
            )
            if callable(_entailment_fn):
                # Include the injected callable as a second source row as
                # well.  The production anchor preserves the real contract,
                # while a test or deployment-specific implementation remains
                # visible as a dependency that can change the fingerprint.
                _entailment_injected_prompt_function = _entailment_fn

        from optomind_research.runtime.upgrade3 import evidence_first as _evidence_first_module
        from optomind_research.runtime.upgrade3 import evidence_atoms as _evidence_atoms_module
        from optomind_research.runtime.upgrade3 import source_resolver as _source_resolver_module
        from optomind_research.runtime.upgrade3 import atomic_claims as _atomic_claims_module
        from optomind_research.runtime.upgrade3 import (
            phase3_claim_factory_snapshot as _factory_snapshot_module,
        )
        from optomind_research.runtime.upgrade3 import (
            blueprint_revision as _blueprint_revision_module,
        )
        from optomind_research.runtime.upgrade3 import (
            evidence_mounting as _evidence_mounting_module,
        )

        def _node_fingerprint(
            node_id: str,
            outputs: Mapping[str, Any],
            *,
            model_context_override: Mapping[str, Any] | None = None,
            policy_context_override: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            if node_id == "P3A_CLAIM_POOL":
                code_paths = [
                    Path(__file__),
                    Path(_node_integration.__file__),
                    _phase3_nodes_path,
                    _claim_decomposer_path,
                    _claim_schema_path,
                    _argument_dag_builder_path,
                    _section_assets_path,
                    _section_overlay_path,
                    _relation_processing_path,
                    # The evidence-first atom producer and the canonical atom
                    # builder are part of this node's identity: a change in either
                    # must invalidate a committed P3A node.
                    Path(_evidence_first_module.__file__),
                    Path(_evidence_atoms_module.__file__),
                    Path(_source_resolver_module.__file__),
                    Path(_atomic_claims_module.__file__),
                    Path(_factory_snapshot_module.__file__),
                    # the mounting pool and the blueprint revision produced inside
                    # this node are part of its identity: a change in either must
                    # invalidate a committed P3A node.
                    Path(_evidence_mounting_module.__file__),
                    Path(_blueprint_revision_module.__file__),
                ]
                prompt_context = {
                    "prompt_contract": "claim_pool_and_m2a_input",
                    "real_llm_enabled": self.real_llm_claims,
                    "prompt_path": str(_claim_prompt_path),
                }
                prompt_paths = [_claim_prompt_path]
                prompt_functions = [
                    getattr(ClaimDecomposer, "_load_prompt", None),
                    getattr(ClaimDecomposer, "_build_input_payload", None),
                    getattr(ClaimDecomposer, "decompose_section", None),
                ]
                model_context = {
                    **_node_model_context,
                    "node_model_boundary": "claim_decomposer",
                    "claim_model_tier": self.claim_model_tier,
                }
                policy_context = {
                    **_node_policy_context,
                    "node_policy_boundary": "claim_pool",
                }
            elif node_id == "P3B_CLAIM_BINDING":
                code_paths = [
                    Path(__file__),
                    _wiring_path,
                    _claim_binding_path,
                    _permission_path,
                    _source_resolver_path,
                    _section_assets_path,
                    _snapshot_path,
                    _phase3_nodes_path,
                ]
                prompt_context = {
                    "prompt_contract": "w4_claim_binding_and_entailment",
                    "prompt_source": str(_wiring_path),
                    "entailment_system_sha256": hashlib.sha256(
                        str(getattr(_wiring_for_fingerprint, "ENTAIL_SYSTEM", ""))
                        .encode("utf-8")
                    ).hexdigest(),
                }
                prompt_paths = [_wiring_path]
                prompt_functions = [_entailment_prompt_function]
                if _entailment_injected_prompt_function is not None:
                    prompt_functions.append(_entailment_injected_prompt_function)
                model_context = {
                    **_node_model_context,
                    "node_model_boundary": "production_entailment",
                    "entailment_provider": "qwen",
                    "entailment_model": "qwen3.7-flash",
                    "entailment_task_id": _budget_task_id,
                    "entailment_base_url": _qwen_base_url,
                    "entailment_max_calls": 1200,
                    "entailment_temperature": 0.0,
                    "entailment_max_input_tokens": 3000,
                    "entailment_max_output_tokens": 2000,
                }
                policy_context = {
                    **_node_policy_context,
                    "node_policy_boundary": "claim_binding",
                    "claim_binding_policy_hash": _policy_hash,
                    "claim_binding_max_claims": 64,
                    "domain_contract_sha256": _domain_contract_hash,
                }
            elif node_id == "P3C_COVERAGE":
                code_paths = [
                    Path(__file__),
                    _snapshot_path,
                    _coverage_readiness_path,
                    _synthesis_bundle_path,
                    _blueprint_adaptation_path,
                    _wiring_path,
                    _coverage_atlas_path,
                    Path(_node_integration.__file__),
                    _phase3_nodes_path,
                    Path(_blueprint_revision_module.__file__),
                ]
                _no_llm_contract = _node_integration.canonical_no_llm_contract()
                prompt_context = {
                    "prompt_contract": _no_llm_contract,
                    "contract_hash": _node_integration.canonical_no_llm_contract_hash(),
                }
                prompt_paths = []
                prompt_functions = []
                model_context = {
                    "real_llm_claims": False,
                    "execution_mode": "no_llm",
                    "contract_hash": _node_integration.canonical_no_llm_contract_hash(),
                    "model_fingerprint": _node_integration.canonical_no_llm_contract_hash(),
                    "contract": _no_llm_contract,
                }
                policy_context = {
                    **_node_policy_context,
                    "coverage_node": "P3C_COVERAGE",
                    "upgrade3_policy_hash": _policy_hash,
                    "domain_contract_sha256": _domain_contract_hash,
                }
                return _node_integration.build_node_fingerprint_details(
                    node_id=node_id,
                    code_paths=code_paths,
                    prompt_context=prompt_context,
                    prompt_paths=prompt_paths,
                    prompt_functions=prompt_functions,
                    model_context=model_context,
                    policy_context=policy_context,
                    schema_context={
                        "schema_version": "research_harness.phase3_node_outputs.v1",
                        "outputs": sorted(outputs),
                    },
                )
            else:
                code_paths = [
                    Path(__file__),
                    Path(_node_integration.__file__),
                    _phase3_nodes_path,
                    _snapshot_path,
                    _r3_handoff_path,
                    _coverage_readiness_path,
                ]
                no_llm_contract = _node_integration.canonical_no_llm_contract()
                prompt_context = {
                    "prompt_contract": no_llm_contract,
                    "contract_hash": _node_integration.canonical_no_llm_contract_hash(),
                }
                prompt_paths = []
                prompt_functions = []
                model_context = {
                    "real_llm_claims": False,
                    "execution_mode": "no_llm",
                    "contract_hash": _node_integration.canonical_no_llm_contract_hash(),
                    "model_fingerprint": _node_integration.canonical_no_llm_contract_hash(),
                    "contract": no_llm_contract,
                }
                policy_context = {
                    **_node_policy_context,
                    "node_policy_boundary": "p3d_acceptance_handoff",
                    "admission_policy": "upgrade3.coverage_readiness.author_admission",
                    "admission_policy_path": str(_coverage_readiness_path),
                    "admission_policy_sha256": _node_integration.sha256_file(
                        _coverage_readiness_path
                    ),
                    "upgrade3_policy_hash": _policy_hash,
                }
            prompt_functions = [function for function in prompt_functions if function is not None]
            if model_context_override is not None:
                model_context = {**model_context, **dict(model_context_override)}
            if policy_context_override is not None:
                policy_context = {**policy_context, **dict(policy_context_override)}
            return _node_integration.build_node_fingerprint_details(
                node_id=node_id,
                code_paths=code_paths,
                prompt_context=prompt_context,
                prompt_paths=prompt_paths,
                prompt_functions=prompt_functions,
                model_context=model_context,
                policy_context=policy_context,
                schema_context={
                    "schema_version": "research_harness.phase3_node_outputs.v1",
                    "outputs": sorted(outputs),
                },
            )
        _node_model_context = {
            "real_llm_claims": self.real_llm_claims,
            "claim_pool_enabled": self.claim_pool_enabled,
            "real_llm_dag": self.real_llm_dag,
            "claim_model_tier": self.claim_model_tier,
            "dag_model_tier": self.dag_model_tier,
            "claim_decomposer_model_tier": self.claim_model_tier,
            "dag_model_tier_configured": self.dag_model_tier,
            "provider": "qwen" if self.real_llm_claims or self.real_llm_dag else "none",
            "budget_task_id": _budget_task_id,
            "qwen_base_url": _qwen_base_url,
        }
        _node_policy_context = {
            "max_m2a_input_tokens": self.max_m2a_input_tokens,
            "max_m2a_records": self.max_m2a_records,
            "max_dag_candidates": self.max_dag_candidates,
            "dag_claims_per_section": self.dag_claims_per_section,
            "dag_total_claims": self.dag_total_claims,
            "claim_pool_shortlist_limit": self.claim_pool_shortlist_limit,
            "claim_pool_served_limit": self.claim_pool_served_limit,
            "claim_pool_target_range": list(self.claim_pool_target_range),
            "authoring_core_chunk_limit": self.authoring_core_chunk_limit,
            "execute_coverage": self.execute_coverage,
            "section_ids_to_process": sorted(self.section_ids_to_process),
            "fresh_evidence_semantic_judge": self.enable_fresh_evidence_semantic_judge,
            "fresh_evidence_semantic_judge_model_tier": self.fresh_evidence_semantic_judge_model_tier,
            "upgrade3_policy_hash": _policy_hash,
            "domain_contract_sha256": _domain_contract_hash,
            "evidence_atom_record_limit": self.evidence_atom_record_limit,
        }
        model_context = _node_model_context
        policy_context = _node_policy_context
        # Evidence-first atoms: the canonical evidence objects this attempt is
        # allowed to reason about.  They are produced before the node payloads are
        # assembled so a failure here fails the node closed instead of silently
        # letting the claim producer work without atoms.
        _atom_payloads = self._p3a_atom_payloads()
        _p3a_payloads = {
            "SECTION_ARGUMENT_CONTRACTS.json": section_contracts_payload,
            "CANDIDATE_CLAIM_POOLS.json": candidate_claim_pools_payload,
            "CLAIM_GRAPH.json": claim_graph,
            "M2A_INPUT_PAYLOADS.json": m2a_input_payloads_payload,
            "RELATION_GRAPH_MIGRATED.json": relation_graph_migrated_payload,
            **_atom_payloads,
            **self._p3a_atomic_claim_payloads(),
            **self._p3a_mounting_payloads(),
            **self._p3a_blueprint_revision_payloads(),
        }
        _p3a_inputs = [
            _node_integration.input_row("node_input_snapshot", _node_input_snapshot_path)
        ]
        if _input_fingerprint.is_file():
            _p3a_inputs.append(
                _node_integration.input_row("phase3_input_fingerprint", _input_fingerprint)
            )
        _node_manifests: dict[str, dict[str, Any]] = {}
        _node_integration_failure: dict[str, Any] | None = None
        try:
            _p3a_fingerprint = _node_fingerprint("P3A_CLAIM_POOL", _p3a_payloads)
            _node_manifests["P3A_CLAIM_POOL"] = _node_integration.commit_node_artifacts(
                self.output_dir,
                node_id="P3A_CLAIM_POOL",
                generation_id=self.generation_id,
                attempt_id=self.attempt_id,
                inputs=_p3a_inputs,
                dependencies=[],
                fingerprints=_p3a_fingerprint["fingerprints"],
                fingerprint_sources=_p3a_fingerprint["sources"],
                outputs={
                    role: {"filename": role, "payload": payload}
                    for role, payload in _p3a_payloads.items()
                },
                root_filenames={role: role for role in _p3a_payloads},
                cost_receipt={"status": "phase3_node_zero_cost", "model_calls": 0},
                validation_receipt={"status": "phase3_node_payloads_constructed"},
            )
            _p3b_payloads = {
                "CLAIM_BINDINGS.json": claim_bindings_payload,
                "MATERIAL_BINDINGS.json": material_bindings_payload,
            }
            _p3b_fingerprint = _node_fingerprint("P3B_CLAIM_BINDING", _p3b_payloads)
            _node_manifests["P3B_CLAIM_BINDING"] = _node_integration.commit_node_artifacts(
                self.output_dir,
                node_id="P3B_CLAIM_BINDING",
                generation_id=self.generation_id,
                attempt_id=self.attempt_id,
                inputs=_node_integration.input_rows_from_manifest(
                    _node_manifests["P3A_CLAIM_POOL"]
                ),
                dependencies=[
                    _node_integration.dependency_row(
                        _node_manifests["P3A_CLAIM_POOL"]
                    )
                ],
                fingerprints=_p3b_fingerprint["fingerprints"],
                fingerprint_sources=_p3b_fingerprint["sources"],
                outputs={
                    role: {"filename": role, "payload": payload}
                    for role, payload in _p3b_payloads.items()
                },
                root_filenames={role: role for role in _p3b_payloads},
                cost_receipt={"status": "phase3_node_zero_cost", "model_calls": 0},
                validation_receipt={"status": "phase3_node_payloads_constructed"},
            )
            _p3c_payloads = {
                "SECTION_COVERAGE_VERDICTS.json": verdicts_payload,
                "COVERAGE_REQUESTS.json": coverage_requests_payload,
                "SYNTHESIS_BUNDLES.json": bundles_payload,
                "COVERAGE_ATLAS.json": updated_atlas,
            }
            _p3c_snapshot = build_phase3_state_snapshot(
                self.generation_id,
                self.attempt_id,
                self.blueprint,
                states,
                all_requests,
                claim_graph,
                {
                    "schema_version": "research_harness.phase3_relation_graph.v1",
                    "edges": migrated_edges,
                },
                relation_audit,
                updated_atlas,
                phase_run,
                llm_summary,
                upgrade3_blocked_sections,
                claim_bindings_by_section,
                upgrade3_verdicts,
            )
            _p3c_payloads["P3C_STATE_SNAPSHOT.json"] = _p3c_snapshot
            _p3c_fingerprint = _node_fingerprint("P3C_COVERAGE", _p3c_payloads)
            _node_manifests["P3C_COVERAGE"] = _node_integration.commit_node_artifacts(
                self.output_dir,
                node_id="P3C_COVERAGE",
                generation_id=self.generation_id,
                attempt_id=self.attempt_id,
                inputs=_node_integration.input_rows_from_manifest(
                    _node_manifests["P3B_CLAIM_BINDING"]
                ),
                dependencies=[
                    _node_integration.dependency_row(
                        _node_manifests["P3B_CLAIM_BINDING"]
                    )
                ],
                fingerprints=_p3c_fingerprint["fingerprints"],
                fingerprint_sources=_p3c_fingerprint["sources"],
                outputs={
                    role: {"filename": role, "payload": payload}
                    for role, payload in _p3c_payloads.items()
                },
                root_filenames={role: role for role in _p3c_payloads},
                cost_receipt={"status": "phase3_node_zero_cost", "model_calls": 0},
                validation_receipt={"status": "phase3_node_payloads_constructed"},
            )
        except Exception as _node_exc:
            _node_integration_failure = {
                "error_type": type(_node_exc).__name__,
                "reason": str(_node_exc)[:500],
            }
            if isinstance(_node_exc, _node_integration.ProjectionError):
                committed_manifest = dict(_node_exc.manifest)
                committed_node_id = str(committed_manifest.get("node_id") or "")
                if committed_node_id:
                    _node_manifests[committed_node_id] = committed_manifest
                _node_integration_failure.update({
                    "classification": "compatibility_projection_failed",
                    "node_state": "committed",
                    "node_id": committed_node_id,
                    "receipt_path": (
                        str(_node_exc.receipt_path)
                        if _node_exc.receipt_path else ""
                    ),
                })
            else:
                failed_node_id = (
                    "P3C_COVERAGE"
                    if "P3B_CLAIM_BINDING" in _node_manifests
                    else "P3B_CLAIM_BINDING"
                    if "P3A_CLAIM_POOL" in _node_manifests
                    else "P3A_CLAIM_POOL"
                )
                try:
                    failed_inputs = (
                        _node_integration.input_rows_from_manifest(
                            _node_manifests["P3B_CLAIM_BINDING"]
                        )
                        if failed_node_id == "P3C_COVERAGE"
                        else _node_integration.input_rows_from_manifest(
                            _node_manifests["P3A_CLAIM_POOL"]
                        )
                        if failed_node_id == "P3B_CLAIM_BINDING"
                        else _p3a_inputs
                    )
                    failed_dependencies = (
                        [_node_integration.dependency_row(_node_manifests["P3B_CLAIM_BINDING"])]
                        if failed_node_id == "P3C_COVERAGE"
                        else [_node_integration.dependency_row(_node_manifests["P3A_CLAIM_POOL"])]
                        if failed_node_id == "P3B_CLAIM_BINDING"
                        else []
                    )
                    _failed_fingerprint = _node_fingerprint(failed_node_id, {})
                    _node_manifests[failed_node_id] = _node_integration.record_failed_node(
                        self.output_dir,
                        node_id=failed_node_id,
                        generation_id=self.generation_id,
                        attempt_id=self.attempt_id,
                        inputs=failed_inputs,
                        dependencies=failed_dependencies,
                        fingerprints=_failed_fingerprint["fingerprints"],
                        fingerprint_sources=_failed_fingerprint["sources"],
                        failure=_node_integration_failure,
                    )
                except Exception as _record_exc:
                    _node_integration_failure["failure_record_error"] = str(_record_exc)[:300]
            binding_authority_ready = False
        if _node_integration_failure is not None:
            phase_status = "failed_closed"
            r4_ready_section_ids = []
            phase_run["status"] = phase_status
            phase_run["stop_reason"] = "phase3_node_integration_failed"
            phase_run["r4_ready_section_ids"] = []
            phase_run["partial_handoff_allowed"] = False
            phase_run["binding_authority_ready"] = False
            phase_run["phase3_node_integration_failure"] = _node_integration_failure
        phase_run["phase3_nodes"] = {
            node_id: {
                "state": manifest.get("state"),
                "generation_id": manifest.get("generation_id"),
                "attempt_id": manifest.get("attempt_id"),
                "manifest_sha256": manifest.get("manifest_sha256"),
            }
            for node_id, manifest in _node_manifests.items()
        }

        # P3D is allowed to run only after a clean P3C commit and a complete
        # binding authority.  The pre-P3D failure path retains the legacy
        # diagnostic projections because no P3D node exists to own them.
        if _node_integration_failure is not None or not binding_authority_ready:
            p3c_manifest_before_d = _node_manifests.get("P3C_COVERAGE")
            p3c_committed_before_d = bool(
                isinstance(p3c_manifest_before_d, Mapping)
                and p3c_manifest_before_d.get("state") == "committed"
            )
            # Once C is committed, the three D JSONs belong exclusively to the
            # P3D node.  Keep the old diagnostic projection only for a run
            # that failed before C could commit (the legacy C-failure path).
            if not p3c_committed_before_d:
                _write_json(self.output_dir / "PHASE3_RUN.json", phase_run)
            acceptance = self._acceptance(
                states=states,
                requests=all_requests,
                claim_graph=claim_graph,
                relation_audit=relation_audit,
                phase_run=phase_run,
                coverage_atlas=updated_atlas,
                llm_summary=llm_summary,
                upgrade3_blocked_sections=upgrade3_blocked_sections,
            )
            acceptance["binding_authority_ready"] = binding_authority_ready
            handoff_path = self.output_dir / R3_HANDOFF_FILENAME
            acceptance["r3_production_handoff"] = {
                "status": "skipped_binding_authority_unavailable",
                "path": str(handoff_path),
                "existing_target_conflict": handoff_path.exists(),
            }
            # The target is never read or overwritten on this path.  An
            # existing file is reported as a conflict so it cannot be
            # mistaken for a handoff produced by this failed attempt.
            acceptance["upgrade3_coverage_verdicts"] = {
                "schema_version": "optomind.upgrade3.section_coverage_verdicts.v1",
                "blocked_sections": sorted(
                    getattr(self, "_upgrade3_blocked_sections", set())),
                "verdicts": getattr(self, "_upgrade3_verdicts", {}),
            }
            if not p3c_committed_before_d:
                _write_json(self.output_dir / "PHASE3_ACCEPTANCE.json", acceptance)
                self._write_markdown(acceptance, phase_run)
            return acceptance

        p3c_manifest = _node_manifests.get("P3C_COVERAGE")
        try:
            if not isinstance(p3c_manifest, Mapping):
                raise RuntimeError("p3d_requires_committed_p3c")
            p3d_result = self._execute_p3d_from_committed_p3c(
                p3c_manifest,
                self.attempt_id,
            )
            _node_manifests["P3D_ACCEPTANCE_HANDOFF"] = p3d_result["manifest"]
            self._write_markdown(
                p3d_result["acceptance"],
                p3d_result["phase_run"],
            )
            return p3d_result["acceptance"]
        except Exception as _p3d_exc:
            p3d_failure = {
                "error_type": type(_p3d_exc).__name__,
                "reason": str(_p3d_exc)[:500],
                "classification": "p3d_acceptance_handoff_failed",
                "node_id": "P3D_ACCEPTANCE_HANDOFF",
            }
            if isinstance(_p3d_exc, _node_integration.ProjectionError):
                committed_manifest = dict(_p3d_exc.manifest)
                _node_manifests["P3D_ACCEPTANCE_HANDOFF"] = committed_manifest
                p3d_failure.update({
                    "classification": "compatibility_projection_failed",
                    "node_state": "committed",
                    "receipt_path": (
                        str(_p3d_exc.receipt_path)
                        if _p3d_exc.receipt_path else ""
                    ),
                })
            elif isinstance(p3c_manifest, Mapping):
                try:
                    p3d_fingerprint = _node_fingerprint(
                        "P3D_ACCEPTANCE_HANDOFF",
                        {},
                        policy_context_override={
                            "p3c_manifest_sha256": str(
                                p3c_manifest.get("manifest_sha256") or ""
                            ),
                            "admission_policy": "upgrade3.coverage_readiness.author_admission",
                            "admission_policy_path": str(_coverage_readiness_path),
                        },
                    )
                    failed = _node_integration.record_failed_node(
                        self.output_dir,
                        node_id="P3D_ACCEPTANCE_HANDOFF",
                        generation_id=self.generation_id,
                        attempt_id=self.attempt_id,
                        inputs=_node_integration.input_rows_from_manifest(p3c_manifest),
                        dependencies=[_node_integration.dependency_row(p3c_manifest)],
                        fingerprints=p3d_fingerprint["fingerprints"],
                        fingerprint_sources=p3d_fingerprint["sources"],
                        failure=p3d_failure,
                    )
                    _node_manifests["P3D_ACCEPTANCE_HANDOFF"] = failed
                except Exception as _record_p3d_exc:
                    p3d_failure["failure_record_error"] = str(_record_p3d_exc)[:300]
            phase_run["status"] = "failed_closed"
            phase_run["stop_reason"] = "phase3_node_integration_failed"
            phase_run["r4_ready_section_ids"] = []
            phase_run["partial_handoff_allowed"] = False
            phase_run["binding_authority_ready"] = False
            phase_run["phase3_node_integration_failure"] = p3d_failure
            phase_run["phase3_nodes"] = {
                node_id: {
                    "state": manifest.get("state"),
                    "generation_id": manifest.get("generation_id"),
                    "attempt_id": manifest.get("attempt_id"),
                    "manifest_sha256": manifest.get("manifest_sha256"),
                }
                for node_id, manifest in _node_manifests.items()
            }
            failure_acceptance = self._acceptance(
                states=states,
                requests=all_requests,
                claim_graph=claim_graph,
                relation_audit=relation_audit,
                phase_run=phase_run,
                coverage_atlas=updated_atlas,
                llm_summary=llm_summary,
                upgrade3_blocked_sections=upgrade3_blocked_sections,
            )
            failure_acceptance["binding_authority_ready"] = False
            failure_acceptance["r3_production_handoff"] = {
                "status": "skipped_p3d_unavailable",
                "path": str(self.output_dir / R3_HANDOFF_FILENAME),
                "existing_target_conflict": (
                    self.output_dir / R3_HANDOFF_FILENAME
                ).exists(),
                "p3d_failure": dict(p3d_failure),
            }
            failure_acceptance["upgrade3_coverage_verdicts"] = {
                "schema_version": "optomind.upgrade3.section_coverage_verdicts.v1",
                "blocked_sections": sorted(
                    getattr(self, "_upgrade3_blocked_sections", set())),
                "verdicts": getattr(self, "_upgrade3_verdicts", {}),
            }
            # P3D owns these three JSON artifacts.  A failed P3D attempt must
            # not overwrite an older root generation with an in-memory error
            # projection.
            return failure_acceptance

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
        upgrade3_blocked_sections: Iterable[str],
    ) -> dict[str, Any]:
        blocked_sections = {str(section_id) for section_id in
                           (upgrade3_blocked_sections or ())}
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
            and sid not in blocked_sections
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
        evidence_traceability_passed = True
        traceability_audit: list[dict[str, Any]] = []
        for state in states:
            record_by_id = {
                str(row.get("chunk_id")): row
                for row in state.get("records", [])
                if isinstance(row, Mapping) and row.get("chunk_id")
            }
            for claim_id, binding in (
                state.get("bindings", {}).get("claims", {}) or {}
            ).items():
                if not isinstance(binding, Mapping):
                    continue
                support_ids = binding.get("supporting_chunk_ids") or []
                if isinstance(support_ids, str):
                    support_ids = [support_ids]
                for chunk_id in support_ids:
                    if str(chunk_id) in record_by_id:
                        continue
                    evidence_traceability_passed = False
                    traceability_audit.append({
                        "section_id": state["section"]["section_id"],
                        "claim_id": str(claim_id),
                        "chunk_id": str(chunk_id),
                        "reason": "supporting_chunk_id_missing_from_records",
                    })
        evidence_permission_passed = True
        permission_audit: list[dict[str, Any]] = []
        for state in states:
            record_by_id = {str(row.get("chunk_id")): row for row in state.get("records", [])}
            for claim_id, binding in (state.get("bindings", {}).get("claims", {}) or {}).items():
                for chunk_id in binding.get("supporting_chunk_ids", []):
                    row = record_by_id.get(str(chunk_id))
                    if not row:
                        # Missing record identity is an engineering failure
                        # reported by evidence_traceability_passed above, not
                        # a scientific permission verdict.
                        continue
                    ceiling, reason = evidence_ceiling(row)
                    if ceiling == DISCOVERY:
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
            and evidence_traceability_passed
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
        decision_gates = (
            ("engineering_passed", engineering_passed),
            ("evidence_traceability_passed", evidence_traceability_passed),
            ("claim_quality_passed", claim_quality_passed),
            ("evidence_permission_passed", evidence_permission_passed),
            ("coverage_request_quality_passed", coverage_request_quality_passed),
            ("argument_task_coverage_passed", task_coverage_passed),
            ("effective_statement_propagation_passed", effective_statement_propagation),
            ("verifier_batch_budget_passed", verifier_batch_budget_passed),
            ("handoff_ready", handoff_ready),
        )
        failed_checks = [name for name, passed in decision_gates if not passed]
        if overall_passed:
            decision_class = "accepted"
        elif not engineering_passed:
            decision_class = "failed_engineering"
        else:
            decision_class = "blocked_science"
        input_budget_status = (
            "passed"
            if input_budget_passed
            else "warning_exceeds_aggregate_observability_budget"
        )
        return {
            "schema_version": "research_harness.phase3_acceptance.v1",
            "status": "passed" if overall_passed else "failed",
            "decision_class": decision_class,
            "failed_checks": failed_checks,
            "blocked_sections": sorted(blocked_sections),
            "r4_entered": False,
            "r4_handoff_ready": handoff_ready and overall_passed,
            "partial_handoff_allowed": handoff_ready and overall_passed,
            "r4_ready_section_ids": r4_ready_section_ids if overall_passed else [],
            "section_outcomes": outcome_by_section,
            "engineering_passed": engineering_passed,
            "claim_quality_passed": claim_quality_passed,
            "evidence_traceability_passed": evidence_traceability_passed,
            "traceability_audit": traceability_audit,
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
