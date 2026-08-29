"""Canonical Phase-2 asset graph for section authoring.

The graph is the single trust boundary between Phase 2 artifacts/KBs and the
Phase 3 authoring tools.  Caller-supplied identifiers, ownership, text, visual
status, and paths are never treated as authoritative.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Optional

from .review_quality_contract import (
    normalize_content_depth,
    normalize_scope_fit,
    permission_for_content,
)
from .section_asset_overlay import read_section_asset_overlay

logger = logging.getLogger(__name__)

ACCEPTED_VISUAL_STATUSES = frozenset({"ok", "accepted", "approved", "verified"})
ALLOWED_VISUAL_KINDS = frozenset({"single_figure", "subfigure"})


def normalize_text(value: Any) -> str:
    """Normalize Unicode and whitespace without truncating evidence text."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _read_json(path: Optional[Path]) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        logger.warning("Could not read Phase-2 artifact %s: %s", path, exc)
        return {}


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


@dataclass(frozen=True)
class PaperAsset:
    paper_id: str
    title: str = ""
    year: Optional[int] = None
    literature_role: str = ""
    scope_fit: str = "unreviewed"
    acquisition_status: str = "unknown"
    not_usable_for: tuple[str, ...] = ()
    discovery_route: str = "unknown"
    materialization_route: str = "not_materialized"
    content_depth: str = "metadata"
    use_permission: str = "discovery_only"
    allowed_claim_kinds: tuple[str, ...] = ()
    route_events: tuple[dict[str, Any], ...] = ()
    metadata_conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChunkAsset:
    chunk_id: str
    paper_id: str
    normalized_text: str
    # Stable location inside the source document.  These fields were present
    # in the SQLite material table but were previously discarded at the
    # canonical graph boundary, making paragraph-level evidence binding
    # impossible for Phase 3.
    ordinal: Optional[int] = None
    section_path: str = ""
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    source_locator: dict[str, Any] = field(default_factory=dict)
    evidence_level: str = "fulltext"
    source_kind: str = "fulltext"
    paper_title: str = ""
    paper_year: Optional[int] = None
    literature_role: str = ""
    scope_fit: str = "unreviewed"
    not_usable_for: tuple[str, ...] = ()
    source_kb: str = ""
    discovery_route: str = "unknown"
    materialization_route: str = "not_materialized"
    content_depth: str = "metadata"
    context_complete: bool = False
    use_permission: str = "discovery_only"
    allowed_claim_kinds: tuple[str, ...] = ()
    route_provenance: dict[str, Any] = field(default_factory=dict)
    relation_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class VisualAsset:
    visual_id: str
    paper_id: str
    status: str
    kind: str
    local_image_path: str
    caption: str = ""
    argument_type: str = ""
    argument_claim: str = ""
    parent_label: str = ""
    subfigure_label: str = ""
    relevance_status: str = ""
    source_kb: str = ""

    @property
    def accepted(self) -> bool:
        return (
            self.status.lower() in ACCEPTED_VISUAL_STATUSES
            and self.kind.lower() in ALLOWED_VISUAL_KINDS
        )

    @property
    def relevant_or_reranked(self) -> bool:
        status = self.relevance_status.lower()
        return status in {
            "direct", "partial", "relevant", "accepted", "approved", "reranked",
            "provided_by_blueprint", "auto_recommended_from_kb", "claim_retrieved_from_kb",
            # ReviewKnowledgeBase stores the reviewed visual usefulness in
            # ``review_utility``.  The canonical loader intentionally folds
            # that column into ``relevance_status`` when a dedicated rerank
            # verdict is absent.  High/medium assets are therefore eligible;
            # low-utility assets remain excluded.
            "high", "medium",
        }


@dataclass
class CanonicalAssetGraph:
    papers: dict[str, PaperAsset] = field(default_factory=dict)
    chunks: dict[str, ChunkAsset] = field(default_factory=dict)
    visuals: dict[str, VisualAsset] = field(default_factory=dict)
    expected_chunk_ids: set[str] = field(default_factory=set)
    source_kbs: tuple[str, ...] = ()
    diagnostics: list[str] = field(default_factory=list)
    unresolved_asset_audit: list[dict[str, Any]] = field(default_factory=list)
    invalid_id_audit: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.papers and not self.chunks and not self.visuals

    @property
    def has_usable_text(self) -> bool:
        return any(chunk.normalized_text for chunk in self.chunks.values())


