"""Labeled synthetic fixtures only: bounded production wiring, no network."""
from copy import deepcopy
import json
import os
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


def run_stage(tmp_path, factory, body="SYNTHETIC A [P0001].", run=True, reparse_saved=False):
    view = route.build_unit_views(book())[0]
    return quality.run_unit_quality(view, payload=route.build_legacy_payload(view, language="en"),
        existing_body=body, output_dir=tmp_path, client_factory=factory, run=run, token_counter=counter,
        language="en", experiment_label="offline_synthetic_fixture", reparse_saved=reparse_saved)


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


def test_ordered_omission_quotes_strictly_anchor_all_fragments():
    body = "SYNTHETIC A first [P0001]. Middle omitted. Last fragment. SYNTHETIC B."
    view = route.build_unit_views(book())[0]
    messages = quality.assessment_messages(view, route.build_legacy_payload(view), body)
    data = json.loads(assessment(body)["content"])
    data["tasks"][0]["body_quote"] = "SYNTHETIC A first [P0001]. ... Last fragment."
    parsed = quality.validate_assessment(response(data), messages, view, body)
    row = parsed["tasks"][0]
    assert row["body_quote"] == data["tasks"][0]["body_quote"]
    assert row["body_quote_match"] == "ordered_omission_excerpt"
    assert [body[span["start"]:span["end"]] for span in row["body_quote_spans"]] == [
        "SYNTHETIC A first [P0001].", "Last fragment."]
    for invalid in ("Last fragment. ... SYNTHETIC A first [P0001].",
                    "SYNTHETIC A changed [P0001]. ... Last fragment.", "...", "... Last fragment."):
        data["tasks"][0]["body_quote"] = invalid
        with pytest.raises(ValueError, match="quality_body_quote_invalid"):
            quality.validate_assessment(response(data), messages, view, body)
    spans, match = quality._body_quote_spans("Literal ... source text", "Literal ... source text")
    assert match == "exact" and len(spans) == 1


def test_whitespace_quote_mapping_preserves_original_characters_and_order():
    body = "整合5个队列，AUC=0.75。 变化未显示增加。 Middle omitted. 尾段ABC。"
    quote = "整合 5 个队列，AUC = 0.75。"
    spans, kind = quality._body_quote_spans(body, quote)
    assert kind == "whitespace_normalized" and spans[0]["text"] == "整合5个队列，AUC=0.75。"
    assert body[spans[0]["start"]:spans[0]["end"]] == spans[0]["text"]
    spans, kind = quality._body_quote_spans(body, quote + " ... 尾段 A B C。")
    assert kind == "ordered_omission_excerpt_whitespace_normalized"
    assert [s["text"] for s in spans] == ["整合5个队列，AUC=0.75。", "尾段ABC。"]
    for invalid in ("整合6个队列，AUC=0.75。", "整合5个队列，AUC=0.76。",
                    "变化显示增加。", "尾段ABC。 ... 整合5个队列，AUC=0.75。"):
        assert quality._body_quote_spans(body, invalid)[0] == []


def test_complete_quote_typo_json_recovery_retains_content_and_validation():
    view = route.build_unit_views(book())[0]
    body = "SYNTHETIC A and SYNTHETIC B"
    messages = quality.assessment_messages(view, route.build_legacy_payload(view), body)
    data = json.loads(assessment(body)["content"])
    data["tasks"][0]["explanation"] = 'Explains "A“ without changing content'
    raw = json.dumps(data).replace('\\"A', '"A')
    malformed = {**response(data), "content": raw}
    parsed = quality.validate_assessment(malformed, messages, view, body)
    assert parsed["parsed_via"] == "json_repair_quote_escape_crosschecked"
    assert parsed["tasks"][0]["explanation"] == data["tasks"][0]["explanation"]
    assert malformed["content"] == raw
    for invalid in ({**malformed, "finish_reason": "length", "complete": False},
                    {**malformed, "complete": False},
                    {**malformed, "content": raw[:-2]},
                    {**malformed, "content": raw[:-3] + "}"},
                    {**malformed, "content": raw.replace("SYNTHETIC A", "Invented A", 1)}):
        with pytest.raises(ValueError, match="quality_"):
            quality.validate_assessment(invalid, messages, view, body)


