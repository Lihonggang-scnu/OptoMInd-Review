"""Offline consumer contracts: preserve complete science across capacity settings."""
from copy import deepcopy
import inspect
import json

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import review_unit_writer as writing
from scripts.upgrade3 import chapter_arrangement as arrangement_cli
from scripts.upgrade3 import review_unit_writer as writer_cli
from test_owner_condition_relation_handoff import _packet, _view, _arranged, _writer, Capture


def long_text(label, length=25000):
    return (label + " evidence; ") * (length // len(label + " evidence; ")) + " TAIL_" + label


@pytest.mark.parametrize("limit", [None, 0, 8, 240, 900])
def test_arrangement_preserves_scientific_fields_and_all_source_cautions(tmp_path, limit):
    conditions = {"negative_finding": long_text("negative"), "window": long_text("window")}
    view = _view(tmp_path, _packet(conditions, {"relation": long_text("relation")}))
    for field in ("purpose", "scope", "thesis", "reader_objective", "review_argument"):
        setattr(view, field, long_text(field, 2000))
    unit = view.units[0]
    for field in ("substantive_point", "ordered_development", "evidence_conditions", "synthesis", "transition"):
        setattr(unit, field, long_text(field, 2000))
    unit.case_uses = [{"source_handle": "P0001", "field": "supporting_studies",
                      "text": long_text("case"), "conditions": conditions}]
    source = view.sources[0]
    source.title = long_text("title", 2000)
    source.planning_view = {"planning_summary": long_text("summary"),
                            "scope_interpretation_cautions": [long_text("caution_" + str(i), 1500) for i in range(4)]}
    view.source_uses[source.source_handle] = [{"kind": "case", "text": long_text("case_use")}]
    before = deepcopy(view.to_dict())
    payload = view.arrangement_payload(**({"max_source_chars": limit} if limit is not None else {}))
    for field in ("purpose", "scope", "thesis", "reader_objective", "review_argument"):
        assert payload[field] == getattr(view, field)
    for field in ("substantive_point", "ordered_development", "evidence_conditions", "synthesis", "transition"):
        assert payload["units"][0][field] == getattr(unit, field)
    assert payload["units"][0]["existing_paragraph_tasks"] == before["units"][0]["paragraph_briefs"]
    assert payload["units"][0]["case_level_uses"][0]["conditions"] == conditions
    assert payload["sources"][0]["material_purpose"] == source.planning_view["planning_summary"]
    assert payload["sources"][0]["conditions"] == source.planning_view["scope_interpretation_cautions"]
    assert payload["sources"][0]["current_uses"][0]["purpose"] == long_text("case_use")
    if not limit:
        assert payload["sources"][0]["title"] == source.title
    else:
        assert payload["sources"][0]["title"] == source.title[:limit] + " …"
    client = Capture(_arranged(view))
    arranging.run_arrangement(view, client=client, model="offline", prompt="Fixture", view_payload=payload)
    sent = json.loads(client.calls[0][0][-1]["content"])
    assert sent == payload
    assert view.to_dict() == before


@pytest.mark.parametrize("limit", [None, 0, 400, 20000])
@pytest.mark.parametrize("revision", [False, True])
def test_writer_and_completion_preserve_long_source_records(tmp_path, limit, revision):
    view = _view(tmp_path, _packet(long_text("owner_condition"), {"relation": "bounded"}))
    science = {
        "study_summary_A": {"negative_findings": long_text("A_negative")},
        "review_planning_B": {"scope_interpretation_cautions": [long_text("B_condition")]},
        "deep_read_material": {"answer": long_text("deep_answer")},
        "deep_read_materials": [{"answer": long_text("deep_variant")}],
        "supplement_material": {"answer": long_text("supplement")},
        "supplement_materials": [{"answer": long_text("supplement_variant")}],
        "local_passages": {"passage": long_text("local_negative")},
        "local_passages_variants": [{"passage": long_text("local_variant")}],
        "tool_supplement_materials": [{"need_id": "need-one", "usable_content": long_text("tool_negative")}],
    }
    arranged = arranging.validate_arrangement(_arranged(view), view)
    _writer(tmp_path, view, arranged)
    path = tmp_path / "CHAPTER_ARRANGEMENT.json"
    saved = json.loads(path.read_text())
    saved["source_catalog"]["P0001"].update(deepcopy(science))
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    wview = writing.build_unit_view(path, "U1", **({"max_material_chars_per_source": limit} if limit is not None else {}))
    material = wview.materials[0]
    encoded = json.dumps(material)
    for key, value in science.items():
        # Deep-read text uses its existing safe representation; other records keep their shape.
        if key not in ("deep_read_material", "deep_read_materials", "tool_supplement_materials"):
            assert material[key] == value
        assert "TAIL_" in json.dumps(value)
    markers = ["A_negative", "B_condition", "deep_answer", "deep_variant", "supplement", "supplement_variant",
               "local_negative", "local_variant", "tool_negative"]
    assert all("TAIL_" + marker in encoded for marker in markers)
    assert "material_truncated" not in material
    assert wview.material_summary()["truncated"] == []
    if limit:
        diagnostic = material["material_capacity_diagnostic"]
        assert diagnostic["threshold_chars"] == limit
        assert diagnostic["material_preserved"] is True
        assert diagnostic["original_chars"] == diagnostic["preserved_chars"] > limit
        assert wview.warnings[0]["code"] == "material_capacity_threshold_exceeded"
    else:
        assert "material_capacity_diagnostic" not in material
    normal = writing.unit_messages(wview, prompt="Fixture", planning_revision=revision)
    completion = writing.completion_messages(wview, "Original BODY remains intact", ["B1"], prompt="Fixture", planning_revision=revision)
    for messages in (normal, completion):
        sent = json.loads(messages[-1]["content"])
        assert sent["sources"][0] == material
        assert all("TAIL_" + marker in messages[-1]["content"] for marker in markers)
        assert "TAIL_owner_condition" in messages[-1]["content"]
        assert sent["paragraph_tasks"][0]["paragraph_id"] == "B1"
    client = Capture({"body_markdown": "Supported [P0001].", "status": "appended",
                      "covered_task_ids": ["B1"]})
    writing.run_unit_writing(wview, client=client, model="offline", prompt="Fixture", planning_revision=revision)
    writing.run_unit_completion(wview, existing_body="Original BODY remains intact", task_ids=["B1"],
                                client=client, model="offline", prompt="Fixture", planning_revision=revision)
    assert len(client.calls) == 2
    for messages, _ in client.calls:
        assert all("TAIL_" + marker in messages[-1]["content"] for marker in markers)
    counted = []
    def counter(request, messages):
        counted.append(deepcopy(messages))
        return len(json.dumps(messages))
    writing.estimate_unit_cost(normal, token_counter=counter)
    assert counted == [normal]
    def overflowing_counter(request, messages):
        assert messages == normal
        return 1_000_001
    oversized = writing.estimate_unit_cost(normal, token_counter=overflowing_counter)
    assert oversized["input_capacity"]["exceeds_capacity"]
    assert oversized["input_capacity"]["action"] == "split_task_before_model_call"
    assert oversized["input_capacity"]["material_preserved"] is True
    assert path.read_bytes() == before


def test_library_and_cli_defaults_are_unlimited():
    assert arranging.DEFAULT_MAX_SOURCE_CHARS == 0
    assert arrangement_cli._parser().parse_args([]).max_source_chars == 0
    assert inspect.signature(arranging.ChapterView.arrangement_payload).parameters["max_source_chars"].default == 0
    assert writing.DEFAULT_MAX_MATERIAL_CHARS_PER_SOURCE == 0
    assert writer_cli._parser().parse_args(["--arrangement", "unused"]).max_material_chars_per_source == 0
    assert inspect.signature(writing.build_unit_view).parameters["max_material_chars_per_source"].default == 0


@pytest.mark.parametrize("cautions", [long_text("text_caution"),
                                     {"conditions": long_text("nested_caution"), "negative": False}])
def test_arrangement_cautions_preserve_original_record_shape(tmp_path, cautions):
    view = _view(tmp_path, _packet("condition", "relation"))
    view.sources[0].planning_view["scope_interpretation_cautions"] = deepcopy(cautions)
    assert view.arrangement_payload(max_source_chars=1)["sources"][0]["conditions"] == cautions


@pytest.mark.parametrize("stage", ["arrangement", "writer", "completion"])
@pytest.mark.parametrize("overflow", [False, "input", "total_context"])
def test_actual_dispatch_guards_full_input_before_any_call(tmp_path, stage, overflow):
    view = _view(tmp_path, _packet(long_text("guard_condition"), {"relation": "bounded"}))
    arranged = arranging.validate_arrangement(_arranged(view), view)
    wview = _writer(tmp_path, view, arranged)
    response = _arranged(view) if stage == "arrangement" else {
        "body_markdown": "Supported [P0001].", "status": "appended", "covered_task_ids": ["B1"]}
    client = Capture(response)
    measured = []
    def counter(request_bytes, messages):
        assert b"TAIL_guard_condition" in request_bytes
        assert "TAIL_guard_condition" in messages[-1]["content"]
        measured.append(deepcopy(messages))
        return 1_000_001 if overflow == "input" else 860_000 if overflow else 100_000
    client.prompt_token_counter = counter
    def run():
        if stage == "arrangement":
            return arranging.run_arrangement(view, client=client, model="qwen3.5-plus", prompt="Fixture")
        if stage == "writer":
            return writing.run_unit_writing(wview, client=client, model="qwen3.5-plus", prompt="Fixture")
        return writing.run_unit_completion(wview, existing_body="Original body", task_ids=["B1"],
                                           client=client, model="qwen3.5-plus", prompt="Fixture")
    if overflow:
        error_type = arranging.ChapterArrangementError if stage == "arrangement" else writing.UnitWritingError
        with pytest.raises(error_type, match="input_requires_batching") as caught:
            run()
        assert not client.calls
        assert caught.value.record["model_calls"] == 0
        assert caught.value.record["material_preserved"]
        assert caught.value.record["messages"] == measured[0]
    else:
        run()
        assert len(client.calls) == 1
        assert client.calls[0][0] == measured[0]
    assert len(measured) == 1


@pytest.mark.parametrize("stage", ["arrangement", "writer", "completion"])
def test_actual_dispatch_fallback_counts_entire_untrimmed_input(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(writing, "_default_qwen_token_counter", lambda: None)
    view = _view(tmp_path, _packet(long_text("fallback_tail", 1_050_000), {"relation": "bounded"}))
    arranged = arranging.validate_arrangement(_arranged(view), view)
    wview = _writer(tmp_path, view, arranged)
    client = Capture({"body_markdown": "Must never run"})
    error_type = arranging.ChapterArrangementError if stage == "arrangement" else writing.UnitWritingError
    with pytest.raises(error_type, match="input_requires_batching") as caught:
        if stage == "arrangement":
            arranging.run_arrangement(view, client=client, model="qwen3.5-plus", prompt="Fixture")
        elif stage == "writer":
            writing.run_unit_writing(wview, client=client, model="qwen3.5-plus", prompt="Fixture")
        else:
            writing.run_unit_completion(wview, existing_body="Original body", task_ids=["B1"],
                                        client=client, model="qwen3.5-plus", prompt="Fixture")
    assert not client.calls
    assert caught.value.record["estimate"]["tokenizer"] == "utf8_conservative_upper_bound"
    assert "TAIL_fallback_tail" in caught.value.record["messages"][-1]["content"]


@pytest.mark.parametrize("stage", ["arrangement", "writer", "completion"])
@pytest.mark.parametrize("raw_tokens,multiplier,framing", [(840_000, 1.12, 8192), (940_000, 1.0, 0)])
def test_real_client_counter_capacity_margin_is_applied_only_once(
        tmp_path, monkeypatch, stage, raw_tokens, multiplier, framing):
    from optomind_research.runtime.upgrade3.module4.runtime import QwenDirectClient

    view = _view(tmp_path, _packet("condition", {"relation": "bounded"}))
    wview = _writer(tmp_path, view, arranging.validate_arrangement(_arranged(view), view))
    counted = []
    def counter(raw, messages):
        assert raw and messages
        counted.append(deepcopy(messages))
        return raw_tokens
    # The configured allowance plus the output fits the real 1M context.
    # Repeating the allowance or substituting default margins would reject it.
    client = QwenDirectClient(model="qwen3.5-plus", json_mode=False,
                              prompt_token_counter=counter,
                              prompt_token_multiplier=multiplier,
                              prompt_token_framing_margin=framing)
    calls = []
    def offline_dispatch(self, messages, **kwargs):
        calls.append(deepcopy(messages))
        response = _arranged(view) if stage == "arrangement" else {
            "body_markdown": "Supported [P0001].", "status": "appended", "covered_task_ids": ["B1"]}
        return {"content": json.dumps(response), "complete": True, "finish_reason": "stop"}
    monkeypatch.setattr(QwenDirectClient, "__call__", offline_dispatch)
    if stage == "arrangement":
        arranging.run_arrangement(view, client=client, model="qwen3.5-plus", prompt="Fixture", thinking_budget=16384)
    elif stage == "writer":
        writing.run_unit_writing(wview, client=client, model="qwen3.5-plus", prompt="Fixture")
    else:
        writing.run_unit_completion(wview, existing_body="Original", task_ids=["B1"],
                                    client=client, model="qwen3.5-plus", prompt="Fixture")
    assert len(calls) == len(counted) == 1
    assert calls == counted


def test_explicit_estimate_margin_matches_transport_rounding():
    estimate = writing.estimate_unit_cost(
        [{"role": "user", "content": "Full input"}], token_counter=lambda raw, rows: 101,
        prompt_token_multiplier=1.05, prompt_token_framing_margin=7)
    assert estimate["reserved_input_tokens"] == 114
    assert estimate["prompt_token_multiplier"] == 1.05
    assert estimate["prompt_token_framing_margin"] == 7
