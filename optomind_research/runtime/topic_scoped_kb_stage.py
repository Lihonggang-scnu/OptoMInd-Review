"""Topic-scoped, fail-closed SQLite staging for the S2-first pipeline.

This module owns the boundary between a broad legacy ReviewKnowledgeBase and a
run-local S2 overlay.  It deliberately does not copy the source database.  It
copies schema, selects rows by deterministic identity and scope rules, applies
the additive provenance migration, and only then accepts scoped S2 records.

The main future integration point is :func:`build_topic_scoped_kb`:

``build_topic_scoped_kb(query_plan_path=..., base_kb_sqlite=..., work_dir=...,
policy_path=..., papers=..., chunks=..., graph=...,
query_telemetry=...)``

The S2 harness can use :class:`TopicScopedKBStage` when it needs the overlay
path before a full-text fallback writes into it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from optomind_research.runtime.coverage_decision_contract import (
    assess_candidate_regime_boundary,
    extract_explicit_scope_regimes,
)
from optomind_research.runtime.review_quality_contract import (
    normalize_content_depth,
    normalize_scope_fit,
    normalize_use_permission,
    permission_for_content,
)
from optomind_research.runtime.s2_policy_runtime import (
    S2Policy,
    load_s2_policy,
)
from optomind_research.metadata_index import normalize_doi, title_match_score
from optomind_research.s2_kb_bridge import S2KnowledgeBaseBridge, _ensure_s2_tables
from optomind_research.s2_literature_graph import LiteratureGraph
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk


MANIFEST_SCHEMA_VERSION = "optomind.topic_scoped_kb_manifest.v1"
TELEMETRY_SCHEMA_VERSION = "optomind.s2_query_telemetry.v1"
SCOPE_DECISION_RULE_VERSION = "optomind.topic_scope_decision.object_identity.v2"
REUSE_CONTRACT_SCHEMA_VERSION = "optomind.topic_scoped_kb_reuse_contract.v1"
QUERY_CATEGORIES = (
    "discovery_search",
    "snippet_search",
    "batch_enrichment",
    "title_match",
    "paper_get",
    "references",
    "citations",
    "recommendations",
    "multi_seed_recommendations",
)
GRAPH_QUERY_CATEGORIES = frozenset(
    {"references", "citations", "recommendations", "multi_seed_recommendations"}
)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "as",
        "at",
        "by",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "to",
        "via",
        "was",
        "were",
        "be",
        "can",
        "may",
        "should",
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "using",
        "into",
        "based",
        "study",
        "paper",
        "papers",
        "method",
        "methods",
        "approach",
        "system",
        "systems",
        "review",
        "research",
        "analysis",
    }
)
_GENERIC_SCOPE_TERMS = frozenset(
    {
        # Broad scientific/optics words are useful query context, but are not
        # safe paper identity anchors on their own.
        "amplitude",
        "application",
        "applications",
        "applied",
        "approach",
        "approaches",
        "area",
        "beam",
        "change",
        "challenge",
        "challenges",
        "characterization",
        "commercialization",
        "computational",
        "current",
        "correction",
        "design",
        "designs",
        "development",
        "direction",
        "directions",
        "device",
        "devices",
        "electromagnetic",
        "engineering",
        "end",
        "evaluation",
        "experimental",
        "experiments",
        "fabrication",
        "field",
        "fields",
        "flat",
        "future",
        "fundamental",
        "fundamentals",
        "generation",
        "high",
        "image",
        "images",
        "imaging",
        "implementation",
        "including",
        "inverse",
        "large",
        "lens",
        "lenses",
        "lithography",
        "material",
        "materials",
        "mechanism",
        "mechanisms",
        "method",
        "methods",
        "model",
        "models",
        "nanoimprint",
        "numerical",
        "optical",
        "optic",
        "optics",
        "performance",
        "perspective",
        "plasmonic",
        "phase",
        "photonics",
        "photonic",
        "principle",
        "principles",
        "progress",
        "properties",
        "property",
        "research",
        "review",
        "reviews",
        "state",
        "study",
        "studies",
        "such",
        "shaping",
        "system",
        "systems",
        "technology",
        "technologies",
        "theory",
        "theoretical",
        "tunable",
        "using",
        "vortex",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{1,}|[\u3400-\u9fff]")
_PAPER_EVIDENCE_WEIGHTS = {
    "title": 5.0,
    "abstract": 4.0,
    "tldr": 3.0,
    "fulltext": 2.0,
    "chunks": 2.0,
}
_OBJECT_ONLY_GENERIC_TERMS = _GENERIC_SCOPE_TERMS | frozenset(
    {
        "beam",
        "commercialization",
        "computational",
        "correction",
        "electron",
        "end",
        "generation",
        "inverse",
        "lithography",
        "nanoimprint",
        "numerical",
        "plasmonic",
        "shaping",
        "tunable",
        "vortex",
    }
)
_OBJECT_HEAD_SUFFIXES = (
    "antenna",
    "atom",
    "cavity",
    "crystal",
    "detector",
    "device",
    "emitter",
    "fiber",
    "film",
    "laser",
    "lens",
    "material",
    "metamaterial",
    "particle",
    "resonator",
    "sensor",
    "structure",
    "surface",
    "waveguide",
)
_METHOD_ONLY_ANCHOR_TERMS = frozenset(
    {
        # Computational/experimental method families can define a review, but
        # they must not impersonate a scientific object when the question also
        # names a concrete material, device, structure, or physical platform.
        "adjoint",
        "algorithm",
        "automatic",
        "differentiable",
        "differentiation",
        "fdtd",
        "fem",
        "framework",
        "gradient",
        "informed",
        "inverse",
        "learning",
        "machine",
        "modeling",
        "modelling",
        "neural",
        "network",
        "optimization",
        "optimizer",
        "physic",
        "pinn",
        "pinns",
        "simulation",
        "solver",
        "surrogate",
        "training",
    }
)
_DISTINCTIVE_METHOD_LABELS = frozenset(
    {
        "pinn",
        "pinns",
        "fdtd",
        "fem",
        "gan",
        "gans",
        "dspsa",
        "mlp",
        "ann",
        "cnn",
        "rnn",
        "lstm",
        "svm",
        "rf",
    }
)
_METHOD_FAMILY_GENERIC_SINGLETONS = frozenset(
    {
        "simulation",
        "solver",
        "model",
        "framework",
        "algorithm",
        "network",
        "learning",
        "optimization",
        "training",
        "surrogate",
        "gradient",
        "informed",
    }
)
_GENERATED_ONLY_QUERY_MIN_SHARED = 2
_GENERATED_ONLY_QUERY_MIN_RATIO = 0.6
_GENERATED_ONLY_QUERY_WINDOW_TOKENS = 48
_METHOD_FAMILY_WINDOW_TOKENS = 24
# Generated-only admission is a broad auditable usefulness score, not a set of
# terminal AND gates.  The threshold is deliberately loose so indirect
# near-neighbor evidence survives; completely unrelated candidates fall below
# it through negative generic/provenance features.
_GENERATED_ONLY_USEFULNESS_THRESHOLD = 46.0
_GENERATED_ONLY_QUERY_SCORE_MAX = 40.0
_GENERATED_ONLY_PROVENANCE_SCORE_MAX = 10.0
_GENERATED_ONLY_METHOD_FAMILY_SCORE_MAX = 30.0
_GENERATED_ONLY_OBJECT_IDENTITY_SCORE_MAX = 25.0
_GENERATED_ONLY_BACKGROUND_SCORE_MAX = 20.0
_GENERATED_ONLY_CONTEXT_SCORE_MAX = 10.0
_GENERATED_ONLY_GENERIC_SINGLETON_PENALTY = 25.0
_GENERATED_ONLY_PROVENANCE_GAP_PENALTY = 18.0
# Semantic-aware usefulness components.  Semantic helpfulness is primary;
# lexical/provenance/method/object signals remain secondary and become the
# full fallback when embeddings fail.
_SEMANTIC_PRECISE_SCORE_MAX = 50.0
_SEMANTIC_BACKGROUND_SCORE_MAX = 20.0
_SEMANTIC_IDENTITY_BONUS_MAX = 10.0
_SEMANTIC_IDENTITY_PARTIAL_BONUS = 5.0
_SEMANTIC_MODE_USEFULNESS_THRESHOLD = 40.0
_SEMANTIC_DOMAIN_GAP_PENALTY = 20.0
_SEMANTIC_DOMAIN_GAP_BACKGROUND_SIM = 0.25
# In semantic mode, precise-query relevance is the primary gate.  When the
# best semantic precise-query similarity is below the precise-side minimum,
# this deficit penalty exceeds the possible secondary bonuses (provenance +
# identity + context), so background overlap or PINN-like method words cannot
# rescue a genuinely weak precise relationship.
_SEMANTIC_PRECISE_DEFICIT_PENALTY = 30.0
# Minimum semantic precise-query similarity that counts as the "precise side"
# in semantic mode.  A lexical token collision never waives this gate while
# embeddings are available; it is only an auditable fallback signal.
_SEMANTIC_PRECISE_SIDE_MIN = 0.5
_QUESTION_INTENT_TERMS = frozenset(
    {
        "accuracy",
        "benchmark",
        "benchmarking",
        "compare",
        "comparison",
        "contrast",
        "credibility",
        "do",
        "experiment",
        "experimental",
        "gap",
        "gaps",
        "generalization",
        "how",
        "limit",
        "limits",
        "path",
        "question",
        "reliability",
        "scalability",
        "translation",
        "validation",
        "versus",
        "what",
        "why",
    }
)
_CONTEXT_GENERIC_TERMS = _GENERIC_SCOPE_TERMS - frozenset(
    {
        "lithography",
        "nanoimprint",
        "plasmonic",
        "vortex",
        "dielectric",
        "holography",
        "huygen",
        "pancharatnam",
        "berry",
    }
) | frozenset(
    {
        "aberration",
        "algorithm",
        "aperture",
        "area",
        "broadband",
        "change",
        "chromatic",
        "commercialization",
        "concept",
        "computational",
        "cover",
        "control",
        "correction",
        "cost",
        "crystal",
        "deep",
        "discontinuity",
        "dynamic",
        "efficiency",
        "end",
        "electron",
        "focusing",
        "generation",
        "high",
        "integrating",
        "inverse",
        "light",
        "liquid",
        "large",
        "manipulation",
        "material",
        "materials",
        "mems",
        "methodology",
        "metric",
        "numerical",
        "operation",
        "shaping",
        "reconfigurable",
        "scalability",
        "sensor",
        "structured",
        "tunable",
        "yield",
        "along",
        "art",
    }
)
_CURRENT_RUN_ROUTE_MARKERS = frozenset(
    {
        "current_run",
        "current-run",
        "run_local",
        "run-local",
        "s2_search",
        "s2_reference",
        "s2_citation",
        "s2_recommendation",
        "s2_snippet_search",
        "s2_review_frontier_search",
        "semantic_scholar_snippet_search",
        "s2_multiwave",
    }
)
_CORE_TABLES = frozenset(
    {
        "papers",
        "text_chunks",
        "visual_assets",
        "visual_chunks",
        "links",
        "concepts",
        "concept_mentions",
        "paper_citations",
        "s2_literature_graph_nodes",
        "s2_literature_graph_edges",
    }
)
_FTS_TABLES = frozenset(
    {
        "paper_fts",
        "text_chunk_fts",
        "visual_asset_fts",
        "visual_chunk_fts",
        "concept_fts",
    }
)
_CORE_COLUMN_DEFS: dict[str, dict[str, str]] = {
    "papers": {
        "paper_id": "TEXT PRIMARY KEY",
        "doi": "TEXT NOT NULL DEFAULT ''",
        "title": "TEXT NOT NULL DEFAULT ''",
        "year": "INTEGER",
        "venue": "TEXT NOT NULL DEFAULT ''",
        "quality_tier": "TEXT NOT NULL DEFAULT ''",
        "query_relevance": "TEXT NOT NULL DEFAULT ''",
        "search_text": "TEXT NOT NULL DEFAULT ''",
        "raw_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "text_chunks": {
        "chunk_id": "TEXT PRIMARY KEY",
        "paper_id": "TEXT NOT NULL DEFAULT ''",
        "doi": "TEXT NOT NULL DEFAULT ''",
        "title": "TEXT NOT NULL DEFAULT ''",
        "ordinal": "INTEGER",
        "section_path": "TEXT NOT NULL DEFAULT ''",
        "char_start": "INTEGER",
        "char_end": "INTEGER",
        "char_count": "INTEGER",
        "boilerplate_score": "REAL NOT NULL DEFAULT 0",
        "text": "TEXT NOT NULL DEFAULT ''",
        "search_text": "TEXT NOT NULL DEFAULT ''",
        "raw_json": "TEXT NOT NULL DEFAULT '{}'",
    },
}


class TopicScopedKBError(RuntimeError):
    """Raised when a scoped overlay cannot be built safely."""


# Isolated-rebuild support: contract-mismatch stale artifacts are relocated
# instead of hard-failing, so callers can rebuild without losing prior work.
_STALE_KB_DIRNAME = "_stale_scoped_kb"


def _relocate_stale_kb_artifacts(
    work_path: "Path",
    reserved_paths: "tuple[Path, ...]",
    started: float,
) -> "Path":
    """Move reserved paths into a timestamped stale subdir; return that dir."""
    ts = f"{int(started * 1000)}"
    stale_dir = work_path / f"{_STALE_KB_DIRNAME}_{ts}"
    stale_dir.mkdir(parents=True, exist_ok=True)
    paths_to_relocate: list[Path] = []
    for path in reserved_paths:
        paths_to_relocate.append(path)
        if path.name.endswith(".sqlite"):
            paths_to_relocate.extend(
                [
                    path.with_name(path.name + "-wal"),
                    path.with_name(path.name + "-shm"),
                ]
            )
    for path in dict.fromkeys(paths_to_relocate):
        if path.is_file():
            dest = stale_dir / path.name
            try:
                path.rename(dest)
            except OSError:
                shutil.copy2(str(path), str(dest))
                try:
                    path.unlink()
                except OSError:
                    pass
    return stale_dir


def _isolated_kb_rebuild_report(
    *,
    stale_dir: "Path",
    reason: str,
) -> dict:
    """Return isolated_rebuild_available status for contract-mismatch cases."""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": "isolated_rebuild_available",
        "error_code": "topic_scoped_kb_reuse_contract_mismatch",
        "error": reason,
        "stale_artifact_dir": str(stale_dir),
        "isolated_rebuild_available": True,
        "reused": False,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _record_payload(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    return value


def _graph_request_payload(graph: LiteratureGraph | None) -> dict[str, Any] | None:
    """Return graph content without derived or runtime-order-dependent views."""

    if graph is None:
        return None
    return {
        "nodes": {
            paper_id: _record_payload(graph.nodes[paper_id])
            for paper_id in sorted(graph.nodes)
        },
        "node_annotations": {
            paper_id: copy.deepcopy(dict(graph.node_annotations.get(paper_id) or {}))
            for paper_id in sorted(graph.node_annotations)
        },
        "edges": sorted(
            (_record_payload(edge) for edge in graph.edges),
            key=_canonical_json,
        ),
        "excluded_candidates": sorted(
            (copy.deepcopy(item) for item in graph.excluded_candidates),
            key=_canonical_json,
        ),
        # Query timing/cache telemetry is bound separately.  It is not graph
        # identity and must not make the same scientific graph path-dependent.
    }


def _reuse_contract(
    *,
    query_plan: Mapping[str, Any],
    base_kb_sqlite: Path,
    policy: S2Policy,
    scope_contract: "TopicScopeContract",
    papers: Sequence[S2PaperRecord],
    chunks: Sequence[UnifiedTextChunk],
    graph: LiteratureGraph | None,
    query_telemetry: Mapping[str, Any] | None,
    extra_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    components = {
        "query_plan_semantic_sha256": _canonical_sha256(dict(query_plan)),
        "source_base_kb_sha256": _sha256_file(base_kb_sqlite),
        "effective_policy_sha256": _canonical_sha256(policy.to_dict()),
        "scope_contract_sha256": scope_contract.contract_sha256,
        "papers_sha256": _canonical_sha256(
            [_record_payload(paper) for paper in papers]
        ),
        "chunks_sha256": _canonical_sha256(
            [_record_payload(chunk) for chunk in chunks]
        ),
        "graph_sha256": _canonical_sha256(_graph_request_payload(graph)),
        "query_telemetry_sha256": _canonical_sha256(dict(query_telemetry or {})),
        "extra_manifest_sha256": _canonical_sha256(dict(extra_manifest or {})),
    }
    request = {
        "schema_version": REUSE_CONTRACT_SCHEMA_VERSION,
        "scope_decision_rule_version": SCOPE_DECISION_RULE_VERSION,
        "components": components,
    }
    request["request_fingerprint_sha256"] = _canonical_sha256(request)
    return request


def _reuse_contract_is_valid(stored: Any) -> bool:
    if not isinstance(stored, Mapping):
        return False
    if stored.get("schema_version") != REUSE_CONTRACT_SCHEMA_VERSION:
        return False
    if stored.get("scope_decision_rule_version") != SCOPE_DECISION_RULE_VERSION:
        return False
    stored_body = dict(stored)
    stored_digest = str(stored_body.pop("request_fingerprint_sha256", ""))
    if not stored_digest or stored_digest != _canonical_sha256(stored_body):
        return False
    return True


def _reuse_contract_matches(
    stored: Any,
    expected: Mapping[str, Any],
) -> bool:
    if not _reuse_contract_is_valid(stored):
        return False
    return _canonical_json(stored) == _canonical_json(expected)


def _deduplicate_input_records(
    values: Sequence[Any],
    *,
    identity_field: str,
    label: str,
) -> list[Any]:
    """Deduplicate identical records and reject ambiguous last-write-wins input."""

    result: list[Any] = []
    seen: dict[str, str] = {}
    for value in values:
        identity = str(getattr(value, identity_field, "") or "").strip()
        if not identity:
            result.append(value)
            continue
        digest = _canonical_sha256(_record_payload(value))
        previous = seen.get(identity)
        if previous is None:
            seen[identity] = digest
            result.append(value)
            continue
        if previous != digest:
            raise TopicScopedKBError(
                f"conflicting duplicate {label} identity: {identity}"
            )
    return result


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _collect_strings(value: Any, *, field_name: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            if isinstance(item, (str, list, tuple, Mapping)):
                result.extend(_collect_strings(item, field_name=field_name, errors=errors))
            else:
                errors.append(f"{field_name} contains a non-text value")
        return result
    if isinstance(value, Mapping):
        result = []
        for item in value.values():
            result.extend(_collect_strings(item, field_name=field_name, errors=errors))
        return result
    errors.append(f"{field_name} must be text, a list, or a mapping of text")
    return []


def _plan_output(query_plan: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("output", "result"):
        candidate = query_plan.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return query_plan


def _first_text(mapping: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass(frozen=True, slots=True)
class TopicScopeContract:
    """The deterministic allowlist/denylist derived from one query plan."""

    canonical_question: str
    lenses: tuple[str, ...]
    inclusion_boundaries: tuple[str, ...]
    exclusion_boundaries: tuple[str, ...]
    keywords: tuple[str, ...]
    validation_errors: tuple[str, ...] = ()
    topic_object_anchors: tuple[str, ...] = ()
    context_anchors: tuple[str, ...] = ()
    focus_anchors: tuple[str, ...] = ()
    core_anchors: tuple[str, ...] = ()
    minimum_core_anchor_hits: int = 2
    minimum_scope_score: float = 4.0
    forbidden_regimes: tuple[str, ...] = ()
    allowed_regimes: tuple[str, ...] = ()
    method_anchors: tuple[str, ...] = ()
    object_anchor_mode: str = "scientific_object"
    scientific_object_anchor_required: bool = True
    explicit_boundary_notes: tuple[str, ...] = ()
    object_head_anchors: tuple[str, ...] = ()
    object_modifier_anchors: tuple[str, ...] = ()
    compound_object_phrases: tuple[str, ...] = ()
    method_family_phrases: tuple[tuple[str, ...], ...] = ()
    # Optional generated-only marker: when set, search_queries() returns only
    # the explicit discovery query list instead of expanding lenses/scope
    # items and appending the canonical question.  Ordinary plans leave both
    # fields at their defaults and keep the historical search_queries().
    discovery_mode: str = ""
    discovery_queries: tuple[str, ...] = ()
    # Compact broad research background carried from the supplementary query
    # plan so the candidate relevance layer sees the same context the query
    # generator used.  It is never expanded into discovery search queries.
    search_background_cue: str = ""
    search_background_terms: tuple[str, ...] = ()
    # Exact bounded compact generation context (task-specific projected cells
    # plus the resolved policy and background cue), durably auditable from the
    # generated-only contract/decision path.  Never a full-registry dump.
    relevance_context: dict[str, Any] = field(default_factory=dict)
    relevance_context_sha256: str = ""

    @property
    def valid(self) -> bool:
        return not self.validation_errors

    @property
    def allowlist_terms(self) -> tuple[str, ...]:
        return tuple(
            _unique(
                [
                    *self.keywords,
                    *self.inclusion_boundaries,
                    *self.lenses,
                    *self.topic_object_anchors,
                    *self.context_anchors,
                    *self.focus_anchors,
                    *self.method_anchors,
                    *self.compound_object_phrases,
                    self.canonical_question,
                ]
            )
        )

    @property
    def denylist_terms(self) -> tuple[str, ...]:
        return self.exclusion_boundaries

    @property
    def contract_sha256(self) -> str:
        return _sha256_bytes(
            _canonical_json(self._to_dict_without_hash()).encode("utf-8")
        )

    def search_queries(self, *, max_items: int | None = None) -> list[str]:
        if self.discovery_mode == "generated_only":
            queries = _unique(self.discovery_queries)
            if not queries:
                queries = _unique(
                    [*self.keywords, *self.lenses, *self.inclusion_boundaries]
                )
            if not queries:
                queries = [self.canonical_question]
            return queries[:max_items] if max_items is not None else queries
        queries = _unique([*self.keywords, *self.lenses, *self.inclusion_boundaries])
        if not queries:
            queries = [self.canonical_question]
        elif self.canonical_question.casefold() not in {item.casefold() for item in queries}:
            queries.append(self.canonical_question)
        return queries[:max_items] if max_items is not None else queries

    def _to_dict_without_hash(self) -> dict[str, Any]:
        payload = {
            "schema_version": "optomind.topic_scope_contract.v1",
            "valid": self.valid,
            "canonical_question": self.canonical_question,
            "lenses": list(self.lenses),
            "inclusion_boundaries": list(self.inclusion_boundaries),
            "exclusion_boundaries": list(self.exclusion_boundaries),
            "keywords": list(self.keywords),
            "topic_object_anchors": list(self.topic_object_anchors),
            "context_anchors": list(self.context_anchors),
            "focus_anchors": list(self.focus_anchors),
            "core_anchors": list(self.core_anchors),
            "minimum_core_anchor_hits": self.minimum_core_anchor_hits,
            "minimum_scope_score": self.minimum_scope_score,
            "forbidden_regimes": list(self.forbidden_regimes),
            "allowed_regimes": list(self.allowed_regimes),
            "method_anchors": list(self.method_anchors),
            "object_anchor_mode": self.object_anchor_mode,
            "scientific_object_anchor_required": self.scientific_object_anchor_required,
            "explicit_boundary_notes": list(self.explicit_boundary_notes),
            "object_head_anchors": list(self.object_head_anchors),
            "object_modifier_anchors": list(self.object_modifier_anchors),
            "compound_object_phrases": list(self.compound_object_phrases),
            "method_family_phrases": [
                list(phrase) for phrase in self.method_family_phrases
            ],
            "allowlist_terms": list(self.allowlist_terms),
            "denylist_terms": list(self.denylist_terms),
            "validation_errors": list(self.validation_errors),
        }
        if self.discovery_mode:
            payload["discovery_mode"] = self.discovery_mode
            payload["discovery_queries"] = list(self.discovery_queries)
            payload["search_background_cue"] = self.search_background_cue
            payload["search_background_terms"] = list(
                self.search_background_terms
            )
            payload["relevance_context"] = dict(self.relevance_context)
            payload["relevance_context_sha256"] = (
                self.relevance_context_sha256
            )
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._to_dict_without_hash(),
            "contract_sha256": self.contract_sha256,
        }


def derive_topic_scope_contract(query_plan: Mapping[str, Any]) -> TopicScopeContract:
    """Derive a scope contract from canonical question, lenses, boundaries, and keywords."""

    if not isinstance(query_plan, Mapping):
        return TopicScopeContract("", (), (), (), (), ("query_plan must be a mapping",))
    output = _plan_output(query_plan)
    errors: list[str] = []
    discovery_mode = ""
    discovery_queries: tuple[str, ...] = ()
    marker_background_cue = ""
    marker_relevance_context: dict[str, Any] = {}
    for marker_source in (
        query_plan.get("supplementary_retrieval"),
        output.get("supplementary_retrieval"),
    ):
        if not isinstance(marker_source, Mapping):
            continue
        if str(marker_source.get("discovery_mode") or "").strip() == "generated_only":
            discovery_mode = "generated_only"
            discovery_queries = tuple(
                _unique(marker_source.get("discovery_queries") or [])
            )
            raw_background_cue = (
                marker_source.get("search_background_cue")
                or marker_source.get("background_cue")
            )
            marker_background_cue = (
                str(raw_background_cue).strip()
                if isinstance(raw_background_cue, str)
                else ""
            )
            raw_relevance_context = marker_source.get("relevance_context")
            if isinstance(raw_relevance_context, Mapping):
                marker_relevance_context = dict(raw_relevance_context)
            break
    # The confirmed English user query is the stable identity of the run.
    # A planner's prose understanding is supporting evidence, not a license
    # to replace the question with a long list of generic domain terms.
    canonical_question = _first_text(output, ("canonical_question",))
    if not canonical_question:
        canonical_question = _first_text(query_plan, ("canonical_question",))
    input_data = query_plan.get("input")
    if not canonical_question and isinstance(input_data, Mapping):
        canonical_question = _first_text(input_data, ("user_query", "question"))
    if not canonical_question:
        canonical_question = _first_text(
            output,
            ("core_question", "research_question", "question", "problem_understanding"),
        )
    if not canonical_question:
        canonical_question = _first_text(
            query_plan,
            ("core_question", "research_question", "question", "problem_understanding"),
        )
    if not canonical_question:
        errors.append("canonical_question_missing")

    scope = output.get("scope_definition") or query_plan.get("scope_definition")
    scope = scope if isinstance(scope, Mapping) else {}
    def plan_field(key: str) -> Any:
        value = output.get(key)
        if value not in (None, "", [], {}):
            return value
        return query_plan.get(key)

    lenses_values: list[str] = []
    for key in (
        "lenses",
        "research_lenses",
        "analytical_lenses",
        "section_lenses",
        "dimensions",
    ):
        lenses_values.extend(_collect_strings(plan_field(key), field_name=key, errors=errors))
    lenses_values.extend(
        _collect_strings(scope.get("lenses"), field_name="scope_definition.lenses", errors=errors)
    )
    lenses_values.extend(
        _collect_strings(scope.get("scope_items"), field_name="scope_definition.scope_items", errors=errors)
    )

    inclusion_values: list[str] = []
    for key in (
        "inclusion_boundaries",
        "inclusions",
        "included_topics",
        "include",
        "scope_inclusions",
    ):
        inclusion_values.extend(_collect_strings(plan_field(key), field_name=key, errors=errors))
    inclusion_values.extend(
        _collect_strings(scope.get("main_scope"), field_name="scope_definition.main_scope", errors=errors)
    )
    inclusion_values.extend(
        _collect_strings(
            scope.get("inclusion_boundaries"),
            field_name="scope_definition.inclusion_boundaries",
            errors=errors,
        )
    )
    inclusion_values.extend(
        _collect_strings(scope.get("scope_items"), field_name="scope_definition.scope_items", errors=errors)
    )
    scope_map = output.get("review_scope_map")
    if not isinstance(scope_map, Mapping):
        root_scope_map = query_plan.get("review_scope_map")
        scope_map = root_scope_map if isinstance(root_scope_map, Mapping) else None
    if isinstance(scope_map, Mapping):
        inclusion_values.extend(
            _collect_strings(
                scope_map.get("inclusion_boundaries"),
                field_name="review_scope_map.inclusion_boundaries",
                errors=errors,
            )
        )

    exclusion_values: list[str] = []
    for key in (
        "exclusion_boundaries",
        "exclusions",
        "excluded_topics",
        "exclude",
        "scope_exclusions",
    ):
        exclusion_values.extend(_collect_strings(plan_field(key), field_name=key, errors=errors))
    exclusion_values.extend(
        _collect_strings(scope.get("exclusions"), field_name="scope_definition.exclusions", errors=errors)
    )
    exclusion_values.extend(
        _collect_strings(
            scope.get("exclusion_boundaries"),
            field_name="scope_definition.exclusion_boundaries",
            errors=errors,
        )
    )
    if isinstance(scope_map, Mapping):
        exclusion_values.extend(
            _collect_strings(
                scope_map.get("exclusion_boundaries"),
                field_name="review_scope_map.exclusion_boundaries",
                errors=errors,
            )
        )

    keyword_values: list[str] = []
    keyword_block = output.get("keyword_decomposition") or query_plan.get("keyword_decomposition")
    if isinstance(keyword_block, Mapping):
        known_keyword_values = _collect_strings(
            keyword_block.get("keywords"),
            field_name="keyword_decomposition.keywords",
            errors=errors,
        )
        for key in ("search_terms", "search_anchors", "query_terms"):
            known_keyword_values.extend(
                _collect_strings(keyword_block.get(key), field_name=f"keyword_decomposition.{key}", errors=errors)
            )
        if known_keyword_values:
            keyword_values = known_keyword_values
        else:
            # Older planner artifacts used facet names as mapping keys.  The
            # values remain explicit search anchors; the schema labels do not.
            keyword_values = _collect_strings(
                keyword_block,
                field_name="keyword_decomposition",
                errors=errors,
            )
    else:
        keyword_values = _collect_strings(
            keyword_block,
            field_name="keyword_decomposition",
            errors=errors,
        )
    for key in ("keywords", "search_anchors", "anchor_terms", "query_keywords"):
        keyword_values.extend(_collect_strings(plan_field(key), field_name=key, errors=errors))

    lenses = tuple(_unique(lenses_values))
    inclusions = tuple(_unique(inclusion_values))
    exclusions = tuple(_unique(exclusion_values))
    keywords = tuple(_unique(keyword_values))

    # Parse explicit boundary prose from extra_notes fields.  Only notes that
    # contain explicit boundary/contrast language produce regime constraints;
    # ordinary descriptive prose is ignored.  This prevents "nanophotonics is
    # interesting" from silently becoming a deny-list entry while still acting
    # on "limited to nanophotonic metasurfaces, not microwave metamaterials."
    extra_notes_values: list[str] = []
    for source_name, source in (("output", output), ("root", query_plan)):
        for key in ("extra_notes", "notes", "scope_notes", "boundary_notes"):
            value = source.get(key)
            if value is not None:
                extra_notes_values.extend(
                    _collect_strings(
                        value,
                        field_name=f"{source_name}.{key}",
                        errors=errors,
                    )
                )
    scope_notes = scope.get("extra_notes")
    if scope_notes is not None:
        extra_notes_values.extend(
            _collect_strings(
                scope_notes,
                field_name="scope_definition.extra_notes",
                errors=errors,
            )
        )
    extra_notes_values = _unique(extra_notes_values)
    if extra_notes_values:
        regime_info = extract_explicit_scope_regimes(extra_notes_values)
    else:
        regime_info = {"forbidden_regimes": [], "allowed_regimes": []}
    derived_forbidden_regimes = tuple(regime_info["forbidden_regimes"])
    derived_allowed_regimes = tuple(regime_info["allowed_regimes"])

    if not any(_meaningful_tokens(value) for value in (*keywords, *inclusions, *lenses, canonical_question)):
        errors.append("scope_allowlist_empty")
    (
        topic_object_anchors,
        context_anchors,
        method_anchors,
        object_anchor_mode,
        object_head_anchors,
        object_modifier_anchors,
        compound_object_phrases,
    ) = _derive_contract_anchors(
        canonical_question=canonical_question,
        lenses=lenses,
        inclusion_boundaries=inclusions,
        keywords=keywords,
    )
    focus_anchors = _derive_focus_anchors(
        canonical_question=canonical_question,
        topic_object_anchors=topic_object_anchors,
        lenses=lenses,
        inclusion_boundaries=inclusions,
        keywords=keywords,
    )
    core_anchors = tuple(
        _unique([*topic_object_anchors, *context_anchors, *focus_anchors])
    )
    if not topic_object_anchors:
        errors.append("topic_object_anchor_missing")
    if len(core_anchors) < 2:
        errors.append("scope_core_anchor_set_too_small")
    method_family_phrases = _derive_method_family_phrases(lenses, inclusions)
    if discovery_mode == "generated_only":
        search_background_cue = marker_background_cue or (
            _compact_generated_only_background_cue(
                lenses=lenses,
                inclusion_boundaries=inclusions,
            )
        )
        search_background_terms = tuple(
            sorted(_meaningful_tokens(search_background_cue))
        )
        relevance_context = dict(marker_relevance_context)
        if not relevance_context:
            # Legacy generated-only plan without the durable compact context:
            # keep a bounded, honest fallback derived from the plan itself.
            relevance_context = {
                "search_background_cue": search_background_cue,
                "topic_scope": {
                    "main_scope": search_background_cue,
                    "lenses": list(lenses),
                    "inclusion_boundaries": list(inclusions),
                },
            }
        relevance_context_sha256 = _canonical_sha256(relevance_context)
    else:
        search_background_cue = ""
        search_background_terms = ()
        relevance_context = {}
        relevance_context_sha256 = ""
    return TopicScopeContract(
        canonical_question=canonical_question,
        lenses=lenses,
        inclusion_boundaries=inclusions,
        exclusion_boundaries=exclusions,
        keywords=keywords,
        validation_errors=tuple(_unique(errors)),
        topic_object_anchors=topic_object_anchors,
        context_anchors=context_anchors,
        focus_anchors=focus_anchors,
        core_anchors=core_anchors,
        forbidden_regimes=derived_forbidden_regimes,
        allowed_regimes=derived_allowed_regimes,
        method_anchors=method_anchors,
        object_anchor_mode=object_anchor_mode,
        scientific_object_anchor_required=(object_anchor_mode == "scientific_object"),
        explicit_boundary_notes=tuple(regime_info.get("matched_notes") or ()),
        object_head_anchors=object_head_anchors,
        object_modifier_anchors=object_modifier_anchors,
        compound_object_phrases=compound_object_phrases,
        method_family_phrases=method_family_phrases,
        discovery_mode=discovery_mode,
        discovery_queries=discovery_queries,
        search_background_cue=search_background_cue,
        search_background_terms=search_background_terms,
        relevance_context=relevance_context,
        relevance_context_sha256=relevance_context_sha256,
    )


build_topic_scope_contract = derive_topic_scope_contract


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip().casefold()


def _canonical_scope_token(value: Any) -> str:
    token = str(value or "").strip().casefold().strip("-_")
    if not token or any("\u3400" <= char <= "\u9fff" for char in token):
        return token
    if token.endswith("lens"):
        return token
    if token.endswith("ies") and len(token) > 5:
        return token[:-3] + "y"
    if token.endswith("sses") and len(token) > 6:
        return token[:-2]
    if token.endswith("ses") and len(token) > 5:
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is")) and len(token) > 5:
        return token[:-1]
    return token


def _meaningful_tokens(value: Any) -> set[str]:
    return {
        _canonical_scope_token(token)
        for token in _TOKEN_RE.findall(_normalize_text(value))
        if _canonical_scope_token(token) not in _STOPWORDS
        and _canonical_scope_token(token) not in {""}
    }


def _anchor_phrase_tokens(value: Any) -> tuple[str, ...]:
    return tuple(
        sorted(
            _meaningful_tokens(value)
            - _CONTEXT_GENERIC_TERMS
            - {_canonical_scope_token(item) for item in _STOPWORDS}
        )
    )


def _ordered_anchor_tokens(value: Any) -> list[str]:
    result: list[str] = []
    for token in _TOKEN_RE.findall(_normalize_text(value)):
        canonical = _canonical_scope_token(token)
        if (
            canonical
            and canonical not in _STOPWORDS
            and canonical not in _CONTEXT_GENERIC_TERMS
            and canonical not in result
        ):
            result.append(canonical)
    return result


def _ordered_raw_tokens(value: Any) -> list[str]:
    """Ordered token stream preserving repeats (stopwords removed).

    Used for bounded local-window coverage so tokens separated by a long run
    of unrelated words cannot satisfy a co-location check after deduplication.
    """

    result: list[str] = []
    for token in _TOKEN_RE.findall(_normalize_text(value)):
        canonical = _canonical_scope_token(token)
        if canonical and canonical not in _STOPWORDS:
            result.append(canonical)
    return result


def _derive_contract_anchors(
    *,
    canonical_question: str,
    lenses: Sequence[str],
    inclusion_boundaries: Sequence[str],
    keywords: Sequence[str],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    str,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    """Derive object/context anchors without treating domain words as identity.

    Object anchors are terms that name the scientific object in the canonical
    question or recur across planner phrases.  Context anchors are non-generic
    terms from the lenses/boundaries/keywords.  The later paper gate requires
    an object anchor plus another core anchor, so ``optical``, ``imaging`` or
    ``design`` cannot admit a paper on their own.
    """

    planner_phrases: list[tuple[str, str, float]] = [
        ("canonical_question", canonical_question, 4.0),
        *[("keyword", value, 3.0) for value in keywords],
        *[("inclusion", value, 2.5) for value in inclusion_boundaries],
        *[("lens", value, 1.5) for value in lenses],
    ]
    weighted: Counter[str] = Counter()
    phrase_counts: Counter[str] = Counter()
    canonical_tokens = set(_meaningful_tokens(canonical_question)) - _OBJECT_ONLY_GENERIC_TERMS
    for _source, phrase, weight in planner_phrases:
        tokens = _anchor_phrase_tokens(phrase)
        for token in tokens:
            weighted[token] += weight
            phrase_counts[token] += 1

    # Separate method families from scientific objects before scoring papers.
    # A device/material head plus its immediate modifier is a stronger legacy
    # object signal than every remaining noun in a natural-language question.
    # This keeps validation goals and method names from becoming identity keys.
    method_candidate_set = {
        token
        for token in weighted
        if token in _METHOD_ONLY_ANCHOR_TERMS
        and (token in canonical_tokens or phrase_counts[token] >= 2)
    }
    scientific_candidates = (
        canonical_tokens
        - _METHOD_ONLY_ANCHOR_TERMS
        - _QUESTION_INTENT_TERMS
    )
    ordered_scientific = [
        token
        for token in _ordered_anchor_tokens(canonical_question)
        if token in scientific_candidates
    ]
    concrete_head_set = {
        token
        for token in ordered_scientific
        if token.endswith(_OBJECT_HEAD_SUFFIXES)
    }
    if concrete_head_set:
        object_anchor_mode = "scientific_object"
        object_candidate_set: set[str] = set()
        for index, token in enumerate(ordered_scientific):
            if token not in concrete_head_set:
                continue
            object_candidate_set.add(token)
            if index:
                object_candidate_set.add(ordered_scientific[index - 1])
        for _source, phrase, _weight in planner_phrases:
            phrase_tokens = [
                token
                for token in _ordered_anchor_tokens(phrase)
                if token not in _OBJECT_ONLY_GENERIC_TERMS
                and token not in _METHOD_ONLY_ANCHOR_TERMS
                and token not in _QUESTION_INTENT_TERMS
            ]
            for index, token in enumerate(phrase_tokens):
                if not token.endswith(_OBJECT_HEAD_SUFFIXES):
                    continue
                if token not in scientific_candidates and phrase_counts[token] < 2:
                    continue
                object_candidate_set.add(token)
                if index:
                    modifier = phrase_tokens[index - 1]
                    if modifier in scientific_candidates or phrase_counts[modifier] >= 2:
                        object_candidate_set.add(modifier)
    else:
        # A planner can name a research object as a multi-word scientific
        # phrase without using one of our material/device suffixes (for
        # example, "daytime radiative cooling").  Prefer that auditable
        # planner evidence to a one-word method fallback such as ``physic``.
        # This is deliberately derived from the current plan, not a domain
        # vocabulary: the same path protects unrelated multi-word objects.
        phrase_object_tokens: set[str] = set()
        phrase_object_phrases: list[str] = []
        for _source, phrase, _weight in planner_phrases:
            tokens = [
                token for token in _ordered_anchor_tokens(phrase)
                if token not in _OBJECT_ONLY_GENERIC_TERMS
                and token not in _METHOD_ONLY_ANCHOR_TERMS
                and token not in _QUESTION_INTENT_TERMS
            ]
            if len(tokens) >= 2:
                phrase_object_tokens.update(tokens)
                candidate = " ".join(tokens)
                if candidate not in phrase_object_phrases:
                    phrase_object_phrases.append(candidate)
        strong_method_candidate_set = {
            token for token in method_candidate_set
            if token not in {"physic", "physics"}
        }
        if strong_method_candidate_set:
            object_anchor_mode = "method_fallback"
            object_candidate_set = set(method_candidate_set)
        elif phrase_object_tokens:
            object_anchor_mode = "scientific_object"
            object_candidate_set = phrase_object_tokens
        elif method_candidate_set:
        # Method-centric questions (e.g., PINN vs differentiable electromagnetic
        # solvers) name no concrete physical object head.  The identity falls
        # back to the configured method anchors; abstract workflow nouns such
        # as path/credibility must never become scientific objects.
            object_anchor_mode = "method_fallback"
            # Very short/stemmed method tokens are too generic to establish
            # topic identity without an object phrase.
            object_candidate_set = set(method_candidate_set)
        else:
        # Unknown-object scientific questions (no recognized concrete head and
        # no method vocabulary) keep the scientific-object contract with the
        # remaining scientific nouns as identity anchors.
            object_anchor_mode = "scientific_object"
            object_candidate_set = set(scientific_candidates)
    object_candidates = list(object_candidate_set)
    object_candidates.sort(
        key=lambda token: (-weighted[token], -phrase_counts[token], token)
    )
    object_anchors = tuple(object_candidates[:6])
    object_head_set = {
        token for token in object_anchors if token.endswith(_OBJECT_HEAD_SUFFIXES)
    }
    if not object_head_set:
        # Unknown scientific objects remain usable.  The entire object set is
        # treated as heads only when no recognized device/material head exists.
        object_head_set.update(object_anchors)
    object_modifier_set = set(object_anchors) - object_head_set
    compound_object_phrases: list[str] = []
    for _source, phrase, _weight in planner_phrases:
        phrase_tokens = _ordered_anchor_tokens(phrase)
        for index, token in enumerate(phrase_tokens):
            if token not in object_head_set or index == 0:
                continue
            modifier = phrase_tokens[index - 1]
            if modifier not in object_modifier_set:
                continue
            compound = f"{modifier} {token}"
            if compound not in compound_object_phrases:
                compound_object_phrases.append(compound)
    # Retain full planner-derived scientific phrases too.  They have a higher
    # identity value than a coincidental single-word method hit.
    if 'phrase_object_phrases' in locals():
        compound_object_phrases = _unique([
            *phrase_object_phrases, *compound_object_phrases
        ])
    object_head_anchors = tuple(
        token for token in object_anchors if token in object_head_set
    )
    object_modifier_anchors = tuple(
        token for token in object_anchors if token in object_modifier_set
    )
    method_anchors = tuple(
        sorted(
            method_candidate_set,
            key=lambda token: (-weighted[token], -phrase_counts[token], token),
        )[:12]
    )

    # Context anchors must be connected to an object-bearing planner phrase;
    # otherwise a common term such as ``optics`` or ``imaging`` becomes a
    # false positive.  Keep enough aliases to cover the review lenses while
    # making the list deterministic and bounded.
    contextual_weight: Counter[str] = Counter()
    contextual_phrase_counts: Counter[str] = Counter()
    object_set = set(object_anchors)
    for _source, phrase, weight in planner_phrases:
        tokens = set(_anchor_phrase_tokens(phrase))
        if not object_set.intersection(tokens):
            continue
        for token in tokens - object_set:
            if token in _CONTEXT_GENERIC_TERMS:
                continue
            contextual_weight[token] += weight
            contextual_phrase_counts[token] += 1
    context_candidates = sorted(
        contextual_weight,
        key=lambda token: (
            -contextual_weight[token],
            -contextual_phrase_counts[token],
            token,
        ),
    )
    context_anchors = tuple(context_candidates[:48])

    # If the question is short and the planner supplied only one object term,
    # use the strongest non-generic planner term as the second core anchor.
    # It is still a real anchor and is never sufficient without the object.
    if not context_anchors:
        fallback = [
            token
            for token in sorted(weighted, key=lambda item: (-weighted[item], item))
            if token not in object_set and token not in _CONTEXT_GENERIC_TERMS
        ]
        context_anchors = tuple(fallback[:8])
    return (
        object_anchors,
        context_anchors,
        method_anchors,
        object_anchor_mode,
        object_head_anchors,
        object_modifier_anchors,
        tuple(compound_object_phrases[:12]),
    )


def _derive_focus_anchors(
    *,
    canonical_question: str,
    topic_object_anchors: Sequence[str],
    lenses: Sequence[str],
    inclusion_boundaries: Sequence[str],
    keywords: Sequence[str],
) -> tuple[str, ...]:
    """Keep explicit question focus terms separate from generic context terms.

    ``imaging`` is too generic to identify a paper by itself, but it is a
    useful required companion when the canonical question explicitly asks for
    imaging.  The same rule works for other query-specific focus terms: they
    are promoted only when the planner puts them in an object-bearing phrase.
    """

    workflow_terms = {
        "application",
        "applications",
        "design",
        "fabrication",
        "flat",
        "method",
        "methods",
        "optic",
        "optical",
        "optics",
        "principle",
        "principles",
        "progress",
        "review",
    }
    object_set = set(topic_object_anchors)
    canonical_tokens = _meaningful_tokens(canonical_question)
    canonical_focus_tokens = {
        token
        for token in canonical_tokens
        if token not in object_set and token not in workflow_terms
    }
    weighted: Counter[str] = Counter()
    phrase_counts: Counter[str] = Counter()
    phrases: list[tuple[str, float]] = [
        (canonical_question, 4.0),
        *[(value, 3.0) for value in keywords],
        *[(value, 2.5) for value in inclusion_boundaries],
        *[(value, 1.5) for value in lenses],
    ]
    for phrase, weight in phrases:
        tokens = _meaningful_tokens(phrase)
        if not object_set.intersection(tokens):
            continue
        for token in tokens - object_set:
            if token in _STOPWORDS or token in workflow_terms:
                continue
            if token in canonical_focus_tokens:
                weighted[token] += weight
                phrase_counts[token] += 1
    if not weighted:
        for token in sorted(canonical_focus_tokens):
            weighted[token] += 1.0
    return tuple(
        sorted(
            weighted,
            key=lambda token: (
                -weighted[token],
                -phrase_counts[token],
                token,
            ),
        )[:8]
    )


def _matches_term(term: str, text: str, tokens: set[str]) -> bool:
    normalized_term = _normalize_text(term)
    if not normalized_term:
        return False
    if normalized_term in text:
        return True
    term_tokens = _meaningful_tokens(normalized_term)
    if not term_tokens:
        return False
    overlap = len(term_tokens & tokens)
    required = 1 if len(term_tokens) <= 2 else 2
    return overlap >= required


def _matches_exclusion_term(term: str, text: str, tokens: set[str]) -> bool:
    """Apply a stricter boundary match so one shared token cannot reject a paper."""

    normalized_term = _normalize_text(term)
    if not normalized_term:
        return False
    if normalized_term in text:
        return True
    term_tokens = _meaningful_tokens(normalized_term)
    return bool(term_tokens) and term_tokens <= tokens


def _matches_anchor_term(term: str, text: str, tokens: set[str]) -> bool:
    normalized_term = _normalize_text(term)
    if not normalized_term:
        return False
    term_tokens = _meaningful_tokens(normalized_term)
    if not term_tokens:
        return False
    return normalized_term in text or term_tokens <= tokens


def _matches_compound_object_phrase(term: str, text: str) -> bool:
    """Match a derived object phrase as an ordered contiguous token sequence."""

    phrase_tokens = [
        _canonical_scope_token(token)
        for token in _TOKEN_RE.findall(_normalize_text(term))
    ]
    text_tokens = [
        _canonical_scope_token(token)
        for token in _TOKEN_RE.findall(_normalize_text(text))
    ]
    if not phrase_tokens or len(phrase_tokens) > len(text_tokens):
        return False
    width = len(phrase_tokens)
    return any(
        text_tokens[index : index + width] == phrase_tokens
        for index in range(len(text_tokens) - width + 1)
    )


def _text_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("text", "value", "body", "content", "abstract", "summary"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def _paper_evidence_fields(
    row: Mapping[str, Any],
    *,
    related_chunks: Iterable[Mapping[str, Any]] = (),
) -> dict[str, str]:
    """Normalize all evidence layers without treating provenance as content."""

    raw = _raw_record(row)
    title = _text_value(_row_value(row, raw, "title"))
    abstract = _text_value(
        _row_value(row, raw, "abstract", "summary", "abstract_text")
    )
    tldr = _text_value(_row_value(row, raw, "tldr", "tldr_text"))
    paper_search = _text_value(_row_value(row, raw, "search_text"))
    # Legacy paper rows often have only ``search_text`` (normally an abstract
    # index).  Use it as an abstract fallback, but do not call it full text;
    # full-text/chunk evidence must come from actual body material below.
    if not abstract and paper_search:
        abstract = paper_search[:2500]
    fulltext_parts: list[str] = [paper_search]
    for key in ("fulltext_text", "full_text", "body_text", "body", "content"):
        value = _text_value(_row_value(row, raw, key))
        if value:
            fulltext_parts.append(value)

    chunk_parts: list[str] = []
    for chunk in related_chunks:
        chunk_raw = _raw_record(chunk)
        section_text = _text_value(_row_value(chunk, chunk_raw, "section_path", "section"))
        if re.search(r"\b(reference|references|bibliograph|works cited|literature cited)\b", section_text, re.I):
            continue
        chunk_text = " ".join(
            part
            for part in (
                _text_value(_row_value(chunk, chunk_raw, "title")),
                section_text,
                _text_value(_row_value(chunk, chunk_raw, "text")),
                _text_value(_row_value(chunk, chunk_raw, "search_text")),
            )
            if part
        )
        if not chunk_text:
            continue
        chunk_parts.append(chunk_text)
        depth = normalize_content_depth(
            _row_value(chunk, chunk_raw, "content_depth", "evidence_level", "source_kind"),
            default="",
        )
        if depth in {"fulltext", "partial_fulltext", "structured_snippet"}:
            fulltext_parts.append(chunk_text)

    return {
        "title": title,
        "abstract": abstract,
        "tldr": tldr,
        "fulltext": " ".join(part for part in fulltext_parts if part),
        "chunks": " ".join(chunk_parts),
    }


def _score_paper_scope(
    contract: TopicScopeContract,
    row: Mapping[str, Any],
    *,
    related_chunks: Iterable[Mapping[str, Any]] = (),
    explicit_current_run: bool = False,
    semantic_features: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one paper from aggregated evidence and return an audit decision."""

    related_chunk_list = list(related_chunks)
    fields = _paper_evidence_fields(row, related_chunks=related_chunk_list)
    normalized_fields = {
        name: _normalize_text(value) for name, value in fields.items()
    }
    token_fields = {
        name: _meaningful_tokens(value) for name, value in normalized_fields.items()
    }
    combined_text = " ".join(value for value in normalized_fields.values() if value)
    combined_tokens = _meaningful_tokens(combined_text)
    generated_only_match = _generated_only_discovery_match(
        contract,
        normalized_fields,
    )
    generated_only_precise_side = bool(
        generated_only_match["matched"] or explicit_current_run
    )
    exclusion_hits = [
        term
        for term in contract.denylist_terms
        if _matches_exclusion_term(term, combined_text, combined_tokens)
    ]

    object_hits: set[str] = set()
    context_hits: set[str] = set()
    focus_hits: set[str] = set()
    field_hits: dict[str, dict[str, Any]] = {}
    score = 0.0
    for field_name, text in normalized_fields.items():
        object_field_hits = sorted(
            term
            for term in contract.topic_object_anchors
            if _matches_anchor_term(term, text, token_fields[field_name])
        )
        object_head_field_hits = sorted(
            term
            for term in contract.object_head_anchors
            if _matches_anchor_term(term, text, token_fields[field_name])
        )
        object_modifier_field_hits = sorted(
            term
            for term in contract.object_modifier_anchors
            if _matches_anchor_term(term, text, token_fields[field_name])
        )
        compound_object_phrase_hits = sorted(
            phrase
            for phrase in contract.compound_object_phrases
            if _matches_compound_object_phrase(phrase, text)
        )
        context_field_hits = sorted(
            term
            for term in contract.context_anchors
            if _matches_anchor_term(term, text, token_fields[field_name])
        )
        focus_field_hits = sorted(
            term
            for term in contract.focus_anchors
            if _matches_anchor_term(term, text, token_fields[field_name])
        )
        object_hits.update(object_field_hits)
        context_hits.update(context_field_hits)
        focus_hits.update(focus_field_hits)
        field_weight = _PAPER_EVIDENCE_WEIGHTS.get(field_name, 1.0)
        score += min(
            field_weight * 4.0,
            field_weight
            * (
                1.25 * len(object_field_hits)
                + 0.9 * len(context_field_hits)
                + 1.1 * len(focus_field_hits)
            ),
        )
        field_hits[field_name] = {
            "object_anchor_hits": object_field_hits,
            "object_head_anchor_hits": object_head_field_hits,
            "object_modifier_anchor_hits": object_modifier_field_hits,
            "compound_object_phrase_hits": compound_object_phrase_hits,
            "context_anchor_hits": context_field_hits,
            "focus_anchor_hits": focus_field_hits,
            "core_anchor_hits": list(
                dict.fromkeys(
                    object_field_hits + context_field_hits + focus_field_hits
                )
            ),
        }

    phrase_hits: list[str] = []
    phrase_candidates = _unique(
        [*contract.keywords, *contract.inclusion_boundaries, *contract.lenses]
    )
    for phrase in phrase_candidates:
        phrase_tokens = _anchor_phrase_tokens(phrase)
        if len(phrase_tokens) < 2:
            continue
        if any(
            set(phrase_tokens) <= token_fields[field_name]
            for field_name in normalized_fields
        ):
            phrase_hits.append(phrase)
    score += min(6.0, len(phrase_hits) * 1.5)

    # ``fulltext`` is still scored and audited, but legacy KB search_text can
    # be a noisy article index.  Require the identity gate to be supported by
    # bibliographic evidence or a real body chunk instead of a late reference
    # occurrence in that index.
    primary_fields = ("title", "abstract", "tldr")
    primary_object_hits = sorted(
        {
            term
            for field_name in primary_fields
            for term in field_hits.get(field_name, {}).get("object_anchor_hits", [])
        }
    )
    primary_object_head_hits = sorted(
        {
            term
            for field_name in primary_fields
            for term in field_hits.get(field_name, {}).get(
                "object_head_anchor_hits", []
            )
        }
    )
    primary_object_modifier_hits = sorted(
        {
            term
            for field_name in primary_fields
            for term in field_hits.get(field_name, {}).get(
                "object_modifier_anchor_hits", []
            )
        }
    )
    primary_compound_object_phrase_hits = sorted(
        {
            phrase
            for field_name in primary_fields
            for phrase in field_hits.get(field_name, {}).get(
                "compound_object_phrase_hits", []
            )
        }
    )
    primary_context_hits = sorted(
        {
            term
            for field_name in primary_fields
            for term in field_hits.get(field_name, {}).get("context_anchor_hits", [])
        }
    )
    primary_focus_hits = sorted(
        {
            term
            for field_name in primary_fields
            for term in field_hits.get(field_name, {}).get("focus_anchor_hits", [])
        }
    )
    chunk_strong_hits: list[dict[str, Any]] = []
    chunk_object_identity_hits: list[dict[str, Any]] = []
    for chunk in related_chunk_list:
        chunk_raw = _raw_record(chunk)
        section_text = _text_value(
            _row_value(chunk, chunk_raw, "section_path", "section")
        )
        if re.search(
            r"\b(reference|references|bibliograph|works cited|literature cited)\b",
            section_text,
            re.I,
        ):
            continue
        chunk_text = " ".join(
            part
            for part in (
                section_text,
                _text_value(_row_value(chunk, chunk_raw, "text")),
                _text_value(_row_value(chunk, chunk_raw, "search_text")),
            )
            if part
        )
        normalized_chunk = _normalize_text(chunk_text)
        chunk_tokens = _meaningful_tokens(normalized_chunk)
        chunk_object_hits = sorted(
            term
            for term in contract.topic_object_anchors
            if _matches_anchor_term(term, normalized_chunk, chunk_tokens)
        )
        chunk_object_head_hits = sorted(
            term
            for term in contract.object_head_anchors
            if _matches_anchor_term(term, normalized_chunk, chunk_tokens)
        )
        chunk_object_modifier_hits = sorted(
            term
            for term in contract.object_modifier_anchors
            if _matches_anchor_term(term, normalized_chunk, chunk_tokens)
        )
        chunk_compound_object_phrase_hits = sorted(
            phrase
            for phrase in contract.compound_object_phrases
            if _matches_compound_object_phrase(phrase, normalized_chunk)
        )
        chunk_context_hits = sorted(
            term
            for term in contract.context_anchors
            if _matches_anchor_term(term, normalized_chunk, chunk_tokens)
        )
        chunk_focus_hits = sorted(
            term
            for term in contract.focus_anchors
            if _matches_anchor_term(term, normalized_chunk, chunk_tokens)
        )
        chunk_hit = {
            "object_anchor_hits": chunk_object_hits,
            "object_head_anchor_hits": chunk_object_head_hits,
            "object_modifier_anchor_hits": chunk_object_modifier_hits,
            "compound_object_phrase_hits": chunk_compound_object_phrase_hits,
            "context_anchor_hits": chunk_context_hits,
            "focus_anchor_hits": chunk_focus_hits,
        }
        if chunk_object_hits and (chunk_context_hits or chunk_focus_hits):
            chunk_strong_hits.append(chunk_hit)
        if (
            (chunk_object_head_hits or chunk_compound_object_phrase_hits)
            and (chunk_context_hits or chunk_focus_hits)
        ):
            chunk_object_identity_hits.append(chunk_hit)
    chunk_focus_hits = sorted(
        {
            term
            for item in chunk_strong_hits
            for term in item.get("focus_anchor_hits") or []
        }
    )
    core_hits = sorted(object_hits | context_hits | focus_hits)
    if contract.scientific_object_anchor_required:
        object_identity_evidence_present = bool(
            primary_object_head_hits
            or primary_compound_object_phrase_hits
            or chunk_object_identity_hits
        )
    else:
        object_identity_evidence_present = bool(
            primary_object_hits or chunk_strong_hits
        )
    # The focus companion is an identity guard.  It must occur in the
    # bibliographic record (title/abstract/TLDR); late full-text/reference
    # mentions are retained for audit but cannot turn a neighboring paper into
    # a topic match.
    focus_evidence_present = not contract.focus_anchors or bool(primary_focus_hits)
    multiple_core_anchors = (
        len(core_hits) >= contract.minimum_core_anchor_hits
        and focus_evidence_present
        and (
            not contract.scientific_object_anchor_required
            or object_identity_evidence_present
        )
        and (
            (
                bool(primary_object_hits)
                and len(primary_context_hits) >= 2
                and len(set(primary_object_hits) | set(primary_context_hits))
                >= contract.minimum_core_anchor_hits
            )
            or (
                any(
                    len(item.get("context_anchor_hits") or []) >= 2
                    for item in chunk_strong_hits
                )
                and len(core_hits) >= contract.minimum_core_anchor_hits
            )
            or (
                len(primary_object_hits) >= contract.minimum_core_anchor_hits
            )
            or (
                bool(primary_object_hits)
                and bool(primary_focus_hits)
                and len(set(primary_object_hits) | set(primary_focus_hits))
                >= contract.minimum_core_anchor_hits
            )
        )
    )
    strong_evidence_fields = [
        field_name
        for field_name, detail in field_hits.items()
        if detail["object_anchor_hits"] and detail["context_anchor_hits"]
    ]
    generic_terms_present = sorted(
        term for term in combined_tokens if term in _GENERIC_SCOPE_TERMS
    )
    generic_only_match = not object_hits and bool(generic_terms_present)
    method_anchor_hits = sorted(
        term
        for term in contract.method_anchors
        if _matches_anchor_term(term, combined_text, combined_tokens)
    )
    contextual_method_transfer = bool(
        contract.scientific_object_anchor_required
        and not object_identity_evidence_present
        and primary_object_modifier_hits
        and len(method_anchor_hits) >= 2
        and primary_focus_hits
        and score >= contract.minimum_scope_score
    )
    paper_id = str(_row_value(row, _raw_record(row), "paper_id") or "").strip()
    title = _text_value(_row_value(row, _raw_record(row), "title"))

    # Apply explicit regime boundary from extra_notes when the contract carries
    # forbidden or allowed regime constraints.  This guard runs before the
    # topic-anchor scoring so that a microwave or acoustic paper is hard-rejected
    # even when it also mentions the topic object word in passing.
    regime_decision: dict[str, Any] = {}
    if contract.forbidden_regimes or contract.allowed_regimes:
        regime_decision = assess_candidate_regime_boundary(
            {"title": fields["title"], "abstract": fields["abstract"]},
            allowed_regimes=contract.allowed_regimes,
            forbidden_regimes=contract.forbidden_regimes,
        )
    method_family_identity_present = (
        _method_family_identity_present(contract, normalized_fields)
        if (
            contract.discovery_mode == "generated_only"
            and contract.object_anchor_mode == "method_fallback"
        )
        else False
    )
    usefulness: dict[str, Any] = {}
    if contract.discovery_mode == "generated_only":
        usefulness = _generated_only_usefulness_score(
            contract=contract,
            generated_only_match=generated_only_match,
            explicit_current_run=explicit_current_run,
            method_family_identity_present=method_family_identity_present,
            object_identity_evidence_present=object_identity_evidence_present,
            object_hits=object_hits,
            primary_context_hits=primary_context_hits,
            primary_focus_hits=primary_focus_hits,
            method_anchor_hits=method_anchor_hits,
            combined_tokens=combined_tokens,
            generic_only_match=generic_only_match,
            semantic_features=semantic_features,
        )
    accepted = False
    reason = "topic_object_anchor_miss"
    scope_fit = "unreviewed"
    if exclusion_hits:
        reason = "exclusion_boundary_match"
    elif regime_decision.get("incompatible"):
        reason = "forbidden_regime_boundary_match"
        scope_fit = "out_of_scope"
    elif contract.discovery_mode == "generated_only":
        # In semantic mode the usefulness score only ranks/audits candidates:
        # embeddings produce precise/background similarities and an auditable
        # score, but admission is not gated on a low score or a precise-side
        # threshold.  Upstream explicit exclusions, forbidden regimes, and
        # identity/material hard failures still reject.  Lexical fallback
        # (embedding failure or no semantic features) keeps the historical
        # threshold gate.
        semantic_mode = str(
            (usefulness.get("features") or {}).get("semantic_mode") or ""
        )
        if semantic_mode == "semantic":
            accepted = True
            reason = (
                "generated_only_discovery_match"
                if generated_only_match["matched"]
                else "generated_only_usefulness_related"
            )
            scope_fit = "direct"
        elif usefulness["score"] < usefulness["threshold"]:
            reason = "generated_only_usefulness_below_threshold"
        else:
            accepted = True
            reason = (
                "generated_only_discovery_match"
                if generated_only_match["matched"]
                else "generated_only_usefulness_related"
            )
            scope_fit = "direct"
    elif explicit_current_run and not object_identity_evidence_present:
        reason = "explicit_current_run_object_anchor_miss"
    elif explicit_current_run and not focus_evidence_present:
        reason = "explicit_current_run_focus_anchor_miss"
    elif explicit_current_run:
        accepted = True
        reason = "explicit_current_run_discovery"
        scope_fit = "direct"
    elif not object_hits:
        reason = "generic_shared_term_only" if generic_only_match else "topic_object_anchor_miss"
    elif contextual_method_transfer:
        accepted = True
        reason = "contextual_method_transfer_without_object_head"
        scope_fit = "contextual"
    elif contract.scientific_object_anchor_required and not object_identity_evidence_present:
        reason = "scientific_object_identity_incomplete"
    elif not multiple_core_anchors:
        reason = "insufficient_core_anchor_evidence"
    elif score < contract.minimum_scope_score:
        reason = "scope_score_below_threshold"
    else:
        accepted = True
        reason = "accepted_by_core_anchor_score"
        scope_fit = "direct"

    if accepted and not scope_fit:
        scope_fit = "direct"
    usefulness_score = (
        float(usefulness.get("score") or 0.0) if usefulness else 0.0
    )
    usefulness_threshold = (
        float(usefulness.get("threshold") or 0.0) if usefulness else 0.0
    )
    decision = {
        "accepted": accepted,
        "scope_fit": scope_fit,
        "reason": reason,
        "identity": paper_id,
        "paper_id": paper_id,
        "title": title,
        "score": round(score, 4),
        "threshold": contract.minimum_scope_score,
        "minimum_core_anchor_hits": contract.minimum_core_anchor_hits,
        "core_anchor_hit_count": len(core_hits),
        "core_anchor_hits": core_hits,
        "object_anchor_hits": sorted(object_hits),
        "object_head_anchor_hits": sorted(
            set(primary_object_head_hits)
            | {
                term
                for item in chunk_object_identity_hits
                for term in item.get("object_head_anchor_hits") or []
            }
        ),
        "object_modifier_anchor_hits": sorted(
            set(primary_object_modifier_hits)
            | {
                term
                for item in chunk_strong_hits
                for term in item.get("object_modifier_anchor_hits") or []
            }
        ),
        "compound_object_phrase_hits": sorted(
            set(primary_compound_object_phrase_hits)
            | {
                phrase
                for item in chunk_object_identity_hits
                for phrase in item.get("compound_object_phrase_hits") or []
            }
        ),
        "method_anchor_hits": method_anchor_hits,
        "object_anchor_mode": contract.object_anchor_mode,
        "scientific_object_anchor_required": contract.scientific_object_anchor_required,
        "object_identity_evidence_present": object_identity_evidence_present,
        "context_anchor_hits": sorted(context_hits),
        "focus_anchor_hits": sorted(focus_hits),
        "primary_object_anchor_hits": primary_object_hits,
        "primary_object_head_anchor_hits": primary_object_head_hits,
        "primary_object_modifier_anchor_hits": primary_object_modifier_hits,
        "primary_compound_object_phrase_hits": primary_compound_object_phrase_hits,
        "primary_context_anchor_hits": primary_context_hits,
        "primary_focus_anchor_hits": primary_focus_hits,
        "focus_evidence_present": focus_evidence_present,
        "chunk_strong_anchor_evidence_count": len(chunk_strong_hits),
        "chunk_object_identity_evidence_count": len(chunk_object_identity_hits),
        "contextual_method_transfer": contextual_method_transfer,
        "multiple_core_anchors": multiple_core_anchors,
        "strong_evidence_fields": strong_evidence_fields,
        "phrase_hits": phrase_hits[:20],
        "evidence_by_field": field_hits,
        "exclusion_hits": exclusion_hits,
        "regime_decision": regime_decision,
        "generic_only_match": generic_only_match,
        "generic_terms_present": generic_terms_present[:20],
        "explicit_current_run": bool(explicit_current_run),
        "discovery_mode": contract.discovery_mode,
        "method_family_identity_present": bool(
            method_family_identity_present
        ),
        "generated_only_precise_side": bool(generated_only_precise_side),
        "generated_only_provenance_match": bool(
            contract.discovery_mode == "generated_only"
            and explicit_current_run
        ),
        "generated_only_discovery_match": generated_only_match["matched"],
        "generated_only_discovery_hits": generated_only_match["hits"],
        "generated_only_discovery_score": generated_only_match["score"],
    }
    if contract.discovery_mode == "generated_only":
        decision.update(
            {
                "usefulness_score": usefulness_score,
                "usefulness_threshold": usefulness_threshold,
                "usefulness_reason": (
                    "usefulness_score_accepted"
                    if accepted
                    else "usefulness_score_below_threshold"
                ),
                "usefulness_features": dict(usefulness["features"]),
                "matched_precise_query": str(
                    usefulness.get("matched_query") or ""
                ),
                "relevance_context_sha256": (
                    contract.relevance_context_sha256
                ),
                "relevance_context_field_count": len(
                    contract.relevance_context
                ),
                "semantic_mode": usefulness["features"].get(
                    "semantic_mode"
                ),
                "background_similarity": usefulness["features"].get(
                    "background_similarity"
                ),
                "max_precise_similarity": usefulness["features"].get(
                    "max_precise_similarity"
                ),
                "semantic_fallback_error_code": usefulness["features"].get(
                    "semantic_fallback_error_code"
                ),
            }
        )
    return decision


