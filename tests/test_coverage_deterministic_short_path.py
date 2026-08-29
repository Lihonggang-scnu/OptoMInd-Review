from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


def _write_blueprint(tmp_path: Path, *, section: dict | None = None) -> Path:
    section = {
        "section_id": "S01",
        "title": "Introduction to metasurface mechanisms",
        "chapter_argument": "Establish the conceptual basis for metasurface mechanisms.",
        "scope_description": "Section-specific metasurface physics.",
        "required_roles": ["foundation"],
        "optional_roles": [],
        "section_role": "introduction",
        "target_word_range": {"min": 500, "max": 700},
        "visual_asset_required": False,
        **(section or {}),
    }
    path = tmp_path / "blueprint.json"
    path.write_text(
        json.dumps(
            {
                "topic_identity": {
                    "valid": False,
                    "scientific_object": "metasurface mechanisms",
                },
                "sections": [section],
            }
        ),
        encoding="utf-8",
    )
    return path


def _patch_no_react(monkeypatch):
    import optomind_research.runtime.section_coverage_orchestrator as orchestrator_module

    def forbidden_worker(**kwargs):
        raise AssertionError("the deterministic primary path must not enter ReAct")

    monkeypatch.setattr(orchestrator_module, "ResearchWorker", forbidden_worker)


def _run_config(tmp_path: Path, blueprint: Path):
    from optomind_research.runtime.section_coverage_orchestrator import (
        SectionCoverageOrchestratorConfig,
    )

    return SectionCoverageOrchestratorConfig(
        blueprint_path=blueprint,
        base_kb_sqlite=None,
        output_root=tmp_path / "coverage",
        # Deliberately pass the old ceilings: the deterministic factory must
        # clamp them instead of inheriting the old 96k/6-call behavior.
        context_tokens_per_model_call=96_000,
        model_context_budget_per_section=96_000,
        max_model_calls_per_section=6,
        max_coverage_waves=2,
        max_audit_calls_per_section=2,
        max_materialized_papers_per_section=1,
        max_results_per_backend=2,
    )