def test_material_excerpt_same_record_paths_and_strict_body_anchor():
    view = route.build_unit_views(book())[0]
    view.materials[0]["study_summary_A"] = {"findings": [
        {"text": "SYNTHETIC value 39 cm, no increase."}, {"text": "SYNTHETIC value 18 cm and 47 cm."}]}
    edit = {"operation": "replace", "original_text": "WRONG value", "replacement_text": "Supported value",
            "reason": "Synthetic diagnostic", "material_handles": ["P0001"],
            "material_quote": "SYNTHETIC value39 cm, no increase. ... SYNTHETIC value18 cm and47 cm."}
    body = "Prefix WRONG value. Untouched suffix."
    changed, applied = quality.apply_evidence_edits(body, [edit], view)
    assert changed == "Prefix Supported value. Untouched suffix."
    evidence = applied[0]["material_quote_evidence"]
    assert evidence["match"] == "ordered_omission_excerpt_whitespace_normalized"
    assert [s["path"] for s in evidence["spans"]] == [
        ["study_summary_A", "findings", 0, "text"], ["study_summary_A", "findings", 1, "text"]]
    assert [s["text"] for s in evidence["spans"]] == [
        "SYNTHETIC value 39 cm, no increase.", "SYNTHETIC value 18 cm and 47 cm."]
    for quote in (edit["material_quote"].replace("39", "40"),
                  edit["material_quote"].replace("no increase", "increase"),
                  "Invented claim ... SYNTHETIC value 18 cm and 47 cm."):
        with pytest.raises(ValueError, match="material_quote"):
            quality.apply_evidence_edits(body, [{**edit, "material_quote": quote}], view)
    with pytest.raises(ValueError, match="anchor"):
        quality.apply_evidence_edits(body, [{**edit, "original_text": "WRONGvalue"}], view)
    first, second = deepcopy(view.materials[0]), deepcopy(view.materials[0])
    first["study_summary_A"]["findings"] = first["study_summary_A"]["findings"][:1]
    second["study_summary_A"]["findings"] = second["study_summary_A"]["findings"][1:]
    view.materials = [first, second]
    with pytest.raises(ValueError, match="material_quote"):
        quality.apply_evidence_edits(body, [edit], view)


def test_reparse_preview_then_run_reuses_raw_and_only_continues_missing_steps(tmp_path, monkeypatch):
    factory = Factory()
    validator = quality.validate_assessment
    with monkeypatch.context() as previous_parser:
        previous_parser.setattr(quality, "validate_assessment", lambda *a: (_ for _ in ()).throw(
            ValueError("quality_body_quote_invalid:old_parser")))
        failed = run_stage(tmp_path, factory)
    assert len(factory.calls) == 1 and failed["status"] == "pending"
    target = Path(failed["output_dir"])
    originals = {name: (target / name).read_bytes() for name in
                 ("QUALITY_RESULT.json", "RESULT_SEAL.json", "QUALITY_BODY.md", "assessment/RAW_RESPONSE.json", "assessment/REQUEST.json")}
    ordinary = run_stage(tmp_path, factory)
    assert ordinary["model_calls"] == 0 and ordinary["status"] == "pending"
    planned = run_stage(tmp_path, factory, run=False, reparse_saved=True)
    assert planned["model_calls"] == 0 and planned["status"] == "planned"
    assert len(factory.calls) == 1 and planned["assessments"]
    history = Path(planned["reparse_saved"]["history_dir"])
    for name in ("QUALITY_RESULT.json", "RESULT_SEAL.json", "QUALITY_BODY.md"):
        assert (history / name).read_bytes() == originals[name]
    completed = run_stage(tmp_path, factory, reparse_saved=True)
    assert completed["model_calls"] == 2
    assert [call[0] for call in factory.calls] == ["assessment", "completion", "post_assessment"]
    assert completed["body_markdown"].startswith("SYNTHETIC A [P0001].\n\n")
    for name in ("assessment/RAW_RESPONSE.json", "assessment/REQUEST.json"):
        assert (target / name).read_bytes() == originals[name]
    feedback = json.loads(factory.calls[1][1][-1]["content"])["gap_feedback"]
    assert "body_quote_spans" not in feedback[0] and "body_quote_match" not in feedback[0]
    assert quality.validate_assessment is validator


