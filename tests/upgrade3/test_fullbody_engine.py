"""Offline full-BODY execution, continuation, reader revision and recovery."""
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
        raise AssertionError("No test may make a network request")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def book():
    chapters = []
    for index in range(1, 4):
        source = f"P{index:04d}"
        chapters.append({"schema_version": INPUT_SCHEMA, "chapter_id": f"C{index}", "language": "en",
            "chapter_frame": {"research_question": "How do relationships explain observations?", "title": f"Chapter {index}"},
            "other_chapters": [], "units": [{"unit_id": "U", "focus": "Explain with conditions",
                "paragraph_tasks": [{"paragraph_id": f"T{part}", "point": f"Complete original task {index}/{part}",
                    "source_handles": [source], "conditions": "Preserve this important boundary"} for part in (1, 2)],
                "table_tasks": [], "owner_unit_context": {}, "source_handles": [source]}],
            "sources": [{"source_handle": source, "paper_id": f"paper-{index}",
                "study_summary_A": {"finding": f"Full evidence {index}", "tail": f"INTACT_SOURCE_TAIL_{index}"}}],
            "chapter_tool_materials": [], "provenance": {}, "warnings": []})
    return build_fullbody_input(chapters)


@pytest.fixture
def config():
    profile = {"model": "qwen3.5-plus", "thinking": True, "thinking_budget": 1024,
        "max_output_tokens": 4096, "stream": True, "json_mode": False,
        "timeout_seconds": 15, "stream_overall_timeout_seconds": 30,
        "prompt_token_multiplier": 1.0, "prompt_token_framing_margin": 0}
    return {"writer": deepcopy(profile), "reader": deepcopy(profile), "reviser": deepcopy(profile)}


def meter(raw, messages):
    payload = json.loads(messages[-1]["content"])
    return 100 + 100 * len(payload.get("editable_task_ids", [])) + len(payload.get("accepted_body_markdown", "")) // 20 + 100 * len(payload.get("reread_segments", []))


def read(path):
    return json.loads(Path(path).read_text())