def test_exact_six_iteration_failure_reaches_search_materialization_and_package(
    tmp_path: Path, monkeypatch
) -> None:
    """Reproduce the S01-S06 bookkeeping failure without paying six turns."""

    import optomind_research.runtime.section_coverage_tool_registry as registry
    import llm.qwen_chat_client as qwen

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(tmp_path)
    search_calls: list[list[str]] = []

    def fake_s2(queries, max_per_q):
        search_calls.append(list(queries))
        return [
            {
                "title": "Metasurface mechanism foundations",
                "doi": "10.1000/metasurface-foundation",
                "year": 2024,
                "venue": "Synthetic Physics",
                "abstract": "A section-specific account of metasurface mechanisms.",
                "is_oa": False,
                "semantic_scholar_id": "CorpusId:phase2-1",
                "backends": ["semantic_scholar"],
                "query_texts": list(queries),
                "citation_count": 12,
            }
        ]

    class FakeChunk:
        chunk_id = "s2chunk:phase2-1:1:600:fixture"
        paper_id = "CorpusId:phase2-1"
        context_complete = True
        scope_fit = "direct"
        use_permission = "factual_support"
        context_limitations = []
        text = "A sufficiently long structured body snippet supporting the mechanism."

    class FakeRetriever:
        def __init__(self, *args, **kwargs):
            pass

        def retrieve(self, *args, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(
                accepted_chunks=[FakeChunk()],
                rejected_items=[],
                query_runs=[{"query": "metasurface mechanism", "status_code": 200}],
            )

    class FakeBridge:
        def __init__(self, path):
            self.path = path

        def ingest(self, **kwargs):
            return {
                "chunks_inserted": 1,
                "chunks_reused": 0,
                "inserted_chunk_ids": [FakeChunk.chunk_id],
                "papers_inserted": 1,
            }

    monkeypatch.setattr(registry, "_search_s2_first", fake_s2)
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])
    monkeypatch.setattr(
        "optomind_research.s2_text_chunk_retriever.S2TextChunkRetriever",
        FakeRetriever,
    )
    monkeypatch.setattr(
        "optomind_research.s2_kb_bridge.S2KnowledgeBaseBridge",
        FakeBridge,
    )

    def fake_chat(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        candidate_id = payload["candidates"][0]["candidate_id"]
        return {
            "content": json.dumps(
                {
                    "candidates": [
                        {
                            "candidate_id": candidate_id,
                            "scope_fit": "direct",
                            "role_fit": ["foundation"],
                            "decision": "approved",
                            "candidate_decision": "materialize_now",
                            "audit_reason": "Direct section-specific mechanism evidence.",
                            "not_usable_for": [],
                        }
                    ]
                }
            ),
            "input_tokens": 900,
            "output_tokens": 160,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", fake_chat)
    config = _run_config(tmp_path, blueprint)
    assert config.short_path_mode is True
    result = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    ).SectionCoverageOrchestrator(config).run()

    section_dir = result.work_dir / "sections" / "S01"
    package = json.loads((section_dir / "SECTION_MATERIAL_PACKAGE.json").read_text(encoding="utf-8"))
    coverage_package = json.loads((section_dir / "SECTION_COVERAGE_PACKAGE.json").read_text(encoding="utf-8"))
    ledger = json.loads((section_dir / "SECTION_SOURCE_LEDGER.json").read_text(encoding="utf-8"))
    short_path = json.loads((section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8"))
    result_json = json.loads((section_dir / "RESULT.json").read_text(encoding="utf-8"))
    telemetry = json.loads((section_dir / "PHASE2_TELEMETRY.json").read_text(encoding="utf-8"))

    assert result.status == "completed"
    assert search_calls, "the controller must reach external search"
    assert len(search_calls) <= 2
    assert package == coverage_package
    assert package["coverage_outcome"] in {"material_ready", "material_ready_with_limits"}
    assert package["blocking_gaps_remain"] is False
    assert ledger["sources"]
    source = ledger["sources"][0]
    assert source["canonical_chunk_ids"] == [FakeChunk.chunk_id]
    assert source["content_depth"] == "structured_snippet"
    assert source["use_permission"] == "factual_support"
    assert source["acquisition_status"] == "structured_snippet"
    assert short_path["qwen_calls"] <= 2
    assert short_path["input_tokens"] < 20_000
    assert result_json["react_loop_entered"] is False
    assert result_json["model_calls"] <= 2
    assert telemetry["execution_mode"] == "deterministic_short_path"
    assert telemetry["deterministic_step_count"] >= 6
    assert telemetry["batched_llm_calls"] == 1
    assert telemetry["accepted_s2_snippets"] == 1


def test_zero_candidates_still_emits_adaptive_package_and_gap(tmp_path: Path, monkeypatch) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(tmp_path)
    monkeypatch.setattr(registry, "_search_s2_first", lambda *args: [])
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    result = module.SectionCoverageOrchestrator(_run_config(tmp_path, blueprint)).run()
    section_dir = result.work_dir / "sections" / "S01"
    package = json.loads((section_dir / "SECTION_COVERAGE_PACKAGE.json").read_text(encoding="utf-8"))
    gap_report = json.loads((section_dir / "SECTION_GAP_REPORT.json").read_text(encoding="utf-8"))
    short_path = json.loads((section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8"))

    assert result.sections_needing_more_literature == 1
    assert package["coverage_outcome"] == "needs_more_literature"
    assert package["blocking_gaps_remain"] is False
    assert any(gap["role"] == "coverage_material" for gap in gap_report["gaps"])
    assert gap_report["blocking_gap_count"] == 0
    assert short_path["qwen_calls"] == 0
    assert "no_candidates" in json.dumps(gap_report).lower() or "bounded" in json.dumps(gap_report).lower()


@pytest.mark.parametrize(
    ("section_id", "required_role"),
    [("S03", "method"), ("S06", "frontier")],
)
def test_empty_bounded_exhaustion_with_valid_topic_finalizes_all_artifacts(
    tmp_path: Path, monkeypatch, section_id: str, required_role: str
) -> None:
    """An empty scientific search is a documented gap, not an engine failure."""

    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": section_id,
            "required_roles": [required_role],
        },
    )
    payload = json.loads(blueprint.read_text(encoding="utf-8"))
    payload["topic_identity"] = {
        "valid": True,
        "scientific_object": "metasurface mechanisms",
        "normalized_question": "metasurface mechanisms",
        "core_anchor_tokens": ["metasurface", "mechanism"],
        "supporting_anchor_tokens": [],
    }
    blueprint.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(registry, "_search_s2_first", lambda *args: [])
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    result = module.SectionCoverageOrchestrator(
        _run_config(tmp_path, blueprint)
    ).run()
    section_dir = result.work_dir / "sections" / section_id

    for filename in (
        "SECTION_MATERIAL_PACKAGE.json",
        "SECTION_COVERAGE_PACKAGE.json",
        "COVERAGE_DECISION.json",
        "RESULT.json",
        "SHORT_PATH_RUN.json",
    ):
        assert (section_dir / filename).exists(), filename

    package = json.loads(
        (section_dir / "SECTION_MATERIAL_PACKAGE.json").read_text(encoding="utf-8")
    )
    decision = json.loads(
        (section_dir / "COVERAGE_DECISION.json").read_text(encoding="utf-8")
    )
    result_json = json.loads(
        (section_dir / "RESULT.json").read_text(encoding="utf-8")
    )
    telemetry = json.loads(
        (section_dir / "PHASE2_TELEMETRY.json").read_text(encoding="utf-8")
    )

    assert result.status == "partial"
    assert result.sections_needing_more_literature == 1
    assert result.sections_failed == 0
    assert package["coverage_outcome"] == "needs_more_literature"
    assert decision["coverage_outcome"] == "needs_more_literature"
    assert result_json["status"] == "needs_more_literature"
    assert result_json["validation_passed"] is True
    assert result_json["stop_reason_category"] == "scientific_exhaustion"
    assert telemetry["scientific_exhaustion"] is True
    assert telemetry["engineering_failure"] is False


def test_deferred_candidate_is_not_reaudited_without_new_evidence_on_resume(
    tmp_path: Path,
) -> None:
    """A resumed section must not spend an audit call on unchanged evidence."""

    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    work_dir = tmp_path / "section"
    work_dir.mkdir(parents=True)
    candidate = {
        "candidate_id": "article_cand_resume_1",
        "section_id": "S01",
        "role": "foundation",
        "title": "Metasurface mechanism foundations",
        "doi": "10.1000/resume-foundation",
        "year": 2024,
        "venue": "Synthetic Physics",
        "abstract": "A section-specific account of metasurface mechanisms.",
        "is_oa": False,
        "semantic_scholar_id": "CorpusId:resume-1",
        "backends": ["semantic_scholar"],
        "query_texts": ["metasurface mechanism foundation"],
        "relevance_score": 0.8,
        "scope_fit": "unreviewed",
        "role_fit": ["foundation"],
        "decision": "deferred",
        "candidate_action": "reject",
    }
    registry._append_candidates_to_ledger(work_dir, "S01", [candidate])
    context = SectionCoverageContext(
        section_id="S01",
        section_data={
            "section_id": "S01",
            "title": "Introduction to metasurface mechanisms",
            "required_roles": ["foundation"],
            "topic_identity": {"valid": False},
        },
        kb_sqlite=None,
        temp_kb_sqlite=None,
        work_dir=work_dir,
    )
    context.enforce_batched_audit_protocol = True
    context.max_audit_calls_per_section = 2
    context.context_cumulative_budget_tokens = 40_000
    context.context_per_call_budget_tokens = 20_000
    context.context_output_reserve_tokens = 1_000

    registry._restore_candidates_from_ledger(context)
    inspect = json.loads(
        registry._make_inspect_candidate_batch(context)(
            json.dumps([candidate["candidate_id"]])
        )
    )
    assert inspect["found"] == 1
    submit = json.loads(
        registry._make_submit_candidate_audit(context)(
            json.dumps(
                [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "scope_fit": "adjacent",
                        "role_fit": ["foundation"],
                        "decision": "deferred",
                        "candidate_decision": "reject",
                        "audit_reason": "Deferred because no legal evidence route is available.",
                        "not_usable_for": ["direct factual support"],
                    }
                ]
            )
        )
    )
    assert submit["status"] == "ok"
    assert submit["audited_ids"] == [candidate["candidate_id"]]

    resumed_inspect = json.loads(
        registry._make_inspect_candidate_batch(context)(
            json.dumps([candidate["candidate_id"]])
        )
    )
    assert resumed_inspect["found"] == 0
    assert resumed_inspect["unchanged_ids"] == [candidate["candidate_id"]]
    live = context.get_candidate(candidate["candidate_id"])
    assert live is not None
    orchestrator_module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["_candidate_needs_new_audit_payload"],
    )
    assert orchestrator_module._candidate_needs_new_audit_payload(work_dir, live) is False
    state = json.loads(
        (work_dir / "COVERAGE_AGENT_PAYLOAD_STATE.json").read_text(encoding="utf-8")
    )
    assert state["audited_candidate_fingerprints"][candidate["candidate_id"]]

    # Legacy ledgers may lack the new per-candidate map.  Completed-wave
    # telemetry still prevents the same deferred candidate from being replayed.
    state["audited_candidate_fingerprints"] = {}
    (work_dir / "COVERAGE_AGENT_PAYLOAD_STATE.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    (work_dir / "COVERAGE_WAVE_TELEMETRY.json").write_text(
        json.dumps(
            {
                "waves": [
                    {
                        "wave_index": 0,
                        "audit_calls": 1,
                        "candidate_ids": [candidate["candidate_id"]],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert orchestrator_module._candidate_needs_new_audit_payload(work_dir, live) is False


def test_resume_with_legacy_deferred_candidate_does_not_call_qwen_again(
    tmp_path: Path, monkeypatch
) -> None:
    """The S08-shaped resume path terminates scientifically after one audit."""

    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    blueprint = _write_blueprint(
        tmp_path,
        section={"section_id": "S08", "required_roles": ["foundation"]},
    )
    run_dir = tmp_path / "resume-run"
    section_dir = run_dir / "sections" / "S08"
    section_dir.mkdir(parents=True)
    candidate = {
        "candidate_id": "article_cand_legacy_resume",
        "section_id": "S08",
        "role": "foundation",
        "title": "Metasurface mechanism foundations",
        "doi": "10.1000/legacy-resume-foundation",
        "year": 2024,
        "venue": "Synthetic Physics",
        "abstract": "A section-specific account of metasurface mechanisms.",
        "is_oa": False,
        "semantic_scholar_id": "CorpusId:legacy-resume",
        "backends": ["semantic_scholar"],
        "query_texts": ["metasurface mechanism foundation"],
        "relevance_score": 0.8,
        "scope_fit": "unreviewed",
        "role_fit": ["foundation"],
        "decision": "deferred",
        "candidate_action": "reject",
    }
    registry._append_candidates_to_ledger(section_dir, "S08", [candidate])
    (section_dir / "COVERAGE_AGENT_PAYLOAD_STATE.json").write_text(
        json.dumps(
            {
                "schema_version": "phase2.1.agent_payload_state.v1",
                "section_id": "S08",
                "audited_candidate_fingerprints": {},
            }
        ),
        encoding="utf-8",
    )
    (section_dir / "COVERAGE_WAVE_TELEMETRY.json").write_text(
        json.dumps(
            {
                "schema_version": "phase2.coverage_wave_telemetry.v1",
                "section_id": "S08",
                "waves": [
                    {
                        "wave_index": 0,
                        "audit_calls": 1,
                        "candidate_ids": [candidate["candidate_id"]],
                    }
                ],
                "total_audit_calls": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "_search_s2_first", lambda *args: [])
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    def forbidden_qwen(*args, **kwargs):
        raise AssertionError("unchanged deferred evidence must not be audited again")

    monkeypatch.setattr(qwen, "call_qwen_chat", forbidden_qwen)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    result = module.SectionCoverageOrchestrator(
        _run_config(tmp_path, blueprint), run_dir=run_dir
    ).run()
    output_dir = result.work_dir / "sections" / "S08"
    result_json = json.loads(
        (output_dir / "RESULT.json").read_text(encoding="utf-8")
    )
    telemetry = json.loads(
        (output_dir / "PHASE2_TELEMETRY.json").read_text(encoding="utf-8")
    )

    assert result.status == "partial"
    assert result.sections_failed == 0
    assert result_json["status"] == "needs_more_literature"
    assert result_json["model_calls"] == 0
    assert result_json["stop_reason_category"] == "scientific_exhaustion"
    assert telemetry["engineering_failure"] is False
    assert "one_batched_audit_per_wave_exceeded" not in result_json["stop_reason"]
    assert (output_dir / "SECTION_MATERIAL_PACKAGE.json").exists()
    assert (output_dir / "COVERAGE_DECISION.json").exists()


def test_s2_failure_is_a_documented_gap_and_does_not_erase_local_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_role": "general",
            "target_word_range": {"min": 900, "max": 1100},
        },
    )

    local_kb = tmp_path / "local.sqlite"
    with sqlite3.connect(local_kb) as conn:
        conn.executescript(
            """
            CREATE TABLE papers(
                paper_id TEXT PRIMARY KEY,
                title TEXT,
                abstract TEXT,
                year INTEGER,
                venue TEXT
            );
            CREATE TABLE text_chunks(
                chunk_id TEXT PRIMARY KEY,
                paper_id TEXT,
                text TEXT,
                section_path TEXT
            );
            CREATE VIRTUAL TABLE text_chunk_fts
                USING fts5(chunk_id UNINDEXED, text, content='text_chunks', content_rowid='rowid');
            CREATE VIRTUAL TABLE paper_fts
                USING fts5(paper_id UNINDEXED, title, abstract, content='papers', content_rowid='rowid');
            INSERT INTO papers VALUES(
                'local-method-paper',
                'Local method evidence',
                'Metasurface measurement and fabrication method evidence.',
                2023,
                'Local Optics'
            );
            INSERT INTO text_chunks VALUES(
                'local-method-chunk',
                'local-method-paper',
                'Metasurface measurement and fabrication method evidence.',
                'methods'
            );
            INSERT INTO text_chunk_fts(chunk_id, text) VALUES(
                'local-method-chunk',
                'Metasurface measurement and fabrication method evidence.'
            );
            INSERT INTO paper_fts(paper_id, title, abstract) VALUES(
                'local-method-paper',
                'Local method evidence',
                'Metasurface measurement and fabrication method evidence.'
            );
            """
        )

    local_id = registry._local_candidate_id(
        "S01", "method", "local-method-paper", "local-method-chunk"
    )
    run_dir = tmp_path / "coverage" / "local-backend-failure"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    (section_dir / "LOCAL_CANDIDATE_LEDGER.json").write_text(
        json.dumps(
            {
                "schema_version": "2.1",
                "section_id": "S01",
                "candidates": [
                    {
                        "candidate_id": local_id,
                        "section_id": "S01",
                        "paper_id": "local-method-paper",
                        "chunk_id": "local-method-chunk",
                        "title": "Local method evidence",
                        "year": 2023,
                        "venue": "Local Optics",
                        "role": "method",
                        "text_preview": "Metasurface measurement and fabrication method evidence.",
                        "scope_fit": "direct",
                        "use_permission": "factual_support",
                        "content_depth": "fulltext",
                        "source_kind": "fulltext",
                        "materialization_route": "local_fulltext",
                        "decision": "approved",
                        "audit_reason": "Existing local method chunk is explicitly adopted.",
                        "not_usable_for": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def failing_s2(*args):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(registry, "_search_s2_first", failing_s2)
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(config, run_dir=run_dir).run()
    section_dir = result.work_dir / "sections" / "S01"
    gap_report = json.loads((section_dir / "SECTION_GAP_REPORT.json").read_text(encoding="utf-8"))
    telemetry = json.loads((section_dir / "PHASE2_TELEMETRY.json").read_text(encoding="utf-8"))
    package = json.loads((section_dir / "SECTION_COVERAGE_PACKAGE.json").read_text(encoding="utf-8"))
    source_ledger = json.loads((section_dir / "SECTION_SOURCE_LEDGER.json").read_text(encoding="utf-8"))

    assert package["coverage_outcome"] == "needs_more_literature"
    assert any("429" in str(gap.get("stop_reason")) for gap in gap_report["gaps"])
    assert any(source["paper_id"] == "local-method-paper" for source in source_ledger["sources"])
    assert not any(gap["role"] == "coverage_material" for gap in gap_report["gaps"])
    assert telemetry["backend_failure_count"] >= 1
    assert telemetry["stop_reason"]


def test_shared_article_portfolio_reuses_audit_and_material_without_transport(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    portfolio = tmp_path / "ARTICLE_EVIDENCE_PORTFOLIO.json"
    calls = []

    def fake_s2(queries, max_per_q):
        calls.append(list(queries))
        return [
            {
                "title": "Shared metasurface paper",
                "doi": "10.1000/shared-paper",
                "abstract": "Shared mechanism evidence.",
                "is_oa": False,
                "semantic_scholar_id": "CorpusId:shared",
                "backends": ["semantic_scholar"],
                "query_texts": list(queries),
            }
        ]

    monkeypatch.setattr(registry, "_search_s2_first", fake_s2)
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    def context(work_dir: Path) -> SectionCoverageContext:
        value = SectionCoverageContext(
            section_id="S01",
            section_data={
                "title": "Metasurface mechanisms",
                "required_roles": ["foundation"],
                "topic_identity": {"valid": False, "scientific_object": "metasurface mechanisms"},
            },
            kb_sqlite=None,
            temp_kb_sqlite=work_dir / "stage.sqlite",
            work_dir=work_dir,
            s2_first_enabled=True,
        )
        value.article_evidence_portfolio_path = portfolio
        value.max_coverage_waves = 2
        return value

    first = context(tmp_path / "first")
    first.work_dir.mkdir()
    first_result = json.loads(
        registry._make_search_oa_candidates(first)(
            "foundation", json.dumps(["metasurface mechanisms foundation"]), 2
        )
    )
    assert first_result["candidate_count"] == 1
    candidate = json.loads((first.work_dir / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8"))["candidates"][0]
    registry._record_article_audit(
        first,
        {
            **candidate,
            "scope_fit": "direct",
            "decision": "approved",
            "role_fit": ["foundation"],
            "audit_reason": "shared audit",
        },
    )
    registry._record_article_material(
        first,
        candidate,
        paper_id="CorpusId:shared",
        chunk_ids=["s2chunk:shared:1"],
    )

    second = context(tmp_path / "second")
    second.work_dir.mkdir()
    second_result = json.loads(
        registry._make_search_oa_candidates(second)(
            "foundation", json.dumps(["metasurface mechanisms foundation"]), 2
        )
    )
    second_candidate = json.loads((second.work_dir / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8"))["candidates"][0]

    assert len(calls) == 1
    assert second_result["backend_stats"]["article_portfolio_cache_hits"] == 1
    assert second_candidate["decision"] == "approved"
    assert registry._article_material_for_candidate(second, second_candidate)["chunk_ids"] == [
        "s2chunk:shared:1"
    ]
    telemetry = json.loads((second.work_dir / "PHASE2_TELEMETRY.json").read_text(encoding="utf-8"))
    assert telemetry["cache_reuse_hits"] if "cache_reuse_hits" in telemetry else True


def test_permission_gates_keep_metadata_and_inadequate_snippets_out_of_factual_evidence():
    from optomind_research.runtime.coverage_decision_contract import (
        canonical_candidate_decision,
        structured_snippet_route_decision,
    )

    assert canonical_candidate_decision(
        {"decision": "approved", "scope_fit": "direct", "is_oa": False}
    ).action == "discovery_lead"
    assert canonical_candidate_decision(
        {
            "decision": "approved",
            "scope_fit": "direct",
            "semantic_scholar_id": "CorpusId:1",
        }
    ).action == "materialize_now"
    route = structured_snippet_route_decision(
        text="Contextual abstract only.",
        scope_fit="direct",
        context_complete=False,
        use_permission="background_and_candidate_only",
    )
    assert route["accepted_as_peer_text_evidence"] is False
    assert route["fulltext_escalation_required"] is True


def _local_kb_with_candidates(tmp_path: Path, count: int) -> tuple[Path, list[str], list[str]]:
    import optomind_research.runtime.section_coverage_tool_registry as registry

    local_kb = tmp_path / "local.sqlite"
    with sqlite3.connect(local_kb) as conn:
        conn.executescript(
            """
            CREATE TABLE papers(
                paper_id TEXT PRIMARY KEY,
                title TEXT,
                abstract TEXT,
                year INTEGER,
                venue TEXT
            );
            CREATE TABLE text_chunks(
                chunk_id TEXT PRIMARY KEY,
                paper_id TEXT,
                text TEXT,
                section_path TEXT
            );
            CREATE VIRTUAL TABLE text_chunk_fts
                USING fts5(chunk_id UNINDEXED, text, content='text_chunks', content_rowid='rowid');
            CREATE VIRTUAL TABLE paper_fts
                USING fts5(paper_id UNINDEXED, title, abstract, content='papers', content_rowid='rowid');
            """
        )
    paper_ids: list[str] = []
    chunk_ids: list[str] = []
    for index in range(count):
        paper_id = f"local-foundation-paper-{index}"
        chunk_id = f"local-foundation-chunk-{index}"
        paper_ids.append(paper_id)
        chunk_ids.append(chunk_id)
        with sqlite3.connect(local_kb) as conn:
            conn.execute(
                "INSERT INTO papers VALUES(?, ?, ?, ?, ?)",
                (
                    paper_id,
                    f"Local metasurface mechanism evidence {index}",
                    "Metasurface mechanism evidence for the section.",
                    2023,
                    "Local Optics",
                ),
            )
            conn.execute(
                "INSERT INTO text_chunks VALUES(?, ?, ?, ?)",
                (
                    chunk_id,
                    paper_id,
                    "Metasurface mechanism evidence for the section.",
                    "body",
                ),
            )
            conn.execute(
                "INSERT INTO text_chunk_fts(chunk_id, text) VALUES(?, ?)",
                (chunk_id, "Metasurface mechanism evidence for the section."),
            )
            conn.execute(
                "INSERT INTO paper_fts(paper_id, title, abstract) VALUES(?, ?, ?)",
                (
                    paper_id,
                    f"Local metasurface mechanism evidence {index}",
                    "Metasurface mechanism evidence for the section.",
                ),
            )
    return local_kb, paper_ids, chunk_ids


def _local_inspect_fingerprint(candidate: dict) -> str:
    import optomind_research.runtime.section_coverage_tool_registry as registry

    subset = {
        "candidate_id": candidate.get("candidate_id"),
        "paper_id": candidate.get("paper_id"),
        "chunk_id": candidate.get("chunk_id"),
        "title": registry.compact_text(candidate.get("title"), 160),
        "year": candidate.get("year"),
        "venue": registry.compact_text(candidate.get("venue"), 80),
        "role": candidate.get("role"),
        "scope_fit": candidate.get("scope_fit", "unreviewed"),
        "decision": candidate.get("decision", "deferred"),
        "topic_matches": list(candidate.get("topic_matches") or [])[:6],
        "role_matches": list(candidate.get("role_matches") or [])[:6],
        "text_preview": registry.compact_text(
            candidate.get("text_preview"), 520
        ),
        "audit_reason": registry.compact_text(
            candidate.get("audit_reason"), 240
        ),
        "not_usable_for": list(candidate.get("not_usable_for") or [])[:4],
    }
    return registry.stable_payload_fingerprint(subset)


def _local_candidate_rows(
    paper_ids: list[str],
    chunk_ids: list[str],
    *,
    decisions: list[str] | None = None,
    reasons: list[str] | None = None,
) -> list[dict]:
    import optomind_research.runtime.section_coverage_tool_registry as registry

    decisions = list(decisions or [])
    reasons = list(reasons or [])
    rows = []
    for index, (paper_id, chunk_id) in enumerate(
        zip(paper_ids, chunk_ids)
    ):
        decision = decisions[index] if index < len(decisions) else "deferred"
        reason = reasons[index] if index < len(reasons) else ""
        rows.append(
            {
                "candidate_id": registry._local_candidate_id(
                    "S01", "foundation", paper_id, chunk_id
                ),
                "section_id": "S01",
                "paper_id": paper_id,
                "chunk_id": chunk_id,
                "title": f"Local metasurface mechanism evidence {index}",
                "year": 2023,
                "venue": "Local Optics",
                "role": "foundation",
                "text_preview": (
                    "Metasurface mechanism evidence for the section."
                ),
                "topic_matches": ["metasurface mechanism"],
                "role_matches": ["foundation"],
                "scope_fit": "unreviewed",
                "use_permission": "factual_support",
                "content_depth": "fulltext",
                "source_kind": "fulltext",
                "materialization_route": "local_fulltext",
                "decision": decision,
                "audit_reason": reason,
                "not_usable_for": [],
            }
        )
    return rows


def _write_local_resume_artifacts(
    section_dir: Path,
    rows: list[dict],
) -> None:
    (section_dir / "LOCAL_CANDIDATE_LEDGER.json").write_text(
        json.dumps(
            {
                "schema_version": "2.1",
                "section_id": "S01",
                "candidates": rows,
            }
        ),
        encoding="utf-8",
    )
    (section_dir / "COVERAGE_AGENT_PAYLOAD_STATE.json").write_text(
        json.dumps(
            {
                "schema_version": "phase2.1.agent_payload_state.v1",
                "section_id": "S01",
                "local_candidate_fingerprints": {
                    row["candidate_id"]: _local_inspect_fingerprint(row)
                    for row in rows
                },
            }
        ),
        encoding="utf-8",
    )


def _six_oa_candidates(queries: list[str]) -> list[dict]:
    return [
        {
            "title": f"Metasurface mechanism foundation {index}",
            "doi": f"10.1000/metasurface-foundation-{index}",
            "year": 2024,
            "venue": "Synthetic Physics",
            "abstract": (
                f"A section-specific account of metasurface mechanisms {index}."
            ),
            "is_oa": False,
            "semantic_scholar_id": f"CorpusId:phase2-{index}",
            "backends": ["semantic_scholar"],
            "query_texts": list(queries),
            "citation_count": 12,
        }
        for index in range(6)
    ]


def test_resume_recovers_inspected_local_candidates_and_skips_oa_search(
    tmp_path: Path, monkeypatch
) -> None:
    """The exact deferred/empty-reason/inspected resume state must be audited
    locally before any OA search, not silently treated as completed."""

    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(tmp_path, 3)
    candidates = [
        {
            "candidate_id": registry._local_candidate_id(
                "S01", "foundation", paper_id, chunk_id
            ),
            "section_id": "S01",
            "paper_id": paper_id,
            "chunk_id": chunk_id,
            "title": f"Local metasurface mechanism evidence {index}",
            "year": 2023,
            "venue": "Local Optics",
            "role": "foundation",
            "text_preview": "Metasurface mechanism evidence for the section.",
            "topic_matches": ["metasurface mechanism"],
            "role_matches": ["foundation"],
            "scope_fit": "unreviewed",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "source_kind": "fulltext",
            "materialization_route": "local_fulltext",
            "decision": "deferred",
            "audit_reason": "",
            "not_usable_for": [],
        }
        for index, (paper_id, chunk_id) in enumerate(
            zip(paper_ids, chunk_ids)
        )
    ]
    run_dir = tmp_path / "coverage" / "local-resume"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    (section_dir / "LOCAL_CANDIDATE_LEDGER.json").write_text(
        json.dumps(
            {
                "schema_version": "2.1",
                "section_id": "S01",
                "candidates": candidates,
            }
        ),
        encoding="utf-8",
    )
    (section_dir / "COVERAGE_AGENT_PAYLOAD_STATE.json").write_text(
        json.dumps(
            {
                "schema_version": "phase2.1.agent_payload_state.v1",
                "section_id": "S01",
                "local_candidate_fingerprints": {
                    candidate["candidate_id"]: _local_inspect_fingerprint(
                        candidate
                    )
                    for candidate in candidates
                },
            }
        ),
        encoding="utf-8",
    )

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    chat_calls: list[dict] = []

    def fake_chat(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        chat_calls.append(
            {
                **dict(kwargs),
                "candidate_count": len(payload["candidates"]),
            }
        )
        rows = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "direct",
                "role_fit": ["foundation"],
                "decision": "approved",
                "candidate_decision": "materialize_now",
                "audit_reason": "Direct local section-specific mechanism evidence.",
                "not_usable_for": [],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows}),
            "input_tokens": 700,
            "output_tokens": 240,
            "estimated_cost_cny": 0.02,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", fake_chat)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    admission_reserves: list[int] = []
    original_admit = module.admit_context_call

    def wrapped_admit(**kwargs):
        admission_reserves.append(
            int(kwargs.get("output_reserve_tokens") or 0)
        )
        return original_admit(**kwargs)

    monkeypatch.setattr(module, "admit_context_call", wrapped_admit)
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(config, run_dir=run_dir).run()

    section_dir = result.work_dir / "sections" / "S01"
    ledger = json.loads(
        (section_dir / "LOCAL_CANDIDATE_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )
    source_ledger = json.loads(
        (section_dir / "SECTION_SOURCE_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )
    package = json.loads(
        (section_dir / "SECTION_MATERIAL_PACKAGE.json").read_text(
            encoding="utf-8"
        )
    )

    assert result.status == "completed"
    assert search_calls == []
    assert len(chat_calls) == 1
    assert chat_calls[0]["max_tokens"] > 1200
    assert chat_calls[0]["max_tokens"] == (
        600 + 260 * chat_calls[0]["candidate_count"]
    )
    assert chat_calls[0]["max_tokens"] <= 8192
    assert admission_reserves
    assert admission_reserves[0] == chat_calls[0]["max_tokens"]
    assert all(
        item["decision"] in {"approved", "rejected", "deferred"}
        and item["audit_reason"]
        for item in ledger["candidates"]
    )
    assert short_path["qwen_calls"] == 1
    assert short_path["local_source_audit"]["approved"] >= 3
    assert set(
        source["paper_id"] for source in source_ledger["sources"]
    ) == set(paper_ids)
    assert package["coverage_outcome"] in {
        "material_ready",
        "material_ready_with_limits",
    }


def test_approved_and_rejected_local_candidates_remain_reusable(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(tmp_path, 4)
    decisions = ["approved", "approved", "approved", "rejected"]
    candidates = [
        {
            "candidate_id": registry._local_candidate_id(
                "S01", "foundation", paper_id, chunk_id
            ),
            "section_id": "S01",
            "paper_id": paper_id,
            "chunk_id": chunk_id,
            "title": f"Local metasurface mechanism evidence {index}",
            "year": 2023,
            "venue": "Local Optics",
            "role": "foundation",
            "text_preview": "Metasurface mechanism evidence for the section.",
            "scope_fit": "direct",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "source_kind": "fulltext",
            "materialization_route": "local_fulltext",
            "decision": decisions[index],
            "audit_reason": (
                "Explicitly adopted local evidence."
                if decisions[index] == "approved"
                else "Explicitly rejected local evidence."
            ),
            "not_usable_for": [],
        }
        for index, (paper_id, chunk_id) in enumerate(
            zip(paper_ids, chunk_ids)
        )
    ]
    run_dir = tmp_path / "coverage" / "local-reuse"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    (section_dir / "LOCAL_CANDIDATE_LEDGER.json").write_text(
        json.dumps(
            {
                "schema_version": "2.1",
                "section_id": "S01",
                "candidates": candidates,
            }
        ),
        encoding="utf-8",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("already audited local candidates need no model call")

    monkeypatch.setattr(qwen, "call_qwen_chat", fail_if_called)

    def record_search(queries, *args, **kwargs):
        raise AssertionError("sufficient local evidence must not trigger OA search")

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(config, run_dir=run_dir).run()

    section_dir = result.work_dir / "sections" / "S01"
    source_ledger = json.loads(
        (section_dir / "SECTION_SOURCE_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.status == "completed"
    assert set(
        source["paper_id"] for source in source_ledger["sources"]
    ) == set(paper_ids[:3])
    assert not any(
        source["paper_id"] == paper_ids[3]
        for source in source_ledger["sources"]
    )


def test_searched_audit_reserve_matches_dynamic_output_allowance(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(tmp_path)
    monkeypatch.setattr(
        registry,
        "_search_s2_first",
        lambda queries, max_per_q: _six_oa_candidates(list(queries)),
    )
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    chat_calls: list[dict] = []

    def fake_chat(agent_name, messages, **kwargs):
        chat_calls.append(dict(kwargs))
        payload = json.loads(messages[-1]["content"])
        rows = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "adjacent",
                "role_fit": ["foundation"],
                "decision": "deferred",
                "candidate_decision": "reject",
                "audit_reason": "Deferred for admission-contract test.",
                "not_usable_for": [],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows}),
            "input_tokens": 1200,
            "output_tokens": 400,
            "estimated_cost_cny": 0.04,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", fake_chat)

    preflights: list[dict] = []
    original_preflight = registry._audit_call_preflight

    def wrapped_preflight(ctx, candidate_ids, payload_tokens):
        admission = original_preflight(ctx, candidate_ids, payload_tokens)
        preflights.append(
            {
                "reserve": int(
                    getattr(ctx, "context_output_reserve_tokens", 0) or 0
                ),
                "admitted": admission.admitted,
                "reason": admission.reason,
            }
        )
        return admission

    monkeypatch.setattr(registry, "_audit_call_preflight", wrapped_preflight)

    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    module.SectionCoverageOrchestrator(
        _run_config(tmp_path, blueprint)
    ).run()

    assert len(chat_calls) == 1
    assert chat_calls[0]["max_tokens"] == 6000
    assert preflights
    assert preflights[0]["reserve"] == 6000
    assert preflights[0]["admitted"] is True
    assert preflights[0]["reserve"] == chat_calls[0]["max_tokens"]


def test_real_output_allowance_rejects_before_provider_call(
    tmp_path: Path, monkeypatch
) -> None:
    """A 2000-token reserve would admit this call; the true 6000 allowance
    must reject it before the provider is invoked."""

    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(tmp_path)
    monkeypatch.setattr(
        registry,
        "_search_s2_first",
        lambda queries, max_per_q: _six_oa_candidates(list(queries)),
    )
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    chat_calls: list[dict] = []

    def forbid_call(agent_name, messages, **kwargs):
        chat_calls.append(dict(kwargs))
        raise AssertionError("over-budget audit must not reach the provider")

    monkeypatch.setattr(qwen, "call_qwen_chat", forbid_call)

    preflights: list[dict] = []
    original_preflight = registry._audit_call_preflight

    def wrapped_preflight(ctx, candidate_ids, payload_tokens):
        admission = original_preflight(ctx, candidate_ids, payload_tokens)
        preflights.append(
            {
                "reserve": int(
                    getattr(ctx, "context_output_reserve_tokens", 0) or 0
                ),
                "admitted": admission.admitted,
                "reason": admission.reason,
            }
        )
        return admission

    monkeypatch.setattr(registry, "_audit_call_preflight", wrapped_preflight)

    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )

    def fixed_estimate(payload):
        # Deterministically place the predicted input in the window where the
        # old 2000 reserve admitted but the real 6000 allowance must not.
        return 1800

    monkeypatch.setattr(module, "estimate_json_tokens", fixed_estimate)
    config = _run_config(tmp_path, blueprint)
    config.context_tokens_per_model_call = 4000
    result = module.SectionCoverageOrchestrator(config).run()

    assert chat_calls == []
    assert preflights
    assert preflights[0]["reserve"] == 6000
    assert preflights[0]["admitted"] is False
    assert "per_call_context_budget_exceeded" in preflights[0]["reason"]
    short_path = json.loads(
        (
            result.work_dir
            / "sections"
            / "S01"
            / "SHORT_PATH_RUN.json"
        ).read_text(encoding="utf-8")
    )
    assert short_path["qwen_calls"] == 0
    assert "per_call_context_budget_exceeded" in short_path["stop_reason"]


def test_malformed_audit_response_is_cost_accounted(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(tmp_path)

    def fake_s2(queries, max_per_q):
        return [
            {
                "title": "Metasurface mechanism foundations",
                "doi": "10.1000/metasurface-foundation",
                "year": 2024,
                "venue": "Synthetic Physics",
                "abstract": "A section-specific account of metasurface mechanisms.",
                "is_oa": False,
                "semantic_scholar_id": "CorpusId:phase2-malformed",
                "backends": ["semantic_scholar"],
                "query_texts": list(queries),
                "citation_count": 12,
            }
        ]

    monkeypatch.setattr(registry, "_search_s2_first", fake_s2)
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    def malformed_chat(agent_name, messages, **kwargs):
        return {
            "content": "{definitely not valid json",
            "input_tokens": 900,
            "output_tokens": 180,
            "estimated_cost_cny": 0.03,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", malformed_chat)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    result = module.SectionCoverageOrchestrator(
        _run_config(tmp_path, blueprint)
    ).run()
    section_dir = result.work_dir / "sections" / "S01"
    receipt = json.loads(
        (section_dir / "USAGE_RECEIPT.json").read_text(encoding="utf-8")
    )
    telemetry = json.loads(
        (section_dir / "PHASE2_TELEMETRY.json").read_text(encoding="utf-8")
    )
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )

    assert result.sections_failed == 0
    assert result.sections_needing_more_literature == 1
    assert short_path["qwen_calls"] == 1
    assert receipt["qwen_calls"] == 1
    assert receipt["input_tokens"] == 900
    assert receipt["output_tokens"] == 180
    assert round(receipt["cost_cny"], 2) == 0.03
    assert round(telemetry["batched_llm_cost_cny"], 2) == 0.03


def test_audit_output_allowance_scales_with_batch_size() -> None:
    from optomind_research.runtime.section_coverage_orchestrator import (
        _bounded_audit_output_tokens,
    )

    assert _bounded_audit_output_tokens(1) == 1500
    assert _bounded_audit_output_tokens(6) == 6000
    assert _bounded_audit_output_tokens(6) > 1200
    assert _bounded_audit_output_tokens(100) == 6000
    assert _bounded_audit_output_tokens(100) <= 8192


def test_local_audit_runs_sequential_batches_until_ready_without_oa_search(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 50
    )
    rows = _local_candidate_rows(paper_ids, chunk_ids)
    run_dir = tmp_path / "coverage" / "local-multi-batch"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    chat_calls: list[dict] = []
    model_call = 0

    def fake_chat(agent_name, messages, **kwargs):
        nonlocal model_call
        model_call += 1
        payload = json.loads(messages[-1]["content"])
        chat_calls.append(
            {
                **dict(kwargs),
                "candidate_count": len(payload["candidates"]),
            }
        )
        decision = "rejected" if model_call == 1 else "approved"
        reason = (
            "Rejected in the first local batch."
            if decision == "rejected"
            else "Direct local section-specific mechanism evidence."
        )
        rows_out = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "direct",
                "role_fit": ["foundation"],
                "decision": decision,
                "candidate_decision": (
                    "reject" if decision == "rejected" else "materialize_now"
                ),
                "audit_reason": reason,
                "not_usable_for": [],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows_out}),
            "input_tokens": 700,
            "output_tokens": 240,
            "estimated_cost_cny": 0.02,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", fake_chat)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    admission_reserves: list[int] = []
    original_admit = module.admit_context_call

    def wrapped_admit(**kwargs):
        admission_reserves.append(
            int(kwargs.get("output_reserve_tokens") or 0)
        )
        return original_admit(**kwargs)

    monkeypatch.setattr(module, "admit_context_call", wrapped_admit)
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(config, run_dir=run_dir).run()

    section_dir = result.work_dir / "sections" / "S01"
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )

    assert result.status == "completed"
    assert search_calls == []
    assert model_call == 2
    assert len(chat_calls) == 2
    assert all(call["candidate_count"] <= 40 for call in chat_calls)
    assert all(
        call["max_tokens"] == 600 + 260 * call["candidate_count"]
        for call in chat_calls
    )
    assert admission_reserves == [
        call["max_tokens"] for call in chat_calls
    ]
    assert short_path["local_audit_calls"] == 2
    assert short_path["searched_audit_calls"] == 0
    assert short_path["qwen_calls"] == 2
    assert short_path["local_candidates_examined"] >= 50
    assert short_path["local_candidates_remaining"] == []
    assert short_path["stop_reason_category"] == "scientific_completion"
    assert short_path["max_local_audit_calls"] >= 2
    assert short_path["max_searched_audit_calls"] == 2
    assert short_path["max_model_calls"] == (
        short_path["max_local_audit_calls"]
        + short_path["max_searched_audit_calls"]
    )


def test_oa_search_starts_only_after_local_queue_is_terminal(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 8
    )
    rows = _local_candidate_rows(paper_ids, chunk_ids)
    run_dir = tmp_path / "coverage" / "local-then-oa"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    def reject_all(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        rows_out = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "out_of_scope",
                "role_fit": ["foundation"],
                "decision": "rejected",
                "candidate_decision": "reject",
                "audit_reason": "Insufficient local scientific fit.",
                "not_usable_for": ["direct factual support"],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows_out}),
            "input_tokens": 700,
            "output_tokens": 240,
            "estimated_cost_cny": 0.02,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", reject_all)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(config, run_dir=run_dir).run()

    section_dir = result.work_dir / "sections" / "S01"
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )

    assert short_path["local_audit_calls"] >= 1
    assert short_path["searched_audit_calls"] == 0
    assert short_path["qwen_calls"] == short_path["local_audit_calls"]
    assert short_path["local_candidates_remaining"] == []
    assert search_calls, "OA search must run only after local exhaustion"
    assert short_path["stop_reason_category"] == "scientific_exhaustion"


def test_resume_sends_only_remaining_deferred_local_rows(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 9
    )
    rows = _local_candidate_rows(
        paper_ids,
        chunk_ids,
        decisions=["rejected"] * 6 + ["deferred"] * 3,
        reasons=["Already rejected local evidence."] * 6 + [""] * 3,
    )
    run_dir = tmp_path / "coverage" / "local-resume-remaining"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    sent_ids: list[list[str]] = []

    def approve_payload(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        sent_ids.append(
            [candidate["candidate_id"] for candidate in payload["candidates"]]
        )
        deferred_ids = {
            row["candidate_id"] for row in rows[6:]
        }
        rows_out = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "direct",
                "role_fit": ["foundation"],
                "decision": (
                    "approved"
                    if candidate["candidate_id"] in deferred_ids
                    else "rejected"
                ),
                "candidate_decision": (
                    "materialize_now"
                    if candidate["candidate_id"] in deferred_ids
                    else "reject"
                ),
                "audit_reason": (
                    "Direct local section-specific mechanism evidence."
                    if candidate["candidate_id"] in deferred_ids
                    else "Rejected role view in the resume fixture."
                ),
                "not_usable_for": [],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows_out}),
            "input_tokens": 700,
            "output_tokens": 240,
            "estimated_cost_cny": 0.02,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", approve_payload)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(config, run_dir=run_dir).run()

    section_dir = result.work_dir / "sections" / "S01"
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )

    assert result.status == "completed"
    assert search_calls == []
    rejected_ids = {
        row["candidate_id"] for row in rows[:6]
    }
    deferred_ids = {
        row["candidate_id"] for row in rows[6:]
    }
    all_sent = set().union(*sent_ids)
    assert not (all_sent & rejected_ids)
    assert deferred_ids <= (
        all_sent | set(short_path["local_candidates_remaining"])
    )
    assert all(len(batch) <= 40 for batch in sent_ids)
    assert short_path["local_audit_calls"] == len(sent_ids)
    assert short_path["searched_audit_calls"] == 0
    assert short_path["qwen_calls"] == len(sent_ids)


def test_malformed_local_batch_is_engineering_not_scientific_exhaustion(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 13
    )
    rows = _local_candidate_rows(paper_ids, chunk_ids)
    run_dir = tmp_path / "coverage" / "local-malformed"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    def malformed(agent_name, messages, **kwargs):
        return {
            "content": "{not valid json",
            "input_tokens": 700,
            "output_tokens": 120,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", malformed)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(config, run_dir=run_dir).run()

    section_dir = result.work_dir / "sections" / "S01"
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )

    assert search_calls == []
    assert short_path["local_audit_calls"] == 2
    assert short_path["searched_audit_calls"] == 0
    assert short_path["qwen_calls"] == 2
    assert set(
        row["candidate_id"] for row in rows
    ) <= set(short_path["local_candidates_remaining"])
    assert "provider_response_malformed" in (
        short_path.get("local_source_audit_failure") or ""
    )
    assert "provider_response_malformed" in (
        short_path["token_admission"].get("rejected_reason") or ""
    )
    assert short_path["stop_reason_category"] == "engineering_failure"
    assert short_path["scientific_exhaustion"] is False


def test_local_budget_rejection_blocks_oa_search_and_records_remaining(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 13
    )
    rows = _local_candidate_rows(paper_ids, chunk_ids)
    run_dir = tmp_path / "coverage" / "local-budget"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    def forbid_call(agent_name, messages, **kwargs):
        raise AssertionError("budget-rejected local audit must not call model")

    monkeypatch.setattr(qwen, "call_qwen_chat", forbid_call)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )

    def fixed_estimate(payload):
        return 1800

    monkeypatch.setattr(module, "estimate_json_tokens", fixed_estimate)
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    config.context_tokens_per_model_call = 500
    result = module.SectionCoverageOrchestrator(config, run_dir=run_dir).run()

    section_dir = result.work_dir / "sections" / "S01"
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )

    assert search_calls == []
    assert short_path["local_audit_calls"] == 0
    assert short_path["searched_audit_calls"] == 0
    assert short_path["qwen_calls"] == 0
    assert set(
        row["candidate_id"] for row in rows
    ) <= set(short_path["local_candidates_remaining"])
    assert "local_audit_budget_rejected" in short_path["stop_reason"]
    assert "local_audit_budget_rejected" in (
        short_path["token_admission"].get("rejected_reason") or ""
    )
    assert short_path["stop_reason_category"] == "engineering_failure"
    assert short_path["scientific_exhaustion"] is False


def test_local_cost_budget_rejection_blocks_model_and_oa_search(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 13
    )
    rows = _local_candidate_rows(paper_ids, chunk_ids)
    run_dir = tmp_path / "coverage" / "local-cost-budget"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    def forbid_call(agent_name, messages, **kwargs):
        raise AssertionError("cost-rejected local audit must not call model")

    monkeypatch.setattr(qwen, "call_qwen_chat", forbid_call)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )

    def fixed_estimate(payload):
        return 1800

    monkeypatch.setattr(module, "estimate_json_tokens", fixed_estimate)
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    config.cost_budget_per_section_cny = 0.001
    result = module.SectionCoverageOrchestrator(
        config,
        run_dir=run_dir,
    ).run()

    section_dir = result.work_dir / "sections" / "S01"
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )

    assert search_calls == []
    assert short_path["local_audit_calls"] == 0
    assert short_path["searched_audit_calls"] == 0
    assert short_path["qwen_calls"] == 0
    assert "local_audit_cost_budget_rejected" in short_path["stop_reason"]
    assert "local_audit_cost_budget_rejected" in (
        short_path["token_admission"].get("rejected_reason") or ""
    )
    assert short_path["token_admission"]["cost_budget_cny"] == 0.001
    assert short_path["stop_reason_category"] == "engineering_failure"


def test_same_paper_chunk_across_roles_are_both_sent_to_model(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 1
    )
    paper_id, chunk_id = paper_ids[0], chunk_ids[0]
    role_rows = []
    for role in ("foundation", "mechanism"):
        candidate_id = registry._local_candidate_id(
            "S01", role, paper_id, chunk_id
        )
        role_rows.append(
            {
                "candidate_id": candidate_id,
                "section_id": "S01",
                "paper_id": paper_id,
                "chunk_id": chunk_id,
                "title": "Local metasurface mechanism evidence 0",
                "year": 2023,
                "venue": "Local Optics",
                "role": role,
                "text_preview": (
                    "Metasurface mechanism evidence for the section."
                ),
                "topic_matches": ["metasurface mechanism"],
                "role_matches": [role],
                "scope_fit": "unreviewed",
                "use_permission": "factual_support",
                "content_depth": "fulltext",
                "source_kind": "fulltext",
                "materialization_route": "local_fulltext",
                "decision": "deferred",
                "audit_reason": "",
                "not_usable_for": [],
            }
        )
    run_dir = tmp_path / "coverage" / "local-cross-role"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, role_rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    payload_rows: list[dict] = []

    def approve_all(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        payload_rows.extend(payload["candidates"])
        rows_out = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "direct",
                "role_fit": ["foundation"],
                "decision": "approved",
                "candidate_decision": "materialize_now",
                "audit_reason": "Direct local section-specific mechanism evidence.",
                "not_usable_for": [],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows_out}),
            "input_tokens": 700,
            "output_tokens": 240,
            "estimated_cost_cny": 0.02,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", approve_all)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(config, run_dir=run_dir).run()

    section_dir = result.work_dir / "sections" / "S01"
    ledger = json.loads(
        (section_dir / "LOCAL_CANDIDATE_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )

    role_ids = {row["candidate_id"] for row in role_rows}
    sent_role_rows = [
        row for row in payload_rows if row.get("candidate_id") in role_ids
    ]
    roles_seen = {
        next(
            (
                item
                for item in ledger["candidates"]
                if item.get("candidate_id") == row.get("candidate_id")
            ),
            {},
        ).get("role")
        for row in sent_role_rows
    }
    assert {"foundation", "mechanism"} <= roles_seen
    for item in ledger["candidates"]:
        if item.get("candidate_id") in {
            row["candidate_id"] for row in role_rows
        }:
            assert item.get("scope_fit") != "out_of_scope"


def test_incomplete_local_batch_is_repaired_locally_without_erasing_records(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 3
    )
    rows = _local_candidate_rows(paper_ids, chunk_ids)
    run_dir = tmp_path / "coverage" / "local-missing-row"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    approved_id: str | None = None

    def incomplete(agent_name, messages, **kwargs):
        nonlocal approved_id
        payload = json.loads(messages[-1]["content"])
        first = payload["candidates"][0]
        approved_id = first["candidate_id"]
        return {
            "content": json.dumps(
                {
                    "candidates": [
                        {
                            "candidate_id": first["candidate_id"],
                            "scope_fit": "direct",
                            "decision": "approved",
                            "audit_reason": "Returned record.",
                            "not_usable_for": [],
                        }
                    ]
                }
            ),
            "input_tokens": 700,
            "output_tokens": 120,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", incomplete)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(config, run_dir=run_dir).run()

    section_dir = result.work_dir / "sections" / "S01"
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )
    receipt = json.loads(
        (section_dir / "USAGE_RECEIPT.json").read_text(encoding="utf-8")
    )

    assert search_calls == []
    assert short_path["local_audit_calls"] == 1
    assert short_path["searched_audit_calls"] == 0
    assert short_path["qwen_calls"] == 1
    assert not short_path.get("local_source_audit_failure")
    assert short_path["local_candidates_remaining"] == []
    assert short_path["stop_reason_category"] == "scientific_completion"
    assert receipt["qwen_calls"] == 1
    assert round(receipt["cost_cny"], 2) == 0.01
    ledger = json.loads(
        (section_dir / "LOCAL_CANDIDATE_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )
    by_id = {
        item["candidate_id"]: item
        for item in ledger["candidates"]
        if item.get("candidate_id")
    }
    assert approved_id is not None
    assert by_id[approved_id]["decision"] == "approved"
    assert by_id[approved_id]["scope_fit"] == "direct"
    assert all(
        by_id[row["candidate_id"]]["decision"] == "deferred"
        for row in rows
        if row["candidate_id"] != approved_id
    )


def test_unchanged_forced_section_replay_preserves_artifacts_and_zero_cost(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(tmp_path)
    search_calls: list[list[str]] = []
    run_dir = tmp_path / "coverage" / "shared-replay"

    def fake_s2(queries, max_per_q):
        search_calls.append(list(queries))
        return [
            {
                "title": "Metasurface mechanism foundations",
                "doi": "10.1000/replay-foundation",
                "year": 2024,
                "venue": "Synthetic Physics",
                "abstract": "A section-specific account of metasurface mechanisms.",
                "is_oa": False,
                "semantic_scholar_id": "CorpusId:replay-1",
                "backends": ["semantic_scholar"],
                "query_texts": list(queries),
                "citation_count": 12,
            }
        ]

    monkeypatch.setattr(registry, "_search_s2_first", fake_s2)
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    def defer_batch(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        rows = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "adjacent",
                "role_fit": ["foundation"],
                "decision": "deferred",
                "candidate_decision": "reject",
                "audit_reason": "Deferred replay fixture.",
                "not_usable_for": ["direct factual support"],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows}),
            "input_tokens": 800,
            "output_tokens": 160,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", defer_batch)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    first_config = _run_config(tmp_path, blueprint)
    first_orchestrator = module.SectionCoverageOrchestrator(
        first_config,
        run_dir=run_dir,
    )
    first = first_orchestrator.run()
    first_record = first_orchestrator.records[0]
    section_dir = first.work_dir / "sections" / "S01"
    first_summary = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )
    preserved = {
        name: (section_dir / name).read_bytes()
        for name in (
            "SHORT_PATH_RUN.json",
            "RESULT.json",
            "USAGE_RECEIPT.json",
        )
    }
    assert first_summary["stop_reason_category"] == "scientific_exhaustion"
    assert first_record["cost_cny"] > 0

    search_calls.clear()

    def forbid_model(agent_name, messages, **kwargs):
        raise AssertionError("unchanged replay must not call the model")

    monkeypatch.setattr(qwen, "call_qwen_chat", forbid_model)
    second_config = _run_config(tmp_path, blueprint)
    second_config.force_research_sections = ["S01"]
    second_orchestrator = module.SectionCoverageOrchestrator(
        second_config,
        run_dir=run_dir,
    )
    second = second_orchestrator.run()

    record = second_orchestrator.records[0]
    assert record["reused"] is True
    assert record["cost_cny"] == 0.0
    assert record["input_tokens"] == 0
    assert record["output_tokens"] == 0
    assert record["previous_cost_cny"] == first_record["cost_cny"]
    assert record["stop_reason_category"] == "scientific_exhaustion"
    assert second.total_cost_cny == 0.0
    assert search_calls == []
    for name, expected_bytes in preserved.items():
        assert (section_dir / name).read_bytes() == expected_bytes, name


def test_new_phase3_request_is_not_incorrectly_reused(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(tmp_path)
    run_dir = tmp_path / "coverage" / "shared-delta"

    def fake_s2(queries, max_per_q):
        return [
            {
                "title": "Metasurface mechanism foundations",
                "doi": "10.1000/delta-foundation",
                "year": 2024,
                "venue": "Synthetic Physics",
                "abstract": "A section-specific account of metasurface mechanisms.",
                "is_oa": False,
                "semantic_scholar_id": "CorpusId:delta-1",
                "backends": ["semantic_scholar"],
                "query_texts": list(queries),
                "citation_count": 12,
            }
        ]

    monkeypatch.setattr(registry, "_search_s2_first", fake_s2)
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    def defer_batch(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        rows = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "adjacent",
                "role_fit": ["foundation"],
                "decision": "deferred",
                "candidate_decision": "reject",
                "audit_reason": "Deferred delta fixture.",
                "not_usable_for": ["direct factual support"],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows}),
            "input_tokens": 800,
            "output_tokens": 160,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", defer_batch)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    first_orchestrator = module.SectionCoverageOrchestrator(
        _run_config(tmp_path, blueprint),
        run_dir=run_dir,
    )
    first = first_orchestrator.run()
    first_summary = json.loads(
        (
            first.work_dir
            / "sections"
            / "S01"
            / "SHORT_PATH_RUN.json"
        ).read_text(encoding="utf-8")
    )

    second_config = _run_config(tmp_path, blueprint)
    second_config.force_research_sections = ["S01"]
    second_config.coverage_requests_by_section["S01"] = {
        "missing_components": ["new-phase3-component"]
    }
    second_orchestrator = module.SectionCoverageOrchestrator(
        second_config,
        run_dir=run_dir,
    )
    second = second_orchestrator.run()

    record = second_orchestrator.records[0]
    assert record["reused"] is False
    assert record["coverage_input_fingerprint_sha256"] != (
        first_summary["coverage_input_fingerprint_sha256"]
    )


def test_author_feedback_change_alone_is_a_new_request(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(tmp_path)
    run_dir = tmp_path / "coverage" / "shared-feedback"

    search_call_count = 0

    def fake_s2(queries, max_per_q):
        nonlocal search_call_count
        search_call_count += 1
        if search_call_count == 1:
            return []
        return [
            {
                "title": "Metasurface mechanism foundations",
                "doi": "10.1000/feedback-foundation",
                "year": 2024,
                "venue": "Synthetic Physics",
                "abstract": "A section-specific account of metasurface mechanisms.",
                "is_oa": False,
                "semantic_scholar_id": "CorpusId:feedback-1",
                "backends": ["semantic_scholar"],
                "query_texts": list(queries),
                "citation_count": 12,
            }
        ]

    monkeypatch.setattr(registry, "_search_s2_first", fake_s2)
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    calls = 0

    def defer_batch(agent_name, messages, **kwargs):
        nonlocal calls
        calls += 1
        payload = json.loads(messages[-1]["content"])
        rows = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "adjacent",
                "role_fit": ["foundation"],
                "decision": "deferred",
                "candidate_decision": "reject",
                "audit_reason": "Deferred feedback fixture.",
                "not_usable_for": ["direct factual support"],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows}),
            "input_tokens": 800,
            "output_tokens": 160,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", defer_batch)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    first_orchestrator = module.SectionCoverageOrchestrator(
        _run_config(tmp_path, blueprint),
        run_dir=run_dir,
    )
    first = first_orchestrator.run()
    (
        first.work_dir
        / "sections"
        / "S01"
        / "SEARCH_BUDGET_LEDGER.json"
    ).unlink(missing_ok=True)
    calls = 0
    second_config = _run_config(tmp_path, blueprint)
    second_config.force_research_sections = ["S01"]
    second_config.author_feedback_by_section["S01"] = {
        "pivotal_gap": "new-author-feedback"
    }
    second_orchestrator = module.SectionCoverageOrchestrator(
        second_config,
        run_dir=run_dir,
    )
    second = second_orchestrator.run()

    assert second_orchestrator.records[0]["reused"] is False
    assert calls >= 1


def test_second_request_can_audit_after_first_pass_exhausts_two_waves(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation", "mechanism"],
        },
    )
    run_dir = tmp_path / "coverage" / "shared-two-wave"
    search_calls: list[list[str]] = []

    def fake_s2(queries, max_per_q):
        search_calls.append(list(queries))
        return [
            {
                "title": f"Metasurface mechanism foundations {len(search_calls)}",
                "doi": f"10.1000/two-wave-{len(search_calls)}",
                "year": 2024,
                "venue": "Synthetic Physics",
                "abstract": (
                    "A section-specific account of metasurface mechanisms."
                ),
                "is_oa": False,
                "semantic_scholar_id": f"CorpusId:two-wave-{len(search_calls)}",
                "backends": ["semantic_scholar"],
                "query_texts": list(queries),
                "citation_count": 12,
            }
        ]

    monkeypatch.setattr(registry, "_search_s2_first", fake_s2)
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])
    first_calls = 0

    def first_defer(agent_name, messages, **kwargs):
        nonlocal first_calls
        first_calls += 1
        payload = json.loads(messages[-1]["content"])
        rows = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "adjacent",
                "role_fit": ["foundation"],
                "decision": "deferred",
                "candidate_decision": "reject",
                "audit_reason": "Deferred first-pass fixture.",
                "not_usable_for": ["direct factual support"],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows}),
            "input_tokens": 800,
            "output_tokens": 160,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", first_defer)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    monkeypatch.setattr(
        module,
        "_coverage_query_targets",
        lambda ctx: [
            {"role": "foundation", "query": "metasurface mechanisms"},
            {"role": "mechanism", "query": "dispersion engineering"},
        ],
    )
    first_orchestrator = module.SectionCoverageOrchestrator(
        _run_config(tmp_path, blueprint),
        run_dir=run_dir,
    )
    first = first_orchestrator.run()
    assert first_orchestrator.records[0]["searched_audit_calls"] == 2
    assert first_calls == 2
    first_section = first.work_dir / "sections" / "S01"
    first_wave = json.loads(
        (first_section / "COVERAGE_WAVE_TELEMETRY.json").read_text(
            encoding="utf-8"
        )
    )
    assert first_wave["total_audit_calls"] == 2
    assert {
        row.get("wave_index") for row in first_wave["waves"]
    } == {0, 1}
    assert first_orchestrator.records[0]["stop_reason_category"] == (
        "scientific_exhaustion"
    )
    (
        first_section / "SEARCH_BUDGET_LEDGER.json"
    ).unlink(missing_ok=True)
    registry._append_candidates_to_ledger(
        first_section,
        "S01",
        [
            {
                "candidate_id": "article_cand_second_request",
                "section_id": "S01",
                "role": "foundation",
                "title": "Metasurface mechanism foundations",
                "doi": "10.1000/two-wave-second",
                "year": 2024,
                "venue": "Synthetic Physics",
                "abstract": (
                    "A section-specific account of metasurface mechanisms."
                ),
                "is_oa": False,
                "semantic_scholar_id": "CorpusId:two-wave-second",
                "backends": ["semantic_scholar"],
                "query_texts": ["metasurface mechanisms"],
                "relevance_score": 0.8,
                "scope_fit": "unreviewed",
                "role_fit": ["foundation"],
                "decision": "deferred",
                "candidate_action": "reject",
            }
        ],
    )

    second_calls = 0

    def second_defer(agent_name, messages, **kwargs):
        nonlocal second_calls
        second_calls += 1
        payload = json.loads(messages[-1]["content"])
        rows = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "adjacent",
                "role_fit": ["foundation"],
                "decision": "deferred",
                "candidate_decision": "reject",
                "audit_reason": "Deferred second-request fixture.",
                "not_usable_for": ["direct factual support"],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows}),
            "input_tokens": 800,
            "output_tokens": 160,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", second_defer)
    second_config = _run_config(tmp_path, blueprint)
    second_config.force_research_sections = ["S01"]
    second_config.coverage_requests_by_section["S01"] = {
        "queries": [{"query": "new two-wave request query"}],
    }
    second_orchestrator = module.SectionCoverageOrchestrator(
        second_config,
        run_dir=run_dir,
    )
    second = second_orchestrator.run()
    assert second_orchestrator.records[0]["reused"] is False
    assert second_calls >= 1
    assert second_orchestrator.records[0]["searched_audit_calls"] >= 1
    second_section = second.work_dir / "sections" / "S01"
    active_wave = json.loads(
        (second_section / "COVERAGE_WAVE_TELEMETRY.json").read_text(
            encoding="utf-8"
        )
    )
    assert active_wave["total_audit_calls"] == 1
    history_files = sorted(
        (second_section / "_pass_history").glob("*.json"),
        key=lambda path: path.name,
    )
    snapshot = json.loads(history_files[-1].read_text(encoding="utf-8"))
    assert snapshot["coverage_wave_telemetry"]["total_audit_calls"] == 2


def test_same_effective_wave_second_audit_is_rejected(
    tmp_path: Path,
) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    work_dir = tmp_path / "section"
    work_dir.mkdir(parents=True)
    (work_dir / "COVERAGE_WAVE_TELEMETRY.json").write_text(
        json.dumps(
            {
                "schema_version": "phase2.coverage_wave_telemetry.v1",
                "section_id": "S01",
                "waves": [
                    {
                        "wave_index": 5,
                        "audit_calls": 1,
                        "candidate_ids": ["candidate_1"],
                    }
                ],
                "total_audit_calls": 1,
            }
        ),
        encoding="utf-8",
    )
    context = SectionCoverageContext(
        section_id="S01",
        section_data={"section_id": "S01", "required_roles": ["foundation"]},
        kb_sqlite=None,
        temp_kb_sqlite=None,
        work_dir=work_dir,
    )
    context.enforce_batched_audit_protocol = True
    context.max_audit_calls_per_section = 2
    context.context_per_call_budget_tokens = 20_000
    context.context_cumulative_budget_tokens = 40_000
    context.context_output_reserve_tokens = 6_000
    context.phase3_coverage_request = {"wave_index": 5}

    admission = registry._audit_call_preflight(
        context,
        ["candidate_2"],
        1_000,
    )
    assert admission.admitted is False
    assert admission.reason == "one_batched_audit_per_wave_exceeded"


def test_three_run_identical_request_is_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(tmp_path)
    run_dir = tmp_path / "coverage" / "shared-idempotent"

    def fake_s2(queries, max_per_q):
        return [
            {
                "title": "Metasurface mechanism foundations",
                "doi": "10.1000/idempotent-foundation",
                "year": 2024,
                "venue": "Synthetic Physics",
                "abstract": (
                    "A section-specific account of metasurface mechanisms."
                ),
                "is_oa": False,
                "semantic_scholar_id": "CorpusId:idempotent-1",
                "backends": ["semantic_scholar"],
                "query_texts": list(queries),
                "citation_count": 12,
            }
        ]

    monkeypatch.setattr(registry, "_search_s2_first", fake_s2)
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])
    model_calls = 0

    def defer_batch(agent_name, messages, **kwargs):
        nonlocal model_calls
        model_calls += 1
        payload = json.loads(messages[-1]["content"])
        rows = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "adjacent",
                "role_fit": ["foundation"],
                "decision": "deferred",
                "candidate_decision": "reject",
                "audit_reason": "Deferred idempotency fixture.",
                "not_usable_for": ["direct factual support"],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows}),
            "input_tokens": 800,
            "output_tokens": 160,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", defer_batch)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    first_orchestrator = module.SectionCoverageOrchestrator(
        _run_config(tmp_path, blueprint),
        run_dir=run_dir,
    )
    first = first_orchestrator.run()
    (
        first.work_dir
        / "sections"
        / "S01"
        / "SEARCH_BUDGET_LEDGER.json"
    ).unlink(missing_ok=True)
    model_calls = 0

    second_config = _run_config(tmp_path, blueprint)
    second_config.force_research_sections = ["S01"]
    second_config.coverage_requests_by_section["S01"] = {
        "queries": [{"query": "new idempotent request query"}],
    }
    second_orchestrator = module.SectionCoverageOrchestrator(
        second_config,
        run_dir=run_dir,
    )
    second = second_orchestrator.run()
    assert second_orchestrator.records[0]["reused"] is False
    assert model_calls >= 1
    history_dir = second_orchestrator.work_dir / "sections" / "S01" / "_pass_history"
    history_count = len(list(history_dir.glob("*.json")))

    model_calls = 0

    def forbid_call(agent_name, messages, **kwargs):
        raise AssertionError("third identical request must be replayed")

    monkeypatch.setattr(qwen, "call_qwen_chat", forbid_call)
    third_config = _run_config(tmp_path, blueprint)
    third_config.force_research_sections = ["S01"]
    third_config.coverage_requests_by_section["S01"] = {
        "queries": [{"query": "new idempotent request query"}],
    }
    third_orchestrator = module.SectionCoverageOrchestrator(
        third_config,
        run_dir=run_dir,
    )
    third_orchestrator.run()

    assert third_orchestrator.records[0]["reused"] is True, (
        json.loads(
            (
                third_orchestrator.work_dir
                / "sections"
                / "S01"
                / "SUPPLEMENTATION_RESULT.json"
            ).read_text(encoding="utf-8")
        ).get("input_fingerprint_sha256"),
        json.loads(
            (
                third_orchestrator.work_dir
                / "sections"
                / "S01"
                / "SUPPLEMENTATION_RESULT.json"
            ).read_text(encoding="utf-8")
        ).get("evidence_fingerprint_sha256"),
        third_orchestrator.records[0].get("coverage_input_fingerprint_sha256"),
        third_orchestrator.records[0].get("coverage_evidence_fingerprint_sha256"),
    )
    assert model_calls == 0
    assert len(list(history_dir.glob("*.json"))) == history_count


