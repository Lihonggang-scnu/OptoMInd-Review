"""Unified low-cost review pipeline built on the AgentScope Research Harness.

This orchestrator connects reusable factories rather than topic-specific
assets:

Query Planner artifact + canonical KB
  -> Review Lead blueprint
  -> section literature coverage / OA supplementation
  -> section authoring + managing-editor revision
  -> article-level visual editorial plan
  -> final content package
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from scripts.run_review_lead import context_from_query_plan, run_review_lead

from .artifact_store import append_jsonl, atomic_write_json
from .article_completion_runner import run_article_completion
from .article_completion_tool_provider import ArticleCompletionContext
from .article_synthesis_map_builder import collect_article_synthesis_inputs
from .article_structure_auditor import audit_complete_manuscript
from .chapter_style_governance import run_chapter_style_governance
from .final_citation_map import build_final_citation_map
from .llm_style_pipeline import (
    apply_deterministic_style_governance,
    load_protected_terms,
    run_style_convergence,
)
from .visual_transformation_workflow import (
    VisualTransformationWorkflow,
    VisualTransformationWorkflowConfig,
)
from .complete_manuscript_assembler import assemble_complete_manuscript
from .global_figure_planner import (
    build_global_figure_plan,
    merge_global_figures_into_visual_plan,
)
from .harness_observability import HarnessObservability
from .event_logger import _redact
from .full_review_orchestrator import (
    FullReviewOrchestrator,
    OrchestratorConfig,
    SectionMaterialBundle,
)
from .section_coverage_orchestrator import (
    SectionCoverageOrchestrator,
    SectionCoverageOrchestratorConfig,
    SectionCoverageOrchestratorResult,
)
from .review_content_evaluator import evaluate_review_content
from .visual_editor_runner import (
    run_visual_editor,
    visual_editor_input_fingerprint,
)
from .literature_portfolio import (
    build_literature_portfolio_report,
    build_portfolio_feedback,
)
from .latex_publication_renderer import build_latex_publication
from .visual_evidence_factory import (
    build_visual_factory_input_fingerprint,
    derive_visual_cache_namespace,
    run_visual_evidence_factory,
    scoped_visual_cache_dir,
    validate_final_visual_package_file,
)
from .scientific_chinese_translator import translate_review_package
from .research_program_runner import run_research_program
from .research_plan_publication import (
    build_bilingual_research_plan_publication,
)
from .research_program_tool_provider import ResearchProgramContext
from .delivery_contract import build_delivery_gate
from .human_decision_gate import (
    request_decision,
    expire_due_decisions,
    decision_state,
)
from .topic_identity import (
    assess_blueprint_topic_alignment,
    assess_topic_alignment,
    build_topic_identity_contract,
    topic_tokens,
)


logger = logging.getLogger(__name__)


# These are the only scientific readiness vocabularies that may cross the
# coverage -> R3 -> R4 boundary.  The harness keeps the producer vocabulary
# intact in its diagnostics, while translating it to a small set of stage
# outcomes for resume/terminal decisions.
_CANONICAL_COVERAGE_OUTCOMES = frozenset(
    {
        "material_ready",
        "material_ready_with_limits",
        "merge_required",
        "needs_more_literature",
    }
)
_CANONICAL_R3_OUTCOMES = frozenset(
    {"ready", "ready_with_limits", "merge_required", "needs_more_literature"}
)
# The S2 bootstrap producer currently seals ``v3`` reports. ``v2`` remains an
# intentional compatibility alias for already-persisted resume reports whose
# field contract is identical. Future versions must be added explicitly here;
# a prefix match would admit unvalidated contracts.
_S2_BOOTSTRAP_SUPPORTED_SCHEMA_VERSIONS = frozenset(
    {
        "review_harness.s2_bootstrap.v2",
        "review_harness.s2_bootstrap.v3",
    }
)

_COVERAGE_OUTCOME_TO_HARNESS_STATUS = {
    "material_ready": "completed",
    "material_ready_with_limits": "completed_with_limits",
    "merge_required": "merge_required",
    "needs_more_literature": "needs_more_literature",
}
_R3_OUTCOME_TO_HARNESS_STATUS = {
    "ready": "completed",
    "ready_with_limits": "completed_with_limits",
    "merge_required": "merge_required",
    "needs_more_literature": "needs_more_literature",
}
# Review findings that justify blocking final delivery.  Everything else in a
# quality report is advisory (fail-open), including reference-length
# shortfalls and future ``needs_attention`` markers.
_HARD_QUALITY_BLOCKERS = frozenset(
    {
        "final_review_missing",
        "non_english_machine_output",
        "planned_sections_not_delivered",
        "unresolved_inline_reference",
        "review_topic_identity_mismatch",
        "visual_plan_topic_identity_mismatch",
        "research_plan_topic_identity_mismatch",
        "citation_map_missing_for_referenced_review",
        "citation_map_contains_unresolved_entries",
        "selected_visual_path_missing",
    }
)

# Gate options that mean "ship it".  Membership here -- not who supplied the
# answer -- decides whether a gated stage completes.  Keying on the ledger's
# ``auto`` flag instead made a human clicking accept strictly worse than a
# human walking away: the timeout path set auto=True and completed the stage,
# while resolve_decision records auto=False for a real person, so the stage
# stayed awaiting and the run degraded.  A decision is a decision.
_GATE_ACCEPT_OPTIONS = frozenset({"accept", "approve", "approved", "yes"})

# Poll interval for the bounded human window.  The window is an upper bound,
# not a fixed cost: someone who answers in two seconds should not wait out the
# remaining twenty-eight.  An unattended run still pays the full window once
# per gate.
_GATE_POLL_SECONDS = 0.5

# Upper bound on visual transformation tasks queued from unmet visual needs.
# Each task is a bounded adapter call; the cap keeps a pathological plan from
# turning into an unbounded fan-out.  When it bites, the overflow is logged and
# counted in the stage row -- never dropped in silence.
_MAX_VISUAL_TRANSFORMATION_TASKS = 16


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _commander_section_order(
    work_order_path: Path,
    planned_section_ids: Sequence[str],
) -> Optional[List[str]]:
    """Read a complete, validated section permutation from the Commander."""

    work_order = _read_json(work_order_path)
    raw_order = work_order.get("proposed_section_order")
    if not isinstance(raw_order, list):
        return None
    order: List[str] = []
    for item in raw_order:
        value = item.get("section_id") if isinstance(item, Mapping) else item
        section_id = str(value or "").strip().upper()
        if section_id:
            order.append(section_id)
    expected = [str(value).strip().upper() for value in planned_section_ids]
    if len(order) != len(expected) or len(set(order)) != len(order):
        return None
    if set(order) != set(expected):
        return None
    return order


def _quality_report_hard_blocks(quality_report: Mapping[str, Any]) -> bool:
    """Return whether a quality report carries a delivery-blocking finding."""

    status = str(quality_report.get("status") or "")
    blockers = [
        str(item).strip()
        for item in quality_report.get("blocking_issues") or []
        if str(item).strip()
    ]
    if any(item in _HARD_QUALITY_BLOCKERS for item in blockers):
        return True
    # A failed report without a reason is malformed and cannot be safely
    # downgraded. Explicit advisory findings may fail open below.
    return status == "failed" and not blockers


def _delivery_quality_report(
    quality_report: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build the gate-facing report while preserving advisory findings.

    Advisory-only failures are downgraded to a passing delivery status with
    the original blockers retained under ``advisory_blocking_issues`` so no
    review finding is lost.
    """

    report = dict(quality_report)
    if _quality_report_hard_blocks(report):
        return report
    advisory = [
        str(item).strip()
        for item in report.get("blocking_issues") or []
        if str(item).strip() and str(item).strip() not in _HARD_QUALITY_BLOCKERS
    ]
    original_status = str(report.get("status") or "")
    if original_status in {"failed", "needs_attention"}:
        report = dict(report)
        report["status"] = "passed"
        report["original_status"] = original_status
        report["advisory_blocking_issues"] = advisory
        report["delivery_fail_open"] = True
    return report


