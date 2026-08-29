"""Phase 3 authoring schemas — typed Pydantic models for Section Review Author outputs."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# SECTION_AUTHORING_CONTEXT.json
# ---------------------------------------------------------------------------

class AuthoringSourceEntry(BaseModel):
    paper_id: str
    doi: str = ""
    title: str = ""
    year: Optional[int] = None
    venue: str = ""
    authors: List[str] = Field(default_factory=list)
    literature_role: str = ""
    scope_fit: str = "unreviewed"
    canonical_chunk_ids: List[str] = Field(default_factory=list)
    acquisition_status: str = "unknown"
    not_usable_for: List[str] = Field(default_factory=list)
    discovery_route: str = "unknown"
    materialization_route: str = "not_materialized"
    content_depth: str = "metadata"
    use_permission: str = "discovery_only"
    allowed_claim_kinds: List[str] = Field(default_factory=list)
    route_events: List[Dict[str, Any]] = Field(default_factory=list)
    metadata_conflicts: List[str] = Field(default_factory=list)
    relation_roles: List[str] = Field(default_factory=list)


class SectionAuthoringContext(BaseModel):
    schema_version: str = "3.2"
    section_id: str
    section_title: str = ""
    chapter_argument: str = ""
    scope_guardrails: List[str] = Field(default_factory=list)
    coverage_status: str = "unknown"
    total_sources: int = 0
    sources_by_role: Dict[str, int] = Field(default_factory=dict)
    chunk_ids_by_role: Dict[str, List[str]] = Field(default_factory=dict)
    blocking_gaps_remain: bool = False
    gap_summary: str = ""
    sources: List[AuthoringSourceEntry] = Field(default_factory=list)
    visual_chunk_ids: List[str] = Field(default_factory=list)
    claims: List[Dict[str, Any]] = Field(default_factory=list)
    mentor_advice: Dict[str, Any] = Field(default_factory=dict)
    full_review_argument: str = ""
    topic_identity: Dict[str, Any] = Field(default_factory=dict)
    section_role: str = ""
    preceding_section_conclusion: str = ""
    following_section_role: str = ""
    transition_contract: Dict[str, Any] = Field(default_factory=dict)
    terminology_ledger: Dict[str, Any] = Field(default_factory=dict)
    revision_instructions: Dict[str, Any] = Field(default_factory=dict)
    existing_draft_text: str = ""
    phase2_gap_report: Dict[str, Any] = Field(default_factory=dict)
    section_contract: Dict[str, Any] = Field(default_factory=dict)
    # This is a section-level synthesis breadth contract, not a demand for
    # sentence-level citation density.  It prevents a long review section from
    # being composed from only one or two papers when a broader audited
    # literature package is already available.
    minimum_synthesis_sources: int = 0
    available_synthesis_sources: int = 0
    synthesis_bundle: Dict[str, Any] = Field(default_factory=dict)
    # R4 deterministic policy: one row per claim, recording the strongest
    # language permitted by the Phase-3 binding and permission audit.
    judgment_ledger: List[Dict[str, Any]] = Field(default_factory=list)
    claim_strength_policy: Dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# SECTION_ARGUMENT_PLAN.json
# ---------------------------------------------------------------------------

class ParagraphPlan(BaseModel):
    paragraph_index: int
    function: str          # "introduction" | "mechanism" | "evidence" | "synthesis" | "transition"
    topic_sentence: str
    key_claims: List[str] = Field(default_factory=list)
    evidence_chunk_ids: List[str] = Field(default_factory=list)
    paper_ids: List[str] = Field(default_factory=list)
    writing_permission: str = "factual_assertion"
    # factual_assertion | hedged_factual_assertion | interpretive_synthesis |
    # common_background | structural_transition | evidence_gap_only
    expected_word_count: int = 0


class SectionArgumentPlan(BaseModel):
    schema_version: str = "3.0"
    section_id: str
    chapter_argument: str = ""
    argument_flow: str = ""         # brief narrative of how the paragraphs build the argument
    paragraphs: List[ParagraphPlan] = Field(default_factory=list)
    total_expected_words: int = 0
    evidence_coverage: Dict[str, List[str]] = Field(default_factory=dict)   # role → chunk_ids used
    open_questions: List[str] = Field(default_factory=list)
    created_at: str = ""


# ---------------------------------------------------------------------------
# SECTION_EVIDENCE_PACKET.json
# ---------------------------------------------------------------------------

class EvidenceItem(BaseModel):
    chunk_id: str
    paper_id: str
    paper_title: str = ""
    year: Optional[int] = None
    literature_role: str = ""
    scope_fit: str = "unreviewed"
    evidence_level: str = "fulltext"
    exact_spans: List[str] = Field(default_factory=list)
    exact_span_source: str = "model_provided"
    claim_ids: List[str] = Field(default_factory=list)
    writing_permission: str = "factual_assertion"
    not_usable_for: List[str] = Field(default_factory=list)


class SectionEvidencePacket(BaseModel):
    schema_version: str = "3.0"
    section_id: str
    items: List[EvidenceItem] = Field(default_factory=list)
    total_items: int = 0
    items_by_role: Dict[str, int] = Field(default_factory=dict)
    uncovered_claim_ids: List[str] = Field(default_factory=list)
    created_at: str = ""


# ---------------------------------------------------------------------------
# SECTION_CITATION_MAP.json
# ---------------------------------------------------------------------------

class CitationEntry(BaseModel):
    sentence_index: int
    sentence_snippet: str = ""
    chunk_ids: List[str] = Field(default_factory=list)
    paper_ids: List[str] = Field(default_factory=list)
    citation_type: str = "factual"      # "factual" | "methodological" | "contextual" | "hedged"
    entailment_verdict: str = "unknown"  # "entailed" | "partial" | "not_entailed" | "unknown"
    audit_note: str = ""


class SectionCitationMap(BaseModel):
    schema_version: str = "3.0"
    section_id: str
    citations: List[CitationEntry] = Field(default_factory=list)
    total_cited_sentences: int = 0
    uncited_sentences: int = 0
    papers_cited: List[str] = Field(default_factory=list)
    created_at: str = ""


# ---------------------------------------------------------------------------
# SECTION_VISUAL_PLACEMENT.json
# ---------------------------------------------------------------------------

class VisualPlacement(BaseModel):
    visual_chunk_id: str
    paper_id: str = ""
    caption: str = ""
    placement_after_paragraph: int = 0
    argument_type: str = ""
    argument_claim: str = ""
    asset_status: str = "unknown"   # "verified_local" | "approved_ai_conceptual_schematic" | "missing"
    local_image_path: str = ""
    placement_rationale: str = ""


class SectionVisualPlacement(BaseModel):
    schema_version: str = "3.0"
    section_id: str
    placements: List[VisualPlacement] = Field(default_factory=list)
    total_placed: int = 0
    missing_visuals: int = 0
    created_at: str = ""


# ---------------------------------------------------------------------------
# SECTION_AUTHORING_AUDIT.json
# ---------------------------------------------------------------------------

class AuditFlag(BaseModel):
    flag_type: str          # "overclaim" | "uncited_fact" | "unknown_ref" | "scope_violation"
    sentence_snippet: str = ""
    sentence_index: int = -1
    severity: str = "minor"  # "blocking" | "important" | "minor"
    reason: str = ""
    suggested_fix: str = ""
    resolved: bool = False
    # Deterministic risk class used by the R4 convergence controller.  Keeping
    # this on the persisted flag makes the blocking decision auditable without
    # asking a later model turn to reinterpret the sentence.
    risk_class: str = ""


class SectionAuthoringAudit(BaseModel):
    schema_version: str = "3.0"
    section_id: str
    overclaim_flags: List[AuditFlag] = Field(default_factory=list)
    citation_flags: List[AuditFlag] = Field(default_factory=list)
    scope_flags: List[AuditFlag] = Field(default_factory=list)
    total_blocking_flags: int = 0
    total_flags: int = 0
    audit_passed: bool = False
    audit_summary: str = ""
    created_at: str = ""


# ---------------------------------------------------------------------------
# SECTION_REVISION_HISTORY.json
# ---------------------------------------------------------------------------

class RevisionEntry(BaseModel):
    revision_index: int
    stage: str          # "initial_draft" | "citation_pass" | "audit_pass" | "revision" | "final"
    reason: str = ""
    word_count_before: int = 0
    word_count_after: int = 0
    flags_resolved: int = 0
    flags_remaining: int = 0
    summary: str = ""
    created_at: str = ""


class SectionRevisionHistory(BaseModel):
    schema_version: str = "3.0"
    section_id: str
    revisions: List[RevisionEntry] = Field(default_factory=list)
    total_revisions: int = 0
    current_stage: str = "draft"
    created_at: str = ""


# ---------------------------------------------------------------------------
# SECTION_COVERAGE_FEEDBACK.json  (optional — only when material gaps block authoring)
# ---------------------------------------------------------------------------

class CoverageFeedbackItem(BaseModel):
    role: str
    severity: str = "important"
    description: str = ""
    blocking_claims: List[str] = Field(default_factory=list)
    suggested_queries: List[str] = Field(default_factory=list)


class SectionCoverageFeedback(BaseModel):
    schema_version: str = "3.0"
    section_id: str
    state: str = "needs_more_literature"
    feedback_items: List[CoverageFeedbackItem] = Field(default_factory=list)
    total_blocking: int = 0
    authoring_can_proceed: bool = False
    created_at: str = ""


# ---------------------------------------------------------------------------
# SECTION_AUTHORING_PACKAGE.json  (final deliverable)
# ---------------------------------------------------------------------------

class SectionAuthoringPackage(BaseModel):
    schema_version: str = "3.0"
    section_id: str
    section_title: str = ""
    chapter_argument: str = ""
    authoring_status: str = "unknown"
    # "completed" | "completed_with_flags" | "needs_more_literature" | "failed"
    word_count: int = 0
    paragraph_count: int = 0
    cited_sentences: int = 0
    total_flags: int = 0
    blocking_flags: int = 0
    papers_cited: List[str] = Field(default_factory=list)
    artifacts: Dict[str, str] = Field(default_factory=dict)
    # keys: SECTION_AUTHORING_CONTEXT, SECTION_ARGUMENT_PLAN, SECTION_EVIDENCE_PACKET,
    #       SECTION_DRAFT_EN, SECTION_CITATION_MAP, SECTION_VISUAL_PLACEMENT,
    #       SECTION_AUTHORING_AUDIT, SECTION_REVISION_HISTORY,
    #       SECTION_COVERAGE_FEEDBACK (optional)
    run_id: str = ""
    task_id: str = ""
    wall_time_seconds: float = 0.0
    total_input_tokens: int = 0
    estimated_cost_usd: float = 0.0
    created_at: str = ""
