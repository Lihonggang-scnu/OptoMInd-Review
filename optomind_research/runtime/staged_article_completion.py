"""Staged full-manuscript article completion contracts and offline runner.

This increment adds an explicit staged artifact/schema layer for the
full-manuscript completion upgrade.  The staged order is fixed:

handoff/metadata repair -> commander structural candidates/adjudication ->
conclusion plan/draft -> introduction plan/draft -> abstract plan/draft ->
whole-manuscript multi-review -> bounded patch proposals -> editorial
revision (bounded local materialization) -> visual remount ->
assembly/PDF/preflight.

The commander may decide article-level structure (order, section
responsibility, duplication, missing axes, visual work orders) but never
silently changes bound claim/evidence.  Semantic patch proposals always carry
``approval_required`` and provenance.  Whole-manuscript reviewer findings are
advisory unless consensus severity is critical.  The editorial revision
stage converts actionable patch proposals and review findings into bounded
work items: each author call sees only the target block and its neighbors,
and every proposed change is independently verified before a new assembled
manuscript is materialized.  No upstream chapter asset is ever overwritten
and no single LLM is asked to rewrite the whole article.  LLM providers fill
only high-information payload fields; local code owns the envelope,
fingerprints, IDs, status, and provenance.

The deterministic offline runner writes one artifact per stage, fingerprints
every stage, and supports resume/no-op when stage input fingerprints are
unchanged.  It never calls a model or the network.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from llm import qwen_chat_client
from pydantic import BaseModel, Field


SCHEMA_VERSION = "optomind.staged_article_completion.v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPTS_DIR = PROJECT_ROOT / "prompts"
PROMPT_FILE_BY_STAGE = {
    "conclusion": "Staged Conclusion Author.txt",
    "introduction": "Staged Introduction Author.txt",
    "abstract": "Staged Abstract Author.txt",
    "whole_manuscript_review": "Staged Whole Manuscript Reviewer.txt",
    "bounded_patch_proposals": "Staged Commander Editor.txt",
    "editorial_revision": "Staged Editorial Revision Author.txt",
}
EDITORIAL_VERIFIER_PROMPT_FILE = "Staged Editorial Revision Verifier.txt"
COMPLETE_REVIEW_MARKDOWN = "STAGED_COMPLETE_REVIEW_EN.md"
COMPLETE_REVIEW_MANIFEST = "STAGED_COMPLETE_REVIEW_EN.sha256.json"
EDITORIAL_CHECKPOINT_JSON = "staged_editorial_revision_checkpoint.json"
EDITORIAL_CHECKPOINT_SCHEMA = "optomind.staged_editorial_revision_checkpoint.v1"
STAGE_ORDER = (
    "handoff_metadata_repair",
    "commander_structure",
    "conclusion",
    "introduction",
    "abstract",
    "whole_manuscript_review",
    "bounded_patch_proposals",
    "editorial_revision",
    "visual_remount",
    "assembly_preflight",
)

STAGE_STATUSES = frozenset(
    {"pending", "running", "completed", "noop", "failed", "awaiting_approval"}
)
REVIEW_SEVERITIES = frozenset({"advisory", "minor", "major", "critical"})
REVIEW_CONSENSUS = frozenset({"none", "split", "majority", "consensus"})
REVIEW_DIMENSIONS = frozenset(
    {"continuity", "clarity", "reader_flow", "logic", "overlap"}
)

SEMANTIC_PATCH_OPERATIONS = frozenset(
    {
        "delete_block",
        "merge_blocks",
        "ownership_change",
        "claim_strength_change",
        "evidence_change",
    }
)
EDITORIAL_KINDS = frozenset(
    {
        "transition",
        "connective_or_coreference",
        "cross_reference",
        "terminology_clarification",
    }
)
AUTO_EDITORIAL_OPERATIONS = frozenset({"rewrite_transition"})
_REF_MARKER_PATTERN = re.compile(r"\[REF:[^\]]*\]", re.IGNORECASE)

PRESENTATION_IR_SCHEMA = "optomind.literature_review_presentation_ir.v1"
TITLE_PLAN_SCHEMA = "optomind.review_title_plan.v1"
FRONT_MATTER_STAGES = frozenset({"conclusion", "introduction", "abstract"})

STATE_JSON = "staged_article_completion_state.json"
ARTIFACT_PREFIX = "staged_"


class StagedCompletionError(RuntimeError):
    """Raised when a staged artifact cannot be produced safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


class StagedStageState(BaseModel):
    """Fixed local envelope for one staged artifact."""

    schema_version: str = SCHEMA_VERSION
    stage: str
    status: str
    fingerprint: str = ""
    input_fingerprint: str = ""
    artifact_path: str = ""
    approval_required: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, str] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


class StagedArticleCompletionState(BaseModel):
    """Persistent run state for the staged completion pipeline."""

    schema_version: str = SCHEMA_VERSION
    run_id: str = ""
    run_fingerprint: str = ""
    stage_order: list[str] = Field(default_factory=list)
    status: str = "pending"
    resumed: bool = False
    stages: dict[str, StagedStageState] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def all_completed(self) -> bool:
        return bool(self.stages) and all(
            stage.status in {"completed", "noop"}
            for stage in self.stages.values()
        )

    @property
    def awaiting_approval_stages(self) -> list[str]:
        """Stages whose payload/proposals carry an explicit approval flag.

        Derived from ``StagedStageState.approval_required`` so approval
        stays visible even when a resumed stage is marked ``noop`` or the
        stage otherwise completes fail-open.
        """

        return [
            stage
            for stage, record in self.stages.items()
            if record.approval_required
        ]


# --------------------------------------------------------------------------- #
# Commander structural authority
# --------------------------------------------------------------------------- #


class SectionOrderEntry(BaseModel):
    section_id: str
    position: int = 0
    rationale: str = ""


class SectionResponsibility(BaseModel):
    section_id: str
    responsibility: str = ""
    provenance: str = "commander_work_order"


class DuplicationFinding(BaseModel):
    section_ids: list[str] = Field(default_factory=list)
    duplication_type: str = ""
    advisory: bool = True


class MissingAxisEntry(BaseModel):
    axis_id: str
    description: str = ""
    recommended_section_id: str = ""


class StructureGapProposal(BaseModel):
    gap_type: str = "structure"
    section_id: str = ""
    proposal: str = ""
    approval_required: bool = False


class VisualWorkOrder(BaseModel):
    figure_id: str = ""
    work_order: str = ""
    visual_chunk_id: str = ""


class CommanderStructuralAuthority(BaseModel):
    """Article-structure authority without silent claim/evidence changes."""

    schema_version: str = "optomind.staged_commander_authority.v1"
    source_work_order_path: str = ""
    fingerprint: str = ""
    section_order: list[SectionOrderEntry] = Field(default_factory=list)
    section_responsibilities: list[SectionResponsibility] = Field(
        default_factory=list
    )
    duplication: list[DuplicationFinding] = Field(default_factory=list)
    missing_axes: list[MissingAxisEntry] = Field(default_factory=list)
    structure_gaps: list[StructureGapProposal] = Field(default_factory=list)
    visual_work_orders: list[VisualWorkOrder] = Field(default_factory=list)
    claim_evidence_invariant: Literal["preserved"] = "preserved"
    read_only: bool = True
    approval_required_for: list[str] = Field(default_factory=list)


def validate_claim_evidence_invariant_preserved(
    payload: Mapping[str, Any],
) -> None:
    """Fail closed if an authority payload smuggles claim/evidence changes."""

    forbidden = {
        "claim_text_changes",
        "claim_changes",
        "evidence_changes",
        "binding_changes",
        "rewritten_claims",
        "rewritten_evidence",
    }
    structure = payload.get("structure", {})
    structure = structure if isinstance(structure, Mapping) else {}
    present = sorted(
        key for key in forbidden if key in payload or key in structure
    )
    if present:
        raise StagedCompletionError(
            "claim/evidence invariant violated: " + ", ".join(present)
        )


def build_commander_structural_authority(
    work_order: Mapping[str, Any],
    *,
    source_work_order_path: str = "",
    authority_fingerprint: str = "",
) -> CommanderStructuralAuthority:
    """Extract a staged authority package from a commander work order."""

    work_order = dict(work_order)
    validate_claim_evidence_invariant_preserved(work_order)
    declaration = _mapping(work_order.get("read_only_declaration"))
    if declaration.get("chapter_text_changed") is True:
        raise StagedCompletionError(
            "commander work order declares chapter text changes; "
            "structural authority cannot be staged"
        )
    section_order = []
    for index, raw in enumerate(work_order.get("proposed_section_order") or []):
        if isinstance(raw, Mapping):
            section_order.append(
                SectionOrderEntry(
                    section_id=str(raw.get("section_id") or ""),
                    position=int(raw.get("position") or index),
                    rationale=str(raw.get("rationale") or ""),
                )
            )
        else:
            section_order.append(
                SectionOrderEntry(
                    section_id=str(raw), position=index
                )
            )
    responsibilities: list[SectionResponsibility] = []
    for raw in work_order.get("section_decisions") or []:
        if not isinstance(raw, Mapping):
            continue
        decision = str(raw.get("decision") or "").strip()
        rationale = str(raw.get("rationale") or "").strip()
        responsibility = str(raw.get("responsibility") or decision).strip()
        parts = [part for part in (responsibility, rationale) if part]
        responsibilities.append(
            SectionResponsibility(
                section_id=str(raw.get("section_id") or ""),
                responsibility=" | ".join(parts),
            )
        )
    duplication: list[DuplicationFinding] = []
    for raw in (
        work_order.get("repeated_paper_role_audit")
        or work_order.get("cross_section_conflicts")
        or []
    ):
        if isinstance(raw, Mapping):
            section_ids = [
                str(value)
                for value in (
                    raw.get("section_ids") or raw.get("sections") or []
                )
                if str(value)
            ]
            duplication_type = str(
                raw.get("duplication_type")
                or raw.get("conflict_type")
                or ""
            )
        else:
            section_ids = [str(raw)]
            duplication_type = ""
        duplication.append(
            DuplicationFinding(
                section_ids=section_ids,
                duplication_type=duplication_type,
            )
        )
    missing_axes = [
        MissingAxisEntry(
            axis_id=str(raw.get("axis_id") or "") if isinstance(raw, Mapping) else str(raw),
            description=str(raw.get("description") or "") if isinstance(raw, Mapping) else "",
            recommended_section_id=str(raw.get("recommended_section_id") or "") if isinstance(raw, Mapping) else "",
        )
        for raw in work_order.get("missing_axes") or []
    ]
    structure_gaps = [
        StructureGapProposal(
            gap_type=str(raw.get("gap_type") or "structure") if isinstance(raw, Mapping) else "structure",
            section_id=str(raw.get("section_id") or "") if isinstance(raw, Mapping) else "",
            proposal=str(raw.get("proposal") or raw.get("gap") or "") if isinstance(raw, Mapping) else str(raw),
            approval_required=bool(raw.get("approval_required")) if isinstance(raw, Mapping) else False,
        )
        for raw in work_order.get("structure_gaps") or []
    ]
    visual_orders = [
        VisualWorkOrder(
            figure_id=str(raw.get("figure_id") or "") if isinstance(raw, Mapping) else "",
            work_order=str(raw.get("work_order") or raw.get("proposal") or "") if isinstance(raw, Mapping) else str(raw),
            visual_chunk_id=str(raw.get("visual_chunk_id") or "") if isinstance(raw, Mapping) else "",
        )
        for raw in work_order.get("visual_work_orders") or []
    ]
    approval_ids = [
        str(proposal.get("patch_id") or "")
        for proposal in work_order.get("proposed_patch_set") or []
        if isinstance(proposal, Mapping)
        and proposal_requires_approval(proposal)
    ]
    authority = CommanderStructuralAuthority(
        source_work_order_path=source_work_order_path,
        fingerprint=authority_fingerprint
        or fingerprint(
            {
                "schema_version": "optomind.staged_commander_authority.v1",
                "work_order_fingerprint": str(
                    work_order.get("fingerprint") or ""
                ),
                "proposed_section_order": work_order.get(
                    "proposed_section_order"
                ),
                "section_decisions": work_order.get("section_decisions"),
                "structure_gaps": work_order.get("structure_gaps"),
            }
        ),
        section_order=section_order,
        section_responsibilities=responsibilities,
        duplication=duplication,
        missing_axes=missing_axes,
        structure_gaps=structure_gaps,
        visual_work_orders=visual_orders,
        claim_evidence_invariant="preserved",
        read_only=True,
        approval_required_for=sorted(approval_ids),
    )
    validate_claim_evidence_invariant_preserved(authority.model_dump())
    return authority


# --------------------------------------------------------------------------- #
# Conclusion / introduction / abstract staged contracts
# --------------------------------------------------------------------------- #


class ConclusionWorkplan(BaseModel):
    stage: Literal["conclusion"] = "conclusion"
    derived_from: list[str] = Field(default_factory=list)
    outline: list[str] = Field(default_factory=list)
    scope_boundaries: list[str] = Field(default_factory=list)
    presentation_ir_fingerprint: str = ""


class ConclusionDraft(BaseModel):
    stage: Literal["conclusion"] = "conclusion"
    text: str = ""
    workplan_fingerprint: str = ""


class IntroductionWorkplan(BaseModel):
    stage: Literal["introduction"] = "introduction"
    promises_derived_from: list[str] = Field(default_factory=list)
    outline: list[str] = Field(default_factory=list)
    background_sources: list[str] = Field(default_factory=list)
    retrieval_proposals: list[str] = Field(default_factory=list)
    presentation_ir_fingerprint: str = ""


class IntroductionDraft(BaseModel):
    stage: Literal["introduction"] = "introduction"
    text: str = ""
    workplan_fingerprint: str = ""


class AbstractWorkplan(BaseModel):
    stage: Literal["abstract"] = "abstract"
    compresses: list[str] = Field(default_factory=list)
    outline: list[str] = Field(default_factory=list)
    presentation_ir_fingerprint: str = ""


