"""WO07 chain 3: real adaptive queue -> reader/store -> owner -> fresh resume.

LOCAL_ONLY, synthetic paper. Only reader/owner model boundaries are substituted.
No live client, external search/download, full planner/BODY, or case review runs.
"""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import directed_reading as dr
from optomind_research.runtime.upgrade3 import progressive_review_plan as p


FIRST = "SYNTHETIC_BASELINE: Sample increased from 2 to 4 at 10 K."
PARTIAL = "SYNTHETIC_PARTIAL: Replication used three synthetic samples."
MISSING = "Temperature comparison remains unavailable."


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def answer(text, remaining=None):
    return {"question_material": [{"question_id": "Q01", "explanation": text,
        "examples": [], "remaining_points": remaining or []}]}


def task(question="What changed at 10 K?", output="Report the measured change"):
    return {"paper_id": "synthetic-paper", "chapter_ids": ["CH01"],
        "knowledge_gaps": ["Explain the synthetic measured result"],
        "questions": [{"question_id": "Q01", "question": question,
            "purpose": "Explain the requested synthetic result", "required_output_ids": ["O01"]}],
        "required_outputs": [{"output_id": "O01", "output_type": "explanation", "description": output}]}


class OwnerBoundary:
    """The injected planner callable is the production planner's model seam."""
    def __init__(self, root, *, expect_update=True):
        self.root = root
        self.calls = []
        self.expect_update = expect_update
        self.fail_owner = False
        self.expect_partial = True

    def __call__(self, stage, payload):
        # Persist the exact production prompt, rather than fabricating an input.
        messages = p._messages_for(stage, payload)
        self.calls.append({"stage": stage, "payload": copy.deepcopy(payload), "messages": messages})
        p._atomic_json(self.root / "boundary" / f"owner-{len(self.calls):02d}.json", self.calls[-1])
        if stage == "whole_plan_improvement":
            return {"chapter_updates": [{"chapter_id": "CH01", "feedback": "Use the current partial answer and retain its limit"}]
                    if self.expect_update else []}
        assert stage == "affected_chapter_revision", stage
        if self.fail_owner:
            raise RuntimeError("LOCAL_ONLY owner network boundary unavailable")
        actual = json.dumps(payload["source_materials"])
        if not self.expect_partial:
            assert FIRST in actual
            assert all(not item["still_missing"] for item in payload["tool_materials"])
            return {"status": "no_change", "updated_plan": copy.deepcopy(payload["chapter_plan"])}
        assert FIRST in actual and PARTIAL in actual and MISSING in actual, actual
        revised = copy.deepcopy(payload["chapter_plan"])
        revised["thesis"] = "Updated from the retained, source-bound partial answer"
        revised["units"][0]["paragraph_briefs"].append({"point": PARTIAL,
            "development": MISSING, "source_handles": ["P0001"]})
        return {"chapter_updates": [{"chapter_id": "CH01", "updated_plan": revised}]}


