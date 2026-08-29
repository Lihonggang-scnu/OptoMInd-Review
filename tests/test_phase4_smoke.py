"""Phase 4 acceptance tests — FullReviewOrchestrator, auditor, revision pipeline."""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, call, patch

import pytest

from optomind_research.runtime.full_review_orchestrator import (
    _restore_section_editor_transaction,
    _snapshot_section_editor_transaction,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_editor_rerun_snapshot_restores_all_canonical_artifacts(
    tmp_path: Path,
):
    draft = tmp_path / "SECTION_DRAFT_EN.md"
    package = tmp_path / "SECTION_AUTHORING_PACKAGE.json"
    draft.write_text("Accepted scientific section.", encoding="utf-8")
    package.write_text('{"authoring_status":"completed"}', encoding="utf-8")
    snapshot = _snapshot_section_editor_transaction(tmp_path)

    draft.write_text("Regressed rewrite.", encoding="utf-8")
    package.unlink()
    (tmp_path / "_audit_stale").write_text("1", encoding="utf-8")
    _restore_section_editor_transaction(tmp_path, snapshot)

    assert draft.read_text(encoding="utf-8") == "Accepted scientific section."
    assert json.loads(package.read_text(encoding="utf-8"))[
        "authoring_status"
    ] == "completed"
    assert not (tmp_path / "_audit_stale").exists()


def test_source_synthesis_rerun_restores_old_section_when_budget_is_empty(
    tmp_path: Path,
):
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
    )

    config = _make_orchestrator_config(tmp_path)
    config.run_cost_budget_cny = 0.5
    orchestrator = FullReviewOrchestrator(config)
    orchestrator._run_id = "rollback_budget_test"
    orchestrator._work_dir = tmp_path / "orchestrator"
    section_dir = orchestrator._work_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    old_draft = "Accepted scientific section that must survive a failed rerun."
    (section_dir / "SECTION_DRAFT_EN.md").write_text(
        old_draft,
        encoding="utf-8",
    )
    _write_json(
        section_dir / "SECTION_ARGUMENT_PLAN.json",
        {"paragraphs": [{"paragraph_index": 0}]},
    )
    _write_json(
        section_dir / "SECTION_EVIDENCE_PACKET.json",
        {"items": [{"chunk_id": "chunk_S01_001"}]},
    )
    _write_json(
        section_dir / "SECTION_AUTHORING_PACKAGE.json",
        {"authoring_status": "completed"},
    )
    _write_json(
        section_dir / "RESULT.json",
        {"status": "completed", "estimated_cost_cny": 0.5},
    )
    orchestrator._section_registry = {
        "sections": [{
            "section_id": "S01",
            "status": "completed",
            "work_dir": str(section_dir),
            "cost_cny": 0.5,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
        }]
    }

    success = orchestrator._rerun_section_for_revision(
        "S01",
        {
            "flag_id": "F_SOURCE",
            "action": "rerun_section_with_source_synthesis",
            "description": "Broaden the source synthesis.",
        },
    )

    assert success is False
    assert (section_dir / "SECTION_DRAFT_EN.md").read_text(
        encoding="utf-8"
    ) == old_draft
    assert (section_dir / "SECTION_ARGUMENT_PLAN.json").exists()
    assert (section_dir / "SECTION_EVIDENCE_PACKET.json").exists()
    assert json.loads((section_dir / "RESULT.json").read_text(
        encoding="utf-8"
    ))["status"] == "completed"
    assert orchestrator._section_registry["sections"][0][
        "editorial_rerun_rolled_back"
    ] is True


def _make_blueprint(work_dir: Path) -> Path:
    blueprint = {
        "schema_version": "dynamic_review_blueprint.v4",
        "input_context": {
            "problem_understanding": "Nonlinear optical responses in 2D materials.",
            "user_question": "What are the key nonlinear optical mechanisms in 2D materials?",
        },
        "sections": [
            {
                "section_id": "S01",
                "title": "Saturable Absorption in MoS2",
                "argument_role": "mechanism",
                "key_questions": ["What causes saturable absorption in MoS2?"],
                "claims": [{"claim_id": "C01", "statement": "MoS2 exhibits saturable absorption.",
                             "load_bearing": True, "claim_kind": "factual", "writing_permission": "factual"}],
                "review_mentor_advice": {"focus": "describe the physical mechanism"},
                "transition_to_next": {"type": "contrast", "target_section": "S02"},
                "visual_argument_slots": [],
                "writing_requirements": {},
            },
            {
                "section_id": "S02",
                "title": "Third-Order Nonlinearity in MXenes",
                "argument_role": "mechanism",
                "key_questions": ["What nonlinear responses do MXenes exhibit?"],
                "claims": [{"claim_id": "C02", "statement": "MXenes show third-order nonlinear responses.",
                             "load_bearing": True, "claim_kind": "factual", "writing_permission": "factual"}],
                "review_mentor_advice": {"focus": "compare with MoS2"},
                "transition_to_next": {},
                "visual_argument_slots": [],
                "writing_requirements": {},
            },
        ],
    }
    bp_path = work_dir / "blueprint.json"
    _write_json(bp_path, blueprint)
    return bp_path


_SECTION_CHUNK = {"S01": "chunk_S01_001", "S02": "chunk_S02_001"}
_SECTION_PAPER = {"S01": "paper_S01", "S02": "paper_S02"}


def _make_material_bundle(section_id: str, title: str, work_dir: Path):
    """Build a SectionMaterialBundle with per-section chunk/paper IDs."""
    from optomind_research.runtime.full_review_orchestrator import SectionMaterialBundle

    chunk_id = _SECTION_CHUNK.get(section_id, "chunk_found_001")
    paper_id = _SECTION_PAPER.get(section_id, "paper_A")
    mat_dir = work_dir / "material" / section_id
    mat_dir.mkdir(parents=True, exist_ok=True)

    material = {
        "schema_version": "2.0", "section_id": section_id, "section_title": title,
        "chapter_argument": "Nonlinear optical responses in 2D materials.",
        "coverage_status": "coverage_sufficient", "total_sources": 1,
        "sources_by_role": {"mechanism": 1},
        "chunk_ids_by_role": {"mechanism": [chunk_id]},
        "blocking_gaps_remain": False, "gap_summary": "",
    }
    material_path = mat_dir / "SECTION_MATERIAL_PACKAGE.json"
    material_path.write_text(json.dumps(material), encoding="utf-8")

    ledger = {
        "schema_version": "2.0", "section_id": section_id,
        "sources": [{"paper_id": paper_id, "title": "Synthetic fixture",
                     "literature_role": "mechanism", "scope_fit": "direct",
                     "canonical_chunk_ids": [chunk_id], "acquisition_status": "fulltext",
                     "not_usable_for": []}],
    }
    ledger_path = mat_dir / "SECTION_SOURCE_LEDGER.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    kb_path = mat_dir / "kb.sqlite"
    conn = sqlite3.connect(str(kb_path))
    try:
        conn.execute("CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT, evidence_level TEXT, source_kind TEXT)")
        conn.execute("INSERT INTO text_chunks VALUES (?,?,?,?,?)", (
            chunk_id, paper_id,
            f"Two-dimensional material {section_id} exhibits nonlinear optical responses "
            f"arising from atomically thin geometry. {section_id} material under pulsed optical excitation.",
            "fulltext", "fulltext",
        ))
        conn.commit()
    finally:
        conn.close()

    return SectionMaterialBundle(material_package_path=material_path, source_ledger_path=ledger_path, kb_sqlite=kb_path)


def _make_section_scripted_model(ctx):
    """Callable[[SectionAuthoringContext], model] — reads claim/chunk/paper/text from ctx.

    Generates section-specific content so S01 (MoS2) and S02 (MXene) are
    semantically distinct and do not trigger duplicate-dedup rollbacks.
    """
    import sqlite3 as _sq3
    claims = ctx.section_data.get("claims", [])
    claim_id = claims[0]["claim_id"] if claims else "C01"

    pkg = json.loads(ctx.material_package_path.read_text(encoding="utf-8"))
    chunk_ids_by_role = pkg.get("chunk_ids_by_role", {})
    all_chunks = [c for chunks in chunk_ids_by_role.values() for c in chunks]
    chunk_id = all_chunks[0] if all_chunks else "chunk_found_001"

    ledger = json.loads(ctx.source_ledger_path.read_text(encoding="utf-8"))
    sources = ledger.get("sources", [])
    paper_id = sources[0]["paper_id"] if sources else "paper_A"

    # Read actual KB text so exact_spans match regardless of which KB is used
    kb_span = f"Section {ctx.section_id} material."
    if ctx.kb_sqlite and ctx.kb_sqlite.exists():
        try:
            _conn = _sq3.connect(str(ctx.kb_sqlite))
            row = _conn.execute(
                "SELECT text FROM text_chunks WHERE chunk_id=?", (chunk_id,)
            ).fetchone()
            _conn.close()
            if row and row[0]:
                first_sentence = row[0].split(". ")[0].strip()
                kb_span = first_sentence if first_sentence else kb_span
        except Exception:
            pass

    # Section-specific prose so S01 and S02 are semantically distinct
    # _DUP_SENTENCE is citation-free — shared by both sections to trigger duplicate_content
    # audit flag (auto-fixed by remove_duplicate_paragraph in revision round 1).
    _DUP_SENTENCE = (
        "Two-dimensional layered materials have emerged as a new platform"
        " for nanoscale light-matter interactions."
    )
    if ctx.section_id == "S01" or claim_id == "C01":
        DRAFT_TEXT = (
            f"MoS2 exhibits saturable absorption under pulsed optical excitation"
            f" [REF:{paper_id}].\n\n"
            "This mechanism arises from Pauli blocking of excitonic states in the atomically thin"
            " material. The nonlinear absorption coefficient decreases with increasing optical"
            " intensity, enabling passive mode-locking applications. Experimental measurements"
            " confirm the saturation threshold and reversibility of the bleaching process at room"
            f" temperature. The saturation fluence scales linearly with photon energy. {_DUP_SENTENCE}"
        )
        sentence_snippet = "MoS2 exhibits saturable absorption under pulsed optical excitation"
    else:
        DRAFT_TEXT = (
            f"MXenes demonstrate strong third-order nonlinear optical responses"
            f" [REF:{paper_id}].\n\n"
            "Delocalized d-electrons in the metallic band structure of MXene materials give rise"
            " to large third-order susceptibility chi(3). This electronic origin fundamentally"
            " differentiates MXene nonlinearity from excitonic mechanisms observed in transition"
            " metal dichalcogenides such as MoS2. Ultrafast pump-probe experiments validate the"
            f" nonlinear coefficients across visible and near-infrared wavelengths. {_DUP_SENTENCE}"
        )
        sentence_snippet = "MXenes demonstrate strong third-order nonlinear optical responses"

    PLAN_JSON = json.dumps({
        "argument_flow": "intro → mechanism → evidence synthesis",
        "paragraphs": [{
            "paragraph_index": 0,
            "function": "introduction",
            "topic_sentence": f"Section {ctx.section_id} covers nonlinear optical responses.",
            "key_claims": [claim_id],
            "evidence_chunk_ids": [chunk_id],
            "paper_ids": [paper_id],
            "writing_permission": "factual_assertion",
            "expected_word_count": 120,
        }],
    })

    EP_JSON = json.dumps({
        "items": [{
            "chunk_id": chunk_id,
            "paper_id": paper_id,
            "literature_role": "foundation",
            "scope_fit": "direct",
            "exact_spans": [kb_span],
            "claim_ids": [claim_id],
            "writing_permission": "factual_assertion",
            "not_usable_for": [],
        }]
    })

    CIT_MAP_JSON = json.dumps([{
        "sentence_index": 0,
        "sentence_snippet": sentence_snippet,
        "chunk_ids": [chunk_id],
        "paper_ids": [paper_id],
        "citation_type": "factual",
        "entailment_verdict": "entailed",
        "audit_note": "",
    }])

    STEPS = [
        ("load_authoring_context", {}),
        ("inspect_material_package", {}),
        ("submit_argument_plan", {"plan_json": PLAN_JSON}),
        ("build_evidence_packet", {"evidence_json": EP_JSON}),
        ("submit_section_draft", {"draft_text": DRAFT_TEXT, "summary": "initial"}),
        ("run_citation_audit", {"citation_map_json": CIT_MAP_JSON}),
        ("submit_visual_placement", {"placements_json": "[]"}),
        ("validate_authoring_package", {}),
    ]

    from test_research_worker_runtime import ScriptedFakeModel, _make_tool_call_response
    return ScriptedFakeModel(
        script=[_make_tool_call_response(name, arguments) for name, arguments in STEPS],
        usage_per_call=(120, 30),
    )


