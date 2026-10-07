"""Offline promotion seams using recorded owner output and controlled consumers.

The calls below capture production arranger/writer messages. Controlled model
responses check field delivery and identity, not generated scientific quality.
"""

from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import review_unit_writer as writing


ROOT = Path(__file__).resolve().parents[2]
RECORDING = ROOT / "docs/verification/on-demand-efficiency-20261007"
DETAIL_FIELDS = ("case_objects", "supporting_studies", "concrete_studies",
                 "cases_and_sources", "cases_and_references", "synthesis_and_transition")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Promotion guidance tests must stay offline")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class Capture:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), deepcopy(kwargs)))
        return {"content": json.dumps(self.response, ensure_ascii=False),
                "complete": True, "finish_reason": "stop"}


def _controlled_arrangement(view):
    units = [{"unit_id": unit.unit_id, "paragraph_tasks": [
        {"paragraph_id": brief.paragraph_id, "source_briefs": [brief.paragraph_id],
         "point": brief.point, "development": brief.development,
         "source_uses": [{"source_handle": handle, "role": "support", "use": "Owner task"}
                         for handle in brief.source_handles]}
        for brief in unit.paragraph_briefs]}
        for unit in view.units]
    placed = {use["source_handle"] for unit in units for task in unit["paragraph_tasks"]
              for use in task["source_uses"]}
    return {"chapter_id": view.chapter_id, "units": units,
            "unused_sources": [{"source_handle": source.source_handle,
                                "reason": "Controlled arrangement leaves this case as context"}
                               for source in view.sources if source.source_handle not in placed]}


def _chain(tmp_path, packet, *, revision=True, legacy_arrangement=False):
    before = deepcopy(packet)
    packet_path = tmp_path / "PACKET.json"
    _dump(packet_path, packet)
    view = arranging.build_chapter_view(packet_path, id_map_path=tmp_path / "ID_MAP.json")
    editor = Capture(_controlled_arrangement(view))
    arrangement = arranging.run_arrangement(
        view, client=editor, model="offline-promotion",
        view_payload=view.arrangement_payload(max_source_chars=8), planning_revision=revision)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    assert len(editor.calls) == 1
    editor_payload = json.loads(editor.calls[0][0][-1]["content"])
    _dump(tmp_path / "ARRANGER_MESSAGES.json", editor.calls[0][0])
    saved = deepcopy(arrangement)
    if legacy_arrangement:
        # An older export can lack all owner context; reconstruct from the
        # authoritative view, not whatever the controlled editor echoes.
        for unit in saved["units"]:
            unit.pop("owner_unit_context")
    saved["source_catalog"] = arranging.build_source_catalog(view, arrangement)
    _dump(tmp_path / "CHAPTER_ARRANGEMENT.json", saved)
    arranging.write_view(view, tmp_path / "ARRANGEMENT_INPUT.json")
    writer_payloads = {}
    for unit in view.units:
        wview = writing.build_unit_view(tmp_path / "CHAPTER_ARRANGEMENT.json", unit.unit_id)
        client = Capture({"body_markdown": "Controlled fixture response."})
        writing.run_unit_writing(wview, client=client, model="offline-promotion", planning_revision=revision)
        assert len(client.calls) == 1
        writer_payloads[unit.unit_id] = json.loads(client.calls[0][0][-1]["content"])
        _dump(tmp_path / (unit.unit_id + "_WRITER_MESSAGES.json"), client.calls[0][0])
        completion = writing.completion_messages(
            wview, "Existing text", [wview.paragraph_tasks[0]["paragraph_id"]], planning_revision=revision)
        assert json.loads(completion[-1]["content"])["owner_unit_context"] == wview.owner_unit_context
    assert packet == before == _read(packet_path)
    return view, arrangement, editor_payload, writer_payloads


def _assert_owner_context(original, context):
    for field in DETAIL_FIELDS:
        if field in original:
            assert context[field] == original[field]
        else:
            assert field not in context
    if "argument_relations" in original:
        assert context["argument_relations"] == original["argument_relations"]