class Factory:
    execution_mode = "recording"
    fixture_sha256 = "fullbody-engine-fixture-1"

    def __init__(self, mode="normal"):
        self.mode = mode
        self.calls = []

    def __call__(self, role, directory, profile):
        def call(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.calls.append({"role": role, "payload": payload, "messages": messages, "kwargs": kwargs, "profile": profile})
            assert (directory / "MESSAGES.json").exists()
            assert (directory / "ACTUAL_REQUEST.json").exists()
            assert kwargs["stream"] and kwargs["max_output_tokens"] == profile["max_output_tokens"]
            response = {"complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 111, "completion_tokens": 222}}
            if role == "reader":
                if self.mode == "reader_fail":
                    raise RuntimeError("controlled reader failure")
                issue = {"issue_id": "I1", "anchor": "Relationships are explained carefully",
                         "problem": "One connection is implicit", "reader_understanding": "I cannot reconstruct this connection",
                         "suggested_action": "Explain the observation boundary"}
                obj = {"issues": [] if self.mode == "no_issues" else [issue], "understanding": "A connected BODY", "complete": True}
            elif role == "reviser":
                if self.mode == "revision_fail":
                    raise RuntimeError("controlled revision failure")
                issue = payload["reader_issues"][0]
                obj = {"patches": [{"issue_id": issue["issue_id"], "anchor": issue["anchor"],
                                    "replacement": "Relationships are explained with their observation boundary"}],
                       "rejected_issues": [], "complete": True}
            else:
                ids = payload["editable_task_ids"]
                nav = payload.get("manuscript_navigation", [])
                if self.mode == "workbench_current":
                    obj = {"read_source_handles": [payload["sources"][0]["source_handle"]]}
                elif self.mode == "workbench_read" and len(nav) == 2 and not payload.get("reread_segments"):
                    obj = {"read_segment_ids": [nav[0]["segment_id"]], "read_source_handles": ["P0001"]}
                elif self.mode == "workbench_unknown" and nav:
                    obj = {"read_segment_ids": ["unknown"]}
                elif self.mode == "workbench_repeat" and nav:
                    obj = {"read_segment_ids": [nav[0]["segment_id"]]}
                elif self.mode == "fail":
                    raise RuntimeError("controlled uncertain failure")
                else:
                    prefix = "Relationships are explained carefully" if not nav else "Building on the accepted argument"
                    source = payload["sources"][0]["source_handle"]
                    content = f"{prefix} for {','.join(ids)}. [{source}]"
                    if self.mode == "length" and not payload.get("output_continuation"):
                        obj = {"body_markdown": "The exact unfinished prefix ", "complete": False}
                        response.update(complete=False, finish_reason="length")
                    elif self.mode == "length":
                        obj = {"body_markdown": "continues into a completed explanation.", "completed_task_ids": ids, "complete": True}
                    elif self.mode == "bare":
                        obj = content
                    else:
                        obj = {"body_markdown": content, "completed_task_ids": ids, "complete": True}
            response["content"] = obj if isinstance(obj, str) else json.dumps(obj)
            return response
        return call


def execute(tmp_path, book, config, factory=None, route="continuous_author", **kwargs):
    return engine.run_fullbody_candidate(book, route=route, output_dir=tmp_path / "out", config=config,
        client_factory=factory, run=True, token_counter=meter, **kwargs)


def test_whole_author_one_complete_body_call(tmp_path, book, config):
    factory = Factory()
    result = execute(tmp_path, book, config, factory, "whole_author")
    assert result["complete"], result
    assert len(factory.calls) == 1
    assert result["completed_task_ids"] == list(fullbody_task_catalog(book))
    assert factory.calls[0]["payload"]["accepted_body_markdown"] == ""
    assert "INTACT_SOURCE_TAIL_3" in json.dumps(factory.calls[0]["payload"])
    assert not result["reader_revision_complete"]


def test_continuous_author_uses_actual_all_prefix_full_plan_and_current_evidence(tmp_path, book, config):
    factory = Factory()
    result = execute(tmp_path, book, config, factory)
    assert result["complete"] and len(factory.calls) == 3
    for index, call in enumerate(factory.calls):
        payload = call["payload"]
        assert payload["accepted_body_markdown"] == engine._join(result["segments"][:index])
        serialized = json.dumps(payload)
        assert all(f"Complete original task {chapter}/1" in serialized for chapter in (1, 2, 3))
        assert f"INTACT_SOURCE_TAIL_{index + 1}" in json.dumps(payload["sources"])
        assert all("body_markdown" not in row for row in payload["recent_prose_segments"])
    assert "Building on the accepted argument" in result["body_markdown"]
    assert len(result["segments"]) == 3


def test_preview_does_not_construct_clients_or_invent_later_prose(tmp_path, book, config):
    def forbidden(*args):
        pytest.fail("preview must not construct clients")
    result = engine.run_fullbody_candidate(book, route="continuous_author", output_dir=tmp_path,
        config=config, client_factory=forbidden, token_counter=meter)
    assert result["status"] == "preview"
    assert len(result["stages"]) == 1
    manifest = read(tmp_path / "RUN_MANIFEST.json")
    assert manifest["not_executed"][0]["reason"] == "later_requests_require_actual_accepted_prose"


def test_whole_input_and_output_capacity_block_before_call(tmp_path, book, config):
    factory = Factory()
    config["max_input_tokens"] = 300
    result = execute(tmp_path, book, config, factory, "whole_author")
    assert not factory.calls and result["stages"][0]["status"] == "capacity_blocked"
    assert "continuous_author" in result["stages"][0]["required_action"]
    config.pop("max_input_tokens")
    config["whole_body_output_tokens"] = 4097
    result = execute(tmp_path, book, config, factory, "whole_author")
    assert not factory.calls and result["stages"][0]["output_capacity_blocked"]
    assert result["effective_route"] == "whole_author"


def test_capacity_splits_complete_tasks_and_explicit_quality_boundary(tmp_path, book, config):
    config["max_tasks_per_window"] = 1
    factory = Factory()
    result = execute(tmp_path, book, config, factory)
    assert result["complete"] and len(factory.calls) == 6
    assert all(len(call["payload"]["editable_task_ids"]) == 1 for call in factory.calls)


def test_workbench_reads_remote_full_prose_and_full_original_source(tmp_path, book, config):
    config["recent_prose_segments"] = 1
    factory = Factory("workbench_read")
    result = execute(tmp_path, book, config, factory, "workbench")
    assert result["complete"] and len(factory.calls) == 4, result
    request, reread = factory.calls[-2]["payload"], factory.calls[-1]["payload"]
    assert request["accepted_body_markdown"] == result["segments"][1]["body_markdown"]
    assert reread["reread_segments"][0]["body_markdown"] == result["segments"][0]["body_markdown"]
    assert reread["reread_sources"][0]["source_handle"] == "P0001"
    assert reread["reread_sources"][0]["full_record_location"] == "sources"
    assert "INTACT_SOURCE_TAIL_1" in json.dumps(reread["sources"])
    assert len(reread["shared_fullbody_context"]["chapters"]) == 3
    assert len(result["cost_summary"]["all_attempts_including_retries"]) == 4


@pytest.mark.parametrize("mode,status,calls", [("workbench_unknown", "invalid_reread_request", 2),
    ("workbench_repeat", "reread_no_progress", 2)])
def test_workbench_invalid_and_repeated_reads_stop_without_hidden_loop(tmp_path, book, config, mode, status, calls):
    factory = Factory(mode)
    result = execute(tmp_path, book, config, factory, "workbench")
    assert not result["complete"] and len(factory.calls) == calls
    assert result["stages"][-1]["status"] == status
    assert result["body_markdown"]


def test_resume_reuses_all_cached_continuation_calls_with_stable_paths(tmp_path, book, config):
    factory = Factory()
    first = execute(tmp_path, book, config, factory)
    second = execute(tmp_path, book, config, factory)
    assert second["complete"] and len(factory.calls) == 3
    assert all(stage["cache_hit"] for stage in second["stages"])
    assert first["body_markdown"] == second["body_markdown"]
    assert [row["full_text_path"] for row in first["segments"]] == [row["full_text_path"] for row in second["segments"]]


def test_raw_recovery_and_valid_attempt_beats_later_uncertain_attempt(tmp_path, book, config):
    factory = Factory()
    first = execute(tmp_path, book, config, factory, "whole_author")
    attempt = Path(first["stages"][0]["attempt_dir"])
    (attempt / "RESULT.json").unlink()
    (attempt.parent / "attempt_002").mkdir()
    second = execute(tmp_path, book, config, factory, "whole_author")
    assert second["complete"] and len(factory.calls) == 1
    assert (attempt / "RESULT.json").exists()


def test_uncertain_failure_requires_explicit_retry(tmp_path, book, config):
    factory = Factory("fail")
    first = execute(tmp_path, book, config, factory)
    assert not first["complete"] and len(factory.calls) == 1
    factory.mode = "normal"
    held = execute(tmp_path, book, config, factory)
    assert not held["complete"] and len(factory.calls) == 1
    success = execute(tmp_path, book, config, factory, retry_failed=True)
    assert success["complete"] and len(factory.calls) == 4
    assert success["cost_summary"]["unknown_cost_attempts"]


def test_length_partial_retained_then_explicitly_continues_exact_prefix(tmp_path, book, config):
    factory = Factory("length")
    first = execute(tmp_path, book, config, factory)
    assert not first["complete"] and first["body_markdown"] == "The exact unfinished prefix "
    resumed = execute(tmp_path, book, config, factory, continue_incomplete=True)
    assert resumed["complete"], resumed
    assert len(factory.calls) == 6
    assert factory.calls[1]["payload"]["current_scope_partial_body_markdown"] == "The exact unfinished prefix "
    assert factory.calls[1]["payload"]["accepted_body_markdown"] == ""
    assert resumed["body_markdown"].count("The exact unfinished prefix continues") == 3
    again = execute(tmp_path, book, config, factory, continue_incomplete=True)
    assert again["complete"] and len(factory.calls) == 6


def test_length_whole_author_does_not_silently_switch_or_call_again(tmp_path, book, config):
    factory = Factory("length")
    result = execute(tmp_path, book, config, factory, "whole_author", continue_incomplete=True)
    assert not result["complete"] and len(factory.calls) == 1
    assert result["effective_route"] == "whole_author"


def test_changed_future_evidence_does_not_repay_unchanged_previous_window(tmp_path, book, config):
    factory = Factory()
    first = execute(tmp_path, book, config, factory)
    chapters = deepcopy(book["chapters"])
    chapters[-1]["sources"][0]["study_summary_A"]["tail"] = "CHANGED_FUTURE_EVIDENCE"
    changed = build_fullbody_input(chapters)
    second = execute(tmp_path, changed, config, factory)
    assert second["complete"] and len(factory.calls) == 4
    assert [stage["cache_hit"] for stage in second["stages"]] == [True, True, False]
    assert first["input_hash"] != second["input_hash"]


def test_changed_effective_profile_invalidates_paid_stage_cache(tmp_path, book, config):
    factory = Factory()
    execute(tmp_path, book, config, factory, "whole_author")
    config["writer"]["thinking_budget"] += 1
    execute(tmp_path, book, config, factory, "whole_author")
    assert len(factory.calls) == 2


def test_bare_prose_kept_pending_never_infers_task_coverage(tmp_path, book, config):
    result = execute(tmp_path, book, config, Factory("bare"), "whole_author")
    assert not result["complete"] and result["body_markdown"]
    assert len(result["pending_task_ids"]) == 6


def make_base(tmp_path, book, config):
    return engine.run_fullbody_candidate(book, route="whole_author", output_dir=tmp_path / "base",
        config=config, client_factory=Factory(), run=True, token_counter=meter)


def test_reader_revision_fresh_full_body_then_exact_targeted_patch(tmp_path, book, config):
    base = make_base(tmp_path, book, config)
    factory = Factory()
    result = execute(tmp_path, book, config, factory, "reader_revision", base_result=base)
    assert result["complete"], result
    assert [call["role"] for call in factory.calls] == ["reader", "reviser"]
    reader = factory.calls[0]["payload"]
    assert reader["body_markdown"] == base["body_markdown"]
    assert "shared_fullbody_context" not in reader and "sources" not in reader and "author_self_evaluation" not in reader
    expected = base["body_markdown"].replace("Relationships are explained carefully", "Relationships are explained with their observation boundary")
    assert result["body_markdown"] == expected
    assert read(tmp_path / "out/runs" / result["run_id"] / "ORIGINAL_FULL_BODY_RESULT.json")["body_markdown"] == base["body_markdown"]
    assert result["cost_summary"]["base_cost_attribution"] == base["cost_summary"]
    assert result["cost_summary"]["base_reuse_charged_again"] is False


def test_reader_no_issues_costs_one_call_and_preserves_exact_body(tmp_path, book, config):
    base = make_base(tmp_path, book, config)
    factory = Factory("no_issues")
    result = execute(tmp_path, book, config, factory, "reader_revision", base_result=base)
    assert result["complete"] and len(factory.calls) == 1
    assert result["body_markdown"] == base["body_markdown"]


def test_reader_must_fit_all_original_prose_before_any_paid_call(tmp_path, book, config):
    base = make_base(tmp_path, book, config)
    factory = Factory()
    config["max_input_tokens"] = 50
    result = execute(tmp_path, book, config, factory, "reader_revision", base_result=base)
    assert not result["complete"] and not factory.calls
    assert result["body_markdown"] == base["body_markdown"]
    assert result["stages"][0]["status"] == "capacity_blocked"


def test_reader_rejects_partial_or_foreign_base(tmp_path, book, config):
    base = make_base(tmp_path, book, config)
    base["input_hash"] = "foreign"
    factory = Factory()
    result = execute(tmp_path, book, config, factory, "reader_revision", base_result=base)
    assert result["status"] == "blocked" and not factory.calls


def test_failed_reader_revision_keeps_original_and_cached_reader_resume(tmp_path, book, config):
    base = make_base(tmp_path, book, config)
    factory = Factory("revision_fail")
    result = execute(tmp_path, book, config, factory, "reader_revision", base_result=base)
    assert not result["complete"] and result["body_markdown"] == base["body_markdown"]
    factory.mode = "normal"
    resumed = execute(tmp_path, book, config, factory, "reader_revision", base_result=base, retry_failed=True)
    assert resumed["complete"] and len(factory.calls) == 3


def test_failed_new_version_never_replaces_valid_selected_body(tmp_path, book, config):
    factory = Factory()
    good = execute(tmp_path, book, config, factory, "whole_author")
    config["writer"]["thinking_budget"] += 1
    factory.mode = "fail"
    failed = execute(tmp_path, book, config, factory, "whole_author")
    assert not failed["complete"]
    selected = read(tmp_path / "out/FULL_BODY_RESULT.json")
    assert selected["run_id"] == good["run_id"]
    assert failed["selected_version"] == good["run_id"]


def test_multiple_exact_related_patches_preserve_untouched_bytes():
    original = "Before. Symptom sentence. Middle stays. Remote condition. After."
    issues = [{"issue_id": "I1", "anchor": "Symptom sentence."}]
    parser = engine._patch_parser(original, issues)
    result = parser({"patches": [{"issue_id": "I1", "anchor": "Symptom sentence.", "replacement": "Explained sentence."},
        {"issue_id": "I1", "anchor": "Remote condition.", "replacement": "Precise condition.", "reason": "Same condition must agree downstream"}],
        "rejected_issues": [], "complete": True})
    assert engine._apply_patches(original, result["patches"]) == "Before. Explained sentence. Middle stays. Precise condition. After."
    with pytest.raises(ValueError, match="overlapping"):
        engine._apply_patches(original, [{"anchor": "Symptom sentence.", "replacement": "X"}, {"anchor": "sentence.", "replacement": "Y"}])


def test_nonunique_reader_anchor_and_unaddressed_issue_fail_closed():
    parsed = engine._reader_parser("Twice Twice")({"issues": [{"issue_id": "I", "anchor": "Twice", "problem": "P", "reader_understanding": "U", "suggested_action": "A"}], "complete": True})
    assert parsed["complete"] and parsed["issues"] == []
    assert parsed["pending_reader_issues"][0]["validation_errors"] == ["reader_anchor_must_match_original_exactly_once"]
    with pytest.raises(ValueError, match="unaddressed"):
        engine._patch_parser("Unique", [{"issue_id": "I", "anchor": "Unique"}])({"patches": [], "rejected_issues": [], "complete": True})


def test_already_visible_source_read_does_not_repay_and_explicit_retry_repairs(tmp_path, book, config):
    factory = Factory("workbench_current")
    failed = execute(tmp_path, book, config, factory, "workbench")
    assert not failed["complete"] and len(factory.calls) == 1
    assert failed["stages"][0]["status"] == "reread_no_progress"
    factory.mode = "normal"
    repaired = execute(tmp_path, book, config, factory, "workbench", retry_failed=True)
    assert repaired["complete"] and len(factory.calls) == 4


def test_revised_result_can_be_reused_with_current_exact_segments(tmp_path, book, config):
    base = make_base(tmp_path, book, config)
    revised = execute(tmp_path, book, config, Factory(), "reader_revision", base_result=base)
    assert revised["complete"] and engine._join(revised["segments"]) == revised["body_markdown"]
    again = engine.run_fullbody_candidate(book, route="reader_revision", output_dir=tmp_path / "second",
        config=config, client_factory=Factory("no_issues"), run=True, token_counter=meter, base_result=revised)
    assert again["complete"] and again["body_markdown"] == revised["body_markdown"]
    assert Path(again["input_path"]).is_file() and Path(again["source_identity_map_path"]).is_file()
    assert again["approved_chapter_order"] == ["C1", "C2", "C3"]


def test_cross_source_related_revision_expands_complete_evidence_before_apply(tmp_path, book, config):
    base = make_base(tmp_path, book, config)
    base.update(body_markdown="First finding [P0001].\n\nLater finding [P0002].\n\nThird finding [P0003].", segments=[])
    base["body_sha256"] = engine._text_hash(base["body_markdown"])
    seen = []
    def factory(role, directory, profile):
        def call(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            seen.append((role, payload))
            if role == "reader":
                obj = {"issues": [{"issue_id": "I1", "anchor": "First finding [P0001].", "problem": "A connection is missing", "reader_understanding": "I see a gap", "suggested_action": "Clarify the relationship"}], "complete": True}
            else:
                obj = {"patches": [{"issue_id": "I1", "anchor": "First finding [P0001].", "replacement": "First conditional finding [P0001]."},
                    {"issue_id": "I1", "anchor": "Later finding [P0002].", "replacement": "Later conditional finding [P0002].", "reason": "Related downstream condition"}], "rejected_issues": [], "complete": True}
            return {"content": json.dumps(obj), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 100, "completion_tokens": 100}}
        return call
    result = execute(tmp_path, book, config, factory, "reader_revision", base_result=base)
    assert result["complete"] and len(seen) == 3, result
    assert {row["source_handle"] for row in seen[1][1]["sources"]} == {"P0001"}
    assert {row["source_handle"] for row in seen[2][1]["sources"]} == {"P0001", "P0002"}
    assert result["stages"][1]["status"] == "evidence_supplement_required"
    assert result["stages"][2]["stage_id"] == "revision_001_evidence_01"
    assert "Third finding [P0003]." in result["body_markdown"]
    assert engine._join(result["segments"]) == result["body_markdown"]


def test_workbench_rereads_cross_chapter_alias_complements_once_without_rewriting_ids(tmp_path, book, config):
    chapters = deepcopy(book["chapters"])
    for index, handle in enumerate(("P0333", "P0584")):
        chapter = chapters[index]
        chapter["sources"][0].update(source_handle=handle, doi="10.1000/explicit-same-study",
                                     paper_id="same-study", aliases=["P0584"] if index == 0 else [])
        chapter["units"][0]["source_handles"] = [handle]
        for task in chapter["units"][0]["paragraph_tasks"]:
            task["source_handles"] = [handle]
    paired = build_fullbody_input(chapters)
    assert paired["source_aliases"]["P0584"] == "P0333"
    before = deepcopy(paired)
    calls = []
    def factory(role, directory, profile):
        def call(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            calls.append(payload)
            if payload["editable_task_ids"][0].startswith("C3::") and not payload.get("reread_sources"):
                obj = {"read_source_handles": ["P0584"]}
            else:
                handle = payload["chapters"][0]["units"][0]["source_handles"][0]
                obj = {"body_markdown": f"Current original citation [{handle}].", "completed_task_ids": payload["editable_task_ids"], "complete": True}
            return {"content": json.dumps(obj), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 111, "completion_tokens": 222}}
        return call
    result = execute(tmp_path, paired, config, factory, "workbench")
    assert result["complete"] and len(calls) == 4, result
    reread = calls[-1]
    records = [row for row in reread["sources"] if row["source_handle"] in {"P0333", "P0584"}]
    assert {row["source_handle"] for row in records} == {"P0333", "P0584"}
    assert len(records) == 2
    assert json.dumps(reread).count("INTACT_SOURCE_TAIL_1") == 1
    assert json.dumps(reread).count("INTACT_SOURCE_TAIL_2") == 1
    assert {row["canonical_source_handle"] for row in reread["reread_sources"]} == {"P0333"}
    assert "[P0584]" in result["body_markdown"] and "[P0333]" in result["body_markdown"]
    assert result["completed_task_ids"] == list(fullbody_task_catalog(before))
    assert paired == before


class MixedReaderFactory(Factory):
    def __init__(self, only_bad=False):
        super().__init__()
        self.only_bad = only_bad

    def __call__(self, role, directory, profile):
        delegate = super().__call__(role, directory, profile)
        def call(messages, **kwargs):
            response = delegate(messages, **kwargs)
            if role == "reader":
                obj = json.loads(response["content"])
                good = obj["issues"][0]
                for optional in ("issue_id", "reader_understanding", "suggested_action"):
                    good.pop(optional)
                bad = {"issue_id": "unlocated", "anchor": "This wording never appeared in the BODY", "problem": "A second relation needs explanation"}
                obj["issues"] = [bad] if self.only_bad else [good, bad]
                response["content"] = json.dumps(obj)
            return response
        return call


def test_one_bad_reader_issue_preserves_valid_edit_and_pending_diagnostic_without_repay(tmp_path, book, config):
    base = make_base(tmp_path, book, config)
    factory = MixedReaderFactory()
    first = execute(tmp_path, book, config, factory, "reader_revision", base_result=base)
    assert first["body_complete"] and not first["complete"]
    assert first["reader_revision_status"] == "partial"
    assert len(factory.calls) == 2
    assert "Relationships are explained with their observation boundary" in first["body_markdown"]
    assert first["pending_reader_issues"][0]["issue_id"] == "unlocated"
    assert factory.calls[1]["payload"]["reader_issues"][0]["issue_id"] == "reader_issue_0001"
    assert len(factory.calls[1]["payload"]["reader_issues"]) == 1
    again = execute(tmp_path, book, config, factory, "reader_revision", base_result=base)
    assert len(factory.calls) == 2 and again["pending_reader_issues"] == first["pending_reader_issues"]
    assert again["body_markdown"] == first["body_markdown"]


def test_invalid_only_reader_diagnostics_are_pending_not_clean_review(tmp_path, book, config):
    base = make_base(tmp_path, book, config)
    factory = MixedReaderFactory(only_bad=True)
    result = execute(tmp_path, book, config, factory, "reader_revision", base_result=base)
    assert result["body_complete"] and not result["complete"]
    assert result["reader_revision_status"] == "pending" and result["pending_reader_issues"]
    assert result["body_markdown"] == base["body_markdown"] and len(factory.calls) == 1