def _scope_match(contract: TopicScopeContract, text: str) -> dict[str, Any]:
    return _score_paper_scope(
        contract,
        {"search_text": text, "raw_json": "{}"},
    )


def _bounded_token_window_match(
    query_tokens: set[str],
    field_tokens: Sequence[str],
    *,
    min_shared: int,
    min_ratio: float,
    window_tokens: int,
) -> dict[str, Any] | None:
    """Return one bounded local-window match or None.

    Query tokens must co-occur inside a single bounded token window of one
    evidence field; scattered occurrences across a long article do not count.
    """

    query_set = set(query_tokens)
    if not query_set or len(field_tokens) == 0:
        return None
    width = max(int(window_tokens), len(query_set) * 3)
    if len(field_tokens) <= width:
        shared = query_set & set(field_tokens)
        ratio = len(shared) / len(query_set)
        if len(shared) >= min_shared and ratio >= min_ratio:
            return {
                "shared_tokens": sorted(shared),
                "ratio": round(ratio, 4),
                "window_start": 0,
                "window_end": len(field_tokens),
            }
        return None
    for start in range(0, len(field_tokens) - width + 1):
        window = set(field_tokens[start : start + width])
        shared = query_set & window
        ratio = len(shared) / len(query_set)
        if len(shared) >= min_shared and ratio >= min_ratio:
            return {
                "shared_tokens": sorted(shared),
                "ratio": round(ratio, 4),
                "window_start": start,
                "window_end": start + width,
            }
    return None


