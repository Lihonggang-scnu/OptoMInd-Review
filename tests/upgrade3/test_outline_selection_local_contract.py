"""First-layer local-deepening regressions; all model invocations are offline."""
from __future__ import annotations

import json

import pytest

from optomind_research.runtime.upgrade3 import outline_selection as selection
from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3.module4 import runtime
from test_outline_selection import _payload, _selection_payload


def _entry_material_text(visible, entries):
    return "\n".join(
        selection._pointer_target(visible, locator["source_excerpt_ref"])
        for entry in entries
        for locator in entry["source_supplied_locators"]
    )


def _group(unit_ids=None):
    return {
        "group_id": "local-investment",
        "unit_ids": unit_ids or ["CH-A_U01", "CH-A_U03"],
        "selection_reason": "Compare the supplied observations within their measured conditions.",
        "improvement_focus": ["KEEP-FOCUS: organize comparison before the bounded inference"],
        "related_read_only_unit_ids": ["CH-B_U02"],
    }


def _semantic_payload():
    chapter = _payload("CH-A")
    long_finding = "A complete atomic finding: " + "measured response; " * 70 + "FINDING-END-UNCLIPPED"
    conditions = "Condition starts: " + "temperature and sample limits; " * 30 + "CONDITION-END-UNCLIPPED"
    limits = "Limit starts: " + "no cross-setting extrapolation; " * 30 + "LIMIT-END-UNCLIPPED"
    chapter["source_materials"][0]["study_summary_A"] = {
        "key_findings": [{"finding": long_finding, "conditions": conditions}],
        "contribution_and_limits": [{"contribution": "Scoped comparison", "limits": limits}],
    }
    chapter["candidate_materials"] = [{
        "source_handle": "C-REPORTED", "paper_id": "review-container",
        "original_paper_id": "reported-original-study", "original_source_handle": "R-17",
        "reported_studies": [{"study_id": "original-17", "finding": "CANDIDATE-REPORTED-FINDING", "conditions": "CANDIDATE-CONDITION", "limits": "CANDIDATE-LIMIT"}],
        "usable_content": "Candidate reports a bounded original study; no owned A/B card exists.",
    }]
    chapter["tool_materials"] = [{
        "source_handle": "T-REPORTED", "paper_id": "tool-container",
        "reported_studies": [{"study_id": "original-18", "finding": "TOOL-REPORTED-FINDING", "conditions": "TOOL-CONDITION", "limits": "TOOL-LIMIT"}],
        "usable_content": "Tool-returned review passage describing an original study.",
    }]
    chapter["input_integrity"] = strengthening._input_integrity(chapter)
    return selection.build_selection_payload([chapter]), (long_finding, conditions, limits)


def test_opt_in_semantic_request_has_complete_materials_beyond_600(monkeypatch):
    payload, atoms = _semantic_payload()
    captured = []
    monkeypatch.setattr(runtime, "invoke_client", lambda client, messages, **kw: captured.append(messages) or {"status": "none", "groups": []})
    result = selection.run_selection(payload, client=object(), model="offline", thinking_budget=1, max_output_tokens=1, model_payload=selection.model_visible_selection_payload(payload, include_material_index=True))
    assert result["status"] == "no_change"
    text = json.dumps(captured[0], ensure_ascii=False)
    for atom in atoms:
        assert len(atom) > 600
        assert atom in text
    for token in ("CANDIDATE-REPORTED-FINDING", "CANDIDATE-CONDITION", "CANDIDATE-LIMIT", "TOOL-REPORTED-FINDING", "TOOL-CONDITION", "TOOL-LIMIT", "reported-original-study"):
        assert token in text


