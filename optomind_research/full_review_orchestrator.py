"""FullReviewOrchestrator — drives the complete review production pipeline.

Design rules:
- Reads all stage prompts from prompts/; never hard-codes system prompts in Python.
- S5-S20 have real planning, grounding, writing, review, and delivery handlers.
- S1-S4 remain explicit bridge placeholders for the separately validated upstream
  query/retrieval/KB chain; real_llm=True fails closed if those bridges are used.
- Delegates provenance to ArtifactRegistry (state is the single source of truth).
  The manifest mirrors state's registry; they never diverge.
- Supports checkpoint/resume: completed/skipped stages skip; needs_human pauses.
- Atomic writes: state and manifest both use tmp→rename to avoid partial files.
- from_stage and only_stage must be valid stage IDs; invalid IDs raise ValueError.
- When from_stage is used, both state AND manifest are reset for the affected stages.
- Each stage records input_artifact_ids from the outputs of its predecessor stage.
- Attempt versioning: each run of a stage writes to attempt_<N>/ subdirectory;
  old outputs are never overwritten — sha256 remains verifiable after rerun.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from optomind_research.artifact_registry import ArtifactRef, utc_now
from optomind_research.full_review_state import (
    FullReviewState,
    RevisionEntry,
    PIPELINE_STAGE_IDS,
)
from optomind_research.run_manifest import ResearchRunManifest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PROJECT_ROOT / "prompts"
ORCHESTRATOR_PROMPT_PATH = PROMPTS_DIR / "Full Review Orchestrator.txt"


class RunIdMismatchError(RuntimeError):
    """Raised when state.run_id does not match the existing manifest's run_id."""


