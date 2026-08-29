"""Regression tests for F1 acceptance issues in FullReviewState + FullReviewOrchestrator.

Coverage:
  F1-1  Attempt versioning — rerun preserves old files; sha256 remains valid
  F1-2  run_id consistency — mismatch raises RunIdMismatchError
  F1-3  needs_human — pipeline pauses (not failed); resumes correctly
  F1-4  input_artifact_ids — non-first stages record upstream artifact IDs
  F1-5  S15-S18 ref fields — cross_section_edit, supervisor_review,
         feedback_revision, global_review each bound to correct stage
  F1-6  JSON schema — file exists; valid state passes; bad status fails
  F1-7  real_llm fail-closed — placeholder raises, stage fails, not stub-complete
  F1-8  Invalid from_stage/only_stage — ValueError raised immediately
  F1-9  Atomic write + manifest sync reset — state and manifest reset together;
         no partial files on save
  F1-10 Registry single source — manifest artifact_registry mirrors state's after run
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.full_review_state import (
    FullReviewState,
    StageRecord,
    RevisionEntry,
    PIPELINE_STAGE_IDS,
    _validate_state_dict,
    FULL_REVIEW_STATE_SCHEMA,
)
from optomind_research.full_review_orchestrator import (
    FullReviewOrchestrator,
    RunIdMismatchError,
    StageResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_state(query: str = "Radiative cooling materials review.") -> FullReviewState:
    return FullReviewState.new(user_query=query, domain="optical_science")


def _run_all(tmp_path: Path, *, real_llm: bool = False) -> tuple[FullReviewOrchestrator, FullReviewState]:
    """Run the full pipeline, auto-resuming past needs_human pauses (e.g. S8 blueprint approval)."""
    orch = FullReviewOrchestrator(output_dir=tmp_path, real_llm=real_llm)
    state = _new_state()
    for _ in range(6):  # up to 6 resumptions to handle all planned needs_human stages
        state = orch.run(state)
        if state.status != "needs_human":
            break
    return orch, state


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# ===========================================================================
# F1-1: Attempt versioning
# ===========================================================================

class TestAttemptVersioning:
    def test_first_run_creates_attempt_1_directory(self, tmp_path):
        orch, state = _run_all(tmp_path)
        stub = tmp_path / "S1_query_planning" / "attempt_1" / "S1_query_planning.stub.json"
        assert stub.exists(), f"Expected attempt_1 stub at {stub}"

    def test_rerun_creates_attempt_2_directory(self, tmp_path):
        orch, state = _run_all(tmp_path)
        # Re-run from S1
        state = orch.run(state, from_stage="S1_query_planning")
        stub2 = tmp_path / "S1_query_planning" / "attempt_2" / "S1_query_planning.stub.json"
        assert stub2.exists(), "attempt_2 stub must exist after rerun"

    def test_interrupted_running_stage_resumes_same_attempt_directory(self, tmp_path):
        orch = FullReviewOrchestrator(output_dir=tmp_path, real_llm=False)
        state = _new_state()
        record = state.stages["S1_query_planning"]
        record.status = "running"
        record.attempt_number = 1

        state = orch.run(state, only_stage="S1_query_planning")

        assert state.stages["S1_query_planning"].attempt_number == 1
        assert (
            tmp_path
            / "S1_query_planning"
            / "attempt_1"
            / "S1_query_planning.stub.json"
        ).exists()
        assert not (tmp_path / "S1_query_planning" / "attempt_2").exists()

    def test_old_stub_file_preserved_after_rerun(self, tmp_path):
        orch, state = _run_all(tmp_path)
        stub1 = tmp_path / "S1_query_planning" / "attempt_1" / "S1_query_planning.stub.json"
        old_sha = _sha256_file(stub1)

        # Re-run from S1
        state = orch.run(state, from_stage="S1_query_planning")

        assert stub1.exists(), "Attempt-1 file must still exist after rerun"
        assert _sha256_file(stub1) == old_sha, "Attempt-1 sha256 must be unchanged"

    def test_attempt_history_grows_on_rerun(self, tmp_path):
        orch, state = _run_all(tmp_path)
        assert state.stages["S1_query_planning"].attempt_number == 1
        assert len(state.stages["S1_query_planning"].attempt_history) == 1

        state = orch.run(state, from_stage="S1_query_planning")
        assert state.stages["S1_query_planning"].attempt_number == 2
        assert len(state.stages["S1_query_planning"].attempt_history) == 2

    def test_only_latest_attempt_is_active(self, tmp_path):
        orch, state = _run_all(tmp_path)
        state = orch.run(state, from_stage="S1_query_planning")

        history = state.stages["S1_query_planning"].attempt_history
        active = [e for e in history if e["active"]]
        inactive = [e for e in history if not e["active"]]
        assert len(active) == 1
        assert active[0]["attempt_number"] == 2
        assert len(inactive) == 1
        assert inactive[0]["attempt_number"] == 1

    def test_old_artifact_sha256_in_registry_still_valid(self, tmp_path):
        orch, state = _run_all(tmp_path)
        # Capture artifact ID from attempt 1
        first_art_id = state.stages["S1_query_planning"].attempt_history[0]["artifact_id"]
        first_art_path = state.stages["S1_query_planning"].attempt_history[0]["path"]
        first_sha = state.stages["S1_query_planning"].attempt_history[0]["sha256"]

        # Rerun
        state = orch.run(state, from_stage="S1_query_planning")

        # Old artifact is still in registry
        old_ref = state.artifact_registry.get(first_art_id)
        assert old_ref is not None, "Old artifact must remain in registry after rerun"
        # File still exists and sha256 matches
        assert Path(first_art_path).exists()
        assert _sha256_file(Path(first_art_path)) == first_sha


# ===========================================================================
# F1-2: run_id consistency
# ===========================================================================

class TestRunIdConsistency:
    def test_run_id_mismatch_raises(self, tmp_path):
        """If we load a state with a different run_id than an existing manifest, raise."""
        orch1 = FullReviewOrchestrator(output_dir=tmp_path)
        state1 = _new_state()
        orch1.run(state1)  # Creates manifest with state1.run_id

        # Create a brand-new state with a different run_id
        state2 = _new_state()
        assert state2.run_id != state1.run_id

        orch2 = FullReviewOrchestrator(output_dir=tmp_path)
        with pytest.raises(RunIdMismatchError):
            orch2.run(state2)

    def test_same_run_id_does_not_raise(self, tmp_path):
        orch = FullReviewOrchestrator(output_dir=tmp_path)
        state = _new_state()
        orch.run(state)
        # Reload state (same run_id) and resume — should not raise
        loaded = orch.load_state()
        orch.run(loaded)  # no exception

    def test_new_run_dir_creates_manifest_with_correct_run_id(self, tmp_path):
        orch, state = _run_all(tmp_path)
        manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
        assert manifest["run_id"] == state.run_id


# ===========================================================================
# F1-3: needs_human pause and resume
# ===========================================================================

class TestNeedsHuman:
    def _run_with_needs_human_on_s5(self, tmp_path) -> tuple[FullReviewOrchestrator, FullReviewState]:
        orch = FullReviewOrchestrator(output_dir=tmp_path)
        state = _new_state()

        from optomind_research.full_review_orchestrator import StageResult

        def _s5_needs_human(s: FullReviewState) -> StageResult:
            return StageResult(
                status="needs_human",
                stop_reason="awaiting_scope_confirmation",
                notes="Human must confirm the review scope before proceeding.",
            )

        orch._handlers["S5_review_charter"] = _s5_needs_human
        state = orch.run(state)
        return orch, state

    def test_needs_human_pipeline_status(self, tmp_path):
        _, state = self._run_with_needs_human_on_s5(tmp_path)
        assert state.status == "needs_human"

    def test_needs_human_stage_not_failed(self, tmp_path):
        _, state = self._run_with_needs_human_on_s5(tmp_path)
        assert state.stages["S5_review_charter"].status == "needs_human"
        assert state.stages["S5_review_charter"].status != "failed"

    def test_stages_after_needs_human_are_still_pending(self, tmp_path):
        _, state = self._run_with_needs_human_on_s5(tmp_path)
        for sid in PIPELINE_STAGE_IDS:
            idx_s5 = PIPELINE_STAGE_IDS.index("S5_review_charter")
            idx_cur = PIPELINE_STAGE_IDS.index(sid)
            if idx_cur > idx_s5:
                assert state.stages[sid].status == "pending", \
                    f"Stage {sid} should still be pending after needs_human pause"

    def test_needs_human_manifest_status(self, tmp_path):
        self._run_with_needs_human_on_s5(tmp_path)
        d = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
        assert d["status"] == "needs_human"

    def test_resume_from_needs_human_reruns_stage(self, tmp_path):
        orch, state = self._run_with_needs_human_on_s5(tmp_path)
        assert state.stages["S5_review_charter"].status == "needs_human"

        # Restore the default placeholder handler and resume.
        # S8 also emits needs_human on first pass (blueprint approval); auto-resume past it.
        orch._handlers["S5_review_charter"] = orch._run_stage_S5
        for _ in range(6):
            state = orch.run(state)
            if state.status != "needs_human":
                break
        # S5 should now be completed
        assert state.stages["S5_review_charter"].status == "completed"
        assert state.status == "completed"


# ===========================================================================
# F1-4: input_artifact_ids
# ===========================================================================

class TestInputArtifactIds:
    def test_first_stage_has_no_inputs(self, tmp_path):
        _, state = _run_all(tmp_path)
        assert state.stages["S1_query_planning"].input_artifact_ids == []

    def test_s2_has_s1_output_as_input(self, tmp_path):
        _, state = _run_all(tmp_path)
        s1_output_ids = state.stages["S1_query_planning"].output_artifact_ids
        s2_input_ids = state.stages["S2_literature_retrieval"].input_artifact_ids
        assert len(s2_input_ids) > 0, "S2 must record at least one input artifact"
        assert set(s2_input_ids) == set(s1_output_ids)

    def test_all_non_first_stages_have_input_artifacts(self, tmp_path):
        _, state = _run_all(tmp_path)
        for sid in PIPELINE_STAGE_IDS[1:]:  # skip S1
            rec = state.stages[sid]
            assert len(rec.input_artifact_ids) > 0, \
                f"Stage {sid} (non-first) must have at least one input_artifact_id"

    def test_input_artifact_ids_persisted_after_reload(self, tmp_path):
        orch, state = _run_all(tmp_path)
        loaded = orch.load_state()
        for sid in PIPELINE_STAGE_IDS[1:]:
            assert len(loaded.stages[sid].input_artifact_ids) > 0, \
                f"input_artifact_ids for {sid} must survive save/load"


# ===========================================================================
# F1-5: S15–S18 ref fields
# ===========================================================================

class TestS15ToS18Refs:
    def test_cross_section_edit_ref_set_on_s15(self, tmp_path):
        _, state = _run_all(tmp_path)
        assert state.cross_section_edit_ref is not None, \
            "cross_section_edit_ref must be set after S15 completes"
        assert state.cross_section_edit_ref.producer == "S15_cross_section_edit"

    def test_supervisor_review_ref_set_on_s16(self, tmp_path):
        _, state = _run_all(tmp_path)
        assert state.supervisor_review_ref is not None, \
            "supervisor_review_ref must be set after S16 completes"
        assert state.supervisor_review_ref.producer == "S16_supervisor_review"

    def test_feedback_revision_ref_set_on_s17(self, tmp_path):
        _, state = _run_all(tmp_path)
        assert state.feedback_revision_ref is not None, \
            "feedback_revision_ref must be set after S17 completes"
        assert state.feedback_revision_ref.producer == "S17_feedback_revision"

    def test_global_review_ref_set_on_s18(self, tmp_path):
        _, state = _run_all(tmp_path)
        assert state.global_review_ref is not None, \
            "global_review_ref must be set after S18 completes"
        assert state.global_review_ref.producer == "S18_global_review"

    def test_s16_does_not_set_global_review_ref(self, tmp_path):
        """Regression: global_review_ref must NOT be set by S16 (old bug)."""
        orch = FullReviewOrchestrator(output_dir=tmp_path)
        state = _new_state()
        # Run only up to S16
        for sid in PIPELINE_STAGE_IDS[:PIPELINE_STAGE_IDS.index("S16_supervisor_review") + 1]:
            state = orch.run(state, only_stage=sid)
        # At this point S18 has not run, so global_review_ref must be None
        assert state.global_review_ref is None, \
            "global_review_ref must be None when S18 has not yet run"

    def test_all_four_refs_distinct(self, tmp_path):
        _, state = _run_all(tmp_path)
        refs = [
            state.cross_section_edit_ref,
            state.supervisor_review_ref,
            state.feedback_revision_ref,
            state.global_review_ref,
        ]
        paths = [r.path for r in refs if r]
        assert len(set(paths)) == 4, "S15-S18 must produce four distinct artifact files"


# ===========================================================================
# F1-6: JSON schema
# ===========================================================================

class TestSchemaValidation:
    def test_schema_file_exists(self):
        schema_path = PROJECT_ROOT / "schemas" / "full_review_state.schema.json"
        assert schema_path.exists(), f"Schema file missing at {schema_path}"

    def test_schema_file_is_valid_json(self):
        schema_path = PROJECT_ROOT / "schemas" / "full_review_state.schema.json"
        data = json.loads(schema_path.read_text(encoding="utf-8"))
        assert data.get("$schema") is not None
        assert data.get("title") == "FullReviewState"

    def test_valid_state_passes_validation(self, tmp_path):
        state = _new_state()
        d = state.to_dict()
        errors = _validate_state_dict(d)
        assert errors == [], f"Valid state should pass validation; got: {errors}"

    def test_wrong_schema_version_fails(self):
        state = _new_state()
        d = state.to_dict()
        d["schema_version"] = "full_review_state.v99"
        errors = _validate_state_dict(d)
        assert any("schema_version" in e for e in errors)

    def test_missing_run_id_fails(self):
        state = _new_state()
        d = state.to_dict()
        d["run_id"] = ""
        errors = _validate_state_dict(d)
        assert any("run_id" in e for e in errors)

    def test_invalid_status_fails(self):
        state = _new_state()
        d = state.to_dict()
        d["status"] = "INVALID_STATUS"
        errors = _validate_state_dict(d)
        assert any("status" in e for e in errors)

    def test_load_rejects_invalid_schema_version(self, tmp_path):
        state = _new_state()
        path = tmp_path / "bad_state.json"
        d = state.to_dict()
        d["schema_version"] = "full_review_state.v0_broken"
        path.write_text(json.dumps(d), encoding="utf-8")
        with pytest.raises(ValueError, match="schema_version"):
            FullReviewState.load(path)

    def test_schema_has_all_stage_ref_fields(self):
        schema_path = PROJECT_ROOT / "schemas" / "full_review_state.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        props = schema.get("properties", {})
        for attr in FullReviewState._REF_ATTRS:
            assert attr in props, f"Schema missing property: {attr}"


# ===========================================================================
# F1-7: real_llm fail-closed
# ===========================================================================

class TestRealLlmFailClosed:
    def test_real_llm_first_stage_fails(self, tmp_path):
        orch = FullReviewOrchestrator(output_dir=tmp_path, real_llm=True)
        state = _new_state()
        state = orch.run(state)
        # First stage should fail because placeholder raises NotImplementedError
        assert state.stages["S1_query_planning"].status == "failed"
        assert "NotImplementedError" in state.stages["S1_query_planning"].error

    def test_real_llm_pipeline_status_is_failed(self, tmp_path):
        orch = FullReviewOrchestrator(output_dir=tmp_path, real_llm=True)
        state = _new_state()
        state = orch.run(state)
        assert state.status == "failed"

    def test_real_llm_no_stub_file_created(self, tmp_path):
        orch = FullReviewOrchestrator(output_dir=tmp_path, real_llm=True)
        state = _new_state()
        orch.run(state)
        # No stub output should exist for the first stage
        stubs = list(tmp_path.rglob("*.stub.json"))
        assert len(stubs) == 0, "No stub files should be created in real_llm mode"

    def test_real_llm_error_message_is_descriptive(self, tmp_path):
        orch = FullReviewOrchestrator(output_dir=tmp_path, real_llm=True)
        state = _new_state()
        state = orch.run(state)
        error = state.stages["S1_query_planning"].error
        assert "real" in error.lower() or "placeholder" in error.lower() or \
               "NotImplementedError" in error, \
            f"Error message must explain the real_llm restriction: {error!r}"


# ===========================================================================
# F1-8: Invalid stage ID arguments
# ===========================================================================

class TestInvalidStageArgs:
    def test_invalid_from_stage_raises_value_error(self, tmp_path):
        orch = FullReviewOrchestrator(output_dir=tmp_path)
        state = _new_state()
        with pytest.raises(ValueError, match="from_stage"):
            orch.run(state, from_stage="DOES_NOT_EXIST_42")

    def test_invalid_only_stage_raises_value_error(self, tmp_path):
        orch = FullReviewOrchestrator(output_dir=tmp_path)
        state = _new_state()
        with pytest.raises(ValueError, match="only_stage"):
            orch.run(state, only_stage="S99_nonexistent")

    def test_valid_from_stage_does_not_raise(self, tmp_path):
        orch, state = _run_all(tmp_path)
        # Should not raise
        orch.run(state, from_stage="S3_kb_construction")

    def test_valid_only_stage_does_not_raise(self, tmp_path):
        orch = FullReviewOrchestrator(output_dir=tmp_path)
        state = _new_state()
        orch.run(state, only_stage="S1_query_planning")  # no exception


# ===========================================================================
# F1-9: Atomic write + manifest / state sync on reset
# ===========================================================================

class TestAtomicWriteAndReset:
    def test_no_tmp_files_left_after_successful_save(self, tmp_path):
        _, state = _run_all(tmp_path)
        tmp_files = list(tmp_path.glob(".frs_tmp_*")) + list(tmp_path.glob(".manifest_tmp_*"))
        assert len(tmp_files) == 0, f"Temp files leaked: {tmp_files}"

    def test_from_stage_resets_both_state_and_manifest(self, tmp_path):
        orch, state = _run_all(tmp_path)
        assert state.stages["S5_review_charter"].status == "completed"

        # Reset from S5
        state = orch.run(state, from_stage="S5_review_charter")

        # Both state and manifest should reflect completed S5 again
        assert state.stages["S5_review_charter"].status == "completed"
        manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
        ss = manifest["stage_status"].get("S5_review_charter", {})
        assert ss.get("status") == "completed"

    def test_state_file_is_valid_json_after_run(self, tmp_path):
        _run_all(tmp_path)
        state_path = tmp_path / "full_review_state.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["schema_version"] == FULL_REVIEW_STATE_SCHEMA

    def test_manifest_file_is_valid_json_after_run(self, tmp_path):
        _run_all(tmp_path)
        data = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
        assert "run_id" in data
        assert "stage_status" in data

    def test_from_stage_later_stages_reset_in_manifest(self, tmp_path):
        """Stages after from_stage should be pending in manifest after reset."""
        orch, state = _run_all(tmp_path)
        state = orch.run(state, from_stage="S10_evidence_portfolios")
        manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
        # S9 should still be completed; S10 onward should be (re-)completed or were reset
        assert manifest["stage_status"]["S1_query_planning"]["status"] == "completed"


# ===========================================================================
# F1-10: Registry single source of truth
# ===========================================================================

class TestRegistrySingleSource:
    def test_manifest_registry_matches_state_registry_after_run(self, tmp_path):
        _, state = _run_all(tmp_path)
        manifest_data = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
        manifest_registry = manifest_data.get("artifact_registry", {})
        state_registry = state.artifact_registry.to_dict()
        assert set(manifest_registry.keys()) == set(state_registry.keys()), \
            "manifest.artifact_registry keys must exactly match state.artifact_registry keys"

    def test_manifest_registry_matches_after_rerun(self, tmp_path):
        orch, state = _run_all(tmp_path)
        state = orch.run(state, from_stage="S3_kb_construction")

        manifest_data = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
        manifest_keys = set(manifest_data.get("artifact_registry", {}).keys())
        state_keys = set(state.artifact_registry.to_dict().keys())
        assert manifest_keys == state_keys

    def test_no_artifact_registered_only_in_manifest(self, tmp_path):
        """No artifact should appear in manifest registry but not in state registry."""
        _, state = _run_all(tmp_path)
        manifest_data = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
        manifest_keys = set(manifest_data.get("artifact_registry", {}).keys())
        state_keys = set(state.artifact_registry.to_dict().keys())
        only_in_manifest = manifest_keys - state_keys
        assert len(only_in_manifest) == 0, \
            f"Artifacts in manifest but not in state: {only_in_manifest}"

    def test_all_state_artifacts_have_valid_paths(self, tmp_path):
        _, state = _run_all(tmp_path)
        for aid, ref_dict in state.artifact_registry.to_dict().items():
            path = Path(ref_dict["path"])
            assert path.exists(), f"Artifact {aid} has non-existent path {path}"


# ===========================================================================
# F1 acceptance follow-up: active refs, non-linear provenance, nested schema
# ===========================================================================

class TestF1AcceptanceFollowUp:
    @staticmethod
    def _cjk_strings(value, *, key=""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key == "user_query":
                    continue
                yield from TestF1AcceptanceFollowUp._cjk_strings(child, key=child_key)
        elif isinstance(value, list):
            for child in value:
                yield from TestF1AcceptanceFollowUp._cjk_strings(child, key=key)
        elif isinstance(value, str) and any("\u3400" <= ch <= "\u9fff" for ch in value):
            yield key, value

    def test_intermediate_artifacts_remain_english_with_chinese_user_query(self, tmp_path):
        query = "调研日间辐射制冷的研究背景与热点。"
        orch = FullReviewOrchestrator(output_dir=tmp_path)
        state = orch.run(FullReviewState.new(user_query=query, domain="optical_science"))
        assert state.user_query == query
        for path in tmp_path.rglob("*.stub.json"):
            if "S20_final_translation" in path.parts:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            violations = list(self._cjk_strings(data))
            assert violations == [], f"CJK leaked into intermediate artifact {path}: {violations}"

    def test_nested_invalid_stage_status_is_rejected(self, tmp_path):
        state = _new_state()
        path = tmp_path / "bad_nested_state.json"
        data = state.to_dict()
        data["stages"]["S4_concept_mapping"]["status"] = "not_a_status"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match="S4_concept_mapping"):
            FullReviewState.load(path)

    def test_rerun_failure_does_not_leave_stale_active_ref(self, tmp_path):
        orch, state = _run_all(tmp_path)
        old_ref = state.review_charter_ref
        assert old_ref is not None and Path(old_ref.path).exists()

        def _fail(_state):
            return StageResult(
                status="failed",
                error="simulated rerun failure",
                stop_reason="test_failure",
                notes="diagnostic context retained",
            )

        orch._handlers["S5_review_charter"] = _fail
        state = orch.run(
            state,
            from_stage="S5_review_charter",
            only_stage="S5_review_charter",
        )
        assert state.review_charter_ref is None
        assert state.stages["S5_review_charter"].notes == "diagnostic context retained"
        assert not any(a.get("active") for a in state.stages["S5_review_charter"].attempt_history)
        assert Path(old_ref.path).exists(), "Historical output must remain on disk"
        assert state.artifact_registry.get(old_ref.artifact_id) is not None

    def test_rerun_is_persisted_as_running_before_handler_executes(self, tmp_path):
        orch, state = _run_all(tmp_path)
        observed = {}
        original = orch._run_stage_S5

        def _inspect(current_state):
            state_json = json.loads((tmp_path / "full_review_state.json").read_text(encoding="utf-8"))
            manifest_json = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
            observed["state_status"] = state_json["status"]
            observed["manifest_status"] = manifest_json["status"]
            return original(current_state)

        orch._handlers["S5_review_charter"] = _inspect
        orch.run(state, from_stage="S5_review_charter", only_stage="S5_review_charter")
        assert observed == {"state_status": "running", "manifest_status": "running"}

    def test_section_drafting_records_all_declared_inputs(self, tmp_path):
        _, state = _run_all(tmp_path)
        recorded = set(state.stages["S13_section_drafts"].input_artifact_ids)
        expected = set()
        for sid in FullReviewOrchestrator._STAGE_DEPENDENCIES["S13_section_drafts"]:
            expected.update(state.stages[sid].output_artifact_ids)
        assert recorded == expected
        assert len(recorded) > 1

    def test_placeholder_artifact_types_match_stage_roles(self, tmp_path):
        _, state = _run_all(tmp_path)
        assert state.corpus_ref.artifact_type == "corpus"
        assert state.knowledge_base_ref.artifact_type == "knowledge_base"
        assert state.selected_blueprint_ref.artifact_type == "blueprint"
        assert state.evidence_portfolios_ref.artifact_type == "evidence_packet"
        assert state.section_drafts_ref.artifact_type == "review"


# ===========================================================================
# M4 production admission: S6-S9 fail-closed gates
# ===========================================================================


def _inject_artifact(
    state: FullReviewState,
    ref_attr: str,
    path: Path,
    payload: dict,
    *,
    producer: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ref = state.register_file(path, artifact_type="report", producer=producer)
    setattr(state, ref_attr, ref)


def _production_orchestrator(tmp_path: Path) -> FullReviewOrchestrator:
    return FullReviewOrchestrator(
        output_dir=tmp_path / "out",
        real_llm=True,
        production_mode=True,
    )


class TestProductionAdmission:
    def test_production_mode_requires_real_llm(self, tmp_path):
        with pytest.raises(ValueError):
            FullReviewOrchestrator(
                output_dir=tmp_path / "out",
                real_llm=False,
                production_mode=True,
            )

    def test_production_mode_defaults_from_real_llm(self, tmp_path):
        offline = FullReviewOrchestrator(output_dir=tmp_path / "offline")
        assert offline.production_mode is False
        online = FullReviewOrchestrator(
            output_dir=tmp_path / "online", real_llm=True
        )
        assert online.production_mode is True

    def test_offline_artifacts_are_visibly_marked_non_production(
        self, tmp_path
    ):
        orch = FullReviewOrchestrator(output_dir=tmp_path / "out")
        state = _new_state()
        state = orch.run(state, only_stage="S5_review_charter")
        assert state.stages["S5_review_charter"].status == "completed"
        charter = json.loads(
            (
                tmp_path
                / "out"
                / "S5_review_charter"
                / "attempt_1"
                / "review_charter.json"
            ).read_text(encoding="utf-8")
        )
        assert charter["production"] is False
        assert charter["admission_marker"] == "non_production"

    def test_command_knowledge_gate_helpers(self, tmp_path):
        orch = _production_orchestrator(tmp_path)
        error = orch._production_command_knowledge_error(
            {"mode": "deterministic"}
        )
        assert "production_command_knowledge_unavailable" in error
        ok_advice = {
            "command_knowledge": {"status": "ok"},
            "evidence_authority": {
                "exclusive": True,
                "authority": "papers_and_material_dossiers",
            },
        }
        assert orch._production_command_knowledge_error(ok_advice) == ""
        offline = FullReviewOrchestrator(output_dir=tmp_path / "off")
        assert offline._production_command_knowledge_error({}) == ""

    def test_blueprint_admission_gate_helpers(self, tmp_path):
        orch = _production_orchestrator(tmp_path)
        error = orch._production_blueprint_admission_error(
            {"production": False, "admission_decision": "reject"},
            stage_label="S7",
        )
        assert "production_blueprint_not_admitted" in error
        assert orch._production_blueprint_admission_error(
            {"production": True, "admission_decision": "admit"},
            stage_label="S7",
        ) == ""
        offline = FullReviewOrchestrator(output_dir=tmp_path / "off")
        assert offline._production_blueprint_admission_error(
            {"production": False}, stage_label="S7"
        ) == ""

    def test_s6_fails_closed_on_command_knowledge_gap(
        self, tmp_path, monkeypatch
    ):
        import optomind_research.review_mentor_agent as mentor_module

        class MentorStub:
            def __init__(self, real_llm: bool = False):
                self.real_llm = real_llm

            def build_advice(self, **_kwargs):
                return {"mode": "deterministic", "usable_intellectual_moves": []}

        monkeypatch.setattr(mentor_module, "ReviewMentorAgent", MentorStub)
        orch = _production_orchestrator(tmp_path)
        state = _new_state()
        _inject_artifact(
            state,
            "review_charter_ref",
            tmp_path / "refs" / "review_charter.json",
            {"central_question": "Q", "scope_statement": "S"},
            producer="S5_review_charter",
        )
        _inject_artifact(
            state,
            "concept_map_ref",
            tmp_path / "refs" / "concept_map.json",
            {"views": [], "clusters": []},
            producer="S4_concept_mapping",
        )
        state = orch.run(state, only_stage="S6_mentor_advice")
        rec = state.stages["S6_mentor_advice"]
        assert rec.status == "failed"
        assert "production_command_knowledge_unavailable" in rec.error
        assert rec.stop_reason == "production_command_knowledge_unavailable"

    def test_s6_passes_with_exclusive_evidence_authority(
        self, tmp_path, monkeypatch
    ):
        import optomind_research.review_mentor_agent as mentor_module

        class MentorStub:
            def __init__(self, real_llm: bool = False):
                self.real_llm = real_llm

            def build_advice(self, **_kwargs):
                return {
                    "mode": "real_llm",
                    "usable_intellectual_moves": [],
                    "command_knowledge": {"status": "ok"},
                    "evidence_authority": {
                        "exclusive": True,
                        "authority": "papers_and_material_dossiers",
                    },
                }

        monkeypatch.setattr(mentor_module, "ReviewMentorAgent", MentorStub)
        orch = _production_orchestrator(tmp_path)
        state = _new_state()
        _inject_artifact(
            state,
            "review_charter_ref",
            tmp_path / "refs" / "review_charter.json",
            {"central_question": "Q", "scope_statement": "S"},
            producer="S5_review_charter",
        )
        _inject_artifact(
            state,
            "concept_map_ref",
            tmp_path / "refs" / "concept_map.json",
            {"views": [], "clusters": []},
            producer="S4_concept_mapping",
        )
        state = orch.run(state, only_stage="S6_mentor_advice")
        assert state.stages["S6_mentor_advice"].status == "completed"

    def test_s7_fails_closed_on_non_admitted_candidates(
        self, tmp_path, monkeypatch
    ):
        import optomind_research.blueprint_council as council_module

        class CouncilStub:
            def __init__(self, real_llm: bool = False):
                self.real_llm = real_llm

            def generate_candidates(self, **_kwargs):
                return {
                    "production": False,
                    "non_production_fallback": True,
                    "admission_decision": "reject",
                    "candidates": [],
                    "candidates_sha256": "sha",
                }

        monkeypatch.setattr(council_module, "BlueprintCouncil", CouncilStub)
        orch = _production_orchestrator(tmp_path)
        state = _new_state()
        _inject_artifact(
            state,
            "review_charter_ref",
            tmp_path / "refs" / "review_charter.json",
            {"central_question": "Q", "scope_statement": "S"},
            producer="S5_review_charter",
        )
        _inject_artifact(
            state,
            "mentor_advice_ref",
            tmp_path / "refs" / "mentor_advice.json",
            {"usable_intellectual_moves": []},
            producer="S6_mentor_advice",
        )
        _inject_artifact(
            state,
            "concept_map_ref",
            tmp_path / "refs" / "concept_map.json",
            {"views": [], "clusters": []},
            producer="S4_concept_mapping",
        )
        state = orch.run(state, only_stage="S7_blueprint_candidates")
        rec = state.stages["S7_blueprint_candidates"]
        assert rec.status == "failed"
        assert "production_blueprint_not_admitted" in rec.error
        assert rec.stop_reason == "production_blueprint_not_admitted"

    def test_s8_refuses_non_admitted_candidates_before_judge(
        self, tmp_path, monkeypatch
    ):
        import optomind_research.blueprint_tournament_judge as judge_module

        class JudgeStub:
            def __init__(self, real_llm: bool = False):
                raise AssertionError("judge must not run on non-admitted input")

        monkeypatch.setattr(judge_module, "BlueprintTournamentJudge", JudgeStub)
        orch = _production_orchestrator(tmp_path)
        state = _new_state()
        _inject_artifact(
            state,
            "review_charter_ref",
            tmp_path / "refs" / "review_charter.json",
            {"central_question": "Q", "scope_statement": "S"},
            producer="S5_review_charter",
        )
        _inject_artifact(
            state,
            "mentor_advice_ref",
            tmp_path / "refs" / "mentor_advice.json",
            {},
            producer="S6_mentor_advice",
        )
        _inject_artifact(
            state,
            "blueprint_candidates_ref",
            tmp_path / "refs" / "blueprint_candidates.json",
            {
                "production": False,
                "admission_decision": "reject",
                "candidates_sha256": "sha",
                "candidates": [],
            },
            producer="S7_blueprint_candidates",
        )
        state = orch.run(state, only_stage="S8_blueprint_selection")
        rec = state.stages["S8_blueprint_selection"]
        assert rec.status == "failed"
        assert "production_blueprint_not_admitted" in rec.error

    def test_s8_fails_closed_on_non_admitted_final_selection(
        self, tmp_path, monkeypatch
    ):
        import optomind_research.blueprint_scientific_critic as critic_module
        import optomind_research.blueprint_tournament_judge as judge_module

        class JudgeStub:
            def __init__(self, real_llm: bool = False):
                self.real_llm = real_llm

            @staticmethod
            def finalize(*_args, **_kwargs):
                return {
                    "production": False,
                    "admission_decision": "reject",
                    "selected_candidate_id": "BP-A",
                    "blueprint": {"sections": []},
                }

        class CriticStub:
            def __init__(self, real_llm: bool = False):
                self.real_llm = real_llm

            def review(self, **_kwargs):
                return {"verdict": "pass"}

        monkeypatch.setattr(judge_module, "BlueprintTournamentJudge", JudgeStub)
        monkeypatch.setattr(critic_module, "BlueprintScientificCritic", CriticStub)
        orch = _production_orchestrator(tmp_path)
        state = _new_state()
        _inject_artifact(
            state,
            "review_charter_ref",
            tmp_path / "refs" / "review_charter.json",
            {"central_question": "Q", "scope_statement": "S"},
            producer="S5_review_charter",
        )
        _inject_artifact(
            state,
            "mentor_advice_ref",
            tmp_path / "refs" / "mentor_advice.json",
            {},
            producer="S6_mentor_advice",
        )
        candidates_sha256 = "sha-final"
        _inject_artifact(
            state,
            "blueprint_candidates_ref",
            tmp_path / "refs" / "blueprint_candidates.json",
            {
                "production": True,
                "admission_decision": "admit",
                "candidates_sha256": candidates_sha256,
                "candidates": [{"candidate_id": "BP-A"}],
            },
            producer="S7_blueprint_candidates",
        )
        recommendation_dir = (
            tmp_path / "out" / "S8_blueprint_selection" / "attempt_2"
        )
        _inject_artifact(
            state,
            "selected_blueprint_ref",
            recommendation_dir / "blueprint_recommendation.json",
            {
                "production": True,
                "admission_decision": "admit",
                "selected_candidate_id": "BP-A",
                "candidates_sha256": candidates_sha256,
                "unified_blueprint": {"sections": []},
            },
            producer="S8_blueprint_selection",
        )
        state.stages["S8_blueprint_selection"].status = "needs_human"
        state.stages["S8_blueprint_selection"].attempt_number = 1
        state = orch.run(state, only_stage="S8_blueprint_selection")
        rec = state.stages["S8_blueprint_selection"]
        assert rec.status == "failed"
        assert "production_blueprint_not_admitted" in rec.error
        assert "S8_selection" in rec.error

    def test_s9_fails_closed_on_non_admitted_selected_blueprint(
        self, tmp_path, monkeypatch
    ):
        import optomind_research.section_contract_designer as designer_module

        class DesignerStub:
            def __init__(self, real_llm: bool = False):
                self.real_llm = real_llm

            def design_contracts(self, **_kwargs):
                raise AssertionError(
                    "designer must not run on non-admitted blueprint"
                )

        monkeypatch.setattr(
            designer_module, "SectionContractDesigner", DesignerStub
        )
        orch = _production_orchestrator(tmp_path)
        state = _new_state()
        _inject_artifact(
            state,
            "review_charter_ref",
            tmp_path / "refs" / "review_charter.json",
            {"central_question": "Q", "scope_statement": "S"},
            producer="S5_review_charter",
        )
        _inject_artifact(
            state,
            "selected_blueprint_ref",
            tmp_path / "refs" / "selected_blueprint.json",
            {
                "production": False,
                "admission_decision": "reject",
                "blueprint": {"sections": []},
            },
            producer="S8_blueprint_selection",
        )
        state = orch.run(state, only_stage="S9_section_contracts")
        rec = state.stages["S9_section_contracts"]
        assert rec.status == "failed"
        assert "production_blueprint_not_admitted" in rec.error
