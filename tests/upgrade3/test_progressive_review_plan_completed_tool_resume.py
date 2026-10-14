"""Bounded offline checks for the accepted adaptive-tool resume boundary."""
import copy
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as p


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Tool resume tests are offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def accepted_cycle(tmp_path, phase="level1", with_source_proof=False):
    cfg = p.ProgressivePlannerConfig("T", tmp_path / "pool", tmp_path / "plan", tmp_path)
    calls = []
    material = {"paper_id": "one", "question_material": [{"finding": "Saved measured result"}]}
    result = {"phase": phase, "status": "complete", "supplement_results": [{"status": "failed"}],
              "directed_results": [{"materials": [material]}],
              "tool_materials_by_chapter": {"CH1": [{"need_id": "N1", "usable_content": "Saved result"}]},
              "retrieval_loop": {"needs": [{"need_id": "N1", "status": "pending", "action": "provider_retry"}]}}
    if with_source_proof:
        result["retrieval_loop"]["needs"][0]["reuse_key"] = "proof"
    def runner(**kwargs):
        calls.append(kwargs)
        return copy.deepcopy(result)
    engine = p.ProgressiveReviewPlanner(cfg, planner=lambda *_: {}, retrieval_loop_runner=runner)
    pool = [{"_paper_id": "one", "_b_summary": {"finding": "Original result"}}]
    args = dict(phase=phase, supplement_requests=[], directed_requests=([] if with_source_proof else [{"paper_id": "one", "questions": ["Measure"]}]),
                pool_rows=pool, plan={"question": "Q"}, prior_directed=None, prior_tool_results=None,
                source_handle_map={"P0001": "one"}, resume=True)
    state = {}
    first = engine._tool_cycle(**args, state=state)
    consumer, key = {"level1": ("level1_outline", "actual_tool_results"),
                     "level2": ("finalize_chapter_scope", "level2_tool_results")}[phase]
    engine._model_stage(consumer, {key: engine._compact_tool_feedback(first)}, resume=False, state=state)
    state["current_stage"] = "chapter_need_analysis"
    return SimpleNamespace(cfg=cfg, engine=engine, runner=runner, state=state, args=args, calls=calls, first=first,
                           path=tmp_path / "stages" / (phase + "_tools.json"))


@pytest.mark.parametrize("phase", ["level1", "level2"])
def test_accepted_tools_restore_exact_material_without_reentering_collector(tmp_path, phase):
    f = accepted_cycle(tmp_path, phase)
    before = f.path.read_bytes()
    saved_inputs = copy.deepcopy(f.state["stage_inputs"])
    saved_contracts = copy.deepcopy(f.state["stage_cache_contracts"])
    # Later phases may grow shared caches and append empty failed attempts.
    p._atomic_json(tmp_path / "DIRECTED_MATERIAL_CACHE.json", {"materials": {"later": {"finding": "Later phase"}}})
    (tmp_path / phase / "retrieval_loop.jsonl").parent.mkdir(parents=True)
    (tmp_path / phase / "retrieval_loop.jsonl").write_text('{"status":"provider_retry"}\n')
    restarted = p.ProgressiveReviewPlanner(f.cfg, planner=lambda *_: pytest.fail("No model call"), retrieval_loop_runner=f.runner)
    restored = restarted._tool_cycle(**f.args, state=f.state)
    assert restored == f.first and len(f.calls) == 1
    assert f.path.read_bytes() == before
    assert f.state["stage_inputs"] == saved_inputs and f.state["stage_cache_contracts"] == saved_contracts
    assert restarted._read_materials["one"] == f.first["directed_results"][0]["materials"][0]
    assert restarted.tool_materials_by_chapter == f.first["tool_materials_by_chapter"]


@pytest.mark.parametrize("change", ["missing", "failed", "unfinished", "unconsumed", "retry", "task", "pool", "reader", "prompt", "external_index"])
def test_unfinished_retry_and_changed_inputs_reenter_collector(tmp_path, monkeypatch, change):
    f = accepted_cycle(tmp_path)
    if change == "missing":
        f.path.unlink()
    elif change == "failed":
        result = copy.deepcopy(f.first); result["status"] = "failed"
        p._atomic_json(f.path, result)
    elif change == "unfinished":
        f.state["current_stage"] = "level1_tools"
    elif change == "unconsumed":
        f.state["completed_stages"].remove("level1_outline")
    elif change == "retry":
        f.args["directed_requests"][0]["retry_empty_result"] = True
    elif change == "task":
        f.args["directed_requests"][0]["questions"] = ["New required measurement"]
    elif change == "pool":
        f.args["pool_rows"][0]["_b_summary"]["finding"] = "Corrected measurement"
    elif change == "reader":
        f.engine.config = replace(f.cfg, reader_model="new-reader")
    elif change == "prompt":
        original = p._planning_helper_settings
        monkeypatch.setattr(p, "_planning_helper_settings", lambda cfg: {**original(cfg), "prompt_revision": "new"})
    elif change == "external_index":
        f.runner.completed_stage_cache_uses_managed_index = False
    f.engine._tool_cycle(**f.args, state=f.state)
    assert len(f.calls) == 2


def test_supplement_source_proof_survives_internal_history_but_rejects_source_change(tmp_path):
    f = accepted_cycle(tmp_path, with_source_proof=True)
    snapshot = tmp_path / "snapshot"; snapshot.mkdir()
    reading = snapshot / "READING_VIEW.md"; reading.write_text("Original source")
    f.args["pool_rows"][0]["snapshot_dir"] = str(snapshot)
    # Freeze the effective outer input with the source path included.
    f.state["stage_inputs"]["level1_tools"]["pool_rows"] = copy.deepcopy(f.args["pool_rows"])
    f.state["stage_cache_contracts"]["level1_tools"] = f.engine._cache_contract("level1_tools", f.state["stage_inputs"]["level1_tools"])
    proof = {"contract": {"reuse_key": "proof"}, "state": {"need_id": "N1"},
             "source_fingerprints": {"one": p._supplement_source_fingerprint(f.args["pool_rows"][0])}}
    # A later internal need generation keeps the original proof in history.
    p._atomic_json(tmp_path / "RETRIEVAL_NEED_CACHE.json", {"records": {
        "proof": {**proof, "state": {"need_id": "N1-later"}}, "proof:prior": proof}})
    assert f.engine._tool_cycle(**f.args, state=f.state) == f.first and len(f.calls) == 1
    reading.write_text("Corrected source measurement")
    f.engine._tool_cycle(**f.args, state=f.state)
    assert len(f.calls) == 2