def setup(tmp_path, monkeypatch, *, expect_update=True):
    source = tmp_path / "source"
    snapshot = source / "snapshot"
    snapshot.mkdir(parents=True)
    (snapshot / "READING_VIEW.md").write_text(
        "# Synthetic source\n" + FIRST + "\n" + PARTIAL + "\n" + MISSING + "\n", encoding="utf-8")
    card = source / "PAPER_READING_CARD.json"
    p._atomic_json(card, {"paper_identity": {"canonical_paper_id": "synthetic-paper", "title": "Synthetic fixture"},
        "material": {"material_scope": "fulltext"},
        "general_understanding": {"finding": "A bounded synthetic study measured the result."},
        "review_planning": {"use": "Explain the synthetic measurement"}})
    p._atomic_json(source / "SOURCE_UNIT.json", {"snapshot_path": str(snapshot)})
    placeholder = tmp_path / "MODEL_BOUNDARY_PLACEHOLDER.txt"
    placeholder.write_text("LOCAL_ONLY not a credential", encoding="utf-8")
    reader_calls = []
    responses = [answer(FIRST), answer(PARTIAL, [MISSING])]

    class ReaderBoundary:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
        def complete(self, messages, **kwargs):
            reader_calls.append({"messages": copy.deepcopy(messages), **kwargs})
            p._atomic_json(tmp_path / "boundary" / f"reader-{len(reader_calls):02d}.json", reader_calls[-1])
            assert len(reader_calls) <= len(responses), "Unexpected extra reader model call"
            return {"content": json.dumps(responses[len(reader_calls) - 1]),
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(dr, "QwenDirectClient", ReaderBoundary)
    config = p.ProgressivePlannerConfig(topic_id="body07-synthetic-reading", pool_path=tmp_path / "POOL.jsonl",
        plan_path=tmp_path / "PLAN.json", output_dir=tmp_path / "run", shared_deep_read_budget=1,
        reader_workers=1, chapter_workers=1, planning_revision_enabled=True)
    owner = OwnerBoundary(tmp_path, expect_update=expect_update)
    pool = [{"_paper_id": "synthetic-paper", "_source_handle": "P0001", "title": "Synthetic fixture", "card_path": str(card)}]
    chapter = {"chapter_id": "CH01", "title": "Measured behavior", "scope": "Synthetic conditions",
               "source_ids": ["synthetic-paper"], "source_handles": ["P0001"]}
    packet = {"topic_id": config.topic_id, "research_question": "Explain synthetic behavior", "chapter": chapter,
        "shared_outline": {"chapters": [chapter]}, "candidate_materials": [], "candidate_navigation": {},
        "source_materials": [p.build_local_material_payload(pool[0])],
        "chapter_plan": {"thesis": "Original bounded result", "reader_objective": "Explain the result", "units": [
            {"unit_id": "CH01_U01", "point": "Synthetic conditions", "source_handles": ["P0001"],
             "paragraph_briefs": [{"point": "Explain the measurement", "source_handles": ["P0001"]}]}]}}

    def new_planner():
        directed = p.make_directed_reading_runner(config, key_file=placeholder,
            budget_ledger_path=tmp_path / "BUDGET.sqlite", budget_limit_cny=30)
        retrieval = p.make_retrieval_loop_runner(config, allow_external=False, directed_reader=directed)
        return p.ProgressiveReviewPlanner(config, planner=owner, directed_reader=directed,
                                         retrieval_loop_runner=retrieval)
    return config, pool, packet, reader_calls, owner, new_planner


def cycle(planner, pool, phase, request, *, prior=None, resume=False):
    return planner._tool_cycle(phase=phase, supplement_requests=[], directed_requests=[request], pool_rows=pool,
        plan={"research_question": "Explain synthetic behavior"}, prior_directed=prior, prior_tool_results={},
        source_handle_map={"P0001": "synthetic-paper"}, resume=resume,
        state=read(planner.config.output_dir / "RUN_STATE.json") if (planner.config.output_dir / "RUN_STATE.json").is_file() else {})


def owner_review(planner, pool, packet, tools, *, resume=True):
    """Invoke only the bounded pre-case revision branch; no cases are run."""
    rows, improvement, _ = planner._post_case_review(root=planner.config.output_dir,
        topic="Explain synthetic behavior", harmonized={"shared_outline": packet["shared_outline"]},
        level1_outline={}, detail_records=[copy.deepcopy(packet)], baseline_detail_records=[copy.deepcopy(packet)],
        case_record={}, level1_tool_result={}, level2_tool_result={}, chapter_tool_result=tools,
        editorial_feedback={}, pool_rows=pool, resume=resume,
        state=read(planner.config.output_dir / "RUN_STATE.json") if (planner.config.output_dir / "RUN_STATE.json").is_file() else {})
    p._atomic_json(planner.config.output_dir / "OWNER_CHAIN_RESULT.json", {"chapters": rows, "improvement": improvement})
    return read(planner.config.output_dir / "OWNER_CHAIN_RESULT.json")


def store_evidence(tmp_path, name, summary):
    """Optional auditable export of real persisted bytes, with local paths relative."""
    destination = os.environ.get("BODY07_READING_EVIDENCE_DIR")
    if not destination:
        return
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    root = str(tmp_path)
    artifacts = {}
    for path in sorted(tmp_path.rglob("*")):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".md"}:
            raw = path.read_bytes()
            redacted = raw.decode("utf-8").replace(root + "/", "")
            artifacts[str(path.relative_to(tmp_path))] = {
                "original_sha256": hashlib.sha256(raw).hexdigest(),
                "redacted_sha256": hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
                "text": redacted,
            }
    p._atomic_json(target / (name + ".json"), {
        "fixture": "LOCAL_ONLY synthetic source", "path_redaction": "Temporary fixture root prefix removed; paths are fixture-relative",
        "summary": json.loads(json.dumps(summary).replace(root + "/", "")), "artifacts": artifacts,
    })


