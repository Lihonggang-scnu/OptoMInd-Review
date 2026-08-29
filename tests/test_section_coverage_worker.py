"""Offline tests for Phase 2: Section Literature Coverage Worker.

No real API calls. All tests run deterministically.
Uses temporary SQLite DBs for KB isolation — production KB is never touched.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

import sys
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "section_coverage"
SECTION_CONTEXT_FIXTURE = FIXTURES_DIR / "section_context.json"


@pytest.fixture(autouse=True)
def _block_external_oa_enrichment(monkeypatch):
    """Keep this entire test module deterministic and network-free."""
    from tools.academic_backends.unpaywall_backend import UnpaywallBackend
    from tools.academic_backends.openalex_backend import OpenAlexBackend

    monkeypatch.setattr(UnpaywallBackend, "lookup", lambda self, doi: None)
    monkeypatch.setattr(OpenAlexBackend, "get_work", lambda self, doi: None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_section_data() -> Dict[str, Any]:
    return json.loads(SECTION_CONTEXT_FIXTURE.read_text(encoding="utf-8"))


def _make_ctx(tmp_path: Path, kb_sqlite: Path | None = None):
    from optomind_research.runtime.tool_provider import SectionCoverageContext
    section_data = _make_section_data()
    temp_kb = tmp_path / "temp_kb.sqlite"
    return SectionCoverageContext(
        section_id=section_data["section_id"],
        section_data=section_data,
        kb_sqlite=kb_sqlite,
        temp_kb_sqlite=temp_kb,
        work_dir=tmp_path,
    )


def _seed_fake_kb(kb_path: Path, paper_count: int = 5, role_hint: str = "foundation") -> List[str]:
    """Create a minimal SQLite KB with fake papers and text_chunks for testing."""
    with sqlite3.connect(str(kb_path)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS papers (
                paper_id TEXT PRIMARY KEY,
                title TEXT,
                abstract TEXT,
                year INTEGER,
                venue TEXT
            );
            CREATE TABLE IF NOT EXISTS text_chunks (
                chunk_id TEXT PRIMARY KEY,
                paper_id TEXT,
                text TEXT,
                section_path TEXT
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS text_chunk_fts
                USING fts5(chunk_id UNINDEXED, text, content='text_chunks', content_rowid='rowid');
            CREATE VIRTUAL TABLE IF NOT EXISTS paper_fts
                USING fts5(paper_id UNINDEXED, title, abstract, content='papers', content_rowid='rowid');
        """)
        paper_ids = []
        for i in range(paper_count):
            pid = f"paper_{uuid.uuid4().hex[:8]}"
            title = f"Study of {role_hint} nonlinear waveguide effects {i}"
            abstract = (
                f"This paper presents {role_hint} research on "
                f"nonlinear optical {role_hint} in photonic crystal waveguides. "
                f"We demonstrate phase matching and Kerr effect analysis."
            )
            conn.execute(
                "INSERT OR IGNORE INTO papers VALUES (?,?,?,?,?)",
                (pid, title, abstract, 2020 + i, "Optics Express"),
            )
            chunk_id = f"chunk_{pid}_0"
            conn.execute(
                "INSERT OR IGNORE INTO text_chunks VALUES (?,?,?,?)",
                (chunk_id, pid, abstract, "introduction"),
            )
            conn.execute(
                "INSERT INTO text_chunk_fts(chunk_id, text) VALUES (?,?)",
                (chunk_id, abstract),
            )
            conn.execute(
                "INSERT INTO paper_fts(paper_id, title, abstract) VALUES (?,?,?)",
                (pid, title, abstract),
            )
            paper_ids.append(pid)
    return paper_ids


# ---------------------------------------------------------------------------
# SC-T1: Artifact schema validation
# ---------------------------------------------------------------------------

def test_artifact_schemas_validate():
    """All Phase 2 Pydantic schemas construct and round-trip correctly."""
    from optomind_research.runtime.artifact_schemas import (
        SectionContext, SectionCoveragePlan, LocalCoverageAudit,
        OACandidateLedger, OACandidate, MaterializationManifest,
        MaterializedPaper, SectionSourceLedger, SourceEntry,
        SectionGapReport, GapEntry, SectionMaterialPackage,
        CoverageRole, RolePriority, ScopeFit, AcquisitionStatus,
    )

    sc = SectionContext(
        section_id="S03",
        section_title="Nonlinear Optics",
        chapter_argument="chi-3 effects",
        scope_description="integrated photonics",
        required_roles=[CoverageRole.foundation, CoverageRole.mechanism],
    )
    assert sc.schema_version == "2.0"
    assert sc.model_dump()["section_id"] == "S03"

    plan_data = SectionCoveragePlan(section_id="S03", chapter_argument="test")
    assert plan_data.roles == {}

    ledger = OACandidateLedger(section_id="S03")
    cand = OACandidate(
        candidate_id="cand_abc",
        section_id="S03",
        role="foundation",
        title="Test Paper",
        doi="10.1234/test",
        scope_fit=ScopeFit.direct,
    )
    ledger.candidates.append(cand)
    assert ledger.approved() == []
    cand.decision = "approved"
    assert len(ledger.approved()) == 1

    manifest = MaterializationManifest(section_id="S03")
    mp = MaterializedPaper(
        candidate_id="cand_abc",
        paper_id="doi:10.1234/test",
        acquisition_status=AcquisitionStatus.abstract_only,
    )
    manifest.papers.append(mp)
    assert manifest.model_dump()["papers"][0]["acquisition_status"] == "abstract_only"

    gap = GapEntry(role="frontier", severity="blocking",
                   description="No frontier papers found")
    report = SectionGapReport(section_id="S03", gaps=[gap])
    assert report.model_dump()["gaps"][0]["role"] == "frontier"


# ---------------------------------------------------------------------------
# SC-T2: ToolProvider exposes exactly 11 tools
# ---------------------------------------------------------------------------

def test_section_coverage_tool_provider_exposes_14_tools(tmp_path: Path):
    """Provider exposes local-candidate inspection/audit separately from OA audit."""
    from optomind_research.runtime.section_coverage_tool_registry import (
        SectionCoverageToolProvider, SECTION_COVERAGE_TOOL_NAMES,
    )
    ctx = _make_ctx(tmp_path)
    provider = SectionCoverageToolProvider(ctx)
    tools = provider.get_tools(tmp_path)
    names = [t.name for t in tools]

    assert len(tools) == 14
    for expected_name in SECTION_COVERAGE_TOOL_NAMES:
        assert expected_name in names, f"Tool {expected_name!r} missing from provider"


# ---------------------------------------------------------------------------
# SC-T3: ResearchWorker accepts tool_provider without breaking Phase 1
# ---------------------------------------------------------------------------

def test_research_worker_accepts_tool_provider_without_breaking_phase1(tmp_path: Path):
    """ResearchWorker with tool_provider=None behaves identically to Phase 1."""
    from optomind_research.runtime.research_worker import ResearchWorker
    import inspect
    sig = inspect.signature(ResearchWorker.__init__)
    assert "tool_provider" in sig.parameters, (
        "ResearchWorker.__init__ must accept tool_provider parameter"
    )
    # Default must be None (backward compat)
    default = sig.parameters["tool_provider"].default
    assert default is None, f"tool_provider default must be None, got: {default}"


# ---------------------------------------------------------------------------
# SC-T4: tool_provider tools appear in worker's canonical tool list
# ---------------------------------------------------------------------------

def test_provider_tools_added_to_worker_canonical_list(tmp_path: Path):
    """When tool_provider is set, its tool names are added to canonical_tools."""
    from optomind_research.runtime.section_coverage_tool_registry import (
        SectionCoverageToolProvider, SECTION_COVERAGE_TOOL_NAMES,
    )
    from optomind_research.runtime.research_worker import _resolve_tool_names, _TOOL_ALIAS_MAP
    ctx = _make_ctx(tmp_path)
    provider = SectionCoverageToolProvider(ctx)
    # Provider names are NOT in the base alias map
    for name in SECTION_COVERAGE_TOOL_NAMES:
        assert name not in _TOOL_ALIAS_MAP, (
            f"Provider tool {name!r} should not be in the base alias map"
        )
    # But they are valid identifiers and would pass through _resolve_tool_names
    resolved = _resolve_tool_names(SECTION_COVERAGE_TOOL_NAMES)
    assert resolved == SECTION_COVERAGE_TOOL_NAMES


# ---------------------------------------------------------------------------
# SC-T5: load_section_context writes SECTION_CONTEXT.json
# ---------------------------------------------------------------------------

def test_load_section_context_writes_artifact(tmp_path: Path):
    """load_section_context must write SECTION_CONTEXT.json with correct fields."""
    from optomind_research.runtime.section_coverage_tool_registry import _make_load_section_context
    ctx = _make_ctx(tmp_path)
    fn = _make_load_section_context(ctx)
    result = fn()
    data = json.loads(result)

    assert data["status"] == "ok"
    assert data["section_id"] == "S03"
    assert (tmp_path / "SECTION_CONTEXT.json").exists()

    artifact = json.loads((tmp_path / "SECTION_CONTEXT.json").read_text())
    assert artifact["section_id"] == "S03"
    assert "chapter_argument" in artifact
    assert "scope_guardrails" in artifact


# ---------------------------------------------------------------------------
# SC-T6: Local KB priority — inspect_section_local_coverage before search
# ---------------------------------------------------------------------------

def test_inspect_local_coverage_before_external_search(tmp_path: Path):
    """inspect_section_local_coverage must query local KB and return gap analysis."""
    from optomind_research.runtime.section_coverage_tool_registry import _make_inspect_section_local_coverage
    kb = tmp_path / "test_kb.sqlite"
    _seed_fake_kb(kb, paper_count=4, role_hint="foundation nonlinear phase matching")
    ctx = _make_ctx(tmp_path, kb_sqlite=kb)
    fn = _make_inspect_section_local_coverage(ctx)
    result = json.loads(fn())

    assert result["status"] == "ok"
    assert "blocking_gaps" in result
    assert "sufficient_roles" in result
    assert "role_summary" in result
    # With 4 papers relevant to foundation, at least one role should show partial/sufficient
    assert len(result["role_summary"]) == 6

    # ARTIFACT must be written
    assert (tmp_path / "LOCAL_COVERAGE_AUDIT.json").exists()
    audit = json.loads((tmp_path / "LOCAL_COVERAGE_AUDIT.json").read_text())
    assert "role_audits" in audit


def test_inspect_local_coverage_no_kb_returns_all_blocking(tmp_path: Path):
    """With no KB, inspect_section_local_coverage reports only required_roles as blocking."""
    from optomind_research.runtime.section_coverage_tool_registry import _make_inspect_section_local_coverage
    ctx = _make_ctx(tmp_path, kb_sqlite=None)
    fn = _make_inspect_section_local_coverage(ctx)
    result = json.loads(fn())
    assert result["status"] == "ok"
    section_data = _make_section_data()
    expected_blocking = section_data.get("required_roles", [])
    assert len(result["blocking_gaps"]) == len(expected_blocking), (
        f"blocking_gaps should equal required_roles count ({len(expected_blocking)}), "
        f"got {result['blocking_gaps']}"
    )


# ---------------------------------------------------------------------------
# SC-T7: Six-role plan required by submit_literature_role_plan
# ---------------------------------------------------------------------------

def test_submit_literature_role_plan_requires_all_six_roles(tmp_path: Path):
    """submit_literature_role_plan must reject plans missing any of the six roles."""
    from optomind_research.runtime.section_coverage_tool_registry import _make_submit_literature_role_plan
    ctx = _make_ctx(tmp_path)
    fn = _make_submit_literature_role_plan(ctx)

    # Incomplete plan (missing 'controversy' and 'application')
    incomplete = {
        "foundation": {"priority": "required", "coverage_question": "Q1", "intended_synthesis": "S1", "queries": ["q1"]},
        "mechanism": {"priority": "required", "coverage_question": "Q2", "intended_synthesis": "S2", "queries": ["q2"]},
        "method": {"priority": "important", "coverage_question": "Q3", "intended_synthesis": "S3", "queries": []},
        "frontier": {"priority": "required", "coverage_question": "Q4", "intended_synthesis": "S4", "queries": ["q4"]},
    }
    result = json.loads(fn(json.dumps(incomplete)))
    assert result["status"] == "error"
    assert any("controversy" in e or "application" in e for e in result["errors"])

    # Complete plan
    complete = {role: {"priority": "useful", "coverage_question": f"Q for {role}",
                       "intended_synthesis": f"S for {role}", "queries": [f"query {role}"]}
                for role in ("foundation", "mechanism", "method", "frontier", "controversy", "application")}
    complete["foundation"]["priority"] = "required"
    result2 = json.loads(fn(json.dumps(complete)))
    assert result2["status"] == "ok"
    assert (tmp_path / "SECTION_COVERAGE_PLAN.json").exists()
    assert set(result2["roles_planned"]) == {"foundation", "mechanism", "method", "frontier", "controversy", "application"}


# ---------------------------------------------------------------------------
# SC-T8: External candidate audit — scope/role decisions validated
# ---------------------------------------------------------------------------