@pytest.mark.parametrize("revision", [False, True])
def test_recorded_plan_guidance_reaches_actual_arranger_and_writer_messages(tmp_path, revision):
    message = _read(RECORDING / "integration_handoff/OWNER_MESSAGES.json")["messages"][1]["content"]
    owner = strengthening.expand_material_projection(json.JSONDecoder().raw_decode(message[message.index("{"):])[0])
    plan = _read(RECORDING / "UPDATED_PLAN.json")
    response = _read(RECORDING / "integration_handoff/OWNER_RESPONSE.json")["model_output"]["parsed_output"]
    assert response["chapter_updates"][0]["updated_plan"] == plan
    packet = strengthening.project_plan_for_arrangement(owner, {
        "status": "updated", "updated_plan": plan, "unit_id_remap": response["unit_id_remap"],
        "accepted_source_materials": owner["source_materials"],
    })
    view, arrangement, editor, writers = _chain(tmp_path, packet, revision=revision)
    assert arrangement["unit_id_remap"] == response["unit_id_remap"]
    assert sum(len(unit["case_objects"]) for unit in plan["units"]) == 10
    assert sum(len(unit["supporting_studies"]) for unit in plan["units"]) == 18
    assert sum(len(payload["paragraph_tasks"]) for payload in writers.values()) == 7
    sources = {row["source_handle"]: row for row in packet["source_materials"]}
    for original, sent, serialized in zip(plan["units"], editor["units"], view.to_dict()["units"]):
        assert original["unit_id"] == sent["unit_id"] == serialized["unit_id"]
        _assert_owner_context(original, sent)
        _assert_owner_context(original, serialized)
        writer = writers[original["unit_id"]]
        _assert_owner_context(original, writer["owner_unit_context"])
        cases = [row for row in sent["case_level_uses"] if row["field"] == "case_objects"]
        assert len(cases) == len(original["case_objects"])
        assert [row["purpose"] for row in cases] == [row["finding"] for row in original["case_objects"]]
        uses = [row for row in sent["case_level_uses"] if row["field"] == "supporting_studies"]
        assert [row["conditions"] for row in uses] == [row["conditions"] for row in original["supporting_studies"]]
        assert [row["limits"] for row in uses] == [row["limits"] for row in original["supporting_studies"]]
        # Owner-specific guidance must arrive as task context even where it is
        # not a verbatim paragraph task or merely available somewhere in A/B.
        paragraphs = json.dumps(original["paragraph_briefs"], ensure_ascii=False)
        assert any(case["finding"] not in paragraphs for case in original["case_objects"])
        for task, brief in zip(writer["paragraph_tasks"], original["paragraph_briefs"]):
            assert task["paragraph_id"] == brief["paragraph_id"]
            assert task["source_briefs"] == [brief["paragraph_id"]]
            for field in ("point", "development", "source_handles"):
                assert task["source_brief_details"][0][field] == brief[field]
        for material in writer["sources"]:
            for field in ("paper_id", "doi", "study_summary_A", "review_planning_B"):
                assert material[field] == sources[material["source_handle"]][field]


def _cross_domain_packet():
    conditions = {"temperature_K": 300, "illumination": {"spectrum": "controlled", "duration_s": 30}}
    limits = ["Short illumination window only", {"excluded": ["outdoor lifetime", "thermal cycling"]}]
    case = {"case_id": "C-STABILITY", "source_handle": "P0001", "paper_id": "solar-one",
            "finding": "Case-only boundary: reversible recovery does not establish field lifetime.",
            "conditions": conditions, "limits": limits,
            "provenance": {"table": "S2", "comparison": "same device before and after rest"}}
    unit = {"unit_id": "SOLAR_U1", "substantive_point": "Separate measured recovery from durability",
            "paragraph_briefs": [{"paragraph_id": "SOLAR_B1", "point": "Explain the recovery measurement",
                                  "development": "Relate the result to its measurement window.",
                                  "source_handles": ["P0001"], "finding_conditions": conditions}],
            "case_objects": [case],
            "supporting_studies": [{"source_handle": "P0001", "paper_id": "solar-one",
                                    "contribution": "A recovery measurement", "conditions": conditions,
                                    "limits": limits}],
            "synthesis": "Keep the existing synthesis independent.",
            "transition": "Keep the existing transition independent.",
            "synthesis_and_transition": {"synthesis": "Recovery is conditional.",
                                         "transition": ["Next assess long-duration tests."],
                                         "source_handles": ["P0001"]},
            "argument_relations": [{"to": "SOLAR_U2", "relation": "motivates lifetime testing"}]}
    return {"chapter_id": "SOLAR", "chapter_plan": {"chapter_id": "SOLAR", "units": [unit]},
            "source_materials": [{"source_handle": "P0001", "paper_id": "solar-one",
                                  "title": "Controlled solar-cell fixture",
                                  "study_summary_A": {"key_findings": ["A measured recovery"]},
                                  "review_planning_B": {"planning_summary": "Explain the observation"}}]}