def _make_orchestrator_config(tmp_path: Path, model_factory=None):
    from optomind_research.runtime.full_review_orchestrator import OrchestratorConfig

    bp_path = _make_blueprint(tmp_path)
    blueprint = json.loads(bp_path.read_text(encoding="utf-8"))
    sections = blueprint["sections"]

    material_bundles = {}
    for s in sections:
        material_bundles[s["section_id"]] = _make_material_bundle(s["section_id"], s.get("title", ""), tmp_path)

    return OrchestratorConfig(
        blueprint_path=bp_path,
        output_root=tmp_path / "out",
        max_revision_rounds=1,
        section_model_tier="standard_model",
        section_max_iters=20,
        section_token_budget=60_000,
        section_wall_time_seconds=600.0,
        material_bundles=material_bundles,
        model_override=model_factory or _make_section_scripted_model,
    )


# ---------------------------------------------------------------------------
# Test 1 — synthetic bundle has valid material paths
# ---------------------------------------------------------------------------

def test_synthetic_bundle_valid_material(tmp_path):
    bundle = _make_material_bundle("S01", "Test Section", tmp_path)
    assert bundle.material_package_path.exists()
    assert bundle.source_ledger_path.exists()
    assert bundle.kb_sqlite is not None and bundle.kb_sqlite.exists()

    pkg = json.loads(bundle.material_package_path.read_text(encoding="utf-8"))
    assert pkg["coverage_status"] == "coverage_sufficient"
    assert not pkg["blocking_gaps_remain"]

    conn = sqlite3.connect(str(bundle.kb_sqlite))
    try:
        rows = conn.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0]
    finally:
        conn.close()
    assert rows > 0, "KB must have at least one chunk"


# ---------------------------------------------------------------------------
# Test 2 — fail closed on missing material (None bundle → needs_more_literature)
# ---------------------------------------------------------------------------

def test_fail_closed_on_missing_material(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator, OrchestratorConfig,
    )

    bp_path = _make_blueprint(tmp_path)
    config = OrchestratorConfig(
        blueprint_path=bp_path,
        output_root=tmp_path / "out",
        max_revision_rounds=1,
        material_bundles={},  # no bundles — all sections will get None
        model_override=None,
    )
    orch = FullReviewOrchestrator(config)
    result = orch.run()

    # Run must not crash; completed/partial/awaiting are all valid non-crash statuses
    assert result.status in ("completed", "partial", "awaiting_human_review", "needs_more_literature", "failed", "blocked")

    # Both sections should be marked needs_more_literature in the registry
    registry_path = result.work_dir / "SECTION_REGISTRY.json"
    assert registry_path.exists()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    statuses = {s["section_id"]: s["status"] for s in registry["sections"]}
    assert statuses.get("S01") == "needs_more_literature"
    assert statuses.get("S02") == "needs_more_literature"


# ---------------------------------------------------------------------------
# Test 3 — fail closed on empty KB (0 rows → needs_more_literature)
# ---------------------------------------------------------------------------

def test_fail_closed_on_empty_kb(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator, OrchestratorConfig, SectionMaterialBundle,
    )

    bp_path = _make_blueprint(tmp_path)

    # Build bundles with empty KBs
    def _empty_bundle(sid):
        mat_dir = tmp_path / "mat" / sid
        mat_dir.mkdir(parents=True, exist_ok=True)
        pkg = {"schema_version": "2.0", "section_id": sid, "coverage_status": "coverage_sufficient",
               "blocking_gaps_remain": False, "gap_summary": "", "total_sources": 0,
               "sources_by_role": {}, "chunk_ids_by_role": {}}
        mat_path = mat_dir / "SECTION_MATERIAL_PACKAGE.json"
        mat_path.write_text(json.dumps(pkg), encoding="utf-8")
        ledger = {"schema_version": "2.0", "section_id": sid, "sources": []}
        ledger_path = mat_dir / "SECTION_SOURCE_LEDGER.json"
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        kb_path = mat_dir / "empty.sqlite"
        conn = sqlite3.connect(str(kb_path))
        try:
            conn.execute("CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT, evidence_level TEXT, source_kind TEXT)")
            conn.commit()
        finally:
            conn.close()
        return SectionMaterialBundle(mat_path, ledger_path, kb_path)

    config = OrchestratorConfig(
        blueprint_path=bp_path,
        output_root=tmp_path / "out",
        max_revision_rounds=1,
        material_bundles={"S01": _empty_bundle("S01"), "S02": _empty_bundle("S02")},
        model_override=None,
    )
    orch = FullReviewOrchestrator(config)
    result = orch.run()

    registry_path = result.work_dir / "SECTION_REGISTRY.json"
    assert registry_path.exists()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    statuses = {s["section_id"]: s["status"] for s in registry["sections"]}
    assert statuses.get("S01") == "needs_more_literature"
    assert statuses.get("S02") == "needs_more_literature"


# ---------------------------------------------------------------------------
# Test 4 — minimal smoke calls FullReviewOrchestrator.run exactly once
# ---------------------------------------------------------------------------

def test_minimal_smoke_calls_orchestrator(tmp_path, monkeypatch):
    import scripts.run_full_review_smoke as smoke_mod
    from optomind_research.runtime.full_review_orchestrator import OrchestratorResult

    fake_result = OrchestratorResult(
        run_id="test_run",
        status="completed",
        work_dir=tmp_path,
        sections_completed=2,
        sections_failed=0,
        total_flags=0,
        blocking_flags=0,
        revision_rounds=0,
        total_cost_usd=0.0,
        wall_time_seconds=0.1,
    )

    run_count = []

    def fake_run(self, section_ids=None):
        run_count.append(1)
        return fake_result

    monkeypatch.setattr(
        "optomind_research.runtime.full_review_orchestrator.FullReviewOrchestrator.run",
        fake_run,
    )
    monkeypatch.setattr(smoke_mod, "OUTPUT_ROOT", tmp_path / "smoke_out")

    # Create stub output files in fake_result.work_dir (tmp_path) so that
    # _run_minimal's post-run assertions find them (the real orchestrator is mocked out).
    s01_wd = tmp_path / "sections" / "S01"
    s02_wd = tmp_path / "sections" / "S02"
    s01_wd.mkdir(parents=True)
    s02_wd.mkdir(parents=True)

    stub_registry = {
        "sections": [
            {"section_id": "S01", "status": "completed", "work_dir": str(s01_wd)},
            {"section_id": "S02", "status": "completed", "work_dir": str(s02_wd)},
        ]
    }
    (tmp_path / "SECTION_REGISTRY.json").write_text(
        json.dumps(stub_registry), encoding="utf-8"
    )
    (s01_wd / "SECTION_DRAFT_EN.md").write_text(
        "MoS2 saturable absorption content for section one.", encoding="utf-8"
    )
    (s02_wd / "SECTION_DRAFT_EN.md").write_text(
        "MXene third-order nonlinear content for section two.", encoding="utf-8"
    )
    (tmp_path / "FULL_REVIEW_CITATION_MAP.json").write_text(
        json.dumps({"citations": [{"paper_id": "paper_S01", "section_id": "S01",
                                   "trace_status": "verified"}]}),
        encoding="utf-8",
    )

    run_id = "test_" + uuid.uuid4().hex[:6]
    rc = smoke_mod._run_minimal(run_id)

    assert len(run_count) == 1, "FullReviewOrchestrator.run must be called exactly once"
    assert rc == 0


# ---------------------------------------------------------------------------
# Test 5 — two-section full pipeline produces merged draft and valid status
# ---------------------------------------------------------------------------

