"""Shared schemas for the Semantic Scholar first literature pipeline.

The module deliberately separates *what a resource contains* from *where it
came from*.  A validated S2 body snippet and a locally parsed paragraph are
both text chunks.  Provenance remains explicit for traceability, but is not a
quality penalty by itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ftfy import fix_text

from optomind_research.runtime.review_quality_contract import (
    CANONICAL_OBSERVED_RELATIONS,
    CANONICAL_RELATION_STATUSES,
    CANONICAL_SEMANTIC_RELATIONS,
    normalize_content_depth,
    normalize_scope_fit,
    normalize_use_permission,
    permission_for_content,
    route_for_content_depth,
)


ContentKind = Literal[
    "metadata",
    "abstract",
    "tldr",
    "text_chunk",
    "fulltext_document",
    "visual_asset",
    "paper_card",
]

TextProvenance = Literal[
    "s2_body_snippet",
    "s2_abstract_snippet",
    "local_publisher_html",
    "local_jats_xml",
    "local_pdf_parse",
    "other_verified_source",
]

CitationRole = Literal[
    "direct_support",
    "partial_support",
    "background_context",
    "historical_origin",
    "mechanism_neighbor",
    "method_example",
    "comparative_example",
    "frontier_progress",
    "controversy_or_boundary",
    "application_example",
    "review_pointer",
]

GraphEdgeType = Literal[
    "cites",
    "cited_by",
    "snippet_ref_mention",
    "semantic_recommendation",
    "co_cited_with",
    "bibliographic_coupling",
    "supports_same_claim",
    "same_research_branch",
]


@dataclass(slots=True)
class S2PaperIdentity:
    paper_id: str = ""
    corpus_id: int | None = None
    doi: str = ""
    arxiv_id: str = ""
    pmid: str = ""
    resolved_by: str = ""
    match_score: float | None = None
    identity_confidence: str = "unknown"
    identity_conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class S2PaperRecord:
    paper_id: str
    corpus_id: int | None = None
    doi: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    publication_types: list[str] = field(default_factory=list)
    publication_date: str = ""
    abstract: str = ""
    tldr: str = ""
    specter2_vector: list[float] = field(default_factory=list)
    citation_count: int = 0
    influential_citation_count: int = 0
    reference_count: int = 0
    is_oa: bool | None = None
    s2_open_access_candidate_url: str = ""
    s2_oa_status: str = ""
    s2_oa_license: str = ""
    text_availability: str = ""
    bibtex: str = ""
    external_ids: dict[str, Any] = field(default_factory=dict)
    fields_of_study: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=lambda: ["semantic_scholar"])
    # Native route/provenance fields.  These are intentionally independent of
    # the quality score: a paper discovered by S2 may later be materialized by
    # the local OA/full-text fallback without losing its discovery history.
    discovery_route: str = "semantic_scholar_graph"
    materialization_route: str = "not_materialized"
    content_depth: str = "metadata"
    use_permission: str = "discovery_only"
    scope_fit: str = "unreviewed"
    literature_roles: list[str] = field(default_factory=list)
    relation_roles: list[str] = field(default_factory=list)
    route_events: list[dict[str, Any]] = field(default_factory=list)
    metadata_conflicts: list[str] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class UnifiedTextChunk:
    chunk_id: str
    paper_id: str
    text: str
    title: str = ""
    corpus_id: int | None = None
    doi: str = ""
    section: str = ""
    content_kind: ContentKind = "text_chunk"
    text_provenance: TextProvenance = "s2_body_snippet"
    source_locator: dict[str, Any] = field(default_factory=dict)
    citation_roles: list[CitationRole] = field(default_factory=list)
    query_links: list[str] = field(default_factory=list)
    score: float = 0.0
    quality_status: str = "accepted"
    # A body snippet and a locally parsed paragraph are peers at the chunk
    # layer.  Their route is explicit, while downstream permission is derived
    # from content depth and scope rather than provider prestige.
    route_provenance: dict[str, Any] = field(default_factory=dict)
    content_depth: str = ""
    # S2 body snippets are not assumed complete merely because they are body
    # snippets.  Retrievers must assess truncation/omitted figures/equations;
    # locally parsed full text can still be promoted below.
    context_complete: bool = False
    use_permission: str = ""
    allowed_claim_kinds: list[str] = field(default_factory=list)
    scope_fit: str = "unreviewed"
    relation_roles: list[str] = field(default_factory=list)
    context_limitations: list[str] = field(default_factory=list)
    reference_mentions: list[dict[str, Any]] = field(default_factory=list)
    sentence_spans: list[dict[str, Any]] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content_depth:
            self.content_depth = (
                "structured_snippet"
                if self.text_provenance == "s2_body_snippet"
                else "abstract"
                if self.text_provenance == "s2_abstract_snippet"
                else "fulltext"
            )
        self.content_depth = normalize_content_depth(self.content_depth)
        self.scope_fit = normalize_scope_fit(self.scope_fit)
        if (
            self.content_depth == "fulltext"
            and self.text_provenance != "s2_body_snippet"
            and not self.context_complete
        ):
            self.context_complete = True
        if not self.route_provenance:
            self.route_provenance = {
                "discovery_route": (
                    "semantic_scholar_graph"
                    if self.text_provenance.startswith("s2_")
                    else "local_fulltext_or_publisher"
                ),
                "materialization_route": route_for_content_depth(
                    self.content_depth,
                    source_kind=self.text_provenance,
                ),
            }
        if not self.use_permission or not self.allowed_claim_kinds:
            permission = permission_for_content(
                self.content_depth,
                scope_fit=self.scope_fit,
                context_complete=self.context_complete,
            )
            self.use_permission = str(permission["use_permission"])
            self.allowed_claim_kinds = list(permission["allowed_claim_kinds"])
        else:
            self.use_permission = normalize_use_permission(self.use_permission)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LiteratureGraphEdge:
    edge_id: str
    source_paper_id: str
    target_paper_id: str
    edge_type: GraphEdgeType
    edge_direction: str = "directed"
    edge_origin: str = "s2_api"
    context: str = ""
    intents: list[str] = field(default_factory=list)
    is_influential: bool | None = None
    confidence: float = 1.0
    historical_role: str = "unknown"
    source_chunk_id: str = ""
    trace_status: str = "verified"
    # Relationship semantics are intentionally separate from the observed
    # graph edge.  Citation/recommendation APIs provide an observed relation;
    # a semantic relation such as "extends" must be inferred or reviewed.
    observed_relation: str = ""
    semantic_relation: str = ""
    relation_basis_chunk_ids: list[str] = field(default_factory=list)
    status: str = "observed"
    scope_conditions: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        observed = str(self.observed_relation or self.edge_type or "").strip().casefold()
        if observed not in CANONICAL_OBSERVED_RELATIONS:
            observed = str(self.edge_type or "unknown")
            self.validation_errors.append("unknown_observed_relation")
        self.observed_relation = observed
        status = str(self.status or "observed").strip().casefold()
        self.status = status if status in CANONICAL_RELATION_STATUSES else "observed"
        semantic = str(self.semantic_relation or "").strip().casefold()
        if semantic and semantic not in CANONICAL_SEMANTIC_RELATIONS:
            self.validation_errors.append("unknown_semantic_relation")
            semantic = ""
        self.relation_basis_chunk_ids = list(
            dict.fromkeys(
                str(item).strip()
                for item in self.relation_basis_chunk_ids
                if str(item).strip()
            )
        )
        if semantic and not self.relation_basis_chunk_ids:
            self.validation_errors.append("semantic_relation_requires_basis_chunk_ids")
            semantic = ""
            self.status = "observed"
        if semantic and self.status == "observed":
            self.validation_errors.append("semantic_relation_cannot_be_observed_only")
            semantic = ""
        self.semantic_relation = semantic
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ClaimCitationLink:
    claim_id: str
    section_id: str
    paper_id: str
    chunk_ids: list[str] = field(default_factory=list)
    citation_roles: list[CitationRole] = field(default_factory=list)
    fit_score: float = 0.0
    placement_scope: str = "paragraph"
    reason: str = ""
    verified_identity: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_paper_record(payload: dict[str, Any]) -> S2PaperRecord:
    """Normalize an S2 paper payload without losing the original response."""

    external_ids = payload.get("externalIds") or {}
    authors = [
        fix_text(str(item.get("name") or "")).strip()
        for item in (payload.get("authors") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    oa = payload.get("openAccessPdf") or {}
    tldr = payload.get("tldr") or {}
    embedding = payload.get("embedding") or {}
    publication_venue = payload.get("publicationVenue") or {}
    journal = payload.get("journal") or {}
    venue = fix_text(
        str(payload.get("venue") or "").strip()
        or str(publication_venue.get("name") or "").strip()
        or str(journal.get("name") or "").strip()
    )
    fields = payload.get("s2FieldsOfStudy") or []
    field_names = [
        str(item.get("category") or "").strip()
        for item in fields
        if isinstance(item, dict) and str(item.get("category") or "").strip()
    ]
    is_oa_raw = payload.get("isOpenAccess")
    corpus_raw = payload.get("corpusId")
    try:
        corpus_id = int(corpus_raw) if corpus_raw not in (None, "") else None
    except (TypeError, ValueError):
        corpus_id = None

    return S2PaperRecord(
        paper_id=str(payload.get("paperId") or "").strip(),
        corpus_id=corpus_id,
        doi=str(external_ids.get("DOI") or "").strip(),
        title=fix_text(str(payload.get("title") or "")).strip(),
        authors=authors,
        year=payload.get("year") if isinstance(payload.get("year"), int) else None,
        venue=venue,
        publication_types=[
            str(item) for item in (payload.get("publicationTypes") or []) if item
        ],
        publication_date=str(payload.get("publicationDate") or "").strip(),
        abstract=fix_text(str(payload.get("abstract") or "")).strip(),
        tldr=fix_text(str(tldr.get("text") or "")).strip()
        if isinstance(tldr, dict)
        else "",
        specter2_vector=[
            float(value)
            for value in (embedding.get("vector") or [])
            if isinstance(value, (int, float))
        ]
        if isinstance(embedding, dict)
        else [],
        citation_count=int(payload.get("citationCount") or 0),
        influential_citation_count=int(payload.get("influentialCitationCount") or 0),
        reference_count=int(payload.get("referenceCount") or 0),
        is_oa=bool(is_oa_raw) if isinstance(is_oa_raw, bool) else None,
        s2_open_access_candidate_url=(
            str(oa.get("url") or "").strip() if isinstance(oa, dict) else ""
        ),
        s2_oa_status=str(oa.get("status") or "").strip()
        if isinstance(oa, dict)
        else "",
        s2_oa_license=str(oa.get("license") or "").strip()
        if isinstance(oa, dict)
        else "",
        text_availability=str(payload.get("textAvailability") or "").strip(),
        bibtex=str((payload.get("citationStyles") or {}).get("bibtex") or "").strip(),
        external_ids=dict(external_ids) if isinstance(external_ids, dict) else {},
        fields_of_study=list(dict.fromkeys(field_names)),
        content_depth=(
            "abstract"
            if payload.get("abstract")
            else "tldr"
            if isinstance(tldr, dict) and tldr.get("text")
            else "metadata"
        ),
        use_permission=(
            "background_and_candidate_only"
            if payload.get("abstract") or (isinstance(tldr, dict) and tldr.get("text"))
            else "discovery_only"
        ),
        route_events=[
            {
                "route": "semantic_scholar_graph",
                "event": "metadata_observed",
            }
        ],
        raw_metadata=dict(payload),
    )