def test_submit_candidate_audit_validates_decisions(tmp_path: Path):
    """submit_candidate_audit must update ledger with agent decisions."""
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_submit_candidate_audit,
        _append_candidates_to_ledger,
    )
    ctx = _make_ctx(tmp_path)

    # Seed a candidate
    cand = {
        "candidate_id": "cand_test001",
        "section_id": "S03",
        "role": "foundation",
        "title": "Nonlinear Kerr Effect in Silicon Photonics",
        "doi": "10.9999/test.001",
        "abstract": "This paper describes chi-3 Kerr effect in silicon waveguides.",
        "is_oa": True,
        "backends": ["openalex"],
        "query_texts": ["nonlinear Kerr silicon"],
    }
    ctx.register_candidates([cand])
    _append_candidates_to_ledger(tmp_path, "S03", [cand])

    fn = _make_submit_candidate_audit(ctx)
    audit_payload = json.dumps([{
        "candidate_id": "cand_test001",
        "scope_fit": "direct",
        "role_fit": ["foundation", "mechanism"],
        "decision": "approved",
        "audit_reason": "Directly discusses chi-3 Kerr effect in photonic platform",
        "not_usable_for": ["exact conversion efficiency claims without experimental setup"],
    }])

    result = json.loads(fn(audit_payload))
    assert result["status"] == "ok"
    assert result["updated"] == 1
    assert result["approved"] == 1
    assert "cand_test001" in result["approved_ids"]

    # Verify ledger updated
    ledger_data = json.loads((tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8"))
    updated_cand = next(c for c in ledger_data["candidates"] if c["candidate_id"] == "cand_test001")
    assert updated_cand["scope_fit"] == "direct"
    assert updated_cand["decision"] == "approved"
    assert len(updated_cand["not_usable_for"]) > 0


# ---------------------------------------------------------------------------
# SC-T9: Candidate audit rejects off-scope candidates
# ---------------------------------------------------------------------------

def test_audit_rejects_out_of_scope_candidates(tmp_path: Path):
    """Out-of-scope candidates must not be approved."""
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_submit_candidate_audit, _append_candidates_to_ledger,
    )
    ctx = _make_ctx(tmp_path)

    cand = {
        "candidate_id": "cand_oos001",
        "section_id": "S03",
        "role": "foundation",
        "title": "THz Nonlinear Effects in Bulk Crystals",
        "doi": "10.9999/oos.001",
        "abstract": "THz nonlinear effects in bulk GaAs crystals.",
        "is_oa": False,
        "backends": ["semantic_scholar"],
        "query_texts": ["nonlinear bulk crystal"],
    }
    ctx.register_candidates([cand])
    _append_candidates_to_ledger(tmp_path, "S03", [cand])

    fn = _make_submit_candidate_audit(ctx)
    audit_payload = json.dumps([{
        "candidate_id": "cand_oos001",
        "scope_fit": "out_of_scope",
        "role_fit": [],
        "decision": "rejected",
        "audit_reason": "THz bulk crystal — excluded by scope guardrail",
    }])

    result = json.loads(fn(audit_payload))
    assert result["approved"] == 0
    assert result["updated"] == 1

    ledger_data = json.loads((tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8"))
    c = next(c for c in ledger_data["candidates"] if c["candidate_id"] == "cand_oos001")
    assert c["decision"] == "rejected"
    assert c["scope_fit"] == "out_of_scope"


# ---------------------------------------------------------------------------
# SC-T10: No fabricated IDs in candidate store
# ---------------------------------------------------------------------------

def test_no_fabricated_candidate_ids(tmp_path: Path):
    """Candidate IDs returned by search must exist in the session store."""
    from optomind_research.runtime.section_coverage_tool_registry import _make_inspect_candidate_batch
    ctx = _make_ctx(tmp_path)

    # Register one real candidate
    ctx.register_candidates([{
        "candidate_id": "cand_real001",
        "section_id": "S03",
        "role": "method",
        "title": "Real Paper",
        "doi": "10.1/real",
        "abstract": "Real abstract.",
    }])

    fn = _make_inspect_candidate_batch(ctx)
    result = json.loads(fn('["cand_real001", "cand_FABRICATED"]'))

    assert result["status"] == "ok"
    assert result["found"] == 1
    assert "cand_FABRICATED" in result["missing"]
    assert result["candidates"][0]["candidate_id"] == "cand_real001"


# ---------------------------------------------------------------------------
# SC-T11: Transaction failure — failed ingest does not pollute staging KB
# ---------------------------------------------------------------------------

def test_failed_ingest_does_not_pollute_staging_kb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """If paper acquisition fails, no partial row must remain in the staging KB."""
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import AcquisitionStatus, OACandidate, OACandidateLedger, CandidateDecision, ScopeFit

    ctx = _make_ctx(tmp_path)
    oa_url = "https://oa.example/noaccess.pdf"

    # Seed an approved candidate with a declared legal OA route.  The route is
    # admitted, then the downstream ingest is forced to fail.
    cand = {
        "candidate_id": "cand_fail001",
        "section_id": "S03",
        "role": "frontier",
        "title": "Inaccessible Paper",
        "doi": "10.9999/noaccess",
        "abstract": "An abstract with a declared but inaccessible OA route.",
        "is_oa": True,
        "oa_url": oa_url,
        "pdf_url": oa_url,
        "backends": ["openalex"],
        "query_texts": ["frontier nonlinear"],
        "scope_fit": "direct",
    }
    ctx.register_candidates([cand])

    # Write approved ledger entry
    ledger = OACandidateLedger(section_id="S03")
    ledger.candidates.append(OACandidate(
        candidate_id="cand_fail001",
        section_id="S03",
        role="frontier",
        title="Inaccessible Paper",
        doi="10.9999/noaccess",
        is_oa=True,
        oa_url=oa_url,
        pdf_url=oa_url,
        decision=CandidateDecision.approved,
        scope_fit=ScopeFit.direct,
    ))
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        json.dumps(ledger.model_dump()), encoding="utf-8"
    )

    def _fail_downstream(candidate, temp_kb, work_dir):
        del temp_kb, work_dir
        return {
            "acquisition_status": "failed",
            "paper_id": "",
            "chunk_ids": [],
            "new_chunk_ids": [],
            "new_paper": False,
            "paper_row_inserted": False,
            "new_chunks": 0,
            "reused_chunks": 0,
            "download_url": "",
            "download_error": "downstream OA ingest failure",
            "attempted_urls": [candidate["oa_url"]],
            "download_errors_by_url": {
                candidate["oa_url"]: "downstream OA ingest failure",
            },
            "content_type_detected": "",
            "parse_failure_reason": "downstream OA ingest failure",
        }

    monkeypatch.setattr(registry, "_ingest_single_candidate", _fail_downstream)
    fn = registry._make_acquire_and_materialize_oa_papers(ctx)
    result = json.loads(fn("frontier", '["cand_fail001"]', max_papers=1))

    assert result["status"] == "ok"
    assert result["attempted_this_call"] == 1
    assert result["failed_this_call"] == 1
    # The manifest must record the attempt
    manifest_data = json.loads((tmp_path / "MATERIALIZATION_MANIFEST.json").read_text())
    papers = manifest_data.get("papers", [])
    assert len(papers) == 1
    # Status must be failed or metadata_only (not fulltext)
    assert papers[0]["acquisition_status"] in ("failed", "metadata_only", "abstract_only")
    assert papers[0]["attempted_urls"] == [oa_url]
    assert papers[0]["download_errors_by_url"][oa_url] == "downstream OA ingest failure"

    # Staging KB must not have a partial row with NULL paper_id
    if ctx.temp_kb_sqlite.exists():
        with sqlite3.connect(str(ctx.temp_kb_sqlite)) as conn:
            try:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM text_chunks WHERE paper_id IS NULL OR paper_id = ''"
                ).fetchone()
                assert rows[0] == 0, "Staging KB has orphaned chunks with no paper_id"
            except sqlite3.OperationalError:
                pass  # table not created yet — also fine


# ---------------------------------------------------------------------------
# SC-T12: Idempotent re-run — same paper not duplicated in manifest
# ---------------------------------------------------------------------------

def test_idempotent_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Calling acquire_and_materialize_oa_papers twice for the same candidate must not duplicate entries."""
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import OACandidateLedger, OACandidate, CandidateDecision, ScopeFit

    ctx = _make_ctx(tmp_path)
    oa_url = "https://oa.example/idempotent.pdf"
    cand = {
        "candidate_id": "cand_idem001",
        "section_id": "S03",
        "role": "foundation",
        "title": "Idempotent Test Paper",
        "doi": "10.1/idem",
        "abstract": "abstract",
        "is_oa": True,
        "oa_url": oa_url,
        "pdf_url": oa_url,
        "backends": ["openalex"],
        "query_texts": ["test"],
        "scope_fit": "adjacent",
    }
    ctx.register_candidates([cand])
    ledger = OACandidateLedger(section_id="S03")
    ledger.candidates.append(OACandidate(
        candidate_id="cand_idem001", section_id="S03", role="foundation",
        title="Idempotent Test Paper", doi="10.1/idem",
        is_oa=True, oa_url=oa_url, pdf_url=oa_url,
        decision=CandidateDecision.approved, scope_fit=ScopeFit.adjacent,
    ))
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        json.dumps(ledger.model_dump()), encoding="utf-8"
    )

    def _succeed_downstream(candidate, temp_kb, work_dir):
        del temp_kb, work_dir
        return {
            "acquisition_status": "fulltext",
            "paper_id": "paper_idem001",
            "chunk_ids": ["chunk_idem001"],
            "new_chunk_ids": ["chunk_idem001"],
            "new_paper": True,
            "paper_row_inserted": True,
            "new_chunks": 1,
            "reused_chunks": 0,
            "download_url": candidate["oa_url"],
            "download_error": "",
            "attempted_urls": [candidate["oa_url"]],
            "download_errors_by_url": {},
            "content_type_detected": "application/pdf",
            "parse_failure_reason": "",
        }

    monkeypatch.setattr(registry, "_ingest_single_candidate", _succeed_downstream)
    fn = registry._make_acquire_and_materialize_oa_papers(ctx)
    fn("foundation", '["cand_idem001"]', max_papers=1)
    fn("foundation", '["cand_idem001"]', max_papers=1)  # second call

    manifest = json.loads((tmp_path / "MATERIALIZATION_MANIFEST.json").read_text())
    assert len(manifest["papers"]) == 1
    ids_in_manifest = [p["candidate_id"] for p in manifest["papers"]]
    assert ids_in_manifest.count("cand_idem001") == 1, (
        "cand_idem001 appears more than once in materialization manifest — not idempotent"
    )


# ---------------------------------------------------------------------------
# SC-T14: _restore_candidates_from_ledger repopulates in-memory store from disk
# ---------------------------------------------------------------------------

def test_restore_candidates_from_ledger(tmp_path: Path):
    """Candidates persisted to OA_CANDIDATE_LEDGER.json must be restored into a
    fresh in-memory store — simulates process restart between search and acquire."""
    from optomind_research.runtime.section_coverage_tool_registry import (
        _append_candidates_to_ledger,
        _restore_candidates_from_ledger,
    )

    ctx = _make_ctx(tmp_path)
    assert ctx.get_candidate("cand_r001") is None

    # Persist directly to ledger without registering in-memory
    cand = {
        "candidate_id": "cand_r001",
        "section_id": "S03",
        "role": "foundation",
        "title": "Restart Test Paper",
        "doi": "10.1/restart",
        "abstract": "abstract",
    }
    _append_candidates_to_ledger(tmp_path, "S03", [cand])

    # Still not in memory
    assert ctx.get_candidate("cand_r001") is None

    # Restore
    count = _restore_candidates_from_ledger(ctx)
    assert count == 1
    assert ctx.get_candidate("cand_r001") is not None
    assert ctx.get_candidate("cand_r001")["title"] == "Restart Test Paper"

    # Idempotent: second restore adds nothing
    count2 = _restore_candidates_from_ledger(ctx)
    assert count2 == 0


# ---------------------------------------------------------------------------
# SC-T15: Section-aware KB queries — anchor terms derived from section data
# ---------------------------------------------------------------------------

def test_query_kb_for_role_uses_section_anchor_terms(tmp_path: Path):
    """_query_kb_for_role with section_data must use section-specific anchor terms
    so that topically relevant papers are retrieved.

    Strategy: seed a KB whose content exactly matches S03 anchor words (nonlinear,
    optical, photonic, waveguide). With section_data supplied those anchors are
    prepended to the FTS query → results returned. Without section_data only the
    generic role term is used → fewer or no matches (role term alone is less
    specific). We assert that at least *some* results are returned when using
    section_data, confirming the anchor path executes without error.
    """
    from optomind_research.runtime.section_coverage_tool_registry import _query_kb_for_role

    section_data = _make_section_data()
    kb = tmp_path / "anchor_test.sqlite"
    # Seed with content containing S03-specific technical terms + foundation role keyword
    _seed_fake_kb(kb, paper_count=4, role_hint="foundation nonlinear optical waveguide photonic Kerr")

    results_with = _query_kb_for_role(kb, "S03", "foundation", top_k=8, section_data=section_data)
    # Must not raise and must return results for a well-seeded KB
    assert isinstance(results_with, list)
    assert len(results_with) > 0, (
        "_query_kb_for_role returned no results when section_data was supplied — "
        "anchor terms may have caused an FTS parse error"
    )
    # Each hit must have the expected keys
    for h in results_with:
        assert "paper_id" in h
        assert "title" in h


def test_query_kb_role_propagates_permission_fields_to_local_candidate_ledger(
    tmp_path: Path,
):
    """Permission and route metadata survive local recall for downstream audit."""
    from optomind_research.runtime.section_coverage_tool_registry import (
        _query_kb_for_role,
        _register_local_hits,
    )

    kb = tmp_path / "permission_query.sqlite"
    _seed_fake_kb(kb, paper_count=1, role_hint="foundation nonlinear optical waveguide photonic")
    allowed = json.dumps([
        "paper_reported_claim", "background", "trend", "candidate_lead",
        "author_synthesis",
    ])
    with sqlite3.connect(str(kb)) as conn:
        for statement in (
            "ALTER TABLE text_chunks ADD COLUMN evidence_level TEXT",
            "ALTER TABLE text_chunks ADD COLUMN content_depth TEXT",
            "ALTER TABLE text_chunks ADD COLUMN use_permission TEXT",
            "ALTER TABLE text_chunks ADD COLUMN source_kind TEXT",
            "ALTER TABLE text_chunks ADD COLUMN discovery_route TEXT",
            "ALTER TABLE text_chunks ADD COLUMN materialization_route TEXT",
            "ALTER TABLE text_chunks ADD COLUMN route_provenance_json TEXT",
            "ALTER TABLE text_chunks ADD COLUMN context_complete INTEGER",
            "ALTER TABLE text_chunks ADD COLUMN allowed_claim_kinds_json TEXT",
        ):
            conn.execute(statement)
        conn.execute(
            "UPDATE text_chunks SET evidence_level=?, content_depth=?, "
            "use_permission=?, source_kind=?, discovery_route=?, "
            "materialization_route=?, route_provenance_json=?, "
            "context_complete=?, allowed_claim_kinds_json=?",
            (
                "abstract_claim",
                "abstract_claim",
                "contextual_or_qualified_support",
                "abstract",
                "semantic_scholar_abstract",
                "semantic_scholar_abstract_claim",
                json.dumps({"migration": "not_r32"}),
                0,
                allowed,
            ),
        )
        conn.commit()

    section_data = _make_section_data()
    hits = _query_kb_for_role(
        kb, "S03", "foundation", top_k=4, section_data=section_data
    )
    assert hits
    hit = hits[0]
    assert hit["content_depth"] == "abstract_claim"
    assert hit["use_permission"] == "contextual_or_qualified_support"
    assert hit["source_kind"] == "abstract"
    assert hit["discovery_route"] == "semantic_scholar_abstract"
    assert hit["materialization_route"] == "semantic_scholar_abstract_claim"
    assert hit["context_complete"] is False
    assert "paper_reported_claim" in hit["allowed_claim_kinds"]
    assert hit["permission_contract_present"] is True

    ctx = _make_ctx(tmp_path, kb_sqlite=kb)
    registered = _register_local_hits(
        ctx, "foundation", hits, retrieval_query="test permission fields"
    )
    assert registered
    candidate = registered[0]
    assert candidate["content_depth"] == "abstract_claim"
    assert candidate["use_permission"] == "contextual_or_qualified_support"
    assert candidate["source_kind"] == "abstract"
    assert candidate["context_complete"] is False
    assert "paper_reported_claim" in candidate["allowed_claim_kinds"]
    assert candidate["canonical_chunk_ids"] == [candidate["chunk_id"]]
    assert candidate["permission_contract_present"] is True


@pytest.mark.parametrize(
    ("role", "claim_kind"),
    [
        ("mechanism", "mechanism"),
        ("method", "method"),
        ("controversy", "controversy"),
    ],
)
def test_abstract_keywords_do_not_close_high_strength_roles_but_text_does(
    role: str,
    claim_kind: str,
):
    from optomind_research.runtime.section_coverage_tool_registry import (
        _role_coverage_sources,
    )

    abstract_sources = [
        {
            "paper_id": f"abstract_{index}",
            "literature_role": role,
            "canonical_chunk_ids": [f"abstract_chunk_{index}"],
            "content_depth": "abstract_claim",
            "use_permission": "contextual_or_qualified_support",
            "context_complete": False,
            "allowed_claim_kinds": [
                "paper_reported_claim", "background", "trend",
                "candidate_lead", "author_synthesis",
            ],
            "scope_fit": "direct",
            "text": f"The abstract mentions a {claim_kind}.",
        }
        for index in range(3)
    ]
    assert _role_coverage_sources(abstract_sources, role) == []

    fulltext_sources = [
        {
            **source,
            "paper_id": f"fulltext_{index}",
            "canonical_chunk_ids": [f"fulltext_chunk_{index}"],
            "content_depth": "fulltext",
            "use_permission": "factual_support",
            "context_complete": True,
            "allowed_claim_kinds": [claim_kind],
        }
        for index, source in enumerate(abstract_sources)
    ]
    assert len(_role_coverage_sources(fulltext_sources, role)) == 3

    structured_sources = [
        {
            **source,
            "paper_id": f"structured_{index}",
            "canonical_chunk_ids": [f"structured_chunk_{index}"],
            "content_depth": "structured_snippet",
            "use_permission": "factual_support",
            "context_complete": True,
            "allowed_claim_kinds": [claim_kind],
        }
        for index, source in enumerate(abstract_sources)
    ]
    assert len(_role_coverage_sources(structured_sources, role)) == 3


def test_ordinary_abstract_remains_eligible_for_foundation_background():
    from optomind_research.runtime.section_coverage_tool_registry import (
        _role_material_is_coverage_eligible,
    )

    assert _role_material_is_coverage_eligible({
        "paper_id": "abstract_background",
        "literature_role": "foundation",
        "canonical_chunk_ids": ["chunk_abstract"],
        "content_depth": "abstract",
        "use_permission": "background_and_candidate_only",
        "allowed_claim_kinds": ["background", "trend"],
        "context_complete": False,
        "scope_fit": "direct",
    }, "foundation") is True


# ---------------------------------------------------------------------------
# SC-T16: _build_source_ledger includes local KB sources (local_prior=True)
# ---------------------------------------------------------------------------

def test_build_source_ledger_includes_local_kb_sources(tmp_path: Path):
    """_build_source_ledger must include papers already in the local KB as
    local_prior=True entries, so validate_section_coverage_package counts them."""
    from optomind_research.runtime.section_coverage_tool_registry import (
        _build_source_ledger,
        _make_inspect_section_local_coverage,
        _make_submit_local_source_audit,
    )

    kb = tmp_path / "local_kb.sqlite"
    _seed_fake_kb(kb, paper_count=3, role_hint="foundation nonlinear optical waveguide Kerr phase")
    ctx = _make_ctx(tmp_path, kb_sqlite=kb)
    _make_inspect_section_local_coverage(ctx)()
    candidates = json.loads(
        (tmp_path / "LOCAL_CANDIDATE_LEDGER.json").read_text(encoding="utf-8")
    )["candidates"]
    decisions = [{
        "candidate_id": item["candidate_id"],
        "scope_fit": "direct",
        "decision": "approved",
        "audit_reason": "Exact local excerpt is in scope and performs the recalled role.",
    } for item in candidates]
    _make_submit_local_source_audit(ctx)(json.dumps(decisions))

    _build_source_ledger(ctx)

    ledger_data = json.loads((tmp_path / "SECTION_SOURCE_LEDGER.json").read_text(encoding="utf-8"))
    sources = ledger_data.get("sources", [])
    local_prior = [s for s in sources if s.get("local_prior")]
    assert len(local_prior) > 0, "No local_prior sources in SECTION_SOURCE_LEDGER.json"
    assert ledger_data.get("local_prior_sources", 0) > 0
    # All local_prior entries must have a paper_id
    for s in local_prior:
        assert s.get("paper_id"), "local_prior source has no paper_id"


def test_source_ledger_distinguishes_method_transfer_fulltext_from_abstract(tmp_path: Path):
    """Use policy must not overwrite the physical acquisition format."""
    from optomind_research.runtime.section_coverage_tool_registry import _build_source_ledger

    def run_case(case_name: str, ingest_source: str) -> set[str]:
        case_dir = tmp_path / case_name
        case_dir.mkdir()
        kb = case_dir / "kb.sqlite"
        _seed_fake_kb(
            kb, paper_count=1,
            role_hint="foundation nonlinear optical waveguide photonic Kerr",
        )
        with sqlite3.connect(kb) as conn:
            conn.execute("ALTER TABLE text_chunks ADD COLUMN evidence_level TEXT")
            conn.execute("ALTER TABLE text_chunks ADD COLUMN source_kind TEXT")
            conn.execute("ALTER TABLE text_chunks ADD COLUMN raw_json TEXT")
            conn.execute(
                "UPDATE text_chunks SET evidence_level=?,source_kind=?,raw_json=?",
                ("background", "method_transfer", json.dumps({"ingest_source": ingest_source})),
            )
        ctx = _make_ctx(case_dir, kb_sqlite=kb)
        from optomind_research.runtime.section_coverage_tool_registry import (
            _make_inspect_section_local_coverage,
            _make_submit_local_source_audit,
        )
        _make_inspect_section_local_coverage(ctx)()
        candidates = json.loads(
            (case_dir / "LOCAL_CANDIDATE_LEDGER.json").read_text(encoding="utf-8")
        )["candidates"]
        _make_submit_local_source_audit(ctx)(json.dumps([{
            "candidate_id": item["candidate_id"],
            "scope_fit": "direct",
            "decision": "approved",
            "audit_reason": "Test fixture is directly in scope.",
        } for item in candidates]))
        _build_source_ledger(ctx)
        data = json.loads((case_dir / "SECTION_SOURCE_LEDGER.json").read_text(encoding="utf-8"))
        return {
            source["acquisition_status"] for source in data["sources"]
            if source.get("local_prior") and source.get("canonical_chunk_ids")
        }

    assert "fulltext" in run_case("fulltext", "m3_real_oa")
    assert "abstract_only" in run_case("abstract", "m3_real_abstract_fallback")


# ---------------------------------------------------------------------------
# SC-T17: Fail-closed acquisition — unapproved candidates are never ingested
# ---------------------------------------------------------------------------

def test_acquisition_fail_closed_requires_explicit_approval(tmp_path: Path):
    """acquire_and_materialize_oa_papers must skip all candidates that are not
    explicitly approved in the ledger — even when approved_set is empty."""
    from optomind_research.runtime.section_coverage_tool_registry import _make_acquire_and_materialize_oa_papers
    from optomind_research.runtime.artifact_schemas import OACandidateLedger, OACandidate

    ctx = _make_ctx(tmp_path)

    # Register candidates but leave decision at default (deferred, not approved)
    cands = [
        {
            "candidate_id": f"cand_fc{i:03d}",
            "section_id": "S03",
            "role": "foundation",
            "title": f"Unapproved Paper {i}",
            "doi": f"10.1/fc{i}",
            "abstract": "abstract",
            "is_oa": True,
            "oa_url": "http://example.com/paper.pdf",
            "scope_fit": "direct",
        }
        for i in range(3)
    ]
    ctx.register_candidates(cands)

    # Write ledger with 'deferred' (not 'approved') decisions
    ledger = OACandidateLedger(section_id="S03")
    for c in cands:
        ledger.candidates.append(OACandidate(
            candidate_id=c["candidate_id"],
            section_id="S03",
            role="foundation",
            title=c["title"],
            doi=c["doi"],
            # decision defaults to 'deferred'
        ))
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        json.dumps(ledger.model_dump()), encoding="utf-8"
    )

    fn = _make_acquire_and_materialize_oa_papers(ctx)
    result = json.loads(fn("foundation", json.dumps([c["candidate_id"] for c in cands]), max_papers=5))

    assert result["status"] == "ok"
    assert result["materialized_this_call"] == 0, (
        "Deferred candidates must not be materialized — fail-closed violated"
    )

    # Also test the empty-approved_set case (no ledger file at all)
    ctx2 = _make_ctx(tmp_path / "sub", kb_sqlite=None)
    ctx2.work_dir.mkdir(parents=True, exist_ok=True)
    ctx2.register_candidates(cands)
    fn2 = _make_acquire_and_materialize_oa_papers(ctx2)
    result2 = json.loads(fn2("foundation", json.dumps([c["candidate_id"] for c in cands]), max_papers=5))
    assert result2["materialized_this_call"] == 0, (
        "No-ledger case must also be fail-closed (approved_set empty → no ingest)"
    )


# ---------------------------------------------------------------------------
# SC-T18: _ingest_single_candidate uses kb_sqlite constructor param (not db_path)
# ---------------------------------------------------------------------------

def test_ingest_single_candidate_uses_correct_constructor(tmp_path: Path):
    """_ingest_single_candidate must call KBIngester(kb_sqlite=...) — never db_path=...
    Smoke test: call with a metadata-only candidate, verify no TypeError from wrong kwarg."""
    from optomind_research.runtime.section_coverage_tool_registry import _ingest_single_candidate

    cand = {
        "candidate_id": "cand_ctor001",
        "title": "KBIngester Constructor Test",
        "doi": "10.1/ctor",
        "year": 2023,
        "venue": "Optics Letters",
        "abstract": "Testing constructor parameter.",
        "is_oa": False,
        "oa_url": "",
        "pdf_url": "",
        "backends": ["openalex"],
        "query_texts": ["nonlinear waveguide"],
        "scope_fit": "direct",
        "citation_count": 5,
    }

    result = _ingest_single_candidate(cand, tmp_path / "ctor_test.sqlite", tmp_path)

    # Must return a dict without raising TypeError
    assert isinstance(result, dict)
    assert "acquisition_status" in result
    assert "paper_id" in result
    # acquisition_status must be one of the valid values
    assert result["acquisition_status"] in ("fulltext", "abstract_only", "metadata_only", "failed")


# ---------------------------------------------------------------------------
# SC-T19: Acquisition status determined by chunk ID pattern
# ---------------------------------------------------------------------------

def test_acquisition_status_detected_from_chunk_id_pattern(tmp_path: Path):
    """_ingest_single_candidate must derive acquisition_status from the chunk ID
    pattern returned by KBIngester, NOT from a .stats field:
      - No chunks → metadata_only
      - Single chunk ending ':abstract' → abstract_only
      - One or more fulltext chunks → fulltext
    """
    from optomind_research.runtime.section_coverage_tool_registry import _ingest_single_candidate
    import unittest.mock as mock

    base_cand = {
        "candidate_id": "cand_pat001",
        "title": "Chunk Pattern Test",
        "doi": "10.1/pat",
        "abstract": "Testing chunk pattern detection.",
        "is_oa": True,
        "oa_url": "http://example.com/paper.pdf",
        "pdf_url": "http://example.com/paper.pdf",
        "backends": ["openalex"],
        "query_texts": ["test"],
    }

    # Helper: mock KBIngester.ingest_oa_candidates to return controlled chunk IDs
    def _run_with_chunks(new_chunks, reused_chunks, new_paper_ids=None):
        import optomind_research.m3_kb_ingest as m3
        mock_result = mock.MagicMock()
        mock_result.new_chunk_ids = new_chunks
        mock_result.reused_chunk_ids = reused_chunks
        mock_result.new_paper_ids = new_paper_ids or []

        with mock.patch.object(m3.KBIngester, "ingest_oa_candidates", return_value=mock_result):
            return _ingest_single_candidate(base_cand, tmp_path / "pat_test.sqlite", tmp_path)

    # Case 1: no chunks → metadata_only
    r1 = _run_with_chunks([], [])
    assert r1["acquisition_status"] == "metadata_only", f"Expected metadata_only, got {r1['acquisition_status']}"

    # Case 2: single chunk ending ':abstract' → abstract_only
    doi_slug = "10.1_pat"
    r2 = _run_with_chunks([], [f"m3gap:{doi_slug}:abstract"])
    assert r2["acquisition_status"] == "abstract_only", f"Expected abstract_only, got {r2['acquisition_status']}"

    # Case 3: fulltext chunks (multiple or non-:abstract) → fulltext
    r3 = _run_with_chunks([f"m3gap:{doi_slug}:0000", f"m3gap:{doi_slug}:0001"], [])
    assert r3["acquisition_status"] == "fulltext", f"Expected fulltext, got {r3['acquisition_status']}"


# ---------------------------------------------------------------------------
# SC-T20: _search_openalex field adapter maps backend fields correctly
# ---------------------------------------------------------------------------

def test_search_openalex_field_adapter(tmp_path: Path):
    """_search_openalex must map OpenAlex-backend-specific field names to the
    internal schema: abstract_or_snippet→abstract, journal_or_venue→venue,
    open_access_url→oa_url, cited_by_count→citation_count."""
    from optomind_research.runtime.section_coverage_tool_registry import _search_openalex
    import unittest.mock as mock

    oa_hit = {
        "title": "Phase Matching in Photonic Crystal Waveguides",
        "doi": "10.1/oa_test",
        "year": 2022,
        "abstract_or_snippet": "We study phase matching conditions in PCW.",
        "journal_or_venue": "Optics Express",
        "open_access_url": "https://openalex.org/fulltext/oa_test.pdf",
        "cited_by_count": 42,
        "is_oa": True,
        "openalex_id": "W123456",
        "authors": ["Author A"],
        "relevance_score": 0.9,
        "raw_metadata": {},
    }

    mock_backend = mock.MagicMock()
    mock_backend.search.return_value = [oa_hit]

    import optomind_research.gap_oa_expander as goe
    with mock.patch.object(goe, "OpenAlexBackend", return_value=mock_backend):
        results = _search_openalex(["phase matching waveguide"], max_per_q=5)

    assert results, "No results returned from _search_openalex"
    r = results[0]
    assert r["abstract"] == "We study phase matching conditions in PCW.", (
        f"abstract_or_snippet not mapped to 'abstract': {r.get('abstract')!r}"
    )
    assert r["venue"] == "Optics Express", (
        f"journal_or_venue not mapped to 'venue': {r.get('venue')!r}"
    )
    assert r["oa_url"] == "https://openalex.org/fulltext/oa_test.pdf", (
        f"open_access_url not mapped to 'oa_url': {r.get('oa_url')!r}"
    )
    assert r["citation_count"] == 42, (
        f"cited_by_count not mapped to 'citation_count': {r.get('citation_count')!r}"
    )
    assert r["openalex_id"] == "W123456"
    assert r["is_oa"] is True


# ---------------------------------------------------------------------------
# SC-T21: _search_semantic_scholar field adapter maps S2 fields correctly
# ---------------------------------------------------------------------------

def test_search_semantic_scholar_field_adapter(tmp_path: Path):
    """_search_semantic_scholar must map S2 backend fields correctly:
    abstract_or_snippet→abstract, journal_or_venue→venue,
    raw_metadata.open_access_pdf.url→pdf_url (and set is_oa=True),
    raw_metadata.citationCount→citation_count."""
    from optomind_research.runtime.section_coverage_tool_registry import _search_semantic_scholar
    import unittest.mock as mock

    s2_hit = {
        "title": "Four-Wave Mixing in Silicon Nanowires",
        "doi": "10.1/s2_test",
        "year": 2021,
        "abstract_or_snippet": "Four-wave mixing (FWM) gain in silicon nanowire waveguides.",
        "journal_or_venue": "Nature Photonics",
        "authors": ["Author B"],
        "relevance_score": 0.85,
        "semantic_scholar_paper_id": "S2abc123",
        "raw_metadata": {
            "open_access_pdf": {"url": "https://pdfs.semanticscholar.org/s2_test.pdf"},
            "citationCount": 88,
        },
    }

    mock_backend = mock.MagicMock()
    mock_backend.search.return_value = [s2_hit]

    import optomind_research.gap_oa_expander as goe
    with mock.patch.object(goe, "SemanticScholarBackend", return_value=mock_backend):
        results = _search_semantic_scholar(["four-wave mixing silicon"], max_per_q=5)

    assert results, "No results returned from _search_semantic_scholar"
    r = results[0]
    assert r["abstract"] == "Four-wave mixing (FWM) gain in silicon nanowire waveguides.", (
        f"abstract_or_snippet not mapped to 'abstract': {r.get('abstract')!r}"
    )
    assert r["venue"] == "Nature Photonics", (
        f"journal_or_venue not mapped to 'venue': {r.get('venue')!r}"
    )
    assert r["pdf_url"] == "https://pdfs.semanticscholar.org/s2_test.pdf", (
        f"raw_metadata.open_access_pdf.url not mapped to 'pdf_url': {r.get('pdf_url')!r}"
    )
    assert r["is_oa"] is True, "is_oa must be True when pdf_url is present"
    assert r["citation_count"] == 88, (
        f"raw_metadata.citationCount not mapped to 'citation_count': {r.get('citation_count')!r}"
    )
    assert r["semantic_scholar_id"] == "S2abc123"


# ---------------------------------------------------------------------------
# SC-T22: Negative role isolation — topic-only seed must NOT satisfy all roles
# ---------------------------------------------------------------------------

def test_query_kb_role_isolation_negative(tmp_path: Path):
    """Papers seeded with only 'foundation' role keywords must return 0 results
    for 'controversy' and 'application' roles — verifying topic/role separation."""
    from optomind_research.runtime.section_coverage_tool_registry import _query_kb_for_role

    section_data = _make_section_data()
    kb = tmp_path / "isolation_test.sqlite"
    # Seed only with foundation + topic keywords — no controversy/application keywords
    _seed_fake_kb(kb, paper_count=5, role_hint="foundation theoretical nonlinear optical waveguide Kerr")

    # Foundation must match (has "foundation", "theoretical")
    results_foundation = _query_kb_for_role(kb, "S03", "foundation", top_k=8, section_data=section_data)
    assert len(results_foundation) > 0, "foundation role should return results for foundation-seeded KB"

    # controversy and application must NOT match (seed has none of their keywords)
    results_controversy = _query_kb_for_role(kb, "S03", "controversy", top_k=8, section_data=section_data)
    assert len(results_controversy) == 0, (
        f"controversy role should return 0 results for foundation-only seed, got {len(results_controversy)}"
    )

    results_application = _query_kb_for_role(kb, "S03", "application", top_k=8, section_data=section_data)
    assert len(results_application) == 0, (
        f"application role should return 0 results for foundation-only seed, got {len(results_application)}"
    )


# ---------------------------------------------------------------------------
# SC-T23: trace_seed_references uses DOI: prefix for S2 API calls
# ---------------------------------------------------------------------------

def test_trace_seed_references_uses_doi_prefix(tmp_path: Path):
    """_make_trace_seed_references must pass 'DOI:10.xxx' (not bare DOI) to
    SemanticScholarBackend.get_references, and must correctly map S2 response fields."""
    from optomind_research.runtime.section_coverage_tool_registry import _make_trace_seed_references
    import unittest.mock as mock

    ctx = _make_ctx(tmp_path)

    # Seed one approved candidate with a DOI into the ledger so restore works
    from optomind_research.runtime.section_coverage_tool_registry import _append_candidates_to_ledger
    from optomind_research.runtime.artifact_schemas import (
        OACandidateLedger, OACandidate, CandidateDecision, ScopeFit,
    )
    doi = "10.1234/seed_paper"
    seed_cand = {
        "candidate_id": "cand_seed001",
        "section_id": "S03",
        "role": "foundation",
        "title": "Seed Paper for Tracing",
        "doi": doi,
        "abstract": "A foundational paper on nonlinear optics.",
        "is_oa": True,
        "pdf_url": "",
        "backends": ["openalex"],
        "query_texts": [],
        "scope_fit": "direct",
        "decision": "approved",
    }
    ctx.register_candidates([seed_cand])
    _append_candidates_to_ledger(tmp_path, "S03", [seed_cand])
    # Mark as approved in ledger file
    ledger = OACandidateLedger(section_id="S03")
    ledger.candidates.append(OACandidate(
        candidate_id="cand_seed001", section_id="S03", role="foundation",
        title="Seed Paper for Tracing", doi=doi,
        decision=CandidateDecision.approved, scope_fit=ScopeFit.direct,
    ))
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        json.dumps(ledger.model_dump()), encoding="utf-8"
    )

    s2_ref = {
        "title": "Referenced Paper via S2",
        "doi": "10.5678/ref_paper",
        "year": 2020,
        "abstract_or_snippet": "A related nonlinear optics reference.",
        "journal_or_venue": "Optics Letters",
        "authors": ["Author X"],
        "semantic_scholar_paper_id": "S2ref001",
        "pdf_url": "https://pdfs.semanticscholar.org/ref_paper.pdf",
        "raw_metadata": {
            "open_access_pdf": {"url": "https://pdfs.semanticscholar.org/ref_paper.pdf"},
            "citation_count": 12,
        },
    }

    captured_ids: list = []

    import tools.academic_backends.semantic_scholar_backend as s2_mod

    def _fake_get_refs(paper_id, *args, **kwargs):
        captured_ids.append(paper_id)
        return [s2_ref]

    with mock.patch.object(s2_mod.SemanticScholarBackend, "get_references", side_effect=_fake_get_refs):
        fn = _make_trace_seed_references(ctx)
        result = json.loads(fn("cand_seed001", max_refs=5))

    assert result["status"] == "ok", f"trace_seed_references failed: {result}"
    # Verify DOI: prefix was used in the S2 API call
    assert any(str(pid).startswith("DOI:") for pid in captured_ids), (
        f"S2 get_references was not called with 'DOI:' prefix. Called with: {captured_ids}"
    )
    assert f"DOI:{doi}" in captured_ids, (
        f"Expected 'DOI:{doi}' in S2 calls, got: {captured_ids}"
    )
    # Verify field mapping in discovered candidates
    ledger_data = json.loads((tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8"))
    ref_cands = [c for c in ledger_data["candidates"] if c["candidate_id"] != "cand_seed001"]
    assert len(ref_cands) > 0, "No reference candidates added to ledger by trace_seed_references"
    r = ref_cands[0]
    assert r.get("abstract") == "A related nonlinear optics reference." or r.get("abstract", "").startswith("A related"), (
        f"abstract_or_snippet not mapped to 'abstract': {r.get('abstract')!r}"
    )


# ---------------------------------------------------------------------------
# SC-T24: _build_source_ledger emits one entry per (paper_id, role) pair
# ---------------------------------------------------------------------------

def test_build_source_ledger_per_role_dedup(tmp_path: Path):
    """A materialized paper with role_fit=[foundation, mechanism] must appear
    as two separate entries in SECTION_SOURCE_LEDGER.json — one per role.
    Same paper_id is allowed under different roles; same (paper_id, role) is deduped."""
    from optomind_research.runtime.section_coverage_tool_registry import (
        _build_source_ledger,
        _append_candidates_to_ledger,
    )
    from optomind_research.runtime.artifact_schemas import (
        OACandidateLedger, OACandidate, CandidateDecision, ScopeFit,
        MaterializationManifest, MaterializedPaper, AcquisitionStatus,
    )

    ctx = _make_ctx(tmp_path)

    pid = "paper_multirole001"
    doi = "10.1/multirole"

    cand = {
        "candidate_id": "cand_mr001",
        "section_id": "S03",
        "role": "foundation",
        "title": "Multi-Role Paper",
        "doi": doi,
        "abstract": "A paper relevant to both foundation and mechanism.",
        "is_oa": True,
        "pdf_url": "https://example.com/mr001.pdf",
        "scope_fit": "direct",
        "decision": "approved",
        "role_fit": ["foundation", "mechanism"],
    }
    ctx.register_candidates([cand])
    _append_candidates_to_ledger(tmp_path, "S03", [cand])

    # Write approved ledger entry
    ledger = OACandidateLedger(section_id="S03")
    ledger.candidates.append(OACandidate(
        candidate_id="cand_mr001", section_id="S03", role="foundation",
        title="Multi-Role Paper", doi=doi,
        decision=CandidateDecision.approved, scope_fit=ScopeFit.direct,
        role_fit=["foundation", "mechanism"],
    ))
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        json.dumps(ledger.model_dump()), encoding="utf-8"
    )

    # Write a MATERIALIZATION_MANIFEST with a fulltext entry for the paper
    manifest = MaterializationManifest(section_id="S03")
    manifest.papers.append(MaterializedPaper(
        candidate_id="cand_mr001",
        paper_id=pid,
        doi=doi,
        title="Multi-Role Paper",
        year=2022,
        acquisition_status=AcquisitionStatus.fulltext,
        chunk_ids=[f"chunk_{pid}_0", f"chunk_{pid}_1"],
        new_paper=True,
        new_chunks=2,
        section_id="S03",
        role="foundation",
    ))
    (tmp_path / "MATERIALIZATION_MANIFEST.json").write_text(
        json.dumps(manifest.model_dump()), encoding="utf-8"
    )

    # Write matching chunks to temp KB so chunk_ids can be retrieved
    with sqlite3.connect(str(ctx.temp_kb_sqlite)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS papers (
                paper_id TEXT PRIMARY KEY, title TEXT, abstract TEXT, year INTEGER, venue TEXT
            );
            CREATE TABLE IF NOT EXISTS text_chunks (
                chunk_id TEXT PRIMARY KEY, paper_id TEXT, text TEXT, section_path TEXT
            );
        """)
        conn.execute("INSERT OR IGNORE INTO papers VALUES (?,?,?,?,?)",
                     (pid, "Multi-Role Paper", "abstract", 2022, "Optics Express"))
        for i in range(2):
            conn.execute("INSERT OR IGNORE INTO text_chunks VALUES (?,?,?,?)",
                         (f"chunk_{pid}_{i}", pid, "nonlinear waveguide Kerr effect", "body"))

    _build_source_ledger(ctx)

    sled = json.loads((tmp_path / "SECTION_SOURCE_LEDGER.json").read_text(encoding="utf-8"))
    sources = sled.get("sources", [])

    # Find entries for our paper_id
    mr_entries = [s for s in sources if s.get("paper_id") == pid]
    roles_found = {s.get("literature_role") for s in mr_entries}

    assert len(mr_entries) >= 2, (
        f"Expected ≥2 entries for paper_id={pid!r} (one per role), got {len(mr_entries)}: {mr_entries}"
    )
    assert "foundation" in roles_found, f"Missing foundation entry; roles found: {roles_found}"
    assert "mechanism" in roles_found, f"Missing mechanism entry; roles found: {roles_found}"

    # Idempotency: calling again must not add duplicates
    _build_source_ledger(ctx)
    sled2 = json.loads((tmp_path / "SECTION_SOURCE_LEDGER.json").read_text(encoding="utf-8"))
    mr_entries2 = [s for s in sled2.get("sources", []) if s.get("paper_id") == pid]
    assert len(mr_entries2) == len(mr_entries), (
        f"Duplicate entries after second _build_source_ledger call: {len(mr_entries2)} vs {len(mr_entries)}"
    )