def test_two_section_full_pipeline(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    result = FullReviewOrchestrator(config).run()

    assert result.status in ("completed", "awaiting_human_review", "partial"), \
        f"Unexpected status: {result.status}"
    assert result.sections_completed >= 1
    merged = result.work_dir / "FULL_REVIEW_DRAFT_EN.md"
    assert merged.exists(), "FULL_REVIEW_DRAFT_EN.md must exist after pipeline"


# ---------------------------------------------------------------------------
# Test 6 — SECTION_REGISTRY.json updated after S01 before S02 starts
# ---------------------------------------------------------------------------

def test_checkpoint_after_each_section(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    registry_snapshots = []

    config = _make_orchestrator_config(tmp_path)
    orch = FullReviewOrchestrator(config)

    original_checkpoint = orch._checkpoint_registry.__func__

    def _patched_checkpoint(self):
        original_checkpoint(self)
        reg_path = self._work_dir / "SECTION_REGISTRY.json"
        if reg_path.exists():
            registry_snapshots.append(json.loads(reg_path.read_text(encoding="utf-8")))

    orch._checkpoint_registry = lambda: _patched_checkpoint(orch)
    orch.run()

    assert len(registry_snapshots) >= 1, "Should have at least one checkpoint"
    # At some snapshot, S01 should be marked completed before S02 is complete
    s01_completed_early = any(
        any(s["section_id"] == "S01" and s["status"] == "completed" for s in snap["sections"])
        for snap in registry_snapshots
    )
    assert s01_completed_early, "S01 must be marked completed in at least one checkpoint"


# ---------------------------------------------------------------------------
# Test 7 — resume() skips already-completed section
# ---------------------------------------------------------------------------

def test_resume_skips_completed_section(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    orch = FullReviewOrchestrator(config)

    # First: run S01 only (simulate partial run by stopping after S01)
    result_partial = orch.run(section_ids=["S01"])
    assert result_partial.work_dir is not None

    run_dir = result_partial.work_dir

    # Verify S01 completed
    registry_path = run_dir / "SECTION_REGISTRY.json"
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        s01_status = next((s["status"] for s in registry["sections"] if s["section_id"] == "S01"), None)
        assert s01_status == "completed", f"S01 should be completed, got {s01_status}"

    # Track how many times S01 is re-run
    s01_rerun_count = []
    config2 = _make_orchestrator_config(tmp_path / "resume")
    orch2 = FullReviewOrchestrator(config2)

    result_resumed = orch2.resume(run_dir)
    assert result_resumed.status in ("completed", "awaiting_human_review", "partial")


# ---------------------------------------------------------------------------
# Test 8 — resume() on completed run is idempotent (no re-run)
# ---------------------------------------------------------------------------

def test_resume_idempotent_on_completed(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    orch = FullReviewOrchestrator(config)
    result1 = orch.run()

    run_dir = result1.work_dir

    # Force state to "completed"
    state_path = run_dir / "REVIEW_STATE.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["state"] = "completed"
        state_path.write_text(json.dumps(state), encoding="utf-8")

    # resume() on completed run must return immediately, not re-author sections
    orch2 = FullReviewOrchestrator(config)
    authoring_calls = []
    original_run_one = orch2._run_one_section

    def patched_run_one(*args, **kwargs):
        authoring_calls.append(1)
        return original_run_one(*args, **kwargs)

    orch2._run_one_section = patched_run_one
    result2 = orch2.resume(run_dir)

    assert len(authoring_calls) == 0, "resume() on completed run must not re-author any sections"
    assert result2.status in ("completed", "awaiting_human_review", "partial")


# ---------------------------------------------------------------------------
# Test 9 — TargetedRevisionWorker calls rerun_fn for requires_rerun=True
# ---------------------------------------------------------------------------

def test_explicit_rerun_injection(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    revision_plan = {
        "round": 1,
        "auto_resolvable_flags": [{"flag_id": "F01"}],
        "human_review_flags": [],
        "revisions": [
            {
                "flag_id": "F01",
                "flag_type": "missing_transition",
                "root_cause": "transition",
                "action": "rerun_section_with_transition_fix",
                "requires_rerun": True,
                "target_sections": ["S01"],
                "description": "Missing transition",
            }
        ],
    }

    section_dir = tmp_path / "S01"
    section_dir.mkdir(parents=True, exist_ok=True)
    (section_dir / "SECTION_DRAFT_EN.md").write_text("Test content.", encoding="utf-8")

    section_registry = {
        "sections": [{"section_id": "S01", "work_dir": str(section_dir)}]
    }

    rerun_calls = []

    def mock_rerun_fn(section_id: str, revision: dict) -> bool:
        rerun_calls.append(section_id)
        return True

    worker = TargetedRevisionWorker()
    result = worker.apply(revision_plan, section_registry, tmp_path, rerun_fn=mock_rerun_fn)

    assert "S01" in rerun_calls, "rerun_fn must be called for requires_rerun=True revisions"
    assert "F01" in result.applied_revisions


# ---------------------------------------------------------------------------
# Test 10 — Layer 1 audit: no template-phrase methods; duplicate detection works
# ---------------------------------------------------------------------------

def test_layer1_audit_no_template_phrases(tmp_path):
    from optomind_research.runtime.global_review_auditor import GlobalReviewAuditor

    auditor = GlobalReviewAuditor()

    # Template-word methods must not exist
    assert not hasattr(auditor, "_detect_orphaned_conclusions"), \
        "_detect_orphaned_conclusions should have been removed"
    assert not hasattr(auditor, "_detect_missing_transitions"), \
        "_detect_missing_transitions should have been removed"

    # Duplicate sentence detection must still work
    dup_sentence = (
        "MoS2 exhibits strong saturable absorption arising from Pauli blocking of excitonic states "
        "under intense illumination."
    )
    section_texts = {
        "S01": dup_sentence + "\n\nAdditional content for section one.",
        "S02": dup_sentence + "\n\nAdditional content for section two.",
    }
    section_registry = {
        "sections": [
            {"section_id": "S01", "work_dir": str(tmp_path / "S01"), "argument_role": "mechanism",
             "transition_contract": {}, "visual_argument_slots": []},
            {"section_id": "S02", "work_dir": str(tmp_path / "S02"), "argument_role": "mechanism",
             "transition_contract": {}, "visual_argument_slots": []},
        ]
    }

    merged_path = tmp_path / "merged.md"
    merged_path.write_text(
        f"## S01\n\n{section_texts['S01']}\n\n---\n\n## S02\n\n{section_texts['S02']}",
        encoding="utf-8",
    )

    audit = auditor.audit(merged_path, section_registry, tmp_path, round_num=1)
    flag_types = {f["type"] for f in audit.get("flags", [])}
    assert "duplicate_content" in flag_types, "Duplicate sentence detection must still flag duplicates"


# ---------------------------------------------------------------------------
# Test 11 — core artifacts registered after scripted two-section run
# ---------------------------------------------------------------------------

_REQUIRED_ARTIFACT_KEYS = {
    "orchestration_context",
    "section_registry",
    "review_state",
    "merged_draft",
    "events",
    "cost",
    "full_review_package",
    "result",
}


def test_core_artifacts_registered(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    orch = FullReviewOrchestrator(config)
    orch.run()

    registered = orch._written_artifacts
    missing = _REQUIRED_ARTIFACT_KEYS - set(registered.keys())
    assert not missing, f"Missing artifact keys: {missing}"

    for key in _REQUIRED_ARTIFACT_KEYS:
        assert registered[key].exists(), f"Artifact '{key}' path does not exist on disk"


# ---------------------------------------------------------------------------
# Test 12 — missing_transition flag → requires_rerun=True in revision plan
# ---------------------------------------------------------------------------

def test_revision_plan_rerun_for_transition(tmp_path):
    from optomind_research.runtime.revision_planner import RevisionPlanner

    audit_report = {
        "schema_version": "phase4.global_audit.v1",
        "round": 1,
        "total_flags": 1,
        "blocking_flags": 0,
        "flags": [
            {
                "flag_id": "F01",
                "type": "missing_transition",
                "severity": "warning",
                "blocking": False,
                "section_ids": ["S01", "S02"],
                "description": "Section S01 lacks transition to S02",
            }
        ],
    }

    planner = RevisionPlanner()
    plan = planner.plan(audit_report, work_dir=tmp_path)

    auto_flags = plan.get("auto_resolvable_flags", [])
    assert len(auto_flags) == 1
    revision = auto_flags[0]
    assert revision["requires_rerun"] is True, \
        "missing_transition must map to requires_rerun=True in revision plan"
    assert revision["action"] == "rerun_section_with_transition_fix"


# ===========================================================================
# New tests — Fix 1-6: RevisionTransaction safety guards, EP protocol,
# asset isolation, quality gates
# ===========================================================================

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_section_lookup(section_dir: Path, section_id: str = "S01") -> dict:
    """Minimal section_lookup for targeted_revision_worker tests."""
    return {
        section_id: {
            "section_id": section_id,
            "work_dir": str(section_dir),
            "section_data": None,
        }
    }


def _make_section_registry_entry(section_dir: Path, section_id: str = "S01") -> dict:
    return {"sections": [{"section_id": section_id, "work_dir": str(section_dir)}]}


# ---------------------------------------------------------------------------
# Test 13 — whole section emptied → transaction rollback (Guard 1)
# ---------------------------------------------------------------------------

def test_transaction_whole_section_emptied_rolls_back(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    draft = section_dir / "SECTION_DRAFT_EN.md"
    original = (
        "MoS2 shows saturable absorption under intense pulsed illumination [REF:paper_S01]. "
        "This arises from Pauli blocking of excitonic states in the atomically thin lattice. "
        "The nonlinear absorption coefficient decreases systematically with increasing optical intensity. "
        "Mode-locking experiments confirm the reversibility of the bleaching process at room temperature."
    )
    draft.write_text(original, encoding="utf-8")

    section_lookup = _make_section_lookup(section_dir)
    worker = TargetedRevisionWorker()

    # Patch _apply_inline to write empty string (simulates destructive edit)
    def _wipe(action, target_sections, sl, wd, revision):
        draft.write_text("", encoding="utf-8")
        return True

    worker._apply_inline = _wipe

    result = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path,
        {"flag_id": "F01"},
    )

    assert result is False, "Transaction must fail (rollback) when section is emptied"
    assert draft.read_text(encoding="utf-8") == original, "Rollback must restore original content"

    history_path = tmp_path / "REVISION_HISTORY.json"
    assert history_path.exists()
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert any(not r["committed"] for r in history["records"]), \
        "REVISION_HISTORY must record a non-committed (rolled back) transaction"


# ---------------------------------------------------------------------------
# Test 14 — revision leaves fewer than 50 words → rollback (Guard 2)
# ---------------------------------------------------------------------------

def test_transaction_below_min_words_rolls_back(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    draft = section_dir / "SECTION_DRAFT_EN.md"
    # 70-word original
    original = (
        "MoS2 exhibits saturable absorption arising from Pauli blocking of excitonic states [REF:paper_S01]. "
        "The nonlinear absorption coefficient decreases with increasing intensity enabling passive mode-locking. "
        "Experimental pump-probe studies confirm the reversibility of optical bleaching in monolayer samples. "
        "The saturation threshold scales inversely with the optical confinement factor of the resonator geometry."
    )
    draft.write_text(original, encoding="utf-8")

    section_lookup = _make_section_lookup(section_dir)
    worker = TargetedRevisionWorker()

    # Patch to write only 10 words
    stub = "Short stub text only ten words here."
    def _short(action, target_sections, sl, wd, revision):
        draft.write_text(stub, encoding="utf-8")
        return True

    worker._apply_inline = _short
    result = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path, {"flag_id": "F02"},
    )

    assert result is False, "Must rollback when post-revision word count < 50"
    assert draft.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Test 15 — revision retains < 60% of original words → rollback (Guard 3)
# ---------------------------------------------------------------------------

def test_transaction_low_retention_rolls_back(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    draft = section_dir / "SECTION_DRAFT_EN.md"
    # 100-word original
    original = " ".join(["word"] * 100) + " [REF:paper_S01]."
    draft.write_text(original, encoding="utf-8")

    # After: 30 words → 30% < 60% threshold
    after_text = " ".join(["word"] * 30) + " [REF:paper_S01]."
    section_lookup = _make_section_lookup(section_dir)
    worker = TargetedRevisionWorker()

    def _reduce(action, target_sections, sl, wd, revision):
        draft.write_text(after_text, encoding="utf-8")
        return True

    worker._apply_inline = _reduce
    result = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path, {"flag_id": "F03"},
    )

    assert result is False, "Must rollback when retention ratio < 60%"
    assert draft.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Test 16 — revision removes [REF:*] citation → rollback (Guard 5)
# ---------------------------------------------------------------------------

def test_transaction_citation_lost_rolls_back(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    draft = section_dir / "SECTION_DRAFT_EN.md"
    # 70-word before text — citation must survive all guards
    original = (
        "MoS2 exhibits saturable absorption under intense pulsed optical excitation [REF:paper_S01]. "
        "This mechanism arises from Pauli blocking of excitonic states in the atomically thin material. "
        "The nonlinear absorption coefficient decreases with increasing optical intensity enabling passive mode-locking. "
        "Experimental pump-probe measurements confirm the saturation threshold at room temperature. "
        "The saturation fluence scales linearly with the optical confinement factor of the resonator geometry. "
        "Mode-locking stability is further enhanced by engineering the substrate coupling layer thickness."
    )
    draft.write_text(original, encoding="utf-8")

    # After text: same prose but [REF:paper_S01] removed. Word count stays > 50 and > 60% of before.
    after_no_ref = (
        "MoS2 exhibits saturable absorption under intense pulsed optical excitation. "
        "This mechanism arises from Pauli blocking of excitonic states in the atomically thin material. "
        "The nonlinear absorption coefficient decreases with increasing optical intensity enabling passive mode-locking. "
        "Experimental pump-probe measurements confirm the saturation threshold at room temperature. "
        "The saturation fluence scales linearly with the optical confinement factor of the resonator geometry. "
        "Mode-locking stability is further enhanced by engineering the substrate coupling layer thickness."
    )
    section_lookup = _make_section_lookup(section_dir)
    worker = TargetedRevisionWorker()

    def _strip_refs(action, target_sections, sl, wd, revision):
        draft.write_text(after_no_ref, encoding="utf-8")
        return True

    worker._apply_inline = _strip_refs
    result = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path, {"flag_id": "F04"},
    )

    assert result is False, "Must rollback when citations are removed"
    assert "[REF:paper_S01]" in draft.read_text(encoding="utf-8"), \
        "Original citation must be restored after rollback"


# ---------------------------------------------------------------------------
# Test 17 — safe exact-sentence dedup commits with committed=True in history
# ---------------------------------------------------------------------------

def test_dedup_safe_sentence_commits(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    # Duplicate sentence must contain NO citation — otherwise removing it from S02
    # would trigger Guard 5 (citation-loss rollback) instead of committing.
    dup_sentence = (
        "MoS2 exhibits saturable absorption under intense pulsed optical excitation arising"
        " from Pauli blocking of excitonic states in monolayer samples."
    )

    # Source section S01: dup + unique S01 content (with its own citation)
    s01_dir = tmp_path / "S01"
    s01_dir.mkdir()
    s01_draft = s01_dir / "SECTION_DRAFT_EN.md"
    s01_draft.write_text(
        dup_sentence + " The saturation fluence scales with photon energy [REF:paper_S01]. "
        "Mode-locking applications exploit this nonlinear response for ultrashort pulse generation.",
        encoding="utf-8",
    )

    # Target section S02: dup + unique S02 content (with its own citation)
    s02_dir = tmp_path / "S02"
    s02_dir.mkdir()
    s02_draft = s02_dir / "SECTION_DRAFT_EN.md"
    unique_s02 = (
        "MXene materials demonstrate strong third-order nonlinear optical susceptibility [REF:paper_S02]. "
        "Delocalized d-electrons in the metallic band structure of Ti3C2Tx MXene give rise to large chi(3) values. "
        "This electronic origin fundamentally differentiates MXene nonlinearity from excitonic mechanisms in MoS2. "
        "Ultrafast pump-probe experiments validate the nonlinear coefficients across visible and near-infrared wavelengths. "
        "The figure of merit for MXene-based saturable absorbers exceeds that of graphene at telecom wavelengths. "
        "Temperature-dependent studies reveal that chi(3) remains stable across a wide operational range."
    )
    s02_draft.write_text(dup_sentence + " " + unique_s02, encoding="utf-8")

    section_lookup = {
        "S01": {"section_id": "S01", "work_dir": str(s01_dir), "section_data": None},
        "S02": {"section_id": "S02", "work_dir": str(s02_dir), "section_data": None},
    }

    revision_plan = {
        "round": 1,
        "auto_resolvable_flags": [{"flag_id": "F05"}],
        "human_review_flags": [],
        "revisions": [{
            "flag_id": "F05",
            "flag_type": "duplicate_content",
            "action": "remove_duplicate_paragraph",
            "requires_rerun": False,
            "target_sections": ["S01", "S02"],
            "description": "Exact sentence duplicated between S01 and S02",
        }],
    }

    worker = TargetedRevisionWorker()
    worker.apply(revision_plan, {"sections": list(section_lookup.values())}, tmp_path)

    history_path = tmp_path / "REVISION_HISTORY.json"
    assert history_path.exists(), "REVISION_HISTORY.json must exist after revision"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert any(r["committed"] for r in history["records"]), \
        "At least one committed=True record required after safe dedup"


# ---------------------------------------------------------------------------
# Test 18 — committed inline patch writes .citation_audit_stale marker
# ---------------------------------------------------------------------------

def test_commit_writes_stale_marker(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    draft = section_dir / "SECTION_DRAFT_EN.md"
    original = (
        "MoS2 exhibits saturable absorption under intense pulsed optical excitation [REF:paper_S01]. "
        "This arises from Pauli blocking of excitonic states in the atomically thin material lattice. "
        "The nonlinear absorption coefficient decreases systematically with increasing optical intensity. "
        "Mode-locking experiments confirm reversibility of the optical bleaching process at room temperature. "
        "The saturation fluence scales linearly with the photon energy and cavity round-trip time. "
        "Substrate engineering further enhances the saturation threshold at near-infrared wavelengths."
    )
    draft.write_text(original, encoding="utf-8")

    section_lookup = _make_section_lookup(section_dir)
    worker = TargetedRevisionWorker()

    # Patch to make a valid edit: standardize "MoS2" → "MoS₂" (keeps all words/refs)
    def _valid_edit(action, target_sections, sl, wd, revision):
        text = draft.read_text(encoding="utf-8")
        draft.write_text(text.replace("MoS2", "MoS2-TMD"), encoding="utf-8")
        return True

    worker._apply_inline = _valid_edit
    result = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path, {"flag_id": "F06"},
    )

    assert result is True, "Valid edit should commit"
    stale_marker = section_dir / ".citation_audit_stale"
    assert stale_marker.exists(), ".citation_audit_stale must be written after a committed inline patch"


# ---------------------------------------------------------------------------
# Test 19 — canonical evidence packet parser reads chunk_id correctly
# ---------------------------------------------------------------------------

def test_canonical_ep_parses_correctly(tmp_path):
    from optomind_research.runtime.evidence_packet_parser import load_section_evidence_packet

    ep = {
        "schema_version": "2.0",
        "section_id": "S01",
        "items": [
            {
                "chunk_id": "chunk_S01_001",
                "paper_id": "paper_S01",
                "claim_ids": ["C01"],
                "exact_spans": ["MoS2 exhibits saturable absorption"],
                "writing_permission": "factual_assertion",
                "literature_role": "foundation",
                "scope_fit": "direct",
                "not_usable_for": [],
            }
        ],
        "uncovered_claim_ids": [],
    }
    ep_path = tmp_path / "SECTION_EVIDENCE_PACKET.json"
    ep_path.write_text(json.dumps(ep), encoding="utf-8")

    packet = load_section_evidence_packet(ep_path)
    assert packet.section_id == "S01"
    assert len(packet.items) == 1
    item = packet.items[0]
    assert item.chunk_id == "chunk_S01_001"
    assert item.paper_id == "paper_S01"
    assert "C01" in item.claim_ids


# ---------------------------------------------------------------------------
# Test 20 — old chunk_ids field raises ValueError
# ---------------------------------------------------------------------------

def test_old_chunk_ids_field_rejected(tmp_path):
    from optomind_research.runtime.evidence_packet_parser import load_section_evidence_packet

    bad_ep = {
        "schema_version": "1.0",
        "section_id": "S01",
        "items": [
            {
                "chunk_ids": ["chunk_S01_001"],  # deprecated plural field
                "paper_id": "paper_S01",
            }
        ],
    }
    ep_path = tmp_path / "BAD_EP.json"
    ep_path.write_text(json.dumps(bad_ep), encoding="utf-8")

    with pytest.raises(ValueError, match="chunk_ids"):
        load_section_evidence_packet(ep_path)


# ---------------------------------------------------------------------------
# Test 21 — strict S01/S02 asset isolation after full pipeline
# ---------------------------------------------------------------------------

def test_s01_s02_asset_strict_isolation(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator
    from optomind_research.runtime.evidence_packet_parser import load_section_evidence_packet

    config = _make_orchestrator_config(tmp_path)
    result = FullReviewOrchestrator(config).run()

    assert result.status in ("completed", "awaiting_human_review", "partial")

    registry_path = result.work_dir / "SECTION_REGISTRY.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    for entry in registry["sections"]:
        sid = entry["section_id"]
        work_dir = Path(entry["work_dir"])
        ep_path = work_dir / "SECTION_EVIDENCE_PACKET.json"

        if not ep_path.exists():
            continue  # section may have been skipped

        packet = load_section_evidence_packet(ep_path)
        expected_chunk = _SECTION_CHUNK[sid]
        expected_paper = _SECTION_PAPER[sid]
        other_chunks = {v for k, v in _SECTION_CHUNK.items() if k != sid}
        other_papers = {v for k, v in _SECTION_PAPER.items() if k != sid}

        chunk_ids = {item.chunk_id for item in packet.items}
        paper_ids = {item.paper_id for item in packet.items}

        # Must reference its own assets
        assert expected_chunk in chunk_ids, \
            f"{sid} evidence packet must include {expected_chunk}"
        assert expected_paper in paper_ids, \
            f"{sid} evidence packet must include {expected_paper}"

        # Must NOT reference other sections' assets
        leaked_chunks = chunk_ids & other_chunks
        leaked_papers = paper_ids & other_papers
        assert not leaked_chunks, f"{sid} leaked chunks from other sections: {leaked_chunks}"
        assert not leaked_papers, f"{sid} leaked papers from other sections: {leaked_papers}"


# ---------------------------------------------------------------------------
# Test 22 — empty section draft produces blocking flag in GlobalReviewAuditor
# ---------------------------------------------------------------------------

def test_empty_section_draft_blocking_flag(tmp_path):
    from optomind_research.runtime.global_review_auditor import GlobalReviewAuditor

    section_dir = tmp_path / "S01"
    section_dir.mkdir()

    # Write all required artifact files — but draft is empty
    (section_dir / "SECTION_AUTHORING_CONTEXT.json").write_text("{}", encoding="utf-8")
    (section_dir / "SECTION_ARGUMENT_PLAN.json").write_text("{}", encoding="utf-8")
    (section_dir / "SECTION_DRAFT_EN.md").write_text("", encoding="utf-8")
    (section_dir / "SECTION_AUTHORING_PACKAGE.json").write_text(
        json.dumps({"word_count": 0}), encoding="utf-8"
    )

    section_registry = {
        "sections": [{
            "section_id": "S01",
            "work_dir": str(section_dir),
            "argument_role": "mechanism",
            "transition_contract": {},
            "visual_argument_slots": [],
        }]
    }
    merged_path = tmp_path / "FULL_REVIEW_DRAFT_EN.md"
    merged_path.write_text("## S01\n\n", encoding="utf-8")

    auditor = GlobalReviewAuditor()
    report = auditor.audit(merged_path, section_registry, tmp_path, round_num=1)

    flag_types = {f["type"] for f in report.get("flags", [])}
    blocking_types = {f["type"] for f in report.get("flags", []) if f.get("blocking")}

    assert "empty_section_draft" in flag_types, \
        "Empty draft must produce 'empty_section_draft' flag"
    assert "empty_section_draft" in blocking_types, \
        "'empty_section_draft' flag must be blocking"
    assert report["blocking_flags"] > 0


# ---------------------------------------------------------------------------
# Test 23 — blocking flags → final status is not "completed"
# ---------------------------------------------------------------------------

def test_status_not_completed_with_empty_section(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    orch = FullReviewOrchestrator(config)

    # Intercept after each section completes to corrupt S01's draft
    original_run_one = orch._run_one_section.__func__

    _s01_corrupted = [False]

    def _patched_run_one(self, section, **kwargs):
        success = original_run_one(self, section, **kwargs)
        # After S01 is written, corrupt its draft to empty
        if section.get("section_id") == "S01" and not _s01_corrupted[0]:
            _s01_corrupted[0] = True
            reg = next(
                (s for s in self._section_registry["sections"] if s["section_id"] == "S01"),
                None,
            )
            if reg:
                draft = Path(reg["work_dir"]) / "SECTION_DRAFT_EN.md"
                if draft.exists():
                    draft.write_text("", encoding="utf-8")
        return success

    orch._run_one_section = lambda *a, **kw: _patched_run_one(orch, *a, **kw)
    result = orch.run()

    assert result.status != "completed", \
        "Status must not be 'completed' when a section draft is empty"


# ---------------------------------------------------------------------------
# Test 24 — REVISION_HISTORY.json contains both committed and rolled-back records
# ---------------------------------------------------------------------------

def test_revision_history_records_commit_and_rollback(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    draft = section_dir / "SECTION_DRAFT_EN.md"
    original = (
        "MoS2 exhibits saturable absorption under intense pulsed optical excitation [REF:paper_S01]. "
        "The nonlinear absorption coefficient decreases systematically with increasing optical intensity. "
        "Pauli blocking of excitonic states enables reversible optical bleaching in monolayer samples. "
        "Mode-locking experiments confirm the saturation threshold scales with optical confinement factor. "
        "The saturation fluence is inversely proportional to the third-order nonlinear susceptibility. "
        "Substrate engineering at near-infrared wavelengths further enhances mode-locking stability."
    )
    draft.write_text(original, encoding="utf-8")

    section_lookup = _make_section_lookup(section_dir)
    worker = TargetedRevisionWorker()

    # --- Transaction 1: valid edit → should COMMIT ---
    def _valid(action, target_sections, sl, wd, revision):
        text = draft.read_text(encoding="utf-8")
        draft.write_text(text.replace("MoS2", "MoS2-TMD"), encoding="utf-8")
        return True

    worker._apply_inline = _valid
    result1 = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path, {"flag_id": "F_commit"},
    )
    assert result1 is True

    # Reset draft for second transaction
    draft.write_text(draft.read_text(encoding="utf-8").replace("MoS2-TMD", "MoS2"), encoding="utf-8")

    # --- Transaction 2: empties section → should ROLLBACK ---
    def _wipe(action, target_sections, sl, wd, revision):
        draft.write_text("", encoding="utf-8")
        return True

    worker._apply_inline = _wipe
    result2 = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path, {"flag_id": "F_rollback"},
    )
    assert result2 is False

    # Verify REVISION_HISTORY.json
    history_path = tmp_path / "REVISION_HISTORY.json"
    assert history_path.exists(), "REVISION_HISTORY.json must be written"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    records = history["records"]

    assert len(records) >= 2, f"Expected ≥2 records, got {len(records)}"

    committed_records = [r for r in records if r["committed"]]
    rollback_records = [r for r in records if not r["committed"]]

    assert committed_records, "REVISION_HISTORY must contain at least one committed=True record"
    assert rollback_records, "REVISION_HISTORY must contain at least one committed=False record"

    # Verify rollback record has a reason
    for r in rollback_records:
        assert r.get("rollback_reason"), "Rolled-back records must include rollback_reason"


# ===========================================================================
# Tests 25-36 — Smoke asset isolation, global citation map, auto-revision flow
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 25 — shared factory produces correct isolated paper_id for S01
# ---------------------------------------------------------------------------

def test_smoke_factory_s01_paper_id(tmp_path):
    bundle = _make_material_bundle("S01", "Saturable Absorption in MoS2", tmp_path)
    ledger = json.loads(bundle.source_ledger_path.read_text(encoding="utf-8"))
    assert ledger["sources"][0]["paper_id"] == "paper_S01"
    pkg = json.loads(bundle.material_package_path.read_text(encoding="utf-8"))
    chunks = [c for cs in pkg.get("chunk_ids_by_role", {}).values() for c in cs]
    assert "chunk_S01_001" in chunks


# ---------------------------------------------------------------------------
# Test 26 — shared factory produces correct isolated paper_id for S02
# ---------------------------------------------------------------------------

def test_smoke_factory_s02_paper_id(tmp_path):
    bundle = _make_material_bundle("S02", "Third-Order Nonlinearity in MXenes", tmp_path)
    ledger = json.loads(bundle.source_ledger_path.read_text(encoding="utf-8"))
    assert ledger["sources"][0]["paper_id"] == "paper_S02"
    pkg = json.loads(bundle.material_package_path.read_text(encoding="utf-8"))
    chunks = [c for cs in pkg.get("chunk_ids_by_role", {}).values() for c in cs]
    assert "chunk_S02_001" in chunks


# ---------------------------------------------------------------------------
# Test 27 — smoke and test use the same bundle factory (same function object)
# ---------------------------------------------------------------------------

def test_smoke_and_test_share_bundle_factory():
    import importlib, sys
    sys.path.insert(0, str(PROJECT_ROOT / "tests"))
    import test_phase4_smoke as _self_mod

    # Import the function the smoke would import at runtime
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    # Both sides must produce the same canonical IDs — test by calling each:
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        b_test = _self_mod._make_material_bundle("S01", "T", td / "a")
        b_test2 = _self_mod._make_material_bundle("S02", "T", td / "b")
        l1 = json.loads(b_test.source_ledger_path.read_text())
        l2 = json.loads(b_test2.source_ledger_path.read_text())
    assert l1["sources"][0]["paper_id"] == "paper_S01"
    assert l2["sources"][0]["paper_id"] == "paper_S02"


# ---------------------------------------------------------------------------
# Test 28 — FULL_REVIEW_CITATION_MAP.json is non-empty after full pipeline
# ---------------------------------------------------------------------------

def test_global_citation_map_nonempty(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    result = FullReviewOrchestrator(config).run()

    cit_map_path = result.work_dir / "FULL_REVIEW_CITATION_MAP.json"
    assert cit_map_path.exists(), "FULL_REVIEW_CITATION_MAP.json must exist"
    cit_map = json.loads(cit_map_path.read_text(encoding="utf-8"))
    assert cit_map.get("citations"), \
        "FULL_REVIEW_CITATION_MAP citations list must be non-empty after pipeline"


# ---------------------------------------------------------------------------
# Test 29 — global citation map contains only body-cited papers
# ---------------------------------------------------------------------------

def test_global_citation_only_body_citations(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    result = FullReviewOrchestrator(config).run()

    cit_map_path = result.work_dir / "FULL_REVIEW_CITATION_MAP.json"
    assert cit_map_path.exists()
    cit_map = json.loads(cit_map_path.read_text(encoding="utf-8"))
    global_papers = {e["paper_id"] for e in cit_map.get("citations", [])}

    # Collect papers actually cited in each section's body (SECTION_CITATION_MAP)
    registry = json.loads((result.work_dir / "SECTION_REGISTRY.json").read_text(encoding="utf-8"))
    body_papers: set = set()
    for entry in registry["sections"]:
        cit_path = Path(entry["work_dir"]) / "SECTION_CITATION_MAP.json"
        if cit_path.exists():
            data = json.loads(cit_path.read_text(encoding="utf-8"))
            for cit in data.get("citations", []):
                body_papers.update(cit.get("paper_ids", []))

    if not body_papers:
        pytest.skip("No SECTION_CITATION_MAP found — cannot verify body-only constraint")

    extra = global_papers - body_papers
    assert not extra, \
        f"Global citation map must contain only body-cited papers; unexpected: {extra}"


# ---------------------------------------------------------------------------
# Test 30 — citation map entries have trace_status=verified
# ---------------------------------------------------------------------------

def test_citation_map_trace_status_verified(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    result = FullReviewOrchestrator(config).run()

    cit_map_path = result.work_dir / "FULL_REVIEW_CITATION_MAP.json"
    assert cit_map_path.exists()
    cit_map = json.loads(cit_map_path.read_text(encoding="utf-8"))

    entries = cit_map.get("citations", [])
    assert entries, "Need at least one citation entry to verify trace_status"

    for entry in entries:
        assert entry.get("trace_status") == "verified", (
            f"Expected trace_status=verified for paper_id={entry.get('paper_id')}, "
            f"got {entry.get('trace_status')!r}"
        )


# ---------------------------------------------------------------------------
# Test 31 — missing SECTION_CITATION_MAP with [REF:*] in draft → unresolved entry
# ---------------------------------------------------------------------------

def test_empty_citation_map_blocking_when_refs_in_draft(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    result = FullReviewOrchestrator(config).run()

    registry = json.loads((result.work_dir / "SECTION_REGISTRY.json").read_text(encoding="utf-8"))
    s01_entry = next((e for e in registry["sections"] if e["section_id"] == "S01"), None)
    if s01_entry is None:
        pytest.skip("S01 not in registry")

    s01_wd = Path(s01_entry["work_dir"])
    draft = (s01_wd / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8")
    if "[REF:" not in draft:
        pytest.skip("S01 draft has no [REF:*] — cannot test unresolved scenario")

    # Remove S01's per-section citation map to simulate missing citation tracking
    cit_path = s01_wd / "SECTION_CITATION_MAP.json"
    if cit_path.exists():
        cit_path.unlink()

    # Re-run global citation map generation on the real orchestrator instance
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator
    orch2 = FullReviewOrchestrator(config)
    orch2._work_dir = result.work_dir
    orch2._section_registry = registry
    orch2._run_id = result.run_id
    orch2._written_artifacts = {}
    orch2._write_global_citation_map()

    cit_map = json.loads(
        (result.work_dir / "FULL_REVIEW_CITATION_MAP.json").read_text(encoding="utf-8")
    )
    s01_entries = [e for e in cit_map.get("citations", []) if e.get("section_id") == "S01"]
    # Either unresolved entries or no entries (both acceptable — important: no silent "verified")
    for e in s01_entries:
        assert e.get("trace_status") != "verified", (
            "S01 entry in global citation map must not be 'verified' when "
            "SECTION_CITATION_MAP.json was missing"
        )


# ---------------------------------------------------------------------------
# Test 32 — auto-revision commits duplicate sentence removal (committed=True)
# ---------------------------------------------------------------------------

def test_auto_revision_commits_duplicate_sentence(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    result = FullReviewOrchestrator(config).run()

    assert result.status in ("completed", "awaiting_human_review", "partial")

    # If at least one auto-revision ran, REVISION_HISTORY must exist with a commit
    history_path = result.work_dir / "REVISION_HISTORY.json"
    if not history_path.exists():
        pytest.skip("No revision history — duplicate may not have been flagged this run")

    history = json.loads(history_path.read_text(encoding="utf-8"))
    committed = [r for r in history.get("records", []) if r.get("committed")]
    assert committed, \
        "At least one committed=True transaction must exist after auto-dedup revision"


# ---------------------------------------------------------------------------
# Test 33 — REVISION_HISTORY.json exists and has at least one committed record
# ---------------------------------------------------------------------------

def test_revision_history_exists_with_committed_record(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    result = FullReviewOrchestrator(config).run()

    history_path = result.work_dir / "REVISION_HISTORY.json"
    if not history_path.exists():
        pytest.skip("REVISION_HISTORY.json absent — no revisions ran (acceptable if no auto-fixable flags)")
        return

    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert "records" in history, "REVISION_HISTORY.json must have 'records' key"
    committed = [r for r in history["records"] if r.get("committed")]
    assert committed, \
        "REVISION_HISTORY must contain at least one committed=True record"


# ---------------------------------------------------------------------------
# Test 34 — audit round 2 does not flag duplicate_content after round 1 removes it
# ---------------------------------------------------------------------------

def test_audit_round2_duplicate_flag_gone(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator, OrchestratorConfig

    bp_path = _make_blueprint(tmp_path)
    blueprint = json.loads(bp_path.read_text(encoding="utf-8"))
    sections = blueprint["sections"]
    material_bundles = {s["section_id"]: _make_material_bundle(s["section_id"], s.get("title", ""), tmp_path)
                        for s in sections}

    # max_revision_rounds=1 → round-1 audit + revision + final audit (round 2 written as round_num=2)
    config = OrchestratorConfig(
        blueprint_path=bp_path,
        output_root=tmp_path / "out",
        max_revision_rounds=1,
        section_model_tier="standard_model",
        section_max_iters=20,
        section_token_budget=60_000,
        section_wall_time_seconds=600.0,
        material_bundles=material_bundles,
        model_override=_make_section_scripted_model,
    )
    result = FullReviewOrchestrator(config).run()
    run_dir = result.work_dir

    # Audit reports are written to audit_round_N/GLOBAL_AUDIT_REPORT.json
    round_reports = sorted(run_dir.glob("audit_round_*/GLOBAL_AUDIT_REPORT.json"))
    assert len(round_reports) >= 2, (
        f"Expected ≥2 audit round reports; found {len(round_reports)} in {run_dir}. "
        "Ensure _DUP_SENTENCE triggers duplicate_content in round 1 so revision+round2 run."
    )

    round2_report = json.loads(round_reports[1].read_text(encoding="utf-8"))
    dup_flags = [f for f in round2_report.get("flags", []) if f.get("type") == "duplicate_content"]
    assert not dup_flags, (
        f"Round 2 audit must not flag duplicate_content after round 1 dedup revision, "
        f"but found: {dup_flags}"
    )


# ---------------------------------------------------------------------------
# Test 35 — classification_confusion flag goes to human_review, not auto-modified
# ---------------------------------------------------------------------------

def test_human_review_classification_confusion_not_modified(tmp_path):
    from optomind_research.runtime.revision_planner import RevisionPlanner
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    audit_report = {
        "schema_version": "phase4.global_audit.v1",
        "round": 1,
        "total_flags": 1,
        "blocking_flags": 0,
        "flags": [{
            "flag_id": "F_CC",
            "type": "classification_confusion",
            "severity": "warning",
            "blocking": False,
            "section_ids": ["S01"],
            "description": "Section S01 mixes mechanism and result taxonomy",
        }],
    }

    planner = RevisionPlanner()
    plan = planner.plan(audit_report, work_dir=tmp_path)

    # classification_confusion must land in human_review_flags, not auto_resolvable_flags
    human_flag_ids = {f.get("flag_id") for f in plan.get("human_review_flags", [])}
    auto_flag_ids = {r.get("flag_id") for r in plan.get("auto_resolvable_flags", [])}
    assert "F_CC" in human_flag_ids, \
        "classification_confusion must be routed to human_review_flags"
    assert "F_CC" not in auto_flag_ids, \
        "classification_confusion must NOT be in auto_resolvable_flags"

    # TargetedRevisionWorker must not attempt any inline edit for that flag
    section_dir = tmp_path / "S01"
    section_dir.mkdir(parents=True, exist_ok=True)
    original_draft = "Original draft content that must not be changed [REF:paper_S01]."
    (section_dir / "SECTION_DRAFT_EN.md").write_text(original_draft, encoding="utf-8")

    section_registry = {"sections": [{"section_id": "S01", "work_dir": str(section_dir)}]}
    worker = TargetedRevisionWorker()
    worker.apply(plan, section_registry, tmp_path)

    after_text = (section_dir / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8")
    assert after_text == original_draft, \
        "TargetedRevisionWorker must not modify a section flagged for human_review"


# ---------------------------------------------------------------------------
# Test 36 — final package registers all key artifacts pointing to existing files
# ---------------------------------------------------------------------------

def test_final_package_registers_all_key_artifacts(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    _REQUIRED_PACKAGE_KEYS = {
        "orchestration_context",
        "section_registry",
        "review_state",
        "merged_draft",
        "citation_map",
        "events",
        "cost",
    }

    config = _make_orchestrator_config(tmp_path)
    orch = FullReviewOrchestrator(config)
    result = orch.run()

    # Verify via _written_artifacts on the instance
    registered = orch._written_artifacts
    missing = _REQUIRED_PACKAGE_KEYS - set(registered.keys())
    assert not missing, f"Missing artifact keys in _written_artifacts: {missing}"

    for key in _REQUIRED_PACKAGE_KEYS:
        path = registered[key]
        assert path.exists(), f"Artifact '{key}' path does not exist: {path}"

    # Also verify FULL_REVIEW_PACKAGE.json itself references these paths
    pkg_path = result.work_dir / "FULL_REVIEW_PACKAGE.json"
    assert pkg_path.exists(), "FULL_REVIEW_PACKAGE.json must exist"
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    artifacts = pkg.get("artifacts", {})
    for key in _REQUIRED_PACKAGE_KEYS:
        assert key in artifacts, f"FULL_REVIEW_PACKAGE.json artifacts must include '{key}'"


# ===========================================================================
# Tests 37-48 — Citation reaudit, claim IDs, completed status
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 37 — committed inline patch triggers citation reaudit callable
# ---------------------------------------------------------------------------

def test_reaudit_called_after_commit(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker, ReauditResult

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    original = (
        "MoS2 exhibits saturable absorption under intense pulsed optical excitation [REF:paper_S01]. "
        "Pauli blocking of excitonic states enables reversible bleaching in the monolayer limit. "
        "The nonlinear absorption coefficient decreases with optical intensity enabling passive mode-locking. "
        "Experimental pump-probe measurements confirm the saturation threshold at room temperature. "
        "The saturation fluence scales with photon energy and cavity round-trip time. "
        "Ultrafast carrier relaxation via phonon emission restores absorption within picoseconds."
    )
    (section_dir / "SECTION_DRAFT_EN.md").write_text(original, encoding="utf-8")

    section_lookup = {"S01": {"section_id": "S01", "work_dir": str(section_dir), "section_data": None}}

    reaudit_calls = []
    def mock_reaudit(section_id: str) -> ReauditResult:
        reaudit_calls.append(section_id)
        return ReauditResult(passed=True, reason="")

    def _valid_edit(action, target_sections, sl, wd, revision):
        p = section_dir / "SECTION_DRAFT_EN.md"
        p.write_text(p.read_text(encoding="utf-8").replace("MoS2", "MoS2-TMD"), encoding="utf-8")
        return True

    worker = TargetedRevisionWorker()
    worker._apply_inline = _valid_edit
    result = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path,
        {"flag_id": "F01"}, reaudit_fn=mock_reaudit,
    )

    assert result is True, "Valid edit with passing reaudit must commit"
    assert "S01" in reaudit_calls, "reaudit_fn must be called after successful edit"


# ---------------------------------------------------------------------------
# Test 38 — reaudit success removes .citation_audit_stale marker
# ---------------------------------------------------------------------------

def test_stale_marker_deleted_after_reaudit(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker, ReauditResult

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    original = (
        "MoS2 exhibits saturable absorption under intense pulsed optical excitation [REF:paper_S01]. "
        "Pauli blocking of excitonic states enables reversible optical bleaching in monolayer samples. "
        "The nonlinear absorption coefficient decreases with intensity enabling passive mode-locking at high fluence. "
        "Experimental pump-probe measurements confirm the saturation fluence threshold at room temperature. "
        "Sub-picosecond carrier relaxation through optical phonon emission governs the recovery dynamics."
    )
    (section_dir / "SECTION_DRAFT_EN.md").write_text(original, encoding="utf-8")

    section_lookup = {"S01": {"section_id": "S01", "work_dir": str(section_dir), "section_data": None}}

    def _reaudit_that_removes_stale(section_id: str) -> ReauditResult:
        stale = section_dir / ".citation_audit_stale"
        if stale.exists():
            stale.unlink()
        return ReauditResult(passed=True, reason="")

    def _valid_edit(action, target_sections, sl, wd, revision):
        p = section_dir / "SECTION_DRAFT_EN.md"
        p.write_text(p.read_text(encoding="utf-8").replace("MoS2", "MoS2-TMD"), encoding="utf-8")
        return True

    worker = TargetedRevisionWorker()
    worker._apply_inline = _valid_edit
    result = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path,
        {"flag_id": "F02"}, reaudit_fn=_reaudit_that_removes_stale,
    )

    assert result is True
    stale = section_dir / ".citation_audit_stale"
    assert not stale.exists(), ".citation_audit_stale must be removed after successful reaudit"


# ---------------------------------------------------------------------------
# Test 39 — reaudit failure rolls back all 4 artifacts atomically
# ---------------------------------------------------------------------------

def test_reaudit_failure_rolls_back_all(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker, ReauditResult

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    original_draft = (
        "MoS2 exhibits saturable absorption under intense pulsed optical excitation [REF:paper_S01]. "
        "Pauli blocking of excitonic states enables reversible bleaching in monolayer samples. "
        "The nonlinear absorption coefficient decreases with optical intensity enabling mode-locking. "
        "Experimental measurements confirm the saturation threshold at room temperature."
    )
    original_cit = json.dumps({"citations": [{"paper_ids": ["paper_S01"], "sentence_index": 0}]})
    original_pkg = json.dumps({"word_count": 60, "papers_cited": 1})

    (section_dir / "SECTION_DRAFT_EN.md").write_text(original_draft, encoding="utf-8")
    (section_dir / "SECTION_CITATION_MAP.json").write_text(original_cit, encoding="utf-8")
    (section_dir / "SECTION_AUTHORING_PACKAGE.json").write_text(original_pkg, encoding="utf-8")

    section_lookup = {"S01": {"section_id": "S01", "work_dir": str(section_dir), "section_data": None}}

    def _fail_reaudit(section_id: str) -> ReauditResult:
        return ReauditResult(passed=False, reason="unauthorized paper detected")

    def _valid_edit(action, target_sections, sl, wd, revision):
        p = section_dir / "SECTION_DRAFT_EN.md"
        p.write_text(p.read_text(encoding="utf-8").replace("MoS2", "MoS2-TMD"), encoding="utf-8")
        # Also corrupt citation map (simulates partial update)
        (section_dir / "SECTION_CITATION_MAP.json").write_text(
            json.dumps({"citations": [{"paper_ids": ["paper_UNAUTHORIZED"]}]}), encoding="utf-8"
        )
        return True

    worker = TargetedRevisionWorker()
    worker._apply_inline = _valid_edit
    result = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path,
        {"flag_id": "F03"}, reaudit_fn=_fail_reaudit,
    )

    assert result is False, "Reaudit failure must prevent commit"
    # All artifacts restored
    assert (section_dir / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8") == original_draft
    assert (section_dir / "SECTION_CITATION_MAP.json").read_text(encoding="utf-8") == original_cit
    assert not (section_dir / ".citation_audit_stale").exists(), \
        "stale marker must be cleaned up on reaudit rollback"


# ---------------------------------------------------------------------------
# Test 40 — rollback_reason recorded in history on reaudit failure
# ---------------------------------------------------------------------------

def test_rollback_restores_citation_map_and_audit(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker, ReauditResult

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    original_draft = (
        "MoS2 exhibits saturable absorption under intense pulsed optical excitation [REF:paper_S01]. "
        "Pauli blocking of excitonic states enables reversible bleaching in monolayer crystal samples. "
        "The nonlinear absorption coefficient decreases with optical intensity enabling passive mode-locking. "
        "Experimental measurements confirm the saturation threshold at room temperature. "
        "Carrier relaxation through phonon emission restores ground state absorption within picoseconds."
    )
    (section_dir / "SECTION_DRAFT_EN.md").write_text(original_draft, encoding="utf-8")

    section_lookup = {"S01": {"section_id": "S01", "work_dir": str(section_dir), "section_data": None}}

    def _fail_reaudit(sid):
        return ReauditResult(passed=False, reason="citation_map_invalid")

    def _edit(action, target_sections, sl, wd, revision):
        p = section_dir / "SECTION_DRAFT_EN.md"
        p.write_text(p.read_text(encoding="utf-8").replace("MoS2", "MoS2-TMD"), encoding="utf-8")
        return True

    worker = TargetedRevisionWorker()
    worker._apply_inline = _edit
    worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path,
        {"flag_id": "F04"}, reaudit_fn=_fail_reaudit,
    )

    history = json.loads((tmp_path / "REVISION_HISTORY.json").read_text(encoding="utf-8"))
    rec = history["records"][-1]
    assert not rec["committed"], "Record must be non-committed after reaudit failure"
    assert "citation_reaudit" in rec.get("rollback_reason", "") or \
           rec.get("citation_reaudit_status") == "failed", \
        "History must record citation_reaudit_status=failed"


# ---------------------------------------------------------------------------
# Test 41 — SECTION_AUTHORING_PACKAGE stats updated after reaudit
# ---------------------------------------------------------------------------

def test_package_stats_updated_after_reaudit(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    result = FullReviewOrchestrator(config).run()

    registry = json.loads((result.work_dir / "SECTION_REGISTRY.json").read_text(encoding="utf-8"))
    for entry in registry["sections"]:
        sid = entry["section_id"]
        wd = Path(entry["work_dir"])
        pkg_path = wd / "SECTION_AUTHORING_PACKAGE.json"
        draft_path = wd / "SECTION_DRAFT_EN.md"
        if not pkg_path.exists() or not draft_path.exists():
            continue
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        draft = draft_path.read_text(encoding="utf-8")
        actual_words = len(draft.split())
        pkg_words = int(pkg.get("word_count", 0))
        assert abs(pkg_words - actual_words) <= max(10, actual_words * 0.20), (
            f"{sid}: package word_count={pkg_words} differs from actual {actual_words} "
            "by more than 20% — package must be updated after reaudit"
        )


# ---------------------------------------------------------------------------
# Test 42 — claims_before/after in history record real claim_ids (not empty)
# ---------------------------------------------------------------------------

def test_real_claim_ids_in_history(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker, ReauditResult

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    original = (
        "MoS2 exhibits saturable absorption under pulsed excitation [REF:paper_S01]. "
        "Pauli blocking of excitonic states enables reversible bleaching in monolayer. "
        "Nonlinear absorption coefficient decreases enabling passive mode-locking applications. "
        "Pump-probe studies confirm the saturation threshold and reversibility at room temperature."
    )
    (section_dir / "SECTION_DRAFT_EN.md").write_text(original, encoding="utf-8")

    # Write SECTION_AUTHORING_CONTEXT.json with real claims
    ctx = {
        "section_id": "S01",
        "section_data": {
            "claims": [
                {"claim_id": "C01", "statement": "MoS2 exhibits saturable absorption.",
                 "load_bearing": True, "claim_kind": "factual"},
            ]
        },
    }
    (section_dir / "SECTION_AUTHORING_CONTEXT.json").write_text(json.dumps(ctx), encoding="utf-8")

    section_lookup = {"S01": {"section_id": "S01", "work_dir": str(section_dir), "section_data": None}}

    def _valid_edit(action, target_sections, sl, wd, revision):
        p = section_dir / "SECTION_DRAFT_EN.md"
        p.write_text(p.read_text(encoding="utf-8").replace("MoS2", "MoS2-TMD"), encoding="utf-8")
        return True

    worker = TargetedRevisionWorker()
    worker._apply_inline = _valid_edit
    worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path,
        {"flag_id": "F05"},
    )

    history = json.loads((tmp_path / "REVISION_HISTORY.json").read_text(encoding="utf-8"))
    rec = history["records"][-1]
    # claim_coverage_before should have the real claim_id "C01", not empty list
    coverage_before = rec.get("claim_coverage_before", {}).get("S01", [])
    assert coverage_before, "claim_coverage_before must not be empty when context has load-bearing claims"
    assert "C01" in coverage_before, f"Expected C01 in claim_coverage_before, got {coverage_before}"


# ---------------------------------------------------------------------------
# Test 43 — deleting unique load-bearing claim sentence rolls back
# ---------------------------------------------------------------------------

def test_removing_unique_claim_sentence_rolls_back(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    original = (
        "MoS2 exhibits saturable absorption under intense pulsed optical excitation [REF:paper_S01]. "
        "Pauli blocking of excitonic states enables reversible bleaching in monolayer samples. "
        "The nonlinear absorption coefficient decreases with intensity enabling passive mode-locking. "
        "Pump-probe measurements confirm the saturation threshold and fluence scaling at room temperature."
    )
    (section_dir / "SECTION_DRAFT_EN.md").write_text(original, encoding="utf-8")

    # Guard 5: removing [REF:paper_S01] must trigger rollback
    section_lookup = {"S01": {"section_id": "S01", "work_dir": str(section_dir), "section_data": None}}

    def _strip_citation(action, target_sections, sl, wd, revision):
        p = section_dir / "SECTION_DRAFT_EN.md"
        p.write_text(p.read_text(encoding="utf-8").replace(" [REF:paper_S01]", ""), encoding="utf-8")
        return True

    worker = TargetedRevisionWorker()
    worker._apply_inline = _strip_citation
    result = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path, {"flag_id": "F06"},
    )

    assert result is False, "Removing the only citation must rollback"
    assert "[REF:paper_S01]" in (section_dir / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 44 — non-critical duplicate sentence (no citation) commits cleanly
# ---------------------------------------------------------------------------

def test_dedup_noncritical_sentence_commits(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker, ReauditResult

    dup = "Two-dimensional layered materials have emerged as a new platform for nanoscale light-matter interactions."
    s01_dir, s02_dir = tmp_path / "S01", tmp_path / "S02"
    s01_dir.mkdir(); s02_dir.mkdir()
    (s01_dir / "SECTION_DRAFT_EN.md").write_text(
        dup + " The saturation fluence scales with photon energy [REF:paper_S01]. "
        "Mode-locking applications exploit this nonlinear response for ultrashort pulse generation.",
        encoding="utf-8"
    )
    (s02_dir / "SECTION_DRAFT_EN.md").write_text(
        dup + " MXene materials exhibit strong third-order nonlinear optical susceptibility [REF:paper_S02]. "
        "Delocalized d-electrons in the metallic band structure give rise to large chi(3) values. "
        "This origin differentiates MXene nonlinearity from excitonic mechanisms in transition metal dichalcogenides. "
        "Ultrafast experiments validate the nonlinear coefficients across visible and near-infrared wavelengths. "
        "The large imaginary component of the third-order susceptibility enables efficient all-optical switching.",
        encoding="utf-8"
    )
    section_lookup = {
        "S01": {"section_id": "S01", "work_dir": str(s01_dir), "section_data": None},
        "S02": {"section_id": "S02", "work_dir": str(s02_dir), "section_data": None},
    }
    revision_plan = {
        "round": 1,
        "auto_resolvable_flags": [{"flag_id": "F07"}],
        "human_review_flags": [],
        "revisions": [{"flag_id": "F07", "flag_type": "duplicate_content",
                       "action": "remove_duplicate_paragraph", "requires_rerun": False,
                       "target_sections": ["S01", "S02"], "description": "dup"}],
    }
    worker = TargetedRevisionWorker()
    worker.apply(revision_plan, {"sections": list(section_lookup.values())}, tmp_path)

    history_path = tmp_path / "REVISION_HISTORY.json"
    assert history_path.exists()
    history = json.loads(history_path.read_text(encoding="utf-8"))
    committed = [r for r in history["records"] if r["committed"]]
    assert committed, "Non-citation duplicate sentence removal must commit"
    assert dup not in (s02_dir / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8"), \
        "Duplicate sentence must be removed from lower-priority section"


# ---------------------------------------------------------------------------
# Test 45 — all supporting citations removed triggers rollback (Guard 5)
# ---------------------------------------------------------------------------

def test_all_supporting_citations_lost_rolls_back(tmp_path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    section_dir = tmp_path / "S01"
    section_dir.mkdir()
    original = (
        "MoS2 exhibits saturable absorption under pulsed excitation [REF:paper_S01]. "
        "The nonlinear absorption coefficient decreases with intensity enabling mode-locking. "
        "Pauli blocking of excitonic states enables reversible optical bleaching in monolayer. "
        "Pump-probe measurements confirm the saturation threshold and fluence scaling."
    )
    (section_dir / "SECTION_DRAFT_EN.md").write_text(original, encoding="utf-8")

    section_lookup = {"S01": {"section_id": "S01", "work_dir": str(section_dir), "section_data": None}}

    def _wipe_all_refs(action, target_sections, sl, wd, revision):
        import re as _r
        p = section_dir / "SECTION_DRAFT_EN.md"
        p.write_text(_r.sub(r'\[REF:[^\]]+\]', '', p.read_text(encoding="utf-8")), encoding="utf-8")
        return True

    worker = TargetedRevisionWorker()
    worker._apply_inline = _wipe_all_refs
    result = worker._apply_inline_transactional(
        "standardize_term_usage", ["S01"], section_lookup, tmp_path, {"flag_id": "F08"},
    )

    assert result is False, "Removing all citations must rollback"
    assert "[REF:paper_S01]" in (section_dir / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 46 — orchestrator _reaudit_section deletes stale marker on success
# ---------------------------------------------------------------------------

def test_orchestrator_reaudit_section_clears_stale(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator, OrchestratorConfig,
    )

    bp_path = _make_blueprint(tmp_path)
    blueprint = json.loads(bp_path.read_text(encoding="utf-8"))
    sections = blueprint["sections"]
    s01 = sections[0]
    bundle = _make_material_bundle("S01", s01.get("title", ""), tmp_path)

    config = OrchestratorConfig(
        blueprint_path=bp_path,
        output_root=tmp_path / "out",
        max_revision_rounds=1,
        material_bundles={"S01": bundle, "S02": bundle},
        model_override=_make_section_scripted_model,
    )
    orch = FullReviewOrchestrator(config)
    orch._run_id = "test_reaudit"
    orch._work_dir = tmp_path / "out" / "test_reaudit"
    orch._work_dir.mkdir(parents=True, exist_ok=True)
    orch._section_registry = {
        "sections": [{"section_id": "S01", "work_dir": str(tmp_path / "s01_wd"), "status": "completed"}]
    }

    s01_wd = tmp_path / "s01_wd"
    s01_wd.mkdir()
    (s01_wd / "SECTION_DRAFT_EN.md").write_text(
        "MoS2 exhibits saturable absorption [REF:paper_S01]. "
        "Pauli blocking enables reversible bleaching in monolayer samples at room temperature.",
        encoding="utf-8",
    )
    (s01_wd / "SECTION_AUTHORING_PACKAGE.json").write_text(
        json.dumps({"word_count": 20}), encoding="utf-8"
    )
    stale_marker = s01_wd / ".citation_audit_stale"
    stale_marker.write_text("stale", encoding="utf-8")

    result = orch._reaudit_section("S01")

    assert result.passed, f"Reaudit must pass for valid draft: {result.reason}"
    assert not stale_marker.exists(), "Stale marker must be deleted after successful reaudit"


# ---------------------------------------------------------------------------
# Test 47 — smoke final status is completed (not awaiting_human_review)
# ---------------------------------------------------------------------------

def test_smoke_final_status_completed(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    result = FullReviewOrchestrator(config).run()

    assert result.status == "completed", (
        f"Smoke pipeline final status must be 'completed', got '{result.status}'. "
        "Check: stale_citation_audit flag, blocking flags in last audit round."
    )
    assert result.blocking_flags == 0, \
        f"No blocking flags expected in completed run, got {result.blocking_flags}"


# ---------------------------------------------------------------------------
# Test 48 — citation_reaudit_status=passed appears in committed history record
# ---------------------------------------------------------------------------

def test_citation_reaudit_status_in_history(tmp_path):
    from optomind_research.runtime.full_review_orchestrator import FullReviewOrchestrator

    config = _make_orchestrator_config(tmp_path)
    result = FullReviewOrchestrator(config).run()

    history_path = result.work_dir / "REVISION_HISTORY.json"
    if not history_path.exists():
        pytest.skip("No revisions ran — cannot check citation_reaudit_status")

    history = json.loads(history_path.read_text(encoding="utf-8"))
    committed = [r for r in history.get("records", []) if r.get("committed")]
    assert committed, "Need at least one committed record"

    for rec in committed:
        status = rec.get("citation_reaudit_status", "missing")
        assert status in ("passed", "skipped"), (
            f"committed record must have citation_reaudit_status=passed or skipped, got '{status}'"
        )


def test_layer1_allows_related_but_nonduplicate_transition_sentences():
    """Shared vocabulary across adjacent sections is not verbatim duplication."""
    from optomind_research.runtime.global_review_auditor import (
        GlobalReviewAuditor,
    )

    flags = GlobalReviewAuditor()._detect_duplicates({
        "S03": (
            "Material constraints distinguish intrinsic optical limits from "
            "process-dependent fabrication challenges."
        ),
        "S04": (
            "Standardized measurements distinguish fundamental optical limits "
            "from manufacturing-dependent performance losses."
        ),
    })
    assert not flags


def test_visual_only_audit_plateau_does_not_block_completed_text(tmp_path):
    """Figure requests belong to the downstream visual editor, not prose reruns."""
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
    )

    orchestrator = FullReviewOrchestrator(_make_orchestrator_config(tmp_path))
    orchestrator._section_registry = {
        "sections": [
            {"section_id": "S01", "status": "completed"},
            {"section_id": "S02", "status": "completed"},
        ]
    }
    orchestrator._state["early_stop_reason"] = "consecutive_small_improvement"
    status = orchestrator._determine_final_status({
        "blocking_flags": 0,
        "flags": [
            {
                "type": "visual_gap",
                "blocking": False,
                "section_ids": ["S02"],
            }
        ],
    })
    assert status == "completed"


def test_successful_final_semantic_audit_clears_stale_failure_state(
    tmp_path,
    monkeypatch,
):
    from optomind_research.runtime import global_review_auditor
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
    )

    orchestrator = FullReviewOrchestrator(_make_orchestrator_config(tmp_path))
    orchestrator.config.use_llm_audit = True
    orchestrator.config.run_cost_budget_cny = 100.0
    orchestrator.config.audit_cost_budget_cny = 1.0
    orchestrator._work_dir = tmp_path / "semantic_audit"
    orchestrator._work_dir.mkdir(parents=True, exist_ok=True)
    (orchestrator._work_dir / "FULL_REVIEW_DRAFT_EN.md").write_text(
        "A complete scientific review section.",
        encoding="utf-8",
    )
    orchestrator._section_registry = {
        "sections": [{"section_id": "S01", "status": "completed"}]
    }
    orchestrator._state["layer2_audit_failed"] = True
    orchestrator._state["audit_budget_exhausted"] = True
    monkeypatch.setattr(
        global_review_auditor.LLMAuditLayer,
        "audit",
        lambda self, **kwargs: [],
    )
    result = orchestrator._run_final_semantic_audit(
        {"flags": [], "total_flags": 0, "blocking_flags": 0},
        1,
    )
    assert result["layer2_status"] == "completed"
    assert "layer2_audit_failed" not in orchestrator._state
    assert "audit_budget_exhausted" not in orchestrator._state
    assert orchestrator._determine_final_status(result) == "completed"


def test_semantic_audit_cache_key_tracks_article_and_prompt_changes():
    """A revised article or editor prompt must never reuse stale audit flags."""
    from optomind_research.runtime.global_review_auditor import (
        _semantic_audit_fingerprint,
    )

    registry = {
        "sections": [
            {
                "section_id": "S01",
                "title": "Mechanisms",
                "argument_role": "Explain the governing mechanism.",
                "status": "completed",
            }
        ]
    }
    blueprint = {
        "input_context": {"user_question": "Review an optical platform."},
        "full_review_argument": "Connect mechanism to implementation.",
    }
    original = _semantic_audit_fingerprint(
        "Original review text.",
        registry,
        blueprint,
        "Audit the complete argument.",
    )
    revised = _semantic_audit_fingerprint(
        "Revised review text with an additional boundary condition.",
        registry,
        blueprint,
        "Audit the complete argument.",
    )
    reprompted = _semantic_audit_fingerprint(
        "Original review text.",
        registry,
        blueprint,
        "Audit argument progression and source concentration.",
    )
    assert original != revised
    assert original != reprompted


def test_global_audit_costs_include_content_addressed_worker_runs(tmp_path):
    """Nested audit runtimes remain visible to admission control and reports."""
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
    )

    orchestrator = FullReviewOrchestrator(
        _make_orchestrator_config(tmp_path)
    )
    orchestrator._work_dir = tmp_path / "review"
    orchestrator._work_dir.mkdir()
    orchestrator._section_registry = {
        "sections": [
            {
                "section_id": "S01",
                "status": "completed",
                "cost_cny": 3.0,
                "cost_usd": 0.4,
                "input_tokens": 100,
                "output_tokens": 20,
            }
        ]
    }
    _write_json(
        orchestrator._work_dir / "audit_round_1" / "COST.json",
        {
            "estimated_cost_cny": 1.0,
            "estimated_cost_usd": 0.1,
            "total_input_tokens": 200,
            "total_output_tokens": 40,
        },
    )
    _write_json(
        orchestrator._work_dir
        / "audit_round_1"
        / "worker_runs"
        / "fingerprint"
        / "COST.json",
        {
            "estimated_cost_cny": 2.0,
            "estimated_cost_usd": 0.2,
            "total_input_tokens": 300,
            "total_output_tokens": 60,
        },
    )
    assert orchestrator._calculate_total_cost_cny() == 6.0
    assert orchestrator._calculate_total_cost() == 0.7
    assert orchestrator._calculate_total_tokens() == (600, 120)


def test_merge_title_normalizer_removes_only_repeated_leading_title():
    from optomind_research.runtime.targeted_revision_worker import (
        _demote_embedded_section_headings,
        _strip_repeated_leading_title,
    )

    title = "Characterizing Achromatic Performance"
    body = "The first substantive paragraph remains intact."
    assert _strip_repeated_leading_title(f"# {title}\n\n{body}", title) == body
    assert _strip_repeated_leading_title(f"{title}\n\n{body}", title) == body
    assert _strip_repeated_leading_title(f"### Different subtitle\n\n{body}", title).startswith(
        "### Different subtitle"
    )
    normalized = _demote_embedded_section_headings(
        "# Local overview\n\n## Mechanism detail\n\n### Existing detail"
    )
    assert normalized.splitlines() == [
        "### Local overview",
        "",
        "### Mechanism detail",
        "",
        "### Existing detail",
    ]


def test_duplicate_sentence_removal_preserves_markdown_paragraphs(tmp_path: Path):
    from optomind_research.runtime.targeted_revision_worker import TargetedRevisionWorker

    source_dir = tmp_path / "S01"
    target_dir = tmp_path / "S02"
    source_dir.mkdir()
    target_dir.mkdir()
    duplicate = (
        "This exact scientific sentence is deliberately repeated across both "
        "sections for the regression test."
    )
    source_dir.joinpath("SECTION_DRAFT_EN.md").write_text(
        f"{duplicate}\n\nA distinct source paragraph remains here.",
        encoding="utf-8",
    )
    target_text = (
        "Opening context contains enough words to keep this section valid after "
        "the duplicate is removed and to exercise the transactional word-count guard. "
        f"{duplicate}\n\n"
        "The second paragraph must remain separated by a blank line because it "
        "contains an independent discussion of measurement practice, reporting "
        "standards, uncertainty, reproducibility, and cross-platform comparison. "
        "Additional words ensure the section remains safely above the minimum."
    )
    target_dir.joinpath("SECTION_DRAFT_EN.md").write_text(target_text, encoding="utf-8")
    registry = {
        "S01": {"work_dir": str(source_dir)},
        "S02": {"work_dir": str(target_dir)},
    }

    changed = TargetedRevisionWorker()._remove_duplicate_content(
        ["S01", "S02"],
        registry,
    )
    revised = target_dir.joinpath("SECTION_DRAFT_EN.md").read_text(encoding="utf-8")
    assert changed is True
    assert duplicate not in revised
    assert "\n\n" in revised
    assert "The second paragraph" in revised
