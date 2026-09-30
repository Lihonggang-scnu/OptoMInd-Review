from optomind_research.runtime.upgrade3.planning_material_search import (
    PaperRecord,
    PlanningMaterialIndex,
)
from optomind_research.runtime.upgrade3.planning_material_triage import (
    LocalGap,
    _triage_payload,
    read_local_capture,
    triage_gap,
)
from optomind_research.runtime.upgrade3 import planning_material_triage as triage_module
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig,
    _merge_supplement_pool_updates,
    make_retrieval_loop_runner,
)


FORMAL_ID = "CorpusId:252309032"
PREPRINT_ID = "CorpusId:242322079"
FORMAL_DOI = "10.1038/s41467-022-33116-z"
PREPRINT_DOI = "10.21203/rs.3.rs-620390/v1"
RCT_ID = "CorpusId:272644866"
RCT_DOI = "10.1002/cam4.70143"
ALIAS_ID = "legacy-formal-alias"


def _index(path):
    with PlanningMaterialIndex(path) as index:
        index.upsert_paper(PaperRecord(
            paper_id=FORMAL_ID, source_handle="P0591", title="UBA6 formal",
            year="2022", doi=FORMAL_DOI,
        ))
        index.upsert_paper(PaperRecord(
            paper_id=PREPRINT_ID, source_handle="P0591", title="UBA6 preprint",
            year="2021", doi=PREPRINT_DOI,
        ))
        index.upsert_paper(PaperRecord(
            paper_id=RCT_ID, source_handle="P0591", title="Inosine Phase II RCT",
            year="2024", doi=RCT_DOI,
        ))
        index.upsert_paper(PaperRecord(
            paper_id=ALIAS_ID, source_handle="P0413", title="UBA6 formal alias",
            year="2022", doi=FORMAL_DOI,
        ))
        for paper_id, text in (
            (FORMAL_ID, "UBA6 study reports the formal mechanism in tumour cells."),
            (PREPRINT_ID, "UBA6 preprint reports an earlier distinct experiment."),
            (RCT_ID, "UBA6 inosine Phase II randomized trial reports a clinical outcome."),
            (ALIAS_ID, "UBA6 study reports the formal mechanism through a legacy identity."),
        ):
            index.add_segments(paper_id, [{
                "segment_kind": "document_block", "section_path": ["Results"],
                "text": text, "ordinal": 0,
            }])
        index.record_term_document_frequency()
        index._conn.commit()


def test_current_identity_rebind_keeps_pool_outside_hit_and_judge_identity(tmp_path):
    index_path = tmp_path / "materials.sqlite"
    _index(index_path)
    captured = []

    def judge(gap, bundle):
        captured.append(_triage_payload(gap, bundle))
        return {"decision": "direct_use", "usable_content": "Both records are distinct.", "reason": "evidence"}

    gap = LocalGap(gap_id="G1", question="What does the UBA6 study report?", concepts=("UBA6",))
    active = {
        "P0582": {"paper_id": FORMAL_ID, "source_handle": "P0582", "doi": FORMAL_DOI},
    }
    with PlanningMaterialIndex(index_path, readonly=True) as index:
        triage_gap(index, gap, judge=judge, source_identity_map=active, max_passages=5)
        triage_gap(index, gap, judge=judge, source_identity_map=None, max_passages=5)

    revised_rows = {
        (row["paper_id"], row["doi"]): row["source_handle"]
        for row in captured[0]["material_found"]
    }
    assert revised_rows[(FORMAL_ID, FORMAL_DOI)] == "P0582"
    assert revised_rows[(PREPRINT_ID, PREPRINT_DOI)] == ""
    assert {row["paper_id"] for row in captured[0]["paper_context"]} == {FORMAL_ID, PREPRINT_ID, RCT_ID, ALIAS_ID}
    assert {row["source_handle"] for row in captured[1]["material_found"]} == {"P0413", "P0591"}

    doi_only = {"P0582": {"paper_id": "current-alias", "source_handle": "P0582", "doi": FORMAL_DOI}}
    with PlanningMaterialIndex(index_path, readonly=True) as index:
        triage_gap(index, gap, judge=judge, source_identity_map=doi_only, max_passages=5)
    doi_rows = {
        (row["paper_id"], row["doi"]): row["source_handle"]
        for row in captured[2]["material_found"]
    }
    assert doi_rows[(FORMAL_ID, FORMAL_DOI)] == "P0582"
    assert doi_rows[(PREPRINT_ID, PREPRINT_DOI)] == ""


def test_pool_inclusion_assigns_new_handle_without_overwriting_identity():
    pool = [{"_paper_id": FORMAL_ID, "_source_handle": "P0582"}]
    _merge_supplement_pool_updates(pool, {"supplement_results": [{"candidate_rows": [{
        "paper_id": PREPRINT_ID,
        "title": "UBA6 preprint",
        "doi": PREPRINT_DOI,
        "source_handle": "P0591",
    }]}]})
    added = next(row for row in pool if row["_paper_id"] == PREPRINT_ID)
    assert added["doi"] == PREPRINT_DOI
    assert added["_source_handle"] != "P0591"
    assert pool[0]["_source_handle"] == "P0582"