class AbstractDraft(BaseModel):
    stage: Literal["abstract"] = "abstract"
    text: str = ""
    workplan_fingerprint: str = ""


# --------------------------------------------------------------------------- #
# Literature review presentation IR and title planner
# --------------------------------------------------------------------------- #


class LiteratureReviewPresentationIR(BaseModel):
    """Structured rhetorical context for front-matter authoring.

    All fields are optional so the IR is always fail-open: a missing or
    partially populated IR still validates and the old prompt path is
    preserved unchanged when the IR is absent.
    """

    schema_version: str = PRESENTATION_IR_SCHEMA
    central_topic: str = ""
    review_subtype: str = ""
    organizing_principle: str = ""
    synthesis_claims: list[str] = Field(default_factory=list)
    consensus: list[str] = Field(default_factory=list)
    controversies: list[str] = Field(default_factory=list)
    bottlenecks: list[str] = Field(default_factory=list)
    emerging_directions: list[str] = Field(default_factory=list)
    safe_citations: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    terminology_registry: dict[str, str] = Field(default_factory=dict)
    ir_fingerprint: str = ""
    provenance: str = ""


class TitleCandidate(BaseModel):
    rank: int = 1
    title: str = ""
    rationale: str = ""


class ReviewTitlePlan(BaseModel):
    schema_version: str = TITLE_PLAN_SCHEMA
    candidates: list[TitleCandidate] = Field(default_factory=list)
    selected_title: str = ""
    fallback_used: bool = False
    provenance: str = ""


def extract_presentation_ir(
    inputs: Mapping[str, Any],
    *,
    provenance: str = "",
) -> "LiteratureReviewPresentationIR | None":
    """Extract a presentation IR from handoff inputs. Always fail-open (returns None on any error)."""
    handoff = inputs.get("full_manuscript_handoff")
    if isinstance(handoff, Mapping):
        raw_ir = handoff.get("presentation_ir") or handoff.get("literature_review_ir")
    else:
        raw_ir = inputs.get("presentation_ir") or inputs.get("literature_review_ir")
    if not isinstance(raw_ir, Mapping):
        return None
    try:
        ir = LiteratureReviewPresentationIR.model_validate(raw_ir)
        ir.ir_fingerprint = fingerprint({
            "central_topic": ir.central_topic,
            "review_subtype": ir.review_subtype,
            "synthesis_claims": sorted(ir.synthesis_claims),
        })
        ir.provenance = provenance or "extracted_from_inputs"
        return ir
    except Exception:
        return None


def _deterministic_title_fallback(
    central_topic: str,
    review_subtype: str,
) -> list[TitleCandidate]:
    raw_topic = str(central_topic or "").strip()
    # User questions often arrive as a long instruction such as
    # "The user seeks a review of X ..." or
    # "The user requires a comprehensive scholarly literature review on X".
    # Extract only the named subject; never copy the full instruction into
    # LaTeX metadata.  We apply three passes:
    #   1. Strip a leading "The user …" prefix up to the first verb phrase.
    #   2. Extract the post-preposition subject from common "review of/on/about X" patterns.
    #   3. Truncate anything that still looks like a run-on instruction.
    topic = raw_topic
    # Pass 1: strip "The user requires/seeks/asks/wants … review [of/on/about/regarding/covering] "
    stripped = re.sub(
        r"^the\s+user\s+(?:requires?|seeks?|asks?\s+for|wants?|needs?|requests?)"
        r"[\s\w,]+"
        r"review\s+(?:of|on|about|regarding|covering|for)\s+",
        "",
        raw_topic,
        flags=re.IGNORECASE,
    ).strip()
    if stripped and stripped != raw_topic:
        topic = stripped.rstrip(" .:;,-")
    else:
        # Pass 2: classic "review of/on/about/regarding X" anywhere in the string
        match = re.search(
            r"\breview\s+(?:of|on|about|regarding|covering)\s+(.+?)"
            r"(?:\.|\s+The\s+research\s+question\b|$)",
            raw_topic,
            flags=re.IGNORECASE,
        )
        if match:
            topic = match.group(1).strip(" .:;,-")
    # Pass 3: if the extracted topic is still suspiciously long (> 120 chars)
    # it likely contains residual instruction text — keep only the first clause.
    if len(topic) > 120:
        clause = re.split(r"[,;]|\s+(?:including|such as|with|and|covering)\s+", topic, maxsplit=1)[0]
        topic = clause.strip(" .:;,-")
    topic = topic or "the field"
    subtype = str(review_subtype or "").strip().capitalize() or "Review"
    return [
        TitleCandidate(rank=1, title=f"A {subtype} of {topic}", rationale="canonical-form fallback"),
        TitleCandidate(rank=2, title=f"Advances in {topic}: A {subtype}", rationale="advances framing"),
        TitleCandidate(rank=3, title=f"{topic.capitalize()}: Current State and Future Directions", rationale="state-of-field framing"),
        TitleCandidate(rank=4, title=f"Recent Progress in {topic}: A {subtype}", rationale="progress framing"),
        TitleCandidate(rank=5, title=f"{topic.capitalize()}: Foundations, Methods, and Outlook", rationale="structure framing"),
    ]


def plan_review_titles(
    ir: "LiteratureReviewPresentationIR | None",
    *,
    candidates_from_provider: "list[Mapping[str, Any]] | None" = None,
) -> ReviewTitlePlan:
    """Return a title plan with 3-5 candidates; falls back deterministically when provider output is absent or thin."""
    if candidates_from_provider:
        parsed: list[TitleCandidate] = []
        for index, raw in enumerate(candidates_from_provider[:5], start=1):
            if not isinstance(raw, Mapping):
                continue
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            parsed.append(TitleCandidate(
                rank=int(raw.get("rank") or index),
                title=title,
                rationale=str(raw.get("rationale") or ""),
            ))
        if len(parsed) >= 3:
            return ReviewTitlePlan(
                candidates=parsed,
                selected_title=parsed[0].title,
                fallback_used=False,
                provenance="provider",
            )
    topic = str((ir.central_topic if ir else "") or "")
    subtype = str((ir.review_subtype if ir else "") or "")
    fallback = _deterministic_title_fallback(topic, subtype)
    return ReviewTitlePlan(
        candidates=fallback,
        selected_title=fallback[0].title,
        fallback_used=True,
        provenance="deterministic_fallback",
    )


# --------------------------------------------------------------------------- #
# Whole-manuscript review and bounded patches
# --------------------------------------------------------------------------- #


class ManuscriptReviewFinding(BaseModel):
    finding_id: str = ""
    issue_key: str = ""
    issue_type: str = ""
    target_ids: list[str] = Field(default_factory=list)
    dimension: str = ""
    severity: str = "advisory"
    consensus: str = "none"
    statement: str = ""
    advisory: bool = True
    blocking: bool = False


class ManuscriptReviewReport(BaseModel):
    stage: Literal["whole_manuscript_review"] = "whole_manuscript_review"
    findings: list[ManuscriptReviewFinding] = Field(default_factory=list)
    fail_open: bool = True


def is_blocking_finding(finding: ManuscriptReviewFinding) -> bool:
    """Only findings the local aggregator marked blocking are blocking.

    The local runtime owns the blocking decision: it requires full
    multi-reviewer consensus on the same canonical issue with every reviewer
    assigning critical severity.  Raw/ordinary findings default to fail-open.
    """

    return bool(finding.blocking)


def findings_are_fail_open(findings: Sequence[ManuscriptReviewFinding]) -> bool:
    return not any(is_blocking_finding(finding) for finding in findings)


class ReviewerRole(BaseModel):
    """One independent reviewer/editor role with its own findings."""

    reviewer_id: str = ""
    role: str = ""
    findings: list[ManuscriptReviewFinding] = Field(default_factory=list)


class MultiReviewerReport(BaseModel):
    """Multiple independent reviewers for the whole-manuscript stage."""

    stage: Literal["whole_manuscript_review"] = "whole_manuscript_review"
    reviewers: list[ReviewerRole] = Field(default_factory=list)


_SEVERITY_RANK = {"advisory": 0, "minor": 1, "major": 2, "critical": 3}


def _severity_rank(severity: Any) -> int:
    return _SEVERITY_RANK.get(str(severity or "").strip().casefold(), 0)


def _finding_key(finding: ManuscriptReviewFinding) -> str:
    if finding.finding_id:
        return "id:" + finding.finding_id
    issue_type = str(finding.issue_type or "").strip().casefold()
    target_ids = sorted(
        str(target) for target in finding.target_ids if str(target).strip()
    )
    if issue_type or target_ids:
        return (
            "issue_type:"
            + issue_type
            + "|targets:"
            + ",".join(target_ids).casefold()
        )
    issue_key = str(finding.issue_key or "").strip()
    if issue_key:
        return "issue_key:" + issue_key.casefold()
    statement = re.sub(r"\s+", " ", str(finding.statement or "")).casefold()
    return f"{finding.dimension}|{statement}"


def _canonical_issue_identity(
    findings: Sequence[ManuscriptReviewFinding],
) -> tuple[str, str, list[str]]:
    """Locally normalize one aggregated finding's canonical issue identity.

    Deterministic ``issue_type`` plus sorted ``target_ids`` win whenever
    present; the free-text ``issue_key`` is only a fallback.  The returned
    ``issue_key`` is always generated locally from the canonical fields.
    """

    representative = max(
        findings, key=lambda item: _severity_rank(item.severity)
    )
    issue_type = str(representative.issue_type or "").strip().casefold()
    target_ids = sorted(
        {
            str(target).strip()
            for finding in findings
            for target in finding.target_ids
            if str(target).strip()
        }
    )
    if issue_type:
        issue_key = issue_type + (
            ":" + ",".join(target_ids) if target_ids else ""
        )
    elif target_ids:
        issue_key = "targets:" + ",".join(target_ids)
    else:
        issue_key = str(representative.issue_key or "").strip()
    return issue_key, issue_type, target_ids


def aggregate_multi_reviewer_report(
    report: MultiReviewerReport,
) -> ManuscriptReviewReport:
    """Aggregate independent reviewer findings with deterministic consensus.

    Findings are grouped by stable identity: canonical ``issue_type`` plus
    normalized ``target_ids`` first, free-text ``issue_key`` only as a
    fallback, then the dimension/statement fallback.  The local runtime owns
    finding identity and status fields: caller/model-provided
    ``consensus``, ``blocking``, and ``advisory`` values are ignored and
    recomputed here, and the aggregated ``issue_key`` is generated locally
    from the preserved ``issue_type``/``target_ids``.  Blocking requires
    every configured reviewer (counted by distinct reviewer identity, never
    raw finding count) to independently flag the same canonical issue (full
    consensus) AND every one of them to assign critical severity; a
    critical/split-severity group stays advisory/fail-open.  Duplicate
    same-reviewer findings for one canonical issue are merged into a single
    vote (higher severity wins), so one reviewer can never cast multiple
    votes.  Blank or duplicate ``reviewer_id`` values receive a local stable
    reviewer slot so separately configured reviewers stay independent.
    ``advisory`` is derived from blocking so nonblocking critical findings
    remain visible as advisory.
    """

    reviewers = list(report.reviewers)
    seen_ids: set[str] = set()
    reviewer_vote_ids: list[str] = []
    for index, reviewer in enumerate(reviewers):
        reviewer_id = str(reviewer.reviewer_id or "").strip()
        if reviewer_id and reviewer_id not in seen_ids:
            seen_ids.add(reviewer_id)
            reviewer_vote_ids.append(reviewer_id)
        else:
            reviewer_vote_ids.append(f"__reviewer_slot_{index}")
    reviewer_count = len(reviewer_vote_ids)
    groups: dict[str, dict[str, ManuscriptReviewFinding]] = {}
    for reviewer, vote_id in zip(reviewers, reviewer_vote_ids):
        for finding in reviewer.findings:
            key = _finding_key(finding)
            votes = groups.setdefault(key, {})
            existing = votes.get(vote_id)
            if existing is None or _severity_rank(
                finding.severity
            ) > _severity_rank(existing.severity):
                votes[vote_id] = finding
    aggregated: list[ManuscriptReviewFinding] = []
    for findings in (list(votes.values()) for votes in groups.values()):
        representative = max(findings, key=lambda item: _severity_rank(item.severity))
        severity = str(representative.severity).strip().casefold()
        issue_key, issue_type, target_ids = _canonical_issue_identity(
            findings
        )
        count = len(findings)
        if reviewer_count == 0 or count == 0:
            consensus = "none"
        elif count == reviewer_count and reviewer_count >= 2:
            consensus = "consensus"
        elif count * 2 > reviewer_count:
            consensus = "majority"
        else:
            consensus = "split"
        all_critical = all(
            str(finding.severity or "").strip().casefold() == "critical"
            for finding in findings
        )
        blocking = consensus == "consensus" and all_critical
        aggregated.append(
            ManuscriptReviewFinding(
                finding_id=representative.finding_id,
                issue_key=issue_key,
                issue_type=issue_type,
                target_ids=target_ids,
                dimension=representative.dimension,
                severity=severity,
                consensus=consensus,
                statement=representative.statement,
                advisory=not blocking,
                blocking=blocking,
            )
        )
    aggregated.sort(
        key=lambda finding: (
            -_severity_rank(finding.severity),
            finding.consensus,
            finding.finding_id,
        )
    )
    for index, finding in enumerate(aggregated, start=1):
        if not finding.finding_id:
            finding.finding_id = f"GMR-{index:03d}"
    return ManuscriptReviewReport(
        findings=aggregated,
        fail_open=not any(finding.blocking for finding in aggregated),
    )


class BoundedPatchProposal(BaseModel):
    patch_id: str = ""
    operation: str = ""
    target: str = ""
    rationale: str = ""
    approval_required: bool = False
    claim_text_change: bool = False
    evidence_change: bool = False
    provenance: str = ""
    status: str = "proposed"


class PatchProposalSet(BaseModel):
    stage: Literal["bounded_patch_proposals"] = "bounded_patch_proposals"
    proposals: list[BoundedPatchProposal] = Field(default_factory=list)
    approval_required: bool = False