def _derive_method_family_phrases(
    lenses: Sequence[str],
    inclusion_boundaries: Sequence[str],
) -> tuple[tuple[str, ...], ...]:
    """Derive generic method-family phrases from method-bearing scope phrases.

    A family phrase is kept only when it contains a distinctive method label
    (e.g., PINN/FDTD/FEM) or a non-generic method anchor.  Single-token phrases
    must be distinctive method labels; generic singleton words such as
    simulation/solver/model are never identity by themselves.  Multi-token
    phrases keep their ordered meaningful tokens (e.g., differentiable,
    electromagnetic, solver), and identity requires at least two of them to
    co-occur in a primary bibliographic field.
    """

    phrases: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for phrase in [*lenses, *inclusion_boundaries]:
        tokens = tuple(_ordered_anchor_tokens(phrase))
        if not tokens:
            continue
        if len(tokens) == 1:
            if tokens[0] not in _DISTINCTIVE_METHOD_LABELS:
                continue
        elif not any(
            token in _DISTINCTIVE_METHOD_LABELS
            or (
                token in _METHOD_ONLY_ANCHOR_TERMS
                and token not in _METHOD_FAMILY_GENERIC_SINGLETONS
            )
            for token in tokens
        ):
            continue
        if tokens in seen:
            continue
        seen.add(tokens)
        phrases.append(tokens)
    return tuple(phrases)


