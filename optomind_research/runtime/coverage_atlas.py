"""Deterministic role/relation coverage atlas for a review blueprint."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .review_quality_contract import resolve_review_contract


ROLE_ORDER = (
    "foundation",
    "mechanism",
    "method",
    "frontier",
    "controversy",
    "application",
)
RELATION_TASKS = (
    "progression",
    "complementarity",
    "controversy",
    "tradeoff",
    "boundary",
)

_SEMANTIC_TO_TASK = {
    "foundation_of": "progression",
    "extends": "progression",
    "progression": "progression",
    "complements": "complementarity",
    "complementarity": "complementarity",
    "contradicts": "controversy",
    "controversy": "controversy",
    "compares_with": "tradeoff",
    "tradeoff": "tradeoff",
    "sets_boundary_for": "boundary",
    "boundary": "boundary",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _role_target(section: dict[str, Any], role: str) -> int:
    target = section.get("role_source_targets")
    if isinstance(target, dict):
        try:
            return max(0, int(target.get(role) or 0))
        except (TypeError, ValueError):
            return 0
    required = set(str(item) for item in section.get("required_roles", []))
    optional = set(str(item) for item in section.get("optional_roles", []))
    if role in required:
        return 2
    if role in optional:
        return 1
    return 0


def _load_relation_edges(blueprint: dict[str, Any], coverage_root: Path) -> list[dict[str, Any]]:
    embedded = (
        blueprint.get("relation_graph")
        or blueprint.get("literature_relation_graph")
        or {}
    )
    candidates: list[dict[str, Any]] = []
    if isinstance(embedded, dict):
        candidates = [
            item for item in embedded.get("edges", [])
            if isinstance(item, dict)
        ]
    if candidates:
        return candidates
    names = {
        "LITERATURE_RELATION_GRAPH.json",
        "S2_LITERATURE_GRAPH.json",
        "RELATION_GRAPH.json",
        "relation_graph.json",
        "S2_RELATION_GRAPH.json",
    }
    for path in [coverage_root / name for name in names]:
        if path.exists():
            payload = _read_json(path)
            candidates.extend(
                item for item in payload.get("edges", [])
                if isinstance(item, dict)
            )
    if not candidates and coverage_root.exists():
        for path in coverage_root.rglob("*.json"):
            if path.name not in names:
                continue
            payload = _read_json(path)
            candidates.extend(
                item for item in payload.get("edges", [])
                if isinstance(item, dict)
            )
    dedup: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = str(
            item.get("edge_id")
            or f"{item.get('source_paper_id')}|{item.get('target_paper_id')}|"
            f"{item.get('observed_relation') or item.get('edge_type')}"
        )
        dedup[key] = item
    return list(dedup.values())


def build_coverage_atlas(
    *,
    blueprint: dict[str, Any],
    coverage_root: Path,
    scope_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build role and relationship coverage without judging scientific truth."""

    sections = [
        item for item in blueprint.get("sections", [])
        if isinstance(item, dict) and item.get("section_id")
    ]
    contract = resolve_review_contract(blueprint)
    scope_map = scope_map or blueprint.get("review_scope_map") or {}
    active_roles = {
        str(item) for item in (scope_map.get("literature_roles") or [])
        if str(item) in ROLE_ORDER
    }
    active_relation_tasks = {
        str(item) for item in (scope_map.get("relation_tasks") or [])
        if str(item) in RELATION_TASKS
    }
    relation_edges = _load_relation_edges(blueprint, Path(coverage_root))
    section_rows: list[dict[str, Any]] = []
    total_papers: set[str] = set()
    total_roles: Counter[str] = Counter()
    for section in sections:
        section_id = str(section["section_id"])
        ledger = _read_json(
            Path(coverage_root) / "sections" / section_id / "SECTION_SOURCE_LEDGER.json"
        )
        sources = [
            item for item in ledger.get("sources", [])
            if isinstance(item, dict) and item.get("paper_id")
        ]
        role_papers: dict[str, set[str]] = {role: set() for role in ROLE_ORDER}
        direct_papers: set[str] = set()
        permission_counts: Counter[str] = Counter()
        depth_counts: Counter[str] = Counter()
        for source in sources:
            paper_id = str(source.get("paper_id"))
            total_papers.add(paper_id)
            if str(source.get("scope_fit") or "") == "direct":
                direct_papers.add(paper_id)
            role = str(source.get("literature_role") or "unknown")
            total_roles[role] += 1
            if role in role_papers:
                role_papers[role].add(paper_id)
            permission_counts[str(source.get("use_permission") or "unknown")] += 1
            depth_counts[str(source.get("content_depth") or "unknown")] += 1
        targets = contract.section_targets(
            section=section,
            section_count=max(1, len(sections)),
        )
        role_rows = {}
        missing_roles = []
        role_universe = active_roles or {
            str(item)
            for item in (
                list(section.get("required_roles") or [])
                + list(section.get("optional_roles") or [])
            )
            if str(item) in ROLE_ORDER
        }
        for role in ROLE_ORDER:
            count = len(role_papers[role])
            target = _role_target(section, role) if role in role_universe else 0
            role_rows[role] = {
                "unique_papers": count,
                "target": target,
                "covered": count >= target if target else True,
            }
            if target and count < target:
                missing_roles.append(role)
        relationship_tasks = [
            str(item)
            for item in section.get("relationship_tasks", [])
            if str(item) in RELATION_TASKS
        ]
        if not relationship_tasks:
            section_scope = next(
                (
                    item for item in scope_map.get("research_dimensions") or []
                    if isinstance(item, dict)
                    and str(item.get("dimension_id")) == section_id
                ),
                {},
            )
            relationship_tasks = [
                str(item) for item in section_scope.get("relation_tasks") or []
                if str(item) in RELATION_TASKS
            ]
        if not relationship_tasks:
            relationship_tasks = sorted(active_relation_tasks)
        section_paper_ids = {str(item.get("paper_id")) for item in sources}
        section_chunk_ids = {
            str(chunk_id)
            for item in sources
            for chunk_id in item.get("canonical_chunk_ids") or []
            if str(chunk_id).strip()
        }
        section_edges = [
            edge
            for edge in relation_edges
            if str(edge.get("source_paper_id") or "") in section_paper_ids
            and str(edge.get("target_paper_id") or "") in section_paper_ids
        ]
        observed_counts: Counter[str] = Counter()
        semantic_counts: Counter[str] = Counter()
        invalid_semantic_edges: list[dict[str, Any]] = []
        for edge in section_edges:
            observed = str(
                edge.get("observed_relation")
                or edge.get("edge_type")
                or "unknown"
            )
            observed_counts[observed] += 1
            semantic = str(edge.get("semantic_relation") or "").strip()
            basis = list(edge.get("relation_basis_chunk_ids") or [])
            status = str(
                edge.get("status")
                or edge.get("relation_status")
                or "observed"
            )
            if semantic:
                basis_in_section = (
                    not section_chunk_ids
                    or not basis
                    or any(item in section_chunk_ids for item in basis)
                )
                if (
                    semantic in _SEMANTIC_TO_TASK
                    and basis
                    and basis_in_section
                    and status in {"inferred", "reviewed", "human_confirmed"}
                ):
                    semantic_counts[semantic] += 1
                else:
                    invalid_semantic_edges.append(
                        {
                            "edge_id": edge.get("edge_id", ""),
                            "semantic_relation": semantic,
                            "status": status,
                            "basis_in_section": basis_in_section,
                            "reason": "semantic_relation_missing_basis_or_inference_status",
                        }
                    )
        actual_relation_tasks = sorted(
            {
                _SEMANTIC_TO_TASK[item]
                for item in semantic_counts
                if item in _SEMANTIC_TO_TASK
            }
        )
        missing_relation_tasks = [
            item for item in relationship_tasks if item not in actual_relation_tasks
        ]
        section_unique_target = targets["minimum_unique_sources"]
        section_direct_target = targets["minimum_direct_sources"]
        breadth_shortfall = {
            "unique_sources": max(
                0, section_unique_target - len({str(item.get("paper_id")) for item in sources})
            ),
            "direct_sources": max(0, section_direct_target - len(direct_papers)),
        }
        section_rows.append(
            {
                "section_id": section_id,
                "unique_papers": len({str(item.get("paper_id")) for item in sources}),
                "direct_papers": len(direct_papers),
                "section_breadth_target": {
                    "minimum_unique_sources": section_unique_target,
                    "minimum_direct_sources": section_direct_target,
                },
                "breadth_shortfall": breadth_shortfall,
                "role_coverage": role_rows,
                "missing_literature_roles": missing_roles,
                "relationship_tasks": relationship_tasks,
                "relationship_coverage": {
                    "observed_edge_count": len(section_edges),
                    "observed_relation_counts": dict(sorted(observed_counts.items())),
                    "semantic_relation_counts": dict(sorted(semantic_counts.items())),
                    "actual_semantic_relation_tasks": actual_relation_tasks,
                    "missing_semantic_relation_tasks": missing_relation_tasks,
                    "invalid_semantic_edges": invalid_semantic_edges,
                    "relation_graph_found": bool(relation_edges),
                },
                "discovery_status": {
                    "complete": not bool(missing_roles or any(breadth_shortfall.values())),
                    "missing_roles": missing_roles,
                    "breadth_shortfall": breadth_shortfall,
                },
                "relation_completion_status": {
                    "complete": not bool(missing_relation_tasks),
                    "required_tasks": relationship_tasks,
                    "satisfied_tasks": actual_relation_tasks,
                    "missing_tasks": missing_relation_tasks,
                },
                "permission_distribution": dict(sorted(permission_counts.items())),
                "content_depth_distribution": dict(sorted(depth_counts.items())),
                "needs_expansion": bool(
                    missing_roles or any(breadth_shortfall.values())
                ),
            }
        )
    return {
        "schema_version": "research_harness.coverage_atlas.v1",
        "review_mode": contract.mode,
        "article_reference_target": contract.reference_target_range,
        "section_count": len(section_rows),
        "article_unique_papers": len(total_papers),
        "role_use_counts": dict(sorted(total_roles.items())),
        "sections_needing_expansion": [
            row["section_id"] for row in section_rows if row["needs_expansion"]
        ],
        "sections": section_rows,
        "relation_graph": {
            "edge_count": len(relation_edges),
            "observed_relation_counts": dict(
                sorted(
                    Counter(
                        str(item.get("observed_relation") or item.get("edge_type") or "unknown")
                        for item in relation_edges
                    ).items()
                )
            ),
            "semantic_relation_counts": dict(
                sorted(
                    Counter(
                        str(item.get("semantic_relation") or "")
                        for item in relation_edges
                        if str(item.get("semantic_relation") or "")
                    ).items()
                )
            ),
            "relation_graph_found": bool(relation_edges),
            "observed_edges_are_not_semantic_relations": True,
            "discovery_completion_is_separate_from_relation_completion": True,
        },
        "interpretation": {
            "role_coverage_is_not_sentence_citation_density": True,
            "abstracts_can_supply_background_but_not_direct_measurement_support": True,
            "metadata_only_records_are_discovery_only": True,
            "relation_tasks_require_observed_or_explicitly_inferred_edges": True,
        },
    }
