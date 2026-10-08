"""Offline guide-first boundary, material fidelity, and response contracts."""
from copy import deepcopy
import json
import socket

import pytest

from optomind_research.runtime.upgrade3.guided_body_contracts import (
    GUIDE_SCHEMA, apply_insertions, build_author_payload, compile_guided_materials,
    parse_completion_response, parse_guided_response, resolve_read_request, validate_guide,
)
from optomind_research.runtime.upgrade3.writer_candidates_contracts import CandidateError


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Guide contracts must not call the network")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def book():
    chapters = []
    for cid, handle in [("optics", "P1"), ("catalysis", "P2")]:
        chapters.append({"chapter_id": cid, "chapter_frame": {"title": "OLD-FRAME"},
            "units": [{"unit_id": "U1", "paragraph_tasks": [{"paragraph_id": "OLD-TASK-ID",
                "point": "OLD-IMPERATIVE", "source_handles": [handle]}], "table_tasks": [],
                "owner_unit_context": {"instruction": "OLD-OWNER"}}],
            "sources": [{"source_handle": handle, "title": "Paper " + handle, "doi": "10.1/" + handle,
                "study_summary_A": {"key_findings": [{"finding": "gain=3", "conditions": "cryogenic cavity",
                    "comparison": "uncoated", "limits": "n=4"}], "method": "spectroscopy"},
                "review_planning_B": {"planning_summary": "OLD-PLANNER", "topic_handles": ["OLD-TOPIC"],
                    "facet_contributions": [{"contribution": "gain", "boundaries": "low temperature",
                        "possible_uses": "OLD-USE"}]}}],
            "chapter_tool_materials": [{"need_id": "OLD-NEED", "unit_key": cid + ":U1",
                "intended_use": "OLD-INTENT", "decision": "OLD-DECISION", "usable_content": "tool measurement",
                "conditions": {"laser": "405 nm"}, "sources": [{"title": "Different paper",
                    "original_source_handle": handle, "identity_status": "conflicting_handle", "doi": "10.9/different"}],
                "unknown_science": {"last_condition": "not in vacuum"},
                "provenance": {"source": "judge+focused_local_read", "model_calls": 1}}]})
    return {"chapters": chapters, "review_argument": "OLD-ARGUMENT", "book_sha256": "backend-only"}


def guide():
    return {"schema_version": GUIDE_SCHEMA, "manuscript_guide": "Explain competing mechanisms across domains.",
        "chapters": [{"chapter_id": "optics", "title": "Light", "writing_arrangement": "Compare gain and loss.",
            "required_content": ["Show gain comparison as a table."]},
            {"chapter_id": "catalysis", "title": "Catalysis", "writing_arrangement": "Explain energy barriers."}]}


def payload(b=None, g=None):
    b, g = b or book(), g or guide()
    return build_author_payload(compile_guided_materials(b), validate_guide(g, b), "optics", "Prior actual prose.")


def test_exact_four_keys_no_original_instructions_or_coverage_map():
    p = payload()
    assert set(p) == {"manuscript_guide", "chapter_assignment", "materials", "accepted_body_markdown"}
    serialized = json.dumps(p)
    for forbidden in ("OLD-", '"tasks"', '"unit_contexts"', '"chapter_frames"', '"review_argument"', '"task_id"'):
        assert forbidden not in serialized
    assert p["chapter_assignment"]["required_content"] == ["Show gain comparison as a table."]
    assert p["accepted_body_markdown"] == "Prior actual prose."
    assert {a["role"] for a in p["materials"]["evidence_atoms"]} == {"evidence"}