def _method_family_identity_present(
    contract: TopicScopeContract,
    fields: Mapping[str, str],
) -> bool:
    """Require the named method family in a primary bibliographic field."""

    primary_fields = ("title", "abstract", "tldr")
    for family in contract.method_family_phrases:
        if len(family) == 1:
            token = family[0]
            if any(
                token in _meaningful_tokens(fields.get(field_name, ""))
                for field_name in primary_fields
            ):
                return True
            continue
        for field_name in primary_fields:
            field_tokens = _ordered_raw_tokens(fields.get(field_name, ""))
            if _bounded_token_window_match(
                set(family),
                field_tokens,
                min_shared=2,
                min_ratio=0.0,
                window_tokens=_METHOD_FAMILY_WINDOW_TOKENS,
            ) is not None:
                return True
    return False


def _generated_only_discovery_match(
    contract: TopicScopeContract,
    fields: Mapping[str, str],
) -> dict[str, Any]:
    """Return bounded local generated-only query-match evidence.

    A query matches only when a clear majority of its meaningful tokens co-occur
    inside one bounded local token window of a single evidence field.  Tokens
    scattered across a long article do not count, and ordinary
    (non-generated-only) contracts always return ``matched=False``.
    """

    if contract.discovery_mode != "generated_only" or not contract.discovery_queries:
        return {"matched": False, "hits": [], "score": 0.0}
    hits: list[dict[str, Any]] = []
    best = 0.0
    for query in contract.discovery_queries:
        query_tokens = set(_anchor_phrase_tokens(query))
        if len(query_tokens) < 2:
            continue
        for field_name, text in fields.items():
            if not text:
                continue
            field_tokens = _ordered_raw_tokens(text)
            window_match = _bounded_token_window_match(
                query_tokens,
                field_tokens,
                min_shared=_GENERATED_ONLY_QUERY_MIN_SHARED,
                min_ratio=_GENERATED_ONLY_QUERY_MIN_RATIO,
                window_tokens=_GENERATED_ONLY_QUERY_WINDOW_TOKENS,
            )
            if window_match is None:
                continue
            hits.append({
                "query": str(query),
                "field": field_name,
                **window_match,
            })
            best = max(best, window_match["ratio"])
            break
    return {"matched": bool(hits), "hits": hits, "score": round(best, 4)}


def _compact_generated_only_background_cue(
    *,
    lenses: Sequence[str],
    inclusion_boundaries: Sequence[str],
    max_chars: int = 240,
) -> str:
    """Derive a compact broad background cue when a plan lacks the marker.

    This is a fallback for legacy generated-only plans.  New supplementary
    plans carry the exact ``search_background_cue`` from the query generator
    context; the fallback never contains the full canonical question and is
    never expanded into discovery queries.
    """

    cue = ""
    seen: set[str] = set()
    for segment in [*lenses, *inclusion_boundaries]:
        text = str(segment or "").strip()
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        candidate = (cue + " " + text).strip()
        if len(candidate) > max_chars:
            break
        cue = candidate
    return cue[:max_chars].strip()


