"""Offline handoff tests; fixture science is deliberately outside optics."""
from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3.post_body_context import (
    body_input_sha256, build_post_body_context, load_body_context_input,
)
from optomind_research.runtime.upgrade3 import serial_manuscript_parts as serial
from optomind_research.runtime.upgrade3 import serial_parts_live as live


def book():
    task = {"paragraph_id": "flow-1", "point": "Compare seasonal flows",
        "development": "Keep the rainfall conditions",
        "source_uses": [{"source_handle": "P0001", "role": "Background definition",
                         "use": "Define seasonal river flow"}],
        "source_brief_details": [{"point": "Compare seasonal flows",
            "development": "Keep the rainfall conditions", "source_handles": ["P0001"]}]}
    return {"schema_version": "optomind.fullbody.input.v1", "research_question": "How does rain affect rivers?",
        "review_scope": {"statement": "Seasonal rivers"}, "review_argument": "Conditions matter",
        "source_identities": {"P0001": {"title": "Secondary river overview", "doi": "10.1/river"},
                              "P0002": {"title": "Unselected land study", "doi": "10.1/land"}},
        "sources": [{"source_handle": "P0001", "title": "Secondary river overview", "doi": "10.1/river",
            "study_summary_A": {"key_findings": [{"finding": "Flow rises", "conditions": "Wet season"}],
                                "research_scope": "River basins"},
            "review_planning_B": {"scope_interpretation_cautions": ["No dry-season extrapolation"],
                                  "broader_review_uses": [{"material": "Existing secondary study account"}]}},
            {"source_handle": "P0002", "title": "Unselected land study", "text": "Must not accompany background"}],
        "chapters": [{"chapter_id": "rivers", "chapter_frame": {"chapter_title": "Flow",
            "chapter_scope": "Seasons", "chapter_thesis": "Seasons"}, "units": [{"unit_id": "rain",
            "paragraph_tasks": [task], "table_tasks": [{"table_id": "table-1", "purpose": "Compare seasons",
                                                        "row_tasks": [{"condition": "Wet and dry"}]}],
            "owner_unit_context": {"title": "Seasonal comparison", "evidence_conditions": "Rainfall observed",
                "argument_relations": "Rain precedes flow", "cases": [{"conditions": "Wet season", "finding": "Higher flow"}],
                "owner_field_values": {"theme": "Seasonal comparison", "extra_condition": "No dams"}},
            "source_handles": ["P0001", "P0002"]}]}]}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def run_report(tmp_path):
    b = book()
    write(tmp_path / "FULL_BODY_INPUT.json", b)
    write(tmp_path / "RUN_IDENTITY.json", {"book_sha256": body_input_sha256(b)})
    draft = tmp_path / "assembled" / "body.md"
    draft.parent.mkdir()
    draft.write_text("# River review\n\n## Seasonal flow\n\nThis labelled fixture describes seasonal rivers [P0001].\n", encoding="utf-8")
    refs = write(tmp_path / "REFERENCES.json", {"entries": [
        {"source_handle": "P0001", "title": "Secondary river overview", "doi": "10.1/river"}]})
    report = {"output_root": str(tmp_path.resolve()), "body_delivery": {"ready": True},
        "selected_body": {"handles_draft": str(draft.resolve()), "references_path": str(refs.resolve())}}
    return b, report, write(tmp_path / "DELIVERY_REPORT.json", report)


