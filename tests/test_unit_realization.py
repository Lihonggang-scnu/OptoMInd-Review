"""Labeled synthetic fixtures only: bounded production wiring, no network."""
from copy import deepcopy
import json
from pathlib import Path
import pytest

from optomind_research.runtime.upgrade3 import legacy_unit_route as route
from optomind_research.runtime.upgrade3 import unit_realization as quality
from optomind_research.runtime.upgrade3 import review_unit_writer as writer
from optomind_research.runtime.upgrade3.fullbody_contracts import register_tool_source_identities


def book(units=1):
    return {"language": "en", "source_aliases": {}, "chapters": [{
        "chapter_id": "CH", "chapter_frame": {"chapter_title": "Synthetic chapter", "chapter_argument": "Metadata assertion"},
        "sources": [{"source_handle": "P0001", "doi": "10.1234/fixture-a", "title": "Fixture A",
                     "study_summary_A": {"text": "Synthetic material supports A and B only."}}],
        "units": [{"unit_id": f"U{i}", "focus": "Synthetic focus", "title": "Explicit unit title",
                   "paragraph_tasks": [{"paragraph_id": "p1", "point": "Explain A", "source_uses": [{"source_handle": "P0001"}]},
                                       {"paragraph_id": "p2", "point": "Explain B", "source_uses": [{"source_handle": "P0001"}]}],
                   "table_tasks": []} for i in range(units)]}]}


def counter(raw, messages):
    return 100