def test_material_identity_conflicts_are_separate_and_keep_both_findings():
    chapter = _payload("CH-A")
    first = {"source_handle": "CONFLICT", "paper_id": "paper-one", "usable_content": "FIRST-CONFLICT-FINDING"}
    second = {"source_handle": "CONFLICT", "paper_id": "paper-two", "usable_content": "SECOND-CONFLICT-FINDING"}
    chapter["candidate_materials"] = [first, second]
    chapter["input_integrity"] = strengthening._input_integrity(chapter)
    visible = selection.model_visible_selection_payload(selection.build_selection_payload([chapter]), include_material_index=True)
    entries = [row for row in visible["material_identity_catalog"] if row["identity"].get("source_handle") == "CONFLICT"]
    assert len(entries) == 2
    assert {row["identity"]["paper_id"] for row in entries} == {"paper-one", "paper-two"}
    assert "FIRST-CONFLICT-FINDING" in _entry_material_text(visible, entries)
    assert "SECOND-CONFLICT-FINDING" in _entry_material_text(visible, entries)


def test_cross_chapter_editable_group_is_invalid_and_never_auto_split():
    payload, chapters = _selection_payload()
    checked = selection.validate_selection_response(payload, {"status": "selected", "groups": [_group(["CH-A_U01", "CH-B_U01"])]})
    assert checked["status"] == "invalid"
    assert any("chapter" in error for error in checked["validation_errors"])
    with pytest.raises(ValueError):
        selection.selection_to_on_demand_payloads(chapters, checked)


@pytest.mark.parametrize("declared", [None, ["CH-A"], ["CH-A", "CH-B"]])
def test_direct_projection_cannot_bypass_local_group_validation(declared):
    _, chapters = _selection_payload()
    group = _group(["CH-A_U01", "CH-B_U01"])
    if declared is not None:
        group["chapter_ids"] = declared
    with pytest.raises(ValueError):
        selection.project_selection_group(chapters, group)


@pytest.mark.parametrize("status", ["none", "no_change"])
def test_no_selection_creates_no_owner_work_or_calls(monkeypatch, status):
    payload, chapters = _selection_payload()
    calls = []
    monkeypatch.setattr(runtime, "invoke_client", lambda client, messages, **kw: calls.append(kw) or {"status": status, "groups": []})
    def forbidden_owner(*args, **kwargs):
        pytest.fail("A no-selection decision must not invoke the owner")
    monkeypatch.setattr(on_demand, "run_on_demand_strengthening", forbidden_owner)
    checked = selection.run_selection(payload, client=object(), model="offline-selector", thinking_budget=1, max_output_tokens=1)
    for job in selection.selection_to_on_demand_payloads(chapters, checked):
        on_demand.run_on_demand_strengthening(job["payload"])
    assert len(calls) == 1


def test_selection_focus_survives_real_access_and_owner_message_builders():
    payload, chapters = _selection_payload()
    checked = selection.validate_selection_response(payload, {"status": "selected", "groups": [_group()]})
    owner = selection.selection_to_on_demand_payloads(chapters, checked)[0]["payload"]
    catalog = on_demand.build_material_catalog(owner)
    trace = on_demand.resolve_material_requests(owner, catalog, {"status": "partial", "material_requests": [{"access_id": "source_materials[0]", "unit_ids": ["CH-A_U01"]}]})
    for messages in (on_demand.access_messages(owner, catalog), on_demand.owner_messages(owner, catalog, trace)):
        text = json.dumps(messages, ensure_ascii=False)
        assert "KEEP-FOCUS: organize comparison before the bounded inference" in text
        assert "autonomous_unit_selector" in text
        assert "advisory_only" in text
        assert _group()["selection_reason"] in text
    assert owner["modifiable_unit_ids"] == ["CH-A_U01", "CH-A_U03"]
    assert {"CH-A_U02", "CH-B_U02"} <= set(owner["read_only_unit_ids"])
    assert owner["source_materials"] == chapters["CH-A"]["source_materials"]