def test_generic_projection_keeps_science_conditions_tasks_and_complementary_values():
    b = book()
    b["sources"].append({**deepcopy(b["sources"][0]), "study_summary_A": {
        "key_findings": [{"finding": "Flow rises", "conditions": "Wet season"},
                         {"finding": "Flow declines", "conditions": "Dry season"}], "research_scope": "River basins"}})
    b["sources"][0]["deep_read_material"] = {"question_material": [{"explanation": "Precision condition", "examples": [{"conditions": "No dams"}]}]}
    b["sources"][0]["tool_supplement_materials"] = [{"usable_content": '{"question_material":[{"explanation":"Tool condition"}]}', "limits": ["No tidal basins"]}]
    before = deepcopy(b)
    context, receipt = build_post_body_context(b)
    assert b == before
    assert receipt["background_role_handles"] == ["P0001"]
    assert len(context["source_identity_map"]) == 2
    material = context["material_records"][0]
    assert len(material["study_summary_A"]["key_findings"]) == 2
    assert "No dry-season extrapolation" in str(material)
    assert material["precision_reading_material"][0]["examples"][0]["conditions"] == "No dams"
    assert material["tool_supplement_materials"][0]["limits"] == ["No tidal basins"]
    assert "Must not accompany" not in str(context)
    unit = context["final_outline"][0]["units"][0]
    assert unit["source_handles"] == b["chapters"][0]["units"][0]["source_handles"]
    assert unit["table_tasks"] == b["chapters"][0]["units"][0]["table_tasks"]
    assert unit["owner_unit_context"]["evidence_conditions"] == "Rainfall observed"
    assert unit["owner_unit_context"]["owner_field_values"] == {"extra_condition": "No dams"}
    assert "point" not in unit["paragraph_tasks"][0]["source_brief_details"][0]
    assert unit["paragraph_tasks"][0]["point"] == "Compare seasonal flows"


def test_explicit_fields_win_and_identity_or_question_conflicts_fail():
    explicit = {"shared_scope": "Explicit narrower scope", "final_outline": [{"title": "Explicit"}],
        "material_records": [{"source_handle": "P0001", "text": "Explicit usable evidence"}]}
    context, receipt = build_post_body_context(book(), context=explicit)
    assert all(context[key] == value for key, value in explicit.items())
    assert receipt["explicit_fields"] == sorted(explicit)
    for conflict in ({"source_identity_map": {"P0001": {"doi": "10.1/wrong"}}},
                     {"research_question": "A different question"}):
        with pytest.raises(serial.SerialPartsError, match="conflict"):
            build_post_body_context(book(), context=conflict)


def test_no_background_is_empty_and_secondary_tool_content_is_usable_without_own_ab():
    b = book()
    b["sources"][0] = {"source_handle": "P0001", "doi": "10.1/river",
                       "tool_supplement_materials": [{"text": "A secondary account", "conditions": "Wet season"}]}
    context, _ = build_post_body_context(b)
    assert context["material_records"][0]["tool_supplement_materials"][0]["conditions"] == "Wet season"
    b["chapters"][0]["units"][0]["paragraph_tasks"][0]["source_uses"][0]["role"] = "Main evidence"
    context, receipt = build_post_body_context(b)
    assert context["material_records"] == [] and receipt["background_role_handles"] == []


@pytest.mark.parametrize("problem", ["hash", "foreign_input", "foreign_draft", "recorded_hash"])
def test_same_run_resolution_uses_native_semantic_hash_and_rejects_conflicts(tmp_path, problem):
    b, report, _ = run_report(tmp_path)
    resolved, receipt = load_body_context_input(report)
    assert resolved == b and receipt["body_input_sha256"] == body_input_sha256(b)
    explicit = None
    if problem == "hash":
        b["review_argument"] = "changed"
        write(tmp_path / "FULL_BODY_INPUT.json", b)
    elif problem == "foreign_input":
        explicit = tmp_path.parent / "foreign" / "FULL_BODY_INPUT.json"
    elif problem == "foreign_draft":
        report["selected_body"]["handles_draft"] = str(tmp_path.parent / "foreign.md")
    else:
        report["body_input_sha256"] = "wrong"
    with pytest.raises(serial.SerialPartsError, match="mismatch|same_run|conflict"):
        load_body_context_input(report, body_input_path=explicit)