@pytest.mark.parametrize("step", ["assessment", "completion", "post_assessment"])
def test_explicit_reparse_does_not_rebuy_partial_stage(tmp_path, step):
    factory = Factory(partial_step=step)
    first = run_stage(tmp_path, factory)
    calls = len(factory.calls)
    replayed = run_stage(tmp_path, factory, reparse_saved=True)
    assert replayed["model_calls"] == 0 and len(factory.calls) == calls
    assert replayed["body_markdown"] == first["body_markdown"]
    assert replayed["pending_problems"]


def test_reparse_completed_request_retains_exact_completion_identity(tmp_path):
    factory = Factory()
    first = run_stage(tmp_path, factory)
    target = Path(first["output_dir"])
    request = (target / "completion/REQUEST.json").read_bytes()
    replayed = run_stage(tmp_path, factory, reparse_saved=True)
    assert replayed["model_calls"] == 0 and len(factory.calls) == 3
    assert replayed["body_markdown"] == first["body_markdown"]
    assert (target / "completion/REQUEST.json").read_bytes() == request


def test_budget_refusal_keeps_request_and_cannot_be_rebought_on_reparse(tmp_path):
    calls = []
    def refused(role, path, profile):
        calls.append(role)
        raise ValueError("synthetic_budget_reservation_refused")
    first = run_stage(tmp_path, refused)
    target = Path(first["output_dir"])
    request = (target / "assessment/REQUEST.json").read_bytes()
    replayed = run_stage(tmp_path, refused, reparse_saved=True)
    assert first["model_calls"] == replayed["model_calls"] == 0 and len(calls) == 1
    assert (target / "assessment/REQUEST.json").read_bytes() == request
    assert any(row["code"] == "quality_assessment_pending_existing_attempt" for row in replayed["pending_problems"])


def test_post_assessment_changes_stay_unapplied_and_explicitly_pending(tmp_path):
    factory = Factory()
    def with_post_changes(role, path, profile):
        base = factory(role, path, profile)
        def client(messages, **kwargs):
            returned = base(messages, **kwargs)
            if role == "post_assessment":
                data = json.loads(returned["content"])
                data["changes"] = [{"operation": "replace", "original_text": "SYNTHETIC B", "replacement_text": "Must not apply"}]
                returned["content"] = json.dumps(data)
            return returned
        client.prompt_token_counter = counter
        return client
    result = run_stage(tmp_path, with_post_changes)
    assert len(factory.calls) == 3 and "SYNTHETIC B" in result["body_markdown"]
    assert "Must not apply" not in result["body_markdown"]
    assert result["status"] == "assessed_with_pending"
    assert any(row["code"] == "quality_postcheck_edits_not_applied" for row in result["pending_problems"])


