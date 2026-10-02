import copy, json, sys
from pathlib import Path

WORKTREE = Path(r"F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree")
RUN = Path(r"F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\chain_audit_20261002")
sys.path.insert(0, str(WORKTREE))
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning

class Counter:
    def __call__(self, _enc, messages):
        content = messages[-1]["content"]
        if "当前任务：chapter_details\n" in content:
            content = content.split("当前任务：chapter_details\n", 1)[1]
            content = content.split("\n\n【本轮交付】", 1)[0]
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            value = {}
        rows = value.get("source_materials") or [] if isinstance(value, dict) else []
        candidates = value.get("candidate_materials") or [] if isinstance(value, dict) else []
        return 100_000 + sum(int(row.get("_weight") or 0) for row in rows if isinstance(row, dict)) + sum(int(row.get("_weight") or 0) for row in candidates if isinstance(row, dict))

class MergeEmptyPlanner:
    counter = Counter()
    output_tokens = 16_000
    chapter_model = "offline"
    model = "offline"
    def __init__(self): self.calls = []
    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(dict(payload))))
        if payload.get("chapter_detail_batches"):
            # Valid JSON and valid top-level contract, but semantically empty.
            plan = {"thesis": "merged", "reader_objective": "merged", "units": [], "source_handles": []}
        else:
            handles = [row["source_handle"] for row in payload.get("source_materials") or []]
            plan = {"thesis": "batch", "reader_objective": "batch", "units": [{"substantive_point": "batch", "source_handles": handles, "paragraph_briefs": []}], "source_handles": handles}
        return {"_planner_call": True, "response": {"chapter_plan": plan}, "telemetry": {"finish_reason": "stop"}}

def payload():
    return {
        "topic_id": "audit",
        "research_question": "audit",
        "chapter": {"chapter_id": "CH01", "title": "Audit"},
        "source_materials": [
            {"source_handle": f"P{i:04d}", "paper_id": f"paper-{i}", "_weight": 150_000, "study_summary_A": {"finding": "A"}, "planning_material": {"finding": "B"}}
            for i in range(6)
        ],
        "candidate_materials": [],
    }

planner = MergeEmptyPlanner()
merged_record, meta = planning._chapter_details_adaptive_record(planner, payload(), chapter_id="CH01", cache_root=RUN / "adaptive_batches", resume=False)
merged_plan = merged_record["response"]["chapter_plan"]
assert meta and meta["batch_count"] == 2
assert merged_plan["units"] == []

cfg = planning.ProgressivePlannerConfig(topic_id="audit", pool_path=RUN / "pool.jsonl", plan_path=RUN / "plan.json", output_dir=RUN / "packet_output", chapter_workers=1)
flow = planning.ProgressiveReviewPlanner(cfg, planner=planner)
pool = [{"_paper_id": f"paper-{i}", "_source_handle": f"P{i:04d}", "_b_summary": {"title": f"Paper {i}"}} for i in range(6)]
packet = {
    "chapter": {"chapter_id": "CH01", "title": "Audit", "source_ids": [f"paper-{i}" for i in range(6)]},
    "chapter_plan": merged_plan,
    "source_materials": payload()["source_materials"],
    "source_identity_map": {f"P{i:04d}": {"paper_id": f"paper-{i}", "title": f"Paper {i}"} for i in range(6)},
}
final = flow._assemble_final(topic="audit", plan={"question": "audit"}, pool_rows=pool,
    provisional={"review_title": "Audit"}, level1_outline={}, harmonized={}, chapters=[packet["chapter"]],
    chapter_records=[packet], level1_tools={}, level2_tools={}, improvement={}, chapter_tools={})
flow._write_final_outputs(final)
written_packet = json.loads((RUN / "packet_output" / "writer_packets" / "CH01.json").read_text(encoding="utf-8"))
written_final = json.loads((RUN / "packet_output" / "DETAILED_REVIEW_PLAN.json").read_text(encoding="utf-8"))
assert written_packet["chapter_plan"]["units"] == []
assert written_final["chapters"][0]["chapter_plan"]["units"] == []

# Retrieval adapter: no index and external disabled makes the need unmet without any provider.
retrieval_cfg = planning.ProgressivePlannerConfig(topic_id="audit", pool_path=RUN / "pool.jsonl", plan_path=RUN / "plan.json", output_dir=RUN / "retrieval_output", shared_deep_read_budget=0)
runner = planning.make_retrieval_loop_runner(retrieval_cfg, local_index_path=RUN / "missing_index.sqlite", allow_external=False)
retrieval = runner(phase="chapters", supplement_requests=[{
    "gap_id": "G1", "gap_question": "specific evidence", "success_criteria": ["finding"],
    "targeted_queries": [{"query_type": "keyword", "query_text": "object relation"}], "chapter_ids": ["CH01"],
}], pool_rows=[], plan={"research_question": "audit"}, resume=False, output_dir=RUN / "retrieval_output")
assert retrieval["status"] == "complete"
need_states = retrieval["retrieval_loop"]["needs"]
assert need_states
assert any(str(state.get("status")) in {"stopped", "partial", "stopped_rounds_exhausted"} or str(state.get("action")) in {"stop", "provider_retry"} for state in need_states)
# Verify what the chapter_details model actually receives from an unmet tool result.
visibility_planner = MergeEmptyPlanner()
visibility_cfg = planning.ProgressivePlannerConfig(topic_id="audit", pool_path=RUN / "pool.jsonl", plan_path=RUN / "plan.json", output_dir=RUN / "visibility_output", chapter_workers=1)
visibility_flow = planning.ProgressiveReviewPlanner(visibility_cfg, planner=visibility_planner)
visibility_flow._chapter_details(
    chapters=[{"chapter_id": "CH01", "title": "Audit", "source_ids": []}],
    shared_outline={}, topic="audit", candidates={}, level1_tools={}, level2_tools={},
    chapter_tools=retrieval, resume=False, state={}, candidate_pool=[],
)
model_payload = visibility_planner.calls[0][1]
model_feedback = model_payload.get("relevant_tool_feedback") or []
report = {
    "merge_reproduction": {
        "adaptive_mode": meta["mode"], "batch_count": meta["batch_count"],
        "merge_returned_units": len(merged_plan["units"]),
        "merge_record_accepted": True,
        "packet_units": len(written_packet["chapter_plan"]["units"]),
        "final_plan_units": len(written_final["chapters"][0]["chapter_plan"]["units"]),
        "planner_calls": len(planner.calls),
    },
    "retrieval_reproduction": {
        "adapter_status": retrieval["status"],
        "need_states": [{"need_id": s.get("need_id"), "status": s.get("status"), "action": s.get("action"), "still_missing": s.get("still_missing")} for s in need_states],
        "retrieval_loop_keys": sorted(retrieval["retrieval_loop"].keys()),
        "provider_calls": len(retrieval.get("supplement_results") or []) + len(retrieval.get("directed_results") or []),        "unmet_in_nested_need_state": any(str(state.get("still_missing")) == "specific evidence" for state in need_states),
        "unmet_in_supplement_results": any(str(item.get("status")) == "unmet" for item in retrieval.get("supplement_results") or []),
        "chapter_model_relevant_tool_feedback": model_feedback,
        "no_network": True,
    },
}
(RUN / "CASE_CHAIN_AUDIT.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))