def proposal_requires_approval(raw: Mapping[str, Any]) -> bool:
    """Return True when a patch proposal needs explicit human approval.

    Pure editorial transition rewrites (``rewrite_transition``) that
    explicitly preserve claim text and evidence are rhetoric-only and do not
    gate the publication mainline.  Delete/merge/ownership/claim-strength/
    evidence-changing operations and any proposal that explicitly declares
    ``approval_required=true`` or a claim/evidence change still require
    approval.
    """

    if bool(raw.get("approval_required")):
        return True
    if bool(raw.get("claim_text_change")) or bool(raw.get("evidence_change")):
        return True
    operation = str(
        raw.get("operation") or raw.get("operation_type") or ""
    ).strip().casefold()
    return operation in SEMANTIC_PATCH_OPERATIONS


def normalize_patch_proposal(
    raw: Mapping[str, Any],
    *,
    patch_id: str = "",
) -> BoundedPatchProposal:
    """Normalize one patch proposal, forcing approval for semantic changes."""

    operation = str(
        raw.get("operation") or raw.get("operation_type") or ""
    ).strip().casefold()
    proposal = BoundedPatchProposal(
        patch_id=str(raw.get("patch_id") or patch_id),
        operation=operation,
        target=str(raw.get("target") or ""),
        rationale=str(raw.get("rationale") or ""),
        approval_required=bool(raw.get("approval_required")),
        claim_text_change=bool(raw.get("claim_text_change")),
        evidence_change=bool(raw.get("evidence_change")),
        provenance=str(raw.get("provenance") or ""),
        status=str(raw.get("status") or "proposed"),
    )
    proposal.approval_required = proposal_requires_approval(raw)
    return proposal


def patch_set_requires_approval(
    proposals: Sequence[Mapping[str, Any] | BoundedPatchProposal],
) -> list[str]:
    """Return patch ids that require explicit approval."""

    approval_ids: list[str] = []
    for raw in proposals:
        proposal = (
            raw
            if isinstance(raw, BoundedPatchProposal)
            else normalize_patch_proposal(raw)
        )
        if proposal.approval_required:
            approval_ids.append(
                proposal.patch_id or f"unnamed-{len(approval_ids) + 1}"
            )
    return sorted(approval_ids)


# --------------------------------------------------------------------------- #
# Editorial revision: bounded materialization of reviewer/commander edits
# --------------------------------------------------------------------------- #


FINDING_KIND_BY_ISSUE_TYPE = {
    "missing_transition": ("transition", "paragraph"),
    "ordering_break": ("transition", "paragraph"),
    "duplicated_explanation": ("cross_reference", "paragraph"),
    "undefined_term": ("terminology_clarification", "paragraph"),
}
FINDING_KIND_BY_DIMENSION = {
    "continuity": ("transition", "paragraph"),
    "reader_flow": ("transition", "paragraph"),
    "clarity": ("terminology_clarification", "paragraph"),
    "overlap": ("cross_reference", "paragraph"),
}
EDITORIAL_OPERATION_KIND = {
    "rewrite_transition": ("transition", "paragraph"),
}


class EditorialWorkItem(BaseModel):
    """One bounded author task: target block + neighbors + editorial anchor."""

    work_item_id: str = ""
    editorial_kind: str = ""
    unit: str = "paragraph"
    target_block_id: str = ""
    target_kind: str = "block"
    section_id: str = ""
    section_title: str = ""
    chapter_thesis: str = ""
    reader_takeaway: str = ""
    original_text: str = ""
    previous_text: str = ""
    next_text: str = ""
    finding: dict[str, Any] = Field(default_factory=dict)
    patch_proposal: dict[str, Any] = Field(default_factory=dict)


class EditorialVerification(BaseModel):
    """Independent verifier verdict for one revised block."""

    meaning_preserved: bool = False
    scope_preserved: bool = False
    citations_preserved: bool = False
    numbers_conditions_preserved: bool = False
    problem_improved: bool = False
    notes: str = ""


class EditorialRevisionRecord(BaseModel):
    """Audit trail for one bounded editorial work item."""

    work_item_id: str = ""
    target_block_id: str = ""
    editorial_kind: str = ""
    unit: str = "paragraph"
    source_finding: str = ""
    source_patch: str = ""
    original_text: str = ""
    revised_text: str = ""
    original_sha256: str = ""
    revised_sha256: str = ""
    status: str = "rejected"
    reason: str = ""
    application_status: str = "not_applicable"
    application_reason: str = ""
    author_usage: dict[str, Any] = Field(default_factory=dict)
    verifier_usage: dict[str, Any] = Field(default_factory=dict)


class EditorialRevisionAudit(BaseModel):
    """Local audit payload for the editorial revision stage."""

    stage: Literal["editorial_revision"] = "editorial_revision"
    work_item_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    applied_revision_ids: list[str] = Field(default_factory=list)
    unapplied_accepted_revision_ids: list[str] = Field(default_factory=list)
    blocking_unresolved: list[dict[str, Any]] = Field(default_factory=list)
    records: list[EditorialRevisionRecord] = Field(default_factory=list)
    review_findings_kept: list[dict[str, Any]] = Field(default_factory=list)
    unselected: list[dict[str, Any]] = Field(default_factory=list)
    checkpoint_fingerprint: str = ""
    resumed_from_checkpoint: bool = False


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def ref_marker_multiset(text: str) -> list[str]:
    """Return the exact ``[REF:...]`` marker tokens in a text (multiset)."""

    return [
        match.group(0)
        for match in _REF_MARKER_PATTERN.finditer(str(text or ""))
    ]


def ref_markers_preserved(original: str, revised: str) -> bool:
    """Replacement must keep the exact REF marker sequence and multiplicity."""

    return ref_marker_multiset(original) == ref_marker_multiset(revised)


_VERIFIER_FLAG_KEYS = (
    "meaning_preserved",
    "scope_preserved",
    "citations_preserved",
    "numbers_conditions_preserved",
    "problem_improved",
)


def verifier_accepts(payload: Mapping[str, Any]) -> bool:
    """Local acceptance rule: every verifier preservation flag must be true."""

    return all(bool(payload.get(key)) for key in _VERIFIER_FLAG_KEYS)


def _artifact_draft_text(record: Mapping[str, Any]) -> str:
    payload = _mapping(record.get("payload"))
    draft = _mapping(payload.get("draft"))
    return str(draft.get("text") or "")


def _strip_matching_leading_heading(text: str, heading: str) -> str:
    """Remove only an initial Markdown heading equal to ``heading``."""

    value = str(text or "")
    match = re.match(
        r"\A[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n|\Z)",
        value,
    )
    if not match:
        return value
    actual = _normalized_spaces(match.group(1)).casefold()
    expected = _normalized_spaces(heading).casefold()
    if not expected or actual != expected:
        return value
    return value[match.end() :].lstrip("\r\n")


def _markdown_body_paragraphs(text: str, heading: str = "") -> list[str]:
    """Return exact non-heading Markdown paragraphs from one section."""

    body = _strip_matching_leading_heading(text, heading)
    return [
        part.strip()
        for part in re.split(r"(?:\r?\n)[ \t]*(?:\r?\n)+", body)
        if part.strip() and not re.match(r"^[ \t]{0,3}#{1,6}[ \t]+", part)
    ]


def _normalized_without_ref_markers(text: str) -> str:
    value = _REF_MARKER_PATTERN.sub("", str(text or ""))
    # REF markers are often inserted immediately before punctuation.  Removing
    # the marker leaves a cosmetic space that must not make an otherwise exact
    # block/paragraph alignment look ambiguous.
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return _normalized_spaces(value)


def _citation_rich_block_texts(
    section: Mapping[str, Any],
) -> dict[str, str]:
    """Uniquely align citation-free block prose to cited full-text paragraphs.

    Alignment intentionally fails closed per block: ambiguous or missing
    matches are omitted, so assembly retains the authoritative original.
    """

    blocks = [
        block
        for block in section.get("blocks") or []
        if isinstance(block, Mapping) and str(block.get("block_id") or "")
    ]
    full_text = str(section.get("full_text") or "")
    if not blocks or not full_text.strip():
        return {}
    paragraphs = _markdown_body_paragraphs(
        full_text, str(section.get("section_title") or "")
    )
    paragraph_keys = [
        _normalized_without_ref_markers(paragraph) for paragraph in paragraphs
    ]
    aligned: dict[str, str] = {}
    claimed_indexes: set[int] = set()
    for block in blocks:
        block_id = str(block.get("block_id") or "")
        block_key = _normalized_without_ref_markers(
            str(block.get("prose") or "")
        )
        candidates = [
            index
            for index, paragraph_key in enumerate(paragraph_keys)
            if block_key and paragraph_key == block_key
        ]
        if len(candidates) != 1 or candidates[0] in claimed_indexes:
            continue
        paragraph_index = candidates[0]
        claimed_indexes.add(paragraph_index)
        aligned[block_id] = paragraphs[paragraph_index]
    return aligned


def _selected_section_order(
    inputs: Mapping[str, Any],
) -> list[str]:
    """Commander-selected section order (fallback to explicit order)."""

    commander = _mapping(inputs.get("commander_structure"))
    proposed = commander.get("proposed_section_order") or []
    order: list[str] = []
    for entry in proposed:
        if isinstance(entry, Mapping):
            section_id = str(entry.get("section_id") or "")
        else:
            section_id = str(entry)
        if section_id:
            order.append(section_id)
    if not order:
        order = [str(section_id) for section_id in inputs.get("section_order") or []]
    return order


