"""Phase 2 artifact schemas — typed Pydantic models for all Section Coverage outputs."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared enumerations
# ---------------------------------------------------------------------------

class CoverageRole(str, Enum):
    foundation = "foundation"
    mechanism = "mechanism"
    method = "method"
    frontier = "frontier"
    controversy = "controversy"
    application = "application"


class RolePriority(str, Enum):
    required = "required"
    important = "important"
    useful = "useful"
    not_needed = "not_needed"


class ScopeFit(str, Enum):
    direct = "direct"
    adjacent = "adjacent"
    contextual = "contextual"
    out_of_scope = "out_of_scope"
    unreviewed = "unreviewed"


class CandidateDecision(str, Enum):
    approved = "approved"
    rejected = "rejected"
    deferred = "deferred"


class CandidateAction(str, Enum):
    """Deterministic post-audit action, distinct from model audit language."""

    materialize_now = "materialize_now"
    discovery_lead = "discovery_lead"
    reject = "reject"


class AcquisitionStatus(str, Enum):
    fulltext = "fulltext"
    structured_snippet = "structured_snippet"
    abstract_only = "abstract_only"
    metadata_only = "metadata_only"
    failed = "failed"
    not_attempted = "not_attempted"


# ---------------------------------------------------------------------------
# SECTION_CONTEXT.json
# ---------------------------------------------------------------------------

class SectionContext(BaseModel):
    schema_version: str = "2.0"
    section_id: str
    section_title: str
    chapter_argument: str
    scope_description: str
    scope_guardrails: List[str] = Field(default_factory=list)
    required_roles: List[CoverageRole] = Field(default_factory=list)
    optional_roles: List[CoverageRole] = Field(default_factory=list)
    kb_sqlite_path: Optional[str] = None
    existing_paper_count: int = 0
    existing_chunk_count: int = 0
    minimum_unique_sources: int = 6
    minimum_direct_sources: int = 3
    topic_identity: Dict[str, Any] = Field(default_factory=dict)
    shared_kb_sqlite_paths: List[str] = Field(default_factory=list)
    source_ledger_path: Optional[str] = None
    section_overlay_path: Optional[str] = None
    selected_paper_ids: List[str] = Field(default_factory=list)
    selected_chunk_ids: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SECTION_COVERAGE_PLAN.json
# ---------------------------------------------------------------------------

class RolePlan(BaseModel):
    role: CoverageRole
    priority: RolePriority
    coverage_question: str
    intended_synthesis: str
    queries: List[str] = Field(default_factory=list)
    local_hit_count: int = 0
    gap_severity: str = "unknown"  # "blocking" | "important" | "minor" | "none"


class SectionCoveragePlan(BaseModel):
    schema_version: str = "2.0"
    section_id: str
    chapter_argument: str
    roles: Dict[str, RolePlan] = Field(default_factory=dict)
    topic_fingerprint: str = ""
    query_topic_corrections: List[Dict[str, Any]] = Field(
        default_factory=list
    )
    planning_model: str = "agent"
    created_at: str = ""


# ---------------------------------------------------------------------------
# LOCAL_COVERAGE_AUDIT.json
# ---------------------------------------------------------------------------

class LocalRoleAudit(BaseModel):
    role: CoverageRole
    paper_count: int = 0
    chunk_count: int = 0
    top_paper_ids: List[str] = Field(default_factory=list)
    coverage_verdict: str = "none"  # "sufficient" | "partial" | "none"
    gap_severity: str = "minor"     # "blocking" | "important" | "minor"
    sample_titles: List[str] = Field(default_factory=list)


class LocalCoverageAudit(BaseModel):
    schema_version: str = "2.0"
    section_id: str
    role_audits: Dict[str, LocalRoleAudit] = Field(default_factory=dict)
    total_local_papers: int = 0
    total_local_chunks: int = 0
    blocking_gaps: List[str] = Field(default_factory=list)
    important_gaps: List[str] = Field(default_factory=list)
    sufficient_roles: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# OA_CANDIDATE_LEDGER.json
# ---------------------------------------------------------------------------

class OACandidate(BaseModel):
    candidate_id: str
    section_id: str
    role: str
    title: str
    doi: str = ""
    year: Optional[int] = None
    venue: str = ""
    authors: List[str] = Field(default_factory=list)
    abstract: str = ""
    is_oa: bool = False
    oa_url: str = ""
    pdf_url: str = ""
    # Extended URL fields — passed through to KBIngester URL waterfall
    url_for_pdf: str = ""          # OpenAlex primary_location url_for_pdf
    best_oa_url: str = ""          # Unpaywall best_oa_url
    open_access_url: str = ""      # explicit OA landing URL
    html_url: str = ""             # HTML landing page
    repository_url: str = ""       # institutional repository
    content_urls: Dict[str, str] = Field(default_factory=dict)   # OpenAlex content URLs
    alternate_urls: List[str] = Field(default_factory=list)      # all other fallback URLs
    semantic_scholar_id: str = ""
    corpus_id: Optional[int | str] = None
    openalex_id: str = ""
    tldr: str = ""
    text_availability: Dict[str, Any] = Field(default_factory=dict)
    citation_count: int = 0
    backends: List[str] = Field(default_factory=list)
    query_texts: List[str] = Field(default_factory=list)
    relevance_score: float = 0.0
    # Agent audit fields
    scope_fit: ScopeFit = ScopeFit.unreviewed
    role_fit: List[str] = Field(default_factory=list)
    # Query-target provenance is a set/union, not a single overwritten role.
    # Values are the scientific roles and the lists retain the originating
    # query strings for audit and adaptive reuse.
    role_provenance: Dict[str, List[str]] = Field(default_factory=dict)
    scope_violations: List[Dict[str, Any]] = Field(default_factory=list)
    boundary_violations: List[Dict[str, Any]] = Field(default_factory=list)
    decision: CandidateDecision = CandidateDecision.deferred
    candidate_action: CandidateAction = CandidateAction.discovery_lead
    audit_reason: str = ""
    not_usable_for: List[str] = Field(default_factory=list)
    # Cross-wave accounting is durable state, not a prompt hint.  These
    # fields let a resumed worker distinguish a new candidate from a new
    # candidate ID for an already-attempted paper.
    material_identity: str = ""
    attempted_waves: List[int] = Field(default_factory=list)
    materialization_attempts: int = 0
    last_materialization_status: str = ""
    no_progress: bool = False
    no_progress_components: List[str] = Field(default_factory=list)


class OACandidateLedger(BaseModel):
    schema_version: str = "2.0"
    section_id: str
    candidates: List[OACandidate] = Field(default_factory=list)

    def approved(self) -> List[OACandidate]:
        return [c for c in self.candidates if c.decision == CandidateDecision.approved]

    def by_role(self, role: str) -> List[OACandidate]:
        return [c for c in self.candidates if c.role == role]


# ---------------------------------------------------------------------------
# MATERIALIZATION_MANIFEST.json
# ---------------------------------------------------------------------------

class MaterializedPaper(BaseModel):
    candidate_id: str
    paper_id: str
    doi: str = ""
    title: str = ""
    year: Optional[int] = None
    venue: str = ""
    acquisition_status: AcquisitionStatus = AcquisitionStatus.not_attempted
    download_url: str = ""
    download_error: str = ""
    chunk_ids: List[str] = Field(default_factory=list)
    new_chunk_ids: List[str] = Field(default_factory=list)
    new_paper: bool = False
    paper_row_inserted: bool = False
    new_chunks: int = 0
    reused_chunks: int = 0
    section_id: str = ""
    role: str = ""
    role_fit: List[str] = Field(default_factory=list)
    role_provenance: Dict[str, List[str]] = Field(default_factory=dict)
    scope_fit: ScopeFit = ScopeFit.unreviewed
    materialization_route: str = "not_materialized"
    chunk_count: int = 0
    # Full diagnostics — never empty when acquisition_status != fulltext
    attempted_urls: List[str] = Field(default_factory=list)
    download_errors_by_url: Dict[str, str] = Field(default_factory=dict)
    content_type_detected: str = ""   # e.g. "text/html", "application/pdf", "challenge_page"
    parse_failure_reason: str = ""    # why chunks==0 even after download
    visual_ingest_status: str = ""
    visual_candidate_count: int = 0
    visual_composite_parent_excluded_count: int = 0
    visual_ingest_report_path: str = ""


class MaterializationManifest(BaseModel):
    schema_version: str = "2.0"
    section_id: str
    papers: List[MaterializedPaper] = Field(default_factory=list)
    total_new_papers: int = 0
    total_new_chunks: int = 0
    total_reused_chunks: int = 0
    total_failed: int = 0
    temp_kb_path: Optional[str] = None


# ---------------------------------------------------------------------------
# SECTION_SOURCE_LEDGER.json  (final accepted sources with full provenance)
# ---------------------------------------------------------------------------

class SourceEntry(BaseModel):
    paper_id: str
    doi: str = ""
    title: str = ""
    year: Optional[int] = None
    venue: str = ""
    authors: List[str] = Field(default_factory=list)
    literature_role: str = ""
    scope_fit: ScopeFit = ScopeFit.unreviewed
    retrieval_query: str = ""
    retrieval_backend: str = ""
    adoption_reason: str = ""
    expected_section_use: str = ""
    canonical_chunk_ids: List[str] = Field(default_factory=list)
    local_prior: bool = False
    new_this_run: bool = False
    acquisition_status: AcquisitionStatus = AcquisitionStatus.not_attempted
    normalization_status: str = ""
    section_id: str = ""
    not_usable_for: List[str] = Field(default_factory=list)
    # Route provenance is part of the source contract, not a report-time join.
    # It allows downstream writers to distinguish S2 structured text, parsed
    # full text, abstract fallback, and metadata discovery without guessing.
    discovery_route: str = "unknown"
    materialization_route: str = "not_materialized"
    content_depth: str = "metadata"
    use_permission: str = "discovery_only"
    allowed_claim_kinds: List[str] = Field(default_factory=list)
    route_events: List[Dict[str, Any]] = Field(default_factory=list)
    metadata_conflicts: List[str] = Field(default_factory=list)
    relation_roles: List[str] = Field(default_factory=list)
    role_provenance: Dict[str, List[str]] = Field(default_factory=dict)


class SectionSourceLedger(BaseModel):
    schema_version: str = "2.0"
    section_id: str
    sources: List[SourceEntry] = Field(default_factory=list)
    total_sources: int = 0
    new_sources: int = 0
    local_prior_sources: int = 0


# ---------------------------------------------------------------------------
# SECTION_GAP_REPORT.json
# ---------------------------------------------------------------------------

class GapEntry(BaseModel):
    role: str
    severity: str  # "blocking" | "important" | "minor"
    description: str
    queries_attempted: List[str] = Field(default_factory=list)
    candidates_found: int = 0
    candidates_approved: int = 0
    candidates_materialized: int = 0
    stop_reason: str = ""
    suggested_followup: str = ""
    is_blocking: bool = False


class SectionGapReport(BaseModel):
    schema_version: str = "2.0"
    section_id: str
    gaps: List[GapEntry] = Field(default_factory=list)
    overall_coverage_status: str = "unknown"
    # "coverage_sufficient" | "completed_with_open_gaps" | "blocking_gaps_remain"
    blocking_gap_count: int = 0
    open_gap_count: int = 0
    stop_conditions_met: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SECTION_MATERIAL_PACKAGE.json  (final deliverable summary)
# ---------------------------------------------------------------------------

class SectionMaterialPackage(BaseModel):
    schema_version: str = "2.0"
    section_id: str
    section_title: str = ""
    chapter_argument: str = ""
    coverage_status: str = "unknown"
    total_sources: int = 0
    unique_sources: int = 0
    direct_sources: int = 0
    minimum_unique_sources: int = 0
    minimum_direct_sources: int = 0
    breadth_target_met: bool = False
    new_sources_this_run: int = 0
    local_prior_sources: int = 0
    sources_by_role: Dict[str, int] = Field(default_factory=dict)
    chunk_ids_by_role: Dict[str, List[str]] = Field(default_factory=dict)
    blocking_gaps_remain: bool = False
    gap_summary: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)
    # keys: "SECTION_CONTEXT", "SECTION_COVERAGE_PLAN", "LOCAL_COVERAGE_AUDIT",
    #       "OA_CANDIDATE_LEDGER", "MATERIALIZATION_MANIFEST",
    #       "SECTION_SOURCE_LEDGER", "SECTION_GAP_REPORT"
    run_id: str = ""
    task_id: str = ""
    wall_time_seconds: float = 0.0
    total_input_tokens: int = 0
    estimated_cost_usd: float = 0.0