# ---------------------------------------------------------------------------
# SC-T25: Smoke hardening — metadata_only sources do not count as qualified
# ---------------------------------------------------------------------------

def test_smoke_qualified_source_rejects_metadata_only(tmp_path: Path):
    """The smoke test's qualified-source check must reject sources that are
    metadata_only, even if scope_fit=direct and section_id matches.
    Only fulltext + non-empty canonical_chunk_ids qualifies."""

    # Build a SECTION_SOURCE_LEDGER.json with only metadata_only sources
    sources_data = {
        "schema_version": "2.0",
        "section_id": "S03",
        "sources": [
            {
                "paper_id": "paper_meta001",
                "doi": "10.1/meta001",
                "title": "Metadata Only Paper",
                "year": 2022,
                "venue": "Optics Express",
                "authors": ["Author A"],
                "literature_role": "foundation",
                "scope_fit": "direct",
                "retrieval_query": "nonlinear optical",
                "retrieval_backend": "openalex",
                "adoption_reason": "Test",
                "expected_section_use": "reference",
                "canonical_chunk_ids": [],       # empty — not qualified
                "local_prior": False,
                "new_this_run": True,
                "acquisition_status": "metadata_only",  # not fulltext
                "normalization_status": "",
                "section_id": "S03",
                "not_usable_for": [],
            },
            {
                "paper_id": "paper_abs001",
                "doi": "10.1/abs001",
                "title": "Abstract Only Paper",
                "year": 2021,
                "venue": "Nature Photonics",
                "authors": ["Author B"],
                "literature_role": "mechanism",
                "scope_fit": "adjacent",
                "retrieval_query": "Kerr effect",
                "retrieval_backend": "semantic_scholar",
                "adoption_reason": "Test",
                "expected_section_use": "context",
                "canonical_chunk_ids": [],       # empty — not qualified
                "local_prior": False,
                "new_this_run": True,
                "acquisition_status": "abstract_only",  # not fulltext
                "normalization_status": "",
                "section_id": "S03",
                "not_usable_for": [],
            },
        ],
        "total_sources": 2,
        "new_sources": 2,
        "local_prior_sources": 0,
    }

    # This is the exact logic from the smoke test (criterion 6b)
    section_id = "S03"
    sled = sources_data
    qualified = [
        s for s in sled.get("sources", [])
        if (s.get("scope_fit") in ("direct", "adjacent")
            and s.get("acquisition_status") == "fulltext"
            and s.get("canonical_chunk_ids")
            and s.get("section_id") == section_id)
    ]
    assert len(qualified) == 0, (
        f"metadata_only/abstract_only sources must NOT be qualified; got {len(qualified)}: {qualified}"
    )

    # Now add a proper fulltext source and verify it IS qualified
    sources_data["sources"].append({
        "paper_id": "paper_full001",
        "doi": "10.1/full001",
        "title": "Fulltext Paper",
        "year": 2023,
        "venue": "Optica",
        "authors": ["Author C"],
        "literature_role": "frontier",
        "scope_fit": "direct",
        "retrieval_query": "phase matching waveguide",
        "retrieval_backend": "openalex",
        "adoption_reason": "Test",
        "expected_section_use": "main claim",
        "canonical_chunk_ids": ["chunk_full001_0", "chunk_full001_1"],  # non-empty
        "local_prior": False,
        "new_this_run": True,
        "acquisition_status": "fulltext",  # qualifies
        "normalization_status": "ok",
        "section_id": "S03",
        "not_usable_for": [],
    })

    qualified2 = [
        s for s in sources_data.get("sources", [])
        if (s.get("scope_fit") in ("direct", "adjacent")
            and s.get("acquisition_status") == "fulltext"
            and s.get("canonical_chunk_ids")
            and s.get("section_id") == section_id)
    ]
    assert len(qualified2) == 1, (
        f"Expected exactly 1 qualified fulltext source, got {len(qualified2)}"
    )
    assert qualified2[0]["paper_id"] == "paper_full001"