def _index_targets(
    sections: Sequence[Mapping[str, Any]],
    front_back: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Index stable targets: body blocks plus front/back artifact texts."""

    targets: dict[str, dict[str, Any]] = {}
    for section in sections:
        section_id = str(section.get("section_id") or "")
        title = str(section.get("section_title") or "")
        thesis = str(section.get("chapter_thesis") or "")
        takeaway = str(section.get("reader_takeaway") or "")
        blocks = [
            block
            for block in section.get("blocks") or []
            if isinstance(block, Mapping)
        ]
        citation_rich = _citation_rich_block_texts(section)
        effective_texts = [
            citation_rich.get(
                str(block.get("block_id") or ""),
                str(block.get("prose") or ""),
            )
            for block in blocks
        ]
        for index, block in enumerate(blocks):
            block_id = str(block.get("block_id") or "")
            if not block_id:
                continue
            targets[block_id] = {
                "target_kind": "block",
                "section_id": section_id,
                "section_title": title,
                "chapter_thesis": thesis,
                "reader_takeaway": takeaway,
                "original_text": effective_texts[index],
                "previous_text": (
                    effective_texts[index - 1]
                    if index > 0
                    else ""
                ),
                "next_text": (
                    effective_texts[index + 1]
                    if index + 1 < len(blocks)
                    else ""
                ),
            }
        if not blocks and section_id:
            full_text = str(section.get("full_text") or "")
            if full_text:
                targets[section_id] = {
                    "target_kind": "block",
                    "section_id": section_id,
                    "section_title": title,
                    "chapter_thesis": thesis,
                    "reader_takeaway": takeaway,
                    "original_text": full_text,
                    "previous_text": "",
                    "next_text": "",
                }
    for name, text in front_back.items():
        targets[name] = {
            "target_kind": "artifact",
            "section_id": name,
            "section_title": name,
            "chapter_thesis": "",
            "reader_takeaway": "",
            "original_text": str(text or ""),
            "previous_text": "",
            "next_text": "",
        }
    return targets


def _section_blocks(
    section_id: str, sections: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    for section in sections:
        if str(section.get("section_id") or "") != section_id:
            continue
        return [
            block
            for block in section.get("blocks") or []
            if isinstance(block, Mapping) and str(block.get("block_id"))
        ]
    return []


def _section_first_block_id(
    section_id: str, sections: Sequence[Mapping[str, Any]]
) -> str | None:
    blocks = _section_blocks(section_id, sections)
    return str(blocks[0].get("block_id")) if blocks else None


def _section_last_block_prose(
    section_id: str, sections: Sequence[Mapping[str, Any]]
) -> str:
    blocks = _section_blocks(section_id, sections)
    return str(blocks[-1].get("prose") or "") if blocks else ""


def _resolve_target(
    target_id: str,
    targets: Mapping[str, Mapping[str, Any]],
    sections: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Resolve one non-transition target to at most one block."""

    target_id = str(target_id or "").strip()
    if target_id in targets:
        return [target_id]
    for section in sections:
        if str(section.get("section_id") or "") != target_id:
            continue
        block_ids = [
            str(block.get("block_id"))
            for block in section.get("blocks") or []
            if isinstance(block, Mapping) and str(block.get("block_id"))
        ]
        if not block_ids:
            return [target_id] if target_id in targets else []
        return [block_ids[0]]
    return []


def _resolve_transition_boundary(
    target_ids: Sequence[str],
    targets: Mapping[str, Mapping[str, Any]],
    sections: Sequence[Mapping[str, Any]],
) -> list[tuple[str, str | None]] | None:
    """Resolve section-level transition targets to at most one boundary edit.

    Two section targets mean one boundary: the first body block of the
    later/destination section, with the last body block of the source as
    ``previous_text``.  A single-section target becomes one conservative
    edit on its first block using the prior section's last block; if that
    context is unavailable the target remains advisory (None).  Explicit
    block targets resolve to themselves.  More than two section targets are
    ambiguous and remain unresolved.
    """

    section_ids_in_order = [
        str(section.get("section_id")) for section in sections
    ]
    block_targets: list[str] = []
    section_targets: list[str] = []
    for raw in target_ids:
        target = str(raw or "").strip()
        if not target:
            continue
        target_record = targets.get(target)
        if target_record is not None and str(
            target_record.get("target_kind") or ""
        ) == "block":
            block_targets.append(target)
        elif target in section_ids_in_order:
            section_targets.append(target)
    if block_targets:
        return [
            (block_id, None)
            for block_id in dict.fromkeys(block_targets)
        ]
    section_targets = list(dict.fromkeys(section_targets))
    if len(section_targets) == 2:
        first_index = section_ids_in_order.index(section_targets[0])
        second_index = section_ids_in_order.index(section_targets[1])
        if first_index == second_index:
            return None
        if first_index < second_index:
            source_id = section_targets[0]
            destination_id = section_targets[1]
        else:
            source_id = section_targets[1]
            destination_id = section_targets[0]
        destination_first = _section_first_block_id(
            destination_id, sections
        )
        if destination_first is None:
            return None
        return [
            (
                destination_first,
                _section_last_block_prose(source_id, sections),
            )
        ]
    if len(section_targets) == 1:
        section_id = section_targets[0]
        section_index = section_ids_in_order.index(section_id)
        destination_first = _section_first_block_id(section_id, sections)
        if destination_first is None:
            return None
        previous_text = ""
        if section_index > 0:
            previous_text = _section_last_block_prose(
                section_ids_in_order[section_index - 1], sections
            )
        if not previous_text:
            return None
        return [(destination_first, previous_text)]
    return None


_ARTIFACT_DOCUMENT_ORDER = {
    "abstract": (0, 0),
    "introduction": (1, 0),
    "conclusion": (100, 0),
}


def _document_order_key(
    target_id: str,
    targets: Mapping[str, Mapping[str, Any]],
    sections: Sequence[Mapping[str, Any]],
) -> tuple[int, int] | None:
    """Document-order position of a resolvable target.

    Document order is defined by the current section sequence and each
    section's stable block order (front/back artifacts are pinned around the
    body).  Unresolvable targets return None.
    """

    record = targets.get(target_id)
    if record is None:
        return None
    if str(record.get("target_kind") or "") == "artifact":
        return _ARTIFACT_DOCUMENT_ORDER.get(target_id, (50, 0))
    section_ids_in_order = [
        str(section.get("section_id")) for section in sections
    ]
    section_id = str(record.get("section_id") or "")
    if section_id not in section_ids_in_order:
        return None
    section_index = section_ids_in_order.index(section_id)
    block_ids = [
        str(block.get("block_id"))
        for block in _section_blocks(section_id, sections)
    ]
    if target_id in block_ids:
        return (2 + section_index, block_ids.index(target_id))
    return (2 + section_index, -1)


def _finding_source_label(finding: Mapping[str, Any]) -> str:
    if not finding:
        return ""
    return str(
        finding.get("issue_key") or finding.get("finding_id") or ""
    )


def _patch_source_label(proposal: Mapping[str, Any]) -> str:
    if not proposal:
        return ""
    return str(
        proposal.get("patch_id") or proposal.get("provenance") or ""
    )


_AUTO_EDIT_SEVERITIES = frozenset({"major", "critical"})
_EDITORIAL_KIND_ORDER = (
    "transition",
    "cross_reference",
    "terminology_clarification",
    "connective_or_coreference",
)


def _kind_order(kind: str) -> int:
    try:
        return _EDITORIAL_KIND_ORDER.index(kind)
    except ValueError:
        return len(_EDITORIAL_KIND_ORDER)


def _candidate_priority(
    candidate: Mapping[str, Any],
    targets: Mapping[str, Mapping[str, Any]],
    sections: Sequence[Mapping[str, Any]],
) -> tuple[int, tuple[int, int], int, str]:
    """Deterministic winner key: severity, document order, kind, source id."""

    document_key = _document_order_key(
        str(candidate.get("target") or ""), targets, sections
    )
    if document_key is None:
        document_key = (2_000_000, 0)
    source = _mapping(candidate.get("source"))
    if str(candidate.get("source_kind") or "") == "proposal":
        source_id = str(
            source.get("patch_id") or source.get("provenance") or ""
        )
    else:
        source_id = _finding_source_label(source)
    return (
        int(candidate.get("severity_rank") or 0),
        document_key,
        _kind_order(str(candidate.get("kind") or "")),
        source_id,
    )


def _unselected_record(
    *,
    source_kind: str,
    source: Mapping[str, Any],
    target_block_id: str,
    kind: str,
    reason: str,
    severity: str = "",
) -> dict[str, Any]:
    if source_kind == "proposal":
        source_id = str(
            source.get("patch_id") or source.get("provenance") or ""
        )
    else:
        source_id = _finding_source_label(source)
    return {
        "source_kind": source_kind,
        "source_id": source_id,
        "target_block_id": target_block_id,
        "kind": kind,
        "reason": reason,
        "severity": severity,
    }


def plan_editorial_work_items_with_provenance(
    *,
    sections: Sequence[Mapping[str, Any]],
    front_back: Mapping[str, str],
    findings: Sequence[Mapping[str, Any]],
    proposals: Sequence[Mapping[str, Any]],
) -> tuple[list[EditorialWorkItem], list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert actionable proposals/findings into bounded work items.

    Only auto-editorial operations and major/critical findings with
    resolvable targets become work items; advisory/minor findings are kept
    in the returned ``unselected`` provenance instead.  Each finding
    materializes at most ONE work item (cross-reference/duplication finds
    the latest resolvable body target; terminology-clarification finds the
    earliest).  At most ONE work item is planned per target block across all
    kinds: an explicit rewrite_transition proposal wins over reviewer
    findings, otherwise higher severity wins, then stable document/kind
    order; losing candidates are returned in ``unselected``.  Critical
    full-consensus findings that cannot be materialized are returned as
    ``blocking_unresolved`` for manual handling.
    """

    targets = _index_targets(sections, front_back)
    blocking_unresolved: list[dict[str, Any]] = []
    unselected: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    def record_unresolved(finding: Mapping[str, Any]) -> None:
        severity = str(finding.get("severity") or "").strip().casefold()
        consensus = str(finding.get("consensus") or "").strip().casefold()
        if severity == "critical" and consensus == "consensus":
            blocking_unresolved.append(
                {
                    "finding_id": str(finding.get("finding_id") or ""),
                    "issue_key": str(finding.get("issue_key") or ""),
                    "severity": severity,
                    "statement": str(finding.get("statement") or ""),
                    "reason": "no_materializable_auto_target",
                }
            )
        else:
            unselected.append(
                _unselected_record(
                    source_kind="finding",
                    source=finding,
                    target_block_id="",
                    kind="",
                    reason="no_materializable_auto_target",
                    severity=severity,
                )
            )

    for proposal in proposals:
        operation = str(proposal.get("operation") or "").strip().casefold()
        kind_info = EDITORIAL_OPERATION_KIND.get(operation)
        if not kind_info:
            continue
        target_id = str(proposal.get("target") or "").strip()
        if not target_id:
            continue
        resolved = _resolve_target(target_id, targets, sections)
        if not resolved:
            unselected.append(
                _unselected_record(
                    source_kind="proposal",
                    source=proposal,
                    target_block_id=target_id,
                    kind=kind_info[0],
                    reason="unresolved_target",
                )
            )
            continue
        for target_block_id in resolved:
            candidates.append(
                {
                    "source_kind": "proposal",
                    "source": dict(proposal),
                    "kind": kind_info[0],
                    "unit": kind_info[1],
                    "target": target_block_id,
                    "previous_text_override": None,
                    "severity_rank": 10,
                }
            )

    for finding in findings:
        finding = dict(finding)
        issue_type = str(finding.get("issue_type") or "").strip().casefold()
        dimension = str(finding.get("dimension") or "").strip().casefold()
        kind_info = FINDING_KIND_BY_ISSUE_TYPE.get(
            issue_type
        ) or FINDING_KIND_BY_DIMENSION.get(dimension)
        severity = str(finding.get("severity") or "").strip().casefold()
        target_ids = [
            str(target)
            for target in finding.get("target_ids") or []
            if str(target).strip()
        ]
        if not kind_info or not target_ids:
            record_unresolved(finding)
            continue
        if severity not in _AUTO_EDIT_SEVERITIES:
            unselected.append(
                _unselected_record(
                    source_kind="finding",
                    source=finding,
                    target_block_id=",".join(target_ids),
                    kind=kind_info[0],
                    reason="severity_too_low",
                    severity=severity,
                )
            )
            continue
        kind, unit = kind_info
        selected: list[tuple[str, str | None]] = []
        if kind in {"transition", "connective_or_coreference"}:
            boundaries = _resolve_transition_boundary(
                target_ids, targets, sections
            )
            if boundaries is None:
                record_unresolved(finding)
                continue
            selected = boundaries
        else:
            resolved_candidates: list[str] = []
            for target_id in target_ids:
                for resolved in _resolve_target(
                    target_id, targets, sections
                ):
                    if resolved not in resolved_candidates:
                        resolved_candidates.append(resolved)
            positioned = [
                (candidate, key)
                for candidate in resolved_candidates
                if (
                    key := _document_order_key(
                        candidate, targets, sections
                    )
                )
                is not None
            ]
            if not positioned:
                record_unresolved(finding)
                continue
            if kind == "cross_reference":
                target_block_id = max(
                    positioned, key=lambda item: item[1]
                )[0]
            else:
                target_block_id = min(
                    positioned, key=lambda item: item[1]
                )[0]
            selected = [(target_block_id, None)]
        for target_block_id, previous_text_override in selected:
            candidates.append(
                {
                    "source_kind": "finding",
                    "source": finding,
                    "kind": kind,
                    "unit": unit,
                    "target": target_block_id,
                    "previous_text_override": previous_text_override,
                    "severity_rank": _severity_rank(severity),
                }
            )

    by_target: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_target.setdefault(str(candidate["target"]), []).append(candidate)

    winners: list[dict[str, Any]] = []
    for target_block_id, group in by_target.items():
        proposals_on_target = [
            candidate
            for candidate in group
            if candidate["source_kind"] == "proposal"
        ]
        if proposals_on_target:
            chosen = proposals_on_target[0]
            losers = proposals_on_target[1:] + [
                candidate
                for candidate in group
                if candidate["source_kind"] != "proposal"
            ]
        else:
            chosen = max(
                group,
                key=lambda candidate: _candidate_priority(
                    candidate, targets, sections
                ),
            )
            losers = [candidate for candidate in group if candidate is not chosen]
        winners.append(chosen)
        for loser in losers:
            loser_source = _mapping(loser.get("source"))
            unselected.append(
                _unselected_record(
                    source_kind=loser["source_kind"],
                    source=loser["source"],
                    target_block_id=target_block_id,
                    kind=loser["kind"],
                    reason="target_collision",
                    severity=str(loser_source.get("severity") or ""),
                )
            )

    winners.sort(
        key=lambda candidate: (
            _document_order_key(
                str(candidate["target"]), targets, sections
            )
            or (2_000_000, 0),
            _kind_order(str(candidate.get("kind") or "")),
            _candidate_priority(candidate, targets, sections)[3],
        )
    )
    work_items: list[EditorialWorkItem] = []
    for index, candidate in enumerate(winners, start=1):
        target = targets[str(candidate["target"])]
        item = EditorialWorkItem(
            work_item_id=f"ER-{index:03d}",
            editorial_kind=str(candidate["kind"]),
            unit=str(candidate["unit"]),
            target_block_id=str(candidate["target"]),
            target_kind=str(target.get("target_kind") or "block"),
            section_id=str(target.get("section_id") or ""),
            section_title=str(target.get("section_title") or ""),
            chapter_thesis=str(target.get("chapter_thesis") or ""),
            reader_takeaway=str(target.get("reader_takeaway") or ""),
            original_text=str(target.get("original_text") or ""),
            previous_text=(
                candidate["previous_text_override"]
                if candidate["previous_text_override"] is not None
                else str(target.get("previous_text") or "")
            ),
            next_text=str(target.get("next_text") or ""),
        )
        if candidate["source_kind"] == "proposal":
            item.patch_proposal = dict(candidate["source"])
        else:
            item.finding = dict(candidate["source"])
        work_items.append(item)
    return work_items, blocking_unresolved, unselected


def plan_editorial_work_items(
    *,
    sections: Sequence[Mapping[str, Any]],
    front_back: Mapping[str, str],
    findings: Sequence[Mapping[str, Any]],
    proposals: Sequence[Mapping[str, Any]],
) -> tuple[list[EditorialWorkItem], list[dict[str, Any]]]:
    """Backward-compatible wrapper returning (work items, unresolved)."""

    work_items, blocking_unresolved, _ = plan_editorial_work_items_with_provenance(
        sections=sections,
        front_back=front_back,
        findings=findings,
        proposals=proposals,
    )
    return work_items, blocking_unresolved


def _normalized_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _blocks_cover_full_text(
    blocks: Sequence[Mapping[str, Any]], full_text: str
) -> bool:
    """Block assembly must reconstruct the section's full_text."""

    if not str(full_text or "").strip():
        return not blocks
    joined = "\n\n".join(
        str(block.get("prose") or "") for block in blocks
    )
    return _normalized_spaces(joined) == _normalized_spaces(full_text)


def assemble_revised_manuscript(
    *,
    sections: Sequence[Mapping[str, Any]],
    front_back: Mapping[str, str],
    revisions: Mapping[str, str],
    section_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Assemble a complete manuscript with accepted revisions applied.

    Sections follow the commander-selected ``section_order``; visible
    headings are injected exactly once for Abstract, Introduction, every
    section, and Conclusion.  When a section's EXPLANATION_BLOCKS do not
    reconstruct its final ``full_text``, the full_text is authoritative:
    edits are applied only to exact, uniquely locatable block prose and the
    complete original section text is retained otherwise.
    """

    by_id = {str(section.get("section_id")): section for section in sections}
    order: list[str] = []
    for section_id in section_order or [
        str(section.get("section_id")) for section in sections
    ]:
        section_id = str(section_id or "").strip()
        if section_id and section_id not in order:
            order.append(section_id)
    assembled: list[dict[str, Any]] = []
    revision_applications: dict[str, dict[str, str]] = {}

    def applied(target_id: str) -> None:
        revision_applications[target_id] = {
            "status": "applied",
            "reason": "unique_target_and_ref_sequence_preserved",
        }

    def unapplied(target_id: str, reason: str) -> None:
        revision_applications[target_id] = {
            "status": "unapplied",
            "reason": reason,
        }

    for name, heading in (
        ("abstract", "Abstract"),
        ("introduction", "Introduction"),
    ):
        if name in front_back:
            original = str(front_back[name] or "")
            text = original
            revised = revisions.get(name)
            if revised and revised != original:
                if ref_markers_preserved(original, revised):
                    text = revised
                    applied(name)
                else:
                    unapplied(name, "ref_marker_sequence_changed")
            text = _strip_matching_leading_heading(text, heading)
            assembled.append(
                {
                    "kind": name,
                    "target_id": name,
                    "heading": heading,
                    "title": heading,
                    "text": text,
                }
            )
    for section_id in order:
        section = by_id.get(section_id)
        if section is None:
            continue
        heading = str(section.get("section_title") or "") or section_id
        blocks = [
            block
            for block in section.get("blocks") or []
            if isinstance(block, Mapping) and str(block.get("block_id"))
        ]
        raw_full_text = str(section.get("full_text") or "")
        full_text = _strip_matching_leading_heading(raw_full_text, heading)
        if blocks and full_text:
            section_text = full_text
            citation_rich = _citation_rich_block_texts(section)
            applied_block_revisions: list[str] = []
            unapplied_block_revisions: list[dict[str, str]] = []
            for block in blocks:
                block_id = str(block.get("block_id"))
                revised = revisions.get(block_id)
                if not revised:
                    continue
                original = citation_rich.get(block_id)
                reason = ""
                if not original:
                    reason = "citation_rich_target_not_uniquely_aligned"
                elif revised == original:
                    reason = "revision_is_no_change"
                elif not ref_markers_preserved(original, revised):
                    reason = "ref_marker_sequence_changed"
                elif section_text.count(original) != 1:
                    reason = "authoritative_target_not_unique"
                if reason:
                    unapplied(block_id, reason)
                    unapplied_block_revisions.append(
                        {"block_id": block_id, "reason": reason}
                    )
                    continue
                section_text = section_text.replace(original, revised, 1)
                applied(block_id)
                applied_block_revisions.append(block_id)
            assembled.append(
                {
                    "kind": "section",
                    "target_id": section_id,
                    "heading": heading,
                    "title": heading,
                    "full_text_authority": True,
                    "text": section_text,
                    "applied_block_revisions": applied_block_revisions,
                    "unapplied_block_revisions": unapplied_block_revisions,
                }
            )
            continue
        if blocks:
            section_blocks: list[dict[str, Any]] = []
            applied_block_revisions: list[str] = []
            unapplied_block_revisions: list[dict[str, str]] = []
            for block in blocks:
                block_id = str(block.get("block_id"))
                original = str(block.get("prose") or "")
                candidate = revisions.get(block_id)
                revised = original
                if candidate and candidate != original:
                    if ref_markers_preserved(original, candidate):
                        revised = candidate
                        applied(block_id)
                        applied_block_revisions.append(block_id)
                    else:
                        reason = "ref_marker_sequence_changed"
                        unapplied(block_id, reason)
                        unapplied_block_revisions.append(
                            {"block_id": block_id, "reason": reason}
                        )
                changed = revised != original
                section_blocks.append(
                    {
                        "block_id": block_id,
                        "title": str(block.get("title") or ""),
                        "original_text": original,
                        "text": revised,
                        "revised": changed,
                        "original_sha256": _sha256_text(original),
                        "revised_sha256": _sha256_text(revised),
                    }
                )
            assembled.append(
                {
                    "kind": "section",
                    "target_id": section_id,
                    "heading": heading,
                    "title": heading,
                    "full_text_authority": False,
                    "blocks": section_blocks,
                    "applied_block_revisions": applied_block_revisions,
                    "unapplied_block_revisions": unapplied_block_revisions,
                }
            )
        else:
            section_text = full_text
            revised = revisions.get(section_id)
            if revised and revised != raw_full_text:
                revised_body = _strip_matching_leading_heading(
                    revised, heading
                )
                if ref_markers_preserved(full_text, revised_body):
                    section_text = revised_body
                    applied(section_id)
                else:
                    unapplied(section_id, "ref_marker_sequence_changed")
            assembled.append(
                {
                    "kind": "section",
                    "target_id": section_id,
                    "heading": heading,
                    "title": heading,
                    "full_text_authority": True,
                    "text": section_text,
                    "applied_block_revisions": (
                        [section_id]
                        if revision_applications.get(section_id, {}).get(
                            "status"
                        )
                        == "applied"
                        else []
                    ),
                    "unapplied_block_revisions": (
                        [
                            {
                                "block_id": section_id,
                                "reason": revision_applications[section_id][
                                    "reason"
                                ],
                            }
                        ]
                        if revision_applications.get(section_id, {}).get(
                            "status"
                        )
                        == "unapplied"
                        else []
                    ),
                }
            )
    if "conclusion" in front_back:
        original = str(front_back["conclusion"] or "")
        text = original
        revised = revisions.get("conclusion")
        if revised and revised != original:
            if ref_markers_preserved(original, revised):
                text = revised
                applied("conclusion")
            else:
                unapplied("conclusion", "ref_marker_sequence_changed")
        text = _strip_matching_leading_heading(text, "Conclusion")
        assembled.append(
            {
                "kind": "conclusion",
                "target_id": "conclusion",
                "heading": "Conclusion",
                "title": "Conclusion",
                "text": text,
            }
        )
    for target_id in revisions:
        if target_id not in revision_applications:
            unapplied(target_id, "revision_target_not_found_or_no_change")
    full_text_parts: list[str] = []
    for entry in assembled:
        if entry.get("heading"):
            full_text_parts.append("## " + str(entry["heading"]))
        if entry.get("blocks") is not None:
            full_text_parts.extend(
                block["text"]
                for block in entry["blocks"]
                if str(block["text"] or "").strip()
            )
        elif str(entry.get("text") or "").strip():
            full_text_parts.append(str(entry["text"]))
    return {
        "assembled": assembled,
        "full_text": "\n\n".join(full_text_parts),
        "revision_applications": revision_applications,
        "applied_revision_ids": [
            target_id
            for target_id, record in revision_applications.items()
            if record.get("status") == "applied"
        ],
        "unapplied_revision_ids": [
            target_id
            for target_id, record in revision_applications.items()
            if record.get("status") == "unapplied"
        ],
    }


# --------------------------------------------------------------------------- #
# Live Qwen stage provider (bounded, one repair)
# --------------------------------------------------------------------------- #


def parse_fenced_json(text: str) -> dict[str, Any] | None:
    """Parse strict JSON or a single fenced JSON block."""

    value = str(text or "").strip()
    if not value:
        return None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    match = re.search(r"```(?:json)?\s*(.*?)```", value, re.S | re.I)
    if match:
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, Mapping):
                return dict(parsed)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return None


class QwenStagedProvider:
    """Generic Qwen stage provider with one bounded JSON repair call.

    The model receives one stage-specific prompt plus the JSON stage input.
    The response must be strict JSON (fenced JSON is accepted).  If parsing
    fails, exactly one compact repair call is made.  Usage metadata is
    returned in the reserved ``_usage`` key and recorded separately by the
    local runner; API keys or raw provider headers are never persisted.
    """

    def __init__(
        self,
        *,
        prompt_path: str | Path,
        agent_name: str,
        model_tier: str = "c_model",
        qwen_call: Callable[..., Any] | None = None,
        max_tokens: int = 6000,
    ) -> None:
        self.prompt_path = Path(prompt_path)
        if not self.prompt_path.is_file():
            raise StagedCompletionError(
                f"missing staged prompt: {self.prompt_path}"
            )
        self.prompt = self.prompt_path.read_text(encoding="utf-8")
        self.agent_name = agent_name
        self.model_tier = model_tier
        self._qwen_call = qwen_call
        self.max_tokens = max_tokens

    def _invoke(self, messages: list[dict[str, str]]) -> Mapping[str, Any]:
        call = self._qwen_call or qwen_chat_client.call_qwen_chat
        result = call(
            self.agent_name,
            messages,
            model_tier=self.model_tier,
            temperature=0.1,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
            stream=True,
            force_mock=False,
            max_retries=0,
            timeout_seconds=150,
            accept_partial_stream=False,
            allow_model_fallback=False,
        )
        if not isinstance(result, Mapping):
            raise StagedCompletionError(
                "staged provider returned a non-mapping result"
            )
        return result

    def _usage_of(self, result: Mapping[str, Any]) -> dict[str, Any]:
        usage = result.get("_llm_usage")
        return dict(usage) if isinstance(usage, Mapping) else {}

    def __call__(
        self, stage_input: Mapping[str, Any]
    ) -> dict[str, Any]:
        user_payload = json.dumps(
            {"stage_input": stage_input}, ensure_ascii=False
        )
        messages = [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": user_payload},
        ]
        result = self._invoke(messages)
        usage = self._usage_of(result)
        parsed = parse_fenced_json(str(result.get("content") or ""))
        repair_calls = 0
        if parsed is None:
            repair_messages = [
                messages[0],
                {
                    "role": "user",
                    "content": (
                        "The previous response was not valid JSON. Return "
                        "ONLY valid JSON for the same task, with no prose.\n\n"
                        + str(result.get("content") or "")
                    ),
                },
            ]
            repair_result = self._invoke(repair_messages)
            repair_calls = 1
            repair_usage = self._usage_of(repair_result)
            if repair_usage:
                usage = {"initial": usage, "repair": repair_usage}
            parsed = parse_fenced_json(
                str(repair_result.get("content") or "")
            )
            if parsed is None:
                raise StagedCompletionError(
                    "staged provider JSON parse failed after one repair call"
                )
        payload = dict(parsed)
        payload["_usage"] = {
            **usage,
            "agent_name": self.agent_name,
            "model_tier": self.model_tier,
            "repair_calls": repair_calls,
            "repair_used": bool(repair_calls),
        }
        return payload


def make_qwen_stage_provider(
    stage: str,
    *,
    prompts_dir: str | Path | None = None,
    model_tier: str = "c_model",
    qwen_call: Callable[..., Any] | None = None,
    agent_name: str | None = None,
    max_tokens: int = 6000,
) -> QwenStagedProvider:
    """Build the Qwen provider for one staged stage from its prompt file."""

    prompt_file = PROMPT_FILE_BY_STAGE.get(stage)
    if not prompt_file:
        raise StagedCompletionError(
            f"no staged prompt registered for stage: {stage}"
        )
    default_agent = {
        "conclusion": "StagedConclusionAuthor",
        "introduction": "StagedIntroductionAuthor",
        "abstract": "StagedAbstractAuthor",
        "whole_manuscript_review": "StagedWholeManuscriptReviewer",
        "bounded_patch_proposals": "StagedCommanderEditor",
        "editorial_revision": "StagedEditorialRevisionAuthor",
    }
    return QwenStagedProvider(
        prompt_path=Path(prompts_dir or DEFAULT_PROMPTS_DIR) / prompt_file,
        agent_name=agent_name or default_agent[stage],
        model_tier=model_tier,
        qwen_call=qwen_call,
        max_tokens=max_tokens,
    )


class QwenMultiReviewerProvider:
    """Invoke the whole-manuscript prompt for multiple reviewers and aggregate.

    Each configured reviewer role/id receives its own bounded Qwen call with
    the same stage prompt.  Reviewers are independent: no earlier reviewer
    outputs are present in any reviewer's stage input.  Model-provided
    finding IDs, free-text issue keys, consensus, blocking, and advisory
    flags are overridden locally (the local runtime owns identity and
    status); raw per-reviewer ``issue_type``/``target_ids`` are retained for
    audit.  Findings are aggregated through
    ``aggregate_multi_reviewer_report`` so the aggregated identity is local
    and deterministic, and only critical severity with full multi-reviewer
    consensus blocks.  Per-reviewer usage is merged into the reserved
    ``_usage`` key; the single provider API is unchanged.
    """

    def __init__(
        self,
        *,
        reviewers: Sequence[Mapping[str, Any]],
        prompt_path: str | Path,
        agent_name: str = "StagedWholeManuscriptReviewer",
        model_tier: str = "c_model",
        qwen_call: Callable[..., Any] | None = None,
        max_tokens: int = 6000,
        workers: int | None = None,
    ) -> None:
        self._single = QwenStagedProvider(
            prompt_path=prompt_path,
            agent_name=agent_name,
            model_tier=model_tier,
            qwen_call=qwen_call,
            max_tokens=max_tokens,
        )
        self.reviewers = [
            {
                "reviewer_id": str(spec.get("reviewer_id") or ""),
                "role": str(spec.get("role") or "")
                or str(spec.get("reviewer_id") or ""),
            }
            for spec in reviewers
            if isinstance(spec, Mapping) and str(spec.get("reviewer_id") or "")
        ]
        if not self.reviewers:
            raise StagedCompletionError(
                "multi-reviewer provider requires at least one reviewer"
            )
        self._workers = max(
            1,
            min(
                (
                    int(workers)
                    if workers is not None
                    else min(4, len(self.reviewers))
                ),
                len(self.reviewers),
            ),
        )

    @property
    def model_tier(self) -> str:
        """Effective model tier used for every reviewer call."""

        return self._single.model_tier

    def __call__(
        self, stage_input: Mapping[str, Any]
    ) -> dict[str, Any]:
        reviewer_inputs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for spec in self.reviewers:
            reviewer_input = dict(stage_input)
            # Reviewer independence: never hand one reviewer another
            # reviewer's outputs, including any nested in prior artifacts.
            for key in (
                "reviewer_outputs",
                "other_reviewers",
                "other_reviewer_outputs",
                "previous_reviewer_outputs",
                "all_reviewer_outputs",
            ):
                reviewer_input.pop(key, None)
            previous = reviewer_input.get("previous_artifacts")
            if isinstance(previous, Mapping):
                reviewer_input["previous_artifacts"] = {
                    name: value
                    for name, value in previous.items()
                    if name != "whole_manuscript_review"
                    and "reviewer" not in str(name).casefold()
                }
            reviewer_input["reviewer_id"] = spec["reviewer_id"]
            reviewer_input["reviewer_role"] = spec["role"]
            reviewer_inputs.append((spec, reviewer_input))

        if self._workers == 1:
            outputs = [
                self._single(reviewer_input)
                for _spec, reviewer_input in reviewer_inputs
            ]
        else:
            with ThreadPoolExecutor(
                max_workers=self._workers,
                thread_name_prefix="staged-reviewer",
            ) as pool:
                futures = {
                    pool.submit(self._single, reviewer_input): index
                    for index, (_spec, reviewer_input) in enumerate(
                        reviewer_inputs
                    )
                }
                ordered = {
                    futures[future]: future.result()
                    for future in as_completed(futures)
                }
            outputs = [ordered[index] for index in range(len(reviewer_inputs))]

        roles: list[ReviewerRole] = []
        per_reviewer_usage: dict[str, Any] = {}
        total_input_tokens = 0
        total_output_tokens = 0
        repair_calls = 0
        for (spec, _reviewer_input), output in zip(
            reviewer_inputs, outputs
        ):
            usage = dict(output.pop("_usage") or {})
            per_reviewer_usage[spec["reviewer_id"]] = usage
            total_input_tokens += int(usage.get("input_tokens") or 0)
            total_output_tokens += int(usage.get("output_tokens") or 0)
            repair_calls += int(usage.get("repair_calls") or 0)
            findings: list[ManuscriptReviewFinding] = []
            for raw in output.get("findings") or []:
                if not isinstance(raw, Mapping):
                    continue
                finding = ManuscriptReviewFinding.model_validate(raw)
                if (
                    str(finding.dimension or "").strip().casefold()
                    not in REVIEW_DIMENSIONS
                ):
                    continue
                # Local runtime owns identity and status fields; anything the
                # model returned for these is overridden.
                finding.finding_id = ""
                finding.issue_key = ""
                finding.consensus = "none"
                finding.advisory = True
                finding.blocking = False
                findings.append(finding)
            roles.append(
                ReviewerRole(
                    reviewer_id=spec["reviewer_id"],
                    role=spec["role"],
                    findings=findings,
                )
            )
        aggregated = aggregate_multi_reviewer_report(
            MultiReviewerReport(reviewers=roles)
        )
        payload: dict[str, Any] = {
            "review": aggregated.model_dump(mode="json"),
            "reviewers": [role.model_dump(mode="json") for role in roles],
            "status": "reviewed",
        }
        payload["_usage"] = {
            "agent_name": self._single.agent_name,
            "model_tier": self._single.model_tier,
            "call_count": len(roles),
            "workers": self._workers,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "repair_calls": repair_calls,
            "repair_used": bool(repair_calls),
            "reviewers": per_reviewer_usage,
        }
        return payload


def make_multi_reviewer_qwen_provider(
    *,
    reviewers: Sequence[Mapping[str, Any]] | Sequence[str] | None = None,
    prompts_dir: str | Path | None = None,
    model_tier: str = "c_model",
    qwen_call: Callable[..., Any] | None = None,
    agent_name: str = "StagedWholeManuscriptReviewer",
    max_tokens: int = 6000,
    workers: int | None = None,
) -> QwenMultiReviewerProvider:
    """Build the multi-reviewer whole-manuscript provider.

    Defaults to five independent editorial dimensions (continuity, clarity,
    reader_flow, logic, overlap); an explicit ``reviewers`` sequence
    overrides that set.
    """

    prompt_file = PROMPT_FILE_BY_STAGE["whole_manuscript_review"]
    if reviewers is None:
        reviewers = [
            {"reviewer_id": "continuity_reviewer", "role": "continuity"},
            {"reviewer_id": "clarity_reviewer", "role": "clarity"},
            {"reviewer_id": "reader_flow_reviewer", "role": "reader_flow"},
            {"reviewer_id": "logic_reviewer", "role": "logic"},
            {"reviewer_id": "overlap_reviewer", "role": "overlap"},
        ]
    specs: list[dict[str, str]] = []
    for raw in reviewers:
        if isinstance(raw, Mapping):
            reviewer_id = str(raw.get("reviewer_id") or "").strip()
            role = str(raw.get("role") or reviewer_id).strip()
        else:
            reviewer_id = str(raw).strip()
            role = reviewer_id
        if not reviewer_id:
            raise StagedCompletionError(
                "multi-reviewer provider requires reviewer_id on every entry"
            )
        specs.append({"reviewer_id": reviewer_id, "role": role or reviewer_id})
    return QwenMultiReviewerProvider(
        reviewers=specs,
        prompt_path=Path(prompts_dir or DEFAULT_PROMPTS_DIR) / prompt_file,
        agent_name=agent_name,
        model_tier=model_tier,
        qwen_call=qwen_call,
        max_tokens=max_tokens,
        workers=workers,
    )


class QwenEditorialRevisionProvider:
    """Bounded editorial revision author + independent verifier provider.

    The provider plans bounded work items locally from review findings and
    patch proposals.  Each author call sees only the target block, its
    immediate neighbors, and the relevant finding/patch anchor; it never
    receives the full manuscript.  Every proposed change is then checked by
    an independent compact verifier plus a local exact REF-marker sequence
    check.  Failures keep the original block and are recorded in the audit;
    the stage always completes fail-open with a new assembled manuscript.

    When an ``execution_context`` with ``work_dir``/``resume`` is supplied,
    every completed work item is atomically checkpointed in the work
    directory.  On resume with a matching checkpoint fingerprint (covering
    the editorial input fingerprint, exact planned items, prompt hashes,
    model tiers, and token budgets), completed items are reused and Qwen is
    called only for unfinished items.  A mismatched fingerprint or a
    non-resume run starts a fresh checkpoint.  Checkpoints never contain
    secrets.
    """

    def __init__(
        self,
        *,
        author_prompt_path: str | Path,
        verifier_prompt_path: str | Path,
        agent_name: str = "StagedEditorialRevisionAuthor",
        verifier_agent_name: str = "StagedEditorialRevisionVerifier",
        model_tier: str = "c_model",
        verifier_tier: str = "c2_model",
        qwen_call: Callable[..., Any] | None = None,
        author_max_tokens: int = 2500,
        verifier_max_tokens: int = 1200,
        workers: int = 1,
    ) -> None:
        self._author = QwenStagedProvider(
            prompt_path=author_prompt_path,
            agent_name=agent_name,
            model_tier=model_tier,
            qwen_call=qwen_call,
            max_tokens=author_max_tokens,
        )
        self._verifier = QwenStagedProvider(
            prompt_path=verifier_prompt_path,
            agent_name=verifier_agent_name,
            model_tier=verifier_tier,
            qwen_call=qwen_call,
            max_tokens=verifier_max_tokens,
        )
        self.model_tier = model_tier
        self.verifier_tier = verifier_tier
        self._workers = max(1, int(workers or 1))
        self._state_lock = threading.RLock()

    @staticmethod
    def _author_input(item: EditorialWorkItem) -> dict[str, Any]:
        return {
            "work_item_id": item.work_item_id,
            "editorial_kind": item.editorial_kind,
            "unit": item.unit,
            "target_block_id": item.target_block_id,
            "target_kind": item.target_kind,
            "section_id": item.section_id,
            "section_title": item.section_title,
            "chapter_thesis": item.chapter_thesis,
            "reader_takeaway": item.reader_takeaway,
            "original_text": item.original_text,
            "previous_text": item.previous_text,
            "next_text": item.next_text,
            "finding": item.finding,
            "patch_proposal": item.patch_proposal,
            "constraints": [
                "preserve REF markers exactly",
                "no new claims/evidence/citations/numbers/conditions",
                "bounded local edit only",
            ],
        }

    @staticmethod
    def _verifier_input(
        item: EditorialWorkItem, revised_text: str
    ) -> dict[str, Any]:
        return {
            "work_item_id": item.work_item_id,
            "editorial_kind": item.editorial_kind,
            "unit": item.unit,
            "target_block_id": item.target_block_id,
            "original_text": item.original_text,
            "revised_text": revised_text,
            "finding": item.finding,
            "patch_proposal": item.patch_proposal,
        }

    def _checkpoint_fingerprint(
        self,
        work_items: Sequence[EditorialWorkItem],
        stage_input: Mapping[str, Any],
    ) -> str:
        return fingerprint(
            {
                "schema_version": EDITORIAL_CHECKPOINT_SCHEMA,
                "input_fingerprint": str(
                    stage_input.get("input_fingerprint") or ""
                ),
                "planned_items": [
                    item.model_dump(mode="json") for item in work_items
                ],
                "author_prompt_sha256": _sha256_text(self._author.prompt),
                "verifier_prompt_sha256": _sha256_text(
                    self._verifier.prompt
                ),
                "model_tiers": [self.model_tier, self.verifier_tier],
                "max_tokens": {
                    "author": self._author.max_tokens,
                    "verifier": self._verifier.max_tokens,
                },
            }
        )

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)

    def _persist_checkpoint(
        self,
        path: Path,
        checkpoint_fingerprint: str,
        items: Mapping[str, Mapping[str, Any]],
        *,
        completed: bool,
    ) -> None:
        with self._state_lock:
            payload = {
                "schema_version": EDITORIAL_CHECKPOINT_SCHEMA,
                "fingerprint": checkpoint_fingerprint,
                "completed": completed,
                "updated_at": _now(),
                "items": [items[key] for key in sorted(items)],
            }
            self._atomic_write_json(path, payload)

    def _checkpoint_context(
        self,
        work_items: Sequence[EditorialWorkItem],
        stage_input: Mapping[str, Any],
        execution_context: Mapping[str, Any] | None,
    ) -> tuple[
        str, bool, dict[str, dict[str, Any]], Path | None
    ]:
        checkpoint_fingerprint = self._checkpoint_fingerprint(
            work_items, stage_input
        )
        resumed = False
        items: dict[str, dict[str, Any]] = {}
        path: Path | None = None
        if not execution_context:
            return checkpoint_fingerprint, resumed, items, path
        raw_work_dir = execution_context.get("work_dir")
        resume = bool(execution_context.get("resume"))
        if not raw_work_dir:
            return checkpoint_fingerprint, resumed, items, path
        path = Path(raw_work_dir) / EDITORIAL_CHECKPOINT_JSON
        path.parent.mkdir(parents=True, exist_ok=True)
        if resume and path.is_file():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                if (
                    str(stored.get("fingerprint") or "")
                    == checkpoint_fingerprint
                ):
                    for entry in stored.get("items") or []:
                        if (
                            isinstance(entry, Mapping)
                            and entry.get("status") == "completed"
                        ):
                            items[
                                str(entry.get("work_item_id") or "")
                            ] = dict(entry)
                    resumed = bool(items)
            except (OSError, ValueError, json.JSONDecodeError):
                items = {}
        self._persist_checkpoint(
            path,
            checkpoint_fingerprint,
            items,
            completed=False,
        )
        return checkpoint_fingerprint, resumed, items, path

    def _run_item(
        self, item: EditorialWorkItem
    ) -> EditorialRevisionRecord:
        record = EditorialRevisionRecord(
            work_item_id=item.work_item_id,
            target_block_id=item.target_block_id,
            editorial_kind=item.editorial_kind,
            unit=item.unit,
            source_finding=_finding_source_label(item.finding),
            source_patch=_patch_source_label(item.patch_proposal),
            original_text=item.original_text,
            original_sha256=_sha256_text(item.original_text),
        )
        revised_text = ""
        reason = ""
        status = "rejected"
        author_usage: dict[str, Any] = {}
        verifier_usage: dict[str, Any] = {}
        try:
            author_output = self._author(self._author_input(item))
            author_usage = dict(author_output.pop("_usage") or {})
            revised_text = str(
                author_output.get("revised_text") or ""
            ).strip()
            if not revised_text or revised_text == item.original_text:
                reason = "author_returned_no_change"
            elif not ref_markers_preserved(
                item.original_text, revised_text
            ):
                reason = "ref_markers_changed"
            else:
                verifier_output = self._verifier(
                    self._verifier_input(item, revised_text)
                )
                verifier_usage = dict(
                    verifier_output.pop("_usage") or {}
                )
                if verifier_accepts(verifier_output):
                    status = "accepted"
                else:
                    reason = "verifier_rejected:" + str(
                        verifier_output.get("notes") or ""
                    )
        except Exception as exc:  # fail-open per work item
            status = "failed"
            reason = f"exception:{exc}"
        record.status = status
        record.reason = reason
        record.revised_text = revised_text
        record.revised_sha256 = (
            _sha256_text(revised_text) if revised_text else ""
        )
        record.author_usage = author_usage
        record.verifier_usage = verifier_usage
        return record

    def __call__(
        self,
        stage_input: Mapping[str, Any],
        *,
        execution_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        inputs = _mapping(stage_input.get("inputs"))
        previous = _mapping(stage_input.get("previous_artifacts"))
        sections = [
            section
            for section in inputs.get("sections") or []
            if isinstance(section, Mapping)
        ]
        front_back: dict[str, str] = {}
        for name in ("conclusion", "introduction", "abstract"):
            record = previous.get(name)
            if isinstance(record, Mapping):
                front_back[name] = _artifact_draft_text(record)
        findings: list[dict[str, Any]] = []
        review_record = previous.get("whole_manuscript_review")
        if isinstance(review_record, Mapping):
            review = _mapping(
                _mapping(review_record.get("payload")).get("review")
            )
            findings = [
                finding
                for finding in review.get("findings") or []
                if isinstance(finding, Mapping)
            ]
        proposals: list[dict[str, Any]] = []
        patches_record = previous.get("bounded_patch_proposals")
        if isinstance(patches_record, Mapping):
            payload = _mapping(patches_record.get("payload"))
            proposals = [
                proposal
                for proposal in payload.get("proposals") or []
                if isinstance(proposal, Mapping)
            ]
        (
            work_items,
            blocking_unresolved,
            unselected,
        ) = plan_editorial_work_items_with_provenance(
            sections=sections,
            front_back=front_back,
            findings=findings,
            proposals=proposals,
        )
        (
            checkpoint_fingerprint,
            resumed,
            checkpoint_items,
            checkpoint_path,
        ) = self._checkpoint_context(
            work_items, stage_input, execution_context
        )
        groups: dict[str, list[EditorialWorkItem]] = {}
        for item in work_items:
            section_key = str(
                item.section_id
                or str(item.target_block_id).split("-", 1)[0]
                or "other"
            )
            groups.setdefault(section_key, []).append(item)
        group_order = list(groups.keys())

        def process_group(
            items: Sequence[EditorialWorkItem],
        ) -> list[
            tuple[str, EditorialRevisionRecord, str]
        ]:
            out: list[tuple[str, EditorialRevisionRecord, str]] = []
            for item in items:
                with self._state_lock:
                    checkpoint_entry = checkpoint_items.get(
                        item.work_item_id
                    )
                if checkpoint_entry is not None:
                    record = EditorialRevisionRecord.model_validate(
                        checkpoint_entry["record"]
                    )
                    revision = (
                        record.revised_text
                        if record.status == "accepted"
                        and record.revised_text
                        else ""
                    )
                    out.append((item.work_item_id, record, revision))
                    continue
                record = self._run_item(item)
                revision = (
                    record.revised_text
                    if record.status == "accepted" and record.revised_text
                    else ""
                )
                if checkpoint_path is not None:
                    with self._state_lock:
                        checkpoint_items[item.work_item_id] = {
                            "work_item_id": item.work_item_id,
                            "target_block_id": item.target_block_id,
                            "status": "completed",
                            "record": record.model_dump(mode="json"),
                            "revision": revision or None,
                        }
                        self._persist_checkpoint(
                            checkpoint_path,
                            checkpoint_fingerprint,
                            checkpoint_items,
                            completed=False,
                        )
                out.append((item.work_item_id, record, revision))
            return out

        worker_count = max(
            1,
            min(self._workers, len(groups)),
        )
        if worker_count == 1:
            group_results = [
                process_group(groups[key]) for key in group_order
            ]
        else:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="staged-editorial",
            ) as pool:
                futures = {
                    pool.submit(process_group, groups[key]): index
                    for index, key in enumerate(group_order)
                }
                ordered = {
                    futures[future]: future.result()
                    for future in as_completed(futures)
                }
            group_results = [ordered[index] for index in range(len(group_order))]
        by_item: dict[str, tuple[EditorialRevisionRecord, str]] = {
            work_item_id: (record, revision)
            for group in group_results
            for work_item_id, record, revision in group
        }
        records: list[EditorialRevisionRecord] = [
            by_item[item.work_item_id][0] for item in work_items
        ]
        revisions: dict[str, str] = {}
        for item in work_items:
            revision = by_item[item.work_item_id][1]
            if revision:
                revisions[item.target_block_id] = revision

        manuscript = assemble_revised_manuscript(
            sections=sections,
            front_back=front_back,
            revisions=revisions,
            section_order=_selected_section_order(inputs),
        )
        application_by_target = _mapping(
            manuscript.get("revision_applications")
        )
        applied_revision_ids: list[str] = []
        unapplied_accepted_revision_ids: list[str] = []
        for record in records:
            if record.status != "accepted":
                continue
            application = _mapping(
                application_by_target.get(record.target_block_id)
            )
            if str(application.get("status") or "") == "applied":
                record.application_status = "applied"
                record.application_reason = str(
                    application.get("reason") or ""
                )
                applied_revision_ids.append(record.work_item_id)
            else:
                record.application_status = "unapplied"
                record.application_reason = str(
                    application.get("reason")
                    or "accepted_revision_not_safely_materialized"
                )
                unapplied_accepted_revision_ids.append(record.work_item_id)
            with self._state_lock:
                checkpoint_entry = checkpoint_items.get(
                    record.work_item_id
                )
                if checkpoint_entry is not None:
                    checkpoint_entry["record"] = record.model_dump(
                        mode="json"
                    )
        accepted_count = sum(
            1 for record in records if record.status == "accepted"
        )
        author_input_tokens = sum(
            int((record.author_usage or {}).get("input_tokens") or 0)
            for record in records
        )
        author_output_tokens = sum(
            int((record.author_usage or {}).get("output_tokens") or 0)
            for record in records
        )
        verifier_input_tokens = sum(
            int((record.verifier_usage or {}).get("input_tokens") or 0)
            for record in records
        )
        verifier_output_tokens = sum(
            int((record.verifier_usage or {}).get("output_tokens") or 0)
            for record in records
        )
        author_calls = sum(
            1 for record in records if record.author_usage
        )
        verifier_calls = sum(
            1 for record in records if record.verifier_usage
        )
        author_repairs = sum(
            int((record.author_usage or {}).get("repair_calls") or 0)
            for record in records
        )
        verifier_repairs = sum(
            int((record.verifier_usage or {}).get("repair_calls") or 0)
            for record in records
        )
        audit = EditorialRevisionAudit(
            work_item_count=len(work_items),
            accepted_count=accepted_count,
            rejected_count=len(records) - accepted_count,
            applied_revision_ids=applied_revision_ids,
            unapplied_accepted_revision_ids=(
                unapplied_accepted_revision_ids
            ),
            blocking_unresolved=blocking_unresolved,
            records=records,
            review_findings_kept=findings,
            unselected=unselected,
            checkpoint_fingerprint=checkpoint_fingerprint,
            resumed_from_checkpoint=resumed,
        )
        payload: dict[str, Any] = {
            "manuscript": manuscript,
            "audit": audit.model_dump(mode="json"),
            "status": "revised",
        }
        payload["_usage"] = {
            "agent_name": self._author.agent_name,
            "model_tier": self.model_tier,
            "verifier_agent_name": self._verifier.agent_name,
            "verifier_model_tier": self.verifier_tier,
            "author_call_count": author_calls,
            "verifier_call_count": verifier_calls,
            "total_input_tokens": (
                author_input_tokens + verifier_input_tokens
            ),
            "total_output_tokens": (
                author_output_tokens + verifier_output_tokens
            ),
            "repair_calls": author_repairs + verifier_repairs,
            "repair_used": bool(author_repairs + verifier_repairs),
            "author": {
                "input_tokens": author_input_tokens,
                "output_tokens": author_output_tokens,
            },
            "verifier": {
                "input_tokens": verifier_input_tokens,
                "output_tokens": verifier_output_tokens,
            },
            "records": {
                record.work_item_id: {
                    "author": record.author_usage,
                    "verifier": record.verifier_usage,
                }
                for record in records
            },
        }
        if checkpoint_path is not None:
            self._persist_checkpoint(
                checkpoint_path,
                checkpoint_fingerprint,
                checkpoint_items,
                completed=True,
            )
        return payload


def make_editorial_revision_qwen_provider(
    *,
    prompts_dir: str | Path | None = None,
    model_tier: str = "c_model",
    verifier_tier: str = "c2_model",
    qwen_call: Callable[..., Any] | None = None,
    agent_name: str = "StagedEditorialRevisionAuthor",
    verifier_agent_name: str = "StagedEditorialRevisionVerifier",
    author_max_tokens: int = 2500,
    verifier_max_tokens: int = 1200,
    workers: int = 1,
) -> QwenEditorialRevisionProvider:
    """Build the bounded editorial revision author+verifier provider."""

    prompts_dir = Path(prompts_dir or DEFAULT_PROMPTS_DIR)
    return QwenEditorialRevisionProvider(
        author_prompt_path=prompts_dir
        / PROMPT_FILE_BY_STAGE["editorial_revision"],
        verifier_prompt_path=prompts_dir / EDITORIAL_VERIFIER_PROMPT_FILE,
        agent_name=agent_name,
        verifier_agent_name=verifier_agent_name,
        model_tier=model_tier,
        verifier_tier=verifier_tier,
        qwen_call=qwen_call,
        author_max_tokens=author_max_tokens,
        verifier_max_tokens=verifier_max_tokens,
        workers=workers,
    )


# --------------------------------------------------------------------------- #
# Deterministic offline runner
# --------------------------------------------------------------------------- #


def _default_offline_stage_provider(
    stage_input: Mapping[str, Any],
) -> dict[str, Any]:
    """Deterministic placeholder payload; never calls a model or network."""

    stage = str(stage_input.get("stage") or "")
    inputs = _mapping(stage_input.get("inputs"))
    previous = _mapping(stage_input.get("previous_artifacts"))
    previous_fingerprints = {
        name: str(record.get("fingerprint") or "")
        for name, record in previous.items()
        if isinstance(record, Mapping)
    }
    if stage == "handoff_metadata_repair":
        handoff = inputs.get("full_manuscript_handoff")
        repair_notes = []
        if isinstance(handoff, Mapping):
            repair_notes.append(
                "full_manuscript_handoff:"
                + str(handoff.get("input_fingerprint") or "")
            )
        return {
            "handoff_package_path": str(inputs.get("handoff_package") or ""),
            "metadata_repair_notes": repair_notes,
            "status": "offline_noop",
        }
    if stage == "commander_structure":
        work_order = inputs.get("commander_work_order")
        if isinstance(work_order, Mapping):
            authority = build_commander_structural_authority(
                work_order,
                source_work_order_path=str(
                    inputs.get("commander_work_order_path") or ""
                ),
            )
            return authority.model_dump(mode="json")
        return CommanderStructuralAuthority(
            section_order=[
                SectionOrderEntry(section_id=str(section_id), position=index)
                for index, section_id in enumerate(
                    inputs.get("section_order") or []
                )
            ],
        ).model_dump(mode="json")
    if stage == "conclusion":
        derived_from = ["commander_structure"]
        commander_fp = previous_fingerprints.get("commander_structure")
        if commander_fp:
            derived_from.append("commander_structure:" + commander_fp)
        _ir = extract_presentation_ir(inputs)
        if _ir is None:
            raw_ir_in_input = stage_input.get("presentation_ir")
            if isinstance(raw_ir_in_input, Mapping):
                _ir = extract_presentation_ir({"presentation_ir": raw_ir_in_input})
        return {
            "workplan": ConclusionWorkplan(
                derived_from=derived_from,
                outline=[],
                presentation_ir_fingerprint=_ir.ir_fingerprint if _ir else "",
            ).model_dump(mode="json"),
            "draft": ConclusionDraft(text="").model_dump(mode="json"),
            "status": "offline_placeholder",
        }
    if stage == "introduction":
        conclusion = previous.get("conclusion") or {}
        conclusion_fp = str(conclusion.get("fingerprint") or "")
        promises = ["conclusion"]
        if conclusion_fp:
            promises.append("conclusion:" + conclusion_fp)
        _ir = extract_presentation_ir(inputs)
        if _ir is None:
            raw_ir_in_input = stage_input.get("presentation_ir")
            if isinstance(raw_ir_in_input, Mapping):
                _ir = extract_presentation_ir({"presentation_ir": raw_ir_in_input})
        return {
            "workplan": IntroductionWorkplan(
                promises_derived_from=promises,
                outline=[],
                presentation_ir_fingerprint=_ir.ir_fingerprint if _ir else "",
            ).model_dump(mode="json"),
            "draft": IntroductionDraft(text="").model_dump(mode="json"),
            "status": "offline_placeholder",
        }
    if stage == "abstract":
        _ir = extract_presentation_ir(inputs)
        if _ir is None:
            raw_ir_in_input = stage_input.get("presentation_ir")
            if isinstance(raw_ir_in_input, Mapping):
                _ir = extract_presentation_ir({"presentation_ir": raw_ir_in_input})
        return {
            "workplan": AbstractWorkplan(
                compresses=[
                    "commander_structure",
                    "conclusion",
                    "introduction",
                ],
                presentation_ir_fingerprint=_ir.ir_fingerprint if _ir else "",
            ).model_dump(mode="json"),
            "draft": AbstractDraft(text="").model_dump(mode="json"),
            "status": "offline_placeholder",
        }
    if stage == "whole_manuscript_review":
        return {
            "review": ManuscriptReviewReport(findings=[]).model_dump(
                mode="json"
            ),
            "status": "offline_noop",
        }
    if stage == "bounded_patch_proposals":
        return PatchProposalSet(proposals=[]).model_dump(mode="json")
    if stage == "editorial_revision":
        sections = [
            section
            for section in inputs.get("sections") or []
            if isinstance(section, Mapping)
        ]
        front_back: dict[str, str] = {}
        for name in ("conclusion", "introduction", "abstract"):
            record = previous.get(name)
            if isinstance(record, Mapping):
                front_back[name] = _artifact_draft_text(record)
        manuscript = assemble_revised_manuscript(
            sections=sections,
            front_back=front_back,
            revisions={},
            section_order=_selected_section_order(inputs),
        )
        audit = EditorialRevisionAudit(
            work_item_count=0,
            accepted_count=0,
            rejected_count=0,
        )
        return {
            "manuscript": manuscript,
            "audit": audit.model_dump(mode="json"),
            "status": "offline_noop",
        }
    if stage == "visual_remount":
        return {
            "visual_package_path": str(
                inputs.get("final_visual_package") or ""
            ),
            "work_orders": [],
            "visual_procurement_manifest": {
                "schema_version": (
                    "optomind.visual_procurement_manifest.v1"
                ),
                "status": "offline_noop",
                "fail_open": True,
                "pending_review_candidates_traceable": True,
            },
            "status": "offline_noop",
        }
    if stage == "assembly_preflight":
        return {
            "assembly_target": "staged_COMPLETE_REVIEW_EN.md",
            "preflight": {
                "latex_renderer": "not_invoked",
                "delivery_gate": "pending",
            },
            "status": "offline_noop",
        }
    raise StagedCompletionError(f"unknown staged stage: {stage}")


def _payload_requires_approval(stage: str, payload: Mapping[str, Any]) -> bool:
    if bool(payload.get("approval_required")):
        return True
    if stage == "bounded_patch_proposals":
        return bool(
            patch_set_requires_approval(payload.get("proposals") or [])
        )
    return False


def _write_complete_review_markdown(
    state: StagedArticleCompletionState,
    work_dir: Path,
) -> None:
    """Write the standalone assembled manuscript plus sha256/provenance."""

    record = state.stages.get("editorial_revision")
    if record is None:
        return
    payload = record.payload or {}
    manuscript = _mapping(payload.get("manuscript"))
    full_text = str(manuscript.get("full_text") or "")
    md_path = work_dir / COMPLETE_REVIEW_MARKDOWN
    md_path.write_text(full_text, encoding="utf-8")
    manifest = {
        "schema_version": "optomind.staged_complete_review_en.v1",
        "file": COMPLETE_REVIEW_MARKDOWN,
        "sha256": _sha256_text(full_text),
        "run_id": state.run_id,
        "editorial_revision_fingerprint": record.fingerprint,
        "source_artifact": Path(record.artifact_path or "").name,
        "updated_at": record.updated_at or _now(),
    }
    (work_dir / COMPLETE_REVIEW_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _provider_accepts_execution_context(provider: Callable[..., Any]) -> bool:
    try:
        return "execution_context" in inspect.signature(
            provider
        ).parameters
    except (TypeError, ValueError):
        return False


def run_staged_article_completion(
    *,
    work_dir: str | Path,
    inputs: Mapping[str, Any] | None = None,
    stage_inputs: Mapping[str, Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
    stage_order: Sequence[str] | None = None,
    stage_providers: Mapping[
        str, Callable[[Mapping[str, Any]], Mapping[str, Any]]
    ] | None = None,
    resume: bool = False,
    run_id: str = "",
    execution_context: Mapping[str, Any] | None = None,
) -> StagedArticleCompletionState:
    """Run all staged completion stages deterministically and resumably."""

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    order = list(stage_order or STAGE_ORDER)
    inputs = dict(inputs or {})
    stage_inputs = {
        str(stage): dict(value)
        for stage, value in (stage_inputs or {}).items()
        if isinstance(value, Mapping)
    }
    metadata = dict(metadata or {})
    run_fingerprint = fingerprint(
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "stage_order": order,
            "inputs": inputs,
            "stage_inputs": stage_inputs,
            "metadata": metadata,
        }
    )
    state_path = work_dir / STATE_JSON
    if resume and state_path.is_file():
        try:
            stored = StagedArticleCompletionState.model_validate(
                json.loads(state_path.read_text(encoding="utf-8"))
            )
        except Exception as exc:
            raise StagedCompletionError(
                f"cannot reload staged state {state_path}: {exc}"
            ) from exc
        if stored.run_fingerprint != run_fingerprint:
            raise StagedCompletionError(
                "resume refused: staged run fingerprint changed"
            )
        state = stored
        state.resumed = True
    else:
        state = StagedArticleCompletionState(
            run_id=run_id,
            run_fingerprint=run_fingerprint,
            stage_order=order,
            status="running",
            resumed=False,
            created_at=_now(),
            updated_at=_now(),
        )

    def persist() -> None:
        state.updated_at = _now()
        state_path.write_text(
            json.dumps(
                state.model_dump(mode="json"), ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )

    providers = dict(stage_providers or {})
    for stage_index, stage in enumerate(order):
        selected_inputs = dict(stage_inputs.get(stage, inputs))
        previous_artifacts = {
            name: state.stages[name].model_dump(mode="json")
            for name in order[:stage_index]
            if name in state.stages
        }
        input_fingerprint = fingerprint(
            {
                "stage": stage,
                "run_fingerprint": run_fingerprint,
                "previous_artifact_fingerprints": {
                    name: record["fingerprint"]
                    for name, record in previous_artifacts.items()
                },
                "selected_inputs": selected_inputs,
                "metadata": metadata,
            }
        )
        existing = state.stages.get(stage)
        artifact_path = work_dir / f"{ARTIFACT_PREFIX}{stage}.json"
        if (
            resume
            and existing is not None
            and existing.status in {"completed", "noop", "awaiting_approval"}
            and existing.input_fingerprint == input_fingerprint
            and artifact_path.is_file()
        ):
            existing.status = "noop"
            existing.approval_required = _payload_requires_approval(
                stage, existing.payload or {}
            )
            existing.updated_at = _now()
            persist()
            continue
        stage_input: dict[str, Any] = {
            "stage": stage,
            "run_fingerprint": run_fingerprint,
            "input_fingerprint": input_fingerprint,
            "inputs": selected_inputs,
            "metadata": metadata,
            "previous_artifacts": previous_artifacts,
        }
        if stage in FRONT_MATTER_STAGES:
            _ir = extract_presentation_ir(inputs)
            if _ir is not None:
                stage_input["presentation_ir"] = _ir.model_dump(mode="json")
                # Keep title planning independent from prose generation while
                # making the same deterministic candidates available to each
                # front-matter author. A live title provider can replace this
                # plan later without changing the stage contract.
                stage_input["title_plan"] = plan_review_titles(
                    _ir
                ).model_dump(mode="json")
        provider = providers.get(stage) or _default_offline_stage_provider
        if (
            execution_context is not None
            and _provider_accepts_execution_context(provider)
        ):
            payload = dict(
                provider(
                    stage_input,
                    execution_context=dict(execution_context),
                )
            )
        else:
            payload = dict(provider(stage_input))
        usage: dict[str, Any] = {}
        if "_usage" in payload:
            usage = dict(payload.pop("_usage") or {})
        validate_claim_evidence_invariant_preserved(payload)
        approval = _payload_requires_approval(stage, payload)
        stage_fingerprint = fingerprint(
            {
                "schema_version": SCHEMA_VERSION,
                "stage": stage,
                "input_fingerprint": input_fingerprint,
                "payload": payload,
            }
        )
        record = StagedStageState(
            stage=stage,
            status="awaiting_approval" if approval else "completed",
            fingerprint=stage_fingerprint,
            input_fingerprint=input_fingerprint,
            artifact_path=str(artifact_path),
            approval_required=approval,
            payload=payload,
            provenance={
                "run_fingerprint": run_fingerprint,
                "stage_order_position": str(order.index(stage)),
                "usage_recorded": "True" if usage else "False",
            },
            usage=usage,
            created_at=_now(),
            updated_at=_now(),
        )
        artifact_path.write_text(
            json.dumps(
                record.model_dump(mode="json"), ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        state.stages[stage] = record
        persist()

    _write_complete_review_markdown(state, work_dir)
    awaiting = state.awaiting_approval_stages
    state.status = "awaiting_approval" if awaiting else "completed"
    state.updated_at = _now()
    persist()
    return state


def visual_procurement_pre_step(
    inputs: "Mapping[str, Any]",
    *,
    papers: "Sequence[Any] | None" = None,
    work_dir: "Path | None" = None,
) -> "dict[str, Any]":
    """Run bounded visual procurement before the visual_remount stage.

    Designed to be called by a live ``visual_remount`` stage_provider before
    it loads the visual factory plan.  Accepts ``kb_sqlite``,
    ``visual_cache_dir``, ``download_dir``, and ``papers`` from *inputs*;
    always returns a well-formed manifest dict.  Missing/rejected images never
    remove text (fail-open contract).

    Parameters
    ----------
    inputs:
        Stage inputs mapping.  Recognised keys:
        ``kb_sqlite``, ``visual_cache_dir``, ``download_dir``,
        ``papers``, ``max_visual_procurement_papers``,
        ``max_visual_procurement_successes``, ``source_task_id``.
    papers:
        Optional explicit list of S2PaperRecord objects.  When absent, falls
        back to ``inputs["papers"]``.
    work_dir:
        Optional work directory for default download_dir resolution.
    """
    _OFFLINE_STUB: dict[str, Any] = {
        "schema_version": "optomind.visual_procurement_manifest.v1",
        "status": "skip",
        "fail_open": True,
        "pending_review_candidates_traceable": True,
        "papers_checked": 0,
    }
    try:
        from optomind_research.s2_fulltext_acquisition import (
            build_visual_procurement_manifest,
        )
    except ImportError:
        return {**_OFFLINE_STUB, "status": "import_error"}

    paper_list = list(papers or [])
    if not paper_list:
        raw_papers = inputs.get("papers") or []
        if isinstance(raw_papers, (list, tuple)):
            paper_list = list(raw_papers)
    if not paper_list:
        return {**_OFFLINE_STUB, "reason": "no_papers_in_inputs"}

    # Handoff/source ledgers are plain mappings; the OA procurement contract
    # uses S2PaperRecord. Normalize at this boundary so live visual remount
    # can consume central-cache identities without requiring callers to build
    # schema objects manually.
    try:
        from optomind_research.s2_schemas import S2PaperRecord

        normalized_papers = []
        for raw in paper_list:
            if isinstance(raw, S2PaperRecord):
                normalized_papers.append(raw)
                continue
            if not isinstance(raw, Mapping):
                continue
            pid = str(raw.get("paper_id") or raw.get("s2_id") or "").strip()
            if not pid:
                continue
            normalized_papers.append(
                S2PaperRecord(
                    paper_id=pid,
                    doi=str(raw.get("doi") or "").strip(),
                    title=str(raw.get("title") or "").strip(),
                    authors=[str(a) for a in (raw.get("authors") or [])],
                    year=(
                        int(raw["year"])
                        if str(raw.get("year") or "").isdigit()
                        else None
                    ),
                    venue=str(raw.get("venue") or "").strip(),
                    is_oa=(
                        True
                        if str(raw.get("acquisition_status") or "")
                        in {"fulltext", "oa_fulltext"}
                        else raw.get("is_oa")
                    ),
                    external_ids=dict(raw.get("external_ids") or {}),
                    s2_open_access_candidate_url=str(
                        raw.get("s2_open_access_candidate_url") or ""
                    ),
                    discovery_route=str(
                        raw.get("discovery_route")
                        or "central_long_term_material_cache"
                    ),
                )
            )
        paper_list = normalized_papers
    except Exception:
        paper_list = []
    if not paper_list:
        return {**_OFFLINE_STUB, "reason": "no_valid_paper_records"}

    kb_sqlite_raw = inputs.get("kb_sqlite")
    visual_cache_dir = inputs.get("visual_cache_dir")
    download_dir_raw = inputs.get("download_dir")

    if not kb_sqlite_raw:
        return {**_OFFLINE_STUB, "reason": "kb_sqlite_not_in_inputs"}

    download_dir = Path(str(download_dir_raw)) if download_dir_raw else None
    if download_dir is None and work_dir is not None:
        download_dir = Path(work_dir) / "visual_procurement_downloads"
    if download_dir is None:
        return {**_OFFLINE_STUB, "reason": "download_dir_not_resolved"}

    visual_cache_sqlite: "Path | None" = None
    if visual_cache_dir:
        candidate = Path(str(visual_cache_dir)) / "visual_cache.sqlite"
        if candidate.is_file():
            visual_cache_sqlite = candidate
        else:
            alt = Path(str(visual_cache_dir))
            if alt.is_file() and alt.suffix == ".sqlite":
                visual_cache_sqlite = alt

    max_papers = int(inputs.get("max_visual_procurement_papers") or 8)
    max_successes = int(inputs.get("max_visual_procurement_successes") or 4)
    source_task_id = str(
        inputs.get("source_task_id") or "visual_procurement_manifest"
    )
    try:
        manifest = build_visual_procurement_manifest(
            paper_list,
            visual_cache_sqlite=visual_cache_sqlite,
            kb_sqlite=Path(str(kb_sqlite_raw)),
            download_dir=download_dir,
            max_papers=max_papers,
            max_successes=max_successes,
            source_task_id=source_task_id,
        )
        return manifest.to_dict()
    except Exception as exc:  # noqa: BLE001 – fail-open
        return {
            **_OFFLINE_STUB,
            "status": "procurement_error_fail_open",
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }



__all__ = [
    "SCHEMA_VERSION",
    "STAGE_ORDER",
    "STAGE_STATUSES",
    "REVIEW_SEVERITIES",
    "REVIEW_CONSENSUS",
    "REVIEW_DIMENSIONS",
    "SEMANTIC_PATCH_OPERATIONS",
    "STATE_JSON",
    "StagedCompletionError",
    "canonical_json",
    "fingerprint",
    "parse_fenced_json",
    "QwenStagedProvider",
    "make_qwen_stage_provider",
    "QwenMultiReviewerProvider",
    "make_multi_reviewer_qwen_provider",
    "QwenEditorialRevisionProvider",
    "make_editorial_revision_qwen_provider",
    "StagedStageState",
    "StagedArticleCompletionState",
    "SectionOrderEntry",
    "SectionResponsibility",
    "DuplicationFinding",
    "MissingAxisEntry",
    "StructureGapProposal",
    "VisualWorkOrder",
    "CommanderStructuralAuthority",
    "validate_claim_evidence_invariant_preserved",
    "build_commander_structural_authority",
    "ConclusionWorkplan",
    "ConclusionDraft",
    "IntroductionWorkplan",
    "IntroductionDraft",
    "AbstractWorkplan",
    "AbstractDraft",
    "ManuscriptReviewFinding",
    "ManuscriptReviewReport",
    "ReviewerRole",
    "MultiReviewerReport",
    "aggregate_multi_reviewer_report",
    "is_blocking_finding",
    "findings_are_fail_open",
    "BoundedPatchProposal",
    "PatchProposalSet",
    "EditorialWorkItem",
    "EditorialVerification",
    "EditorialRevisionRecord",
    "EditorialRevisionAudit",
    "normalize_patch_proposal",
    "proposal_requires_approval",
    "patch_set_requires_approval",
    "plan_editorial_work_items",
    "plan_editorial_work_items_with_provenance",
    "assemble_revised_manuscript",
    "ref_marker_multiset",
    "ref_markers_preserved",
    "verifier_accepts",
    "EDITORIAL_KINDS",
    "AUTO_EDITORIAL_OPERATIONS",
    "COMPLETE_REVIEW_MARKDOWN",
    "COMPLETE_REVIEW_MANIFEST",
    "EDITORIAL_CHECKPOINT_JSON",
    "EDITORIAL_CHECKPOINT_SCHEMA",
    "run_staged_article_completion",
    "visual_procurement_pre_step",
    "PRESENTATION_IR_SCHEMA",
    "TITLE_PLAN_SCHEMA",
    "FRONT_MATTER_STAGES",
    "LiteratureReviewPresentationIR",
    "TitleCandidate",
    "ReviewTitlePlan",
    "extract_presentation_ir",
    "plan_review_titles",
]