def _read_prompt(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return f"[PLACEHOLDER] Prompt file not found: {path.name}"


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON to a temp file then atomically rename to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_str = tempfile.mkstemp(
        dir=str(path.parent), prefix=".orch_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp_str, str(path))
    except Exception:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# StageResult
# ---------------------------------------------------------------------------

class StageResult:
    """Return value from each stage handler."""

    def __init__(
        self,
        *,
        status: str = "completed",
        output_path: Optional[Path] = None,
        artifact_type: str = "report",
        notes: str = "",
        error: str = "",
        stop_reason: str = "",
    ) -> None:
        self.status = status        # completed | failed | skipped | needs_human
        self.output_path = output_path
        self.artifact_type = artifact_type
        self.notes = notes
        self.error = error
        self.stop_reason = stop_reason


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class FullReviewOrchestrator:
    """Drives all pipeline stages with checkpoint/resume, attempt versioning,
    needs_human pausing, and fail-closed real_llm enforcement.

    Usage:
        state = FullReviewState.new(user_query="...", domain="optical_science")
        orch = FullReviewOrchestrator(output_dir=Path("outputs/my_run"))
        orch.run(state)
    """

    # Stage → ArtifactRef attribute on FullReviewState
    _STAGE_TO_REF_ATTR: dict[str, str] = {
        "S1_query_planning":       "query_plan_ref",
        "S2_literature_retrieval": "corpus_ref",
        "S3_kb_construction":      "knowledge_base_ref",
        "S4_concept_mapping":      "concept_map_ref",
        "S5_review_charter":       "review_charter_ref",
        "S6_mentor_advice":        "mentor_advice_ref",
        "S7_blueprint_candidates": "blueprint_candidates_ref",
        "S8_blueprint_selection":  "selected_blueprint_ref",
        "S9_section_contracts":    "section_contracts_ref",
        "S10_evidence_portfolios": "evidence_portfolios_ref",
        "S11_gap_resolution":      "gap_history_ref",
        "S12_visual_planning":     "visual_plans_ref",
        "S13_section_drafts":      "section_drafts_ref",
        "S14_citation_audits":     "citation_audits_ref",
        "S15_cross_section_edit":  "cross_section_edit_ref",   # F1-fix: was missing
        "S16_supervisor_review":   "supervisor_review_ref",    # F1-fix: was global_review_ref
        "S17_feedback_revision":   "feedback_revision_ref",    # F1-fix: was missing
        "S18_global_review":       "global_review_ref",        # F1-fix: now correctly S18
        "S19_peer_reviews":        "peer_reviews_ref",
        "S20_revision_loop":       "revision_loop_ref",
        "S20_final_translation":   "final_outputs_ref",
    }

    # Explicit data dependencies.  A long-form review is not a purely linear
    # pipeline: later stages consume the KB, blueprint, evidence and visuals
    # together.  Recording only the immediately preceding stage would create a
    # misleading provenance graph once real handlers are connected.
    _STAGE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
        "S1_query_planning": (),
        "S2_literature_retrieval": ("S1_query_planning",),
        "S3_kb_construction": ("S2_literature_retrieval",),
        "S4_concept_mapping": ("S3_kb_construction",),
        "S5_review_charter": ("S1_query_planning", "S3_kb_construction", "S4_concept_mapping"),
        "S6_mentor_advice": ("S1_query_planning", "S4_concept_mapping", "S5_review_charter"),
        "S7_blueprint_candidates": ("S4_concept_mapping", "S5_review_charter", "S6_mentor_advice"),
        "S8_blueprint_selection": ("S5_review_charter", "S6_mentor_advice", "S7_blueprint_candidates"),
        "S9_section_contracts": ("S5_review_charter", "S8_blueprint_selection"),
        "S10_evidence_portfolios": ("S3_kb_construction", "S8_blueprint_selection", "S9_section_contracts"),
        "S11_gap_resolution": ("S3_kb_construction", "S9_section_contracts", "S10_evidence_portfolios"),
        "S12_visual_planning": ("S3_kb_construction", "S8_blueprint_selection", "S10_evidence_portfolios", "S11_gap_resolution"),
        "S13_section_drafts": ("S3_kb_construction", "S8_blueprint_selection", "S9_section_contracts", "S10_evidence_portfolios", "S11_gap_resolution", "S12_visual_planning"),
        "S14_citation_audits": ("S3_kb_construction", "S10_evidence_portfolios", "S13_section_drafts"),
        "S15_cross_section_edit": ("S8_blueprint_selection", "S9_section_contracts", "S13_section_drafts", "S14_citation_audits"),
        "S16_supervisor_review": ("S5_review_charter", "S8_blueprint_selection", "S10_evidence_portfolios", "S11_gap_resolution", "S12_visual_planning", "S15_cross_section_edit"),
        "S17_feedback_revision": ("S3_kb_construction", "S10_evidence_portfolios", "S12_visual_planning", "S15_cross_section_edit", "S16_supervisor_review"),
        "S18_global_review": ("S5_review_charter", "S8_blueprint_selection", "S9_section_contracts", "S15_cross_section_edit", "S17_feedback_revision"),
        "S19_peer_reviews": ("S5_review_charter", "S8_blueprint_selection", "S18_global_review"),
        "S20_revision_loop": ("S3_kb_construction", "S5_review_charter", "S9_section_contracts", "S16_supervisor_review", "S17_feedback_revision", "S18_global_review", "S19_peer_reviews"),
        "S20_final_translation": ("S5_review_charter", "S20_revision_loop"),
    }

    _STAGE_ARTIFACT_TYPES: dict[str, str] = {
        "S2_literature_retrieval": "corpus",
        "S3_kb_construction": "knowledge_base",
        "S7_blueprint_candidates": "blueprint",
        "S8_blueprint_selection": "blueprint",
        "S10_evidence_portfolios": "evidence_packet",
        "S13_section_drafts": "review",
        "S15_cross_section_edit": "review",
        "S17_feedback_revision": "review",
        "S20_revision_loop": "review",
        "S20_final_translation": "review",
    }

    def __init__(
        self,
        output_dir: Path,
        *,
        real_llm: bool = False,
        domain: str = "optical_science",
        kb_path: Path | str | None = None,
        enable_external_gap_retrieval: bool = False,
        external_gap_max_rounds: int = 2,
        external_gap_max_claims: int = 6,
        visual_rerank_max_items: int | None = None,
        generate_conceptual_visuals: bool = True,
        max_generated_conceptual_visuals: int = 4,
        require_human_supervisor_approval: bool = False,
        production_mode: Optional[bool] = None,
        publication_revision_loop: bool = True,
        revision_max_rounds: int = 3,
        revision_max_tasks_per_round: int = 8,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.real_llm = real_llm
        self.domain = domain
        self.kb_path = Path(kb_path) if kb_path else None
        self.enable_external_gap_retrieval = bool(enable_external_gap_retrieval)
        self.external_gap_max_rounds = max(0, int(external_gap_max_rounds))
        self.external_gap_max_claims = max(1, int(external_gap_max_claims))
        self.visual_rerank_max_items = (
            None if visual_rerank_max_items is None else max(0, int(visual_rerank_max_items))
        )
        self.generate_conceptual_visuals = bool(generate_conceptual_visuals)
        self.max_generated_conceptual_visuals = max(
            0, int(max_generated_conceptual_visuals)
        )
        self.require_human_supervisor_approval = bool(require_human_supervisor_approval)
        self.production_mode = (
            bool(real_llm) if production_mode is None else bool(production_mode)
        )
        if self.production_mode and not real_llm:
            raise ValueError(
                "production_mode=True requires real_llm=True. Deterministic "
                "offline artifacts are marked non-production and can never "
                "enter the production mainline."
            )
        self.publication_revision_loop = bool(publication_revision_loop)
        self.revision_max_rounds = max(1, int(revision_max_rounds))
        self.revision_max_tasks_per_round = max(1, int(revision_max_tasks_per_round))
        # Optional test/diagnostic role provider for the M4 S15 proposer.
        # Production runs the live Qwen provider; this is never a substitute
        # for a deterministic scientific decision.
        self._m4_role_provider: Optional[Callable[..., Any]] = None
        self._state_path = self.output_dir / "full_review_state.json"
        self._manifest_path = self.output_dir / "run_manifest.json"
        self._manifest: Optional[ResearchRunManifest] = None

        self._handlers: dict[str, Callable[[FullReviewState], StageResult]] = {
            "S1_query_planning":       self._run_stage_S1,
            "S2_literature_retrieval": self._run_stage_S2,
            "S3_kb_construction":      self._run_stage_S3,
            "S4_concept_mapping":      self._run_stage_S4,
            "S5_review_charter":       self._run_stage_S5,
            "S6_mentor_advice":        self._run_stage_S6,
            "S7_blueprint_candidates": self._run_stage_S7,
            "S8_blueprint_selection":  self._run_stage_S8,
            "S9_section_contracts":    self._run_stage_S9,
            "S10_evidence_portfolios": self._run_stage_S10,
            "S11_gap_resolution":      self._run_stage_S11,
            "S12_visual_planning":     self._run_stage_S12,
            "S13_section_drafts":      self._run_stage_S13,
            "S14_citation_audits":     self._run_stage_S14,
            "S15_cross_section_edit":  self._run_stage_S15,
            "S16_supervisor_review":   self._run_stage_S16,
            "S17_feedback_revision":   self._run_stage_S17,
            "S18_global_review":       self._run_stage_S18,
            "S19_peer_reviews":        self._run_stage_S19,
            "S20_revision_loop":       self._run_stage_S20_revision_loop,
            "S20_final_translation":   self._run_stage_S20,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        state: FullReviewState,
        *,
        from_stage: Optional[str] = None,
        only_stage: Optional[str] = None,
    ) -> FullReviewState:
        """Execute the pipeline.

        Raises:
            ValueError: if from_stage or only_stage is not a valid stage ID.
            RunIdMismatchError: if an existing manifest's run_id differs from state.run_id.
        """
        # F1-8: Validate stage arguments up front.
        if from_stage is not None and from_stage not in PIPELINE_STAGE_IDS:
            raise ValueError(
                f"from_stage={from_stage!r} is not a valid stage ID. "
                f"Valid IDs: {PIPELINE_STAGE_IDS}"
            )
        if only_stage is not None and only_stage not in PIPELINE_STAGE_IDS:
            raise ValueError(
                f"only_stage={only_stage!r} is not a valid stage ID. "
                f"Valid IDs: {PIPELINE_STAGE_IDS}"
            )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        state.domain = state.domain or self.domain

        # F1-2: Load or create manifest; verify run_id consistency.
        self._manifest = self._init_manifest(state)
        state.manifest_path = str(self._manifest_path)

        # F1-9: When from_stage is set, synchronously reset both state and manifest.
        if from_stage:
            self._reset_from_stage(state, from_stage)

        stages_to_run = [only_stage] if only_stage else list(state.stage_order)

        print(
            f"[FullReviewOrchestrator] run_id={state.run_id}  "
            f"real_llm={self.real_llm}  domain={state.domain}"
        )

        for stage_id in stages_to_run:
            if stage_id not in self._handlers:
                print(f"  [WARN] No handler for stage {stage_id!r} — skipping.")
                state.skip_stage(stage_id, reason="no_handler")
                continue

            if state.is_stage_done(stage_id) and not only_stage:
                print(f"  [SKIP] {stage_id} — already {state.stage_status(stage_id)}")
                continue

            print(f"  [RUN ] {stage_id}")

            # F1-4: Record input artifact IDs before running.
            input_ids = self._collect_input_artifact_ids(state, stage_id)

            # A process interruption leaves the active stage in ``running``
            # state and its handler-owned checkpoints on disk. Resuming that
            # stage must reuse the same attempt directory; incrementing the
            # attempt number here would silently orphan potentially hours of
            # verified retrieval or revision work. Failed/completed stages
            # that are explicitly reset still receive a new attempt number.
            resuming_interrupted_attempt = state.stage_status(stage_id) == "running"
            if resuming_interrupted_attempt:
                stage_record = state.stages[stage_id]
                stage_record.error = ""
                stage_record.stop_reason = ""
                stage_record.notes = "resuming_interrupted_attempt"
                if not stage_record.started_at:
                    stage_record.started_at = utc_now()
                state.updated_at = utc_now()
            else:
                stage_record = state.start_stage(stage_id)
            stage_record.input_artifact_ids = input_ids
            self._manifest.set_status("running")
            manifest_stage = self._manifest.stage_statuses.get(stage_id)
            if manifest_stage is not None:
                manifest_stage.started_at = utc_now()
                manifest_stage.completed_at = ""
                manifest_stage.error = ""
                manifest_stage.notes = ""
            self._manifest.mark_stage(stage_id, "running")
            self._save_state(state)
            self._save_manifest(state)

            try:
                result = self._handlers[stage_id](state)
            except Exception as exc:
                tb = traceback.format_exc()
                result = StageResult(
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    stop_reason="unhandled_exception",
                    notes=tb[:800],
                )

            # Register output artifact if produced.
            artifact_ids: list[str] = []
            if result.output_path and result.output_path.exists():
                ref = state.register_file(
                    result.output_path,
                    artifact_type=result.artifact_type,
                    producer=stage_id,
                )
                artifact_ids.append(ref.artifact_id)
                # F1-10: Manifest registry mirrors state registry at save time.
                # Failed or skipped outputs remain auditable in the registry,
                # but must never replace the active accepted stage artifact.
                if result.status in {"completed", "needs_human"}:
                    self._attach_stage_ref(state, stage_id, ref)
                # F1-1: Record this attempt in the stage history.
                state.stages[stage_id].record_attempt(
                    artifact_id=ref.artifact_id,
                    path=ref.path,
                    sha256=ref.sha256,
                    active=result.status in {"completed", "needs_human"},
                )

            # Dispatch on result status.
            if result.status == "completed":
                state.complete_stage(stage_id, output_artifact_ids=artifact_ids, notes=result.notes)
                self._manifest.mark_stage(stage_id, "completed", notes=result.notes)
                print(f"         → completed  {result.notes}")

            elif result.status == "skipped":
                state.skip_stage(
                    stage_id,
                    reason=result.stop_reason or "handler_skip",
                    notes=result.notes,
                )
                self._manifest.mark_stage(stage_id, "skipped", notes=result.notes)
                print(f"         → skipped    {result.stop_reason}")

            elif result.status == "needs_human":
                # F1-3: Pause pipeline; do NOT mark as failed.
                state.needs_human_stage(
                    stage_id,
                    reason=result.stop_reason or "needs_human_input",
                    notes=result.notes,
                )
                self._manifest.mark_stage(stage_id, "needs_human", notes=result.notes)
                state.status = "needs_human"
                self._save_state(state)
                self._manifest.set_status("needs_human")
                self._save_manifest(state)
                print(f"         → needs_human  {result.stop_reason}")
                break  # Pause; resume next call.

            else:  # failed
                state.fail_stage(
                    stage_id,
                    error=result.error,
                    stop_reason=result.stop_reason,
                    notes=result.notes,
                )
                self._manifest.add_error(
                    stage=stage_id,
                    module="FullReviewOrchestrator",
                    error_type="stage_failure",
                    message=result.error[:500],
                )
                self._manifest.mark_stage(stage_id, "failed", error=result.error[:200])
                print(f"         → FAILED     {result.error[:120]}")
                self._save_state(state)
                self._manifest.set_status("failed")
                self._save_manifest(state)
                break

            self._save_state(state)
            self._save_manifest(state)

        # Final pipeline status.
        any_needs_human = any(
            state.stage_status(sid) == "needs_human" for sid in state.stage_order
        )
        any_failed = any(
            state.stage_status(sid) == "failed" for sid in state.stage_order
        )
        all_done = all(state.is_stage_done(sid) for sid in state.stage_order)

        if any_needs_human:
            state.status = "needs_human"
        elif any_failed:
            state.status = "failed"
        elif all_done:
            state.status = "completed"
        # else: still running (only_stage or partial run)

        self._save_state(state)
        m_status = "needs_human" if any_needs_human else ("failed" if any_failed else ("completed" if all_done else "running"))
        self._manifest.set_status(m_status)
        self._save_manifest(state)
        print(f"[FullReviewOrchestrator] pipeline status → {state.status}")
        return state

    # ------------------------------------------------------------------
    # Checkpoint helpers
    # ------------------------------------------------------------------

    def save_state(self, state: FullReviewState) -> None:
        self._save_state(state)

    def load_state(self) -> Optional[FullReviewState]:
        if self._state_path.exists():
            return FullReviewState.load(self._state_path)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_manifest(self, state: FullReviewState) -> ResearchRunManifest:
        """Load existing manifest (verifying run_id) or create a fresh one.

        F1-2: Raises RunIdMismatchError if run_id of loaded manifest differs from state.
        """
        if self._manifest_path.exists():
            manifest = ResearchRunManifest.load(self._manifest_path)
            if manifest.run_id != state.run_id:
                raise RunIdMismatchError(
                    f"run_id mismatch: state.run_id={state.run_id!r} but "
                    f"existing manifest has run_id={manifest.run_id!r}. "
                    f"Use a fresh output directory for a new run, or --resume to continue "
                    f"the existing run."
                )
            return manifest
        manifest = ResearchRunManifest(run_id=state.run_id, domain=state.domain)
        # A production run may be bootstrapped from already verified reusable
        # S1-S4 artifacts.  Mirror those imported stage states into a newly
        # created manifest so the state file and audit manifest cannot disagree.
        for stage_id in state.stage_order:
            status = state.stage_status(stage_id)
            if status in {"completed", "skipped"}:
                rec = state.stages.get(stage_id)
                manifest.mark_stage(stage_id, status, notes=(rec.notes if rec else ""))
        return manifest

    def _save_state(self, state: FullReviewState) -> None:
        """Atomic save of state (tmp → rename)."""
        state.save(self._state_path)

    def _save_manifest(self, state: FullReviewState) -> None:
        """F1-10: Sync manifest artifact registry from state, then atomic save."""
        # State is the single source of truth; manifest registry is a mirror.
        self._manifest.artifact_registry = state.artifact_registry
        # Atomic write
        tmp_fd, tmp_str = tempfile.mkstemp(
            dir=str(self._manifest_path.parent),
            prefix=".manifest_tmp_",
            suffix=".json",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._manifest.to_dict(), ensure_ascii=False, indent=2))
            os.replace(tmp_str, str(self._manifest_path))
        except Exception:
            try:
                os.unlink(tmp_str)
            except OSError:
                pass
            raise

    def _reset_from_stage(self, state: FullReviewState, from_stage: str) -> None:
        """F1-9: Reset both state and manifest for from_stage and all later stages."""
        idx = state.stage_order.index(from_stage)  # already validated
        for sid in state.stage_order[idx:]:
            if sid in state.stages:
                rec = state.stages[sid]
                rec.status = "pending"
                rec.started_at = ""
                rec.completed_at = ""
                rec.error = ""
                rec.stop_reason = ""
                rec.notes = ""
                rec.output_artifact_ids = []
                rec.input_artifact_ids = []
                for attempt in rec.attempt_history:
                    attempt["active"] = False
                ref_attr = self._STAGE_TO_REF_ATTR.get(sid)
                if ref_attr:
                    setattr(state, ref_attr, None)
            # Reset in manifest too.
            if self._manifest and sid in self._manifest.stage_statuses:
                self._manifest.stage_statuses[sid].status = "pending"
                self._manifest.stage_statuses[sid].error = ""
                self._manifest.stage_statuses[sid].started_at = ""
                self._manifest.stage_statuses[sid].completed_at = ""
                self._manifest.stage_statuses[sid].notes = ""
        state.status = "running"
        if self._manifest:
            self._manifest.set_status("running")

    def _collect_input_artifact_ids(
        self, state: FullReviewState, stage_id: str
    ) -> list[str]:
        """Return active output artifact IDs of all declared dependencies."""
        artifact_ids: list[str] = []
        for dependency in self._STAGE_DEPENDENCIES.get(stage_id, ()):
            rec = state.stages.get(dependency)
            if rec and rec.output_artifact_ids:
                artifact_ids.extend(rec.output_artifact_ids)
        return list(dict.fromkeys(artifact_ids))

    @staticmethod
    def _attach_stage_ref(state: FullReviewState, stage_id: str, ref: ArtifactRef) -> None:
        attr = FullReviewOrchestrator._STAGE_TO_REF_ATTR.get(stage_id)
        if attr:
            setattr(state, attr, ref)

    # ------------------------------------------------------------------
    # Placeholder stage handlers
    # ------------------------------------------------------------------

    def _placeholder(
        self,
        stage_id: str,
        state: FullReviewState,
        payload: dict,
    ) -> StageResult:
        """F1-7: Fail-closed when real_llm=True (no real handler yet).
        F1-1: Writes to attempt_<N>/ subdirectory to avoid overwriting prior runs.
        """
        if self.real_llm:
            raise NotImplementedError(
                f"Stage {stage_id!r} has no real LLM implementation. "
                "Disable --real-llm or implement the handler before running in real mode. "
                "Placeholder outputs must never be presented as real pipeline results."
            )

        rec = state.stages.get(stage_id)
        attempt = rec.attempt_number if rec else 1
        out_dir = self.output_dir / stage_id / f"attempt_{attempt}"
        out_path = out_dir / f"{stage_id}.stub.json"
        _atomic_write_json(out_path, {
            "schema": f"{stage_id}.stub.v1",
            "stage_id": stage_id,
            "run_id": state.run_id,
            "real_llm": False,
            "placeholder": True,
            "attempt_number": attempt,
            "production": False,
            "admission_marker": "non_production",
            "created_at": utc_now(),
            **payload,
        })
        return StageResult(
            output_path=out_path,
            artifact_type=self._STAGE_ARTIFACT_TYPES.get(stage_id, "report"),
            notes=f"placeholder attempt={attempt} for {stage_id}",
        )

    def _run_stage_S1(self, state: FullReviewState) -> StageResult:
        prompt_text = _read_prompt(PROMPTS_DIR / "Query Planner.txt")
        return self._placeholder("S1_query_planning", state, {
            "user_query": state.user_query,
            "prompt_preview": prompt_text[:200],
            "query_plan": {
                "problem_understanding": "[PLACEHOLDER] English reformulation of user query.",
                "scope_definition": {
                    "main_scope": "[PLACEHOLDER] One-sentence scope.",
                    "scope_items": ["[PLACEHOLDER] Focus 1", "[PLACEHOLDER] Focus 2"],
                },
                "keyword_decomposition": {"keywords": ["[PLACEHOLDER] kw1", "[PLACEHOLDER] kw2"]},
            },
        })

    def _run_stage_S2(self, state: FullReviewState) -> StageResult:
        return self._placeholder("S2_literature_retrieval", state, {
            "query_plan_path": str(state.query_plan_ref.path) if state.query_plan_ref else "",
            "corpus_summary": {"total_papers": 0, "sources": ["[PLACEHOLDER] arxiv"]},
        })

    def _run_stage_S3(self, state: FullReviewState) -> StageResult:
        return self._placeholder("S3_kb_construction", state, {
            "corpus_path": str(state.corpus_ref.path) if state.corpus_ref else "",
            "kb_summary": {"text_chunks": 0, "visual_chunks": 0, "papers_ingested": 0},
        })

    def _run_stage_S4(self, state: FullReviewState) -> StageResult:
        return self._placeholder("S4_concept_mapping", state, {
            "kb_path": str(state.knowledge_base_ref.path) if state.knowledge_base_ref else "",
            "concept_map_summary": {"clusters": 0, "nodes": 0},
        })

    # ------------------------------------------------------------------
    # Planning-stage real handlers (S5–S9)
    # ------------------------------------------------------------------

    def _load_artifact_json(self, ref_path: str) -> dict:
        """Load a JSON artifact from disk; return {} on any error."""
        if not ref_path:
            return {}
        try:
            return json.loads(Path(ref_path).read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _load_m4_approvals(path: Path) -> dict[str, str]:
        """Load patch decisions from the human approvals file, if present.

        Supports both a flat ``{patch_id: approved|declined}`` map and the
        S16-style ``{"decisions": [{patch_id, decision, ...}]}`` list form.
        """
        if not Path(path).is_file():
            return {}
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        decisions = raw.get("decisions")
        if isinstance(decisions, list):
            return {
                str(row.get("patch_id") or ""): str(row.get("decision") or "")
                for row in decisions
                if isinstance(row, dict) and row.get("patch_id")
            }
        return {
            str(key): str(value)
            for key, value in raw.items()
            if str(key) != "schema_version"
        }

    def _production_command_knowledge_error(self, mentor_data: dict) -> str:
        """Fail-closed S6 production gate.

        Production S6 advice must carry command_knowledge.status=ok and an
        exclusive papers/material-dossiers evidence authority. Any gap yields
        ``production_command_knowledge_unavailable``.
        """
        if not self.production_mode:
            return ""
        command = mentor_data.get("command_knowledge")
        if not isinstance(command, dict) or str(command.get("status") or "") != "ok":
            return (
                "production_command_knowledge_unavailable: S6 "
                "command_knowledge.status must be ok in production mode"
            )
        evidence = mentor_data.get("evidence_authority")
        if not isinstance(evidence, dict) or evidence.get("exclusive") is not True:
            return (
                "production_command_knowledge_unavailable: S6 "
                "evidence_authority must be exclusive in production mode"
            )
        authority = str(evidence.get("authority") or "")
        if "papers_and_material_dossiers" not in authority:
            return (
                "production_command_knowledge_unavailable: S6 evidence "
                "authority must be papers_and_material_dossiers in "
                "production mode"
            )
        return ""

    def _production_blueprint_admission_error(
        self, artifact: dict, *, stage_label: str
    ) -> str:
        """Fail-closed production admission gate for S7-S9 blueprint data."""
        if not self.production_mode:
            return ""
        if not isinstance(artifact, dict):
            return (
                f"production_blueprint_not_admitted: {stage_label} artifact "
                "is missing or malformed"
            )
        if artifact.get("production") is not True:
            return (
                f"production_blueprint_not_admitted: {stage_label} "
                f"production={artifact.get('production')!r}; only "
                "production=true artifacts are admitted"
            )
        if str(artifact.get("admission_decision") or "") != "admit":
            return (
                f"production_blueprint_not_admitted: {stage_label} "
                f"admission_decision={artifact.get('admission_decision')!r}; "
                "only admit artifacts are admitted"
            )
        return ""

    def _stage_output_path(
        self,
        state: FullReviewState,
        stage_id: str,
        filename: str,
    ) -> Path:
        rec = state.stages.get(stage_id)
        attempt = rec.attempt_number if rec else 1
        return self.output_dir / stage_id / f"attempt_{attempt}" / filename

    def _active_kb_path(self, state: FullReviewState) -> Path | None:
        """Prefer the explicit reusable KB; otherwise resolve the S3 artifact."""
        from optomind_research.full_review_evidence import resolve_kb_sqlite

        explicit = resolve_kb_sqlite(self.kb_path)
        if explicit:
            return explicit
        ref_path = state.knowledge_base_ref.path if state.knowledge_base_ref else ""
        return resolve_kb_sqlite(ref_path)

    def _prepare_intermediate_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Enforce the English-only agent boundary for S5-S19 artifacts.

        In real mode, CJK values are translated by the established normalizer.
        Mock mode never calls an API: isolated mojibake characters are repaired
        and any remaining CJK characters are visibly quarantined.
        """
        payload = dict(payload)
        payload.setdefault("production", self.production_mode)
        payload["admission_marker"] = (
            "production" if self.production_mode else "non_production"
        )
        if self.real_llm:
            from optomind_research.intermediate_language_guard import ensure_english_payload

            return ensure_english_payload(payload)
        from optomind_research.scientific_text_english_normalizer import (
            contains_cjk,
            repair_likely_scientific_mojibake,
        )

        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, str):
                repaired = repair_likely_scientific_mojibake(value)
                if contains_cjk(repaired):
                    repaired = re.sub(
                        r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]",
                        "[NON_ENGLISH_TEXT_QUARANTINED]",
                        repaired,
                    )
                return repaired
            return value

        return clean(payload)

    @staticmethod
    def _compact_concept_map_summary(concept_map: dict[str, Any]) -> dict[str, Any]:
        """Expose grounded multi-view content, not only obsolete flat keys."""
        existing = concept_map.get("concept_map_summary")
        if isinstance(existing, dict) and existing:
            return existing
        views_out: list[dict[str, Any]] = []
        labels: list[str] = []
        for view in concept_map.get("views") or []:
            if not isinstance(view, dict):
                continue
            nodes_out = []
            for node in (view.get("nodes") or [])[:8]:
                if not isinstance(node, dict):
                    continue
                label = str(node.get("label") or "").strip()
                if label:
                    labels.append(label)
                nodes_out.append(
                    {
                        "label": label,
                        "purpose": node.get("purpose", ""),
                        "planning_value": node.get("planning_value", ""),
                        "blueprint_roles": node.get("blueprint_roles", []),
                        "topic_overlap_score": node.get("topic_overlap_score", 0),
                        "evidence_counts": node.get("evidence_counts", {}),
                    }
                )
            views_out.append(
                {
                    "view_id": view.get("view_id", ""),
                    "name": view.get("name", ""),
                    "purpose": view.get("purpose", ""),
                    "node_count": len(view.get("nodes") or []),
                    "top_nodes": nodes_out,
                }
            )
        return {
            "view_count": len(views_out),
            "cluster_count": sum(item["node_count"] for item in views_out),
            "top_labels": labels[:40],
            "views": views_out,
            "planning_advisories": concept_map.get("planning_advisories", []),
        }

    def _run_stage_S5(self, state: FullReviewState) -> StageResult:
        """ReviewCharterAgent: convert query plan + concept map into a formal charter."""
        from optomind_research.review_charter_agent import ReviewCharterAgent

        query_plan_data = self._load_artifact_json(
            state.query_plan_ref.path if state.query_plan_ref else ""
        )
        concept_map_data = self._load_artifact_json(
            state.concept_map_ref.path if state.concept_map_ref else ""
        )

        # Extract concept_map_summary from artifact
        concept_map_summary = self._compact_concept_map_summary(concept_map_data)

        agent = ReviewCharterAgent(real_llm=self.real_llm)
        charter = agent.build_charter(
            user_query=state.user_query,
            query_plan=query_plan_data.get("query_plan") or query_plan_data,
            concept_map_summary=concept_map_summary,
            domain=state.domain,
        )

        rec = state.stages.get("S5_review_charter")
        attempt = rec.attempt_number if rec else 1
        out_dir = self.output_dir / "S5_review_charter" / f"attempt_{attempt}"
        out_path = out_dir / "review_charter.json"
        payload = self._prepare_intermediate_payload({
            "stage_id": "S5_review_charter",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            "attempt_number": attempt,
            **charter,
        })
        _atomic_write_json(out_path, payload)
        return StageResult(
            output_path=out_path,
            artifact_type="report",
            notes=f"charter title={charter.get('title', '')[:60]}",
        )

    def _run_stage_S6(self, state: FullReviewState) -> StageResult:
        """ReviewMentorAgent: generate M1-grounded structural advice."""
        from optomind_research.review_mentor_agent import ReviewMentorAgent

        charter_data = self._load_artifact_json(
            state.review_charter_ref.path if state.review_charter_ref else ""
        )
        concept_map_data = self._load_artifact_json(
            state.concept_map_ref.path if state.concept_map_ref else ""
        )

        problem_understanding = charter_data.get("central_question", "")
        scope_definition = charter_data.get("scope_statement", "")
        planning_evidence = self._compact_concept_map_summary(concept_map_data)

        agent = ReviewMentorAgent(real_llm=self.real_llm)
        advice = agent.build_advice(
            user_question=state.user_query,
            problem_understanding=problem_understanding,
            scope_definition=scope_definition,
            planning_evidence=planning_evidence,
        )
        command_error = self._production_command_knowledge_error(advice)
        if command_error:
            return StageResult(
                status="failed",
                error=command_error,
                stop_reason="production_command_knowledge_unavailable",
                notes=(
                    "S6 failed closed in production mode; no deterministic "
                    "mentor advice was admitted."
                ),
            )

        rec = state.stages.get("S6_mentor_advice")
        attempt = rec.attempt_number if rec else 1
        out_dir = self.output_dir / "S6_mentor_advice" / f"attempt_{attempt}"
        out_path = out_dir / "mentor_advice.json"
        payload = self._prepare_intermediate_payload({
            "stage_id": "S6_mentor_advice",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            "attempt_number": attempt,
            **advice,
        })
        _atomic_write_json(out_path, payload)
        n_moves = len(advice.get("usable_intellectual_moves") or [])
        return StageResult(
            output_path=out_path,
            artifact_type="report",
            notes=f"mentor_moves={n_moves}  mode={advice.get('mode','?')}",
        )

    def _run_stage_S7(self, state: FullReviewState) -> StageResult:
        """BlueprintCouncil: generate three structurally distinct candidate blueprints."""
        from optomind_research.blueprint_council import BlueprintCouncil

        charter_data = self._load_artifact_json(
            state.review_charter_ref.path if state.review_charter_ref else ""
        )
        mentor_data = self._load_artifact_json(
            state.mentor_advice_ref.path if state.mentor_advice_ref else ""
        )
        concept_map_data = self._load_artifact_json(
            state.concept_map_ref.path if state.concept_map_ref else ""
        )
        concept_map_summary = self._compact_concept_map_summary(concept_map_data)

        council = BlueprintCouncil(real_llm=self.real_llm)
        result = council.generate_candidates(
            charter=charter_data,
            concept_map_summary=concept_map_summary,
            mentor_advice=mentor_data,
            run_id=state.run_id,
        )
        admission_error = self._production_blueprint_admission_error(
            result, stage_label="S7_blueprint_candidates"
        )
        if admission_error:
            return StageResult(
                status="failed",
                error=admission_error,
                stop_reason="production_blueprint_not_admitted",
                notes=(
                    "S7 failed closed in production mode; the candidate set "
                    "was not admitted and no deterministic scientific outline "
                    "was generated."
                ),
            )

        rec = state.stages.get("S7_blueprint_candidates")
        attempt = rec.attempt_number if rec else 1
        out_dir = self.output_dir / "S7_blueprint_candidates" / f"attempt_{attempt}"
        out_path = out_dir / "blueprint_candidates.json"
        payload = self._prepare_intermediate_payload({
            "stage_id": "S7_blueprint_candidates",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            "attempt_number": attempt,
            **result,
        })
        _atomic_write_json(out_path, payload)
        n_candidates = len(result.get("candidates") or [])
        return StageResult(
            output_path=out_path,
            artifact_type="blueprint",
            notes=f"candidates={n_candidates}  logics=argument_first,chronological_synthesis,taxonomic_contrast",
        )

    def _run_stage_S8(self, state: FullReviewState) -> StageResult:
        """BlueprintTournamentJudge: compare candidates, select/unify, with needs_human pause.

        First call: generates recommendation → needs_human (optional human review).
        Resume call: loads recommendation + optional override → completed.
        """
        from optomind_research.blueprint_tournament_judge import BlueprintTournamentJudge

        stage_dir = self.output_dir / "S8_blueprint_selection"
        charter_data = self._load_artifact_json(
            state.review_charter_ref.path if state.review_charter_ref else ""
        )
        mentor_data = self._load_artifact_json(
            state.mentor_advice_ref.path if state.mentor_advice_ref else ""
        )
        candidates_data = self._load_artifact_json(
            state.blueprint_candidates_ref.path if state.blueprint_candidates_ref else ""
        )
        candidates = candidates_data.get("candidates") or []
        candidates_by_id = {c.get("candidate_id"): c for c in candidates}
        candidates_sha256 = candidates_data.get("candidates_sha256", "")
        candidates_admission_error = self._production_blueprint_admission_error(
            candidates_data, stage_label="S8_candidates"
        )
        if candidates_admission_error:
            return StageResult(
                status="failed",
                error=candidates_admission_error,
                stop_reason="production_blueprint_not_admitted",
                notes=(
                    "S8 refused non-admitted candidate input before the "
                    "judge in production mode."
                ),
            )

        # Resume only from the recommendation registered by the active S8 attempt.
        prior_ref = state.selected_blueprint_ref
        prior_path = Path(prior_ref.path) if prior_ref and prior_ref.path else None
        if prior_path and prior_path.name == "blueprint_recommendation.json" and prior_path.exists():
            recommendation = self._load_artifact_json(str(prior_path))
            if recommendation.get("candidates_sha256") != candidates_sha256:
                raise RuntimeError(
                    "Blueprint recommendation is stale relative to the active candidate set; rerun S8."
                )
            override_path = prior_path.parent / "blueprint_override.json"
            override = self._load_artifact_json(str(override_path)) if override_path.exists() else None
            judge = BlueprintTournamentJudge(real_llm=self.real_llm)
            selection = judge.finalize(
                recommendation,
                override=override,
                candidates_by_id=candidates_by_id,
            )
            selection_admission_error = (
                self._production_blueprint_admission_error(
                    selection, stage_label="S8_selection"
                )
            )
            if selection_admission_error:
                return StageResult(
                    status="failed",
                    error=selection_admission_error,
                    stop_reason="production_blueprint_not_admitted",
                    notes=(
                        "S8 refused a final selection that is not "
                        "production=true/admit in production mode."
                    ),
                )
            # A structural tournament can still select a rhetorically strong
            # but scientifically ill-posed outline.  Run a distinct pre-
            # evidence peer review that checks terminology, thesis strength,
            # section-role ownership, and manuscript continuity before S9.
            from optomind_research.blueprint_scientific_critic import (
                BlueprintScientificCritic,
            )

            scientific_review = BlueprintScientificCritic(
                real_llm=self.real_llm
            ).review(
                charter=charter_data,
                blueprint=selection.get("blueprint") or {},
            )
            if scientific_review.get("verdict") == "block":
                rec = state.stages.get("S8_blueprint_selection")
                blocked_attempt = rec.attempt_number if rec else 2
                blocked_dir = stage_dir / f"attempt_{blocked_attempt}"
                _atomic_write_json(
                    blocked_dir / "blueprint_scientific_review.blocked.json",
                    self._prepare_intermediate_payload({
                        "stage_id": "S8_blueprint_selection",
                        "run_id": state.run_id,
                        "real_llm": self.real_llm,
                        "attempt_number": blocked_attempt,
                        **scientific_review,
                    }),
                )
                raise RuntimeError(
                    "Selected blueprint failed the scientific scope/continuity review. "
                    "Inspect blueprint_scientific_review.blocked.json before proceeding to section contracts."
                )
            selection["blueprint"] = scientific_review.get("blueprint") or selection.get("blueprint") or {}
            selection["blueprint_scientific_review"] = {
                key: value
                for key, value in scientific_review.items()
                if key != "blueprint"
            }

            rec = state.stages.get("S8_blueprint_selection")
            attempt = rec.attempt_number if rec else 2
            out_dir = stage_dir / f"attempt_{attempt}"
            out_path = out_dir / "selected_blueprint.json"
            payload = self._prepare_intermediate_payload({
                "stage_id": "S8_blueprint_selection",
                "run_id": state.run_id,
                "real_llm": self.real_llm,
                "attempt_number": attempt,
                **selection,
            })
            _atomic_write_json(out_path, payload)
            human_tag = "human_confirmed" if selection.get("human_confirmed") else "auto_accepted"
            return StageResult(
                output_path=out_path,
                artifact_type="blueprint",
                notes=(
                    f"selected={selection.get('selected_candidate_id','?')}  {human_tag}  "
                    f"scientific_review={scientific_review.get('verdict','?')}"
                ),
            )

        # First call: generate a recommendation tied to this exact candidate hash.
        judge = BlueprintTournamentJudge(real_llm=self.real_llm)
        recommendation = judge.evaluate_and_recommend(
            candidates=candidates,
            charter=charter_data,
            mentor_advice=mentor_data,
        )
        rec = state.stages.get("S8_blueprint_selection")
        attempt = rec.attempt_number if rec else 1
        attempt_dir = stage_dir / f"attempt_{attempt}"
        attempt_path = attempt_dir / "blueprint_recommendation.json"
        _atomic_write_json(attempt_path, self._prepare_intermediate_payload(recommendation))

        selected_id = recommendation.get("selected_candidate_id", "?")
        return StageResult(
            status="needs_human",
            output_path=attempt_path,
            artifact_type="blueprint",
            stop_reason="awaiting_blueprint_approval",
            notes=(
                f"Recommendation: {selected_id}. "
                f"Resume to auto-accept, or create {attempt_dir}/blueprint_override.json "
                f"with {{\"choice_id\": \"BP-A/B/C\"}} to override."
            ),
        )

    def _run_stage_S9(self, state: FullReviewState) -> StageResult:
        """SectionContractDesigner: generate 11-field argument contracts for each section."""
        from optomind_research.section_contract_designer import SectionContractDesigner

        charter_data = self._load_artifact_json(
            state.review_charter_ref.path if state.review_charter_ref else ""
        )
        selection_data = self._load_artifact_json(
            state.selected_blueprint_ref.path if state.selected_blueprint_ref else ""
        )
        blueprint = selection_data.get("blueprint") or {}
        selection_admission_error = (
            self._production_blueprint_admission_error(
                selection_data, stage_label="S9_selected_blueprint"
            )
        )
        if selection_admission_error:
            return StageResult(
                status="failed",
                error=selection_admission_error,
                stop_reason="production_blueprint_not_admitted",
                notes=(
                    "S9 refused a selected blueprint that is not "
                    "production=true/admit in production mode."
                ),
            )

        designer = SectionContractDesigner(real_llm=self.real_llm)
        contracts_result = designer.design_contracts(
            charter=charter_data,
            blueprint=blueprint,
        )

        rec = state.stages.get("S9_section_contracts")
        attempt = rec.attempt_number if rec else 1
        out_dir = self.output_dir / "S9_section_contracts" / f"attempt_{attempt}"
        out_path = out_dir / "section_contracts.json"
        payload = self._prepare_intermediate_payload({
            "stage_id": "S9_section_contracts",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            "attempt_number": attempt,
            **contracts_result,
        })
        _atomic_write_json(out_path, payload)
        n = contracts_result.get("section_count", 0)
        total_words = contracts_result.get("total_word_budget", 0)
        return StageResult(
            output_path=out_path,
            artifact_type="report",
            notes=f"contracts={n}  total_word_budget={total_words}",
        )

    def _run_stage_S10(self, state: FullReviewState) -> StageResult:
        from optomind_research.full_review_evidence import build_evidence_portfolios

        selection = self._load_artifact_json(
            state.selected_blueprint_ref.path if state.selected_blueprint_ref else ""
        )
        contracts_data = self._load_artifact_json(
            state.section_contracts_ref.path if state.section_contracts_ref else ""
        )
        charter = self._load_artifact_json(
            state.review_charter_ref.path if state.review_charter_ref else ""
        )
        mentor = self._load_artifact_json(
            state.mentor_advice_ref.path if state.mentor_advice_ref else ""
        )
        blueprint = selection.get("blueprint") or selection
        contracts = contracts_data.get("contracts") or contracts_data.get("section_contracts") or []
        kb_path = self._active_kb_path(state)
        if self.real_llm and kb_path is None:
            raise RuntimeError(
                "S10 requires a real ReviewKnowledgeBase SQLite file. Pass kb_path/--kb-dir "
                "or attach a resolvable S3 knowledge-base artifact."
            )
        scope = " ".join(
            str(value or "")
            for value in (
                charter.get("central_question"),
                charter.get("scope_statement"),
            )
        )
        bundle = build_evidence_portfolios(
            blueprint,
            list(contracts),
            kb_path=kb_path,
            real_llm=self.real_llm,
            scope_definition=scope,
            mentor_advice=mentor,
        )
        payload = self._prepare_intermediate_payload({
            "stage_id": "S10_evidence_portfolios",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **bundle,
        })
        out_path = self._stage_output_path(
            state, "S10_evidence_portfolios", "evidence_portfolios.json"
        )
        _atomic_write_json(out_path, payload)
        summary = bundle.get("quality_summary") or {}
        return StageResult(
            output_path=out_path,
            artifact_type="evidence_packet",
            notes=(
                f"claims={summary.get('claim_count', 0)}  "
                f"bound={summary.get('claims_with_text_support', 0)}  "
                f"gaps={summary.get('claims_without_text_support', 0)}  "
                f"dag_edges={summary.get('dag_edge_count', 0)}"
            ),
        )

    def _run_stage_S11(self, state: FullReviewState) -> StageResult:
        from optomind_research.full_review_evidence import resolve_evidence_gaps

        evidence_bundle = self._load_artifact_json(
            state.evidence_portfolios_ref.path if state.evidence_portfolios_ref else ""
        )
        charter = self._load_artifact_json(
            state.review_charter_ref.path if state.review_charter_ref else ""
        )
        kb_path = self._active_kb_path(state)
        scope = " ".join(
            str(value or "")
            for value in (charter.get("central_question"), charter.get("scope_statement"))
        )
        out_path = self._stage_output_path(state, "S11_gap_resolution", "gap_history.json")
        bundle = resolve_evidence_gaps(
            evidence_bundle,
            kb_path=kb_path,
            real_llm=self.real_llm,
            scope_definition=scope,
            enable_external_oa=self.enable_external_gap_retrieval,
            external_output_dir=out_path.parent / "external_oa",
            max_external_rounds=self.external_gap_max_rounds,
            max_external_claims=self.external_gap_max_claims,
        )
        payload = self._prepare_intermediate_payload({
            "stage_id": "S11_gap_resolution",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **bundle,
        })
        _atomic_write_json(out_path, payload)
        return StageResult(
            output_path=out_path,
            artifact_type="report",
            notes=(
                f"unresolved_load_bearing={len(bundle.get('unresolved_load_bearing_claim_ids') or [])}  "
                f"stop={bundle.get('stop_reason', '')}"
            ),
        )

    def _run_stage_S12(self, state: FullReviewState) -> StageResult:
        from optomind_research.full_review_evidence import plan_visual_evidence

        gap_bundle = self._load_artifact_json(
            state.gap_history_ref.path if state.gap_history_ref else ""
        )
        kb_path = self._active_kb_path(state)
        out_path = self._stage_output_path(state, "S12_visual_planning", "visual_plans.json")
        bundle = plan_visual_evidence(
            gap_bundle,
            kb_path=kb_path,
            real_llm=self.real_llm,
            rerank_max_items=self.visual_rerank_max_items,
            rerank_workers=4,
            cache_path=self.output_dir / "persistent_caches" / "visual_rerank.jsonl",
            generate_conceptual_visuals=(
                self.real_llm and self.generate_conceptual_visuals
            ),
            generated_visual_output_dir=out_path.parent / "generated_conceptual_visuals",
            max_generated_conceptual_visuals=self.max_generated_conceptual_visuals,
        )
        payload = self._prepare_intermediate_payload({
            "stage_id": "S12_visual_planning",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **bundle,
        })
        _atomic_write_json(out_path, payload)
        summary = bundle.get("quality_summary") or {}
        return StageResult(
            output_path=out_path,
            artifact_type="report",
            notes=(
                f"promoted_visuals={summary.get('promoted_direct_visuals', 0)}  "
                f"provisional_visuals={summary.get('provisional_visuals', 0)}  "
                f"unverified_visual_sections="
                f"{len(summary.get('sections_without_verified_visual_support') or [])}  "
                f"generated_conceptual={summary.get('generated_conceptual_visual_count', 0)}"
            ),
        )

    def _run_stage_S13(self, state: FullReviewState) -> StageResult:
        from optomind_research.full_review_production import write_section_drafts

        visual_bundle = self._load_artifact_json(
            state.visual_plans_ref.path if state.visual_plans_ref else ""
        )
        kb_path = self._active_kb_path(state)
        out_path = self._stage_output_path(state, "S13_section_drafts", "section_drafts.json")
        checkpoint_dir = out_path.parent / "checkpoints"
        if not checkpoint_dir.exists() or not any(checkpoint_dir.glob("*.checkpoint.json")):
            prior_attempts = sorted(
                (
                    path for path in out_path.parent.parent.glob("attempt_*")
                    if path != out_path.parent and (path / "checkpoints").is_dir()
                ),
                key=lambda path: int(path.name.rsplit("_", 1)[-1]),
                reverse=True,
            )
            if prior_attempts:
                checkpoint_dir.mkdir(parents=True, exist_ok=True)
                for source in (prior_attempts[0] / "checkpoints").glob("*.json"):
                    shutil.copy2(source, checkpoint_dir / source.name)
        bundle = write_section_drafts(
            visual_bundle,
            kb_path=kb_path,
            real_llm=self.real_llm,
            checkpoint_dir=checkpoint_dir,
        )
        markdown_path = out_path.parent / "full_review.draft.en.md"
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(str(bundle.get("full_review_english") or ""), encoding="utf-8")
        bundle["full_review_english_path"] = str(markdown_path)
        payload = self._prepare_intermediate_payload({
            "stage_id": "S13_section_drafts",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **bundle,
        })
        _atomic_write_json(out_path, payload)
        summary = bundle.get("quality_summary") or {}
        if self.real_llm and not summary.get("draft_ready_for_audit"):
            return StageResult(
                status="failed",
                output_path=out_path,
                artifact_type="review",
                error="One or more section drafts are empty or failed.",
                stop_reason="section_draft_quality_gate_failed",
                notes=f"failed={summary.get('failed_section_ids')} empty={summary.get('empty_section_ids')}",
            )
        return StageResult(
            output_path=out_path,
            artifact_type="review",
            notes=(
                f"sections={summary.get('section_count', 0)}  "
                f"words={summary.get('english_word_count', 0)}  "
                f"uncited_load_bearing={summary.get('uncited_load_bearing_claim_count', 0)}"
            ),
        )

    def _run_stage_S14(self, state: FullReviewState) -> StageResult:
        from optomind_research.full_review_production import audit_citations

        draft_bundle = self._load_artifact_json(
            state.section_drafts_ref.path if state.section_drafts_ref else ""
        )
        audit = audit_citations(draft_bundle, real_llm=self.real_llm)
        out_path = self._stage_output_path(state, "S14_citation_audits", "citation_audits.json")
        payload = self._prepare_intermediate_payload({
            "stage_id": "S14_citation_audits",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **audit,
        })
        _atomic_write_json(out_path, payload)
        judge_failures = int(audit.get("quality_judge_failure_count") or 0)
        if self.real_llm and judge_failures:
            return StageResult(
                status="failed",
                output_path=out_path,
                artifact_type="report",
                error=(
                    f"Independent section quality judge unavailable for "
                    f"{judge_failures} section(s)."
                ),
                stop_reason="section_quality_judge_unavailable",
                notes=(
                    "This is an infrastructure failure; manuscript revision was not "
                    "started from an incomplete audit."
                ),
            )
        return StageResult(
            output_path=out_path,
            artifact_type="report",
            notes=(
                f"formal_ready_sections={audit.get('formal_ready_section_count', 0)}  "
                f"invalid_citations={audit.get('invalid_citation_count', 0)}  "
                f"uncited_load_bearing={audit.get('uncited_load_bearing_claim_count', 0)}"
            ),
        )

    def _run_stage_S15(self, state: FullReviewState) -> StageResult:
        from optomind_research.full_review_production import edit_cross_section

        out_path = self._stage_output_path(
            state, "S15_cross_section_edit", "cross_section_edit.json"
        )
        attempt_dir = out_path.parent
        snapshot_dir = attempt_dir / "m4_snapshot"
        diagnostics_dir = attempt_dir / "m4_diagnostics"
        proposal_path = attempt_dir / "m4_patch_proposal.json"
        ref_path = (
            state.cross_section_edit_ref.path
            if state.cross_section_edit_ref
            else ""
        )
        ref_dir = Path(ref_path).parent if ref_path else attempt_dir
        approvals_path = ref_dir / "m4_patch_approvals.json"
        if not proposal_path.exists() and ref_dir != attempt_dir:
            candidate = ref_dir / "m4_patch_proposal.json"
            if candidate.exists():
                proposal_path = candidate
        approvals = (
            self._load_m4_approvals(approvals_path)
            if approvals_path.is_file()
            else None
        )
        draft_bundle = self._load_artifact_json(
            state.section_drafts_ref.path if state.section_drafts_ref else ""
        )
        citation_bundle = self._load_artifact_json(
            state.citation_audits_ref.path if state.citation_audits_ref else ""
        )
        bundle = edit_cross_section(
            draft_bundle,
            citation_bundle,
            real_llm=self.real_llm,
            approvals=approvals,
            m4_snapshot_dir=snapshot_dir,
            m4_diagnostics_dir=diagnostics_dir,
            m4_proposal_path=proposal_path,
            m4_role_provider=self._m4_role_provider,
        )
        markdown_path = out_path.parent / "full_review.edited.en.md"
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(str(bundle.get("full_review_english") or ""), encoding="utf-8")
        bundle["full_review_english_path"] = str(markdown_path)
        payload = self._prepare_intermediate_payload({
            "stage_id": "S15_cross_section_edit",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **bundle,
        })
        _atomic_write_json(out_path, payload)
        summary = bundle.get("quality_summary") or {}
        m4 = bundle.get("m4_contract") or {}
        m4_status = str(m4.get("status") or bundle.get("m4_apply_status") or "noop")
        if m4_status == "awaiting_approval":
            return StageResult(
                status="needs_human",
                output_path=out_path,
                artifact_type="review",
                stop_reason="awaiting_m4_patch_approval",
                notes=(
                    f"Create {approvals_path} and decide each patch_id "
                    f"({len((m4.get('validation') or {}).get('awaiting_patches') or [])} "
                    "pending) before resuming S15."
                ),
            )
        if m4_status == "failed_qwen":
            return StageResult(
                status="failed",
                output_path=out_path,
                artifact_type="review",
                error=str((m4.get("commander") or {}).get("error") or ""),
                stop_reason="failed_qwen",
                notes=(
                    "Live Qwen patch proposer unavailable; diagnostics "
                    "recorded, original bundle retained, no deterministic "
                    "scientific decision substituted."
                ),
            )
        if m4_status == "rejected":
            rollback = m4.get("rollback_report") or {}
            post_apply_failed = (
                rollback.get("status") == "rolled_back"
            )
            stop_reason = (
                "m4_post_apply_audit_failed"
                if post_apply_failed
                else "m4_patches_rejected"
            )
            error = (
                (
                    "Post-apply audit failed; candidate rolled back to the "
                    "frozen original: "
                    + ", ".join(rollback.get("audit_failures") or [])
                )
                if post_apply_failed
                else (
                    "; ".join(
                        (m4.get("validation") or {}).get("errors") or []
                    )
                    or "M4 patch package rejected by the safety gate"
                )
            )
            return StageResult(
                status="failed",
                output_path=out_path,
                artifact_type="review",
                error=error,
                stop_reason=stop_reason,
                notes=(
                    "Patch candidate failed the post-apply audit and was "
                    "rolled back byte-for-byte to the frozen original; "
                    "original bundle retained and recoverable."
                    if post_apply_failed
                    else "Patch package failed the safety gate; original "
                    "bundle retained and recoverable."
                ),
            )
        return StageResult(
            output_path=out_path,
            artifact_type="review",
            notes=(
                f"changed_sections={summary.get('changed_section_count', 0)}  "
                f"post_edit_citations={summary.get('post_edit_citation_count', 0)}  "
                f"m4_status={m4_status}  stop={m4.get('stop_reason', '')}"
            ),
            stop_reason=str(m4.get("stop_reason") or "m4_noop"),
        )

    def _run_stage_S16(self, state: FullReviewState) -> StageResult:
        from optomind_research.full_review_production import run_supervisor_review

        edit_bundle = self._load_artifact_json(
            state.cross_section_edit_ref.path if state.cross_section_edit_ref else ""
        )
        charter = self._load_artifact_json(
            state.review_charter_ref.path if state.review_charter_ref else ""
        )
        prior = self._load_artifact_json(
            state.supervisor_review_ref.path if state.supervisor_review_ref else ""
        )
        decisions: dict[str, str] = {}
        prior_path = Path(state.supervisor_review_ref.path) if state.supervisor_review_ref else None
        decision_path = prior_path.parent / "supervisor_decisions.json" if prior_path else None
        if decision_path and decision_path.exists():
            raw = self._load_artifact_json(str(decision_path))
            if isinstance(raw.get("decisions"), list):
                decisions = {
                    str(row.get("suggestion_id")): str(row.get("decision") or "")
                    for row in raw["decisions"] if isinstance(row, dict)
                }
            else:
                decisions = {str(key): str(value) for key, value in raw.items()}

        if prior.get("suggestions") and decisions:
            bundle = copy.deepcopy(prior)
            for suggestion in bundle.get("suggestions") or []:
                decision = decisions.get(str(suggestion.get("suggestion_id") or ""), "").lower()
                if decision in {"accepted", "rejected"}:
                    suggestion["status"] = decision
                    suggestion["human_decision_by"] = "human"
                    suggestion["human_decision_at"] = utc_now()
            pending = [
                row for row in bundle.get("suggestions") or []
                if row.get("status") == "pending"
            ]
            bundle["status_summary"] = {
                "total_suggestions": len(bundle.get("suggestions") or []),
                "pending": len(pending),
                "accepted": sum(row.get("status") == "accepted" for row in bundle.get("suggestions") or []),
                "rejected": sum(row.get("status") == "rejected" for row in bundle.get("suggestions") or []),
                "critical_pending": sum(row.get("severity") == "critical" for row in pending),
                "high_pending": sum(row.get("severity") == "high" for row in pending),
                "needs_human_action": any(row.get("severity") in {"high", "critical"} for row in pending),
            }
        else:
            bundle = run_supervisor_review(
                edit_bundle,
                real_llm=self.real_llm,
                charter=charter,
            )

        out_path = self._stage_output_path(
            state, "S16_supervisor_review", "supervisor_review.json"
        )
        payload = self._prepare_intermediate_payload({
            "stage_id": "S16_supervisor_review",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **bundle,
        })
        _atomic_write_json(out_path, payload)
        summary = bundle.get("status_summary") or {}
        if self.require_human_supervisor_approval and summary.get("needs_human_action"):
            return StageResult(
                status="needs_human",
                output_path=out_path,
                artifact_type="report",
                stop_reason="awaiting_supervisor_decisions",
                notes=(
                    f"Create {out_path.parent / 'supervisor_decisions.json'} and decide each "
                    "high/critical suggestion by suggestion_id."
                ),
            )
        return StageResult(
            output_path=out_path,
            artifact_type="report",
            notes=(
                f"suggestions={summary.get('total_suggestions', 0)}  "
                f"high_pending={summary.get('high_pending', 0)}  "
                f"critical_pending={summary.get('critical_pending', 0)}"
            ),
        )

    def _run_stage_S17(self, state: FullReviewState) -> StageResult:
        from optomind_research.full_review_production import apply_feedback_revision

        edit_bundle = self._load_artifact_json(
            state.cross_section_edit_ref.path if state.cross_section_edit_ref else ""
        )
        supervisor_bundle = self._load_artifact_json(
            state.supervisor_review_ref.path if state.supervisor_review_ref else ""
        )
        bundle = apply_feedback_revision(
            edit_bundle, supervisor_bundle, real_llm=self.real_llm
        )
        out_path = self._stage_output_path(
            state, "S17_feedback_revision", "feedback_revision.json"
        )
        markdown_path = out_path.parent / "full_review.revised.en.md"
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(str(bundle.get("full_review_english") or ""), encoding="utf-8")
        bundle["full_review_english_path"] = str(markdown_path)
        payload = self._prepare_intermediate_payload({
            "stage_id": "S17_feedback_revision",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **bundle,
        })
        _atomic_write_json(out_path, payload)
        before = sum(
            row.get("status") == "accepted" and row.get("severity") in {"high", "critical"}
            for row in (supervisor_bundle.get("suggestions") or [])
        )
        after = sum(
            row.get("severity") in {"high", "critical"}
            for row in (bundle.get("unhandled_accepted_suggestions") or [])
        )
        state.add_revision(RevisionEntry(
            round_number=len(state.revision_history) + 1,
            triggered_by="S16_supervisor_review",
            high_issues_before=before,
            high_issues_after=after,
            stop_reason=str(bundle.get("stop_reason") or "revision_completed"),
        ))
        return StageResult(
            output_path=out_path,
            artifact_type="review",
            notes=(
                f"accepted_suggestions={bundle.get('accepted_suggestion_count', 0)}  "
                f"unhandled={len(bundle.get('unhandled_accepted_suggestions') or [])}  "
                f"stop={bundle.get('stop_reason', '')}"
            ),
        )

    def _run_stage_S18(self, state: FullReviewState) -> StageResult:
        from optomind_research.full_review_production import audit_citations, run_global_review

        revision = self._load_artifact_json(
            state.feedback_revision_ref.path if state.feedback_revision_ref else ""
        )
        charter = self._load_artifact_json(
            state.review_charter_ref.path if state.review_charter_ref else ""
        )
        contracts_data = self._load_artifact_json(
            state.section_contracts_ref.path if state.section_contracts_ref else ""
        )
        post_revision_audit = audit_citations(revision, real_llm=self.real_llm)
        bundle = run_global_review(
            revision,
            charter=charter,
            contracts=list(contracts_data.get("contracts") or []),
            citation_bundle=post_revision_audit,
            real_llm=self.real_llm,
        )
        bundle["post_revision_citation_audit"] = post_revision_audit
        out_path = self._stage_output_path(state, "S18_global_review", "global_review.json")
        payload = self._prepare_intermediate_payload({
            "stage_id": "S18_global_review",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **bundle,
        })
        _atomic_write_json(out_path, payload)
        judgment = bundle.get("judgment") or {}
        return StageResult(
            output_path=out_path,
            artifact_type="report",
            notes=(
                f"verdict={judgment.get('verdict', '')}  "
                f"score={judgment.get('overall_score', 0)}  "
                f"high_or_critical={bundle.get('high_or_critical_issue_count', 0)}"
            ),
        )

    def _run_stage_S19(self, state: FullReviewState) -> StageResult:
        from optomind_research.full_review_production import run_peer_review_panel

        revision = self._load_artifact_json(
            state.feedback_revision_ref.path if state.feedback_revision_ref else ""
        )
        global_review = self._load_artifact_json(
            state.global_review_ref.path if state.global_review_ref else ""
        )
        charter = self._load_artifact_json(
            state.review_charter_ref.path if state.review_charter_ref else ""
        )
        bundle = run_peer_review_panel(
            revision, global_review, charter=charter, real_llm=self.real_llm
        )
        out_path = self._stage_output_path(state, "S19_peer_reviews", "peer_reviews.json")
        payload = self._prepare_intermediate_payload({
            "stage_id": "S19_peer_reviews",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **bundle,
        })
        _atomic_write_json(out_path, payload)
        return StageResult(
            output_path=out_path,
            artifact_type="report",
            notes=(
                f"reviewers={len(bundle.get('peer_reviews') or [])}  "
                f"high_or_critical={bundle.get('high_or_critical_issue_count', 0)}"
            ),
        )

    def _run_stage_S20_revision_loop(self, state: FullReviewState) -> StageResult:
        """Run the bounded author-team revision loop before final translation."""
        from optomind_research.publication_revision_loop import (
            run_publication_revision_loop,
        )

        revision = self._load_artifact_json(
            state.feedback_revision_ref.path if state.feedback_revision_ref else ""
        )
        global_review = self._load_artifact_json(
            state.global_review_ref.path if state.global_review_ref else ""
        )
        peer_review = self._load_artifact_json(
            state.peer_reviews_ref.path if state.peer_reviews_ref else ""
        )
        supervisor = self._load_artifact_json(
            state.supervisor_review_ref.path if state.supervisor_review_ref else ""
        )
        charter = self._load_artifact_json(
            state.review_charter_ref.path if state.review_charter_ref else ""
        )
        contracts_data = self._load_artifact_json(
            state.section_contracts_ref.path if state.section_contracts_ref else ""
        )
        out_path = self._stage_output_path(
            state, "S20_revision_loop", "publication_revision_loop.json"
        )
        report = run_publication_revision_loop(
            revision,
            global_review,
            peer_review,
            supervisor_bundle=supervisor,
            charter=charter,
            contracts=list(contracts_data.get("contracts") or []),
            kb_path=self._active_kb_path(state),
            output_dir=out_path.parent / "revision_versions",
            real_llm=self.real_llm,
            enabled=self.publication_revision_loop,
            max_rounds=self.revision_max_rounds,
            max_tasks_per_round=self.revision_max_tasks_per_round,
            enable_external_oa=self.enable_external_gap_retrieval,
            max_external_rounds=self.external_gap_max_rounds,
            max_external_claims=self.external_gap_max_claims,
            generate_conceptual_visuals=self.generate_conceptual_visuals,
            max_generated_visuals=self.max_generated_conceptual_visuals,
        )
        payload = self._prepare_intermediate_payload({
            "stage_id": "S20_revision_loop",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **report,
        })
        _atomic_write_json(out_path, payload)
        decision = report.get("final_decision") or {}
        return StageResult(
            output_path=out_path,
            artifact_type="review",
            notes=(
                f"rounds={report.get('round_count', 0)}  "
                f"decision={decision.get('action', '')}:{decision.get('reason', '')}  "
                f"human_tasks={len(report.get('unresolved_human_tasks') or [])}"
            ),
        )

    def _run_stage_S20(self, state: FullReviewState) -> StageResult:
        """S20 is the only stage allowed to write Chinese content."""
        from optomind_research.full_review_production import finalize_review

        loop_report = self._load_artifact_json(
            state.revision_loop_ref.path if state.revision_loop_ref else ""
        )
        revision = loop_report.get("final_revision_bundle") or self._load_artifact_json(
            state.feedback_revision_ref.path if state.feedback_revision_ref else ""
        )
        global_review = loop_report.get("final_global_review") or self._load_artifact_json(
            state.global_review_ref.path if state.global_review_ref else ""
        )
        peer_review = loop_report.get("final_peer_reviews") or self._load_artifact_json(
            state.peer_reviews_ref.path if state.peer_reviews_ref else ""
        )
        charter = self._load_artifact_json(
            state.review_charter_ref.path if state.review_charter_ref else ""
        )
        bundle = finalize_review(
            revision,
            global_review,
            peer_review,
            charter=charter,
            kb_path=self._active_kb_path(state),
            real_llm=self.real_llm,
        )
        bundle["publication_revision_decision"] = dict(
            loop_report.get("final_decision") or {}
        )
        bundle["publication_revision_round_count"] = int(
            loop_report.get("round_count") or 0
        )
        loop_rounds = [
            row for row in (loop_report.get("rounds") or []) if isinstance(row, dict)
        ]
        inferred_rejected_rounds = [
            int(row.get("round_number") or 0)
            for row in loop_rounds if row.get("candidate_promoted") is False
        ]
        inferred_accepted_round = max(
            [
                int(row.get("round_number") or 0)
                for row in loop_rounds if row.get("candidate_promoted") is not False
            ]
            or [0]
        )
        final_citation = loop_report.get("final_citation_audit") or {}
        bundle["accepted_version_round"] = int(
            loop_report.get("accepted_version_round") or inferred_accepted_round
        )
        bundle["rejected_candidate_rounds"] = list(
            loop_report.get("rejected_candidate_rounds") or inferred_rejected_rounds
        )
        bundle["final_candidate_promoted"] = bool(
            loop_report.get(
                "final_candidate_promoted",
                loop_rounds[-1].get("candidate_promoted") is not False
                if loop_rounds else True,
            )
        )
        bundle["final_manuscript_evidence_integrity_passed"] = bool(
            loop_report.get(
                "final_manuscript_evidence_integrity_passed",
                int(final_citation.get("invalid_citation_count") or 0) == 0
                and int(final_citation.get("uncited_load_bearing_claim_count") or 0) == 0
                and int(final_citation.get("quality_judge_failure_count") or 0) == 0,
            )
        )
        bundle["unresolved_human_tasks"] = list(
            loop_report.get("unresolved_human_tasks") or []
        )
        out_path = self._stage_output_path(state, "S20_final_translation", "final_outputs.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        english_path = out_path.parent / "final_review.en.md"
        chinese_path = out_path.parent / "final_review.zh.md"
        english_path.write_text(str(bundle.pop("english_review", "")), encoding="utf-8")
        chinese_path.write_text(str(bundle.pop("chinese_review", "")), encoding="utf-8")
        bundle["english_review_path"] = str(english_path)
        bundle["chinese_review_path"] = str(chinese_path)
        _atomic_write_json(out_path, {
            "stage_id": "S20_final_translation",
            "run_id": state.run_id,
            "real_llm": self.real_llm,
            **bundle,
        })
        return StageResult(
            output_path=out_path,
            artifact_type="review",
            notes=(
                f"formal_status={bundle.get('formal_status', '')}  "
                f"references={(bundle.get('quality_summary') or {}).get('reference_count', 0)}  "
                f"translation_failures={len(bundle.get('translation_failure_section_ids') or [])}"
            ),
        )