def test_factory_local_judge_rebinds_rct_and_promotes_pool_outside_material(tmp_path, monkeypatch):
    index_path = tmp_path / "materials.sqlite"
    _index(index_path)
    captured = []

    class OfflineJudge:
        def __init__(self, **_kwargs):
            pass

        def __call__(self, gap, bundle):
            captured.append(_triage_payload(gap, bundle))
            return {"decision": "direct_use", "usable_content": "Identity-separated local evidence.", "reason": "offline"}

    monkeypatch.setattr(triage_module, "QwenLocalTriageJudge", OfflineJudge)
    config = ProgressivePlannerConfig(
        topic_id="identity-fixture",
        plan_path=tmp_path / "PLAN.json",
        pool_path=tmp_path / "POOL.jsonl",
        output_dir=tmp_path / "runner-out",
        planning_revision_enabled=True,
    )
    runner = make_retrieval_loop_runner(
        config,
        key_file=tmp_path / "unused-key",
        budget_ledger_path=tmp_path / "budget.sqlite",
        budget_limit_cny=100,
        local_index_path=index_path,
        allow_external=False,
    )
    formal = {
        "_paper_id": FORMAL_ID, "_source_handle": "P0582",
        "planning_view": {"paper_identity": {"canonical_paper_id": FORMAL_ID, "title": "UBA6 formal", "doi": FORMAL_DOI, "year": "2022"}},
    }
    rct = {
        "_paper_id": RCT_ID, "_source_handle": "P0591",
        "planning_view": {"paper_identity": {"canonical_paper_id": RCT_ID, "title": "Inosine Phase II RCT", "doi": RCT_DOI, "year": "2024"}},
    }
    result = runner(
        phase="chapters",
        supplement_requests=[{
            "gap_id": "G_RCT", "gap_question": "What does the UBA6 study report?",
            "chapter_ids": ["CH01"], "targeted_queries": [{"query_text": "UBA6", "query_type": "keyword"}],
        }],
        directed_requests=[], pool_rows=[formal, rct],
        plan={"question": "UBA6 evidence"},
        source_handle_map={"P0582": FORMAL_ID, "P0591": RCT_ID},
        resume=False, output_dir=tmp_path / "runner-out" / "chapters",
    )
    assert len(captured) == 1
    judge_handles = {
        (row["paper_id"], row["doi"]): row["source_handle"]
        for row in captured[0]["material_found"]
    }
    assert judge_handles[(FORMAL_ID, FORMAL_DOI)] == "P0582"
    assert judge_handles[(RCT_ID, RCT_DOI)] == "P0591"
    assert judge_handles[(PREPRINT_ID, PREPRINT_DOI)] == ""

    pool_by_id = {row["_paper_id"]: row for row in result["pool_rows"]}
    assert ALIAS_ID not in pool_by_id
    assert pool_by_id[PREPRINT_ID]["_source_handle"] == "P0003"
    assert pool_by_id[PREPRINT_ID]["local_passages"][0]["text"].startswith("UBA6 preprint")
    sources = result["tool_materials_by_chapter"]["CH01"][0]["sources"]
    source_handles = {(row["paper_id"], row["doi"]): row["source_handle"] for row in sources}
    assert source_handles[(FORMAL_ID, FORMAL_DOI)] == "P0582"
    assert source_handles[(ALIAS_ID, FORMAL_DOI)] == "P0582"
    assert source_handles[(RCT_ID, RCT_DOI)] == "P0591"
    assert source_handles[(PREPRINT_ID, PREPRINT_DOI)] == "P0003"

    # A process restart must recover the local admission without paying the
    # judge again, just as it recovers externally acquired candidates.
    phase_root = tmp_path / "runner-out" / "chapters"
    (phase_root / "retrieval_loop.jsonl").write_bytes(
        (phase_root / "retrieval_loop.fresh.jsonl").read_bytes()
    )
    resumed = runner(
        phase="chapters",
        supplement_requests=[{
            "gap_id": "G_RCT", "gap_question": "What does the UBA6 study report?",
            "chapter_ids": ["CH01"], "targeted_queries": [{"query_text": "UBA6", "query_type": "keyword"}],
        }],
        directed_requests=[], pool_rows=[formal, rct],
        plan={"question": "UBA6 evidence"},
        source_handle_map={"P0582": FORMAL_ID, "P0591": RCT_ID},
        resume=True, output_dir=phase_root,
    )
    assert len(captured) == 1
    resumed_pool = {row["_paper_id"]: row for row in resumed["pool_rows"]}
    assert resumed_pool[PREPRINT_ID]["_source_handle"] == "P0003"
    assert resumed["local_pool_updates"]


def test_focused_capture_rebinds_passages_carried_from_old_judgment(tmp_path):
    index_path = tmp_path / "materials.sqlite"
    _index(index_path)
    calls = []

    def judge(gap, bundle):
        calls.append(_triage_payload(gap, bundle))
        if len(calls) == 1:
            return {"decision": "local_deep_read", "usable_content": "", "read_focus": "read the result", "reason": "focused"}
        return {"decision": "direct_use", "usable_content": "focused evidence", "reason": "focused"}

    gap = LocalGap(gap_id="G_CAPTURE", question="What does the UBA6 study report?", concepts=("UBA6",))
    active = {
        "P0582": {"paper_id": FORMAL_ID, "source_handle": "P0582", "doi": FORMAL_DOI},
        "P0591": {"paper_id": RCT_ID, "source_handle": "P0591", "doi": RCT_DOI},
    }
    with PlanningMaterialIndex(index_path, readonly=True) as index:
        judgment = triage_gap(index, gap, judge=judge, source_identity_map=active, max_passages=5)
        judgment.passages[0].source_handle = "P0591"
        refreshed = read_local_capture(index, judgment, judge=judge, source_identity_map=active)
    handles = {
        passage.paper_id: passage.source_handle
        for passage in refreshed.passages
    }
    assert handles[FORMAL_ID] == "P0582"
    assert handles[RCT_ID] == "P0591"
    assert handles[PREPRINT_ID] == ""