def test_resume_preserves_old_signature_and_reuses_only_matching_new_result(tmp_path, monkeypatch):
    payload, _ = _selection_payload()
    old_choice = {"status": "selected", "groups": [_group(["CH-A_U01", "CH-B_U01"])]}
    old_stage = {"signature": {"request_sha256": "old-global-selection-no-material-index"}, "parsed_response": old_choice, "raw_response": old_choice, "telemetry": {}}
    old_raw = {"signature": old_stage["signature"], "raw_response": old_choice}
    for name, value in (("SELECTION_STAGE.json", old_stage), ("SELECTION_RAW_STAGE.json", old_raw)):
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    calls = []
    monkeypatch.setattr(runtime, "invoke_client", lambda client, messages, **kw: calls.append(messages) or {"status": "none", "groups": []})
    args = dict(client=object(), model="offline", thinking_budget=1, max_output_tokens=1, checkpoint_dir=tmp_path, resume=True)
    first = selection.run_selection(payload, **args)
    assert first["status"] == "no_change"
    assert len(calls) == 1
    archives = [json.loads(path.read_text(encoding="utf-8")) for path in tmp_path.glob("*.incompatible-*.json")]
    assert old_stage in archives and old_raw in archives
    second = selection.run_selection(payload, **args)
    assert second["stage_reused"] is True
    assert second["status"] == "no_change"
    assert len(calls) == 1
    assert first["request_sha256"] == second["request_sha256"]