@pytest.mark.parametrize("change", ["question", "required_outputs"])
def test_same_paper_new_task_partial_owner_resume(tmp_path, monkeypatch, change):
    config, pool, packet, calls, owner, new_planner = setup(tmp_path, monkeypatch)
    planner = new_planner()
    baseline = task()
    changed = task(question="How was the result replicated?") if change == "question" else task(output="Report replication and the temperature comparison")
    first = cycle(planner, pool, "first_read", baseline)
    first_material = first["directed_results"][0]["materials"][0]
    first_path = first["directed_results"][0]["results"][0]["output_dir"]
    original = Path(first_path)
    before = {name: (original / name).read_bytes() for name in ("INPUT.json", "RAW_RESPONSE.json", "DIRECTED_READING.json")}
    partial = cycle(planner, pool, "changed_read", changed, prior=first)
    material = partial["directed_results"][-1]["materials"][0]
    partial_path = partial["directed_results"][-1]["results"][0]["output_dir"]
    state = partial["retrieval_loop"]["needs"][0]
    assert len(calls) == 2
    assert state["status"] == "partial" and state["still_missing"]
    assert FIRST in json.dumps(material) and PARTIAL in json.dumps(material) and MISSING in json.dumps(material)
    assert p._directed_material_status(changed, material) == "partial"
    assert not p._directed_material_compatible(changed, material)
    assert material["task_id"] != first_material["task_id"]
    assert partial_path != first_path
    assert material["_progressive_task_signature"] != first_material["_progressive_task_signature"]
    second_input = read(Path(partial_path) / "INPUT.json")
    assert second_input["task_id"] == material["task_id"]
    assert second_input["task"]["questions"][0]["question"] == changed["questions"][0]["question"]
    assert second_input["task"]["required_outputs"][0]["description"] == changed["required_outputs"][0]["description"]
    store = dr.DirectedReadingStore(config.output_dir / "directed_reading.sqlite", core_cap=1)
    assert store.summary(config.topic_id)["core_count"] == 1
    assert store.summary(config.topic_id)["task_count"] == 2
    assert store.committed_reading(config.topic_id, first_material["task_id"])
    assert store.committed_reading(config.topic_id, material["task_id"]) is None
    updated = owner_review(planner, pool, packet, partial)
    saved = read(config.output_dir / "stages/affected_chapter_revision/CH01.json")
    assert saved["owner_status"] == "updated" and saved["status"] == "complete", saved
    assert PARTIAL in json.dumps(saved["updated_plan"])
    assert all(text in json.dumps(saved["cache_inputs"]["source_materials"]) for text in (FIRST, PARTIAL, MISSING))
    assert all(text in json.dumps(owner.calls[-1]["messages"]) for text in (FIRST, PARTIAL, MISSING))
    assert saved["cache_inputs"]["tool_materials"][0]["still_missing"] == ""
    assert saved["cache_inputs"]["tool_materials"][1]["still_missing"]
    assert first["tool_materials_by_chapter"]["CH01"][0]["still_missing"] == ""

    # Brand-new production constructors, loaded SQLite/material/stage caches.
    # A cached owner must survive even when the next network attempt would fail.
    owner.fail_owner = True
    resumed = new_planner()
    restored_first = cycle(resumed, pool, "first_read", baseline, resume=True)
    retained = cycle(resumed, pool, "changed_read", changed, prior=restored_first, resume=True)
    again = owner_review(resumed, pool, packet, retained)
    assert again["chapters"][0]["chapter_plan"] == updated["chapters"][0]["chapter_plan"]
    assert len([call for call in owner.calls if call["stage"] == "affected_chapter_revision"]) == 1
    assert len(calls) == 2
    assert all(text in json.dumps(again["chapters"][0]["source_materials"]) for text in (FIRST, PARTIAL, MISSING))

    # Cross-phase fulfilled-task reuse must also clear the stale local question.
    reused_cycle = cycle(resumed, pool, "fulfilled_reuse", baseline, prior=retained)
    assert reused_cycle["retrieval_loop"]["needs"][0]["status"] == "answered"
    assert reused_cycle["tool_materials_by_chapter"]["CH01"][0]["still_missing"] == ""
    assert len(calls) == 2
    # The actual adapter/store retains its task directory across another phase.
    reused = resumed.directed_reader([baseline], output_dir=config.output_dir / "reuse_elsewhere",
        pool_by_id={"synthetic-paper": pool[0]}, plan={"research_question": "Explain synthetic behavior"})
    reused_row = reused["results"][0]
    assert reused_row["reused"] and reused_row["task_id"] == first_material["task_id"]
    assert reused_row["output_dir"] == first_path and len(calls) == 2
    retained_raw = resumed.directed_reader([changed], output_dir=config.output_dir / "partial_elsewhere",
        pool_by_id={"synthetic-paper": pool[0]}, plan={"research_question": "Explain synthetic behavior"})
    assert retained_raw["results"][0]["status"] == "partial" and not retained_raw["results"][0]["reused"]
    assert retained_raw["results"][0]["output_dir"] == partial_path and len(calls) == 2
    for name, contents in before.items():
        assert (original / name).read_bytes() == contents
    with store._connect() as db:
        db_rows = {name: [dict(row) for row in db.execute("SELECT * FROM " + name)]
                   for name in ("directed_readings", "directed_tasks")}
    store_evidence(tmp_path, change, {"status": "passed", "reader_model_calls": len(calls),
        "owner_stage_calls": [call["stage"] for call in owner.calls], "new_task_status": state["status"],
        "unresolved": state["still_missing"], "first_task_id": first_material["task_id"],
        "new_task_id": material["task_id"], "first_output": str(original), "new_output": partial_path,
        "owner_cache_contract": saved["owner_cache_contract"], "store_summary": store.summary(config.topic_id),
        "store_rows": db_rows, "fulfilled_reused": reused_row["reused"], "partial_preserved": True})