@pytest.mark.parametrize("revision", [False, True])
@pytest.mark.parametrize("legacy_arrangement", [False, True])
def test_structured_cross_domain_cases_keep_shape_and_separate_synthesis(tmp_path, revision, legacy_arrangement):
    packet = _cross_domain_packet()
    original = packet["chapter_plan"]["units"][0]
    # Existing aliases retain their distinct complete records and field names.
    for field in ("concrete_studies", "cases_and_sources", "cases_and_references"):
        original[field] = [{"source_handle": "P0001", "use": field,
                            "conditions_limits": {"setting": "same device", "limit": field},
                            "extra_boundary": "Owner-selected detail absent from paragraph text"}]
    view, arrangement, editor, writers = _chain(
        tmp_path, packet, revision=revision, legacy_arrangement=legacy_arrangement)
    context = writers["SOLAR_U1"]["owner_unit_context"]
    for delivered in (view.to_dict()["units"][0], editor["units"][0],
                      arrangement["units"][0]["owner_unit_context"], context):
        _assert_owner_context(original, delivered)
        assert delivered["synthesis"] == original["synthesis"]
        assert delivered["transition"] == original["transition"]
    use = next(row for row in editor["units"][0]["case_level_uses"] if row["field"] == "supporting_studies")
    assert use["conditions"] == original["supporting_studies"][0]["conditions"]
    assert use["limits"] == original["supporting_studies"][0]["limits"]


def test_missing_optional_owner_fields_are_not_invented(tmp_path):
    packet = _cross_domain_packet()
    unit = packet["chapter_plan"]["units"][0]
    for field in DETAIL_FIELDS:
        unit.pop(field, None)
    _, _, editor, writers = _chain(tmp_path, packet)
    _assert_owner_context(unit, editor["units"][0])
    _assert_owner_context(unit, writers["SOLAR_U1"]["owner_unit_context"])


@pytest.mark.parametrize("variant_only", [False, True])
def test_complementary_ab_variants_reach_consumers_as_one_source(tmp_path, variant_only):
    packet = _cross_domain_packet()
    source = packet["source_materials"][0]
    source["study_summary_A"].update(negative_findings=[], uncertainty=None, measured_change=0)
    source["review_planning_B"].update(conditions={"window_s": 30}, limits=[],
                                     provenance={"round": "first"}, extrapolation_supported=False)
    source["study_summary_A_variants"] = [{"finding": "Complementary recovery result",
        "conditions": {"window_s": 120}, "limits": ["One device"], "provenance": {"round": "second"}}]
    source["review_planning_B_variants"] = [{"planning_summary": "Retain complementary conditions",
        "conditions": {"window_s": 120}, "limits": ["Do not infer field lifetime"]}]
    if variant_only:
        source.pop("study_summary_A")
        source.pop("review_planning_B")
    view, arrangement, editor, writers = _chain(tmp_path, packet)
    writer_sources = writers["SOLAR_U1"]["sources"]
    assert len(view.sources) == len(editor["sources"]) == len(writer_sources) == 1
    for field in ("study_summary_A_variants", "review_planning_B_variants"):
        assert view.to_dict()["sources"][0][field] == source[field]
        assert arranging.build_source_catalog(view, arrangement)["P0001"][field] == source[field]
        assert editor["sources"][0][field] == source[field]
        assert writer_sources[0][field] == source[field]
    if not variant_only:
        for field in ("study_summary_A", "review_planning_B"):
            assert writer_sources[0][field] == source[field]
    assert writer_sources[0]["material_status"] == "resolved"
    assert arranging.source_usage_summary(view, arrangement)["distinct_papers"] == 1
    writer_view = writing.build_unit_view(tmp_path / "CHAPTER_ARRANGEMENT.json", "SOLAR_U1")
    assert not any(row["code"] == "sources_without_any_material" for row in writer_view.warnings)


def test_doi_alias_keeps_complementary_ab_without_creating_extra_source(tmp_path):
    packet = _cross_domain_packet()
    primary = packet["source_materials"][0]
    primary["doi"] = "10.0000/controlled"
    alias = deepcopy(primary)
    alias.update(source_handle="P0002", paper_id="solar-alias",
                 study_summary_A={"finding": "Alias-only complementary measurement"},
                 review_planning_B={"planning_summary": "Alias-only use", "conditions": "A separate window"})
    packet["source_materials"].append(alias)
    packet["chapter_plan"]["units"][0]["paragraph_briefs"][0]["source_handles"].append("P0002")
    view, _, editor, writers = _chain(tmp_path, packet)
    assert len(view.sources) == len(writers["SOLAR_U1"]["sources"]) == 1
    assert writers["SOLAR_U1"]["sources"][0]["aliases"] == ["P0002"]
    for field in ("study_summary_A", "review_planning_B"):
        assert writers["SOLAR_U1"]["sources"][0][field] == primary[field]
        assert writers["SOLAR_U1"]["sources"][0][field + "_variants"] == [alias[field]]
        assert editor["sources"][0][field + "_variants"] == [alias[field]]