def test_only_verified_missing_table_structure_resolves_other_issues_remain():
    view = route.build_unit_views(book())[0]
    view.table_tasks = [{"table_id": "t1"}, {"table_id": "t2"}]
    original = [{"code": "markdown_table_missing_or_invalid"}, {"code": "table_markdown_missing_or_invalid"},
                {"problem": "Unresolved scientific attribution", "source_handles": ["P0001"]}]
    result = {"body_markdown": "|Combined task|Result|\n|---|---|\n|t1 and t2|SYNTHETIC evidence|",
              "assessments": [{"report": {"tasks": [{"task_id": "t1", "status": "covered"}, {"task_id": "t2", "status": "covered"}]}}]}
    retained, resolved = quality.retained_original_issues(view, original, result)
    assert retained == original[2:] and resolved == original[:2]
    result["assessments"][0]["report"]["tasks"][1]["status"] = "partial"
    assert quality.retained_original_issues(view, original, result) == (original, [])
    result["assessments"][0]["report"]["tasks"][1]["status"] = "covered"
    result["changed"] = True
    assert quality.retained_original_issues(view, original, result) == (original, [])
    result["assessments"][0]["step"] = "post_assessment"
    assert quality.retained_original_issues(view, original, result) == (original[2:], original[:2])
    result["body_markdown"] = "Claimed table without actual Markdown"
    assert quality.retained_original_issues(view, original, result) == (original, [])


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


def test_route_reparse_preserves_old_sealed_derivative_and_selects_new_body(tmp_path, monkeypatch):
    factory = Factory()
    factory.execution_mode = "live"  # Offline synthetic assembler control only.
    with monkeypatch.context() as old_parser:
        old_parser.setattr(quality, "validate_assessment", lambda *a: (_ for _ in ()).throw(
            ValueError("quality_body_quote_invalid:old_parser")))
        first = route.run_legacy_units(book(), output_dir=tmp_path, run=True,
            client_factory=factory, token_counter=counter, quality_control=True)
    original = Path(first["units"][0]["attempt_dir"])
    quality_root = Path(first["units"][0]["quality"]["output_dir"])
    old_derivative = quality_root / "assembly_derivative"
    preserved = {path: path.read_bytes() for path in [original / "UNIT_RESULT.json", original / "UNIT_BODY.md",
                 old_derivative / "UNIT_RESULT.json", old_derivative / "RESULT_SEAL.json", old_derivative / "UNIT_BODY.md"]}
    replayed = route.run_legacy_units(book(), output_dir=tmp_path, run=True, client_factory=factory,
        token_counter=counter, quality_control=True, reparse_saved=True)
    assert replayed["model_calls"] == 2 and len(factory.calls) == 4
    for path, content in preserved.items():
        assert path.read_bytes() == content
    job = json.loads((tmp_path / "batch/BATCH_JOBS.json").read_text(encoding="utf-8"))[0]
    assert Path(job["output"]).parent == quality_root / "assembly_derivatives"
    assert "SYNTHETIC B" in (tmp_path / "assembled/REVIEW_DRAFT_HANDLES.md").read_text(encoding="utf-8")