def test_normal_pre_case_owner_path_without_new_material(tmp_path, monkeypatch):
    config, pool, packet, calls, owner, new_planner = setup(tmp_path, monkeypatch, expect_update=False)
    planner = new_planner()
    result = owner_review(planner, pool, packet, {})
    assert result["chapters"][0]["chapter_plan"] == packet["chapter_plan"]
    assert not calls
    assert [call["stage"] for call in owner.calls] == ["whole_plan_improvement"]
    assert not (config.output_dir / "stages/affected_chapter_revision/CH01.json").exists()
    assert not (config.output_dir / "directed_reading.sqlite").exists()
    resumed = owner_review(new_planner(), pool, packet, {})
    assert resumed["chapters"][0]["chapter_plan"] == packet["chapter_plan"]
    assert len(owner.calls) == 1
    store_evidence(tmp_path, "normal", {"status": "passed", "reader_model_calls": 0,
        "owner_model_calls": 0, "coordinator_model_calls": 1, "plan_unchanged": True,
        "new_material": False, "recovery": False, "oversized_batch": False})


def test_fulfilled_read_clears_stale_local_gap_before_owner(tmp_path, monkeypatch):
    config, pool, packet, calls, owner, new_planner = setup(tmp_path, monkeypatch)
    owner.expect_partial = False
    planner = new_planner()
    complete = cycle(planner, pool, "normal_fulfilled", task())
    state = complete["retrieval_loop"]["needs"][0]
    assert state["status"] == "answered" and state["still_missing"] == ""
    # The earlier local lookup was unmet. External completion is authoritative.
    assert state["local_triage"]["still_missing"]
    assert complete["tool_materials_by_chapter"]["CH01"][0]["still_missing"] == ""
    result = owner_review(planner, pool, packet, complete)
    saved = read(config.output_dir / "stages/affected_chapter_revision/CH01.json")
    assert saved["owner_status"] == "no_change", saved
    assert saved["cache_inputs"]["tool_materials"][0]["still_missing"] == ""
    assert result["chapters"][0]["chapter_plan"] == packet["chapter_plan"]
    assert len(calls) == 1
    store_evidence(tmp_path, "normal_fulfilled", {"status": "passed", "reader_model_calls": 1,
        "owner_stage_calls": [call["stage"] for call in owner.calls], "need_status": state["status"],
        "final_still_missing": state["still_missing"], "stale_local_still_missing": state["local_triage"]["still_missing"],
        "writer_material_still_missing": complete["tool_materials_by_chapter"]["CH01"][0]["still_missing"],
        "owner_status": saved["owner_status"], "plan_unchanged": True})