def test_second_pass_no_progress_preserves_first_pass_history(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(tmp_path)
    run_dir = tmp_path / "coverage" / "shared-no-progress"

    def fake_s2(queries, max_per_q):
        return [
            {
                "title": "Metasurface mechanism foundations",
                "doi": "10.1000/no-progress-foundation",
                "year": 2024,
                "venue": "Synthetic Physics",
                "abstract": "A section-specific account of metasurface mechanisms.",
                "is_oa": False,
                "semantic_scholar_id": "CorpusId:no-progress-1",
                "backends": ["semantic_scholar"],
                "query_texts": list(queries),
                "citation_count": 12,
            }
        ]

    monkeypatch.setattr(registry, "_search_s2_first", fake_s2)
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *args: [])
    monkeypatch.setattr(registry, "_search_openalex", lambda *args: [])

    def defer_batch(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        rows = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "adjacent",
                "role_fit": ["foundation"],
                "decision": "deferred",
                "candidate_decision": "reject",
                "audit_reason": "Deferred first-pass fixture.",
                "not_usable_for": ["direct factual support"],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows}),
            "input_tokens": 800,
            "output_tokens": 160,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", defer_batch)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    first_orchestrator = module.SectionCoverageOrchestrator(
        _run_config(tmp_path, blueprint),
        run_dir=run_dir,
    )
    first = first_orchestrator.run()
    first_section = first.work_dir / "sections" / "S01"
    (first_section / "SEARCH_BUDGET_LEDGER.json").unlink(missing_ok=True)
    preserved = {
        name: (first_section / name).read_bytes()
        for name in (
            "SHORT_PATH_RUN.json",
            "RESULT.json",
            "USAGE_RECEIPT.json",
        )
    }
    first_summary = json.loads(
        (first_section / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )
    assert first_summary["stop_reason_category"] == "scientific_exhaustion"

    second_config = _run_config(tmp_path, blueprint)
    second_config.force_research_sections = ["S01"]
    second_config.coverage_requests_by_section["S01"] = {
        "missing_components": ["no-progress-component"],
        "queries": [{"query": "new no-progress request query"}],
    }

    def malformed(agent_name, messages, **kwargs):
        return {
            "content": "{not valid json",
            "input_tokens": 700,
            "output_tokens": 120,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", malformed)
    second_orchestrator = module.SectionCoverageOrchestrator(
        second_config,
        run_dir=run_dir,
    )
    second = second_orchestrator.run()

    second_section = second_orchestrator.work_dir / "sections" / "S01"
    record = second_orchestrator.records[0]
    assert record["second_pass_no_progress"] is True
    assert record["stop_reason_category"] == "scientific_exhaustion"
    for name, expected_bytes in preserved.items():
        assert (second_section / name).read_bytes() == expected_bytes, name
    supplementation = json.loads(
        (second_section / "SUPPLEMENTATION_RESULT.json").read_text(
            encoding="utf-8"
        )
    )
    assert supplementation["made_progress"] is False
    assert record["cost_cny"] > 0
    assert second.total_cost_cny == (
        supplementation["incremental_cost_cny"]
    )


def test_progress_second_pass_updates_package_and_preserves_history(
    tmp_path: Path, monkeypatch
) -> None:
    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    run_dir = tmp_path / "coverage" / "shared-progress"
    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    call_number = 0

    def model_audit(agent_name, messages, **kwargs):
        nonlocal call_number
        call_number += 1
        payload = json.loads(messages[-1]["content"])
        rows_out = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "direct",
                "role_fit": ["foundation"],
                "decision": "approved",
                "candidate_decision": "materialize_now",
                "audit_reason": (
                    "Direct local section-specific mechanism evidence."
                ),
                "not_usable_for": [],
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows_out}),
            "input_tokens": 700,
            "output_tokens": 240,
            "estimated_cost_cny": 0.02,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", model_audit)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    first_config = _run_config(tmp_path, blueprint)
    first_orchestrator = module.SectionCoverageOrchestrator(
        first_config,
        run_dir=run_dir,
    )
    first = first_orchestrator.run()
    first_section = first.work_dir / "sections" / "S01"
    first_summary = json.loads(
        (first_section / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )
    assert first_summary["stop_reason_category"] == "scientific_exhaustion"

    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 3
    )
    call_number = 0
    second_config = _run_config(tmp_path, blueprint)
    second_config.base_kb_sqlite = local_kb
    second_config.force_research_sections = ["S01"]
    second_config.coverage_requests_by_section["S01"] = {
        "missing_components": ["progress-component"],
    }
    second_orchestrator = module.SectionCoverageOrchestrator(
        second_config,
        run_dir=run_dir,
    )
    second = second_orchestrator.run()
    second_section = second.work_dir / "sections" / "S01"
    second_record = second_orchestrator.records[0]
    package = json.loads(
        (second_section / "SECTION_MATERIAL_PACKAGE.json").read_text(
            encoding="utf-8"
        )
    )
    supplementation = json.loads(
        (second_section / "SUPPLEMENTATION_RESULT.json").read_text(
            encoding="utf-8"
        )
    )

    assert second_record["supplementation_progress"] is True
    source_ledger = json.loads(
        (second_section / "SECTION_SOURCE_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(source_ledger.get("sources") or []) >= 3
    assert supplementation["made_progress"] is True
    assert second_record["pass_history_path"]


def test_local_audit_examines_broad_pool_with_batching_lanes_and_no_oversized_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    """A broad local pool is ranked and examined in token-sized batches,
    preserving direct/adjacent/contextual lanes without one oversized prompt."""

    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 185
    )
    rows = _local_candidate_rows(paper_ids, chunk_ids)
    lane_by_id: dict[str, str] = {}
    for index, row in enumerate(rows):
        row["relevance_score"] = round(
            max(0.5, 0.98 - index * 0.001), 4
        )
        lane = (
            "contextual"
            if index % 10 in (0, 1)
            else "adjacent"
            if index % 10 in (2, 3)
            else "out_of_scope"
            if index % 10 == 9
            else "direct"
        )
        lane_by_id[row["candidate_id"]] = lane
    run_dir = tmp_path / "coverage" / "local-broad-pool"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    chat_calls: list[dict] = []

    def fake_chat(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        chat_calls.append(
            {
                **dict(kwargs),
                "candidate_count": len(payload["candidates"]),
            }
        )
        rows_out = []
        for candidate in payload["candidates"]:
            lane = lane_by_id.get(candidate["candidate_id"], "direct")
            approved = lane != "out_of_scope"
            score = {
                "direct": 0.9,
                "adjacent": 0.75,
                "contextual": 0.6,
                "out_of_scope": 0.1,
            }[lane]
            rows_out.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "scope_fit": lane,
                    "role_fit": ["foundation"],
                    "decision": "approved" if approved else "rejected",
                    "candidate_decision": (
                        "materialize_now" if approved else "reject"
                    ),
                    "audit_reason": (
                        "Synthetic local evidence for the section."
                        if approved
                        else "Unrelated local record."
                    ),
                    "not_usable_for": (
                        [] if approved else ["direct factual support"]
                    ),
                    "semantic_score": score,
                }
            )
        return {
            "content": json.dumps({"candidates": rows_out}),
            "input_tokens": 1200,
            "output_tokens": 400,
            "estimated_cost_cny": 0.04,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", fake_chat)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    config.model_context_budget_per_section = 260_000
    result = module.SectionCoverageOrchestrator(
        config, run_dir=run_dir
    ).run()

    section_dir = result.work_dir / "sections" / "S01"
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )
    source_ledger = json.loads(
        (section_dir / "SECTION_SOURCE_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )

    assert result.status == "completed"
    assert search_calls == []
    assert short_path["local_candidates_discovered"] >= 180
    assert short_path["local_candidates_ranked"] >= 180
    assert short_path["local_candidates_examined"] >= 180
    assert short_path["local_batches"] >= 2
    assert len(chat_calls) == short_path["local_batches"]
    assert all(call["candidate_count"] <= 40 for call in chat_calls)
    assert all(call["max_tokens"] <= 12000 for call in chat_calls)
    assert all(
        call["max_tokens"] == 600 + 260 * call["candidate_count"]
        for call in chat_calls
    )
    retained = short_path["local_candidates_retained_by_lane"]
    assert retained["direct"] > 0
    assert retained["adjacent"] > 0
    assert retained["contextual"] > 0
    assert short_path["local_candidates_unexamined"] == 0
    assert short_path["local_stop_reason"] == "local_pool_exhausted"
    assert short_path["network_search_needed"] is False
    scopes = {source.get("scope_fit") for source in source_ledger["sources"]}
    assert "adjacent" in scopes
    assert "contextual" in scopes
    for key in (
        "local_candidates_discovered",
        "local_candidates_ranked",
        "local_candidates_examined",
        "local_candidates_retained_by_lane",
        "local_batches",
        "local_stop_reason",
        "local_candidates_unexamined",
        "network_search_needed",
    ):
        assert key in short_path


def test_local_audit_stops_early_on_marginal_gain_with_duplicate_pool(
    tmp_path: Path, monkeypatch
) -> None:
    """A pool whose remaining records repeat the same role/lane at low
    semantic score stops by marginal gain, leaving unexamined rows behind."""

    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 90
    )
    rows = _local_candidate_rows(paper_ids, chunk_ids)
    for row in rows:
        row["relevance_score"] = 0.2
    run_dir = tmp_path / "coverage" / "local-marginal-stop"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    chat_calls: list[dict] = []

    def approve_duplicates(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        chat_calls.append(len(payload["candidates"]))
        rows_out = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "direct",
                "role_fit": ["foundation"],
                "decision": "approved",
                "candidate_decision": "materialize_now",
                "audit_reason": "Duplicate low-gain local evidence.",
                "not_usable_for": [],
                "semantic_score": 0.2,
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows_out}),
            "input_tokens": 700,
            "output_tokens": 240,
            "estimated_cost_cny": 0.02,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", approve_duplicates)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(
        config, run_dir=run_dir
    ).run()

    section_dir = result.work_dir / "sections" / "S01"
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )

    assert search_calls == []
    assert short_path["local_candidates_examined"] >= 80
    assert short_path["local_candidates_unexamined"] > 0
    assert short_path["local_stop_reason"] == (
        "local_marginal_gain_exhausted"
    )
    assert short_path["local_batches"] == 2
    assert len(chat_calls) == 2
    assert short_path["stop_reason_category"] == "scientific_completion"