def response(data, finish="stop"):
    return {"content": json.dumps(data), "finish_reason": finish, "complete": finish == "stop",
            "model": "qwen3.5-plus", "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


def assessment(body, second="covered", changes=None):
    return response({"tasks": [
        {"task_id": "p1", "status": "covered", "body_quote": "SYNTHETIC A", "explanation": "A is present", "material_handles": ["P0001"]},
        {"task_id": "p2", "status": second, "body_quote": "SYNTHETIC B" if second == "covered" else "",
         "explanation": "B is present" if second == "covered" else "B has not been explained", "material_handles": ["P0001"]}],
        "changes": changes or [], "issues": []})


class Factory:
    execution_mode = "injected"
    def __init__(self, first_status="missing", partial_step="", edit=None):
        self.calls = []
        self.first_status, self.partial_step, self.edit = first_status, partial_step, edit

    def __call__(self, role, path, profile):
        def call(messages, **kwargs):
            self.calls.append((role, deepcopy(messages), deepcopy(kwargs)))
            payload = json.loads(messages[-1]["content"])
            if role == "writer":
                data = response({"body_markdown": "SYNTHETIC A [P0001].", "issues": []})
            elif role == "completion":
                assert payload["requested_task_ids"] == ["p2"]
                assert [task["paragraph_id"] for task in payload["paragraph_tasks"]] == ["p2"]
                assert [row["task_id"] for row in payload["gap_feedback"]] == ["p2"]
                assert payload["gap_feedback"][0]["explanation"] == "B has not been explained"
                data = response({"body_markdown": "SYNTHETIC B [P0001].", "status": "appended", "covered_task_ids": ["p2"], "issues": []})
            else:
                data = assessment(payload["actual_body_markdown"], self.first_status if role == "assessment" else "covered",
                                  [self.edit] if self.edit and role == "assessment" else [])
            if role == self.partial_step:
                data.update(finish_reason="length", complete=False)
            return data
        call.prompt_token_counter = counter
        return call


def run_stage(tmp_path, factory, body="SYNTHETIC A [P0001].", run=True):
    view = route.build_unit_views(book())[0]
    return quality.run_unit_quality(view, payload=route.build_legacy_payload(view, language="en"),
        existing_body=body, output_dir=tmp_path, client_factory=factory, run=run, token_counter=counter,
        language="en", experiment_label="offline_synthetic_fixture")


def test_omitted_task_old_response_completed_once_and_prefix_preserved(tmp_path):
    factory = Factory()
    original = "SYNTHETIC A [P0001].\r\nexact prefix"
    result = run_stage(tmp_path, factory, original)
    assert [row[0] for row in factory.calls] == ["assessment", "completion", "post_assessment"]
    assert result["body_markdown"].startswith(original + "\n\n")
    assert result["status"] == "assessed_pending_human_review"
    assert result["scientific_acceptance"] is False
    assert factory.calls[0][2]["thinking_budget"] == 16384
    assert factory.calls[0][2]["max_output_tokens"] == 24576
    assert factory.calls[1][2]["thinking_budget"] == 8192
    assert factory.calls[1][2]["max_output_tokens"] == 32768
    resumed = run_stage(tmp_path, factory, original)
    assert resumed["cache_hit"] and resumed["model_calls"] == 0
    assert len(factory.calls) == 3


def test_naturally_combined_tasks_no_forced_extra_paragraph(tmp_path):
    factory = Factory(first_status="covered")
    original = "SYNTHETIC A and SYNTHETIC B [P0001] share this natural paragraph."
    result = run_stage(tmp_path, factory, original)
    assert len(factory.calls) == 1
    assert result["body_markdown"] == original
    assert not result["changed"] and not result["pending_problems"]


def test_material_limited_does_not_force_completion(tmp_path):
    factory = Factory(first_status="material_limited")
    result = run_stage(tmp_path, factory)
    assert len(factory.calls) == 1
    assert result["body_markdown"] == "SYNTHETIC A [P0001]."
    assert result["pending_problems"][0]["code"] == "quality_task_material_limited"


@pytest.mark.parametrize("step", ["assessment", "completion", "post_assessment"])
def test_partial_quality_response_saved_and_never_rebought(tmp_path, step):
    factory = Factory(partial_step=step)
    first = run_stage(tmp_path, factory)
    count = len(factory.calls)
    result = run_stage(tmp_path, factory)
    assert len(factory.calls) == count and result["model_calls"] == 0
    assert result["pending_problems"]
    assert (Path(first["output_dir"]) / step / "RAW_RESPONSE.json").is_file()
    if step == "post_assessment":
        assert "SYNTHETIC B" in result["body_markdown"]
    else:
        assert result["body_markdown"] == "SYNTHETIC A [P0001]."


def test_preview_then_run_and_raw_recovery_do_not_rebuy(tmp_path):
    factory = Factory(first_status="covered")
    body = "SYNTHETIC A and SYNTHETIC B [P0001]."
    planned = run_stage(tmp_path, factory, body, run=False)
    assert not factory.calls and planned["status"] == "planned"
    first = run_stage(tmp_path, factory, body)
    assert len(factory.calls) == 1
    target = Path(first["output_dir"])
    (target / "QUALITY_RESULT.json").unlink()
    (target / "RESULT_SEAL.json").unlink()
    recovered = run_stage(tmp_path, factory, body)
    assert len(factory.calls) == 1 and recovered["model_calls"] == 0


def test_quotes_task_ids_and_handles_are_checked():
    view = route.build_unit_views(book())[0]
    body = "SYNTHETIC A and SYNTHETIC B"
    messages = quality.assessment_messages(view, route.build_legacy_payload(view), body)
    for field, value in [("task_id", "invented"), ("body_quote", "invented quote"), ("material_handles", ["P9999"]), ("status", "covered_elsewhere")]:
        data = json.loads(assessment(body)["content"])
        data["tasks"][0][field] = value
        with pytest.raises(ValueError, match="quality_"):
            quality.validate_assessment(response(data), messages, view, body)


def test_evidence_edit_unique_anchor_preserves_other_bytes():
    view = route.build_unit_views(book())[0]
    body = "prefix\r\nSYNTHETIC WRONG [P0001].\n\nuntouched suffix"
    edit = {"operation": "replace", "original_text": "SYNTHETIC WRONG", "replacement_text": "SYNTHETIC A",
            "reason": "Actual material supports only A and B", "material_handles": ["P0001"],
            "material_quote": "Synthetic material supports A and B only."}
    changed, applied = quality.apply_evidence_edits(body, [edit], view)
    assert changed == body.replace("SYNTHETIC WRONG", "SYNTHETIC A") and len(applied) == 1
    with pytest.raises(ValueError, match="anchor"):
        quality.apply_evidence_edits(body + " SYNTHETIC WRONG", [edit], view)
    overlap = {**edit, "original_text": "WRONG", "replacement_text": "A"}
    with pytest.raises(ValueError, match="overlap"):
        quality.apply_evidence_edits(body, [edit, overlap], view)


def test_multiline_material_quote_matches_actual_recursive_string():
    view = route.build_unit_views(book())[0]
    view.materials[0]["study_summary_A"]["detail"] = {"text": "First material line\nSecond material line"}
    edit = {"operation": "replace", "original_text": "Wrong", "replacement_text": "Correct",
            "reason": "Supplied lines support correction", "material_handles": ["P0001"],
            "material_quote": "First material line\nSecond material line"}
    changed, applied = quality.apply_evidence_edits("Untouched\r\nWrong\nSuffix", [edit], view)
    assert changed == "Untouched\r\nCorrect\nSuffix" and applied


def test_optional_gap_feedback_old_default_and_explicit_task_validation():
    view = route.build_unit_views(book())[0]
    payload = writer.build_completion_payload(view, "Original", ["p2"])
    assert "gap_feedback" not in payload
    feedback = [{"task_id": "p2", "status": "partial", "explanation": "Explain missing boundary", "body_quote": "Original", "material_handles": ["P0001"]}]
    messages = writer.completion_messages(view, "Original", ["p2"], gap_feedback=feedback)
    actual = json.loads(messages[-1]["content"])
    assert actual["gap_feedback"] == feedback
    assert actual["paragraph_tasks"] == payload["paragraph_tasks"] and actual["sources"] == payload["sources"]
    with pytest.raises(writer.UnitWritingError, match="gap_feedback_task_invalid"):
        writer.build_completion_payload(view, "Original", ["p2"], gap_feedback=[{"task_id": "p1"}])


def test_completion_full_recursive_review_evidence_and_covered_ids():
    data = book()
    data["chapters"][0]["sources"][0]["review_planning_B"] = {"review_source_handle": "P0002"}
    data["chapters"][0]["sources"].append({"source_handle": "P0002", "title": "Review-only source", "review_content": "complete review " * 5000})
    view = route.build_unit_views(data)[0]
    payload = writer.build_completion_payload(view, "original", ["p2"])
    assert [item["source_handle"] for item in payload["sources"]] == ["P0001", "P0002"]
    assert payload["sources"][1]["review_content"] == "complete review " * 5000
    result = writer.run_unit_completion(view, existing_body="original", task_ids=["p2"],
        client=lambda m, **kw: response({"body_markdown": "Candidate", "status": "appended", "covered_task_ids": ["p1"]}),
        model="offline-synthetic")
    assert result["pending"] and result["body_markdown"] == "original"


def test_tool_same_identity_reuse_new_identity_conflict_and_unknown_retained():
    data = book()
    data["chapters"][0]["chapter_tool_materials"] = [{"unit_key": "CH:U0", "usable_content": "FULL MULTISOURCE ANSWER " * 500,
        "sources": [{"doi": "https://doi.org/10.1234/fixture-a", "source_handle": "P0099", "title": "Existing"},
                    {"doi": "10.1234/fixture-b", "source_handle": "P0001", "title": "New"},
                    {"source_handle": "P0998", "title": "Unidentified", "content": "Retained unknown content"}]}]
    before = deepcopy(data)
    registered = register_tool_source_identities(data)
    tool = registered["chapters"][0]["chapter_tool_materials"][0]
    assert tool["sources"][0]["source_handle"] == "P0001"
    assert tool["sources"][1]["source_handle"] == "P0002"
    assert tool["sources"][1]["conflicting_handle"] == "P0001"
    assert tool["sources"][2]["content"] == "Retained unknown content"
    assert "source_handle" not in tool["sources"][2]
    assert tool["source_identity_diagnostics"]
    assert data == before
    assert registered["chapters"][0]["units"][0]["paragraph_tasks"] == before["chapters"][0]["units"][0]["paragraph_tasks"]
    view = route.build_unit_views(data)[0]
    assert set(view.sources) == {"P0001", "P0002"}
    assert view.chapter_tool_materials[0]["usable_content"] == tool["usable_content"]
    assert "usable_content" not in view.sources["P0002"]
    assert register_tool_source_identities(registered) == registered


def test_tool_identity_allocation_does_not_rebind_uncatalogued_task_handle():
    data = book()
    task = data["chapters"][0]["units"][0]["paragraph_tasks"][1]
    task["source_uses"] = [{"source_handle": "P0002"}]
    data["chapters"][0]["chapter_tool_materials"] = [{"unit_key": "CH:U0", "usable_content": "Synthetic new identity evidence",
        "sources": [{"doi": "10.1234/fixture-new", "source_handle": "P0001", "title": "Different new source"}]}]
    registered = register_tool_source_identities(data)
    source = registered["chapters"][0]["chapter_tool_materials"][0]["sources"][0]
    assert source["source_handle"] == "P0003"
    assert registered["chapters"][0]["units"][0]["paragraph_tasks"][1] == task
    assert not any(row["source_handle"] == "P0002" for row in registered["chapters"][0]["sources"])
    with pytest.raises(ValueError, match="legacy_unit_source_missing:CH:U0:P0002"):
        route.build_unit_views(registered)


def test_integrated_quality_derivative_assembly_and_selection_resume(tmp_path):
    factory = Factory()
    factory.execution_mode = "live"  # Synthetic offline seam exercises actual assembler.
    data = book(units=2)
    first = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory,
        token_counter=counter, quality_control=True, only_units=["CH:U0"])
    assert first["requested_unit_count"] == 1 and first["full_unit_count"] == 2
    assert first["units"][1]["status"] == "not_selected"
    original = Path(first["units"][0]["attempt_dir"])
    before = (original / "UNIT_RESULT.json").read_bytes(), (original / "UNIT_BODY.md").read_bytes()
    text = (tmp_path / "assembled" / "REVIEW_DRAFT_HANDLES.md").read_text(encoding="utf-8")
    assert "SYNTHETIC B" in text and "本章论断" not in text
    derivative = Path(first["units"][0]["quality"]["output_dir"]) / "assembly_derivative"
    saved_derivative = json.loads((derivative / "UNIT_RESULT.json").read_text(encoding="utf-8"))
    actual_diagnostics = writer._output_diagnostics(saved_derivative["body_markdown"], ["P0001"], [])
    for key in ("used_source_handles", "unknown_citations", "unresolved_numeric_citations", "citation_problems", "fence_report"):
        assert saved_derivative[key] == actual_diagnostics[key]
    second = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory,
        token_counter=counter, quality_control=True)
    assert second["complete_units"] == 2 and second["model_calls"] == 4
    assert before == ((original / "UNIT_RESULT.json").read_bytes(), (original / "UNIT_BODY.md").read_bytes())
    third = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory,
        token_counter=counter, quality_control=True)
    assert third["model_calls"] == 0


def test_display_heading_precedence_and_plain_body_preserved():
    from scripts.upgrade3 import full_review_draft as draft
    assert draft.author_unit_title("## Author title\n\nPlain [P0001].") == "Author title"
    assert draft.clean_unit_body("## Author title\n\nPlain [P0001].\n\n### Internal heading\nText") == "Plain [P0001].\n\n##### Internal heading\nText"
    assert draft.clean_unit_body("Plain [P0001].") == "Plain [P0001]."
    assert draft.author_unit_title("## Table 1. Evidence\n|A|B|") == ""


def test_cli_matching_flags():
    from scripts.upgrade3.legacy_unit_writer import parser
    args = parser().parse_args(["--quality-control", "--only-unit", "CH:U0", "--only-unit", "CH:U1"])
    assert args.quality_control and args.only_unit == ["CH:U0", "CH:U1"]
