"""Coordinate section-level literature coverage with bounded AgentScope workers."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from optomind_research.s2_fulltext_acquisition import merge_kb_sqlite_into

from .full_review_orchestrator import SectionMaterialBundle
from .research_worker import ResearchWorker
from .section_coverage_tool_registry import (
    SECTION_COVERAGE_TOOL_NAMES,
    SectionCoverageToolProvider,
    _bounded_materialization_limit_reached,
    _document_bounded_materialization_gaps,
    _make_submit_section_gap_report,
    _make_validate_section_coverage_package,
    _make_load_section_context,
    _make_inspect_section_local_coverage,
    _make_inspect_local_candidate_batch,
    _make_submit_literature_role_plan,
    _make_search_oa_candidates,
    _make_inspect_candidate_batch,
    _make_submit_candidate_audit,
    _make_submit_local_source_audit,
    _make_refresh_section_coverage,
    _candidate_audit_evidence_fingerprint,
    ROLE_DEFINITIONS,
    _deterministic_candidate_action,
    _coverage_query_targets,
    _record_deterministic_step,
    _record_short_path_stop,
    _sync_article_portfolio_telemetry,
    _reconcile_batched_audit_usage,
)
from .coverage_ledger import (
    load as _load_coverage_ledger,
    save as _save_coverage_ledger,
)
from .task_contract import TaskContract
from .tool_provider import SectionCoverageContext
from .review_quality_contract import (
    build_adaptive_coverage_contract,
    resolve_review_contract,
)
from .coverage_decision_contract import (
    admit_context_call,
    admit_batched_audit_call,
    bounded_audit_output_tokens,
    build_compact_batched_audit_payload,
    build_uncovered_query_targets,
    decode_json_payload,
    evaluate_local_audit_stop,
    evaluate_coverage_readiness,
    estimate_json_tokens,
    normalize_local_audit_records,
    plan_local_audit_batch,
    normalize_qwen_usage,
    normalize_scientific_query,
    scientific_query_anchor,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The deterministic controller keeps the searched-audit path tight (two
# bounded waves with one compact audit each), while the local-first audit is
# token-bounded and batch-sized from estimates.  There is deliberately no
# fixed six-candidate/two-call ceiling for local candidate examination.
SHORT_PATH_MAX_MODEL_CALLS = 2
SHORT_PATH_MAX_AUDIT_CALLS = 2
SHORT_PATH_MAX_WAVES = 2
SHORT_PATH_PER_CALL_CONTEXT_TOKENS = 20_000
SHORT_PATH_CUMULATIVE_CONTEXT_TOKENS = 40_000
SHORT_PATH_SEARCH_AUDIT_CANDIDATES = 6
# Local audit is allowed to use the caller's per-call/cumulative budgets
# (with safety ceilings) so a broad local pool can actually be examined.
LOCAL_AUDIT_PER_CALL_CONTEXT_TOKENS = 32_000
LOCAL_AUDIT_CUMULATIVE_CONTEXT_TOKENS = 240_000
LOCAL_AUDIT_SOFT_CANDIDATE_TARGET = 180
LOCAL_AUDIT_MAX_BATCH_CANDIDATES = 40
LOCAL_AUDIT_MALFORMED_RETRY_LIMIT = 1
LOCAL_AUDIT_MIN_SEMANTIC_GAIN = 0.35
# The searched-audit path keeps the historical conservative allowance; the
# local path uses a compact allowance because it owns the envelope/schema.
SHORT_PATH_AUDIT_OUTPUT_BASE_TOKENS = 600
SHORT_PATH_AUDIT_OUTPUT_TOKENS_PER_CANDIDATE = 900
SHORT_PATH_AUDIT_OUTPUT_HARD_CAP = 8192
LOCAL_AUDIT_OUTPUT_BASE_TOKENS = 600
LOCAL_AUDIT_OUTPUT_TOKENS_PER_CANDIDATE = 260
LOCAL_AUDIT_OUTPUT_HARD_CAP = 12_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_worker_count(env_name: str, default: int) -> int:
    """Resolve a bounded worker count from an explicit value or env var.

    Follows the S2 discovery/retrieval convention: environment wins over the
    conservative default, and the result is clamped to [1, 16] so a
    misconfiguration can never create hidden unlimited fanout.
    """

    try:
        raw = os.environ.get(env_name, str(default))
        return max(1, min(int(raw), 16))
    except (TypeError, ValueError):
        return max(1, min(int(default), 16))


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bounded_audit_output_tokens(candidate_count: int) -> int:
    """Size the searched-audit output allowance from the admitted batch size.

    The searched path still clamps to its six-candidate wave ceiling; the
    local path bypasses this helper and uses its own compact reserve.
    """

    return bounded_audit_output_tokens(
        min(
            int(candidate_count or 0),
            SHORT_PATH_SEARCH_AUDIT_CANDIDATES,
        ),
        base_tokens=SHORT_PATH_AUDIT_OUTPUT_BASE_TOKENS,
        per_candidate_tokens=SHORT_PATH_AUDIT_OUTPUT_TOKENS_PER_CANDIDATE,
        hard_cap_tokens=SHORT_PATH_AUDIT_OUTPUT_HARD_CAP,
    )


def _resolve_model_name(model_tier: str) -> str:
    try:
        from config.qwen_config import get_model_name

        return str(get_model_name(model_tier) or "").strip()
    except Exception:
        return str(model_tier or "").strip()


def _estimate_model_call_cost_cny(
    model_tier: str,
    *,
    predicted_input_tokens: int,
    output_reserve_tokens: int,
) -> float:
    try:
        from .cost_ledger import estimate_call_cost_cny

        return round(
            float(
                estimate_call_cost_cny(
                    _resolve_model_name(model_tier),
                    max(0, int(predicted_input_tokens or 0)),
                    max(0, int(output_reserve_tokens or 0)),
                )
            ),
            6,
        )
    except Exception:
        return 0.0


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _candidate_needs_new_audit_payload(
    section_work_dir: Path,
    candidate: Dict[str, Any],
) -> bool:
    """Return whether a deferred candidate has materially new evidence.

    The candidate ledger survives a process restart.  A deferred record is not
    a fresh retrieval result merely because the controller is resumed; only a
    changed evidence payload may reopen the audit.
    """

    candidate_id = str(candidate.get("candidate_id") or "")
    if not candidate_id:
        return False
    state = _read_json(section_work_dir / "COVERAGE_AGENT_PAYLOAD_STATE.json")
    audited = state.get("audited_candidate_fingerprints") or {}
    previous = str(audited.get(candidate_id) or "")
    if not previous:
        # Pre-fix runs did not persist per-candidate fingerprints, but they did
        # persist the candidate IDs included in each completed audit wave.  A
        # same-payload legacy resume must therefore fail closed rather than
        # spend another audit call.  New runs use the precise fingerprint path
        # above and can reopen on genuinely changed evidence.
        wave_telemetry = _read_json(
            section_work_dir / "COVERAGE_WAVE_TELEMETRY.json"
        )
        if any(
            candidate_id in {
                str(value)
                for value in (row.get("candidate_ids") or [])
            }
            and int(row.get("audit_calls") or 0) > 0
            for row in wave_telemetry.get("waves", [])
            if isinstance(row, dict)
        ):
            return False
        return True
    return previous != _candidate_audit_evidence_fingerprint(candidate)


def _stop_reason_category(reason: str) -> str:
    """Classify a short-path stop for machine-readable run telemetry."""

    lowered = str(reason or "").casefold()
    if any(
        marker in lowered
        for marker in (
            "bounded_waves_exhausted",
            "no_candidates",
            "no_novel",
            "scientific_exhaustion",
            "local_pool_exhausted",
            "local_marginal_gain_exhausted",
            "local_soft_visibility_target_reached",
        )
    ):
        return "scientific_exhaustion"
    if "coverage_outcome_reached" in lowered or "coverage_sufficient" in lowered:
        return "scientific_completion"
    if any(
        marker in lowered
        for marker in (
            "error", "exception", "validation_failed", "audit_gap",
            "malformed", "provider_response",
            "budget_rejected", "permission", "schema", "runtime",
        )
    ):
        return "engineering_failure"
    return "unknown"


def _build_section_usage_receipt(
    *,
    section_work_dir: Path,
    model_tier: str,
    qwen_usage: Dict[str, Any],
    qwen_calls: int,
    input_tokens: int,
    output_tokens: int,
    cost_cny: float,
    cost_bases: List[str],
    cost_estimated_flags: List[bool],
) -> Dict[str, Any]:
    """Write one cumulative, auditable usage receipt for a section.

    The short-path artifacts used to mix a cumulative token total with the
    latest model-call cost.  This receipt is the sole source for the final
    per-section accounting; RESULT, SHORT_PATH_RUN, telemetry, and the parent
    manifest copy its values rather than recomputing them independently.
    """

    bases = [str(value or "unavailable") for value in cost_bases if value]
    distinct_bases = list(dict.fromkeys(bases))
    if not distinct_bases:
        cost_basis = "unavailable"
    elif len(distinct_bases) == 1:
        cost_basis = distinct_bases[0]
    else:
        cost_basis = "mixed"
    model_name = str(qwen_usage.get("model_name") or "").strip()
    if not model_name and qwen_calls:
        try:
            from config.qwen_config import get_model_name

            model_name = str(get_model_name(model_tier) or "").strip()
        except Exception:
            model_name = str(model_tier or "").strip()
    receipt_body = {
        "schema_version": "optomind.section_coverage_usage_receipt.v1",
        "model_tier": str(model_tier or ""),
        "model_name": model_name,
        "qwen_calls": max(0, int(qwen_calls or 0)),
        "input_tokens": max(0, int(input_tokens or 0)),
        "output_tokens": max(0, int(output_tokens or 0)),
        "cost_cny": round(max(0.0, float(cost_cny or 0.0)), 6),
        "cost_basis": cost_basis,
        "cost_is_estimated": any(bool(value) for value in cost_estimated_flags),
        "cost_provenance": (
            "provider_reported"
            if cost_basis == "provider_reported"
            else "configured_list_price_estimate"
            if cost_basis == "estimated_list_price"
            else "mixed"
            if cost_basis == "mixed"
            else "unavailable"
        ),
        "input_source": str(qwen_usage.get("input_source") or "aggregate"),
        "output_source": str(qwen_usage.get("output_source") or "aggregate"),
        "pricing_source": str(qwen_usage.get("pricing_source") or ""),
    }
    receipt_id = hashlib.sha1(
        json.dumps(receipt_body, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    receipt = {"receipt_id": receipt_id, **receipt_body}
    receipt_path = section_work_dir / "USAGE_RECEIPT.json"
    receipt["receipt_path"] = str(receipt_path)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Keep both telemetry files as views of the same cumulative receipt.  The
    # existing wave-level rows remain intact for diagnostics; only their
    # aggregate accounting fields are canonicalized here.
    for name in ("COVERAGE_WAVE_TELEMETRY.json", "PHASE2_TELEMETRY.json"):
        path = section_work_dir / name
        data = _read_json(path)
        data["usage_receipt_id"] = receipt_id
        data["usage_receipt_path"] = str(receipt_path)
        data["canonical_usage"] = dict(receipt)
        data["batched_llm_input_tokens"] = receipt["input_tokens"]
        data["batched_llm_output_tokens"] = receipt["output_tokens"]
        data["batched_llm_cost_cny"] = receipt["cost_cny"]
        data["model_input_tokens"] = receipt["input_tokens"]
        data["model_output_tokens"] = receipt["output_tokens"]
        data["total_input_tokens"] = receipt["input_tokens"]
        data["total_output_tokens"] = receipt["output_tokens"]
        data["total_model_calls"] = receipt["qwen_calls"]
        data["batched_llm_calls"] = receipt["qwen_calls"]
        data["cost_basis"] = receipt["cost_basis"]
        data["cost_is_estimated"] = receipt["cost_is_estimated"]
        data["model_tier"] = receipt["model_tier"]
        data["model_name"] = receipt["model_name"]
        data["pricing_source"] = receipt["pricing_source"]
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return receipt


@dataclass
class SectionCoverageOrchestratorConfig:
    blueprint_path: Path
    base_kb_sqlite: Optional[Path]
    output_root: Path
    model_tier: str = "advanced_model"
    model_override: Any = None
    # Candidate auditing is model-led, while approved-paper acquisition is a
    # coverage-aware deterministic batch that rechecks the package after each
    # paper.  Thirty-two remains an exception ceiling for malformed JSON,
    # citation chasing, and one strategically different search round; normal
    # sections terminate much earlier through provider auto-finalisation.
    max_iters_per_section: int = 32
    # This is a cumulative context-traffic guard, not a cost target.  ReAct
    # calls repeatedly include prior context, so 100k can stop a cheap section
    # immediately after OA materialization but before validation.  The CNY,
    # iteration, wall-time, and stage caps remain the real admission controls.
    token_budget_per_section: int = 500_000
    # Per-call context admission is separate from the cumulative budget.  The
    # latter remains 500k by default for compatibility with existing callers.
    context_tokens_per_model_call: int = 32_000
    context_output_reserve_tokens: int = 2_000
    # Hard ceilings for the reusable bounded chain.  The legacy 500k field
    # remains part of the public config for compatibility, but new runs use
    # this smaller model-context budget and call ceiling.
    model_context_budget_per_section: int = 96_000
    max_model_calls_per_section: int = 6
    max_coverage_waves: int = 2
    max_audit_calls_per_section: int = 2
    adaptive_coverage_enabled: bool = True
    cost_budget_per_section_cny: float = 2.5
    stage_cost_budget_cny: float = 14.0
    wall_time_per_section_seconds: float = 600.0
    max_queries_per_call: int = 3
    max_results_per_backend: int = 12
    # Six independent sources are enough for the default chapter-level
    # breadth gate.  The provider now validates after every successful paper,
    # so this is a safety ceiling rather than a target.
    max_materialized_papers_per_section: int = 6
    max_materialization_seconds_per_call: int = 170
    max_search_rounds_per_role: int = 3
    # A feedback pass may share the original staging KB while writing its own
    # auditable task artifacts in a separate run directory.
    staging_kb_path: Optional[Path] = None
    author_feedback_by_section: Dict[str, Dict[str, Any]] = field(
        default_factory=dict
    )
    # Targeted portfolio/editorial returns reuse the scientific ledgers in the
    # same section directory but archive runtime control files so the worker
    # genuinely re-enters research instead of returning a cached RESULT.json.
    force_research_sections: List[str] = field(default_factory=list)
    retry_label: str = "targeted_retry"
    preserve_existing_manifest: bool = False
    # One clean restart is allowed when a worker has useful scientific
    # artifacts on disk but its ReAct conversation has grown too large to
    # reach validation. The scientific ledgers are retained; only runtime
    # conversation/control files are archived.
    max_runtime_restarts_per_section: int = 1
    s2_first_enabled: bool = True
    # Optional Phase-3 targeted request contract.  The coverage worker must
    # consume this contract instead of inferring a new broad search from only
    # a section_id.
    coverage_requests_by_section: Dict[str, Dict[str, Any]] = field(
        default_factory=dict
    )
    # Explicit Phase 3 material bridge.  These fields are optional so legacy
    # section-coverage callers retain their historical behavior.
    shared_kb_sqlite_paths: List[Path] = field(default_factory=list)
    source_ledger_path: Optional[Path] = None
    section_overlay_paths: Dict[str, Path] = field(default_factory=dict)
    selected_paper_ids_by_section: Dict[str, List[str]] = field(default_factory=dict)
    selected_chunk_ids_by_section: Dict[str, List[str]] = field(default_factory=dict)
    selected_permissions_by_section: Dict[str, Dict[str, str]] = field(default_factory=dict)
    selected_content_depths_by_section: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # Deterministic Phase-2 is the primary factory path.  Set this explicitly
    # to False only for a bounded ReAct recovery/debug run.
    short_path_mode: bool = True
    resume_candidate_ledger_path: Optional[Path] = None
    cross_wave_state_path: Optional[Path] = None
    global_coverage_ledger_path: Optional[Path] = None
    article_evidence_portfolio_path: Optional[Path] = None
    # Bounded cross-section fanout for the BIC mainline.  Chapter coverage
    # has no scientific dependency once the immutable blueprint and shared
    # source inputs exist, so independent sections may run concurrently.
    # Each worker writes only section-local staging SQLite/portfolio/ledger
    # artifacts; the main thread merges them into the shared staging SQLite
    # and article manifest in section order through one deterministic writer.
    # Set max_section_workers=1 (or OPTOMIND_SECTION_COVERAGE_WORKERS=1) to
    # restore the historical fully serial path.
    max_section_workers: int = field(
        default_factory=lambda: _resolve_worker_count(
            "OPTOMIND_SECTION_COVERAGE_WORKERS", 3
        )
    )


@dataclass
class SectionCoverageOrchestratorResult:
    run_id: str
    status: str
    sections_total: int
    sections_completed: int
    sections_needing_more_literature: int
    sections_failed: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_cny: float
    work_dir: Path
    material_bundles: Dict[str, SectionMaterialBundle] = field(
        default_factory=dict
    )
    total_cost_basis: str = "unavailable"
    cost_is_estimated: bool = False
    # A recovery view is reconstructed from immutable, already-paid Phase-2
    # artifacts.  It must remain distinguishable from a fresh coverage run so
    # the harness can suppress portfolio retry/acquisition side effects.
    reused_for_phase3_recovery: bool = False
    recovery_telemetry: Dict[str, Any] = field(default_factory=dict)


class SectionCoverageOrchestrator:
    """Run one bounded literature researcher per blueprint section."""

    def __init__(
        self,
        config: SectionCoverageOrchestratorConfig,
        *,
        run_dir: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.run_id = run_dir.name if run_dir else "sco_" + uuid.uuid4().hex[:8]
        self.work_dir = run_dir or config.output_root / self.run_id
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.staging_kb = (
            config.staging_kb_path
            if config.staging_kb_path is not None
            else self.work_dir / "supplemental_oa_kb.sqlite"
        )
        # One portfolio is shared by every section in this article run.  It
        # stores deterministic identities, audit facts and material pointers;
        # it never grants factual permission on its own.
        self.article_evidence_portfolio_path = (
            config.article_evidence_portfolio_path
            if config.article_evidence_portfolio_path is not None
            else self.work_dir / "ARTICLE_EVIDENCE_PORTFOLIO.json"
        )
        self.records: List[Dict[str, Any]] = []

    def run(
        self,
        section_ids: Optional[List[str]] = None,
    ) -> SectionCoverageOrchestratorResult:
        # Rebuild the current manifest from the canonical per-section task
        # directories.  This keeps resume idempotent and prevents duplicated
        # records when a process is restarted.
        self.records = []
        blueprint = _read_json(self.config.blueprint_path)
        review_contract = resolve_review_contract(blueprint)
        sections = [
            {
                **section,
                "topic_identity": dict(
                    blueprint.get("topic_identity", {})
                ),
                "review_quality_contract": review_contract.to_dict(),
                "review_scope_map": dict(
                    blueprint.get("review_scope_map") or {}
                ),
                "_review_section_count": len(
                    [
                        item
                        for item in blueprint.get("sections", [])
                        if isinstance(item, dict)
                    ]
                ),
                "phase3_coverage_request": dict(
                    self.config.coverage_requests_by_section.get(
                        str(section.get("section_id")), {}
                    )
                ),
            }
            for section in blueprint.get("sections", [])
            if isinstance(section, dict)
            and (
                section_ids is None
                or section.get("section_id") in set(section_ids)
            )
        ]
        bundles: Dict[str, SectionMaterialBundle] = {}
        worker_count = self._effective_section_worker_count(sections)
        if worker_count > 1:
            bundles = self._run_sections_parallel(
                sections, worker_count=worker_count
            )
        else:
            for section in sections:
                remaining_stage_budget = round(
                    self.config.stage_cost_budget_cny - self._total_cost_cny(),
                    6,
                )
                if remaining_stage_budget <= 0.25:
                    self.records.append(
                        {
                            "section_id": section.get("section_id", ""),
                            "status": "budget_exhausted",
                            "stop_reason": "stage_cost_budget_reached",
                            "cost_cny": 0.0,
                        }
                    )
                    continue
                record, bundle = self._run_one(
                    section,
                    remaining_stage_budget=remaining_stage_budget,
                )
                self.records.append(record)
                if bundle is not None:
                    bundles[section["section_id"]] = bundle
                self._write_manifest(bundles)
                # Do not repeat a workbench-level failure for every chapter.
                # A permission/runtime configuration error is independent of
                # section content, so another section would only reproduce
                # the same error and consume time/API calls.  Scientific
                # coverage gaps remain section-local and do not trigger this
                # fail-fast path.
                if self._is_systemic_runtime_failure(record):
                    break

        status = self._overall_status(sections)
        result = self._build_result(sections, bundles, status)
        self._write_manifest(bundles, final_status=status)
        return result

    def _effective_section_worker_count(
        self, sections: List[Dict[str, Any]]
    ) -> int:
        if len(sections) <= 1:
            return 1
        try:
            configured = int(self.config.max_section_workers)
        except (TypeError, ValueError):
            configured = 1
        return max(1, min(configured, 16))

    def _overall_status(self, sections: List[Dict[str, Any]]) -> str:
        completed = sum(
            record.get("status") == "completed" for record in self.records
        )
        needs_more = sum(
            record.get("status") == "needs_more_literature"
            for record in self.records
        )
        if completed == len(sections):
            return "completed"
        if completed or needs_more:
            return "partial"
        return "failed"

    def _build_result(
        self,
        sections: List[Dict[str, Any]],
        bundles: Dict[str, SectionMaterialBundle],
        status: str,
    ) -> SectionCoverageOrchestratorResult:
        completed = sum(
            record.get("status") == "completed" for record in self.records
        )
        needs_more = sum(
            record.get("status") == "needs_more_literature"
            for record in self.records
        )
        failed = len(self.records) - completed - needs_more
        result = SectionCoverageOrchestratorResult(
            run_id=self.run_id,
            status=status,
            sections_total=len(sections),
            sections_completed=completed,
            sections_needing_more_literature=needs_more,
            sections_failed=failed,
            total_input_tokens=sum(
                int(record.get("input_tokens", 0)) for record in self.records
            ),
            total_output_tokens=sum(
                int(record.get("output_tokens", 0)) for record in self.records
            ),
            total_cost_cny=self._total_cost_cny(),
            work_dir=self.work_dir,
            material_bundles=bundles,
            total_cost_basis=(
                next(iter({
                    str(record.get("cost_basis") or "unavailable")
                    for record in self.records
                }))
                if len({
                    str(record.get("cost_basis") or "unavailable")
                    for record in self.records
                }) == 1
                else "mixed"
            ),
            cost_is_estimated=any(
                bool(record.get("cost_is_estimated")) for record in self.records
            ),
        )
        return result

    def _run_sections_parallel(
        self,
        sections: List[Dict[str, Any]],
        *,
        worker_count: int,
    ) -> Dict[str, SectionMaterialBundle]:
        """Run independent section workers concurrently with fail-open.

        Each worker writes only section-local staging SQLite, article
        portfolio, global coverage ledger, and cross-wave checkpoint paths.
        The main thread then merges every section-local artifact into the
        shared staging SQLite / article portfolio / global ledger in section
        order through one deterministic writer.  Section workers never open
        the shared SQLite connection or shared manifest.
        """

        stage_budget = max(0.0, float(self.config.stage_cost_budget_cny))
        # Reserve the stage budget evenly so concurrent sections cannot
        # collectively exceed the stage cap; the serial path keeps its exact
        # remaining-budget accounting.
        per_section_stage_budget = max(
            0.01, (stage_budget - 0.25) / max(1, len(sections))
        )
        staging_root = self.work_dir / "_section_staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        for section in sections:
            section_id = str(section.get("section_id") or "")
            if section_id:
                (staging_root / section_id).mkdir(parents=True, exist_ok=True)

        def worker_config_for(
            section_id: str,
        ) -> SectionCoverageOrchestratorConfig:
            worker_root = staging_root / section_id
            return replace(
                self.config,
                staging_kb_path=(
                    worker_root / "supplemental_oa_kb.sqlite"
                ),
                article_evidence_portfolio_path=(
                    worker_root / "ARTICLE_EVIDENCE_PORTFOLIO.json"
                ),
                global_coverage_ledger_path=(
                    worker_root / "COVERAGE_GLOBAL_LEDGER.json"
                ),
                cross_wave_state_path=(
                    worker_root / "COVERAGE_CROSS_WAVE_STATE.json"
                ),
            )

        def run_section_worker(
            section: Dict[str, Any],
        ) -> Dict[str, Any]:
            section_id = str(section.get("section_id") or "")
            worker_config = worker_config_for(section_id)
            try:
                worker = SectionCoverageOrchestrator(
                    worker_config, run_dir=self.work_dir
                )
                record, bundle = worker._run_one(
                    section,
                    remaining_stage_budget=per_section_stage_budget,
                )
                return {
                    "section_id": section_id,
                    "record": record,
                    "bundle": bundle,
                    "staging_kb": worker.staging_kb,
                    "portfolio": worker.article_evidence_portfolio_path,
                    "ledger": worker_config.global_coverage_ledger_path,
                    "error": "",
                }
            except Exception as exc:
                return {
                    "section_id": section_id,
                    "record": {
                        "section_id": section_id,
                        "status": "failed",
                        "worker_status": "parallel_worker",
                        "stop_reason": (
                            "parallel_worker_exception:"
                            f"{type(exc).__name__}:{str(exc)[:240]}"
                        ),
                        "stop_reason_category": "engineering_failure",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_cny": 0.0,
                        "work_dir": str(
                            self.work_dir / "sections" / section_id
                        ),
                        "reused": False,
                        "coverage_budget": self._coverage_budget_metadata(),
                    },
                    "bundle": None,
                    "staging_kb": None,
                    "portfolio": None,
                    "ledger": None,
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                }

        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="section-coverage",
        ) as pool:
            results = list(pool.map(run_section_worker, sections))

        # Records and bundles are consumed in section order so the manifest
        # and downstream consumers see a deterministic record order even
        # though the workers completed out of order.
        #
        # Repair 5: Restore original cost/token totals for reused sections so
        # that resuming a run does not zero out costs already paid in a prior pass.
        merged_records: List[Dict[str, Any]] = []
        for item in results:
            rec = dict(item["record"])
            sid = str(rec.get("section_id") or "")
            if rec.get("reused") and sid and float(rec.get("cost_cny") or 0.0) == 0.0:
                receipt_path = self.work_dir / "sections" / sid / "USAGE_RECEIPT.json"
                prior_receipt = _read_json(receipt_path)
                if isinstance(prior_receipt, dict) and float(
                    prior_receipt.get("cost_cny") or 0.0
                ) > 0.0:
                    rec["cost_cny"] = float(prior_receipt["cost_cny"])
                    rec["input_tokens"] = int(prior_receipt.get("input_tokens") or 0)
                    rec["output_tokens"] = int(prior_receipt.get("output_tokens") or 0)
                    rec["cost_basis"] = str(
                        prior_receipt.get("cost_basis") or "reused_restored"
                    )
                    rec["cost_is_estimated"] = bool(
                        prior_receipt.get("cost_is_estimated")
                    )
                    rec["prior_receipt_restored"] = True
            merged_records.append(rec)
        self.records = merged_records
        bundles: Dict[str, SectionMaterialBundle] = {}
        for item in results:
            if item["bundle"] is not None and item["section_id"]:
                bundles[item["section_id"]] = item["bundle"]

        # One deterministic main-thread writer merges section-local staging
        # SQLite, article portfolio, and global coverage ledger in section
        # order.  Workers never open the shared SQLite or shared manifest.
        for item in results:
            section_id = item["section_id"]
            if not section_id:
                continue
            if item["staging_kb"] and Path(item["staging_kb"]).is_file():
                merge_kb_sqlite_into(
                    self.staging_kb, Path(item["staging_kb"])
                )
            if item["portfolio"] and Path(item["portfolio"]).is_file():
                self._merge_worker_article_portfolio(
                    Path(item["portfolio"]), section_id=section_id
                )
            if item["ledger"] and Path(item["ledger"]).is_file():
                self._merge_worker_coverage_ledger(Path(item["ledger"]))

        merged_staging = self.staging_kb if self.staging_kb.exists() else None
        if merged_staging is not None:
            bundles = {
                section_id: replace(
                    bundle, staging_kb_sqlite=merged_staging
                )
                for section_id, bundle in bundles.items()
            }
        return bundles

    def _merge_worker_article_portfolio(
        self,
        worker_portfolio_path: Path,
        *,
        section_id: str,
    ) -> None:
        shared_path = self.article_evidence_portfolio_path
        shared = _read_json(shared_path)
        if not isinstance(shared, dict) or not shared:
            shared = {
                "schema_version": "phase2.article_evidence_portfolio.v1",
                "topic_fingerprint": "",
                "candidates": [],
                "audits": {},
                "materials": {},
                "section_links": {},
                "telemetry": {},
            }
        worker = _read_json(worker_portfolio_path)
        if not isinstance(worker, dict) or not worker:
            return
        self._merge_worker_article_portfolio_values(
            shared, worker, section_id=section_id
        )
        shared_path.parent.mkdir(parents=True, exist_ok=True)
        shared_path.write_text(
            json.dumps(shared, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _merge_worker_article_portfolio_values(
        shared: Dict[str, Any],
        worker: Dict[str, Any],
        *,
        section_id: str,
    ) -> None:
        """Merge one worker portfolio into the shared portfolio in place.

        Mirrors the registry's sequential merge semantics: candidates are
        collapsed by material identity, rejected audits are never downgraded
        by deferred records, materials union their chunk ids, and telemetry
        counters are summed.
        """

        shared.setdefault("candidates", [])
        shared.setdefault("audits", {})
        shared.setdefault("materials", {})
        shared.setdefault("section_links", {})
        shared.setdefault("telemetry", {})

        shared_by_identity = {
            str(row.get("material_identity") or ""): row
            for row in shared["candidates"]
            if isinstance(row, dict)
        }
        for row in worker.get("candidates") or []:
            if not isinstance(row, dict):
                continue
            identity = str(row.get("material_identity") or "")
            existing = shared_by_identity.get(identity)
            if existing is None:
                merged = {
                    key: value
                    for key, value in row.items()
                    if value not in (None, "", [], {})
                }
                merged["source_sections"] = list(dict.fromkeys([
                    *(merged.get("source_sections") or []),
                    section_id,
                ]))
                shared["candidates"].append(merged)
                shared_by_identity[identity] = merged
                continue
            for key, value in row.items():
                if value in (None, "", [], {}):
                    continue
                if key == "abstract":
                    if len(str(value or "")) >= len(
                        str(existing.get("abstract") or "")
                    ):
                        existing[key] = value
                    continue
                if key in {"source_sections", "roles", "role_fit"}:
                    existing[key] = list(dict.fromkeys([
                        *(existing.get(key) or []),
                        *value,
                    ]))
                    continue
                existing[key] = value
            existing["source_sections"] = list(dict.fromkeys([
                *(existing.get("source_sections") or []),
                section_id,
            ]))

        for identity, audit in (worker.get("audits") or {}).items():
            if not isinstance(audit, dict):
                continue
            existing = shared["audits"].get(identity) or {}
            decision = str(
                audit.get("decision")
                or existing.get("decision")
                or "deferred"
            )
            if (
                str(existing.get("decision") or "") == "rejected"
                and decision == "deferred"
            ):
                decision = "rejected"
            merged = dict(existing)
            for key, value in audit.items():
                if value in (None, "", [], {}):
                    continue
                if key in {"role_fit", "not_usable_for", "source_sections"}:
                    merged[key] = list(dict.fromkeys([
                        *(merged.get(key) or []),
                        *value,
                    ]))
                    continue
                merged[key] = value
            merged["decision"] = decision
            merged["source_sections"] = list(dict.fromkeys([
                *(merged.get("source_sections") or []),
                section_id,
            ]))
            shared["audits"][identity] = merged

        for identity, material in (worker.get("materials") or {}).items():
            if not isinstance(material, dict):
                continue
            existing = shared["materials"].get(identity) or {}
            merged = dict(existing)
            merged["chunk_ids"] = list(dict.fromkeys([
                *(existing.get("chunk_ids") or []),
                *(material.get("chunk_ids") or []),
            ]))
            merged["source_sections"] = list(dict.fromkeys([
                *(existing.get("source_sections") or []),
                *(material.get("source_sections") or []),
                section_id,
            ]))
            merged.setdefault("material_identity", identity)
            merged.setdefault("paper_id", material.get("paper_id") or "")
            shared["materials"][identity] = merged

        for key, value in (worker.get("section_links") or {}).items():
            shared["section_links"][key] = value

        worker_telemetry = worker.get("telemetry") or {}
        for name, amount in worker_telemetry.items():
            try:
                delta = int(amount)
            except (TypeError, ValueError):
                delta = 0
            shared["telemetry"][name] = (
                int(shared["telemetry"].get(name, 0) or 0) + delta
            )

    def _merge_worker_coverage_ledger(self, worker_path: Path) -> None:
        shared_path = self.config.global_coverage_ledger_path
        if shared_path is None:
            return
        shared = _load_coverage_ledger(shared_path)
        worker = _load_coverage_ledger(worker_path)
        for key in ("queries", "audits", "materials"):
            bucket = shared.setdefault(key, {})
            bucket.update(worker.get(key) or {})
        stats = shared.setdefault("stats", {})
        for name, amount in (worker.get("stats") or {}).items():
            try:
                delta = int(amount)
            except (TypeError, ValueError):
                delta = 0
            stats[name] = int(stats.get(name, 0) or 0) + delta
        _save_coverage_ledger(shared_path, shared)

    def _run_deterministic_short_path(
        self,
        context: SectionCoverageContext,
        section: Dict[str, Any],
        *,
        section_work_dir: Path,
        remaining_stage_budget: float,
    ) -> tuple[Dict[str, Any], Optional[SectionMaterialBundle]]:
        """Execute the program-owned Phase-2 state machine.

        The only model operation here is one compact candidate judgement per
        wave.  All bookkeeping, search admission, route permission, reuse,
        materialisation, package writing, and stopping are deterministic.
        """

        from .phase2_phase3_feedback import canonical_material_identity
        from .section_coverage_tool_registry import (
            _audit_call_preflight,
            _candidate_identity,
            _current_wave_index,
            _read_artifact,
            _read_cross_wave_state,
            _restore_candidates_from_ledger,
            _staging_material_for_candidate,
        )

        started = __import__("time").perf_counter()
        token_budget = min(
            int(self.config.token_budget_per_section),
            int(self.config.model_context_budget_per_section),
            SHORT_PATH_CUMULATIVE_CONTEXT_TOKENS,
        )
        per_call_budget = min(
            int(self.config.context_tokens_per_model_call),
            SHORT_PATH_PER_CALL_CONTEXT_TOKENS,
        )
        input_tokens = 0
        output_tokens = 0
        cost_cny = 0.0
        section_cost_budget = min(
            max(0.0, float(self.config.cost_budget_per_section_cny)),
            max(0.0, float(remaining_stage_budget)),
        )
        qwen_calls = 0
        local_audit_calls = 0
        searched_audit_calls = 0
        qwen_failure = ""
        qwen_usage: Dict[str, Any] = {}
        cost_bases: List[str] = []
        cost_estimated_flags: List[bool] = []
        load_result: Dict[str, Any] = {}
        local_result: Dict[str, Any] = {}
        audit_result: Dict[str, Any] = {}
        refresh_result = ""
        validation = ""
        search_receipts: List[Dict[str, Any]] = []
        stop_reason = "deterministic_short_path_complete"

        def as_object(raw: Any) -> Dict[str, Any]:
            if isinstance(raw, dict):
                return dict(raw)
            try:
                value = json.loads(raw)
            except Exception:
                return {}
            return value if isinstance(value, dict) else {}

        def load_candidates() -> List[Dict[str, Any]]:
            data = _read_artifact(section_work_dir, "OA_CANDIDATE_LEDGER.json") or {}
            return [
                dict(item)
                for item in data.get("candidates", [])
                if isinstance(item, dict) and item.get("candidate_id")
            ]

        def eligible_candidates(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            state = _read_cross_wave_state(context)
            attempted_ids = set(state.get("attempted_candidate_ids") or [])
            attempted_identities = set(
                state.get("attempted_material_identities") or []
            )
            outcomes = state.get("candidate_outcomes") or {}
            identity_index = state.get("material_identity_index") or {}
            seen: set[str] = set()
            result: List[Dict[str, Any]] = []
            for item in raw:
                candidate_id = str(item.get("candidate_id") or "")
                identity = _candidate_identity(item) or canonical_material_identity(item)
                dedup_key = identity or candidate_id
                outcome = outcomes.get(candidate_id) or {}
                if not outcome and identity:
                    outcome = next(
                        (
                            outcomes.get(str(previous_id)) or {}
                            for previous_id in identity_index.get(identity, [])
                        ),
                        {},
                    )
                if not candidate_id or dedup_key in seen:
                    continue
                seen.add(dedup_key)
                if candidate_id in attempted_ids or (
                    identity and identity in attempted_identities
                ):
                    continue
                if outcome.get("no_progress"):
                    continue
                if (
                    str(item.get("decision") or "deferred").casefold()
                    not in {"approved", "rejected"}
                    and not _candidate_needs_new_audit_payload(
                        section_work_dir, item
                    )
                ):
                    # A deferred candidate with the same evidence payload was
                    # already audited.  A resume is not a new evidence wave.
                    continue
                if (
                    str(item.get("candidate_action") or "").casefold() == "reject"
                    and str(item.get("decision") or "").casefold() == "rejected"
                ):
                    continue
                existing_paper_id, _ = _staging_material_for_candidate(context, item)
                if existing_paper_id:
                    continue
                item["material_identity"] = identity
                result.append(item)
            result.sort(
                key=lambda item: (
                    str(item.get("decision") or "deferred") != "approved",
                    str(item.get("candidate_action") or "") != "materialize_now",
                    str(item.get("scope_fit") or "") != "direct",
                    -float(item.get("relevance_score") or 0.0),
                    -int(item.get("citation_count") or 0),
                    str(item.get("title") or ""),
                )
            )
            return result

        def package() -> Dict[str, Any]:
            return _read_json(section_work_dir / "SECTION_MATERIAL_PACKAGE.json")

        def ready(value: Dict[str, Any]) -> bool:
            return str(
                value.get("coverage_outcome")
                or value.get("readiness_outcome")
                or ""
            ) in {
                "material_ready",
                "material_ready_with_limits",
                "merge_required",
            }

        def account_usage(
            response: Dict[str, Any],
            *,
            predicted_input: int,
            fallback_output: int = 0,
        ) -> Dict[str, Any]:
            """Normalize and ledger one provider response immediately."""

            nonlocal input_tokens, output_tokens, cost_cny
            usage = normalize_qwen_usage(
                response,
                fallback_input_tokens=predicted_input,
                fallback_output_tokens=fallback_output,
                model_tier=self.config.model_tier,
            )
            cost_bases.append(str(usage.get("cost_basis") or "unavailable"))
            cost_estimated_flags.append(bool(usage.get("cost_is_estimated")))
            input_tokens += int(usage["input_tokens"])
            output_tokens += int(usage["output_tokens"])
            cost_cny += float(usage["cost_cny"])
            _reconcile_batched_audit_usage(context, usage)
            return usage

        def cost_admission_failure(
            phase: str,
            *,
            predicted_input_tokens: int,
            output_reserve_tokens: int,
        ) -> str:
            estimated_next = _estimate_model_call_cost_cny(
                self.config.model_tier,
                predicted_input_tokens=predicted_input_tokens,
                output_reserve_tokens=output_reserve_tokens,
            )
            if estimated_next <= 0:
                return ""
            remaining = max(0.0, section_cost_budget - cost_cny)
            if estimated_next <= remaining + 0.000001:
                return ""
            return (
                f"{phase}_cost_budget_rejected:"
                f"estimated_next_call_cost_cny={estimated_next:.6f};"
                f"remaining_cost_budget_cny={remaining:.6f}"
            )

        # Deterministic setup: load -> local audit -> complete role plan.
        load_result = as_object(_make_load_section_context(context)())
        _record_deterministic_step(
            context,
            "load_section_context",
            status="completed" if load_result.get("status") == "ok" else "failed",
        )
        local_result = as_object(_make_inspect_section_local_coverage(context)())
        _record_deterministic_step(
            context,
            "inspect_section_local_coverage",
            status="completed" if local_result.get("status") == "ok" else "failed",
            details={
                "blocking_gaps": list(local_result.get("blocking_gaps") or []),
                "local_candidates": sum(
                    len(ids or [])
                    for ids in (local_result.get("candidate_ids_by_role") or {}).values()
                    if isinstance(ids, list)
                ),
            },
        )

        existing_plan = _read_artifact(
            section_work_dir, "SECTION_COVERAGE_PLAN.json"
        ) or {}
        existing_roles = existing_plan.get("roles") or {}
        plan_is_complete = (
            set(existing_roles) == set(ROLE_DEFINITIONS)
            and all(
                isinstance(value, dict)
                and str(value.get("priority") or "")
                in {"required", "important", "useful", "not_needed"}
                for value in existing_roles.values()
            )
        )
        if not plan_is_complete:
            adaptive_contract = (
                build_adaptive_coverage_contract(
                    section,
                    section_count=section.get("_review_section_count"),
                )
                if self.config.adaptive_coverage_enabled
                else None
            )
            if adaptive_contract is not None:
                required = set(adaptive_contract.required_roles)
                optional = set(adaptive_contract.optional_roles)
            else:
                required = {
                    str(value).strip().casefold()
                    for value in section.get("required_roles") or []
                    if str(value).strip()
                }
                optional = {
                    str(value).strip().casefold()
                    for value in section.get("optional_roles") or []
                    if str(value).strip()
                }
            required.update(context.targeted_missing_roles)
            components = list(
                (context.phase3_coverage_request or {}).get("missing_components")
                or []
            )
            explicit_targets = [
                *context.targeted_query_targets,
                *[
                    {"query": query, "source": "phase3_request"}
                    for query in context.targeted_queries
                ],
            ]
            section_view = {
                **section,
                "section_id": context.section_id,
                "phase3_coverage_request": context.phase3_coverage_request,
            }
            plan: Dict[str, Any] = {}
            for role in ROLE_DEFINITIONS:
                if role in required:
                    priority = "required"
                elif role in optional:
                    priority = "important"
                elif role in {"foundation", "mechanism", "frontier"}:
                    priority = "useful"
                else:
                    priority = "not_needed"
                role_targets = build_uncovered_query_targets(
                    section_view,
                    roles=[role],
                    components=components,
                    existing_targets=explicit_targets,
                    max_targets=2,
                )
                queries = [
                    str(target.get("query") or "").strip()
                    for target in role_targets
                    if str(target.get("query") or "").strip()
                    and (
                        not target.get("role")
                        or str(target.get("role")) == role
                    )
                ][:2]
                if priority != "not_needed" and not queries:
                    queries = [
                        normalize_scientific_query(
                            scientific_query_anchor(section_view),
                            section_data=section_view,
                            role=role,
                        )
                    ]
                plan[role] = {
                    "priority": priority,
                    "coverage_question": ROLE_DEFINITIONS[role],
                    "intended_synthesis": ROLE_DEFINITIONS[role],
                    "queries": queries if priority != "not_needed" else [],
                    "gap_severity": (
                        "blocking"
                        if priority == "required"
                        else "important"
                        if priority == "important"
                        else "none"
                    ),
                }
            plan_result = as_object(
                _make_submit_literature_role_plan(context)(
                    json.dumps(plan, ensure_ascii=False)
                )
            )
            if plan_result.get("status") != "ok":
                raise RuntimeError(
                    "deterministic_role_plan_failed: "
                    + "; ".join(
                        str(item) for item in plan_result.get("errors") or []
                    )[:400]
                )
            _record_deterministic_step(
                context,
                "build_complete_role_plan",
                details={"roles": list(plan)},
            )
        else:
            _record_deterministic_step(
                context,
                "reuse_complete_role_plan",
                details={"roles": sorted(existing_roles)},
            )

        # The local ledger is the durable recall boundary. Rows that already
        # have a terminal decision (approved, rejected, or deferred with a
        # reason) are reused without another provider call. A paper/chunk may
        # serve several literature roles, so identity includes the role; only
        # an exact paper/chunk/role duplicate is a bookkeeping duplicate.
        local_examined_ids: set[str] = set()
        local_audit_results: List[Dict[str, Any]] = []
        local_audit_result: Dict[str, Any] = {}
        local_audit_failure = ""
        local_candidates_remaining: List[str] = []
        local_approved_by_lane: Dict[str, int] = {
            "direct": 0,
            "adjacent": 0,
            "contextual": 0,
        }
        local_approved_roles: set[str] = set()
        local_no_gain_batches = 0
        local_stop_reason = ""
        local_network_search_needed = False

        def local_identity(row: Dict[str, Any]) -> str:
            return (
                "|".join(
                    str(row.get(key) or "")
                    for key in ("paper_id", "chunk_id", "role")
                )
                or str(row.get("candidate_id") or "")
            )

        def nonterminal_local_rows() -> List[Dict[str, Any]]:
            ledger = _read_artifact(
                section_work_dir, "LOCAL_CANDIDATE_LEDGER.json"
            ) or {}
            rows: List[Dict[str, Any]] = []
            for item in ledger.get("candidates", []):
                if not isinstance(item, dict) or not item.get("candidate_id"):
                    continue
                row = dict(item)
                decision = str(row.get("decision") or "deferred").casefold()
                audit_reason = str(row.get("audit_reason") or "").strip()
                if decision in {"approved", "rejected"} or audit_reason:
                    continue
                rows.append(row)
            rows.sort(
                key=lambda row: (
                    str(row.get("scope_fit") or "unreviewed") != "direct",
                    -float(row.get("relevance_score") or 0.0),
                    -len(row.get("topic_matches") or []),
                    -len(row.get("role_matches") or []),
                    -int(row.get("citation_count") or 0),
                    str(row.get("candidate_id") or ""),
                )
            )
            return rows

        def terminalize_duplicate_local_identities(
            rows: List[Dict[str, Any]],
        ) -> None:
            first_by_identity: Dict[str, Dict[str, Any]] = {}
            duplicates: List[Dict[str, Any]] = []
            for row in rows:
                identity = local_identity(row)
                if identity in first_by_identity:
                    duplicates.append(row)
                else:
                    first_by_identity[identity] = row
            if not duplicates:
                return
            records = [
                {
                    "candidate_id": row.get("candidate_id"),
                    "scope_fit": "unreviewed",
                    "decision": "deferred",
                    "audit_reason": "duplicate_paper_chunk_role_identity_in_local_pass",
                    "not_usable_for": [],
                }
                for row in duplicates
            ]
            _make_submit_local_source_audit(context)(
                json.dumps(records, ensure_ascii=False)
            )

        def remaining_local_ids() -> List[str]:
            return [
                str(row.get("candidate_id") or "")
                for row in nonterminal_local_rows()
            ]

        def local_audit_envelope(row: Dict[str, Any]) -> Dict[str, Any]:
            return {
                **row,
                "material_identity": (
                    str(row.get("material_identity") or "")
                    or local_identity(row)
                ),
                "abstract": str(
                    row.get("abstract") or row.get("text_preview") or ""
                ),
                "is_oa": bool(row.get("is_oa")),
                "candidate_route": str(
                    row.get("candidate_route")
                    or row.get("materialization_route")
                    or "local_fulltext"
                ),
            }

        def all_local_rows() -> List[Dict[str, Any]]:
            ledger = _read_artifact(
                section_work_dir, "LOCAL_CANDIDATE_LEDGER.json"
            ) or {}
            return [
                dict(item)
                for item in ledger.get("candidates", [])
                if isinstance(item, dict) and item.get("candidate_id")
            ]

        initial_nonterminal = nonterminal_local_rows()
        local_candidates_discovered = len(
            {local_identity(row) for row in all_local_rows()}
        )
        terminalize_duplicate_local_identities(initial_nonterminal)
        eligible_local_identity_count = len(
            {local_identity(row) for row in nonterminal_local_rows()}
        )
        local_candidates_ranked = eligible_local_identity_count
        max_local_audit_calls = (
            (
                eligible_local_identity_count
                + LOCAL_AUDIT_MAX_BATCH_CANDIDATES
                - 1
            )
            // LOCAL_AUDIT_MAX_BATCH_CANDIDATES
            if eligible_local_identity_count
            else 0
        )
        local_per_call_budget = min(
            int(self.config.context_tokens_per_model_call),
            LOCAL_AUDIT_PER_CALL_CONTEXT_TOKENS,
        )
        local_cumulative_budget = min(
            int(self.config.token_budget_per_section),
            int(self.config.model_context_budget_per_section),
            LOCAL_AUDIT_CUMULATIVE_CONTEXT_TOKENS,
        )
        _record_deterministic_step(
            context,
            "prepare_local_source_audit_batch",
            details={
                "candidate_count": len(
                    set(
                        local_identity(row)
                        for row in nonterminal_local_rows()
                    )
                ),
                "recovered_inspected_unjudged": len(nonterminal_local_rows()),
            },
        )

        # Local-first multi-batch routing: rank the full local pool, examine it
        # in token-sized batches, and continue past coverage readiness so
        # adjacent/contextual material is preserved instead of being discarded
        # after a fixed two-call ceiling.  Stopping is marginal-gain driven.
        while True:
            queue = nonterminal_local_rows()
            if not queue:
                local_stop_reason = "local_pool_exhausted"
                break
            remaining_count = len(queue)
            plan = plan_local_audit_batch(
                section={**section, "section_id": context.section_id},
                candidates=[
                    local_audit_envelope(row) for row in queue
                ],
                wave_index=_current_wave_index(context),
                per_call_budget_tokens=local_per_call_budget,
                output_base_tokens=LOCAL_AUDIT_OUTPUT_BASE_TOKENS,
                output_tokens_per_candidate=(
                    LOCAL_AUDIT_OUTPUT_TOKENS_PER_CANDIDATE
                ),
                output_hard_cap_tokens=LOCAL_AUDIT_OUTPUT_HARD_CAP,
                max_candidates=LOCAL_AUDIT_MAX_BATCH_CANDIDATES,
                components=(context.phase3_coverage_request or {}).get(
                    "missing_components", []
                ),
                covered_roles=sorted(local_approved_roles),
                retained_lanes=sorted(
                    lane
                    for lane, count in local_approved_by_lane.items()
                    if count > 0
                ),
                remaining_candidate_count=remaining_count,
            )
            batch = plan["batch"]
            if not batch:
                local_audit_failure = (
                    "local_audit_budget_rejected:"
                    "no_candidate_fits_per_call_budget"
                )
                local_stop_reason = local_audit_failure
                _record_deterministic_step(
                    context,
                    "local_batched_candidate_audit",
                    status="budget_rejected",
                    details={
                        "reason": "per_call_context_budget_exceeded",
                        "remaining_candidates": remaining_count,
                    },
                )
                break
            compact_payload = plan["payload"]
            predicted_input = plan["predicted_input_tokens"]
            audit_output_allowance = plan["output_allowance_tokens"]
            batch_ids = [
                str(row.get("candidate_id") or "") for row in batch
            ]
            # Persist the delta inspection contract for this batch.  The
            # inspection limit is raised for local batches so the fingerprint
            # ledger matches what the model actually judged.
            _make_inspect_local_candidate_batch(context)(
                json.dumps(batch_ids, ensure_ascii=False)
            )
            admission = admit_context_call(
                predicted_input_tokens=predicted_input,
                output_reserve_tokens=audit_output_allowance,
                cumulative_input_tokens=input_tokens,
                cumulative_budget_tokens=local_cumulative_budget,
                per_call_budget_tokens=local_per_call_budget,
            )
            if not admission.admitted:
                local_audit_failure = (
                    "local_audit_budget_rejected:" + str(admission.reason)
                )
                local_stop_reason = local_audit_failure
                _record_deterministic_step(
                    context,
                    "local_batched_candidate_audit",
                    status="budget_rejected",
                    details={
                        "reason": admission.reason,
                        "remaining_candidates": remaining_count,
                    },
                )
                break
            cost_failure = cost_admission_failure(
                "local_audit",
                predicted_input_tokens=predicted_input,
                output_reserve_tokens=audit_output_allowance,
            )
            if cost_failure:
                local_audit_failure = cost_failure
                local_stop_reason = local_audit_failure
                _record_deterministic_step(
                    context,
                    "local_batched_candidate_audit",
                    status="budget_rejected",
                    details={
                        "reason": cost_failure,
                        "remaining_candidates": remaining_count,
                        "estimated_input_tokens": predicted_input,
                        "output_reserve_tokens": audit_output_allowance,
                    },
                )
                break
            local_examined_ids.update(batch_ids)
            _record_deterministic_step(
                context,
                "local_batched_candidate_audit",
                details={
                    "candidate_count": len(batch),
                    "predicted_input_tokens": predicted_input,
                    "output_cap_tokens": audit_output_allowance,
                    "batch_ordinal": len(local_audit_results) + 1,
                },
            )

            attempt = 0
            last_exc: Optional[Exception] = None
            local_records: List[Dict[str, Any]] = []
            repair_warnings: List[str] = []
            while attempt <= LOCAL_AUDIT_MALFORMED_RETRY_LIMIT:
                attempt += 1
                local_audit_calls += 1
                qwen_calls += 1
                try:
                    from llm.qwen_chat_client import call_qwen_chat

                    prompt_path = (
                        PROJECT_ROOT
                        / "prompts"
                        / "Phase 2 Short Path Candidate Auditor.txt"
                    )
                    system_prompt = (
                        prompt_path.read_text(encoding="utf-8")
                        if prompt_path.exists()
                        else (
                            "Audit this candidate batch for scope and legal "
                            "acquisition. Return JSON only."
                        )
                    )
                    response = call_qwen_chat(
                        "Phase2ShortPathCandidateAuditor",
                        [
                            {"role": "system", "content": system_prompt},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    compact_payload, ensure_ascii=False
                                ),
                            },
                        ],
                        model_tier=self.config.model_tier,
                        temperature=0,
                        max_tokens=audit_output_allowance,
                        response_format={"type": "json_object"},
                        force_mock=False,
                        max_retries=1,
                    )
                    qwen_usage = account_usage(
                        response,
                        predicted_input=predicted_input,
                        fallback_output=estimate_json_tokens(
                            str(response.get("content") or "")
                        ),
                    )
                    raw_content = response.get("content") or "[]"
                    if isinstance(raw_content, str):
                        decoded = decode_json_payload(
                            raw_content, expected="any"
                        )
                        if decoded.error:
                            raise ValueError(decoded.error)
                        parsed = decoded.value
                    elif isinstance(raw_content, (dict, list)):
                        parsed = raw_content
                    else:
                        raise ValueError(
                            "model response content must be JSON text, object, or list"
                        )
                    repaired, repair_errors = normalize_local_audit_records(
                        parsed, batch
                    )
                    if not repaired:
                        raise ValueError(
                            "provider_response_malformed: "
                            "no candidate records could be repaired"
                        )
                    local_records = repaired
                    repair_warnings = repair_errors
                    break
                except Exception as exc:
                    last_exc = exc
                    _record_deterministic_step(
                        context,
                        "local_batched_candidate_audit",
                        status=(
                            "retry"
                            if attempt <= LOCAL_AUDIT_MALFORMED_RETRY_LIMIT
                            else "gap"
                        ),
                        details={
                            "attempt": attempt,
                            "candidate_count": len(batch),
                            "error": f"{type(exc).__name__}:{str(exc)[:180]}",
                        },
                    )
            if not local_records:
                local_audit_failure = (
                    "local_audit_gap: provider_response_malformed:"
                    f"{type(last_exc).__name__}:{str(last_exc)[:180]}"
                )
                local_stop_reason = local_audit_failure
                break

            batch_result = as_object(
                _make_submit_local_source_audit(context)(
                    json.dumps(local_records, ensure_ascii=False)
                )
            )
            local_audit_results.append(batch_result)
            local_audit_result = batch_result
            _record_deterministic_step(
                context,
                "local_source_audit_submitted",
                status=(
                    "completed"
                    if batch_result.get("status") == "ok"
                    else "partial"
                ),
                details={
                    "records": len(local_records),
                    "approved": int(batch_result.get("approved") or 0),
                    "repair_warnings": repair_warnings[:5],
                },
            )

            # Track marginal coverage gain by lane/role so the next stop
            # decision is semantic, not a fixed tiny candidate count.
            row_by_id = {
                str(row.get("candidate_id") or ""): row for row in batch
            }
            lanes_before = {
                lane
                for lane, count in local_approved_by_lane.items()
                if count > 0
            }
            roles_before = set(local_approved_roles)
            marginal_semantic_gain = 0.0
            approved_max_score = 0.0
            for record in local_records:
                if str(record.get("decision") or "") != "approved":
                    continue
                lane = str(record.get("scope_fit") or "")
                roles = [
                    str(item).strip().casefold()
                    for item in (record.get("role_fit") or [])
                    if str(item).strip()
                ]
                if not roles:
                    source_row = row_by_id.get(
                        str(record.get("candidate_id") or ""), {}
                    )
                    roles = [
                        str(source_row.get("role") or "foundation")
                        .strip()
                        .casefold()
                    ]
                score = record.get("semantic_score")
                if isinstance(score, (int, float)) and not isinstance(
                    score, bool
                ):
                    score = max(0.0, min(1.0, float(score)))
                    approved_max_score = max(approved_max_score, score)
                    if lane not in lanes_before or any(
                        role not in roles_before for role in roles
                    ):
                        marginal_semantic_gain = max(
                            marginal_semantic_gain, score
                        )
                if lane in local_approved_by_lane:
                    local_approved_by_lane[lane] += 1
                local_approved_roles.update(roles)
            if marginal_semantic_gain <= 0.0 and approved_max_score > 0.0:
                marginal_semantic_gain = approved_max_score
            new_lanes = len(
                {
                    lane
                    for lane, count in local_approved_by_lane.items()
                    if count > 0
                }
                - lanes_before
            )
            new_roles = len(local_approved_roles - roles_before)
            if (
                new_lanes == 0
                and new_roles == 0
                and marginal_semantic_gain < LOCAL_AUDIT_MIN_SEMANTIC_GAIN
            ):
                local_no_gain_batches += 1
            else:
                local_no_gain_batches = 0

            refresh_result = _make_refresh_section_coverage(context)()
            validation = _make_validate_section_coverage_package(context)()
            _record_deterministic_step(
                context,
                "local_source_audit_refresh",
                status=(
                    "completed"
                    if "VALIDATION_PASSED" in validation
                    else "open"
                ),
            )
            stop_decision = evaluate_local_audit_stop(
                examined_count=len(local_examined_ids),
                soft_candidate_target=LOCAL_AUDIT_SOFT_CANDIDATE_TARGET,
                remaining_candidates=nonterminal_local_rows(),
                last_batch_new_lanes=new_lanes,
                last_batch_new_roles=new_roles,
                last_batch_semantic_gain=marginal_semantic_gain,
                no_gain_batches=local_no_gain_batches,
            )
            if stop_decision.stop:
                local_stop_reason = stop_decision.reason
                break

        local_candidates_remaining = remaining_local_ids()
        local_network_search_needed = bool(
            not ready(package()) and not local_audit_failure
        )
        _record_deterministic_step(
            context,
            "local_source_audit_queue",
            details={
                "discovered_candidates": local_candidates_discovered,
                "ranked_candidates": local_candidates_ranked,
                "examined_candidates": len(local_examined_ids),
                "retained_by_lane": dict(local_approved_by_lane),
                "batches": len(local_audit_results),
                "remaining_candidates": len(local_candidates_remaining),
                "stop_reason": local_stop_reason or "local_audit_complete",
                "network_search_needed": local_network_search_needed,
                "failure": local_audit_failure,
            },
        )

        refresh_result = _make_refresh_section_coverage(context)()
        validation = _make_validate_section_coverage_package(context)()
        _record_deterministic_step(
            context,
            "local_material_checkpoint",
            status="completed" if "VALIDATION_PASSED" in validation else "open",
        )

        query_targets = _coverage_query_targets(context)
        _record_deterministic_step(
            context,
            "identify_component_role_gaps",
            details={
                "roles": list(dict.fromkeys(
                    str(target.get("role") or "")
                    for target in query_targets
                    if str(target.get("role") or "")
                )),
                "components": list(dict.fromkeys(
                    str(component)
                    for target in query_targets
                    for component in (
                        target.get("components")
                        or target.get("missing_components")
                        or []
                    )
                    if str(component).strip()
                )),
            },
        )
        _record_deterministic_step(
            context,
            "generate_role_specific_queries",
            details={"query_count": len(query_targets)},
        )
        target_groups: List[tuple[str, List[str]]] = []
        target_group_components: Dict[str, set[str]] = {}
        for target in query_targets:
            query = str(target.get("query") or "").strip()
            if not query:
                continue
            role = str(target.get("role") or "").strip().casefold()
            if role not in ROLE_DEFINITIONS:
                role = (
                    context.targeted_missing_roles
                    or list(section.get("required_roles") or [])
                    or ["foundation"]
                )[0]
            group = next((item for item in target_groups if item[0] == role), None)
            if group is None:
                target_groups.append((role, [query]))
            elif query not in group[1] and len(group[1]) < self.config.max_queries_per_call:
                group[1].append(query)
            target_group_components.setdefault(role, set()).update(
                str(item).strip().casefold()
                for item in (
                    target.get("components")
                    or target.get("missing_components")
                    or []
                )
                if str(item).strip()
            )
        if not target_groups:
            role = (
                context.targeted_missing_roles
                or list(section.get("required_roles") or [])
                or ["foundation"]
            )[0]
            target_groups = [
                (
                    role,
                    [
                        normalize_scientific_query(
                            scientific_query_anchor(section),
                            section_data=section,
                            role=role,
                        )
                    ],
                )
            ]
        searched_queries: set[str] = set()
        searched_roles: set[str] = set()
        searched_components: set[str] = set()
        prior_search = _read_artifact(section_work_dir, "SEARCH_BUDGET_LEDGER.json") or {}
        for row in prior_search.get("rounds", []) or []:
            if isinstance(row, dict):
                prior_role = str(row.get("role") or "").strip().casefold()
                if prior_role:
                    searched_roles.add(prior_role)
                for target in row.get("query_targets") or []:
                    if isinstance(target, dict):
                        searched_components.update(
                            str(item).strip().casefold()
                            for item in (
                                target.get("components")
                                or target.get("missing_components")
                                or []
                            )
                            if str(item).strip()
                        )
                searched_queries.update(
                    " ".join(str(query).casefold().split())
                    for query in row.get("queries") or []
                    if str(query).strip()
                )

        searched_base_wave = _current_wave_index(context)

        # At most two controller waves.  Search is invoked only when no novel
        # audited/materializable candidate is already available.
        for wave_number in range(SHORT_PATH_MAX_WAVES):
            if local_audit_failure:
                # A malformed or budget-rejected local batch is an engineering
                # boundary. Do not mislabel it scientific exhaustion and do
                # not bypass the remaining local queue with OA search.
                stop_reason = local_audit_failure
                break
            # Each controller wave within one request must get its own
            # effective admission namespace: the request's durable base wave
            # plus this controller offset. This preserves the fresh base used
            # by a targeted retry while preventing wave 1 from being mistaken
            # for a second audit of wave 0.
            effective_wave_index = searched_base_wave + wave_number
            base_request = dict(context.phase3_coverage_request or {})
            context.phase3_coverage_request = {
                **base_request,
                "wave_index": effective_wave_index,
            }
            _restore_candidates_from_ledger(context)
            candidates = eligible_candidates(load_candidates())
            if ready(package()):
                stop_reason = "coverage_outcome_reached_before_next_wave"
                break

            selected_group: Optional[tuple[str, List[str]]] = None
            if not candidates:
                novel_groups = [
                    group
                    for group in target_groups
                    if any(
                        " ".join(query.casefold().split()) not in searched_queries
                        for query in group[1]
                    )
                ]
                if wave_number == 0:
                    selected_group = novel_groups[0] if novel_groups else None
                else:
                    # A second bounded wave must open a different inferential
                    # route.  Prefer an uncovered role, then a genuinely
                    # uncovered component; never spend the wave replaying the
                    # first role plus one generic token.
                    selected_group = next(
                        (
                            group for group in novel_groups
                            if group[0] not in searched_roles
                        ),
                        None,
                    )
                    if selected_group is None:
                        selected_group = next(
                            (
                                group for group in novel_groups
                                if target_group_components.get(group[0], set())
                                - searched_components
                            ),
                            None,
                        )
            if selected_group is not None:
                role, queries = selected_group
                try:
                    raw_search = _make_search_oa_candidates(context)(
                        role,
                        json.dumps(queries, ensure_ascii=False),
                        max_per_backend=min(
                            self.config.max_results_per_backend,
                            SHORT_PATH_SEARCH_AUDIT_CANDIDATES,
                        ),
                    )
                    search_receipt = as_object(raw_search)
                except Exception as exc:
                    search_receipt = {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                        "role": role,
                        "queries_used": queries,
                    }
                search_receipts.append(search_receipt)
                searched_queries.update(
                    " ".join(query.casefold().split()) for query in queries
                )
                searched_roles.add(role)
                searched_components.update(target_group_components.get(role, set()))
                _record_deterministic_step(
                    context,
                    "search_oa_candidates",
                    status="completed" if search_receipt.get("status") == "ok" else "gap",
                    details={
                        "wave": wave_number,
                        "role": role,
                        "queries": queries,
                        "result_count": int(search_receipt.get("result_count", 0) or 0),
                        "error_code": str(search_receipt.get("error_code") or ""),
                    },
                )
            else:
                _record_deterministic_step(
                    context,
                    "search_oa_candidates",
                    status="skipped",
                    details={"wave": wave_number, "reason": "no_novel_target_or_candidate"},
                )

            _restore_candidates_from_ledger(context)
            candidates = eligible_candidates(load_candidates())
            candidate_ids = [
                str(item.get("candidate_id"))
                for item in candidates[:SHORT_PATH_SEARCH_AUDIT_CANDIDATES]
            ]
            inspection: Dict[str, Any] = {}
            if candidate_ids:
                inspection = as_object(
                    _make_inspect_candidate_batch(context)(
                        json.dumps(candidate_ids, ensure_ascii=False)
                    )
                )
            _record_deterministic_step(
                context,
                "inspect_candidate_batch",
                details={
                    "wave": wave_number,
                    "candidate_count": len(candidate_ids),
                    "payload_tokens": int(inspection.get("estimated_input_tokens", 0) or 0),
                },
            )

            pending = [
                item
                for item in candidates[:SHORT_PATH_SEARCH_AUDIT_CANDIDATES]
                if str(item.get("decision") or "deferred")
                not in {"approved", "rejected"}
            ]
            audit_records: List[Dict[str, Any]] = []
            if pending and searched_audit_calls < SHORT_PATH_MAX_MODEL_CALLS:
                compact_payload = build_compact_batched_audit_payload(
                    section={**section, "section_id": context.section_id},
                    candidates=inspection.get("candidates") or pending,
                    wave_index=_current_wave_index(context),
                    max_candidates=SHORT_PATH_SEARCH_AUDIT_CANDIDATES,
                    components=(context.phase3_coverage_request or {}).get("missing_components", []),
                )
                predicted_input = estimate_json_tokens(compact_payload)
                audit_output_allowance = _bounded_audit_output_tokens(
                    len(pending)
                )
                # ``_audit_call_preflight`` reads the reserve from this context
                # field. Keep it byte-for-byte identical to the allowance that
                # will be passed to the provider as ``max_tokens`` so admission
                # and the actual call cannot disagree.
                context.context_output_reserve_tokens = audit_output_allowance
                admission = _audit_call_preflight(
                    context,
                    [str(item.get("candidate_id")) for item in pending],
                    predicted_input,
                )
                cost_failure = (
                    cost_admission_failure(
                        "searched_audit",
                        predicted_input_tokens=predicted_input,
                        output_reserve_tokens=audit_output_allowance,
                    )
                    if admission.admitted
                    else ""
                )
                if admission.admitted and not cost_failure:
                    qwen_calls += 1
                    searched_audit_calls += 1
                    _record_deterministic_step(
                        context,
                        "batched_candidate_audit",
                        details={
                            "wave": wave_number,
                            "candidate_count": len(pending),
                            "predicted_input_tokens": predicted_input,
                            "output_reserve_tokens": audit_output_allowance,
                            "searched_audit_call": searched_audit_calls,
                        },
                    )
                    try:
                        from llm.qwen_chat_client import call_qwen_chat

                        prompt_path = PROJECT_ROOT / "prompts" / "Phase 2 Short Path Candidate Auditor.txt"
                        system_prompt = (
                            prompt_path.read_text(encoding="utf-8")
                            if prompt_path.exists()
                            else "Audit this candidate batch for scope and legal acquisition. Return JSON only."
                        )
                        response = call_qwen_chat(
                            "Phase2ShortPathCandidateAuditor",
                            [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": json.dumps(compact_payload, ensure_ascii=False)},
                            ],
                            model_tier=self.config.model_tier,
                            temperature=0,
                            max_tokens=audit_output_allowance,
                            response_format={"type": "json_object"},
                            force_mock=False,
                            max_retries=1,
                        )
                        qwen_usage = account_usage(
                            response,
                            predicted_input=predicted_input,
                            fallback_output=estimate_json_tokens(
                                str(response.get("content") or "")
                            ),
                        )
                        raw_content = response.get("content") or "[]"
                        if isinstance(raw_content, str):
                            decoded = decode_json_payload(raw_content, expected="any")
                            if decoded.error:
                                raise ValueError(decoded.error)
                            parsed = decoded.value
                        elif isinstance(raw_content, (dict, list)):
                            parsed = raw_content
                        else:
                            raise ValueError("model response content must be JSON text, object, or list")
                        audit_records = parsed.get("candidates", parsed) if isinstance(parsed, dict) else parsed
                        if not isinstance(audit_records, list):
                            raise ValueError("candidate audit response must contain a list")
                        _record_deterministic_step(
                            context,
                            "batched_candidate_audit_result",
                            details={"wave": wave_number, "records": len(audit_records)},
                        )
                    except Exception as exc:
                        qwen_failure = f"{type(exc).__name__}: {str(exc)[:240]}"
                        _record_deterministic_step(
                            context,
                            "batched_candidate_audit_result",
                            status="gap",
                            details={"wave": wave_number, "error": qwen_failure},
                        )
                else:
                    qwen_failure = (
                        cost_failure
                        if cost_failure
                        else (
                        admission.reason
                        if not admission.admitted
                        else "per_call_context_budget_exceeded"
                        )
                    )
                    _record_deterministic_step(
                        context,
                        "batched_candidate_audit",
                        status="budget_rejected",
                        details={"wave": wave_number, "reason": qwen_failure},
                    )

            if audit_records:
                audit_result = as_object(
                    _make_submit_candidate_audit(context)(
                        json.dumps(audit_records, ensure_ascii=False)
                    )
                )
            elif candidates and not pending:
                replay = [
                    {
                        "candidate_id": item.get("candidate_id"),
                        "scope_fit": item.get("scope_fit", "unreviewed"),
                        "role_fit": item.get("role_fit") or [item.get("role", "foundation")],
                        "decision": item.get("decision", "deferred"),
                        "candidate_decision": item.get("candidate_action", ""),
                        "audit_reason": item.get("audit_reason") or "durable_audit_replayed",
                        "not_usable_for": item.get("not_usable_for", []),
                    }
                    for item in candidates[:SHORT_PATH_SEARCH_AUDIT_CANDIDATES]
                ]
                audit_result = as_object(
                    _make_submit_candidate_audit(context)(
                        json.dumps(replay, ensure_ascii=False)
                    )
                )
            else:
                audit_result = {
                    "status": "no_candidates" if not candidates else "audit_unavailable",
                    "error": qwen_failure,
                }

            refresh_result = _make_refresh_section_coverage(context)()
            validation = _make_validate_section_coverage_package(context)()
            if ready(package()):
                stop_reason = "coverage_outcome_reached"
                break
            if qwen_failure:
                stop_reason = "batched_audit_gap: " + qwen_failure
                break
            if not candidates and selected_group is None:
                stop_reason = "bounded_waves_exhausted_without_candidates"
                break
            if selected_group is None and not pending:
                stop_reason = "bounded_waves_exhausted_without_new_audit"
                break

        if not ready(package()):
            if not stop_reason or stop_reason == "deterministic_short_path_complete":
                stop_reason = (
                    "batched_audit_gap: " + qwen_failure
                    if qwen_failure
                    else "no_candidates_after_bounded_search"
                )
            self._document_short_path_gaps(
                context,
                section,
                stop_reason=stop_reason,
                query_targets=_coverage_query_targets(context),
                candidates_found=len(load_candidates()),
                search_receipts=search_receipts,
            )
            refresh_result = _make_refresh_section_coverage(context)()
            validation = _make_validate_section_coverage_package(context)()

        # Coverage readiness after a broad local audit is a scientific
        # completion even when the local loop stopped on marginal gain or
        # pool exhaustion; it is not a reason to spend network search.
        if ready(package()) and not local_audit_failure:
            stop_reason = "local_coverage_outcome_reached_after_local_audit"

        stop_telemetry = _record_short_path_stop(
            context,
            stop_reason,
            details={
                "validation_passed": "VALIDATION_PASSED" in validation,
                "qwen_calls": qwen_calls,
                "local_audit_calls": local_audit_calls,
                "searched_audit_calls": searched_audit_calls,
                "local_candidates_discovered": local_candidates_discovered,
                "local_candidates_ranked": local_candidates_ranked,
                "local_candidates_examined": len(local_examined_ids),
                "local_candidates_retained_by_lane": dict(
                    local_approved_by_lane
                ),
                "local_batches": len(local_audit_results),
                "local_stop_reason": local_stop_reason
                or "local_audit_complete",
                "local_candidates_unexamined": len(
                    local_candidates_remaining
                ),
                "local_candidates_remaining": len(local_candidates_remaining),
                "network_search_needed": local_network_search_needed,
                "search_calls": len(search_receipts),
            },
        )
        stop_reason_category = str(
            stop_telemetry.get("stop_reason_category")
            or _stop_reason_category(stop_reason)
        )
        _sync_article_portfolio_telemetry(context)

        usage_receipt = _build_section_usage_receipt(
            section_work_dir=section_work_dir,
            model_tier=self.config.model_tier,
            qwen_usage=qwen_usage,
            qwen_calls=qwen_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=cost_cny,
            cost_bases=cost_bases,
            cost_estimated_flags=cost_estimated_flags,
        )
        input_tokens = int(usage_receipt["input_tokens"])
        output_tokens = int(usage_receipt["output_tokens"])
        cost_cny = float(usage_receipt["cost_cny"])

        package_path = section_work_dir / "SECTION_MATERIAL_PACKAGE.json"
        ledger_path = section_work_dir / "SECTION_SOURCE_LEDGER.json"
        package_data = _read_json(package_path)
        status = self._status_from_package(package_data) if package_data else "failed"
        result_payload = {
            "schema_version": "phase2.1.short_path_result.v1",
            "status": status,
            "worker_status": "deterministic_short_path",
            "structural_task_complete": bool(package_data),
            "validation_passed": "VALIDATION_PASSED" in validation,
            "coverage_outcome": package_data.get("coverage_outcome", "") if package_data else "",
            "stop_reason": stop_reason,
            "stop_reason_category": stop_reason_category,
            "scientific_exhaustion": stop_reason_category == "scientific_exhaustion",
            "engineering_failure": stop_reason_category == "engineering_failure",
            "short_path": True,
            "react_loop_entered": False,
            "model_calls": qwen_calls,
            "local_audit_calls": local_audit_calls,
            "searched_audit_calls": searched_audit_calls,
            "local_candidates_discovered": local_candidates_discovered,
            "local_candidates_ranked": local_candidates_ranked,
            "local_candidates_examined": len(local_examined_ids),
            "local_candidates_retained_by_lane": dict(
                local_approved_by_lane
            ),
            "local_batches": len(local_audit_results),
            "local_stop_reason": local_stop_reason or "local_audit_complete",
            "local_candidates_unexamined": len(local_candidates_remaining),
            "network_search_needed": local_network_search_needed,
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "estimated_cost_cny": round(cost_cny, 6),
            "cost_basis": usage_receipt["cost_basis"],
            "cost_is_estimated": usage_receipt["cost_is_estimated"],
            "usage_receipt_id": usage_receipt["receipt_id"],
            "usage_receipt_path": usage_receipt["receipt_path"],
            "model_tier": usage_receipt["model_tier"],
            "model_name": usage_receipt["model_name"],
            "coverage_telemetry": self._coverage_telemetry(section_work_dir),
        }
        (section_work_dir / "RESULT.json").write_text(
            json.dumps(result_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summary = {
            "schema_version": "phase2.1.short_path_run.v2",
            "section_id": context.section_id,
            "status": status,
            "qwen_calls": qwen_calls,
            "local_audit_calls": local_audit_calls,
            "searched_audit_calls": searched_audit_calls,
            "local_candidates_discovered": local_candidates_discovered,
            "local_candidates_ranked": local_candidates_ranked,
            "local_candidates_examined": len(local_examined_ids),
            "local_candidates_retained_by_lane": dict(
                local_approved_by_lane
            ),
            "local_batches": len(local_audit_results),
            "local_stop_reason": local_stop_reason or "local_audit_complete",
            "local_candidates_unexamined": len(local_candidates_remaining),
            "network_search_needed": local_network_search_needed,
            "local_candidates_remaining": local_candidates_remaining,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_cny": round(cost_cny, 6),
            "cost_basis": usage_receipt["cost_basis"],
            "cost_is_estimated": usage_receipt["cost_is_estimated"],
            "usage_receipt_id": usage_receipt["receipt_id"],
            "usage_receipt_path": usage_receipt["receipt_path"],
            "model_tier": usage_receipt["model_tier"],
            "model_name": usage_receipt["model_name"],
            "token_budget": token_budget,
            "max_model_calls": (
                max_local_audit_calls + SHORT_PATH_MAX_MODEL_CALLS
            ),
            "max_local_audit_calls": max_local_audit_calls,
            "max_searched_audit_calls": SHORT_PATH_MAX_MODEL_CALLS,
            "max_coverage_waves": SHORT_PATH_MAX_WAVES,
            "max_audit_calls": SHORT_PATH_MAX_AUDIT_CALLS,
                "token_admission": {
                    "predicted_input_plus_reserve_checked": True,
                    "per_call_budget_tokens": per_call_budget,
                    "cumulative_budget_tokens": token_budget,
                    "local_per_call_budget_tokens": local_per_call_budget,
                    "local_cumulative_budget_tokens": local_cumulative_budget,
                    "cost_budget_cny": round(section_cost_budget, 6),
                    "rejected_reason": local_audit_failure or qwen_failure,
                },
            "load_context": load_result,
            "local_audit": local_result,
            "local_source_audit": local_audit_result,
            "local_source_audit_failure": local_audit_failure,
            "local_source_audit_batches": local_audit_results,
            "candidate_audit": audit_result,
            "search_receipts": search_receipts,
            "post_audit_transition": _read_json(section_work_dir / "POST_AUDIT_TRANSITION.json"),
            "refresh": as_object(refresh_result),
            "validation": validation,
            "stop_reason": stop_reason,
            "stop_reason_category": stop_reason_category,
            "scientific_exhaustion": stop_reason_category == "scientific_exhaustion",
            "engineering_failure": stop_reason_category == "engineering_failure",
            "elapsed_seconds": round(
                __import__("time").perf_counter() - started,
                3,
            ),
        }
        (section_work_dir / "SHORT_PATH_RUN.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        record = {
            "section_id": context.section_id,
            "status": status,
            "worker_status": "deterministic_short_path",
            "stop_reason": stop_reason,
            "stop_reason_category": stop_reason_category,
            "local_audit_calls": local_audit_calls,
            "searched_audit_calls": searched_audit_calls,
            "local_candidates_discovered": local_candidates_discovered,
            "local_candidates_ranked": local_candidates_ranked,
            "local_candidates_examined": len(local_examined_ids),
            "local_candidates_retained_by_lane": dict(
                local_approved_by_lane
            ),
            "local_batches": len(local_audit_results),
            "local_stop_reason": local_stop_reason or "local_audit_complete",
            "local_candidates_unexamined": len(local_candidates_remaining),
            "network_search_needed": local_network_search_needed,
            "model_calls": qwen_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_cny": round(cost_cny, 6),
            "cost_basis": usage_receipt["cost_basis"],
            "cost_is_estimated": usage_receipt["cost_is_estimated"],
            "usage_receipt_id": usage_receipt["receipt_id"],
            "usage_receipt_path": usage_receipt["receipt_path"],
            "model_tier": usage_receipt["model_tier"],
            "model_name": usage_receipt["model_name"],
            "work_dir": str(section_work_dir),
            "reused": False,
            "short_path": True,
            "coverage_budget": self._coverage_budget_metadata(),
            "coverage_telemetry": self._coverage_telemetry(section_work_dir),
        }
        bundle = None
        if (
            package_path.exists()
            and ledger_path.exists()
            and status in {"completed", "needs_more_literature"}
        ):
            bundle = SectionMaterialBundle(
                material_package_path=package_path,
                source_ledger_path=ledger_path,
                kb_sqlite=self.config.base_kb_sqlite,
                staging_kb_sqlite=(
                    self.staging_kb if self.staging_kb.exists() else None
                ),
            )
        return record, bundle

    def _document_short_path_gaps(
        self,
        context: SectionCoverageContext,
        section: Dict[str, Any],
        *,
        stop_reason: str,
        query_targets: List[Dict[str, Any]],
        candidates_found: int,
        search_receipts: List[Dict[str, Any]],
    ) -> bool:
        """Document a normal bounded stop without upgrading discovery data."""

        package = _read_json(context.work_dir / "SECTION_MATERIAL_PACKAGE.json")
        adaptive_contract = (
            build_adaptive_coverage_contract(
                section,
                section_count=section.get("_review_section_count"),
            )
            if self.config.adaptive_coverage_enabled
            else None
        )
        source_ledger = _read_json(context.work_dir / "SECTION_SOURCE_LEDGER.json")
        covered_roles = {
            str(source.get("literature_role") or "")
            for source in source_ledger.get("sources", [])
            if isinstance(source, dict) and source.get("canonical_chunk_ids")
        }
        required_roles = list(
            adaptive_contract.required_roles
            if adaptive_contract is not None
            else section.get("required_roles") or ["foundation"]
        )
        if package.get("adaptive_readiness"):
            covered_roles.update(
                str(role)
                for role in package.get("adaptive_readiness", {}).get(
                    "covered_required_roles", []
                )
            )
        unresolved_roles = [role for role in required_roles if role not in covered_roles]
        if not unresolved_roles:
            unresolved_roles = list(
                dict.fromkeys(
                    str(target.get("role") or "")
                    for target in query_targets
                    if str(target.get("role") or "") in ROLE_DEFINITIONS
                )
            )
        if not unresolved_roles and not source_ledger.get("sources"):
            unresolved_roles = ["foundation"]

        queries_by_role: Dict[str, List[str]] = {}
        for target in query_targets:
            if not isinstance(target, dict):
                continue
            role = str(target.get("role") or "")
            query = str(target.get("query") or "").strip()
            if query:
                queries_by_role.setdefault(role, []).append(query)
        oa_ledger = _read_json(context.work_dir / "OA_CANDIDATE_LEDGER.json")
        approved = sum(
            str(item.get("decision") or "") == "approved"
            for item in oa_ledger.get("candidates", [])
            if isinstance(item, dict)
        )
        manifest = _read_json(context.work_dir / "MATERIALIZATION_MANIFEST.json")
        materialized = sum(
            bool(item.get("new_chunk_ids") or item.get("chunk_ids"))
            for item in manifest.get("papers", [])
            if isinstance(item, dict)
        )
        backend_errors = [
            str(receipt.get("error") or receipt.get("error_code") or "backend returned no candidates")[:240]
            for receipt in search_receipts
            if receipt.get("status") != "ok" or receipt.get("error") or receipt.get("error_code")
        ]
        for receipt in search_receipts:
            stats = receipt.get("backend_stats") or {}
            if isinstance(stats, dict):
                backend_errors.extend(
                    str(value)[:240]
                    for key, value in stats.items()
                    if str(key).endswith("_error") and str(value).strip()
                )
        gaps: List[Dict[str, Any]] = []
        for role in unresolved_roles:
            gaps.append({
                "role": role,
                "severity": "important",
                "description": (
                    f"The deterministic bounded path did not establish defensible {role} coverage; "
                    "any existing evidence remains preserved in the source ledger."
                ),
                "queries_attempted": list(dict.fromkeys(queries_by_role.get(role, []))),
                "candidates_found": int(candidates_found),
                "candidates_approved": int(approved),
                "candidates_materialized": int(materialized),
                "stop_reason": "; ".join([str(stop_reason), *backend_errors])[:500],
                "suggested_followup": (
                    "Reopen a targeted legal-OA/full-text pass only if this role becomes load-bearing."
                ),
                "is_blocking": False,
            })
        if not source_ledger.get("sources") or not covered_roles:
            gaps.append({
                "role": "coverage_material",
                "severity": "important",
                "description": "No permitted factual material was produced by the bounded pass.",
                "queries_attempted": [
                    str(target.get("query") or "")
                    for target in query_targets
                    if str(target.get("query") or "")
                ],
                "candidates_found": int(candidates_found),
                "candidates_approved": int(approved),
                "candidates_materialized": int(materialized),
                "stop_reason": "; ".join([str(stop_reason), *backend_errors])[:500],
                "suggested_followup": "Supply legal OA/full text or reopen a targeted search wave.",
                "is_blocking": False,
            })
        if not self.config.adaptive_coverage_enabled:
            gaps.append({
                "role": "coverage_breadth",
                "severity": "important",
                "description": "The short path stopped before the legacy breadth target was met.",
                "queries_attempted": [
                    str(target.get("query") or "")
                    for target in query_targets
                    if str(target.get("query") or "")
                ],
                "candidates_found": int(candidates_found),
                "candidates_approved": int(approved),
                "candidates_materialized": int(materialized),
                "stop_reason": str(stop_reason)[:500],
                "suggested_followup": "Use a targeted breadth pass when additional sources are required.",
                "is_blocking": False,
            })
        if not gaps:
            return False
        raw_result = _make_submit_section_gap_report(context)(
            json.dumps(
                {
                    "gaps": gaps,
                    "overall_coverage_status": "completed_with_open_gaps",
                    "stop_conditions_met": [
                        "deterministic_short_path",
                        "bounded_search_waves",
                        "unresolved_evidence_preserved_without_fabrication",
                    ],
                },
                ensure_ascii=False,
            )
        )
        try:
            result = json.loads(raw_result)
        except Exception:
            result = {}
        return result.get("status") == "ok"

    @staticmethod
    def _is_systemic_runtime_failure(record: Dict[str, Any]) -> bool:
        if str(record.get("status") or "") not in {
            "failed",
            "waiting_for_human",
        }:
            return False
        reason = str(record.get("stop_reason") or "").lower()
        return any(
            marker in reason
            for marker in (
                "unexpected permission request",
                "mock_mode_no_api_key",
                "all model candidates",
            )
        )

    @staticmethod
    def _coverage_input_fingerprint(
        section_work_dir: Path,
        section: Dict[str, Any],
        *,
        author_feedback: Dict[str, Any],
        selected_paper_ids: List[str],
        selected_chunk_ids: List[str],
        selected_permissions: Dict[str, str],
        selected_content_depths: Dict[str, str],
    ) -> str:
        """Fingerprint logical request identity, excluding runtime state."""
        logical_phase3_request = {
            key: value
            for key, value in (
                section.get("phase3_coverage_request") or {}
            ).items()
            if key != "wave_index"
        }
        return _canonical_sha256(
            {
                "phase3_coverage_request": logical_phase3_request,
                "author_feedback_by_section": dict(author_feedback or {}),
                "selected_paper_ids": sorted(
                    str(value) for value in selected_paper_ids
                ),
                "selected_chunk_ids": sorted(
                    str(value) for value in selected_chunk_ids
                ),
                "selected_permissions": dict(sorted(selected_permissions.items())),
                "selected_content_depths": dict(
                    sorted(selected_content_depths.items())
                ),
            }
        )

    @staticmethod
    def _coverage_evidence_fingerprint(section_work_dir: Path) -> str:
        return _canonical_sha256(
            {
                "local_candidate_ledger": _read_json(
                    section_work_dir / "LOCAL_CANDIDATE_LEDGER.json"
                ),
                "oa_candidate_ledger": _read_json(
                    section_work_dir / "OA_CANDIDATE_LEDGER.json"
                ),
            }
        )

    @staticmethod
    def _has_genuine_coverage_delta(
        section_work_dir: Path,
        section: Dict[str, Any],
        previous_short_path: Dict[str, Any],
        *,
        author_feedback: Dict[str, Any],
        selected_paper_ids: List[str],
        selected_chunk_ids: List[str],
        selected_permissions: Dict[str, str],
        selected_content_depths: Dict[str, str],
    ) -> bool:
        current = SectionCoverageOrchestrator._coverage_input_fingerprint(
            section_work_dir,
            section,
            author_feedback=author_feedback,
            selected_paper_ids=selected_paper_ids,
            selected_chunk_ids=selected_chunk_ids,
            selected_permissions=selected_permissions,
            selected_content_depths=selected_content_depths,
        )
        stored = str(
            previous_short_path.get("coverage_input_fingerprint_sha256")
            or ""
        )
        if stored:
            current_evidence = (
                SectionCoverageOrchestrator._coverage_evidence_fingerprint(
                    section_work_dir
                )
            )
            stored_evidence = str(
                previous_short_path.get(
                    "coverage_evidence_fingerprint_sha256"
                )
                or ""
            )
            return current != stored or (
                bool(stored_evidence) and current_evidence != stored_evidence
            )
        # Legacy first-pass artifacts predate the durable fingerprint. Treat
        # them as no-delta unless an explicit Phase-3 material bridge/request
        # proves that the caller supplied new evidence.
        return bool(
            section.get("phase3_coverage_request")
            or author_feedback
            or selected_paper_ids
            or selected_chunk_ids
            or selected_permissions
            or selected_content_depths
        )

    @staticmethod
    def _second_pass_made_progress(
        record: Dict[str, Any],
        snapshot: Dict[str, Any],
        section_work_dir: Path,
    ) -> bool:
        if str(record.get("stop_reason_category") or "") == (
            "scientific_completion"
        ):
            return True
        if str(record.get("status") or "") == "completed" and str(
            snapshot.get("status") or ""
        ) != "completed":
            return True
        telemetry = record.get("coverage_telemetry") or {}
        phase2 = telemetry.get("phase2") or {}
        if bool(
            int(phase2.get("newly_inserted_papers") or 0)
            or int(phase2.get("newly_inserted_chunks") or 0)
            or int(phase2.get("accepted_s2_snippets") or 0)
        ):
            return True

        def approved_count(value: Dict[str, Any]) -> int:
            return sum(
                str(item.get("decision") or "") == "approved"
                for item in value.get("candidates", [])
                if isinstance(item, dict)
            )

        current_local = _read_json(
            section_work_dir / "LOCAL_CANDIDATE_LEDGER.json"
        )
        return approved_count(current_local) > approved_count(
            snapshot.get("local_candidate_ledger") or {}
        )

    def _run_one(
        self,
        section: Dict[str, Any],
        *,
        remaining_stage_budget: float,
    ) -> tuple[Dict[str, Any], Optional[SectionMaterialBundle]]:
        section_id = str(section["section_id"])
        section_work_dir = self.work_dir / "sections" / section_id
        section_work_dir.mkdir(parents=True, exist_ok=True)

        force_research = section_id in set(
            self.config.force_research_sections
        )

        phase3_bridge = section.get("phase3_material_context") or {}
        configured_shared = self.config.shared_kb_sqlite_paths or [
            Path(value) for value in phase3_bridge.get("shared_kb_paths") or []
        ]
        configured_ledger = self.config.source_ledger_path or (
            Path(str(phase3_bridge.get("source_ledger_path")))
            if phase3_bridge.get("source_ledger_path") else None
        )
        configured_overlay = self.config.section_overlay_paths.get(section_id) or (
            Path(str(phase3_bridge.get("section_overlay_path")))
            if phase3_bridge.get("section_overlay_path") else None
        )
        configured_papers = self.config.selected_paper_ids_by_section.get(section_id) or list(
            phase3_bridge.get("selected_paper_ids") or []
        )
        configured_chunks = self.config.selected_chunk_ids_by_section.get(section_id) or list(
            phase3_bridge.get("selected_chunk_ids") or []
        )
        configured_permissions = dict(
            self.config.selected_permissions_by_section.get(section_id)
            or phase3_bridge.get("selected_permissions")
            or {}
        )
        configured_content_depths = dict(
            self.config.selected_content_depths_by_section.get(section_id)
            or phase3_bridge.get("selected_content_depths")
            or {}
        )
        author_feedback = dict(
            self.config.author_feedback_by_section.get(section_id) or {}
        )
        logical_input_fingerprint = self._coverage_input_fingerprint(
            section_work_dir,
            section,
            author_feedback=author_feedback,
            selected_paper_ids=configured_papers,
            selected_chunk_ids=configured_chunks,
            selected_permissions=configured_permissions,
            selected_content_depths=configured_content_depths,
        )
        previous_short_path = _read_json(
            section_work_dir / "SHORT_PATH_RUN.json"
        )
        previous_supplementation = _read_json(
            section_work_dir / "SUPPLEMENTATION_RESULT.json"
        )
        latest_processed_fingerprint = str(
            previous_supplementation.get("input_fingerprint_sha256")
            or previous_short_path.get(
                "coverage_input_fingerprint_sha256"
            )
            or ""
        )
        latest_evidence_fingerprint = str(
            previous_supplementation.get("evidence_fingerprint_sha256")
            or previous_short_path.get(
                "coverage_evidence_fingerprint_sha256"
            )
            or ""
        )
        if not latest_evidence_fingerprint:
            # Legacy first-pass artifacts predate the evidence field. Use the
            # current candidate ledgers as the immutable baseline so later
            # evidence changes remain detectable after a no-progress retry.
            latest_evidence_fingerprint = self._coverage_evidence_fingerprint(
                section_work_dir
            )
        if force_research and not self._has_genuine_coverage_delta(
            section_work_dir,
            section,
            {
                "coverage_input_fingerprint_sha256": (
                    latest_processed_fingerprint
                ),
                "coverage_evidence_fingerprint_sha256": (
                    latest_evidence_fingerprint
                ),
            },
            author_feedback=author_feedback,
            selected_paper_ids=configured_papers,
            selected_chunk_ids=configured_chunks,
            selected_permissions=configured_permissions,
            selected_content_depths=configured_content_depths,
        ):
            # A portfolio/editorial replay of unchanged durable inputs is
            # reuse, not a fresh scientific attempt. Preserve the prior
            # short-path result and usage rather than archiving RESULT and
            # re-running against stale wave admission state.
            force_research = False
        prior_snapshot_path: Optional[Path] = None
        if force_research:
            prior_search = _read_json(
                section_work_dir / "SEARCH_BUDGET_LEDGER.json"
            )
            prior_max_wave = max(
                [
                    int(item.get("wave_index") or 0)
                    for item in prior_search.get("rounds", [])
                    if isinstance(item, dict)
                ]
                or [0]
            )
            request = dict(section.get("phase3_coverage_request") or {})
            # Each genuinely new request gets its own bounded audit-wave
            # namespace instead of inheriting first-pass admission state.
            request["wave_index"] = prior_max_wave + 1
            section = {**section, "phase3_coverage_request": request}
            prior_snapshot_path = self._prepare_for_targeted_retry(
                section_work_dir,
                input_fingerprint=logical_input_fingerprint,
                evidence_fingerprint=latest_evidence_fingerprint,
            )
        context = SectionCoverageContext(
            section_id=section_id,
            section_data={
                **section,
                "author_coverage_feedback": self.config.author_feedback_by_section.get(
                    section_id, {}
                ),
            },
            kb_sqlite=self.config.base_kb_sqlite,
            temp_kb_sqlite=self.staging_kb,
            work_dir=section_work_dir,
            shared_kb_sqlite_paths=[
                Path(path) for path in configured_shared
                if Path(path).exists()
            ],
            source_ledger_path=configured_ledger,
            section_overlay_path=configured_overlay,
            selected_paper_ids=list(configured_papers),
            selected_chunk_ids=list(configured_chunks),
            selected_permissions=dict(configured_permissions),
            selected_content_depths=dict(configured_content_depths),
            min_mode_allowed_role=None,
            min_mode_max_queries=self.config.max_queries_per_call,
            min_mode_max_per_backend=self.config.max_results_per_backend,
            min_mode_max_total_papers=(
                self.config.max_materialized_papers_per_section
            ),
            max_search_rounds_per_role=(
                self.config.max_search_rounds_per_role
            ),
            max_materialization_seconds_per_call=(
                self.config.max_materialization_seconds_per_call
            ),
            s2_first_enabled=self.config.s2_first_enabled,
            review_quality_contract=dict(
                section.get("review_quality_contract") or {}
            ),
            phase3_coverage_request=dict(
                section.get("phase3_coverage_request") or {}
            ),
            short_path_mode=bool(self.config.short_path_mode),
            resume_candidate_ledger_path=self.config.resume_candidate_ledger_path,
            cross_wave_state_path=self.config.cross_wave_state_path,
            global_coverage_ledger_path=self.config.global_coverage_ledger_path,
        )
        # These runtime-only attributes are intentionally attached after
        # construction so legacy direct SectionCoverageContext callers keep
        # their original dataclass surface.  SectionCoverageToolProvider uses
        # them to stop before the next oversized model call.
        deterministic_mode = bool(self.config.short_path_mode)
        context.context_per_call_budget_tokens = int(
            min(
                self.config.context_tokens_per_model_call,
                SHORT_PATH_PER_CALL_CONTEXT_TOKENS,
            )
            if deterministic_mode
            else self.config.context_tokens_per_model_call
        )
        context.context_cumulative_budget_tokens = int(
            min(
                self.config.token_budget_per_section,
                self.config.model_context_budget_per_section,
                SHORT_PATH_CUMULATIVE_CONTEXT_TOKENS,
            )
            if deterministic_mode
            else min(
                self.config.token_budget_per_section,
                self.config.model_context_budget_per_section,
            )
        )
        context.context_output_reserve_tokens = int(
            self.config.context_output_reserve_tokens
        )
        context.max_coverage_waves = int(
            min(self.config.max_coverage_waves, SHORT_PATH_MAX_WAVES)
            if deterministic_mode
            else self.config.max_coverage_waves
        )
        context.max_audit_calls_per_section = int(
            min(self.config.max_audit_calls_per_section, SHORT_PATH_MAX_AUDIT_CALLS)
            if deterministic_mode
            else self.config.max_audit_calls_per_section
        )
        context.max_model_calls_per_section = int(
            SHORT_PATH_MAX_MODEL_CALLS
            if deterministic_mode
            else self.config.max_model_calls_per_section
        )
        context.enforce_batched_audit_protocol = True
        context.adaptive_coverage_enabled = bool(self.config.adaptive_coverage_enabled)
        context.article_evidence_portfolio_path = self.article_evidence_portfolio_path
        if (
            context.resume_candidate_ledger_path
            and context.resume_candidate_ledger_path.exists()
            and not (section_work_dir / "OA_CANDIDATE_LEDGER.json").exists()
        ):
            shutil.copy2(
                context.resume_candidate_ledger_path,
                section_work_dir / "OA_CANDIDATE_LEDGER.json",
            )

        # Idempotent resume: revalidate a previous package against the current
        # code before reuse.  This prevents a package admitted by an older,
        # weaker trust rule from becoming a permanent cache hit.
        previous_result = _read_json(section_work_dir / "RESULT.json")
        previous_recovery = _read_json(
            section_work_dir / "COVERAGE_RECOVERY.json"
        )
        retry_cost_receipt = _read_json(
            section_work_dir / "COVERAGE_RETRY_COST.json"
        )
        if self._should_restart_incomplete_runtime(
            section_work_dir,
            previous_result,
            retry_cost_receipt,
        ):
            retry_cost_receipt = self._restart_incomplete_runtime(
                section_work_dir,
                previous_result,
                retry_cost_receipt,
            )
            previous_result = {}
            previous_recovery = {}
        package_path = section_work_dir / "SECTION_MATERIAL_PACKAGE.json"
        ledger_path = section_work_dir / "SECTION_SOURCE_LEDGER.json"
        reusable_candidate = (
            not force_research
            and
            (
                previous_result.get("status") == "completed"
                or previous_recovery.get("recovered") is True
                or previous_result.get("structural_task_complete") is True
            )
            and package_path.exists()
            and ledger_path.exists()
        )
        reuse_validation = (
            _make_validate_section_coverage_package(context)()
            if reusable_candidate else ""
        )
        if "VALIDATION_PASSED" in reuse_validation:
            package = _read_json(package_path)
            record = {
                "section_id": section_id,
                "status": self._status_from_package(package),
                "stop_reason": str(
                    previous_result.get("stop_reason")
                    or "reused_validated_package"
                ),
                "stop_reason_category": str(
                    previous_result.get("stop_reason_category")
                    or _stop_reason_category(
                        previous_result.get("stop_reason")
                        or "reused_validated_package"
                    )
                ),
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_cny": 0.0,
                "previous_input_tokens": int(
                    previous_result.get("total_input_tokens", 0)
                ),
                "previous_output_tokens": int(
                    previous_result.get("total_output_tokens", 0)
                ),
                "previous_cost_cny": float(
                    previous_result.get("estimated_cost_cny", 0.0)
                ),
                "work_dir": str(section_work_dir),
                "reused": True,
                "deterministic_recovery": previous_recovery,
                "reuse_validation": reuse_validation,
                "preserved_short_path_run": str(
                    section_work_dir / "SHORT_PATH_RUN.json"
                ),
                "preserved_result": str(section_work_dir / "RESULT.json"),
                "preserved_usage_receipt": str(
                    section_work_dir / "USAGE_RECEIPT.json"
                ),
                "coverage_budget": self._coverage_budget_metadata(),
                "coverage_telemetry": self._coverage_telemetry(section_work_dir),
            }
            return record, SectionMaterialBundle(
                material_package_path=package_path,
                source_ledger_path=ledger_path,
                kb_sqlite=self.config.base_kb_sqlite,
                staging_kb_sqlite=(
                    self.staging_kb if self.staging_kb.exists() else None
                ),
            )

        provider = SectionCoverageToolProvider(context)
        if self.config.short_path_mode:
            record, bundle = self._run_deterministic_short_path(
                context,
                section,
                section_work_dir=section_work_dir,
                remaining_stage_budget=remaining_stage_budget,
            )
            input_fingerprint = logical_input_fingerprint
            evidence_fingerprint = self._coverage_evidence_fingerprint(
                section_work_dir
            )
            record["coverage_input_fingerprint_sha256"] = input_fingerprint
            record["coverage_evidence_fingerprint_sha256"] = evidence_fingerprint
            summary_path = section_work_dir / "SHORT_PATH_RUN.json"
            summary = _read_json(summary_path)
            if summary:
                summary["coverage_input_fingerprint_sha256"] = (
                    input_fingerprint
                )
                summary["coverage_evidence_fingerprint_sha256"] = (
                    evidence_fingerprint
                )
                summary_path.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            if prior_snapshot_path is not None:
                snapshot = _read_json(prior_snapshot_path)
                made_progress = self._second_pass_made_progress(
                    record,
                    snapshot,
                    section_work_dir,
                )
                supplementation = {
                    "schema_version": (
                        "research_harness.coverage_supplementation.v1"
                    ),
                    "pass_id": str(snapshot.get("pass_id") or ""),
                    "input_fingerprint_sha256": input_fingerprint,
                    "evidence_fingerprint_sha256": (
                        str(snapshot.get("evidence_fingerprint_sha256") or "")
                        if not made_progress
                        else evidence_fingerprint
                    ),
                    "made_progress": bool(made_progress),
                    "incremental_calls": int(record.get("model_calls") or 0),
                    "incremental_input_tokens": int(
                        record.get("input_tokens") or 0
                    ),
                    "incremental_output_tokens": int(
                        record.get("output_tokens") or 0
                    ),
                    "incremental_cost_cny": round(
                        float(record.get("cost_cny") or 0.0), 6
                    ),
                    "attempt_stop_reason": str(
                        record.get("stop_reason") or ""
                    ),
                    "attempt_stop_reason_category": str(
                        record.get("stop_reason_category") or ""
                    ),
                    "preserved_stop_reason": str(
                        snapshot.get("stop_reason") or ""
                    ),
                    "preserved_stop_reason_category": str(
                        snapshot.get("stop_reason_category") or ""
                    ),
                    "history_snapshot_path": str(prior_snapshot_path),
                }
                supplementation_path = (
                    section_work_dir / "SUPPLEMENTATION_RESULT.json"
                )
                supplementation_path.write_text(
                    json.dumps(
                        supplementation,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                if not made_progress:
                    for filename, content in (
                        ("RESULT.json", snapshot.get("result")),
                        (
                            "SHORT_PATH_RUN.json",
                            snapshot.get("short_path_run"),
                        ),
                        (
                            "USAGE_RECEIPT.json",
                            snapshot.get("usage_receipt"),
                        ),
                        (
                            "SECTION_MATERIAL_PACKAGE.json",
                            snapshot.get("material_package"),
                        ),
                        (
                            "SECTION_SOURCE_LEDGER.json",
                            snapshot.get("source_ledger"),
                        ),
                        (
                            "LOCAL_CANDIDATE_LEDGER.json",
                            snapshot.get("local_candidate_ledger"),
                        ),
                        (
                            "OA_CANDIDATE_LEDGER.json",
                            snapshot.get("oa_candidate_ledger"),
                        ),
                    ):
                        if not isinstance(content, dict):
                            continue
                        (section_work_dir / filename).write_text(
                            json.dumps(
                                content,
                                ensure_ascii=False,
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                    record = {
                        **record,
                        "status": str(snapshot.get("status") or ""),
                        "stop_reason": str(
                            snapshot.get("stop_reason") or ""
                        ),
                        "stop_reason_category": str(
                            snapshot.get("stop_reason_category") or ""
                        ),
                        "second_pass_no_progress": True,
                        "second_pass_calls": int(
                            record.get("model_calls") or 0
                        ),
                        "second_pass_input_tokens": int(
                            record.get("input_tokens") or 0
                        ),
                        "second_pass_output_tokens": int(
                            record.get("output_tokens") or 0
                        ),
                        "second_pass_cost_cny": round(
                            float(record.get("cost_cny") or 0.0), 6
                        ),
                        "pass_history_path": str(prior_snapshot_path),
                        "supplementation_result": str(supplementation_path),
                    }
                else:
                    record["supplementation_progress"] = True
                    record["pass_history_path"] = str(prior_snapshot_path)
                    record["supplementation_result"] = str(
                        supplementation_path
                    )
            return record, bundle
        role_prompt = (
            PROJECT_ROOT
            / "prompts"
            / "roles"
            / "Section Literature Coverage Researcher.txt"
        ).read_text(encoding="utf-8")
        model_override = self.config.model_override
        model = (
            model_override(context)
            if callable(model_override)
            else model_override
        )
        worker = ResearchWorker(
            tool_provider=provider,
            _model_override=model,
            _system_prompt_override=role_prompt,
            _work_dir_override=section_work_dir,
        )
        required_roles = section.get(
            "required_roles",
            ["foundation", "mechanism", "method", "frontier"],
        )
        optional_roles = section.get(
            "optional_roles",
            ["controversy", "application"],
        )
        # A section cannot reserve more money than remains in the stage.  The
        # 0.25 CNY reserve used by ResearchWorker therefore remains meaningful
        # even for the final section in a run.
        # Automatic runtime restarts belong to the same scientific attempt and
        # must share its per-section budget. A targeted portfolio/editorial
        # return is a new, explicitly budgeted stage, so charging the earlier
        # runtime-recovery receipt again would both double-count spend and
        # starve the new stage before it can act.
        prior_retry_cost = self._chargeable_prior_retry_cost(
            force_research=force_research,
            retry_receipt=retry_cost_receipt,
        )
        section_budget = min(
            max(
                0.01,
                self.config.cost_budget_per_section_cny - prior_retry_cost,
            ),
            max(0.01, remaining_stage_budget),
        )
        contract = TaskContract(
            run_id=self.run_id,
            task_id=f"coverage_{section_id}",
            goal=(
                f"Build and validate the literature coverage package for "
                f"{section_id}. Required roles: {required_roles}. Optional roles: "
                f"{optional_roles}. Reuse qualified local sources first; search and "
                "materialize OA papers only for consequential gaps."
                + (
                    " This is a targeted feedback pass. Resolve the author's "
                    "documented pivotal gaps without reopening already adequate roles."
                    if self.config.author_feedback_by_section.get(section_id)
                    else ""
                )
            ),
            constraints=[
                "Use English only.",
                "Use only legal open-access full-text routes.",
                "Do not fabricate paper IDs, candidate IDs, DOIs, or acquisition status.",
                "Prefer directly relevant sources, but accept adjacent evidence when "
                "its inferential distance is explicitly recorded.",
                "Stop searching when required roles are sufficiently covered or "
                "additional retrieval has low expected marginal value.",
            ],
            success_criteria=[
                "Local coverage is audited before external retrieval.",
                "All required literature roles are covered or explicitly documented.",
                "Approved sources are materialized into the canonical staging KB.",
                "The section material package passes deterministic validation.",
            ],
            allowed_tools=provider.get_allowed_tool_names(),
            skill_ids=["section-literature-coverage"],
            model_tier=self.config.model_tier,
            max_iters=min(
                self.config.max_iters_per_section,
                self.config.max_model_calls_per_section,
            ),
            token_budget=min(
                self.config.token_budget_per_section,
                self.config.model_context_budget_per_section,
            ),
            cost_budget_cny=section_budget,
            next_call_cost_reserve_cny=min(0.25, section_budget * 0.2),
            wall_time_budget_seconds=(
                self.config.wall_time_per_section_seconds
            ),
            expected_outputs=[
                "SECTION_CONTEXT.json",
                "LOCAL_COVERAGE_AUDIT.json",
                "SECTION_COVERAGE_PLAN.json",
                "SECTION_MATERIAL_PACKAGE.json",
                "SECTION_COVERAGE_PACKAGE.json",
            ],
            metadata={
                # AgentScope uses these values for early context compaction;
                # the provider's payload ledger supplies the deterministic
                # pre-next-call guard for the same contract.
                "context_trigger_ratio": 0.50,
                "context_reserve_ratio": 0.10,
                "context_tool_result_limit": 900,
                "coverage_context_budget": {
                    "per_call_tokens": int(self.config.context_tokens_per_model_call),
                    "cumulative_tokens": int(min(
                        self.config.token_budget_per_section,
                        self.config.model_context_budget_per_section,
                    )),
                    "output_reserve_tokens": int(self.config.context_output_reserve_tokens),
                    "max_model_calls": int(self.config.max_model_calls_per_section),
                    "max_coverage_waves": int(self.config.max_coverage_waves),
                    "max_audit_calls": int(self.config.max_audit_calls_per_section),
                },
            },
        )
        result = worker.run(contract)
        recovery = self._try_deterministic_finalize(
            context,
            worker_status=result.status.value,
            stop_reason=result.stop_reason or "",
        )
        package = _read_json(package_path)
        self._reconcile_result_artifact(
            section_work_dir,
            package,
            structural_task_complete=bool(package),
        )
        if recovery.get("recovered"):
            status = self._status_from_package(package)
        elif result.status.value == "completed":
            status = self._status_from_package(package)
        else:
            status = result.status.value
        record = {
            "section_id": section_id,
            "status": status,
            "worker_status": result.status.value,
            "stop_reason": result.stop_reason,
            "input_tokens": result.total_input_tokens,
            "output_tokens": result.total_output_tokens,
            "cost_cny": round(
                prior_retry_cost + result.estimated_cost_cny,
                6,
            ),
            "work_dir": str(section_work_dir),
            "reused": False,
            "deterministic_recovery": recovery,
            "coverage_budget": self._coverage_budget_metadata(),
            "coverage_telemetry": self._coverage_telemetry(section_work_dir),
        }
        bundle = None
        if (
            package_path.exists()
            and ledger_path.exists()
            and status in {"completed", "needs_more_literature"}
        ):
            bundle = SectionMaterialBundle(
                material_package_path=package_path,
                source_ledger_path=ledger_path,
                kb_sqlite=self.config.base_kb_sqlite,
                staging_kb_sqlite=(
                    self.staging_kb if self.staging_kb.exists() else None
                ),
            )
        return record, bundle

    def _run_short_path(
        self,
        context: SectionCoverageContext,
        section: Dict[str, Any],
        *,
        section_work_dir: Path,
        remaining_stage_budget: float,
    ) -> tuple[Dict[str, Any], Optional[SectionMaterialBundle]]:
        """Legacy compatibility helper; production uses the controller above.

        Run the bounded deterministic path around one compact judgement.

        This path is used for Phase 2.1 acceptance and is generic: it consumes
        whatever section and candidate ledger it is given.  Search, ingestion,
        refresh, validation and stopping remain deterministic tool calls.  A
        Qwen call is admitted only when the durable candidate ledger lacks an
        audit for the inspected candidates.
        """

        from .section_coverage_tool_registry import (
            _read_artifact,
            _restore_candidates_from_ledger,
        )

        started = __import__("time").perf_counter()
        token_budget = int(min(
            self.config.token_budget_per_section,
            self.config.model_context_budget_per_section,
        ))
        input_tokens = 0
        output_tokens = 0
        cost_cny = 0.0
        qwen_calls = 0
        qwen_failure = ""
        qwen_usage: Dict[str, Any] = {}
        cost_bases: List[str] = []
        cost_estimated_flags: List[bool] = []

        load_result = _make_load_section_context(context)()
        local_result = _make_inspect_section_local_coverage(context)()

        if not (section_work_dir / "SECTION_COVERAGE_PLAN.json").exists():
            adaptive_contract = build_adaptive_coverage_contract(
                section,
                section_count=section.get("_review_section_count"),
            ) if self.config.adaptive_coverage_enabled else None
            required = set(
                adaptive_contract.required_roles
                if adaptive_contract is not None
                else (str(value) for value in section.get("required_roles") or [])
            )
            optional = set(
                adaptive_contract.optional_roles
                if adaptive_contract is not None
                else (str(value) for value in section.get("optional_roles") or [])
            )
            targets = context.targeted_queries or [
                " ".join(str(section.get("topic_identity", {}).get("scientific_object") or section.get("title") or "optical science").split())
            ]
            plan = {}
            for role in ROLE_DEFINITIONS:
                priority = "required" if role in required else "important" if role in optional else "useful"
                if role not in required and role not in optional and not context.targeted_missing_roles:
                    priority = "not_needed" if role not in {"foundation", "mechanism", "frontier"} else priority
                plan[role] = {
                    "priority": priority,
                    "coverage_question": ROLE_DEFINITIONS[role],
                    "intended_synthesis": ROLE_DEFINITIONS[role],
                    "queries": targets[:1] if priority != "not_needed" else [],
                }
            _make_submit_literature_role_plan(context)(json.dumps(plan, ensure_ascii=False))

        _restore_candidates_from_ledger(context)
        from .phase2_phase3_feedback import canonical_material_identity
        from .section_coverage_tool_registry import (
            _candidate_identity,
            _audit_call_preflight,
            _current_wave_index,
            _read_cross_wave_state,
            _staging_material_for_candidate,
        )

        def load_candidates() -> list[dict[str, Any]]:
            ledger_data = _read_artifact(section_work_dir, "OA_CANDIDATE_LEDGER.json") or {}
            return [
                dict(item) for item in ledger_data.get("candidates", [])
                if isinstance(item, dict) and item.get("candidate_id")
            ]

        def eligible_candidates(raw_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
            cross_state = _read_cross_wave_state(context)
            attempted_ids = set(cross_state.get("attempted_candidate_ids") or [])
            attempted_identities = set(cross_state.get("attempted_material_identities") or [])
            outcomes = cross_state.get("candidate_outcomes") or {}
            identity_index = cross_state.get("material_identity_index") or {}
            seen: set[str] = set()
            eligible: list[dict[str, Any]] = []
            for item in raw_candidates:
                cid = str(item.get("candidate_id") or "")
                identity = _candidate_identity(item) or canonical_material_identity(item)
                outcome = outcomes.get(cid) or {}
                if not outcome and identity:
                    outcome = next(
                        (outcomes.get(str(previous_id)) or {} for previous_id in identity_index.get(identity, [])),
                        {},
                    )
                if not cid or identity in seen:
                    continue
                seen.add(identity)
                if cid in attempted_ids or identity in attempted_identities:
                    continue
                if outcome.get("no_progress"):
                    continue
                if (
                    str(item.get("decision") or "deferred").casefold()
                    not in {"approved", "rejected"}
                    and not _candidate_needs_new_audit_payload(
                        section_work_dir, item
                    )
                ):
                    continue
                if (
                    str(item.get("candidate_action") or "").casefold() == "reject"
                    and str(item.get("decision") or "").casefold() == "rejected"
                ):
                    continue
                existing_id, _ = _staging_material_for_candidate(context, item)
                if existing_id:
                    continue
                item["material_identity"] = identity
                eligible.append(item)
            eligible.sort(
                key=lambda item: (
                    str(item.get("decision") or "deferred") != "approved",
                    str(item.get("candidate_action") or "") != "materialize_now",
                    str(item.get("scope_fit") or "") != "direct",
                    -float(item.get("relevance_score") or 0.0),
                    -int(item.get("citation_count") or 0),
                    str(item.get("title") or ""),
                )
            )
            return eligible

        candidates = eligible_candidates(load_candidates())
        searched_for_novel_candidate = False
        if not candidates:
            role = (context.targeted_missing_roles or list(section.get("required_roles") or ["foundation"]))[0]
            request = context.phase3_coverage_request or {}
            targets = [
                item for item in request.get("query_targets") or []
                if isinstance(item, dict) and str(item.get("query") or "").strip()
            ]
            queries = [str(item.get("query")) for item in targets]
            if not queries:
                queries = context.targeted_queries[:5]
            if queries:
                searched_for_novel_candidate = True
                _make_search_oa_candidates(context)(
                    role,
                    json.dumps(queries[:5], ensure_ascii=False),
                    max_per_backend=self.config.max_results_per_backend,
                )
                _restore_candidates_from_ledger(context)
                candidates = eligible_candidates(load_candidates())

        candidate_ids = [str(item["candidate_id"]) for item in candidates[:6]]
        inspection = {}
        if candidate_ids:
            inspection = json.loads(_make_inspect_candidate_batch(context)(json.dumps(candidate_ids)))

        audited = [
            item for item in candidates[:6]
            if str(item.get("decision") or "deferred") in {"approved", "rejected"}
        ]
        audit_records: list[dict[str, Any]] = []
        if len(audited) < len(candidate_ids):
            compact_payload = build_compact_batched_audit_payload(
                section={
                    **section,
                    "section_id": context.section_id,
                },
                candidates=(inspection.get("candidates") or [])[:SHORT_PATH_SEARCH_AUDIT_CANDIDATES],
                wave_index=_current_wave_index(context),
                max_candidates=SHORT_PATH_SEARCH_AUDIT_CANDIDATES,
                components=(context.phase3_coverage_request or {}).get(
                    "missing_components", []
                ),
            )
            predicted_input = estimate_json_tokens(compact_payload)
            output_reserve = max(1000, int(self.config.context_output_reserve_tokens))
            admission = _audit_call_preflight(
                context,
                candidate_ids,
                predicted_input,
            )
            if admission.admitted:
                try:
                    from llm.qwen_chat_client import call_qwen_chat
                    prompt_path = PROJECT_ROOT / "prompts" / "Phase 2 Short Path Candidate Auditor.txt"
                    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else (
                        "Audit candidates for scientific scope and legal acquisition. Return JSON array only."
                    )
                    response = call_qwen_chat(
                        "Phase2ShortPathCandidateAuditor",
                        [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": json.dumps(compact_payload, ensure_ascii=False)},
                        ],
                        model_tier=self.config.model_tier,
                        temperature=0,
                        max_tokens=1400,
                        response_format={"type": "json_object"},
                        force_mock=False,
                        max_retries=1,
                    )
                    qwen_calls = 1
                    raw_content = response.get("content") or "[]"
                    if isinstance(raw_content, str):
                        decoded = decode_json_payload(raw_content, expected="any")
                        if decoded.error:
                            raise ValueError(decoded.error)
                        parsed = decoded.value
                    elif isinstance(raw_content, (dict, list)):
                        parsed = raw_content
                    else:
                        raise ValueError("model response content must be JSON text, object, or list")
                    audit_records = parsed.get("candidates", parsed) if isinstance(parsed, dict) else parsed
                    if not isinstance(audit_records, list):
                        audit_records = []
                    qwen_usage = normalize_qwen_usage(
                        response,
                        fallback_input_tokens=predicted_input,
                        fallback_output_tokens=estimate_json_tokens(audit_records),
                        model_tier=self.config.model_tier,
                    )
                    cost_bases.append(str(qwen_usage.get("cost_basis") or "unavailable"))
                    cost_estimated_flags.append(bool(qwen_usage.get("cost_is_estimated")))
                    input_tokens = int(qwen_usage["input_tokens"])
                    output_tokens = int(qwen_usage["output_tokens"])
                    cost_cny = float(qwen_usage["cost_cny"])
                except Exception as exc:
                    qwen_failure = f"{type(exc).__name__}: {str(exc)[:240]}"
            else:
                qwen_failure = admission.reason

        if not audit_records:
            audit_records = [
                {
                    "candidate_id": item.get("candidate_id"),
                    "scope_fit": item.get("scope_fit", "unreviewed"),
                    "role_fit": item.get("role_fit") or [item.get("role", "foundation")],
                    "decision": item.get("decision", "deferred"),
                    "candidate_decision": item.get("candidate_action", ""),
                    "audit_reason": item.get("audit_reason", "durable_audit_replayed"),
                    "not_usable_for": item.get("not_usable_for", []),
                }
                for item in candidates[:6]
            ]
        audit_result = json.loads(
            _make_submit_candidate_audit(context)(json.dumps(audit_records, ensure_ascii=False))
        ) if audit_records else {"status": "no_candidates"}
        if qwen_usage:
            _reconcile_batched_audit_usage(context, qwen_usage)

        refresh_result = _make_refresh_section_coverage(context)()
        validation = _make_validate_section_coverage_package(context)()
        if "VALIDATION_FAILED" in validation and _document_bounded_materialization_gaps(context):
            refresh_result = _make_refresh_section_coverage(context)()
            validation = _make_validate_section_coverage_package(context)()

        usage_receipt = _build_section_usage_receipt(
            section_work_dir=section_work_dir,
            model_tier=self.config.model_tier,
            qwen_usage=qwen_usage,
            qwen_calls=qwen_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=cost_cny,
            cost_bases=cost_bases,
            cost_estimated_flags=cost_estimated_flags,
        )
        input_tokens = int(usage_receipt["input_tokens"])
        output_tokens = int(usage_receipt["output_tokens"])
        cost_cny = float(usage_receipt["cost_cny"])

        package_path = section_work_dir / "SECTION_MATERIAL_PACKAGE.json"
        ledger_path = section_work_dir / "SECTION_SOURCE_LEDGER.json"
        package = _read_json(package_path)
        status = self._status_from_package(package) if package else "failed"
        if not package and "VALIDATION_FAILED" in validation:
            status = "needs_more_literature"
        summary = {
            "schema_version": "phase2.1.short_path_run.v1",
            "section_id": context.section_id,
            "status": status,
            "qwen_calls": qwen_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_cny": cost_cny,
            "cost_basis": usage_receipt["cost_basis"],
            "cost_is_estimated": usage_receipt["cost_is_estimated"],
            "usage_receipt_id": usage_receipt["receipt_id"],
            "usage_receipt_path": usage_receipt["receipt_path"],
            "model_tier": usage_receipt["model_tier"],
            "model_name": usage_receipt["model_name"],
            "token_budget": token_budget,
            "max_model_calls": int(self.config.max_model_calls_per_section),
            "max_coverage_waves": int(self.config.max_coverage_waves),
            "max_audit_calls": int(self.config.max_audit_calls_per_section),
            "token_admission": {
                "predicted_input_plus_reserve_checked": True,
                "per_call_budget_tokens": int(self.config.context_tokens_per_model_call),
                "cumulative_budget_tokens": token_budget,
                "rejected_reason": qwen_failure if "budget" in qwen_failure or "context" in qwen_failure else "",
            },
            "local_audit": json.loads(local_result),
            "candidate_audit": audit_result,
            "post_audit_transition": _read_json(section_work_dir / "POST_AUDIT_TRANSITION.json"),
            "validation": validation,
            "elapsed_seconds": round(__import__("time").perf_counter() - started, 3),
        }
        (section_work_dir / "SHORT_PATH_RUN.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        record = {
            "section_id": context.section_id,
            "status": status,
            "worker_status": "deterministic_short_path",
            "stop_reason": validation,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_cny": round(cost_cny, 6),
            "cost_basis": usage_receipt["cost_basis"],
            "cost_is_estimated": usage_receipt["cost_is_estimated"],
            "usage_receipt_id": usage_receipt["receipt_id"],
            "usage_receipt_path": usage_receipt["receipt_path"],
            "model_tier": usage_receipt["model_tier"],
            "model_name": usage_receipt["model_name"],
            "work_dir": str(section_work_dir),
            "reused": False,
            "short_path": True,
            "coverage_budget": self._coverage_budget_metadata(),
            "coverage_telemetry": self._coverage_telemetry(section_work_dir),
        }
        bundle = None
        if package_path.exists() and ledger_path.exists() and status in {"completed", "needs_more_literature"}:
            bundle = SectionMaterialBundle(
                material_package_path=package_path,
                source_ledger_path=ledger_path,
                kb_sqlite=self.config.base_kb_sqlite,
                staging_kb_sqlite=self.staging_kb if self.staging_kb.exists() else None,
            )
        return record, bundle

    @staticmethod
    def _status_from_package(package: Dict[str, Any]) -> str:
        """Keep a usable package distinct from a fully covered package.

        Deterministic validation may intentionally admit a package whose
        unresolved breadth gap is documented.  Such a package is safe for
        downstream drafting, but it must remain visible to portfolio-level
        gap resolution instead of being silently upgraded to ``completed``.
        Older packages do not contain the newer breadth fields, so the legacy
        ``blocking_gaps_remain`` flag remains the final compatibility check.
        """

        adaptive_outcome = str(
            package.get("coverage_outcome") or package.get("readiness_outcome") or ""
        ).strip()
        if adaptive_outcome in {
            "material_ready",
            "material_ready_with_limits",
            "merge_required",
        }:
            # ``merge_required`` is an editorial action, not a failed review
            # run: the evidence is preserved and the caller can combine this
            # section with its declared merge target.
            return "completed"
        if adaptive_outcome == "needs_more_literature":
            return "needs_more_literature"
        coverage_status = str(package.get("coverage_status") or "").strip()
        if coverage_status == "completed_with_open_gaps":
            return "needs_more_literature"
        if package.get("breadth_target_met") is False:
            return "needs_more_literature"
        if package.get("blocking_gaps_remain") is True:
            return "needs_more_literature"
        return "completed"

    @staticmethod
    def _reconcile_result_artifact(
        section_work_dir: Path,
        package: Dict[str, Any],
        *,
        structural_task_complete: bool,
    ) -> None:
        """Make worker RESULT truthfully expose scientific readiness.

        ``ResearchWorker`` quite correctly treats a deterministic structural
        validator as a terminal task gate.  Section coverage has a second
        scientific breadth gate, so an open package must not leave a stale
        ``completed``/``all_gates_passed`` result on disk.
        """

        result_path = section_work_dir / "RESULT.json"
        if not result_path.exists() or not package:
            return
        result = _read_json(result_path)
        readiness = evaluate_coverage_readiness(
            required_artifacts=(),
            work_dir_exists=structural_task_complete,
            package=package,
        )
        result["coverage_readiness"] = readiness.to_dict()
        result["coverage_outcome"] = readiness.outcome
        result["coverage_status"] = readiness.outcome
        result["structural_task_complete"] = readiness.structural_task_complete
        result["scientific_coverage_ready"] = readiness.scientific_coverage_ready
        result["all_gates_passed"] = readiness.scientific_coverage_ready
        if (
            not readiness.scientific_coverage_ready
            and str(result.get("status") or "") == "completed"
        ):
            result["status"] = "validation_failed"
            result["validation_passed"] = False
            result["stop_reason"] = (
                "structural_task_complete_but_scientific_coverage_not_ready: "
                + readiness.reason
            )
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result_md = section_work_dir / "RESULT.md"
        if result_md.exists() and not readiness.scientific_coverage_ready:
            result_md_text = result_md.read_text(encoding="utf-8")
            readiness_note = (
                "Scientific coverage readiness: **needs_more_literature**  \n"
                "Structural task complete: **true**; all scientific gates passed: **false**\n"
            )
            if readiness_note not in result_md_text:
                result_md.write_text(
                    result_md_text + "\n" + readiness_note,
                    encoding="utf-8",
                )

    @staticmethod
    def _chargeable_prior_retry_cost(
        *,
        force_research: bool,
        retry_receipt: Dict[str, Any],
    ) -> float:
        """Return prior spend chargeable to the current stage budget."""

        if force_research:
            return 0.0
        return float(retry_receipt.get("cumulative_cost_cny", 0.0))

    def _should_restart_incomplete_runtime(
        self,
        section_work_dir: Path,
        previous_result: Dict[str, Any],
        retry_receipt: Dict[str, Any],
    ) -> bool:
        """Return whether a bloated failed conversation should restart cleanly.

        This is not a scientific retry. Existing source audits, candidate
        decisions, search ledgers, and materialised KB records remain in place.
        A clean AgentState lets the worker inspect those durable artifacts and
        finish the package without replaying an oversized conversation.
        """

        if int(retry_receipt.get("restart_count", 0)) >= int(
            self.config.max_runtime_restarts_per_section
        ):
            return False
        if str(previous_result.get("status") or "") not in {
            "budget_exhausted",
            "failed",
        }:
            return False
        previous_recovery = _read_json(
            section_work_dir / "COVERAGE_RECOVERY.json"
        )
        if previous_recovery.get("recovered") is True:
            return False
        reason = str(previous_result.get("stop_reason") or "").lower()
        return any(
            marker in reason
            for marker in (
                "max_iters",
                "token_overshoot",
                "token_budget",
            )
        )

    def _restart_incomplete_runtime(
        self,
        section_work_dir: Path,
        previous_result: Dict[str, Any],
        retry_receipt: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Archive runtime state while retaining and charging prior work."""

        restart_count = int(retry_receipt.get("restart_count", 0)) + 1
        archive_root = (
            section_work_dir
            / "_runtime_archive"
            / f"automatic_restart_{restart_count}_{uuid.uuid4().hex[:8]}"
        )
        archive_root.mkdir(parents=True, exist_ok=True)
        runtime_names = (
            "RESULT.json",
            "RESULT.md",
            "AGENT_STATE.json",
            "TASK.json",
            "PLAN.md",
            "EVENTS.jsonl",
            "COST.json",
            "COVERAGE_RECOVERY.json",
        )
        for name in runtime_names:
            source = section_work_dir / name
            if source.exists():
                shutil.move(str(source), str(archive_root / name))

        previous_cost = float(
            previous_result.get("estimated_cost_cny", 0.0)
        )
        attempts = list(retry_receipt.get("attempts") or [])
        attempts.append(
            {
                "restart_index": restart_count,
                "previous_status": previous_result.get("status", ""),
                "previous_stop_reason": previous_result.get(
                    "stop_reason", ""
                ),
                "cost_cny": previous_cost,
                "archive_dir": str(archive_root),
                "created_at": _now(),
            }
        )
        receipt = {
            "schema_version": "research_harness.coverage_retry_cost.v1",
            "restart_count": restart_count,
            "cumulative_cost_cny": round(
                float(retry_receipt.get("cumulative_cost_cny", 0.0))
                + previous_cost,
                6,
            ),
            "attempts": attempts,
            "created_or_updated_at": _now(),
        }
        (section_work_dir / "COVERAGE_RETRY_COST.json").write_text(
            json.dumps(receipt, indent=2),
            encoding="utf-8",
        )
        return receipt

    def _prepare_for_targeted_retry(
        self,
        section_work_dir: Path,
        *,
        input_fingerprint: str = "",
        evidence_fingerprint: str = "",
    ) -> Path:
        """Archive runtime state and snapshot first-pass immutable history."""

        archive_root = (
            section_work_dir
            / "_runtime_archive"
            / f"{self.config.retry_label}_{uuid.uuid4().hex[:8]}"
        )
        history_root = section_work_dir / "_pass_history"
        history_root.mkdir(parents=True, exist_ok=True)
        pass_id = f"{self.config.retry_label}_{uuid.uuid4().hex[:8]}"
        snapshot_path = history_root / f"{pass_id}.json"
        previous_result = _read_json(section_work_dir / "RESULT.json")
        previous_short_path = _read_json(
            section_work_dir / "SHORT_PATH_RUN.json"
        )
        previous_usage = _read_json(section_work_dir / "USAGE_RECEIPT.json")
        previous_package = _read_json(
            section_work_dir / "SECTION_MATERIAL_PACKAGE.json"
        )
        previous_source_ledger = _read_json(
            section_work_dir / "SECTION_SOURCE_LEDGER.json"
        )
        snapshot = {
            "schema_version": "research_harness.coverage_pass_history.v1",
            "pass_id": pass_id,
            "input_fingerprint_sha256": input_fingerprint,
            "evidence_fingerprint_sha256": evidence_fingerprint,
            "calls": int(previous_result.get("model_calls", 0) or 0),
            "input_tokens": int(
                previous_result.get("total_input_tokens", 0) or 0
            ),
            "output_tokens": int(
                previous_result.get("total_output_tokens", 0) or 0
            ),
            "cost_cny": round(
                float(previous_result.get("estimated_cost_cny", 0.0) or 0.0),
                6,
            ),
            "status": str(previous_result.get("status") or ""),
            "stop_reason": str(previous_result.get("stop_reason") or ""),
            "stop_reason_category": str(
                previous_result.get("stop_reason_category") or ""
            ),
            "result": previous_result,
            "short_path_run": previous_short_path,
            "usage_receipt": previous_usage,
            "material_package": previous_package,
            "source_ledger": previous_source_ledger,
            "local_candidate_ledger": _read_json(
                section_work_dir / "LOCAL_CANDIDATE_LEDGER.json"
            ),
            "oa_candidate_ledger": _read_json(
                section_work_dir / "OA_CANDIDATE_LEDGER.json"
            ),
            "phase2_telemetry": _read_json(
                section_work_dir / "PHASE2_TELEMETRY.json"
            ),
            "coverage_wave_telemetry": _read_json(
                section_work_dir / "COVERAGE_WAVE_TELEMETRY.json"
            ),
            "created_at": _now(),
        }
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        runtime_names = (
            "RESULT.json",
            "AGENT_STATE.json",
            "TASK.json",
            "PLAN.md",
            "EVENTS.jsonl",
            "COST.json",
            "COVERAGE_RECOVERY.json",
            "COVERAGE_WAVE_TELEMETRY.json",
            "PHASE2_TELEMETRY.json",
        )
        moved = False
        for name in runtime_names:
            source = section_work_dir / name
            if not source.exists():
                continue
            archive_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(archive_root / name))
            moved = True

        # A prior documented breadth stop must be reconsidered during the new
        # bounded retry. Other genuine role gaps remain part of the record.
        gap_path = section_work_dir / "SECTION_GAP_REPORT.json"
        if gap_path.exists():
            value = _read_json(gap_path)
            gaps = [
                gap
                for gap in value.get("gaps", [])
                if isinstance(gap, dict)
                and gap.get("role") not in {
                    "coverage_breadth",
                    "source_concentration",
                }
            ]
            value["gaps"] = gaps
            value["blocking_gap_count"] = sum(
                bool(gap.get("is_blocking")) for gap in gaps
            )
            value["open_gap_count"] = len(gaps)
            gap_path.write_text(
                json.dumps(value, indent=2),
                encoding="utf-8",
            )
        package_path = section_work_dir / "SECTION_MATERIAL_PACKAGE.json"
        if package_path.exists():
            if moved:
                archive_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(
                    package_path,
                    archive_root / "SECTION_MATERIAL_PACKAGE.before_retry.json",
                )
            package_path.unlink()
        return snapshot_path

    def _try_deterministic_finalize(
        self,
        context: SectionCoverageContext,
        *,
        worker_status: str,
        stop_reason: str,
    ) -> Dict[str, Any]:
        """Finish a complete package without paying for one final model turn.

        A worker may hit its cumulative-context or iteration guard immediately
        after writing every scientific artifact.  The deterministic validator,
        not the model's final prose, is the authority.  We preserve the worker
        status for audit and write a separate recovery receipt.
        """
        if worker_status == "completed":
            return {"attempted": False, "recovered": False}
        prerequisites = (
            "SECTION_CONTEXT.json",
            "SECTION_COVERAGE_PLAN.json",
            "LOCAL_COVERAGE_AUDIT.json",
        )
        if not all((context.work_dir / name).exists() for name in prerequisites):
            return {
                "attempted": False,
                "recovered": False,
                "reason": "prerequisite_artifacts_missing",
            }
        validation_text = _make_validate_section_coverage_package(context)()
        if (
            "VALIDATION_FAILED" in validation_text
            and worker_status == "budget_exhausted"
        ):
            # Prefer the same precise, non-fabricating materialization-limit
            # treatment as the live provider. A post-mortem recovery must not
            # regress into a harsher or semantically different gap report.
            if _bounded_materialization_limit_reached(context):
                documented = _document_bounded_materialization_gaps(context)
            else:
                documented = self._document_budget_stop_gaps(
                    context,
                    validation_text=validation_text,
                    worker_stop_reason=stop_reason,
                )
            if documented:
                validation_text = (
                    _make_validate_section_coverage_package(context)()
                )
        recovered = "VALIDATION_PASSED" in validation_text
        receipt = {
            "schema_version": "research_harness.coverage_recovery.v1",
            "attempted": True,
            "recovered": recovered,
            "worker_status": worker_status,
            "worker_stop_reason": stop_reason,
            "validation_result": validation_text,
            "created_at": _now(),
        }
        (context.work_dir / "COVERAGE_RECOVERY.json").write_text(
            json.dumps(receipt, indent=2),
            encoding="utf-8",
        )
        return receipt

    @staticmethod
    def _document_budget_stop_gaps(
        context: SectionCoverageContext,
        *,
        validation_text: str,
        worker_stop_reason: str,
    ) -> bool:
        """Persist honest stop reasons for searched-but-unresolved gaps.

        The model is not required to spend one final paid turn restating what
        the deterministic runtime already knows: a bounded search ended and a
        specific gap remains. This never upgrades evidence or marks a role as
        covered; it only makes the stopping decision auditable so downstream
        authors can write cautiously or trigger a later targeted return.
        """

        marker = "no documented stop reason:"
        if marker not in validation_text:
            return False
        raw = validation_text.split(marker, 1)[1].split(".", 1)[0].strip()
        try:
            unresolved = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return False
        if not isinstance(unresolved, list) or not unresolved:
            return False

        search_ledger = _read_json(
            context.work_dir / "SEARCH_BUDGET_LEDGER.json"
        )
        rounds = [
            item
            for item in search_ledger.get("rounds", [])
            if isinstance(item, dict)
        ]
        gaps = []
        for role_value in unresolved:
            role = str(role_value or "").strip()
            if not role:
                continue
            relevant_rounds = (
                rounds
                if role == "coverage_breadth"
                else [
                    item
                    for item in rounds
                    if str(item.get("role") or "") == role
                ]
            )
            queries = list(
                dict.fromkeys(
                    str(query).strip()
                    for item in relevant_rounds
                    for query in (item.get("queries") or [])
                    if str(query).strip()
                )
            )
            candidates_found = sum(
                int(item.get("candidate_count") or 0)
                for item in relevant_rounds
            )
            if role == "controversy":
                followup = (
                    "Do not manufacture a controversy. Reopen this gap only "
                    "when a genuinely conflicting result, definition dispute, "
                    "or measurement disagreement is found."
                )
            elif role == "coverage_breadth":
                followup = (
                    "A later targeted feedback pass may seek a small number of "
                    "additional directly relevant OA sources; authoring must "
                    "retain the present breadth limitation meanwhile."
                )
            else:
                followup = (
                    "Revisit through a targeted OA search or human-supplied "
                    "full text only if this role becomes load-bearing in the "
                    "article argument."
                )
            gaps.append(
                {
                    "role": role,
                    "severity": "blocking",
                    "description": (
                        f"The bounded retrieval pass did not establish "
                        f"defensible {role} coverage."
                    ),
                    "queries_attempted": queries,
                    "candidates_found": candidates_found,
                    "candidates_approved": 0,
                    "candidates_materialized": 0,
                    "stop_reason": (
                        "Deterministic bounded-search stop: "
                        + worker_stop_reason
                    ),
                    "suggested_followup": followup,
                    "is_blocking": True,
                }
            )
        if not gaps:
            return False
        result_text = _make_submit_section_gap_report(context)(
            json.dumps(
                {
                    "gaps": gaps,
                    "overall_coverage_status": "blocking_gaps_remain",
                    "stop_conditions_met": [
                        "bounded_runtime_guard_reached",
                        "unresolved_gap_preserved_without_fabrication",
                    ],
                }
            )
        )
        try:
            result = json.loads(result_text)
        except Exception:
            return False
        return result.get("status") == "ok"

    def _total_cost_cny(self) -> float:
        return round(
            sum(float(record.get("cost_cny", 0.0)) for record in self.records),
            6,
        )

    def _coverage_budget_metadata(self) -> Dict[str, Any]:
        deterministic_mode = bool(self.config.short_path_mode)
        effective_context = int(
            min(
                self.config.token_budget_per_section,
                self.config.model_context_budget_per_section,
                SHORT_PATH_CUMULATIVE_CONTEXT_TOKENS,
            )
            if deterministic_mode
            else min(
                self.config.token_budget_per_section,
                self.config.model_context_budget_per_section,
            )
        )
        per_call = int(
            min(self.config.context_tokens_per_model_call, SHORT_PATH_PER_CALL_CONTEXT_TOKENS)
            if deterministic_mode
            else self.config.context_tokens_per_model_call
        )
        return {
            "execution_mode": (
                "deterministic_short_path" if deterministic_mode else "react_recovery"
            ),
            "legacy_token_budget_per_section": int(self.config.token_budget_per_section),
            "effective_model_context_budget_per_section": effective_context,
            "per_call_context_budget_tokens": per_call,
            "output_reserve_tokens": int(self.config.context_output_reserve_tokens),
            "max_model_calls_per_section": int(
                SHORT_PATH_MAX_MODEL_CALLS
                if deterministic_mode
                else self.config.max_model_calls_per_section
            ),
            "max_coverage_waves": int(
                min(self.config.max_coverage_waves, SHORT_PATH_MAX_WAVES)
                if deterministic_mode
                else self.config.max_coverage_waves
            ),
            "max_audit_calls_per_section": int(
                min(self.config.max_audit_calls_per_section, SHORT_PATH_MAX_AUDIT_CALLS)
                if deterministic_mode
                else self.config.max_audit_calls_per_section
            ),
            "article_evidence_portfolio_path": str(self.article_evidence_portfolio_path),
        }

    @staticmethod
    def _coverage_telemetry(section_work_dir: Path) -> Dict[str, Any]:
        return {
            "wave": _read_json(section_work_dir / "COVERAGE_WAVE_TELEMETRY.json"),
            "phase2": _read_json(section_work_dir / "PHASE2_TELEMETRY.json"),
        }

    def _write_manifest(
        self,
        bundles: Dict[str, SectionMaterialBundle],
        final_status: str = "running",
    ) -> None:
        serialized_bundles = {
            section_id: {
                "material_package_path": str(
                    bundle.material_package_path
                ),
                "source_ledger_path": str(bundle.source_ledger_path),
                "kb_sqlite": (
                    str(bundle.kb_sqlite) if bundle.kb_sqlite else ""
                ),
                "staging_kb_sqlite": (
                    str(bundle.staging_kb_sqlite)
                    if bundle.staging_kb_sqlite
                    else ""
                ),
            }
            for section_id, bundle in bundles.items()
        }
        records = list(self.records)
        manifest_path = self.work_dir / "SECTION_COVERAGE_RUN.json"
        if self.config.preserve_existing_manifest and manifest_path.exists():
            previous = _read_json(manifest_path)
            updated_ids = {
                str(record.get("section_id") or "") for record in records
            }
            records = [
                record
                for record in previous.get("sections", [])
                if isinstance(record, dict)
                and str(record.get("section_id") or "") not in updated_ids
            ] + records
            serialized_bundles = {
                **dict(previous.get("material_bundles") or {}),
                **serialized_bundles,
            }

        payload = {
            "schema_version": "research_harness.section_coverage_run.v1",
            "run_id": self.run_id,
            "status": final_status,
            "blueprint_path": str(self.config.blueprint_path),
            "base_kb_sqlite": (
                str(self.config.base_kb_sqlite)
                if self.config.base_kb_sqlite
                else ""
            ),
            "supplemental_kb_sqlite": (
                str(self.staging_kb) if self.staging_kb.exists() else ""
            ),
            "sections": records,
            "material_bundles": serialized_bundles,
            "coverage_efficiency": {
                **self._coverage_budget_metadata(),
                "sections_with_wave_telemetry": sum(
                    bool(record.get("coverage_telemetry", {}).get("wave"))
                    for record in records
                ),
                "total_audit_calls": sum(
                    int(record.get("coverage_telemetry", {}).get("wave", {}).get("total_audit_calls", 0) or 0)
                    for record in records
                ),
                "total_search_calls": sum(
                    int(record.get("coverage_telemetry", {}).get("wave", {}).get("total_search_calls", 0) or 0)
                    for record in records
                ),
                "total_audit_payload_input_tokens": sum(
                    int(record.get("coverage_telemetry", {}).get("wave", {}).get("audit_payload_input_tokens", 0) or 0)
                    for record in records
                ),
            },
            "total_cost_cny": round(
                sum(float(record.get("cost_cny", 0.0)) for record in records),
                6,
            ),
            "total_input_tokens": sum(
                int(record.get("input_tokens", 0) or 0) for record in records
            ),
            "total_output_tokens": sum(
                int(record.get("output_tokens", 0) or 0) for record in records
            ),
            "total_cost_basis": (
                next(iter({
                    str(record.get("cost_basis") or "unavailable")
                    for record in records
                }))
                if len({
                    str(record.get("cost_basis") or "unavailable")
                    for record in records
                }) == 1
                else "mixed"
            ),
            "cost_is_estimated": any(
                bool(record.get("cost_is_estimated")) for record in records
            ),
            "created_or_updated_at": _now(),
        }
        manifest_path.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