def _existing_unique_paths(paths: Iterable[Optional[Path]]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        if raw is None:
            continue
        path = Path(raw)
        key = str(path.resolve()).lower() if path.exists() else str(path.absolute()).lower()
        if path.exists() and key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _select_rows(conn: sqlite3.Connection, table: str, wanted: list[str]) -> list[dict[str, Any]]:
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if not columns:
        return []
    selected = [name for name in wanted if name in columns]
    if not selected:
        return []
    rows = conn.execute(f"SELECT {', '.join(selected)} FROM {table} ORDER BY rowid").fetchall()
    return [dict(zip(selected, row)) for row in rows]


def _coalesce(row: dict[str, Any], raw: dict[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
        value = raw.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def build_canonical_asset_graph(
    *,
    material_package_path: Optional[Path],
    source_ledger_path: Optional[Path],
    work_dir: Path,
    kb_paths: Iterable[Optional[Path]],
    overlay_path: Optional[Path] = None,
) -> CanonicalAssetGraph:
    """Build a deterministic graph from Phase-2 artifacts and every available KB.

    The source ledger is authoritative for allowed papers.  KB rows are accepted
    only when their paper is in that ledger.  Main/staging paths are traversed in
    caller order and first occurrence wins, making duplicate resolution stable.
    """
    mp_path = material_package_path or (work_dir / "SECTION_MATERIAL_PACKAGE.json")
    sl_path = source_ledger_path or (work_dir / "SECTION_SOURCE_LEDGER.json")
    material = _read_json(mp_path)
    ledger = _read_json(sl_path)
    graph = CanonicalAssetGraph()
    chunk_policies: dict[str, dict[str, Any]] = {}

    for source in ledger.get("sources", []):
        if not isinstance(source, dict):
            continue
        paper_id = str(source.get("paper_id") or "").strip()
        if not paper_id:
            continue
        try:
            year = int(source["year"]) if source.get("year") is not None else None
        except (TypeError, ValueError):
            year = None
        role = str(source.get("literature_role") or "")
        scope = normalize_scope_fit(source.get("scope_fit"))
        acquisition = str(source.get("acquisition_status") or "unknown").lower()
        raw_source = _json_dict(source.get("raw_json") or source.get("provenance_json"))
        source_depth = normalize_content_depth(
            source.get("content_depth")
            or raw_source.get("content_depth")
            or ("fulltext" if acquisition == "fulltext" else "metadata")
        )
        source_context_complete = bool(
            source.get("context_complete", source_depth in {"fulltext", "structured_snippet"})
        )
        source_permission = str(source.get("use_permission") or "").strip()
        if not source_permission:
            source_permission = str(
                permission_for_content(
                    source_depth,
                    scope_fit=scope,
                    context_complete=source_context_complete,
                )["use_permission"]
            )
        source_allowed = tuple(
            str(item)
            for item in (
                source.get("allowed_claim_kinds")
                or permission_for_content(
                    source_depth,
                    scope_fit=scope,
                    context_complete=source_context_complete,
                )["allowed_claim_kinds"]
            )
        )
        discovery_route = str(
            source.get("discovery_route")
            or raw_source.get("discovery_route")
            or ""
        ).strip()
        route_unresolved = discovery_route.casefold() in {
            "",
            "unknown",
            "legacy_unresolved",
        }
        if route_unresolved:
            graph.unresolved_asset_audit.append(
                {
                    "asset_type": "paper",
                    "paper_id": paper_id,
                    "reason": "legacy_route_missing_or_unconfirmed",
                    "conservative_permission": "discovery_only",
                }
            )
            # A legacy acquisition status is not proof of provenance.  Until
            # migration recovers a route from the KB/path evidence, keep this
            # paper at discovery-only permission.
            source_depth = "metadata"
            source_context_complete = False
            source_permission = "discovery_only"
            source_allowed = ("discovery", "candidate_lead")
        restrictions = tuple(
            normalize_text(item)
            for item in source.get("not_usable_for", [])
            if normalize_text(item)
        )
        previous = graph.papers.get(paper_id)
        if previous is None:
            graph.papers[paper_id] = PaperAsset(
                paper_id=paper_id,
                title=normalize_text(source.get("title")),
                year=year,
                literature_role=role,
                scope_fit=scope,
                acquisition_status=acquisition,
                not_usable_for=restrictions,
                discovery_route=discovery_route or "legacy_unresolved",
                materialization_route=str(
                    source.get("materialization_route") or "not_materialized"
                ),
                content_depth=source_depth,
                use_permission=source_permission,
                allowed_claim_kinds=source_allowed,
                route_events=tuple(source.get("route_events") or ()),
                metadata_conflicts=tuple(source.get("metadata_conflicts") or ()),
            )
        else:
            scope_rank = {
                "out_of_scope": 0,
                "unreviewed": 1,
                "contextual": 2,
                "adjacent": 3,
                "direct": 4,
            }
            acquisition_rank = {
                "failed": 0,
                "not_attempted": 1,
                "metadata_only": 2,
                "abstract_only": 3,
                "fulltext": 4,
            }
            merged_roles = ", ".join(dict.fromkeys(
                item for item in [*previous.literature_role.split(", "), role] if item
            ))
            graph.papers[paper_id] = PaperAsset(
                paper_id=paper_id,
                title=previous.title or normalize_text(source.get("title")),
                year=previous.year if previous.year is not None else year,
                literature_role=merged_roles,
                scope_fit=max(
                    (previous.scope_fit, scope),
                    key=lambda item: scope_rank.get(item, 0),
                ),
                acquisition_status=max(
                    (previous.acquisition_status, acquisition),
                    key=lambda item: acquisition_rank.get(item, 0),
                ),
                not_usable_for=tuple(dict.fromkeys([
                    *previous.not_usable_for,
                    *restrictions,
                ])),
                discovery_route=previous.discovery_route or str(
                    source.get("discovery_route") or "unknown"
                ),
                materialization_route=previous.materialization_route or str(
                    source.get("materialization_route") or "not_materialized"
                ),
                content_depth=previous.content_depth or source_depth,
                use_permission=previous.use_permission or source_permission,
                allowed_claim_kinds=tuple(
                    dict.fromkeys(
                        [*previous.allowed_claim_kinds, *source_allowed]
                    )
                ),
                route_events=tuple(
                    [*previous.route_events, *(source.get("route_events") or [])]
                ),
                metadata_conflicts=tuple(
                    dict.fromkeys(
                        [*previous.metadata_conflicts, *tuple(source.get("metadata_conflicts") or ())]
                    )
                ),
            )
        graph.expected_chunk_ids.update(
            str(item).strip() for item in source.get("canonical_chunk_ids", []) if str(item).strip()
        )
        for raw_chunk_id in source.get("canonical_chunk_ids", []):
            chunk_id = str(raw_chunk_id).strip()
            if not chunk_id:
                continue
            policy = chunk_policies.setdefault(chunk_id, {
                "roles": [],
                "scope_fit": scope,
                "not_usable_for": [],
            })
            if role and role not in policy["roles"]:
                policy["roles"].append(role)
            policy["not_usable_for"] = list(dict.fromkeys([
                *policy["not_usable_for"],
                *restrictions,
            ]))
            scope_rank = {
                "out_of_scope": 0,
                "unreviewed": 1,
                "contextual": 2,
                "adjacent": 3,
                "direct": 4,
            }
            if scope_rank.get(scope, 0) > scope_rank.get(policy["scope_fit"], 0):
                policy["scope_fit"] = scope

    for role_ids in (material.get("chunk_ids_by_role") or {}).values():
        if isinstance(role_ids, list):
            graph.expected_chunk_ids.update(str(item).strip() for item in role_ids if str(item).strip())

    paths = _existing_unique_paths(kb_paths)
    graph.source_kbs = tuple(str(path) for path in paths)
    for kb_path in paths:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(str(kb_path))
            text_rows = _select_rows(conn, "text_chunks", [
                "chunk_id", "paper_id", "title", "text", "ordinal", "section_path",
                "char_start", "char_end", "evidence_level", "source_kind",
                "raw_json", "provenance_json", "discovery_route",
                "materialization_route", "content_depth", "context_complete",
                "use_permission", "allowed_claim_kinds_json",
                "route_provenance_json", "scope_fit", "relation_roles_json",
            ])
            for row in text_rows:
                chunk_id = str(row.get("chunk_id") or "").strip()
                paper_id = str(row.get("paper_id") or "").strip()
                if (
                    not chunk_id
                    or paper_id not in graph.papers
                    or chunk_id in graph.chunks
                    or (
                        graph.expected_chunk_ids
                        and chunk_id not in graph.expected_chunk_ids
                    )
                ):
                    continue
                paper = graph.papers[paper_id]
                policy = chunk_policies.get(chunk_id, {})
                raw_chunk = _json_dict(row.get("raw_json"))
                provenance = _json_dict(
                    row.get("route_provenance_json")
                    or row.get("provenance_json")
                    or raw_chunk.get("route_provenance")
                )
                chunk_discovery_route = str(
                    row.get("discovery_route")
                    or provenance.get("discovery_route")
                    or paper.discovery_route
                    or ""
                ).strip()
                chunk_route_unresolved = chunk_discovery_route.casefold() in {
                    "",
                    "unknown",
                    "legacy_unresolved",
                }
                chunk_depth = normalize_content_depth(
                    row.get("content_depth")
                    or provenance.get("content_depth")
                    or raw_chunk.get("content_depth")
                    or row.get("source_kind")
                )
                chunk_scope = normalize_scope_fit(
                    # The section source ledger is the section-level trust
                    # boundary.  A stale KB row must not silently promote an
                    # adjacent source back to direct scope.
                    policy.get("scope_fit")
                    or row.get("scope_fit")
                    or provenance.get("scope_fit")
                    or paper.scope_fit
                )
                chunk_complete = bool(
                    row.get("context_complete")
                    if row.get("context_complete") is not None
                    else provenance.get("context_complete", chunk_depth == "fulltext")
                )
                permission = permission_for_content(
                    chunk_depth,
                    scope_fit=chunk_scope,
                    context_complete=chunk_complete,
                )
                if chunk_route_unresolved:
                    graph.unresolved_asset_audit.append(
                        {
                            "asset_type": "text_chunk",
                            "chunk_id": chunk_id,
                            "paper_id": paper_id,
                            "reason": "chunk_route_missing_or_unconfirmed",
                            "conservative_permission": "discovery_only",
                        }
                    )
                    # Keep the locally observed parsing depth as a content
                    # fact, but recompute permission below from the
                    # unresolved route.  Content depth and evidence
                    # permission are independent axes.
                    chunk_complete = False
                    # Keep the independently assessed scope_fit visible.  An
                    # unresolved acquisition route lowers permission, but it
                    # must not erase the domain-boundary signal (for example,
                    # an adjacent source must still be rejected for an
                    # unqualified factual assertion).
                    permission = permission_for_content(
                        "metadata",
                        scope_fit=chunk_scope,
                        context_complete=False,
                    )
                allowed_raw = row.get("allowed_claim_kinds_json")
                allowed = (
                    json.loads(str(allowed_raw))
                    if allowed_raw
                    else provenance.get("allowed_claim_kinds")
                )
                if not isinstance(allowed, list):
                    allowed = permission["allowed_claim_kinds"]
                if chunk_route_unresolved:
                    allowed = list(permission["allowed_claim_kinds"])
                relation_roles = row.get("relation_roles_json")
                try:
                    relation_roles = json.loads(str(relation_roles or "[]"))
                except Exception:
                    relation_roles = provenance.get("relation_roles", [])
                if not isinstance(relation_roles, list):
                    relation_roles = []
                raw_locator = raw_chunk.get("source_locator")
                if not isinstance(raw_locator, dict):
                    raw_locator = provenance.get("source_locator")
                if not isinstance(raw_locator, dict):
                    raw_locator = {}
                section_path = normalize_text(row.get("section_path"))
                ordinal_raw = row.get("ordinal")
                char_start_raw = row.get("char_start")
                char_end_raw = row.get("char_end")
                try:
                    ordinal = int(ordinal_raw) if ordinal_raw not in (None, "") else None
                except (TypeError, ValueError):
                    ordinal = None
                try:
                    char_start = int(char_start_raw) if char_start_raw not in (None, "") else None
                except (TypeError, ValueError):
                    char_start = None
                try:
                    char_end = int(char_end_raw) if char_end_raw not in (None, "") else None
                except (TypeError, ValueError):
                    char_end = None
                source_locator = dict(raw_locator)
                source_locator.setdefault("chunk_id", chunk_id)
                source_locator.setdefault("paper_id", paper_id)
                if section_path:
                    source_locator.setdefault("section_path", section_path)
                if ordinal is not None:
                    source_locator.setdefault("ordinal", ordinal)
                if char_start is not None:
                    source_locator.setdefault("char_start", char_start)
                if char_end is not None:
                    source_locator.setdefault("char_end", char_end)
                graph.chunks[chunk_id] = ChunkAsset(
                    chunk_id=chunk_id,
                    paper_id=paper_id,
                    normalized_text=normalize_text(row.get("text")),
                    ordinal=ordinal,
                    section_path=section_path,
                    char_start=char_start,
                    char_end=char_end,
                    source_locator=source_locator,
                    evidence_level=str(row.get("evidence_level") or "fulltext"),
                    source_kind=str(row.get("source_kind") or "fulltext"),
                    paper_title=normalize_text(row.get("title")) or paper.title,
                    paper_year=paper.year,
                    literature_role=", ".join(policy.get("roles", [])),
                    scope_fit=chunk_scope,
                    not_usable_for=tuple(policy.get("not_usable_for") or ()),
                    source_kb=str(kb_path),
                    discovery_route=chunk_discovery_route or "legacy_unresolved",
                    materialization_route=str(
                        row.get("materialization_route")
                        or provenance.get("materialization_route")
                        or paper.materialization_route
                        or "not_materialized"
                    ),
                    content_depth=chunk_depth,
                    context_complete=chunk_complete,
                    use_permission=(
                        permission["use_permission"]
                        if chunk_route_unresolved
                        else str(
                            row.get("use_permission")
                            or provenance.get("use_permission")
                            or permission["use_permission"]
                        )
                    ),
                    allowed_claim_kinds=tuple(str(item) for item in allowed),
                    route_provenance=provenance,
                    relation_roles=tuple(str(item) for item in relation_roles),
                )

            visual_rows = _select_rows(conn, "visual_chunks", [
                "chunk_id", "visual_chunk_id", "paper_id", "caption", "local_image_path",
                "chunk_kind", "asset_kind", "visual_argument_type", "visual_argument_claim",
                "visual_argument_status", "status", "parent_label", "subfigure_label",
                "review_utility", "relevance_status", "rerank_verdict", "raw_json",
            ])
            for row in visual_rows:
                raw = _json_dict(row.get("raw_json"))
                visual_id = _coalesce(row, raw, "chunk_id", "visual_chunk_id").strip()
                paper_id = _coalesce(row, raw, "paper_id").strip()
                if not visual_id or paper_id not in graph.papers or visual_id in graph.visuals:
                    continue
                graph.visuals[visual_id] = VisualAsset(
                    visual_id=visual_id,
                    paper_id=paper_id,
                    status=_coalesce(row, raw, "visual_argument_status", "status").lower(),
                    kind=_coalesce(row, raw, "chunk_kind", "asset_kind").lower(),
                    local_image_path=_coalesce(row, raw, "local_image_path"),
                    caption=normalize_text(_coalesce(row, raw, "caption")),
                    argument_type=_coalesce(row, raw, "visual_argument_type"),
                    argument_claim=normalize_text(_coalesce(row, raw, "visual_argument_claim")),
                    parent_label=_coalesce(row, raw, "parent_label"),
                    subfigure_label=_coalesce(row, raw, "subfigure_label"),
                    relevance_status=_coalesce(
                        row, raw, "rerank_verdict", "relevance_status", "review_utility"
                    ),
                    source_kb=str(kb_path),
                )
        except Exception as exc:
            graph.diagnostics.append(f"{kb_path}: {type(exc).__name__}: {exc}")
            logger.warning("Could not load authoring assets from %s: %s", kb_path, exc)
        finally:
            if conn is not None:
                conn.close()

    # Reconcile paper-level depth from the strongest verified local chunk.
    # A stale ledger may say ``metadata`` even though the shared KB contains
    # parsed full text.  This updates depth only; scope and use permission are
    # deliberately left independent so method-transfer material cannot become
    # direct evidence merely because a full text file exists.
    depth_rank = {
        "metadata": 0,
        "abstract": 1,
        "structured_snippet": 2,
        "fulltext": 3,
    }
    best_by_paper: dict[str, str] = {}
    for chunk in graph.chunks.values():
        if not str(chunk.normalized_text or "").strip():
            continue
        depth = normalize_content_depth(chunk.content_depth)
        if depth_rank.get(depth, 0) > depth_rank.get(best_by_paper.get(chunk.paper_id, "metadata"), 0):
            best_by_paper[chunk.paper_id] = depth
    for paper_id, best_depth in best_by_paper.items():
        paper = graph.papers.get(paper_id)
        if paper is None:
            continue
        if depth_rank.get(best_depth, 0) > depth_rank.get(normalize_content_depth(paper.content_depth), 0):
            graph.papers[paper_id] = replace(paper, content_depth=best_depth)
    if best_by_paper:
        graph.diagnostics.append(
            f"paper_content_depth_aggregated_from_{len(best_by_paper)}_local_papers"
        )

    # Apply lightweight section routing after reading the shared KB.  The
    # overlay can narrow the active set and carry section-specific permission
    # decisions without creating another SQLite copy.
    overlay = read_section_asset_overlay(overlay_path)
    if overlay:
        active_papers = set(str(item) for item in overlay.get("paper_ids") or [] if str(item).strip())
        active_chunks = set(str(item) for item in overlay.get("chunk_ids") or [] if str(item).strip())
        if active_papers:
            graph.papers = {
                paper_id: paper
                for paper_id, paper in graph.papers.items()
                if paper_id in active_papers
            }
        if active_chunks:
            graph.chunks = {
                chunk_id: chunk
                for chunk_id, chunk in graph.chunks.items()
                if chunk_id in active_chunks and chunk.paper_id in graph.papers
            }
        for paper_id, override in (overlay.get("paper_overrides") or {}).items():
            paper = graph.papers.get(str(paper_id))
            if paper is None or not isinstance(override, dict):
                continue
            graph.papers[str(paper_id)] = replace(
                paper,
                scope_fit=str(override.get("scope_fit") or paper.scope_fit),
                use_permission=str(override.get("use_permission") or paper.use_permission),
                literature_role=str(override.get("literature_role") or paper.literature_role),
                discovery_route=str(override.get("discovery_route") or paper.discovery_route),
                materialization_route=str(override.get("materialization_route") or paper.materialization_route),
            )
        for chunk_id, override in (overlay.get("chunk_overrides") or {}).items():
            chunk = graph.chunks.get(str(chunk_id))
            if chunk is None or not isinstance(override, dict):
                continue
            graph.chunks[str(chunk_id)] = replace(
                chunk,
                scope_fit=str(override.get("scope_fit") or chunk.scope_fit),
                use_permission=str(override.get("use_permission") or chunk.use_permission),
                literature_role=str(override.get("literature_role") or chunk.literature_role),
            )
        graph.expected_chunk_ids = set(active_chunks or graph.chunks)
        graph.diagnostics.append("section_overlay_applied_without_database_copy")

    if not graph.papers:
        graph.diagnostics.append("No allowed paper IDs were found in SECTION_SOURCE_LEDGER.json.")
    missing_expected = sorted(graph.expected_chunk_ids - set(graph.chunks))
    if missing_expected:
        graph.diagnostics.append(
            f"{len(missing_expected)} Phase-2 chunk ID(s) were absent from both KBs: "
            + ", ".join(missing_expected[:8])
        )
    return graph
