"""Full review orchestrator — coordinates multi-section review writing with global auditing.

Reads v4 blueprint, runs Section Authoring Worker for each section with cross-section
context propagation, merges sections, audits globally, and applies targeted revisions.
Reuses existing AgentScope ResearchWorker infrastructure.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]

from .global_review_auditor import GlobalReviewAuditor
from .r4_phase3_artifacts import R4Phase3ArtifactStore, _fingerprint_file
from .research_worker import ResearchWorker
from .revision_planner import RevisionPlanner
from .section_authoring_tool_registry import (
    SECTION_AUTHORING_TOOL_NAMES,
    SectionAuthoringToolProvider,
    _make_run_citation_audit,
    _make_validate_authoring_package,
    _has_durable_section_candidate,
    _restore_last_valid_section_candidate,
    _read_revision_control,
    _write_awaiting_human_review_package,
    backfill_authoring_package_stats,
)
from .compact_section_authoring import (
    COMPACT_SECTION_AUTHORING_TOOL_NAMES,
    CompactSectionAuthoringToolProvider,
)
from .targeted_revision_worker import (
    TargetedRevisionWorker,
    _demote_embedded_section_headings,
    _strip_repeated_leading_title,
)
from .task_contract import TaskContract, TaskStatus
from .tool_provider import SectionAuthoringContext

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        val = json.loads(path.read_text(encoding="utf-8"))
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def _percentile(values: List[int], ratio: float) -> int:
    """Nearest-rank percentile for small, auditable per-call samples."""
    if not values:
        return 0
    ordered = sorted(max(0, int(value)) for value in values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) * ratio) - 0.000001)))
    return ordered[index]


def _authoring_runtime_observations(section_work_dir: Path) -> Dict[str, Any]:
    """Persist token truth separately from the cumulative cost ledger."""
    calls: List[int] = []
    schema_faults = 0
    events_path = section_work_dir / "EVENTS.jsonl"
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            value = event.get("input_tokens", event.get("prompt_tokens", 0))
            try:
                if int(value or 0) > 0:
                    calls.append(int(value))
            except (TypeError, ValueError):
                pass
            rendered = json.dumps(event, ensure_ascii=False).lower()
            if "schema" in rendered and any(token in rendered for token in ("error", "invalid", "fail")):
                schema_faults += 1
    workspace_path = section_work_dir / "AUTHORING_WORKSPACE.json"
    workspace_tokens = 0
    if workspace_path.is_file():
        workspace_tokens = max(1, len(workspace_path.read_text(encoding="utf-8")) // 4)
    cumulative = int(_read_json(section_work_dir / "COST.json").get("total_input_tokens", 0) or 0)
    result = {
        "schema_version": "full_review.section_input_tokens.v1",
        "workspace_tokens_estimated": workspace_tokens,
        "model_call_input_tokens": {"count": len(calls), "p50": _percentile(calls, 0.50), "p95": _percentile(calls, 0.95), "max": max(calls, default=0)},
        "section_cumulative_input_tokens": cumulative,
        "schema_format_failure_count": schema_faults,
    }
    (section_work_dir / "SECTION_INPUT_TOKEN_OBSERVATIONS.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def _reset_short_context_after_schema_failures(section_work_dir: Path) -> Optional[Path]:
    """Archive only ReAct state once two malformed-schema turns are observed."""
    observations = _authoring_runtime_observations(section_work_dir)
    if (
        int(observations["schema_format_failure_count"]) < 2
        or not (section_work_dir / "AGENT_STATE.json").exists()
    ):
        return None
    return _archive_section_runtime_for_retry(
        section_work_dir,
        terminal_status="two_schema_format_failures",
    )


def _resolve_path(raw: Any) -> Optional[Path]:
    if not raw:
        return None
    p = Path(str(raw))
    return p if p.exists() else None


_AUTHORING_REBUILD_ARTIFACTS = (
    "RESULT.json",
    "RESULT.md",
    "AGENT_STATE.json",
    "TASK.json",
    "PLAN.md",
    "EVENTS.jsonl",
    "SECTION_ARGUMENT_PLAN.json",
    "SECTION_EVIDENCE_PACKET.json",
    "SECTION_DRAFT_EN.md",
    "SECTION_CITATION_MAP.json",
    "SECTION_AUTHORING_AUDIT.json",
    "SECTION_AUTHORING_PACKAGE.json",
    "SECTION_REVISION_HISTORY.json",
    "SECTION_VISUAL_PLACEMENT.json",
    "_audit_stale",
)
_AUTHORING_RUNTIME_ARTIFACTS = (
    "RESULT.json",
    "RESULT.md",
    "AGENT_STATE.json",
    "TASK.json",
    "PLAN.md",
    "EVENTS.jsonl",
)


def _archive_section_authoring_for_rebuild(
    section_work_dir: Path,
    *,
    reason: str,
) -> Optional[Path]:
    """Archive stale authoring state while preserving cumulative cost.

    Schema/quality upgrades can make an old completed argument plan invalid.
    ResearchWorker would otherwise restore its completed RESULT and never give
    the agent a chance to rebuild the plan.  Scientific and runtime artifacts
    are moved to an auditable archive, while COST.json and Phase-2 material are
    deliberately retained so retries remain budget-honest.
    """

    present = [
        section_work_dir / name
        for name in _AUTHORING_REBUILD_ARTIFACTS
        if (section_work_dir / name).exists()
    ]
    if not present:
        return None
    archive = (
        section_work_dir
        / "_runtime_archive"
        / f"{reason}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    )
    archive.mkdir(parents=True, exist_ok=False)
    for path in present:
        shutil.move(str(path), str(archive / path.name))
    _restore_last_valid_section_candidate(section_work_dir)
    cost_path = section_work_dir / "COST.json"
    if cost_path.exists():
        shutil.copy2(cost_path, archive / "COST.snapshot.json")
    (archive / "REBUILD_REASON.json").write_text(
        json.dumps(
            {
                "reason": reason,
                "archived_at": _now(),
                "cost_preserved_in_work_dir": cost_path.exists(),
                "artifacts": [path.name for path in present],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return archive


def _archive_section_runtime_for_retry(
    section_work_dir: Path,
    *,
    terminal_status: str,
) -> Optional[Path]:
    """Restart a terminal worker without discarding accepted science assets."""

    present = [
        section_work_dir / name
        for name in _AUTHORING_RUNTIME_ARTIFACTS
        if (section_work_dir / name).exists()
    ]
    if not present:
        return None
    archive = (
        section_work_dir
        / "_runtime_archive"
        / (
            "runtime_retry_"
            + re.sub(r"[^A-Za-z0-9_-]+", "_", terminal_status)[:32]
            + "_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "_"
            + uuid.uuid4().hex[:6]
        )
    )
    archive.mkdir(parents=True, exist_ok=False)
    for path in present:
        shutil.move(str(path), str(archive / path.name))
    _restore_last_valid_section_candidate(section_work_dir)
    (archive / "RETRY_REASON.json").write_text(
        json.dumps(
            {
                "terminal_status": terminal_status,
                "scientific_assets_preserved": True,
                "cost_preserved_in_work_dir": (
                    section_work_dir / "COST.json"
                ).exists(),
                "archived_at": _now(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return archive


_EDITOR_TRANSACTION_FILES = (
    "RESULT.json",
    "RESULT.md",
    "SECTION_ARGUMENT_PLAN.json",
    "SECTION_ARGUMENT_PLAN_HISTORY.json",
    "SECTION_EVIDENCE_PACKET.json",
    "SECTION_EVIDENCE_PACKET_HISTORY.json",
    "SECTION_DRAFT_EN.md",
    "SECTION_CITATION_MAP.json",
    "SECTION_AUTHORING_AUDIT.json",
    "SECTION_AUTHORING_PACKAGE.json",
    "SECTION_REVISION_HISTORY.json",
    "SECTION_VISUAL_PLACEMENT.json",
    "SECTION_COVERAGE_FEEDBACK.json",
    "_audit_stale",
    ".citation_audit_stale",
)


def _snapshot_section_editor_transaction(
    section_work_dir: Path,
) -> Dict[str, Optional[bytes]]:
    """Capture canonical section artifacts before an editor-directed rerun."""
    snapshot: Dict[str, Optional[bytes]] = {}
    for name in _EDITOR_TRANSACTION_FILES:
        path = section_work_dir / name
        snapshot[name] = path.read_bytes() if path.exists() else None
    return snapshot


def _restore_section_editor_transaction(
    section_work_dir: Path,
    snapshot: Dict[str, Optional[bytes]],
) -> None:
    """Roll back every canonical artifact after a failed editor rerun."""
    for name in _EDITOR_TRANSACTION_FILES:
        path = section_work_dir / name
        payload = snapshot.get(name)
        if payload is None:
            if path.exists():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)


def _compact_authoring_task_metadata(config: "OrchestratorConfig") -> Dict[str, Any]:
    """Per-authoring context allowance for the compact structured workspace.

    ResearchWorker reads ``context_tool_result_limit`` from the task metadata
    (global default 1800 tokens).  The compact workspace targets 20k-25k
    estimated tokens, so a per-task allowance is supplied here instead of
    touching the global default.
    """

    return {
        "context_tool_result_limit": max(
            1, int(config.compact_tool_result_limit or 32_000)
        ),
        "compact_workspace_target_tokens": max(
            1, int(config.compact_workspace_target_tokens or 25_000)
        ),
    }


@dataclass
class SectionMaterialBundle:
    """Resolved Phase-2 material paths for one section."""
    material_package_path: Optional[Path] = None
    source_ledger_path: Optional[Path] = None
    kb_sqlite: Optional[Path] = None
    staging_kb_sqlite: Optional[Path] = None
    synthesis_bundle_path: Optional[Path] = None
    section_overlay_path: Optional[Path] = None
    phase3_artifacts_root: Optional[Path] = None
    additional_kb_sqlite_paths: List[Path] = field(default_factory=list)
    phase3_payload: Dict[str, Any] = field(default_factory=dict)
    phase3_audit_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrchestratorConfig:
    """Configuration for FullReviewOrchestrator."""

    blueprint_path: Path
    output_root: Path
    max_revision_rounds: int = 3
    improvement_threshold: float = 0.05
    # B+ is the normal author. A-tier models are reserved for the review lead
    # and managing-editor escalation rather than multiplied across all sections.
    section_model_tier: str = "advanced_model"
    # A section normally needs context inspection, planning, evidence binding,
    # drafting, one or more audits, visual placement, and final validation.
    # Sixteen ReAct turns can stop a correct worker midway through revision.
    section_max_iters: int = 30
    # Cumulative ReAct context traffic can exceed 140k while the actual section
    # costs well below one yuan.  Keep a generous safety ceiling here and rely
    # on the independent CNY/iteration/wall-time gates for cost control.
    section_token_budget: int = 1_000_000
    section_cost_budget_cny: float = 3.0
    # Legacy/non-compact retries keep an incremental allowance. Compact
    # authoring recovery below uses the full compact ceiling after archiving
    # the failed dialogue, so a stale oversized conversation is not replayed.
    section_retry_token_allowance: int = 350_000
    section_retry_cost_allowance_cny: float = 1.0
    section_revision_token_budget: int = 400_000
    section_revision_cost_budget_cny: float = 1.2
    section_wall_time_seconds: float = 600.0
    # Production first drafts use the bounded coarse-grained workbench.  The
    # legacy fine-grained ReAct tools remain available as an explicit fallback
    # for difficult migrations and tests.
    compact_authoring_mode: bool = False
    # Compact authoring still performs context loading, argument planning,
    # drafting, auditing, and package validation. Eight ReAct turns routinely
    # stopped valid sections before their package was materialized; cost and
    # wall-time contracts remain the real safety controls.
    compact_section_max_iters: int = 24
    # A compact authoring repair can legitimately re-submit a large evidence
    # packet several times while the canonical provenance gate removes
    # unsupported claims.  Keep the context-traffic ceiling high enough for
    # that bounded repair path; CNY, iteration, and wall-time gates still
    # control runaway work.
    compact_section_token_budget: int = 1_000_000
    # Number of core evidence chunks exposed to the chapter author after the
    # upstream claim pool has already performed broad batched reading.
    authoring_core_chunk_limit: int = 12
    # Adaptive core batch bounds for compact authoring (8..16 by default).
    authoring_core_chunk_min: int = 8
    authoring_core_chunk_max: int = 16
    # Per-authoring TaskContract tool-result allowance (tokens).  The compact
    # structured workspace targets 20k-25k estimated tokens; the global
    # ResearchWorker default (1800) is never changed.
    compact_tool_result_limit: int = 32_000
    compact_workspace_target_tokens: int = 25_000
    # Optional explicit material bundle mapping: section_id → SectionMaterialBundle
    material_bundles: Optional[Dict[str, SectionMaterialBundle]] = None
    # Optional root of the Phase-3 run artifacts.  When present, R4 uses the
    # CoverageAtlas, SynthesisBundles, bindings, relations, and visual IDs from
    # this root as the authoritative authoring contract.
    phase3_artifacts_root: Optional[Path] = None
    # A declared Phase-3 root is canonical by default.  Legacy plural
    # artifacts are accepted only when the caller explicitly selects the
    # migration mode; the migration marker is persisted in the R4 payload.
    phase3_handoff_mode: str = "canonical"
    # Optional model override: either a model instance or Callable[[SectionAuthoringContext], model]
    model_override: Optional[Any] = None
    # Whether to run the LLM-based Layer 2 audit (disable for offline/minimal mode)
    use_llm_audit: bool = False
    audit_model_tier: str = "premium_model"
    audit_cost_budget_cny: float = 4.0
    audit_model_override: Optional[Any] = None
    m1_library_path: Optional[Path] = None
    # Hard ceiling for this authoring/revision stage. Earlier pipeline stages
    # have their own ledgers and are aggregated by the unified orchestrator.
    run_cost_budget_cny: float = 35.0


@dataclass
class OrchestratorResult:
    """Result of a full review orchestration run."""

    run_id: str
    status: str  # completed, awaiting_human_review, failed, blocked, partial
    sections_completed: int
    sections_failed: int
    total_flags: int
    blocking_flags: int
    revision_rounds: int
    total_cost_usd: float
    wall_time_seconds: float
    work_dir: Path
    total_cost_cny: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


class FullReviewOrchestrator:
    """Orchestrates multi-section review writing with global quality control."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        run_dir: Optional[Path] = None,
    ) -> None:
        self.config = config
        self._requested_run_dir = run_dir
        self._run_id: Optional[str] = None
        self._work_dir: Optional[Path] = None
        self._state: Dict[str, Any] = {}
        self._section_registry: Dict[str, Any] = {}
        self._auditor = GlobalReviewAuditor()
        self._planner = RevisionPlanner()
        self._revision_worker = TargetedRevisionWorker()
        self._written_artifacts: Dict[str, Path] = {}
        self._phase3_store: Optional[R4Phase3ArtifactStore] = None
        self._merged_section_metadata: List[Dict[str, Any]] = []
        self._excluded_section_metadata: List[Dict[str, Any]] = []
        self._reconciliation_metadata: Dict[str, Any] = {}

    def run(self, section_ids: Optional[List[str]] = None) -> OrchestratorResult:
        """Run full review orchestration from scratch."""
        self._run_id = (
            self._requested_run_dir.name
            if self._requested_run_dir
            else "fro_" + uuid.uuid4().hex[:8]
        )
        self._work_dir = (
            self._requested_run_dir
            if self._requested_run_dir
            else self.config.output_root / self._run_id
        )
        self._work_dir.mkdir(parents=True, exist_ok=True)

        start_time = datetime.now(timezone.utc)

        try:
            # Load blueprint
            self._update_state("loading_blueprint")
            blueprint = self._load_blueprint()

            # Build section list
            sections = self._extract_sections(blueprint, section_ids)
            if not sections:
                return self._fail_result("No sections found in blueprint", start_time)

            # Write orchestration context
            self._write_orchestration_context(blueprint, sections)

            # Initialize section registry and state
            self._initialize_registry(sections)

            # Author sections
            self._update_state("authoring")
            for i, section in enumerate(sections):
                section_id = section["section_id"]
                logger.info(f"authoring section {i+1}/{len(sections)}: {section_id}")

                success = self._run_one_section(
                    section,
                    preceding_section=sections[i-1] if i > 0 else None,
                    following_section=sections[i+1] if i < len(sections)-1 else None,
                    blueprint=blueprint
                )

                if not success:
                    # Preserve needs_more_literature — only overwrite if not already set
                    current_status = self._section_registry["sections"][i].get("status", "")
                    if current_status not in (
                        "needs_more_literature",
                        "awaiting_human_review",
                    ):
                        self._section_registry["sections"][i]["status"] = "failed"
                    logger.warning(f"section {section_id} failed (status={self._section_registry['sections'][i]['status']})")

                self._checkpoint_registry()

            # Merge drafts
            self._update_state("merging")
            self._merge_drafts()

            # Run audit-revision loop
            final_flags = self._run_audit_revision_loop()
            final_flags = self._reconcile_final_audit(final_flags)
            self._checkpoint_registry()

            # Write final package
            status = self._determine_final_status(final_flags)
            self._update_state(status)
            self._write_final_package(status, final_flags)

            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

            return OrchestratorResult(
                run_id=self._run_id,
                status=status,
                sections_completed=sum(1 for s in self._section_registry["sections"] if s["status"] == "completed"),
                sections_failed=sum(1 for s in self._section_registry["sections"] if s["status"] == "failed"),
                total_flags=final_flags.get("total_flags", 0),
                blocking_flags=final_flags.get("blocking_flags", 0),
                revision_rounds=self._state.get("revision_rounds_completed", 0),
                total_cost_cny=self._calculate_total_cost_cny(),
                total_input_tokens=self._calculate_total_tokens()[0],
                total_output_tokens=self._calculate_total_tokens()[1],
                total_cost_usd=self._calculate_total_cost(),
                wall_time_seconds=elapsed,
                work_dir=self._work_dir,
            )

        except Exception as exc:
            logger.exception("orchestration failed")
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            self._update_state("failed", stop_reason=str(exc))
            # Failure must not erase already incurred section/audit usage.  The
            # registry and COST ledgers are authoritative even when a later
            # orchestration step raises.
            return self._build_result_from_state("failed", elapsed=elapsed)

    def resume(self, run_dir: Path) -> OrchestratorResult:
        """Resume from an existing run directory. Idempotent: safe to call on completed runs."""
        state_path = run_dir / "REVIEW_STATE.json"
        registry_path = run_dir / "SECTION_REGISTRY.json"
        if not state_path.exists() or not registry_path.exists():
            raise FileNotFoundError(f"Cannot resume: missing state files in {run_dir}")

        self._work_dir = run_dir
        self._written_artifacts = {}
        self._state = _read_json(state_path)
        self._run_id = self._state.get("run_id") or ("fro_resumed_" + run_dir.name)
        self._section_registry = _read_json(registry_path)

        current_state = self._state.get("state", "")
        if current_state in ("completed", "failed"):
            return self._build_result_from_state(current_state)

        start_time = datetime.now(timezone.utc)
        try:
            blueprint = self._load_blueprint()
            # The registry is the durable admission contract for a resumed R4
            # run.  Do not silently widen a partial handoff back to every
            # section in the blueprint after restart.
            registered_section_ids = [
                str(item.get("section_id"))
                for item in self._section_registry.get("sections", [])
                if isinstance(item, dict) and item.get("section_id")
            ]
            sections = self._extract_sections(
                blueprint,
                registered_section_ids or None,
            )

            # Re-validate completed sections — re-author if SECTION_DRAFT_EN.md missing
            for reg_sec in self._section_registry.get("sections", []):
                if reg_sec["status"] == "completed":
                    draft = Path(reg_sec.get("work_dir", "")) / "SECTION_DRAFT_EN.md"
                    if not draft.exists():
                        reg_sec["status"] = "pending"
                        continue
                    section_id = str(reg_sec.get("section_id") or "")
                    section_index = next(
                        (
                            index
                            for index, value in enumerate(sections)
                            if str(value.get("section_id") or "")
                            == section_id
                        ),
                        -1,
                    )
                    if section_index >= 0:
                        try:
                            section = sections[section_index]
                            ctx = self._build_section_context(
                                section,
                                (
                                    sections[section_index - 1]
                                    if section_index > 0
                                    else None
                                ),
                                (
                                    sections[section_index + 1]
                                    if section_index < len(sections) - 1
                                    else None
                                ),
                                blueprint,
                                Path(reg_sec.get("work_dir", "")),
                            )
                            validation = (
                                _make_validate_authoring_package(ctx)()
                            )
                            if (
                                "insufficient synthesis-source diversity"
                                in validation
                            ):
                                reg_sec["status"] = "pending"
                                reg_sec["quality_revalidation"] = (
                                    "synthesis_source_diversity_rebuild"
                                )
                            elif "VALIDATION_PASSED" not in validation:
                                # Any newly introduced deterministic quality
                                # rule must be able to reopen an old "completed"
                                # section. Restricting migration to one named
                                # rule lets stale packages bypass later schema
                                # and quality improvements.
                                reg_sec["status"] = "pending"
                                reg_sec["quality_revalidation"] = (
                                    "deterministic_quality_repair"
                                )
                        except Exception as exc:
                            logger.warning(
                                "section %s quality migration precheck "
                                "failed without invalidating its durable "
                                "package: %s",
                                section_id,
                                exc,
                            )
                elif reg_sec.get("status") == "failed":
                    result = _read_json(
                        Path(reg_sec.get("work_dir", "")) / "RESULT.json"
                    )
                    if result.get("status") in {
                        "budget_exhausted",
                        "validation_failed",
                    }:
                        reg_sec["status"] = str(result["status"])

            # Resume authoring from first incomplete section
            if current_state in (
                "authoring",
                "loading_blueprint",
                "pending",
                "merging",
                "partial",
            ) or any(
                section.get("status") == "pending"
                for section in self._section_registry.get("sections", [])
            ):
                self._update_state("authoring")
                for i, section in enumerate(sections):
                    sid = section["section_id"]
                    reg = next((s for s in self._section_registry["sections"]
                                if s["section_id"] == sid), None)
                    if reg and reg["status"] in ("completed", "needs_more_literature"):
                        continue
                    success = self._run_one_section(
                        section,
                        preceding_section=sections[i - 1] if i > 0 else None,
                        following_section=sections[i + 1] if i < len(sections) - 1 else None,
                        blueprint=blueprint,
                    )
                    if (
                        not success
                        and reg
                        and reg.get("status") in {"pending", "running", ""}
                    ):
                        reg["status"] = "failed"
                    self._checkpoint_registry()

            self._update_state("merging")
            self._merge_drafts()
            final_flags = self._run_audit_revision_loop()
            final_flags = self._reconcile_final_audit(final_flags)
            self._checkpoint_registry()
            status = self._determine_final_status(final_flags)
            self._update_state(status)
            self._write_final_package(status, final_flags)

            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            return OrchestratorResult(
                run_id=self._run_id,
                status=status,
                sections_completed=sum(1 for s in self._section_registry["sections"] if s["status"] == "completed"),
                sections_failed=sum(1 for s in self._section_registry["sections"] if s["status"] == "failed"),
                total_flags=final_flags.get("total_flags", 0),
                blocking_flags=final_flags.get("blocking_flags", 0),
                revision_rounds=self._state.get("revision_rounds_completed", 0),
                total_cost_cny=self._calculate_total_cost_cny(),
                total_input_tokens=self._calculate_total_tokens()[0],
                total_output_tokens=self._calculate_total_tokens()[1],
                total_cost_usd=self._calculate_total_cost(),
                wall_time_seconds=elapsed,
                work_dir=self._work_dir,
            )
        except Exception as exc:
            logger.exception("resume failed")
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            self._update_state("failed", stop_reason=str(exc))
            return self._build_result_from_state("failed", elapsed=elapsed)

    def _load_blueprint(self) -> Dict[str, Any]:
        """Load a supported legacy-v4 or Research Harness blueprint."""
        if not self.config.blueprint_path.exists():
            raise FileNotFoundError(f"Blueprint not found: {self.config.blueprint_path}")

        try:
            blueprint = json.loads(self.config.blueprint_path.read_text(encoding="utf-8"))
            schema = blueprint.get("schema_version", "")
            supported = (
                schema.startswith("dynamic_review_blueprint.v4")
                or schema.startswith("research_harness.review_blueprint.v1")
                or schema.startswith("research_harness.review_blueprint.v2")
                or schema.startswith("research_harness.phase2_phase3_s01_feedback_blueprint.v1")
            )
            if not supported:
                logger.warning(
                    "Blueprint schema %s is not a recognized review schema",
                    schema,
                )
            # Permit an upstream Phase-3 run to hand off its artifact root in
            # the blueprint.  An explicit runtime config still wins.  This
            # keeps the production path usable after a restart without making
            # the author guess which historical output directory is current.
            if self.config.phase3_artifacts_root is None:
                raw_phase3_root = (
                    blueprint.get("phase3_artifacts_root")
                    or blueprint.get("phase3_artifact_root")
                    or blueprint.get("phase3_run_dir")
                )
                if raw_phase3_root:
                    candidate = Path(str(raw_phase3_root))
                    if candidate.exists():
                        self.config.phase3_artifacts_root = candidate
            return blueprint
        except Exception as exc:
            raise ValueError(f"Failed to load blueprint: {exc}")

    def _extract_sections(
        self, blueprint: Dict[str, Any], section_ids: Optional[List[str]]
    ) -> List[Dict[str, Any]]:
        """Extract ordered section list from blueprint."""
        all_sections = blueprint.get("sections", [])

        if section_ids is not None:
            # Filter to requested sections, preserving order
            sections = [s for s in all_sections if s.get("section_id") in section_ids]
        else:
            sections = all_sections

        return sections

    def _write_orchestration_context(
        self, blueprint: Dict[str, Any], sections: List[Dict[str, Any]]
    ) -> None:
        """Write REVIEW_ORCHESTRATION_CONTEXT.json."""
        context = {
            "schema_version": "phase4.orchestration.v1",
            "run_id": self._run_id,
            "blueprint_path": str(self.config.blueprint_path),
            "section_count": len(sections),
            "section_ids": [s["section_id"] for s in sections],
            "full_review_argument": blueprint.get("input_context", {}).get("problem_understanding", ""),
            "config": {
                "max_revision_rounds": self.config.max_revision_rounds,
                "improvement_threshold": self.config.improvement_threshold,
                "section_model_tier": self.config.section_model_tier,
                "phase3_artifacts_root": (
                    str(self.config.phase3_artifacts_root)
                    if self.config.phase3_artifacts_root
                    else ""
                ),
                "phase3_handoff_mode": self.config.phase3_handoff_mode,
            },
            "created_at": _now(),
        }

        path = self._work_dir / "REVIEW_ORCHESTRATION_CONTEXT.json"
        path.write_text(json.dumps(context, indent=2), encoding="utf-8")
        self._register_artifact("orchestration_context", path)

    def _initialize_registry(self, sections: List[Dict[str, Any]]) -> None:
        """Initialize SECTION_REGISTRY.json."""
        self._section_registry = {
            "schema_version": "phase4.section_registry.v1",
            "run_id": self._run_id,
            "sections": [
                {
                    "section_id": s["section_id"],
                    "title": s.get("title", ""),
                    "argument_role": s.get("argument_role", ""),
                    "status": "pending",
                    "work_dir": "",
                    "cost_cny": 0.0,
                    "cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "blocking_flags": 0,
                    "transition_contract": s.get("transition_to_next", {}),
                    "visual_argument_slots": s.get("visual_argument_slots", []),
                }
                for s in sections
            ],
        }
        self._checkpoint_registry()

    def _checkpoint_registry(self) -> None:
        """Write SECTION_REGISTRY.json."""
        path = self._work_dir / "SECTION_REGISTRY.json"
        path.write_text(json.dumps(self._section_registry, indent=2), encoding="utf-8")
        self._register_artifact("section_registry", path)

    def _update_state(self, state: str, **kwargs) -> None:
        """Update and write REVIEW_STATE.json, and append to EVENTS.jsonl."""
        self._state["state"] = state
        self._state["updated_at"] = _now()
        self._state.update(kwargs)

        if self._work_dir:
            path = self._work_dir / "REVIEW_STATE.json"
            path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            self._register_artifact("review_state", path)
            self._log_event({"state": state, **{k: v for k, v in kwargs.items() if not k.startswith("_")}})

    def _run_one_section(
        self,
        section: Dict[str, Any],
        preceding_section: Optional[Dict[str, Any]],
        following_section: Optional[Dict[str, Any]],
        blueprint: Dict[str, Any],
    ) -> bool:
        """Run Section Authoring Worker for one section."""
        section_id = section["section_id"]

        if self._calculate_total_cost_cny() >= self.config.run_cost_budget_cny:
            logger.warning(
                "Run cost budget reached before section %s; no new model call admitted",
                section_id,
            )
            section_meta = next(
                (
                    item
                    for item in self._section_registry.get("sections", [])
                    if item.get("section_id") == section_id
                ),
                None,
            )
            if section_meta is not None:
                section_meta["status"] = "budget_exhausted"
            return False

        # Find section metadata in registry
        section_meta = next((s for s in self._section_registry["sections"] if s["section_id"] == section_id), None)
        if not section_meta:
            return False

        # Build section work_dir
        section_work_dir = self._work_dir / "sections" / section_id
        section_work_dir.mkdir(parents=True, exist_ok=True)
        section_meta["work_dir"] = str(section_work_dir)

        # Check material availability — fail-closed if no material found or KB is empty
        bundle = self._discover_phase2_material(section_id)
        if bundle is None:
            logger.warning(f"section {section_id}: no Phase-2 material found → needs_more_literature")
            section_meta["status"] = "needs_more_literature"
            return False

        if bundle.kb_sqlite is not None and bundle.kb_sqlite.exists():
            try:
                import sqlite3 as _sq3
                conn = _sq3.connect(str(bundle.kb_sqlite))
                try:
                    row_count = conn.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0]
                finally:
                    conn.close()
                if row_count == 0:
                    logger.warning(f"section {section_id}: KB has 0 rows → needs_more_literature")
                    section_meta["status"] = "needs_more_literature"
                    return False
            except Exception:
                pass  # if KB is unreadable, let ResearchWorker handle the error

        # Build SectionAuthoringContext
        ctx = self._build_section_context(
            section, preceding_section, following_section, blueprint, section_work_dir
        )

        # Resume fast path: old audit findings may have been produced by a
        # superseded deterministic rule. Re-audit the durable draft before
        # paying a model to rewrite it. If the current code accepts the package,
        # finish the section without another model call.
        draft_path = section_work_dir / "SECTION_DRAFT_EN.md"
        if (
            draft_path.exists()
            and draft_path.stat().st_size
            and (section_work_dir / "SECTION_ARGUMENT_PLAN.json").exists()
            and (section_work_dir / "SECTION_EVIDENCE_PACKET.json").exists()
        ):
            try:
                audit_result = json.loads(
                    _make_run_citation_audit(ctx)("[]")
                )
                if bool(audit_result.get("audit_passed")):
                    validation = _make_validate_authoring_package(ctx)()
                    if "VALIDATION_PASSED" in validation:
                        previous = _read_json(
                            section_work_dir / "RESULT.json"
                        )
                        section_meta["status"] = "completed"
                        section_meta["authoring_status"] = "completed"
                        section_meta["blocking_flags"] = 0
                        # Registry totals may include failed editor retries
                        # stored in isolated runtimes. Never erase that honest
                        # cumulative spend with an older accepted RESULT.json.
                        section_meta["cost_cny"] = max(
                            float(section_meta.get("cost_cny", 0.0) or 0.0),
                            float(previous.get("estimated_cost_cny", 0.0) or 0.0),
                        )
                        section_meta["cost_usd"] = max(
                            float(section_meta.get("cost_usd", 0.0) or 0.0),
                            float(previous.get("estimated_cost_usd", 0.0) or 0.0),
                        )
                        section_meta["input_tokens"] = max(
                            int(section_meta.get("input_tokens", 0) or 0),
                            int(previous.get("total_input_tokens", 0) or 0),
                        )
                        section_meta["output_tokens"] = max(
                            int(section_meta.get("output_tokens", 0) or 0),
                            int(previous.get("total_output_tokens", 0) or 0),
                        )
                        section_meta[
                            "resume_fast_path"
                        ] = "deterministic_reaudit_passed"
                        self._extract_terminology(section_work_dir)
                        self._write_cost_json()
                        self._record_claim_ownership(section, section_id)
                        return True
                    if "insufficient synthesis-source diversity" in validation:
                        archive = _archive_section_authoring_for_rebuild(
                            section_work_dir,
                            reason="synthesis_source_diversity",
                        )
                        section_meta["resume_fast_path"] = (
                            "quality_rebuild_required"
                        )
                        section_meta["quality_rebuild_archive"] = (
                            str(archive) if archive else ""
                        )
                        logger.info(
                            "section %s reopened after synthesis-source "
                            "diversity migration; prior cost retained",
                            section_id,
                        )
                    elif (
                        "VALIDATION_FAILED" in validation
                        and (section_work_dir / "AGENT_STATE.json").exists()
                    ):
                        # Durable scientific artifacts are the authoritative
                        # memory for section repair. Carrying a long sequence
                        # of failed ReAct turns back into the model inflates
                        # latency and tokens without adding scientific value.
                        archive = _archive_section_runtime_for_retry(
                            section_work_dir,
                            terminal_status="deterministic_validation_failed",
                        )
                        section_meta["runtime_retry_archive"] = (
                            str(archive) if archive else ""
                        )
                        section_meta["resume_fast_path"] = (
                            "fresh_runtime_after_validation_failure"
                        )
                        logger.info(
                            "section %s retained its draft/plan/evidence but "
                            "discarded stale agent dialogue before revision",
                            section_id,
                        )
                elif any(
                    str(item.get("type") or "")
                    == "insufficient_synthesis_source_diversity"
                    for item in audit_result.get("flags_detail", [])
                    if isinstance(item, dict)
                ):
                    # The scientific plan and evidence packet are already
                    # broad enough; only the finished prose collapsed onto too
                    # few sources. Restart the agent runtime while preserving
                    # those durable scientific artifacts so it can revise the
                    # section instead of rebuilding or re-searching.
                    archive = _archive_section_runtime_for_retry(
                        section_work_dir,
                        terminal_status="finished_section_source_diversity",
                    )
                    section_meta["runtime_retry_archive"] = (
                        str(archive) if archive else ""
                    )
                    section_meta["resume_fast_path"] = (
                        "finished_section_source_diversity_retry"
                    )
                    logger.info(
                        "section %s reopened because the finished draft used "
                        "too few of its planned audited sources",
                        section_id,
                    )
                elif (
                    int(audit_result.get("blocking_flags", 0) or 0) > 0
                    and (section_work_dir / "AGENT_STATE.json").exists()
                ):
                    archive = _archive_section_runtime_for_retry(
                        section_work_dir,
                        terminal_status="deterministic_reaudit_failed",
                    )
                    section_meta["runtime_retry_archive"] = (
                        str(archive) if archive else ""
                    )
                    section_meta["resume_fast_path"] = (
                        "fresh_runtime_after_reaudit_failure"
                    )
                    logger.info(
                        "section %s retained its scientific artifacts but "
                        "discarded stale failed dialogue before revision",
                        section_id,
                    )
            except Exception as exc:
                logger.warning(
                    "section %s deterministic resume re-audit failed: %s",
                    section_id,
                    exc,
                )

        terminal_result = _read_json(section_work_dir / "RESULT.json")
        partial_cost = _read_json(section_work_dir / "COST.json")
        partial_input_tokens = int(
            partial_cost.get("total_input_tokens", 0) or 0
        )
        if (
            not str(terminal_result.get("status") or "")
            and (section_work_dir / "AGENT_STATE.json").exists()
            and partial_input_tokens
            >= max(100_000, int(self.config.section_token_budget * 0.75))
        ):
            archive = _archive_section_runtime_for_retry(
                section_work_dir,
                terminal_status="interrupted_near_token_limit",
            )
            section_meta["runtime_retry_archive"] = (
                str(archive) if archive else ""
            )
            section_meta["resume_fast_path"] = (
                "fresh_runtime_after_interrupted_near_limit"
            )
            terminal_result = {}

        terminal_status = str(terminal_result.get("status") or "")
        if terminal_status in {"budget_exhausted", "validation_failed"}:
            archive = _archive_section_runtime_for_retry(
                section_work_dir,
                terminal_status=terminal_status,
            )
            section_meta["runtime_retry_archive"] = (
                str(archive) if archive else ""
            )

        # Run ResearchWorker (pass model_override for scripted/test mode)
        # model_override may be: a model instance, or Callable[[SectionAuthoringContext], model]
        _mo = self.config.model_override
        _model = _mo(ctx) if callable(_mo) else _mo
        compact_mode = bool(self.config.compact_authoring_mode)
        provider = (
            CompactSectionAuthoringToolProvider(ctx)
            if compact_mode
            else SectionAuthoringToolProvider(ctx)
        )
        prompt = (
            self._load_compact_system_prompt()
            if compact_mode
            else self._load_system_prompt()
        )
        worker = ResearchWorker(
            tool_provider=provider,
            runs_root=self.config.output_root,
            _system_prompt_override=prompt,
            _model_override=_model,
            # Canonical dir: provider writes here, StopController checks here
            _work_dir_override=section_work_dir,
        )

        prior_cost = _read_json(section_work_dir / "COST.json")
        prior_input_tokens = int(
            prior_cost.get("total_input_tokens", 0) or 0
        )
        prior_cost_cny = float(
            prior_cost.get("estimated_cost_cny", 0.0) or 0.0
        )
        fresh_runtime_after_archive = (
            not (section_work_dir / "AGENT_STATE.json").exists()
            and (section_work_dir / "_runtime_archive").exists()
        )
        effective_token_budget = (
            min(
                self.config.section_token_budget,
                self.config.compact_section_token_budget,
            )
            if compact_mode
            else self.config.section_token_budget
        )
        effective_cost_budget = self.config.section_cost_budget_cny
        if fresh_runtime_after_archive and prior_input_tokens:
            # The archived ReAct dialogue is no longer part of the new model
            # context. For compact authoring, give the clean retry the same
            # full ceiling as a fresh chapter: a prior 500k admission stop
            # must not be followed by a smaller 350k incremental retry. The
            # CNY, iteration, and wall-time gates remain active.
            if compact_mode:
                effective_token_budget = min(
                    self.config.section_token_budget,
                    self.config.compact_section_token_budget,
                )
            else:
                effective_token_budget = (
                    prior_input_tokens
                    + max(1, self.config.section_retry_token_allowance)
                )
            effective_cost_budget = (
                prior_cost_cny
                + max(0.1, self.config.section_retry_cost_allowance_cny)
            )

        contract = TaskContract(
            run_id=self._run_id,
            task_id=f"authoring_{section_id}",
            goal=f"Author section {section_id}: {section.get('title', '')}",
            metadata=(
                _compact_authoring_task_metadata(self.config)
                if compact_mode
                else {}
            ),
            allowed_tools=(
                COMPACT_SECTION_AUTHORING_TOOL_NAMES
                if compact_mode
                else SECTION_AUTHORING_TOOL_NAMES
            ),
            skill_ids=[] if compact_mode else ["section-review-authoring"],
            model_tier=self.config.section_model_tier,
            max_iters=(
                min(
                    self.config.section_max_iters,
                    self.config.compact_section_max_iters,
                )
                if compact_mode
                else self.config.section_max_iters
            ),
            token_budget=effective_token_budget,
            cost_budget_cny=effective_cost_budget,
            next_call_cost_reserve_cny=0.25,
            wall_time_budget_seconds=self.config.section_wall_time_seconds,
            expected_outputs=["SECTION_AUTHORING_CONTEXT.json", "SECTION_ARGUMENT_PLAN.json",
                            "SECTION_DRAFT_EN.md", "SECTION_AUTHORING_PACKAGE.json"],
        )

        try:
            result = worker.run(contract)
            backfill_authoring_package_stats(section_work_dir, result)
            archive = _reset_short_context_after_schema_failures(section_work_dir)
            if archive is not None:
                # The next bounded repair receives durable section artifacts,
                # not the two failed schema conversations.  This is a context
                # reset, not a retry/budget expansion.
                section_meta["runtime_retry_archive"] = str(archive)
                section_meta["short_context_reset_after_schema_failures"] = True

            # Update registry
            worker_status = (
                result.status.value
                if hasattr(result.status, "value")
                else str(result.status)
            )
            package = _read_json(
                section_work_dir / "SECTION_AUTHORING_PACKAGE.json"
            )
            authoring_status = str(
                package.get("authoring_status") or worker_status
            )
            # A bounded authoring worker may have produced a useful draft and
            # complete provenance artifacts before its final validator could
            # clear a technical/global flag.  Preserve that candidate as a
            # review handoff instead of converting a safe stop into failure.
            if (
                worker_status in {"budget_exhausted", "validation_failed", "waiting_for_human"}
                and _has_durable_section_candidate(section_work_dir)
            ):
                control = _read_revision_control(ctx)
                _write_awaiting_human_review_package(
                    ctx,
                    reason=(
                        str(control.get("stop_reason") or "worker_stopped_after_bounded_authoring")
                        if isinstance(control, dict)
                        else "worker_stopped_after_bounded_authoring"
                    ),
                    control=control if isinstance(control, dict) else None,
                )
                package = _read_json(section_work_dir / "SECTION_AUTHORING_PACKAGE.json")
                authoring_status = "awaiting_human_review"
                worker_status = "awaiting_human_review"
            elif authoring_status == "awaiting_human_review":
                worker_status = "awaiting_human_review"
            section_meta["status"] = (
                "needs_more_literature"
                if authoring_status == "needs_more_literature"
                else worker_status
            )
            section_meta["authoring_status"] = authoring_status
            # ResearchWorker restores COST.json from the canonical section
            # directory, so its result is already cumulative across resumes.
            # Adding registry totals again would double-count every retry.
            section_meta["cost_cny"] = float(
                getattr(result, "estimated_cost_cny", 0.0) or 0.0
            )
            section_meta["cost_usd"] = float(
                getattr(result, "estimated_cost_usd", 0.0) or 0.0
            )
            section_meta["input_tokens"] = int(
                getattr(result, "total_input_tokens", 0) or 0
            )
            section_meta["output_tokens"] = int(
                getattr(result, "total_output_tokens", 0) or 0
            )

            # Extract terminology for next sections
            self._extract_terminology(section_work_dir)

            # Update cost tracking
            self._write_cost_json()

            # Record claim ownership
            self._record_claim_ownership(section, section_id)

            return (
                result.status == TaskStatus.completed
                and section_meta["status"] == "completed"
            )

        except Exception as exc:
            logger.exception(f"section {section_id} worker failed")
            section_meta["status"] = "failed"
            section_meta["error"] = str(exc)
            partial_cost = _read_json(section_work_dir / "COST.json")
            section_meta["cost_cny"] = max(
                float(section_meta.get("cost_cny", 0.0) or 0.0),
                float(partial_cost.get("estimated_cost_cny", 0.0) or 0.0),
            )
            section_meta["cost_usd"] = max(
                float(section_meta.get("cost_usd", 0.0) or 0.0),
                float(partial_cost.get("estimated_cost_usd", 0.0) or 0.0),
            )
            section_meta["input_tokens"] = max(
                int(section_meta.get("input_tokens", 0) or 0),
                int(partial_cost.get("total_input_tokens", 0) or 0),
            )
            section_meta["output_tokens"] = max(
                int(section_meta.get("output_tokens", 0) or 0),
                int(partial_cost.get("total_output_tokens", 0) or 0),
            )
            self._write_cost_json()
            return False

    def prepare_literature_feedback_retry(
        self,
        section_ids: List[str],
    ) -> List[str]:
        """Archive stale section outputs and reopen only feedback sections.

        The operation is deterministic and confined to this generated run
        directory. Earlier costs remain in SECTION_REGISTRY and are therefore
        included in the resumed total.
        """

        selected = {str(value) for value in section_ids if str(value)}
        reopened: List[str] = []
        if not selected:
            return reopened
        history_root = self._work_dir / ".history" / (
            "literature_feedback_" + datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )
        )
        for section in self._section_registry.get("sections", []):
            section_id = str(section.get("section_id") or "")
            if section_id not in selected:
                continue
            section_dir = Path(str(section.get("work_dir") or ""))
            try:
                if (
                    not section_dir.resolve().is_relative_to(
                        self._work_dir.resolve()
                    )
                    or not section_dir.is_dir()
                ):
                    continue
            except Exception:
                continue
            destination = history_root / section_id
            destination.mkdir(parents=True, exist_ok=True)
            for path in section_dir.iterdir():
                if path.name == ".history":
                    continue
                target = destination / path.name
                if target.exists():
                    continue
                path.replace(target)
            section["status"] = "pending"
            section["feedback_retry_count"] = int(
                section.get("feedback_retry_count", 0) or 0
            ) + 1
            reopened.append(section_id)
        if reopened:
            self._state["state"] = "authoring"
            self._state["literature_feedback_retry_sections"] = reopened
            self._checkpoint_registry()
            self._update_state("authoring")
        return reopened

    def _build_section_context(
        self,
        section: Dict[str, Any],
        preceding_section: Optional[Dict[str, Any]],
        following_section: Optional[Dict[str, Any]],
        blueprint: Dict[str, Any],
        section_work_dir: Path,
    ) -> SectionAuthoringContext:
        """Build SectionAuthoringContext with cross-section fields."""
        section_id = section["section_id"]
        section_work_dir.mkdir(parents=True, exist_ok=True)

        # Extract preceding conclusion
        preceding_conclusion = ""
        if preceding_section:
            preceding_id = preceding_section["section_id"]
            preceding_draft = self._work_dir / "sections" / preceding_id / "SECTION_DRAFT_EN.md"
            if preceding_draft.exists():
                text = preceding_draft.read_text(encoding="utf-8")
                paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
                preceding_conclusion = paragraphs[-1] if paragraphs else ""

        # Extract following role
        following_role = ""
        if following_section:
            following_role = following_section.get("argument_role", "")

        # Load terminology ledger
        terminology_ledger = self._load_terminology_ledger()

        # Preserve the Review Lead's intellectual contract.  The previous
        # implementation replaced these fields with empty lists/global text,
        # which erased the very section distinctions the author needed.
        word_range = section.get("target_word_range", {})
        if isinstance(word_range, dict):
            estimated_word_budget = int(
                word_range.get("max")
                or word_range.get("target")
                or word_range.get("min")
                or 0
            )
        elif isinstance(word_range, (list, tuple)):
            # Older blueprints encode [minimum, maximum] rather than the
            # newer {min, max} object.  Treat the midpoint as the planning
            # budget; retain the original range in section_data for the
            # authoring contract instead of crashing on int(list).
            numeric_range = []
            for value in word_range:
                try:
                    numeric_range.append(int(value))
                except (TypeError, ValueError):
                    continue
            estimated_word_budget = (
                sum(numeric_range) // len(numeric_range)
                if numeric_range
                else 0
            )
        else:
            try:
                estimated_word_budget = int(word_range or 0)
            except (TypeError, ValueError):
                estimated_word_budget = 0
        supplied_contract = dict(section.get("section_contract") or {})
        if estimated_word_budget:
            supplied_contract.setdefault(
                "word_budget",
                (
                    int(word_range.get("min", estimated_word_budget))
                    + int(word_range.get("max", estimated_word_budget))
                )
                // 2
                if isinstance(word_range, dict)
                else estimated_word_budget,
            )
        supplied_contract.setdefault(
            "key_questions",
            section.get("key_questions", []),
        )
        supplied_contract.setdefault(
            "synthesis_task",
            section.get("synthesis_task", ""),
        )
        # Coherent adaptive bounds: 1 <= min <= default <= max.
        authoring_core_min = max(
            1, int(self.config.authoring_core_chunk_min or 8)
        )
        authoring_core_max = max(
            1, int(self.config.authoring_core_chunk_max or 16)
        )
        authoring_core_default = min(
            max(
                min(authoring_core_min, authoring_core_max),
                int(self.config.authoring_core_chunk_limit or 12),
            ),
            max(authoring_core_min, authoring_core_max),
        )
        section_data = {
            "section_id": section_id,
            "title": section.get("title", ""),
            "argument_role": section.get("argument_role", ""),
            "chapter_argument": (
                section.get("chapter_argument")
                or section.get("argument_role", "")
            ),
            "key_questions": section.get("key_questions", []),
            "synthesis_task": section.get("synthesis_task", ""),
            "mentor_guidance": section.get("mentor_guidance", ""),
            "scope_guardrails": section.get("scope_guardrails", []),
            "required_roles": section.get("required_roles", []),
            "optional_roles": section.get("optional_roles", []),
            "claims": section.get("claims", []),
            "section_contract": supplied_contract,
            "target_word_range": word_range,
            "estimated_word_budget": estimated_word_budget,
            # Coherent adaptive bounds: 1 <= min <= default <= max.
            "authoring_core_chunk_min": authoring_core_min,
            "authoring_core_chunk_max": authoring_core_max,
            "authoring_core_chunk_limit": authoring_core_default,
            "compact_tool_result_limit": max(
                1, int(self.config.compact_tool_result_limit or 32_000)
            ),
            "compact_workspace_target_tokens": max(
                1, int(self.config.compact_workspace_target_tokens or 25_000)
            ),
            "visual_argument_slots": section.get(
                "visual_argument_slots",
                section.get("expected_visual_arguments", []),
            ),
            "transition_from_previous": section.get(
                "transition_from_previous", ""
            ),
            "transition_to_next": section.get("transition_to_next", ""),
        }

        # Find Phase 2 material for this section
        bundle = self._discover_phase2_material(section_id)

        # Phase 3 is the scientific authoring contract for R4.  Prefer its
        # claims/relations/strength ledger over stale blueprint placeholders,
        # while preserving the blueprint when Phase 3 has no data for this
        # section.  The payload is also persisted in the section work dir so
        # a later audit can prove exactly what the author saw.
        phase3_payload = dict(bundle.phase3_payload) if bundle else {}
        phase3_audit_payload = (
            dict(bundle.phase3_audit_payload) if bundle else {}
        )
        phase3_claims = phase3_payload.get("claims") or []
        if isinstance(phase3_claims, list) and phase3_claims:
            section_data["claims"] = phase3_claims
        if phase3_payload.get("relations"):
            section_data["relation_edges"] = phase3_payload["relations"]
        if phase3_payload:
            section_data["phase3_artifacts"] = phase3_payload
            section_data["judgment_ledger"] = phase3_payload.get(
                "judgment_ledger", []
            )
            section_data["claim_strength_policy"] = {
                "established": "factual_assertion",
                "qualified": "hedged_factual_assertion",
                "boundary": "interpretive_synthesis",
                "open": "evidence_gap_only",
            }
            visual_ids = phase3_payload.get("visual_chunk_ids") or []
            if visual_ids:
                section_data["visual_chunk_ids"] = visual_ids

            phase3_context_path = section_work_dir / "PHASE3_AUTHORING_CONTEXT.json"
            if phase3_audit_payload:
                phase3_audit_path = section_work_dir / "PHASE3_AUTHORING_AUDIT.json"
                phase3_audit_path.write_text(
                    json.dumps(
                        phase3_audit_payload,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                audit_ref = _fingerprint_file(phase3_audit_path, section_work_dir)
                if audit_ref:
                    phase3_payload["audit_artifact_ref"] = audit_ref
            phase3_context_path.write_text(
                json.dumps(phase3_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            bundle.phase3_payload["authoring_context_path"] = str(
                phase3_context_path
            )

        # Build context — material_package_path may be None if no material found
        raw_mentor = (
            section.get("mentor_advice")
            or section.get("review_mentor_advice")
            or section.get("mentor_guidance")
        )
        mentor_advice = (
            raw_mentor
            if isinstance(raw_mentor, dict)
            else ({"guidance": str(raw_mentor)} if raw_mentor else None)
        )
        full_review_argument = (
            blueprint.get("full_review_argument")
            or blueprint.get("review_thesis")
            or blueprint.get("input_context", {}).get(
                "problem_understanding", ""
            )
        )
        raw_transition = section.get("transition_to_next", {})
        transition_contract = (
            raw_transition
            if isinstance(raw_transition, dict)
            else {"to_next": str(raw_transition)}
        )
        gap_report_path = (
            bundle.material_package_path.with_name("SECTION_GAP_REPORT.json")
            if bundle
            and bundle.material_package_path
            and bundle.material_package_path.with_name(
                "SECTION_GAP_REPORT.json"
            ).exists()
            else None
        )
        synthesis_bundle_path = (
            bundle.synthesis_bundle_path
            if bundle and bundle.synthesis_bundle_path
            else (
                bundle.material_package_path.with_name("SYNTHESIS_BUNDLE.json")
                if bundle
                and bundle.material_package_path
                and bundle.material_package_path.with_name(
                    "SYNTHESIS_BUNDLE.json"
                ).exists()
                else None
            )
        )
        if phase3_payload.get("synthesis_bundle"):
            phase3_bundle_path = section_work_dir / "PHASE3_SYNTHESIS_BUNDLE.json"
            phase3_bundle_path.write_text(
                json.dumps(
                    phase3_payload["synthesis_bundle"],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            synthesis_bundle_path = phase3_bundle_path
        ctx = SectionAuthoringContext(
            section_id=section_id,
            section_data=section_data,
            kb_sqlite=bundle.kb_sqlite if bundle else None,
            temp_kb_sqlite=bundle.staging_kb_sqlite if bundle else None,
            additional_kb_sqlite_paths=(
                list(bundle.additional_kb_sqlite_paths) if bundle else []
            ),
            work_dir=section_work_dir,
            material_package_path=bundle.material_package_path if bundle else None,
            source_ledger_path=bundle.source_ledger_path if bundle else None,
            gap_report_path=gap_report_path,
            synthesis_bundle_path=synthesis_bundle_path,
            section_overlay_path=(
                bundle.section_overlay_path if bundle else None
            ),
            # Cross-section fields
            mentor_advice=mentor_advice,
            full_review_argument=full_review_argument,
            topic_identity=dict(blueprint.get("topic_identity", {})),
            section_role=section.get("argument_role", ""),
            preceding_section_conclusion=preceding_conclusion,
            following_section_role=following_role,
            transition_contract=transition_contract,
            terminology_ledger=terminology_ledger,
        )

        return ctx

    def _phase3_legacy_migration_requested(self) -> bool:
        """Return whether this caller explicitly accepts legacy Phase-3 data."""

        mode = str(self.config.phase3_handoff_mode or "canonical").strip().casefold()
        if mode in {"legacy", "legacy_migration", "migration"}:
            return True
        if mode in {"canonical", "production", "production_first"}:
            return False
        raise ValueError(
            "Unsupported phase3_handoff_mode; use 'canonical' or 'legacy_migration'."
        )

    def _discover_phase2_material(self, section_id: str) -> Optional[SectionMaterialBundle]:
        """Find Phase 2 material for a section.

        Returns None if no material found — caller must mark section needs_more_literature.
        """
        # 1. Explicit mapping takes priority
        if self.config.material_bundles:
            bundle = self.config.material_bundles.get(section_id)
            if bundle:
                return bundle

        # 2. Phase-3 artifact bridge.  This must precede the legacy directory
        # scan: otherwise an older SECTION_MATERIAL_PACKAGE silently wins and
        # the author never sees the current claims or strength calibration.
        phase3_root = self.config.phase3_artifacts_root
        if phase3_root:
            root = Path(phase3_root)
            try:
                if not root.exists():
                    logger.warning(
                        "Phase-3 R4 artifact root is missing for %s: %s",
                        section_id,
                        root,
                    )
                    return None
                if (
                    self._phase3_store is None
                    or self._phase3_store.root != root.resolve()
                ):
                    self._phase3_store = R4Phase3ArtifactStore(
                        root,
                        allow_legacy_migration=(
                            self._phase3_legacy_migration_requested()
                        ),
                    )
                artifacts = self._phase3_store.section(section_id)
                # Enforce the same fail-open-with-limits policy at the last
                # point before any author model call.  Structural failures
                # (missing canonical handoff, source ledger, KB, or any
                # authorable claim) stay closed; scientific shortfalls admit
                # the section with limits and excluded-claim audit retained.
                if not artifacts.admitted_for_authoring:
                    logger.warning(
                        "Phase-3 R4 section admission closed for %s: %s",
                        section_id,
                        "; ".join(artifacts.diagnostics[-4:]),
                    )
                    return None
                if (
                    (
                        artifacts.production_handoff_valid
                        or artifacts.legacy_migration_active
                    )
                    and (
                        artifacts.source_ledger_path
                        or artifacts.kb_paths
                        or artifacts.bundle
                        or artifacts.claims
                    )
                ):
                    phase3_payload = artifacts.to_context_payload()
                    phase3_payload["source_ledger_path"] = (
                        str(artifacts.source_ledger_path)
                        if artifacts.source_ledger_path
                        else ""
                    )
                    phase3_payload["overlay_path"] = (
                        str(artifacts.overlay_path)
                        if artifacts.overlay_path
                        else ""
                    )
                    phase3_payload["kb_paths"] = [
                        str(path) for path in artifacts.kb_paths
                    ]
                    return SectionMaterialBundle(
                        material_package_path=None,
                        source_ledger_path=artifacts.source_ledger_path,
                        kb_sqlite=(artifacts.kb_paths[0] if artifacts.kb_paths else None),
                        staging_kb_sqlite=(artifacts.kb_paths[1] if len(artifacts.kb_paths) > 1 else None),
                        section_overlay_path=artifacts.overlay_path,
                        phase3_artifacts_root=root,
                        additional_kb_sqlite_paths=list(artifacts.kb_paths[2:]),
                        phase3_payload=phase3_payload,
                        phase3_audit_payload=artifacts.to_audit_payload(),
                    )
                logger.warning(
                    "Phase-3 R4 handoff closed for %s: %s",
                    section_id,
                    "; ".join(artifacts.diagnostics[-4:]),
                )
                # A declared Phase-3 root is authoritative.  Never fall
                # through to an older Phase-2 directory after a missing or
                # incompatible canonical handoff.
                return None
            except Exception as exc:
                logger.warning(
                    "Phase-3 R4 artifact discovery failed for %s: %s",
                    section_id,
                    exc,
                )
                # The root was declared by the caller, so an error here is a
                # closed handoff rather than permission to use stale material.
                return None

        # 3. Auto-discover from Phase-2 manifest
        phase2_root = PROJECT_ROOT / "outputs" / "section_coverage_runs"
        if phase2_root.exists():
            candidates = sorted(
                phase2_root.glob(f"*/tasks/*{section_id}*/SECTION_MATERIAL_PACKAGE.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for pkg_path in candidates:
                ledger = pkg_path.with_name("SECTION_SOURCE_LEDGER.json")
                if not ledger.exists():
                    continue
                ctx_data = _read_json(pkg_path.with_name("SECTION_CONTEXT.json"))
                manifest = _read_json(pkg_path.with_name("MATERIALIZATION_MANIFEST.json"))
                kb = _resolve_path(ctx_data.get("kb_sqlite_path"))
                staging = _resolve_path(manifest.get("temp_kb_path"))
                return SectionMaterialBundle(pkg_path, ledger, kb, staging)

        # 3. No material found
        return None

    def _load_system_prompt(self) -> str:
        """Load Section Review Author system prompt."""
        prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "roles" / "Section Review Author.txt"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return ""

    def _load_compact_system_prompt(self) -> str:
        """Load the bounded production section-authoring prompt."""

        prompt_path = (
            Path(__file__).resolve().parents[2]
            / "prompts"
            / "roles"
            / "Compact Section Review Author.txt"
        )
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return self._load_system_prompt()

    def _load_terminology_ledger(self) -> Dict[str, Any]:
        """Load accumulated terminology ledger."""
        ledger_path = self._work_dir / "TERMINOLOGY_LEDGER.json"
        if ledger_path.exists():
            try:
                return json.loads(ledger_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"terms": {}}

    def _extract_terminology(self, section_work_dir: Path) -> None:
        """Extract key terms from section draft and update terminology ledger."""
        draft_path = section_work_dir / "SECTION_DRAFT_EN.md"
        if not draft_path.exists():
            return

        text = draft_path.read_text(encoding="utf-8")

        _STOP_CAPS = {
            "The", "A", "An", "In", "On", "At", "To", "For", "Of", "With",
            "And", "Or", "But", "By", "As", "Is", "Are", "Was", "Were",
            "Be", "Been", "Has", "Have", "Had", "Not", "This", "That",
            "These", "Those", "Its", "Their", "When", "Where", "Which",
            "While", "Although", "However", "Furthermore", "Therefore",
            "Section", "Figure", "Table", "Chapter",
        }

        terms: dict[str, str] = {}
        source_label = section_work_dir.name

        # Uppercase acronyms (2–6 chars)
        for m in re.finditer(r'\b([A-Z]{2,6})\b', text):
            acr = m.group(1)
            if acr not in terms:
                terms[acr] = f"acronym ({source_label})"

        # Title-Case multi-word technical phrases (2–4 capitalized words)
        phrase_pat = re.compile(
            r'\b((?:[A-Z][a-z]{2,}(?:-[A-Z][a-z]+)?)'
            r'(?:\s+(?:[A-Z][a-z]{2,}(?:-[A-Z][a-z]+)?))+)\b'
        )
        for m in phrase_pat.finditer(text):
            phrase = m.group(1)
            words = phrase.split()
            if len(words) > 4:
                continue
            if all(w in _STOP_CAPS for w in words):
                continue
            if words[0] in _STOP_CAPS and len(words) <= 2:
                continue
            if phrase not in terms:
                terms[phrase] = f"technical phrase ({source_label})"

        ledger = self._load_terminology_ledger()
        ledger["terms"].update(terms)
        ledger["updated_at"] = _now()

        ledger_path = self._work_dir / "TERMINOLOGY_LEDGER.json"
        ledger_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")

    def _merge_drafts(self) -> None:
        """Merge durable section drafts without hiding review-state metadata.

        A section awaiting human review may still contain a scientifically useful
        durable candidate.  Its manuscript body is merged exactly like a
        completed section; the distinction is carried in
        ``_merged_section_metadata`` and the final package, never as workflow
        prose inserted into the manuscript.
        """
        parts = []
        merged_metadata: List[Dict[str, Any]] = []
        excluded_metadata: List[Dict[str, Any]] = []
        mergeable_statuses = {"completed", "awaiting_human_review"}

        for section in self._section_registry.get("sections", []):
            section_id = str(section.get("section_id") or "")
            status = str(section.get("status") or "")
            work_dir = Path(str(section.get("work_dir") or ""))
            draft_path = work_dir / "SECTION_DRAFT_EN.md"

            if status not in mergeable_statuses:
                excluded_metadata.append({
                    "section_id": section_id,
                    "status": status,
                    "reason": "section_status_not_mergeable",
                    "draft_path": str(draft_path),
                })
                continue
            if not draft_path.exists():
                excluded_metadata.append({
                    "section_id": section_id,
                    "status": status,
                    "reason": "draft_missing",
                    "draft_path": str(draft_path),
                })
                continue

            raw_text = draft_path.read_text(encoding="utf-8").strip()
            if not raw_text:
                excluded_metadata.append({
                    "section_id": section_id,
                    "status": status,
                    "reason": "draft_empty",
                    "draft_path": str(draft_path),
                })
                continue
            if status == "awaiting_human_review" and not _has_durable_section_candidate(work_dir):
                excluded_metadata.append({
                    "section_id": section_id,
                    "status": status,
                    "reason": "awaiting_section_has_no_durable_candidate",
                    "draft_path": str(draft_path),
                })
                continue

            title = section.get("title", section_id)
            text = _strip_repeated_leading_title(raw_text, title)
            text = _demote_embedded_section_headings(text)
            parts.append(f"## {title}\n\n{text}")
            merged_metadata.append({
                "section_id": section_id,
                "title": title,
                "status": status,
                "authoring_status": section.get("authoring_status", status),
                "candidate": status == "awaiting_human_review",
                "draft_path": str(draft_path),
                "word_count": len(re.findall(r"\b[\w'-]+\b", raw_text)),
            })

        self._merged_section_metadata = merged_metadata
        self._excluded_section_metadata = excluded_metadata
        merged = '\n\n---\n\n'.join(parts)
        if not merged.strip():
            excluded_ids = [m.get("section_id", "?") for m in excluded_metadata]
            raise RuntimeError(
                f"All {len(excluded_metadata)} sections were excluded from the draft "
                f"({excluded_ids}); FULL_REVIEW_DRAFT_EN.md would be empty. "
                "Check section authoring logs for repeated repair_required failures."
            )
        merged_path = self._work_dir / "FULL_REVIEW_DRAFT_EN.md"
        merged_path.write_text(merged, encoding="utf-8")
        self._register_artifact("merged_draft", merged_path)

    def _collect_section_authoring_flags(self) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Collect unresolved/editorial flags from durable section packages.

        The global auditor operates on the merged manuscript and may not see a
        section-level audit when that section is awaiting human review.  This
        collector keeps the section audit authoritative and adds bounded
        summary flags if an old package contains counts without per-flag detail.
        """
        flags: List[Dict[str, Any]] = []
        summaries: List[Dict[str, Any]] = []
        for section in self._section_registry.get("sections", []):
            section_id = str(section.get("section_id") or "")
            work_dir = Path(str(section.get("work_dir") or ""))
            package = _read_json(work_dir / "SECTION_AUTHORING_PACKAGE.json")
            audit = _read_json(work_dir / "SECTION_AUTHORING_AUDIT.json")
            if not package and not audit:
                continue

            package_total = int(package.get("total_flags", 0) or 0)
            package_blocking = int(
                package.get("blocking_flags", package.get("total_blocking_flags", 0)) or 0
            )
            detail_rows: List[Dict[str, Any]] = []
            for key in ("overclaim_flags", "citation_flags", "scope_flags", "flags"):
                value = audit.get(key, [])
                if isinstance(value, list):
                    detail_rows.extend(item for item in value if isinstance(item, dict))

            section_flags: List[Dict[str, Any]] = []
            for index, raw in enumerate(detail_rows, start=1):
                severity = str(raw.get("severity") or "important").lower()
                flag_type = str(raw.get("flag_type") or raw.get("type") or "section_audit")
                normalized = dict(raw)
                normalized.update({
                    "flag_id": f"{section_id}-AF{index:02d}",
                    "type": flag_type,
                    "section_ids": [section_id],
                    "blocking": severity == "blocking",
                    "severity": severity,
                    "description": str(
                        raw.get("reason") or raw.get("description") or "Section-level authoring audit flag"
                    ),
                    "source": "section_authoring_audit",
                    "source_section_package": str(work_dir / "SECTION_AUTHORING_PACKAGE.json"),
                    "resolved": bool(raw.get("resolved", False)),
                })
                section_flags.append(normalized)

            # Preserve package-level counts even for legacy packages that only
            # stored aggregate numbers.  Never silently turn missing details
            # into a clean final package.
            detail_blocking = sum(1 for item in section_flags if item.get("blocking"))
            missing_total = max(0, package_total - len(section_flags))
            missing_blocking = max(0, package_blocking - detail_blocking)
            for offset in range(missing_total):
                is_blocking = offset < missing_blocking
                section_flags.append({
                    "flag_id": f"{section_id}-AF{len(section_flags)+1:02d}",
                    "type": "section_audit_summary",
                    "section_ids": [section_id],
                    "blocking": is_blocking,
                    "severity": "blocking" if is_blocking else "important",
                    "description": (
                        "Section package reports an unresolved blocking issue without per-flag detail."
                        if is_blocking
                        else "Section package reports an unresolved editorial issue without per-flag detail."
                    ),
                    "source": "section_authoring_package_summary",
                    "source_section_package": str(work_dir / "SECTION_AUTHORING_PACKAGE.json"),
                    "detail_available": False,
                    "resolved": False,
                })

            if section_flags:
                flags.extend(section_flags)
            if package_total or package_blocking:
                summaries.append({
                    "section_id": section_id,
                    "status": section.get("status", ""),
                    "package_total_flags": package_total,
                    "package_blocking_flags": package_blocking,
                    "aggregated_flags": len(section_flags),
                    "aggregated_blocking_flags": sum(
                        1 for item in section_flags if item.get("blocking")
                    ),
                    "source_package": str(work_dir / "SECTION_AUTHORING_PACKAGE.json"),
                })
                section["blocking_flags"] = package_blocking
                section["total_flags"] = package_total

        return flags, summaries

    def _reconcile_final_audit(self, audit_report: Dict[str, Any]) -> Dict[str, Any]:
        """Merge section-level audit state into the final deterministic ledger."""
        reconciled = dict(audit_report or {})
        existing = list(reconciled.get("flags", []) or [])
        section_flags, summaries = self._collect_section_authoring_flags()
        existing_signatures = {
            (
                item.get("type", ""),
                tuple(sorted(item.get("section_ids", []) or [])),
                str(item.get("description", ""))[:160],
            )
            for item in existing
            if isinstance(item, dict)
        }
        existing_flag_ids = {
            str(item.get("flag_id"))
            for item in existing
            if isinstance(item, dict) and item.get("flag_id")
        }
        for item in section_flags:
            # Section audit rows are individually meaningful even when legacy
            # summary rows share the same description.  Their stable flag_id
            # is the deduplication key across a replay, not their prose.
            if str(item.get("source", "")).startswith("section_"):
                flag_id = str(item.get("flag_id") or "")
                if flag_id and flag_id in existing_flag_ids:
                    continue
                existing.append(item)
                if flag_id:
                    existing_flag_ids.add(flag_id)
                continue
            signature = (
                item.get("type", ""),
                tuple(sorted(item.get("section_ids", []) or [])),
                str(item.get("description", ""))[:160],
            )
            if signature not in existing_signatures:
                existing.append(item)
                existing_signatures.add(signature)

        reconciled["flags"] = existing
        reconciled["section_flags"] = section_flags
        reconciled["section_flag_summary"] = summaries
        reconciled["section_flags_total"] = len(section_flags)
        reconciled["section_blocking_flags"] = sum(
            1 for item in section_flags if item.get("blocking")
        )
        reconciled["section_audits_reconciled"] = True
        reconciled["total_flags"] = len(existing)
        reconciled["blocking_flags"] = sum(
            1 for item in existing if item.get("blocking")
        )
        return reconciled

    def _persist_audit_report(
        self,
        audit_report: Dict[str, Any],
        round_num: int,
    ) -> None:
        """Persist the merged deterministic + semantic audit, not a stale L1 copy."""

        audit_dir = self._work_dir / f"audit_round_{round_num}"
        audit_dir.mkdir(parents=True, exist_ok=True)
        report_path = audit_dir / "GLOBAL_AUDIT_REPORT.json"
        report_path.write_text(
            json.dumps(audit_report, indent=2),
            encoding="utf-8",
        )
        self._register_artifact(f"audit_round_{round_num}", report_path)
        layer2_path = audit_dir / "LAYER2_AUDIT_FLAGS.json"
        if layer2_path.exists():
            self._register_artifact(
                f"layer2_audit_round_{round_num}",
                layer2_path,
            )

    @staticmethod
    def _merge_semantic_flags(
        audit_report: Dict[str, Any],
        semantic_flags: List[Dict[str, Any]],
    ) -> None:
        existing = {
            (
                flag.get("type", ""),
                tuple(sorted(flag.get("section_ids", []))),
                flag.get("description", "")[:60],
            )
            for flag in audit_report.get("flags", [])
        }
        next_index = 1
        for flag in semantic_flags:
            signature = (
                flag.get("type", ""),
                tuple(sorted(flag.get("section_ids", []))),
                flag.get("description", "")[:60],
            )
            if signature in existing:
                continue
            flag = dict(flag)
            flag["flag_id"] = f"L2-F{next_index:02d}"
            next_index += 1
            audit_report.setdefault("flags", []).append(flag)
            existing.add(signature)
        audit_report["total_flags"] = len(audit_report.get("flags", []))
        audit_report["blocking_flags"] = sum(
            1 for flag in audit_report.get("flags", []) if flag.get("blocking")
        )

    def _run_final_semantic_audit(
        self,
        audit_report: Dict[str, Any],
        round_num: int,
    ) -> Dict[str, Any]:
        """Run the semantic editor after the last revision before final status."""

        if not self.config.use_llm_audit:
            audit_report["layer2_status"] = "disabled"
            self._persist_audit_report(audit_report, round_num)
            return audit_report
        if (
            self._calculate_total_cost_cny()
            + self.config.audit_cost_budget_cny
            > self.config.run_cost_budget_cny
        ):
            audit_report["layer2_status"] = "skipped_cost_budget"
            self._state["audit_budget_exhausted"] = True
            self._persist_audit_report(audit_report, round_num)
            return audit_report

        from .global_review_auditor import LLMAuditLayer

        merged_path = self._work_dir / "FULL_REVIEW_DRAFT_EN.md"
        layer2 = LLMAuditLayer(
            model_tier=self.config.audit_model_tier,
            model_override=self.config.audit_model_override,
            cost_budget_cny=self.config.audit_cost_budget_cny,
            m1_library_path=self.config.m1_library_path,
        )
        semantic_flags = layer2.audit(
            merged_draft=(
                merged_path.read_text(encoding="utf-8")
                if merged_path.exists()
                else ""
            ),
            section_registry=self._section_registry,
            blueprint=self._load_blueprint(),
            work_dir=self._work_dir,
            round_num=round_num,
        )
        failed = any(
            flag.get("type") == "layer2_audit_failed"
            for flag in semantic_flags
        )
        if failed:
            self._state["layer2_audit_failed"] = True
        else:
            # Failure/budget markers are transient attempt state, not a
            # permanent property of the manuscript.  A successful final
            # semantic audit supersedes earlier failed rounds.
            self._state.pop("layer2_audit_failed", None)
            self._state.pop("audit_budget_exhausted", None)
        self._merge_semantic_flags(audit_report, semantic_flags)
        audit_report["layer2_status"] = "failed" if failed else "completed"
        self._persist_audit_report(audit_report, round_num)
        return audit_report

    def _run_audit_revision_loop(self) -> Dict[str, Any]:
        """Run up to max_revision_rounds of audit-plan-revise cycles."""
        # A resumed authoring run must reassess the current article rather than
        # inherit a plateau/failure verdict from an older, incomplete draft.
        for transient_key in (
            "early_stop_reason",
            "layer2_audit_failed",
            "audit_budget_exhausted",
        ):
            self._state.pop(transient_key, None)
        last_flags = None
        small_improvement_streak = 0
        layer2_failed_once = False

        for round_num in range(1, self.config.max_revision_rounds + 1):
            self._state["current_round"] = round_num
            self._update_state(f"audit_round_{round_num}")

            # Layer 1: deterministic audit
            merged_path = self._work_dir / "FULL_REVIEW_DRAFT_EN.md"
            audit_report = self._auditor.audit(merged_path, self._section_registry, self._work_dir, round_num)

            # Layer 2: optional LLM-based audit
            layer2_status = "disabled"
            # Run the expensive editor on round 1 and whenever deterministic
            # checks say the draft is clean enough for a final semantic verdict.
            layer2_affordable = (
                self._calculate_total_cost_cny()
                + self.config.audit_cost_budget_cny
                <= self.config.run_cost_budget_cny
            )
            deterministic_actionable = self._actionable_flag_count(
                audit_report
            )
            run_layer2 = (
                self.config.use_llm_audit
                and layer2_affordable
                and not layer2_failed_once
                and (round_num == 1 or deterministic_actionable == 0)
            )
            if run_layer2:
                try:
                    from .global_review_auditor import LLMAuditLayer
                    blueprint = self._load_blueprint()
                    layer2 = LLMAuditLayer(
                        model_tier=self.config.audit_model_tier,
                        model_override=self.config.audit_model_override,
                        cost_budget_cny=self.config.audit_cost_budget_cny,
                        m1_library_path=self.config.m1_library_path,
                    )
                    merged_text = merged_path.read_text(encoding="utf-8") if merged_path.exists() else ""
                    l2_flags = layer2.audit(
                        merged_draft=merged_text,
                        section_registry=self._section_registry,
                        blueprint=blueprint,
                        work_dir=self._work_dir,
                        round_num=round_num,
                    )
                    layer2_failed = any(
                        flag.get("type") == "layer2_audit_failed"
                        for flag in l2_flags
                    )
                    if layer2_failed:
                        self._state["layer2_audit_failed"] = True
                        layer2_failed_once = True

                    # Merge by issue identity rather than raw flag count.
                    existing_sigs = {
                        (f["type"], tuple(sorted(f.get("section_ids", []))), f.get("description", "")[:60])
                        for f in audit_report["flags"]
                    }
                    added = 0
                    for f in l2_flags:
                        sig = (f["type"], tuple(sorted(f.get("section_ids", []))), f.get("description", "")[:60])
                        if sig not in existing_sigs:
                            f["flag_id"] = f"L2-F{added+1:02d}"
                            audit_report["flags"].append(f)
                            existing_sigs.add(sig)
                            if f.get("blocking"):
                                audit_report["blocking_flags"] = audit_report.get("blocking_flags", 0) + 1
                            added += 1
                    audit_report["total_flags"] = len(audit_report["flags"])
                    layer2_status = "failed" if layer2_failed else "completed"
                except Exception as exc:
                    logger.warning("Layer 2 audit failed: %s", exc)
                    layer2_status = "failed"
                    # Layer 2 failure → downgrade to awaiting_human_review at the end
                    self._state["layer2_audit_failed"] = True
                    layer2_failed_once = True

            elif self.config.use_llm_audit:
                if not layer2_affordable:
                    layer2_status = "skipped_cost_budget"
                    self._state["audit_budget_exhausted"] = True
                else:
                    layer2_status = "deferred_until_clean_or_final"

            audit_report["layer2_status"] = layer2_status
            self._persist_audit_report(audit_report, round_num)

            total_flags = self._actionable_flag_count(audit_report)

            # Check for completion
            if total_flags == 0:
                self._state["revision_rounds_completed"] = round_num
                return audit_report

            # Check for improvement plateau
            if last_flags is not None:
                improvement = (last_flags - total_flags) / last_flags if last_flags > 0 else 0
                if improvement < self.config.improvement_threshold:
                    small_improvement_streak += 1
                else:
                    small_improvement_streak = 0

                if small_improvement_streak >= 2:
                    self._state["revision_rounds_completed"] = round_num
                    self._state["early_stop_reason"] = "consecutive_small_improvement"
                    return audit_report

            last_flags = total_flags

            # Plan revisions
            self._update_state(f"revision_round_{round_num}")
            revision_plan = self._planner.plan(audit_report, self._section_registry, self._work_dir)
            revision_plan_path = (
                self._work_dir
                / f"revision_round_{round_num}"
                / "REVISION_PLAN.json"
            )
            self._register_artifact(
                f"revision_plan_round_{round_num}",
                revision_plan_path,
            )

            # Apply revisions — pass explicit rerun callable (no callback attribute hack)
            self._revision_worker.apply(
                revision_plan,
                self._section_registry,
                self._work_dir,
                rerun_fn=self._rerun_section_for_revision,
                reaudit_fn=self._reaudit_section,
            )
            for artifact_key, artifact_path in (
                ("revision_history", self._work_dir / "REVISION_HISTORY.json"),
                (
                    "conceptual_figure_requests",
                    self._work_dir / "CONCEPTUAL_FIGURE_REQUESTS.json",
                ),
            ):
                self._register_artifact(artifact_key, artifact_path)

            # Re-merge after revisions
            self._merge_drafts()

        # Max rounds reached
        self._state["revision_rounds_completed"] = self.config.max_revision_rounds

        # Run final audit
        final_round = self.config.max_revision_rounds + 1
        final_audit = self._auditor.audit(
            self._work_dir / "FULL_REVIEW_DRAFT_EN.md",
            self._section_registry,
            self._work_dir,
            final_round,
        )
        return self._run_final_semantic_audit(final_audit, final_round)

    def _determine_final_status(self, final_audit: Dict[str, Any]) -> str:
        """Determine orchestration final status based on sections and flags."""
        sections = self._section_registry["sections"]
        completed = sum(1 for s in sections if s["status"] == "completed")
        total = len(sections)
        incomplete = [
            section
            for section in sections
            if section.get("status") != "completed"
        ]

        blocking_flags = int(final_audit.get("blocking_flags", 0) or 0)
        semantic_failure = bool(
            self._state.get("layer2_audit_failed")
            or final_audit.get("layer2_status") == "failed"
        )
        has_human_review = any(
            section.get("status") == "awaiting_human_review"
            for section in sections
        )

        # These are safe non-success outcomes.  They must be checked before
        # partial/failed classification so a durable draft or a failed global
        # semantic audit can never be relabeled as an ordinary failure.
        if blocking_flags > 0 or semantic_failure or has_human_review:
            return "awaiting_human_review"

        # Check for blocked (all needs_more_literature)
        blocked = all(s.get("status") == "needs_more_literature" for s in sections)
        if blocked:
            return "blocked"

        # Never label an article complete when any planned section failed,
        # exhausted its budget, or still needs literature.
        if incomplete and completed > 0:
            return "partial"

        # Check for complete failure
        if completed == 0:
            return "failed"

        actionable_flags = self._actionable_flag_count(final_audit)

        # A plateau matters only while authoring/reasoning issues remain.
        # Visual gaps are deliberately handled by the downstream Visual Editor.
        if self._state.get("early_stop_reason") and actionable_flags > 0:
            return "awaiting_human_review"

        if self._state.get("audit_budget_exhausted"):
            return "awaiting_human_review"

        return "completed"

    @staticmethod
    def _actionable_flag_count(audit_report: Dict[str, Any]) -> int:
        """Count issues owned by the authoring/revision loop.

        Missing or desirable figures are useful editorial signals, but they
        must not trigger repeated prose rewrites. The article-level Visual
        Editor resolves them after text authoring and records any intentionally
        unfilled quantitative needs.
        """
        return sum(
            1
            for flag in audit_report.get("flags", [])
            if isinstance(flag, dict)
            and flag.get("type") != "visual_gap"
        )

    def _calculate_total_cost(self) -> float:
        """Sum USD display costs across section workers and global audits."""

        total = sum(
            float(section.get("cost_usd", 0.0))
            for section in self._section_registry.get("sections", [])
        )
        if self._work_dir:
            for audit_dir in self._work_dir.glob("audit_round_*"):
                for path in audit_dir.rglob("COST.json"):
                    total += float(
                        _read_json(path).get("estimated_cost_usd", 0.0)
                    )
        return round(total, 6)

    def _calculate_total_cost_cny(self) -> float:
        """Sum authoritative CNY costs across section workers and audits."""

        total = sum(
            float(section.get("cost_cny", 0.0))
            for section in self._section_registry.get("sections", [])
        )
        if self._work_dir:
            for audit_dir in self._work_dir.glob("audit_round_*"):
                for path in audit_dir.rglob("COST.json"):
                    total += float(
                        _read_json(path).get("estimated_cost_cny", 0.0)
                    )
        return round(total, 6)

    def _calculate_total_tokens(self) -> tuple[int, int]:
        """Sum cumulative section and global-audit token usage."""

        input_tokens = sum(
            int(section.get("input_tokens", 0) or 0)
            for section in self._section_registry.get("sections", [])
        )
        output_tokens = sum(
            int(section.get("output_tokens", 0) or 0)
            for section in self._section_registry.get("sections", [])
        )
        if self._work_dir:
            for audit_dir in self._work_dir.glob("audit_round_*"):
                for path in audit_dir.rglob("COST.json"):
                    value = _read_json(path)
                    input_tokens += int(
                        value.get("total_input_tokens", 0) or 0
                    )
                    output_tokens += int(
                        value.get("total_output_tokens", 0) or 0
                    )
        return input_tokens, output_tokens

    def _write_final_package(self, status: str, final_audit: Dict[str, Any]) -> None:
        """Write all remaining artifacts and FULL_REVIEW_PACKAGE.json."""
        # Write FINAL_REVIEW_EN.md (clean copy of last merged draft)
        merged = self._work_dir / "FULL_REVIEW_DRAFT_EN.md"
        if merged.exists() and status in ("completed", "awaiting_human_review", "partial"):
            final_path = self._work_dir / "FINAL_REVIEW_EN.md"
            shutil.copy2(merged, final_path)
            self._register_artifact("final_review", final_path)

        # Write SECTION_TRANSITION_CONTRACTS.json
        self._write_section_transition_contracts()

        # Write FULL_REVIEW_CITATION_MAP.json
        self._write_global_citation_map()

        # Write RESULT.json
        result_data = {
            "schema_version": "phase4.result.v1",
            "run_id": self._run_id,
            "status": status,
            "sections_completed": sum(1 for s in self._section_registry["sections"] if s["status"] == "completed"),
            "sections_awaiting_human_review": sum(
                1 for s in self._section_registry["sections"]
                if s.get("status") == "awaiting_human_review"
            ),
            "sections_failed": sum(1 for s in self._section_registry["sections"] if s["status"] == "failed"),
            "total_flags": final_audit.get("total_flags", 0),
            "blocking_flags": final_audit.get("blocking_flags", 0),
            "section_flags_total": final_audit.get("section_flags_total", 0),
            "section_blocking_flags": final_audit.get("section_blocking_flags", 0),
            "revision_rounds": self._state.get("revision_rounds_completed", 0),
            "total_cost_cny": self._calculate_total_cost_cny(),
            "total_cost_usd": self._calculate_total_cost(),
            "total_input_tokens": self._calculate_total_tokens()[0],
            "total_output_tokens": self._calculate_total_tokens()[1],
            "created_at": _now(),
        }
        result_path = self._work_dir / "RESULT.json"
        result_path.write_text(json.dumps(result_data, indent=2), encoding="utf-8")
        self._register_artifact("result", result_path)

        # Register terminology ledger if exists
        ledger_path = self._work_dir / "TERMINOLOGY_LEDGER.json"
        if ledger_path.exists():
            self._register_artifact("terminology_ledger", ledger_path)

        # Write FULL_REVIEW_PACKAGE.json — use real paths from _written_artifacts
        package = {
            "schema_version": "phase4.full_review_package.v1",
            "run_id": self._run_id,
            "status": status,
            "section_count": len(self._section_registry["sections"]),
            "sections_completed": result_data["sections_completed"],
            "sections_awaiting_human_review": result_data["sections_awaiting_human_review"],
            "sections_failed": result_data["sections_failed"],
            "merged_section_count": len(self._merged_section_metadata),
            "candidate_section_count": sum(
                1 for item in self._merged_section_metadata if item.get("candidate")
            ),
            "merged_sections": self._merged_section_metadata,
            "excluded_sections": self._excluded_section_metadata,
            "total_flags": result_data["total_flags"],
            "blocking_flags": result_data["blocking_flags"],
            "section_flags_total": result_data["section_flags_total"],
            "section_blocking_flags": result_data["section_blocking_flags"],
            "section_flag_summary": final_audit.get("section_flag_summary", []),
            "review_gate": {
                "status": status,
                "candidate_sections_require_human_review": [
                    item["section_id"]
                    for item in self._merged_section_metadata
                    if item.get("candidate")
                ],
                "global_semantic_audit_status": final_audit.get("layer2_status", "unknown"),
            },
            "revision_rounds": result_data["revision_rounds"],
            "total_cost_cny": result_data["total_cost_cny"],
            "total_cost_usd": result_data["total_cost_usd"],
            "artifacts": {k: str(v) for k, v in self._written_artifacts.items()},
            "reconciliation": self._reconciliation_metadata,
            "created_at": _now(),
        }
        pkg_path = self._work_dir / "FULL_REVIEW_PACKAGE.json"
        pkg_path.write_text(json.dumps(package, indent=2), encoding="utf-8")
        self._register_artifact("full_review_package", pkg_path)

    def _fail_result(self, reason: str, start_time: datetime) -> OrchestratorResult:
        """Build a failed result."""
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        self._update_state("failed", stop_reason=reason)
        return self._build_result_from_state("failed", elapsed=elapsed)

    def _build_result_from_state(
        self, status: Optional[str] = None, elapsed: float = 0.0
    ) -> OrchestratorResult:
        """Build OrchestratorResult from current state and registry."""
        s = status or self._state.get("state", "failed")
        sections = self._section_registry.get("sections", [])
        # Try to read total_flags from the last audit round written
        total_flags = 0
        blocking_flags = 0
        for rnd in range(self.config.max_revision_rounds + 2, 0, -1):
            audit_path = self._work_dir / f"audit_round_{rnd}" / "GLOBAL_AUDIT_REPORT.json"
            if audit_path.exists():
                audit = _read_json(audit_path)
                total_flags = audit.get("total_flags", 0)
                blocking_flags = audit.get("blocking_flags", 0)
                break
        return OrchestratorResult(
            run_id=self._run_id or "unknown",
            status=s,
            sections_completed=sum(1 for sec in sections if sec.get("status") == "completed"),
            sections_failed=sum(1 for sec in sections if sec.get("status") == "failed"),
            total_flags=total_flags,
            blocking_flags=blocking_flags,
            revision_rounds=self._state.get("revision_rounds_completed", 0),
            total_cost_cny=self._calculate_total_cost_cny(),
            total_input_tokens=self._calculate_total_tokens()[0],
            total_output_tokens=self._calculate_total_tokens()[1],
            total_cost_usd=self._calculate_total_cost(),
            wall_time_seconds=elapsed,
            work_dir=self._work_dir or Path("."),
        )

    def _register_artifact(self, key: str, path: Path) -> None:
        """Record a written artifact path only if the file exists."""
        if path.exists():
            self._written_artifacts[key] = path

    def _log_event(self, event: Dict[str, Any]) -> None:
        """Append a structured event line to EVENTS.jsonl."""
        if not self._work_dir:
            return
        events_path = self._work_dir / "EVENTS.jsonl"
        line = json.dumps({"ts": _now(), "run_id": self._run_id, **event})
        with events_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._register_artifact("events", events_path)

    def _rerun_section_for_revision(
        self, section_id: str, revision: Dict[str, Any]
    ) -> bool:
        """Re-run a section through ResearchWorker with revision instructions injected."""
        section_meta = next(
            (s for s in self._section_registry["sections"] if s["section_id"] == section_id),
            None,
        )
        if not section_meta:
            return False

        # Find section definition in blueprint
        blueprint = self._load_blueprint()
        section = next(
            (s for s in blueprint.get("sections", []) if s.get("section_id") == section_id),
            None,
        )
        if not section:
            return False

        all_sections = self._extract_sections(blueprint, None)
        idx = next((i for i, s in enumerate(all_sections) if s.get("section_id") == section_id), 0)
        preceding = all_sections[idx - 1] if idx > 0 else None
        following = all_sections[idx + 1] if idx < len(all_sections) - 1 else None

        # Augment mentor_advice with revision context
        raw_base_advice = (
            section.get("review_mentor_advice")
            or section.get("mentor_advice")
            or section.get("mentor_guidance")
            or {}
        )
        base_advice = (
            dict(raw_base_advice)
            if isinstance(raw_base_advice, dict)
            else {"guidance": str(raw_base_advice)}
        )
        base_advice["revision_instructions"] = {
            "flag_id": revision.get("flag_id", ""),
            "root_cause": revision.get("root_cause", ""),
            "action": revision.get("action", ""),
            "description": revision.get("description", ""),
            "editor_root_cause": revision.get("editor_root_cause", ""),
            "recommended_action": revision.get("recommended_action", ""),
            "scope": "fix_identified_issue_only",
        }
        # Temporarily patch section's review_mentor_advice for context building
        patched_section = dict(section)
        patched_section["review_mentor_advice"] = base_advice

        section_work_dir = Path(section_meta.get("work_dir", ""))
        if not section_work_dir.exists():
            return False
        editor_snapshot = _snapshot_section_editor_transaction(
            section_work_dir
        )
        existing_draft_text = (
            (section_work_dir / "SECTION_DRAFT_EN.md").read_text(
                encoding="utf-8",
                errors="replace",
            )
            if (section_work_dir / "SECTION_DRAFT_EN.md").exists()
            else ""
        )
        destructive_rebuild = (
            revision.get("action") == "rerun_section_with_source_synthesis"
        )
        if destructive_rebuild:
            archive = _archive_section_authoring_for_rebuild(
                section_work_dir,
                reason="editor_source_synthesis",
            )
            section_meta["editorial_rebuild_archive"] = (
                str(archive) if archive else ""
            )

        ctx = self._build_section_context(patched_section, preceding, following, blueprint, section_work_dir)
        ctx.revision_instructions = dict(
            base_advice.get("revision_instructions") or {}
        )
        ctx.existing_draft_text = existing_draft_text

        provider = SectionAuthoringToolProvider(ctx)
        _mo2 = self.config.model_override
        _model2 = _mo2(ctx) if callable(_mo2) else _mo2
        flag_slug = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            str(revision.get("flag_id") or "rev"),
        )[:64]
        task_id = (
            f"revision_{section_id}_{flag_slug}_{uuid.uuid4().hex[:8]}"
        )
        revision_runtime_dir = (
            section_work_dir / ".runtime" / task_id
        )
        worker = ResearchWorker(
            tool_provider=provider,
            runs_root=self.config.output_root,
            _system_prompt_override=self._load_system_prompt(),
            _model_override=_model2,
            # Provider tools remain bound to section_work_dir through ctx, while
            # each revision gets isolated state/log/cost files.  Reusing the
            # original authoring runtime would otherwise return its cached
            # RESULT.json without performing the revision.
            _work_dir_override=revision_runtime_dir,
        )
        remaining_budget = max(
            0.0,
            self.config.run_cost_budget_cny
            - self._calculate_total_cost_cny(),
        )
        if remaining_budget <= 0.1:
            if destructive_rebuild:
                _restore_section_editor_transaction(
                    section_work_dir,
                    editor_snapshot,
                )
                section_meta["editorial_rerun_rolled_back"] = True
                section_meta["editorial_rerun_rollback_reason"] = (
                    "insufficient_budget_before_worker_start"
                )
            return False
        revision_budget = min(
            self.config.section_revision_cost_budget_cny,
            remaining_budget,
        )
        contract = TaskContract(
            run_id=self._run_id,
            task_id=task_id,
            goal=f"Revise section {section_id}: {revision.get('description', '')}",
            allowed_tools=SECTION_AUTHORING_TOOL_NAMES,
            skill_ids=["section-review-authoring"],
            model_tier=self.config.section_model_tier,
            max_iters=min(self.config.section_max_iters, 18),
            token_budget=self.config.section_revision_token_budget,
            cost_budget_cny=revision_budget,
            next_call_cost_reserve_cny=min(
                0.25, revision_budget * 0.2
            ),
            wall_time_budget_seconds=self.config.section_wall_time_seconds,
            # The provider writes canonical section artifacts outside the
            # isolated runtime directory and its validator is authoritative.
            expected_outputs=[],
        )
        try:
            result = worker.run(contract)
            section_meta["cost_cny"] = (
                float(section_meta.get("cost_cny", 0.0))
                + float(getattr(result, "estimated_cost_cny", 0.0) or 0.0)
            )
            section_meta["cost_usd"] = (
                float(section_meta.get("cost_usd", 0.0))
                + float(getattr(result, "estimated_cost_usd", 0.0) or 0.0)
            )
            section_meta["input_tokens"] = (
                int(section_meta.get("input_tokens", 0) or 0)
                + int(getattr(result, "total_input_tokens", 0) or 0)
            )
            section_meta["output_tokens"] = (
                int(section_meta.get("output_tokens", 0) or 0)
                + int(getattr(result, "total_output_tokens", 0) or 0)
            )
            self._write_cost_json()
            success = result.status == TaskStatus.completed
            if success:
                try:
                    audit_result = json.loads(
                        _make_run_citation_audit(ctx)("[]")
                    )
                    validation = _make_validate_authoring_package(ctx)()
                    success = (
                        bool(audit_result.get("audit_passed"))
                        and "VALIDATION_PASSED" in validation
                    )
                except Exception as exc:
                    logger.warning(
                        "post-editor validation failed for %s: %s",
                        section_id,
                        exc,
                    )
                    success = False
            if not success:
                _restore_section_editor_transaction(
                    section_work_dir,
                    editor_snapshot,
                )
                section_meta["editorial_rerun_rolled_back"] = True
            return success
        except Exception as exc:
            _restore_section_editor_transaction(
                section_work_dir,
                editor_snapshot,
            )
            section_meta["editorial_rerun_rolled_back"] = True
            logger.warning(f"rerun section {section_id} failed: {exc}")
            return False

    def _reaudit_section(self, section_id: str):
        """Deterministic citation re-audit after inline revision.

        Validates [REF:*] markers, updates package stats, removes stale marker.
        Returns ReauditResult(passed=True) on success.
        """
        from .targeted_revision_worker import ReauditResult

        section_meta = next(
            (s for s in self._section_registry["sections"] if s["section_id"] == section_id),
            None,
        )
        if not section_meta:
            return ReauditResult(passed=False, reason=f"{section_id} not in registry")

        work_dir = Path(section_meta["work_dir"])
        draft_path = work_dir / "SECTION_DRAFT_EN.md"
        if not draft_path.exists():
            return ReauditResult(passed=False, reason="draft missing")

        draft_text = draft_path.read_text(encoding="utf-8")
        refs_in_draft = set(re.findall(r'\[REF:([^\]]+)\]', draft_text))

        # Build allowed paper_ids from source ledger (bundle or authoring context)
        allowed_paper_ids: set = set()
        bundle = self._discover_phase2_material(section_id)
        if bundle and bundle.source_ledger_path and bundle.source_ledger_path.exists():
            try:
                ledger = json.loads(bundle.source_ledger_path.read_text(encoding="utf-8"))
                for src in ledger.get("sources", []):
                    pid = src.get("paper_id", "")
                    if pid:
                        allowed_paper_ids.add(pid)
            except Exception:
                pass

        ctx_path = work_dir / "SECTION_AUTHORING_CONTEXT.json"
        if ctx_path.exists():
            try:
                ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
                lp = _resolve_path(ctx.get("source_ledger_path"))
                if lp:
                    ledger2 = json.loads(lp.read_text(encoding="utf-8"))
                    for src in ledger2.get("sources", []):
                        pid = src.get("paper_id", "")
                        if pid:
                            allowed_paper_ids.add(pid)
            except Exception:
                pass

        # Validate: no unauthorized paper IDs in draft
        if allowed_paper_ids:
            unauthorized = refs_in_draft - allowed_paper_ids
            if unauthorized:
                return ReauditResult(
                    passed=False,
                    reason=f"unauthorized paper_id(s) in draft: {sorted(unauthorized)}"
                )

        # Prune dead entries from SECTION_CITATION_MAP.json (papers no longer cited)
        cit_path = work_dir / "SECTION_CITATION_MAP.json"
        if cit_path.exists():
            try:
                cit_data = json.loads(cit_path.read_text(encoding="utf-8"))
                live = [c for c in cit_data.get("citations", [])
                        if any(p in refs_in_draft for p in c.get("paper_ids", []))]
                if len(live) != len(cit_data.get("citations", [])):
                    cit_data["citations"] = live
                    cit_path.write_text(json.dumps(cit_data, indent=2), encoding="utf-8")
            except Exception:
                pass

        # Update SECTION_AUTHORING_PACKAGE.json stats
        pkg_path = work_dir / "SECTION_AUTHORING_PACKAGE.json"
        if pkg_path.exists():
            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
                pkg["word_count"] = len(draft_text.split())
                paragraphs = [p for p in draft_text.split("\n\n") if p.strip()]
                pkg["paragraph_count"] = len(paragraphs)
                pkg["papers_cited"] = len(refs_in_draft)
                pkg["citation_reaudit_timestamp"] = _now()
                pkg_path.write_text(json.dumps(pkg, indent=2), encoding="utf-8")
            except Exception:
                pass

        # Write SECTION_AUTHORING_AUDIT.json
        audit_path = work_dir / "SECTION_AUTHORING_AUDIT.json"
        audit_doc = {
            "schema_version": "phase4.section_audit.v1",
            "section_id": section_id,
            "blocking_flags": 0,
            "flags": [],
            "citation_reaudit": {
                "status": "passed",
                "refs_in_draft": sorted(refs_in_draft),
                "allowed_paper_ids": sorted(allowed_paper_ids),
                "timestamp": _now(),
            },
        }
        audit_path.write_text(json.dumps(audit_doc, indent=2), encoding="utf-8")

        # Remove stale marker — all checks passed
        stale_marker = work_dir / ".citation_audit_stale"
        if stale_marker.exists():
            stale_marker.unlink()

        return ReauditResult(passed=True, reason="")

    def _write_cost_json(self) -> None:
        """Write COST.json with per-section running totals."""
        cost_data = {
            "schema_version": "phase4.cost.v1",
            "run_id": self._run_id,
            "total_cost_cny": self._calculate_total_cost_cny(),
            "total_cost_usd": self._calculate_total_cost(),
            "sections": [
                {
                    "section_id": s["section_id"],
                    "cost_cny": s.get("cost_cny", 0.0),
                    "cost_usd": s.get("cost_usd", 0.0),
                }
                for s in self._section_registry.get("sections", [])
            ],
            "updated_at": _now(),
        }
        cost_path = self._work_dir / "COST.json"
        cost_path.write_text(json.dumps(cost_data, indent=2), encoding="utf-8")
        self._register_artifact("cost", cost_path)

    def _write_section_transition_contracts(self) -> None:
        """Write SECTION_TRANSITION_CONTRACTS.json from section registry."""
        contracts = [
            {
                "section_id": s["section_id"],
                "transition_to_next": s.get("transition_contract", {}),
            }
            for s in self._section_registry.get("sections", [])
        ]
        path = self._work_dir / "SECTION_TRANSITION_CONTRACTS.json"
        path.write_text(json.dumps({
            "schema_version": "phase4.transition_contracts.v1",
            "run_id": self._run_id,
            "contracts": contracts,
            "created_at": _now(),
        }, indent=2), encoding="utf-8")
        self._register_artifact("section_transition_contracts", path)

    def _write_global_citation_map(self) -> None:
        """Build FULL_REVIEW_CITATION_MAP.json from per-section SECTION_CITATION_MAP.json.

        Only body-text citations are included (not all candidate papers from the ledger).
        claim_ids are resolved from SECTION_EVIDENCE_PACKET.json.
        Entries with no SECTION_CITATION_MAP.json record trace_status='unresolved'.
        """
        from .evidence_packet_parser import load_section_evidence_packet

        entries = []
        for section in self._section_registry.get("sections", []):
            section_id = section["section_id"]
            work_dir = Path(section.get("work_dir", ""))

            # Load evidence packet to map chunk_id → claim_ids
            ep_path = work_dir / "SECTION_EVIDENCE_PACKET.json"
            chunk_to_claims: Dict[str, List[str]] = {}
            if ep_path.exists():
                try:
                    packet = load_section_evidence_packet(ep_path)
                    for item in packet.items:
                        chunk_to_claims[item.chunk_id] = item.claim_ids
                except Exception:
                    pass

            # Read per-section citation map (actual body citations)
            cit_path = work_dir / "SECTION_CITATION_MAP.json"
            if not cit_path.exists():
                # Record unresolved if draft has [REF:*] markers
                draft_path = work_dir / "SECTION_DRAFT_EN.md"
                if draft_path.exists():
                    import re as _re
                    draft_text = draft_path.read_text(encoding="utf-8")
                    refs = _re.findall(r'\[REF:[^\]]+\]', draft_text)
                    for ref in refs:
                        entries.append({
                            "section_id": section_id,
                            "paper_id": ref,
                            "chunk_ids": [],
                            "sentence_indices": [],
                            "citation_type": "unknown",
                            "entailment_verdict": "unknown",
                            "claim_ids": [],
                            "trace_status": "unresolved",
                        })
                continue

            try:
                cit_data = json.loads(cit_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            # Per-paper aggregation: group by paper_id
            paper_entries: Dict[str, Dict[str, Any]] = {}
            for cit in cit_data.get("citations", []):
                for paper_id in cit.get("paper_ids", []):
                    if not paper_id:
                        continue
                    if paper_id not in paper_entries:
                        paper_entries[paper_id] = {
                            "section_id": section_id,
                            "paper_id": paper_id,
                            "chunk_ids": [],
                            "sentence_indices": [],
                            "citation_type": cit.get("citation_type", ""),
                            "entailment_verdict": cit.get("entailment_verdict", ""),
                            "claim_ids": [],
                            "trace_status": "verified",
                        }
                    entry = paper_entries[paper_id]
                    for cid in cit.get("chunk_ids", []):
                        if cid not in entry["chunk_ids"]:
                            entry["chunk_ids"].append(cid)
                            entry["claim_ids"].extend(
                                c for c in chunk_to_claims.get(cid, [])
                                if c not in entry["claim_ids"]
                            )
                    idx = cit.get("sentence_index")
                    if idx is not None and idx not in entry["sentence_indices"]:
                        entry["sentence_indices"].append(idx)

            entries.extend(paper_entries.values())

        path = self._work_dir / "FULL_REVIEW_CITATION_MAP.json"
        path.write_text(json.dumps({
            "schema_version": "phase4.citation_map.v1",
            "run_id": self._run_id,
            "citations": entries,
            "total_citations": len(entries),
            "created_at": _now(),
        }, indent=2), encoding="utf-8")
        self._register_artifact("citation_map", path)

    def _record_claim_ownership(self, section: Dict[str, Any], section_id: str) -> None:
        """Track which section authors which claim_id in CLAIM_OWNERSHIP_MAP.json."""
        path = self._work_dir / "CLAIM_OWNERSHIP_MAP.json"
        existing: Dict[str, Any] = {}
        if path.exists():
            existing = _read_json(path) or {}
        ownership = existing.get("ownership", {})
        for claim in section.get("claims", []):
            cid = claim.get("claim_id", "")
            if cid:
                ownership[cid] = section_id
        doc = {
            "schema_version": "phase4.claim_ownership.v1",
            "run_id": self._run_id,
            "ownership": ownership,
            "updated_at": _now(),
        }
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        self._register_artifact("claim_ownership_map", path)


def reconcile_existing_run(run_dir: Path) -> Dict[str, Any]:
    """Rebuild a durable R4 run's manuscript/package without model calls.

    This is intentionally a reconciliation operation, not ``resume()``:
    section drafts, section audits, the registry, and the existing blueprint
    are read from disk; no authoring worker, Qwen call, Semantic Scholar call,
    or revision loop is entered.  It is safe for a run that stopped at
    ``awaiting_human_review`` and repairs the final handoff artifacts in place.
    """
    run_dir = Path(run_dir).resolve()
    if not run_dir.exists():
        raise FileNotFoundError(f"R4 run directory not found: {run_dir}")

    context = _read_json(run_dir / "REVIEW_ORCHESTRATION_CONTEXT.json")
    blueprint_path = _resolve_path(context.get("blueprint_path"))
    if blueprint_path is None:
        raise FileNotFoundError(
            "Cannot reconcile run: REVIEW_ORCHESTRATION_CONTEXT.json has no existing blueprint_path"
        )
    raw_config = context.get("config", {}) if isinstance(context.get("config"), dict) else {}
    phase3_root = _resolve_path(raw_config.get("phase3_artifacts_root"))
    phase3_mode = str(raw_config.get("phase3_handoff_mode") or "canonical")

    config = OrchestratorConfig(
        blueprint_path=blueprint_path,
        output_root=run_dir.parent,
        phase3_artifacts_root=phase3_root,
        phase3_handoff_mode=phase3_mode,
        use_llm_audit=False,
    )
    orchestrator = FullReviewOrchestrator(config, run_dir=run_dir)
    orchestrator._run_id = run_dir.name
    orchestrator._work_dir = run_dir
    orchestrator._state = _read_json(run_dir / "REVIEW_STATE.json")
    orchestrator._section_registry = _read_json(run_dir / "SECTION_REGISTRY.json")
    if not orchestrator._section_registry.get("sections"):
        raise ValueError("Cannot reconcile run: SECTION_REGISTRY.json has no sections")

    # Preserve existing artifact references while replacing only the final
    # reconciliation outputs.
    old_package = _read_json(run_dir / "FULL_REVIEW_PACKAGE.json")
    for key, raw_path in (old_package.get("artifacts", {}) or {}).items():
        path = Path(str(raw_path))
        if path.exists():
            orchestrator._written_artifacts[str(key)] = path

    orchestrator._merge_drafts()
    merged_path = run_dir / "FULL_REVIEW_DRAFT_EN.md"
    audit = orchestrator._auditor.audit(
        merged_path,
        orchestrator._section_registry,
        run_dir,
        round_num=0,
    )
    final_audit = orchestrator._reconcile_final_audit(audit)
    audit_path = run_dir / "RECONCILIATION_AUDIT.json"
    audit_path.write_text(json.dumps(final_audit, indent=2), encoding="utf-8")
    orchestrator._register_artifact("reconciliation_audit", audit_path)

    status = orchestrator._determine_final_status(final_audit)
    merged_text = merged_path.read_text(encoding="utf-8") if merged_path.exists() else ""
    report = {
        "schema_version": "phase4.r4_reconciliation.v1",
        "source_run": str(run_dir),
        "blueprint_path": str(blueprint_path),
        "status": status,
        "replayed_without_llm": True,
        "qwen_calls": 0,
        "semantic_scholar_calls": 0,
        "merged_draft_path": str(merged_path),
        "merged_draft_word_count": len(re.findall(r"\b[\w'-]+\b", merged_text)),
        "merged_sections": orchestrator._merged_section_metadata,
        "excluded_sections": orchestrator._excluded_section_metadata,
        "total_flags": final_audit.get("total_flags", 0),
        "blocking_flags": final_audit.get("blocking_flags", 0),
        "section_flags_total": final_audit.get("section_flags_total", 0),
        "section_blocking_flags": final_audit.get("section_blocking_flags", 0),
        "section_flag_summary": final_audit.get("section_flag_summary", []),
        "created_at": _now(),
    }
    report_path = run_dir / "RECONCILIATION_REPORT.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    orchestrator._register_artifact("reconciliation_report", report_path)
    orchestrator._reconciliation_metadata = {
        "schema_version": "phase4.r4_reconciliation.v1",
        "report_path": str(report_path),
        "audit_path": str(audit_path),
        "replayed_without_llm": True,
    }

    orchestrator._update_state(
        status,
        reconciliation_report=str(report_path),
        reconciliation_audit=str(audit_path),
    )
    orchestrator._checkpoint_registry()
    orchestrator._write_final_package(status, final_audit)
    return report
