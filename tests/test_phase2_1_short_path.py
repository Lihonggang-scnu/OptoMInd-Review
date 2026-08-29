from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace


def _context(tmp_path: Path, **kwargs):
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    return SectionCoverageContext(
        section_id="S01",
        section_data={
            "required_roles": ["foundation"],
            "optional_roles": [],
            "topic_identity": {"valid": False},
        },
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "stage.sqlite",
        work_dir=tmp_path,
        short_path_mode=True,
        **kwargs,
    )


def test_phase3_allowlist_is_counted_without_fts(tmp_path: Path):
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_inspect_section_local_coverage,
    )

    db = tmp_path / "shared.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE papers(paper_id TEXT PRIMARY KEY, doi TEXT, title TEXT, year INTEGER, venue TEXT);
            CREATE TABLE text_chunks(
                chunk_id TEXT PRIMARY KEY, paper_id TEXT, doi TEXT, title TEXT,
                section_path TEXT, text TEXT, content_depth TEXT,
                use_permission TEXT, scope_fit TEXT, source_kind TEXT,
                route_provenance_json TEXT, provenance_json TEXT
            );
            """
        )
        for index in range(2):
            pid = f"paper-{index}"
            cid = f"chunk-{index}"
            conn.execute(
                "INSERT INTO papers VALUES(?,?,?,?,?)",
                (pid, f"10.1000/{index}", f"Selected paper {index}", 2024, "Test"),
            )
            conn.execute(
                "INSERT INTO text_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid, pid, f"10.1000/{index}", f"Selected paper {index}",
                    "Introduction", "direct selected material", "fulltext",
                    "factual_support", "direct", "fulltext", "{}", "{}",
                ),
            )

    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "paper_id": f"paper-{index}",
                        "literature_role": "foundation",
                        "scope_fit": "direct",
                        "content_depth": "fulltext",
                        "use_permission": "factual_support",
                        "canonical_chunk_ids": [f"chunk-{index}"],
                    }
                    for index in range(2)
                ]
            }
        ),
        encoding="utf-8",
    )
    ctx = _context(
        tmp_path,
        shared_kb_sqlite_paths=[db],
        source_ledger_path=ledger,
        selected_paper_ids=["paper-0", "paper-1"],
        selected_chunk_ids=["chunk-0", "chunk-1"],
    )
    result = json.loads(_make_inspect_section_local_coverage(ctx)())
    assert result["total_local_papers"] == 2
    assert result["total_local_chunks"] == 2
    assert result["role_summary"]["foundation"] == "partial"


def test_candidate_action_clamps_non_oa_to_discovery_lead():
    from optomind_research.runtime.section_coverage_tool_registry import (
        _deterministic_candidate_action,
    )

    assert _deterministic_candidate_action(
        {"decision": "approved", "scope_fit": "direct", "is_oa": False},
    ) == "discovery_lead"
    assert _deterministic_candidate_action(
        {
            "decision": "approved",
            "scope_fit": "direct",
            "is_oa": False,
            "backends": ["semantic_scholar"],
        },
    ) == "discovery_lead"
    assert _deterministic_candidate_action(
        {
            "decision": "approved",
            "scope_fit": "direct",
            "is_oa": False,
            "semantic_scholar_id": "CorpusId:123",
            "backends": ["semantic_scholar"],
        },
    ) == "materialize_now"
    assert _deterministic_candidate_action(
        {"decision": "approved", "scope_fit": "out_of_scope", "is_oa": True, "oa_url": "https://example.org/a.pdf"},
    ) == "reject"


def test_post_audit_transition_ingests_structured_snippet_before_trace(
    tmp_path: Path, monkeypatch
):
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import OACandidateLedger

    class FakeChunk:
        chunk_id = "s2chunk:123:1:600:abc"
        paper_id = "CorpusId:123"
        context_complete = True
        scope_fit = "direct"
        use_permission = "factual_support"
        text = "A sufficiently long structured body snippet."

    class FakeRetriever:
        def __init__(self, *args, **kwargs):
            pass

        def retrieve(self, *args, **kwargs):
            return SimpleNamespace(
                accepted_chunks=[FakeChunk()],
                rejected_items=[],
                query_runs=[{"query": "exceptional point", "status_code": 200}],
            )

    class FakeBridge:
        def __init__(self, path):
            self.path = path

        def ingest(self, **kwargs):
            return {"chunks_inserted": 1, "chunks_reused": 0}

    monkeypatch.setattr(
        "optomind_research.s2_text_chunk_retriever.S2TextChunkRetriever",
        FakeRetriever,
    )
    monkeypatch.setattr(
        "optomind_research.s2_kb_bridge.S2KnowledgeBaseBridge",
        FakeBridge,
    )
    monkeypatch.setattr(registry, "_make_refresh_section_coverage", lambda ctx: lambda: "ok")
    monkeypatch.setattr(registry, "_make_validate_section_coverage_package", lambda ctx: lambda: "VALIDATION_FAILED: bounded")

    ctx = _context(tmp_path)
    candidate = {
        "candidate_id": "cand_generic_1",
        "section_id": "S01",
        "role": "foundation",
        "title": "Exceptional points",
        "is_oa": False,
        "semantic_scholar_id": "CorpusId:123",
        "backends": ["semantic_scholar"],
        "query_texts": ["exceptional point eigenvector coalescence"],
        "scope_fit": "direct",
        "decision": "deferred",
    }
    ledger_path = tmp_path / "OA_CANDIDATE_LEDGER.json"
    ledger_path.write_text(
        json.dumps(OACandidateLedger(section_id="S01", candidates=[candidate]).model_dump()),
        encoding="utf-8",
    )
    result = json.loads(
        registry._make_submit_candidate_audit(ctx)(
            json.dumps(
                [{
                    "candidate_id": "cand_generic_1",
                    "scope_fit": "direct",
                    "role_fit": ["foundation"],
                    "decision": "approved",
                    "candidate_decision": "materialize_now",
                    "audit_reason": "structured route",
                }]
            )
        )
    )
    assert result["candidate_actions"]["materialize_now"] == 1
    assert result["post_audit_transition"]["status"] == "materialized"
    assert "s2chunk:123:1:600:abc" in result["post_audit_transition"]["successful_chunk_ids"]


def test_explicit_query_targets_are_used_without_lexical_reconstruction():
    from optomind_research.runtime.phase2_phase3_feedback import build_query_component_map

    request = {
        "queries": ["unrelated words"],
        "query_targets": [{
            "query": "unrelated words",
            "claim_ids": ["C1"],
            "missing_components": ["Jordan chain multiplicity"],
        }],
        "missing_claim_ids": ["C1"],
    }
    assert build_query_component_map(request, [{"claim_id": "C1", "missing_components": ["wrong fallback"]}]) == {
        "unrelated words": ["Jordan chain multiplicity"]
    }


def test_refresh_audit_persists_real_totals_after_rebinding(tmp_path: Path):
    from optomind_research.runtime.section_coverage_tool_registry import (
        _make_refresh_section_coverage,
    )

    ctx = _context(tmp_path)
    (tmp_path / "SECTION_COVERAGE_PLAN.json").write_text(
        json.dumps({"roles": {"foundation": {"priority": "required"}}}),
        encoding="utf-8",
    )
    (tmp_path / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps({
            "sources": [
                {
                    "paper_id": "paper-a",
                    "literature_role": "foundation",
                    "scope_fit": "direct",
                    "canonical_chunk_ids": ["chunk-a"],
                },
                {
                    "paper_id": "paper-b",
                    "literature_role": "foundation",
                    "scope_fit": "direct",
                    "canonical_chunk_ids": ["chunk-b"],
                },
            ]
        }),
        encoding="utf-8",
    )
    _make_refresh_section_coverage(ctx)()
    audit = json.loads((tmp_path / "LOCAL_COVERAGE_AUDIT.json").read_text(encoding="utf-8"))
    assert audit["total_local_papers"] == 2
    assert audit["total_local_chunks"] == 2


def test_phase3_bridge_does_not_duplicate_existing_source_rows(tmp_path: Path):
    from optomind_research.runtime.section_coverage_tool_registry import (
        _build_source_ledger,
    )

    db = tmp_path / "shared.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE papers(paper_id TEXT PRIMARY KEY, doi TEXT, title TEXT, year INTEGER, venue TEXT);
            CREATE TABLE text_chunks(
                chunk_id TEXT PRIMARY KEY, paper_id TEXT, doi TEXT, title TEXT,
                section_path TEXT, text TEXT, content_depth TEXT,
                use_permission TEXT, scope_fit TEXT, source_kind TEXT,
                route_provenance_json TEXT, provenance_json TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO papers VALUES(?,?,?,?,?)",
            ("paper-a", "10.1000/a", "A", 2024, "Test"),
        )
        for cid in ("chunk-a", "chunk-b"):
            conn.execute(
                "INSERT INTO text_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid, "paper-a", "10.1000/a", "A", "Introduction", "text",
                    "fulltext", "factual_support", "direct", "fulltext", "{}", "{}",
                ),
            )
    ledger = tmp_path / "shared_ledger.json"
    ledger.write_text(
        json.dumps({
            "sources": [{
                "paper_id": "paper-a",
                "title": "A",
                "literature_role": "foundation",
                "scope_fit": "direct",
                "use_permission": "factual_support",
                "content_depth": "fulltext",
                "canonical_chunk_ids": ["chunk-a"],
                "discovery_route": "phase3",
                "materialization_route": "reused_local_asset",
            }]
        }),
        encoding="utf-8",
    )
    ctx = _context(
        tmp_path,
        shared_kb_sqlite_paths=[db],
        source_ledger_path=ledger,
        selected_paper_ids=["paper-a"],
        selected_chunk_ids=["chunk-a", "chunk-b"],
    )
    (tmp_path / "SECTION_COVERAGE_PLAN.json").write_text(
        json.dumps({"roles": {"foundation": {"priority": "required"}}}),
        encoding="utf-8",
    )
    _build_source_ledger(ctx)
    out = json.loads((tmp_path / "SECTION_SOURCE_LEDGER.json").read_text(encoding="utf-8"))
    keys = [(row.get("paper_id"), row.get("literature_role")) for row in out["sources"]]
    assert keys.count(("paper-a", "foundation")) == 1
    assert set(out["sources"][0]["canonical_chunk_ids"]) == {"chunk-a", "chunk-b"}


def test_zero_new_chunks_cannot_report_new_paper_success(tmp_path: Path, monkeypatch):
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import OACandidateLedger

    ctx = _context(
        tmp_path,
        cross_wave_state_path=tmp_path / "cross_wave.json",
    )
    candidate = {
        "candidate_id": "cand_zero_chunks",
        "section_id": "S01",
        "role": "foundation",
        "title": "Already present paper",
        "doi": "10.1000/already-present",
        "is_oa": True,
        "oa_url": "https://example.org/already.pdf",
        "scope_fit": "direct",
        "decision": "approved",
        "candidate_action": "materialize_now",
    }
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        json.dumps(OACandidateLedger(section_id="S01", candidates=[candidate]).model_dump()),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        registry,
        "_ingest_single_candidate_bounded",
        lambda *args, **kwargs: {
            "paper_id": "doi:10.1000/already-present",
            "chunk_ids": ["existing-chunk"],
            "new_chunk_ids": [],
            "new_paper": True,
            "paper_row_inserted": True,
            "new_chunks": 0,
            "reused_chunks": 1,
            "acquisition_status": "fulltext",
        },
    )
    monkeypatch.setattr(
        registry,
        "_make_refresh_section_coverage",
        lambda ctx: lambda: json.dumps({"status": "ok", "blocking_gaps": []}),
    )

    result = json.loads(
        registry._make_acquire_and_materialize_oa_papers(ctx)(
            "foundation", json.dumps(["cand_zero_chunks"]), max_papers=1
        )
    )
    manifest = json.loads((tmp_path / "MATERIALIZATION_MANIFEST.json").read_text(encoding="utf-8"))
    assert result["total_new_papers"] == 0
    assert result["total_new_chunks"] == 0
    assert result["papers_this_call"][0]["new_paper"] is False
    assert manifest["papers"][0]["new_paper"] is False
    assert manifest["papers"][0]["paper_row_inserted"] is True


def test_duplicate_candidate_is_skipped_against_cross_wave_state_and_staging(tmp_path: Path, monkeypatch):
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import OACandidateLedger

    stage = tmp_path / "stage.sqlite"
    with sqlite3.connect(stage) as conn:
        conn.executescript(
            """
            CREATE TABLE papers(paper_id TEXT PRIMARY KEY, doi TEXT, title TEXT);
            CREATE TABLE text_chunks(chunk_id TEXT PRIMARY KEY, paper_id TEXT);
            INSERT INTO papers VALUES ('doi:10.1103/pf6y-lxzp','10.1103/pf6y-lxzp','Already ingested');
            INSERT INTO text_chunks VALUES ('chunk-1','doi:10.1103/pf6y-lxzp');
            """
        )
    ctx = _context(
        tmp_path,
        cross_wave_state_path=tmp_path / "cross_wave.json",
    )
    ctx.temp_kb_sqlite = stage
    candidate = {
        "candidate_id": "cand_duplicate_wave_2",
        "section_id": "S01",
        "role": "foundation",
        "title": "Already ingested",
        "doi": "10.1103/pf6y-lxzp",
        "is_oa": True,
        "oa_url": "https://example.org/pf6y.pdf",
        "scope_fit": "direct",
        "decision": "approved",
        "candidate_action": "materialize_now",
    }
    (tmp_path / "OA_CANDIDATE_LEDGER.json").write_text(
        json.dumps(OACandidateLedger(section_id="S01", candidates=[candidate]).model_dump()),
        encoding="utf-8",
    )
    (tmp_path / "cross_wave.json").write_text(
        json.dumps({
            "candidate_outcomes": {
                "cand_original": {
                    "candidate_id": "cand_original",
                    "material_identity": "doi:10.1103/pf6y-lxzp",
                    "no_progress": True,
                    "no_progress_components": ["Jordan block"],
                }
            },
            "material_identity_index": {"doi:10.1103/pf6y-lxzp": ["cand_original"]},
            "attempted_candidate_ids": ["cand_original"],
            "attempted_material_identities": ["doi:10.1103/pf6y-lxzp"],
        }),
        encoding="utf-8",
    )

    def should_not_ingest(*args, **kwargs):
        raise AssertionError("duplicate candidate was reprocessed")

    monkeypatch.setattr(registry, "_ingest_single_candidate_bounded", should_not_ingest)
    result = json.loads(
        registry._make_acquire_and_materialize_oa_papers(ctx)(
            "foundation", json.dumps(["cand_duplicate_wave_2"]), max_papers=1
        )
    )
    assert result["materialized_this_call"] == 0
    assert result["papers_this_call"] == []
    assert result["skipped_candidates"][0]["status"] == "no_progress_candidate_skipped"
    telemetry = json.loads((tmp_path / "PHASE2_TELEMETRY.json").read_text(encoding="utf-8"))
    assert telemetry["reused_candidates"] == 1


def test_invalid_chunk_ownership_warnings_are_deduplicated_and_counted(
    tmp_path: Path,
    caplog,
) -> None:
    import logging

    from optomind_research.runtime.section_coverage_tool_registry import (
        _record_invalid_chunk_ownership,
    )

    ctx = _context(
        tmp_path,
        cross_wave_state_path=tmp_path / "cross_wave.json",
    )
    with caplog.at_level(logging.WARNING):
        _record_invalid_chunk_ownership(ctx, "paper-a", "chunk-wrong")
        _record_invalid_chunk_ownership(ctx, "paper-a", "chunk-wrong")
        _record_invalid_chunk_ownership(ctx, "paper-b", "chunk-wrong")

    matching = [
        record for record in caplog.records
        if "invalid chunk ownership" in record.getMessage()
    ]
    assert len(matching) == 2
    telemetry = json.loads(
        (tmp_path / "PHASE2_TELEMETRY.json").read_text(encoding="utf-8")
    )
    assert telemetry["invalid_chunk_ownership_rejection_count"] == 3
    assert telemetry["invalid_chunk_ownership_unique_count"] == 2
    rows = {
        (item["paper_id"], item["chunk_id"]): item["occurrences"]
        for item in telemetry["invalid_chunk_ownership_rejections"]
    }
    assert rows[("paper-a", "chunk-wrong")] == 2
    assert rows[("paper-b", "chunk-wrong")] == 1


def _budget_candidate(
    candidate_id: str,
    *,
    doi: str,
    query: str = "target alpha",
    scope_fit: str = "direct",
    relevance: float = 1.0,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "section_id": "S01",
        "role": "foundation",
        "title": f"Paper {candidate_id}",
        "doi": doi,
        "is_oa": True,
        "oa_url": f"https://example.org/{candidate_id}.pdf",
        "query_texts": [query],
        "relevance_score": relevance,
        "scope_fit": scope_fit,
        "decision": "approved",
        # Deliberately permit constructing an adversarial out-of-scope row.
        "candidate_action": "materialize_now",
    }


def _install_budget_ledger(ctx, candidates: list[dict]) -> None:
    from optomind_research.runtime.artifact_schemas import OACandidateLedger

    ctx.register_candidates([dict(item) for item in candidates])
    (ctx.work_dir / "OA_CANDIDATE_LEDGER.json").write_text(
        json.dumps(
            OACandidateLedger(
                section_id=ctx.section_id, candidates=candidates
            ).model_dump()
        ),
        encoding="utf-8",
    )


def _stub_refresh_and_validation(monkeypatch, registry) -> None:
    monkeypatch.setattr(
        registry,
        "_make_refresh_section_coverage",
        lambda ctx: lambda: json.dumps({"status": "ok", "blocking_gaps": ["open"]}),
    )
    monkeypatch.setattr(
        registry,
        "_make_validate_section_coverage_package",
        lambda ctx: lambda: "VALIDATION_FAILED: expected bounded gap",
    )


def test_oa_batch_failure_does_not_consume_success_slot(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry

    ctx = _context(
        tmp_path,
        min_mode_max_total_papers=1,
        phase3_coverage_request={"expected_new_papers": 1},
    )
    candidates = [
        _budget_candidate("cand_fail", doi="10.1000/fail"),
        _budget_candidate("cand_success", doi="10.1000/success"),
    ]
    _install_budget_ledger(ctx, candidates)
    calls: list[str] = []

    def ingest(candidate, *_args, **_kwargs):
        calls.append(candidate["candidate_id"])
        if candidate["candidate_id"] == "cand_fail":
            return {
                "paper_id": "doi:10.1000/fail",
                "chunk_ids": [],
                "new_chunk_ids": [],
                "new_paper": False,
                "new_chunks": 0,
                "reused_chunks": 0,
                "acquisition_status": "failed",
            }
        return {
            "paper_id": "doi:10.1000/success",
            "chunk_ids": ["chunk-success"],
            "new_chunk_ids": ["chunk-success"],
            "new_paper": True,
            "new_chunks": 1,
            "reused_chunks": 0,
            "acquisition_status": "fulltext",
        }

    monkeypatch.setattr(registry, "_ingest_single_candidate_bounded", ingest)
    _stub_refresh_and_validation(monkeypatch, registry)
    result = json.loads(
        registry._make_acquire_and_materialize_oa_papers(ctx)(
            "foundation",
            json.dumps(["cand_fail", "cand_success"]),
            max_papers=1,
        )
    )
    assert calls == ["cand_fail", "cand_success"]
    assert result["attempted_this_call"] == 2
    assert result["successful_this_call"] == 1
    assert result["successful_slots_remaining"] == 0
    assert ctx._papers_materialized_total == 1


def test_successful_insert_stops_batch_when_current_coverage_is_sufficient(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry

    ctx = _context(
        tmp_path,
        min_mode_max_total_papers=2,
        phase3_coverage_request={"expected_new_papers": 2},
    )
    candidates = [
        _budget_candidate("cand_0_failed_route", doi="10.1000/failed-route"),
        _budget_candidate("cand_1_closes_target", doi="10.1000/closes-target"),
        _budget_candidate("cand_2_surplus", doi="10.1000/surplus"),
    ]
    _install_budget_ledger(ctx, candidates)
    calls: list[str] = []

    def ingest(candidate, *_args, **_kwargs):
        cid = candidate["candidate_id"]
        calls.append(cid)
        if cid == "cand_0_failed_route":
            return {
                "paper_id": "",
                "chunk_ids": [],
                "new_chunk_ids": [],
                "new_paper": False,
                "new_chunks": 0,
                "reused_chunks": 0,
                "acquisition_status": "failed",
            }
        return {
            "paper_id": "paper:" + cid,
            "chunk_ids": ["chunk:" + cid],
            "new_chunk_ids": ["chunk:" + cid],
            "new_paper": True,
            "new_chunks": 1,
            "reused_chunks": 0,
            "acquisition_status": "fulltext",
        }

    monkeypatch.setattr(registry, "_ingest_single_candidate_bounded", ingest)
    monkeypatch.setattr(
        registry,
        "_make_refresh_section_coverage",
        lambda ctx: lambda: json.dumps({
            "status": "ok", "blocking_gaps": [],
            "source_breadth": {"target_met": True},
        }),
    )
    result = json.loads(
        registry._make_acquire_and_materialize_oa_papers(ctx)(
            "foundation",
            json.dumps([item["candidate_id"] for item in candidates]),
            max_papers=2,
        )
    )
    assert calls == ["cand_0_failed_route", "cand_1_closes_target"]
    assert result["attempted_this_call"] == 2
    assert result["successful_this_call"] == 1
    assert result["successful_slots_remaining"] == 1
    assert result["coverage_target_met"] is True


def test_transition_fills_three_successful_papers_in_one_wave(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import OACandidateLedger

    ctx = _context(
        tmp_path,
        min_mode_max_total_papers=3,
        phase3_coverage_request={"expected_new_papers": 3},
    )
    candidates = [
        _budget_candidate(f"cand_{index}", doi=f"10.1000/{index}")
        for index in range(3)
    ]
    _install_budget_ledger(ctx, candidates)
    attempts: list[str] = []

    def acquire(_ctx):
        def run(_role, candidate_ids, max_papers=1):
            cid = json.loads(candidate_ids)[0]
            attempts.append(cid)
            _ctx._papers_materialized_total += 1
            return json.dumps({
                "status": "ok",
                "attempted_this_call": 1,
                "successful_this_call": 1,
                "papers_this_call": [{
                    "candidate_id": cid,
                    "paper_id": "paper:" + cid,
                    "new_paper": True,
                    "new_chunks": 1,
                    "acquisition_status": "fulltext",
                }],
            })
        return run

    monkeypatch.setattr(registry, "_make_acquire_and_materialize_oa_papers", acquire)
    monkeypatch.setattr(
        registry,
        "_search_component_candidates_for_budget_fill",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("search must not run when local candidates fill the budget")
        ),
    )
    _stub_refresh_and_validation(monkeypatch, registry)
    ledger = OACandidateLedger.model_validate(json.loads(
        (tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8")
    ))
    result = registry._deterministic_post_audit_transition(ctx, ledger)
    assert len(attempts) == 3
    assert result["successful_slots_filled"] == 3
    assert result["successful_slots_remaining"] == 0
    assert result["stop_reason"] == "successful_paper_slots_filled"
    assert result["search_escalation"]["invoked"] is False


def test_transition_stops_surplus_slots_after_coverage_becomes_sufficient(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import OACandidateLedger

    ctx = _context(
        tmp_path,
        min_mode_max_total_papers=3,
        phase3_coverage_request={"expected_new_papers": 3},
    )
    candidates = [
        _budget_candidate(f"cand_ready_{index}", doi=f"10.1000/ready-{index}")
        for index in range(3)
    ]
    _install_budget_ledger(ctx, candidates)
    attempts: list[str] = []

    def acquire(_ctx):
        def run(_role, candidate_ids, max_papers=1):
            cid = json.loads(candidate_ids)[0]
            attempts.append(cid)
            _ctx._papers_materialized_total += 1
            return json.dumps({
                "status": "ok",
                "attempted_this_call": 1,
                "successful_this_call": 1,
                "papers_this_call": [{
                    "candidate_id": cid,
                    "paper_id": "paper:" + cid,
                    "new_paper": True,
                    "new_chunks": 1,
                    "acquisition_status": "fulltext",
                }],
            })
        return run

    monkeypatch.setattr(registry, "_make_acquire_and_materialize_oa_papers", acquire)
    monkeypatch.setattr(
        registry,
        "_make_refresh_section_coverage",
        lambda ctx: lambda: json.dumps({"status": "ok", "blocking_gaps": []}),
    )
    monkeypatch.setattr(
        registry,
        "_make_validate_section_coverage_package",
        lambda ctx: lambda: "VALIDATION_OK",
    )
    ledger = OACandidateLedger.model_validate(json.loads(
        (tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8")
    ))
    result = registry._deterministic_post_audit_transition(ctx, ledger)
    assert len(attempts) == 1
    assert result["coverage_target_met"] is True
    assert result["successful_slots_filled"] == 1
    assert result["successful_slots_remaining"] == 2
    assert result["stop_reason"] == "section_coverage_target_met"


def test_transition_hard_rejects_out_of_scope_materialize_override(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import OACandidateLedger

    ctx = _context(
        tmp_path,
        min_mode_max_total_papers=1,
        phase3_coverage_request={"expected_new_papers": 1},
    )
    candidates = [
        _budget_candidate(
            "cand_bad", doi="10.1000/bad", scope_fit="out_of_scope",
            relevance=100,
        ),
        _budget_candidate("cand_good", doi="10.1000/good"),
    ]
    _install_budget_ledger(ctx, candidates)
    attempted: list[str] = []

    def acquire(_ctx):
        def run(_role, candidate_ids, max_papers=1):
            cid = json.loads(candidate_ids)[0]
            attempted.append(cid)
            _ctx._papers_materialized_total += 1
            return json.dumps({
                "status": "ok", "successful_this_call": 1,
                "papers_this_call": [{
                    "candidate_id": cid, "paper_id": "paper:" + cid,
                    "new_paper": True, "new_chunks": 1,
                    "acquisition_status": "fulltext",
                }],
            })
        return run

    monkeypatch.setattr(registry, "_make_acquire_and_materialize_oa_papers", acquire)
    _stub_refresh_and_validation(monkeypatch, registry)
    ledger = OACandidateLedger.model_validate(json.loads(
        (tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8")
    ))
    result = registry._deterministic_post_audit_transition(ctx, ledger)
    assert attempted == ["cand_good"]
    assert "cand_bad" not in result["candidates_considered"]
    assert any(
        row["candidate_id"] == "cand_bad"
        and row["status"] == "rejected_out_of_scope"
        for row in result["skipped_candidates"]
    )


def test_budget_candidate_ranking_diversifies_query_targets() -> None:
    from optomind_research.runtime.artifact_schemas import OACandidate
    from optomind_research.runtime.section_coverage_tool_registry import (
        _rank_budget_fill_candidates,
    )

    targets = [
        {"query": "alpha spectral response", "missing_components": ["alpha"]},
        {"query": "beta temporal response", "missing_components": ["beta"]},
    ]
    candidates = [
        OACandidate(**_budget_candidate(
            "cand_alpha_1", doi="10.1000/a1",
            query="alpha spectral response", relevance=10,
        )),
        OACandidate(**_budget_candidate(
            "cand_alpha_2", doi="10.1000/a2",
            query="alpha spectral response", relevance=9,
        )),
        OACandidate(**_budget_candidate(
            "cand_beta", doi="10.1000/b",
            query="beta temporal response", relevance=1,
        )),
    ]
    ranked = _rank_budget_fill_candidates(candidates, targets)
    assert [item.candidate_id for item in ranked] == [
        "cand_alpha_1", "cand_beta", "cand_alpha_2"
    ]


def test_reused_candidate_does_not_consume_transition_slot(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import OACandidateLedger

    state_path = tmp_path / "cross_wave.json"
    state_path.write_text(json.dumps({
        "attempted_material_identities": ["doi:10.1000/reused"],
        "candidate_outcomes": {},
        "material_identity_index": {},
    }), encoding="utf-8")
    ctx = _context(
        tmp_path,
        min_mode_max_total_papers=1,
        phase3_coverage_request={"expected_new_papers": 1},
        cross_wave_state_path=state_path,
    )
    candidates = [
        _budget_candidate("cand_reused", doi="10.1000/reused", relevance=10),
        _budget_candidate("cand_novel", doi="10.1000/novel", relevance=1),
    ]
    _install_budget_ledger(ctx, candidates)
    attempted: list[str] = []

    def acquire(_ctx):
        def run(_role, candidate_ids, max_papers=1):
            cid = json.loads(candidate_ids)[0]
            attempted.append(cid)
            _ctx._papers_materialized_total += 1
            return json.dumps({
                "status": "ok", "successful_this_call": 1,
                "papers_this_call": [{
                    "candidate_id": cid, "paper_id": "paper:" + cid,
                    "new_paper": True, "new_chunks": 1,
                    "acquisition_status": "fulltext",
                }],
            })
        return run

    monkeypatch.setattr(registry, "_make_acquire_and_materialize_oa_papers", acquire)
    _stub_refresh_and_validation(monkeypatch, registry)
    ledger = OACandidateLedger.model_validate(json.loads(
        (tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8")
    ))
    result = registry._deterministic_post_audit_transition(ctx, ledger)
    assert attempted == ["cand_novel"]
    assert result["successful_slots_filled"] == 1
    assert any(
        row["candidate_id"] == "cand_reused"
        and row["status"] == "reused_candidate"
        for row in result["skipped_candidates"]
    )


def test_component_search_escalates_only_after_local_candidates_are_insufficient(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import OACandidateLedger

    ctx = _context(
        tmp_path,
        min_mode_max_total_papers=2,
        phase3_coverage_request={
            "expected_new_papers": 2,
            "queries": ["target alpha", "target beta"],
            "query_targets": [
                {"query": "target alpha", "missing_components": ["alpha"]},
                {"query": "target beta", "missing_components": ["beta"]},
            ],
            "missing_roles": ["foundation"],
        },
    )
    local = _budget_candidate(
        "cand_local", doi="10.1000/local", query="target alpha"
    )
    _install_budget_ledger(ctx, [local])
    search_calls: list[int] = []

    def search(_ctx, *, successful_target_keys, remaining_slots):
        search_calls.append(remaining_slots)
        raw = json.loads(
            (_ctx.work_dir / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8")
        )
        discovered = _budget_candidate(
            "cand_searched", doi="10.1000/searched", query="target beta",
            scope_fit="unreviewed",
        )
        discovered["decision"] = "deferred"
        discovered["candidate_action"] = "discovery_lead"
        raw["candidates"].append(discovered)
        (_ctx.work_dir / "OA_CANDIDATE_LEDGER.json").write_text(
            json.dumps(raw), encoding="utf-8"
        )
        _ctx.register_candidates([dict(discovered)])
        return {
            "status": "ok", "candidate_ids": ["cand_searched"],
            "queries_used": ["target beta"],
            "backend_stats": {"semantic_scholar": 1, "openalex": 0},
        }

    def acquire(_ctx):
        def run(_role, candidate_ids, max_papers=1):
            cid = json.loads(candidate_ids)[0]
            _ctx._papers_materialized_total += 1
            return json.dumps({
                "status": "ok", "successful_this_call": 1,
                "papers_this_call": [{
                    "candidate_id": cid, "paper_id": "paper:" + cid,
                    "new_paper": True, "new_chunks": 1,
                    "acquisition_status": "fulltext",
                }],
            })
        return run

    monkeypatch.setattr(
        registry, "_search_component_candidates_for_budget_fill", search
    )
    monkeypatch.setattr(registry, "_make_acquire_and_materialize_oa_papers", acquire)
    _stub_refresh_and_validation(monkeypatch, registry)
    ledger = OACandidateLedger.model_validate(json.loads(
        (tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8")
    ))
    result = registry._deterministic_post_audit_transition(ctx, ledger)
    assert search_calls == [1]
    assert result["successful_slots_filled"] == 2
    assert result["search_escalation"]["invoked"] is True
    assert result["search_escalation"]["queries"] == ["target beta"]


def test_transition_enforces_hard_success_total_budget(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import OACandidateLedger

    ctx = _context(
        tmp_path,
        min_mode_max_total_papers=2,
        phase3_coverage_request={"expected_new_papers": 5},
    )
    candidates = [
        _budget_candidate(f"cand_cap_{index}", doi=f"10.1000/cap-{index}")
        for index in range(4)
    ]
    _install_budget_ledger(ctx, candidates)
    attempted: list[str] = []

    def acquire(_ctx):
        def run(_role, candidate_ids, max_papers=1):
            cid = json.loads(candidate_ids)[0]
            attempted.append(cid)
            _ctx._papers_materialized_total += 1
            return json.dumps({
                "status": "ok", "successful_this_call": 1,
                "papers_this_call": [{
                    "candidate_id": cid, "paper_id": "paper:" + cid,
                    "new_paper": True, "new_chunks": 1,
                    "acquisition_status": "fulltext",
                }],
            })
        return run

    monkeypatch.setattr(registry, "_make_acquire_and_materialize_oa_papers", acquire)
    _stub_refresh_and_validation(monkeypatch, registry)
    ledger = OACandidateLedger.model_validate(json.loads(
        (tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8")
    ))
    result = registry._deterministic_post_audit_transition(ctx, ledger)
    assert len(attempted) == 2
    assert result["slots_requested"] == 2
    assert result["successful_slots_filled"] == 2
    assert result["budget_remaining"] == 0


def test_component_search_telemetry_counts_real_s2_fallback_and_openalex_calls(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry

    ctx = _context(
        tmp_path,
        min_mode_max_queries=2,
        min_mode_max_per_backend=2,
        s2_first_enabled=True,
    )
    monkeypatch.setattr(registry, "_search_s2_first", lambda queries, limit: [])
    monkeypatch.setattr(
        registry, "_search_semantic_scholar", lambda queries, limit: []
    )
    monkeypatch.setattr(registry, "_search_openalex", lambda queries, limit: [])
    result = json.loads(
        registry._make_search_oa_candidates(ctx)(
            "foundation", json.dumps(["target alpha"]), max_per_backend=1
        )
    )
    assert result["status"] == "ok"
    assert result["backend_stats"]["semantic_scholar_calls"] == 2
    telemetry = json.loads(
        (tmp_path / "PHASE2_TELEMETRY.json").read_text(encoding="utf-8")
    )
    assert telemetry["s2_search_calls"] == 2
    assert telemetry["openalex_calls"] == 1