def test_scientific_objects_source_identity_and_supplements_stay_intact():
    p = payload()["materials"]
    values = [a["value"] for a in p["evidence_atoms"]]
    assert {"finding": "gain=3", "conditions": "cryogenic cavity", "comparison": "uncoated", "limits": "n=4"} in values
    assert {"contribution": "gain", "boundaries": "low temperature"} in values
    assert p["source_identities"]["P1"]["doi"] == "10.1/P1"
    tool = p["tool_materials"][0]
    assert tool["conditions"] == {"laser": "405 nm"}
    assert tool["unknown_science"]["last_condition"] == "not in vacuum"
    assert tool["sources"][0]["identity_status"] == "conflicting_handle"
    assert tool["sources"][0]["doi"] == "10.9/different"


def test_guide_changes_authority_without_mutation_or_task_mapping():
    b, g = book(), guide()
    original = deepcopy(b)
    g["chapters"][0]["writing_arrangement"] = "Reverse explanatory order."
    del g["chapters"][0]["required_content"]
    p = payload(b, g)
    assert p["chapter_assignment"]["writing_arrangement"] == "Reverse explanatory order."
    assert "required_content" not in p["chapter_assignment"]
    assert b == original


@pytest.mark.parametrize("change", [
    lambda g: g.update(manuscript_guide=""),
    lambda g: g.update(chapters=list(reversed(g["chapters"]))),
    lambda g: g["chapters"][1].update(chapter_id="optics"),
    lambda g: g["chapters"][0].update(tasks=[]),
    lambda g: g["chapters"][0].update(required_content="bad"),
    lambda g: g["chapters"][0].update(title=""),
    lambda g: g.update(schema_version="wrong"),
])
def test_invalid_guide_rejected_cheaply(change):
    g = guide(); change(g)
    with pytest.raises(CandidateError):
        validate_guide(g, book())


def test_optional_schema_normalized_and_nonmedical_chapter_names_supported():
    g = guide(); del g["schema_version"]
    assert validate_guide(g, book())["schema_version"] == GUIDE_SCHEMA


def test_explicit_guide_sources_and_exact_rereads_add_full_conditions():
    b, g = book(), guide(); pack = compile_guided_materials(b)
    g["chapters"][0]["source_handles"] = ["P2"]
    p = build_author_payload(pack, g, "optics", "")
    assert set(p["materials"]["source_identities"]) == {"P1", "P2"}
    requested = next(aid for aid, a in pack["atoms"].items() if a["source_handle"] == "P2" and a["role"] == "evidence")
    ids = resolve_read_request(pack, {"read_atom_ids": [requested]})
    assert {pack["atoms"][aid]["role"] for aid in ids} == {"evidence"}
    assert len(ids) > 1
    p = build_author_payload(pack, guide(), "optics", "", ids)
    assert set(p["materials"]["source_identities"]) == {"P1", "P2"}
    for request in ({"read_source_handles": ["p2"]}, {"read_atom_ids": ["unknown"]},
                    {"read_source_handles": ["P1", "P1"]}, {"read_atom_ids": []}):
        with pytest.raises(CandidateError):
            resolve_read_request(pack, request)
    planner = next(aid for aid, atom in pack["atoms"].items() if atom["role"] == "planner_interpretation")
    with pytest.raises(CandidateError, match="not_scientific"):
        resolve_read_request(pack, {"read_atom_ids": [planner]})


def test_read_parser_and_invalid_mixed_output():
    p = parse_guided_response({"choices": [{"message": {"content": json.dumps({"read_source_handles": ["P1"]})}, "finish_reason": "stop"}]})
    assert p["kind"] == "reread_request" and p["read_source_handles"] == ["P1"]
    with pytest.raises(CandidateError):
        parse_guided_response({"read_atom_ids": ["atom"], "body_markdown": "prose"})


@pytest.mark.parametrize("response", [
    {"body_markdown": "## Light\nScientific prose.", "complete": True, "remaining_content": []},
    '## Light\nScientific prose.\n```guide_writer_metadata\n{"complete":true,"remaining_content":[]}\n```',
    '```json\n{"body_markdown":"## Light\\nScientific prose.","complete":true,"remaining_content":[]}\n```',
])
def test_natural_and_json_complete_without_task_ids(response):
    parsed = parse_guided_response(response)
    assert parsed["complete"] and parsed["body_markdown"] == "## Light\nScientific prose."
    assert "completed_task_ids" not in parsed


