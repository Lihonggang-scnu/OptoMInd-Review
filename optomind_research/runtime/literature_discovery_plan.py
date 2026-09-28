"""Deterministic multi-wave discovery and relationship-coverage planning.

The planner does not make network calls.  It gives S2 Graph, Snippet,
Recommendations, citation-neighborhood, and the legacy fallback routes one
shared order of operations and one stopping vocabulary.  A caller can then
execute only the waves justified by the current coverage ledger.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .review_quality_contract import evaluate_discovery_stop
from .review_quality_contract import (
    CANONICAL_OBSERVED_RELATIONS,
    CANONICAL_SEMANTIC_RELATIONS,
)


WAVE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "wave_id": "W0_direct",
        "purpose": "Retrieve papers directly matching the confirmed question and section role.",
        "channels": ["s2_graph_search", "s2_snippet_search"],
        "cost_class": "low",
    },
    {
        "wave_id": "W1_facets",
        "purpose": "Expand mechanism, method, material, application, and frontier vocabulary.",
        "channels": ["s2_graph_search", "s2_specter_similarity"],
        "cost_class": "low",
    },
    {
        "wave_id": "W2_backward",
        "purpose": "Trace references from high-value seeds to foundations and prerequisites.",
        "channels": ["s2_references"],
        "cost_class": "medium",
    },
    {
        "wave_id": "W3_forward",
        "purpose": "Trace citations from high-value seeds to development and frontier work.",
        "channels": ["s2_citations"],
        "cost_class": "medium",
    },
    {
        "wave_id": "W4_recommendations",
        "purpose": "Use S2 recommendations to find near-neighbor branches without broad repeated searches.",
        "channels": ["s2_recommendations"],
        "cost_class": "low",
    },
    {
        "wave_id": "W5_boundary",
        "purpose": "Target limitations, conflicting results, negative evidence, and measurement disputes.",
        "channels": ["s2_snippet_search", "s2_citation_context"],
        "cost_class": "medium",
    },
    {
        "wave_id": "W6_review_frontier",
        "purpose": "Add reviews, perspectives, roadmaps, landmarks, and current frontier anchors.",
        "channels": ["s2_graph_search", "s2_recommendations"],
        "cost_class": "low",
    },
)


@dataclass(slots=True)
class DiscoveryWave:
    wave_id: str
    purpose: str
    channels: list[str] = field(default_factory=list)
    requested_roles: list[str] = field(default_factory=list)
    seed_paper_ids: list[str] = field(default_factory=list)
    query_templates: list[str] = field(default_factory=list)
    cost_class: str = "low"
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def build_discovery_wave_plan(
    *,
    user_question: str = "",
    section_title: str = "",
    section_argument: str = "",
    requested_roles: Iterable[str] = (),
    seed_paper_ids: Iterable[str] = (),
    base_queries: Iterable[str] = (),
    enable_expensive_waves: bool = True,
) -> dict[str, Any]:
    """Create the machine-readable W0-W6 plan used by a discovery run."""

    roles = _unique(requested_roles)
    seeds = _unique(seed_paper_ids)
    queries = _unique(base_queries)
    base = " ".join(
        part.strip()
        for part in (user_question, section_title, section_argument)
        if str(part).strip()
    )[:800]
    if base and base not in queries:
        queries.insert(0, base)
    waves: list[DiscoveryWave] = []
    for definition in WAVE_DEFINITIONS:
        wave_id = str(definition["wave_id"])
        enabled = enable_expensive_waves or definition["cost_class"] != "medium"
        templates = list(queries[:4])
        if wave_id == "W1_facets":
            templates = [f"{query} {role}" for query in templates[:2] for role in roles[:4]]
        elif wave_id == "W5_boundary":
            templates = [f"{query} limitation controversy boundary uncertainty" for query in templates[:2]]
        elif wave_id == "W6_review_frontier":
            templates = [f"{query} review perspective roadmap frontier" for query in templates[:2]]
        waves.append(
            DiscoveryWave(
                wave_id=wave_id,
                purpose=str(definition["purpose"]),
                channels=list(definition["channels"]),
                requested_roles=roles,
                seed_paper_ids=seeds,
                query_templates=_unique(templates),
                cost_class=str(definition["cost_class"]),
                enabled=enabled,
            )
        )
    return {
        "schema_version": "research_harness.discovery_wave_plan.v1",
        "order": [wave.wave_id for wave in waves],
        "waves": [wave.to_dict() for wave in waves],
        "seed_paper_ids": seeds,
        "requested_roles": roles,
        "stop_policy": {
            "stop_when": [
                "article and section breadth targets are met",
                "all required literature roles are represented",
                "two distinct waves produce no material new information",
            ],
            "never_stop_only_because": [
                "one search query returned enough papers",
                "one paper has a high citation count",
                "metadata exists without usable content",
            ],
        },
    }


def relation_coverage_ledger(
    graph: Any | None = None,
    *,
    required_relation_roles: Iterable[str] = (),
) -> dict[str, Any]:
    """Summarize observed relation evidence without inventing relation edges."""

    if graph is None:
        nodes: dict[str, Any] = {}
        edges: list[Any] = []
    elif isinstance(graph, dict):
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), dict) else {}
        edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    else:
        nodes = getattr(graph, "nodes", {}) or {}
        edges = getattr(graph, "edges", []) or []
    counts: dict[str, int] = {}
    observed_counts: dict[str, int] = {}
    semantic_counts: dict[str, int] = {}
    semantic_with_basis = 0
    invalid_semantic_edges: list[dict[str, Any]] = []
    for edge in edges:
        if isinstance(edge, dict):
            kind = str(
                edge.get("observed_relation")
                or edge.get("edge_type")
                or "unknown"
            )
            semantic = str(edge.get("semantic_relation") or "").strip()
            basis = list(edge.get("relation_basis_chunk_ids") or [])
            status = str(edge.get("status") or edge.get("relation_status") or "observed")
        else:
            kind = str(
                getattr(edge, "observed_relation", "")
                or getattr(edge, "edge_type", "unknown")
            )
            semantic = str(getattr(edge, "semantic_relation", "") or "").strip()
            basis = list(getattr(edge, "relation_basis_chunk_ids", []) or [])
            status = str(getattr(edge, "status", "observed") or "observed")
        counts[kind] = counts.get(kind, 0) + 1
        observed_counts[kind] = observed_counts.get(kind, 0) + 1
        if semantic:
            if semantic in CANONICAL_SEMANTIC_RELATIONS and basis and status != "observed":
                semantic_counts[semantic] = semantic_counts.get(semantic, 0) + 1
                semantic_with_basis += 1
            else:
                invalid_semantic_edges.append(
                    {
                        "observed_relation": kind,
                        "semantic_relation": semantic,
                        "status": status,
                        "basis_chunk_ids": basis,
                        "reason": "semantic_relation_requires_valid_basis_and_non_observed_status",
                    }
                )
    observed_roles = set(observed_counts)
    required = _unique(required_relation_roles)
    missing = [role for role in required if role not in observed_roles and role not in semantic_counts]
    return {
        "schema_version": "research_harness.relation_coverage_ledger.v1",
        "node_count": len(nodes),
        "edge_count": len(edges),
        "edge_type_counts": dict(sorted(counts.items())),
        "observed_relation_counts": dict(sorted(observed_counts.items())),
        "semantic_relation_counts": dict(sorted(semantic_counts.items())),
        "semantic_edges_with_basis": semantic_with_basis,
        "invalid_semantic_edges": invalid_semantic_edges,
        "observed_relations_are_not_semantic_claims": True,
        "required_relation_roles": required,
        "missing_relation_roles": missing,
        "relationship_evidence_is_observed_not_inferred": True,
    }


def evaluate_wave_stop(
    *,
    unique_papers: int,
    minimum_papers: int,
    covered_roles: Iterable[str],
    required_roles: Iterable[str],
    new_information_gain: float,
    no_gain_rounds: int,
    max_rounds: int,
    current_wave_index: int = 0,
    max_wave_index: int = 0,
    covered_dimensions: Iterable[str] = (),
    required_dimensions: Iterable[str] = (),
    observed_relation_count: int = 0,
    new_papers: int = 0,
    new_roles: int = 0,
    new_dimensions: int = 0,
    new_relations: int = 0,
    required_relation_tasks: Iterable[str] = (),
    satisfied_relation_tasks: Iterable[str] = (),
) -> dict[str, Any]:
    """Public alias used by S2/M3 and future section researchers."""

    return evaluate_discovery_stop(
        unique_papers=unique_papers,
        minimum_papers=minimum_papers,
        covered_roles=covered_roles,
        required_roles=required_roles,
        new_information_gain=new_information_gain,
        no_gain_rounds=no_gain_rounds,
        max_rounds=max_rounds,
        current_wave_index=current_wave_index,
        max_wave_index=max_wave_index,
        covered_dimensions=covered_dimensions,
        required_dimensions=required_dimensions,
        observed_relation_count=observed_relation_count,
        new_papers=new_papers,
        new_roles=new_roles,
        new_dimensions=new_dimensions,
        new_relations=new_relations,
        required_relation_tasks=required_relation_tasks,
        satisfied_relation_tasks=satisfied_relation_tasks,
    )
