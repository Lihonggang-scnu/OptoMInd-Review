"""Second-stop case handoff and actual batch reentry, with offline model boundary.

The complete production planner, persisted case cache, arrangement export and
standalone writer are used. No execution/attachment/cache helpers are replaced.
One cache control changes only the in-memory case prompt text.
Set BODY_PREFLIGHT_CASE_EVIDENCE_ROOT to retain complete local fixture evidence.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3.module4 import runtime as transport
from scripts.upgrade3 import chapter_arrangement as arrangement_cli
from scripts.upgrade3 import review_unit_writer as writer_cli


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("case acceptance forbids network access")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


class CaseRun:
    def __init__(self, root, monkeypatch, *, revision=True):
        self.root = root
        self.calls = []
        self.chapter = {
            "chapter_id": "CH01", "title": "Mechanism", "purpose": "Explain the mechanism",
            "scope": "Material-backed conditions", "source_handles": ["P0001"],
            # The owner has not selected P0002. It remains a routed candidate
            # for the later case selection, outside the one-row navigation.
            "excluded_source_handles": ["P0002"], "excluded_source_ids": ["paper-2"],
        }
        self.deep = {
            "paper_id": "paper-2", "title": "Independent case", "doi": "10.1000/case-2",
            "status": "partial", "reading_mode": "review_reported_original",
            "question_material": [{
                "question": "Which conditions support the original study?",
                "explanation": "DEEP_ONLY_ANSWER: " + "Supported condition. " * 75 + "UNCLIPPED_TAIL",
                "conditions": "ORIGINAL_STUDY_CONDITION",
                "limits": "ORIGINAL_STUDY_LIMIT",
                "reference_ids": ["R2"],
            }],
            "references": [{"reference_id": "R2", "title": "Independent case", "doi": "10.1000/case-2"}],
            "still_missing": ["Prospective validation remains unavailable in this material"],
        }
        card = root / "P0001_CARD.json"
        dump(card, {
            "paper_identity": {"paper_id": "paper-1", "title": "Mechanism source"},
            "general_understanding": {"finding": "Baseline mechanism condition"},
            "review_planning": {"planning_summary": "Baseline mechanism condition"},
        })
        dump(root / "PLAN.json", {"question": "How does the mechanism depend on conditions?", "facets": [{"facet_id": "F1", "question": "mechanism conditions"}]})
        self.pool = [
            {"paper_id": "paper-1", "card_path": str(card), "planning_view": {
                "paper_identity": {"paper_id": "paper-1", "title": "Mechanism source"},
                "planning_summary": "Baseline mechanism condition"}},
            {"paper_id": "paper-2", "planning_view": {"paper_identity": {
                "paper_id": "paper-2", "title": "Independent case", "doi": "10.1000/case-2"}}},
        ]
        self.write_pool()
        driver = self

        class OfflineProvider:
            def __init__(self, **settings):
                self.settings = settings

            def complete(self, messages, **kwargs):
                stage = kwargs["call_id"].split(":")[1]
                raw = messages[-1]["content"]
                payload, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
                call = {"stage": stage, "payload": copy.deepcopy(payload), "messages": copy.deepcopy(messages),
                        "model": kwargs["model"], "output_tokens": kwargs["max_output_tokens"],
                        "thinking_budget": kwargs["thinking_budget"]}
                driver.calls.append(call)
                dump(root / "model_calls" / f"{len(driver.calls):03d}_{stage}.json", call)
                response = driver.response(stage, payload)
                return {"content": json.dumps(response), "complete": True, "finish_reason": "stop",
                        "requested_model": kwargs["model"], "usage": {"input_tokens": 0, "output_tokens": 0}}

        monkeypatch.setattr(transport, "QwenDirectClient", OfflineProvider)
        # Avoid constructing a paid ledger or loading a tokenizer; actual
        # QwenProgressivePlanner.__call__ and invoke_client run unchanged.
        adapter = object.__new__(planning.QwenProgressivePlanner)
        adapter.model = "offline-outline-model"
        adapter.chapter_model = "offline-case-model-a"
        adapter.output_tokens = 18000
        adapter.thinking_budget = 8192
        adapter.output_dir = root / "run"
        adapter.key_file = root / "NO_KEY_READ"
        adapter.timeout_seconds = 1
        adapter.ledger = None
        adapter.counter = lambda *_args, **_kwargs: 32
        self.adapter = adapter
        self.planner = planning.ProgressiveReviewPlanner(
            planning.ProgressivePlannerConfig(
                topic_id="case-material-consumer", plan_path=root / "PLAN.json", pool_path=root / "POOL.jsonl",
                output_dir=root / "run", chapter_workers=1, planning_revision_enabled=revision,
                planning_revision_candidate_limit=1,
            ), planner=adapter, prior_readings=[self.deep],
        )

    def write_pool(self):
        (self.root / "POOL.jsonl").write_text("".join(json.dumps(row) + "\n" for row in self.pool), encoding="utf-8")

    def response(self, stage, payload):
        if stage == "provisional_scope":
            return {"provisional_outline": [self.chapter], "material_theme_inventory": ["mechanism"]}
        if stage == "level1_outline":
            return {"shared_outline": [self.chapter]}
        if stage == "source_routing":
            return {"source_routes": [
                {"source_handle": handle, "chapter_ids": ["CH01"], "specific_usable_material": "Condition-bound mechanism"}
                for handle in ["P0001", "P0002"]]}
        if stage == "chapter_proposals":
            return {"chapter_proposals": [self.chapter]}
        if stage in {"harmonize_scope", "finalize_chapter_scope"}:
            return {"shared_outline": [self.chapter], "chapters": [self.chapter]}
        if stage == "chapter_details":
            return {"chapter_plan": {"thesis": "Conditions determine the mechanism", "units": [{
                "unit_id": "U1", "substantive_point": "Compare conditions", "source_handles": ["P0001"],
                "paragraph_briefs": [{"paragraph_id": "B1", "point": "State the mechanism",
                    "development": "Relate the mechanism to its conditions", "source_handles": ["P0001"]}],
            }]}}
        if stage in {"whole_plan_improvement", "affected_chapter_revision"}:
            return {"status": "no_change"}
        if stage == "case_groups":
            return {"additions": [{"unit_key": "CH01:1", "studies": [{
                "source_handle": "P0002", "contribution": "Use the independent original study under its stated condition.",
            }]}]}
        raise AssertionError(stage)

    def run(self, *, resume=False):
        start = len(self.calls)
        result = self.planner.run(resume=resume)
        return result, self.calls[start:]

    def batch(self):
        return read(self.root / "run/stages/case_groups/CH01__batch_001_of_001.json")


def root_for(tmp_path, name):
    supplied = os.environ.get("BODY_PREFLIGHT_CASE_EVIDENCE_ROOT")
    root = (Path(supplied) / name if supplied else tmp_path / name).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def writer_handoff(root, *, name):
    packet_path = root / "run/writer_packets/CH01.json"
    view = arranging.build_chapter_view(packet_path, id_map_path=root / "ID_MAP.json")
    arrangement = {"chapter_id": "CH01", "chapter_argument": "Conditions determine the mechanism", "units": [{
        "unit_id": "U1", "paragraph_tasks": [{"paragraph_id": "B1", "source_briefs": ["B1"],
            "point": "Explain both studies under their conditions", "development": "Preserve the source boundaries",
            "source_uses": [{"source_handle": "P0001"}, {"source_handle": "P0002", "role": "case"}]}],
    }]}
    arrangement_root = root / name / "arrangement"
    arrangement_root.mkdir(parents=True, exist_ok=True)
    arrangement = arranging.validate_arrangement(arrangement, view, planning_revision=True)
    assert arrangement["validation"]["ok"] is True, arrangement["validation"]
    arranging.write_view(view, arrangement_root / "ARRANGEMENT_INPUT.json")
    arrangement_cli._export(arrangement_root, view, arrangement)
    writer_root = root / name / "writer"
    result = writer_cli.main(["--arrangement", str(arrangement_root / "CHAPTER_ARRANGEMENT.json"),
                             "--unit", "U1", "--planning-revision", "--output-root", str(writer_root)])
    assert result == 0
    messages = read(writer_root / "CH01_U1/UNIT_MESSAGES.json")
    return read(arrangement_root / "CHAPTER_ARRANGEMENT.json"), messages


@pytest.mark.parametrize("revision", [False, True], ids=["legacy", "body_revision"])
def test_first_selected_independent_deep_reaches_actual_writer(tmp_path, monkeypatch, revision):
    root = root_for(tmp_path, "independent_deep_" + str(revision))
    driver = CaseRun(root, monkeypatch, revision=revision)
    result, calls = driver.run()
    case = next(call for call in calls if call["stage"] == "case_groups")
    candidate = next(row for row in case["payload"]["source_materials"] if row["source_handle"] == "P0002")
    assert candidate["material_available"] is True
    assert "DEEP_ONLY_ANSWER" in json.dumps(candidate)
    assert "UNCLIPPED_TAIL" not in json.dumps(candidate)  # selection alone is bounded
    packet = read(root / "run/writer_packets/CH01.json")
    # Explicit failure-first evidence precedes the regression assertion.
    dump(root / "FIRST_SELECTION_RESULT.json", {
        "candidate_material_available": candidate["material_available"],
        "supporting_studies": packet["chapter_plan"]["units"][0].get("supporting_studies"),
        "packet_source_handles": [row.get("source_handle") for row in packet["source_materials"]],
    })
    assert packet["chapter_plan"]["units"][0]["supporting_studies"][0]["source_handle"] == "P0002"
    source = next(row for row in packet["source_materials"] if row["source_handle"] == "P0002")
    assert not source.get("study_summary_A") and not source.get("review_planning_B")
    assert "UNCLIPPED_TAIL" in json.dumps(source)
    assert source["deep_read_material"]["status"] == "partial"
    arrangement, messages = writer_handoff(root, name="fresh")
    assert "DEEP_ONLY_ANSWER" in json.dumps(arrangement["source_catalog"])
    assert "UNCLIPPED_TAIL" in json.dumps(messages)
    assert "ORIGINAL_STUDY_CONDITION" in json.dumps(messages)
    assert "ORIGINAL_STUDY_LIMIT" in json.dumps(messages)
    assert "10.1000/case-2" in json.dumps(messages)
    driver.planner = planning.ProgressiveReviewPlanner(
        driver.planner.config, planner=driver.adapter, prior_readings=[driver.deep],
    )
    resumed, resumed_calls = driver.run(resume=True)
    assert not [call for call in resumed_calls if call["stage"] == "case_groups"]
    cached_arrangement, cached_messages = writer_handoff(root, name="resumed")
    assert cached_messages == messages
    assert cached_arrangement["source_catalog"] == arrangement["source_catalog"]
    if revision:
        stages = [call["stage"] for call in calls]
        assert stages.index("whole_plan_improvement") < stages.index("case_groups")
        assert not any(stage in {"whole_plan_improvement", "affected_chapter_revision"} for stage in stages[stages.index("case_groups") + 1:])


@pytest.mark.parametrize("change,expected_case_calls", [
    ("same", 0), ("model", 1), ("output_tokens", 1), ("inactive_thinking", 0),
    ("prompt", 1), ("material", 1), ("metadata_only", 0),
    ("clipped_suffix", 0), ("legacy_batch", 1), ("failed_batch", 1),
])
def test_actual_case_batch_cache_reentry(tmp_path, monkeypatch, change, expected_case_calls):
    root = root_for(tmp_path, "cache_" + change)
    driver = CaseRun(root, monkeypatch)
    _, calls = driver.run()
    initial_case = [call for call in calls if call["stage"] == "case_groups"]
    assert len(initial_case) == 1
    before = driver.batch()
    dump(root / "BEFORE_BATCH.json", before)
    if change == "model":
        driver.adapter.chapter_model = "offline-case-model-b"
    elif change == "output_tokens":
        driver.adapter.output_tokens = 9000
    elif change == "inactive_thinking":
        driver.adapter.thinking_budget = 4096  # case adapter always uses 2048
    elif change == "prompt":
        old = planning._planner_instructions
        monkeypatch.setattr(planning, "_planner_instructions", lambda stage, **kwargs:
                            old(stage, **kwargs) + ("\nCASE_TEST_CHANGED_PROMPT" if stage == "case_groups" else ""))
    elif change == "material":
        driver.planner._read_materials["paper-2"]["question_material"][0]["explanation"] = "UPDATED_CASE_ANSWER"
    elif change == "clipped_suffix":
        driver.planner._read_materials["paper-2"]["question_material"][0]["explanation"] += " UPDATED_UNSEEN_SUFFIX"
    elif change in {"legacy_batch", "failed_batch"}:
        mutated = dict(before)
        if change == "legacy_batch":
            mutated.pop("input_contract", None)
        else:
            mutated["status"] = "failed"
        dump(root / "run/stages/case_groups/CH01__batch_001_of_001.json", mutated)
    elif change == "metadata_only":
        driver.planner._read_materials["paper-2"]["output_path"] = str(root / "moved" / "READING.json")
    _, resumed_calls = driver.run(resume=True)
    actual = [call for call in resumed_calls if call["stage"] == "case_groups"]
    after = driver.batch()
    dump(root / "CACHE_RESULT.json", {"change": change, "case_calls": len(actual), "expected_case_calls": expected_case_calls,
                                     "before": before, "after": after,
                                     "initial_effective_settings": {key: initial_case[0][key] for key in ("model", "output_tokens", "thinking_budget")},
                                     "resumed_case_calls": actual})
    assert len(actual) == expected_case_calls
    if actual:
        assert actual[0]["thinking_budget"] == 2048
    if change == "material":
        assert "UPDATED_CASE_ANSWER" in json.dumps(actual[0]["payload"]["source_materials"])
    if change == "clipped_suffix":
        assert before["material_signature"] == after["material_signature"]
        assert "UPDATED_UNSEEN_SUFFIX" in json.dumps(read(root / "run/writer_packets/CH01.json"))
        _, messages = writer_handoff(root, name="suffix_resumed")
        assert "UPDATED_UNSEEN_SUFFIX" in json.dumps(messages)



def test_conflicting_independent_deep_is_unavailable_even_if_case_model_selects_it(tmp_path, monkeypatch):
    root = root_for(tmp_path, "independent_deep_conflict")
    driver = CaseRun(root, monkeypatch)
    driver.planner.prior_readings[0]["doi"] = "10.1000/different-study"
    _, calls = driver.run()
    case = next(call for call in calls if call["stage"] == "case_groups")
    candidate = next(row for row in case["payload"]["source_materials"] if row["source_handle"] == "P0002")
    assert candidate["material_available"] is False
    assert "DEEP_ONLY_ANSWER" not in json.dumps(candidate)
    packet = read(root / "run/writer_packets/CH01.json")
    assert not packet["chapter_plan"]["units"][0]["supporting_studies"]
    assert "P0002" not in [row.get("source_handle") for row in packet["source_materials"]]


def test_case_append_preserves_existing_chapter_specific_reading():
    records = [{
        "chapter": {"chapter_id": chapter_id},
        "chapter_plan": {"units": [{"unit_id": chapter_id + "_U1", "source_handles": ["P0001"]}]},
        "source_materials": [{"source_handle": "P0001", "paper_id": "paper-1", "title": "Same study",
            "deep_read_material": {"question_material": [{"explanation": content}]}}],
    } for chapter_id, content in [("CH01", "CHAPTER_ONE_CONTEXT"), ("CH02", "CHAPTER_TWO_CONTEXT")]]
    result = planning._attach_case_groups(records, {"additions": [{"unit_key": "CH02:1", "studies": [{
        "source_handle": "P0001", "contribution": "Use the same study with chapter-specific conditions",
    }]}]}, planning_revision=True, body_case_additions=True)
    assert result[1]["source_materials"] == records[1]["source_materials"]
    assert result[1]["chapter_plan"]["units"][0]["supporting_studies"]


def test_case_append_upgrades_thin_target_without_losing_local_metadata():
    records = [{"chapter": {"chapter_id": "CH01"}, "chapter_plan": {"units": [{"unit_id": "U1"}]},
        "source_materials": [{"source_handle": "P0002", "paper_id": "paper-2", "doi": "10.1000/case-2",
                              "source_role": "local_navigation", "local_context": "Keep the original placement"}]}]
    candidate = {"_source_handle": "P0002", "_paper_id": "paper-2", "doi": "10.1000/case-2",
                 "planning_view": {"paper_identity": {"paper_id": "paper-2", "doi": "10.1000/case-2"}}}
    result = planning._attach_case_groups(records, {"additions": [{"unit_key": "CH01:1", "studies": [{
        "source_handle": "P0002", "contribution": "Use the independent source",
    }]}]}, planning_revision=True, body_case_additions=True, candidate_rows=[candidate],
        read_materials={"paper-2": {"paper_id": "paper-2", "question_material": [{"explanation": "INDEPENDENT_READING"}]}})
    source = result[0]["source_materials"][0]
    assert source["source_role"] == "local_navigation"
    assert source["local_context"] == "Keep the original placement"
    assert "INDEPENDENT_READING" in json.dumps(source)
    assert result[0]["chapter_plan"]["units"][0]["supporting_studies"]