def _generated_only_usefulness_score(
    *,
    contract: TopicScopeContract,
    generated_only_match: Mapping[str, Any],
    explicit_current_run: bool,
    method_family_identity_present: bool,
    object_identity_evidence_present: bool,
    object_hits: set[str],
    primary_context_hits: Sequence[str],
    primary_focus_hits: Sequence[str],
    method_anchor_hits: Sequence[str],
    combined_tokens: set[str],
    generic_only_match: bool,
    semantic_features: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score generated-only usefulness on a 0-100 auditable scale.

    Each signal (precise gap-query match, provenance, method/object identity,
    broad background, and context amplification) contributes independently, so
    no single miss is terminal.  Generic-only method vocabulary and the lack
    of both precise-query and current-run provenance are negative features.
    When semantic embeddings are available, semantic helpfulness is the
    primary component (precise-gap and broad-background similarity) and the
    lexical signals become secondary.  On embedding failure the lexical
    signals become the full fallback.
    """

    semantic_features = dict(semantic_features or {})
    semantic_mode = str(semantic_features.get("mode") or "")
    semantic_background_similarity: float | None = None
    semantic_precise_similarity: float | None = None
    semantic_matched_query = ""
    semantic_fallback_error = ""
    if semantic_mode == "semantic":
        semantic_background_similarity = float(
            semantic_features.get("background_similarity") or 0.0
        )
        semantic_precise_similarity = float(
            semantic_features.get("max_precise_similarity") or 0.0
        )
        semantic_matched_query = str(
            semantic_features.get("matched_query") or ""
        )
    elif semantic_features:
        semantic_mode = "lexical_fallback"
        semantic_fallback_error = str(
            semantic_features.get("fallback_error_code") or ""
        )

    query_match_score = (
        round(
            _GENERATED_ONLY_QUERY_SCORE_MAX
            * float(generated_only_match.get("score") or 0.0),
            2,
        )
        if generated_only_match.get("matched")
        else 0.0
    )
    provenance_score = (
        _GENERATED_ONLY_PROVENANCE_SCORE_MAX
        if explicit_current_run
        else 0.0
    )
    if contract.object_anchor_mode == "method_fallback":
        if method_family_identity_present:
            lexical_identity_score = _GENERATED_ONLY_METHOD_FAMILY_SCORE_MAX
        elif any(
            term not in _METHOD_FAMILY_GENERIC_SINGLETONS
            for term in method_anchor_hits
        ):
            lexical_identity_score = 12.0
        else:
            lexical_identity_score = 0.0
    elif object_identity_evidence_present:
        lexical_identity_score = _GENERATED_ONLY_OBJECT_IDENTITY_SCORE_MAX
    elif object_hits:
        lexical_identity_score = 8.0
    else:
        lexical_identity_score = 0.0
    # In semantic mode, lexical method/object identity is a secondary audit
    # bonus, never a gate: relevant electromagnetic/optical support papers are
    # admitted on semantic precise/background strength alone.  In lexical
    # fallback it keeps its full role.
    if semantic_mode == "semantic":
        identity_score = (
            _SEMANTIC_IDENTITY_BONUS_MAX
            if lexical_identity_score >= 20.0
            else (
                _SEMANTIC_IDENTITY_PARTIAL_BONUS
                if lexical_identity_score > 0
                else 0.0
            )
        )
    else:
        identity_score = lexical_identity_score

    background_hits = sorted(
        set(contract.search_background_terms) & combined_tokens
    )
    background_score = min(
        _GENERATED_ONLY_BACKGROUND_SCORE_MAX,
        2.5 * len(background_hits),
    )
    if semantic_mode == "semantic":
        precise_component = min(
            _SEMANTIC_PRECISE_SCORE_MAX,
            _SEMANTIC_PRECISE_SCORE_MAX
            * float(semantic_precise_similarity or 0.0),
        )
        background_component = min(
            _SEMANTIC_BACKGROUND_SCORE_MAX,
            _SEMANTIC_BACKGROUND_SCORE_MAX
            * float(semantic_background_similarity or 0.0),
        )
        domain_gap_penalty = (
            _SEMANTIC_DOMAIN_GAP_PENALTY
            if float(semantic_background_similarity or 0.0)
            < _SEMANTIC_DOMAIN_GAP_BACKGROUND_SIM
            else 0.0
        )
    else:
        precise_component = query_match_score
        background_component = background_score
        domain_gap_penalty = 0.0
    context_pool = sorted(set(primary_context_hits) | set(primary_focus_hits))
    context_score = (
        min(_GENERATED_ONLY_CONTEXT_SCORE_MAX, 2.0 * len(context_pool))
        if identity_score > 0
        else 0.0
    )

    generic_penalty = 0.0
    if contract.object_anchor_mode == "method_fallback":
        if (
            lexical_identity_score == 0
            and (
                generic_only_match
                or (
                    bool(method_anchor_hits)
                    and not any(
                        term not in _METHOD_FAMILY_GENERIC_SINGLETONS
                        for term in method_anchor_hits
                    )
                )
            )
        ):
            generic_penalty = _GENERATED_ONLY_GENERIC_SINGLETON_PENALTY
    elif generic_only_match and lexical_identity_score == 0:
        generic_penalty = _GENERATED_ONLY_GENERIC_SINGLETON_PENALTY
    if semantic_mode == "semantic":
        # Lexical method/object overlaps are audit information, not a negative
        # gate, while semantic vectors are available.
        generic_penalty = 0.0
    if semantic_mode == "semantic":
        precise_side_present = (
            semantic_precise_similarity is not None
            and float(semantic_precise_similarity)
            >= _SEMANTIC_PRECISE_SIDE_MIN
        )
    else:
        precise_side_present = bool(generated_only_match.get("matched"))
    if semantic_mode == "semantic":
        provenance_gap_penalty = (
            _SEMANTIC_PRECISE_DEFICIT_PENALTY
            if not precise_side_present
            else 0.0
        )
    else:
        provenance_gap_penalty = (
            _GENERATED_ONLY_PROVENANCE_GAP_PENALTY
            if (not explicit_current_run and not precise_side_present)
            else 0.0
        )
    raw_score = (
        precise_component
        + provenance_score
        + identity_score
        + background_component
        + context_score
        - generic_penalty
        - provenance_gap_penalty
        - domain_gap_penalty
    )
    score = round(max(0.0, min(100.0, raw_score)), 2)
    threshold = (
        _SEMANTIC_MODE_USEFULNESS_THRESHOLD
        if semantic_mode == "semantic"
        else _GENERATED_ONLY_USEFULNESS_THRESHOLD
    )
    hits = generated_only_match.get("hits") or []
    matched_query = semantic_matched_query
    if not matched_query and hits and isinstance(hits[0], Mapping):
        matched_query = str(hits[0].get("query") or "")
    return {
        "score": score,
        "threshold": threshold,
        "matched_query": matched_query,
        "features": {
            "query_match_score": query_match_score,
            "semantic_mode": semantic_mode,
            "background_similarity": (
                round(float(semantic_background_similarity), 6)
                if semantic_background_similarity is not None
                else None
            ),
            "max_precise_similarity": (
                round(float(semantic_precise_similarity), 6)
                if semantic_precise_similarity is not None
                else None
            ),
            "semantic_matched_query": semantic_matched_query,
            "semantic_fallback_error_code": semantic_fallback_error,
            "domain_gap_penalty": domain_gap_penalty,
            "provenance_score": provenance_score,
            "identity_score": identity_score,
            "background_score": round(background_score, 2),
            "context_amplifier_score": context_score,
            "generic_singleton_penalty": generic_penalty,
            "provenance_gap_penalty": provenance_gap_penalty,
            "background_hit_terms": background_hits[:20],
            "generic_only_match": bool(generic_only_match),
            "method_family_identity_present": bool(
                method_family_identity_present
            ),
            "object_identity_evidence_present": bool(
                object_identity_evidence_present
            ),
            "method_anchor_hits": sorted(method_anchor_hits)[:20],
        },
    }


def _paper_decision_sample(decision: Mapping[str, Any]) -> dict[str, Any]:
    """Keep manifest samples informative without copying article text."""

    return {
        key: decision.get(key)
        for key in (
            "paper_id",
            "title",
            "accepted",
            "reason",
            "scope_fit",
            "score",
            "threshold",
            "minimum_core_anchor_hits",
            "core_anchor_hit_count",
            "core_anchor_hits",
            "object_anchor_hits",
            "object_head_anchor_hits",
            "object_modifier_anchor_hits",
            "compound_object_phrase_hits",
            "method_anchor_hits",
            "object_anchor_mode",
            "scientific_object_anchor_required",
            "object_identity_evidence_present",
            "contextual_method_transfer",
            "context_anchor_hits",
            "focus_anchor_hits",
            "primary_focus_anchor_hits",
            "focus_evidence_present",
            "strong_evidence_fields",
            "chunk_object_identity_evidence_count",
            "phrase_hits",
            "exclusion_hits",
            "regime_decision",
            "generic_only_match",
            "explicit_current_run",
            "usefulness_score",
            "usefulness_threshold",
            "usefulness_reason",
            "usefulness_features",
            "matched_precise_query",
            "relevance_context_sha256",
            "relevance_context_field_count",
            "semantic_mode",
            "background_similarity",
            "max_precise_similarity",
            "semantic_fallback_error_code",
        )
        if key in decision
    }


def _selection_contamination_indicators(
    decisions: Iterable[Mapping[str, Any]],
    *,
    threshold: float,
    minimum_core_anchor_hits: int,
) -> dict[str, Any]:
    items = list(decisions)
    reason_counts: Counter[str] = Counter(
        str(item.get("reason") or "unknown") for item in items if not item.get("accepted")
    )
    exclusion_counts: Counter[str] = Counter(
        str(term)
        for item in items
        for term in (item.get("exclusion_hits") or [])
    )
    selected_count = sum(1 for item in items if item.get("accepted"))
    source_count = len(items)
    rejection_rate = (
        round((source_count - selected_count) / source_count, 4)
        if source_count
        else 0.0
    )
    flags: list[str] = []
    if source_count and selected_count == 0:
        flags.append("no_base_papers_passed_topic_contract")
    if source_count and rejection_rate >= 0.8:
        flags.append("high_rejection_rate")
    generic_only_count = sum(1 for item in items if item.get("generic_only_match"))
    if generic_only_count:
        flags.append("generic_shared_terms_detected")
    if exclusion_counts:
        flags.append("explicit_exclusion_boundary_contamination")
    focus_miss_count = sum(
        1
        for item in items
        if item.get("reason") == "insufficient_core_anchor_evidence"
        and item.get("focus_evidence_present") is False
    )
    if focus_miss_count:
        flags.append("focus_anchor_mismatch_detected")
    return {
        "source_paper_count": source_count,
        "selected_paper_count": selected_count,
        "rejected_paper_count": source_count - selected_count,
        "selection_rate": round(selected_count / source_count, 4) if source_count else 0.0,
        "rejection_rate": rejection_rate,
        "threshold": threshold,
        "minimum_core_anchor_hits": minimum_core_anchor_hits,
        "rejection_reason_counts": dict(sorted(reason_counts.items())),
        "exclusion_hit_counts": dict(sorted(exclusion_counts.items())),
        "generic_only_match_count": generic_only_count,
        "single_or_insufficient_core_anchor_count": sum(
            1
            for item in items
            if item.get("reason") == "insufficient_core_anchor_evidence"
        ),
        "focus_anchor_miss_count": focus_miss_count,
        "topic_object_anchor_miss_count": sum(
            1
            for item in items
            if item.get("reason") in {"topic_object_anchor_miss", "generic_shared_term_only"}
        ),
        "flags": flags,
    }


def _quote_identifier(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _table_names(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]).casefold(): str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()]


def _table_xcolumns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_xinfo({_quote_identifier(table)})").fetchall()
    if not rows:
        return _table_columns(conn, table)
    return [str(row[1]) for row in rows if len(row) < 7 or int(row[6] or 0) == 0]


def _row_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}  # type: ignore[attr-defined]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _raw_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return _json_object(row.get("raw_json"))


def _row_value(row: Mapping[str, Any], raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
        if key in raw and raw.get(key) not in (None, ""):
            return raw.get(key)
    return None


def _search_text_for_row(row: Mapping[str, Any], *, table: str) -> str:
    raw = _raw_record(row)
    values: list[Any] = []
    if table == "papers":
        keys = (
            "title",
            "doi",
            "venue",
            "search_text",
            "abstract",
            "keywords",
            "fields_of_study",
            "topic_tags",
        )
    else:
        keys = (
            "title",
            "doi",
            "section_path",
            "section",
            "search_text",
            "text",
            "caption",
            "visual_role",
            "keywords",
        )
    for key in keys:
        value = _row_value(row, raw, key)
        if isinstance(value, (list, tuple)):
            values.extend(value)
        else:
            values.append(value)
    return " ".join(str(value or "") for value in values)


def _canonical_identity(row: Mapping[str, Any]) -> str:
    raw = _raw_record(row)
    doi = str(_row_value(row, raw, "doi", "DOI") or "").strip().casefold()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi).strip().strip(".;,")
    if doi:
        return f"doi:{doi}"
    paper_id = str(_row_value(row, raw, "paper_id") or "").strip()
    return f"paper:{paper_id}"


def _identity_decision(
    row: Mapping[str, Any],
    *,
    table: str,
    policy: S2Policy,
    contract: TopicScopeContract,
    related_chunks: Iterable[Mapping[str, Any]] = (),
    explicit_current_run: bool = False,
) -> dict[str, Any]:
    raw = _raw_record(row)
    identity_key = "paper_id" if table == "papers" else "chunk_id"
    identity = str(_row_value(row, raw, identity_key) or "").strip()
    paper_id = str(_row_value(row, raw, "paper_id") or "").strip()
    title = str(_row_value(row, raw, "title") or "").strip()
    if not identity:
        return {"accepted": False, "reason": f"missing_{identity_key}", "scope_fit": "unreviewed"}
    if table != "papers" and not paper_id:
        return {"accepted": False, "reason": "missing_chunk_paper_id", "scope_fit": "unreviewed"}
    if table == "papers" and policy.title_identity_required and not title:
        return {"accepted": False, "reason": "missing_title_identity", "scope_fit": "unreviewed"}
    existing_scope = normalize_scope_fit(_row_value(row, raw, "scope_fit"), default="unreviewed")
    if existing_scope == "out_of_scope":
        return {"accepted": False, "reason": "record_scope_fit_out_of_scope", "scope_fit": existing_scope}
    if table == "papers":
        match = _score_paper_scope(
            contract,
            row,
            related_chunks=related_chunks,
            explicit_current_run=explicit_current_run,
        )
    else:
        match = _scope_match(contract, _search_text_for_row(row, table=table))
    if not match["accepted"]:
        return match
    return {
        **match,
        "identity": identity,
        "paper_id": paper_id or identity,
        "title": title,
    }


def _permission_fields(
    row: Mapping[str, Any],
    *,
    table: str,
    scope_fit: str,
) -> dict[str, Any]:
    raw = _raw_record(row)
    route: dict[str, Any] = {}
    for value in (
        raw.get("route_provenance"),
        raw.get("route_provenance_json"),
        row.get("route_provenance"),
        row.get("route_provenance_json"),
    ):
        route.update(_json_object(value))
    provenance: dict[str, Any] = {}
    for value in (
        raw.get("provenance"),
        raw.get("provenance_json"),
        row.get("provenance"),
        row.get("provenance_json"),
    ):
        provenance.update(_json_object(value))
    raw_depth_value = (
        raw.get("content_depth")
        or route.get("content_depth")
        or provenance.get("content_depth")
        or raw.get("evidence_level")
        or raw.get("source_kind")
    )
    column_depth_value = row.get("content_depth") or row.get("evidence_level") or row.get("source_kind")
    raw_depth = normalize_content_depth(raw_depth_value, default="") if raw_depth_value else ""
    column_depth = normalize_content_depth(column_depth_value, default="") if column_depth_value else ""
    # When legacy route JSON explicitly says metadata/abstract, it wins over a
    # migration default such as fulltext.  A stale default must not promote it.
    depth = raw_depth if raw_depth in {"metadata", "abstract", "abstract_claim", "tldr"} else (column_depth or raw_depth or "metadata")
    context_raw = _row_value(row, raw, "context_complete")
    if context_raw in (None, ""):
        context_raw = route.get("context_complete", provenance.get("context_complete", depth in {"fulltext", "structured_snippet"}))
    context_complete = bool(context_raw) and str(context_raw).casefold() not in {"0", "false", "no"}
    raw_permission_value = (
        raw.get("use_permission")
        or route.get("use_permission")
        or provenance.get("use_permission")
    )
    column_permission_value = row.get("use_permission")
    raw_permission = (
        normalize_use_permission(raw_permission_value, default="")
        if raw_permission_value
        else ""
    )
    column_permission = (
        normalize_use_permission(column_permission_value, default="")
        if column_permission_value
        else ""
    )
    permission = raw_permission or column_permission
    if raw_permission in {"discovery_only", "background_and_candidate_only"}:
        permission = raw_permission
    if not permission:
        permission = str(
            permission_for_content(
                depth,
                scope_fit=scope_fit,
                context_complete=context_complete,
            )["use_permission"]
        )
    # A legacy or corrupted row cannot gain factual permission from a stale
    # column when its content depth is metadata/abstract or its scope is not
    # direct.  This is the final permission ceiling.
    if depth in {"metadata", "abstract", "abstract_claim", "tldr"}:
        permission = str(
            permission_for_content(
                depth,
                scope_fit=scope_fit,
                context_complete=context_complete,
            )["use_permission"]
        )
    elif scope_fit != "direct" and permission == "factual_support":
        permission = "contextual_or_qualified_support"
    allowed = _row_value(row, raw, "allowed_claim_kinds_json", "allowed_claim_kinds")
    if isinstance(allowed, str):
        allowed = _json_object({"value": allowed}).get("value")
        try:
            allowed = json.loads(str(allowed))
        except json.JSONDecodeError:
            allowed = []
    if not isinstance(allowed, list):
        allowed = list(
            permission_for_content(
                depth,
                scope_fit=scope_fit,
                context_complete=context_complete,
            )["allowed_claim_kinds"]
        )
    if permission == "discovery_only":
        allowed = ["discovery", "candidate_lead"]
    return {
        "discovery_route": str(
            _row_value(row, raw, "discovery_route")
            or route.get("discovery_route")
            or "unknown"
        ),
        "materialization_route": str(
            _row_value(row, raw, "materialization_route")
            or route.get("materialization_route")
            or "not_materialized"
        ),
        "content_depth": depth,
        "use_permission": normalize_use_permission(permission),
        "scope_fit": normalize_scope_fit(scope_fit),
        "context_complete": context_complete,
        "allowed_claim_kinds": [str(item) for item in allowed if str(item).strip()],
        "route_provenance": {
            **route,
            "discovery_route": str(
                _row_value(row, raw, "discovery_route")
                or route.get("discovery_route")
                or "unknown"
            ),
            "materialization_route": str(
                _row_value(row, raw, "materialization_route")
                or route.get("materialization_route")
                or "not_materialized"
            ),
            "content_depth": depth,
            "use_permission": normalize_use_permission(permission),
            "scope_fit": normalize_scope_fit(scope_fit),
            "context_complete": context_complete,
        },
    }


def _is_evidence_permission(fields: Mapping[str, Any]) -> bool:
    return str(fields.get("use_permission") or "") in {
        "factual_support",
        "contextual_or_qualified_support",
    } and str(fields.get("content_depth") or "") in {
        "fulltext",
        "partial_fulltext",
        "structured_snippet",
        "abstract_claim",
    } and str(fields.get("scope_fit") or "") == "direct"


def _is_factual_permission(fields: Mapping[str, Any]) -> bool:
    return _is_evidence_permission(fields) and fields.get("use_permission") == "factual_support"


def _set_json_field(raw_json: Any, updates: Mapping[str, Any]) -> str:
    raw = _json_object(raw_json)
    raw["topic_scope_audit"] = dict(updates)
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


def _ensure_core_schema(conn: sqlite3.Connection) -> None:
    for table, columns in _CORE_COLUMN_DEFS.items():
        definitions = ", ".join(
            f"{_quote_identifier(name)} {definition}" for name, definition in columns.items()
        )
        conn.execute(f"CREATE TABLE IF NOT EXISTS {_quote_identifier(table)}({definitions})")
        existing = set(_table_columns(conn, table))
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE {_quote_identifier(table)} ADD COLUMN {_quote_identifier(name)} {definition}"
                )
    _ensure_s2_tables(conn)


def create_empty_review_kb(path: str | Path) -> Path:
    """Create or validate an immutable, paper-free starting database.

    A new research question does not need a historical paper collection in
    order to start.  The S2/OA stages can populate the run-local overlay from
    this empty schema.  On resume, an existing seed is checked rather than
    rewritten so accidental contamination cannot be hidden.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file():
            raise TopicScopedKBError(
                f"empty task seed is not a file: {target}"
            )
        try:
            connection = sqlite3.connect(
                f"file:{target.resolve()}?mode=ro",
                uri=True,
            )
            try:
                integrity = str(
                    connection.execute("PRAGMA integrity_check").fetchone()[0]
                )
                names = _table_names(connection)
                missing = [
                    name for name in ("papers", "text_chunks")
                    if name not in names
                ]
                populated = {
                    name: int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {_quote_identifier(names[name])}"
                        ).fetchone()[0]
                    )
                    for name in ("papers", "text_chunks")
                    if name in names
                }
            finally:
                connection.close()
        except (OSError, sqlite3.DatabaseError) as exc:
            raise TopicScopedKBError(
                f"empty task seed is not a readable SQLite database: {exc}"
            ) from exc
        if integrity.casefold() != "ok":
            raise TopicScopedKBError(
                f"empty task seed failed SQLite integrity check: {integrity}"
            )
        if missing:
            raise TopicScopedKBError(
                "empty task seed is missing required tables: "
                + ", ".join(missing)
            )
        nonempty = {name: count for name, count in populated.items() if count}
        if nonempty:
            raise TopicScopedKBError(
                "empty task seed already contains research material: "
                + ", ".join(
                    f"{name}={count}" for name, count in sorted(nonempty.items())
                )
            )
        return target

    connection = sqlite3.connect(str(target))
    try:
        with connection:
            _ensure_core_schema(connection)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    return target


def _copy_schema(source: sqlite3.Connection, target: sqlite3.Connection) -> list[str]:
    schema_rows = source.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 1 WHEN 'index' THEN 2 ELSE 3 END, name"
    ).fetchall()
    virtual_names = {
        str(name).casefold()
        for object_type, name, sql in schema_rows
        if object_type == "table"
        and str(sql or "").lstrip().casefold().startswith("create virtual table")
    }
    virtual_shadow_prefixes = tuple(f"{name}_" for name in virtual_names)
    deferred: list[str] = []
    for object_type, name, sql in schema_rows:
        if not sql:
            continue
        folded_name = str(name).casefold()
        if any(
            folded_name.startswith(prefix) and folded_name not in virtual_names
            for prefix in virtual_shadow_prefixes
        ):
            continue
        if object_type in {"index", "trigger"}:
            deferred.append(str(sql))
            continue
        try:
            target.execute(str(sql))
        except sqlite3.DatabaseError as exc:
            raise TopicScopedKBError(f"cannot clone SQLite {object_type} {name}: {exc}") from exc
    return deferred


def _copy_rows(
    target: sqlite3.Connection,
    table: str,
    rows: Iterable[Mapping[str, Any]],
) -> int:
    rows = list(rows)
    if not rows or table.casefold() in _FTS_TABLES:
        return 0
    columns = _table_xcolumns(target, table)
    if not columns:
        return 0
    insert_columns = [column for column in columns if column in rows[0]]
    if not insert_columns:
        return 0
    sql = (
        f"INSERT INTO {_quote_identifier(table)} "
        f"({','.join(_quote_identifier(column) for column in insert_columns)}) "
        f"VALUES ({','.join('?' for _ in insert_columns)})"
    )
    target.executemany(sql, [tuple(row.get(column) for column in insert_columns) for row in rows])
    return len(rows)


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    table_names = _table_names(conn)
    if "paper_fts" in table_names and "papers" in table_names:
        columns = set(_table_columns(conn, table_names["paper_fts"]))
        if {"paper_id", "title", "search_text"} <= columns:
            conn.execute(f"DELETE FROM {_quote_identifier(table_names['paper_fts'])}")
            conn.execute(
                f"INSERT INTO {_quote_identifier(table_names['paper_fts'])}(paper_id,title,search_text) "
                f"SELECT paper_id,title,search_text FROM {_quote_identifier(table_names['papers'])}"
            )
    if "text_chunk_fts" in table_names and "text_chunks" in table_names:
        columns = set(_table_columns(conn, table_names["text_chunk_fts"]))
        required = {"chunk_id", "paper_id", "title", "section_path", "text"}
        if required <= columns:
            conn.execute(f"DELETE FROM {_quote_identifier(table_names['text_chunk_fts'])}")
            conn.execute(
                f"INSERT INTO {_quote_identifier(table_names['text_chunk_fts'])}(chunk_id,paper_id,title,section_path,text) "
                f"SELECT chunk_id,paper_id,title,section_path,text FROM {_quote_identifier(table_names['text_chunks'])}"
            )
    if "visual_chunk_fts" in table_names and "visual_chunks" in table_names:
        columns = set(_table_columns(conn, table_names["visual_chunk_fts"]))
        required = {"chunk_id", "paper_id", "title", "visual_role", "caption", "search_text"}
        if required <= columns:
            conn.execute(f"DELETE FROM {_quote_identifier(table_names['visual_chunk_fts'])}")
            conn.execute(
                f"INSERT INTO {_quote_identifier(table_names['visual_chunk_fts'])}(chunk_id,paper_id,title,visual_role,caption,search_text) "
                f"SELECT chunk_id,paper_id,title,visual_role,caption,search_text FROM {_quote_identifier(table_names['visual_chunks'])}"
            )
    if "visual_asset_fts" in table_names and "visual_assets" in table_names:
        columns = set(_table_columns(conn, table_names["visual_asset_fts"]))
        required = {"asset_id", "paper_id", "title", "label", "caption", "search_text"}
        if required <= columns:
            conn.execute(f"DELETE FROM {_quote_identifier(table_names['visual_asset_fts'])}")
            conn.execute(
                f"INSERT INTO {_quote_identifier(table_names['visual_asset_fts'])}(asset_id,paper_id,title,label,caption,search_text) "
                f"SELECT asset_id,paper_id,title,label,caption,search_text FROM {_quote_identifier(table_names['visual_assets'])}"
            )
    if "concept_fts" in table_names and "concepts" in table_names:
        columns = set(_table_columns(conn, table_names["concept_fts"]))
        required = {"concept_id", "kind", "label", "description"}
        if required <= columns:
            conn.execute(f"DELETE FROM {_quote_identifier(table_names['concept_fts'])}")
            conn.execute(
                f"INSERT INTO {_quote_identifier(table_names['concept_fts'])}(concept_id,kind,label,description) "
                f"SELECT concept_id,kind,label,description FROM {_quote_identifier(table_names['concepts'])}"
            )


def _event_category(value: str, *, default: str) -> str:
    normalized = str(value or "").strip().casefold()
    aliases = {
        "search": "discovery_search",
        "s2_relevance_search": "discovery_search",
        "s2_review_search": "discovery_search",
        "s2_foundation_search": "discovery_search",
        "snippet": "snippet_search",
        "snippets": "snippet_search",
        "batch": "batch_enrichment",
        "multi_seed_recommendation": "multi_seed_recommendations",
    }
    return aliases.get(normalized, normalized if normalized in QUERY_CATEGORIES else default)


