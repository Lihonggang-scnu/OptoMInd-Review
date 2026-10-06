"""Offline regressions against actual access/owner messages, without paid calls."""
from __future__ import annotations

import copy
import json

import pytest

from optomind_research.runtime.upgrade3 import outline_on_demand as demand
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from scripts.upgrade3 import outline_strengthening as cli


def _payload(record):
    return strengthening.build_strengthening_payload(
        research_question="Which findings apply under which conditions?",
        chapter_id="CH01",
        chapter_plan={"chapter_id": "CH01", "units": [{"unit_id": "CH01_U01"}]},
        source_materials=[record],
    )


def _record(finding="Treatment improved the measured outcome", condition="Only in cohort A"):
    return {
        "source_handle": "P0001", "paper_id": "study-1", "title": "Study",
        "study_summary_A": {
            "approach": "LONG-METHOD-ONLY " * 700,
            "problem_or_question": "Does the relation generalize?",
            "research_scope": "A bounded comparison",
            "key_findings": [{"finding": finding, "conditions": condition}],
            "contribution_and_limits": [{"contribution": "Conditional evidence", "limits": "No extrapolation"}],
        },
        "review_planning_B": {"planning_summary": "Compare the conditional results", "topic_handles": ["comparison"]},
    }


def _visible_locators(messages):
    value = json.loads(messages[1]["content"])
    return value["material_catalog"]["entries"][0]["source_supplied_locators"]


@pytest.mark.parametrize("finding,condition", [
    ("Optical response peaks at 820 nm", "Only for the measured polarization"),
    ("Clinical response differs between arms", "Adults in the randomized cohort only"),
    ("Historical accounts disagree on the date", "Only the surviving archive was reviewed"),
])
def test_semantic_material_visible_after_long_method_in_actual_messages(finding, condition):
    row = _record(finding, condition)
    original = copy.deepcopy(row)
    assert finding not in json.dumps(row["study_summary_A"], ensure_ascii=False)[:600]
    payload = _payload(row)
    catalog = demand.build_material_catalog(payload)
    access = demand.access_messages(payload, catalog)
    locators = {item["path"]: item for item in _visible_locators(access)}
    summary = json.loads(locators["study_summary_A"]["source_excerpt"])
    assert summary["key_findings"] == original["study_summary_A"]["key_findings"]
    assert summary["problem_or_question"] == "Does the relation generalize?"
    assert summary["research_scope"] == "A bounded comparison"
    assert summary["contribution_and_limits"] == original["study_summary_A"]["contribution_and_limits"]
    assert locators["study_summary_A"]["omitted_material_paths"] == ["study_summary_A.approach"]
    assert locators["study_summary_A"]["omitted_materials_requestable"] is True
    assert json.loads(locators["review_planning_B"]["source_excerpt"]) == row["review_planning_B"]
    unread = demand.resolve_material_requests(payload, catalog, {"status": "no_change"})
    owner_text = json.dumps(demand.owner_messages(payload, catalog, unread), ensure_ascii=False)
    assert finding in owner_text and condition in owner_text
    assert "LONG-METHOD-ONLY" not in owner_text
    assert row == original


def test_atomic_findings_never_cut_and_omitted_records_are_requestable():
    row = _record()
    oversized = {"finding": "oversized " * 1000, "conditions": "ESSENTIAL FINAL CONDITION"}
    visible = {"finding": "Useful later finding", "conditions": "Paired later condition"}
    row["study_summary_A"]["key_findings"] = [oversized, visible]
    payload = _payload(row)
    catalog = demand.build_material_catalog(payload)
    loc = next(item for item in _visible_locators(demand.access_messages(payload, catalog)) if item["path"] == "study_summary_A")
    assert json.loads(loc["source_excerpt"])["key_findings"] == [visible]
    assert "oversized" not in loc["source_excerpt"]
    assert "study_summary_A.key_findings" in loc["omitted_material_paths"]
    assert len(loc["source_excerpt"]) <= demand._LOCATOR_CONTENT_BUDGET
    trace = demand.resolve_material_requests(payload, catalog, {
        "status": "access_plan", "material_requests": [{
            "access_id": "source_materials[0]", "material_paths": loc["omitted_material_paths"],
        }],
    })
    assert trace["selected_materials"][0]["record"] == row
    owner = json.dumps(demand.owner_messages(payload, catalog, trace), ensure_ascii=False)
    assert "ESSENTIAL FINAL CONDITION" in owner and "LONG-METHOD-ONLY" in owner
    assert demand.full_catalog_trace(catalog)["selected_materials"][0]["record"] == row


def test_sibling_finding_and_conditions_are_atomic_and_long_passage_is_not_prefix():
    for material in ({"finding": "Short claim", "conditions": "long condition " * 1000}, "Long passage " * 1000):
        loc = demand._semantic_locator(material, "usable_content")
        assert loc["omitted_materials_requestable"]
        assert loc["atomic_content_budget_overflow"] is True
        assert json.loads(loc["source_excerpt"]) == material
        assert not loc["excerpt"]
        assert len(loc["source_excerpt"]) > demand._LOCATOR_CONTENT_BUDGET


def test_catalog_and_actual_request_hashes_are_deterministic_and_sensitive():
    payload = _payload(_record())
    first = demand.build_material_catalog(payload)
    second = demand.build_material_catalog(copy.deepcopy(payload))
    assert demand.public_catalog(first) == demand.public_catalog(second)
    messages = demand.access_messages(payload, first)
    assert cli._hash(messages) == cli._hash(demand.access_messages(payload, second))
    assert first["locator_policy"]["content_char_budget_per_material_root"] >= 4 * 600
    # A change beyond the old prefix changes both catalog and prepared-request hashes.
    changed_row = _record(finding="Different late finding")
    changed = _payload(changed_row)
    changed_catalog = demand.build_material_catalog(changed)
    assert first["catalog_sha256"] != changed_catalog["catalog_sha256"]
    assert cli._hash(messages) != cli._hash(demand.access_messages(changed, changed_catalog))
    # Even omitted-source changes invalidate hashes without surfacing clipped text.
    changed_row = copy.deepcopy(changed_row)
    changed_row["study_summary_A"]["approach"] += " Omitted source correction"
    omitted = _payload(changed_row)
    omitted_catalog = demand.build_material_catalog(omitted)
    assert changed_catalog["catalog_sha256"] != omitted_catalog["catalog_sha256"]
    assert cli._hash(demand.access_messages(changed, changed_catalog)) != cli._hash(demand.access_messages(omitted, omitted_catalog))


def test_generous_locator_retains_complete_multikilobyte_finding_and_full_b():
    finding = "Detailed source observation. " * 80
    condition = "This final condition must remain attached to the complete finding."
    row = _record(finding, condition)
    row["review_planning_B"]["planning_summary"] = "Source-authored review use. " * 100
    payload = _payload(row)
    catalog = demand.build_material_catalog(payload)
    locators = {item["path"]: item for item in _visible_locators(demand.access_messages(payload, catalog))}
    assert len(locators["study_summary_A"]["source_excerpt"]) > 4 * 600
    assert json.loads(locators["study_summary_A"]["source_excerpt"])["key_findings"] == row["study_summary_A"]["key_findings"]
    assert json.loads(locators["review_planning_B"]["source_excerpt"]) == row["review_planning_B"]