@pytest.mark.parametrize("response,expected", [
    ("Partial prose.", "Partial prose."),
    ('Partial prose.\n```guide_writer_metadata\n{"complete":tr', "Partial prose."),
    ('{"body_markdown":"Partial prose.', "Partial prose."),
    ({"body_markdown": "Partial prose.", "complete": False, "remaining_content": ["comparison"]}, "Partial prose."),
    ({"body_markdown": "Partial prose.", "complete": True, "remaining_content": ["comparison"]}, "Partial prose."),
])
def test_partial_or_inconsistent_prose_preserved_without_false_complete(response, expected):
    p = parse_guided_response(response)
    assert p["body_markdown"] == expected and not p["complete"]


def test_truncated_transport_never_claims_completion():
    p = parse_guided_response({"content": '{"body_markdown":"Prose","complete":true}', "finish_reason": "length"})
    assert p["body_markdown"] == "Prose" and not p["complete"] and not p["transport_complete"]


def test_anchor_insertions_preserve_original_prose_and_guide_gaps_only():
    original = "A first.\nB second."
    response = {"insertions": [{"after_anchor": "A first.", "text": " Added relation."}],
                "complete": True, "remaining_content": []}
    result = parse_completion_response(response, original)
    assert result["body_markdown"] == "A first. Added relation.\nB second."
    assert result["complete"] and result["untouched_prose_preserved"]
    assert apply_insertions(original, [{"after_anchor": "", "text": " More."}]) == original + " More."
    for insertions in ([{"after_anchor": "unknown", "text": "X"}],
                       [{"after_anchor": "A", "text": "X"}, {"after_anchor": "A", "text": "Y"}]):
        with pytest.raises(CandidateError):
            apply_insertions(original, insertions)


def test_incomplete_completion_keeps_added_prose_and_transport_separate():
    p = parse_completion_response({"insertions": [{"text": " Addition."}], "complete": False,
                                   "remaining_content": ["Another comparison"]}, "Original.")
    assert p["body_markdown"] == "Original. Addition."
    assert not p["complete"] and p["transport_complete"]
    assert p["remaining_content"] == ["Another comparison"]


def test_global_navigation_uses_counts_not_redundant_full_pool_atom_addresses():
    navigation = payload()["materials"]["source_navigation"]
    assert {row["source_handle"] for row in navigation} == {"P1", "P2"}
    assert all("atom_ids" not in row and row["atom_count"] > 0 for row in navigation)


def test_known_nested_orchestration_removed_but_scientific_siblings_intact():
    b = book()
    nested = {"tasks": [{"point": "NESTED_OLD_TASK"}],
              "unit_contexts": {"instruction": "NESTED_OWNER"},
              "review_planning_B": {"planning_summary": "NESTED_PLAN", "conditions": "ambient pressure"},
              "conditions": {"duration": "3 fs"}, "finding": "observed gain"}
    b["chapters"][0]["chapter_tool_materials"][0]["wrapper"] = nested
    b["chapters"][0]["sources"][0]["task_ids"] = ["NESTED_SOURCE_TASK"]
    b["chapters"][0]["sources"][0]["deep_read_material"] = {
        "wrapped": {"_progressive_task_requirements": "NESTED_REQUIREMENT",
                    "task_ids": ["NESTED_TASK_ID"], "conditions": "dry atmosphere", "measurement": 5}}
    p = payload(b)
    serialized = json.dumps(p)
    assert "NESTED_" not in serialized
    assert p["materials"]["tool_materials"][0]["wrapper"]["conditions"] == {"duration": "3 fs"}
    assert p["materials"]["tool_materials"][0]["wrapper"]["review_planning_B"]["conditions"] == "ambient pressure"
    assert any(atom["value"] == {"conditions": "dry atmosphere", "measurement": 5}
               for atom in p["materials"]["evidence_atoms"])