def test_route_clears_repaired_table_code_and_keeps_scientific_issue(tmp_path):
    data = book()
    data["chapters"][0]["units"][0]["table_tasks"] = [{"table_id": "t1", "purpose": "Synthetic task table",
        "columns": ["Dimension", "Result"], "row_tasks": [{"content": "Synthetic row", "source_uses": [{"source_handle": "P0001"}]}]}]
    scientific = {"problem": "Synthetic unresolved scientific attribution", "source_handles": ["P0001"], "action": "chapter_owner"}
    calls = []
    def factory(role, path, profile):
        def client(messages, **kwargs):
            calls.append(role)
            payload = json.loads(messages[-1]["content"])
            if role == "writer":
                return response({"body_markdown": "SYNTHETIC A and SYNTHETIC B [P0001].", "issues": [scientific]})
            if role == "completion":
                assert payload["requested_task_ids"] == ["t1"]
                return response({"body_markdown": "|Dimension|Result|\n|---|---|\n|SYNTHETIC T|Supported [P0001]|",
                                 "status": "appended", "covered_task_ids": ["t1"], "issues": []})
            assessed = json.loads(assessment(payload["actual_body_markdown"])["content"])
            assessed["tasks"].append({"task_id": "t1", "status": "missing" if role == "assessment" else "covered",
                "body_quote": "" if role == "assessment" else "SYNTHETIC T",
                "explanation": "Synthetic table is absent" if role == "assessment" else "Synthetic table is present",
                "material_handles": ["P0001"]})
            return response(assessed)
        client.prompt_token_counter = counter
        return client
    factory.execution_mode = "live"
    result = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory,
                                     token_counter=counter, quality_control=True)
    assert calls == ["writer", "assessment", "completion", "post_assessment"]
    row = result["units"][0]
    original = json.loads((Path(row["attempt_dir"]) / "UNIT_RESULT.json").read_text(encoding="utf-8"))
    assert {"code": "markdown_table_missing_or_invalid"} in original["issues"]
    derivative = Path(row["quality"]["output_dir"]) / "assembly_derivative"
    final = json.loads((derivative / "UNIT_RESULT.json").read_text(encoding="utf-8"))
    assert final["table_check"]["valid"] is True
    assert final["issues"] == [scientific] and row["pending_problems"] == [scientific]
    assert final["resolved_original_issues"] == [{"code": "markdown_table_missing_or_invalid"}]
    assert result["status"] == "restricted_draft"


def test_explicit_real_saved_response_free_reparse_then_offline_missing_steps(tmp_path):
    """Optional read-only paid RAW fixture; all continuation calls are synthetic."""
    supplied = os.environ.get("OPTO_QUALITY_REPLAY_FIXTURE")
    if not supplied:
        pytest.skip("Set OPTO_QUALITY_REPLAY_FIXTURE to a labeled read-only saved quality stage")
    source = Path(supplied)
    names = ("IDENTITY.json", "ORIGINAL_PAYLOAD.json", "ORIGINAL_BODY.md", "QUALITY_BODY.md",
             "QUALITY_RESULT.json", "RESULT_SEAL.json", "assessment/RAW_RESPONSE.json",
             "assessment/REQUEST.json", "assessment/MESSAGES.json", "assessment/PROFILE.json")
    original_files = {name: (source / name).read_bytes() for name in names}
    identity = json.loads(original_files["IDENTITY.json"].decode("utf-8"))
    payload = json.loads(original_files["ORIGINAL_PAYLOAD.json"].decode("utf-8"))
    body = original_files["ORIGINAL_BODY.md"].decode("utf-8")
    view = writer.UnitWritingView(chapter_id=payload["chapter_id"], unit_id=payload["unit_id"],
        focus=payload["unit_focus"], unit_index=payload["unit_position"]["index"],
        unit_count=payload["unit_position"]["of"], sibling_units=payload.get("sibling_units", []),
        chapter_frame=payload.get("chapter_frame", {}), other_chapters=payload.get("other_chapters", []),
        paragraph_tasks=payload["paragraph_tasks"], table_tasks=payload["table_tasks"],
        materials=payload["sources"], sources={row["source_handle"]: row for row in payload["sources"]},
        chapter_tool_materials=payload.get("chapter_tool_materials", []), unit_notes=payload.get("unit_notes", ""),
        owner_unit_context=payload.get("owner_unit_context", {}))
    output = tmp_path / "explicit_real_raw_offline_replay_only"
    clone = output / source.name
    for name, content in original_files.items():
        target = clone / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    roles = []
    def offline_factory(role, path, profile):
        def client(messages, **kwargs):
            roles.append(role)
            request = json.loads(messages[-1]["content"])
            if role == "completion":
                return response({"body_markdown": "OFFLINE SYNTHETIC REPLAY CONTROL ONLY: completion marker.",
                    "status": "appended", "covered_task_ids": request["requested_task_ids"], "issues": []})
            assert role == "post_assessment"
            return response({"tasks": [{"task_id": row["task_id"], "status": "covered",
                "body_quote": "OFFLINE SYNTHETIC REPLAY CONTROL ONLY", "explanation": "Offline wiring control only",
                "material_handles": []} for row in request["task_catalog"]], "changes": [], "issues": []})
        client.prompt_token_counter = counter
        return client
    options = dict(payload=payload, existing_body=body, output_dir=output,
        client_factory=offline_factory, token_counter=counter, language=payload["language"],
        writer_profile=identity["author_profile"], experiment_label=identity["experiment_label"], reparse_saved=True)
    planned = quality.run_unit_quality(view, run=False, **options)
    assert planned["status"] == "planned" and planned["model_calls"] == 0 and not roles
    assert Path(planned["output_dir"]) == clone
    assert planned["identity"] == identity
    assert any(row["body_quote_match"] == "ordered_omission_excerpt"
               for row in planned["assessments"][0]["report"]["tasks"])
    completed = quality.run_unit_quality(view, run=True, **options)
    assert roles == ["completion", "post_assessment"] and completed["model_calls"] == 2
    assert completed["body_markdown"].startswith(body) and completed["scientific_acceptance"] is False
    assert (clone / "assessment/RAW_RESPONSE.json").read_bytes() == original_files["assessment/RAW_RESPONSE.json"]
    assert (clone / "assessment/REQUEST.json").read_bytes() == original_files["assessment/REQUEST.json"]
    for name, content in original_files.items():
        assert (source / name).read_bytes() == content


