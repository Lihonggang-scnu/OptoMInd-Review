"""FullReviewState — unified state envelope for the complete review production pipeline.

Design rules:
- Large artifacts are NEVER embedded inline; they are written to disk as ArtifactRef.
- All text fields stored here are English (intermediate messages, titles, IDs).
- Chinese content is allowed only in: raw user_query and final_outputs artifact.
- Serialises to / loads from a single JSON file (full_review_state.json).
- save() uses atomic write (tmp → rename) to avoid partial-file corruption.
- load() performs structural schema validation; malformed files raise ValueError.
- ArtifactRegistry in this state object is the single source of truth for all artifacts.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from optomind_research.artifact_registry import ArtifactRef, ArtifactRegistry, utc_now


FULL_REVIEW_STATE_SCHEMA = "full_review_state.v1"

PIPELINE_STAGE_IDS: list[str] = [
    "S1_query_planning",
    "S2_literature_retrieval",
    "S3_kb_construction",
    "S4_concept_mapping",
    "S5_review_charter",
    "S6_mentor_advice",
    "S7_blueprint_candidates",
    "S8_blueprint_selection",
    "S9_section_contracts",
    "S10_evidence_portfolios",
    "S11_gap_resolution",
    "S12_visual_planning",
    "S13_section_drafts",
    "S14_citation_audits",
    "S15_cross_section_edit",
    "S16_supervisor_review",
    "S17_feedback_revision",
    "S18_global_review",
    "S19_peer_reviews",
    "S20_revision_loop",
    "S20_final_translation",
]

VALID_STAGE_STATUSES = frozenset({
    "pending",
    "running",
    "completed",
    "failed",
    "skipped",
    "needs_human",
})

VALID_PIPELINE_STATUSES = frozenset({"running", "completed", "failed", "needs_human"})


# ---------------------------------------------------------------------------
# Schema validation (stdlib only — no jsonschema dependency required)
# ---------------------------------------------------------------------------

def _validate_state_dict(d: dict) -> list[str]:
    """Return a list of validation errors (empty list = valid)."""
    errors: list[str] = []
    if not isinstance(d, dict):
        return ["root must be a JSON object"]
    if d.get("schema_version") != FULL_REVIEW_STATE_SCHEMA:
        errors.append(
            f"schema_version must be {FULL_REVIEW_STATE_SCHEMA!r}, "
            f"got {d.get('schema_version')!r}"
        )
    if not d.get("run_id"):
        errors.append("run_id is required and must be non-empty")
    if d.get("status") not in VALID_PIPELINE_STATUSES:
        errors.append(f"status must be one of {sorted(VALID_PIPELINE_STATUSES)}, got {d.get('status')!r}")
    stages = d.get("stages")
    if not isinstance(stages, dict):
        errors.append("stages must be a JSON object")
        stages = {}
    stage_order = d.get("stage_order")
    if not isinstance(stage_order, list):
        errors.append("stage_order must be a JSON array")
        stage_order = []
    unknown = set(stages.keys()) - set(PIPELINE_STAGE_IDS)
    if unknown:
        errors.append(f"stages contains unknown stage IDs: {sorted(unknown)}")
    unknown_order = set(stage_order) - set(PIPELINE_STAGE_IDS)
    if unknown_order:
        errors.append(f"stage_order contains unknown stage IDs: {sorted(unknown_order)}")
    if len(stage_order) != len(set(stage_order)):
        errors.append("stage_order contains duplicate stage IDs")
    for sid, record in stages.items():
        if not isinstance(record, dict):
            errors.append(f"stages[{sid!r}] must be a JSON object")
            continue
        if record.get("stage_id") != sid:
            errors.append(f"stages[{sid!r}].stage_id must equal its map key")
        if record.get("status") not in VALID_STAGE_STATUSES:
            errors.append(
                f"stages[{sid!r}].status must be one of "
                f"{sorted(VALID_STAGE_STATUSES)}, got {record.get('status')!r}"
            )
        attempt_number = record.get("attempt_number", 0)
        if not isinstance(attempt_number, int) or attempt_number < 0:
            errors.append(f"stages[{sid!r}].attempt_number must be a non-negative integer")
        for key in ("input_artifact_ids", "output_artifact_ids"):
            values = record.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                errors.append(f"stages[{sid!r}].{key} must be an array of strings")
    return errors


# ---------------------------------------------------------------------------
# StageRecord
# ---------------------------------------------------------------------------

@dataclass
class StageRecord:
    """Per-stage execution metadata — timing, model, provenance, errors, attempt history."""

    stage_id: str
    status: str = "pending"
    started_at: str = ""
    completed_at: str = ""
    model_tier: str = ""
    model_name: str = ""
    input_artifact_ids: list[str] = field(default_factory=list)
    output_artifact_ids: list[str] = field(default_factory=list)
    error: str = ""
    stop_reason: str = ""
    notes: str = ""
    # Increments on every execution of this stage; preserved across resets.
    attempt_number: int = 0
    # Full history of all attempts: [{attempt_number, artifact_id, path, sha256, created_at, active}]
    attempt_history: list[dict] = field(default_factory=list)

    def record_attempt(
        self,
        *,
        artifact_id: str,
        path: str,
        sha256: str,
        active: bool = True,
    ) -> None:
        """Append an attempt artifact and optionally make it the active result."""
        if active:
            for prev in self.attempt_history:
                prev["active"] = False
        self.attempt_history.append({
            "attempt_number": self.attempt_number,
            "artifact_id": artifact_id,
            "path": path,
            "sha256": sha256,
            "created_at": utc_now(),
            "active": active,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "model_tier": self.model_tier,
            "model_name": self.model_name,
            "input_artifact_ids": self.input_artifact_ids,
            "output_artifact_ids": self.output_artifact_ids,
            "error": self.error,
            "stop_reason": self.stop_reason,
            "notes": self.notes,
            "attempt_number": self.attempt_number,
            "attempt_history": list(self.attempt_history),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StageRecord":
        return cls(
            stage_id=str(d.get("stage_id", "")),
            status=str(d.get("status", "pending")),
            started_at=str(d.get("started_at", "")),
            completed_at=str(d.get("completed_at", "")),
            model_tier=str(d.get("model_tier", "")),
            model_name=str(d.get("model_name", "")),
            input_artifact_ids=list(d.get("input_artifact_ids") or []),
            output_artifact_ids=list(d.get("output_artifact_ids") or []),
            error=str(d.get("error", "")),
            stop_reason=str(d.get("stop_reason", "")),
            notes=str(d.get("notes", "")),
            attempt_number=int(d.get("attempt_number", 0)),
            attempt_history=list(d.get("attempt_history") or []),
        )


# ---------------------------------------------------------------------------
# RevisionEntry
# ---------------------------------------------------------------------------

@dataclass
class RevisionEntry:
    """One human-visible revision cycle (supervisor → feedback → re-audit)."""

    revision_id: str = field(default_factory=lambda: "rev-" + str(uuid.uuid4())[:8])
    round_number: int = 1
    triggered_by: str = ""
    high_issues_before: int = 0
    high_issues_after: int = 0
    stop_reason: str = ""
    created_at: str = field(default_factory=utc_now)
    artifact_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "round_number": self.round_number,
            "triggered_by": self.triggered_by,
            "high_issues_before": self.high_issues_before,
            "high_issues_after": self.high_issues_after,
            "stop_reason": self.stop_reason,
            "created_at": self.created_at,
            "artifact_ids": self.artifact_ids,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RevisionEntry":
        return cls(
            revision_id=str(d.get("revision_id", "rev-unknown")),
            round_number=int(d.get("round_number", 1)),
            triggered_by=str(d.get("triggered_by", "")),
            high_issues_before=int(d.get("high_issues_before", 0)),
            high_issues_after=int(d.get("high_issues_after", 0)),
            stop_reason=str(d.get("stop_reason", "")),
            created_at=str(d.get("created_at", utc_now())),
            artifact_ids=list(d.get("artifact_ids") or []),
        )


# ---------------------------------------------------------------------------
# FullReviewState
# ---------------------------------------------------------------------------

@dataclass
class FullReviewState:
    """Unified state envelope for the complete review production pipeline.

    Stores only IDs and refs; never embeds large artifact content.
    All intermediate string fields are English.
    Chinese is allowed only in user_query and final_outputs artifacts.
    ArtifactRegistry in this object is the single source of truth.
    """

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    pipeline_version: str = "full_review_pipeline.v1"
    domain: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    status: str = "running"

    # Raw user input — language-neutral; the only field where non-English is allowed.
    user_query: str = ""

    stages: dict[str, StageRecord] = field(default_factory=dict)

    # ---- Artifact refs (light pointers) ----
    # S1
    query_plan_ref: Optional[ArtifactRef] = None
    # S2
    corpus_ref: Optional[ArtifactRef] = None
    # S3
    knowledge_base_ref: Optional[ArtifactRef] = None
    # S4
    concept_map_ref: Optional[ArtifactRef] = None
    # S5
    review_charter_ref: Optional[ArtifactRef] = None
    # S6
    mentor_advice_ref: Optional[ArtifactRef] = None
    # S7
    blueprint_candidates_ref: Optional[ArtifactRef] = None
    # S8
    selected_blueprint_ref: Optional[ArtifactRef] = None
    # S9
    section_contracts_ref: Optional[ArtifactRef] = None
    # S10
    evidence_portfolios_ref: Optional[ArtifactRef] = None
    # S11
    gap_history_ref: Optional[ArtifactRef] = None
    # S12
    visual_plans_ref: Optional[ArtifactRef] = None
    # S13
    section_drafts_ref: Optional[ArtifactRef] = None
    # S14
    citation_audits_ref: Optional[ArtifactRef] = None
    # S15
    cross_section_edit_ref: Optional[ArtifactRef] = None
    # S16
    supervisor_review_ref: Optional[ArtifactRef] = None
    # S17
    feedback_revision_ref: Optional[ArtifactRef] = None
    # S18
    global_review_ref: Optional[ArtifactRef] = None
    # S19
    peer_reviews_ref: Optional[ArtifactRef] = None
    # S20 revision loop (the accepted English manuscript version)
    revision_loop_ref: Optional[ArtifactRef] = None
    # S20
    final_outputs_ref: Optional[ArtifactRef] = None

    revision_history: list[RevisionEntry] = field(default_factory=list)

    # Single source of truth for all artifact refs in this run.
    artifact_registry: ArtifactRegistry = field(default_factory=ArtifactRegistry)

    manifest_path: str = ""
    stage_order: list[str] = field(default_factory=lambda: list(PIPELINE_STAGE_IDS))

    # -----------------------------------------------------------------------
    # Stage helpers
    # -----------------------------------------------------------------------

    def start_stage(
        self,
        stage_id: str,
        *,
        model_tier: str = "",
        model_name: str = "",
    ) -> StageRecord:
        now = utc_now()
        rec = self.stages.setdefault(stage_id, StageRecord(stage_id=stage_id))
        rec.attempt_number += 1
        rec.status = "running"
        rec.started_at = now
        rec.completed_at = ""
        rec.error = ""
        rec.stop_reason = ""
        rec.notes = ""
        rec.model_tier = model_tier
        rec.model_name = model_name
        rec.output_artifact_ids = []   # cleared each attempt; history kept in attempt_history
        self.updated_at = now
        return rec

    def complete_stage(
        self,
        stage_id: str,
        *,
        output_artifact_ids: list[str] | None = None,
        notes: str = "",
    ) -> StageRecord:
        now = utc_now()
        rec = self.stages.setdefault(stage_id, StageRecord(stage_id=stage_id))
        rec.status = "completed"
        rec.completed_at = now
        if output_artifact_ids is not None:
            rec.output_artifact_ids = list(output_artifact_ids)
        if notes:
            rec.notes = notes
        self.updated_at = now
        return rec

    def fail_stage(
        self,
        stage_id: str,
        *,
        error: str,
        stop_reason: str = "",
        notes: str = "",
    ) -> StageRecord:
        now = utc_now()
        rec = self.stages.setdefault(stage_id, StageRecord(stage_id=stage_id))
        rec.status = "failed"
        rec.completed_at = now
        rec.error = error
        rec.stop_reason = stop_reason or "stage_error"
        rec.notes = notes
        self.updated_at = now
        return rec

    def needs_human_stage(
        self,
        stage_id: str,
        *,
        reason: str = "",
        notes: str = "",
    ) -> StageRecord:
        now = utc_now()
        rec = self.stages.setdefault(stage_id, StageRecord(stage_id=stage_id))
        rec.status = "needs_human"
        rec.completed_at = ""   # not done — paused
        rec.stop_reason = reason or "needs_human_input"
        rec.notes = notes
        self.updated_at = now
        return rec

    def skip_stage(self, stage_id: str, *, reason: str = "", notes: str = "") -> StageRecord:
        now = utc_now()
        rec = self.stages.setdefault(stage_id, StageRecord(stage_id=stage_id))
        rec.status = "skipped"
        rec.completed_at = now
        rec.stop_reason = reason
        rec.notes = notes
        self.updated_at = now
        return rec

    def stage_status(self, stage_id: str) -> str:
        return self.stages.get(stage_id, StageRecord(stage_id=stage_id)).status

    def is_stage_done(self, stage_id: str) -> bool:
        """True only for completed or skipped stages — needs_human is NOT done."""
        return self.stage_status(stage_id) in {"completed", "skipped"}

    def first_pending_stage(self) -> Optional[str]:
        for sid in self.stage_order:
            if not self.is_stage_done(sid):
                return sid
        return None

    # -----------------------------------------------------------------------
    # Artifact helpers — delegate to registry (single source of truth)
    # -----------------------------------------------------------------------

    def register_artifact(self, ref: ArtifactRef) -> ArtifactRef:
        self.artifact_registry.register(ref)
        self.updated_at = utc_now()
        return ref

    def register_file(
        self,
        path: Path,
        *,
        artifact_id: Optional[str] = None,
        artifact_type: str = "report",
        producer: str = "",
    ) -> ArtifactRef:
        ref = self.artifact_registry.register_file(
            path,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            producer=producer,
        )
        self.updated_at = utc_now()
        return ref

    # -----------------------------------------------------------------------
    # Revision helpers
    # -----------------------------------------------------------------------

    def add_revision(self, entry: RevisionEntry) -> None:
        self.revision_history.append(entry)
        self.updated_at = utc_now()

    # -----------------------------------------------------------------------
    # Serialisation
    # -----------------------------------------------------------------------

    _REF_ATTRS: tuple[str, ...] = (
        "query_plan_ref",
        "corpus_ref",
        "knowledge_base_ref",
        "concept_map_ref",
        "review_charter_ref",
        "mentor_advice_ref",
        "blueprint_candidates_ref",
        "selected_blueprint_ref",
        "section_contracts_ref",
        "evidence_portfolios_ref",
        "gap_history_ref",
        "visual_plans_ref",
        "section_drafts_ref",
        "citation_audits_ref",
        "cross_section_edit_ref",
        "supervisor_review_ref",
        "feedback_revision_ref",
        "global_review_ref",
        "peer_reviews_ref",
        "revision_loop_ref",
        "final_outputs_ref",
    )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": FULL_REVIEW_STATE_SCHEMA,
            "run_id": self.run_id,
            "pipeline_version": self.pipeline_version,
            "domain": self.domain,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "user_query": self.user_query,
            "manifest_path": self.manifest_path,
            "stage_order": self.stage_order,
            "stages": {sid: rec.to_dict() for sid, rec in self.stages.items()},
            "revision_history": [r.to_dict() for r in self.revision_history],
            "artifact_registry": self.artifact_registry.to_dict(),
        }
        for attr in self._REF_ATTRS:
            ref = getattr(self, attr)
            d[attr] = ref.to_dict() if ref else None
        return d

    def save(self, path: Path) -> None:
        """Atomic write: serialise to a temp file then rename to avoid partial writes."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        # Write to a sibling temp file, then atomic rename
        tmp_fd, tmp_str = tempfile.mkstemp(
            dir=str(path.parent), prefix=".frs_tmp_", suffix=".json"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp_str, str(path))
        except Exception:
            try:
                os.unlink(tmp_str)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path) -> "FullReviewState":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        errors = _validate_state_dict(d)
        if errors:
            raise ValueError(
                f"FullReviewState schema validation failed loading {path}:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )
        stage_order = list(d.get("stage_order") or PIPELINE_STAGE_IDS)
        # Backward-compatible migration for runs created before the publication
        # revision loop was inserted.  Old immutable artifacts remain valid;
        # the new stage is simply scheduled immediately before final delivery.
        if "S20_revision_loop" not in stage_order:
            try:
                stage_order.insert(stage_order.index("S20_final_translation"), "S20_revision_loop")
            except ValueError:
                stage_order.append("S20_revision_loop")
        state = cls(
            run_id=d["run_id"],
            pipeline_version=d.get("pipeline_version", "full_review_pipeline.v1"),
            domain=d.get("domain", ""),
            status=d.get("status", "running"),
            created_at=d.get("created_at", utc_now()),
            updated_at=d.get("updated_at", utc_now()),
            user_query=d.get("user_query", ""),
            manifest_path=d.get("manifest_path", ""),
            stage_order=stage_order,
        )
        for sid, sd in (d.get("stages") or {}).items():
            state.stages[sid] = StageRecord.from_dict(sd)
        for sid in stage_order:
            state.stages.setdefault(sid, StageRecord(stage_id=sid, status="pending"))
        for rev in (d.get("revision_history") or []):
            state.revision_history.append(RevisionEntry.from_dict(rev))
        state.artifact_registry = ArtifactRegistry.from_dict(d.get("artifact_registry") or {})
        for attr in cls._REF_ATTRS:
            ref_dict = d.get(attr)
            if isinstance(ref_dict, dict):
                setattr(state, attr, ArtifactRef.from_dict(ref_dict))
        return state

    @classmethod
    def new(cls, *, user_query: str, domain: str = "") -> "FullReviewState":
        """Create a fresh state with all stages pending."""
        state = cls(user_query=user_query, domain=domain)
        for sid in PIPELINE_STAGE_IDS:
            state.stages[sid] = StageRecord(stage_id=sid, status="pending")
        return state