# ---------------------------------------------------------------------------
# SC-T26: Tool layer rejects non-allowed role when min_mode_allowed_role is set
# ---------------------------------------------------------------------------

def test_search_oa_candidates_rejects_non_allowed_role(tmp_path: Path):
    """search_oa_candidates must return an error immediately (no network call)
    when role != min_mode_allowed_role, regardless of what the model requests."""
    from optomind_research.runtime.tool_provider import SectionCoverageContext
    from optomind_research.runtime.section_coverage_tool_registry import _make_search_oa_candidates
    import unittest.mock as mock

    section_data = _make_section_data()
    temp_kb = tmp_path / "temp_kb.sqlite"
    # Set min_mode_allowed_role="frontier" — only frontier is allowed
    ctx = SectionCoverageContext(
        section_id="S03",
        section_data=section_data,
        kb_sqlite=None,
        temp_kb_sqlite=temp_kb,
        work_dir=tmp_path,
        min_mode_allowed_role="frontier",
    )

    fn = _make_search_oa_candidates(ctx)

    # Patch backends so no network calls happen
    import optomind_research.gap_oa_expander as goe
    with mock.patch.object(goe, "OpenAlexBackend"), \
         mock.patch.object(goe, "SemanticScholarBackend"):
        for disallowed_role in ("foundation", "mechanism", "method", "controversy", "application"):
            result = json.loads(fn(disallowed_role, '["some query"]'))
            assert result["status"] == "error", (
                f"Expected error for role={disallowed_role!r} with min_mode_allowed_role='frontier', "
                f"got status={result['status']!r}"
            )
            assert "Budget constraint" in result["error"] or "only" in result["error"].lower(), (
                f"Error message should mention budget constraint: {result['error']!r}"
            )

        # frontier itself must NOT be rejected
        mock_backend = mock.MagicMock()
        mock_backend.search.return_value = []
        with mock.patch.object(goe, "OpenAlexBackend", return_value=mock_backend), \
             mock.patch.object(goe, "SemanticScholarBackend", return_value=mock_backend):
            result_ok = json.loads(fn("frontier", '["phase matching waveguide"]'))
        assert result_ok["status"] == "ok", (
            f"frontier role should be allowed, got: {result_ok!r}"
        )


# ---------------------------------------------------------------------------
# SC-T27: Tool layer clips query list to min_mode_max_queries
# ---------------------------------------------------------------------------

def test_search_oa_candidates_clips_queries_to_budget(tmp_path: Path):
    """search_oa_candidates must clip the query list to min_mode_max_queries,
    even if the model passes more queries."""
    from optomind_research.runtime.tool_provider import SectionCoverageContext
    from optomind_research.runtime.section_coverage_tool_registry import _make_search_oa_candidates
    import unittest.mock as mock

    section_data = _make_section_data()
    temp_kb = tmp_path / "temp_kb.sqlite"
    ctx = SectionCoverageContext(
        section_id="S03",
        section_data=section_data,
        kb_sqlite=None,
        temp_kb_sqlite=temp_kb,
        work_dir=tmp_path,
        min_mode_max_queries=1,  # hard limit: max 1 query
    )

    fn = _make_search_oa_candidates(ctx)

    captured_queries: list = []

    import optomind_research.gap_oa_expander as goe

    def _fake_search(query, max_results=5):
        captured_queries.append(query)
        return []

    mock_backend = mock.MagicMock()
    mock_backend.search.side_effect = _fake_search

    with mock.patch.object(goe, "OpenAlexBackend", return_value=mock_backend), \
         mock.patch.object(goe, "SemanticScholarBackend", return_value=mock_backend):
        result = json.loads(fn("frontier", '["query A", "query B", "query C"]'))

    assert result["status"] == "ok"
    # queries_used must have at most 1 element
    assert len(result["queries_used"]) <= 1, (
        f"Expected ≤1 query used (min_mode_max_queries=1), got {result['queries_used']}"
    )
    # Backend was called at most 1 time per backend (2 backends × 1 query = 2 calls max)
    assert len(captured_queries) <= 2, (
        f"Backend called {len(captured_queries)} times — should be ≤2 for 1 query × 2 backends"
    )


# ---------------------------------------------------------------------------
# SC-T28: Tool layer clips max_per_backend to min_mode_max_per_backend
# ---------------------------------------------------------------------------

def test_search_oa_candidates_clips_max_per_backend(tmp_path: Path):
    """search_oa_candidates must cap max_per_backend at min_mode_max_per_backend,
    even if the model passes a larger value."""
    from optomind_research.runtime.tool_provider import SectionCoverageContext
    from optomind_research.runtime.section_coverage_tool_registry import _make_search_oa_candidates
    import unittest.mock as mock

    section_data = _make_section_data()
    temp_kb = tmp_path / "temp_kb.sqlite"
    ctx = SectionCoverageContext(
        section_id="S03",
        section_data=section_data,
        kb_sqlite=None,
        temp_kb_sqlite=temp_kb,
        work_dir=tmp_path,
        min_mode_max_per_backend=3,  # hard limit: max 3 results per backend
    )

    fn = _make_search_oa_candidates(ctx)

    captured_max_results: list = []

    import optomind_research.gap_oa_expander as goe

    def _fake_search(query, max_results=5):
        captured_max_results.append(max_results)
        return []

    mock_backend = mock.MagicMock()
    mock_backend.search.side_effect = _fake_search

    with mock.patch.object(goe, "OpenAlexBackend", return_value=mock_backend), \
         mock.patch.object(goe, "SemanticScholarBackend", return_value=mock_backend):
        # Model passes max_per_backend=10 — tool layer must clip to 3
        result = json.loads(fn("frontier", '["waveguide nonlinear"]', max_per_backend=10))

    assert result["status"] == "ok"
    # Every backend.search call must have received max_results ≤ 3
    assert all(mr <= 3 for mr in captured_max_results), (
        f"Backend was called with max_results > 3: {captured_max_results}. "
        "min_mode_max_per_backend=3 was not enforced."
    )


def test_search_round_limit_is_persisted_and_enforced(tmp_path: Path):
    """A process restart must not reset the per-role external-search budget."""
    from unittest import mock
    from optomind_research.runtime.section_coverage_tool_registry import (
        SEARCH_BUDGET_LEDGER,
        _make_search_oa_candidates,
    )
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    kwargs = dict(
        section_id="S03",
        section_data=_make_section_data(),
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "temp.sqlite",
        work_dir=tmp_path,
        max_search_rounds_per_role=1,
    )
    ctx = SectionCoverageContext(**kwargs)
    with mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry._search_openalex",
        return_value=[],
    ), mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry._search_semantic_scholar",
        return_value=[],
    ):
        first = json.loads(
            _make_search_oa_candidates(ctx)(
                "frontier", '["first distinct frontier query"]'
            )
        )
    assert first["status"] == "ok"
    assert (tmp_path / SEARCH_BUDGET_LEDGER).exists()

    # New context simulates process restart.
    restarted = SectionCoverageContext(**kwargs)
    with mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry._search_openalex",
    ) as openalex, mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry._search_semantic_scholar",
    ) as semantic:
        second = json.loads(
            _make_search_oa_candidates(restarted)(
                "frontier", '["second distinct frontier query"]'
            )
        )
    assert second["status"] == "error"
    assert second["error_code"] == "role_search_round_limit_reached"
    openalex.assert_not_called()
    semantic.assert_not_called()


def test_search_requires_candidate_audit_before_next_round(tmp_path: Path):
    """The agent may not abandon an unaudited batch and buy another search."""
    from unittest import mock
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_search_oa_candidates,
    )
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    ctx = SectionCoverageContext(
        section_id="S03",
        section_data=_make_section_data(),
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "temp.sqlite",
        work_dir=tmp_path,
        max_search_rounds_per_role=3,
    )
    candidate = {
        "title": "Relevant optical frontier",
        "doi": "10.1234/frontier",
        "year": 2025,
        "abstract": "A directly relevant optical frontier result.",
        "is_oa": True,
        "backends": ["openalex"],
    }
    with mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry._search_openalex",
        return_value=[candidate],
    ), mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry._search_semantic_scholar",
        return_value=[],
    ):
        first = json.loads(
            _make_search_oa_candidates(ctx)(
                "frontier", '["first frontier route"]'
            )
        )
    assert first["candidate_count"] == 1

    with mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry._search_openalex",
    ) as openalex, mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry._search_semantic_scholar",
    ) as semantic:
        second = json.loads(
            _make_search_oa_candidates(ctx)(
                "frontier", '["second frontier route"]'
            )
        )
    assert second["status"] == "error"
    assert second["error_code"] == "candidate_audit_required"
    openalex.assert_not_called()
    semantic.assert_not_called()


def test_two_empty_search_rounds_trigger_no_yield_stop(tmp_path: Path):
    """Two strategically distinct empty rounds should terminate broad search."""
    from unittest import mock
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_search_oa_candidates,
    )
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    ctx = SectionCoverageContext(
        section_id="S03",
        section_data=_make_section_data(),
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "temp.sqlite",
        work_dir=tmp_path,
        max_search_rounds_per_role=3,
    )
    fn = _make_search_oa_candidates(ctx)
    with mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry._search_openalex",
        return_value=[],
    ), mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry._search_semantic_scholar",
        return_value=[],
    ):
        assert json.loads(fn("controversy", '["route one"]'))["status"] == "ok"
        assert json.loads(fn("controversy", '["route two"]'))["status"] == "ok"
        stopped = json.loads(fn("controversy", '["route three"]'))
    assert stopped["status"] == "error"
    assert stopped["error_code"] == "consecutive_no_yield_stop"


# ---------------------------------------------------------------------------
# SC-T29: Tool layer caps total materialized papers at min_mode_max_total_papers
# ---------------------------------------------------------------------------

def test_acquire_and_materialize_caps_total_papers(tmp_path: Path):
    """acquire_and_materialize_oa_papers must not exceed min_mode_max_total_papers
    across multiple calls, even if the model requests more papers per call."""
    from optomind_research.runtime.tool_provider import SectionCoverageContext
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_acquire_and_materialize_oa_papers,
        _append_candidates_to_ledger,
    )
    from optomind_research.runtime.artifact_schemas import (
        OACandidateLedger, OACandidate, CandidateDecision, ScopeFit,
    )
    import optomind_research.m3_kb_ingest as m3
    import unittest.mock as mock

    temp_kb = tmp_path / "temp_kb.sqlite"
    ctx = SectionCoverageContext(
        section_id="S03",
        section_data=_make_section_data(),
        kb_sqlite=None,
        temp_kb_sqlite=temp_kb,
        work_dir=tmp_path,
        min_mode_max_total_papers=1,  # hard limit: only 1 paper total
    )

    # Register 3 approved candidates
    cands = []
    for i in range(3):
        cid = f"cand_cap{i:03d}"
        doi = f"10.1/cap{i}"
        cand = {
            "candidate_id": cid,
            "section_id": "S03",
            "role": "frontier",
            "title": f"Capped Paper {i}",
            "doi": doi,
            "abstract": "nonlinear waveguide",
            "is_oa": True,
            "pdf_url": f"https://example.com/cap{i}.pdf",
            "scope_fit": "direct",
            "decision": "approved",
        }
        cands.append(cand)
    ctx.register_candidates(cands)
    _append_candidates_to_ledger(tmp_path, "S03", cands)

    ledger = OACandidateLedger(section_id="S03")
    for c in cands:
        ledger.candidates.append(OACandidate(
            candidate_id=c["candidate_id"], section_id="S03", role="frontier",
            title=c["title"], doi=c["doi"],
            decision=CandidateDecision.approved, scope_fit=ScopeFit.direct,
        ))
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        json.dumps(ledger.model_dump()), encoding="utf-8"
    )

    # Mock ingest to return fulltext success for any paper
    mock_ingest_result = mock.MagicMock()
    mock_ingest_result.new_chunk_ids = ["chunk_0", "chunk_1"]
    mock_ingest_result.reused_chunk_ids = []
    mock_ingest_result.new_paper_ids = ["paper_cap_mock"]

    fn = _make_acquire_and_materialize_oa_papers(ctx)

    with mock.patch.object(m3.KBIngester, "ingest_oa_candidates",
                           return_value=mock_ingest_result):
        # First call: request all 3 — only 1 should be materialized
        r1 = json.loads(fn("frontier", json.dumps([c["candidate_id"] for c in cands]), max_papers=3))
        assert r1["status"] == "ok"
        assert r1["materialized_this_call"] == 1, (
            f"First call should materialize exactly 1 (budget=1), got {r1['materialized_this_call']}"
        )

        # Second call: budget exhausted — must return error
        r2 = json.loads(fn("frontier", json.dumps([cands[1]["candidate_id"]]), max_papers=1))
        assert r2["status"] == "error", (
            f"Second call should fail (budget exhausted), got status={r2['status']!r}: {r2}"
        )
        assert "Budget constraint" in r2["error"], (
            f"Error should mention budget constraint: {r2['error']!r}"
        )


# ---------------------------------------------------------------------------
# SC-T30: URL waterfall — _ingest_single_candidate builds full alternate_urls
# ---------------------------------------------------------------------------