def test_live_preview_automatically_hands_off_same_run_input_without_budget_or_client(tmp_path, monkeypatch):
    b, report, path = run_report(tmp_path)
    monkeypatch.setattr(live, "ledger_snapshot", lambda path, limit: {
        "path": str(path), "limit_cny": limit, "used_cny": 0, "actual_cny": 0, "held_cny": 0})
    args = Namespace(output=tmp_path / "preview", draft=report["selected_body"]["handles_draft"],
        body_report=path, body_input=None, identity_catalog=[], thinking_budget=16384, output_tokens=32768,
        incremental_budget=5, ledger=tmp_path / "not-opened-project", budget_limit=80,
        account_ledger=tmp_path / "not-opened-account", account_budget_limit=300,
        account_mapping_dir=tmp_path / "not-opened-mappings", research_question="", language="en", run=False)
    def forbidden(*args, **kwargs):
        pytest.fail("Preview must not call a model")
    result = live.run_live_parts(args=args, context={}, chapter_roles=[],
        token_counter=lambda raw, messages: 100, tokenizer_info={"mode": "offline fixture"}, client_factory=forbidden)
    assert result["status"] == "preview" and result["model_calls"] == 0
    saved = json.loads((args.output / "CONTEXT.json").read_text(encoding="utf-8"))
    assert saved["shared_scope"] == b["review_scope"]
    assert saved["material_records"][0]["study_summary_A"]["key_findings"][0]["conditions"] == "Wet season"
    assert (args.output / "CONTEXT_PROVENANCE.json").is_file()
    assert all(not Path(path).exists() for path in [args.ledger, args.account_ledger, args.account_mapping_dir])


def test_unicode_engineering_guard_is_bounded_and_material_guard_unchanged():
    assert serial.CONTEXT_CHAR_LIMIT == 250_000
    assert serial.MATERIAL_CHAR_LIMIT == 100_000
    assert serial.PROMPT_CHAR_LIMIT == 650_000
    normalized = serial.normalize_context({"final_outline": "界" * 200_000}, research_question="rivers", body_text="River evidence")
    assert len(normalized["final_outline"]) == 200_000
    with pytest.raises(serial.SerialPartsError, match="context_too_long"):
        serial.normalize_context({"final_outline": "界" * 250_000}, research_question="rivers", body_text="River evidence")
    with pytest.raises(serial.SerialPartsError, match="material_records_too_long"):
        serial.normalize_context({"material_records": [{"text": "界" * 100_000}]}, research_question="rivers", body_text="River evidence")


def test_normal_delivery_reuses_body_input_projection(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import review_delivery as delivery, article_text_editor
    b, report, _ = run_report(tmp_path)
    draft = Path(report["selected_body"]["handles_draft"])
    (tmp_path / "REVIEW_DRAFT_HANDLES.md").write_bytes(draft.read_bytes())
    seen = {}
    monkeypatch.setattr(article_text_editor, "run_text_edit_stage", lambda **kw: {
        "status": "edited", "edited_draft": str(draft)})
    def capture(**kwargs):
        seen.update(kwargs)
        return {"status": "failed", "final_manuscript": None, "model_calls": 0}
    monkeypatch.setattr(serial, "run_serial_parts", capture)
    result = delivery.run_downstream_delivery(config={"text_edit_fixture": {}, "front_back_fixture": {},
        "front_back_mode": "post_body", "post_body_context": {}},
        assembly_report={"output_root": str(tmp_path), "assembly": {"status": "complete", "problems_resolved": True}},
        out_dir=tmp_path / "normal", body_input=b)
    assert result["halt_reasons"] == ["03_front_back"]
    assert seen["research_question"] == b["research_question"]
    assert seen["context"]["material_records"][0]["study_summary_A"]["key_findings"][0]["conditions"] == "Wet season"
    assert result["stages"]["03_front_back"]["context_provenance"]["body_input_sha256"] == body_input_sha256(b)
