from __future__ import annotations

import json
import pathlib
import tempfile
from pathlib import Path
import sys

ROOT = Path(r"F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree")
sys.path.insert(0, str(ROOT))

from optomind_research.runtime.upgrade3.planning_retrieval_loop import (
    InformationNeed,
    LoopConfig,
    run_retrieval_loop,
)
from optomind_research.runtime.upgrade3.planning_supplement import (
    OutputDirectoryError,
    run_planning_supplement,
)
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig,
    make_retrieval_loop_runner,
)


def first_round_no_query(root: Path) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    calls = {"external": 0, "refine": 0}

    need = InformationNeed(
        need_id="N_NO_QUERY",
        question="Find a direct study for the missing evidence.",
        owners=("CH1",),
        success_criteria=("Direct evidence",),
        kind="supplement",
    )

    def local_triage(**kwargs):
        return {
            "decision": "external_research",
            "usable_content": "",
            "still_missing": need.question,
            "external_ask": need.question,
        }

    def external_closure(**kwargs):
        calls["external"] += 1
        return {"status": "fulfilled", "usable_content": "fake material"}

    def refine_queries(*args, **kwargs):
        calls["refine"] += 1
        return {"targeted_queries": [{"query_text": "direct evidence", "query_type": "keyword"}]}

    result = run_retrieval_loop(
        [need],
        LoopConfig(index_path=root / "index.sqlite", journal_path=root / "loop.jsonl", max_rounds=1),
        local_triage=local_triage,
        external_closure=external_closure,
        refine_queries=refine_queries,
        resume=False,
    )
    state = result["needs"][0]
    return {
        "status": state.get("status"),
        "attempt_status": state.get("attempts", [{}])[0].get("status"),
        "queries": state.get("attempts", [{}])[0].get("queries"),
        "external_calls": calls["external"],
        "refine_calls": calls["refine"],
    }


def cross_stage_reuse(root: Path) -> dict[str, object]:
    config = ProgressivePlannerConfig(
        topic_id="continuity-demo",
        pool_path=root / "pool.json",
        plan_path=root / "plan.json",
        output_dir=root / "runner",
        planning_revision_enabled=True,
        reader_workers=1,
    )
    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.pool_path.write_text("", encoding="utf-8")
    valid_plan = {
        "schema_version": "research_harness.query_plan.v2",
        "question_en": "demo",
        "research_object": "demo",
        "ambiguity": {"is_ambiguous": False, "default_reading": "", "needs_user_input": []},
        "facets": [{"id": "F1", "ask": "demo", "keyword_queries": ["demo"], "question_queries": ["What is demo?"], "filters": {"publication_type": [], "fields_of_study": [], "text_availability": []}, "must_exclude": []}],
        "seeds": [],
        "criteria": {"must_include_topic": [], "must_exclude_domain": [], "synonyms": {}},
        "additional_constraints": [],
    }
    config.plan_path.write_text(json.dumps(valid_plan), encoding="utf-8")
    calls = {"supplement": 0}

    def supplement_runner(gaps, **context):
        calls["supplement"] += 1
        return {
            "status": "completed",
            "results": [
                {
                    "gap_id": gaps[0].get("gap_id"),
                    "status": "completed",
                    "fulfillment_judgment": {"status": "fulfilled", "useful_material": ["prior material"]},
                    "source_units": [],
                }
            ],
        }

    gap = {
        "gap_id": "G1",
        "gap_question": "Need direct evidence.",
        "targeted_queries": [{"query_text": "direct evidence", "query_type": "keyword", "facet_id": "F1"}],
        "chapter_ids": ["CH1"],
    }
    first_runner = make_retrieval_loop_runner(
        config,
        allow_external=True,
        supplement_runner=supplement_runner,
    )
    first = first_runner(
        phase="level2_tools",
        supplement_requests=[gap],
        pool_rows=[],
        plan={"research_question": "demo"},
        output_dir=config.output_dir / "level2_tools",
        resume=False,
    )
    second_runner = make_retrieval_loop_runner(
        config,
        allow_external=True,
        supplement_runner=supplement_runner,
    )
    second = second_runner(
        phase="chapters_tools",
        supplement_requests=[gap],
        pool_rows=[],
        plan={"research_question": "demo"},
        prior_tool_results=first,
        output_dir=config.output_dir / "chapters_tools",
        resume=False,
    )
    return {
        "supplement_calls": calls["supplement"],
        "first_need": (first.get("retrieval_loop", {}).get("needs") or [{}])[0].get("status"),
        "second_need": (second.get("retrieval_loop", {}).get("needs") or [{}])[0].get("status"),
        "second_external_results": len(second.get("supplement_results") or []),
        "first_output": str(config.output_dir / "level2_tools"),
        "second_output": str(config.output_dir / "chapters_tools"),
    }


def supplement_directory_retry(root: Path) -> dict[str, object]:
    work = root / "supplement_direct"
    work.mkdir(parents=True, exist_ok=True)
    valid_plan = {
        "schema_version": "research_harness.query_plan.v2",
        "question_en": "demo",
        "research_object": "demo",
        "ambiguity": {"is_ambiguous": False, "default_reading": "", "needs_user_input": []},
        "facets": [{"id": "F1", "ask": "demo", "keyword_queries": ["demo"], "question_queries": ["What is demo?"], "filters": {"publication_type": [], "fields_of_study": [], "text_availability": []}, "must_exclude": []}],
        "seeds": [],
        "criteria": {"must_include_topic": [], "must_exclude_domain": [], "synonyms": {}},
        "additional_constraints": [],
    }
    pool = work / "POOL.jsonl"
    pool.write_text("", encoding="utf-8")
    plan = work / "PLAN.json"
    plan.write_text(json.dumps(valid_plan), encoding="utf-8")
    request = work / "REQUEST.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": "optomind.planning_supplement.request.v1",
                "request_id": "req-1",
                "topic_id": "topic-1",
                "gap_id": "G1",
                "gap_question": "Need direct evidence.",
                "success_criteria": ["direct evidence"],
                "base_pool_path": str(pool),
                "plan_path": str(plan),
                "targeted_queries": [],
                "reuse_plan_facet_ids": [],
                "known_papers": [],
                "reviewed_references": [],
                "limits": {"max_candidates": 1, "max_acquisitions": 0, "per_query_limit": 1},
            }
        ),
        encoding="utf-8",
    )
    output = work / "supplements" / "G1"
    kwargs = {
        "request_path": request,
        "output_dir": output,
        "gateway": None,
        "acquirer_factory": lambda path: None,
        "card_runner": lambda **kwargs: {},
        "fulfillment_judge": None,
    }
    first = run_planning_supplement(**kwargs)
    second_error = ""
    try:
        run_planning_supplement(**kwargs)
    except Exception as exc:
        second_error = f"{type(exc).__name__}:{exc}"
    return {
        "first_status": first.get("status"),
        "output_exists": output.exists(),
        "second_error": second_error,
        "output_children": sorted(path.name for path in output.iterdir()),
    }


def main() -> None:
    base = Path(r"F:\OptoMind-Review-2\outputs\body_design_continuity_verification_20261003\retrieval")
    base.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    result = {
        "run_root": str(run_root),
        "first_round_no_query": first_round_no_query(run_root / "no_query"),
        "cross_stage_reuse": cross_stage_reuse(run_root / "cross_stage"),
        "supplement_directory_retry": supplement_directory_retry(run_root),
    }
    (run_root / "RESULT.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