def test_ingest_single_candidate_builds_url_waterfall(tmp_path: Path):
    """_ingest_single_candidate must pass ALL candidate URL fields to KBIngester
    as alternate_urls so the waterfall can find a working OA source."""
    from optomind_research.runtime.section_coverage_tool_registry import _ingest_single_candidate
    import optomind_research.m3_kb_ingest as m3
    import unittest.mock as mock

    cand = {
        "candidate_id": "cand_wf001",
        "title": "Waterfall Test Paper",
        "doi": "10.9999/wf001",
        "year": 2023,
        "abstract": "test abstract",
        "is_oa": True,
        "pdf_url": "https://primary.example.com/paper.pdf",
        "url_for_pdf": "https://openalex.example.com/pdf",
        "best_oa_url": "https://unpaywall.example.com/pdf",
        "open_access_url": "https://oa.example.com/landing",
        "oa_url": "https://oa2.example.com/pdf",
        "html_url": "https://html.example.com/paper",
        "repository_url": "https://repo.example.com/paper",
        "alternate_urls": ["https://alt1.example.com/pdf", "https://alt2.example.com/pdf"],
        "content_urls": {"oa_pdf": "https://content.example.com/pdf"},
        "scope_fit": "direct",
    }

    captured: dict = {}
    mock_result = mock.MagicMock()
    mock_result.new_chunk_ids = ["chunk_wf_0", "chunk_wf_1"]
    mock_result.reused_chunk_ids = []
    mock_result.new_paper_ids = ["paper_wf"]

    def capture_ingest(candidates, claim, **kwargs):
        captured["gap_cand"] = candidates[0]
        return mock_result

    with mock.patch.object(m3.KBIngester, "ingest_oa_candidates", side_effect=capture_ingest):
        result = _ingest_single_candidate(cand, tmp_path / "kb.sqlite", tmp_path)

    assert result["acquisition_status"] == "fulltext"

    gc = captured["gap_cand"]
    # Primary URL must be the first URL field
    assert gc["pdf_url"] == "https://primary.example.com/paper.pdf"
    # alternate_urls must contain the remaining URLs (at least 5 distinct ones)
    alts = gc["alternate_urls"]
    assert len(alts) >= 5, f"Expected ≥5 alternate_urls, got {len(alts)}: {alts}"
    # All original URL values should appear somewhere in primary+alternates
    all_urls = [gc["pdf_url"]] + alts
    for expected_url in [
        "https://openalex.example.com/pdf",
        "https://unpaywall.example.com/pdf",
        "https://alt1.example.com/pdf",
        "https://content.example.com/pdf",
    ]:
        assert expected_url in all_urls, f"{expected_url!r} not in URL waterfall: {all_urls}"


# ---------------------------------------------------------------------------
# SC-T31: Direct scope → llm_relevance_grade "A"; adjacent → "B"
# ---------------------------------------------------------------------------

def test_ingest_single_candidate_scope_grade_mapping(tmp_path: Path):
    """_ingest_single_candidate must map direct scope_fit → grade 'A', adjacent → 'B'."""
    from optomind_research.runtime.section_coverage_tool_registry import _ingest_single_candidate
    import optomind_research.m3_kb_ingest as m3
    import unittest.mock as mock

    base_cand = {
        "candidate_id": "cand_grade",
        "title": "Grade Test",
        "doi": "10.9999/grade",
        "abstract": "test",
        "is_oa": True,
        "pdf_url": "https://example.com/grade.pdf",
    }

    grades_captured: list = []
    mock_result = mock.MagicMock()
    mock_result.new_chunk_ids = ["ck0", "ck1"]
    mock_result.reused_chunk_ids = []
    mock_result.new_paper_ids = ["pid_grade"]

    def capture_grade(candidates, claim, **kwargs):
        grades_captured.append(candidates[0].get("llm_relevance_grade"))
        return mock_result

    with mock.patch.object(m3.KBIngester, "ingest_oa_candidates", side_effect=capture_grade):
        _ingest_single_candidate(
            dict(base_cand, scope_fit="direct"), tmp_path / "kb.sqlite", tmp_path
        )
        _ingest_single_candidate(
            dict(base_cand, scope_fit="adjacent"), tmp_path / "kb.sqlite", tmp_path
        )

    assert grades_captured[0] == "direct", f"direct should map to 'direct', got {grades_captured[0]!r}"
    assert grades_captured[1] == "adjacent", f"adjacent should map to 'adjacent', got {grades_captured[1]!r}"


# ---------------------------------------------------------------------------
# SC-T32: inspect_section_local_coverage — optional roles NOT in blocking_gaps
# ---------------------------------------------------------------------------

def test_inspect_local_coverage_optional_roles_not_blocking(tmp_path: Path):
    """inspect_section_local_coverage must not classify optional roles as blocking,
    even when the local KB has zero papers for those roles."""
    from optomind_research.runtime.tool_provider import SectionCoverageContext
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_inspect_section_local_coverage,
    )

    section_data = _make_section_data()
    section_data = dict(section_data)
    section_data["required_roles"] = ["foundation", "mechanism"]
    section_data["optional_roles"] = ["application", "controversy"]

    ctx = SectionCoverageContext(
        section_id=section_data["section_id"],
        section_data=section_data,
        kb_sqlite=None,   # no KB → triggers the "all roles" path
        temp_kb_sqlite=tmp_path / "temp.sqlite",
        work_dir=tmp_path,
    )

    fn = _make_inspect_section_local_coverage(ctx)
    result = json.loads(fn())

    blocking = result["blocking_gaps"]
    assert "application" not in blocking, (
        f"Optional role 'application' must NOT be in blocking_gaps: {blocking}"
    )
    assert "controversy" not in blocking, (
        f"Optional role 'controversy' must NOT be in blocking_gaps: {blocking}"
    )
    assert "foundation" in blocking, (
        f"Required role 'foundation' must be in blocking_gaps: {blocking}"
    )
    assert "mechanism" in blocking, (
        f"Required role 'mechanism' must be in blocking_gaps: {blocking}"
    )


# ---------------------------------------------------------------------------
# SC-T33: submit_section_gap_report writes and merge-updates SECTION_GAP_REPORT.json
# ---------------------------------------------------------------------------

def test_submit_section_gap_report_writes_and_merges(tmp_path: Path):
    """submit_section_gap_report must create the file on first call and merge
    (not overwrite) new roles on subsequent calls."""
    from optomind_research.runtime.tool_provider import SectionCoverageContext
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_submit_section_gap_report,
    )
    from optomind_research.runtime.artifact_schemas import SectionGapReport

    ctx = SectionCoverageContext(
        section_id="S03",
        section_data=_make_section_data(),
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "temp.sqlite",
        work_dir=tmp_path,
    )
    fn = _make_submit_section_gap_report(ctx)

    # First call: one blocking gap
    r1 = json.loads(fn(json.dumps({
        "gaps": [{
            "role": "frontier",
            "severity": "blocking",
            "description": "No OA fulltext found",
            "queries_attempted": ["frontier nonlinear waveguide"],
            "candidates_found": 2,
            "candidates_approved": 0,
            "candidates_materialized": 0,
            "stop_reason": "no OA fulltext",
            "suggested_followup": "try arXiv search",
            "is_blocking": True,
        }],
        "overall_coverage_status": "blocking_gaps_remain",
    })))
    assert r1["status"] == "ok"
    assert r1["blocking_gap_count"] == 1

    report_path = tmp_path / "SECTION_GAP_REPORT.json"
    assert report_path.exists()

    # Second call: add a different role without overwriting the first
    r2 = json.loads(fn(json.dumps({
        "gaps": [{
            "role": "application",
            "severity": "important",
            "description": "Limited application papers",
            "queries_attempted": [],
            "candidates_found": 0,
            "candidates_approved": 0,
            "candidates_materialized": 0,
            "stop_reason": "not required",
            "suggested_followup": "",
            "is_blocking": False,
        }],
        "overall_coverage_status": "completed_with_open_gaps",
    })))
    assert r2["status"] == "ok"
    assert r2["open_gap_count"] == 2, f"Expected 2 gaps after merge, got {r2['open_gap_count']}"

    report = SectionGapReport.model_validate(
        json.loads(report_path.read_text(encoding="utf-8"))
    )
    roles = {g.role for g in report.gaps}
    assert "frontier" in roles and "application" in roles, (
        f"Both roles must survive merge: {roles}"
    )
    assert report.overall_coverage_status == "completed_with_open_gaps"


# ---------------------------------------------------------------------------
# SC-T34: validate_section_coverage_package result drives VALIDATION_PASSED gate
# ---------------------------------------------------------------------------

def test_validate_section_coverage_package_emits_validation_passed(tmp_path: Path):
    """validate_section_coverage_package must include 'VALIDATION_PASSED' in its
    return value when coverage is sufficient, so the ResearchWorker gate fires."""
    from optomind_research.runtime.tool_provider import SectionCoverageContext
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_validate_section_coverage_package,
        _make_submit_section_gap_report,
    )
    from optomind_research.runtime.artifact_schemas import (
        SectionSourceLedger, SourceEntry, ScopeFit, AcquisitionStatus,
        SectionContext, SectionCoveragePlan, LocalCoverageAudit,
    )

    section_data = _make_section_data()
    # This test exercises the validation marker rather than article-scale
    # breadth.  Declare an intentionally small fixture target explicitly so
    # the production default (6--12 unique sources per section) remains strict.
    section_data["literature_coverage_target"] = {
        "minimum_unique_sources": 4,
        "minimum_direct_sources": 4,
    }
    ctx = SectionCoverageContext(
        section_id="S03",
        section_data=section_data,
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "temp.sqlite",
        work_dir=tmp_path,
    )

    # Write the three prerequisite artifacts required by validate
    sc = SectionContext(
        section_id="S03",
        section_title="Nonlinear Optical Mechanisms",
        chapter_argument="chi-3 test",
        scope_description="test scope",
        required_roles=section_data.get("required_roles", []),
        minimum_unique_sources=4,
        minimum_direct_sources=4,
    )
    (tmp_path / "SECTION_CONTEXT.json").write_text(
        json.dumps(sc.model_dump(mode="json")), encoding="utf-8"
    )

    plan = SectionCoveragePlan(section_id="S03", chapter_argument="chi-3 test")
    (tmp_path / "SECTION_COVERAGE_PLAN.json").write_text(
        json.dumps(plan.model_dump(mode="json")), encoding="utf-8"
    )

    audit = LocalCoverageAudit(
        section_id="S03",
        blocking_gaps=[],   # no blocking gaps
        sufficient_roles=list(section_data.get("required_roles", [])),
    )
    (tmp_path / "LOCAL_COVERAGE_AUDIT.json").write_text(
        json.dumps(audit.model_dump(mode="json")), encoding="utf-8"
    )

    # Write a qualified source ledger
    ledger = SectionSourceLedger(section_id="S03")
    for index, role in enumerate(section_data.get("required_roles", [])):
        ledger.sources.append(SourceEntry(
            paper_id=f"paper_valid_{index:03d}",
            doi=f"10.1/valid{index:03d}",
            title=f"Valid Fulltext Paper {index}",
            year=2022,
            literature_role=role,
            scope_fit=ScopeFit.direct,
            acquisition_status=AcquisitionStatus.fulltext,
            canonical_chunk_ids=[f"chunk_{index}"],
            new_this_run=True,
            section_id="S03",
        ))
    ledger.total_sources = len(ledger.sources)
    (tmp_path / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps(ledger.model_dump(mode="json")), encoding="utf-8"
    )

    fn = _make_validate_section_coverage_package(ctx)
    result_text = fn()

    assert "VALIDATION_PASSED" in result_text, (
        f"Expected 'VALIDATION_PASSED' when coverage sufficient; got: {result_text[:300]}"
    )


def test_default_coverage_breadth_is_independent_of_role_count(tmp_path: Path):
    """One source per role must not masquerade as review-chapter breadth."""

    from optomind_research.runtime.tool_provider import SectionCoverageContext

    section_data = _make_section_data()
    section_data["target_word_range"] = {"min": 1000, "max": 1300}
    ctx = SectionCoverageContext(
        section_id="S03",
        section_data=section_data,
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "staging.sqlite",
        work_dir=tmp_path,
    )
    targets = ctx.coverage_breadth_targets()
    assert targets["minimum_unique_sources"] == 8
    assert targets["minimum_direct_sources"] == 5
    assert targets["minimum_unique_sources"] > len(
        section_data["required_roles"]
    )


def test_adjacent_sources_receive_non_negotiable_scope_restrictions():
    from optomind_research.runtime.section_coverage_tool_registry import (
        _canonical_scope_restrictions,
    )

    restrictions = _canonical_scope_restrictions("adjacent", [])
    assert any("application domain outside" in item for item in restrictions)
    assert any("quantitative results" in item for item in restrictions)
    direct = _canonical_scope_restrictions("direct", ["keep this boundary"])
    assert direct == ["keep this boundary"]


def test_external_candidate_direct_scope_is_downgraded_for_broad_background(
    tmp_path: Path,
):
    """A generic field review must not satisfy an exact direct-source target."""

    from optomind_research.runtime.section_coverage_tool_registry import (
        _append_candidates_to_ledger,
        _make_submit_candidate_audit,
    )
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    section_data = {
        "section_id": "S01",
        "title": "Physical Foundations of Achromatic Phase Compensation",
        "chapter_argument": (
            "Broadband achromatic metalenses require phase and group delay "
            "control for visible imaging."
        ),
        "key_questions": [
            "How do phase and group delay constrain achromatic focusing?"
        ],
        "topic_identity": {
            "valid": True,
            "fingerprint": "test",
            "core_anchor_tokens": [
                "achromatic", "broadband", "imaging",
                "metalens", "metasurface", "visible",
            ],
            "supporting_anchor_tokens": ["phase", "delay", "focusing"],
            "anchor_phrases": ["broadband achromatic metalens"],
        },
    }
    ctx = SectionCoverageContext(
        section_id="S01",
        section_data=section_data,
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "staging.sqlite",
        work_dir=tmp_path,
    )
    candidates = [
        {
            "candidate_id": "cand_broad",
            "section_id": "S01",
            "role": "foundation",
            "title": "A review of metasurfaces: physics and applications",
            "abstract": (
                "This broad review surveys visible metasurface physics, "
                "wavefront shaping, polarization, and applications."
            ),
        },
        {
            "candidate_id": "cand_direct",
            "section_id": "S01",
            "role": "mechanism",
            "title": "Broadband achromatic dielectric metalenses",
            "abstract": (
                "Broadband visible imaging with an achromatic metalens is "
                "enabled through phase and group delay compensation."
            ),
        },
    ]
    ctx.register_candidates(candidates)
    _append_candidates_to_ledger(tmp_path, "S01", candidates)
    payload = [
        {
            "candidate_id": item["candidate_id"],
            "scope_fit": "direct",
            "role_fit": [item["role"]],
            "decision": "approved",
            "audit_reason": "Model judged this candidate useful.",
        }
        for item in candidates
    ]
    result = json.loads(
        _make_submit_candidate_audit(ctx)(json.dumps(payload))
    )
    assert result["status"] == "ok"
    ledger = json.loads(
        (tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )
    by_id = {
        item["candidate_id"]: item for item in ledger["candidates"]
    }
    assert by_id["cand_broad"]["scope_fit"] == "adjacent"
    assert "deterministic_scope_downgrade" in by_id["cand_broad"]["audit_reason"]
    assert by_id["cand_direct"]["scope_fit"] == "direct"


def test_external_candidate_inspection_is_context_bounded(tmp_path: Path):
    """Full abstracts stay on disk while ReAct receives compact previews."""

    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_inspect_candidate_batch,
    )

    ctx = _make_ctx(tmp_path)
    candidates = [
        {
            "candidate_id": f"cand_long_{index}",
            "section_id": "S03",
            "role": "mechanism",
            "title": f"Long candidate {index}",
            "abstract": "nonlinear photonic mechanism " * 300,
            "oa_url": f"https://example.org/{index}",
            "pdf_url": f"https://example.org/{index}.pdf",
        }
        for index in range(8)
    ]
    ctx.register_candidates(candidates)
    result = json.loads(
        _make_inspect_candidate_batch(ctx)(
            json.dumps([item["candidate_id"] for item in candidates])
        )
    )
    assert result["found"] == 6
    assert all(
        len(item["abstract"]) <= 1200
        for item in result["candidates"]
    )
    assert all(
        "pdf_url" not in item and "oa_url" not in item
        for item in result["candidates"]
    )


def test_documented_breadth_shortfall_is_not_coverage_sufficient(
    tmp_path: Path,
):
    """A documented shortfall may pass, but its status must remain explicit."""

    from optomind_research.runtime.artifact_schemas import (
        AcquisitionStatus,
        LocalCoverageAudit,
        ScopeFit,
        SectionContext,
        SectionCoveragePlan,
        SectionSourceLedger,
        SourceEntry,
    )
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_submit_section_gap_report,
        _make_validate_section_coverage_package,
    )
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    section_data = _make_section_data()
    section_data["literature_coverage_target"] = {
        "minimum_unique_sources": 8,
        "minimum_direct_sources": 5,
    }
    ctx = SectionCoverageContext(
        section_id=section_data["section_id"],
        section_data=section_data,
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "staging.sqlite",
        work_dir=tmp_path,
    )
    (tmp_path / "SECTION_CONTEXT.json").write_text(
        json.dumps(
            SectionContext(
                section_id=section_data["section_id"],
                section_title="Fixture section",
                chapter_argument="Fixture argument",
                scope_description="Fixture scope",
                required_roles=section_data["required_roles"],
                minimum_unique_sources=8,
                minimum_direct_sources=5,
            ).model_dump(mode="json")
        ),
        encoding="utf-8",
    )
    (tmp_path / "SECTION_COVERAGE_PLAN.json").write_text(
        json.dumps(
            SectionCoveragePlan(
                section_id=section_data["section_id"],
                chapter_argument="Fixture argument",
            ).model_dump(mode="json")
        ),
        encoding="utf-8",
    )
    (tmp_path / "LOCAL_COVERAGE_AUDIT.json").write_text(
        json.dumps(
            LocalCoverageAudit(
                section_id=section_data["section_id"],
                blocking_gaps=[],
                sufficient_roles=list(section_data["required_roles"]),
            ).model_dump(mode="json")
        ),
        encoding="utf-8",
    )
    ledger = SectionSourceLedger(section_id=section_data["section_id"])
    roles = list(section_data["required_roles"])
    for index in range(8):
        ledger.sources.append(
            SourceEntry(
                paper_id=f"paper_{index}",
                title=f"Paper {index}",
                literature_role=roles[index % len(roles)],
                scope_fit=(
                    ScopeFit.direct if index < 2 else ScopeFit.adjacent
                ),
                canonical_chunk_ids=[f"chunk_{index}"],
                acquisition_status=AcquisitionStatus.fulltext,
                section_id=section_data["section_id"],
            )
        )
    ledger.total_sources = 8
    (tmp_path / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps(ledger.model_dump(mode="json")),
        encoding="utf-8",
    )
    _make_submit_section_gap_report(ctx)(
        json.dumps({
            "gaps": [{
                "role": "coverage_breadth",
                "severity": "important",
                "description": "Only two directly aligned sources were found.",
                "stop_reason": "bounded search exhausted",
                "is_blocking": False,
            }],
            "overall_coverage_status": "completed_with_open_gaps",
        })
    )

    result = _make_validate_section_coverage_package(ctx)()
    package = json.loads(
        (tmp_path / "SECTION_MATERIAL_PACKAGE.json").read_text(
            encoding="utf-8"
        )
    )
    assert "VALIDATION_PASSED" in result
    assert package["breadth_target_met"] is False
    assert package["coverage_status"] == "completed_with_open_gaps"
    assert package["blocking_gaps_remain"] is False
    assert "coverage_breadth" in package["gap_summary"]