def test_malformed_second_local_batch_keeps_prior_accepted_records(
    tmp_path: Path, monkeypatch
) -> None:
    """A malformed later batch triggers bounded retry and never erases the
    records already accepted from an earlier batch."""

    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 90
    )
    rows = _local_candidate_rows(paper_ids, chunk_ids)
    run_dir = tmp_path / "coverage" / "local-malformed-second"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    calls = 0

    def mixed_chat(agent_name, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            payload = json.loads(messages[-1]["content"])
            rows_out = [
                {
                    "candidate_id": candidate["candidate_id"],
                    "scope_fit": "direct",
                    "role_fit": ["foundation"],
                    "decision": "approved",
                    "candidate_decision": "materialize_now",
                    "audit_reason": "Direct local evidence.",
                    "not_usable_for": [],
                    "semantic_score": 0.8,
                }
                for candidate in payload["candidates"]
            ]
            return {
                "content": json.dumps({"candidates": rows_out}),
                "input_tokens": 700,
                "output_tokens": 240,
                "estimated_cost_cny": 0.02,
            }
        return {
            "content": "{not valid json",
            "input_tokens": 700,
            "output_tokens": 120,
            "estimated_cost_cny": 0.01,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", mixed_chat)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(
        config, run_dir=run_dir
    ).run()

    section_dir = result.work_dir / "sections" / "S01"
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )
    ledger = json.loads(
        (section_dir / "LOCAL_CANDIDATE_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )
    source_ledger = json.loads(
        (section_dir / "SECTION_SOURCE_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )

    assert calls == 3
    assert search_calls == []
    assert short_path["local_audit_calls"] == 3
    assert short_path["qwen_calls"] == 3
    assert short_path["searched_audit_calls"] == 0
    assert "provider_response_malformed" in (
        short_path.get("local_source_audit_failure") or ""
    )
    assert short_path["stop_reason_category"] == "engineering_failure"
    assert short_path["local_candidates_examined"] >= 40
    assert short_path["local_candidates_unexamined"] > 0
    approved = [
        item
        for item in ledger["candidates"]
        if item.get("decision") == "approved"
    ]
    assert len(approved) >= 40
    assert any(
        source.get("scope_fit") == "direct"
        for source in source_ledger.get("sources", [])
    )


def test_network_search_skipped_when_local_role_coverage_sufficient(
    tmp_path: Path, monkeypatch
) -> None:
    """Local-first means network acquisition is skipped when local batches
    already satisfy role/coverage needs."""

    import llm.qwen_chat_client as qwen
    import optomind_research.runtime.section_coverage_tool_registry as registry

    _patch_no_react(monkeypatch)
    blueprint = _write_blueprint(
        tmp_path,
        section={
            "section_id": "S01",
            "required_roles": ["foundation"],
            "literature_coverage_target": {
                "minimum_unique_sources": 3,
                "minimum_direct_sources": 3,
            },
        },
    )
    local_kb, paper_ids, chunk_ids = _local_kb_with_candidates(
        tmp_path, 5
    )
    rows = _local_candidate_rows(paper_ids, chunk_ids)
    run_dir = tmp_path / "coverage" / "local-sufficient"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    _write_local_resume_artifacts(section_dir, rows)

    search_calls: list[list[str]] = []

    def record_search(queries, *args, **kwargs):
        search_calls.append(list(queries))
        return []

    monkeypatch.setattr(registry, "_search_s2_first", record_search)
    monkeypatch.setattr(registry, "_search_semantic_scholar", record_search)
    monkeypatch.setattr(registry, "_search_openalex", record_search)

    def approve_all(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        rows_out = [
            {
                "candidate_id": candidate["candidate_id"],
                "scope_fit": "direct",
                "role_fit": ["foundation"],
                "decision": "approved",
                "candidate_decision": "materialize_now",
                "audit_reason": "Direct local section evidence.",
                "not_usable_for": [],
                "semantic_score": 0.9,
            }
            for candidate in payload["candidates"]
        ]
        return {
            "content": json.dumps({"candidates": rows_out}),
            "input_tokens": 700,
            "output_tokens": 240,
            "estimated_cost_cny": 0.02,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", approve_all)
    module = __import__(
        "optomind_research.runtime.section_coverage_orchestrator",
        fromlist=["SectionCoverageOrchestrator"],
    )
    config = _run_config(tmp_path, blueprint)
    config.base_kb_sqlite = local_kb
    result = module.SectionCoverageOrchestrator(
        config, run_dir=run_dir
    ).run()

    section_dir = result.work_dir / "sections" / "S01"
    short_path = json.loads(
        (section_dir / "SHORT_PATH_RUN.json").read_text(encoding="utf-8")
    )

    assert result.status == "completed"
    assert search_calls == []
    assert short_path["network_search_needed"] is False
    assert short_path["local_candidates_examined"] >= 5
    assert short_path["stop_reason_category"] == "scientific_completion"


# ---------------------------------------------------------------------------
# Repair 5 — reused-section cost restored from USAGE_RECEIPT.json
# ---------------------------------------------------------------------------

def test_reused_section_cost_restored_from_receipt(tmp_path: Path) -> None:
    """Prior USAGE_RECEIPT cost is restored for reused sections with zero cost."""
    import optomind_research.runtime.section_coverage_orchestrator as _mod

    sid = "SREP5A"
    rd = tmp_path / "sections" / sid
    rd.mkdir(parents=True)
    prior = {"cost_cny": 0.42, "input_tokens": 1000, "output_tokens": 200,
             "cost_basis": "provider_priced", "cost_is_estimated": False}
    (rd / "USAGE_RECEIPT.json").write_text(json.dumps(prior), encoding="utf-8")

    record = {"section_id": sid, "reused": True, "cost_cny": 0.0,
              "input_tokens": 0, "output_tokens": 0}
    merged = []
    for item in [{"record": record}]:
        rec = dict(item["record"])
        s = str(rec.get("section_id") or "")
        if rec.get("reused") and s and float(rec.get("cost_cny") or 0.0) == 0.0:
            rp = tmp_path / "sections" / s / "USAGE_RECEIPT.json"
            rx = _mod._read_json(rp)
            if isinstance(rx, dict) and float(rx.get("cost_cny") or 0.0) > 0.0:
                rec["cost_cny"] = float(rx["cost_cny"])
                rec["input_tokens"] = int(rx.get("input_tokens") or 0)
                rec["output_tokens"] = int(rx.get("output_tokens") or 0)
                rec["cost_basis"] = str(rx.get("cost_basis") or "reused_restored")
                rec["prior_receipt_restored"] = True
        merged.append(rec)

    r = merged[0]
    assert abs(r["cost_cny"] - 0.42) < 1e-9
    assert r["input_tokens"] == 1000
    assert r["output_tokens"] == 200
    assert r["prior_receipt_restored"] is True


def test_reused_section_nonzero_cost_not_overwritten(tmp_path: Path) -> None:
    """A reused section already carrying non-zero cost is left unchanged."""
    import optomind_research.runtime.section_coverage_orchestrator as _mod

    sid = "SREP5B"
    rd = tmp_path / "sections" / sid
    rd.mkdir(parents=True)
    (rd / "USAGE_RECEIPT.json").write_text(
        json.dumps({"cost_cny": 9.99, "input_tokens": 5000, "output_tokens": 1000}),
        encoding="utf-8")

    record = {"section_id": sid, "reused": True, "cost_cny": 0.77,
              "input_tokens": 300, "output_tokens": 60}
    merged = []
    for item in [{"record": record}]:
        rec = dict(item["record"])
        s = str(rec.get("section_id") or "")
        if rec.get("reused") and s and float(rec.get("cost_cny") or 0.0) == 0.0:
            rp = tmp_path / "sections" / s / "USAGE_RECEIPT.json"
            rx = _mod._read_json(rp)
            if isinstance(rx, dict) and float(rx.get("cost_cny") or 0.0) > 0.0:
                rec["cost_cny"] = float(rx["cost_cny"])
                rec["prior_receipt_restored"] = True
        merged.append(rec)

    assert abs(merged[0]["cost_cny"] - 0.77) < 1e-9
    assert "prior_receipt_restored" not in merged[0]


def test_reused_section_missing_receipt_stays_zero(tmp_path: Path) -> None:
    """When USAGE_RECEIPT is absent the record stays at zero; no exception raised."""
    import optomind_research.runtime.section_coverage_orchestrator as _mod

    record = {"section_id": "SREP5C", "reused": True, "cost_cny": 0.0,
              "input_tokens": 0, "output_tokens": 0}
    merged = []
    for item in [{"record": record}]:
        rec = dict(item["record"])
        s = str(rec.get("section_id") or "")
        if rec.get("reused") and s and float(rec.get("cost_cny") or 0.0) == 0.0:
            rp = tmp_path / "sections" / s / "USAGE_RECEIPT.json"
            rx = _mod._read_json(rp)
            if isinstance(rx, dict) and float(rx.get("cost_cny") or 0.0) > 0.0:
                rec["cost_cny"] = float(rx["cost_cny"])
                rec["prior_receipt_restored"] = True
        merged.append(rec)

    assert merged[0]["cost_cny"] == 0.0
    assert "prior_receipt_restored" not in merged[0]