def test_display_heading_precedence_and_plain_body_preserved():
    from scripts.upgrade3 import full_review_draft as draft
    assert draft.author_unit_title("## Author title\n\nPlain [P0001].") == "Author title"
    assert draft.clean_unit_body("## Author title\n\nPlain [P0001].\n\n### Internal heading\nText") == "Plain [P0001].\n\n##### Internal heading\nText"
    assert draft.clean_unit_body("Plain [P0001].") == "Plain [P0001]."
    assert draft.author_unit_title("## Table 1. Evidence\n|A|B|") == ""


@pytest.mark.parametrize("fixture_env, category", [
    ("OPTO_QUALITY_WHITESPACE_FIXTURE", "whitespace"),
    ("OPTO_QUALITY_JSON_QUOTE_FIXTURE", "json_quote_and_material_excerpts"),
])
def test_explicit_real_quote_recovery_free_then_offline_continuation(tmp_path, fixture_env, category):
    """Read-only real RAW fixtures; subsequent responses are labeled controls."""
    supplied = os.environ.get(fixture_env)
    if not supplied:
        pytest.skip("Set " + fixture_env + " to a labeled saved quality stage")
    source = Path(supplied)
    names = ("IDENTITY.json", "ORIGINAL_PAYLOAD.json", "ORIGINAL_BODY.md", "QUALITY_BODY.md",
             "QUALITY_RESULT.json", "RESULT_SEAL.json", "assessment/RAW_RESPONSE.json",
             "assessment/REQUEST.json", "assessment/MESSAGES.json", "assessment/PROFILE.json")
    originals = {name: (source / name).read_bytes() for name in names}
    identity = json.loads(originals["IDENTITY.json"])
    payload = json.loads(originals["ORIGINAL_PAYLOAD.json"])
    body = originals["ORIGINAL_BODY.md"].decode("utf-8")
    view = writer.UnitWritingView(chapter_id=payload["chapter_id"], unit_id=payload["unit_id"],
        focus=payload["unit_focus"], unit_index=payload["unit_position"]["index"],
        unit_count=payload["unit_position"]["of"], sibling_units=payload.get("sibling_units", []),
        chapter_frame=payload.get("chapter_frame", {}), other_chapters=payload.get("other_chapters", []),
        paragraph_tasks=payload["paragraph_tasks"], table_tasks=payload["table_tasks"],
        materials=payload["sources"], sources={row["source_handle"]: row for row in payload["sources"]},
        chapter_tool_materials=payload.get("chapter_tool_materials", []), unit_notes=payload.get("unit_notes", ""),
        owner_unit_context=payload.get("owner_unit_context", {}))
    clone_root = tmp_path / "explicit_paid_raw_offline_control"
    clone = clone_root / source.name
    for name, content in originals.items():
        target = clone / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    calls = []
    def offline_factory(role, path, profile):
        def client(messages, **kwargs):
            calls.append((role, deepcopy(messages)))
            request = json.loads(messages[-1]["content"])
            if role == "completion":
                return response({"body_markdown": "OFFLINE SYNTHETIC QUOTE REPLAY CONTROL ONLY.",
                    "status": "appended", "covered_task_ids": request["requested_task_ids"], "issues": []})
            assert role == "post_assessment"
            return response({"tasks": [{"task_id": row["task_id"], "status": "covered",
                "body_quote": "OFFLINE SYNTHETIC QUOTE REPLAY CONTROL ONLY.",
                "explanation": "Offline parser wiring control only", "material_handles": []}
                for row in request["task_catalog"]], "changes": [], "issues": []})
        client.prompt_token_counter = counter
        return client
    options = dict(payload=payload, existing_body=body, output_dir=clone_root,
        client_factory=offline_factory, token_counter=counter, language=payload["language"],
        writer_profile=identity["author_profile"], experiment_label=identity["experiment_label"], reparse_saved=True)
    planned = quality.run_unit_quality(view, run=False, **options)
    assert planned["status"] in {"planned", "assessed_pending_human_review"} and planned["model_calls"] == 0 and not calls
    assert planned["identity"] == identity and Path(planned["output_dir"]) == clone
    report = planned["assessments"][0]["report"]
    if category == "whitespace":
        assert any("whitespace_normalized" in row["body_quote_match"] for row in report["tasks"])
    else:
        assert report["parsed_via"] == "json_repair_quote_escape_crosschecked"
        assert len(planned["applied_edits"]) == 1
        evidence = planned["applied_edits"][0]["material_quote_evidence"]
        assert evidence["match"] == "ordered_omission_excerpt_whitespace_normalized"
        assert len(evidence["spans"]) == 2 and evidence["spans"][0]["path"] != evidence["spans"][1]["path"]
    completed = quality.run_unit_quality(view, run=True, **options)
    expected_roles = ["completion", "post_assessment"] if category != "whitespace" else []
    assert [row[0] for row in calls] == expected_roles and completed["model_calls"] == len(expected_roles)
    assert completed["body_markdown"].startswith(planned["body_markdown"])
    if calls:
        feedback = json.loads(calls[0][1][-1]["content"])["gap_feedback"]
        expected_feedback = [{key: value for key, value in row.items()
            if key not in {"body_quote_spans", "body_quote_match"}}
            for row in report["tasks"] if row["status"] in {"partial", "missing"}]
        assert feedback == expected_feedback
    completion_request = (clone / "completion/REQUEST.json").read_bytes() if calls else None
    again = quality.run_unit_quality(view, run=True, **options)
    assert again["model_calls"] == 0 and len(calls) == len(expected_roles)
    if completion_request:
        assert (clone / "completion/REQUEST.json").read_bytes() == completion_request
    assert (clone / "assessment/RAW_RESPONSE.json").read_bytes() == originals["assessment/RAW_RESPONSE.json"]
    assert (clone / "assessment/REQUEST.json").read_bytes() == originals["assessment/REQUEST.json"]
    assert all((source / name).read_bytes() == value for name, value in originals.items())


def test_cli_matching_flags():
    from scripts.upgrade3.legacy_unit_writer import parser
    args = parser().parse_args(["--quality-control", "--reparse-saved", "--only-unit", "CH:U0", "--only-unit", "CH:U1"])
    assert args.quality_control and args.reparse_saved and args.only_unit == ["CH:U0", "CH:U1"]
