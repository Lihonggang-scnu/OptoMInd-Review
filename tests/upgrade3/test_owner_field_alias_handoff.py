"""Alias delivery only: synthetic owner fields, no scientific generation."""
from copy import deepcopy
import json

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import review_unit_writer as writing
from test_owner_condition_relation_handoff import _packet, _view, _arranged, _writer


@pytest.mark.parametrize("fields,condition_key,relation_key,title_key,development_key", [
    ({"theme": "Synthetic optical scope", "ordered_development": "Explain the supplied setting " * 100,
      "evidence_conditions_limits": "Synthetic fixed angle", "relations_conditions_limits": "Synthetic boundary"},
     "evidence_conditions_limits", "relations_conditions_limits", "theme", "ordered_development"),
    ({"unit_title": "Synthetic chemical scope", "development": ["First setting", {"then": "Second setting"}],
      "conditions_and_limits": {"temperature": "synthetic", "duration": [2, 3]},
      "relations_and_limits": {"parallel": ["Synthetic A", "Synthetic B"]}},
     "conditions_and_limits", "relations_and_limits", "unit_title", "development"),
    ({"title": "Synthetic ecological scope", "ordered_development": "Retain observation period",
      "evidence_conditions_and_limits": ["Synthetic period", {"limit": "Synthetic boundary"}],
      "material_supported_relations": ["Synthetic relation", {"scale": "Synthetic scale"}]},
     "evidence_conditions_and_limits", "material_supported_relations", "title", "ordered_development"),
    ({"theme": "Synthetic conceptual scope", "development": "Explain each supplied duty",
      "evidence_conditions_limits": "Synthetic object", "relations_limits": "Synthetic relation"},
     "evidence_conditions_limits", "relations_limits", "theme", "development"),
])
def test_aliases_reach_actual_arrangement_and_writer_messages_with_original_types(
        tmp_path, fields, condition_key, relation_key, title_key, development_key):
    packet = _packet("Original paragraph condition", None)
    owner = packet["chapter_plan"]["units"][0]
    owner.pop("argument_relations")
    owner.pop("synthesis_and_conflicts")
    owner.update(deepcopy(fields))
    before = deepcopy(packet)
    view = _view(tmp_path, packet)
    messages = arranging.arrangement_messages(view.arrangement_payload(max_source_chars=8), planning_revision=True)
    sent = json.loads(messages[-1]["content"])["units"][0]
    assert sent["title"] == fields[title_key]
    assert sent["ordered_development"] == str(fields[development_key])
    assert sent["evidence_conditions"] == str(fields[condition_key])
    assert sent["synthesis"] == str(fields[relation_key])
    assert sent["argument_relations"] == fields[relation_key]
    for key, value in fields.items():
        assert sent["owner_field_values"][key] == value
    validated = arranging.validate_arrangement(_arranged(view), view, planning_revision=True)
    assert validated["validation"]["ok"]
    context = validated["units"][0]["owner_unit_context"]
    writer_view = _writer(tmp_path, view, validated)
    payload = json.loads(writing.unit_messages(writer_view, planning_revision=True)[-1]["content"])
    assert payload["owner_unit_context"] == context
    for key, value in fields.items():
        assert context["owner_field_values"][key] == value
    assert packet == before


def test_existing_canonical_precedence_keeps_complementary_raw_aliases(tmp_path):
    packet = _packet("Paragraph condition", {"canonical": "Original relation"})
    owner = packet["chapter_plan"]["units"][0]
    owner.update({"unit_title": "First title", "title": "Second title", "theme": "Complementary theme",
                  "ordered_development": "Canonical development", "development": {"extra": "Complementary development"},
                  "evidence_conditions_and_limits": "First conditions", "evidence_conditions": "Second conditions",
                  "evidence_conditions_limits": {"extra": "Complementary condition"},
                  "relations_conditions_limits": "Complementary relation"})
    view = _view(tmp_path, packet)
    unit = view.units[0]
    assert unit.title == "First title"
    assert unit.ordered_development == "Canonical development"
    assert unit.evidence_conditions == "First conditions"
    assert unit.synthesis == "Keep this legacy synthesis separate."
    assert unit.argument_relations == {"canonical": "Original relation"}
    validated = arranging.validate_arrangement(_arranged(view), view, planning_revision=True)
    # Older arrangements reconstruct the same authoritative context from the
    # saved view, including all complementary aliases, without writer changes.
    validated["units"][0].pop("owner_unit_context")
    payload = json.loads(writing.unit_messages(_writer(tmp_path, view, validated), planning_revision=True)[-1]["content"])
    raw = payload["owner_unit_context"]["owner_field_values"]
    for key in ("unit_title", "title", "theme", "development", "evidence_conditions", "evidence_conditions_limits", "relations_conditions_limits"):
        assert raw[key] == owner[key]
    assert "paragraph_briefs" not in raw and "source_materials" not in raw


def test_cases_and_supporting_studies_keep_complementary_findings_conditions_and_context(tmp_path):
    packet = _packet("Paragraph condition", "Original relation")
    owner = packet["chapter_plan"]["units"][0]
    owner["supporting_studies"] = [{"source_handle": "P0001", "contribution": "Existing study use", "conditions": "Existing setting"}]
    owner["cases"] = [{"source_handle": "P0001", "finding": "Additional original case", "conditions": {"setting": "First"}},
                      {"source_handle": "P0002", "finding": "Another original case", "conditions": ["Second", "Bounded"]}]
    view = _view(tmp_path, packet)
    sent = json.loads(arranging.arrangement_messages(view.arrangement_payload(), planning_revision=True)[-1]["content"])["units"][0]
    uses = sent["case_level_uses"]
    assert [(u["field"], u["source_handle"]) for u in uses] == [("supporting_studies", "P0001"), ("cases", "P0001"), ("cases", "P0002")]
    assert [(u["purpose"], u["conditions"]) for u in uses if u["field"] == "cases"] == [
        (c["finding"], c["conditions"]) for c in owner["cases"]]
    validated = arranging.validate_arrangement(_arranged(view), view, planning_revision=True)
    assert validated["validation"]["ok"]
    payload = json.loads(writing.unit_messages(_writer(tmp_path, view, validated), planning_revision=True)[-1]["content"])
    assert payload["owner_unit_context"]["cases"] == owner["cases"]
    assert payload["owner_unit_context"]["supporting_studies"] == owner["supporting_studies"]
