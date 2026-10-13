"""Free native batch recovery from exact actual requests; network is prohibited.

This is development-supervision recovery, not a new planning algorithm.
Original decisions/results remain immutable and no routing answer is supplied here.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

E = Path(__file__).resolve().parents[2]
O = E / "worktree/outputs/optics_e2e/07_progressive_review_plan_docling_20261013"
BINDING = E / "RUN_BINDING_PLANNING_V18_PARALLEL_PRECISION_20261014.json"
binding = json.loads(BINDING.read_text(encoding="utf-8"))
binding_hash = hashlib.sha256(BINDING.read_bytes()).hexdigest()
sys.path.insert(0, str(E / "preparation/planning_supervision_candidate"))
sys.path.insert(0, binding["source_worktree"])
from planning_transport import PlanningTransport
from optomind_research.runtime.upgrade3.module4.runtime import QwenDirectClient
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig, ProgressiveReviewPlanner, QwenProgressivePlanner,
)

def ledger_rows():
    with sqlite3.connect((E / "budget.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("select * from reservations order by rowid").fetchall()

requests = {}
for gate in (E / "supervision/calls").iterdir():
    if not (gate / "REQUEST.json").exists():
        continue
    q = json.loads((gate / "REQUEST.json").read_text(encoding="utf-8"))
    if q["profile"].get("binding_sha256") != binding_hash:
        continue
    call = q["kwargs"].get("call_id", "")
    for i in range(1, 6):
        if call == f"progressive-review:source_routing:source-routing-batch_{i:03d}-of-005":
            user = next(m["content"] for m in q["messages"] if m["role"] == "user")
            requests[i] = json.JSONDecoder().raw_decode(user[user.index("{"):])[0]
assert set(requests) == set(range(1, 6))
pool = []
for i in range(1, 6):
    for row in requests[i]["candidate_batch"]:
        b = dict(row["B_review_planning"])
        if row["curator_note"]:
            b["root_review_note"] = row["curator_note"]
        pool.append({"_source_handle": row["source_handle"], "_b_summary": b,
                     "supplement_gap_material": row["supplement_material"],
                     "supplement_gap_materials": row["other_supplement_materials"]})
assert len(pool) == 245 and len({x["_source_handle"] for x in pool}) == 245

class NoNetworkClient(QwenDirectClient):
    def __call__(self, *args, **kwargs):
        raise AssertionError("Physical API prohibited in native saved-response recovery")

class SavedOnlyPlanner(QwenProgressivePlanner):
    def __call__(self, stage, payload):
        assert stage == "source_routing" and payload == requests[3]
        return super().__call__(stage, payload)

before = ledger_rows()
config = ProgressivePlannerConfig(topic_id="optics-e2e-20261012",
    pool_path=E / "worktree/outputs/optics_e2e/06_planning_pool_docling_20261013/PLANNING_POOL.jsonl",
    plan_path=E / "worktree/outputs/optics_e2e/01_question_retrieval_plan/PLAN.json",
    output_dir=O, chapter_workers=3, reader_workers=32)
with PlanningTransport(binding_path=BINDING, fake_provider=NoNetworkClient).activate():
    planner = SavedOnlyPlanner(model="qwen3.5-plus", key_file=binding["key_file"],
        budget_ledger_path=binding["project_ledger"], budget_limit_cny=80,
        output_dir=O, tokenizer_path=r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json",
        timeout_seconds=900, thinking_budget=16384, output_tokens=32768,
        chapter_model="qwen3.7-flash")
    engine = ProgressiveReviewPlanner(config, planner=planner)
    for i in (1, 2, 4, 5):
        cached = json.loads((O / f"stages/source_routing/batch_{i:03d}.json").read_text(encoding="utf-8"))
        assert cached["cache_contract"] == engine._cache_contract("source_routing", requests[i])
    state = json.loads((O / "RUN_STATE.json").read_text(encoding="utf-8"))
    outline = json.loads((O / "stages/level1_outline.json").read_text(encoding="utf-8"))["response"]["shared_outline"]
    routes = engine._route_sources(pool, shared_outline=outline, resume=True, state=state)
assert ledger_rows() == before
assert len(routes["source_routes"]) == 245
assert all(r["route_status"] in {"assigned", "not_relevant"} for r in routes["source_routes"])
receipt = {"schema": "optomind.native_saved_routing_recovery.v1",
    "source_commit": binding["source_commit"], "binding_sha256": binding_hash,
    "new_api_calls": 0, "ledger_rows_unchanged": True, "routes": 245,
    "assigned": routes["assigned_sources"], "native_batch3": str(O / "stages/source_routing/batch_003.json"),
    "native_batch3_sha256": hashlib.sha256((O / "stages/source_routing/batch_003.json").read_bytes()).hexdigest(),
    "input_provenance": "Exact original actual requests, not handwritten routes",
    "network_guard": "NoNetworkClient prohibits physical invocation; cached actual response only"}
(Path(__file__).parent / "NATIVE_V18_ROUTING_FREE_RECOVERY.json").write_text(
    json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(receipt, ensure_ascii=False))