@pytest.fixture
def selector_cli():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "upgrade3" / "outline_strengthening.py"
    spec = importlib.util.spec_from_file_location("local_selector_cli_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("shape", ["mapping", "list", "single", "plain"])
def test_cli_default_prepare_from_complete_chapter_packets_is_outline_first_and_offline(tmp_path, monkeypatch, selector_cli, shape):
    chapter = _payload("CH-A")
    if shape == "plain":
        # Older callers also pass the plain packet rather than owner envelopes.
        raw = {key: chapter[key] for key in ("research_question", "chapter_id", "chapter_plan", "source_materials", "candidate_materials", "tool_materials")}
    else:
        raw = {"CH-A": chapter} if shape == "mapping" else [chapter] if shape == "list" else chapter
    source = tmp_path / "chapters.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    output = tmp_path / "prepared"
    def no_client(*args, **kwargs):
        pytest.fail("Offline prepare must never construct a paid client")
    monkeypatch.setattr(strengthening, "make_strengthening_client", no_client)
    assert selector_cli.main(["--input", str(source), "--output", str(output), "--mode", "select"]) == 0
    request = json.loads((output / "SELECTION_REQUEST.json").read_text(encoding="utf-8"))
    report = json.loads((output / "PREPARE_REPORT.json").read_text(encoding="utf-8"))
    assert request["include_selection_material_index"] is False
    assert request["model_payload"]["model_visible_projection"]["outline_first"] is True
    assert all(not row["source_supplied_locators"] for row in request["model_payload"]["material_identity_catalog"])
    assert request["model_payload"]["chapters"][0]["chapter_plan"] == chapter["chapter_plan"]
    assert report["cross_chapter_groups_allowed"] is False
    assert report["status"] == "prepared_no_paid_calls"


def test_cli_outline_first_run_reaches_explicit_credentials_gate(tmp_path, monkeypatch, selector_cli):
    source = tmp_path / "chapters.json"
    source.write_text(json.dumps([_payload("CH-A")]), encoding="utf-8")
    args = ["--input", str(source), "--output", str(tmp_path / "prepared"), "--mode", "select", "--no-include-selection-material-index"]
    def no_client(*args, **kwargs):
        pytest.fail("A run without explicit credentials must not construct a paid client")
    monkeypatch.setattr(strengthening, "make_strengthening_client", no_client)
    assert selector_cli.main(args) == 0
    with pytest.raises(SystemExit, match="run_requires_budget_ledger_and_key_file"):
        selector_cli.main(args + ["--run"])


def test_duplicate_identity_with_conflicting_contents_is_not_merged():
    chapter = _payload("CH-A")
    chapter["candidate_materials"] = [
        {"source_handle": "SAME", "paper_id": "same-paper", "usable_content": "FIRST DISTINCT CONTENT"},
        {"source_handle": "SAME", "paper_id": "same-paper", "usable_content": "SECOND DISTINCT CONTENT"},
    ]
    chapter["input_integrity"] = strengthening._input_integrity(chapter)
    visible = selection.model_visible_selection_payload(selection.build_selection_payload([chapter]), include_material_index=True)
    rows = [entry for entry in visible["material_identity_catalog"] if entry["identity"].get("source_handle") == "SAME"]
    assert len(rows) == 2
    assert len({entry["record_sha256"] for entry in rows}) == 2
    assert "FIRST DISTINCT CONTENT" in _entry_material_text(visible, rows)
    assert "SECOND DISTINCT CONTENT" in _entry_material_text(visible, rows)


def test_selector_prompt_is_local_investment_not_general_polish_or_answer_generation():
    payload, _ = _selection_payload()
    text = "\n".join(message["content"] for message in selection.selection_messages(payload))
    for principle in ("不设数量配额", "none/no_change", "同一章节", "边际收益", "篇幅短", "引用少", "反例", "比较维度", "不同问题", "不写正文", "不补充外部证据"):
        assert principle in text
    assert "不强制要求每条证据同时有 A/B" in text


@pytest.mark.parametrize("tamper", ["missing_reference", "changed_finding", "missing_readback"])
def test_explicit_model_projection_cannot_silently_drop_semantic_materials(tamper):
    payload, _ = _selection_payload()
    visible = selection.model_visible_selection_payload(payload, include_material_index=True)
    if tamper == "missing_reference":
        visible["chapters"][0]["material_identity_refs"].pop()
    elif tamper == "changed_finding":
        entry = next(row for row in visible["material_identity_catalog"] if row["source_supplied_locators"])
        entry["source_supplied_locators"][0]["source_excerpt"] = "Forged compressed material"
    else:
        visible["material_identity_catalog"][0]["readback_locations"] = []
    with pytest.raises(ValueError, match="selection_projection_"):
        selection.verify_model_visible_projection(payload, visible)


def test_opt_in_changed_material_conditions_invalidate_cached_selection(tmp_path, monkeypatch):
    chapter = _payload("CH-A")
    first_payload = selection.build_selection_payload([chapter])
    calls = []
    monkeypatch.setattr(runtime, "invoke_client", lambda client, messages, **kw: calls.append(messages) or {"status": "none", "groups": []})
    args = dict(client=object(), model="offline", thinking_budget=1, max_output_tokens=1, checkpoint_dir=tmp_path, resume=True)
    first = selection.run_selection(first_payload, model_payload=selection.model_visible_selection_payload(first_payload, include_material_index=True), **args)
    original_checkpoint = json.loads((tmp_path / "SELECTION_STAGE.json").read_text(encoding="utf-8"))
    chapter["source_materials"][0]["study_summary_A"]["key_findings"][0]["conditions"] = "NEW CONDITION LIMITS INTERPRETATION"
    chapter["input_integrity"] = strengthening._input_integrity(chapter)
    second_payload = selection.build_selection_payload([chapter])
    second = selection.run_selection(second_payload, model_payload=selection.model_visible_selection_payload(second_payload, include_material_index=True), **args)
    assert len(calls) == 2
    assert first["request_sha256"] != second["request_sha256"]
    assert "NEW CONDITION LIMITS INTERPRETATION" in json.dumps(calls[-1])
    assert original_checkpoint in [json.loads(path.read_text(encoding="utf-8")) for path in tmp_path.glob("SELECTION_STAGE.incompatible-*.json")]
    third = selection.run_selection(second_payload, model_payload=selection.model_visible_selection_payload(second_payload, include_material_index=True), **args)
    assert third["stage_reused"] is True
    assert len(calls) == 2


def test_semantic_interning_pointers_resolve_exact_scientific_content():
    payload, atoms = _semantic_payload()
    visible = selection.model_visible_selection_payload(payload, include_material_index=True)
    text = _entry_material_text(visible, visible["material_identity_catalog"])
    for atom in atoms:
        assert atom in text
    for entry in visible["material_identity_catalog"]:
        paths = selection._pointer_target(visible, entry["available_material_paths_ref"])
        assert isinstance(paths, list)
        for locator in entry["source_supplied_locators"]:
            assert isinstance(selection._pointer_target(visible, locator["source_excerpt_ref"]), str)
    assert len(visible["shared_material_contents"]) == len(set(visible["shared_material_contents"]))


def test_long_findings_list_keeps_all_records_past_semantic_budget():
    chapter = _payload("CH-A")
    findings = [{"finding": f"COMPLETE-FINDING-{index}: " + "observed interaction " * 45, "conditions": f"SETTING-{index}", "limits": f"LIMIT-{index}"} for index in range(15)]
    chapter["source_materials"][0]["study_summary_A"]["key_findings"] = findings
    chapter["input_integrity"] = strengthening._input_integrity(chapter)
    assert len(json.dumps(findings)) > 4800
    visible = selection.model_visible_selection_payload(selection.build_selection_payload([chapter]), include_material_index=True)
    text = _entry_material_text(visible, visible["material_identity_catalog"])
    for finding in findings:
        for value in finding.values():
            assert value in text


@pytest.mark.parametrize("field", ["source_excerpt", "available_material_paths"])
def test_forged_inline_value_cannot_override_valid_pointer(field):
    payload, _ = _selection_payload()
    visible = selection.model_visible_selection_payload(payload, include_material_index=True)
    entry = next(row for row in visible["material_identity_catalog"] if row["source_supplied_locators"])
    if field == "source_excerpt":
        entry["source_supplied_locators"][0][field] = "FORGED DIFFERENT SCIENCE"
    else:
        entry[field] = ["FORGED_UNREADABLE_MATERIAL_PATH"]
    with pytest.raises(ValueError):
        selection.verify_model_visible_projection(payload, visible)


def test_no_change_with_groups_is_invalid():
    payload, _ = _selection_payload()
    checked = selection.validate_selection_response(payload, {"status": "no_change", "groups": [_group()]})
    assert checked["status"] == "invalid"
    assert "no_change_with_groups" in checked["validation_errors"]


@pytest.mark.parametrize("malformed", ["validation_errors", "duplicate_groups", "unknown_related"])
def test_projection_rejects_forged_accepted_selection(malformed):
    _, chapters = _selection_payload()
    group = _group()
    forged = {"status": "selected", "groups": [group], "validation_errors": []}
    if malformed == "validation_errors":
        forged["validation_errors"] = ["cross_chapter_group_forbidden:0"]
    elif malformed == "duplicate_groups":
        forged["groups"].append(_group())
    else:
        group["related_read_only_unit_ids"] = ["UNKNOWN_RELATED_UNIT"]
    with pytest.raises(ValueError):
        selection.selection_to_on_demand_payloads(chapters, forged)
    if malformed == "unknown_related":
        with pytest.raises(ValueError):
            selection.project_selection_group(chapters, group)


def test_default_request_is_outline_first_without_unreferenced_material_pool(monkeypatch):
    chapter = _payload("CH-A")
    chapter["candidate_materials"].append({
        "source_handle": "UNREFERENCED-HUGE-CANDIDATE",
        "paper_id": "unreferenced-paper",
        "usable_content": "UNREFERENCED-BULK-FINDING " * 5000,
    })
    chapter["input_integrity"] = strengthening._input_integrity(chapter)
    payload = selection.build_selection_payload([chapter])
    visible = selection.model_visible_selection_payload(payload)
    assert visible["chapters"][0]["chapter_plan"] == chapter["chapter_plan"]
    assert visible["model_visible_projection"]["material_visibility"] == "plan_referenced_navigation_only"
    assert all(not row["source_supplied_locators"] for row in visible["material_identity_catalog"])
    assert {row["identity"].get("source_handle") for row in visible["material_identity_catalog"]} == {"P0001", "P0002", "P0003"}
    assert "shared_material_contents" not in visible
    captured = []
    monkeypatch.setattr(runtime, "invoke_client", lambda client, messages, **kw: captured.append(messages) or {"status": "none", "groups": []})
    result = selection.run_selection(payload, client=object(), model="offline", thinking_budget=1, max_output_tokens=1)
    assert result["status"] == "no_change"
    text = json.dumps(captured[0])
    assert "UNREFERENCED-BULK-FINDING" not in text
    assert len(text) < 30000
    # Full materials remain untouched for the existing downstream owner.
    assert "UNREFERENCED-BULK-FINDING" in chapter["candidate_materials"][-1]["usable_content"]
