"""Offline, full-BODY baseline contracts, accounting, cache and draft recovery."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from optomind_research.runtime.upgrade3 import fullbody_writer as engine
from optomind_research.runtime.upgrade3.fullbody_contracts import build_fullbody_input, fullbody_task_catalog
from optomind_research.runtime.upgrade3.writer_candidates_contracts import INPUT_SCHEMA


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Plain-route tests may not use a network")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def book():
    chapters = []
    for index in range(1, 4):
        source = f"P{index:04d}"
        chapters.append({"schema_version": INPUT_SCHEMA, "chapter_id": f"C{index}", "language": "en",
            "chapter_frame": {"research_question": "Which observations support these claims?", "title": f"Chapter {index}"},
            "other_chapters": [], "units": [{"unit_id": "U", "focus": "Respect observed conditions",
                "paragraph_tasks": [{"paragraph_id": f"T{part}", "point": f"ORIGINAL_TASK_{index}_{part}",
                    "source_handles": [source], "conditions": f"CONDITION_{index}_{part}"} for part in (1, 2)],
                "table_tasks": [], "owner_unit_context": {}, "source_handles": [source]}],
            "sources": [{"source_handle": source, "paper_id": f"paper-{index}",
                "study_summary_A": {"finding": f"Complete evidence {index}", "tail": f"INTACT_SOURCE_TAIL_{index}"}}],
            "chapter_tool_materials": [], "provenance": {}, "warnings": []})
    return build_fullbody_input(chapters, user_request="Write the entire approved BODY", target_reader="New researcher",
                                review_scope="All three accepted chapters")


@pytest.fixture
def config():
    profile = {"model": "qwen3.5-plus", "thinking": True, "thinking_budget": 1024,
        "max_output_tokens": 4096, "stream": True, "json_mode": False,
        "timeout_seconds": 15, "stream_overall_timeout_seconds": 30,
        "prompt_token_multiplier": 1.0, "prompt_token_framing_margin": 0}
    return {"writer": deepcopy(profile), "reader": deepcopy(profile), "reviser": deepcopy(profile)}


def meter(raw, messages):
    payload = json.loads(messages[-1]["content"])
    return 100 + 100 * len(payload.get("editable_task_ids", [])) + len(payload.get("original_body_markdown", "")) // 10


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Factory:
    execution_mode = "recording"
    fixture_sha256 = "plain-fullbody-fixture-1"

    def __init__(self, mode="normal"):
        self.mode, self.calls = mode, []

    def __call__(self, role, directory, profile):
        def call(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.calls.append({"role": role, "payload": payload, "messages": messages, "kwargs": kwargs, "profile": profile})
            assert (directory / "MESSAGES.json").exists()
            assert (directory / "ACTUAL_REQUEST.json").exists()
            assert kwargs["max_output_tokens"] == profile["max_output_tokens"]
            ids = payload["editable_task_ids"]
            response = {"complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 111, "completion_tokens": 222}}
            if role == "reviser":
                output = directory.parents[3]
                draft = read(output / "INDEPENDENT_FULL_BODY_RESULT.json")
                assert draft["complete"] and draft["body_markdown"] == payload["original_body_markdown"]
                if self.mode == "editor_fail":
                    raise RuntimeError("Controlled full-editor failure")
                body = "An integrated replacement for the complete BODY. " + " ".join(ids) + " [P0001] [P0002] [P0003]"
            else:
                if self.mode == "chapter_fail" and payload["current_chapter_ids"] == ["C2"]:
                    raise RuntimeError("Controlled missing chapter")
                body = "INDEPENDENT_PROSE_" + "_".join(payload["current_chapter_ids"]) + " " + ",".join(ids) + " " + " ".join(
                    "[" + row["source_handle"] + "]" for row in payload["sources"])
            if self.mode == "editor_incomplete" and role == "reviser":
                ids = ids[:-1]
            obj = {"body_markdown": body, "completed_task_ids": ids, "complete": True}
            if self.mode == "bare":
                response["content"] = body
            elif self.mode == "length":
                obj.update(complete=False)
                response.update(complete=False, finish_reason="length", content=json.dumps(obj))
            else:
                response["content"] = json.dumps(obj)
            return response
        return call


def execute(tmp_path, book, config, factory=None, route="chapter_concat", **kwargs):
    return engine.run_fullbody_candidate(book, route=route, output_dir=tmp_path / "out", config=config,
        client_factory=factory, run=True, token_counter=meter, **kwargs)


def make_base(tmp_path, book, config):
    return engine.run_fullbody_candidate(book, route="chapter_concat", output_dir=tmp_path / "base", config=config,
        client_factory=Factory(), run=True, token_counter=meter)


def test_plain_whole_one_full_body_call_same_complete_inputs(tmp_path, book, config):
    factory = Factory()
    result = execute(tmp_path, book, config, factory, "plain_whole")
    assert result["complete"], result
    assert len(factory.calls) == result["model_calls"] == 1
    call = factory.calls[0]
    assert call["payload"]["editable_task_ids"] == list(fullbody_task_catalog(book))
    assert all(f"INTACT_SOURCE_TAIL_{index}" in json.dumps(call["payload"]) for index in (1, 2, 3))
    assert call["payload"]["user_request"] == book["user_request"]
    assert call["payload"]["target_reader"] == book["target_reader"]
    assert call["payload"]["review_scope"] == book["review_scope"]
    assert call["messages"][0]["content"] == engine._prompt("plain_writer", "plain_whole")
    assert engine._prompt("writer") not in call["messages"][0]["content"]
    assert result["approved_chapter_order"] == ["C1", "C2", "C3"]
    assert not result["global_integration_performed"]


def test_chapter_concat_independent_complete_chapters_full_plan_no_prior_prose(tmp_path, book, config):
    factory = Factory()
    config["max_tasks_per_window"] = 1  # Advanced quality knob cannot fragment this baseline.
    result = execute(tmp_path, book, config, factory)
    assert result["complete"] and result["model_calls"] == 3
    for index, call in enumerate(factory.calls, 1):
        payload = call["payload"]
        assert payload["current_chapter_ids"] == [f"C{index}"]
        assert len(payload["editable_task_ids"]) == 2
        assert "accepted_body_markdown" not in payload and "manuscript_navigation" not in payload
        assert "INDEPENDENT_PROSE_" not in json.dumps(payload)
        assert not payload["preceding_prose_included"]
        assert f"INTACT_SOURCE_TAIL_{index}" in json.dumps(payload["sources"])
        assert all(f"ORIGINAL_TASK_{chapter}_{part}" in json.dumps(payload) for chapter in (1, 2, 3) for part in (1, 2))
        assert all(f"CONDITION_{chapter}_{part}" in json.dumps(payload) for chapter in (1, 2, 3) for part in (1, 2))
        assert call["messages"][0]["content"] == engine._prompt("plain_writer", "chapter_concat")
    assert result["body_markdown"] == "\n\n".join(segment["body_markdown"] for segment in result["segments"])
    assert result["selected_kind"] == "independent_chapter_concatenation"
    assert not result["global_integration_performed"]
    draft = read(result["independent_draft_result_path"])
    assert draft["complete"] and draft["effective_route"] == "chapter_concat"
    assert draft["body_markdown"] == result["body_markdown"]


def test_independent_preview_exposes_all_real_requests_without_calls(tmp_path, book, config):
    def forbidden(*args):
        pytest.fail("Preview must not construct a provider")
    result = engine.run_fullbody_candidate(book, route="hierarchical_full", output_dir=tmp_path,
        config=config, client_factory=forbidden, token_counter=meter)
    assert result["status"] == "preview" and len(result["stages"]) == 3
    assert all(stage["status"] == "planned" for stage in result["stages"])
    assert result["model_calls"] == 0 and result["body_markdown"] == ""
    assert result["pending_task_ids"] == list(fullbody_task_catalog(book))
    assert result["integration_status"] == "awaiting_complete_draft"
    assert not (tmp_path / "INDEPENDENT_FULL_BODY_RESULT.json").exists()
    assert read(tmp_path / "RUN_MANIFEST.json")["not_executed"][-1]["reason"] == "requires_complete_independent_full_body_draft"


def test_chapter_splits_only_at_intact_tasks_for_actual_input_capacity(tmp_path, book, config):
    config["max_input_tokens"] = 250
    factory = Factory()
    result = execute(tmp_path, book, config, factory)
    assert result["complete"] and len(factory.calls) == 6
    assert all(len(call["payload"]["editable_task_ids"]) == 1 for call in factory.calls)
    assert [call["payload"]["current_chapter_ids"] for call in factory.calls] == [["C1"], ["C1"], ["C2"], ["C2"], ["C3"], ["C3"]]
    assert all("INDEPENDENT_PROSE_" not in json.dumps(call["payload"]) for call in factory.calls)


def test_unsplittable_task_retains_full_input_and_dispatches_nothing(tmp_path, book, config):
    config["max_input_tokens"] = 150
    factory = Factory()
    result = execute(tmp_path, book, config, factory)
    assert not result["complete"] and not factory.calls
    stage = result["stages"][0]
    assert stage["status"] == "capacity_blocked" and "intact task" in stage["required_action"]
    payload = read(stage["messages_path"])[-1]["content"]
    assert "INTACT_SOURCE_TAIL_1" in payload and "ORIGINAL_TASK_3_2" in payload


def test_hierarchy_generates_all_chapters_then_exactly_one_complete_editor(tmp_path, book, config):
    factory = Factory()
    result = execute(tmp_path, book, config, factory, "hierarchical_full")
    assert result["complete"], result
    assert [call["role"] for call in factory.calls] == ["writer", "writer", "writer", "reviser"]
    draft = read(result["independent_draft_result_path"])
    editor = factory.calls[-1]
    assert editor["payload"]["original_body_markdown"] == draft["body_markdown"]
    assert editor["payload"]["editable_task_ids"] == list(fullbody_task_catalog(book))
    assert all(f"INTACT_SOURCE_TAIL_{index}" in json.dumps(editor["payload"]) for index in (1, 2, 3))
    assert editor["messages"][0]["content"] == engine._prompt("plain_writer", "hierarchical_full")
    assert editor["payload"]["editing_scope"] == "entire_original_full_body"
    assert editor["payload"]["output_scope"] == "complete_replacement_body"
    assert editor["profile"]["model"] == config["reviser"]["model"]
    assert result["body_markdown"].startswith("An integrated replacement")
    assert result["body_markdown"] != draft["body_markdown"]
    assert result["integration_complete"] and not result["integration_pending"]
    assert len(result["segments"]) == 1
    assert engine._join(result["segments"]) == result["body_markdown"]
    assert len(result["cost_summary"]["all_attempts_including_retries"]) == 4
    assert len(draft["cost_summary"]["all_attempts_including_retries"]) == 3


def test_hierarchy_reuses_complete_draft_editor_only_and_no_double_charging(tmp_path, book, config):
    base = make_base(tmp_path, book, config)
    factory = Factory()
    result = execute(tmp_path, book, config, factory, "hierarchical_full", base_result=base)
    assert result["complete"] and [call["role"] for call in factory.calls] == ["reviser"]
    assert result["cost_summary"]["base_cost_attribution"] == base["cost_summary"]
    assert result["cost_summary"]["base_reuse_charged_again"] is False
    assert result["cost_summary"]["total_known_cost_including_base_cny"] == pytest.approx(
        result["cost_summary"]["candidate_stage_known_cost_cny"] + base["cost_summary"]["total_known_cost_including_base_cny"])
    again = execute(tmp_path, book, config, factory, "hierarchical_full", base_result=base)
    assert again["complete"] and again["model_calls"] == 0 and len(factory.calls) == 1
    config["reviser"]["thinking_budget"] += 1
    changed = execute(tmp_path, book, config, factory, "hierarchical_full", base_result=base)
    assert changed["complete"] and changed["model_calls"] == 1 and len(factory.calls) == 2
    assert all(call["role"] == "reviser" for call in factory.calls)


def test_hierarchy_and_concat_share_independent_cache_without_explicit_base(tmp_path, book, config):
    factory = Factory()
    concatenated = execute(tmp_path, book, config, factory)
    hierarchy = execute(tmp_path, book, config, factory, "hierarchical_full")
    assert hierarchy["complete"] and len(factory.calls) == 4
    assert hierarchy["model_calls"] == 1
    assert [stage.get("cache_hit") for stage in hierarchy["stages"]] == [True, True, True, False]
    assert read(hierarchy["independent_draft_result_path"])["body_markdown"] == concatenated["body_markdown"]
    resumed = execute(tmp_path, book, config, factory, "hierarchical_full")
    assert resumed["complete"] and resumed["model_calls"] == 0 and len(factory.calls) == 4


def test_changed_early_chapter_material_does_not_repurchase_later_independent_drafts(tmp_path, book, config):
    factory = Factory()
    execute(tmp_path, book, config, factory)
    chapters = deepcopy(book["chapters"])
    chapters[0]["sources"][0]["study_summary_A"]["tail"] = "Changed first evidence, same later materials"
    changed = build_fullbody_input(chapters, user_request=book["user_request"], target_reader=book["target_reader"], review_scope=book["review_scope"])
    result = execute(tmp_path, changed, config, factory)
    assert result["complete"] and len(factory.calls) == 4
    assert [stage["cache_hit"] for stage in result["stages"]] == [False, True, True]
    assert all(not stage["dependencies"] for stage in result["stages"])


@pytest.mark.parametrize("mode", ["editor_fail", "editor_incomplete"])
def test_failed_or_incomplete_editor_preserves_complete_draft_and_requires_explicit_retry(tmp_path, book, config, mode):
    factory = Factory(mode)
    failed = execute(tmp_path, book, config, factory, "hierarchical_full")
    assert not failed["complete"] and failed["body_complete"] and failed["integration_pending"]
    draft = read(failed["independent_draft_result_path"])
    assert failed["body_markdown"] == draft["body_markdown"]
    assert failed["pending_task_ids"] == [] and draft["complete"]
    assert read(tmp_path / "out/FULL_BODY_RESULT.json")["body_markdown"] == draft["body_markdown"]
    assert "--retry-failed" in failed["required_action"]
    factory.mode = "normal"
    held = execute(tmp_path, book, config, factory, "hierarchical_full")
    assert held["integration_pending"] and len(factory.calls) == 4
    recovered = execute(tmp_path, book, config, factory, "hierarchical_full", retry_failed=True)
    assert recovered["complete"] and len(factory.calls) == 5
    assert len(recovered["cost_summary"]["all_attempts_including_retries"]) == 5


def test_full_editor_capacity_never_silently_windows_or_loses_assembled_draft(tmp_path, book, config):
    config["max_input_tokens"] = 400
    factory = Factory()
    result = execute(tmp_path, book, config, factory, "hierarchical_full")
    assert not result["complete"] and result["body_complete"] and len(factory.calls) == 3
    assert result["stages"][-1]["status"] == "capacity_blocked"
    assert result["stages"][-1]["stage_id"] == "integrate_full_body"
    assert "complete assembled BODY" in result["required_action"]
    assert result["body_markdown"] == read(result["independent_draft_result_path"])["body_markdown"]
    assert not any(call["role"] == "reviser" for call in factory.calls)


def test_whole_and_editor_enforce_explicit_output_capacity_without_lowering_allowance(tmp_path, book, config):
    base = make_base(tmp_path, book, config)
    config["whole_body_output_tokens"] = 4097
    factory = Factory()
    whole = execute(tmp_path, book, config, factory, "plain_whole")
    assert whole["stages"][0]["output_capacity_blocked"] and not factory.calls
    edited = execute(tmp_path, book, config, factory, "hierarchical_full", base_result=base)
    assert edited["body_complete"] and edited["integration_pending"] and not factory.calls
    assert edited["stages"][-1]["output_capacity_blocked"]
    assert edited["stages"][-1]["effective_profile"]["max_output_tokens"] == 4096


def test_missing_chapter_is_pending_not_a_complete_body_or_editor_input(tmp_path, book, config):
    factory = Factory("chapter_fail")
    result = execute(tmp_path, book, config, factory, "hierarchical_full")
    assert not result["complete"] and not result["body_complete"]
    assert result["integration_status"] == "awaiting_complete_draft"
    assert result["completed_task_ids"] == [key for key in fullbody_task_catalog(book) if key.startswith("C1::")]
    assert all(key.startswith(("C2::", "C3::")) for key in result["pending_task_ids"])
    assert [call["role"] for call in factory.calls] == ["writer", "writer"]
    assert not result["independent_draft_result_path"]
    assert not (tmp_path / "out/INDEPENDENT_FULL_BODY_RESULT.json").exists()


@pytest.mark.parametrize("mutation", ["partial", "foreign", "wrong_route", "missing_chapter", "wrong_order", "tampered_text"])
def test_hierarchy_rejects_unusable_base_without_writer_or_editor_calls(tmp_path, book, config, mutation):
    base = make_base(tmp_path, book, config)
    if mutation == "partial":
        base["complete"] = False
    elif mutation == "foreign":
        base["input_hash"] = "foreign"
    elif mutation == "wrong_route":
        base["effective_route"] = "whole_author"
    elif mutation == "missing_chapter":
        base["completed_task_ids"] = base["completed_task_ids"][:-2]
    elif mutation == "wrong_order":
        base["approved_chapter_order"] = list(reversed(base["approved_chapter_order"]))
    else:
        base["body_markdown"] += "Changed after approval"
    factory = Factory()
    result = execute(tmp_path, book, config, factory, "hierarchical_full", base_result=base)
    assert result["status"] == "blocked" and not factory.calls and result["model_calls"] == 0


def test_bare_prose_and_length_never_infer_full_body_completion(tmp_path, book, config):
    for mode in ("bare", "length"):
        factory = Factory(mode)
        result = engine.run_fullbody_candidate(book, route="plain_whole", output_dir=tmp_path / mode,
            config=config, client_factory=factory, run=True, token_counter=meter)
        assert not result["complete"] and result["body_markdown"]
        assert result["pending_task_ids"] == list(fullbody_task_catalog(book))
        assert len(factory.calls) == 1


def test_plain_raw_recovery_never_reissues_complete_call(tmp_path, book, config):
    factory = Factory()
    first = execute(tmp_path, book, config, factory, "plain_whole")
    attempt = Path(first["stages"][0]["attempt_dir"])
    (attempt / "RESULT.json").unlink()
    resumed = execute(tmp_path, book, config, factory, "plain_whole")
    assert resumed["complete"] and len(factory.calls) == 1 and resumed["model_calls"] == 0
    assert (attempt / "RESULT.json").exists()


def test_editor_planning_error_still_leaves_reusable_complete_artifact(tmp_path, book, config, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("Controlled editor preparation failure")
    monkeypatch.setattr(engine, "_integration_stage", fail)
    result = execute(tmp_path, book, config, Factory(), "hierarchical_full")
    assert result["status"] == "blocked" and result["body_complete"] and result["integration_pending"]
    draft = read(result["independent_draft_result_path"])
    assert draft["complete"] and result["body_markdown"] == draft["body_markdown"]
    assert engine._independent_base_validate(draft, book, engine._hash(book))["complete"]
    assert Path(draft["source_identity_map_path"]).is_file()
    assert Path(draft["input_path"]).is_file()
    assert draft["source_identity_map_sha256"] == result["source_identity_map_sha256"]


def test_all_complementary_source_records_and_alias_identity_reach_full_editor(tmp_path, book, config):
    chapters = deepcopy(book["chapters"])
    chapters[0]["sources"][0]["aliases"] = ["old-paper-1"]
    chapters[0]["units"][0]["paragraph_tasks"][0]["source_handles"] = ["old-paper-1"]
    complementary = deepcopy(chapters[0]["sources"][0])
    complementary.pop("study_summary_A")
    complementary["study_summary_B"] = {"boundary": "COMPLEMENTARY_REVIEW_MEDIATED_CONDITION", "provenance_type": "review-mediated"}
    chapters[2]["sources"].append(complementary)
    book = build_fullbody_input(chapters, user_request=book["user_request"], target_reader=book["target_reader"], review_scope=book["review_scope"])
    factory = Factory()
    result = execute(tmp_path, book, config, factory, "hierarchical_full")
    assert result["complete"], result
    for index in (0, -1):
        payload = factory.calls[index]["payload"]
        assert payload["source_aliases"]["old-paper-1"] == "P0001"
        records = [row for row in payload["sources"] if row["source_handle"] == "P0001"]
        assert len(records) == 2
        assert "INTACT_SOURCE_TAIL_1" in json.dumps(records)
        assert "COMPLEMENTARY_REVIEW_MEDIATED_CONDITION" in json.dumps(records)
    assert result["source_identity_map_sha256"] == read(result["independent_draft_result_path"])["source_identity_map_sha256"]


@pytest.mark.parametrize("route", engine.PLAIN_ROUTES)
def test_plain_routes_reject_prefix_continuation_before_any_dispatch(tmp_path, book, config, route):
    factory = Factory()
    result = execute(tmp_path, book, config, factory, route, continue_incomplete=True)
    assert result["status"] == "blocked" and result["model_calls"] == 0 and not factory.calls
    assert "use continuous_author or workbench for prefix continuation" in result["issues"][0]["error"]