def test_unreviewed_local_recall_never_enters_source_ledger(tmp_path: Path):
    """Broad local recall is not silently promoted into writing material."""
    from optomind_research.runtime.section_coverage_tool_registry import (
        _build_source_ledger,
        _make_inspect_section_local_coverage,
    )

    kb = tmp_path / "local.sqlite"
    _seed_fake_kb(kb, paper_count=2, role_hint="foundation nonlinear optical")
    ctx = _make_ctx(tmp_path, kb_sqlite=kb)
    _make_inspect_section_local_coverage(ctx)()
    assert (tmp_path / "LOCAL_CANDIDATE_LEDGER.json").exists()

    _build_source_ledger(ctx)
    ledger = json.loads(
        (tmp_path / "SECTION_SOURCE_LEDGER.json").read_text(encoding="utf-8")
    )
    assert ledger["sources"] == []


def test_failed_materialization_never_enters_adopted_source_ledger(
    tmp_path: Path,
):
    from optomind_research.runtime.section_coverage_tool_registry import (
        _build_source_ledger,
    )

    ctx = _make_ctx(tmp_path, kb_sqlite=None)
    ctx.section_data["required_roles"] = ["mechanism"]
    ctx.section_data["optional_roles"] = []
    (tmp_path / "SECTION_COVERAGE_PLAN.json").write_text(
        json.dumps(
            {"roles": {"mechanism": {"priority": "required"}}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "candidate_id": "cand_failed",
                        "decision": "approved",
                        "scope_fit": "direct",
                        "role": "mechanism",
                        "role_fit": ["mechanism"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "MATERIALIZATION_MANIFEST.json").write_text(
        json.dumps(
            {
                "papers": [
                    {
                        "candidate_id": "cand_failed",
                        "paper_id": "doi:10.1000/failed",
                        "role": "mechanism",
                        "acquisition_status": "failed",
                        "chunk_ids": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    _build_source_ledger(ctx)
    ledger = json.loads(
        (tmp_path / "SECTION_SOURCE_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )
    assert ledger["sources"] == []


def test_not_needed_role_is_excluded_after_local_audit(tmp_path: Path):
    """An explicit role plan wins over noisy multi-role local recall."""
    from optomind_research.runtime.section_coverage_tool_registry import (
        _build_source_ledger,
        _make_inspect_section_local_coverage,
        _make_submit_local_source_audit,
    )

    kb = tmp_path / "local.sqlite"
    _seed_fake_kb(
        kb,
        paper_count=3,
        role_hint="foundation method mechanism frontier controversy application",
    )
    ctx = _make_ctx(tmp_path, kb_sqlite=kb)
    _make_inspect_section_local_coverage(ctx)()
    local = json.loads(
        (tmp_path / "LOCAL_CANDIDATE_LEDGER.json").read_text(encoding="utf-8")
    )
    _make_submit_local_source_audit(ctx)(json.dumps([{
        "candidate_id": item["candidate_id"],
        "scope_fit": "direct",
        "decision": "approved",
        "audit_reason": "Fixture candidate performs this role.",
    } for item in local["candidates"]]))

    plan_roles = {
        role: {
            "role": role,
            "priority": "required" if role == "foundation" else "not_needed",
            "coverage_question": "fixture",
            "intended_synthesis": "fixture",
            "queries": [],
        }
        for role in (
            "foundation", "mechanism", "method",
            "frontier", "controversy", "application",
        )
    }
    (tmp_path / "SECTION_COVERAGE_PLAN.json").write_text(
        json.dumps({
            "schema_version": "2.0",
            "section_id": ctx.section_id,
            "chapter_argument": "fixture",
            "roles": plan_roles,
        }),
        encoding="utf-8",
    )
    # Required roles in the immutable section context always remain active.
    ctx.section_data["required_roles"] = ["foundation"]
    ctx.section_data["optional_roles"] = []
    _build_source_ledger(ctx)
    ledger = json.loads(
        (tmp_path / "SECTION_SOURCE_LEDGER.json").read_text(encoding="utf-8")
    )
    assert {source["literature_role"] for source in ledger["sources"]} == {"foundation"}


def test_not_usable_for_overrides_an_approved_role(tmp_path: Path):
    """A contradictory approval may never enter the writing material graph."""
    from optomind_research.runtime.section_coverage_tool_registry import (
        _build_source_ledger,
    )

    ctx = _make_ctx(tmp_path, kb_sqlite=None)
    ctx.section_data["required_roles"] = ["controversy"]
    ctx.section_data["optional_roles"] = []
    (tmp_path / "SECTION_COVERAGE_PLAN.json").write_text(
        json.dumps(
            {"roles": {"controversy": {"priority": "required"}}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps(
            {
                "section_id": ctx.section_id,
                "sources": [
                    {
                        "paper_id": "paper_conflict",
                        "literature_role": "controversy",
                        "scope_fit": "adjacent",
                        "canonical_chunk_ids": ["chunk_conflict"],
                        "acquisition_status": "fulltext",
                        "section_id": ctx.section_id,
                        "not_usable_for": ["controversy"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _build_source_ledger(ctx)
    ledger = json.loads(
        (tmp_path / "SECTION_SOURCE_LEDGER.json").read_text(encoding="utf-8")
    )
    assert ledger["sources"] == []


# ---------------------------------------------------------------------------
# SC-T35: inspect_section_local_coverage with KB — optional gap ≠ blocking
# ---------------------------------------------------------------------------

def test_inspect_local_coverage_optional_gap_not_blocking_with_kb(tmp_path: Path):
    """When a real KB has papers for required roles but none for an optional role,
    the optional role must appear in important_gaps (or sufficient), NOT blocking_gaps."""
    from optomind_research.runtime.tool_provider import SectionCoverageContext
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_inspect_section_local_coverage,
    )

    kb_path = tmp_path / "test_kb.sqlite"
    # Seed only "foundation" papers (required role)
    _seed_fake_kb(kb_path, paper_count=4, role_hint="foundation")

    section_data = _make_section_data()
    section_data = dict(section_data)
    section_data["required_roles"] = ["foundation"]
    section_data["optional_roles"] = ["application"]

    ctx = SectionCoverageContext(
        section_id=section_data["section_id"],
        section_data=section_data,
        kb_sqlite=kb_path,
        temp_kb_sqlite=tmp_path / "temp.sqlite",
        work_dir=tmp_path,
    )

    fn = _make_inspect_section_local_coverage(ctx)
    result = json.loads(fn())

    blocking = result["blocking_gaps"]
    assert "application" not in blocking, (
        f"Optional role 'application' must NOT be blocking even with zero papers: {blocking}"
    )


# ---------------------------------------------------------------------------
# SC-T36: Token budget stops worker between model calls (Fix 7)
# ---------------------------------------------------------------------------

def test_token_budget_stops_worker_mid_stream(tmp_path: Path):
    """ResearchWorker must stop with status=budget_exhausted when token_budget
    is exceeded after a ModelCallEndEvent, not only at ReplyEndEvent.

    Skipped: requires AgentScope model injection infrastructure not available offline.
    The behaviour is verified by code inspection of research_worker.py Fix7.
    """
    pytest.skip("ResearchWorker integration test requires AgentScope mock setup")

    # Use a fake model that always burns more tokens than the budget
    class HighTokenFakeModel:
        """Fake model that reports very high token usage and produces a valid reply."""
        current_model_name = "fake-high-token"

        def reply_with_events(self, messages, tools=None, **kwargs):
            from agentscope.models import ModelResponse
            from unittest.mock import MagicMock

            # Emit events: ModelCallStart, ModelCallEnd (with huge token counts), ReplyEnd
            start_ev = MagicMock()
            start_ev.__class__.__name__ = "ModelCallStartEvent"

            end_ev = MagicMock()
            end_ev.__class__.__name__ = "ModelCallEndEvent"
            end_ev.input_tokens = 999_999   # far above any budget
            end_ev.output_tokens = 100

            reply_ev = MagicMock()
            reply_ev.__class__.__name__ = "ReplyEndEvent"

            yield start_ev
            yield end_ev
            yield reply_ev

    # Build a minimal contract with a very tight token budget
    contract = TaskContract(
        run_id="test_budget_run",
        task_id="test_budget_task",
        goal="Do nothing — budget should exhaust before completion.",
        constraints=[],
        success_criteria=[],
        expected_outputs=[],
        allowed_tools=[],
        skill_ids=[],
        model_tier="standard_model",
        max_iters=20,
        wall_time_budget_seconds=300.0,
        token_budget=1_000,   # tiny budget — will be exceeded on first model call
    )

    work_dir = tmp_path / "runs"
    worker = ResearchWorker(runs_root=work_dir, tool_provider=None)

    # Inject fake model
    import unittest.mock as mock
    from optomind_research.runtime import agent_model_factory as amf

    fake_model = HighTokenFakeModel()
    with mock.patch.object(amf.AgentModelFactory, "build_agent", return_value=None), \
         mock.patch.object(amf.AgentModelFactory, "current_model_name",
                           new_callable=lambda: property(lambda self: "fake-high-token")):
        # This path is hard to fully mock without access to AgentScope internals.
        # Skip if AgentScope isn't importable or model integration is unavailable.
        pytest.skip("ResearchWorker integration test requires AgentScope mock setup")


# ---------------------------------------------------------------------------
# SC-T37: MaterializedPaper diagnostics non-empty when acquisition fails
# ---------------------------------------------------------------------------

def test_materialized_paper_diagnostics_non_empty_on_failure(tmp_path: Path):
    """When _ingest_single_candidate returns metadata_only (0 chunks), the
    download_error and parse_failure_reason fields must be non-empty strings."""
    from optomind_research.runtime.section_coverage_tool_registry import _ingest_single_candidate
    import optomind_research.m3_kb_ingest as m3
    import unittest.mock as mock

    cand = {
        "candidate_id": "cand_fail001",
        "title": "Challenge Page Paper",
        "doi": "10.9999/fail001",
        "year": 2023,
        "abstract": "nonlinear optics test",
        "is_oa": True,
        "pdf_url": "https://challenge.example.com/paper.pdf",
        "scope_fit": "direct",
    }

    # Mock KBIngester to return zero chunks (simulates challenge page / HTML download)
    mock_result = mock.MagicMock()
    mock_result.new_chunk_ids = []
    mock_result.reused_chunk_ids = []
    mock_result.new_paper_ids = []
    mock_result.stats = {"parse_failed": 1}

    with mock.patch(
        "optomind_research.m3_kb_ingest._try_download_bytes",
        return_value=b"%PDF-1.4\n" + (b"0" * 6_000),
    ), mock.patch.object(m3.KBIngester, "ingest_oa_candidates", return_value=mock_result):
        result = _ingest_single_candidate(cand, tmp_path / "kb.sqlite", tmp_path)

    assert result["acquisition_status"] == "metadata_only"
    assert result["download_error"], (
        f"download_error must be non-empty for metadata_only; got: {result['download_error']!r}"
    )
    assert result["parse_failure_reason"], (
        f"parse_failure_reason must be non-empty for metadata_only; got: {result['parse_failure_reason']!r}"
    )
    assert result["attempted_urls"], (
        f"attempted_urls must be non-empty when a URL was provided; got: {result['attempted_urls']!r}"
    )
    assert "parse_failed_after_download" in result["download_errors_by_url"][cand["pdf_url"]]


# ---------------------------------------------------------------------------
# SC-T38 (P0-A): Direct scope → abstract fallback retained as factual evidence
# ---------------------------------------------------------------------------

def test_direct_scope_abstract_fallback_is_factual(tmp_path: Path):
    """P0-A integration: A directly relevant paper whose fulltext download fails must
    still produce an abstract-only chunk with factual_support_allowed=True.

    The old bug: _grade_map mapped 'direct' → 'A', which is NOT in _candidate_relevance_tier's
    accepted set {"direct","partial","strong",...}, so the abstract fallback was skipped with
    reason 'weak_or_unreviewed_relevance'. Fixed by using 'direct' as the grade label.
    """
    from optomind_research.m3_kb_ingest import (
        _abstract_fallback_reason, candidate_evidence_policy, _candidate_relevance_tier,
    )

    # Simulate a candidate as built by _ingest_single_candidate with scope_fit="direct"
    cand_direct = {
        "candidate_id": "cand_direct_test",
        "title": "Direct Scope Kerr Waveguide Paper",
        "doi": "10.1234/direct_test",
        "abstract": "This paper presents measurements of the Kerr effect in photonic waveguides.",
        "is_oa": True,
        "llm_scope_fit": "in_domain",
        "llm_retrieval_role": "evidence_candidate",
        "llm_relevance_grade": "direct",   # P0-A fix: 'direct' not 'A'
        "llm_support_status": "supporting",
        "backends": ["openalex"],
    }

    # Should NOT be blocked by _abstract_fallback_reason
    reason = _abstract_fallback_reason(cand_direct)
    assert reason == "", (
        f"Direct scope candidate should not be blocked from abstract fallback; "
        f"got reason: {reason!r}. Check that llm_relevance_grade='direct' is accepted."
    )

    # Policy must allow factual support
    policy = candidate_evidence_policy(cand_direct)
    assert policy["factual_support_allowed"] is True, (
        f"direct scope should allow factual support; policy={policy}"
    )

    # Contrast: old 'A' grade must NOT be in the accepted tier set
    cand_old_grade = dict(cand_direct, llm_relevance_grade="A")
    tier = _candidate_relevance_tier(cand_old_grade)
    assert tier == "", (
        f"Grade 'A' should not be recognized as a valid relevance tier; "
        f"this confirms the old bug. tier={tier!r}"
    )


# ---------------------------------------------------------------------------
# SC-T39 (P0-A): Adjacent scope → abstract fallback retained as method-transfer only
# ---------------------------------------------------------------------------

def test_adjacent_scope_abstract_fallback_is_method_transfer(tmp_path: Path):
    """P0-A: An adjacent-scope paper must still trigger abstract fallback but its
    chunk must be method_transfer, NOT factual evidence."""
    from optomind_research.m3_kb_ingest import (
        _abstract_fallback_reason, candidate_evidence_policy,
    )

    cand_adjacent = {
        "candidate_id": "cand_adj_test",
        "title": "Adjacent Scope Paper",
        "doi": "10.1234/adj_test",
        "abstract": "A silicon photonics paper with adjacent relevance.",
        "is_oa": True,
        "llm_scope_fit": "cross_domain_analogy",
        "llm_retrieval_role": "evidence_candidate",
        "llm_relevance_grade": "adjacent",
        "llm_support_status": "supporting",
        "backends": ["semantic_scholar"],
    }

    # Adjacent must NOT be blocked from abstract fallback (just classified differently)
    reason = _abstract_fallback_reason(cand_adjacent)
    assert reason == "", f"Adjacent abstract should be retained for method transfer: {reason}"
    # 'adjacent' is not in the accepted tier set {"direct","partial","strong",...}
    # so it will be blocked with "weak_or_unreviewed_relevance" — this is correct behavior
    # Adjacent abstracts should be retained as method-transfer/background, not factual
    policy = candidate_evidence_policy(cand_adjacent)
    assert policy["retrieval_role"] in ("method_transfer", "background_only"), (
        f"adjacent scope must NOT allow factual support; policy={policy}"
    )
    assert policy["factual_support_allowed"] is False, (
        f"adjacent scope must not have factual_support_allowed=True; policy={policy}"
    )

    from optomind_research.m3_kb_ingest import KBIngester
    cand_adjacent.update({
        "source_url": "https://example.test/adjacent",
        "download_attempts_complete": True,
    })
    result = KBIngester(kb_sqlite=tmp_path / "adjacent.sqlite").ingest_oa_candidates(
        [cand_adjacent],
        {"claim_id": "adjacent_claim", "statement": "adjacent method transfer"},
        max_successes=1,
    )
    assert result.method_transfer_chunk_ids
    assert result.factual_candidate_chunk_ids == []


# ---------------------------------------------------------------------------
# SC-T40 (P0-B): Cross-backend dedup preserves complementary URL + abstract
# ---------------------------------------------------------------------------

def test_dedup_cross_backend_merges_url_and_abstract(tmp_path: Path):
    """P0-B: When OpenAlex returns a candidate with empty pdf_url and S2 returns the
    same DOI with a valid PDF URL and a longer abstract, _dedup_raw_candidates must
    merge them so the final candidate has S2's pdf_url, the longer abstract, and both
    backends listed.  The old bug discarded the second backend's data entirely."""
    from optomind_research.runtime.section_coverage_tool_registry import _dedup_raw_candidates

    oa_cand = {
        "candidate_id": "cand_oa001",
        "title": "Kerr Effect in Photonic Waveguides",
        "doi": "10.1234/kerr_test",
        "year": 2022,
        "venue": "Optics Express",
        "abstract": "Short abstract from OpenAlex.",
        "pdf_url": "",
        "url_for_pdf": "",
        "best_oa_url": "",
        "oa_url": "",
        "is_oa": False,
        "citation_count": 5,
        "backends": ["openalex"],
        "query_texts": ["kerr effect waveguide"],
        "alternate_urls": [],
    }
    s2_cand = {
        "candidate_id": "cand_s2001",
        "title": "Kerr Effect in Photonic Waveguides",
        "doi": "10.1234/kerr_test",  # same DOI
        "year": 2022,
        "venue": "",
        "abstract": "Much longer abstract from Semantic Scholar describing methodology and results.",
        "pdf_url": "https://s2.example.com/paper.pdf",
        "url_for_pdf": "",
        "best_oa_url": "https://s2.example.com/paper.pdf",
        "oa_url": "",
        "is_oa": True,
        "citation_count": 3,
        "backends": ["semantic_scholar"],
        "query_texts": ["photonic kerr nonlinear"],
        "alternate_urls": [],
    }

    merged = _dedup_raw_candidates([oa_cand, s2_cand])

    assert len(merged) == 1, f"Same DOI must produce 1 merged candidate, got {len(merged)}"
    m = merged[0]
    assert m["pdf_url"] == "https://s2.example.com/paper.pdf", (
        f"S2's pdf_url must be preserved after dedup; got {m['pdf_url']!r}"
    )
    assert m["best_oa_url"] == "https://s2.example.com/paper.pdf", (
        f"S2's best_oa_url must be preserved; got {m['best_oa_url']!r}"
    )
    assert "openalex" in m["backends"] and "semantic_scholar" in m["backends"], (
        f"Both backends must be listed after merge; got {m['backends']}"
    )
    assert len(m["abstract"]) > len(oa_cand["abstract"]), (
        f"Longer abstract (S2) must win after merge; got {m['abstract']!r}"
    )
    assert m["citation_count"] == 5, (
        f"Higher citation count (OpenAlex=5) must win; got {m['citation_count']}"
    )
    assert m["is_oa"] is True, (
        f"is_oa must be True after merging with S2's is_oa=True; got {m['is_oa']}"
    )

    same_slot = _dedup_raw_candidates([
        dict(oa_cand, pdf_url="https://repo-a.example/paper.pdf",
             content_urls={"pdf": "https://repo-a.example/paper.pdf"}),
        dict(s2_cand, pdf_url="https://repo-b.example/paper.pdf",
             content_urls={"pdf": "https://repo-b.example/paper.pdf"}),
    ])[0]
    assert "https://repo-a.example/paper.pdf" in same_slot["alternate_urls"]
    assert "https://repo-b.example/paper.pdf" in same_slot["alternate_urls"]


# ---------------------------------------------------------------------------
# SC-T41 (P1-E): Failed acquisition is retryable — manifest entry replaced, not duplicated
# ---------------------------------------------------------------------------

def test_failed_acquisition_retry_replaces_manifest_entry(tmp_path: Path):
    """P1-E: When acquire_and_materialize_oa_papers is called twice for the same
    candidate_id, and the first call yields acquisition_status=failed, the second
    call must replace the failed entry in-place rather than appending a duplicate."""
    import unittest.mock as mock
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_acquire_and_materialize_oa_papers,
    )
    from optomind_research.runtime.artifact_schemas import (
        OACandidate, OACandidateLedger, CandidateDecision, ScopeFit,
    )
    import json as _json

    ctx = _make_ctx(tmp_path)
    ctx.min_mode_max_total_papers = 10

    cid = "cand_retry001"
    cand = {
        "candidate_id": cid,
        "title": "Retry Test Paper",
        "doi": "10.1234/retry",
        "abstract": "Nonlinear optics retry test.",
        "is_oa": True,
        "pdf_url": "https://retry.example.com/paper.pdf",
        "scope_fit": "direct",
    }
    ctx.register_candidates([cand])

    # Write ledger with this candidate approved
    ledger = OACandidateLedger(section_id=ctx.section_id)
    ledger.candidates.append(OACandidate(
        candidate_id=cid, section_id=ctx.section_id, role="frontier",
        title="Retry Test Paper", doi="10.1234/retry",
        decision=CandidateDecision.approved, scope_fit=ScopeFit.direct,
    ))
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        ledger.model_dump_json(), encoding="utf-8"
    )

    fn = _make_acquire_and_materialize_oa_papers(ctx)

    # First call: ingestion fails
    def _fail_ingest(c, kb, wd):
        return {"acquisition_status": "failed", "paper_id": "", "chunk_ids": [],
                "new_paper": True, "new_chunks": 0, "reused_chunks": 0,
                "download_url": "", "download_error": "connection refused",
                "attempted_urls": ["https://retry.example.com/paper.pdf"],
                "download_errors_by_url": {"https://retry.example.com/paper.pdf": "connection refused"},
                "content_type_detected": "", "parse_failure_reason": ""}

    mod = "optomind_research.runtime.section_coverage_tool_registry._ingest_single_candidate"
    with mock.patch(mod, side_effect=_fail_ingest):
        fn(role="frontier", candidate_ids=_json.dumps([cid]), max_papers=1)

    manifest_path = tmp_path / "MATERIALIZATION_MANIFEST.json"
    man1 = _json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(man1["papers"]) == 1
    assert man1["papers"][0]["acquisition_status"] == "failed"

    # Second call: ingestion succeeds
    def _succeed_ingest(c, kb, wd):
        return {"acquisition_status": "fulltext", "paper_id": "paper_retry001",
                "chunk_ids": ["chunk_retry001_0"], "new_paper": True,
                "new_chunks": 1, "reused_chunks": 0,
                "download_url": "https://retry.example.com/paper.pdf", "download_error": "",
                "attempted_urls": ["https://retry.example.com/paper.pdf"],
                "download_errors_by_url": {}, "content_type_detected": "application/pdf",
                "parse_failure_reason": ""}

    with mock.patch(mod, side_effect=_succeed_ingest):
        fn(role="frontier", candidate_ids=_json.dumps([cid]), max_papers=1)

    man2 = _json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(man2["papers"]) == 1, (
        f"Manifest must have exactly 1 entry after retry (not 2); got {len(man2['papers'])}"
    )
    assert man2["papers"][0]["acquisition_status"] == "fulltext", (
        f"Retried entry must be fulltext after successful retry; got {man2['papers'][0]['acquisition_status']!r}"
    )


# ---------------------------------------------------------------------------
# SC-T42 (P0-D): _probe_url_waterfall records per-URL diagnostics truthfully
# ---------------------------------------------------------------------------

def test_probe_url_waterfall_diagnostics(tmp_path: Path):
    """P0-D: _probe_url_waterfall must populate attempted_urls, download_errors_by_url,
    and content_type_detected accurately, including per-URL error messages.
    Only URLs that were actually tried appear in attempted_urls."""
    import unittest.mock as mock
    from optomind_research.runtime.section_coverage_tool_registry import _probe_url_waterfall

    url_fail = "https://fail.example.com/paper.pdf"
    url_challenge = "https://challenge.example.com/paper.html"
    url_tiny_pdf = "https://tiny.example.com/error.pdf"
    url_good = "https://good.example.com/paper.pdf"
    doi = "10.1234/probe_test"

    def _fake_download(url):
        if url == url_fail:
            raise ConnectionError("connection refused")
        if url == url_challenge:
            # Minimal HTML, too short to be scholarly (< 3000 bytes)
            return b"<html><body>Please verify you are human.</body></html>"
        if url == url_tiny_pdf:
            return b"%PDF-1.4 tiny error payload"
        if url == url_good:
            return b"%PDF-1.4\n" + (b"0" * 6_000)
        return None

    with mock.patch(
        "optomind_research.m3_kb_ingest._try_download_bytes",
        side_effect=_fake_download,
    ):
        raw, source_url, attempted, errors_by_url, ct = _probe_url_waterfall(
            [url_fail, url_challenge, url_tiny_pdf, url_good], doi, tmp_path
        )

    assert url_fail in attempted
    assert url_challenge in attempted
    assert url_good in attempted
    assert len(attempted) == 4, f"All 4 URLs were tried; got {attempted}"

    assert url_fail in errors_by_url, "Failed URL must appear in errors_by_url"
    assert "connection" in errors_by_url[url_fail].lower() or "download_error" in errors_by_url[url_fail]
    assert url_challenge in errors_by_url, "Challenge page URL must appear in errors_by_url"
    assert "challenge" in errors_by_url[url_challenge].lower() or "too_short" in errors_by_url[url_challenge]
    assert "pdf_too_short" in errors_by_url[url_tiny_pdf]

    assert source_url == url_good, f"Successful URL must be returned as source_url; got {source_url!r}"
    assert ct == "application/pdf"
    assert raw is not None and raw[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# SC-T43 (P0-C): _enrich_candidate_oa_routes adds Unpaywall routes (mockable)
# ---------------------------------------------------------------------------

def test_enrich_candidate_oa_routes_adds_unpaywall_url(tmp_path: Path):
    """P0-C: _enrich_candidate_oa_routes must call UnpaywallBackend.lookup and
    append any new URLs to alternate_urls without duplicating existing ones."""
    import unittest.mock as mock
    from optomind_research.runtime.section_coverage_tool_registry import _enrich_candidate_oa_routes

    cand = {
        "candidate_id": "cand_uw001",
        "title": "Unpaywall Enrichment Test Paper",
        "doi": "10.1234/uw_test",
        "pdf_url": "https://existing.example.com/paper.pdf",
        "best_oa_url": "",
        "oa_url": "",
        "is_oa": False,
        "alternate_urls": [],
    }

    uw_response = {
        "best_oa_url": "https://unpaywall.example.com/paper.pdf",
        "pdf_url": "https://unpaywall.example.com/paper.pdf",
        "oa_url": "https://repo.example.com/paper",
        "repository_url": "https://arxiv.example.com/abs/1234",
        "is_oa": True,
    }

    with mock.patch(
        "tools.academic_backends.unpaywall_backend.UnpaywallBackend.lookup",
        return_value=uw_response,
    ), mock.patch(
        "tools.academic_backends.openalex_backend.OpenAlexBackend.get_work",
        return_value=None,
    ):
        enriched = _enrich_candidate_oa_routes(cand)

    assert enriched is not cand, "Must return a new dict (original unchanged)"
    assert enriched.get("best_oa_url") == "https://unpaywall.example.com/paper.pdf"
    assert enriched.get("is_oa") is True, "is_oa must propagate from Unpaywall"

    # The existing pdf_url must not be duplicated in alternate_urls
    alt = enriched.get("alternate_urls", [])
    assert "https://existing.example.com/paper.pdf" not in alt, (
        f"Existing pdf_url must not be duplicated in alternate_urls; got {alt}"
    )
    # New Unpaywall routes not already in the candidate must appear in alternate_urls
    new_uw_urls = {"https://unpaywall.example.com/paper.pdf",
                   "https://repo.example.com/paper",
                   "https://arxiv.example.com/abs/1234"}
    for u in new_uw_urls:
        assert u in alt or enriched.get("best_oa_url") == u, (
            f"Unpaywall URL {u!r} must be in alternate_urls or best_oa_url; alt={alt}"
        )


def test_enrich_candidate_oa_routes_real_shapes_and_independent_failures():
    """Unpaywall locations and exact-DOI OpenAlex content metadata are unioned."""
    from unittest import mock
    from optomind_research.runtime.section_coverage_tool_registry import _enrich_candidate_oa_routes

    candidate = {
        "candidate_id": "oa-shapes",
        "doi": "https://doi.org/10.5555/OA.TEST",
        "title": "OA route shape test",
        "alternate_urls": [],
        "content_urls": {},
    }
    unpaywall = {
        "doi": "10.5555/oa.test",
        "is_oa": True,
        "oa_status": "green",
        "best_oa_url": "https://repo.example/best.pdf",
        "oa_locations": [
            {"url": "https://repo.example/landing", "url_for_pdf": "https://repo.example/a.pdf"},
            {"url": "https://publisher.example/html", "url_for_pdf": "https://publisher.example/b.pdf"},
        ],
    }
    openalex = {
        "doi": "10.5555/oa.test",
        "openalex_id": "W123456",
        "is_oa": True,
        "oa_status": "gold",
        "pdf_url": "https://content.openalex.org/works/W123456.pdf",
        "open_access_url": "https://publisher.example/article",
        "content_urls": {
            "pdf": "https://content.openalex.org/works/W123456.pdf",
            "grobid_xml": "https://content.openalex.org/works/W123456.grobid-xml",
        },
        "raw_metadata": {
            "content_urls": {"pdf": "https://content.openalex.org/works/W123456.pdf"},
            "oa_locations": [{"url": "https://archive.example/item"}],
        },
    }

    with mock.patch(
        "tools.academic_backends.unpaywall_backend.UnpaywallBackend.lookup",
        return_value=unpaywall,
    ), mock.patch(
        "tools.academic_backends.openalex_backend.OpenAlexBackend.get_work",
        return_value=openalex,
    ):
        enriched = _enrich_candidate_oa_routes(candidate)

    routes = set(enriched["alternate_urls"]) | {enriched.get("best_oa_url", "")}
    assert "https://repo.example/a.pdf" in routes
    assert "https://publisher.example/b.pdf" in routes
    assert "https://archive.example/item" in routes
    assert enriched["openalex_id"] == "W123456"
    assert enriched["content_urls"]["grobid_xml"].endswith(".grobid-xml")

    # A failed Unpaywall resolver must not suppress the bounded OpenAlex lookup.
    with mock.patch(
        "tools.academic_backends.unpaywall_backend.UnpaywallBackend.lookup",
        side_effect=RuntimeError("offline mock failure"),
    ), mock.patch(
        "tools.academic_backends.openalex_backend.OpenAlexBackend.get_work",
        return_value=openalex,
    ):
        openalex_only = _enrich_candidate_oa_routes(candidate)
    assert openalex_only["openalex_id"] == "W123456"


# ---------------------------------------------------------------------------
# SC-T44 (P1-G): Non-allowlisted tool VALIDATION_PASSED does not complete task
# ---------------------------------------------------------------------------

def test_non_allowlisted_tool_validation_passed_does_not_complete(tmp_path: Path):
    """P1-G: StopController.check_completion must return validation_failed when
    validation_tool_result=None, which is what the ResearchWorker passes when no
    allowlisted tool (validate_task_result / validate_section_coverage_package) has
    returned VALIDATION_PASSED.  A non-allowlisted tool (e.g. write_task_note) returning
    'VALIDATION_PASSED' in its text MUST NOT set last_validation_result."""
    from optomind_research.runtime.stop_controller import StopController
    from optomind_research.runtime.task_contract import TaskContract, TaskStatus

    contract = TaskContract(
        run_id="test_p1g_run",
        task_id="test_p1g_task",
        goal="P1-G regression test.",
        constraints=[],
        success_criteria=[],
        expected_outputs=[],
        allowed_tools=["write_task_note"],
        skill_ids=[],
        model_tier="standard_model",
        max_iters=10,
        wall_time_budget_seconds=60.0,
        token_budget=100_000,
    )
    ctrl = StopController(contract, tmp_path)

    # Simulate: write_task_note returned "VALIDATION_PASSED" but it is NOT an allowlisted
    # validation tool, so the ResearchWorker leaves last_validation_result=None.
    status, reason, _, criteria_failed = ctrl.check_completion(validation_tool_result=None)
    assert status == TaskStatus.validation_failed, (
        f"When validation_tool_result=None (non-allowlisted tool), status must be "
        f"validation_failed; got {status!r}, reason={reason!r}"
    )
    assert any("validate_task_result" in f or "not_passed" in f for f in criteria_failed), (
        f"criteria_failed must note missing validation; got {criteria_failed}"
    )

    # Also confirm that when the correct allowlisted tool returns VALIDATION_PASSED, it completes
    status_ok, _, _, _ = ctrl.check_completion(validation_tool_result="VALIDATION_PASSED")
    assert status_ok == TaskStatus.completed, (
        f"With VALIDATION_PASSED from allowlisted tool, status must be completed; got {status_ok!r}"
    )


# ---------------------------------------------------------------------------
# SC-T45 (P1-H): Token budget exhaustion is deterministic — replaces skipped SC-T36
# ---------------------------------------------------------------------------

def test_token_budget_exhaustion_via_stop_controller(tmp_path: Path):
    """P1-H (replaces skipped SC-T36): StopController.check() must return
    budget_exhausted when input_tokens >= token_budget.  This is the deterministic
    gate that ResearchWorker uses after each ModelCallEndEvent."""
    from optomind_research.runtime.stop_controller import StopController
    from optomind_research.runtime.task_contract import TaskContract, TaskStatus

    contract = TaskContract(
        run_id="test_budget_run",
        task_id="test_budget_task",
        goal="Budget exhaustion test.",
        constraints=[],
        success_criteria=[],
        expected_outputs=[],
        allowed_tools=[],
        skill_ids=[],
        model_tier="standard_model",
        max_iters=100,
        wall_time_budget_seconds=3600.0,
        token_budget=5_000,
    )
    ctrl = StopController(contract, tmp_path)

    # Under budget: should still be running
    status, reason = ctrl.check(iter_count=1, wall_time_seconds=1.0, input_tokens=4_999)
    assert status == TaskStatus.running, f"Under budget must stay running; got {status!r}"

    # At exact budget: exhausted
    status_at, reason_at = ctrl.check(iter_count=1, wall_time_seconds=1.0, input_tokens=5_000)
    assert status_at == TaskStatus.budget_exhausted, (
        f"At token_budget must be budget_exhausted; got {status_at!r}, reason={reason_at!r}"
    )
    assert "5000" in reason_at or "input_tokens" in reason_at, (
        f"Stop reason must mention tokens; got {reason_at!r}"
    )

    # Far over budget: also exhausted
    status_over, _ = ctrl.check(iter_count=2, wall_time_seconds=5.0, input_tokens=999_999)
    assert status_over == TaskStatus.budget_exhausted

    # Verify iter_count budget also triggers exhaustion independently
    contract2 = TaskContract(
        run_id="test_iter_budget", task_id="task2", goal="iter test",
        constraints=[], success_criteria=[], expected_outputs=[], allowed_tools=[],
        skill_ids=[], model_tier="standard_model",
        max_iters=3, wall_time_budget_seconds=3600.0, token_budget=1_000_000,
    )
    ctrl2 = StopController(contract2, tmp_path)
    status_iter, reason_iter = ctrl2.check(iter_count=3, wall_time_seconds=1.0, input_tokens=0)
    assert status_iter == TaskStatus.budget_exhausted
    assert "max_iters" in reason_iter or "3" in reason_iter


# ---------------------------------------------------------------------------
# SC-T46 (Acceptance E2E): Full deterministic end-to-end pipeline
# ---------------------------------------------------------------------------

def test_e2e_pipeline_acceptance(tmp_path: Path):
    """Acceptance E2E: deterministic, no real network.

    Verifies all five acceptance criteria in a single chained test:
    1. OpenAlex empty URL + S2 valid PDF/abstract survives dedup with merged metadata.
    2. Direct scope candidate: _candidate_relevance_tier accepts 'direct' grade (P0-A).
    3. Adjacent scope candidate: factual_support_allowed=False, method_transfer role (P0-A).
    4. Failed acquisition retry: manifest entry replaced in-place, not duplicated (P1-E).
    5. Diagnostics: attempted_urls and download_errors_by_url reflect actual attempts (P0-D).
    """
    import unittest.mock as mock
    import json as _json
    from optomind_research.runtime.section_coverage_tool_registry import (
        _dedup_raw_candidates,
        _probe_url_waterfall,
        _make_acquire_and_materialize_oa_papers,
    )
    from optomind_research.runtime.artifact_schemas import (
        OACandidate, OACandidateLedger, CandidateDecision, ScopeFit,
    )
    from optomind_research.m3_kb_ingest import (
        _abstract_fallback_reason, candidate_evidence_policy, _candidate_relevance_tier,
    )

    # ------------------------------------------------------------------
    # 1. Dedup: OpenAlex (no URL) + S2 (valid PDF) → merge preserves S2 URL
    # ------------------------------------------------------------------
    oa = {"candidate_id": "e2e_oa", "title": "E2E Test Paper",
          "doi": "10.1234/e2e", "abstract": "Short.", "pdf_url": "",
          "best_oa_url": "", "is_oa": False, "citation_count": 2,
          "backends": ["openalex"], "query_texts": [], "alternate_urls": []}
    s2 = {"candidate_id": "e2e_s2", "title": "E2E Test Paper",
          "doi": "10.1234/e2e",
          "abstract": "Longer abstract with methodology and results for E2E test.",
          "pdf_url": "https://s2.e2e.example.com/paper.pdf",
          "best_oa_url": "https://s2.e2e.example.com/paper.pdf",
          "is_oa": True, "citation_count": 1,
          "backends": ["semantic_scholar"], "query_texts": [], "alternate_urls": []}

    merged = _dedup_raw_candidates([oa, s2])
    assert len(merged) == 1, f"Dedup must yield 1 candidate; got {len(merged)}"
    m = merged[0]
    assert m["pdf_url"] == "https://s2.e2e.example.com/paper.pdf"
    assert m["is_oa"] is True
    assert "openalex" in m["backends"] and "semantic_scholar" in m["backends"]

    # ------------------------------------------------------------------
    # 2. Direct scope: grade 'direct' accepted by _candidate_relevance_tier (P0-A fix)
    # ------------------------------------------------------------------
    cand_direct = {
        "doi": "10.1234/e2e_direct", "title": "Direct E2E Paper",
        "abstract": "Direct scope abstract for E2E test.",
        "source_url": "https://e2e.direct.example.com/paper",
        "backends": ["openalex"],
        "llm_relevance_grade": "direct", "llm_scope_fit": "in_domain",
        "llm_retrieval_role": "evidence_candidate", "llm_support_status": "supporting"
    }
    tier_direct = _candidate_relevance_tier(cand_direct)
    assert tier_direct != "", (
        f"Grade 'direct' must be recognized by _candidate_relevance_tier (P0-A); got {tier_direct!r}"
    )
    reason_direct = _abstract_fallback_reason(cand_direct)
    assert reason_direct == "", (
        f"Direct scope must not block abstract fallback; got reason={reason_direct!r}"
    )
    policy_direct = candidate_evidence_policy(cand_direct)
    assert policy_direct["factual_support_allowed"] is True

    # ------------------------------------------------------------------
    # 3. Adjacent scope: method-transfer only, not factual (P0-A fix)
    # ------------------------------------------------------------------
    cand_adj = {
        "doi": "10.1234/e2e_adj", "title": "Adjacent E2E Paper",
        "abstract": "Adjacent scope abstract for E2E test.",
        "source_url": "https://e2e.adj.example.com/paper",
        "backends": ["semantic_scholar"],
        "llm_relevance_grade": "adjacent", "llm_scope_fit": "cross_domain_analogy",
        "llm_retrieval_role": "evidence_candidate", "llm_support_status": "supporting"
    }
    policy_adj = candidate_evidence_policy(cand_adj)
    assert policy_adj["factual_support_allowed"] is False
    assert policy_adj["retrieval_role"] in ("method_transfer", "background_only")

    # ------------------------------------------------------------------
    # 4. Retry semantics: failed → fulltext replaces entry (P1-E)
    # ------------------------------------------------------------------
    ctx = _make_ctx(tmp_path)
    ctx.min_mode_max_total_papers = 10
    cid = "e2e_retry"
    cand_retry = {"candidate_id": cid, "title": "E2E Retry", "doi": "10.1234/e2e_retry",
                  "abstract": "retry test", "is_oa": True,
                  "pdf_url": "https://e2e.example.com/retry.pdf", "scope_fit": "direct"}
    ctx.register_candidates([cand_retry])
    ledger_e2e = OACandidateLedger(section_id=ctx.section_id)
    ledger_e2e.candidates.append(OACandidate(
        candidate_id=cid, section_id=ctx.section_id, role="frontier",
        title="E2E Retry", doi="10.1234/e2e_retry",
        decision=CandidateDecision.approved, scope_fit=ScopeFit.direct,
    ))
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        ledger_e2e.model_dump_json(), encoding="utf-8"
    )
    fn = _make_acquire_and_materialize_oa_papers(ctx)
    mod = "optomind_research.runtime.section_coverage_tool_registry._ingest_single_candidate"

    def _fail(c, kb, wd):
        return {"acquisition_status": "failed", "paper_id": "", "chunk_ids": [],
                "new_paper": True, "new_chunks": 0, "reused_chunks": 0,
                "download_url": "", "download_error": "timeout",
                "attempted_urls": ["https://e2e.example.com/retry.pdf"],
                "download_errors_by_url": {"https://e2e.example.com/retry.pdf": "timeout"},
                "content_type_detected": "", "parse_failure_reason": ""}

    def _ok(c, kb, wd):
        return {"acquisition_status": "fulltext", "paper_id": "paper_e2e",
                "chunk_ids": ["chunk_e2e_0"], "new_paper": True, "new_chunks": 1,
                "reused_chunks": 0, "download_url": "https://e2e.example.com/retry.pdf",
                "download_error": "", "attempted_urls": ["https://e2e.example.com/retry.pdf"],
                "download_errors_by_url": {}, "content_type_detected": "application/pdf",
                "parse_failure_reason": ""}

    with mock.patch(mod, side_effect=_fail):
        fn(role="frontier", candidate_ids=_json.dumps([cid]), max_papers=1)
    with mock.patch(mod, side_effect=_ok):
        fn(role="frontier", candidate_ids=_json.dumps([cid]), max_papers=1)

    manifest_data = _json.loads((tmp_path / "MATERIALIZATION_MANIFEST.json").read_text())
    assert len(manifest_data["papers"]) == 1, "Retry must replace, not duplicate"
    assert manifest_data["papers"][0]["acquisition_status"] == "fulltext"

    # ------------------------------------------------------------------
    # 5. Diagnostics: _probe_url_waterfall reflects actual attempts (P0-D)
    # ------------------------------------------------------------------
    url_err = "https://diag.example.com/fail.pdf"
    url_ok = "https://diag.example.com/ok.pdf"

    def _fake_dl(url):
        if url == url_err:
            raise ConnectionError("refused")
        return b"%PDF-1.4\n" + (b"0" * 6_000)

    with mock.patch("optomind_research.m3_kb_ingest._try_download_bytes", side_effect=_fake_dl):
        _, src, attempted, err_map, ct = _probe_url_waterfall(
            [url_err, url_ok], "10.1234/diag", tmp_path
        )

    assert url_err in attempted and url_ok in attempted
    assert url_err in err_map, "Failed URL must appear in errors_by_url"
    assert src == url_ok
    assert ct == "application/pdf"


def test_ingest_reuses_probed_local_file_and_redacts_diagnostics(tmp_path: Path):
    """A successful probe is reused locally and secret URL values stay out of artifacts."""
    from types import SimpleNamespace
    from unittest import mock
    from optomind_research.runtime.section_coverage_tool_registry import _ingest_single_candidate

    candidate = {
        "candidate_id": "local_reuse",
        "title": "Local reuse test",
        "doi": "10.1234/local-reuse",
        "abstract": "A sufficiently descriptive abstract for deterministic fallback testing.",
        "is_oa": True,
        "pdf_url": "https://example.test/paper.pdf?token=super-secret",
        "scope_fit": "direct",
        "backends": ["mock"],
    }
    fake_result = SimpleNamespace(
        new_chunk_ids=["m3gap:10.1234_local-reuse:0000"],
        reused_chunk_ids=[],
        new_paper_ids=["doi:10.1234/local-reuse"],
    )

    with mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry._enrich_candidate_oa_routes",
        side_effect=lambda c: c,
    ), mock.patch(
        "optomind_research.m3_kb_ingest._try_download_bytes",
        return_value=b"%PDF-1.4\n" + (b"0" * 6_000),
    ), mock.patch(
        "optomind_research.m3_kb_ingest.KBIngester"
    ) as ingester_cls:
        ingester_cls.return_value.ingest_oa_candidates.return_value = fake_result
        result = _ingest_single_candidate(candidate, tmp_path / "staging.sqlite", tmp_path)

    passed_candidate = ingester_cls.return_value.ingest_oa_candidates.call_args.args[0][0]
    assert Path(passed_candidate["local_download_path"]).is_file()
    assert passed_candidate["download_attempts_complete"] is True
    assert "super-secret" not in json.dumps(result)
    assert "REDACTED" in result["attempted_urls"][0]


# ---------------------------------------------------------------------------
# SC-T47--T49: deterministic marginal-value stopping
# ---------------------------------------------------------------------------

def _write_coverage_prerequisites(
    tmp_path: Path,
    section_data: Dict[str, Any],
    *,
    sources: List[Any],
) -> None:
    """Create the durable artifacts a provider needs for auto-finalization."""

    from optomind_research.runtime.artifact_schemas import (
        LocalCoverageAudit,
        SectionContext,
        SectionCoveragePlan,
        SectionSourceLedger,
    )

    (tmp_path / "SECTION_CONTEXT.json").write_text(
        SectionContext(
            section_id=section_data["section_id"],
            section_title=section_data["title"],
            chapter_argument=section_data["chapter_argument"],
            scope_description=section_data.get("scope_description", ""),
            required_roles=section_data["required_roles"],
        ).model_dump_json(),
        encoding="utf-8",
    )
    (tmp_path / "SECTION_COVERAGE_PLAN.json").write_text(
        SectionCoveragePlan(
            section_id=section_data["section_id"],
            chapter_argument=section_data["chapter_argument"],
        ).model_dump_json(),
        encoding="utf-8",
    )
    (tmp_path / "LOCAL_COVERAGE_AUDIT.json").write_text(
        LocalCoverageAudit(
            section_id=section_data["section_id"],
            blocking_gaps=[],
            sufficient_roles=list(section_data["required_roles"]),
        ).model_dump_json(),
        encoding="utf-8",
    )
    ledger = SectionSourceLedger(
        section_id=section_data["section_id"],
        sources=sources,
        total_sources=len(sources),
    )
    (tmp_path / "SECTION_SOURCE_LEDGER.json").write_text(
        ledger.model_dump_json(), encoding="utf-8"
    )


def test_provider_auto_finalizes_when_durable_coverage_is_sufficient(
    tmp_path: Path,
):
    """A valid package must close before another model/download turn occurs."""

    from optomind_research.runtime.artifact_schemas import (
        AcquisitionStatus,
        ScopeFit,
        SourceEntry,
    )
    from optomind_research.runtime.section_coverage_tool_registry import (
        SectionCoverageToolProvider,
    )

    section_data = _make_section_data()
    section_data["literature_coverage_target"] = {
        "minimum_unique_sources": 4,
        "minimum_direct_sources": 4,
    }
    ctx = _make_ctx(tmp_path)
    ctx.section_data = section_data
    sources = [
        SourceEntry(
            paper_id=f"paper_stop_{index}",
            title=f"Direct stop-test paper {index}",
            literature_role=role,
            scope_fit=ScopeFit.direct,
            canonical_chunk_ids=[f"chunk_stop_{index}"],
            acquisition_status=AcquisitionStatus.fulltext,
            section_id=section_data["section_id"],
        )
        for index, role in enumerate(section_data["required_roles"])
    ]
    _write_coverage_prerequisites(tmp_path, section_data, sources=sources)

    result = SectionCoverageToolProvider(ctx).try_auto_finalize()
    assert result is not None and "VALIDATION_PASSED" in result
    package = json.loads(
        (tmp_path / "SECTION_MATERIAL_PACKAGE.json").read_text(
            encoding="utf-8"
        )
    )
    assert package["coverage_status"] == "coverage_sufficient"
    assert package["direct_sources"] == 4


def test_materialization_runtime_batches_approved_papers_with_reassessment(
    tmp_path: Path,
):
    """Approved candidates are acquired without one model turn per paper."""

    from unittest import mock
    from optomind_research.runtime.artifact_schemas import (
        CandidateDecision,
        OACandidate,
        OACandidateLedger,
        ScopeFit,
    )
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_acquire_and_materialize_oa_papers,
    )

    ctx = _make_ctx(tmp_path)
    ctx.min_mode_max_total_papers = 4
    candidates = []
    ledger = OACandidateLedger(section_id=ctx.section_id)
    for index in range(2):
        candidate_id = f"cand_one_at_a_time_{index}"
        candidate = {
            "candidate_id": candidate_id,
            "section_id": ctx.section_id,
            "role": "frontier",
            "title": f"One-at-a-time paper {index}",
            "doi": f"10.1234/one-at-a-time-{index}",
            "abstract": "Directly relevant optical evidence.",
            "is_oa": True,
            "pdf_url": f"https://example.test/{index}.pdf",
            "scope_fit": "direct",
        }
        candidates.append(candidate)
        ledger.candidates.append(
            OACandidate(
                candidate_id=candidate_id,
                section_id=ctx.section_id,
                role="frontier",
                title=candidate["title"],
                doi=candidate["doi"],
                decision=CandidateDecision.approved,
                scope_fit=ScopeFit.direct,
            )
        )
    ctx.register_candidates(candidates)
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        ledger.model_dump_json(), encoding="utf-8"
    )
    fulltext = {
        "paper_id": "paper_one_at_a_time",
        "chunk_ids": ["chunk_one_at_a_time"],
        "new_paper": True,
        "new_chunks": 1,
        "reused_chunks": 0,
        "acquisition_status": "fulltext",
        "download_url": "https://example.test/paper.pdf",
        "download_error": "",
        "attempted_urls": [],
        "download_errors_by_url": {},
        "content_type_detected": "application/pdf",
        "parse_failure_reason": "",
    }
    with mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry."
        "_ingest_single_candidate_bounded",
        return_value=fulltext,
    ) as ingest:
        result = json.loads(
            _make_acquire_and_materialize_oa_papers(ctx)(
                "frontier",
                json.dumps([item["candidate_id"] for item in candidates]),
                max_papers=2,
            )
        )
    assert ingest.call_count == 2
    assert result["materialized_this_call"] == 2
    assert result["total_materialized"] == 2
    assert result["stop_retrieval"] is False


def test_materialization_batch_stops_immediately_when_coverage_target_is_met(
    tmp_path: Path,
):
    """A bounded batch must not download surplus approved papers."""

    from unittest import mock
    from optomind_research.runtime.artifact_schemas import (
        AcquisitionStatus,
        CandidateDecision,
        OACandidate,
        OACandidateLedger,
        ScopeFit,
        SourceEntry,
    )
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_acquire_and_materialize_oa_papers,
    )

    section_data = _make_section_data()
    section_data["required_roles"] = ["frontier"]
    section_data["literature_coverage_target"] = {
        "minimum_unique_sources": 4,
        "minimum_direct_sources": 4,
    }
    ctx = _make_ctx(tmp_path)
    ctx.section_data = section_data
    ctx.min_mode_max_total_papers = 4
    prior_sources = [
        SourceEntry(
            paper_id=f"paper_prior_{index}",
            title=f"Prior paper {index}",
            literature_role="frontier",
            scope_fit=ScopeFit.direct,
            canonical_chunk_ids=[f"chunk_prior_{index}"],
            acquisition_status=AcquisitionStatus.fulltext,
            section_id=ctx.section_id,
        )
        for index in range(3)
    ]
    _write_coverage_prerequisites(
        tmp_path,
        section_data,
        sources=prior_sources,
    )

    ledger = OACandidateLedger(section_id=ctx.section_id)
    candidates = []
    for index in range(2):
        candidate_id = f"cand_stop_when_sufficient_{index}"
        candidate = {
            "candidate_id": candidate_id,
            "section_id": ctx.section_id,
            "role": "frontier",
            "title": f"Coverage-aware paper {index}",
            "doi": f"10.1234/coverage-aware-{index}",
            "abstract": "Directly relevant optical frontier evidence.",
            "is_oa": True,
            "pdf_url": f"https://example.test/coverage-{index}.pdf",
            "scope_fit": "direct",
        }
        candidates.append(candidate)
        ledger.candidates.append(
            OACandidate(
                candidate_id=candidate_id,
                section_id=ctx.section_id,
                role="frontier",
                title=candidate["title"],
                doi=candidate["doi"],
                decision=CandidateDecision.approved,
                scope_fit=ScopeFit.direct,
                role_fit=["frontier"],
            )
        )
    ctx.register_candidates(candidates)
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        ledger.model_dump_json(),
        encoding="utf-8",
    )

    def _fulltext(candidate, *_args, **_kwargs):
        return {
            "paper_id": f"paper_{candidate['candidate_id']}",
            "chunk_ids": [f"chunk_{candidate['candidate_id']}"],
            "new_paper": True,
            "new_chunks": 1,
            "reused_chunks": 0,
            "acquisition_status": "fulltext",
            "download_url": candidate["pdf_url"],
            "download_error": "",
            "attempted_urls": [],
            "download_errors_by_url": {},
            "content_type_detected": "application/pdf",
            "parse_failure_reason": "",
        }

    with mock.patch(
        "optomind_research.runtime.section_coverage_tool_registry."
        "_ingest_single_candidate_bounded",
        side_effect=_fulltext,
    ) as ingest:
        result = json.loads(
            _make_acquire_and_materialize_oa_papers(ctx)(
                "frontier",
                json.dumps([item["candidate_id"] for item in candidates]),
                max_papers=2,
            )
        )

    assert ingest.call_count == 1
    assert result["materialized_this_call"] == 1
    assert result["coverage_target_met"] is True
    assert result["coverage_after_batch"]["source_breadth"]["target_met"] is True


def test_provider_converts_spent_oa_budget_into_honest_open_gaps(
    tmp_path: Path,
):
    """A bounded search shortfall must close transparently, never deadlock."""

    from optomind_research.runtime.artifact_schemas import (
        AcquisitionStatus,
        MaterializationManifest,
        MaterializedPaper,
        ScopeFit,
        SourceEntry,
    )
    from optomind_research.runtime.section_coverage_tool_registry import (
        SectionCoverageToolProvider,
    )

    section_data = _make_section_data()
    section_data["literature_coverage_target"] = {
        "minimum_unique_sources": 4,
        "minimum_direct_sources": 4,
    }
    ctx = _make_ctx(tmp_path)
    ctx.section_data = section_data
    ctx.min_mode_max_total_papers = 1
    source = SourceEntry(
        paper_id="paper_foundation_only",
        title="Foundation-only paper",
        literature_role="foundation",
        scope_fit=ScopeFit.direct,
        canonical_chunk_ids=["chunk_foundation_only"],
        acquisition_status=AcquisitionStatus.fulltext,
        section_id=section_data["section_id"],
    )
    _write_coverage_prerequisites(tmp_path, section_data, sources=[source])
    (tmp_path / "MATERIALIZATION_MANIFEST.json").write_text(
        MaterializationManifest(
            section_id=section_data["section_id"],
            papers=[
                MaterializedPaper(
                    candidate_id="cand_foundation_only",
                    paper_id=source.paper_id,
                    role="foundation",
                    acquisition_status=AcquisitionStatus.fulltext,
                    chunk_ids=source.canonical_chunk_ids,
                )
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )

    result = SectionCoverageToolProvider(ctx).try_auto_finalize()
    assert result is not None and "VALIDATION_PASSED" in result
    gap_report = json.loads(
        (tmp_path / "SECTION_GAP_REPORT.json").read_text(encoding="utf-8")
    )
    documented = {item["role"]: item for item in gap_report["gaps"]}
    assert {"mechanism", "method", "frontier", "coverage_breadth"} <= set(documented)
    assert all(item["is_blocking"] is False for item in documented.values())
    assert all(
        "bounded_oa_materialization_limit_reached" in item["stop_reason"]
        for item in documented.values()
    )
    package = json.loads(
        (tmp_path / "SECTION_MATERIAL_PACKAGE.json").read_text(
            encoding="utf-8"
        )
    )
    assert package["coverage_status"] == "completed_with_open_gaps"
    assert package["blocking_gaps_remain"] is False