def build_s2_query_telemetry(
    *,
    discovery_runs: Iterable[Mapping[str, Any]] = (),
    snippet_runs: Iterable[Mapping[str, Any]] = (),
    graph_runs: Iterable[Mapping[str, Any]] = (),
    enrichment_runs: Iterable[Mapping[str, Any]] = (),
    extra_events: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Normalize all observed S2 traffic, including graph endpoints."""

    events: list[dict[str, Any]] = []

    def add(run: Mapping[str, Any], category: str) -> None:
        event = {
            "query_category": _event_category(category, default=category),
            "query": str(run.get("query") or ""),
            "channel": str(run.get("channel") or ""),
            "endpoint": str(run.get("endpoint") or ""),
            "status_category": str(run.get("status_category") or "unknown"),
            "status_code": int(run.get("status_code") or 0),
            "result_count": int(run.get("result_count") or 0),
            "cache_hit": bool(run.get("cache_hit", False)),
            "wait_seconds": float(run.get("wait_seconds") or 0.0),
            "ok": str(run.get("status_category") or "ok") in {"ok", "cached", "skipped"}
            or bool(run.get("ok", False)),
        }
        for key in ("facet_id", "wave_id", "seed_count", "paper_id", "paper_ids"):
            if key in run:
                event[key] = copy.deepcopy(run[key])
        events.append(event)

    for run in discovery_runs:
        add(run, "discovery_search")
    for run in snippet_runs:
        add(run, "snippet_search")
    for run in graph_runs:
        add(run, str(run.get("channel") or "graph"))
    for run in enrichment_runs:
        add(run, "batch_enrichment")
    for event in extra_events:
        add(event, str(event.get("query_category") or event.get("category") or "discovery_search"))

    category_counts = {category: 0 for category in QUERY_CATEGORIES}
    result_counts = {category: 0 for category in QUERY_CATEGORIES}
    failed_counts = {category: 0 for category in QUERY_CATEGORIES}
    cache_hit_counts = {category: 0 for category in QUERY_CATEGORIES}
    wait_seconds = {category: 0.0 for category in QUERY_CATEGORIES}
    for event in events:
        category = event["query_category"]
        if category not in category_counts:
            category_counts[category] = 0
            result_counts[category] = 0
            failed_counts[category] = 0
            cache_hit_counts[category] = 0
            wait_seconds[category] = 0.0
        category_counts[category] += 1
        result_counts[category] += int(event["result_count"])
        failed_counts[category] += 0 if event["ok"] else 1
        cache_hit_counts[category] += 1 if event["cache_hit"] else 0
        wait_seconds[category] += float(event["wait_seconds"])
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "total_query_count": len(events),
        "category_counts": category_counts,
        "result_counts": result_counts,
        "failed_counts": failed_counts,
        "cache_hit_counts": cache_hit_counts,
        "wait_seconds_by_category": {
            key: round(value, 4) for key, value in wait_seconds.items()
        },
        "graph_query_count": sum(category_counts.get(key, 0) for key in GRAPH_QUERY_CATEGORIES),
        "events": events,
    }


def _immutable_json_write(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TopicScopedKBError(f"immutable artifact is unreadable: {path}") from exc
        if _canonical_json(existing) != _canonical_json(payload):
            raise TopicScopedKBError(f"immutable artifact conflict: {path}")
        return dict(existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
    except FileExistsError:
        return _immutable_json_write(path, payload)
    return dict(payload)


def _manifest_hash_is_valid(manifest: Mapping[str, Any]) -> bool:
    stored = str(manifest.get("manifest_sha256") or "")
    if not stored:
        return False
    body = dict(manifest)
    body.pop("manifest_sha256", None)
    return stored == _sha256_bytes(_canonical_json(body).encode("utf-8"))


def _is_explicit_current_run_paper(paper: S2PaperRecord) -> bool:
    """Recognize only explicit run-local discovery provenance.

    A normal legacy row has no such marker.  This exception is intentionally
    narrow so a stale ``scope_fit=direct`` or a generic provider label cannot
    bypass the paper-level topic gate.
    """

    route = str(getattr(paper, "discovery_route", "") or "").casefold()
    if any(marker in route for marker in _CURRENT_RUN_ROUTE_MARKERS):
        return True
    raw_metadata = getattr(paper, "raw_metadata", {}) or {}
    if isinstance(raw_metadata, Mapping):
        for key in ("current_run", "current_run_discovery", "run_local", "run_local_discovery"):
            value = raw_metadata.get(key)
            if value is True or str(value).casefold() in {"1", "true", "yes"}:
                return True
        raw_route = str(raw_metadata.get("discovery_route") or "").casefold()
        if any(marker in raw_route for marker in _CURRENT_RUN_ROUTE_MARKERS):
            return True
    for event in getattr(paper, "route_events", []) or []:
        if not isinstance(event, Mapping):
            continue
        if any(
            event.get(key) is True
            or str(event.get(key) or "").casefold() in {"1", "true", "yes"}
            for key in ("current_run", "current_run_discovery", "run_local", "run_local_discovery")
        ):
            return True
        event_text = " ".join(
            str(event.get(key) or "").casefold()
            for key in ("route", "discovery_route", "channel", "event", "wave_id", "source")
        )
        if any(marker in event_text for marker in _CURRENT_RUN_ROUTE_MARKERS):
            return True
    return False


def _s2_identity_alias(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if text.startswith("https://doi.org/"):
        text = text[len("https://doi.org/"):]
    if text.startswith("doi:"):
        text = text[4:]
    for prefix in ("s2:", "s2paper:", "semantic_scholar:", "semantic-scholar:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return re.sub(r"\s+", " ", text)


def _s2_paper_aliases(paper: S2PaperRecord) -> set[str]:
    aliases: set[str] = set()
    for value in (paper.paper_id, paper.doi, paper.title):
        alias = _s2_identity_alias(value)
        if alias:
            aliases.add(alias)
    if paper.corpus_id not in (None, ""):
        aliases.add(_s2_identity_alias(paper.corpus_id))
        aliases.add(_s2_identity_alias(f"CorpusId:{paper.corpus_id}"))
    for value in (paper.external_ids or {}).values():
        alias = _s2_identity_alias(value)
        if alias:
            aliases.add(alias)
    return aliases


def _s2_chunk_aliases(chunk: UnifiedTextChunk) -> set[str]:
    aliases: set[str] = set()
    for value in (chunk.paper_id, chunk.doi, chunk.title, chunk.corpus_id):
        alias = _s2_identity_alias(value)
        if alias:
            aliases.add(alias)
    if chunk.corpus_id not in (None, ""):
        aliases.add(_s2_identity_alias(f"CorpusId:{chunk.corpus_id}"))
    raw = chunk.raw_metadata if isinstance(chunk.raw_metadata, Mapping) else {}
    item = raw.get("s2_item") if isinstance(raw, Mapping) else {}
    parent = item.get("paper") if isinstance(item, Mapping) else {}
    if isinstance(parent, Mapping):
        for key in ("paperId", "corpusId", "title"):
            alias = _s2_identity_alias(parent.get(key))
            if alias:
                aliases.add(alias)
        external_ids = parent.get("externalIds") or {}
        if isinstance(external_ids, Mapping):
            for value in external_ids.values():
                alias = _s2_identity_alias(value)
                if alias:
                    aliases.add(alias)
    return aliases


def _rebind_s2_chunk_parents(
    papers: Sequence[S2PaperRecord],
    chunks: Sequence[UnifiedTextChunk],
) -> tuple[list[UnifiedTextChunk], list[dict[str, Any]]]:
    """Resolve provider aliases to the exact accepted-paper identity.

    This is an identity normalization step, not a scope bypass: exact parent
    membership is still checked by :meth:`_incoming_chunk` after rebinding,
    and ambiguous aliases fail closed.
    """

    alias_to_ids: dict[str, set[str]] = {}
    paper_ids = {paper.paper_id for paper in papers if paper.paper_id}
    for paper in papers:
        if not paper.paper_id:
            continue
        for alias in _s2_paper_aliases(paper):
            alias_to_ids.setdefault(alias, set()).add(paper.paper_id)
    rebound: list[UnifiedTextChunk] = []
    events: list[dict[str, Any]] = []
    for chunk in chunks:
        if chunk.paper_id in paper_ids:
            rebound.append(chunk)
            continue
        matches = {
            paper_id
            for alias in _s2_chunk_aliases(chunk)
            for paper_id in alias_to_ids.get(alias, set())
        }
        if len(matches) != 1:
            rebound.append(chunk)
            continue
        canonical_parent = next(iter(matches))
        rebound.append(replace(
            chunk,
            paper_id=canonical_parent,
            route_provenance={
                **dict(chunk.route_provenance),
                "identity_resolution": {
                    "provider_parent_id": chunk.paper_id,
                    "canonical_parent_id": canonical_parent,
                    "method": "exact_s2_alias",
                },
            },
        ))
        events.append({
            "chunk_id": chunk.chunk_id,
            "provider_parent_id": chunk.paper_id,
            "canonical_parent_id": canonical_parent,
            "method": "exact_s2_alias",
        })
    return rebound, events


def _supplement_identity_matches(
    prior: S2PaperRecord,
    incoming: S2PaperRecord,
) -> bool:
    """Require a later material wave to remain bound to the same paper."""

    if prior.paper_id != incoming.paper_id:
        return False
    prior_doi = normalize_doi(prior.doi)
    incoming_doi = normalize_doi(incoming.doi)
    if prior_doi and incoming_doi and prior_doi != incoming_doi:
        return False
    if prior.title and incoming.title and title_match_score(prior.title, incoming.title) < 0.92:
        return False
    if prior.corpus_id is not None and incoming.corpus_id is not None:
        if int(prior.corpus_id) != int(incoming.corpus_id):
            return False
    if prior.year and incoming.year and abs(int(prior.year) - int(incoming.year)) > 2:
        return False
    return True


class TopicScopedKBStage:
    """Create, enrich, audit, and finalize one run-local scoped overlay."""

    def __init__(
        self,
        *,
        query_plan_path: str | Path,
        base_kb_sqlite: str | Path,
        work_dir: str | Path,
        policy: S2Policy,
        scope_contract: TopicScopeContract,
        semantic_scores: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.query_plan_path = Path(query_plan_path)
        self.base_kb_sqlite = Path(base_kb_sqlite)
        self.work_dir = Path(work_dir)
        self.policy = policy
        self.scope_contract = scope_contract
        self.runtime_kb = self.work_dir / "review_knowledge_base.s2.sqlite"
        self.manifest_path = self.work_dir / "KB_MANIFEST.json"
        self.telemetry_path = self.work_dir / "S2_QUERY_TELEMETRY.json"
        self.selection_report: dict[str, Any] = {}
        self.ingest_report: dict[str, Any] = {}
        self.final_filter_report: dict[str, Any] = {}
        self.filtered_graph: LiteratureGraph | None = None
        self._allowed_paper_ids: set[str] = set()
        self._paper_decisions: dict[str, dict[str, Any]] = {}
        self._incoming_papers: list[S2PaperRecord] = []
        self._incoming_chunks: list[UnifiedTextChunk] = []
        self._incoming_graph: LiteratureGraph | None = None
        self._incoming_query_telemetry: Mapping[str, Any] = {}
        self._incoming_extra_manifest: Mapping[str, Any] = {}
        self._semantic_scores: dict[str, Mapping[str, Any]] = dict(
            semantic_scores or {}
        )

    def register_semantic_scores(
        self, scores: Mapping[str, Mapping[str, Any]]
    ) -> None:
        """Register precomputed candidate semantic features for one run.

        The bootstrap computes these once per generated-only discovery batch
        (one batched embedding call) and registers them before ranking; the
        same features are reused by later ingestion so a paper is never
        accepted semantically pre-cap and then rejected lexically at ingest.
        """

        self._semantic_scores.update(
            {
                str(paper_id): dict(features)
                for paper_id, features in (scores or {}).items()
            }
        )

    def _source_rows(self, conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        conn.row_factory = sqlite3.Row
        return [
            _row_dict(row)
            for row in conn.execute(f"SELECT * FROM {_quote_identifier(table)}").fetchall()
        ]

    def _scope_decision_for_mapping(
        self,
        row: Mapping[str, Any],
        *,
        table: str,
        related_chunks: Iterable[Mapping[str, Any]] = (),
        explicit_current_run: bool = False,
    ) -> dict[str, Any]:
        return _identity_decision(
            row,
            table=table,
            policy=self.policy,
            contract=self.scope_contract,
            related_chunks=related_chunks,
            explicit_current_run=explicit_current_run,
        )

    def _native_update(self, conn: sqlite3.Connection, table: str, identity: str, fields: Mapping[str, Any]) -> None:
        if table == "papers":
            key = "paper_id"
        elif table == "text_chunks":
            key = "chunk_id"
        else:
            return
        columns = set(_table_columns(conn, table))
        updates: dict[str, Any] = {}
        for name, value in {
            "discovery_route": fields.get("discovery_route"),
            "materialization_route": fields.get("materialization_route"),
            "content_depth": fields.get("content_depth"),
            "use_permission": fields.get("use_permission"),
            "scope_fit": fields.get("scope_fit"),
            "context_complete": int(bool(fields.get("context_complete"))),
            "allowed_claim_kinds_json": json.dumps(fields.get("allowed_claim_kinds") or [], ensure_ascii=False),
            "route_provenance_json": json.dumps(fields.get("route_provenance") or {}, ensure_ascii=False, separators=(",", ":")),
            "provenance_json": json.dumps(fields.get("route_provenance") or {}, ensure_ascii=False, separators=(",", ":")),
        }.items():
            if name in columns:
                updates[name] = value
        if "raw_json" in columns:
            raw_row = conn.execute(
                f"SELECT raw_json FROM {_quote_identifier(table)} WHERE {_quote_identifier(key)}=?",
                (identity,),
            ).fetchone()
            updates["raw_json"] = _set_json_field(
                raw_row[0] if raw_row else "{}",
                {
                    "scope_fit": fields.get("scope_fit"),
                    "use_permission": fields.get("use_permission"),
                    "content_depth": fields.get("content_depth"),
                    "context_complete": bool(fields.get("context_complete")),
                },
            )
        if not updates:
            return
        assignments = ",".join(f"{_quote_identifier(name)}=?" for name in updates)
        conn.execute(
            f"UPDATE {_quote_identifier(table)} SET {assignments} WHERE {_quote_identifier(key)}=?",
            (*updates.values(), identity),
        )

    def _select_base_rows(self, source: sqlite3.Connection) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
        names = _table_names(source)
        selected: dict[str, list[dict[str, Any]]] = {}
        totals: Counter[str] = Counter()
        rejected: Counter[str] = Counter()
        rejection_samples: dict[str, list[str]] = {}
        paper_decisions: list[dict[str, Any]] = []

        paper_table = names.get("papers")
        chunk_table = names.get("text_chunks")
        chunk_rows = self._source_rows(source, chunk_table) if chunk_table else []
        chunks_by_paper: dict[str, list[dict[str, Any]]] = {}
        for chunk in chunk_rows:
            paper_id = str(chunk.get("paper_id") or "").strip()
            if paper_id:
                chunks_by_paper.setdefault(paper_id, []).append(chunk)

        if paper_table:
            seen_identity_keys: set[str] = set()
            paper_rows = sorted(
                self._source_rows(source, paper_table),
                key=lambda row: (_canonical_identity(row), str(row.get("paper_id") or "")),
            )
            for row in paper_rows:
                totals["papers"] += 1
                identity_key = _canonical_identity(row)
                if identity_key in seen_identity_keys:
                    rejected["papers:duplicate_identity"] += 1
                    rejection_samples.setdefault("papers:duplicate_identity", []).append(
                        str(row.get("paper_id") or "<missing>")
                    )
                    continue
                seen_identity_keys.add(identity_key)
                identity = str(row.get("paper_id") or "").strip()
                decision = self._scope_decision_for_mapping(
                    row,
                    table="papers",
                    related_chunks=chunks_by_paper.get(identity, ()),
                )
                paper_decisions.append(dict(decision))
                if decision.get("accepted"):
                    selected.setdefault(paper_table, []).append(row)
                    if identity:
                        self._allowed_paper_ids.add(identity)
                        self._paper_decisions[identity] = decision
                else:
                    reason = str(decision.get("reason") or "rejected")
                    rejected[f"papers:{reason}"] += 1
                    rejection_samples.setdefault(f"papers:{reason}", []).append(identity or "<missing>")

        if chunk_table:
            for row in chunk_rows:
                totals["text_chunks"] += 1
                paper_id = str(row.get("paper_id") or "").strip()
                chunk_id = str(row.get("chunk_id") or "").strip()
                if paper_id in self._allowed_paper_ids and chunk_id:
                    # Chunk scope is inherited only after the parent paper has
                    # passed.  This intentionally keeps a paper's generic
                    # methods/results paragraphs together for downstream use.
                    selected.setdefault(chunk_table, []).append(row)
                else:
                    reason = "missing_chunk_id" if not chunk_id else "parent_paper_not_selected"
                    rejected[f"text_chunks:{reason}"] += 1
                    rejection_samples.setdefault(f"text_chunks:{reason}", []).append(
                        chunk_id or "<missing>"
                    )

        for logical_table in ("visual_assets", "visual_chunks"):
            table = names.get(logical_table)
            if not table:
                continue
            for row in self._source_rows(source, table):
                totals[logical_table] += 1
                paper_id = str(row.get("paper_id") or "").strip()
                visual_id = str(row.get("chunk_id") or row.get("asset_id") or "").strip()
                if paper_id in self._allowed_paper_ids and visual_id:
                    selected.setdefault(table, []).append(row)
                else:
                    reason = (
                        "missing_visual_identity"
                        if not visual_id
                        else "parent_paper_not_selected"
                    )
                    rejected[f"{logical_table}:{reason}"] += 1
                    rejection_samples.setdefault(f"{logical_table}:{reason}", []).append(
                        visual_id or "<missing>"
                    )

        selected_ids = {
            "papers": set(self._allowed_paper_ids),
            "text_chunks": {
                str(row.get("chunk_id"))
                for table, rows in selected.items()
                if table.casefold() == "text_chunks"
                for row in rows
                if row.get("chunk_id")
            },
            "visual_chunks": {
                str(row.get("chunk_id"))
                for table, rows in selected.items()
                if table.casefold() == "visual_chunks"
                for row in rows
                if row.get("chunk_id")
            },
            "visual_assets": {
                str(row.get("asset_id"))
                for table, rows in selected.items()
                if table.casefold() == "visual_assets"
                for row in rows
                if row.get("asset_id")
            },
        }
        for logical_table in ("paper_citations", "s2_literature_graph_nodes", "s2_literature_graph_edges", "links", "concept_mentions", "concepts"):
            table = names.get(logical_table)
            if not table:
                continue
            rows = self._source_rows(source, table)
            kept: list[dict[str, Any]] = []
            for row in rows:
                if logical_table == "paper_citations":
                    keep = str(row.get("citing_paper_id") or "") in selected_ids["papers"] and str(row.get("cited_paper_id") or "") in selected_ids["papers"]
                elif logical_table == "s2_literature_graph_nodes":
                    keep = str(row.get("paper_id") or "") in selected_ids["papers"]
                elif logical_table == "s2_literature_graph_edges":
                    keep = str(row.get("source_paper_id") or "") in selected_ids["papers"] and str(row.get("target_paper_id") or "") in selected_ids["papers"]
                elif logical_table == "concept_mentions":
                    keep = str(row.get("paper_id") or "") in selected_ids["papers"] or str(row.get("source_id") or "") in selected_ids["text_chunks"] | selected_ids["visual_chunks"]
                elif logical_table == "concepts":
                    keep = False
                else:
                    related = {
                        str(row.get("source_id") or ""),
                        str(row.get("target_id") or ""),
                        str(row.get("paper_id") or ""),
                    }
                    keep = bool(related & (selected_ids["papers"] | selected_ids["text_chunks"] | selected_ids["visual_chunks"] | selected_ids["visual_assets"]))
                if keep:
                    kept.append(row)
                else:
                    totals[logical_table] += 1
                    rejected[f"{logical_table}:dependency_not_scoped"] += 1
            if kept:
                selected[table] = kept
        # Count selected rows for tables that were not handled by the loops.
        selected_counts = {
            table.casefold(): len(rows) for table, rows in selected.items()
        }
        if self.scope_contract.discovery_mode == "generated_only":
            paper_decisions.sort(
                key=lambda item: (
                    -float(item.get("usefulness_score") or 0.0),
                    str(item.get("paper_id") or ""),
                )
            )
        else:
            paper_decisions.sort(
                key=lambda item: (
                    -float(item.get("score") or 0.0),
                    str(item.get("paper_id") or ""),
                )
            )
        if (
            self.scope_contract.discovery_mode == "generated_only"
            and selected.get(paper_table)
        ):
            selected[paper_table] = sorted(
                selected[paper_table],
                key=lambda row: (
                    -float(
                        self._paper_decisions.get(
                            str(row.get("paper_id") or ""), {}
                        ).get("usefulness_score")
                        or 0.0
                    ),
                    _canonical_identity(row),
                ),
            )
        contamination = _selection_contamination_indicators(
            paper_decisions,
            threshold=self.scope_contract.minimum_scope_score,
            minimum_core_anchor_hits=self.scope_contract.minimum_core_anchor_hits,
        )
        selected_samples = [
            _paper_decision_sample(item)
            for item in paper_decisions
            if item.get("accepted")
        ][:20]
        rejected_samples = [
            _paper_decision_sample(item)
            for item in paper_decisions
            if not item.get("accepted")
        ][:20]
        return selected, {
            "source_row_counts": dict(totals),
            "selected_row_counts": selected_counts,
            "rejected_row_counts": dict(rejected),
            "rejection_samples": {key: values[:10] for key, values in rejection_samples.items()},
            "selected_paper_ids": sorted(self._allowed_paper_ids),
            "papers": {
                "source_count": len(paper_decisions),
                "selected_count": sum(1 for item in paper_decisions if item.get("accepted")),
                "rejected_count": sum(1 for item in paper_decisions if not item.get("accepted")),
                "selected_paper_ids": sorted(
                    str(item.get("paper_id") or "")
                    for item in paper_decisions
                    if item.get("accepted") and str(item.get("paper_id") or "").strip()
                ),
                "rejected_paper_ids": sorted(
                    str(item.get("paper_id") or "")
                    for item in paper_decisions
                    if not item.get("accepted") and str(item.get("paper_id") or "").strip()
                ),
                "threshold": self.scope_contract.minimum_scope_score,
                "minimum_core_anchor_hits": self.scope_contract.minimum_core_anchor_hits,
                "paper_decisions": paper_decisions,
                "selected_paper_samples": selected_samples,
                "rejected_paper_samples": rejected_samples,
                "contamination_indicators": contamination,
            },
        }

    def create_overlay(self) -> dict[str, Any]:
        if not self.scope_contract.valid:
            raise TopicScopedKBError(
                "invalid topic scope contract: " + ", ".join(self.scope_contract.validation_errors)
            )
        if not self.base_kb_sqlite.is_file():
            raise TopicScopedKBError(f"base KB does not exist: {self.base_kb_sqlite}")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.runtime_kb, self.runtime_kb.with_name(self.runtime_kb.name + "-wal"), self.runtime_kb.with_name(self.runtime_kb.name + "-shm")):
            if path.exists():
                path.unlink()
        source = sqlite3.connect(f"file:{self.base_kb_sqlite.resolve()}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        target = sqlite3.connect(str(self.runtime_kb))
        target.execute("PRAGMA foreign_keys=OFF")
        target.execute("PRAGMA journal_mode=WAL")
        try:
            deferred_schema = _copy_schema(source, target)
            selected, report = self._select_base_rows(source)
            with target:
                for table, rows in selected.items():
                    actual = _table_names(target).get(table.casefold(), table)
                    if actual.casefold() not in _FTS_TABLES:
                        _copy_rows(target, actual, rows)
                _ensure_core_schema(target)
                for table, rows in selected.items():
                    logical = table.casefold()
                    if logical == "papers":
                        for row in rows:
                            identity = str(row.get("paper_id") or "").strip()
                            if identity:
                                fields = _permission_fields(
                                    row,
                                    table="papers",
                                    scope_fit=str(self._paper_decisions.get(identity, {}).get("scope_fit") or "direct"),
                                )
                                self._native_update(target, "papers", identity, fields)
                    elif logical == "text_chunks":
                        for row in rows:
                            identity = str(row.get("chunk_id") or "").strip()
                            if identity:
                                parent_id = str(row.get("paper_id") or "").strip()
                                parent_scope_fit = str(
                                    self._paper_decisions.get(parent_id, {}).get(
                                        "scope_fit"
                                    )
                                    or "unreviewed"
                                )
                                fields = _permission_fields(
                                    row,
                                    table="text_chunks",
                                    scope_fit=parent_scope_fit,
                                )
                                self._native_update(target, "text_chunks", identity, fields)
                _rebuild_fts(target)
                for sql in deferred_schema:
                    try:
                        target.execute(sql)
                    except sqlite3.DatabaseError:
                        # Legacy indexes/triggers are not evidence.  A broken
                        # optional object must not make the overlay broad or
                        # cause us to copy its data.
                        continue
            self.selection_report = report
        finally:
            target.close()
            source.close()
        return {
            "runtime_kb_sqlite": str(self.runtime_kb),
            "scope_contract": self.scope_contract.to_dict(),
            "selection": copy.deepcopy(self.selection_report),
        }

    def _incoming_paper(
        self,
        paper: S2PaperRecord,
        *,
        related_chunks: Iterable[UnifiedTextChunk] = (),
        explicit_current_run: bool | None = None,
    ) -> tuple[S2PaperRecord | None, dict[str, Any]]:
        if not paper.paper_id:
            return None, {"accepted": False, "reason": "missing_paper_id", "scope_fit": "unreviewed"}
        if self.policy.title_identity_required and not paper.title.strip():
            return None, {"accepted": False, "reason": "missing_title_identity", "scope_fit": "unreviewed"}
        if normalize_scope_fit(paper.scope_fit, default="unreviewed") == "out_of_scope":
            return None, {
                "accepted": False,
                "reason": "record_scope_fit_out_of_scope",
                "scope_fit": "out_of_scope",
            }
        mapping = paper.to_dict()
        mapping["raw_json"] = json.dumps(
            paper.raw_metadata or {}, ensure_ascii=False, separators=(",", ":")
        )
        current_run = (
            _is_explicit_current_run_paper(paper)
            if explicit_current_run is None
            else bool(explicit_current_run)
        )
        semantic_features = self._semantic_scores.get(paper.paper_id)
        match = _score_paper_scope(
            self.scope_contract,
            mapping,
            related_chunks=(chunk.to_dict() for chunk in related_chunks),
            explicit_current_run=current_run,
            semantic_features=semantic_features,
        )
        if not match["accepted"]:
            return None, match
        fields = _permission_fields(
            {
                "content_depth": paper.content_depth,
                "use_permission": paper.use_permission,
                "scope_fit": match["scope_fit"],
                "route_provenance": paper.route_events,
            },
            table="papers",
            scope_fit=str(match["scope_fit"]),
        )
        normalized = replace(
            paper,
            scope_fit=str(match["scope_fit"]),
            content_depth=str(fields["content_depth"]),
            use_permission=str(fields["use_permission"]),
            route_events=[
                *list(paper.route_events),
                {
                    "event": "topic_scope_accepted",
                    "scope_fit": match["scope_fit"],
                    "reason": match.get("reason"),
                    "explicit_current_run": bool(match.get("explicit_current_run")),
                },
            ],
        )
        return normalized, {**match, "identity": paper.paper_id, "permission": fields}

    def accepts_s2_paper(
        self,
        paper: S2PaperRecord,
        *,
        related_chunks: Iterable[UnifiedTextChunk] = (),
    ) -> bool:
        """Return whether one incoming S2 paper passes identity and scope gates."""

        normalized, _ = self._incoming_paper(
            paper,
            related_chunks=related_chunks,
        )
        return normalized is not None

    def evaluate_s2_paper(
        self,
        paper: S2PaperRecord,
        *,
        related_chunks: Iterable[UnifiedTextChunk] = (),
    ) -> dict[str, Any]:
        """Side-effect-safe candidate evaluation returning the full decision.

        Unlike ``accepts_s2_paper``, this returns the complete audit decision
        (including the generated-only ``usefulness_score``) without mutating
        stage state.  It is used by the bootstrap to rank supplementary
        candidates before any per-route caps consume papers.
        """

        _, decision = self._incoming_paper(
            paper,
            related_chunks=related_chunks,
        )
        return dict(decision)

    def _incoming_chunk(
        self,
        chunk: UnifiedTextChunk,
        allowed_paper_ids: set[str],
    ) -> tuple[UnifiedTextChunk | None, dict[str, Any]]:
        if not chunk.chunk_id:
            return None, {"accepted": False, "reason": "missing_chunk_id", "scope_fit": "unreviewed"}
        if not chunk.paper_id or chunk.paper_id not in allowed_paper_ids:
            return None, {"accepted": False, "reason": "chunk_parent_not_scoped", "scope_fit": "unreviewed"}
        parent = self._paper_decisions.get(chunk.paper_id, {})
        match = {
            "accepted": True,
            "scope_fit": str(parent.get("scope_fit") or "direct"),
            "reason": "inherited_from_selected_paper",
            "identity": chunk.chunk_id,
            "paper_id": chunk.paper_id,
            "exclusion_hits": [],
            "explicit_current_run": bool(parent.get("explicit_current_run")),
        }
        fields = _permission_fields(
            {
                "content_depth": chunk.content_depth,
                "use_permission": chunk.use_permission,
                "context_complete": chunk.context_complete,
                "allowed_claim_kinds": chunk.allowed_claim_kinds,
                "route_provenance": chunk.route_provenance,
            },
            table="text_chunks",
            scope_fit=str(match["scope_fit"]),
        )
        normalized = replace(
            chunk,
            scope_fit=str(match["scope_fit"]),
            content_depth=str(fields["content_depth"]),
            use_permission=str(fields["use_permission"]),
            allowed_claim_kinds=list(fields["allowed_claim_kinds"]),
            route_provenance={
                **dict(chunk.route_provenance),
                "topic_scope": {
                    "scope_fit": match["scope_fit"],
                    "contract_sha256": self.scope_contract.contract_sha256,
                },
            },
        )
        return normalized, {**match, "identity": chunk.chunk_id, "permission": fields}

    def ingest_s2(
        self,
        *,
        papers: Iterable[S2PaperRecord] = (),
        chunks: Iterable[UnifiedTextChunk] = (),
        graph: LiteratureGraph | None = None,
    ) -> dict[str, Any]:
        paper_map: dict[str, S2PaperRecord] = {}
        rejected: Counter[str] = Counter()
        paper_list = _deduplicate_input_records(
            list(papers), identity_field="paper_id", label="paper"
        )
        chunk_list = _deduplicate_input_records(
            list(chunks), identity_field="chunk_id", label="chunk"
        )
        self._incoming_papers = list(paper_list)
        self._incoming_chunks = list(chunk_list)
        self._incoming_graph = graph
        chunk_list, identity_rebindings = _rebind_s2_chunk_parents(
            paper_list,
            chunk_list,
        )
        incoming_decisions: list[dict[str, Any]] = []
        chunks_by_paper: dict[str, list[UnifiedTextChunk]] = {}
        for chunk in chunk_list:
            if chunk.paper_id:
                chunks_by_paper.setdefault(chunk.paper_id, []).append(chunk)
        for paper in paper_list:
            normalized, decision = self._incoming_paper(
                paper,
                related_chunks=chunks_by_paper.get(paper.paper_id, ()),
            )
            incoming_decisions.append(dict(decision))
            if normalized is None:
                rejected[f"papers:{decision.get('reason', 'rejected')}"] += 1
                continue
            paper_map[normalized.paper_id] = normalized
            self._allowed_paper_ids.add(normalized.paper_id)
            self._paper_decisions[normalized.paper_id] = decision
        for paper_id in list(self._allowed_paper_ids):
            self._paper_decisions.setdefault(paper_id, {"scope_fit": "direct"})
        accepted_chunks: list[UnifiedTextChunk] = []
        for chunk in chunk_list:
            normalized, decision = self._incoming_chunk(chunk, self._allowed_paper_ids)
            if normalized is None:
                rejected[f"text_chunks:{decision.get('reason', 'rejected')}"] += 1
                continue
            accepted_chunks.append(normalized)

        filtered_graph: LiteratureGraph | None = None
        graph_rejected = 0
        if graph is not None:
            graph_nodes: dict[str, S2PaperRecord] = {}
            annotations: dict[str, dict[str, Any]] = {}
            for paper_id, paper in graph.nodes.items():
                normalized, decision = self._incoming_paper(
                    paper,
                    related_chunks=chunks_by_paper.get(paper.paper_id, ()),
                )
                incoming_decisions.append(dict(decision))
                if normalized is None:
                    graph_rejected += 1
                    continue
                graph_nodes[paper_id] = normalized
                paper_map[paper_id] = normalized
                self._allowed_paper_ids.add(paper_id)
                self._paper_decisions[paper_id] = decision
                annotations[paper_id] = dict(graph.node_annotations.get(paper_id) or {})
                annotations[paper_id]["topic_scope_fit"] = decision.get("scope_fit")
            edges = [
                edge
                for edge in graph.edges
                if edge.source_paper_id in graph_nodes and edge.target_paper_id in graph_nodes
            ]
            graph_rejected += len(graph.edges) - len(edges)
            filtered_graph = LiteratureGraph(
                nodes=graph_nodes,
                node_annotations=annotations,
                edges=edges,
                excluded_candidates=list(graph.excluded_candidates),
                query_runs=list(graph.query_runs),
            )
        self.filtered_graph = filtered_graph

        if self.scope_contract.discovery_mode == "generated_only":
            paper_map = dict(
                sorted(
                    paper_map.items(),
                    key=lambda item: (
                        -float(
                            self._paper_decisions.get(item[0], {}).get(
                                "usefulness_score"
                            )
                            or 0.0
                        ),
                        item[0],
                    ),
                )
            )
            incoming_decisions.sort(
                key=lambda item: (
                    -float(item.get("usefulness_score") or 0.0),
                    str(item.get("paper_id") or ""),
                )
            )
        if not self.runtime_kb.exists():
            raise TopicScopedKBError("create_overlay must run before ingest_s2")
        bridge = S2KnowledgeBaseBridge(self.runtime_kb)
        ingest = bridge.ingest(papers=paper_map.values(), chunks=accepted_chunks)
        graph_ingest: dict[str, Any] = {}
        if filtered_graph is not None:
            graph_ingest = bridge.ingest_graph(filtered_graph)
        self.ingest_report = {
            "papers_accepted": len(paper_map),
            "chunks_accepted": len(accepted_chunks),
            "rejected": dict(rejected),
            "paper_decisions": incoming_decisions,
            "paper_decision_samples": [
                _paper_decision_sample(item) for item in incoming_decisions
            ][:50],
            "identity_rebindings": identity_rebindings,
            "graph_nodes_accepted": len(filtered_graph.nodes) if filtered_graph else 0,
            "graph_edges_accepted": len(filtered_graph.edges) if filtered_graph else 0,
            "graph_summary": filtered_graph.summary() if filtered_graph else {},
            "graph_records_rejected": graph_rejected,
            "kb_ingest": ingest,
            "graph_ingest": graph_ingest,
        }
        return copy.deepcopy(self.ingest_report)

    def ingest_s2_supplement(
        self,
        *,
        papers: Iterable[S2PaperRecord],
        chunks: Iterable[UnifiedTextChunk],
        label: str,
    ) -> dict[str, Any]:
        """Append a later material wave through the same scope and identity gate.

        The bootstrap uses this only after the primary S2-body wave, for
        example when an identified paper reaches the final abstract-claim
        fallback.  It deliberately does not rebuild or overwrite the graph.
        """

        paper_list = _deduplicate_input_records(
            list(papers), identity_field="paper_id", label="paper"
        )
        chunk_list = _deduplicate_input_records(
            list(chunks), identity_field="chunk_id", label="chunk"
        )
        chunk_list, identity_rebindings = _rebind_s2_chunk_parents(
            paper_list, chunk_list
        )
        chunks_by_paper: dict[str, list[UnifiedTextChunk]] = {}
        for chunk in chunk_list:
            if chunk.paper_id:
                chunks_by_paper.setdefault(chunk.paper_id, []).append(chunk)

        normalized_papers: dict[str, S2PaperRecord] = {}
        rejected: Counter[str] = Counter()
        paper_decisions: list[dict[str, Any]] = []
        prior_papers = {
            paper.paper_id: paper
            for paper in self._incoming_papers
            if paper.paper_id in self._allowed_paper_ids
        }
        for paper in paper_list:
            prior = prior_papers.get(paper.paper_id)
            prior_decision = self._paper_decisions.get(paper.paper_id)
            if prior is not None and prior_decision:
                if not _supplement_identity_matches(prior, paper):
                    normalized = None
                    decision = {
                        "accepted": False,
                        "reason": "supplement_identity_conflict",
                        "scope_fit": "unreviewed",
                        "identity": paper.paper_id,
                    }
                else:
                    scope_fit = str(prior_decision.get("scope_fit") or "direct")
                    fields = _permission_fields(
                        {
                            "content_depth": paper.content_depth,
                            "use_permission": paper.use_permission,
                            "scope_fit": scope_fit,
                            "route_provenance": paper.route_events,
                        },
                        table="papers",
                        scope_fit=scope_fit,
                    )
                    normalized = replace(
                        paper,
                        scope_fit=scope_fit,
                        content_depth=str(fields["content_depth"]),
                        use_permission=str(fields["use_permission"]),
                        route_events=[
                            *list(paper.route_events),
                            {
                                "event": "topic_scope_decision_reused_for_verified_supplement",
                                "scope_fit": scope_fit,
                                "reason": str(prior_decision.get("reason") or "prior_acceptance"),
                            },
                        ],
                    )
                    decision = {
                        **dict(prior_decision),
                        "accepted": True,
                        "reason": "prior_scope_acceptance_reused_for_verified_supplement",
                        "identity": paper.paper_id,
                        "permission": fields,
                    }
            else:
                normalized, decision = self._incoming_paper(
                    paper,
                    related_chunks=chunks_by_paper.get(paper.paper_id, ()),
                )
            paper_decisions.append(dict(decision))
            if normalized is None:
                rejected[f"papers:{decision.get('reason', 'rejected')}"] += 1
                continue
            normalized_papers[normalized.paper_id] = normalized
            self._allowed_paper_ids.add(normalized.paper_id)
            self._paper_decisions[normalized.paper_id] = decision

        normalized_chunks: list[UnifiedTextChunk] = []
        for chunk in chunk_list:
            normalized, decision = self._incoming_chunk(
                chunk, self._allowed_paper_ids
            )
            if normalized is None:
                rejected[f"text_chunks:{decision.get('reason', 'rejected')}"] += 1
                continue
            normalized_chunks.append(normalized)

        bridge_result = S2KnowledgeBaseBridge(self.runtime_kb).ingest(
            papers=normalized_papers.values(),
            chunks=normalized_chunks,
        )
        self._incoming_papers.extend(normalized_papers.values())
        self._incoming_chunks.extend(normalized_chunks)
        supplement = {
            "label": str(label or "supplement"),
            "papers_accepted": len(normalized_papers),
            "chunks_accepted": len(normalized_chunks),
            "rejected": dict(rejected),
            "paper_decisions": paper_decisions,
            "identity_rebindings": identity_rebindings,
            "kb_ingest": bridge_result,
        }
        self.ingest_report.setdefault("supplements", []).append(supplement)
        return copy.deepcopy(supplement)

    def _sanitize_permissions(self, conn: sqlite3.Connection) -> None:
        names = _table_names(conn)
        for logical_table in ("papers", "text_chunks"):
            table = names.get(logical_table)
            if not table:
                continue
            conn.row_factory = sqlite3.Row
            for row in conn.execute(f"SELECT * FROM {_quote_identifier(table)}").fetchall():
                mapping = _row_dict(row)
                identity = str(mapping.get("paper_id" if logical_table == "papers" else "chunk_id") or "")
                if not identity:
                    continue
                scope_fit = normalize_scope_fit(mapping.get("scope_fit"), default="unreviewed")
                fields = _permission_fields(mapping, table=logical_table, scope_fit=scope_fit)
                self._native_update(conn, table, identity, fields)

    def _prune_overlay_to_contract(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Re-apply identity/scope rules to direct full-text fallback writes."""

        names = _table_names(conn)
        removed_papers: list[str] = []
        allowed_papers: set[str] = set()
        paper_decisions: list[dict[str, Any]] = []
        paper_table = names.get("papers")
        chunk_table = names.get("text_chunks")
        conn.row_factory = sqlite3.Row
        chunk_rows = (
            [_row_dict(row) for row in conn.execute(f"SELECT * FROM {_quote_identifier(chunk_table)}").fetchall()]
            if chunk_table
            else []
        )
        chunks_by_paper: dict[str, list[dict[str, Any]]] = {}
        for chunk in chunk_rows:
            paper_id = str(chunk.get("paper_id") or "").strip()
            if paper_id:
                chunks_by_paper.setdefault(paper_id, []).append(chunk)
        if paper_table:
            paper_rows = conn.execute(
                f"SELECT * FROM {_quote_identifier(paper_table)}"
            ).fetchall()
            for row in paper_rows:
                mapping = _row_dict(row)
                paper_id = str(mapping.get("paper_id") or "").strip()
                known_decision = self._paper_decisions.get(paper_id, {})
                if paper_id in self._allowed_paper_ids and known_decision.get("accepted", True):
                    decision = {
                        **known_decision,
                        "accepted": True,
                        "scope_fit": str(known_decision.get("scope_fit") or "direct"),
                        "reason": str(known_decision.get("reason") or "preserved_selected_paper"),
                    }
                else:
                    decision = self._scope_decision_for_mapping(
                        mapping,
                        table="papers",
                        related_chunks=chunks_by_paper.get(paper_id, ()),
                    )
                paper_decisions.append(dict(decision))
                if paper_id and decision.get("accepted"):
                    allowed_papers.add(paper_id)
                    self._allowed_paper_ids.add(paper_id)
                    self._paper_decisions[paper_id] = decision
                    fields = _permission_fields(
                        mapping,
                        table="papers",
                        scope_fit=str(decision.get("scope_fit") or "direct"),
                    )
                    self._native_update(conn, paper_table, paper_id, fields)
                else:
                    if paper_id:
                        removed_papers.append(paper_id)
                    if paper_id:
                        conn.execute(
                            f"DELETE FROM {_quote_identifier(paper_table)} WHERE paper_id=?",
                            (paper_id,),
                        )

        removed_chunks: list[str] = []
        if chunk_table:
            for mapping in chunk_rows:
                chunk_id = str(mapping.get("chunk_id") or "").strip()
                paper_id = str(mapping.get("paper_id") or "").strip()
                parent_scope_fit = str(
                    self._paper_decisions.get(paper_id, {}).get("scope_fit")
                    or "unreviewed"
                )
                decision = {
                    "accepted": bool(paper_id in allowed_papers and chunk_id),
                    "scope_fit": parent_scope_fit,
                    "reason": "inherited_from_selected_paper",
                }
                if chunk_id and decision.get("accepted"):
                    fields = _permission_fields(
                        mapping,
                        table="text_chunks",
                        scope_fit=str(decision.get("scope_fit") or "direct"),
                    )
                    self._native_update(conn, chunk_table, chunk_id, fields)
                else:
                    if chunk_id:
                        removed_chunks.append(chunk_id)
                        conn.execute(
                            f"DELETE FROM {_quote_identifier(chunk_table)} WHERE chunk_id=?",
                            (chunk_id,),
                        )

        removed_visual_ids: list[str] = []
        for logical_table in ("visual_assets", "visual_chunks"):
            table = names.get(logical_table)
            if not table:
                continue
            columns = set(_table_columns(conn, table))
            identity_column = "asset_id" if logical_table == "visual_assets" else "chunk_id"
            if identity_column not in columns or "paper_id" not in columns:
                continue
            for row in conn.execute(f"SELECT * FROM {_quote_identifier(table)}").fetchall():
                visual_id = str(row[identity_column] or "").strip()
                paper_id = str(row["paper_id"] or "").strip()
                if visual_id and paper_id in allowed_papers:
                    continue
                if visual_id:
                    removed_visual_ids.append(visual_id)
                if identity_column in columns:
                    conn.execute(
                        f"DELETE FROM {_quote_identifier(table)} WHERE {_quote_identifier(identity_column)}=?",
                        (visual_id,),
                    )

        # Keep relationship/graph tables closed over the surviving paper and
        # chunk identities.  Unknown tables remain empty by construction.
        selected_ids = allowed_papers | {
            str(row[0])
            for row in conn.execute(
                f"SELECT chunk_id FROM {_quote_identifier(chunk_table)}"
            ).fetchall()
        } if chunk_table else set(allowed_papers)
        for logical_table in (
            "paper_citations",
            "s2_literature_graph_nodes",
            "s2_literature_graph_edges",
        ):
            table = names.get(logical_table)
            if not table:
                continue
            columns = set(_table_columns(conn, table))
            if logical_table == "paper_citations" and not {"citing_paper_id", "cited_paper_id"} <= columns:
                continue
            if logical_table == "s2_literature_graph_nodes" and "paper_id" not in columns:
                continue
            if logical_table == "s2_literature_graph_edges" and not {"source_paper_id", "target_paper_id"} <= columns:
                continue
            if logical_table == "s2_literature_graph_nodes":
                if allowed_papers:
                    placeholders = ",".join("?" for _ in allowed_papers)
                    conn.execute(
                        f"DELETE FROM {_quote_identifier(table)} WHERE paper_id NOT IN ({placeholders})",
                        tuple(sorted(allowed_papers)),
                    )
                else:
                    conn.execute(f"DELETE FROM {_quote_identifier(table)}")
            elif logical_table == "paper_citations":
                if allowed_papers:
                    placeholders = ",".join("?" for _ in allowed_papers)
                    conn.execute(
                        f"DELETE FROM {_quote_identifier(table)} WHERE citing_paper_id NOT IN ({placeholders}) OR cited_paper_id NOT IN ({placeholders})",
                        tuple(sorted(allowed_papers)) * 2,
                    )
                else:
                    conn.execute(f"DELETE FROM {_quote_identifier(table)}")
            else:
                if allowed_papers:
                    placeholders = ",".join("?" for _ in allowed_papers)
                    conn.execute(
                        f"DELETE FROM {_quote_identifier(table)} WHERE source_paper_id NOT IN ({placeholders}) OR target_paper_id NOT IN ({placeholders})",
                        tuple(sorted(allowed_papers)) * 2,
                    )
                else:
                    conn.execute(f"DELETE FROM {_quote_identifier(table)}")
        _rebuild_fts(conn)
        return {
            "removed_paper_ids": removed_papers,
            "removed_chunk_ids": removed_chunks,
            "removed_visual_ids": removed_visual_ids,
            "remaining_paper_count": len(allowed_papers),
            "remaining_identity_count": len(selected_ids),
            "paper_decisions": paper_decisions,
            "contamination_indicators": _selection_contamination_indicators(
                paper_decisions,
                threshold=self.scope_contract.minimum_scope_score,
                minimum_core_anchor_hits=self.scope_contract.minimum_core_anchor_hits,
            ),
        }

    def _audit(self) -> dict[str, Any]:
        conn = sqlite3.connect(str(self.runtime_kb))
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                self.final_filter_report = self._prune_overlay_to_contract(conn)
                self._sanitize_permissions(conn)
                _rebuild_fts(conn)
            names = _table_names(conn)
            table_counts: dict[str, int] = {}
            permission_counts: Counter[str] = Counter()
            content_depth_counts: Counter[str] = Counter()
            scope_fit_counts: Counter[str] = Counter()
            route_counts: Counter[str] = Counter()
            evidence_rows: list[dict[str, Any]] = []
            text_paper_ids: set[str] = set()
            factual_paper_ids: set[str] = set()
            qualified_paper_ids: set[str] = set()
            for logical_table in ("papers", "text_chunks"):
                table = names.get(logical_table)
                if not table:
                    table_counts[logical_table] = 0
                    continue
                rows = conn.execute(f"SELECT * FROM {_quote_identifier(table)}").fetchall()
                table_counts[logical_table] = len(rows)
                for row in rows:
                    mapping = _row_dict(row)
                    scope_fit = normalize_scope_fit(mapping.get("scope_fit"), default="unreviewed")
                    fields = _permission_fields(mapping, table=logical_table, scope_fit=scope_fit)
                    permission_counts[str(fields["use_permission"])] += 1
                    content_depth_counts[str(fields["content_depth"])] += 1
                    scope_fit_counts[str(fields["scope_fit"])] += 1
                    route_counts[str(fields["discovery_route"])] += 1
                    if logical_table == "text_chunks":
                        paper_id = str(mapping.get("paper_id") or "")
                        if paper_id:
                            text_paper_ids.add(paper_id)
                        requires_local_fulltext = self.policy.feature_enabled(
                            "require_local_fulltext_before_using_s2_chunk",
                            default=False,
                        )
                        evidence_eligible = _is_evidence_permission(fields) and not (
                            requires_local_fulltext
                            and fields.get("content_depth") == "structured_snippet"
                        )
                        factual_support = evidence_eligible and _is_factual_permission(fields)
                        if evidence_eligible and paper_id:
                            qualified_paper_ids.add(paper_id)
                        if factual_support and paper_id:
                            factual_paper_ids.add(paper_id)
                        evidence_rows.append(
                            {
                                "chunk_id": str(mapping.get("chunk_id") or ""),
                                "paper_id": paper_id,
                                "use_permission": fields["use_permission"],
                                "content_depth": fields["content_depth"],
                                "scope_fit": fields["scope_fit"],
                                "evidence_eligible": evidence_eligible,
                                "factual_support": factual_support,
                            }
                        )
            for logical_table in _CORE_TABLES | _FTS_TABLES:
                if logical_table not in table_counts:
                    actual = names.get(logical_table)
                    if actual:
                        table_counts[logical_table] = int(
                            conn.execute(f"SELECT COUNT(*) FROM {_quote_identifier(actual)}").fetchone()[0]
                        )
            factual_chunks = sum(1 for row in evidence_rows if row["factual_support"])
            qualified_chunks = sum(
                1 for row in evidence_rows if row["evidence_eligible"] and not row["factual_support"]
            )
            evidence_chunks = factual_chunks + qualified_chunks
            minimum_reached = (
                len(factual_paper_ids) >= self.policy.minimum_factual_papers
                and factual_chunks >= self.policy.minimum_factual_chunks
            )
            target_reached = len(factual_paper_ids) >= self.policy.minimum_target_papers
            if evidence_chunks == 0:
                status = "needs_more_literature"
            elif minimum_reached and target_reached:
                status = "completed"
            else:
                # Some qualified/contextual evidence is still useful and is
                # explicitly reported as partial; only a genuinely empty
                # evidence pool receives needs_more_literature.
                status = "partial"
            surviving_paper_ids = sorted(
                str(row[0])
                for row in (
                    conn.execute(
                        f"SELECT paper_id FROM {_quote_identifier(names['papers'])}"
                    ).fetchall()
                    if names.get("papers")
                    else []
                )
                if str(row[0] or "").strip()
            )
            base_paper_scope = dict(self.selection_report.get("papers") or {})
            paper_scope = {
                "source_selection": base_paper_scope,
                "surviving_paper_count": len(surviving_paper_ids),
                "surviving_paper_ids": surviving_paper_ids,
                "selected_paper_count": len(surviving_paper_ids),
                "selected_paper_ids": surviving_paper_ids,
                "rejected_paper_ids": list(
                    base_paper_scope.get("rejected_paper_ids") or []
                ),
                "threshold": self.scope_contract.minimum_scope_score,
                "minimum_core_anchor_hits": self.scope_contract.minimum_core_anchor_hits,
                "final_filter": copy.deepcopy(self.final_filter_report),
                "contamination_indicators": (
                    base_paper_scope.get("contamination_indicators")
                    or self.final_filter_report.get("contamination_indicators")
                    or _selection_contamination_indicators(
                        [],
                        threshold=self.scope_contract.minimum_scope_score,
                        minimum_core_anchor_hits=self.scope_contract.minimum_core_anchor_hits,
                    )
                ),
                "representative_selected_papers": list(
                    base_paper_scope.get("selected_paper_samples") or []
                )[:20],
                "representative_rejected_papers": list(
                    base_paper_scope.get("rejected_paper_samples") or []
                )[:20],
            }
            return {
                "tables": table_counts,
                "permission_counts": dict(sorted(permission_counts.items())),
                "content_depth_counts": dict(sorted(content_depth_counts.items())),
                "scope_fit_counts": dict(sorted(scope_fit_counts.items())),
                "discovery_route_counts": dict(sorted(route_counts.items())),
                "evidence": {
                    "text_chunk_count": len(evidence_rows),
                    "evidence_eligible_chunk_count": evidence_chunks,
                    "factual_support_chunk_count": factual_chunks,
                    "qualified_support_chunk_count": qualified_chunks,
                    "evidence_paper_count": len(qualified_paper_ids),
                    "factual_support_paper_count": len(factual_paper_ids),
                    "factual_paper_ids": sorted(factual_paper_ids),
                    "qualified_paper_ids": sorted(qualified_paper_ids),
                    "text_paper_count": len(text_paper_ids),
                    "minimum_factual_papers": self.policy.minimum_factual_papers,
                    "minimum_factual_chunks": self.policy.minimum_factual_chunks,
                    "target_papers": self.policy.minimum_target_papers,
                    "minimum_reached": minimum_reached,
                    "target_reached": target_reached,
                    "rows": evidence_rows,
                },
                "paper_scope": paper_scope,
                "status": status,
            }
        finally:
            conn.close()

    def finalize(
        self,
        *,
        query_telemetry: Mapping[str, Any] | None = None,
        extra_manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.runtime_kb.exists():
            raise TopicScopedKBError("cannot finalize a missing overlay")
        audit = self._audit()
        telemetry = dict(query_telemetry or build_s2_query_telemetry())
        telemetry.setdefault("schema_version", TELEMETRY_SCHEMA_VERSION)
        _immutable_json_write(self.telemetry_path, telemetry)
        try:
            query_plan = json.loads(self.query_plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TopicScopedKBError("query plan changed or became unreadable") from exc
        reuse_contract = _reuse_contract(
            query_plan=query_plan,
            base_kb_sqlite=self.base_kb_sqlite,
            policy=self.policy,
            scope_contract=self.scope_contract,
            papers=self._incoming_papers,
            chunks=self._incoming_chunks,
            graph=self._incoming_graph,
            query_telemetry=telemetry,
            extra_manifest=extra_manifest,
        )
        conn = sqlite3.connect(str(self.runtime_kb))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass
        finally:
            conn.close()
        manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "created_at": _utc_now(),
            "status": audit["status"],
            "source_base_kb_sqlite": str(self.base_kb_sqlite),
            "source_base_kb_sha256": _sha256_file(self.base_kb_sqlite),
            "query_plan_path": str(self.query_plan_path),
            "query_plan_sha256": _sha256_file(self.query_plan_path),
            "policy_path": self.policy.config_path,
            "policy_sha256": self.policy.config_sha256,
            "policy": self.policy.to_dict(),
            "runtime_kb_sqlite": str(self.runtime_kb),
            "runtime_kb_sha256": _sha256_file(self.runtime_kb),
            "scope_decision_rule_version": SCOPE_DECISION_RULE_VERSION,
            "reuse_contract": reuse_contract,
            "scope_contract": self.scope_contract.to_dict(),
            "selection": copy.deepcopy(self.selection_report),
            "ingest": copy.deepcopy(self.ingest_report),
            "final_filter": copy.deepcopy(self.final_filter_report),
            "audit": {
                "paper_scope": copy.deepcopy(audit["paper_scope"]),
                "threshold": self.scope_contract.minimum_scope_score,
                "minimum_core_anchor_hits": self.scope_contract.minimum_core_anchor_hits,
                "contamination_indicators": copy.deepcopy(
                    audit["paper_scope"].get("contamination_indicators") or {}
                ),
                "representative_samples": {
                    "selected_papers": list(
                        audit["paper_scope"].get("representative_selected_papers") or []
                    )[:20],
                    "rejected_papers": list(
                        audit["paper_scope"].get("representative_rejected_papers") or []
                    )[:20],
                    "evidence_chunks": list(audit["evidence"].get("rows") or [])[:20],
                },
            },
            "provenance_counts": {
                "permission": audit["permission_counts"],
                "content_depth": audit["content_depth_counts"],
                "scope_fit": audit["scope_fit_counts"],
                "discovery_route": audit["discovery_route_counts"],
            },
            "evidence": {
                key: value
                for key, value in audit["evidence"].items()
                if key != "rows"
            },
            "evidence_sample": list(audit["evidence"].get("rows") or [])[:50],
            "table_counts": audit["tables"],
            "s2_query_telemetry": telemetry,
            "telemetry_sha256": _sha256_file(self.telemetry_path),
        }
        if extra_manifest:
            manifest["integration"] = copy.deepcopy(dict(extra_manifest))
        manifest_hash = _sha256_bytes(_canonical_json(manifest).encode("utf-8"))
        manifest["manifest_sha256"] = manifest_hash
        saved_manifest = _immutable_json_write(self.manifest_path, manifest)
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "status": audit["status"],
            "runtime_kb_sqlite": str(self.runtime_kb),
            "manifest_path": str(self.manifest_path),
            "telemetry_path": str(self.telemetry_path),
            "manifest_sha256": str(saved_manifest.get("manifest_sha256") or manifest_hash),
            "selection": copy.deepcopy(self.selection_report),
            "ingest": copy.deepcopy(self.ingest_report),
            "final_filter": copy.deepcopy(self.final_filter_report),
            "audit": copy.deepcopy(audit),
            "provenance_counts": copy.deepcopy(manifest["provenance_counts"]),
            "evidence": copy.deepcopy(audit["evidence"]),
            "table_counts": copy.deepcopy(audit["tables"]),
            "s2_query_telemetry": telemetry,
            "manifest": saved_manifest,
        }


def build_topic_scoped_kb(
    *,
    query_plan_path: str | Path,
    base_kb_sqlite: str | Path,
    work_dir: str | Path,
    policy_path: str | Path | None = None,
    papers: Iterable[S2PaperRecord] = (),
    chunks: Iterable[UnifiedTextChunk] = (),
    graph: LiteratureGraph | None = None,
    query_telemetry: Mapping[str, Any] | None = None,
    extra_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a scoped overlay and return its auditable status/report."""

    query_path = Path(query_plan_path)
    try:
        query_plan = json.loads(query_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "status": "failed",
            "error_code": "invalid_query_plan",
            "error": str(exc),
        }
    try:
        policy = load_s2_policy(policy_path)
        contract = derive_topic_scope_contract(query_plan)
        paper_list = _deduplicate_input_records(
            list(papers), identity_field="paper_id", label="paper"
        )
        chunk_list = _deduplicate_input_records(
            list(chunks), identity_field="chunk_id", label="chunk"
        )
        telemetry = dict(query_telemetry or build_s2_query_telemetry())
        telemetry.setdefault("schema_version", TELEMETRY_SCHEMA_VERSION)
        base_path = Path(base_kb_sqlite)
        expected_reuse_contract = _reuse_contract(
            query_plan=query_plan,
            base_kb_sqlite=base_path,
            policy=policy,
            scope_contract=contract,
            papers=paper_list,
            chunks=chunk_list,
            graph=graph,
            query_telemetry=telemetry,
            extra_manifest=extra_manifest,
        )
        stage = TopicScopedKBStage(
            query_plan_path=query_path,
            base_kb_sqlite=base_path,
            work_dir=work_dir,
            policy=policy,
            scope_contract=contract,
        )
        work_path = Path(work_dir)
        manifest_path = work_path / "KB_MANIFEST.json"
        runtime_path = work_path / "review_knowledge_base.s2.sqlite"
        telemetry_path = work_path / "S2_QUERY_TELEMETRY.json"
        reserved_paths = (manifest_path, runtime_path, telemetry_path)
        occupied = [path for path in reserved_paths if path.exists()]
        if occupied:
            if len(occupied) != len(reserved_paths):
                raise TopicScopedKBError(
                    "occupied scoped-KB work_dir has an incomplete immutable artifact set"
                )
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("schema_version") != MANIFEST_SCHEMA_VERSION:
                # Contract mismatch — relocate stale artifacts so the caller
                # can rebuild without hard-failing the whole harness.
                import time as _time
                _stale = _relocate_stale_kb_artifacts(
                    work_path, reserved_paths, _time.time()
                )
                return _isolated_kb_rebuild_report(
                    stale_dir=_stale,
                    reason="existing KB_MANIFEST has an incompatible schema",
                )
            if not _manifest_hash_is_valid(existing):
                raise TopicScopedKBError("existing KB_MANIFEST failed its integrity hash")
            if existing.get("scope_decision_rule_version") != SCOPE_DECISION_RULE_VERSION:
                import time as _time
                _stale = _relocate_stale_kb_artifacts(
                    work_path, reserved_paths, _time.time()
                )
                return _isolated_kb_rebuild_report(
                    stale_dir=_stale,
                    reason="existing KB_MANIFEST uses a stale scope-decision rule",
                )
            if not _reuse_contract_matches(
                existing.get("reuse_contract"), expected_reuse_contract
            ):
                import time as _time
                _stale = _relocate_stale_kb_artifacts(
                    work_path, reserved_paths, _time.time()
                )
                return _isolated_kb_rebuild_report(
                    stale_dir=_stale,
                    reason="existing KB_MANIFEST does not match the current build request",
                )
            if str(existing.get("runtime_kb_sha256") or "") != _sha256_file(
                runtime_path
            ):
                raise TopicScopedKBError(
                    "existing run-local overlay failed its integrity hash"
                )
            if str(existing.get("telemetry_sha256") or "") != _sha256_file(
                telemetry_path
            ):
                raise TopicScopedKBError(
                    "existing S2 query telemetry failed its integrity hash"
                )
            return {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "status": existing.get("status") or "failed",
                "runtime_kb_sqlite": str(runtime_path),
                "manifest_path": str(manifest_path),
                "telemetry_path": str(telemetry_path),
                "manifest_sha256": existing.get("manifest_sha256", ""),
                "manifest": existing,
                "reused": True,
            }
        stage.create_overlay()
        stage.ingest_s2(papers=paper_list, chunks=chunk_list, graph=graph)
        result = stage.finalize(
            query_telemetry=telemetry,
            extra_manifest=extra_manifest,
        )
        result["reused"] = False
        return result
    except Exception as exc:
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "status": "failed",
            "error_code": "topic_scoped_kb_stage_failed",
            "error": str(exc),
            "reused": False,
        }


__all__ = [
    "GRAPH_QUERY_CATEGORIES",
    "MANIFEST_SCHEMA_VERSION",
    "QUERY_CATEGORIES",
    "REUSE_CONTRACT_SCHEMA_VERSION",
    "SCOPE_DECISION_RULE_VERSION",
    "TELEMETRY_SCHEMA_VERSION",
    "TopicScopeContract",
    "TopicScopedKBError",
    "TopicScopedKBStage",
    "build_s2_query_telemetry",
    "build_topic_scope_contract",
    "build_topic_scoped_kb",
    "derive_topic_scope_contract",
]