class HarnessArtifactValidationError(RuntimeError):
    """Raised when a resumable harness artifact is present but unsafe."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _same_path(left: Any, right: Any) -> bool:
    try:
        return Path(str(left)).resolve() == Path(str(right)).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return str(left) == str(right)


def _compact_stage_detail(value: Any, *, sample_limit: int = 20) -> Any:
    """Strip bulk row/paper payloads out of a stage-state detail block.

    ``HARNESS_STATE.json`` is rewritten in full on every stage transition and
    is re-read by the UI on every poll, so anything stored here is paid for
    many times over.  Verified on rhr_be780761: the ``topic_scoped_kb`` entry
    alone was 5,555,789 B -- ``evidence.rows`` held 14,033 chunk records
    (3.64 MB) and ``selection.papers`` held 1.87 MB -- inflating the state file
    to 9,077,142 B and the corresponding event line to 5.55 MB.

    The durable copies are unaffected: ``KB_MANIFEST.json`` already persists
    the compact form (``evidence`` without ``rows``, plus a 50-row
    ``evidence_sample``), and the full chunk set lives in the SQLite overlay.
    Only counts and a short sample are kept here, so nothing that a reader
    needs is lost and nothing is silently dropped without a count.
    """

    if not isinstance(value, dict):
        return value
    compact: Dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            compact[key] = _compact_stage_detail(item, sample_limit=sample_limit)
        elif isinstance(item, list) and len(item) > sample_limit:
            compact[key + "_count"] = len(item)
            compact[key + "_sample"] = item[:sample_limit]
        else:
            compact[key] = item
    return compact


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


@dataclass
class ReviewHarnessConfig:
    query_plan_path: Path
    base_kb_sqlite: Path
    output_root: Path
    # Describes why the source database is allowed into this run.  The
    # default is intentionally neutral for library callers; the user-facing
    # CLI labels its generated paper-free seed explicitly.
    base_kb_asset_role: str = "user_supplied_research_material"
    long_term_material_cache_root: Optional[Path] = None
    long_term_material_cache_writeback: bool = True
    # ``None`` deliberately selects the checked-in production S2 policy.  A
    # caller may provide a policy path for an explicitly controlled recovery or
    # test run, but the harness never falls back to a broad-KB mode.
    s2_policy_path: Optional[Path] = None
    m1_library_path: Optional[Path] = None
    # Optional Phase-3 artifact root passed into production R4 authoring.
    # When set, the authoring worker consumes the audited CoverageAtlas,
    # SynthesisBundles, bindings, relations, and visual candidates from this
    # root instead of reconstructing them from legacy coverage folders.
    phase3_artifacts_root: Optional[Path] = None
    # Raised from 49.0 → 120.0 to give all stages comfortable headroom.
    # Typical Qwen runs cost 15–50 CNY; the ceiling is generous so
    # early-stage overruns never starve authoring or article completion.
    # F5: PDF compilation master switch. True keeps the current behaviour
    # when a LaTeX toolchain exists; if latexmk is missing the run DEGRADES
    # (skip PDF, record pdf_skipped_reason, emit an explicit event) unless
    # pdf_strict (--require-pdf) fast-fails instead.
    compile_pdf: bool = True
    pdf_strict: bool = False
    global_cost_budget_cny: float = 120.0
    review_lead_budget_cny: float = 4.0
    # Raised from 10.0 → 14.0 for broader literature coverage (more refs).
    section_coverage_budget_cny: float = 14.0
    portfolio_coverage_budget_cny: float = 4.0
    feedback_coverage_budget_cny: float = 3.0
    # Raised from 17.5 → 28.0: supports 10–12 sections at ~2 CNY each plus
    # a managing-editor audit reserve.
    authoring_budget_cny: float = 28.0
    # Raised from 2.0 → 18.0: this is the hard cap passed to
    # run_article_completion (intro/abstract/conclusion/outlook) and to the
    # publication_mainline_enhancement stage when publication_mainline is on.
    # The former 2.0 CNY cap caused budget_exhausted after a single section.
    article_completion_budget_cny: float = 18.0
    # When enabled, the per-section publication mainline replaces the legacy
    # one-call article completion path. The field is disabled by default for
    # focused/offline library callers; the production CLI enables it.
    publication_mainline_enabled: bool = False
    publication_mainline_enhancement_runner: Any = None
    publication_mainline_enhancement_qwen_caller: Any = None
    publication_mainline_local_metadata_db_path: Optional[Path] = None
    publication_mainline_local_search_callback: Any = None
    publication_mainline_s2_search_callback: Any = None
    publication_mainline_commander_role_provider: Any = None
    publication_mainline_staged_providers: Optional[Dict[str, Any]] = None
    publication_mainline_commander_model_tier: str = "c2_model"
    publication_mainline_staged_model_tier: str = "c_model"
    publication_mainline_staged_reviewer_tier: str = "c2_model"
    publication_mainline_staged_editorial_verifier_tier: str = "c2_model"
    publication_mainline_representative_applications_enabled: bool = True
    publication_mainline_application_max_targets: int = 5
    publication_mainline_application_soft_min_targets: int = 4
    publication_mainline_application_per_target_cap: int = 6
    publication_mainline_application_local_max_results: int = 6
    publication_mainline_application_writer_tier: str = "c2_model"
    publication_mainline_s2_metadata_fallback: bool = True
    publication_mainline_enhancement_workers: int = 3
    publication_mainline_staged_editorial_workers: int = 3
    # Shared cap for visual planning, selected-asset audit, and bounded image
    # generation.  It is one envelope rather than additive sub-budgets.
    # Offline/library callers retain the historical 2.5 CNY envelope.  The
    # user-facing CLI explicitly selects the balanced 3.0 CNY profile.
    # Quality-first visual envelope: four conceptual requests must have
    # enough room for generation and audit reservations after planning.
    visual_editor_budget_cny: float = 5.0
    visual_real_audit: bool = False
    visual_real_generation: bool = False
    visual_test_mode: bool = True
    visual_image_model: str = "qwen-image-2.0-pro"
    # Matches MAX_CONCEPTUAL_FIGURE_REQUESTS (4): the editor is allowed to
    # request four conceptual figures, so the factory must be allowed to
    # generate four.  See visual_evidence_factory.max_generated_images.
    visual_max_generated_images: int = 4
    execution_profile: str = "library_offline"
    visual_fulltext_processing: bool = False
    oa_fulltext_paper_cap: int = 0
    visual_review_auto_accept_seconds: Optional[float] = None
    # P0-1 (round 3): every human gate waits at most this many seconds and
    # then auto-accepts (default_option = accept).  ``None`` or ``<=0``
    # restores the historical infinite-wait behaviour.  Overridable via the
    # HUMAN_GATE_AUTO_ACCEPT_SECONDS environment variable.
    human_gate_auto_accept_seconds: Optional[float] = 30.0
    # P1-1 (round 3): distribution-driven LLM style governance.  Disabled at
    # the dataclass level so offline/library runs stay zero-API; the user-facing
    # CLI enables it.  Budget envelope 0.50 CNY / max 60 rewrites per run.
    llm_style_pipeline_enabled: bool = False
    llm_style_pipeline_budget_cny: float = 0.50
    llm_style_pipeline_max_rewrites: int = 60
    # P1-2: chapter-scoped reviewer/author governance.  The production CLI
    # inherits this switch from the live style profile; the dataclass stays
    # disabled so focused/offline callers remain zero-API by default.
    chapter_style_governance_enabled: bool = False
    chapter_style_governance_budget_cny: float = 0.75
    chapter_style_governance_workers: int = 6
    chapter_style_reviewer_model_tier: str = "c_model"
    chapter_style_reviser_model_tier: str = "c2_model"
    visual_workers: int = 2
    research_plan_budget_cny: float = 4.0
    produce_research_plan: bool = True
    produce_research_plan_publication: bool = False
    research_plan_translation_cost_budget_cny: float = 0.5
    # Kept disabled at the dataclass level so focused/offline unit tests do not
    # invoke external publication binaries.  The user-facing CLI enables the
    # deterministic stage by default.
    produce_latex_publication: bool = False
    # Also disabled at the dataclass level for offline tests.  The CLI enables
    # the bilingual deliverable by default whenever LaTeX publication is on.
    produce_chinese_publication: bool = False
    publication_metadata_path: Optional[Path] = None
    # When True (production default), the publication metadata resolver is
    # allowed to call online providers (OpenAlex, Crossref, Semantic Scholar)
    # to enrich bibliography entries.  Set to False for offline/test runs to
    # avoid network calls without having to toggle visual_test_mode.
    publication_metadata_online: bool = True
    latex_enrich_crossref: bool = True
    latex_render_previews: bool = True
    translation_model_tier: str = "c2_model"
    translation_fallback_model_tier: str = "c_model"
    translation_workers: int = 3
    # Raised from 1.0 → 3.0 for quality Chinese translation of a full article.
    translation_cost_budget_cny: float = 3.0
    # fail-open by default: translation failure produces a partial draft
    # rather than aborting the run. Pass False for strict / CI contexts.
    translation_fail_open: bool = True
    # Optional cost already spent by an upstream Query Planner invocation.
    # It is included in the same global budget instead of disappearing from
    # the final report.
    upstream_cost_cny: float = 0.0
    upstream_input_tokens: int = 0
    upstream_output_tokens: int = 0
    # The production CLI uses one shared CNY pool when the operator does not
    # provide legacy per-stage overrides.  Library callers keep the historical
    # hard-cap behaviour by default so existing focused tests and integrations
    # remain compatible.  In global-only mode each stage receives the current
    # remaining pool at admission time; the outer ledger, not a pre-allocated
    # sum of stage ceilings, is the budget authority.
    global_budget_only: bool = False
    review_lead_model_tier: str = "premium_model"
    coverage_model_tier: str = "advanced_model"
    author_model_tier: str = "advanced_model"
    article_completion_model_tier: str = "premium_model"
    managing_editor_model_tier: str = "premium_model"
    visual_editor_model_tier: str = "advanced_model"
    use_llm_global_audit: bool = True
    # Retry runtime-incomplete chapters after the first manuscript pass.
    # Scientific evidence gaps use the separate author-researcher feedback loop.
    max_authoring_recovery_passes: int = 2
    # These switches are recovery-only.  The production defaults always reuse
    # validated immutable manifests/handoffs and fail closed on stale or
    # incomplete artifacts rather than rebuilding them implicitly.
    rebuild_scoped_kb: bool = False
    rebuild_phase3_handoff: bool = False
    # ``None`` selects the environment-aware production policy: live CLI
    # runs use bounded real M2a, while deterministic/offline test mode stays
    # zero-API.  Explicit booleans always win over that policy.
    phase3_real_llm_claims: Optional[bool] = None
    phase3_claim_pool_enabled: Optional[bool] = None
    phase3_real_llm_dag: Optional[bool] = None
    phase3_claim_model_tier: str = "b_plus_model"
    phase3_dag_model_tier: str = "standard_model"
    phase3_max_m2a_input_tokens: int = 8_000
    phase3_max_m2a_records: int = 24
    phase3_max_dag_candidates: int = 80
    phase3_dag_claims_per_section: int = 16
    phase3_dag_total_claims: int = 128
    # Cost-aware strong claim-pool profile.  200 served chunks reproduces the
    # proven PINN-scale input; shortlist/core-author limits stay at the
    # already validated 32/12 instead of multiplying downstream tokens.
    phase3_claim_pool_served_limit: int = 200
    phase3_claim_pool_target_range: list[int] = field(
        default_factory=lambda: [100, 140]
    )
    phase3_claim_pool_shortlist_limit: int = 32
    phase3_authoring_core_chunk_limit: int = 12
    # Phase 2 owns literature acquisition, while Phase 3 is allowed to return
    # precise section/claim gaps to it.  ``None`` selects the production-safe
    # policy: a live natural-language run executes the bounded two-wave loop,
    # whereas offline/test mode remains zero-network.  Explicit True/False
    # always wins.  This prevents a partial R3 handoff from being mistaken for
    # a complete manuscript while still keeping focused unit tests deterministic.
    phase3_execute_coverage: Optional[bool] = None
    model_overrides: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewHarnessResult:
    run_id: str
    status: str
    completed_stage: str
    total_cost_cny: float
    total_input_tokens: int
    total_output_tokens: int
    work_dir: Path
    final_review_path: Optional[Path]
    visual_plan_path: Optional[Path]
    final_visual_package_path: Optional[Path]
    research_plan_path: Optional[Path]
    latex_pdf_path: Optional[Path]
    latex_source_archive_path: Optional[Path]
    chinese_review_path: Optional[Path]
    chinese_latex_pdf_path: Optional[Path]
    chinese_latex_source_archive_path: Optional[Path]
    package_path: Path
    research_plan_latex_pdf_path: Optional[Path] = None
    research_plan_chinese_latex_pdf_path: Optional[Path] = None


class ReviewHarnessOrchestrator:
    STAGES = (
        "topic_scoped_kb",
        "s2_literature_intelligence",
        "review_lead",
        "section_coverage",
        "section_coverage_portfolio",
        "phase3_argument_orchestration",
        "authoring_revision",
        "section_coverage_feedback",
        "section_supplementary_closure",
        "publication_mainline_enhancement",
        "publication_mainline_handoff",
        "publication_mainline_commander",
        "publication_mainline_staged_completion",
        "quality_review_gate",
        "llm_style_pipeline",
        "chapter_style_governance",
        "article_completion",
        "article_structure_audit",
        "visual_editor",
        "visual_materialization",
        "research_plan",
        "packaging",
        "latex_publication",
        "chinese_translation",
        "latex_publication_zh",
        "research_plan_publication",
    )

    @staticmethod
    def _cumulative_stage_ceiling(
        prior_cost_cny: float,
        newly_admitted_cost_cny: float,
    ) -> float:
        """Translate a resume-only allowance into a cumulative stage ceiling."""

        return max(0.0, float(prior_cost_cny)) + max(
            0.0, float(newly_admitted_cost_cny)
        )

    def __init__(
        self,
        config: ReviewHarnessConfig,
        *,
        run_dir: Optional[Path] = None,
        observability: Optional[HarnessObservability] = None,
    ) -> None:
        self.config = config
        self.run_id = (
            run_dir.name if run_dir else "rhr_" + uuid.uuid4().hex[:8]
        )
        self.work_dir = run_dir or config.output_root / self.run_id
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.work_dir / "HARNESS_STATE.json"
        self.cost_path = self.work_dir / "HARNESS_COST.json"
        self.package_path = self.work_dir / "REVIEW_CONTENT_PACKAGE.json"
        state_existed = self.state_path.exists()
        # Recovery from a paid snapshot is deliberately limited to an
        # existing harness run.  A caller that merely points a fresh run at an
        # old coverage directory must go through normal acquisition instead.
        self._resumed_existing_run = bool(state_existed)
        self.state = _read_json(self.state_path) or {
            "schema_version": "research_harness.state.v1",
            "run_id": self.run_id,
            "status": "running",
            "current_stage": "initializing",
            "error_count": 0,
            "stages": {},
            "created_at": _now(),
        }
        self.state.setdefault("error_count", 0)
        stored_canonical_stages = self.state.get("canonical_stages")
        canonical_stages = (
            [
                str(stage).strip()
                for stage in stored_canonical_stages
                if str(stage).strip()
            ]
            if isinstance(stored_canonical_stages, (list, tuple))
            else []
        )
        for stage in ("query_planner", *self.STAGES):
            if stage not in canonical_stages:
                canonical_stages.append(stage)
        self.state["canonical_stages"] = canonical_stages
        self.observability = observability or HarnessObservability(
            self.work_dir,
            self.run_id,
        )
        if observability is None:
            self.observability.start_run(
                entry_mode="orchestrator_direct",
                resumed=state_existed,
            )
        self.stage_costs: Dict[str, Dict[str, Any]] = dict(
            _read_json(self.cost_path).get("stages", {})
        )
        self._merge_append_only_stage_cost_floors()
        self._merge_query_planner_cost_artifact()
        self._repair_stale_terminal_receipt()
        # Background central-cache write-back threading primitives.
        # Threads are launched from _sync_central_material_cache and joined
        # in _finish so the process never exits while a sync is in flight.
        self._writeback_threads: list[threading.Thread] = []
        self._writeback_lock = threading.Lock()
        # Round-2 defect A: set once the S2 stage resolves its runtime KB;
        # read later by the visual-editor candidate resolution.  The plain
        # ``runtime_kb`` local and ``config.base_kb_sqlite`` are both subject
        # to the blueprint projection overwrite and must not be trusted here.
        self._s2_visual_kb: Optional[Path] = None
        # P0-1: environment override for the human-gate timeout.
        _env_gate_seconds = os.environ.get("HUMAN_GATE_AUTO_ACCEPT_SECONDS")
        if _env_gate_seconds:
            try:
                self.config.human_gate_auto_accept_seconds = float(
                    _env_gate_seconds
                )
            except ValueError:
                logger.warning(
                    "ignoring non-numeric HUMAN_GATE_AUTO_ACCEPT_SECONDS=%r",
                    _env_gate_seconds,
                )

    @staticmethod
    def _stage_cost_cny(value: Mapping[str, Any]) -> float:
        """Read one numeric CNY value without summing legacy aliases."""

        for key in ("cost_cny", "estimated_cost_cny", "total_cost_cny"):
            if value.get(key) is not None:
                try:
                    return float(value.get(key) or 0.0)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    @staticmethod
    def _stage_token_count(value: Mapping[str, Any], key: str) -> int:
        for candidate in (key, f"total_{key}"):
            if value.get(candidate) is not None:
                try:
                    return int(value.get(candidate) or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    @staticmethod
    def _query_planner_model_call_count(value: Mapping[str, Any]) -> int:
        for key in ("model_call_count", "model_calls", "call_count"):
            if value.get(key) is not None:
                try:
                    return max(0, int(value.get(key) or 0))
                except (TypeError, ValueError):
                    return 0
        status = str(value.get("status") or "")
        if status in {"provided_confirmed_plan", "reused_confirmed_query_plan"}:
            return 0
        if status in {
            "repaired_by_format_model",
            "deterministic_fallback_after_repair_failed",
        }:
            return 2
        if any(
            ReviewHarnessOrchestrator._stage_token_count(value, key) > 0
            for key in ("input_tokens", "output_tokens")
        ) or ReviewHarnessOrchestrator._stage_cost_cny(value) > 0:
            return 1
        return 0

    def _merge_query_planner_cost_artifact(self) -> None:
        """Admit upstream Query Planner spend exactly once on resume.

        The Query Planner is an upstream stage and normally has no leaf
        ``EVENTS.jsonl``.  Its durable cost artifact is therefore the fallback
        source for canonical totals when the caller did not pass upstream
        metrics explicitly.  Existing values are merged with max semantics so
        config, timeline, and artifact receipts cannot be added twice.
        """

        artifact_path = (
            self.work_dir / "query_planner" / "QUERY_PLANNER_COST.json"
        )
        artifact = _read_json(artifact_path)
        if not artifact:
            return
        artifact_plan = artifact.get("compact_query_plan")
        if (
            artifact_plan
            and Path(str(artifact_plan)).exists()
            and self.config.query_plan_path.exists()
            and not _same_path(artifact_plan, self.config.query_plan_path)
        ):
            return
        previous = dict(self.stage_costs.get("query_planner", {}))
        artifact_cost = self._stage_cost_cny(artifact)
        previous_cost = self._stage_cost_cny(previous)
        cost_cny = round(max(previous_cost, artifact_cost), 6)
        input_tokens = max(
            self._stage_token_count(previous, "input_tokens"),
            self._stage_token_count(artifact, "input_tokens"),
        )
        output_tokens = max(
            self._stage_token_count(previous, "output_tokens"),
            self._stage_token_count(artifact, "output_tokens"),
        )
        model_call_count = max(
            self._query_planner_model_call_count(previous),
            self._query_planner_model_call_count(artifact),
        )
        previous.update(
            {
                "cost_cny": cost_cny,
                "estimated_cost_cny": cost_cny,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model_call_count": model_call_count,
                "source": "query_planner_cost_artifact",
            }
        )
        self.stage_costs["query_planner"] = previous
        self.config.upstream_cost_cny = max(
            float(self.config.upstream_cost_cny or 0.0), cost_cny
        )
        self.config.upstream_input_tokens = max(
            int(self.config.upstream_input_tokens or 0), input_tokens
        )
        self.config.upstream_output_tokens = max(
            int(self.config.upstream_output_tokens or 0), output_tokens
        )

    def _repair_stale_terminal_receipt(self) -> None:
        """Repair state left running when a prior terminal receipt was durable."""

        if self.state.get("status") != "running":
            return
        events_path = self.observability.events_path
        if not events_path.exists():
            return
        try:
            events = [
                json.loads(line)
                for line in events_path.read_text(
                    encoding="utf-8",
                ).splitlines()
                if line.strip()
            ]
        except (OSError, UnicodeDecodeError, TypeError, ValueError):
            return
        terminal = next(
            (
                event
                for event in reversed(events)
                if isinstance(event, dict)
                and event.get("event") == "run_finished"
                and str(event.get("status") or "") != "running"
            ),
            None,
        )
        if not terminal:
            return
        status = str(terminal.get("status") or "failed")
        current_stage = str(
            terminal.get("current_stage")
            or terminal.get("completed_stage")
            or self.state.get("current_stage")
            or "orchestrator"
        ).strip() or "orchestrator"
        metrics = _read_json(self.observability.metrics_path)
        error_count = int(
            terminal.get("error_count")
            if terminal.get("error_count") is not None
            else (metrics.get("operations", {}) or {}).get("error_count", 0)
            or 0
        )
        self.state["status"] = status
        self.state["current_stage"] = current_stage
        self.state["error_count"] = error_count
        metrics.setdefault("schema_version", "research_harness.metrics.v1")
        metrics.setdefault("run_id", self.run_id)
        metrics["status"] = status
        metrics["current_stage"] = current_stage
        metrics["completed_stage"] = current_stage
        metrics.setdefault("operations", {})["error_count"] = error_count
        metrics["reconciled_at"] = _now()
        if terminal.get("reconciliation_id"):
            reconciliation_id = str(terminal["reconciliation_id"])
            self.state["terminal_reconciliation_id"] = reconciliation_id
            metrics["terminal_reconciliation_id"] = reconciliation_id
        atomic_write_json(self.observability.metrics_path, metrics)
        self._save_cost()
        self._save_state()

    def _merge_append_only_stage_cost_floors(self) -> None:
        """Recover monotonic cost/token totals from the append-only timeline.

        A resumed stage can finish quickly because it reuses prior artifacts.
        Its current-attempt counters must not overwrite the larger historical
        totals already paid for that stage.  HARNESS_EVENTS.jsonl is append-only
        and therefore acts as the recovery source if HARNESS_COST.json was
        written by an older non-monotonic version.
        """

        events_path = self.work_dir / "HARNESS_EVENTS.jsonl"
        if not events_path.exists():
            return
        try:
            lines = events_path.read_text(
                encoding="utf-8"
            ).splitlines()
        except (OSError, UnicodeDecodeError):
            return
        for line in lines:
            try:
                event = json.loads(line)
            except Exception:
                continue
            if (
                not isinstance(event, dict)
                or event.get("event") != "stage_finished"
                or not event.get("stage")
            ):
                continue
            stage = str(event["stage"])
            previous = dict(self.stage_costs.get(stage, {}))
            previous_cost = self._stage_cost_cny(previous)
            event_cost = self._stage_cost_cny(event)
            previous["cost_cny"] = max(previous_cost, event_cost)
            previous["estimated_cost_cny"] = previous["cost_cny"]
            previous["input_tokens"] = max(
                self._stage_token_count(previous, "input_tokens"),
                self._stage_token_count(event, "input_tokens"),
            )
            previous["output_tokens"] = max(
                self._stage_token_count(previous, "output_tokens"),
                self._stage_token_count(event, "output_tokens"),
            )
            previous["wall_time_seconds"] = max(
                float(previous.get("wall_time_seconds", 0.0) or 0.0),
                float(event.get("wall_time_seconds", 0.0) or 0.0),
            )
            if event.get("model_call_count") is not None:
                previous["model_call_count"] = max(
                    int(previous.get("model_call_count", 0) or 0),
                    int(event.get("model_call_count", 0) or 0),
                )
            self.stage_costs[stage] = previous

    @staticmethod
    def _guard_resume_artifact_shape(
        root: Path,
        required_names: Iterable[str],
        *,
        stage: str,
    ) -> None:
        """Reject orphaned resume files instead of silently rebuilding them."""

        required = [Path(root) / name for name in required_names]
        present = [path for path in required if path.exists()]
        if present and len(present) != len(required):
            missing = [path.name for path in required if not path.exists()]
            raise HarnessArtifactValidationError(
                f"{stage} has an incomplete resumable artifact set; "
                f"missing {', '.join(missing)}"
            )

    def _validate_topic_scoped_manifest(
        self,
        result: Mapping[str, Any],
        *,
        work_dir: Path,
        expected_source_base: Path,
        stage: str,
    ) -> tuple[dict[str, Any], Path]:
        """Validate one immutable KB manifest against the current run inputs."""

        if str(result.get("status") or "") == "failed":
            raise HarnessArtifactValidationError(
                f"{stage} producer rejected its artifacts: "
                f"{result.get('error') or result.get('error_code') or 'unknown error'}"
            )
        expected_manifest = work_dir / "KB_MANIFEST.json"
        manifest_path = expected_manifest
        if not manifest_path.is_file() or not _is_within(manifest_path, work_dir):
            raise HarnessArtifactValidationError(
                f"{stage} manifest is missing from its current stage directory"
            )
        manifest = _read_json(manifest_path)
        if manifest.get("schema_version") != "optomind.topic_scoped_kb_manifest.v1":
            raise HarnessArtifactValidationError(
                f"{stage} manifest schema is incompatible"
            )
        stored_hash = str(manifest.get("manifest_sha256") or "")
        body = dict(manifest)
        body.pop("manifest_sha256", None)
        if not stored_hash or stored_hash != _canonical_sha256(body):
            raise HarnessArtifactValidationError(
                f"{stage} manifest integrity hash failed"
            )

        expected_runtime = work_dir / "review_knowledge_base.s2.sqlite"
        runtime_kb = expected_runtime
        if not runtime_kb.is_file() or not _is_within(runtime_kb, work_dir):
            raise HarnessArtifactValidationError(
                f"{stage} runtime overlay is missing from its current stage directory"
            )
        try:
            if str(manifest.get("source_base_kb_sha256") or "") != _sha256_file(
                expected_source_base
            ):
                raise HarnessArtifactValidationError(
                    f"{stage} manifest source KB hash does not match the current input"
                )
            if str(manifest.get("runtime_kb_sha256") or "") != _sha256_file(
                runtime_kb
            ):
                raise HarnessArtifactValidationError(
                    f"{stage} manifest runtime overlay hash does not match the artifact"
                )
        except OSError as exc:
            raise HarnessArtifactValidationError(
                f"{stage} manifest input hash could not be checked: {exc}"
            ) from exc

        from .s2_policy_runtime import load_s2_policy
        from .topic_scoped_kb_stage import (
            SCOPE_DECISION_RULE_VERSION,
            _reuse_contract_is_valid,
            derive_topic_scope_contract,
        )

        current_plan = _read_json(self.config.query_plan_path)
        current_contract = derive_topic_scope_contract(current_plan)
        if not current_contract.valid:
            raise HarnessArtifactValidationError(
                f"{stage} current query plan cannot form a valid topic scope"
            )
        reuse_contract = manifest.get("reuse_contract")
        if not _reuse_contract_is_valid(reuse_contract):
            raise HarnessArtifactValidationError(
                f"{stage} manifest lacks a valid reuse contract"
            )
        if (
            manifest.get("scope_decision_rule_version")
            != SCOPE_DECISION_RULE_VERSION
        ):
            raise HarnessArtifactValidationError(
                f"{stage} manifest uses a stale scope-decision rule"
            )
        try:
            current_policy = load_s2_policy(self.config.s2_policy_path)
        except Exception as exc:
            raise HarnessArtifactValidationError(
                f"{stage} current S2 policy cannot be validated: {exc}"
            ) from exc
        components = dict(reuse_contract.get("components") or {})
        expected_components = {
            "query_plan_semantic_sha256": _canonical_sha256(current_plan),
            "source_base_kb_sha256": _sha256_file(expected_source_base),
            "effective_policy_sha256": _canonical_sha256(current_policy.to_dict()),
            "scope_contract_sha256": current_contract.contract_sha256,
        }
        for name, expected in expected_components.items():
            if components.get(name) != expected:
                raise HarnessArtifactValidationError(
                    f"{stage} manifest current-input mismatch: {name}"
                )
        telemetry_path = work_dir / "S2_QUERY_TELEMETRY.json"
        if not telemetry_path.is_file() or not _is_within(telemetry_path, work_dir):
            raise HarnessArtifactValidationError(
                f"{stage} query telemetry is missing from its current stage directory"
            )
        if str(manifest.get("telemetry_sha256") or "") != _sha256_file(
            telemetry_path
        ):
            raise HarnessArtifactValidationError(
                f"{stage} query telemetry hash does not match the artifact"
            )
        manifest_contract = manifest.get("scope_contract") or {}
        if str(manifest_contract.get("contract_sha256") or "") != current_contract.contract_sha256:
            raise HarnessArtifactValidationError(
                f"{stage} manifest topic scope does not match the current query plan"
            )
        status = str(manifest.get("status") or result.get("status") or "")
        if status not in {"completed", "partial", "needs_more_literature"}:
            raise HarnessArtifactValidationError(
                f"{stage} manifest has unsafe status: {status or 'missing'}"
            )
        return manifest, runtime_kb

    @staticmethod
    def _kb_paths_with_visual_tables(candidates: List[Path]) -> List[Path]:
        """Keep only KB files that expose a visual table (ticket 2.1).

        Order is preserved so callers control priority.  A library without
        visual_chunks/units cannot yield any visual candidate and only
        misleads consumers that trust the provided paths.
        """

        import sqlite3 as _sqlite3

        selected: List[Path] = []
        seen: set[str] = set()
        for raw in candidates:
            try:
                path = Path(raw)
            except (TypeError, ValueError):
                continue
            if not path.is_file():
                continue
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                continue
            try:
                conn = _sqlite3.connect(
                    f"file:{path.resolve().as_posix()}?mode=ro", uri=True
                )
                try:
                    tables = {
                        str(row[0])
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table'"
                        ).fetchall()
                    }
                finally:
                    conn.close()
            except _sqlite3.Error:
                continue
            if ("visual_chunks" in tables) or ("units" in tables):
                seen.add(key)
                selected.append(path)
        return selected

    def _prepare_topic_scoped_kb(self, source_base_kb: Path) -> dict[str, Any]:
        """Build or validate the run-local retrieval overlay before S2."""

        from .topic_scoped_kb_stage import build_topic_scoped_kb

        work_dir = self.work_dir / "topic_scoped_kb"
        required = (
            work_dir / "KB_MANIFEST.json",
            work_dir / "review_knowledge_base.s2.sqlite",
            work_dir / "S2_QUERY_TELEMETRY.json",
        )
        if self.config.rebuild_scoped_kb and any(path.exists() for path in required):
            self._archive_invalid_stage(
                "topic_scoped_kb",
                work_dir,
                reason="explicit_scoped_kb_recovery_rebuild",
            )
        self._guard_resume_artifact_shape(
            work_dir,
            (
                "KB_MANIFEST.json",
                "review_knowledge_base.s2.sqlite",
                "S2_QUERY_TELEMETRY.json",
            ),
            stage="topic_scoped_kb",
        )
        result = build_topic_scoped_kb(
            query_plan_path=self.config.query_plan_path,
            base_kb_sqlite=source_base_kb,
            work_dir=work_dir,
            policy_path=self.config.s2_policy_path,
            extra_manifest={
                "producer": "ReviewHarnessOrchestrator",
                "retrieval_scope": "topic_scoped_overlay",
                "broad_base_kb_used_for_retrieval": False,
                "source_base_asset_role": self.config.base_kb_asset_role,
            },
        )
        manifest, runtime_kb = self._validate_topic_scoped_manifest(
            result,
            work_dir=work_dir,
            expected_source_base=source_base_kb,
            stage="topic_scoped_kb",
        )
        return {
            **dict(result),
            "status": str(manifest.get("status") or result.get("status") or ""),
            "runtime_kb_sqlite": str(runtime_kb),
            "manifest_path": str(work_dir / "KB_MANIFEST.json"),
            "manifest": manifest,
            "reused": bool(result.get("reused")),
        }

    def _prepare_s2_kb(
        self,
        *,
        scoped_runtime_kb: Path,
    ) -> dict[str, Any]:
        """Run S2 only against the already scoped overlay."""

        from optomind_research.s2_harness_bootstrap import (
            prepare_s2_harness_kb,
        )

        work_dir = self.work_dir / "s2_literature_intelligence"
        required = (
            work_dir / "S2_BOOTSTRAP_REPORT.json",
            work_dir / "KB_MANIFEST.json",
            work_dir / "review_knowledge_base.s2.sqlite",
            work_dir / "S2_QUERY_TELEMETRY.json",
            work_dir / "S2_LITERATURE_GRAPH.json",
        )
        if self.config.rebuild_scoped_kb and any(path.exists() for path in required):
            self._archive_invalid_stage(
                "s2_literature_intelligence",
                work_dir,
                reason="explicit_scoped_kb_recovery_rebuild_dependency",
            )
        self._guard_resume_artifact_shape(
            work_dir,
            (
                "S2_BOOTSTRAP_REPORT.json",
                "KB_MANIFEST.json",
                "review_knowledge_base.s2.sqlite",
                "S2_QUERY_TELEMETRY.json",
                "S2_LITERATURE_GRAPH.json",
            ),
            stage="s2_literature_intelligence",
        )
        report = prepare_s2_harness_kb(
            query_plan_path=self.config.query_plan_path,
            base_kb_sqlite=scoped_runtime_kb,
            work_dir=work_dir,
            policy_path=self.config.s2_policy_path,
            visual_fulltext_processing=self.config.visual_fulltext_processing,
            oa_fulltext_paper_cap=self.config.oa_fulltext_paper_cap,
        )
        if not isinstance(report, dict):
            raise HarnessArtifactValidationError(
                "s2_literature_intelligence returned a non-object report"
            )
        if str(report.get("status") or "") == "failed":
            raise HarnessArtifactValidationError(
                "s2_literature_intelligence rejected its artifacts: "
                + str(report.get("error") or report.get("error_code") or "unknown error")
            )
        report_path = work_dir / "S2_BOOTSTRAP_REPORT.json"
        if not report_path.is_file() or not _is_within(report_path, work_dir):
            raise HarnessArtifactValidationError(
                "s2_literature_intelligence report is missing from its current stage directory"
            )
        persisted_report = _read_json(report_path)
        if (
            persisted_report.get("schema_version")
            not in _S2_BOOTSTRAP_SUPPORTED_SCHEMA_VERSIONS
        ):
            raise HarnessArtifactValidationError(
                "s2_literature_intelligence report schema is incompatible"
            )
        report_body = dict(persisted_report)
        report_hash = str(report_body.pop("report_sha256", ""))
        if not report_hash or report_hash != _canonical_sha256(report_body):
            raise HarnessArtifactValidationError(
                "s2_literature_intelligence report integrity hash failed"
            )
        if str(persisted_report.get("source_base_kb_sha256") or "") != _sha256_file(
            scoped_runtime_kb
        ):
            raise HarnessArtifactValidationError(
                "s2_literature_intelligence source KB hash does not match the scoped input"
            )
        manifest, runtime_kb = self._validate_topic_scoped_manifest(
            {
                "status": persisted_report.get("status"),
            },
            work_dir=work_dir,
            expected_source_base=scoped_runtime_kb,
            stage="s2_literature_intelligence",
        )
        if str(persisted_report.get("kb_manifest_sha256") or "") != str(
            manifest.get("manifest_sha256") or ""
        ):
            raise HarnessArtifactValidationError(
                "s2_literature_intelligence report is not bound to the current manifest"
            )
        graph_path = work_dir / "S2_LITERATURE_GRAPH.json"
        telemetry_path = work_dir / "S2_QUERY_TELEMETRY.json"
        for artifact, field_name in (
            (graph_path, "graph_sha256"),
            (telemetry_path, "telemetry_sha256"),
            (runtime_kb, "runtime_kb_sha256"),
        ):
            if not artifact.is_file() or not _is_within(artifact, work_dir):
                raise HarnessArtifactValidationError(
                    f"s2_literature_intelligence artifact is missing: {artifact.name}"
                )
            if str(persisted_report.get(field_name) or "") != _sha256_file(artifact):
                raise HarnessArtifactValidationError(
                    f"s2_literature_intelligence artifact hash mismatch: {artifact.name}"
                )
        report_status = str(persisted_report.get("status") or "")
        if report_status not in {"completed", "partial", "needs_more_literature"}:
            raise HarnessArtifactValidationError(
                "s2_literature_intelligence returned an unsafe status"
            )
        return {
            **persisted_report,
            "status": report_status,
            "runtime_kb_sqlite": str(runtime_kb),
            "kb_manifest_path": str(work_dir / "KB_MANIFEST.json"),
            "kb_manifest": manifest,
            "reused": bool(report.get("reused")),
        }

    @classmethod
    def _coverage_outcomes_by_section(cls, coverage: Any) -> dict[str, dict[str, Any]]:
        """Read typed coverage outcomes for the canonical Phase-3 input view."""

        manifest = _read_json(Path(getattr(coverage, "work_dir", "")) / "SECTION_COVERAGE_RUN.json")
        manifest_bundles = manifest.get("material_bundles") or {}
        outcomes: dict[str, dict[str, Any]] = {}
        for raw_record in manifest.get("sections", []) or []:
            if not isinstance(raw_record, Mapping):
                continue
            section_id = str(raw_record.get("section_id") or "")
            if not section_id:
                continue
            package: dict[str, Any] = {}
            package_ref = (manifest_bundles.get(section_id) or {}) if isinstance(manifest_bundles, Mapping) else {}
            package_path = package_ref.get("material_package_path") if isinstance(package_ref, Mapping) else None
            if not package_path:
                package_path = Path(str(raw_record.get("work_dir") or "")) / "SECTION_MATERIAL_PACKAGE.json"
            if package_path:
                package = _read_json(Path(str(package_path)))
            outcome = cls._coverage_outcome_from_record(raw_record, package)
            if not outcome:
                status = str(raw_record.get("status") or "").strip().lower()
                if status == "completed":
                    outcome = "material_ready"
                elif status in {"needs_more_literature", "budget_exhausted"}:
                    outcome = "needs_more_literature"
            if outcome:
                outcomes[section_id] = {
                    "coverage_outcome": outcome,
                    "harness_status": cls._coverage_status_from_outcome(outcome),
                    "source_record_status": str(raw_record.get("status") or ""),
                    "package_path": str(package_path) if package_path else "",
                }

        # Deterministic mocks and resumed callers may expose the typed outcome
        # directly on a material bundle without a run manifest.
        for section_id, bundle in dict(
            getattr(coverage, "material_bundles", {}) or {}
        ).items():
            section_key = str(section_id)
            if section_key in outcomes:
                continue
            candidate = {
                "coverage_outcome": getattr(bundle, "coverage_outcome", None),
                "status": getattr(bundle, "status", None),
            }
            outcome = cls._coverage_outcome_from_record(candidate)
            if outcome:
                outcomes[section_key] = {
                    "coverage_outcome": outcome,
                    "harness_status": cls._coverage_status_from_outcome(outcome),
                    "source_record_status": str(getattr(bundle, "status", "") or ""),
                    "package_path": "",
                }
        return outcomes

    @staticmethod
    def _path_is_within(root: Path, candidate: Path) -> bool:
        """Return whether ``candidate`` is inside ``root`` after resolution."""

        try:
            candidate.resolve().relative_to(root.resolve())
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    @staticmethod
    def _stable_project_relative_path(path: Path, project_root: Path) -> str:
        """Return a stable project-relative path spelling when inside root."""

        resolved = Path(path).resolve()
        try:
            relative = resolved.relative_to(Path(project_root).resolve())
            return str(relative)
        except (OSError, RuntimeError, ValueError):
            return str(resolved)

    def _rehydrate_phase3_recovery_coverage(
        self,
        *,
        blueprint: Dict[str, Any],
        topic_identity: Dict[str, Any],
        scoped_runtime_kb: Path,
        source_base_kb: Path,
    ) -> Optional[SectionCoverageOrchestratorResult]:
        """Rebuild the Phase-2 view from a paid snapshot for Phase-3 recovery.

        This is intentionally a narrow cache boundary.  It is enabled only
        for an existing harness run when the same run already contains a valid
        Phase-3 handoff/fingerprint.  Phase 3 itself decides whether that exact
        fingerprint can be reused or must be rebuilt; Phase 2 must not be paid
        for again merely to make that decision.  The method only reads and validates local JSON
        and SQLite paths; it never invokes the coverage worker, a search
        backend, an audit, a downloader, or a portfolio retry.
        """

        if not self._resumed_existing_run:
            return None

        phase3_root = Path(
            self.config.phase3_artifacts_root
            or self.work_dir / "phase3_argument_orchestration"
        )
        phase3_root = phase3_root.resolve()
        phase3_handoff = phase3_root / "R3_PRODUCTION_HANDOFF.json"
        phase3_fingerprint = phase3_root / "PHASE3_INPUT_FINGERPRINT.json"
        handoff_present = phase3_handoff.exists()
        fingerprint_present = phase3_fingerprint.exists()
        if not handoff_present and not fingerprint_present:
            # This is a fresh/early recovery request with no Phase-3 snapshot;
            # let the normal coverage path decide what to do.
            return None
        skip_handoff_validation = False
        if handoff_present and not fingerprint_present:
            # Handoff-present/fingerprint-missing is never recoverable through
            # this path: it cannot prove the paid inputs that produced it.
            raise HarnessArtifactValidationError(
                "phase3 recovery requires both R3_PRODUCTION_HANDOFF.json "
                "and PHASE3_INPUT_FINGERPRINT.json"
            )
        if fingerprint_present and not handoff_present:
            if not self.config.rebuild_phase3_handoff:
                raise HarnessArtifactValidationError(
                    "phase3 recovery requires both R3_PRODUCTION_HANDOFF.json "
                    "and PHASE3_INPUT_FINGERPRINT.json"
                )
            # Explicit crash recovery: the missing handoff is the only artifact
            # allowed to be absent. All other Phase-2/Phase-3 validations below
            # remain unchanged.
            skip_handoff_validation = True
        if not self._path_is_within(self.work_dir, phase3_root):
            raise HarnessArtifactValidationError(
                "phase3 recovery artifacts are outside the current harness run"
            )

        # Confirmed topic identity is required for both entry modes. A
        # same-run state file alone is not enough because a caller could reuse
        # a directory with a different query plan.
        topic_path = self.work_dir / "TOPIC_IDENTITY.json"
        gate_path = self.work_dir / "QUERY_PLAN_ENTRY_GATE.json"
        original_question_path = (
            self.work_dir / "query_planner" / "ORIGINAL_USER_QUESTION.json"
        )
        cached_plan_path = self.work_dir / "query_planner" / "query_plan.json"
        stored_topic = _read_json(topic_path)
        if not topic_path.is_file():
            raise HarnessArtifactValidationError(
                "phase3 recovery requires the confirmed topic identity"
            )
        expected_topic_fingerprint = str(topic_identity.get("fingerprint") or "")
        if (
            not expected_topic_fingerprint
            or stored_topic.get("fingerprint") != expected_topic_fingerprint
            or stored_topic.get("valid") is not True
        ):
            raise HarnessArtifactValidationError(
                "phase3 recovery topic confirmation does not match"
            )

        natural_language_artifacts = [
            gate_path,
            original_question_path,
            cached_plan_path,
        ]
        provided_plan_mode = not any(
            path.exists() for path in natural_language_artifacts
        )
        if not provided_plan_mode:
            if not all(path.is_file() for path in natural_language_artifacts):
                raise HarnessArtifactValidationError(
                    "phase3 recovery has an incomplete natural-language query-plan confirmation set"
                )
            entry_gate = _read_json(gate_path)
            if (
                entry_gate.get("status") != "passed"
                or entry_gate.get("execution_ready") is not True
            ):
                raise HarnessArtifactValidationError(
                    "phase3 recovery query-entry gate is not confirmed"
                )
            if not _same_path(cached_plan_path, self.config.query_plan_path):
                try:
                    if _sha256_file(cached_plan_path) != _sha256_file(
                        self.config.query_plan_path
                    ):
                        raise HarnessArtifactValidationError(
                            "phase3 recovery query plan differs from the confirmed run"
                        )
                except OSError as exc:
                    raise HarnessArtifactValidationError(
                        "phase3 recovery query plan cannot be verified"
                    ) from exc
        else:
            # Canonical provided-query-plan entry has no natural-language
            # confirmation artifacts. It must bind directly to the current
            # query-plan file through the Phase-3 input fingerprint.
            query_plan_path = Path(self.config.query_plan_path)
            if not query_plan_path.is_file():
                raise HarnessArtifactValidationError(
                    "phase3 recovery provided query plan is missing"
                )

        fingerprint = _read_json(phase3_fingerprint)
        if (
            fingerprint.get("schema_version")
            != "research_harness.phase3_input_fingerprint.v1"
            or not str(fingerprint.get("sha256") or "")
            or not isinstance(fingerprint.get("files"), Mapping)
        ):
            raise HarnessArtifactValidationError(
                "phase3 recovery fingerprint is malformed"
            )
        stored_query_hash = str(
            (fingerprint.get("files") or {}).get("query_plan") or ""
        )
        if provided_plan_mode:
            if not stored_query_hash:
                raise HarnessArtifactValidationError(
                    "phase3 recovery fingerprint is missing the query-plan hash"
                )
            try:
                current_query_hash = _sha256_file(query_plan_path)
            except OSError as exc:
                raise HarnessArtifactValidationError(
                    "phase3 recovery provided query plan cannot be verified"
                ) from exc
            if stored_query_hash != current_query_hash:
                raise HarnessArtifactValidationError(
                    "phase3 recovery fingerprint belongs to another query plan"
                )
        elif stored_query_hash and stored_query_hash != _sha256_file(
            self.config.query_plan_path
        ):
            raise HarnessArtifactValidationError(
                "phase3 recovery fingerprint belongs to another query plan"
            )

        # Validate the existing handoff before it is archived by the Phase-3
        # rebuild path.  This checks topic and exact section identity without
        # spending any model/API budget.
        if not skip_handoff_validation:
            self._validate_existing_phase3_handoff(
                handoff_path=phase3_handoff,
                blueprint=blueprint,
                topic_identity=topic_identity,
            )

        coverage_root = (self.work_dir / "section_coverage").resolve()
        project_root = Path(__file__).resolve().parents[2]
        manifest_path = coverage_root / "SECTION_COVERAGE_RUN.json"
        if not manifest_path.is_file():
            raise HarnessArtifactValidationError(
                "phase3 recovery is missing SECTION_COVERAGE_RUN.json"
            )
        manifest = _read_json(manifest_path)
        if manifest.get("schema_version") != "research_harness.section_coverage_run.v1":
            raise HarnessArtifactValidationError(
                "phase2 recovery coverage manifest schema is incompatible"
            )
        manifest_blueprint = manifest.get("blueprint_path")
        # The explicit blueprint path is the strongest local topic-boundary
        # check available in the Phase-2 manifest.  It must resolve to the
        # current review-lead blueprint, not merely to a file with a
        # similar title elsewhere on disk.
        expected_blueprint = self.work_dir / "review_lead" / "REVIEW_BLUEPRINT.json"
        if not manifest_blueprint or not _same_path(
            manifest_blueprint, expected_blueprint
        ):
            # A prior author-feedback retry can legitimately leave the shared
            # coverage directory with a manifest bound to
            # ``AUTHOR_FEEDBACK_BLUEPRINT.json``.  That manifest is not a
            # valid Phase-2 recovery snapshot for the current review-lead
            # blueprint.  Keep the safety boundary, but recover by moving only
            # this stale manifest aside and letting the normal coverage path
            # rebuild its current manifest; do not invalidate the paid section
            # packages or their supplemental KB.
            stale_archive = (
                coverage_root
                / "_invalidated"
                / "recovery_manifests"
            )
            stale_archive.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            stale_target = stale_archive / (
                f"SECTION_COVERAGE_RUN_{stamp}.json"
            )
            try:
                shutil.move(str(manifest_path), str(stale_target))
            except OSError:
                # If another resumable process already moved it, the normal
                # coverage path is still the safe fallback.
                pass
            self.observability.emit(
                "phase2_recovery_skipped",
                stage="section_coverage",
                reason="manifest_blueprint_mismatch",
                manifest_blueprint=str(manifest_blueprint),
                expected_blueprint=str(expected_blueprint),
                archived_manifest=(
                    str(stale_target) if stale_target.exists() else ""
                ),
            )
            return None

        expected_sections = {
            str(item.get("section_id"))
            for item in blueprint.get("sections", [])
            if isinstance(item, dict) and item.get("section_id")
        }
        if not expected_sections:
            raise HarnessArtifactValidationError(
                "phase3 recovery blueprint has no expected sections"
            )
        section_rows = manifest.get("sections")
        if not isinstance(section_rows, list):
            raise HarnessArtifactValidationError(
                "phase2 recovery manifest has no section records"
            )
        records_by_id: dict[str, dict[str, Any]] = {}
        allowed_statuses = {
            "completed",
            "completed_with_limits",
            "needs_more_literature",
            "failed",
            "error",
            "runtime_failed",
            "budget_exhausted",
            "running",
            "partial",
            "waiting_for_human",
        }
        for raw_record in section_rows:
            if not isinstance(raw_record, Mapping):
                raise HarnessArtifactValidationError(
                    "phase2 recovery contains a non-object section record"
                )
            section_id = str(raw_record.get("section_id") or "").strip()
            if not section_id or section_id not in expected_sections:
                raise HarnessArtifactValidationError(
                    "phase2 recovery contains an unknown section id"
                )
            if section_id in records_by_id:
                raise HarnessArtifactValidationError(
                    f"phase2 recovery contains duplicate section: {section_id}"
                )
            status = str(raw_record.get("status") or "").strip().casefold()
            if status not in allowed_statuses:
                raise HarnessArtifactValidationError(
                    f"phase2 recovery contains unsupported status for {section_id}: {status}"
                )
            records_by_id[section_id] = dict(raw_record)

        raw_bundle_map = manifest.get("material_bundles") or {}
        if not isinstance(raw_bundle_map, Mapping):
            raise HarnessArtifactValidationError(
                "phase2 recovery material_bundles is not an object"
            )
        for section_id in raw_bundle_map:
            if str(section_id) not in expected_sections:
                raise HarnessArtifactValidationError(
                    "phase2 recovery material bundle has an unknown section id"
                )

        def resolve_recovery_path(
            raw: Any,
            *,
            default: Path,
            label: str,
            within: Path,
            require_file: bool = True,
        ) -> Path:
            candidate = Path(str(raw)) if raw else default
            candidates = [candidate]
            if not candidate.is_absolute():
                candidates = [
                    coverage_root / candidate,
                    self.work_dir / candidate,
                    project_root / candidate,
                ]
            resolved: Optional[Path] = None
            for option in candidates:
                option_resolved = option.resolve()
                if not self._path_is_within(within, option_resolved):
                    continue
                if require_file and not option_resolved.is_file():
                    continue
                if not require_file and not option_resolved.exists():
                    continue
                resolved = option_resolved
                break
            if resolved is None:
                resolved = candidates[0].resolve()
                if not self._path_is_within(within, resolved):
                    raise HarnessArtifactValidationError(
                        f"phase2 recovery {label} escapes the coverage snapshot"
                    )
                if require_file and not resolved.is_file():
                    raise HarnessArtifactValidationError(
                        f"phase2 recovery {label} is missing: {resolved}"
                    )
            if require_file and not resolved.is_file():
                raise HarnessArtifactValidationError(
                    f"phase2 recovery {label} is missing: {resolved}"
                )
            return resolved

        manifest_base = manifest.get("base_kb_sqlite") or scoped_runtime_kb
        manifest_base_path = resolve_recovery_path(
            manifest_base,
            default=scoped_runtime_kb,
            label="base KB",
            within=self.work_dir,
        )
        if not manifest_base_path.is_file() or _same_path(
            manifest_base_path, source_base_kb
        ) or not _same_path(manifest_base_path, scoped_runtime_kb):
            raise HarnessArtifactValidationError(
                "phase2 recovery base KB is missing, broad, or cross-topic"
            )
        manifest_staging = manifest.get("supplemental_kb_sqlite")
        staging_path: Optional[Path] = None
        if manifest_staging:
            staging_path = resolve_recovery_path(
                manifest_staging,
                default=coverage_root / "supplemental_oa_kb.sqlite",
                label="supplemental KB",
                within=coverage_root,
            )
            if not staging_path.is_file() or not self._path_is_within(
                coverage_root, staging_path
            ):
                raise HarnessArtifactValidationError(
                    "phase2 recovery supplemental KB is missing or outside the run"
                )

        bundles: dict[str, Any] = {}
        usable_statuses = {"completed", "needs_more_literature"}
        for section_id, record in records_by_id.items():
            status = str(record.get("status") or "").strip().casefold()
            raw_bundle = raw_bundle_map.get(section_id) or {}
            if raw_bundle and not isinstance(raw_bundle, Mapping):
                raise HarnessArtifactValidationError(
                    f"phase2 recovery bundle for {section_id} is malformed"
                )
            raw_bundle = dict(raw_bundle) if isinstance(raw_bundle, Mapping) else {}
            raw_record_work_dir = str(record.get("work_dir") or "").strip()
            record_work_dir: Optional[Path] = None
            if raw_record_work_dir:
                record_work_dir = resolve_recovery_path(
                    raw_record_work_dir,
                    default=coverage_root / "sections" / section_id,
                    label=f"{section_id} work directory",
                    within=coverage_root,
                    require_file=False,
                )
                if not record_work_dir.is_dir():
                    raise HarnessArtifactValidationError(
                        f"phase2 recovery {section_id} work directory is missing: {record_work_dir}"
                    )
            package_default = (
                record_work_dir / "SECTION_MATERIAL_PACKAGE.json"
                if record_work_dir is not None
                else coverage_root / "sections" / section_id / "SECTION_MATERIAL_PACKAGE.json"
            )
            ledger_default = (
                record_work_dir / "SECTION_SOURCE_LEDGER.json"
                if record_work_dir is not None
                else coverage_root / "sections" / section_id / "SECTION_SOURCE_LEDGER.json"
            )
            has_bundle_declaration = bool(raw_bundle) or status in usable_statuses
            package_path: Optional[Path] = None
            ledger_path: Optional[Path] = None
            if has_bundle_declaration:
                package_path = resolve_recovery_path(
                    raw_bundle.get("material_package_path"),
                    default=package_default,
                    label=f"{section_id} material package",
                    within=coverage_root,
                )
                ledger_path = resolve_recovery_path(
                    raw_bundle.get("source_ledger_path"),
                    default=ledger_default,
                    label=f"{section_id} source ledger",
                    within=coverage_root,
                )
                package = _read_json(package_path)
                ledger = _read_json(ledger_path)
                if (
                    package.get("section_id") != section_id
                    or ledger.get("section_id") != section_id
                ):
                    raise HarnessArtifactValidationError(
                        f"phase2 recovery artifacts have a wrong section id: {section_id}"
                    )
                if status in usable_statuses:
                    bundle_base = raw_bundle.get("kb_sqlite") or manifest_base_path
                    bundle_base_path = resolve_recovery_path(
                        bundle_base,
                        default=manifest_base_path,
                        label=f"{section_id} KB",
                        within=self.work_dir,
                    )
                    if not bundle_base_path.is_file() or _same_path(
                        bundle_base_path, source_base_kb
                    ) or not _same_path(bundle_base_path, scoped_runtime_kb):
                        raise HarnessArtifactValidationError(
                            f"phase2 recovery {section_id} KB is missing or cross-topic"
                        )
                    bundle_staging = raw_bundle.get("staging_kb_sqlite") or staging_path
                    bundle_staging_path = None
                    if bundle_staging:
                        bundle_staging_path = resolve_recovery_path(
                            bundle_staging,
                            default=coverage_root / "supplemental_oa_kb.sqlite",
                            label=f"{section_id} staging KB",
                            within=coverage_root,
                        )
                        if not bundle_staging_path.is_file() or not self._path_is_within(
                            coverage_root, bundle_staging_path
                        ):
                            raise HarnessArtifactValidationError(
                                f"phase2 recovery {section_id} staging KB is invalid"
                            )
                    bundles[section_id] = SectionMaterialBundle(
                        material_package_path=package_path,
                        source_ledger_path=ledger_path,
                        kb_sqlite=bundle_base_path,
                        staging_kb_sqlite=bundle_staging_path,
                    )

        rehydrated_records: list[dict[str, Any]] = []
        missing_sections: list[str] = []
        for section_id in sorted(expected_sections):
            record = records_by_id.get(section_id)
            if record is None:
                missing_sections.append(section_id)
                rehydrated_records.append(
                    {
                        "section_id": section_id,
                        "status": "needs_more_literature",
                        "worker_status": "phase3_recovery_snapshot",
                        "stop_reason": "missing_section_snapshot_artifacts",
                        "recovery_gap": True,
                        "reused": True,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_cny": 0.0,
                    }
                )
            else:
                rehydrated_records.append(dict(record))

        def numeric(value: Any, default: float = 0.0) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        completed = sum(
            str(row.get("status") or "").casefold()
            in {"completed", "completed_with_limits"}
            for row in rehydrated_records
        )
        needs_more = sum(
            str(row.get("status") or "").casefold()
            in {"needs_more_literature", "budget_exhausted", "running", "partial"}
            for row in rehydrated_records
        )
        failed = sum(
            str(row.get("status") or "").casefold()
            in {"failed", "error", "runtime_failed", "waiting_for_human"}
            for row in rehydrated_records
        )
        if completed == len(expected_sections):
            overall_status = "completed"
        elif completed or needs_more:
            overall_status = "partial"
        else:
            overall_status = "failed"

        total_input = int(
            numeric(
                manifest.get("total_input_tokens"),
                sum(int(row.get("input_tokens", 0) or 0) for row in rehydrated_records),
            )
        )
        total_output = int(
            numeric(
                manifest.get("total_output_tokens"),
                sum(int(row.get("output_tokens", 0) or 0) for row in rehydrated_records),
            )
        )
        total_cost = round(
            numeric(
                manifest.get("total_cost_cny"),
                sum(numeric(row.get("cost_cny")) for row in rehydrated_records),
            ),
            6,
        )
        if total_input < 0 or total_output < 0 or total_cost < 0:
            raise HarnessArtifactValidationError(
                "phase2 recovery contains negative cost/token accounting"
            )
        portfolio_path = coverage_root / "ARTICLE_EVIDENCE_PORTFOLIO.json"
        portfolio_state = "missing_deterministic_recompute_allowed"
        if portfolio_path.is_file():
            if not _read_json(portfolio_path):
                portfolio_state = "invalid_deterministic_recompute_allowed"
            else:
                portfolio_state = "reused"
        telemetry = {
            "schema_version": "research_harness.phase2_recovery.v1",
            "reused_for_phase3_recovery": True,
            "coverage_worker_called": False,
            "coverage_search_called": False,
            "coverage_audit_called": False,
            "coverage_download_called": False,
            "portfolio_retry_allowed": False,
            "portfolio_state": portfolio_state,
            "source_manifest": str(manifest_path),
            "source_phase3_handoff": str(phase3_handoff),
            "source_phase3_fingerprint": str(phase3_fingerprint),
            "preserved_section_statuses": {
                row["section_id"]: row.get("status") for row in rehydrated_records
            },
            "missing_section_ids": missing_sections,
            "input_tokens_reused": total_input,
            "output_tokens_reused": total_output,
            "cost_cny_reused": total_cost,
        }
        atomic_write_json(coverage_root / "PHASE2_RECOVERY_TELEMETRY.json", telemetry)
        return SectionCoverageOrchestratorResult(
            run_id=str(manifest.get("run_id") or coverage_root.name),
            status=overall_status,
            sections_total=len(expected_sections),
            sections_completed=completed,
            sections_needing_more_literature=needs_more,
            sections_failed=failed,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_cost_cny=total_cost,
            work_dir=coverage_root,
            material_bundles=bundles,
            total_cost_basis=str(manifest.get("total_cost_basis") or "unavailable"),
            cost_is_estimated=bool(manifest.get("cost_is_estimated")),
            reused_for_phase3_recovery=True,
            recovery_telemetry=telemetry,
        )

    def _phase3_runtime_options(self, *, section_count: int = 0) -> dict[str, Any]:
        """Resolve the canonical Phase-3 execution policy once.

        The old harness embedded three unrelated ``False`` literals at the
        call site.  That made a live run look as if the literature were weak
        when M2a/M2b had never actually been attempted.  The policy is now
        explicit, recorded in the Phase-3 fingerprint, and still keeps unit
        tests/offline replay deterministic.
        """

        offline = bool(self.config.visual_test_mode)
        real_claims = self.config.phase3_real_llm_claims
        if real_claims is None:
            real_claims = not offline
        real_claims = bool(real_claims)
        claim_pool_enabled = self.config.phase3_claim_pool_enabled
        if claim_pool_enabled is None:
            claim_pool_enabled = bool(real_claims and not offline)

        real_dag = self.config.phase3_real_llm_dag
        if real_dag is None:
            # Fast mode is now the explicit default. The optional LLM DAG
            # enriches cross-section relation semantics, but it is not
            # required for R4 claim/evidence readiness and can push large
            # reviews to multi-hour/near-ten-hour runtimes. Only an explicit
            # configuration True (or the positive CLI flag) enables it.
            real_dag = False
        real_dag = bool(real_dag)
        execute_coverage = self.config.phase3_execute_coverage
        if execute_coverage is None:
            execute_coverage = not offline
        return {
            "offline": offline,
            "real_llm_claims": real_claims,
            "claim_pool_enabled": bool(claim_pool_enabled),
            "real_llm_dag": real_dag,
            "claim_model_tier": str(self.config.phase3_claim_model_tier or "b_plus_model"),
            "dag_model_tier": str(self.config.phase3_dag_model_tier or "standard_model"),
            "max_m2a_input_tokens": max(1000, int(self.config.phase3_max_m2a_input_tokens or 8000)),
            "max_m2a_records": max(1, int(self.config.phase3_max_m2a_records or 24)),
            "max_dag_candidates": max(1, int(self.config.phase3_max_dag_candidates or 80)),
            "dag_claims_per_section": max(
                4, int(self.config.phase3_dag_claims_per_section or 16)
            ),
            "dag_total_claims": max(
                16, int(self.config.phase3_dag_total_claims or 128)
            ),
            "claim_pool_served_limit": max(
                12, int(self.config.phase3_claim_pool_served_limit or 200)
            ),
            "claim_pool_target_range": list(
                self.config.phase3_claim_pool_target_range or [100, 140]
            ),
            "claim_pool_shortlist_limit": max(
                1, int(self.config.phase3_claim_pool_shortlist_limit or 32)
            ),
            "authoring_core_chunk_limit": max(
                4, int(self.config.phase3_authoring_core_chunk_limit or 12)
            ),
            "execute_coverage": bool(execute_coverage),
        }

    @staticmethod
    def _build_claim_pool_inventory_ledger(
        *,
        kb_path: Path,
        output_path: Path,
    ) -> dict[str, Any]:
        """Expose central-cache papers to the claim pool without changing R3 authority.

        The section source ledger remains the binding authority.  This
        inventory ledger is only a broad, auditable candidate menu for the
        batched claim-pool reader; unreviewed rows are downgraded to
        contextual support until a section binding explicitly revalidates
        scope and permission.
        """

        import sqlite3

        rows: list[dict[str, Any]] = []
        try:
            with sqlite3.connect(str(kb_path)) as connection:
                connection.row_factory = sqlite3.Row
                paper_rows = connection.execute(
                    "SELECT * FROM papers"
                ).fetchall()
                chunk_rows = connection.execute(
                    "SELECT paper_id, chunk_id FROM text_chunks"
                ).fetchall()
        except Exception as exc:
            return {
                "status": "failed",
                "error": f"{type(exc).__name__}:{exc}",
                "source_count": 0,
                "chunk_count": 0,
            }

        chunks_by_paper: dict[str, list[str]] = {}
        for row in chunk_rows:
            paper_id = str(row["paper_id"] or "").strip()
            chunk_id = str(row["chunk_id"] or "").strip()
            if paper_id and chunk_id:
                chunks_by_paper.setdefault(paper_id, []).append(chunk_id)

        for row in paper_rows:
            paper = dict(row)
            paper_id = str(paper.get("paper_id") or "").strip()
            chunk_ids = list(dict.fromkeys(chunks_by_paper.get(paper_id, [])))
            if not paper_id or not chunk_ids:
                continue
            raw: dict[str, Any] = {}
            try:
                parsed = json.loads(str(paper.get("raw_json") or "{}"))
                if isinstance(parsed, dict):
                    raw = parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = {}
            scope_fit = str(
                paper.get("scope_fit") or raw.get("scope_fit") or "unreviewed"
            ).strip()
            content_depth = str(
                paper.get("content_depth")
                or raw.get("content_depth")
                or "metadata"
            ).strip()
            permission = str(
                paper.get("use_permission")
                or raw.get("use_permission")
                or "contextual_or_qualified_support"
            ).strip()
            if scope_fit == "unreviewed" and permission == "factual_support":
                permission = "contextual_or_qualified_support"
            allowed = raw.get("allowed_claim_kinds")
            if not isinstance(allowed, list):
                allowed = [
                    "background",
                    "candidate_lead",
                    "trend",
                    "author_synthesis",
                    "paper_reported_claim",
                ]
            rows.append(
                {
                    "paper_id": paper_id,
                    "doi": str(paper.get("doi") or ""),
                    "title": str(paper.get("title") or ""),
                    "year": paper.get("year"),
                    "venue": str(paper.get("venue") or ""),
                    "literature_role": str(raw.get("literature_role") or ""),
                    "scope_fit": scope_fit,
                    "use_permission": permission,
                    "content_depth": content_depth,
                    "context_complete": bool(
                        raw.get(
                            "context_complete",
                            content_depth in {"fulltext", "structured_snippet"},
                        )
                    ),
                    "allowed_claim_kinds": allowed,
                    "canonical_chunk_ids": chunk_ids,
                    "acquisition_status": (
                        "fulltext"
                        if content_depth == "fulltext"
                        else "abstract_only"
                        if content_depth == "abstract_claim"
                        else "snippet"
                    ),
                    "discovery_route": str(
                        paper.get("discovery_route")
                        or raw.get("discovery_route")
                        or "central_long_term_material_cache"
                    ),
                    "materialization_route": str(
                        paper.get("materialization_route")
                        or raw.get("materialization_route")
                        or ""
                    ),
                    "route_events": raw.get("route_events") or [],
                    "not_usable_for": raw.get("not_usable_for") or [],
                }
            )
        atomic_write_json(
            output_path,
            {
                "schema_version": (
                    "research_harness.central_claim_pool_inventory_ledger.v1"
                ),
                "source_kb": str(kb_path),
                "authority": "claim_pool_candidate_inventory_only",
                "binding_authority": "SHARED_SECTION_SOURCE_LEDGER.json",
                "sources": rows,
            },
        )
        return {
            "status": "completed",
            "source_count": len(rows),
            "chunk_count": sum(len(row["canonical_chunk_ids"]) for row in rows),
            "path": str(output_path),
        }

    def _build_phase3_inputs(
        self,
        *,
        blueprint: Dict[str, Any],
        coverage: Any,
        scoped_runtime_kb: Path,
        source_base_kb: Path,
        phase3_root: Path,
        runtime_options: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Materialize deterministic Phase-3 routing views from coverage."""

        from .coverage_atlas import build_coverage_atlas
        from .section_asset_overlay import build_section_asset_overlay

        input_root = phase3_root / "input"
        input_root.mkdir(parents=True, exist_ok=True)
        project_root = Path(__file__).resolve().parents[2]

        material_bundles = dict(getattr(coverage, "material_bundles", {}) or {})
        adaptive_outcomes = self._coverage_outcomes_by_section(coverage)
        runtime_options = dict(runtime_options or {})
        coverage_manifest = _read_json(
            Path(coverage.work_dir) / "SECTION_COVERAGE_RUN.json"
        )
        runtime_failures: dict[str, dict[str, Any]] = {}
        for raw in coverage_manifest.get("sections", []) or []:
            if not isinstance(raw, dict):
                continue
            section_id = str(raw.get("section_id") or "").strip()
            status = str(raw.get("status") or "").strip().casefold()
            worker_status = str(raw.get("worker_status") or "").strip().casefold()
            # A bounded search that ends with no candidates is a scientific
            # coverage outcome, not a crashed worker.  Only explicit runtime
            # markers are routed to R3's engineering-failure channel; the
            # actual claim/chunk binding remains responsible for readiness.
            explicit_runtime_failure = bool(
                worker_status in {
                    "failed", "error", "runtime_failed", "exception", "crashed"
                }
                or status in {"error", "runtime_failed", "exception"}
                or raw.get("runtime_error")
                or raw.get("exception")
                or raw.get("exception_type")
            )
            if section_id and explicit_runtime_failure:
                runtime_failures[section_id] = {
                    "section_id": section_id,
                    "kind": "runtime_failure",
                    "status": status or worker_status or "failed",
                    "worker_status": worker_status,
                    "reason": str(
                        raw.get("error")
                        or raw.get("failure_reason")
                        or raw.get("stop_reason")
                        or "section coverage worker failed"
                    ),
                    "source": "section_coverage_manifest",
                }
        sections = [
            dict(item)
            for item in blueprint.get("sections", [])
            if isinstance(item, dict) and item.get("section_id")
        ]

        shared_kb_paths: list[Path] = []

        def add_kb_path(raw: Any) -> None:
            if not raw:
                return
            candidate = Path(str(raw))
            if not candidate.exists() or _same_path(candidate, source_base_kb):
                # The broad core KB is never a Phase-3 source once the scoped
                # overlay exists.  Supplemental run-local KBs remain allowed.
                return
            resolved = candidate.resolve()
            if resolved not in shared_kb_paths:
                shared_kb_paths.append(resolved)

        # The S2 overlay is the first and authoritative Phase-3 input.  A
        # section's supplemental OA KB may add material, but cannot replace
        # the scoped base or reintroduce the broad core database.
        add_kb_path(scoped_runtime_kb)
        claim_pool_inventory_ledger_path: Optional[Path] = None
        if bool(runtime_options.get("claim_pool_enabled")):
            claim_pool_inventory_ledger_path = (
                input_root / "CENTRAL_CLAIM_POOL_INVENTORY_LEDGER.json"
            )
            inventory_summary = self._build_claim_pool_inventory_ledger(
                kb_path=scoped_runtime_kb,
                output_path=claim_pool_inventory_ledger_path,
            )
            if not inventory_summary.get("source_count"):
                claim_pool_inventory_ledger_path = None
        sources_by_section: dict[str, list[dict[str, Any]]] = {}
        for section in sections:
            section_id = str(section["section_id"])
            bundle = material_bundles.get(section_id)
            rows: list[dict[str, Any]] = []
            ledger_path = getattr(bundle, "source_ledger_path", None)
            ledger = _read_json(Path(ledger_path)) if ledger_path else {}
            for raw in ledger.get("sources", []) or []:
                if not isinstance(raw, dict) or not raw.get("paper_id"):
                    continue
                row = dict(raw)
                row["section_id"] = section_id
                rows.append(row)
            sources_by_section[section_id] = rows
            if bundle is not None:
                add_kb_path(getattr(bundle, "kb_sqlite", None))
                add_kb_path(getattr(bundle, "staging_kb_sqlite", None))

        combined_sources: list[dict[str, Any]] = []
        seen_source_keys: set[tuple[str, str]] = set()
        overlay_paths: dict[str, Path] = {}
        for section in sections:
            section_id = str(section["section_id"])
            rows = sources_by_section.get(section_id, [])
            for row in rows:
                key = (section_id, str(row.get("paper_id") or ""))
                if key in seen_source_keys:
                    continue
                seen_source_keys.add(key)
                combined_sources.append(row)
            overlay_path = (
                input_root / "sections" / section_id / "SECTION_ASSET_OVERLAY.json"
            )
            build_section_asset_overlay(
                section_id=section_id,
                sources=rows,
                shared_kb_paths=shared_kb_paths,
                output_path=overlay_path,
            )
            overlay_paths[section_id] = overlay_path

        shared_ledger_path = input_root / "SHARED_SECTION_SOURCE_LEDGER.json"
        atomic_write_json(
            shared_ledger_path,
            {
                "schema_version": "research_harness.phase3_shared_source_ledger.v1",
                "sections": sorted(sources_by_section),
                "sources": combined_sources,
                "adaptive_coverage_outcomes": adaptive_outcomes,
                "source_of_truth": "review_harness_section_coverage",
            },
        )
        adaptive_outcomes_path = input_root / "ADAPTIVE_COVERAGE_OUTCOMES.json"
        atomic_write_json(
            adaptive_outcomes_path,
            {
                "schema_version": "research_harness.adaptive_coverage_outcomes.v1",
                "source_of_truth": "canonical_section_coverage_packages",
                "sections": adaptive_outcomes,
                "shared_portfolio_path": self._stable_project_relative_path(
                    Path(coverage.work_dir) / "ARTICLE_EVIDENCE_PORTFOLIO.json",
                    project_root,
                ),
            },
        )
        runtime_failures_path = phase3_root / "RUNTIME_FAILURES.json"
        atomic_write_json(
            runtime_failures_path,
            {
                "schema_version": "research_harness.phase3_runtime_failures.v1",
                "sections": runtime_failures,
                "source": "section_coverage_manifest",
            },
        )

        scope_map = dict(blueprint.get("review_scope_map") or {})
        try:
            coverage_atlas = build_coverage_atlas(
                blueprint=blueprint,
                coverage_root=Path(coverage.work_dir),
                scope_map=scope_map,
            )
        except Exception as exc:
            # Phase 3 must still emit a fail-closed handoff when coverage is
            # empty or structurally incomplete.  The missing material remains
            # visible in the handoff rather than being replaced by a broad KB.
            coverage_atlas = {
                "schema_version": "research_harness.coverage_atlas.v1",
                "section_count": len(sections),
                "sections": [
                    {
                        "section_id": str(item["section_id"]),
                        "needs_expansion": True,
                        "phase3_input_error": f"{type(exc).__name__}: {exc}",
                    }
                    for item in sections
                ],
                "relation_graph": {"edge_count": 0, "semantic_relation_counts": {}},
            }

        relation_graph = _read_json(
            Path(coverage.work_dir) / "RELATION_GRAPH.json"
        )
        if not relation_graph:
            raw_relation_graph = blueprint.get("relation_graph")
            relation_graph = (
                dict(raw_relation_graph)
                if isinstance(raw_relation_graph, dict)
                else {"edges": []}
            )

        input_files: dict[str, str] = {
            "query_plan": _sha256_file(self.config.query_plan_path),
            "blueprint": _canonical_sha256(blueprint),
            "shared_ledger": _sha256_file(shared_ledger_path),
            "adaptive_coverage_outcomes": _sha256_file(adaptive_outcomes_path),
            "runtime_failures": _sha256_file(runtime_failures_path),
            "phase3_runtime_options": _canonical_sha256(runtime_options),
        }
        if claim_pool_inventory_ledger_path is not None:
            input_files["claim_pool_inventory_ledger"] = _sha256_file(
                claim_pool_inventory_ledger_path
            )
        for section_id, path in sorted(overlay_paths.items()):
            input_files[f"overlay:{section_id}"] = _sha256_file(path)
        for path in shared_kb_paths:
            input_files[f"kb:{path}"] = _sha256_file(path)
        input_fingerprint = _canonical_sha256(input_files)
        fingerprint_path = phase3_root / "PHASE3_INPUT_FINGERPRINT.json"
        atomic_write_json(
            fingerprint_path,
            {
                "schema_version": "research_harness.phase3_input_fingerprint.v1",
                "sha256": input_fingerprint,
                "files": input_files,
                "scoped_runtime_kb": str(scoped_runtime_kb),
                "broad_base_kb_excluded": str(source_base_kb),
            },
        )
        return {
            "shared_ledger_path": shared_ledger_path,
            "claim_pool_inventory_ledger_path": claim_pool_inventory_ledger_path,
            "adaptive_outcomes_path": adaptive_outcomes_path,
            "adaptive_outcomes": adaptive_outcomes,
            "runtime_failures_path": runtime_failures_path,
            "runtime_failures": runtime_failures,
            "runtime_options": runtime_options,
            "shared_kb_paths": shared_kb_paths,
            "overlay_paths": overlay_paths,
            "coverage_atlas": coverage_atlas,
            "relation_graph": relation_graph,
            "input_fingerprint": input_fingerprint,
            "fingerprint_path": fingerprint_path,
        }

    @staticmethod
    def _outcome_value(value: Any) -> str:
        """Extract a canonical outcome from a string or artifact row."""

        if isinstance(value, Mapping):
            for key in (
                "coverage_outcome",
                "readiness_outcome",
                "section_outcome",
                "outcome",
                "readiness_status",
                "status",
            ):
                candidate = value.get(key)
                if candidate is not None:
                    return str(candidate).strip().lower()
            return ""
        return str(value or "").strip().lower()

    @classmethod
    def _coverage_outcome_from_record(
        cls,
        record: Mapping[str, Any] | None,
        package: Mapping[str, Any] | None = None,
    ) -> str:
        """Return the adaptive coverage outcome without trusting metadata."""

        candidates: list[Mapping[str, Any]] = [package or {}, record or {}]
        for parent in (package or {}, record or {}):
            for key in (
                "adaptive_coverage",
                "coverage_readiness",
                "coverage_decision",
            ):
                nested = parent.get(key)
                if isinstance(nested, Mapping):
                    candidates.append(nested)
        for candidate in candidates:
            outcome = cls._outcome_value(candidate)
            if outcome in _CANONICAL_COVERAGE_OUTCOMES:
                return outcome
        # A legacy completed record predates the typed adaptive field.  It is
        # safe to treat that record as material_ready only when the producer
        # actually marked the section completed; factual permission is still
        # checked by R3 and is never inferred from metadata here.
        if cls._outcome_value(record) == "completed":
            return "material_ready"
        return ""

    @staticmethod
    def _coverage_status_from_outcome(outcome: Any) -> str:
        """Translate one canonical coverage outcome to a harness status."""

        value = ReviewHarnessOrchestrator._outcome_value(outcome)
        return _COVERAGE_OUTCOME_TO_HARNESS_STATUS.get(value, "failed")

    @staticmethod
    def _r3_status_from_outcome(outcome: Any) -> str:
        """Translate one canonical R3 section outcome to a harness status."""

        value = ReviewHarnessOrchestrator._outcome_value(outcome)
        return _R3_OUTCOME_TO_HARNESS_STATUS.get(value, "failed")

    @staticmethod
    def _coverage_outcome_to_harness_status(outcome: Any) -> str:
        """Publicly named alias used by integration diagnostics and tests."""

        return ReviewHarnessOrchestrator._coverage_status_from_outcome(outcome)

    @staticmethod
    def _r3_outcome_to_harness_status(outcome: Any) -> str:
        """Publicly named alias used by integration diagnostics and tests."""

        return ReviewHarnessOrchestrator._r3_status_from_outcome(outcome)

    def _build_coverage_config(
        self,
        *,
        blueprint_path: Path,
        base_kb_sqlite: Path,
        source_base_kb: Path,
        output_root: Path,
        stage_cost_budget_cny: float,
        cost_budget_per_section_cny: float | None = None,
        staging_kb_path: Path | None = None,
        author_feedback_by_section: Mapping[str, Any] | None = None,
        force_research_sections: Iterable[str] = (),
        retry_label: str = "initial",
        preserve_existing_manifest: bool = False,
        bounded_search: bool = False,
    ) -> SectionCoverageOrchestratorConfig:
        """Build every harness coverage call from one bounded control contract."""

        output_root = Path(output_root)
        base_kb_sqlite = Path(base_kb_sqlite)
        source_base_kb = Path(source_base_kb)
        shared_kb_paths: list[Path] = []
        # The scoped/S2 runtime KB is the only base that may enter coverage.
        # Keep the broad source path out even if a caller accidentally passes it
        # as a shared path during a retry.
        if base_kb_sqlite.is_file() and not _same_path(
            base_kb_sqlite, source_base_kb
        ):
            shared_kb_paths.append(base_kb_sqlite)
        if (
            staging_kb_path is not None
            and Path(staging_kb_path).is_file()
            and not _same_path(staging_kb_path, source_base_kb)
        ):
            shared_kb_paths.append(Path(staging_kb_path))

        if bounded_search:
            max_queries_per_call = 2
            max_results_per_backend = 8
            max_materialized_papers_per_section = 4
            max_search_rounds_per_role = 2
        else:
            max_queries_per_call = 3
            max_results_per_backend = 12
            max_materialized_papers_per_section = 6
            max_search_rounds_per_role = 3

        return SectionCoverageOrchestratorConfig(
            blueprint_path=Path(blueprint_path),
            base_kb_sqlite=base_kb_sqlite,
            output_root=output_root,
            model_tier=self.config.coverage_model_tier,
            model_override=self.config.model_overrides.get("section_coverage"),
            token_budget_per_section=96_000,
            context_tokens_per_model_call=32_000,
            context_output_reserve_tokens=2_000,
            model_context_budget_per_section=96_000,
            max_model_calls_per_section=6,
            max_coverage_waves=2,
            max_audit_calls_per_section=2,
            adaptive_coverage_enabled=True,
            s2_first_enabled=True,
            stage_cost_budget_cny=max(0.0, float(stage_cost_budget_cny)),
            cost_budget_per_section_cny=max(
                0.0,
                float(
                    cost_budget_per_section_cny
                    if cost_budget_per_section_cny is not None
                    else min(2.5, float(stage_cost_budget_cny))
                ),
            ),
            max_queries_per_call=max_queries_per_call,
            max_results_per_backend=max_results_per_backend,
            max_materialized_papers_per_section=(
                max_materialized_papers_per_section
            ),
            max_search_rounds_per_role=max_search_rounds_per_role,
            max_runtime_restarts_per_section=1,
            staging_kb_path=(Path(staging_kb_path) if staging_kb_path else None),
            author_feedback_by_section=dict(author_feedback_by_section or {}),
            force_research_sections=[str(item) for item in force_research_sections],
            retry_label=str(retry_label),
            preserve_existing_manifest=bool(preserve_existing_manifest),
            shared_kb_sqlite_paths=shared_kb_paths,
            cross_wave_state_path=output_root / "COVERAGE_CROSS_WAVE_STATE.json",
            global_coverage_ledger_path=self.work_dir
            / "COVERAGE_GLOBAL_LEDGER.json",
            article_evidence_portfolio_path=(
                output_root / "ARTICLE_EVIDENCE_PORTFOLIO.json"
            ),
        )

    def _cache_question(self) -> str:
        plan = _read_json(self.config.query_plan_path)
        input_row = plan.get("input")
        input_row = input_row if isinstance(input_row, Mapping) else {}
        output = plan.get("output")
        output = output if isinstance(output, Mapping) else {}
        return str(
            input_row.get("user_query")
            or output.get("problem_understanding")
            or self.run_id
        )

    def _sync_central_material_cache(
        self,
        *,
        source_stage: str,
        kb_paths: Iterable[Path],
    ) -> dict[str, Any]:
        """Persist newly materialized run text in the background.

        Launches a daemon thread so the write-back never serially blocks the
        next pipeline stage.  Threads are joined in _finish, guaranteeing that
        no sync is abandoned mid-write when the process exits.  Conflict
        detection is handled inside sync_review_kbs_to_central; any error is
        recorded as ``pending_recovery`` so operators can replay the sync.
        """

        root = self.config.long_term_material_cache_root
        if root is None or not self.config.long_term_material_cache_writeback:
            return {"status": "disabled"}

        _paths = list(kb_paths)
        _source_stage = source_stage
        _root = root
        sync_dir = (
            self.work_dir
            / "long_term_material_cache_sync"
            / re.sub(r"[^A-Za-z0-9_.-]+", "_", source_stage)
        )
        _question = self._cache_question()
        _run_id = self.run_id

        def _run_sync() -> None:
            from .central_material_cache import sync_review_kbs_to_central

            try:
                report: dict[str, Any] = sync_review_kbs_to_central(
                    kb_paths=_paths,
                    question=_question,
                    run_id=_run_id,
                    source_stage=_source_stage,
                    work_dir=sync_dir,
                    cache_root=_root,
                )
            except Exception as exc:
                report = {
                    "status": "pending_recovery",
                    "source_stage": _source_stage,
                    "error": f"{type(exc).__name__}:{exc}",
                    "source_kbs": [str(p) for p in _paths],
                }
                atomic_write_json(
                    sync_dir / "CENTRAL_CACHE_SYNC_REPORT.json",
                    report,
                )
            with self._writeback_lock:
                history = self.state.setdefault(
                    "central_material_cache_sync", {}
                ).setdefault("history", [])
                history.append(report)
                self.state["central_material_cache_sync"]["latest"] = report
                self._save_state()
            self.observability.emit(
                "central_material_cache_sync",
                stage=_source_stage,
                status=str(report.get("status") or ""),
                new_unit_count=int(report.get("new_unit_count") or 0),
                snapshot=str(report.get("snapshot") or ""),
            )

        thread = threading.Thread(
            target=_run_sync,
            daemon=True,
            name=f"cache-writeback-{source_stage}",
        )
        with self._writeback_lock:
            self._writeback_threads.append(thread)
        thread.start()
        return {"status": "background_started", "source_stage": source_stage}

    def _expand_central_projection_for_blueprint(
        self,
        *,
        blueprint: Mapping[str, Any],
    ) -> Path | None:
        """Broaden the local view using section-specific semantic duties."""

        if (
            self.config.long_term_material_cache_root is None
            or self.config.base_kb_asset_role
            != "central_long_term_material_cache_projection"
        ):
            return None
        section_queries: list[str] = []
        for section in blueprint.get("sections") or []:
            if not isinstance(section, Mapping):
                continue
            fields = [
                section.get("section_title") or section.get("title"),
                section.get("central_thesis")
                or section.get("central_judgment"),
                section.get("section_purpose"),
                section.get("argument_role"),
                section.get("core_question"),
                section.get("synthesis_task"),
                *(section.get("key_questions") or []),
            ]
            query = " | ".join(
                " ".join(str(value).split())
                for value in fields
                if str(value or "").strip()
            )
            if query:
                section_queries.append(query[:1800])
        if not section_queries:
            return None
        from .central_material_cache import project_to_review_kb
        from .topic_scoped_kb_stage import build_topic_scoped_kb

        scoped_dir_early = self.work_dir / "topic_scoped_kb_blueprint"
        _early_sqlite = scoped_dir_early / "review_knowledge_base.s2.sqlite"
        _early_manifest_path = scoped_dir_early / "KB_MANIFEST.json"

        def _blueprint_qp_sha_matches(manifest_path: "Path") -> bool:
            try:
                m = json.loads(manifest_path.read_text(encoding="utf-8"))
                stored = str(m.get("query_plan_sha256") or "")
                return bool(stored and stored == _sha256_file(self.config.query_plan_path))
            except Exception:
                return False

        # Reuse guard: if a valid blueprint KB is already present for the
        # same query plan, skip the expensive re-projection entirely.
        if _early_sqlite.is_file() and _early_manifest_path.is_file():
            if _blueprint_qp_sha_matches(_early_manifest_path):
                self.state.setdefault("central_material_cache_sync", {})[
                    "blueprint_expansion"
                ] = {"status": "reused", "scoped_kb": str(_early_sqlite)}
                self._save_state()
                return _early_sqlite

        # Restore guard: if the blueprint KB was archived to a stale dir for
        # the same query plan (e.g. contract mismatch after a code-version
        # change), restore the most-recent matching stale copy before
        # re-projecting.  This avoids needlessly re-running the expensive
        # project_to_review_kb step for a resume that already has valid work.
        if not _early_sqlite.is_file():
            stale_candidates = sorted(
                scoped_dir_early.glob("_stale_scoped_kb_*"),
                key=lambda p: p.name,
                reverse=True,
            )
            for _stale_dir in stale_candidates:
                _stale_sqlite = _stale_dir / "review_knowledge_base.s2.sqlite"
                _stale_manifest = _stale_dir / "KB_MANIFEST.json"
                if _stale_sqlite.is_file() and _stale_manifest.is_file():
                    if _blueprint_qp_sha_matches(_stale_manifest):
                        import shutil as _shutil
                        _shutil.copy2(_stale_sqlite, _early_sqlite)
                        _shutil.copy2(_stale_manifest, _early_manifest_path)
                        _stale_telemetry = _stale_dir / "S2_QUERY_TELEMETRY.json"
                        if _stale_telemetry.is_file():
                            _shutil.copy2(
                                _stale_telemetry,
                                scoped_dir_early / "S2_QUERY_TELEMETRY.json",
                            )
                        self.state.setdefault("central_material_cache_sync", {})[
                            "blueprint_expansion"
                        ] = {
                            "status": "restored_from_stale",
                            "stale_dir": str(_stale_dir),
                            "scoped_kb": str(_early_sqlite),
                        }
                        self._save_state()
                        return _early_sqlite

        projection_path = (
            self.work_dir
            / "task_material"
            / "LONG_TERM_MATERIAL_BLUEPRINT_PROJECTION.sqlite"
        )
        projection_report_path = projection_path.with_suffix(".json")
        project_to_review_kb(
            query_plan_path=self.config.query_plan_path,
            output_kb_path=projection_path,
            cache_root=self.config.long_term_material_cache_root,
            report_path=projection_report_path,
            extra_query_texts=section_queries,
            max_queries=max(24, len(section_queries) + 12),
            top_k_per_query=360,
            max_selected_works=600,
            max_projected_units=16_000,
        )
        scoped_dir = self.work_dir / "topic_scoped_kb_blueprint"
        scoped = build_topic_scoped_kb(
            query_plan_path=self.config.query_plan_path,
            base_kb_sqlite=projection_path,
            work_dir=scoped_dir,
            policy_path=self.config.s2_policy_path,
            extra_manifest={
                "source": "central_material_cache_blueprint_expansion",
                "section_query_count": len(section_queries),
            },
        )
        runtime_path = Path(str(scoped.get("runtime_kb_sqlite") or ""))
        if not runtime_path.is_file():
            self.state.setdefault("central_material_cache_sync", {})[
                "blueprint_expansion"
            ] = {
                "status": "failed",
                "error": str(scoped.get("error") or "missing projected KB"),
                "query_count": len(section_queries),
            }
            self._save_state()
            return None
        self.state.setdefault("central_material_cache_sync", {})[
            "blueprint_expansion"
        ] = {
            "status": scoped.get("status") or "completed",
            "query_count": len(section_queries),
            "projection_report": str(projection_report_path),
            "projection_kb": str(projection_path),
            "scoped_kb": str(runtime_path),
            "soft_served_portfolio_target": [200, 250],
            "soft_target_policy": (
                "below-target sections continue with explicit audit; no hard gate"
            ),
        }
        self._save_state()
        return runtime_path

    @classmethod
    def _r3_section_outcomes(cls, report: Any) -> dict[str, str]:
        """Read per-section outcomes from canonical R3, never global aliases."""

        outcomes: dict[str, str] = {}
        readiness = getattr(report, "section_readiness", {}) or {}
        if isinstance(readiness, Mapping):
            for section_id, row in readiness.items():
                outcome = cls._outcome_value(row)
                if outcome in _CANONICAL_R3_OUTCOMES:
                    outcomes[str(section_id)] = outcome
        return outcomes

    @classmethod
    def _phase3_acceptance_hard_failure(
        cls,
        acceptance: Mapping[str, Any],
    ) -> bool:
        """Return True only for genuine scientific/runtime/permission blocks."""

        if not isinstance(acceptance, Mapping):
            return True
        safety = acceptance.get("engineering_safety") or {}
        material = acceptance.get("material_quality") or {}
        cost = acceptance.get("cost") or {}
        hard_flags = (
            acceptance.get("claim_quality_passed") is False,
            acceptance.get("evidence_permission_passed") is False,
            acceptance.get("coverage_request_quality_passed") is False,
            acceptance.get("verifier_batch_budget_passed") is False,
            acceptance.get("duplicate_bundle_categories_detected") is True,
            safety.get("all_ids_traceable") is False,
            safety.get("relation_revalidation_passed") is False,
            safety.get("coverage_atlas_uses_migrated_relation_graph") is False,
            int(cost.get("runtime_failure_count") or 0) > 0,
            material.get("generic_claims_detected") is True,
        )
        return any(hard_flags)

    @classmethod
    def _phase3_status(
        cls,
        *,
        report: Any,
        acceptance: Mapping[str, Any] | None = None,
    ) -> str:
        """Map canonical R3 readiness, including valid partial handoffs."""

        if not getattr(report, "valid", False):
            return "failed"
        global_readiness = getattr(report, "global_readiness", {}) or {}
        global_status = str(global_readiness.get("status") or "").strip().lower()
        section_outcomes = cls._r3_section_outcomes(report)
        ready_ids = {
            section_id
            for section_id, outcome in section_outcomes.items()
            if outcome in {"ready", "ready_with_limits"}
        }
        ready_ids.update(
            str(section_id)
            for section_id in (global_readiness.get("ready_section_ids") or [])
        )

        # An acceptance failure is an engineering/permission failure when the
        # report claims there is an authorable section.  For an all-weak report,
        # the same ``r4_handoff_ready: false`` is the truthful scientific
        # ``needs_more_literature`` outcome, not a harness crash.
        if acceptance and ready_ids:
            if cls._phase3_acceptance_hard_failure(acceptance):
                return "failed"

        if global_status == "ready_for_authoring" and not section_outcomes:
            return "completed"
        if global_status == "ready_for_authoring" and ready_ids:
            if any(
                outcome == "ready_with_limits"
                for outcome in section_outcomes.values()
            ):
                return "completed_with_limits"
            return "completed"
        if global_status == "ready_with_limits" and not section_outcomes:
            return "completed_with_limits"
        if global_status == "ready_with_limits" and ready_ids:
            return "completed_with_limits"
        if ready_ids:
            # A valid canonical report with a partial handoff is authorable for
            # its ready sections; unresolved optional sections remain explicit
            # R3 gaps/merge tasks and are not silently discarded.
            return "completed_with_limits"

        if global_status == "needs_more_literature":
            return "needs_more_literature"
        if global_status == "merge_required":
            return "merge_required"
        if section_outcomes:
            translated = {
                cls._r3_status_from_outcome(outcome)
                for outcome in section_outcomes.values()
            }
            if translated == {"merge_required"}:
                return "merge_required"
            if "needs_more_literature" in translated:
                return "needs_more_literature"
        return "failed"

    @staticmethod
    def _phase3_input_budget_limit(section_count: int) -> int:
        """Return a review-size-aware Phase-3 input-token ceiling.

        The former fixed 50k ceiling made a multi-section real run fail even
        when every individual batch was bounded and the stage cost was small.
        Keep 50k for small jobs, then allocate 25k input tokens per planned
        section.  This is an observability/cost warning ceiling; per-call
        and per-batch bounds remain the hard safety limits.
        """

        return max(50_000, max(1, int(section_count)) * 25_000)

    def _reconcile_phase3_acceptance(
        self,
        *,
        acceptance_path: Path,
        report: Any,
    ) -> Dict[str, Any]:
        """Refresh a stale acceptance record from a currently valid handoff.

        Canonical R3 validation rules can become stricter or fix a false
        positive without changing the scientific input fingerprint.  In that
        case an old ``PHASE3_ACCEPTANCE.json`` must not force another expensive
        M2a/M2b run.  Reconciliation is deterministic and is allowed only when
        all independent quality checks still pass under the current policy.
        """

        acceptance = _read_json(acceptance_path)
        if not acceptance or not getattr(report, "valid", False):
            return acceptance

        global_readiness = dict(getattr(report, "global_readiness", {}) or {})
        ready_ids = {
            section_id
            for section_id, outcome in self._r3_section_outcomes(report).items()
            if outcome in {"ready", "ready_with_limits"}
        }
        ready_ids.update(
            str(section_id)
            for section_id in global_readiness.get("ready_section_ids", [])
            if str(section_id)
        )
        if not ready_ids:
            return acceptance

        independent_boolean_checks = (
            "engineering_passed",
            "claim_quality_passed",
            "evidence_permission_passed",
            "coverage_request_quality_passed",
            "argument_task_coverage_passed",
            "effective_statement_propagation_passed",
            "full_blueprint_context_passed",
            "context_value_handoff_passed",
            "content_depth_aggregation_passed",
            "verifier_batch_budget_passed",
        )
        failed_checks = [
            name for name in independent_boolean_checks
            if acceptance.get(name) is False
        ]
        if acceptance.get("duplicate_bundle_categories_detected") is True:
            failed_checks.append("duplicate_bundle_categories_detected")
        if (acceptance.get("engineering_safety") or {}).get("passes") is False:
            failed_checks.append("engineering_safety.passes")
        if (acceptance.get("material_quality") or {}).get("passes") is False:
            failed_checks.append("material_quality.passes")

        section_count = int(global_readiness.get("section_count") or 0)
        input_budget_limit = self._phase3_input_budget_limit(section_count)
        estimated_input_tokens = int(
            (acceptance.get("cost") or {}).get("estimated_input_tokens_total")
            or (acceptance.get("cost") or {}).get("input_tokens_observed")
            or 0
        )
        input_budget_passed = estimated_input_tokens <= input_budget_limit
        if failed_checks:
            return acceptance

        refreshed = dict(acceptance)
        refreshed.update(
            {
                "status": "passed",
                "r4_handoff_ready": True,
                "partial_handoff_allowed": True,
                "r4_ready_section_ids": sorted(ready_ids),
                "section_outcomes": self._r3_section_outcomes(report),
                "input_budget_passed": input_budget_passed,
                "input_budget_limit": input_budget_limit,
                "input_budget_status": (
                    "passed"
                    if input_budget_passed
                    else "warning_exceeds_aggregate_observability_budget"
                ),
                "input_budget_warning": not input_budget_passed,
                "r4_handoff_ready_explicit": True,
                "acceptance_reconciliation": {
                    "status": "revalidated_without_model_calls",
                    "timestamp": _now(),
                    "reason": (
                        "canonical_handoff_passes_current_validation_and_all_"
                        "independent_acceptance_checks"
                    ),
                    "prior_status": str(acceptance.get("status") or ""),
                    "prior_r4_handoff_ready": acceptance.get("r4_handoff_ready"),
                    "input_budget_limit": input_budget_limit,
                    "estimated_input_tokens": estimated_input_tokens,
                    "model_calls_added": 0,
                },
                "r3_production_handoff": {
                    **dict(acceptance.get("r3_production_handoff") or {}),
                    "validation_status": str(getattr(report, "status", "passed")),
                    "validation_errors": [],
                    "global_readiness": global_readiness,
                },
            }
        )
        atomic_write_json(acceptance_path, refreshed)
        self.observability.emit(
            "phase3_acceptance_reconciled",
            stage="phase3_argument_orchestration",
            ready_section_ids=sorted(ready_ids),
            input_budget_limit=input_budget_limit,
            estimated_input_tokens=estimated_input_tokens,
            model_calls_added=0,
        )
        return refreshed

    def _validate_existing_phase3_handoff(
        self,
        *,
        handoff_path: Path,
        blueprint: Dict[str, Any],
        topic_identity: Dict[str, Any],
    ) -> tuple[Any, Any]:
        from .r3_production_handoff import read_r3_production_handoff

        try:
            handoff, report = read_r3_production_handoff(handoff_path)
        except Exception as exc:
            raise HarnessArtifactValidationError(
                f"R3 handoff could not be read: {type(exc).__name__}: {exc}"
            ) from exc
        if not report.valid:
            raise HarnessArtifactValidationError(
                "R3 handoff validation failed: " + "; ".join(report.errors[:8])
            )
        expected_fingerprint = str(topic_identity.get("fingerprint") or "")
        actual_fingerprint = str(
            (handoff.topic_identity or {}).get("fingerprint") or ""
        )
        if expected_fingerprint and actual_fingerprint != expected_fingerprint:
            raise HarnessArtifactValidationError(
                "R3 handoff topic fingerprint does not match the current run"
            )
        expected_sections = {
            str(item.get("section_id"))
            for item in blueprint.get("sections", [])
            if isinstance(item, dict) and item.get("section_id")
        }
        if set(handoff.section_ids) != expected_sections:
            raise HarnessArtifactValidationError(
                "R3 handoff section set does not match the current blueprint"
            )
        acceptance_path = handoff_path.parent / "PHASE3_ACCEPTANCE.json"
        acceptance = self._reconcile_phase3_acceptance(
            acceptance_path=acceptance_path,
            report=report,
        )
        r3_ready_ids = {
            section_id
            for section_id, outcome in self._r3_section_outcomes(report).items()
            if outcome in {"ready", "ready_with_limits"}
        }
        r3_ready_ids.update(
            str(section_id)
            for section_id in (
                (report.global_readiness or {}).get("ready_section_ids") or []
            )
        )
        if (
            acceptance
            and r3_ready_ids
            and self._phase3_acceptance_hard_failure(acceptance)
        ):
            raise HarnessArtifactValidationError(
                "R3 handoff acceptance explicitly closed R4 authoring"
            )
        return handoff, report

    def _run_phase3_argument_orchestration(
        self,
        *,
        blueprint: Dict[str, Any],
        topic_identity: Dict[str, Any],
        coverage: Any,
        scoped_runtime_kb: Path,
        source_base_kb: Path,
    ) -> dict[str, Any]:
        """Run or safely resume the offline Phase-3 to canonical R3 handoff."""

        from .phase3_argument_orchestrator import Phase3ArgumentOrchestrator

        phase3_root = Path(
            self.config.phase3_artifacts_root
            or self.work_dir / "phase3_argument_orchestration"
        )
        phase3_root.mkdir(parents=True, exist_ok=True)
        handoff_path = phase3_root / "R3_PRODUCTION_HANDOFF.json"
        runtime_options = self._phase3_runtime_options(
            section_count=len(
                [
                    item
                    for item in blueprint.get("sections", [])
                    if isinstance(item, dict) and item.get("section_id")
                ]
            )
        )
        force_rebuild = bool(
            self.config.rebuild_phase3_handoff
            or self.config.rebuild_scoped_kb
        )
        has_prior_phase3_artifacts = any(
            item.name != "_invalidated" for item in phase3_root.iterdir()
        )
        if force_rebuild and has_prior_phase3_artifacts:
            self._archive_invalid_stage(
                "phase3_argument_orchestration",
                phase3_root,
                reason="explicit_phase3_handoff_recovery_rebuild",
            )
        elif handoff_path.exists():
            old_fingerprint = _read_json(
                phase3_root / "PHASE3_INPUT_FINGERPRINT.json"
            ).get("sha256")
            inputs = self._build_phase3_inputs(
                blueprint=blueprint,
                coverage=coverage,
                scoped_runtime_kb=scoped_runtime_kb,
                source_base_kb=source_base_kb,
                phase3_root=phase3_root,
                runtime_options=runtime_options,
            )
            if not old_fingerprint or old_fingerprint != inputs["input_fingerprint"]:
                # A missing fingerprint is not a safe cache hit: legacy
                # handoffs may have been produced before live M2a/M2b and
                # runtime-option provenance existed.  Archive the old handoff
                # and rebuild from the already-paid run-local ledgers/SQLite;
                # Phase 2 is not rerun.
                invalidation_reason = (
                    "phase3_input_fingerprint_missing"
                    if not old_fingerprint
                    else "phase3_input_fingerprint_changed"
                )
                self._archive_invalid_stage(
                    "phase3_argument_orchestration",
                    phase3_root,
                    reason=invalidation_reason,
                    details={
                        "old_fingerprint": old_fingerprint or None,
                        "new_fingerprint": inputs["input_fingerprint"],
                        "runtime_options": runtime_options,
                    },
                )
            else:
                # Only an exact input-fingerprint match is eligible for
                # validation and reuse.  A stale/legacy handoff must not be
                # allowed to block rebuilding merely because it is malformed.
                _, existing_report = self._validate_existing_phase3_handoff(
                    handoff_path=handoff_path,
                    blueprint=blueprint,
                    topic_identity=topic_identity,
                )
                status = self._phase3_status(
                    report=existing_report,
                    acceptance=_read_json(phase3_root / "PHASE3_ACCEPTANCE.json"),
                )
                details = {
                    "work_dir": str(phase3_root),
                    "r3_handoff_path": str(handoff_path),
                    "r3_validation_status": existing_report.status,
                    "r3_global_readiness": dict(existing_report.global_readiness),
                    "section_statuses": self._r3_section_outcomes(existing_report),
                    "input_fingerprint": inputs["input_fingerprint"],
                    "runtime_options": runtime_options,
                    "offline_safe": bool(runtime_options.get("offline")),
                    "reused": True,
                }
                atomic_write_json(
                    phase3_root / "PHASE3_STAGE_METRICS.json",
                    {"status": status, **details},
                )
                self.config.phase3_artifacts_root = phase3_root
                return {"status": status, **details}

        prior_outputs = [
            item
            for item in phase3_root.iterdir()
            if item.name not in {"_invalidated"}
        ]
        if prior_outputs:
            raise HarnessArtifactValidationError(
                "Phase-3 has prior artifacts but no canonical R3 handoff; "
                "explicit Phase-3 recovery is required"
            )
        inputs = self._build_phase3_inputs(
            blueprint=blueprint,
            coverage=coverage,
            scoped_runtime_kb=scoped_runtime_kb,
            source_base_kb=source_base_kb,
            phase3_root=phase3_root,
            runtime_options=runtime_options,
        )
        phase3 = Phase3ArgumentOrchestrator(
            blueprint=blueprint,
            scope_map=dict(blueprint.get("review_scope_map") or {}),
            coverage_atlas=inputs["coverage_atlas"],
            relation_graph=inputs["relation_graph"],
            shared_ledger_path=inputs["shared_ledger_path"],
            claim_pool_inventory_ledger_path=inputs.get(
                "claim_pool_inventory_ledger_path"
            ),
            shared_kb_paths=inputs["shared_kb_paths"],
            overlay_paths=inputs["overlay_paths"],
            output_dir=phase3_root,
            max_iterations=2,
            real_llm_claims=bool(runtime_options["real_llm_claims"]),
            claim_pool_enabled=bool(runtime_options["claim_pool_enabled"]),
            claim_model_tier=str(runtime_options["claim_model_tier"]),
            real_llm_dag=bool(runtime_options["real_llm_dag"]),
            dag_model_tier=str(runtime_options["dag_model_tier"]),
            max_m2a_input_tokens=int(runtime_options["max_m2a_input_tokens"]),
            max_m2a_records=int(runtime_options["max_m2a_records"]),
            max_dag_candidates=int(runtime_options["max_dag_candidates"]),
            dag_claims_per_section=int(
                runtime_options["dag_claims_per_section"]
            ),
            dag_total_claims=int(runtime_options["dag_total_claims"]),
            claim_pool_served_limit=int(
                runtime_options["claim_pool_served_limit"]
            ),
            claim_pool_target_range=list(
                runtime_options["claim_pool_target_range"]
            ),
            claim_pool_shortlist_limit=int(
                runtime_options["claim_pool_shortlist_limit"]
            ),
            authoring_core_chunk_limit=int(
                runtime_options["authoring_core_chunk_limit"]
            ),
            runtime_failures=inputs.get("runtime_failures"),
            execute_coverage=bool(runtime_options["execute_coverage"]),
            enable_fresh_evidence_semantic_judge=False,
        )
        acceptance = phase3.run()
        if not handoff_path.is_file():
            raise HarnessArtifactValidationError(
                "Phase3ArgumentOrchestrator did not produce R3_PRODUCTION_HANDOFF.json"
            )
        _, report = self._validate_existing_phase3_handoff(
            handoff_path=handoff_path,
            blueprint=blueprint,
            topic_identity=topic_identity,
        )
        acceptance = _read_json(phase3_root / "PHASE3_ACCEPTANCE.json")
        status = self._phase3_status(report=report, acceptance=acceptance)
        phase_run = _read_json(phase3_root / "PHASE3_RUN.json")
        details = {
            "work_dir": str(phase3_root),
            "r3_handoff_path": str(handoff_path),
            "r3_validation_status": report.status,
            "r3_global_readiness": dict(report.global_readiness),
            "section_statuses": (
                self._r3_section_outcomes(report)
                or dict(phase_run.get("section_statuses") or {})
            ),
            "acceptance_status": str(acceptance.get("status") or "")
            if isinstance(acceptance, dict)
            else "",
            "r4_handoff_ready": bool(
                acceptance.get("r4_handoff_ready")
            )
            if isinstance(acceptance, dict)
            else False,
            "coverage_request_count": len(phase_run.get("coverage_runs") or []),
            "input_fingerprint": inputs["input_fingerprint"],
            "runtime_options": runtime_options,
            "offline_safe": bool(runtime_options.get("offline")),
            "reused": False,
        }
        atomic_write_json(
            phase3_root / "PHASE3_STAGE_METRICS.json",
            {"status": status, **details},
        )
        self.config.phase3_artifacts_root = phase3_root
        return {"status": status, **details}

    def _run_phase3_stage_and_record(
        self,
        *,
        blueprint: Dict[str, Any],
        topic_identity: Dict[str, Any],
        coverage: Any,
        scoped_runtime_kb: Path,
        source_base_kb: Path,
    ) -> dict[str, Any]:
        self._set_stage("phase3_argument_orchestration", "running")
        try:
            result = self._run_phase3_argument_orchestration(
                blueprint=blueprint,
                topic_identity=topic_identity,
                coverage=coverage,
                scoped_runtime_kb=scoped_runtime_kb,
                source_base_kb=source_base_kb,
            )
        except Exception as exc:
            result = {
                "status": "failed",
                "work_dir": str(
                    self.config.phase3_artifacts_root
                    or self.work_dir / "phase3_argument_orchestration"
                ),
                "error": f"{type(exc).__name__}: {exc}",
                "offline_safe": True,
                "reused": False,
            }
        phase3_root = Path(
            result.get("work_dir")
            or self.config.phase3_artifacts_root
            or self.work_dir / "phase3_argument_orchestration"
        )
        phase3_cost, phase3_input_tokens, phase3_output_tokens, phase3_usage = (
            self._phase3_usage_from_artifacts(phase3_root)
        )
        self._record_stage(
            "phase3_argument_orchestration",
            str(result.get("status") or "failed"),
            phase3_cost,
            phase3_input_tokens,
            phase3_output_tokens,
            {
                key: value
                for key, value in dict(result).items()
                if key != "status"
            }
            | {
                "phase3_usage": phase3_usage,
                "model_call_count": int(phase3_usage.get("calls", 0) or 0),
            },
        )
        phase3_kbs = sorted(phase3_root.rglob("*.sqlite"))
        shared_supplemental = (
            self.work_dir / "section_coverage" / "supplemental_oa_kb.sqlite"
        )
        if shared_supplemental.is_file():
            phase3_kbs.append(shared_supplemental)
        self._sync_central_material_cache(
            source_stage="phase3_argument_orchestration",
            kb_paths=list(dict.fromkeys(phase3_kbs)),
        )
        return result

    def _require_r3_authoring_gate(
        self,
        *,
        phase3_root: Path,
        blueprint: Dict[str, Any],
    ) -> dict[str, Any]:
        """Require canonical R3 and section-level readiness before R4."""

        from .r4_phase3_artifacts import R4Phase3ArtifactStore

        store = R4Phase3ArtifactStore(phase3_root)
        try:
            store.require_canonical_handoff()
        except Exception as exc:
            return {
                "status": "failed",
                "reason": f"canonical_r3_handoff_required:{type(exc).__name__}:{exc}",
                "diagnostics": list(store.diagnostics),
            }
        expected_sections = [
            str(item.get("section_id"))
            for item in blueprint.get("sections", [])
            if isinstance(item, dict) and item.get("section_id")
        ]
        section_rows = {
            str(item.get("section_id")): item
            for item in blueprint.get("sections", [])
            if isinstance(item, dict) and item.get("section_id")
        }
        blocked: dict[str, list[str]] = {}
        blocked_outcomes: dict[str, str] = {}
        ready_sections: list[str] = []
        admitted_with_limits_sections: list[str] = []
        excluded_claim_ids_by_section: dict[str, list[str]] = {}
        retained_gap_notes_by_section: dict[str, dict[str, Any]] = {}
        phase2_advisory_outcomes: dict[str, str] = {}
        phase3_advisory_outcomes: dict[str, str] = {}
        merge_required_sections: list[str] = []
        needs_more_sections: list[str] = []
        adaptive_outcomes = _read_json(
            Path(phase3_root) / "input" / "ADAPTIVE_COVERAGE_OUTCOMES.json"
        ).get("sections", {})
        section_readiness = (
            dict(store.handoff_report.section_readiness)
            if store.handoff_report is not None
            else {}
        )
        for section_id in expected_sections:
            try:
                artifacts = store.section(section_id)
            except Exception as exc:
                artifacts = None
                blocked[section_id] = [
                    f"canonical_section_read_error:{type(exc).__name__}:{exc}"
                ]
            report_row = section_readiness.get(section_id, {})
            outcome = self._outcome_value(report_row)
            adaptive_row = (
                adaptive_outcomes.get(section_id, {})
                if isinstance(adaptive_outcomes, Mapping)
                else {}
            )
            adaptive_outcome = self._outcome_value(adaptive_row)
            # Phase-2 adaptive outcomes are advisory history once a valid
            # canonical R3 exists.  They never revoke a structurally
            # authorable R3 section.
            phase2_advisory_outcomes[section_id] = (
                adaptive_outcome or "not_recorded"
            )
            phase3_advisory_outcomes[section_id] = outcome or "not_recorded"
            if (
                artifacts is not None
                and artifacts.admitted_for_authoring
            ):
                ready_sections.append(section_id)
                if not artifacts.ready_for_authoring:
                    admitted_with_limits_sections.append(section_id)
                excluded_claim_ids_by_section[section_id] = list(
                    artifacts.excluded_claim_ids
                )
                retained_gap_notes_by_section[section_id] = {
                    "declared_limits": list(
                        report_row.get("declared_limits") or []
                    ),
                    "warnings": list(report_row.get("warnings") or []),
                    "blocking_gap_ids": list(
                        report_row.get("blocking_gap_ids") or []
                    ),
                    "unresolved_load_bearing_claim_ids": list(
                        report_row.get(
                            "unresolved_load_bearing_claim_ids"
                        )
                        or []
                    ),
                    "adaptive_coverage_outcome": adaptive_outcome,
                    "r3_outcome": outcome,
                }
                continue
            blocked_outcomes[section_id] = (
                adaptive_outcome or outcome or "blocked"
            )
            reasons = list(artifacts.diagnostics) if artifacts is not None else []
            if artifacts is not None:
                # Structural material/ownership failures stay closed even
                # when the scientific outcome is a soft shortfall.
                if not artifacts.source_ledger_path:
                    reasons.append("section_source_ledger_not_found")
                if not artifacts.kb_paths:
                    reasons.append("phase3_kb_not_found")
                if not artifacts.authorable_claim_ids:
                    reasons.append("no_authorable_claims_for_section")
            reasons.extend(
                str(item)
                for item in report_row.get("blocking_reasons") or []
            )
            if outcome:
                reasons.append(f"r3_outcome:{outcome}")
            if adaptive_outcome:
                reasons.append(f"adaptive_coverage_outcome:{adaptive_outcome}")
            if not reasons:
                reasons.append("canonical_r3_section_not_authorable")
            blocked[section_id] = list(dict.fromkeys(reasons))
            if adaptive_outcome == "merge_required" or outcome == "merge_required":
                merge_required_sections.append(section_id)
            else:
                needs_more_sections.append(section_id)

        admission = {
            "schema_version": "research_harness.r4_authoring_admission.v1",
            "source": "canonical_r3_production_handoff",
            "status": (
                "partial"
                if blocked or admitted_with_limits_sections
                else "full"
            ),
            "planned_section_ids": expected_sections,
            "admitted_section_ids": ready_sections,
            "admitted_with_limits_section_ids": admitted_with_limits_sections,
            "excluded_claim_ids_by_section": excluded_claim_ids_by_section,
            "not_admitted_section_ids": list(blocked),
            "blocked_sections": blocked,
            "blocked_outcomes": blocked_outcomes,
            "phase2_advisory_outcomes": phase2_advisory_outcomes,
            "phase3_advisory_outcomes": phase3_advisory_outcomes,
            "retained_gap_notes_by_section": retained_gap_notes_by_section,
            "merge_required_section_ids": merge_required_sections,
            "needs_more_literature_section_ids": needs_more_sections,
            "load_bearing_planned_section_ids": [
                section_id
                for section_id in expected_sections
                if bool(
                    section_rows.get(section_id, {}).get("load_bearing")
                    or section_rows.get(section_id, {}).get("is_load_bearing")
                    or str(section_rows.get(section_id, {}).get("risk") or "")
                    in {"load_bearing", "blocking"}
                )
            ],
        }
        atomic_write_json(Path(phase3_root) / "R4_AUTHORING_ADMISSION.json", admission)

        if ready_sections:
            return {
                "status": "passed",
                "reason": (
                    "canonical_r3_partial_handoff_admitted"
                    if blocked
                    else "canonical_r3_handoff_valid_and_all_sections_ready"
                ),
                "partial_handoff_allowed": bool(
                    blocked or admitted_with_limits_sections
                ),
                "section_ids": expected_sections,
                "ready_section_ids": ready_sections,
                "admitted_with_limits_section_ids": (
                    admitted_with_limits_sections
                ),
                "excluded_claim_ids_by_section": excluded_claim_ids_by_section,
                "phase2_advisory_outcomes": phase2_advisory_outcomes,
                "phase3_advisory_outcomes": phase3_advisory_outcomes,
                "retained_gap_notes_by_section": (
                    retained_gap_notes_by_section
                ),
                "blocked_section_ids": list(blocked),
                "blocked_sections": blocked,
                "blocked_outcomes": blocked_outcomes,
                "merge_required_section_ids": merge_required_sections,
                "needs_more_literature_section_ids": needs_more_sections,
                "admission_path": str(
                    Path(phase3_root) / "R4_AUTHORING_ADMISSION.json"
                ),
                "diagnostics": list(store.diagnostics),
            }

        # No section was admitted.  This only happens when every planned
        # section lacks its canonical structure/material/claims; scientific
        # ``needs_more_literature`` outcomes alone no longer produce this
        # terminal state.
        return {
            "status": "needs_more_literature",
            "reason": "canonical_r3_has_no_authorable_sections",
            "section_ids": expected_sections,
            "ready_section_ids": [],
            "admitted_with_limits_section_ids": [],
            "excluded_claim_ids_by_section": {},
            "phase2_advisory_outcomes": phase2_advisory_outcomes,
            "phase3_advisory_outcomes": phase3_advisory_outcomes,
            "retained_gap_notes_by_section": retained_gap_notes_by_section,
            "blocked_section_ids": list(blocked),
            "blocked_sections": blocked,
            "blocked_outcomes": blocked_outcomes,
            "merge_required_section_ids": merge_required_sections,
            "needs_more_literature_section_ids": needs_more_sections,
            "admission_path": str(
                Path(phase3_root) / "R4_AUTHORING_ADMISSION.json"
            ),
            "diagnostics": list(store.diagnostics),
        }

    def preflight(self) -> Dict[str, Any]:
        """Return the hard worst-case allocation before any model call."""

        observed_query_planner_spend = self._stage_cost_cny(
            self.stage_costs.get("query_planner", {})
        )
        if self.config.global_budget_only:
            # No stage budget is reserved in this policy.  The global ledger
            # remains the sole admission ceiling and future stages are not
            # starved by adding unrelated worst-case caps up front.
            global_budget = max(0.0, float(self.config.global_cost_budget_cny))
            observed_spend = self._total_cost_cny()
            return {
                "schema_version": "research_harness.preflight.v1",
                "global_cost_budget_cny": global_budget,
                "budget_policy": "global_only",
                # Keep the stage-cap map empty in global-only mode.  The
                # upstream allowance remains a separate informational field,
                # so it cannot be mistaken for a per-stage reservation.
                "stage_hard_caps_cny": {},
                "upstream_query_planner_allowance_cny": (
                    self.config.upstream_cost_cny
                ),
                "observed_spend_cny": {
                    "query_planner": observed_query_planner_spend,
                    "all_stages": observed_spend,
                },
                "allocated_max_cny": global_budget,
                "unallocated_reserve_cny": 0.0,
                "within_budget": observed_spend <= global_budget,
                "planning_estimate_cny": {
                    "likely_low": round(
                        self.config.upstream_cost_cny + 8.0, 2
                    ),
                    "likely_high": round(global_budget, 2),
                    "hard_admission_ceiling": global_budget,
                },
                "note": (
                    "No per-stage budgets were supplied. Every model call is "
                    "admitted from the shared global CNY balance; deterministic "
                    "stages consume no model budget."
                ),
            }
        allocations = {
            # The upstream value is an allowance in a preflight report.  It
            # may be a reservation before the Planner runs, so calling it
            # ``spent`` made zero-cost preflight look like paid usage.
            "upstream_query_planner_allowance_cny": (
                self.config.upstream_cost_cny
            ),
            "review_lead": self.config.review_lead_budget_cny,
            "section_coverage": self.config.section_coverage_budget_cny,
            "section_coverage_portfolio": (
                self.config.portfolio_coverage_budget_cny
            ),
            "authoring_revision": self.config.authoring_budget_cny,
            "article_completion": (
                0.0
                if self.config.publication_mainline_enabled
                else self.config.article_completion_budget_cny
            ),
            "publication_mainline_enhancement": (
                self.config.article_completion_budget_cny
                if self.config.publication_mainline_enabled
                else 0.0
            ),
            "publication_mainline_handoff": 0.0,
            "publication_mainline_commander": 0.0,
            "publication_mainline_staged_completion": 0.0,
            "chapter_style_governance": (
                self.config.chapter_style_governance_budget_cny
                if self.config.chapter_style_governance_enabled
                else 0.0
            ),
            "section_coverage_feedback": (
                self.config.feedback_coverage_budget_cny
            ),
            "visual_editor": self.config.visual_editor_budget_cny,
            "research_plan": (
                self.config.research_plan_budget_cny
                if self.config.produce_research_plan
                else 0.0
            ),
            "latex_publication": 0.0,
            "chinese_translation": (
                self.config.translation_cost_budget_cny
                if self.config.produce_chinese_publication
                else 0.0
            ),
            "latex_publication_zh": 0.0,
            "research_plan_publication": (
                self.config.research_plan_translation_cost_budget_cny
                if self.config.produce_research_plan_publication
                else 0.0
            ),
        }
        allocated = round(sum(allocations.values()), 4)
        return {
            "schema_version": "research_harness.preflight.v1",
            "global_cost_budget_cny": self.config.global_cost_budget_cny,
            "stage_hard_caps_cny": allocations,
            "observed_spend_cny": {
                "query_planner": observed_query_planner_spend,
            },
            "allocated_max_cny": allocated,
            "unallocated_reserve_cny": round(
                self.config.global_cost_budget_cny - allocated,
                4,
            ),
            "within_budget": allocated <= self.config.global_cost_budget_cny,
            "planning_estimate_cny": {
                "likely_low": round(
                    self.config.upstream_cost_cny + 8.0, 2
                ),
                "likely_high": round(
                    min(
                        self.config.global_cost_budget_cny,
                        self.config.upstream_cost_cny + 28.0,
                    ),
                    2,
                ),
                "hard_admission_ceiling": allocated,
            },
            "note": (
                "Caps are admission ceilings, not expected spend. Reused artifacts "
                "and deterministic tools consume no model budget."
            ),
        }

    def _remaining_global_budget(self) -> float:
        """Return the spendable balance of the single global budget pool."""

        return max(
            0.0,
            float(self.config.global_cost_budget_cny)
            - self._total_cost_cny(),
        )

    def _admission_budget(self, configured_budget: float) -> float:
        """Resolve a worker cap without bypassing the global cost ledger."""

        remaining = self._remaining_global_budget()
        if self.config.global_budget_only:
            return remaining
        return min(max(0.0, float(configured_budget)), remaining)

    def _remaining_after_future_reserve(self, *configured_budgets: float) -> float:
        """Keep legacy future-stage reservations out of global-only mode."""

        if self.config.global_budget_only:
            return self._remaining_global_budget()
        return max(
            0.0,
            self._remaining_global_budget()
            - sum(max(0.0, float(value)) for value in configured_budgets),
        )

    @staticmethod
    def _missing_planned_material_sections(
        blueprint: Dict[str, Any],
        material_bundles: Dict[str, Any],
    ) -> list[str]:
        planned_section_ids = {
            str(section.get("section_id") or "")
            for section in blueprint.get("sections", [])
            if isinstance(section, dict) and section.get("section_id")
        }
        return sorted(planned_section_ids - set(material_bundles))

    def _write_topic_gate(
        self,
        *,
        stage: str,
        alignment: Dict[str, Any],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Path:
        path = self.work_dir / f"TOPIC_GATE.{stage}.json"
        atomic_write_json(
            path,
            {
                "schema_version": "research_harness.topic_gate.v1",
                "stage": stage,
                **alignment,
                **(extra or {}),
            },
        )
        self.observability.emit(
            "topic_gate",
            stage=stage,
            status=alignment.get("status", "failed"),
            reason=alignment.get("reason", ""),
            core_hits=alignment.get("core_hits", []),
        )
        return path

    def _coverage_topic_alignment(
        self,
        *,
        material_bundles: Dict[str, Any],
        topic_identity: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Audit scientific-topic drift without penalising honest gaps.

        A review-wide topic fingerprint is deliberately broader than any one
        specialist chapter.  Requiring every chapter to repeat a fixed share
        of the article-wide anchors (for example ``metalens``, ``imaging`` and
        ``phase`` in a nanofabrication chapter) creates false drift failures.

        This gate therefore separates three questions:

        1. Does the section framing still name the review's scientific object?
        2. Do the *actual sources* share either multiple review-wide anchors,
           or one review-wide anchor plus a section-specific anchor?
        3. Is there any material to assess at all?  Empty ``needs more
           literature`` packages remain explicit gaps and are excluded from
           the drift denominator; they are not silently counted as passes.

        The aggregate guard remains fail-closed for evidence that has no
        review-wide scientific-object anchor, and the existing 80% threshold
        still rejects systematic cross-topic contamination.
        """

        def semantic_source_values(ledger: Mapping[str, Any]) -> list[str]:
            values: list[str] = []
            sources = ledger.get("sources") or []
            if not isinstance(sources, list):
                return values
            for raw_source in sources:
                if not isinstance(raw_source, Mapping):
                    continue
                # Use source-owned scientific text.  Do not let workflow keys,
                # generated adoption rationales, or section labels satisfy the
                # topic gate on behalf of an off-topic paper.
                for key in (
                    "title",
                    "abstract",
                    "abstract_or_snippet",
                    "tldr",
                    "snippet",
                    "venue",
                ):
                    value = raw_source.get(key)
                    if isinstance(value, str) and value.strip():
                        values.append(value.strip())
            return values

        core_anchors = {
            str(token).strip().casefold()
            for token in topic_identity.get("core_anchor_tokens", [])
            if str(token).strip()
        }
        section_results: Dict[str, Any] = {}
        combined_evidence: list[str] = []
        deferred_sections: list[str] = []
        assessable_sections: list[str] = []
        for section_id, bundle in material_bundles.items():
            package = (
                _read_json(bundle.material_package_path)
                if bundle.material_package_path
                and bundle.material_package_path.exists()
                else {}
            )
            ledger = (
                _read_json(bundle.source_ledger_path)
                if bundle.source_ledger_path
                and bundle.source_ledger_path.exists()
                else {}
            )
            source_rows = ledger.get("sources") or []
            source_count = len(source_rows) if isinstance(source_rows, list) else 0
            declared_source_count = max(
                int(package.get("total_sources") or 0),
                int(package.get("unique_sources") or 0),
                int(package.get("direct_sources") or 0),
            )

            # Section coverage already performs the source-aware topic
            # alignment against the actual adopted material. Reuse that
            # artifact when present; recomputing from a sparse ledger here can
            # turn a valid central-cache section into a false drift failure.
            section_alignment_path = (
                bundle.material_package_path.parent / "SECTION_TOPIC_ALIGNMENT.json"
                if bundle.material_package_path
                else None
            )
            section_alignment = (
                _read_json(section_alignment_path)
                if section_alignment_path and section_alignment_path.is_file()
                else {}
            )
            if str(section_alignment.get("status") or "") == "passed":
                assessable_sections.append(section_id)
                section_results[section_id] = {
                    **section_alignment,
                    "status": "passed",
                    "reason": "reused_section_coverage_topic_alignment",
                    "material_source_count": max(
                        source_count, declared_source_count
                    ),
                    "assessable": True,
                    "reused_authoritative_section_alignment": True,
                }
                combined_evidence.extend(
                    semantic_source_values(ledger)
                )
                continue
            if str(section_alignment.get("status") or "").startswith(
                "not_applicable"
            ) and source_count == 0 and declared_source_count == 0:
                deferred_sections.append(section_id)
                section_results[section_id] = {
                    **section_alignment,
                    "status": "not_assessed",
                    "reason": "no_material_to_assess_preserved_as_gap",
                    "material_source_count": 0,
                    "assessable": False,
                    "reused_authoritative_section_alignment": True,
                }
                continue

            framing_values = [
                str(package.get("section_title") or ""),
                str(package.get("chapter_argument") or ""),
            ]
            framing_text = " ".join(framing_values)
            framing_tokens = set(topic_tokens(framing_text))
            framing_core_hits = sorted(core_anchors & framing_tokens)

            if source_count == 0 and declared_source_count == 0:
                deferred_sections.append(section_id)
                section_results[section_id] = {
                    "schema_version": "research_harness.topic_alignment.v2",
                    "status": "not_assessed",
                    "reason": "no_material_to_assess_preserved_as_gap",
                    "topic_fingerprint": str(topic_identity.get("fingerprint") or ""),
                    "material_source_count": 0,
                    "framing_core_hits": framing_core_hits,
                    "assessable": False,
                }
                continue

            # A declared non-empty package without source-ledger evidence is a
            # malformed handoff, not an empty scientific gap.
            if source_count == 0:
                assessable_sections.append(section_id)
                section_results[section_id] = {
                    "schema_version": "research_harness.topic_alignment.v2",
                    "status": "failed",
                    "reason": "declared_material_missing_source_evidence",
                    "topic_fingerprint": str(topic_identity.get("fingerprint") or ""),
                    "material_source_count": 0,
                    "declared_source_count": declared_source_count,
                    "framing_core_hits": framing_core_hits,
                    "assessable": True,
                }
                continue

            assessable_sections.append(section_id)
            evidence_values = semantic_source_values(ledger)
            evidence_text = " ".join(evidence_values)
            evidence_tokens = set(topic_tokens(evidence_text))
            evidence_core_hits = sorted(core_anchors & evidence_tokens)
            section_specific_hits = sorted(
                (framing_tokens - core_anchors) & evidence_tokens
            )
            combined_evidence.extend(evidence_values)

            # Two global anchors are enough for a specialised source set.  A
            # source set with one global anchor must independently share a
            # section-specific scientific term, preventing the section frame
            # from laundering unrelated evidence.
            required_global_hits = 1 if len(core_anchors) <= 3 else 2
            framing_aligned = bool(framing_core_hits)
            evidence_aligned = (
                len(evidence_core_hits) >= required_global_hits
                or (
                    bool(evidence_core_hits)
                    and bool(section_specific_hits)
                )
            )
            aligned = framing_aligned and evidence_aligned
            section_results[section_id] = {
                "schema_version": "research_harness.topic_alignment.v2",
                "status": "passed" if aligned else "failed",
                "reason": (
                    "section_framing_and_evidence_preserve_scientific_object"
                    if aligned
                    else "section_evidence_drift_from_scientific_object"
                ),
                "topic_fingerprint": str(topic_identity.get("fingerprint") or ""),
                "material_source_count": source_count,
                "required_global_hits": required_global_hits,
                "framing_core_hits": framing_core_hits,
                "evidence_core_hits": evidence_core_hits,
                "section_specific_hits": section_specific_hits[:30],
                "assessable": True,
            }

        if combined_evidence:
            aggregate = assess_topic_alignment(
                combined_evidence,
                topic_identity,
                strict=True,
            )
        else:
            aggregate = {
                "schema_version": "research_harness.topic_alignment.v2",
                "status": "passed",
                "topic_fingerprint": str(topic_identity.get("fingerprint") or ""),
                "core_anchor_count": len(core_anchors),
                "required_core_hits": 0,
                "core_hits": [],
                "missing_core_anchors": sorted(core_anchors),
                "supporting_hits": [],
                "core_coverage": 0.0,
                "reason": "no_material_to_assess_preserved_as_gap",
            }
        failed_sections = sorted(
            section_id
            for section_id, result in section_results.items()
            if result.get("status") == "failed"
        )
        section_pass_rate = round(
            (len(assessable_sections) - len(failed_sections))
            / max(1, len(assessable_sections)),
            3,
        )
        aggregate_has_object_anchor = bool(aggregate.get("core_hits"))
        if assessable_sections and (
            not aggregate_has_object_anchor or section_pass_rate < 0.8
        ):
            aggregate["status"] = "failed"
            aggregate["reason"] = "section_materials_drift_from_scientific_object"
        elif assessable_sections:
            # Per-section two-factor checks are the primary guard.  The strict
            # aggregate score is diagnostic because specialised chapters need
            # not collectively repeat every broad review dimension.
            aggregate["status"] = "passed"
            aggregate["reason"] = "assessable_section_materials_preserve_topic"
        return {
            **aggregate,
            "section_results": section_results,
            "failed_section_ids": failed_sections,
            "deferred_section_ids": sorted(deferred_sections),
            "assessable_section_ids": sorted(assessable_sections),
            "assessable_section_count": len(assessable_sections),
            "section_pass_rate": section_pass_rate,
        }

    def run(self) -> ReviewHarnessResult:
        """Run the harness and always close an unexpected exception."""

        try:
            return self._run_impl()
        except Exception as exc:
            return self._recover_terminal(
                status="failed",
                completed_stage=self._current_stage("orchestrator"),
                final_review=None,
                visual_plan=None,
                research_plan=None,
                final_visual_package=None,
                error=exc,
            )

    def _run_impl(self) -> ReviewHarnessResult:
        # A preflight reservation is not spend.  Register upstream cost only
        # when the real harness run starts, and replace any stale reservation
        # left by an older preflight implementation.
        if self.config.upstream_cost_cny > 0:
            previous = dict(self.stage_costs.get("query_planner", {}))
            existing_model_call_count = previous.get("model_call_count")
            cost_cny = round(
                max(
                    self._stage_cost_cny(previous),
                    float(self.config.upstream_cost_cny),
                ),
                6,
            )
            previous.update(
                {
                    "cost_cny": cost_cny,
                    "estimated_cost_cny": cost_cny,
                    "input_tokens": max(
                        self._stage_token_count(
                            previous, "input_tokens"
                        ),
                        int(self.config.upstream_input_tokens),
                    ),
                    "output_tokens": max(
                        self._stage_token_count(
                            previous, "output_tokens"
                        ),
                        int(self.config.upstream_output_tokens),
                    ),
                    "model_call_count": max(
                        int(existing_model_call_count or 0)
                        if existing_model_call_count is not None
                        else 1,
                        0,
                    ),
                    "source": previous.get(
                        "source", "upstream_query_planner"
                    ),
                }
            )
            self.stage_costs["query_planner"] = previous
            self._save_cost()
        preflight = self.preflight()
        atomic_write_json(self.work_dir / "COST_PREFLIGHT.json", preflight)
        if not preflight["within_budget"]:
            return self._finish(
                "budget_rejected",
                "preflight",
                None,
                None,
            )
        if not self.config.query_plan_path.exists():
            return self._finish(
                "failed",
                "query_plan_missing",
                None,
                None,
            )
        if not self.config.base_kb_sqlite.exists():
            return self._finish(
                "failed",
                "base_kb_missing",
                None,
                None,
            )
        query_plan = _read_json(self.config.query_plan_path)
        topic_identity = build_topic_identity_contract(query_plan)
        if not topic_identity.get("valid"):
            self._write_topic_gate(
                stage="query_plan",
                alignment={
                    "status": "failed",
                    "reason": "query_plan_has_no_scientific_object_anchor",
                    "core_hits": [],
                },
            )
            return self._finish(
                "needs_query_plan_revision",
                "query_plan_topic_gate",
                None,
                None,
            )
        atomic_write_json(
            self.work_dir / "TOPIC_IDENTITY.json",
            topic_identity,
        )

        # Stage 0a: create the immutable run-local topic overlay before any
        # retrieval.  The normal CLI starts with a paper-free seed.  An
        # explicitly supplied database is filtered here and is never passed
        # directly to S2, coverage, Phase 3, or authoring.
        source_base_kb = Path(self.config.base_kb_sqlite)
        source_base_snapshot_path = (
            self.work_dir / "task_material" / "SOURCE_BASE_SNAPSHOT.json"
        )
        if source_base_snapshot_path.is_file():
            try:
                self.state["source_base_snapshot"] = json.loads(
                    source_base_snapshot_path.read_text(encoding="utf-8")
                )
                self._save_state()
            except (OSError, json.JSONDecodeError):
                pass
        self._set_stage(
            "topic_scoped_kb",
            "running",
            source_base_kb=str(source_base_kb),
            source_base_asset_role=self.config.base_kb_asset_role,
            retrieval_scope="topic_scoped_overlay",
        )
        try:
            scoped_result = self._prepare_topic_scoped_kb(source_base_kb)
        except Exception as exc:
            scoped_result = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "reused": False,
            }
        scoped_status = str(scoped_result.get("status") or "failed")
        self._record_stage(
            "topic_scoped_kb",
            scoped_status,
            0.0,
            0,
            0,
            {
                "runtime_kb_sqlite": str(
                    scoped_result.get("runtime_kb_sqlite") or ""
                ),
                "manifest_path": str(
                    scoped_result.get("manifest_path") or ""
                ),
                "retrieval_scope": "topic_scoped_overlay",
                "broad_base_kb_used_for_retrieval": False,
                "source_base_asset_role": self.config.base_kb_asset_role,
                "reused": bool(scoped_result.get("reused")),
                # Counts + short samples only: the full row/paper payloads are
                # 5.5 MB and belong in KB_MANIFEST.json and the SQLite overlay,
                # not in a state file rewritten on every stage transition.
                "selection": _compact_stage_detail(
                    scoped_result.get("selection") or {}
                ),
                "evidence": _compact_stage_detail(
                    scoped_result.get("evidence") or {}
                ),
                "error": str(scoped_result.get("error") or ""),
            },
        )
        if scoped_status == "failed":
            return self._finish(
                "failed",
                "topic_scoped_kb",
                None,
                None,
            )
        scoped_runtime_kb = Path(str(scoped_result["runtime_kb_sqlite"]))

        # Stage 0b: S2 may enrich the scoped overlay, but it must receive the
        # scoped path as its base.  Its partial/needs-more status is retained
        # in the stage ledger; only the canonical Phase-3 gate can authorize
        # later writing.
        self._set_stage(
            "s2_literature_intelligence",
            "running",
            source_base_kb=str(scoped_runtime_kb),
            retrieval_scope="topic_scoped_overlay",
        )
        try:
            s2_bootstrap = self._prepare_s2_kb(
                scoped_runtime_kb=scoped_runtime_kb,
            )
        except Exception as exc:
            s2_bootstrap = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        s2_status = str(s2_bootstrap.get("status") or "failed")
        runtime_kb = Path(
            str(s2_bootstrap.get("runtime_kb_sqlite") or "")
        )
        # Round-2 defect A: remember the S2 library while it is still the
        # truth.  The blueprint projection expansion below rebinds both
        # ``runtime_kb`` and ``config.base_kb_sqlite`` to a library without
        # a visual_chunks table; the visual editor must keep seeing this one.
        if runtime_kb.name:
            self._s2_visual_kb = runtime_kb
        self._record_stage(
            "s2_literature_intelligence",
            s2_status,
            float(s2_bootstrap.get("estimated_cost_cny") or 0.0),
            int(s2_bootstrap.get("input_tokens") or 0),
            int(s2_bootstrap.get("output_tokens") or 0),
            {
                "runtime_kb_sqlite": str(runtime_kb),
                "source_base_kb_sqlite": str(scoped_runtime_kb),
                "retrieval_scope": "topic_scoped_overlay",
                "broad_base_kb_used_for_retrieval": False,
                "accepted_s2_body_chunks": int(
                    s2_bootstrap.get("accepted_s2_body_chunks") or 0
                ),
                "graph_summary": s2_bootstrap.get("graph_summary") or {},
                "bootstrap_wall_time_seconds": float(
                    s2_bootstrap.get("wall_time_seconds") or 0.0
                ),
                "kb_manifest_path": str(
                    s2_bootstrap.get("kb_manifest_path") or ""
                ),
                "error": str(s2_bootstrap.get("error") or ""),
                "reused": bool(s2_bootstrap.get("reused")),
            },
        )
        if s2_status == "failed" or not runtime_kb.is_file():
            return self._finish(
                "failed",
                "s2_literature_intelligence",
                None,
                None,
            )
        self._sync_central_material_cache(
            source_stage="s2_literature_intelligence",
            kb_paths=[runtime_kb],
        )
        self.config.base_kb_sqlite = runtime_kb

        # Stage 1: one A-class lead designs the intellectual architecture.
        lead_dir = self.work_dir / "review_lead"
        blueprint_path = lead_dir / "REVIEW_BLUEPRINT.json"
        lead_was_reused = self._stage_completed(
            "review_lead",
            blueprint_path,
        )
        if not lead_was_reused:
            self._set_stage("review_lead", "running")
            lead_ctx = context_from_query_plan(
                self.config.query_plan_path,
                lead_dir,
                self.config.base_kb_sqlite,
                self.config.m1_library_path,
            )
            lead_result = run_review_lead(
                lead_ctx,
                model_tier=self.config.review_lead_model_tier,
                model_override=self.config.model_overrides.get("review_lead"),
                cost_budget_cny=self._admission_budget(
                    self.config.review_lead_budget_cny
                ),
            )
            self._record_worker_stage("review_lead", lead_result)
            if lead_result.status.value != "completed":
                return self._finish(
                    lead_result.status.value,
                    "review_lead",
                    None,
                    None,
                )
        else:
            self._set_stage("review_lead", "completed", reused=True)
        blueprint = _read_json(blueprint_path)
        if not blueprint:
            return self._finish("failed", "review_lead", None, None)
        blueprint_alignment = assess_blueprint_topic_alignment(
            blueprint,
            topic_identity,
        )
        # A cached blueprint may have passed an older, review-wide-only gate.
        # Invalidate it once and rebuild rather than either trusting stale work
        # or requiring an operator to delete runtime files by hand.
        if (
            lead_was_reused
            and blueprint_alignment["status"] != "passed"
        ):
            self._archive_invalid_stage(
                "review_lead",
                lead_dir,
                reason="cached_blueprint_failed_current_topic_gate",
                details=blueprint_alignment,
            )
            self._set_stage(
                "review_lead",
                "running",
                retry_reason=(
                    "cached_blueprint_failed_current_topic_gate"
                ),
            )
            lead_ctx = context_from_query_plan(
                self.config.query_plan_path,
                lead_dir,
                self.config.base_kb_sqlite,
                self.config.m1_library_path,
            )
            lead_result = run_review_lead(
                lead_ctx,
                model_tier=self.config.review_lead_model_tier,
                model_override=self.config.model_overrides.get(
                    "review_lead"
                ),
                cost_budget_cny=self._admission_budget(
                    self.config.review_lead_budget_cny
                ),
            )
            self._record_worker_stage("review_lead", lead_result)
            if lead_result.status.value != "completed":
                return self._finish(
                    lead_result.status.value,
                    "review_lead",
                    None,
                    None,
                )
            blueprint = _read_json(blueprint_path)
            blueprint_alignment = assess_blueprint_topic_alignment(
                blueprint,
                topic_identity,
            )
        self._write_topic_gate(
            stage="review_lead",
            alignment=blueprint_alignment,
        )
        if blueprint_alignment["status"] != "passed":
            return self._finish(
                "semantic_drift_blocked",
                "review_lead_topic_gate",
                None,
                None,
            )
        blueprint["topic_identity"] = topic_identity
        atomic_write_json(blueprint_path, blueprint)

        expanded_runtime_kb = self._expand_central_projection_for_blueprint(
            blueprint=blueprint,
        )
        if expanded_runtime_kb is not None:
            runtime_kb = expanded_runtime_kb
            self.config.base_kb_sqlite = runtime_kb

        # Stage 2: reuse local assets first, acquire legal OA evidence only for
        # consequential section-role gaps.  A same-run Phase-3 recovery may
        # rehydrate the already-paid Phase-2 snapshot instead of entering the
        # coverage worker again.
        coverage_root = self.work_dir / "section_coverage"
        self._set_stage("section_coverage", "running")
        coverage = self._rehydrate_phase3_recovery_coverage(
            blueprint=blueprint,
            topic_identity=topic_identity,
            scoped_runtime_kb=runtime_kb,
            source_base_kb=source_base_kb,
        )
        if coverage is None:
            coverage = SectionCoverageOrchestrator(
                self._build_coverage_config(
                    blueprint_path=blueprint_path,
                    base_kb_sqlite=runtime_kb,
                    source_base_kb=source_base_kb,
                    output_root=coverage_root,
                    stage_cost_budget_cny=self._admission_budget(
                        self.config.section_coverage_budget_cny
                    ),
                    cost_budget_per_section_cny=(
                        self._remaining_global_budget()
                        if self.config.global_budget_only
                        else None
                    ),
                ),
                run_dir=coverage_root,
            ).run()
        coverage_reused_for_phase3_recovery = bool(
            getattr(coverage, "reused_for_phase3_recovery", False)
        )
        if coverage_reused_for_phase3_recovery:
            recovery_telemetry = dict(
                getattr(coverage, "recovery_telemetry", {}) or {}
            )
            self.observability.emit(
                "reused_for_phase3_recovery",
                stage="section_coverage",
                **recovery_telemetry,
            )
        adaptive_coverage_outcomes = self._coverage_outcomes_by_section(coverage)
        self._record_stage(
            "section_coverage",
            coverage.status,
            coverage.total_cost_cny,
            coverage.total_input_tokens,
            coverage.total_output_tokens,
            {
                "work_dir": str(coverage.work_dir),
                "sections_completed": coverage.sections_completed,
                "sections_needing_more_literature": (
                    coverage.sections_needing_more_literature
                ),
                "adaptive_coverage_outcomes": adaptive_coverage_outcomes,
                "shared_article_evidence_portfolio": str(
                    coverage_root / "ARTICLE_EVIDENCE_PORTFOLIO.json"
                ),
                "reused_for_phase3_recovery": coverage_reused_for_phase3_recovery,
                "recovery_telemetry": dict(
                    getattr(coverage, "recovery_telemetry", {}) or {}
                ),
            },
        )
        self._sync_central_material_cache(
            source_stage="section_coverage",
            kb_paths=[coverage.work_dir / "supplemental_oa_kb.sqlite"],
        )
        if not coverage.material_bundles:
            # A missing package caused by a worker/runtime failure is not a
            # scientific evidence gap.  Keep these terminal states distinct so
            # operators and the resume controller know whether to fix the
            # workbench or retrieve more literature.
            terminal_status = (
                "needs_more_literature"
                if coverage.sections_needing_more_literature > 0
                and coverage.sections_failed == 0
                else "failed"
            )
            phase3_result = self._run_phase3_stage_and_record(
                blueprint=blueprint,
                topic_identity=topic_identity,
                coverage=coverage,
                scoped_runtime_kb=runtime_kb,
                source_base_kb=source_base_kb,
            )
            if (
                terminal_status == "failed"
                and phase3_result.get("status") == "needs_more_literature"
            ):
                terminal_status = "needs_more_literature"
            return self._finish(
                terminal_status,
                "phase3_argument_orchestration",
                None,
                None,
            )
        missing_bundle_ids = self._missing_planned_material_sections(
            blueprint,
            coverage.material_bundles,
        )
        if missing_bundle_ids:
            # Keep every planned section in the canonical R3 input even when a
            # worker did not produce a package.  Phase 3 records the missing
            # section as an explicit gap/merge task; returning here would make
            # a single weak optional section block the supported backbone.
            coverage_manifest = _read_json(
                coverage.work_dir / "SECTION_COVERAGE_RUN.json"
            )
            section_statuses = {
                str(record.get("section_id") or ""): str(
                    record.get("status") or ""
                )
                for record in coverage_manifest.get("sections", [])
                if isinstance(record, dict)
            }
            self.state.setdefault("stages", {}).setdefault(
                "section_coverage", {}
            )["missing_material_section_ids"] = missing_bundle_ids
            self.observability.emit(
                "sections_pending",
                stage="section_coverage",
                reason="deferred_to_canonical_r3_partial_handoff",
                section_ids=missing_bundle_ids,
                section_statuses=section_statuses,
            )
            self._save_state()

        # Keep needs-more bundles in the coverage view.  They carry the
        # section-scoped ledger and explicit adaptive outcome that R3 must
        # preserve; R4 decides section admission from canonical R3, not from a
        # destructive mutation of this dictionary.
        _cov_manifest = _read_json(coverage.work_dir / "SECTION_COVERAGE_RUN.json")
        _insufficient_ids = {
            str(rec.get("section_id") or "")
            for rec in _cov_manifest.get("sections", [])
            if isinstance(rec, dict)
            and str(rec.get("status") or "") == "needs_more_literature"
        }
        if _insufficient_ids:
            logger.info(
                "Deferring weak sections to canonical R3 admission",
                extra={
                    "run_id": self.run_id,
                    "stage": "section_coverage",
                    "section_count": len(_insufficient_ids),
                },
            )
            self.observability.emit(
                "sections_deferred",
                stage="section_coverage",
                reason="needs_more_literature",
                section_ids=sorted(_insufficient_ids),
            )

        coverage_topic_alignment = self._coverage_topic_alignment(
            material_bundles=coverage.material_bundles,
            topic_identity=topic_identity,
        )
        self._write_topic_gate(
            stage="section_coverage",
            alignment=coverage_topic_alignment,
        )
        if coverage_topic_alignment["status"] != "passed":
            return self._finish(
                "needs_more_literature",
                "section_coverage_topic_gate",
                None,
                None,
            )

        # Deterministic article-scale portfolio audit.  During a Phase-3
        # recovery, an existing report is reused (or recomputed locally) but
        # the portfolio coverage worker is never retried: its prior decision
        # is part of the paid snapshot and Phase 3 must be allowed to
        # re-evaluate it without acquiring duplicate literature.
        initial_portfolio_path = (
            self.work_dir / "LITERATURE_PORTFOLIO_REPORT.initial.json"
        )
        final_portfolio_path = self.work_dir / "LITERATURE_PORTFOLIO_REPORT.json"
        if coverage_reused_for_phase3_recovery:
            initial_portfolio = _read_json(initial_portfolio_path)
            reused_final_portfolio = bool(_read_json(final_portfolio_path))
            if not initial_portfolio:
                initial_portfolio = build_literature_portfolio_report(
                    blueprint=blueprint,
                    coverage_root=coverage.work_dir,
                    output_path=initial_portfolio_path,
                )
            if not reused_final_portfolio:
                build_literature_portfolio_report(
                    blueprint=blueprint,
                    coverage_root=coverage.work_dir,
                    output_path=final_portfolio_path,
                )
            portfolio_feedback = {}
            self._set_stage(
                "section_coverage_portfolio",
                "reused_for_phase3_recovery",
                reused=True,
                retry_suppressed=True,
                deterministic_recompute=not reused_final_portfolio,
            )
        else:
            initial_portfolio = build_literature_portfolio_report(
                blueprint=blueprint,
                coverage_root=coverage.work_dir,
                output_path=initial_portfolio_path,
            )
            portfolio_feedback = build_portfolio_feedback(initial_portfolio)
        prior_portfolio_stage = dict(
            self.state.get("stages", {}).get(
                "section_coverage_portfolio", {}
            )
        )
        bounded_portfolio_retry_already_completed = (
            prior_portfolio_stage.get("status") == "completed"
            and (
                self.work_dir / "LITERATURE_PORTFOLIO_REPORT.json"
            ).exists()
        )
        portfolio_budget_remaining = self._remaining_after_future_reserve(
            self.config.authoring_budget_cny,
            self.config.article_completion_budget_cny,
            self.config.visual_editor_budget_cny,
            (
                self.config.research_plan_budget_cny
                if self.config.produce_research_plan
                else 0.0
            ),
        )
        if coverage_reused_for_phase3_recovery:
            # The recovery branch above has already recorded the portfolio
            # reuse state.  In particular, do not enter the portfolio worker
            # even when the deterministic report contains open breadth gaps.
            pass
        elif (
            portfolio_feedback
            and not bounded_portfolio_retry_already_completed
            and portfolio_budget_remaining >= 0.35
        ):
            retry_budget = (
                self._admission_budget(
                    self.config.portfolio_coverage_budget_cny
                )
                if self.config.global_budget_only
                else min(
                    self.config.portfolio_coverage_budget_cny,
                    portfolio_budget_remaining,
                )
            )
            target_sections = sorted(portfolio_feedback)
            self._set_stage(
                "section_coverage_portfolio",
                "running",
                requested_sections=target_sections,
            )
            portfolio_retry = SectionCoverageOrchestrator(
                self._build_coverage_config(
                    blueprint_path=blueprint_path,
                    base_kb_sqlite=runtime_kb,
                    source_base_kb=source_base_kb,
                    output_root=coverage.work_dir,
                    stage_cost_budget_cny=retry_budget,
                    cost_budget_per_section_cny=(
                        self._remaining_global_budget()
                        if self.config.global_budget_only
                        else min(1.5, retry_budget)
                    ),
                    staging_kb_path=(
                        coverage.work_dir / "supplemental_oa_kb.sqlite"
                    ),
                    author_feedback_by_section=portfolio_feedback,
                    force_research_sections=target_sections,
                    retry_label="portfolio_breadth",
                    preserve_existing_manifest=True,
                    bounded_search=True,
                ),
                run_dir=coverage.work_dir,
            ).run(section_ids=target_sections)
            coverage.material_bundles.update(
                portfolio_retry.material_bundles
            )
            self._record_stage(
                "section_coverage_portfolio",
                portfolio_retry.status,
                portfolio_retry.total_cost_cny,
                portfolio_retry.total_input_tokens,
                portfolio_retry.total_output_tokens,
                {
                    "work_dir": str(portfolio_retry.work_dir),
                    "requested_sections": target_sections,
                    "sections_completed": (
                        portfolio_retry.sections_completed
                    ),
                },
            )
            self._sync_central_material_cache(
                source_stage="section_coverage_portfolio",
                kb_paths=[
                    coverage.work_dir / "supplemental_oa_kb.sqlite"
                ],
            )
        elif portfolio_feedback and bounded_portfolio_retry_already_completed:
            self._set_stage(
                "section_coverage_portfolio",
                "completed",
                requested_sections=sorted(portfolio_feedback),
                reused=True,
                stop_reason=(
                    "bounded portfolio retry already completed; unresolved "
                    "breadth limits remain documented for authoring"
                ),
            )
        elif portfolio_feedback:
            self._set_stage(
                "section_coverage_portfolio",
                "skipped_cost_budget",
                requested_sections=sorted(portfolio_feedback),
            )
        else:
            self._set_stage(
                "section_coverage_portfolio",
                "not_needed",
            )
        if not coverage_reused_for_phase3_recovery:
            build_literature_portfolio_report(
                blueprint=blueprint,
                coverage_root=coverage.work_dir,
                output_path=final_portfolio_path,
            )

        # Stage 3: convert the final coverage view into the canonical R3
        # production handoff.  This is deterministic and offline-safe; no
        # authoring path is opened until both the Phase-3 result and the typed
        # R3 handoff explicitly allow it.
        phase3_result = self._run_phase3_stage_and_record(
            blueprint=blueprint,
            topic_identity=topic_identity,
            coverage=coverage,
            scoped_runtime_kb=runtime_kb,
            source_base_kb=source_base_kb,
        )
        phase3_status = str(phase3_result.get("status") or "failed")
        if phase3_status not in {"completed", "completed_with_limits"}:
            return self._finish(
                phase3_status
                if phase3_status in {
                    "needs_more_literature",
                    "merge_required",
                }
                else "failed",
                "phase3_argument_orchestration",
                None,
                None,
            )
        phase3_root = Path(
            str(
                phase3_result.get("work_dir")
                or self.config.phase3_artifacts_root
                or self.work_dir / "phase3_argument_orchestration"
            )
        )
        r3_gate = self._require_r3_authoring_gate(
            phase3_root=phase3_root,
            blueprint=blueprint,
        )
        if r3_gate.get("status") == "passed":
            self.state.setdefault("stages", {}).setdefault(
                "phase3_argument_orchestration", {}
            ).update(
                {
                    "r4_gate": r3_gate,
                    "r3_handoff_path": str(
                        phase3_root / "R3_PRODUCTION_HANDOFF.json"
                    ),
                }
            )
            self._save_state()
        else:
            self._record_stage(
                "phase3_argument_orchestration",
                str(r3_gate.get("status") or "failed"),
                0.0,
                0,
                0,
                {
                    "r4_gate": r3_gate,
                    "r3_handoff_path": str(
                        phase3_root / "R3_PRODUCTION_HANDOFF.json"
                    ),
                },
            )
        if r3_gate.get("status") != "passed":
            return self._finish(
                str(r3_gate.get("status") or "failed")
                if r3_gate.get("status")
                in {"needs_more_literature", "merge_required"}
                else "failed",
                "phase3_argument_orchestration",
                None,
                None,
            )

        authoring_section_ids = [
            str(section_id)
            for section_id in r3_gate.get("ready_section_ids", [])
            if str(section_id)
        ]
        if not authoring_section_ids:
            return self._finish(
                "needs_more_literature",
                "phase3_argument_orchestration",
                None,
                None,
            )

        # Stage 3/4/5: authors write from section packages; deterministic and
        # A-level editorial layers revise the complete article.
        if self.config.global_budget_only:
            remaining_for_authoring = self._remaining_global_budget()
        else:
            remaining_for_authoring = min(
                self.config.authoring_budget_cny,
                self._remaining_after_future_reserve(
                    self.config.article_completion_budget_cny,
                    self.config.visual_editor_budget_cny,
                    (
                        self.config.research_plan_budget_cny
                        if self.config.produce_research_plan
                        else 0.0
                    ),
                ),
            )
        if remaining_for_authoring < 0.5:
            return self._finish(
                "budget_exhausted",
                "authoring_revision",
                None,
                None,
            )
        deterministic_authoring_dir = (
            self.work_dir / "authoring" / "full_review"
        )
        prior_authoring_cost_cny = float(
            _read_json(deterministic_authoring_dir / "COST.json").get(
                "total_cost_cny", 0.0
            )
            or 0.0
        )
        # A prior partial attempt may have been created under a wider admission
        # list.  Reusing it would retry blocked chapters and contaminate both
        # cost accounting and the manuscript.  Archive it whenever the durable
        # orchestration context does not match the current canonical R3 gate.
        prior_authoring_context = _read_json(
            deterministic_authoring_dir / "REVIEW_ORCHESTRATION_CONTEXT.json"
        )
        prior_admitted_ids = [
            str(item)
            for item in prior_authoring_context.get("section_ids", [])
            if str(item)
        ]
        admission_changed = bool(prior_admitted_ids) and (
            prior_admitted_ids != authoring_section_ids
        )
        if admission_changed:
            self._archive_invalid_stage(
                "authoring_revision",
                deterministic_authoring_dir,
                reason="canonical_r3_authoring_admission_changed",
                details={
                    "prior_section_ids": prior_admitted_ids,
                    "current_section_ids": authoring_section_ids,
                    "r4_admission_path": str(
                        phase3_root / "R4_AUTHORING_ADMISSION.json"
                    ),
                },
        )
        self._set_stage(
            "authoring_revision",
            "running",
            admitted_section_ids=authoring_section_ids,
            blocked_section_ids=list(r3_gate.get("blocked_section_ids", [])),
        )
        section_count = max(1, len(authoring_section_ids))
        if self.config.global_budget_only:
            # A global-only run must not subdivide the shared balance into
            # section or audit allowances.  The nested authoring runner still
            # receives the global remaining ceiling as a safety boundary, while
            # the outer ledger remains the sole budget allocator.
            audit_reserve = remaining_for_authoring
            section_budget = max(0.5, remaining_for_authoring)
        else:
            audit_reserve = min(4.0, remaining_for_authoring * 0.2)
            section_budget = max(
                0.5,
                (remaining_for_authoring - audit_reserve) / section_count,
            )
        authoring_config = OrchestratorConfig(
            blueprint_path=blueprint_path,
            output_root=self.work_dir / "authoring",
            # Do not hand coverage bundles directly to R4.  The canonical R3
            # handoff is the only production material source after the gate.
            material_bundles=None,
            phase3_artifacts_root=phase3_root,
            section_model_tier=self.config.author_model_tier,
            compact_authoring_mode=True,
            authoring_core_chunk_limit=int(
                self.config.phase3_authoring_core_chunk_limit or 12
            ),
            section_cost_budget_cny=section_budget,
            model_override=self.config.model_overrides.get("author"),
            use_llm_audit=self.config.use_llm_global_audit,
            audit_model_tier=self.config.managing_editor_model_tier,
            audit_cost_budget_cny=audit_reserve,
            audit_model_override=self.config.model_overrides.get(
                "managing_editor"
            ),
            m1_library_path=self.config.m1_library_path,
            # FullReviewOrchestrator compares this ceiling with its cumulative
            # historical section cost.  On resume, add the newly admitted
            # allowance instead of comparing old spend with a fresh-only cap.
            run_cost_budget_cny=(
                self._cumulative_stage_ceiling(
                    prior_authoring_cost_cny,
                    remaining_for_authoring,
                )
            ),
        )
        authoring_runner = FullReviewOrchestrator(
            authoring_config,
            run_dir=deterministic_authoring_dir,
        )
        prior_authoring_dir = (
            self._prior_stage_work_dir("authoring_revision")
            or (
                deterministic_authoring_dir
                if deterministic_authoring_dir.exists()
                else None
            )
        )
        if (
            not admission_changed
            and prior_authoring_dir
            and (prior_authoring_dir / "REVIEW_STATE.json").exists()
            and (prior_authoring_dir / "SECTION_REGISTRY.json").exists()
            and _read_json(prior_authoring_dir / "REVIEW_STATE.json").get(
                "state"
            )
            != "failed"
        ):
            authoring = authoring_runner.resume(prior_authoring_dir)
        else:
            authoring = authoring_runner.run(
                section_ids=authoring_section_ids,
            )
        recovery_passes: list[Dict[str, Any]] = []
        for recovery_index in range(
            max(0, int(self.config.max_authoring_recovery_passes))
        ):
            if authoring.status != "partial":
                break
            registry = _read_json(
                authoring.work_dir / "SECTION_REGISTRY.json"
            )
            retryable: list[str] = []
            for section_row in registry.get("sections", []):
                if not isinstance(section_row, dict):
                    continue
                section_status = str(section_row.get("status") or "")
                result_status = str(
                    _read_json(
                        Path(str(section_row.get("work_dir") or ""))
                        / "RESULT.json"
                    ).get("status")
                    or ""
                )
                if section_status in {
                    "budget_exhausted",
                    "validation_failed",
                } or (
                    section_status == "failed"
                    and result_status
                    in {"budget_exhausted", "validation_failed"}
                ):
                    retryable.append(
                        str(section_row.get("section_id") or "")
                    )
            if not retryable:
                break
            projected_remaining = self._remaining_after_future_reserve(
                float(authoring.total_cost_cny or 0.0),
                self.config.article_completion_budget_cny,
                self.config.visual_editor_budget_cny,
                (
                    self.config.research_plan_budget_cny
                    if self.config.produce_research_plan
                    else 0.0
                ),
            )
            if projected_remaining < 0.5:
                break
            recovery_passes.append(
                {
                    "pass": recovery_index + 1,
                    "retryable_section_ids": retryable,
                }
            )
            authoring = authoring_runner.resume(authoring.work_dir)
        author_input = int(
            getattr(authoring, "total_input_tokens", 0) or 0
        )
        author_output = int(
            getattr(authoring, "total_output_tokens", 0) or 0
        )
        if not author_input and not author_output:
            author_input, author_output = self._token_totals_from_task_costs(
                authoring.work_dir
            )
        self._record_stage(
            "authoring_revision",
            authoring.status,
            authoring.total_cost_cny,
            author_input,
            author_output,
            {
                "work_dir": str(authoring.work_dir),
                "runtime_recovery_passes": recovery_passes,
            },
        )

        # Fail-open authoring applies to the first authoring pass as well as
        # the later literature-feedback retry.  A bounded author can stop at
        # max_iters after persisting a durable, validated candidate; leaving
        # this initial ``awaiting_human_review`` status untouched prevents the
        # downstream staged completion gate from being reached at all.  Keep
        # the decision record for auditability, but let the configured gate
        # settle the usable candidate before continuing.
        if str(authoring.status) == "awaiting_human_review":
            if self._resolve_human_gate(
                stage="authoring_revision",
                kind="authoring_revision",
                subject_id=f"{self.run_id}:authoring_revision",
                context={
                    "work_dir": str(authoring.work_dir),
                },
                original_status="awaiting_human_review",
            ):
                authoring.status = "completed"

        # One bounded author-to-researcher-to-author feedback cycle. It runs only
        # when the author explicitly records a pivotal evidence gap, reuses the
        # original supplemental KB, and reopens only affected sections.
        feedback_by_section = self._coverage_feedback_requests(
            authoring.work_dir
        )
        feedback_sections = sorted(feedback_by_section)
        feedback_remaining = self._remaining_after_future_reserve(
            self.config.article_completion_budget_cny,
            self.config.visual_editor_budget_cny,
            (
                self.config.research_plan_budget_cny
                if self.config.produce_research_plan
                else 0.0
            ),
        )
        if feedback_sections and feedback_remaining >= 0.35:
            feedback_budget = (
                self._admission_budget(
                    self.config.feedback_coverage_budget_cny
                )
                if self.config.global_budget_only
                else min(
                    self.config.feedback_coverage_budget_cny,
                    feedback_remaining,
                )
            )
            # Open the timer before the runner starts: this stage used to reach
            # _record_stage with no matching start_stage, so it emitted no
            # stage_started event and recorded wall_time 0.0 for a real
            # multi-section literature search.  The UI stage track also needs
            # stage_started to ever show this stage as in-flight.
            self.observability.start_stage(
                "section_coverage_feedback",
                requested_sections=feedback_sections,
            )
            feedback_blueprint = self._write_feedback_blueprint(
                blueprint,
                authoring.work_dir,
                feedback_sections,
                feedback_by_section=feedback_by_section,
            )
            feedback_result = SectionCoverageOrchestrator(
                self._build_coverage_config(
                    blueprint_path=feedback_blueprint,
                    base_kb_sqlite=runtime_kb,
                    source_base_kb=source_base_kb,
                    output_root=coverage.work_dir,
                    stage_cost_budget_cny=feedback_budget,
                    cost_budget_per_section_cny=(
                        self._remaining_global_budget()
                        if self.config.global_budget_only
                        else min(2.0, feedback_budget)
                    ),
                    staging_kb_path=(
                        coverage.work_dir / "supplemental_oa_kb.sqlite"
                    ),
                    author_feedback_by_section={
                        section_id: feedback_by_section[section_id]
                        for section_id in feedback_sections
                    },
                    force_research_sections=feedback_sections,
                    retry_label="author_editor_feedback",
                    preserve_existing_manifest=True,
                    bounded_search=True,
                ),
                run_dir=coverage.work_dir,
            ).run(section_ids=feedback_sections)
            self._record_stage(
                "section_coverage_feedback",
                feedback_result.status,
                feedback_result.total_cost_cny,
                feedback_result.total_input_tokens,
                feedback_result.total_output_tokens,
                {
                    "work_dir": str(feedback_result.work_dir),
                    "requested_sections": feedback_sections,
                    "sections_completed": feedback_result.sections_completed,
                },
            )
            self._sync_central_material_cache(
                source_stage="section_coverage_feedback",
                kb_paths=[
                    coverage.work_dir / "supplemental_oa_kb.sqlite"
                ],
            )
            coverage.material_bundles.update(
                feedback_result.material_bundles
            )
            reopened = authoring_runner.prepare_literature_feedback_retry(
                [
                    section_id
                    for section_id in feedback_sections
                    if section_id in feedback_result.material_bundles
                ]
            )
            if reopened:
                authoring = authoring_runner.resume(authoring.work_dir)
                author_input = int(
                    getattr(authoring, "total_input_tokens", 0) or 0
                )
                author_output = int(
                    getattr(authoring, "total_output_tokens", 0) or 0
                )
                self._record_stage(
                    "authoring_revision",
                    authoring.status,
                    authoring.total_cost_cny,
                    author_input,
                    author_output,
                    {
                        "work_dir": str(authoring.work_dir),
                        "literature_feedback_retry_sections": reopened,
                    },
                )
                if str(authoring.status) == "awaiting_human_review":
                    # P0-1: authoring used to park here with no decision
                    # object at all; register one and rewrite on timeout so
                    # the delivery gate no longer sees a permanent awaiting.
                    if self._resolve_human_gate(
                        stage="authoring_revision",
                        kind="authoring_revision",
                        subject_id=f"{self.run_id}:authoring_revision",
                        context={
                            "work_dir": str(authoring.work_dir),
                        },
                        original_status="awaiting_human_review",
                    ):
                        authoring.status = "completed"
        elif feedback_sections:
            self._set_stage(
                "section_coverage_feedback",
                "skipped_cost_budget",
                requested_sections=feedback_sections,
            )

        self._record_supplementary_closure_gap(feedback_sections)

        final_review = authoring.work_dir / "FINAL_REVIEW_EN.md"
        if not final_review.exists() or not final_review.stat().st_size:
            fallback = authoring.work_dir / "FULL_REVIEW_DRAFT_EN.md"
            final_review = (
                fallback if (fallback.exists() and fallback.stat().st_size) else None
            )
        if final_review is None:
            return self._finish(
                authoring.status,
                "authoring_revision",
                None,
                None,
            )
        body_review = final_review
        visual_review_work_dir = authoring.work_dir
        publication_mainline_result = None
        global_figure_plan_path: Optional[Path] = None

        if self.config.publication_mainline_enabled:
            from .publication_mainline_adapter import run_publication_mainline

            self._set_stage(
                "publication_mainline_enhancement",
                "running",
                admitted_section_ids=authoring_section_ids,
            )
            try:
                publication_mainline_result = run_publication_mainline(
                    project_root=self.work_dir,
                    authoring_work_dir=authoring.work_dir,
                    output_root=self.work_dir / "publication_mainline",
                    admitted_section_ids=authoring_section_ids,
                    blueprint=blueprint,
                    run_id=self.run_id,
                    enhancement_live=True,
                    enhancement_runner=(
                        self.config.publication_mainline_enhancement_runner
                    ),
                    enhancement_qwen_caller=(
                        self.config.publication_mainline_enhancement_qwen_caller
                    ),
                    local_metadata_db_path=(
                        self.config.publication_mainline_local_metadata_db_path
                        or self.config.base_kb_sqlite
                    ),
                    local_search_callback=(
                        self.config.publication_mainline_local_search_callback
                    ),
                    s2_search_callback=(
                        self.config.publication_mainline_s2_search_callback
                    ),
                    representative_applications_enabled=(
                        self.config
                        .publication_mainline_representative_applications_enabled
                    ),
                    application_max_targets=(
                        self.config.publication_mainline_application_max_targets
                    ),
                    application_soft_min_targets=(
                        self.config
                        .publication_mainline_application_soft_min_targets
                    ),
                    application_per_target_cap=(
                        self.config
                        .publication_mainline_application_per_target_cap
                    ),
                    application_local_max_results=(
                        self.config
                        .publication_mainline_application_local_max_results
                    ),
                    application_writer_tier=(
                        self.config.publication_mainline_application_writer_tier
                    ),
                    s2_metadata_fallback_enabled=(
                        self.config.publication_mainline_s2_metadata_fallback
                    ),
                    enhancement_workers=(
                        self.config.publication_mainline_enhancement_workers
                    ),
                    staged_editorial_workers=(
                        self.config
                        .publication_mainline_staged_editorial_workers
                    ),
                    commander_live=True,
                    commander_model_tier=(
                        self.config.publication_mainline_commander_model_tier
                    ),
                    commander_role_provider=(
                        self.config.publication_mainline_commander_role_provider
                    ),
                    staged_live=True,
                    staged_model_tier=(
                        self.config.publication_mainline_staged_model_tier
                    ),
                    staged_reviewer_tier=(
                        self.config.publication_mainline_staged_reviewer_tier
                    ),
                    staged_editorial_verifier_tier=(
                        self.config.publication_mainline_staged_editorial_verifier_tier
                    ),
                    staged_providers=(
                        self.config.publication_mainline_staged_providers
                    ),
                    commander_resume=(
                        self._resumed_existing_run
                        and (
                            self.work_dir
                            / "publication_mainline"
                            / "commander"
                            / "run_state.json"
                        ).exists()
                    ),
                    staged_resume=(
                        self._resumed_existing_run
                        and (
                            self.work_dir
                            / "publication_mainline"
                            / "staged_completion"
                            / "staged_article_completion_state.json"
                        ).exists()
                    ),
                )
            except Exception as exc:
                self._record_stage(
                    "publication_mainline_enhancement",
                    "failed",
                    0.0,
                    0,
                    0,
                    {
                        "error": f"{type(exc).__name__}:{exc}",
                    },
                )
                return self._finish(
                    "failed",
                    "publication_mainline_enhancement",
                    body_review,
                    None,
                )

            for stage_name, metric in (
                publication_mainline_result.stage_metrics or {}
            ).items():
                if stage_name not in self.STAGES:
                    # P3-3: an unknown adapter stage name must not be
                    # dropped silently -- it would hide accounting drift
                    # between the adapter and the orchestrator's STAGES.
                    logger.warning(
                        "publication_mainline reported unknown stage %r; "
                        "recorded under publication_mainline.ignored_stages",
                        stage_name,
                    )
                    ignored = self.state.setdefault(
                        "publication_mainline", {}
                    ).setdefault("ignored_stages", [])
                    if stage_name not in ignored:
                        ignored.append(stage_name)
                    continue
                details = dict(metric)
                status = str(details.pop("status") or "failed")
                cost = float(details.pop("cost_cny", 0.0) or 0.0)
                input_tokens = int(details.pop("input_tokens", 0) or 0)
                output_tokens = int(details.pop("output_tokens", 0) or 0)
                # These stages ran inside the adapter, so this orchestrator
                # never opened a timer for them; the adapter measured each one
                # and reports it here.  Absent for stages the adapter returned
                # early on (still ``pending``), which finish_stage then marks
                # ``wall_time_measured: false`` rather than claiming 0 seconds.
                measured_wall = details.pop("wall_time_seconds", None)
                self._record_stage(
                    stage_name,
                    status,
                    cost,
                    input_tokens,
                    output_tokens,
                    details,
                    wall_time_seconds=(
                        None if measured_wall is None else float(measured_wall)
                    ),
                )

            self.state["publication_mainline"] = (
                publication_mainline_result.summary
            )
            self.state.setdefault("stages", {})[
                "publication_mainline"
            ] = publication_mainline_result.summary
            self._save_state()

            if publication_mainline_result.final_review_path is None:
                # A failed publication mainline must never silently publish
                # the pre-enhancement authoring aggregate. The adapter owns
                # fail-open preservation; if it has no final artifact, close
                # delivery and retain only the durable diagnostics.
                return self._finish(
                    publication_mainline_result.status,
                    publication_mainline_result.completed_stage,
                    None,
                    None,
                )
            final_review = publication_mainline_result.final_review_path
            visual_review_work_dir = (
                publication_mainline_result.downstream_review_work_dir
                or authoring.work_dir
            )
            self._set_stage(
                "article_completion",
                "disabled_by_publication_mainline",
                publication_mainline_status=(
                    publication_mainline_result.status
                ),
            )
            # P1-1 (round 3): style governance between staged completion and
            # LaTeX.  Distribution-driven candidates, one budget envelope,
            # rewritten text becomes the LaTeX input as a side-by-side file so
            # the adapter artifact stays untouched for diffing.
            if (
                self.config.llm_style_pipeline_enabled
                and not self.config.visual_test_mode
            ):
                self._set_stage("llm_style_pipeline", "running")
                try:
                    _style_source = final_review.read_text(encoding="utf-8")
                    # Do not silently rewrite a completed manuscript without
                    # the mainline terminology ledger.  The profile can enable
                    # style governance, but protection remains a hard precondition.
                    protected_terms = load_protected_terms(
                        ledger_path=(
                            authoring.work_dir / "TERMINOLOGY_LEDGER.json"
                        )
                    )
                    if not protected_terms:
                        raise RuntimeError("mainline_protected_terms_unavailable")
                    style_report = run_style_convergence(
                        _style_source,
                        enabled=True,
                        cost_budget_cny=self._admission_budget(
                            self.config.llm_style_pipeline_budget_cny
                        ),
                        max_rewrites=(
                            self.config.llm_style_pipeline_max_rewrites
                        ),
                        protected_terms=protected_terms,
                    )
                    # Model convergence is budgeted and may honestly stop at
                    # a fixed point.  The publication boundary still runs a
                    # zero-cost deterministic guard for safe mechanical
                    # defects (repeated While openers, not-but constructions,
                    # and the locked abstract-subject phrases).  It uses the
                    # same hard fact verifier, so this cannot bypass citation,
                    # number, unit, formula, hedge, or terminology checks.
                    deterministic_style_report = (
                        apply_deterministic_style_governance(
                            str(style_report.get("review_text") or _style_source),
                            enabled=True,
                            protected_terms=protected_terms,
                        )
                    )
                    style_report["deterministic_governance"] = {
                        key: value
                        for key, value in deterministic_style_report.items()
                        if key != "review_text"
                    }
                    style_report["review_text"] = str(
                        deterministic_style_report.get("review_text")
                        or style_report.get("review_text")
                        or _style_source
                    )
                    style_report["metrics_after"] = (
                        deterministic_style_report.get("metrics_after")
                        or style_report.get("metrics_after")
                    )
                    style_report["converged"] = bool(
                        style_report.get("converged")
                        or (
                            style_report.get("metrics_after", {}).get(
                                "paragraph_opener_max_share", 1.0
                            )
                            <= 0.12
                        )
                    )
                    atomic_write_json(
                        self.work_dir
                        / "publication_mainline"
                        / "STYLE_CONVERGENCE_REPORT.json",
                        {
                            key: value
                            for key, value in style_report.items()
                            if key != "review_text"
                        },
                    )
                    styled_text = str(style_report.get("review_text") or "")
                    if (
                        styled_text
                        and styled_text != _style_source
                        and (
                            int(style_report.get("rewrites_accepted", 0) or 0)
                            > 0
                            or int(
                                deterministic_style_report.get(
                                    "rewrites_accepted", 0
                                )
                                or 0
                            )
                            > 0
                        )
                    ):
                        styled_path = (
                            final_review.parent
                            / (final_review.stem + ".style.md")
                        )
                        styled_path.write_text(styled_text, encoding="utf-8")
                        final_review = styled_path
                    self._set_stage(
                        "llm_style_pipeline",
                        "completed" if not style_report.get("budget_exhausted") else "partial",
                        rewrites_accepted=int(
                            style_report.get("rewrites_accepted", 0) or 0
                        ),
                        deterministic_rewrites_accepted=int(
                            deterministic_style_report.get(
                                "rewrites_accepted", 0
                            )
                            or 0
                        ),
                        deterministic_rewrite_kind_counts=(
                            deterministic_style_report.get(
                                "rewrite_kind_counts", {}
                            )
                        ),
                        estimated_cost_cny=round(
                            float(style_report.get("estimated_cost_cny", 0.0) or 0.0),
                            4,
                        ),
                        template_opener_share_before=(
                            style_report.get("metrics_before", {}).get(
                                "template_opener_share"
                            )
                        ),
                        template_opener_share_after=(
                            style_report.get("metrics_after", {}).get(
                                "template_opener_share"
                            )
                        ),
                        paragraph_opener_max_share_before=(
                            style_report.get("metrics_before", {}).get(
                                "paragraph_opener_max_share"
                            )
                        ),
                        paragraph_opener_max_share_after=(
                            style_report.get("metrics_after", {}).get(
                                "paragraph_opener_max_share"
                            )
                        ),
                        converged=bool(style_report.get("converged")),
                    )
                except Exception as exc:
                    # Fail-open: a style failure must never kill a finished
                    # manuscript -- record and continue with the original text.
                    self._set_stage(
                        "llm_style_pipeline",
                        "failed_fail_open",
                        error=f"{type(exc).__name__}:{exc}",
                    )
            else:
                self._set_stage(
                    "llm_style_pipeline",
                    (
                        "skipped_offline_test_mode"
                        if self.config.visual_test_mode
                        else "disabled"
                    ),
                )

            # P1-2: chapter-scoped human-style review after the existing
            # distribution pass and before any structure audit or renderer.
            # Review and revision are separate Qwen roles; selected chapters
            # run in parallel, while each chapter remains reviewer -> author
            # serial.  The governance module is fail-open and promotes only
            # its locally reconstructed, hard-verified candidate.
            if (
                self.config.chapter_style_governance_enabled
                and not self.config.visual_test_mode
            ):
                self._set_stage("chapter_style_governance", "running")
                try:
                    _chapter_style_source = final_review.read_text(
                        encoding="utf-8"
                    )
                    protected_terms = load_protected_terms(
                        ledger_path=(
                            authoring.work_dir / "TERMINOLOGY_LEDGER.json"
                        )
                    )
                    if not protected_terms:
                        raise RuntimeError(
                            "mainline_protected_terms_unavailable"
                        )
                    _chapter_manifest_path = (
                        self.work_dir
                        / "publication_mainline"
                        / "full_manuscript_manifest.json"
                    )
                    _chapter_manifest = _read_json(_chapter_manifest_path)
                    _chapter_sections = [
                        row
                        for row in (_chapter_manifest.get("sections", []) or [])
                        if isinstance(row, Mapping)
                        and str(row.get("title") or "").strip()
                    ]
                    _chapter_titles = [
                        str(row.get("title") or "").strip()
                        for row in _chapter_sections
                    ]
                    _chapter_ids = {
                        str(row.get("title") or "").strip(): str(
                            row.get("section_id") or ""
                        ).strip()
                        for row in _chapter_sections
                        if str(row.get("title") or "").strip()
                    }
                    if not _chapter_titles:
                        raise RuntimeError(
                            "mainline_chapter_manifest_unavailable"
                        )
                    chapter_style_report = run_chapter_style_governance(
                        _chapter_style_source,
                        enabled=True,
                        chapter_titles=_chapter_titles,
                        chapter_ids=_chapter_ids,
                        reviewer_model_tier=(
                            self.config.chapter_style_reviewer_model_tier
                        ),
                        reviser_model_tier=(
                            self.config.chapter_style_reviser_model_tier
                        ),
                        workers=max(
                            1, self.config.chapter_style_governance_workers
                        ),
                        cost_budget_cny=self._admission_budget(
                            self.config.chapter_style_governance_budget_cny
                        ),
                        protected_terms=protected_terms,
                    )
                    _chapter_style_report_path = (
                        self.work_dir
                        / "publication_mainline"
                        / "CHAPTER_STYLE_GOVERNANCE_REPORT.json"
                    )
                    atomic_write_json(
                        _chapter_style_report_path,
                        {
                            key: value
                            for key, value in chapter_style_report.items()
                            if key != "review_text"
                        },
                    )
                    _chapter_style_text = str(
                        chapter_style_report.get("review_text") or ""
                    )
                    _promoted = bool(
                        chapter_style_report.get("promotion_eligible")
                        and chapter_style_report.get("changed")
                        and chapter_style_report.get("global_guard", {}).get(
                            "ok", False
                        )
                        and _chapter_style_text
                        and _chapter_style_text != _chapter_style_source
                    )
                    if _promoted:
                        _chapter_style_path = (
                            final_review.parent
                            / (final_review.stem + ".chapter_style.md")
                        )
                        _chapter_style_path.write_text(
                            _chapter_style_text,
                            encoding="utf-8",
                        )
                        final_review = _chapter_style_path
                    _chapter_style_status = (
                        "partial"
                        if chapter_style_report.get("budget_exhausted")
                        else "completed"
                    )
                    self._record_stage(
                        "chapter_style_governance",
                        _chapter_style_status,
                        float(
                            chapter_style_report.get(
                                "estimated_cost_cny", 0.0
                            )
                            or 0.0
                        ),
                        int(
                            chapter_style_report.get("input_tokens", 0) or 0
                        ),
                        int(
                            chapter_style_report.get("output_tokens", 0) or 0
                        ),
                        {
                            "model_call_count": int(
                                chapter_style_report.get("reviewer_calls", 0)
                                or 0
                            )
                            + int(
                                chapter_style_report.get("reviser_calls", 0)
                                or 0
                            ),
                            "reviewer_calls": int(
                                chapter_style_report.get("reviewer_calls", 0)
                                or 0
                            ),
                            "reviser_calls": int(
                                chapter_style_report.get("reviser_calls", 0)
                                or 0
                            ),
                            "chapters_attempted": int(
                                chapter_style_report.get(
                                    "chapters_attempted", 0
                                )
                                or 0
                            ),
                            "chapters_accepted": int(
                                chapter_style_report.get(
                                    "chapters_accepted", 0
                                )
                                or 0
                            ),
                            "chapters_changed": int(
                                chapter_style_report.get(
                                    "chapters_changed", 0
                                )
                                or 0
                            ),
                            "promotion_eligible": bool(
                                chapter_style_report.get(
                                    "promotion_eligible", False
                                )
                            ),
                            "promoted": _promoted,
                            "promotion_reason": str(
                                chapter_style_report.get(
                                    "promotion_reason", ""
                                )
                                or ""
                            ),
                            "style_score_before": chapter_style_report.get(
                                "style_score_before"
                            ),
                            "style_score_after": chapter_style_report.get(
                                "style_score_after"
                            ),
                            "global_guard_ok": bool(
                                chapter_style_report.get(
                                    "global_guard", {}
                                ).get("ok", False)
                            ),
                            "report_path": str(_chapter_style_report_path),
                            "reviewer_model_tier": str(
                                self.config.chapter_style_reviewer_model_tier
                            ),
                            "reviser_model_tier": str(
                                self.config.chapter_style_reviser_model_tier
                            ),
                            "output_path": (
                                str(final_review) if _promoted else ""
                            ),
                        },
                    )
                except Exception as exc:
                    # A failed chapter-style pass must never invalidate an
                    # otherwise complete manuscript or replace its source.
                    self._record_stage(
                        "chapter_style_governance",
                        "failed_fail_open",
                        0.0,
                        0,
                        0,
                        {"error": f"{type(exc).__name__}:{exc}"},
                    )
            else:
                self._set_stage(
                    "chapter_style_governance",
                    (
                        "skipped_offline_test_mode"
                        if self.config.visual_test_mode
                        else "disabled"
                    ),
                )

            # P3-1 (round 3): run the deterministic structure audit on the
            # staged manuscript too.  The publication-mainline path used to
            # mark this stage disabled, so repeated openers / duplicate
            # paragraphs in mainline output were never measured.
            try:
                structure_report = audit_complete_manuscript(
                    manuscript_path=final_review,
                    body_review_path=final_review,
                    completion_package_path=(
                        self.work_dir
                        / "publication_mainline"
                        / "STAGED_COMPLETION_PACKAGE.json"
                    ),
                    blueprint_path=blueprint_path,
                    output_path=(
                        self.work_dir
                        / "publication_mainline"
                        / "ARTICLE_STRUCTURE_AUDIT.json"
                    ),
                    section_manifest_path=(
                        self.work_dir
                        / "publication_mainline"
                        / "full_manuscript_manifest.json"
                    ),
                    expected_section_order=_commander_section_order(
                        self.work_dir
                        / "publication_mainline"
                        / "commander"
                        / "global_commander_work_order.json",
                        [
                            str(row.get("section_id") or "")
                            for row in _read_json(
                                self.work_dir
                                / "publication_mainline"
                                / "full_manuscript_manifest.json"
                            ).get("sections", [])
                            if isinstance(row, Mapping)
                        ],
                    ),
                    body_is_complete_manuscript=True,
                )
                self._set_stage(
                    "article_structure_audit",
                    structure_report["status"],
                    source="publication_mainline",
                    blocking_flag_count=len(
                        structure_report.get("blocking_flags", [])
                    ),
                    nonblocking_flag_count=len(
                        structure_report.get("nonblocking_flags", [])
                    ),
                )
            except Exception as exc:
                # Fail-open: a missing/unshaped staged package must not kill
                # the run; the stage records why it could not audit.
                self._set_stage(
                    "article_structure_audit",
                    "skipped_audit_input_unavailable",
                    error=f"{type(exc).__name__}:{exc}",
                )
        else:
            # Complete the article around the already audited body.  One bounded
            # A-model task builds the cross-section synthesis map and writes all
            # front/back matter together, avoiding four independent premium calls.
            completion_dir = self.work_dir / "article_completion"
            completion_package = (
                completion_dir / "ARTICLE_COMPLETION_PACKAGE.json"
            )
            completion_validation = _read_json(
                completion_dir / "ARTICLE_COMPLETION_VALIDATION.json"
            )
            current_completion_input = collect_article_synthesis_inputs(
                blueprint_path,
                authoring.work_dir / "sections",
            )
            reusable_completion = (
                completion_package.exists()
                and completion_validation.get("status") == "passed"
                and completion_validation.get("input_fingerprint")
                == current_completion_input.get("input_fingerprint")
            )
            if reusable_completion:
                self._set_stage(
                    "article_completion",
                    "completed",
                    reused=True,
                )
            else:
                if completion_dir.exists() and any(
                    item.name != "_invalidated"
                    for item in completion_dir.iterdir()
                ):
                    self._archive_invalid_stage(
                        "article_completion",
                        completion_dir,
                        reason="article_completion_input_changed_or_invalid",
                        details={
                            "expected_input_fingerprint": (
                                current_completion_input.get(
                                    "input_fingerprint",
                                    "",
                                )
                            ),
                            "stored_input_fingerprint": (
                                completion_validation.get(
                                    "input_fingerprint",
                                    "",
                                )
                            ),
                        },
                    )
                completion_remaining = self._remaining_after_future_reserve(
                    self.config.visual_editor_budget_cny,
                    (
                        self.config.research_plan_budget_cny
                        if self.config.produce_research_plan
                        else 0.0
                    ),
                )
                completion_budget = (
                    self._admission_budget(
                        self.config.article_completion_budget_cny
                    )
                    if self.config.global_budget_only
                    else min(
                        self.config.article_completion_budget_cny,
                        completion_remaining,
                    )
                )
                if completion_budget < 0.35:
                    return self._finish(
                        "budget_exhausted",
                        "article_completion",
                        body_review,
                        None,
                    )
                self._set_stage("article_completion", "running")
                completion_result = run_article_completion(
                    ArticleCompletionContext(
                        blueprint_path=blueprint_path,
                        sections_root=authoring.work_dir / "sections",
                        work_dir=completion_dir,
                    ),
                    model_tier=self.config.article_completion_model_tier,
                    model_override=self.config.model_overrides.get(
                        "article_completion"
                    ),
                    cost_budget_cny=completion_budget,
                    token_budget=180_000,
                )
                self._record_worker_stage(
                    "article_completion",
                    completion_result,
                )
                completion_validation = _read_json(
                    completion_dir / "ARTICLE_COMPLETION_VALIDATION.json"
                )
                if (
                    not completion_package.exists()
                    or completion_validation.get("status") != "passed"
                ):
                    return self._finish(
                        "partial",
                        "article_completion",
                        body_review,
                        None,
                    )
                if completion_result.status.value != "completed":
                    self._set_stage(
                        "article_completion",
                        "completed",
                        recovered_after_worker_stop=True,
                        worker_terminal_status=(
                            completion_result.status.value
                        ),
                        stop_reason=completion_result.stop_reason,
                    )

            global_figure_plan_path = (
                completion_dir / "GLOBAL_FIGURE_PLAN.json"
            )
            # Figure plan + assembly + audit all happen under this stage, so
            # open its timer here.  Previously the only call for this stage was
            # the terminal _set_stage below, which emitted no stage_started and
            # left wall_time at 0.0 for real assembly work -- and left the UI
            # stage track unable to ever show it as in-flight.
            self._set_stage("article_structure_audit", "running")
            try:
                build_global_figure_plan(
                    blueprint_path=blueprint_path,
                    synthesis_map_path=(
                        completion_dir / "ARTICLE_SYNTHESIS_MAP.json"
                    ),
                    output_path=global_figure_plan_path,
                )
                initial_manifest = assemble_complete_manuscript(
                    completion_package_path=completion_package,
                    body_review_path=body_review,
                    output_dir=completion_dir / "manuscript",
                    global_figure_plan_path=global_figure_plan_path,
                )
                final_review = Path(initial_manifest["manuscript_path"])
                structure_report = audit_complete_manuscript(
                    manuscript_path=final_review,
                    body_review_path=body_review,
                    completion_package_path=completion_package,
                    blueprint_path=blueprint_path,
                    output_path=(
                        completion_dir / "ARTICLE_STRUCTURE_AUDIT.json"
                    ),
                )
                self._set_stage(
                    "article_structure_audit",
                    structure_report["status"],
                    blocking_flag_count=len(
                        structure_report.get("blocking_flags", [])
                    ),
                    nonblocking_flag_count=len(
                        structure_report.get("nonblocking_flags", [])
                    ),
                )
                if structure_report["status"] == "failed":
                    return self._finish(
                        "partial",
                        "article_structure_audit",
                        final_review,
                        None,
                    )
            except Exception as exc:
                self._set_stage(
                    "article_structure_audit",
                    "failed",
                    error=f"{type(exc).__name__}:{exc}",
                )
                return self._finish(
                    "partial",
                    "article_structure_audit",
                    body_review,
                    None,
                )

        # Stage 4 visual editor: content plan and canonical image paths only.
        visual_plan = None
        remaining = (
            self._remaining_global_budget()
            if self.config.global_budget_only
            else (
                self.config.global_cost_budget_cny
                - self._total_cost_cny()
                - (
                    self.config.research_plan_budget_cny
                    if self.config.produce_research_plan
                    else 0.0
                )
            )
        )
        visual_dir = self.work_dir / "visual_editor"
        existing_visual_plan = visual_dir / "VISUAL_EDITORIAL_PLAN.json"
        # Backend-fix ticket 2.1: the visual editor used to receive the raw
        # seed KB (base_kb_sqlite), which carries no visual_chunks table, so
        # the editor saw zero candidates while 260 sat in the S2 runtime KB.
        # Resolve candidate libraries by whether they actually expose the
        # table, S2 runtime first.
        visual_kb_candidates: List[Path] = []
        # Round-2 defect A: resolve from the pinned S2 KB, NOT from the
        # ``runtime_kb`` local -- by this point the blueprint projection may
        # have rebound it (and config.base_kb_sqlite with it) to a library
        # without visual_chunks, which silently starved the editor again.
        if (
            self._s2_visual_kb is not None
            and Path(self._s2_visual_kb).exists()
        ):
            visual_kb_candidates.append(Path(self._s2_visual_kb))
        if coverage.work_dir.joinpath("supplemental_oa_kb.sqlite").exists():
            visual_kb_candidates.append(
                coverage.work_dir / "supplemental_oa_kb.sqlite"
            )
        visual_kb_candidates.append(self.config.base_kb_sqlite)
        visual_kb_paths = self._kb_paths_with_visual_tables(
            visual_kb_candidates
        )
        visual_cache_namespace = derive_visual_cache_namespace(blueprint)
        visual_cache_dir = scoped_visual_cache_dir(
            Path(__file__).resolve().parents[2]
            / "literature_workspace"
            / "visual_evidence_cache",
            blueprint,
            namespace=visual_cache_namespace,
        )
        if not visual_kb_paths:
            # Ticket 2.2/2.4 companion: an empty candidate list would leave
            # the provider-side alarms without any input at all.  Say so.
            logger.warning(
                "visual editor found no candidate library exposing a "
                "visual_chunks/units table (checked: %s)",
                [str(path) for path in visual_kb_candidates],
            )
        visual_prompt = (
            Path(__file__).resolve().parents[2]
            / "prompts"
            / "roles"
            / "Visual Editor.txt"
        ).read_text(encoding="utf-8")
        expected_visual_fingerprint = visual_editor_input_fingerprint(
            blueprint=blueprint,
            review_work_dir=visual_review_work_dir,
            kb_sqlite_paths=visual_kb_paths,
            role_prompt=visual_prompt,
        )
        from .visual_editor_tool_provider import (
            validate_visual_editorial_plan_file,
        )
        expected_visual_section_ids = {
            str(section.get("section_id"))
            for section in blueprint.get("sections", [])
            if isinstance(section, dict)
            and section.get("section_id")
            and (
                section.get("visual_argument_slots")
                or section.get("expected_visual_arguments")
            )
        }
        existing_visual_validation = validate_visual_editorial_plan_file(
            existing_visual_plan,
            expected_visual_fingerprint,
            expected_visual_section_ids,
        )
        if existing_visual_validation.startswith("VALIDATION_PASSED"):
            visual_plan = existing_visual_plan
            self._set_stage(
                "visual_editor",
                "completed",
                reused=True,
                deterministic_validation=existing_visual_validation,
            )
        elif remaining >= 0.3:
            self._set_stage("visual_editor", "running")
            # Preserve most of the visual envelope for auditing the selected
            # shortlist and generating at most a few actual assets.  The
            # planning agent historically needs only a small fraction.
            planning_budget = (
                self._admission_budget(self.config.visual_editor_budget_cny)
                if self.config.global_budget_only
                else min(
                    max(0.45, self.config.visual_editor_budget_cny * 0.35),
                    remaining,
                )
            )
            visual_result = run_visual_editor(
                blueprint=blueprint,
                review_work_dir=visual_review_work_dir,
                output_dir=visual_dir,
                kb_sqlite_paths=visual_kb_paths,
                model_tier=self.config.visual_editor_model_tier,
                model_override=self.config.model_overrides.get("visual_editor"),
                cost_budget_cny=planning_budget,
            )
            nested_cost = _read_json(
                visual_dir / "NESTED_VISION_COST.json"
            )
            visual_cost, visual_input, visual_output = (
                self._cost_totals_from_task_costs(visual_dir)
            )
            self._record_stage(
                "visual_editor",
                visual_result.status.value,
                visual_cost
                + float(nested_cost.get("estimated_cost_cny", 0.0) or 0.0),
                visual_input
                + int(nested_cost.get("input_tokens", 0) or 0),
                visual_output
                + int(nested_cost.get("output_tokens", 0) or 0),
                {
                    "stop_reason": visual_result.stop_reason,
                    "nested_vision_calls": int(nested_cost.get("calls", 0) or 0),
                    "nested_vision_cost_cny": float(
                        nested_cost.get("estimated_cost_cny", 0.0) or 0.0
                    ),
                },
            )
            candidate = visual_dir / "VISUAL_EDITORIAL_PLAN.json"
            if validate_visual_editorial_plan_file(
                candidate,
                expected_visual_fingerprint,
                expected_visual_section_ids,
            ).startswith("VALIDATION_PASSED"):
                visual_plan = candidate
        else:
            self._set_stage(
                "visual_editor",
                "skipped_cost_budget",
            )

        if (
            visual_plan is not None
            and global_figure_plan_path is not None
            and global_figure_plan_path.exists()
        ):
            try:
                merge_global_figures_into_visual_plan(
                    visual_plan_path=visual_plan,
                    global_figure_plan_path=global_figure_plan_path,
                    blueprint_path=blueprint_path,
                )
                merged_validation = validate_visual_editorial_plan_file(
                    visual_plan,
                    expected_visual_fingerprint,
                    expected_visual_section_ids,
                )
                if not merged_validation.startswith("VALIDATION_PASSED"):
                    self._set_stage(
                        "visual_editor",
                        "failed",
                        deterministic_validation=merged_validation,
                    )
                    visual_plan = None
            except Exception as exc:
                self._set_stage(
                    "visual_editor",
                    "failed",
                    error=f"global_figure_merge:{type(exc).__name__}:{exc}",
                )
                visual_plan = None

        # Materialize actual source/generated images.  A request is not a
        # figure until this stage writes a validated FINAL_VISUAL_PACKAGE.
        final_visual_package = None
        if visual_plan is not None:
            factory_dir = visual_dir / "final"
            candidate_package = (
                factory_dir / "FINAL_VISUAL_PACKAGE.json"
            )
            existing_factory_validation = (
                validate_final_visual_package_file(candidate_package)
            )
            existing_factory_value = _read_json(candidate_package)
            expected_factory_fingerprint = (
                build_visual_factory_input_fingerprint(
                    visual_plan=_read_json(visual_plan),
                    blueprint=blueprint,
                    real_visual_audit=(
                        self.config.visual_real_audit
                    ),
                    real_image_generation=(
                        self.config.visual_real_generation
                    ),
                    test_mode=self.config.visual_test_mode,
                    vision_model_tier="vision_plus_model",
                    image_model=self.config.visual_image_model,
                    max_generated_images=(
                        self.config.visual_max_generated_images
                    ),
                    cache_namespace=visual_cache_namespace,
                )
            )
            reusable_factory_package = bool(
                existing_factory_validation.startswith(
                    "VALIDATION_PASSED"
                )
                and existing_factory_value.get("input_fingerprint")
                == expected_factory_fingerprint
            )
            if reusable_factory_package:
                final_visual_package = candidate_package
                self._set_stage(
                    "visual_materialization",
                    "completed",
                    reused=True,
                    deterministic_validation=(
                        existing_factory_validation
                    ),
                )
            else:
                visual_spent = float(
                    self.stage_costs.get("visual_editor", {}).get(
                        "estimated_cost_cny",
                        0.0,
                    )
                    or 0.0
                )
                if self.config.global_budget_only:
                    # Do not reserve a separate visual-stage envelope in a
                    # global-only run.  The visual planner and materializer
                    # draw from the same live balance as every other stage.
                    factory_budget = self._remaining_global_budget()
                else:
                    factory_envelope = max(
                        0.0,
                        self.config.visual_editor_budget_cny
                        - visual_spent,
                    )
                    global_remaining = max(
                        0.0,
                        self.config.global_cost_budget_cny
                        - self._total_cost_cny()
                        - (
                            self.config.research_plan_budget_cny
                            if self.config.produce_research_plan
                            else 0.0
                        ),
                    )
                    factory_budget = min(
                        factory_envelope,
                        global_remaining,
                    )
                stage_budget_details = (
                    {
                        "budget_policy": "global_remaining_snapshot",
                        "global_remaining_cny": round(factory_budget, 6),
                    }
                    if self.config.global_budget_only
                    else {
                        "budget_policy": "legacy_stage_cap",
                        "budget_cny": round(factory_budget, 6),
                    }
                )
                self._set_stage(
                    "visual_materialization",
                    "running",
                    **stage_budget_details,
                )
                visual_package_value = run_visual_evidence_factory(
                    visual_plan_path=visual_plan,
                    blueprint=blueprint,
                    review_work_dir=visual_review_work_dir,
                    output_dir=factory_dir,
                    cost_budget_cny=(
                        factory_budget
                        if not self.config.global_budget_only
                        else 5.0
                    ),
                    global_budget_remaining_cny=(
                        factory_budget
                        if self.config.global_budget_only
                        else None
                    ),
                    real_visual_audit=(
                        self.config.visual_real_audit
                    ),
                    real_image_generation=(
                        self.config.visual_real_generation
                    ),
                    test_mode=self.config.visual_test_mode,
                    vision_model_tier="vision_plus_model",
                    image_model=self.config.visual_image_model,
                    max_generated_images=(
                        self.config.visual_max_generated_images
                    ),
                    visual_review_auto_accept_seconds=(
                        self.config.visual_review_auto_accept_seconds
                        if self.config.visual_review_auto_accept_seconds
                        is not None
                        else self._effective_gate_seconds()
                    ),
                    execution_profile=self.config.execution_profile,
                    workers=self.config.visual_workers,
                    run_id=self.run_id,
                    shared_cache_dir=(
                        visual_cache_dir
                    ),
                    cache_namespace=visual_cache_namespace,
                )
                visual_cost = dict(
                    visual_package_value.get(
                        "visual_cost_report",
                        {},
                    )
                    or {}
                )
                factory_validation = (
                    validate_final_visual_package_file(
                        candidate_package
                    )
                )
                # P1-3 (round 3): only a clean pass counts as completed.
                # DEGRADED (no figures / unfilled needs) must stay visible as
                # its own outcome instead of silently reading "completed".
                if factory_validation.startswith("VALIDATION_PASSED"):
                    factory_status = "completed"
                elif factory_validation.startswith("VALIDATION_DEGRADED"):
                    factory_status = "degraded"
                else:
                    factory_status = "failed"
                self._record_stage(
                    "visual_materialization",
                    factory_status,
                    float(
                        visual_cost.get(
                            "estimated_cost_cny",
                            0.0,
                        )
                        or 0.0
                    ),
                    int(
                        visual_cost.get(
                            "vision_input_tokens",
                            0,
                        )
                        or 0
                    )
                    + int(
                        visual_cost.get(
                            "diagram_spec_input_tokens",
                            0,
                        )
                        or 0
                    ),
                    int(
                        visual_cost.get(
                            "vision_output_tokens",
                            0,
                        )
                        or 0
                    )
                    + int(
                        visual_cost.get(
                            "diagram_spec_output_tokens",
                            0,
                        )
                        or 0
                    ),
                    {
                        "validation": factory_validation,
                        "figure_count": int(
                            visual_package_value.get(
                                "validation",
                                {},
                            ).get("figure_count", 0)
                            or 0
                        ),
                    },
                )
                if factory_status in {"completed", "degraded"}:
                    final_visual_package = candidate_package
                # P1-3 (round 3): every unmet need becomes one bounded
                # transformation task.  With no adapters configured (offline/
                # test) each task is recorded as not-executed -- the queue is
                # measured, not silently dropped; a production CLI injects
                # real adapters through VISUAL_TRANSFORMATION_ADAPTERS.
                # Both field names are live and they do not overlap.  Measured
                # on rhr_be780761's FINAL_VISUAL_PACKAGE: unfilled_visual_needs
                # was empty and all seven rows sat in
                # unfilled_visual_opportunities -- including the only three
                # worth retrying (S01 generation_attempts_exhausted, S04/S05
                # generation_task_budget_or_lower_priority).  Reading one name
                # queued the four "no traceable source figures" rows, which
                # retrying cannot help, and dropped the three that a retry is
                # exactly for.  The LaTeX renderer already reads both under the
                # single label ``unfilled_visual_needs``, which is why the
                # build report said 7 while the queue saw 4.
                _unfilled_needs = self._collect_unfilled_visual_needs(
                    visual_package_value,
                    _read_json(visual_plan) if visual_plan else {},
                )
                if _unfilled_needs:
                    try:
                        workflow = VisualTransformationWorkflow(
                            VisualTransformationWorkflowConfig()
                        )
                        # The cap is a cost bound, not a judgement that the
                        # rest do not matter.  It used to be a bare slice, so
                        # a run with 40 unmet needs reported "submitted: 16"
                        # and the other 24 vanished with no log and no field
                        # anyone could count.  Say how many were dropped.
                        _queued = _unfilled_needs[
                            :_MAX_VISUAL_TRANSFORMATION_TASKS
                        ]
                        _dropped = len(_unfilled_needs) - len(_queued)
                        if _dropped:
                            logger.warning(
                                "visual transformation queue capped at %d: "
                                "%d of %d unfilled needs not submitted",
                                _MAX_VISUAL_TRANSFORMATION_TASKS,
                                _dropped,
                                len(_unfilled_needs),
                            )
                        task_reports = []
                        for _idx, _need in enumerate(_queued):
                            _task = {
                                "task_id": (
                                    f"{self.run_id}:unfilled:{_idx}"
                                ),
                                "source": "unfilled_visual_need",
                                "need": _need,
                                "run_id": self.run_id,
                            }
                            _record = workflow.submit(_task)
                            _record = workflow.run(_record)
                            task_reports.append(
                                {
                                    "task_id": _task["task_id"],
                                    "policy_decision": (
                                        _record.get("classification", {}).get(
                                            "policy_decision"
                                        )
                                        or _record.get("policy_decision")
                                    ),
                                    "status": str(
                                        _record.get("state")
                                        or _record.get("status") or ""
                                    ),
                                }
                            )
                        atomic_write_json(
                            factory_dir / "VISUAL_TRANSFORMATION_REPORT.json",
                            {
                                "schema_version": (
                                    "research_harness.visual_transformation.v1"
                                ),
                                "submitted": len(task_reports),
                                "unfilled_needs_total": len(_unfilled_needs),
                                "not_submitted": _dropped,
                                "submission_cap": (
                                    _MAX_VISUAL_TRANSFORMATION_TASKS
                                ),
                                "tasks": task_reports,
                            },
                        )
                        # Annotate, not overwrite: a bare _set_stage here
                        # dropped the validation verdict, figure_count and
                        # wall_time_seconds that _record_stage had just
                        # written for this same stage.
                        self._annotate_stage(
                            "visual_materialization",
                            factory_status,
                            unfilled_needs_submitted=len(task_reports),
                            unfilled_needs_total=len(_unfilled_needs),
                            unfilled_needs_not_submitted=_dropped,
                        )
                    except Exception as exc:
                        logger.warning(
                            "visual transformation queue failed: %s: %s",
                            type(exc).__name__,
                            exc,
                        )
        else:
            self._set_stage(
                "visual_materialization",
                "skipped_no_visual_plan",
            )

        if not self.config.publication_mainline_enabled:
            # Refresh the legacy manuscript manifest with final canonical
            # figure paths. The prose is unchanged; the companion placement
            # file is the handoff to the later LaTeX layout stage.
            final_manifest = assemble_complete_manuscript(
                completion_package_path=completion_package,
                body_review_path=body_review,
                output_dir=completion_dir / "manuscript",
                final_visual_package_path=final_visual_package,
                global_figure_plan_path=global_figure_plan_path,
            )
            final_review = Path(final_manifest["manuscript_path"])

        final_status = authoring.status
        if (
            publication_mainline_result is not None
            and final_status == "completed"
        ):
            if publication_mainline_result.status == "awaiting_human_review":
                # P0-1: bounded_patch_proposals parked here with no decision
                # object.  Give the human the window; on timeout rewrite the
                # stage status so the run can finish instead of stalling.
                if self._resolve_human_gate(
                    stage="publication_mainline_staged_completion",
                    kind="staged_completion_approval",
                    subject_id=f"{self.run_id}:staged_completion",
                    context={
                        "awaiting_approval_stages": list(
                            getattr(
                                publication_mainline_result,
                                "fail_open_issues",
                                [],
                            )
                            or []
                        ),
                        "completed_stage": str(
                            publication_mainline_result.completed_stage or ""
                        ),
                    },
                    original_status="awaiting_human_review",
                ):
                    final_status = "completed"
                else:
                    final_status = "awaiting_human_review"
            elif publication_mainline_result.status != "completed":
                final_status = "partial"
        if final_status == "completed" and visual_plan is None:
            # Text can be complete without figures, but the visual editorial
            # step still needs explicit human review when it did not run.
            final_status = "awaiting_human_review"
        elif final_status == "completed" and final_visual_package:
            final_visual_value = _read_json(final_visual_package)
            if (
                expected_visual_section_ids
                and not final_visual_value.get("figures")
            ):
                final_status = "awaiting_human_review"

        # Stage 6: derive a research program from the completed review.  This
        # is a separate intellectual product, but it reuses the same evidence
        # allowlist, state/recovery kernel, and global cost ledger.
        research_plan = None
        if self.config.produce_research_plan:
            # Give the architect the deterministic review assessment without
            # waiting for final packaging.  Packaging is the normal owner of
            # the final map, but this stage runs before packaging; create the
            # same final-text-only map once when needed and reuse it.  Never
            # feed the pre-enhancement FULL_REVIEW_CITATION_MAP directly into
            # downstream quality logic because it has a different identity
            # universe from the delivered manuscript.
            research_citation_map_path = self.work_dir / "FINAL_CITATION_MAP.json"
            if not research_citation_map_path.is_file():
                build_final_citation_map(
                    markdown_path=final_review,
                    output_path=research_citation_map_path,
                )
            evaluate_review_content(
                final_review_path=final_review,
                blueprint=blueprint,
                visual_plan_path=(
                    final_visual_package or visual_plan
                ),
                citation_map_path=research_citation_map_path,
                output_dir=self.work_dir,
            )
            plan_remaining = (
                self.config.global_cost_budget_cny
                - self._total_cost_cny()
            )
            if plan_remaining >= 0.5:
                self._set_stage("research_plan", "running")
                research_dir = self.work_dir / "research_program"
                plan_result = run_research_program(
                    ResearchProgramContext(
                        blueprint_path=blueprint_path,
                        final_review_path=final_review,
                        coverage_root=coverage.work_dir,
                        work_dir=research_dir,
                        phase3_artifacts_root=self.config.phase3_artifacts_root,
                        base_kb_sqlite=self.config.base_kb_sqlite,
                        staging_kb_sqlite=(
                            coverage.work_dir
                            / "supplemental_oa_kb.sqlite"
                        ),
                        quality_report_path=(
                            self.work_dir
                            / "REVIEW_HARNESS_QUALITY_REPORT.json"
                        ),
                        literature_portfolio_path=(
                            self.work_dir
                            / "LITERATURE_PORTFOLIO_REPORT.json"
                        ),
                        visual_plan_path=(
                            final_visual_package or visual_plan
                        ),
                        query_plan_path=self.config.query_plan_path,
                    ),
                    run_id=self.run_id,
                    model_tier=self.config.managing_editor_model_tier,
                    model_override=self.config.model_overrides.get(
                        "research_program"
                    ),
                    cost_budget_cny=(
                        self._admission_budget(
                            self.config.research_plan_budget_cny
                        )
                        if self.config.global_budget_only
                        else min(
                            self.config.research_plan_budget_cny,
                            plan_remaining,
                        )
                    ),
                    auto_continue_discovery=True,
                )
                self._record_worker_stage("research_plan", plan_result)
                plan_stage_status = str(getattr(plan_result, "status", ""))
                plan_status_value = (
                    plan_stage_status.value
                    if hasattr(plan_stage_status, "value")
                    else str(plan_stage_status)
                )
                if plan_status_value in {"waiting_for_human", "awaiting_human_review"}:
                    # P0-1: the discovery TODO call site in
                    # research_program_runner deliberately left registration
                    # to this layer; give the human their window and rewrite
                    # the parked stage status on timeout.
                    self._resolve_human_gate(
                        stage="research_plan",
                        kind="discovery_needs_more_literature",
                        subject_id=f"{self.run_id}:research_plan",
                        context={
                            "stop_reason": str(
                                getattr(plan_result, "stop_reason", "") or ""
                            ),
                            "gap_description": "see R5_DISCOVERY_STATUS.json",
                        },
                        original_status=plan_status_value,
                    )
                candidate_plan = research_dir / "RESEARCH_PLAN.md"
                audit = _read_json(
                    research_dir / "RESEARCH_PLAN_AUDIT.json"
                )
                if (
                    candidate_plan.exists()
                    and audit.get("status") == "passed"
                ):
                    research_plan = candidate_plan
                elif final_status == "completed":
                    final_status = "partial"
            else:
                self._set_stage(
                    "research_plan",
                    "skipped_cost_budget",
                )
                if final_status == "completed":
                    final_status = "partial"
        else:
            self._set_stage("research_plan", "disabled")
        return self._finish(
            final_status,
            "packaging",
            final_review,
            visual_plan,
            research_plan,
            final_visual_package,
        )

    def _stage_completed(self, stage: str, artifact: Path) -> bool:
        return (
            self.state.get("stages", {}).get(stage, {}).get("status")
            == "completed"
            and artifact.exists()
        )

    def _archive_invalid_stage(
        self,
        stage: str,
        stage_dir: Path,
        *,
        reason: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """Archive, rather than delete, a cached stage that fails new gates."""

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_root = stage_dir / "_invalidated"
        archive_dir = archive_root / stamp
        archive_dir.mkdir(parents=True, exist_ok=True)
        for item in list(stage_dir.iterdir()):
            if item == archive_root:
                continue
            shutil.move(str(item), str(archive_dir / item.name))
        atomic_write_json(
            archive_dir / "INVALIDATION.json",
            {
                "schema_version": "research_harness.invalidation.v1",
                "stage": stage,
                "reason": reason,
                "details": details or {},
                "invalidated_at": _now(),
            },
        )
        prior_cost = self.stage_costs.pop(stage, None)
        if prior_cost:
            suffix = 1
            historical_name = f"{stage}_invalidated_attempt_{suffix}"
            while historical_name in self.stage_costs:
                suffix += 1
                historical_name = (
                    f"{stage}_invalidated_attempt_{suffix}"
                )
            self.stage_costs[historical_name] = prior_cost
        self.state.setdefault("stages", {})[stage] = {
            "status": "invalidated",
            "updated_at": _now(),
            "reason": reason,
            "archive_dir": str(archive_dir),
        }
        self._save_cost()
        self._save_state()
        return archive_dir

    def _prior_stage_work_dir(self, stage: str) -> Optional[Path]:
        raw = (
            self.state.get("stages", {})
            .get(stage, {})
            .get("work_dir", "")
        )
        if not raw:
            return None
        path = Path(str(raw))
        return path if path.exists() else None

    @staticmethod
    def _author_feedback_sections(authoring_dir: Path) -> list[str]:
        registry = _read_json(authoring_dir / "SECTION_REGISTRY.json")
        return [
            str(section.get("section_id"))
            for section in registry.get("sections", [])
            if isinstance(section, dict)
            and section.get("status") == "needs_more_literature"
            and section.get("section_id")
        ]

    def _coverage_feedback_requests(
        self,
        authoring_dir: Path,
    ) -> Dict[str, Dict[str, Any]]:
        """Route only literature-owned findings back to section research."""

        feedback: Dict[str, Dict[str, Any]] = {}
        for section_id in self._author_feedback_sections(authoring_dir):
            value = _read_json(
                authoring_dir
                / "sections"
                / section_id
                / "SECTION_COVERAGE_FEEDBACK.json"
            )
            if value:
                feedback[section_id] = value

        audit_paths = sorted(
            authoring_dir.glob("audit_round_*/GLOBAL_AUDIT_REPORT.json")
        )
        if not audit_paths:
            return feedback
        latest = _read_json(audit_paths[-1])
        literature_types = {
            "source_concentration",
            "source_diversity",
            "citation_concentration",
            "literature_breadth",
            "insufficient_source_diversity",
            "evidence_synthesis",
            "missing_pivotal_evidence",
        }
        for flag in latest.get("flags", []):
            if (
                not isinstance(flag, dict)
                or str(flag.get("type") or "") not in literature_types
            ):
                continue
            for section_id in flag.get("section_ids", []):
                section_id = str(section_id)
                if not section_id:
                    continue
                entry = feedback.setdefault(
                    section_id,
                    {
                        "schema_version": (
                            "research_harness.coverage_feedback.v1"
                        ),
                        "feedback_origin": "managing_editor",
                        "feedback_items": [],
                    },
                )
                entry.setdefault("feedback_items", []).append(
                    {
                        "gap_type": str(flag.get("type") or ""),
                        "role": "",
                        "description": str(
                            flag.get("description") or ""
                        ),
                        "required_outcome": (
                            "Adopt additional independent direct sources and "
                            "reduce reliance on the dominant paper. Preserve "
                            "the section argument unless new evidence changes it."
                        ),
                        "editor_recommended_action": str(
                            flag.get("recommended_action") or ""
                        ),
                    }
                )
        return feedback

    def _write_feedback_blueprint(
        self,
        blueprint: Dict[str, Any],
        authoring_dir: Path,
        section_ids: list[str],
        feedback_by_section: Optional[
            Dict[str, Dict[str, Any]]
        ] = None,
    ) -> Path:
        selected = set(section_ids)
        feedback_sections = []
        for section in blueprint.get("sections", []):
            if (
                not isinstance(section, dict)
                or section.get("section_id") not in selected
            ):
                continue
            item = dict(section)
            feedback = (
                (feedback_by_section or {}).get(
                    str(section["section_id"]), {}
                )
                or _read_json(
                    authoring_dir
                    / "sections"
                    / str(section["section_id"])
                    / "SECTION_COVERAGE_FEEDBACK.json"
                )
            )
            item["author_coverage_feedback"] = feedback
            questions = list(item.get("key_questions", []))
            required = list(item.get("required_roles", []))
            for entry in feedback.get("feedback_items", []):
                if not isinstance(entry, dict):
                    continue
                description = str(entry.get("description") or "").strip()
                if description and description not in questions:
                    questions.append(description)
                role = str(entry.get("role") or "")
                if role and role not in required:
                    required.append(role)
            item["key_questions"] = questions[:8]
            item["required_roles"] = required[:6]
            feedback_sections.append(item)
        path = self.work_dir / "AUTHOR_FEEDBACK_BLUEPRINT.json"
        atomic_write_json(
            path,
            {
                **blueprint,
                "schema_version": "research_harness.feedback_blueprint.v1",
                "sections": feedback_sections,
                "feedback_scope": "pivotal_author_reported_gaps_only",
            },
        )
        return path

    @staticmethod
    def _token_totals_from_task_costs(root: Path) -> tuple[int, int]:
        """Aggregate leaf ResearchWorker ledgers without double counting.

        Aggregate stage-level COST files do not contain ``task_id`` and are
        deliberately skipped.
        """

        input_tokens = 0
        output_tokens = 0
        if not root.exists():
            return 0, 0
        for path in root.rglob("COST.json"):
            value = _read_json(path)
            if not value.get("task_id"):
                continue
            input_tokens += int(value.get("total_input_tokens", 0) or 0)
            output_tokens += int(value.get("total_output_tokens", 0) or 0)
        return input_tokens, output_tokens

    @staticmethod
    def _cost_totals_from_task_costs(
        root: Path,
    ) -> tuple[float, int, int]:
        """Aggregate all leaf worker costs for a resumable stage."""

        cost_cny = 0.0
        input_tokens = 0
        output_tokens = 0
        if not root.exists():
            return 0.0, 0, 0
        for path in root.rglob("COST.json"):
            value = _read_json(path)
            if not value.get("task_id"):
                continue
            cost_cny += float(
                value.get("cost_cny", value.get("estimated_cost_cny", 0.0))
                or 0.0
            )
            input_tokens += int(
                value.get("input_tokens", value.get("total_input_tokens", 0))
                or 0
            )
            output_tokens += int(
                value.get("output_tokens", value.get("total_output_tokens", 0))
                or 0
            )
        return round(cost_cny, 6), input_tokens, output_tokens

    @staticmethod
    def _phase3_usage_from_artifacts(
        root: Path,
    ) -> tuple[float, int, int, dict[str, Any]]:
        """Read Phase-3 usage from canonical artifacts exactly once.

        ``PHASE3_RUN.json`` is authoritative for M2a/M2b usage.  The
        acceptance cost block is a compatibility fallback for older runs.
        The harness stage ledger keeps maxima across a resume, so reading the
        same artifact again cannot double-count a cached Phase-3 attempt.
        """
        candidates = [
            (
                "PHASE3_RUN.json:llm",
                _read_json(root / "PHASE3_RUN.json").get("llm"),
            ),
            (
                "PHASE3_ACCEPTANCE.json:cost",
                _read_json(root / "PHASE3_ACCEPTANCE.json").get("cost"),
            ),
        ]
        for source, raw in candidates:
            if not isinstance(raw, dict):
                continue
            cost = float(
                raw.get("estimated_cost_cny", raw.get("cost_cny", 0.0)) or 0.0
            )
            input_tokens = max(
                int(raw.get("input_tokens_observed", 0) or 0),
                int(raw.get("input_tokens", 0) or 0),
                int(raw.get("estimated_input_tokens_total", 0) or 0),
            )
            output_tokens = max(
                int(raw.get("output_tokens_observed", 0) or 0),
                int(raw.get("output_tokens", 0) or 0),
                int(raw.get("estimated_output_tokens_total", 0) or 0),
            )
            calls = int(
                raw.get("calls_observed_or_estimated", raw.get("qwen_calls", 0))
                or 0
            )
            if cost or input_tokens or output_tokens or calls:
                return (
                    round(cost, 6),
                    input_tokens,
                    output_tokens,
                    {
                        "source": source,
                        "calls": calls,
                        "token_count_source": raw.get("token_count_source", "unavailable"),
                        "usage_is_provider_reported": bool(
                            raw.get("usage_is_provider_reported", False)
                        ),
                    },
                )
        return 0.0, 0, 0, {"source": "unavailable", "calls": 0}

    def _set_stage(self, stage: str, status: str, **details: Any) -> None:
        if status == "running":
            self.observability.start_stage(stage, **details)
        else:
            self.observability.finish_stage(
                stage,
                status,
                **details,
            )
        self.state["current_stage"] = stage
        self.state["status"] = "running"
        self.state.setdefault("stages", {})[stage] = {
            "status": status,
            "updated_at": _now(),
            **details,
        }
        self._save_state()
        self.observability.snapshot(
            status="running",
            current_stage=stage,
            stage_costs=self.stage_costs,
            harness_state=self.state,
        )

    def _effective_gate_seconds(self) -> Optional[float]:
        """P0-1: None/<=0 keeps the historical infinite wait."""

        seconds = self.config.human_gate_auto_accept_seconds
        if seconds is None or float(seconds) <= 0:
            return None
        return float(seconds)

    def _resolve_human_gate(
        self,
        *,
        stage: str,
        kind: str,
        subject_id: str,
        context: Dict[str, Any],
        original_status: str,
        options: Optional[List[str]] = None,
    ) -> bool:
        """Register one human gate, wait at most N seconds, then settle it.

        Round-3 P0-1: stages that used to park on ``awaiting_*`` forever now
        register a real decision, give a human the configured window, and —
        when the window closes with no answer — rewrite the stage status to
        ``completed`` with ``human_gate`` provenance.  Without the status
        rewrite the delivery gate would still see ``awaiting_*`` and the run
        would still be degraded, which is exactly the failure this closes.

        Round-4: the verdict comes from the *chosen option*, so an explicit
        human ``accept`` completes the stage exactly like a timeout does, and
        an explicit ``reject`` holds it on its awaiting status.  The stage row
        is annotated either way, so a gate that was reached is always visible
        in ``stage_status`` even when it did not open.  Returns True when the
        stage was accepted.
        """

        seconds = self._effective_gate_seconds()
        options = list(options or ["accept", "reject"])
        try:
            decision_id = request_decision(
                run_dir=self.work_dir,
                kind=kind,
                subject_id=subject_id,
                context=context,
                options=options,
                auto_accept_after_seconds=seconds,
                default_option=options[0],
            )
            state = self._await_gate_decision(decision_id, seconds)
        except Exception as exc:
            logger.warning(
                "human gate %s/%s failed to register: %s: %s",
                kind,
                subject_id,
                type(exc).__name__,
                exc,
            )
            self._annotate_stage(
                stage,
                original_status,
                human_gate="registration_failed",
                human_gate_error=f"{type(exc).__name__}: {exc}",
            )
            return False
        chosen = str(state.get("chosen") or "").strip().lower()
        resolved = state.get("state") == "resolved"
        if resolved and chosen in _GATE_ACCEPT_OPTIONS:
            if state.get("auto"):
                provenance = (
                    f"auto_accepted_after_"
                    f"{seconds:g}s" if seconds is not None else "auto_accepted"
                )
            else:
                provenance = f"human_accepted:{state.get('actor') or 'human'}"
            self._annotate_stage(
                stage,
                "completed",
                human_gate=provenance,
                human_gate_chosen=chosen,
                original_status=original_status,
                decision_id=decision_id,
            )
            return True
        self._annotate_stage(
            stage,
            original_status,
            human_gate=("rejected" if resolved else "pending"),
            human_gate_chosen=(chosen or None),
            decision_id=decision_id,
        )
        return False

    def _await_gate_decision(
        self,
        decision_id: str,
        seconds: Optional[float],
    ) -> Dict[str, Any]:
        """Return the decision state once resolved or once its window closes."""

        if seconds is None:
            # No window configured: the gate is not self-settling.  Sweep once
            # so any *other* due decision still gets its timeout, then report
            # whatever this one currently is.
            expire_due_decisions(self.work_dir)
            return decision_state(self.work_dir, decision_id)
        deadline = time.monotonic() + float(seconds)
        while True:
            state = decision_state(self.work_dir, decision_id)
            if state.get("state") == "resolved":
                return state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_GATE_POLL_SECONDS, remaining))
        # P0-1.5: registration alone never settles due items -- without an
        # explicit sweep a last-registered gate would sit pending forever
        # because nothing else triggers _expire_due.
        expire_due_decisions(self.work_dir)
        return decision_state(self.work_dir, decision_id)

    def _annotate_stage(self, stage: str, status: str, **details: Any) -> None:
        """Annotate an existing stage without emitting another transition.

        ``_set_stage`` replaces the whole stage row.  Used bare by a gate that
        fires after ``_record_stage``, it silently dropped that stage's
        ``work_dir`` -- which ``_prior_stage_work_dir`` needs on resume -- and
        its measured ``wall_time_seconds``.  Merge instead of overwrite.
        """

        prior = dict(self.state.get("stages", {}).get(stage) or {})
        prior.pop("status", None)
        prior.pop("updated_at", None)
        prior.update({k: v for k, v in details.items() if v is not None})
        # A late gate verdict is metadata for work that already finished.
        # Calling _set_stage here emits a second stage_finished event and asks
        # observability to add the same wall-clock interval again.
        self.state.setdefault("stages", {})[stage] = {
            "status": status,
            "updated_at": _now(),
            **prior,
        }
        self._save_state()
        self.observability.snapshot(
            status="running",
            current_stage=self.state.get("current_stage", stage),
            stage_costs=self.stage_costs,
            harness_state=self.state,
        )

    @staticmethod
    def _collect_unfilled_visual_needs(
        visual_package: Mapping[str, Any],
        visual_plan: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        """Merge every field an unmet visual need can be filed under.

        ``unfilled_visual_needs`` and ``unfilled_visual_opportunities`` are both
        live, they carry different rows, and which one a given run populates
        depends on the producer.  Deduplicated on (section_id, reason) so a row
        present in both the package and the plan is queued once.
        """

        merged: List[Dict[str, Any]] = []
        seen: set = set()
        for source in (visual_package or {}, visual_plan or {}):
            for field in (
                "unfilled_visual_needs",
                "unfilled_visual_opportunities",
            ):
                for row in source.get(field) or []:
                    if isinstance(row, dict):
                        key = (
                            str(row.get("section_id") or ""),
                            str(row.get("reason") or "")[:160],
                        )
                    else:
                        key = ("", str(row)[:160])
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(
                        row
                        if isinstance(row, dict)
                        else {"reason": str(row)}
                    )
        return merged

    def _record_supplementary_closure_gap(
        self, feedback_sections: Sequence[str]
    ) -> None:
        """Record that the closure stage is declared but not wired, and why.

        ``section_supplementary_closure`` is in ``STAGES`` and has a UI row, but
        ``run_section_supplementary_closure`` has no production call site: it
        requires a ``blueprint_quality_probe.v1`` report and nothing in the
        harness emits one.  Measured against the real claim ledger (806 claims),
        the fields its gap selector keys on -- ``closure_disposition``,
        ``evidence_binding_status``, ``missing_evidence_components`` -- are
        empty on every row, and ``importance`` carries ``load_bearing`` /
        ``supporting`` where the selector wants ``high``.  Wiring it would mean
        inventing the evidence-gap verdict the module exists to compute.

        So say so, once per run, in the place an operator already looks.  A
        stage that is silently absent reads as "did not apply"; this reads as
        "not built yet", which is what it is.  The status is inert for the
        delivery gate -- ``delivery_contract`` reads only ``research_plan`` --
        it is not in the UI's completed set, and ``status_label`` renders it.

        Deliberately not ``_set_stage``: that would move ``current_stage`` onto
        a stage the run never enters and emit a stage_finished event for work
        that never started.  This is a declaration about the build, not a step
        of the run, so it writes the row and persists, nothing more.
        """

        self.state.setdefault("stages", {})["section_supplementary_closure"] = {
            "status": "not_integrated",
            "updated_at": _now(),
            "reason": "no producer for blueprint_quality_probe.v1",
            "blocked_on": "blueprint_quality_probe.v1",
            "entry_point": (
                "optomind_research.runtime.section_supplementary_orchestrator"
                ":run_section_supplementary_closure"
            ),
            "coverage_feedback_sections": list(feedback_sections),
        }
        self._save_state()

    def _record_worker_stage(self, stage: str, result: Any) -> None:
        self._record_stage(
            stage,
            result.status.value,
            float(getattr(result, "estimated_cost_cny", 0.0) or 0.0),
            int(getattr(result, "total_input_tokens", 0) or 0),
            int(getattr(result, "total_output_tokens", 0) or 0),
            {"stop_reason": result.stop_reason},
        )

    def _record_stage(
        self,
        stage: str,
        status: str,
        cost_cny: float,
        input_tokens: int,
        output_tokens: int,
        details: Dict[str, Any],
        *,
        wall_time_seconds: float | None = None,
    ) -> None:
        """Close a stage in both ledgers.

        ``wall_time_seconds`` is for stages that run *inside* an adapter, past
        the reach of this orchestrator's own timer: the caller measured the
        duration itself and passes it in rather than letting ``finish_stage``
        fall back to 0.0.  See the publication-mainline metrics loop.
        """

        stage_duration = self.observability.finish_stage(
            stage,
            status,
            estimated_cost_cny=cost_cny,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            wall_time_seconds=wall_time_seconds,
            **details,
        )
        previous = dict(self.stage_costs.get(stage, {}))
        cumulative_cost_cny = round(
            max(self._stage_cost_cny(previous), float(cost_cny)),
            6,
        )
        self.stage_costs[stage] = {
            "cost_cny": cumulative_cost_cny,
            "estimated_cost_cny": cumulative_cost_cny,
            "input_tokens": max(
                self._stage_token_count(previous, "input_tokens"),
                int(input_tokens),
            ),
            "output_tokens": max(
                self._stage_token_count(previous, "output_tokens"),
                int(output_tokens),
            ),
            "wall_time_seconds": round(max(
                float(previous.get("wall_time_seconds", 0.0) or 0.0),
                float(stage_duration),
            ), 3),
            "last_attempt_cost_cny": round(float(cost_cny), 6),
            "last_attempt_input_tokens": int(input_tokens),
            "last_attempt_output_tokens": int(output_tokens),
            "last_attempt_wall_time_seconds": round(stage_duration, 3),
        }
        attempt_model_calls = int(details.get("model_call_count", 0) or 0)
        if previous.get("model_call_count") is not None or attempt_model_calls:
            self.stage_costs[stage]["model_call_count"] = int(
                max(
                    int(previous.get("model_call_count", 0) or 0),
                    attempt_model_calls,
                )
            )
        self.state.setdefault("stages", {})[stage] = {
            "status": status,
            "updated_at": _now(),
            "wall_time_seconds": round(stage_duration, 3),
            **details,
        }
        self._save_cost()
        self._save_state()
        self.observability.snapshot(
            status="running",
            current_stage=stage,
            stage_costs=self.stage_costs,
            harness_state=self.state,
        )

    def finalize_upstream_stop(
        self,
        *,
        status: str,
        stage: str,
        stage_status: str,
        stage_metrics: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Close an upstream gate with the canonical terminal receipts.

        Upstream entry stages can intentionally stop before ``run()`` creates
        the normal orchestrator checkpoints.  They still need the same state,
        cost, metrics, event, and content-package contract as every later
        terminal path so a new process can audit and resume the run safely.
        """

        status = str(status or "failed")
        stage = str(stage or "orchestrator").strip() or "orchestrator"
        stage_status = str(stage_status or "failed_closed")
        previous = dict(self.stage_costs.get(stage, {}))
        cost_cny = round(
            max(
                self._stage_cost_cny(previous),
                self._stage_cost_cny(stage_metrics),
            ),
            6,
        )
        input_tokens = max(
            self._stage_token_count(previous, "input_tokens"),
            self._stage_token_count(stage_metrics, "input_tokens"),
        )
        output_tokens = max(
            self._stage_token_count(previous, "output_tokens"),
            self._stage_token_count(stage_metrics, "output_tokens"),
        )
        model_call_count = 0
        if stage == "query_planner":
            model_call_count = max(
                self._query_planner_model_call_count(previous),
                self._query_planner_model_call_count(stage_metrics),
            )
        else:
            for source in (previous, stage_metrics):
                for key in ("model_call_count", "model_calls", "call_count"):
                    if source.get(key) is not None:
                        try:
                            model_call_count = max(
                                model_call_count,
                                int(source.get(key) or 0),
                            )
                        except (TypeError, ValueError):
                            pass
                        break
        planner_generation_status = str(
            stage_metrics.get("planner_generation_status")
            or stage_metrics.get("status")
            or ""
        )
        self.stage_costs[stage] = {
            **previous,
            "cost_cny": cost_cny,
            "estimated_cost_cny": cost_cny,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "wall_time_seconds": max(
                float(previous.get("wall_time_seconds", 0.0) or 0.0),
                float(stage_metrics.get("wall_time_seconds", 0.0) or 0.0),
            ),
            "model_call_count": model_call_count,
            "source": "upstream_stage_receipt",
        }
        if planner_generation_status:
            self.stage_costs[stage][
                "planner_generation_status"
            ] = planner_generation_status
        self.state["current_stage"] = stage
        self.state["status"] = "running"
        self.state.setdefault("stages", {})[stage] = {
            "status": stage_status,
            "updated_at": _now(),
            "terminal_status": status,
            "planner_generation_status": planner_generation_status,
            "execution_ready": bool(stage_metrics.get("execution_ready")),
        }
        metrics = self._reconcile_terminal_artifacts(
            status=status,
            current_stage=stage,
        )

        package = _read_json(self.package_path)
        package.setdefault(
            "schema_version", "research_harness.content_package.v1"
        )
        package.update(
            {
                "run_id": self.run_id,
                "status": status,
                "completed_stage": stage,
                "cost_cny": self._total_cost_cny(),
                "total_cost_cny": self._total_cost_cny(),
                "total_input_tokens": self._total_stage_tokens(
                    "input_tokens"
                ),
                "total_output_tokens": self._total_stage_tokens(
                    "output_tokens"
                ),
                "stage_status": self.state.get("stages", {}),
                "terminal_reconciliation_id": self.state.get(
                    "terminal_reconciliation_id", ""
                ),
                "active_wall_time_seconds": metrics.get(
                    "active_wall_time_seconds", 0.0
                ),
                "operations_summary": metrics.get("operations", {}),
            }
        )
        package.setdefault("artifacts", {}).update(
            {
                "harness_state": str(self.state_path),
                "cost_ledger": str(self.cost_path),
                "run_metrics": str(self.observability.metrics_path),
                "run_timeline": str(self.observability.events_path),
                "log_index": str(self.observability.log_index_path),
                "operations_report": str(self.observability.report_path),
            }
        )
        atomic_write_json(self.package_path, package)
        return metrics

    def _total_cost_cny(self) -> float:
        return round(
            sum(
                self._stage_cost_cny(stage)
                for stage in self.stage_costs.values()
                if isinstance(stage, Mapping)
            ),
            6,
        )

    def _total_stage_tokens(self, key: str) -> int:
        return sum(
            self._stage_token_count(stage, key)
            for stage in self.stage_costs.values()
            if isinstance(stage, Mapping)
        )

    def _canonical_model_call_count(self) -> int:
        """Count leaf calls and upstream calls once per canonical stage."""

        logged_counts: Dict[str, int] = {}
        try:
            log_index = self.observability._build_log_index()
            logged_counts = {
                str(stage): int(count or 0)
                for stage, count in (
                    log_index.get("stage_model_call_counts", {}) or {}
                ).items()
            }
        except Exception:
            logger.debug("Unable to read model call counts for cost ledger", exc_info=True)
        stage_names = {
            str(stage).strip()
            for stage in self.state.get("canonical_stages", [])
            if str(stage).strip()
        }
        stage_names.update(str(stage) for stage in self.stage_costs)
        stage_names.update(str(stage) for stage in logged_counts)
        total = 0
        for stage in stage_names:
            value = self.stage_costs.get(stage, {})
            if not isinstance(value, Mapping):
                value = {}
            explicit = None
            for key in ("model_call_count", "model_calls", "call_count"):
                if value.get(key) is not None:
                    try:
                        explicit = max(0, int(value.get(key) or 0))
                    except (TypeError, ValueError):
                        explicit = 0
                    break
            if explicit is None and stage == "query_planner":
                explicit = self._query_planner_model_call_count(value)
            total += max(logged_counts.get(stage, 0), explicit or 0)
        return total

    def _save_cost(self) -> None:
        canonical_model_calls = self._canonical_model_call_count()
        payload = {
            "schema_version": "research_harness.cost.v1",
            "run_id": self.run_id,
            "status": str(self.state.get("status") or "running"),
            "current_stage": self._current_stage("orchestrator"),
            "completed_stage": (
                self._current_stage("orchestrator")
                if self.state.get("status") != "running"
                else ""
            ),
            "error_count": int(self.state.get("error_count", 0) or 0),
            "terminal_reconciliation_id": str(
                self.state.get("terminal_reconciliation_id") or ""
            ),
            "global_cost_budget_cny": self.config.global_cost_budget_cny,
            "cost_cny": self._total_cost_cny(),
            "estimated_cost_cny": self._total_cost_cny(),
            "model_call_count": canonical_model_calls,
            "remaining_budget_cny": round(
                self.config.global_cost_budget_cny - self._total_cost_cny(),
                6,
            ),
            "billing_currency": "CNY",
            "canonical_totals": {
                "model_calls": canonical_model_calls,
                "input_tokens": self._total_stage_tokens("input_tokens"),
                "output_tokens": self._total_stage_tokens("output_tokens"),
                "total_tokens": self._total_stage_tokens("input_tokens")
                + self._total_stage_tokens("output_tokens"),
                "cost_cny": self._total_cost_cny(),
                "billing_currency": "CNY",
                "stages": list(
                    dict.fromkeys(
                        [
                            *self.state.get("canonical_stages", []),
                            *self.stage_costs.keys(),
                        ]
                    )
                ),
            },
            "stages": {
                stage_name: {
                    **dict(stage_value),
                    "cost_cny": self._stage_cost_cny(stage_value),
                    "estimated_cost_cny": self._stage_cost_cny(stage_value),
                }
                for stage_name, stage_value in self.stage_costs.items()
                if isinstance(stage_value, Mapping)
            },
            "updated_at": _now(),
        }
        atomic_write_json(self.cost_path, payload)

    def _save_state(self) -> None:
        self.state["updated_at"] = _now()
        atomic_write_json(self.state_path, self.state)

    @staticmethod
    def _content_quality_summary(
        final_review: Optional[Path],
        visual_plan: Optional[Path],
        blueprint: Dict[str, Any],
    ) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "planned_sections": len(blueprint.get("sections", [])),
            "review_word_count": 0,
            "existing_visual_placements": 0,
            "conceptual_visual_requests": 0,
            "unfilled_visual_needs": 0,
        }
        if final_review and final_review.exists():
            text = final_review.read_text(encoding="utf-8")
            summary["review_word_count"] = len(text.split())
            summary["review_section_heading_count"] = sum(
                1 for line in text.splitlines() if line.startswith("## ")
            )
        if visual_plan and visual_plan.exists():
            value = _read_json(visual_plan)
            summary["existing_visual_placements"] = len(
                value.get("placements", [])
            )
            summary["conceptual_visual_requests"] = len(
                value.get("conceptual_figure_requests", [])
            )
            summary["unfilled_visual_needs"] = len(
                value.get("unfilled_visual_needs", [])
            )
        return summary

    @staticmethod
    def _review_body_validation(
        path: Optional[Path],
    ) -> Dict[str, Any]:
        """Validate the review body before any package can claim completion."""

        if not path:
            return {
                "status": "failed",
                "reason": "missing_review_body",
                "path": "",
            }
        candidate = Path(path)
        if not candidate.is_file():
            return {
                "status": "failed",
                "reason": "missing_review_body",
                "path": str(candidate),
            }
        try:
            body = candidate.read_text(
                encoding="utf-8",
            )
        except (OSError, UnicodeDecodeError) as exc:
            return {
                "status": "failed",
                "reason": f"review_body_unreadable:{type(exc).__name__}",
                "path": str(candidate),
            }
        if not body.strip():
            return {
                "status": "failed",
                "reason": "empty_review_body",
                "path": str(candidate),
            }
        return {
            "status": "passed",
            "reason": "non_empty_review_body",
            "path": str(candidate),
            "character_count": len(body.strip()),
        }

    @classmethod
    def _validated_review_path(
        cls,
        path: Optional[Path],
    ) -> Optional[Path]:
        return (
            Path(path)
            if cls._review_body_validation(path).get("status") == "passed"
            else None
        )

    def _current_stage(self, fallback: str = "orchestrator") -> str:
        value = str(self.state.get("current_stage") or "").strip()
        return fallback if value in {"", "initializing"} else value

    def _truthful_terminal_stage(self, requested: Any) -> str:
        value = str(requested or "").strip()
        if value in {"", "initializing"}:
            return self._current_stage("orchestrator")
        return value

    def _observed_error_count(self) -> int:
        """Return the same durable error count used by run metrics."""

        try:
            index = self.observability._build_log_index()
            return int(len(index.get("errors", [])))
        except Exception as exc:
            logger.warning(
                "Unable to rebuild the harness log index while closing the run: %s",
                exc,
            )
            count = 0
            if self.observability.events_path.exists():
                try:
                    for line in self.observability.events_path.read_text(
                        encoding="utf-8",
                    ).splitlines():
                        try:
                            event = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(event, dict) and event.get("event") == "error":
                            count += 1
                except (OSError, UnicodeDecodeError):
                    pass
            return count

    def _record_terminal_exception(
        self,
        *,
        stage: str,
        error: Exception,
    ) -> None:
        """Record an exception through the structured run event stream."""

        error_type = type(error).__name__
        detail = str(error)[:1000]
        try:
            self.observability.fail(
                stage=stage,
                error_type=error_type,
                detail=detail,
            )
        except Exception:
            logger.exception("Failed to emit the harness run_error event")
        try:
            # HarnessObservability indexes ``error`` events into the durable
            # error_count.  Keep ``run_error`` above for compatibility with
            # existing run timelines while making the count auditable.
            self.observability.emit(
                "error",
                stage=stage,
                error_type=error_type,
                detail=detail,
            )
        except Exception:
            logger.exception("Failed to emit the harness error event")
            try:
                append_jsonl(
                    self.observability.events_path,
                    _redact(
                        {
                            "timestamp": _now(),
                            "run_id": self.run_id,
                            "event": "error",
                            "stage": stage,
                            "error_type": error_type,
                            "detail": detail,
                        }
                    ),
                )
            except Exception:
                logger.exception("Failed to append the fallback error event")

    def _emit_terminal_event(
        self,
        *,
        status: str,
        current_stage: str,
        error_count: int,
        reconciliation_id: str,
    ) -> None:
        current_stage = self._truthful_terminal_stage(current_stage)
        elapsed = 0.0
        elapsed_fn = getattr(
            self.observability,
            "_current_invocation_elapsed",
            None,
        )
        if callable(elapsed_fn):
            try:
                elapsed = float(elapsed_fn())
            except Exception:
                elapsed = 0.0
        record = {
            "status": status,
            "current_stage": current_stage,
            "completed_stage": current_stage,
            "error_count": int(error_count),
            "reconciliation_id": reconciliation_id,
            "invocation_wall_time_seconds": round(elapsed, 3),
        }
        try:
            self.observability.emit("run_finished", **record)
            return
        except Exception:
            logger.exception("Failed to emit the terminal run_finished event")
        try:
            append_jsonl(
                self.observability.events_path,
                _redact(
                    {
                        "timestamp": _now(),
                        "run_id": self.run_id,
                        "event": "run_finished",
                        **record,
                    }
                ),
            )
        except Exception:
            logger.exception("Failed to append the fallback run_finished event")

    def _fallback_terminal_metrics(
        self,
        *,
        status: str,
        current_stage: str,
        error_count: int,
        reconciliation_id: str,
    ) -> Dict[str, Any]:
        """Write a minimal final metrics artifact if observer finalization fails."""

        current_stage = self._truthful_terminal_stage(current_stage)
        prior = _read_json(self.observability.metrics_path)
        prior_operations = dict(prior.get("operations", {}) or {})
        event_log_count = int(prior_operations.get("event_log_count", 0) or 0)
        if self.observability.events_path.exists():
            event_log_count = max(1, event_log_count)
        total_input = self._total_stage_tokens("input_tokens")
        total_output = self._total_stage_tokens("output_tokens")
        prior_canonical = dict(prior.get("canonical_totals", {}) or {})
        canonical_stages = sorted(
            set(
                str(stage).strip()
                for stage in self.state.get("canonical_stages", [])
                if str(stage).strip()
            )
            | set(self.stage_costs)
            | set(prior_canonical.get("stages", []) or [])
        )
        canonical_totals = {
            "model_calls": int(
                prior_canonical.get(
                    "model_calls",
                    prior_operations.get("model_call_count", 0),
                )
                or 0
            ),
            "tool_calls": int(
                prior_canonical.get(
                    "tool_calls",
                    prior_operations.get("tool_call_count", 0),
                )
                or 0
            ),
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "cost_cny": self._total_cost_cny(),
            "billing_currency": "CNY",
            "stages": canonical_stages,
        }
        metrics = {
            "schema_version": "research_harness.metrics.v1",
            "run_id": self.run_id,
            "status": status,
            "current_stage": current_stage,
            "completed_stage": current_stage,
            "started_at": str(
                prior.get("started_at")
                or self.state.get("created_at")
                or _now()
            ),
            "completed_at": _now(),
            "active_wall_time_seconds": float(
                prior.get("active_wall_time_seconds", 0.0) or 0.0
            ),
            "committed_active_wall_time_seconds": float(
                prior.get("committed_active_wall_time_seconds", 0.0) or 0.0
            ),
            "current_invocation_wall_time_seconds": float(
                prior.get("current_invocation_wall_time_seconds", 0.0) or 0.0
            ),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "cost_cny": self._total_cost_cny(),
            "estimated_cost_cny": self._total_cost_cny(),
            "billing_currency": "CNY",
            "canonical_totals": canonical_totals,
            "stage_metrics": dict(prior.get("stage_metrics", {}) or {}),
            "operations": {
                **prior_operations,
                "event_log_count": event_log_count,
                "error_count": int(error_count),
            },
            "artifacts": {
                "timeline": str(self.observability.events_path),
                "log_index": str(self.observability.log_index_path),
                "human_report": str(self.observability.report_path),
            },
            "tool_call_reconciliation": dict(
                prior.get("tool_call_reconciliation", {}) or {}
            ),
            "terminal_reconciliation_id": reconciliation_id,
            "generated_at": _now(),
        }
        atomic_write_json(self.observability.metrics_path, metrics)
        return metrics

    def _reconcile_terminal_artifacts(
        self,
        *,
        status: str,
        current_stage: str,
        terminal_error: Optional[Exception] = None,
    ) -> Dict[str, Any]:
        """Atomically close all run-level artifacts with one terminal receipt."""

        status = str(status or "failed")
        if status == "running":
            status = "failed"
        current_stage = self._truthful_terminal_stage(current_stage)
        reconciliation_id = "term_" + uuid.uuid4().hex
        error_count = self._observed_error_count()
        self.state["status"] = status
        self.state["current_stage"] = current_stage
        self.state["error_count"] = int(error_count)
        self.state["terminal_reconciliation_id"] = reconciliation_id
        if terminal_error is not None:
            self.state["terminal_error"] = {
                "type": type(terminal_error).__name__,
                "detail": str(terminal_error)[:1000],
            }
        self._save_cost()
        self._save_state()

        self._emit_terminal_event(
            status=status,
            current_stage=current_stage,
            error_count=error_count,
            reconciliation_id=reconciliation_id,
        )
        try:
            metrics = self.observability.snapshot(
                status=status,
                current_stage=current_stage,
                stage_costs=self.stage_costs,
                harness_state=self.state,
                final=True,
            )
        except Exception:
            logger.exception("Structured observability snapshot failed at terminal close")
            metrics = self._fallback_terminal_metrics(
                status=status,
                current_stage=current_stage,
                error_count=error_count,
                reconciliation_id=reconciliation_id,
            )
        metrics["status"] = status
        metrics["current_stage"] = current_stage
        metrics["completed_stage"] = current_stage
        metrics["terminal_reconciliation_id"] = reconciliation_id
        metrics.setdefault("operations", {})["error_count"] = int(error_count)
        atomic_write_json(self.observability.metrics_path, metrics)
        # Keep the state receipt after metrics persistence so state, metrics,
        # cost, and the final event all expose the same terminal identity.
        self._save_state()
        return metrics

    def _recover_terminal(
        self,
        *,
        status: str,
        completed_stage: str,
        final_review: Optional[Path],
        visual_plan: Optional[Path],
        research_plan: Optional[Path],
        final_visual_package: Optional[Path],
        error: Exception,
    ) -> ReviewHarnessResult:
        """Fail closed without discarding resumable stage artifacts."""

        stage = self._truthful_terminal_stage(completed_stage)
        self._record_terminal_exception(stage=stage, error=error)
        stage_record = self.state.setdefault("stages", {}).get(stage)
        if isinstance(stage_record, dict) and stage_record.get("status") == "running":
            stage_record.update(
                {
                    "status": "failed",
                    "updated_at": _now(),
                    "error": f"{type(error).__name__}:{error}",
                }
            )
        try:
            metrics = self._reconcile_terminal_artifacts(
                status="failed",
                current_stage=stage,
                terminal_error=error,
            )
        except Exception as reconcile_error:
            logger.exception("Terminal artifact reconciliation failed")
            self.state["status"] = "failed"
            self.state["current_stage"] = stage
            self.state["error_count"] = self._observed_error_count()
            try:
                self._save_state()
            except Exception:
                logger.exception("Failed to write fail-closed harness state")
            try:
                self._save_cost()
            except Exception:
                logger.exception("Failed to write fail-closed harness cost")
            try:
                metrics = self._fallback_terminal_metrics(
                    status="failed",
                    current_stage=stage,
                    error_count=int(self.state.get("error_count", 0) or 0),
                    reconciliation_id=str(
                        self.state.get("terminal_reconciliation_id")
                        or "terminal_fallback"
                    ),
                )
            except Exception:
                logger.exception("Failed to write fail-closed harness metrics")
                metrics = {
                    "status": "failed",
                    "current_stage": stage,
                    "completed_stage": stage,
                    "cost_cny": self._total_cost_cny(),
                    "estimated_cost_cny": self._total_cost_cny(),
                    "operations": {
                        "error_count": int(
                            self.state.get("error_count", 0) or 0
                        )
                    },
                    "terminal_error": str(reconcile_error),
                }

        blueprint = _read_json(
            self.work_dir / "review_lead" / "REVIEW_BLUEPRINT.json"
        )
        valid_review = self._validated_review_path(final_review)
        package = {
            "schema_version": "research_harness.content_package.v1",
            "run_id": self.run_id,
            "status": "failed",
            "completed_stage": stage,
            "query_plan_path": str(self.config.query_plan_path),
            "base_kb_sqlite": str(self.config.base_kb_sqlite),
            "final_review_path": str(valid_review) if valid_review else "",
            "visual_editorial_plan_path": (
                str(visual_plan) if visual_plan and Path(visual_plan).exists() else ""
            ),
            "final_visual_package_path": (
                str(final_visual_package)
                if final_visual_package and Path(final_visual_package).exists()
                else ""
            ),
            "research_plan_path": (
                str(research_plan)
                if research_plan and Path(research_plan).exists()
                else ""
            ),
            "review_body_validation": self._review_body_validation(final_review),
            "cost_cny": self._total_cost_cny(),
            "total_cost_cny": self._total_cost_cny(),
            "total_input_tokens": self._total_stage_tokens("input_tokens"),
            "total_output_tokens": self._total_stage_tokens("output_tokens"),
            "stage_status": self.state.get("stages", {}),
            "quality_summary": self._content_quality_summary(
                valid_review,
                visual_plan,
                blueprint,
            ),
            "quality_gate": {
                "status": "failed",
                "blocking_issues": ["unexpected_orchestrator_exception"],
                "error_type": type(error).__name__,
            },
            "terminal_reconciliation_id": self.state.get(
                "terminal_reconciliation_id", ""
            ),
            "active_wall_time_seconds": metrics.get(
                "active_wall_time_seconds", 0.0
            ),
            "operations_summary": metrics.get("operations", {}),
            "artifacts": {
                "harness_state": str(self.state_path),
                "cost_ledger": str(self.cost_path),
                "run_metrics": str(self.observability.metrics_path),
                "run_timeline": str(self.observability.events_path),
                "log_index": str(self.observability.log_index_path),
                "operations_report": str(self.observability.report_path),
            },
            "scope_note": (
                "Terminal failure recorded. Existing stage artifacts remain "
                "available for resumable recovery."
            ),
            "created_at": _now(),
        }
        atomic_write_json(self.package_path, package)
        return ReviewHarnessResult(
            run_id=self.run_id,
            status="failed",
            completed_stage=stage,
            total_cost_cny=self._total_cost_cny(),
            total_input_tokens=package["total_input_tokens"],
            total_output_tokens=package["total_output_tokens"],
            work_dir=self.work_dir,
            final_review_path=valid_review,
            visual_plan_path=(
                Path(visual_plan)
                if visual_plan and Path(visual_plan).exists()
                else None
            ),
            final_visual_package_path=(
                Path(final_visual_package)
                if final_visual_package and Path(final_visual_package).exists()
                else None
            ),
            research_plan_path=(
                Path(research_plan)
                if research_plan and Path(research_plan).exists()
                else None
            ),
            latex_pdf_path=None,
            latex_source_archive_path=None,
            chinese_review_path=None,
            chinese_latex_pdf_path=None,
            chinese_latex_source_archive_path=None,
            package_path=self.package_path,
        )

    def _finish(
        self,
        status: str,
        completed_stage: str,
        final_review: Optional[Path],
        visual_plan: Optional[Path],
        research_plan: Optional[Path] = None,
        final_visual_package: Optional[Path] = None,
    ) -> ReviewHarnessResult:
        """Finalize normally, or fail closed if finalization itself breaks."""

        # Drain all background central-cache write-backs before returning.
        # Each thread gets up to 120s; we do not abort the pipeline if a
        # write-back times out — the pending_recovery entry in state is the
        # operator signal to replay it later.
        for _wt in getattr(self, "_writeback_threads", []):
            _wt.join(timeout=120)

        # P0-1.5: settle any gate whose timeout elapsed after the last
        # registration.  request_decision only sweeps incidentally, so a run
        # that ends right after its final registration would otherwise leave
        # a pending file and an unresolved ledger row behind.
        try:
            expire_due_decisions(self.work_dir)
        except Exception as exc:
            logger.warning(
                "final human-gate sweep failed: %s: %s",
                type(exc).__name__,
                exc,
            )

        completed_stage = self._truthful_terminal_stage(completed_stage)
        try:
            return self._finish_impl(
                status,
                completed_stage,
                final_review,
                visual_plan,
                research_plan,
                final_visual_package,
            )
        except Exception as exc:
            # 收尾路径的编程错误与业务上的 fail-closed 是两回事：都必须 fail
            # closed，但前者是缺陷，必须留下可断言、可告警的痕迹。
            # stage 用新名字 "finalization"，不复用已有阶段名，
            # 避免污染 T-04 的墙钟归因表。
            # 留痕是「更好」，四方一致的终态是「必须」。emit 会走
            # append_jsonl 做真实磁盘 I/O（mkdir + open + write，本身不设兜
            # 底），磁盘满 / 权限 / 文件被占用都会抛。若让它抛出这个
            # except，_recover_terminal 就永远不会执行，状态停在 running，
            # run 变成不可恢复——正是 T-01 禁止的那个回归，只是走了另一条
            # 路径进来。所以留痕失败只能被吞掉，绝不允许挡住收尾。
            try:
                self.observability.fail(
                    stage="finalization",
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
            except Exception:
                pass
            return self._recover_terminal(
                status="failed",
                completed_stage=completed_stage,
                final_review=final_review,
                visual_plan=visual_plan,
                research_plan=research_plan,
                final_visual_package=final_visual_package,
                error=exc,
            )

    def _finish_impl(
        self,
        status: str,
        completed_stage: str,
        final_review: Optional[Path],
        visual_plan: Optional[Path],
        research_plan: Optional[Path] = None,
        final_visual_package: Optional[Path] = None,
    ) -> ReviewHarnessResult:
        status = str(status or "failed")
        if status == "running":
            status = "failed"
        completed_stage = self._truthful_terminal_stage(completed_stage)
        review_body_validation = self._review_body_validation(final_review)
        final_review = self._validated_review_path(final_review)
        if (
            review_body_validation.get("status") != "passed"
            and status == "completed"
        ):
            status = "failed"
        self.observability.start_stage(
            "packaging",
            requested_terminal_status=status,
            completed_stage=completed_stage,
        )
        blueprint_path = self.work_dir / "review_lead" / "REVIEW_BLUEPRINT.json"
        blueprint = _read_json(blueprint_path)
        metadata_catalog_path = ""
        metadata_audit_path = ""
        metadata_resolution_summary: dict[str, Any] = {}
        if (
            final_review
            and final_review.exists()
            and (
                self.config.produce_latex_publication
                or self.config.produce_chinese_publication
            )
        ):
            try:
                from .publication_metadata_resolver import (
                    ResolverOptions,
                    build_publication_metadata_catalog,
                    make_default_crossref_provider,
                    make_default_openalex_provider,
                    make_default_s2_provider,
                )

                handoff_path = (
                    self.work_dir
                    / "publication_mainline"
                    / "handoff"
                    / "UNIFIED_MANUSCRIPT_HANDOFF.json"
                )
                if not handoff_path.is_file():
                    raise FileNotFoundError(handoff_path)
                metadata_dir = self.work_dir / "publication" / "metadata"
                allow_online = bool(
                    self.config.latex_enrich_crossref
                    and self.config.publication_metadata_online
                )
                s2_cache_candidates = sorted(
                    self.work_dir.rglob("s2_online_cache.sqlite")
                )
                resolver_options = ResolverOptions(
                    allow_openalex=allow_online,
                    allow_crossref=allow_online,
                    allow_s2=allow_online,
                    max_provider_calls=120 if allow_online else 0,
                    openalex_provider=(
                        make_default_openalex_provider()
                        if allow_online
                        else None
                    ),
                    crossref_provider=(
                        make_default_crossref_provider()
                        if allow_online
                        else None
                    ),
                    s2_provider=(
                        make_default_s2_provider() if allow_online else None
                    ),
                )
                metadata_resolution_summary = (
                    build_publication_metadata_catalog(
                        staged_manuscript_path=final_review,
                        handoff_path=handoff_path,
                        project_root=self.work_dir,
                        output_dir=metadata_dir,
                        options=resolver_options,
                        staged_context_path=(
                            self.work_dir
                            / "publication_mainline"
                            / "staged_context"
                            / "STAGED_GLOBAL_INPUTS.json"
                        ),
                        material_cache_dirs=(
                            [self.config.long_term_material_cache_root]
                            if self.config.long_term_material_cache_root
                            else []
                        ),
                        s2_cache_path=(
                            s2_cache_candidates[-1]
                            if s2_cache_candidates
                            else None
                        ),
                    )
                )
                metadata_catalog_path = str(
                    metadata_resolution_summary["output_paths"]["catalog"]
                )
                metadata_audit_path = str(
                    metadata_resolution_summary["output_paths"]["audit"]
                )
            except Exception as exc:
                metadata_resolution_summary = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}:{exc}",
                }
                self.state["publication_metadata_resolution"] = (
                    metadata_resolution_summary
                )
        quality_report: Dict[str, Any] = {}
        if final_review:
            authoring_dir = self._prior_stage_work_dir("authoring_revision")
            final_citation_map_path = self.work_dir / "FINAL_CITATION_MAP.json"
            build_final_citation_map(
                markdown_path=final_review,
                output_path=final_citation_map_path,
                intermediate_map_path=(
                    authoring_dir / "FULL_REVIEW_CITATION_MAP.json"
                    if authoring_dir
                    else None
                ),
                metadata_catalog_path=(
                    Path(metadata_catalog_path)
                    if metadata_catalog_path
                    else None
                ),
            )
            quality_report = evaluate_review_content(
                final_review_path=final_review,
                blueprint=blueprint,
                visual_plan_path=(
                    final_visual_package or visual_plan
                ),
                citation_map_path=final_citation_map_path,
                output_dir=self.work_dir,
                research_plan_path=research_plan,
            )
            if (
                status == "completed"
                and _quality_report_hard_blocks(quality_report)
            ):
                status = "awaiting_human_review"
                # The gate is skipped here on purpose -- a hard blocker is not
                # something a human is asked to wave through -- but skipping
                # it silently left no trace that it had been considered.
                self._set_stage(
                    "quality_review_gate",
                    "blocked_hard_quality",
                    blocking_issues=list(
                        quality_report.get("blocking_issues") or []
                    )[:8],
                )
            elif (
                status == "completed"
                and str(quality_report.get("status") or "")
                == "needs_attention"
            ):
                # P2 (round 3): closure loop -- a needs_attention verdict is
                # either remediated or explicitly accepted.  The bounded gate
                # gives a human the window; the timeout auto-accept IS the
                # recorded explicit acceptance (stage detail below).
                if self._resolve_human_gate(
                    stage="quality_review_gate",
                    kind="quality_attention_acceptance",
                    subject_id=f"{self.run_id}:quality_attention",
                    context={
                        "warnings": list(
                            quality_report.get("warnings") or []
                        ),
                        "remediation_hints": dict(
                            quality_report.get("remediation_hints") or {}
                        ),
                    },
                    original_status="needs_attention",
                ):
                    # Persist it.  The direct state["stages"][...] assignment
                    # this replaces never reached _save_state, so the
                    # acceptance flag lived in memory only and was absent from
                    # HARNESS_STATE.json -- the one file an auditor reads to
                    # find out who accepted what.
                    gate_row = (
                        self.state.get("stages", {}).get("quality_review_gate")
                        or {}
                    )
                    self._annotate_stage(
                        "quality_review_gate",
                        str(gate_row.get("status") or "completed"),
                        attention_accepted=True,
                    )
            elif status == "completed":
                # Clean verdict: nothing to decide.  Record that, so a stage
                # declared in STAGES stops reading as "never ran" on every
                # successful run.
                self._set_stage(
                    "quality_review_gate",
                    "not_required",
                    quality_status=str(quality_report.get("status") or ""),
                )
        else:
            quality_report = {
                "schema_version": "research_harness.quality_report.v1",
                "status": "failed",
                "blocking_issues": [
                    str(
                        review_body_validation.get("reason")
                        or "missing_review_body"
                    )
                ],
                "metrics": {
                    "review_body_validation": review_body_validation,
                },
            }
            atomic_write_json(
                self.work_dir / "REVIEW_HARNESS_QUALITY_REPORT.json",
                quality_report,
            )
        self.state["status"] = status
        self.state["current_stage"] = completed_stage
        self._save_cost()
        self._save_state()
        package = {
            "schema_version": "research_harness.content_package.v1",
            "run_id": self.run_id,
            "status": status,
            "completed_stage": completed_stage,
            "query_plan_path": str(self.config.query_plan_path),
            "base_kb_sqlite": str(self.config.base_kb_sqlite),
            "topic_identity_path": str(
                self.work_dir / "TOPIC_IDENTITY.json"
            ),
            "topic_fingerprint": str(
                blueprint.get("topic_identity", {}).get("fingerprint", "")
            ),
            "final_review_path": str(final_review) if final_review else "",
            "metadata_catalog_path": metadata_catalog_path,
            "metadata_audit_path": metadata_audit_path,
            "review_body_validation": review_body_validation,
            "visual_editorial_plan_path": (
                str(visual_plan) if visual_plan else ""
            ),
            "final_visual_package_path": (
                str(final_visual_package)
                if final_visual_package
                else ""
            ),
            "research_plan_path": (
                str(research_plan) if research_plan else ""
            ),
            "cost_cny": self._total_cost_cny(),
            "total_cost_cny": self._total_cost_cny(),
            "total_input_tokens": self._total_stage_tokens("input_tokens"),
            "total_output_tokens": self._total_stage_tokens("output_tokens"),
            "stage_status": self.state.get("stages", {}),
            "quality_summary": self._content_quality_summary(
                final_review,
                visual_plan,
                blueprint,
            ),
            "quality_gate": quality_report,
            "artifacts": {
                "preflight": str(self.work_dir / "COST_PREFLIGHT.json"),
                "harness_state": str(self.state_path),
                "cost_ledger": str(self.cost_path),
                "quality_report_json": str(
                    self.work_dir / "REVIEW_HARNESS_QUALITY_REPORT.json"
                )
                if quality_report
                else "",
                "quality_report_markdown": str(
                    self.work_dir / "REVIEW_HARNESS_QUALITY_REPORT.md"
                )
                if (
                    quality_report
                    and (
                        self.work_dir / "REVIEW_HARNESS_QUALITY_REPORT.md"
                    ).exists()
                )
                else "",
                "review_blueprint": (
                    str(blueprint_path) if blueprint_path.exists() else ""
                ),
                "topic_identity": str(
                    self.work_dir / "TOPIC_IDENTITY.json"
                ),
                "topic_scoped_kb_manifest": str(
                    self.work_dir
                    / "topic_scoped_kb"
                    / "KB_MANIFEST.json"
                ),
                "s2_bootstrap_report": str(
                    self.work_dir
                    / "s2_literature_intelligence"
                    / "S2_BOOTSTRAP_REPORT.json"
                ),
                "r3_production_handoff": str(
                    self.config.phase3_artifacts_root
                    / "R3_PRODUCTION_HANDOFF.json"
                    if self.config.phase3_artifacts_root
                    else ""
                ),
                "phase3_stage_metrics": str(
                    self.config.phase3_artifacts_root
                    / "PHASE3_STAGE_METRICS.json"
                    if self.config.phase3_artifacts_root
                    else ""
                ),
                "section_coverage_manifest": str(
                    self.work_dir
                    / "section_coverage"
                    / "SECTION_COVERAGE_RUN.json"
                ),
                "coverage_global_ledger": str(
                    self.work_dir / "COVERAGE_GLOBAL_LEDGER.json"
                ),
                "authoring_work_dir": str(
                    self._prior_stage_work_dir("authoring_revision") or ""
                ),
                "publication_mainline_summary": str(
                    self.work_dir
                    / "publication_mainline"
                    / "PUBLICATION_MAINLINE_SUMMARY.json"
                )
                if (
                    self.work_dir
                    / "publication_mainline"
                    / "PUBLICATION_MAINLINE_SUMMARY.json"
                ).exists()
                else "",
                "publication_mainline_manifest": str(
                    self.work_dir
                    / "publication_mainline"
                    / "full_manuscript_manifest.json"
                )
                if (
                    self.work_dir
                    / "publication_mainline"
                    / "full_manuscript_manifest.json"
                ).exists()
                else "",
                "publication_mainline_handoff": str(
                    self.work_dir
                    / "publication_mainline"
                    / "handoff"
                    / "UNIFIED_MANUSCRIPT_HANDOFF.json"
                )
                if (
                    self.work_dir
                    / "publication_mainline"
                    / "handoff"
                    / "UNIFIED_MANUSCRIPT_HANDOFF.json"
                ).exists()
                else "",
                "publication_mainline_commander_work_order": str(
                    self.work_dir
                    / "publication_mainline"
                    / "commander"
                    / "global_commander_work_order.json"
                )
                if (
                    self.work_dir
                    / "publication_mainline"
                    / "commander"
                    / "global_commander_work_order.json"
                ).exists()
                else "",
                "publication_mainline_staged_state": str(
                    self.work_dir
                    / "publication_mainline"
                    / "staged_completion"
                    / "staged_article_completion_state.json"
                )
                if (
                    self.work_dir
                    / "publication_mainline"
                    / "staged_completion"
                    / "staged_article_completion_state.json"
                ).exists()
                else "",
                "chapter_style_governance_report": str(
                    self.work_dir
                    / "publication_mainline"
                    / "CHAPTER_STYLE_GOVERNANCE_REPORT.json"
                )
                if (
                    self.work_dir
                    / "publication_mainline"
                    / "CHAPTER_STYLE_GOVERNANCE_REPORT.json"
                ).exists()
                else "",
                "chapter_style_governance_output": (
                    str(final_review)
                    if final_review
                    and final_review.name.endswith(".chapter_style.md")
                    else ""
                ),
                "literature_portfolio_report": str(
                    self.work_dir / "LITERATURE_PORTFOLIO_REPORT.json"
                ),
                "research_program_work_dir": str(
                    self.work_dir / "research_program"
                )
                if (self.work_dir / "research_program").exists()
                else "",
                "article_completion_package": str(
                    self.work_dir
                    / "article_completion"
                    / "ARTICLE_COMPLETION_PACKAGE.json"
                )
                if (
                    self.work_dir
                    / "article_completion"
                    / "ARTICLE_COMPLETION_PACKAGE.json"
                ).exists()
                else "",
                "article_synthesis_map": str(
                    self.work_dir
                    / "article_completion"
                    / "ARTICLE_SYNTHESIS_MAP.json"
                )
                if (
                    self.work_dir
                    / "article_completion"
                    / "ARTICLE_SYNTHESIS_MAP.json"
                ).exists()
                else "",
                "article_structure_audit": str(
                    self.work_dir
                    / "article_completion"
                    / "ARTICLE_STRUCTURE_AUDIT.json"
                )
                if (
                    self.work_dir
                    / "article_completion"
                    / "ARTICLE_STRUCTURE_AUDIT.json"
                ).exists()
                else "",
                "global_figure_plan": str(
                    self.work_dir
                    / "article_completion"
                    / "GLOBAL_FIGURE_PLAN.json"
                )
                if (
                    self.work_dir
                    / "article_completion"
                    / "GLOBAL_FIGURE_PLAN.json"
                ).exists()
                else "",
                "article_figure_placements": str(
                    self.work_dir
                    / "article_completion"
                    / "manuscript"
                    / "ARTICLE_FIGURE_PLACEMENTS.md"
                )
                if (
                    self.work_dir
                    / "article_completion"
                    / "manuscript"
                    / "ARTICLE_FIGURE_PLACEMENTS.md"
                ).exists()
                else "",
                "final_visual_package": (
                    str(final_visual_package)
                    if final_visual_package
                    else ""
                ),
                "metadata_catalog": metadata_catalog_path,
                "metadata_audit": metadata_audit_path,
            },
            "scope_note": (
                "This package contains review content and canonical image paths. "
                "Formal LaTeX layout is intentionally delegated to a later stage."
            ),
            "created_at": _now(),
        }
        atomic_write_json(self.package_path, package)
        packaging_seconds = self.observability.finish_stage(
            "packaging",
            "completed",
        )
        self.state.setdefault("stages", {})["packaging"] = {
            "status": "completed",
            "updated_at": _now(),
            "wall_time_seconds": round(packaging_seconds, 3),
        }
        self._save_state()

        # Article-level metadata written by publication_mainline_adapter.
        # Defined once here so both the LaTeX stage and the Chinese translation
        # stage can reference it without duplicating the path derivation.
        # DISTINCT from PUBLICATION_METADATA_CATALOG.json (bibliography catalog).
        _article_metadata_path = (
            self.work_dir
            / "publication_mainline"
            / "staged_completion"
            / "PUBLICATION_METADATA.json"
        )
        publication_hard_failure = False
        latex_report: Dict[str, Any] = {}
        latex_pdf_path: Optional[Path] = None
        latex_source_archive_path: Optional[Path] = None
        if (
            self.config.produce_latex_publication
            and final_review
            and final_review.exists()
        ):
            self.observability.start_stage("latex_publication")
            try:
                latex_report = build_latex_publication(
                    content_package_path=self.package_path,
                    output_dir=self.work_dir / "publication" / "latex",
                    metadata_path=(
                        _article_metadata_path
                        if _article_metadata_path.is_file()
                        else self.config.publication_metadata_path
                    ),
                    enrich_crossref=self.config.latex_enrich_crossref,
                    compile_pdf=self.config.compile_pdf,
                    pdf_strict=self.config.pdf_strict,
                    render_previews=self.config.latex_render_previews,
                )
                if latex_report.get("pdf_skipped_reason"):
                    self.observability.emit(
                        "pdf_skipped",
                        stage="latex_publication",
                        reason=str(latex_report["pdf_skipped_reason"]),
                        detail=(
                            "未检测到 LaTeX，已跳过 PDF 编译；"
                            ".tex/.md 已生成"
                        ),
                    )
                latex_status = str(latex_report.get("status") or "failed")
                artifacts = latex_report.get("artifacts", {})
                raw_pdf = str(artifacts.get("compiled_pdf") or "")
                raw_archive = str(artifacts.get("arxiv_source_zip") or "")
                if raw_pdf and Path(raw_pdf).is_file():
                    latex_pdf_path = Path(raw_pdf)
                if raw_archive and Path(raw_archive).is_file():
                    latex_source_archive_path = Path(raw_archive)
                if latex_status == "failed" and status == "completed":
                    status = "awaiting_human_review"
                if latex_status == "failed":
                    publication_hard_failure = True
            except Exception as exc:
                latex_status = "failed"
                latex_report = {
                    "schema_version": (
                        "research_harness.latex_build_report.v1"
                    ),
                    "status": "failed",
                    "submission_blockers": [
                        f"{type(exc).__name__}:{exc}"
                    ],
                }
                self.observability.fail(
                    stage="latex_publication",
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
                if status == "completed":
                    status = "awaiting_human_review"
                publication_hard_failure = True
            latex_seconds = self.observability.finish_stage(
                "latex_publication",
                latex_status,
                compiled_pdf=bool(latex_pdf_path),
                source_archive=bool(latex_source_archive_path),
            )
            self.stage_costs["latex_publication"] = {
                "estimated_cost_cny": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "wall_time_seconds": round(latex_seconds, 3),
                "status": latex_status,
            }
            self.state.setdefault("stages", {})["latex_publication"] = {
                "status": latex_status,
                "updated_at": _now(),
                "wall_time_seconds": round(latex_seconds, 3),
                "build_report_path": str(
                    self.work_dir
                    / "publication"
                    / "latex"
                    / "LATEX_BUILD_REPORT.json"
                ),
            }
        else:
            latex_status = (
                "disabled"
                if not self.config.produce_latex_publication
                else "disabled_no_final_review"
            )
            self.state.setdefault("stages", {})["latex_publication"] = {
                "status": latex_status,
                "updated_at": _now(),
            }

        translation_report: Dict[str, Any] = {}
        chinese_latex_report: Dict[str, Any] = {}
        # Bound before the translation ``try`` because both are read after it
        # (the Chinese LaTeX guard and the stage ledger).  Leaving them to be
        # assigned inside the ``try`` made an exception in
        # ``translate_review_package`` raise ``UnboundLocalError`` here, which
        # ``_finish``'s blanket recovery then turned into ``status="failed"``
        # *before* ``build_delivery_gate`` ran -- discarding the already-paid
        # English deliverable and the gate itself.  Fail closed on translation,
        # never on the packaging code path.
        translation_ok = False
        translation_status = "failed"
        chinese_review_path: Optional[Path] = None
        chinese_latex_pdf_path: Optional[Path] = None
        chinese_latex_source_archive_path: Optional[Path] = None
        if (
            self.config.produce_chinese_publication
            and final_review
            and final_review.exists()
        ):
            self.observability.start_stage("chinese_translation")
            try:
                translation_report = translate_review_package(
                    content_package_path=self.package_path,
                    source_markdown_path=final_review,
                    output_dir=(
                        self.work_dir / "publication" / "translation_zh"
                    ),
                    # Fix E4: prefer the article-level metadata written by the
                    # publication mainline (contains the LLM/deterministic
                    # title resolved by plan_review_titles) over the config-
                    # level path, which is None by default.  Fallback keeps
                    # callers that explicitly pass a metadata path working.
                    english_metadata_path=(
                        _article_metadata_path
                        if _article_metadata_path.is_file()
                        else self.config.publication_metadata_path
                    ),
                    model_tier=self.config.translation_model_tier,
                    fallback_model_tier=(
                        self.config.translation_fallback_model_tier
                    ),
                    workers=self.config.translation_workers,
                    cost_budget_cny=(
                        self._admission_budget(
                            self.config.translation_cost_budget_cny
                        )
                    ),
                    allow_partial_output=self.config.translation_fail_open,
                )
                translation_status = str(
                    translation_report.get("status") or "failed"
                )
                raw_review_zh = str(
                    translation_report.get("translated_path") or ""
                )
                raw_partial_zh = str(
                    translation_report.get("partial_translated_path") or ""
                )
                if raw_review_zh and Path(raw_review_zh).is_file():
                    chinese_review_path = Path(raw_review_zh)
                elif raw_partial_zh and Path(raw_partial_zh).is_file():
                    # Degraded delivery: the Markdown partial transcript is a
                    # legitimate LaTeX source so the paid-for Chinese content
                    # still reaches the rendering stage.
                    chinese_review_path = Path(raw_partial_zh)
                translation_ok = translation_status in {
                    "completed",
                    "completed_with_warnings",
                }
                if not translation_ok and status == "completed":
                    status = "awaiting_human_review"
                if not translation_ok:
                    publication_hard_failure = True
            except Exception as exc:
                translation_status = "failed"
                translation_report = {
                    "schema_version": (
                        "research_harness.chinese_translation_report.v1"
                    ),
                    "status": "failed",
                    "failed_unit_ids": [],
                    "errors": [f"{type(exc).__name__}:{exc}"],
                }
                self.observability.fail(
                    stage="chinese_translation",
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
                if status == "completed":
                    status = "awaiting_human_review"
                publication_hard_failure = True
            translation_seconds = self.observability.finish_stage(
                "chinese_translation",
                translation_status,
                translated_review=bool(chinese_review_path),
                failed_unit_count=len(
                    translation_report.get("failed_unit_ids", []) or []
                ),
            )
            translation_cost = float(
                translation_report.get(
                    "cumulative_estimated_cost_cny",
                    translation_report.get("estimated_cost_cny", 0.0),
                )
                or 0.0
            )
            translation_input_tokens = int(
                translation_report.get(
                    "cumulative_input_tokens",
                    translation_report.get("estimated_input_tokens", 0),
                )
                or 0
            )
            translation_output_tokens = int(
                translation_report.get(
                    "cumulative_output_tokens",
                    translation_report.get("estimated_output_tokens", 0),
                )
                or 0
            )
            self.stage_costs["chinese_translation"] = {
                "estimated_cost_cny": round(translation_cost, 6),
                "input_tokens": translation_input_tokens,
                "output_tokens": translation_output_tokens,
                "wall_time_seconds": round(translation_seconds, 3),
                "status": translation_status,
            }
            self.state.setdefault("stages", {})["chinese_translation"] = {
                "status": translation_status,
                "updated_at": _now(),
                "wall_time_seconds": round(translation_seconds, 3),
                "translation_report_path": str(
                    self.work_dir
                    / "publication"
                    / "translation_zh"
                    / "TRANSLATION_REPORT.json"
                ),
            }

            if translation_ok and chinese_review_path:
                self.observability.start_stage("latex_publication_zh")
                try:
                    metadata_zh = str(
                        translation_report.get(
                            "translated_metadata_path"
                        )
                        or ""
                    )
                    chinese_latex_report = build_latex_publication(
                        content_package_path=self.package_path,
                        output_dir=(
                            self.work_dir / "publication" / "latex_zh"
                        ),
                        metadata_path=(
                            Path(metadata_zh) if metadata_zh else None
                        ),
                        source_markdown_path=chinese_review_path,
                        language="zh-CN",
                        enrich_crossref=(
                            self.config.latex_enrich_crossref
                        ),
                        compile_pdf=self.config.compile_pdf,
                        pdf_strict=self.config.pdf_strict,
                        render_previews=(
                            self.config.latex_render_previews
                        ),
                    )
                    if chinese_latex_report.get("pdf_skipped_reason"):
                        self.observability.emit(
                            "pdf_skipped",
                            stage="latex_publication_zh",
                            reason=str(
                                chinese_latex_report["pdf_skipped_reason"]
                            ),
                            detail=(
                                "未检测到 LaTeX，已跳过 PDF 编译；"
                                ".tex/.md 已生成"
                            ),
                        )
                    chinese_latex_status = str(
                        chinese_latex_report.get("status") or "failed"
                    )
                    artifacts_zh = chinese_latex_report.get(
                        "artifacts", {}
                    )
                    raw_pdf_zh = str(
                        artifacts_zh.get("compiled_pdf") or ""
                    )
                    raw_archive_zh = str(
                        artifacts_zh.get("arxiv_source_zip") or ""
                    )
                    if raw_pdf_zh and Path(raw_pdf_zh).is_file():
                        chinese_latex_pdf_path = Path(raw_pdf_zh)
                    if (
                        raw_archive_zh
                        and Path(raw_archive_zh).is_file()
                    ):
                        chinese_latex_source_archive_path = Path(
                            raw_archive_zh
                        )
                    if (
                        chinese_latex_status == "failed"
                        and status == "completed"
                    ):
                        status = "awaiting_human_review"
                    if chinese_latex_status == "failed":
                        publication_hard_failure = True
                except Exception as exc:
                    chinese_latex_status = "failed"
                    chinese_latex_report = {
                        "schema_version": (
                            "research_harness.latex_build_report.v1"
                        ),
                        "status": "failed",
                        "submission_blockers": [
                            f"{type(exc).__name__}:{exc}"
                        ],
                    }
                    self.observability.fail(
                        stage="latex_publication_zh",
                        error_type=type(exc).__name__,
                        detail=str(exc),
                    )
                    if status == "completed":
                        status = "awaiting_human_review"
                    publication_hard_failure = True
                chinese_latex_seconds = self.observability.finish_stage(
                    "latex_publication_zh",
                    chinese_latex_status,
                    compiled_pdf=bool(chinese_latex_pdf_path),
                    source_archive=bool(
                        chinese_latex_source_archive_path
                    ),
                )
                self.stage_costs["latex_publication_zh"] = {
                    "estimated_cost_cny": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "wall_time_seconds": round(
                        chinese_latex_seconds, 3
                    ),
                    "status": chinese_latex_status,
                }
                self.state.setdefault("stages", {})[
                    "latex_publication_zh"
                ] = {
                    "status": chinese_latex_status,
                    "updated_at": _now(),
                    "wall_time_seconds": round(
                        chinese_latex_seconds, 3
                    ),
                    "build_report_path": str(
                        self.work_dir
                        / "publication"
                        / "latex_zh"
                        / "LATEX_BUILD_REPORT.json"
                    ),
                }
            else:
                chinese_latex_status = "disabled_translation_failed"
                self.state.setdefault("stages", {})[
                    "latex_publication_zh"
                ] = {
                    "status": chinese_latex_status,
                    "updated_at": _now(),
                }
        else:
            translation_status = (
                "disabled"
                if not self.config.produce_chinese_publication
                else "disabled_no_final_review"
            )
            chinese_latex_status = translation_status
            self.state.setdefault("stages", {})[
                "chinese_translation"
            ] = {
                "status": translation_status,
                "updated_at": _now(),
            }
            self.state.setdefault("stages", {})[
                "latex_publication_zh"
            ] = {
                "status": chinese_latex_status,
                "updated_at": _now(),
            }

        research_plan_publication_report: Dict[str, Any] = {}
        research_plan_latex_pdf_path: Optional[Path] = None
        research_plan_chinese_latex_pdf_path: Optional[Path] = None
        if (
            self.config.produce_research_plan_publication
            and research_plan
            and research_plan.exists()
            and self.config.produce_latex_publication
            and self.config.produce_chinese_publication
        ):
            self.observability.start_stage("research_plan_publication")
            try:
                research_plan_publication_report = (
                    build_bilingual_research_plan_publication(
                        research_program_dir=research_plan.parent,
                        review_content_package_path=self.package_path,
                        output_dir=(
                            self.work_dir / "publication" / "research_plan"
                        ),
                        translation_model_tier=self.config.translation_model_tier,
                        translation_fallback_model_tier=(
                            self.config.translation_fallback_model_tier
                        ),
                        translation_workers=max(1, self.config.translation_workers),
                        translation_cost_budget_cny=(
                            self._admission_budget(
                                self.config.research_plan_translation_cost_budget_cny
                            )
                        ),
                        enrich_crossref=self.config.latex_enrich_crossref,
                        compile_pdf=self.config.compile_pdf,
                        pdf_strict=self.config.pdf_strict,
                        render_previews=self.config.latex_render_previews,
                    )
                )
                artifacts_plan = research_plan_publication_report.get("artifacts", {})
                raw_plan_en = str(artifacts_plan.get("english_pdf") or "")
                raw_plan_zh = str(artifacts_plan.get("chinese_pdf") or "")
                if raw_plan_en and Path(raw_plan_en).is_file():
                    research_plan_latex_pdf_path = Path(raw_plan_en)
                if raw_plan_zh and Path(raw_plan_zh).is_file():
                    research_plan_chinese_latex_pdf_path = Path(raw_plan_zh)
                research_plan_publication_status = str(
                    research_plan_publication_report.get("status") or "failed"
                )
                if research_plan_publication_status == "failed" and status == "completed":
                    status = "awaiting_human_review"
                if research_plan_publication_status == "failed":
                    publication_hard_failure = True
            except Exception as exc:
                research_plan_publication_status = "failed"
                research_plan_publication_report = {
                    "status": "failed",
                    "errors": [f"{type(exc).__name__}:{exc}"],
                }
                self.observability.fail(
                    stage="research_plan_publication",
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
                if status == "completed":
                    status = "awaiting_human_review"
                publication_hard_failure = True
            plan_pub_seconds = self.observability.finish_stage(
                "research_plan_publication",
                research_plan_publication_status,
                english_pdf=bool(research_plan_latex_pdf_path),
                chinese_pdf=bool(research_plan_chinese_latex_pdf_path),
            )
            plan_translation = research_plan_publication_report.get("translation", {})
            self.stage_costs["research_plan_publication"] = {
                "estimated_cost_cny": round(
                    float(plan_translation.get("estimated_cost_cny", 0.0) or 0.0),
                    6,
                ),
                "input_tokens": int(
                    plan_translation.get("estimated_input_tokens", 0) or 0
                ),
                "output_tokens": int(
                    plan_translation.get("estimated_output_tokens", 0) or 0
                ),
                "wall_time_seconds": round(plan_pub_seconds, 3),
                "status": research_plan_publication_status,
            }
            self.state.setdefault("stages", {})["research_plan_publication"] = {
                "status": research_plan_publication_status,
                "updated_at": _now(),
                "report_path": str(
                    self.work_dir / "publication" / "research_plan" / "BILINGUAL_RESEARCH_PLAN_REPORT.json"
                ),
            }
        else:
            research_plan_publication_status = "disabled"
            self.state.setdefault("stages", {})["research_plan_publication"] = {
                "status": research_plan_publication_status,
                "updated_at": _now(),
            }

        # Attach every publication result before evaluating the delivery gate.
        # The package is also the input to the LaTeX renderer, but the gate
        # must inspect the fresh paths/reports from this invocation rather than
        # the earlier pre-publication snapshot.
        package["completed_stage"] = (
            "latex_publication_zh"
            if self.config.produce_chinese_publication and final_review
            else (
                "latex_publication"
                if self.config.produce_latex_publication and final_review
                else completed_stage
            )
        )
        package["latex_publication_status"] = latex_status
        package["latex_pdf_path"] = (
            str(latex_pdf_path) if latex_pdf_path else ""
        )
        package["latex_source_archive_path"] = (
            str(latex_source_archive_path)
            if latex_source_archive_path
            else ""
        )
        package["latex_build_report"] = latex_report
        package["chinese_translation_status"] = translation_status
        package["final_review_zh_path"] = (
            str(chinese_review_path) if chinese_review_path else ""
        )
        package["chinese_latex_publication_status"] = chinese_latex_status
        package["chinese_latex_pdf_path"] = (
            str(chinese_latex_pdf_path)
            if chinese_latex_pdf_path
            else ""
        )
        package["chinese_latex_source_archive_path"] = (
            str(chinese_latex_source_archive_path)
            if chinese_latex_source_archive_path
            else ""
        )
        package["chinese_translation_report"] = translation_report
        package["chinese_latex_build_report"] = chinese_latex_report
        package["research_plan_publication_status"] = (
            research_plan_publication_status
        )
        package["research_plan_publication_report"] = (
            research_plan_publication_report
        )
        package["research_plan_audit_path"] = str(
            self.work_dir / "research_program" / "RESEARCH_PLAN_AUDIT.json"
        )
        package["research_plan_latex_pdf_path"] = (
            str(research_plan_latex_pdf_path)
            if research_plan_latex_pdf_path
            else ""
        )
        package["research_plan_chinese_latex_pdf_path"] = (
            str(research_plan_chinese_latex_pdf_path)
            if research_plan_chinese_latex_pdf_path
            else ""
        )
        package["artifacts"].update(
            {
                "latex_build_report": str(
                    self.work_dir
                    / "publication"
                    / "latex"
                    / "LATEX_BUILD_REPORT.json"
                )
                if latex_report
                else "",
                "latex_pdf": str(latex_pdf_path) if latex_pdf_path else "",
                "latex_source_archive": (
                    str(latex_source_archive_path)
                    if latex_source_archive_path
                    else ""
                ),
                "chinese_translation_report": str(
                    self.work_dir
                    / "publication"
                    / "translation_zh"
                    / "TRANSLATION_REPORT.json"
                )
                if translation_report
                else "",
                "final_review_zh": (
                    str(chinese_review_path)
                    if chinese_review_path
                    else ""
                ),
                "chinese_latex_build_report": str(
                    self.work_dir
                    / "publication"
                    / "latex_zh"
                    / "LATEX_BUILD_REPORT.json"
                )
                if chinese_latex_report
                else "",
                "chinese_latex_pdf": (
                    str(chinese_latex_pdf_path)
                    if chinese_latex_pdf_path
                    else ""
                ),
                "chinese_latex_source_archive": (
                    str(chinese_latex_source_archive_path)
                    if chinese_latex_source_archive_path
                    else ""
                ),
                "research_plan_publication_report": str(
                    self.work_dir
                    / "publication"
                    / "research_plan"
                    / "BILINGUAL_RESEARCH_PLAN_REPORT.json"
                )
                if research_plan_publication_report
                else "",
                "research_plan_audit": str(
                    self.work_dir
                    / "research_program"
                    / "RESEARCH_PLAN_AUDIT.json"
                )
                if self.config.produce_research_plan_publication
                else "",
                "research_plan_latex_pdf": (
                    str(research_plan_latex_pdf_path)
                    if research_plan_latex_pdf_path
                    else ""
                ),
                "research_plan_chinese_latex_pdf": (
                    str(research_plan_chinese_latex_pdf_path)
                    if research_plan_chinese_latex_pdf_path
                    else ""
                ),
            }
        )

        # Fail-open review semantics: advisory ``awaiting_human_review`` /
        # ``needs_attention`` findings do not block final delivery when the
        # durable manuscript, source map, integrity checks, and compilation
        # all passed.  Provenance/contract/integrity/compiler failures remain
        # blocking through ``publication_hard_failure`` and the quality gate.
        if (
            status == "awaiting_human_review"
            and not publication_hard_failure
            and review_body_validation.get("status") == "passed"
            and final_review
            and not _quality_report_hard_blocks(quality_report)
        ):
            status = "completed"

        delivery_gate = build_delivery_gate(
            work_dir=self.work_dir,
            package=package,
            quality_report=_delivery_quality_report(quality_report),
            latex_report=latex_report,
            chinese_translation_report=translation_report,
            chinese_latex_report=chinese_latex_report,
            research_plan_publication_report=research_plan_publication_report,
            # These are policy requests, not observations of what happened.
            # A configured-but-missing output must therefore remain blocking.
            require_review=bool(self.config.produce_latex_publication),
            require_chinese_review=bool(self.config.produce_chinese_publication),
            require_research_plan=bool(
                self.config.produce_research_plan_publication
            ),
        )
        delivery_gate_path = self.work_dir / "DELIVERY_GATE.json"
        atomic_write_json(delivery_gate_path, delivery_gate)
        package["delivery_gate"] = delivery_gate
        package.setdefault("artifacts", {})["delivery_gate"] = str(delivery_gate_path)
        # P2-1 wiring 1: register an awaiting-human decision whenever the
        # gate has any check parked on a human.  The condition is the
        # non-empty awaiting_human_checks list — there is NO top-level
        # "awaiting_human" status (it is folded into "degraded").
        # delivery_gate is a mandatory-human kind: no auto-accept parameter,
        # ever.  Registration failure must not destroy a finished run, but
        # it is loudly logged rather than swallowed silently.
        if delivery_gate.get("awaiting_human_checks"):
            try:
                # P0-1: the delivery gate now honours the same bounded wait.
                # Round-3 policy change authorized by the user: a finished
                # run must not die on an unanswered gate.
                request_decision(
                    run_dir=self.work_dir,
                    kind="delivery_gate",
                    subject_id=str(self.run_id or "delivery_gate"),
                    context={
                        "gate_status": delivery_gate.get("status"),
                        "awaiting_human_checks": list(
                            delivery_gate.get("awaiting_human_checks") or []
                        ),
                    },
                    options=["accept", "reject"],
                    auto_accept_after_seconds=(
                        self._effective_gate_seconds()
                    ),
                    default_option="accept",
                )
                expire_due_decisions(self.work_dir)
            except OSError as exc:
                logger.warning(
                    "human_decision_gate registration failed "
                    "for delivery_gate: %s: %s",
                    type(exc).__name__,
                    exc,
                )
        gate_state = str(delivery_gate.get("status") or "")
        if status == "completed" and not delivery_gate.get("passed", False):
            # A degraded gate means deliverables exist with recorded issues, so
            # it must not be crushed into the same bucket as a hard failure.
            # It maps onto ``awaiting_human_review`` rather than a new word:
            # that status is already in r6's NONTERMINAL_STATUSES and already
            # maps to CLI exit code 3.  A bespoke ``completed_with_warnings``
            # terminal status is in no consumer whitelist, so it would surface
            # as exit code 1 (indistinguishable from a hard failure, breaking
            # CI) and as ``failed_or_incomplete`` in r6.  The degradation
            # detail is preserved in ``delivery_gate`` / the field below.
            status = (
                "incomplete_required_outputs_missing"
                if gate_state != "degraded"
                else "awaiting_human_review"
            )
        package["delivery_degraded"] = gate_state == "degraded"
        if not final_review and status == "completed":
            status = "failed"

        package["status"] = status
        package["cost_cny"] = self._total_cost_cny()
        package["total_cost_cny"] = self._total_cost_cny()
        package["total_input_tokens"] = self._total_stage_tokens("input_tokens")
        package["total_output_tokens"] = self._total_stage_tokens("output_tokens")
        package["scope_note"] = (
            "This package contains the review, research plan, canonical visual "
            "decisions, and an arXiv-compatible LaTeX manuscript when the "
            "publication stage is enabled. Author metadata and pending "
            "conceptual visuals remain explicit human-review items."
        )
        package["review_body_validation"] = review_body_validation
        metrics = self._reconcile_terminal_artifacts(
            status=status,
            current_stage=package["completed_stage"],
        )
        status = str(self.state.get("status") or "failed")
        package["status"] = status
        package["terminal_reconciliation_id"] = self.state.get(
            "terminal_reconciliation_id", ""
        )
        package["active_wall_time_seconds"] = metrics[
            "active_wall_time_seconds"
        ]
        package["operations_summary"] = metrics["operations"]
        package["artifacts"].update(
            {
                "run_metrics": str(self.observability.metrics_path),
                "run_timeline": str(self.observability.events_path),
                "log_index": str(self.observability.log_index_path),
                "operations_report": str(self.observability.report_path),
            }
        )
        atomic_write_json(self.package_path, package)
        return ReviewHarnessResult(
            run_id=self.run_id,
            status=status,
            completed_stage=str(package["completed_stage"]),
            total_cost_cny=self._total_cost_cny(),
            total_input_tokens=package["total_input_tokens"],
            total_output_tokens=package["total_output_tokens"],
            work_dir=self.work_dir,
            final_review_path=final_review,
            visual_plan_path=visual_plan,
            final_visual_package_path=final_visual_package,
            research_plan_path=research_plan,
            latex_pdf_path=latex_pdf_path,
            latex_source_archive_path=latex_source_archive_path,
            chinese_review_path=chinese_review_path,
            chinese_latex_pdf_path=chinese_latex_pdf_path,
            chinese_latex_source_archive_path=(
                chinese_latex_source_archive_path
            ),
            package_path=self.package_path,
            research_plan_latex_pdf_path=research_plan_latex_pdf_path,
            research_plan_chinese_latex_pdf_path=(
                research_plan_chinese_latex_pdf_path
            ),
        )
