"""Offline outline middleware: full intent, light navigation, whole reading."""
from copy import deepcopy
import json
import socket

import pytest

from optomind_research.runtime.upgrade3.guide_maker_contracts import (
    build_maker_payload, compile_guide_input, parse_maker_response, resolve_material_requests)
from optomind_research.runtime.upgrade3.guided_body_contracts import GUIDE_SCHEMA, validate_guide
from optomind_research.runtime.upgrade3.writer_candidates_contracts import CandidateError


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Contracts are offline")
    monkeypatch.setattr(socket.socket, "connect", denied)


def book():
    chapters = []
    for cid, handle in [("optics", "P1"), ("social-science", "P2")]:
        chapters.append({"chapter_id": cid, "chapter_frame": {"purpose": "Compare explanations " + cid},
            "units": [{"unit_id": "U1", "focus": "Full focus " * 200,
                "paragraph_tasks": [{"paragraph_id": "p1", "point": "Keep entire task " * 300,
                    "source_handles": [handle], "unknown_intent": {"keep": ["yes"]}}],
                "table_tasks": [{"table_id": "t1", "columns": ["finding", "condition", "negative case"],
                    "rows": ["all rows preserved"], "source_handles": [handle]}],
                "owner_unit_context": {"argument": "Do not equate correlation with cause"}}],
            "sources": [{"source_handle": handle, "title": "Title " + handle,
                "content_clue": "Existing short navigation", "source_type": "empirical",
                "study_summary_A": {"approach": "Study design", "key_findings": [
                    {"finding": "Null effect", "conditions": {"sample": 42, "regime": "low flux"},
                     "limits": "One setting", "negative_case": "No transfer"}],
                    "giant_role": "SCIENCE-ONLY-" * 3000},
                "review_planning_B": {"planning_summary": "EDITORIAL-ONLY", "facet_contributions": [
                    {"contribution": "Small effect", "boundaries": "Only here", "possible_uses": "EDITORIAL-ONLY"}]}}]})
    return {"chapters": chapters, "research_question": "Why do mechanisms differ?", "target_reader": None,
            "approved_plan": {"tables_required": True}, "unknown_intent": {"last_requirement": "Keep me"}}


def guide():
    return {"schema_version": GUIDE_SCHEMA, "manuscript_guide": "Compare explanatory mechanisms.",
        "chapters": [{"chapter_id": cid, "title": cid, "writing_arrangement": "Explain limits then compare.",
            "required_content": ["Include a comparison table."]} for cid in ("optics", "social-science")]}


def response(**updates):
    return {"guide": guide(), "reading_needs": [], "complete": False, "changes": [], **updates}


def need(**updates):
    return {"need_id": "N1", "question": "Which conditions help frame the comparison?", "source_handles": ["P1"], **updates}


def test_full_tasks_tables_and_unknown_intent_without_raw_science():
    original = book(); saved = deepcopy(original)
    bundle = compile_guide_input(original)
    payload = build_maker_payload(bundle)
    assert payload["full_outline"]["chapters"][0]["units"] == original["chapters"][0]["units"]
    assert payload["full_outline"]["unknown_intent"] == original["unknown_intent"]
    assert payload["full_outline"]["target_reader"] is None
    serialized = json.dumps(payload)
    assert "SCIENCE-ONLY" not in serialized and "EDITORIAL-ONLY" not in serialized
    assert "feedback" not in payload and payload["prior_guide"] is None
    assert "old_body" not in payload and "manual_guide" not in payload
    assert original == saved
    row = payload["source_catalog"][0]
    assert row["source_handle"] == "P1" and row["identity"]["year"] is None
    assert row["identity"]["doi"] is None
    assert row["content_clues"] == [{"field": "content_clue", "value": "Existing short navigation"}]
    assert row["unit_links"] == [{"chapter_id": "optics", "unit_id": "U1"}]
    assert "task_ids" not in row


def test_feedback_optional_and_payload_does_not_alias_archive():
    b = compile_guide_input(book(), {"instruction": "Explain the table first"})
    p = build_maker_payload(b)
    assert p["feedback"] == {"instruction": "Explain the table first"}
    p["full_outline"]["chapters"].clear()
    assert len(b["full_outline"]["chapters"]) == 2


def test_whole_science_conditions_negative_cases_and_stable_hashes():
    b = compile_guide_input(book())
    packets = resolve_material_requests(b, [need()])
    assert len(packets) == 1 and packets[0]["source_handle"] == "P1"
    packet = packets[0]
    values = [a["value"] for a in packet["evidence_atoms"]]
    assert {"finding": "Null effect", "conditions": {"sample": 42, "regime": "low flux"},
            "limits": "One setting", "negative_case": "No transfer"} in values
    assert "Study design" in values
    assert "SCIENCE-ONLY-" * 3000 in values
    assert "EDITORIAL-ONLY" not in json.dumps(packet)
    again = resolve_material_requests(compile_guide_input(book()), [need(question="Different purpose")])[0]
    assert again["packet_sha256"] == packet["packet_sha256"]
    assert [a["atom_id"] for a in again["evidence_atoms"]] == [a["atom_id"] for a in packet["evidence_atoms"]]
    assert packet["need_ids"] == ["N1"]


def test_review_dependency_read_with_whole_source_and_no_fake_metadata():
    raw = book()
    raw["chapters"][0]["sources"][0]["mediation"] = {"review_source_handle": "P2", "limits": "review-mediated"}
    bundle = compile_guide_input(raw)
    units = resolve_material_requests(bundle, [need()])
    assert [u["source_handle"] for u in units] == ["P1", "P2"]
    assert units[0]["review_source_handles"] == ["P2"]
    assert units[1]["need_ids"] == ["N1"]


def test_atom_selection_expands_to_complete_source():
    b = compile_guide_input(book())
    aid = next(k for k, v in b["science_archive"]["atoms"].items() if v["source_handle"] == "P1" and v["role"] == "evidence")
    assert resolve_material_requests(b, [need(atom_ids=[aid])])[0]["evidence_atoms"] == resolve_material_requests(b, [need()])[0]["evidence_atoms"]


def test_provisional_and_final_guides_use_existing_writer_schema():
    raw = book(); bundle = compile_guide_input(raw)
    draft = parse_maker_response(response(reading_needs=[need()]), raw, bundle)
    assert not draft["complete"] and draft["transport_complete"]
    final = parse_maker_response(json.dumps(response(complete=True, changes=["Clarified comparison limits"])), raw, bundle)
    assert final["complete"] and final["guide"] == validate_guide({**guide(), "chapters": [{**c, "writing_units": []} for c in guide()["chapters"]]}, raw)
    pending = parse_maker_response(response(), raw, bundle)
    assert not pending["complete"] and not pending["reading_needs"]


@pytest.mark.parametrize("bad", [
    need(source_handles=["p1"]), need(source_handles=[]), need(source_handles=["P1", "P1"]),
    need(atom_ids=["atom::absent"]), need(need_id="optics::p1"), need(question=""),
    need(task_ids=["p1"]), need(source_handles="P1"),
])
def test_invalid_needs_fail_without_fuzzy_repair(bad):
    raw = book(); bundle = compile_guide_input(raw)
    parsed = parse_maker_response(response(reading_needs=[bad]), raw, bundle)
    assert parsed["errors"] and not parsed["complete"] and parsed["reading_needs"] == []
    assert parsed["guide"] == validate_guide({**guide(), "chapters": [{**c, "writing_units": []} for c in guide()["chapters"]]}, raw)
    assert parsed["transport_complete"]
    with pytest.raises(CandidateError):
        resolve_material_requests(bundle, [bad])


@pytest.mark.parametrize("bad", [
    response(complete=True, reading_needs=[need()]), response(complete="true"),
    response(changes="essay"), response(guide={}), response(scientific_critic="none"),
])
def test_invalid_final_or_envelope_rejected(bad):
    raw = book()
    with pytest.raises(CandidateError):
        parse_maker_response(bad, raw, compile_guide_input(raw))


def test_invalid_atom_role_or_source_and_unknown_guide_source():
    raw = book(); bundle = compile_guide_input(raw); atoms = bundle["science_archive"]["atoms"]
    for aid in (next(k for k, a in atoms.items() if a["role"] == "planner_interpretation"),
                next(k for k, a in atoms.items() if a["source_handle"] == "P2" and a["role"] == "evidence")):
        with pytest.raises(CandidateError):
            resolve_material_requests(bundle, [need(atom_ids=[aid])])
    bad = response(); bad["guide"]["chapters"][0]["source_handles"] = ["UNKNOWN"]
    with pytest.raises(CandidateError):
        parse_maker_response(bad, raw, bundle)


def test_truncated_transport_cannot_claim_final():
    raw = book()
    output = {"choices": [{"message": {"content": json.dumps(response(complete=True))}, "finish_reason": "length"}]}
    parsed = parse_maker_response(output, raw, compile_guide_input(raw))
    assert not parsed["complete"] and not parsed["transport_complete"]


def test_canonical_alias_preserved_and_duplicate_alias_requests_rejected():
    raw = book(); raw["chapters"][0]["sources"][0]["aliases"] = ["old-P1"]
    bundle = compile_guide_input(raw)
    assert bundle["source_catalog"][0]["source_handle"] == "P1"
    assert resolve_material_requests(bundle, [need(source_handles=["old-P1"])])[0]["source_handle"] == "P1"
    with pytest.raises(CandidateError):
        resolve_material_requests(bundle, [need(source_handles=["P1", "old-P1"])])


def test_invalid_reading_array_preserves_valid_provisional_guide():
    raw = book(); bundle = compile_guide_input(raw)
    for malformed in ("wrong", [need(), need()], [{"need_id": "N1"}]):
        parsed = parse_maker_response(response(reading_needs=malformed, complete=True), raw, bundle)
        assert parsed["guide"] == validate_guide({**guide(), "chapters": [{**c, "writing_units": []} for c in guide()["chapters"]]}, raw)
        assert parsed["errors"] and not parsed["complete"] and parsed["reading_needs"] == []


def test_atom_only_need_and_explicit_reread_reason():
    bundle = compile_guide_input(book())
    aid = next(k for k, a in bundle["science_archive"]["atoms"].items() if a["role"] == "evidence")
    request = {"need_id": "N1", "question": "Recheck limits", "atom_ids": [aid], "reread_reason": "Resolve a newly identified comparison boundary"}
    packet = resolve_material_requests(bundle, [request])[0]
    assert packet["reread_reasons"] == [request["reread_reason"]]
    assert len(packet["evidence_atoms"]) > 1
    with pytest.raises(CandidateError):
        resolve_material_requests(bundle, [{**request, "reread_reason": ""}])


def test_nested_existing_catalog_clues_and_single_copy_science():
    raw = book()
    summary = raw["chapters"][0]["sources"][0]["study_summary_A"]
    summary.update(paper_kind="observational", work_summary="Existing concise description", research_scope="Specific population")
    bundle = compile_guide_input(raw)
    catalog = json.dumps(bundle["source_catalog"])
    assert "Existing concise description" in catalog and "observational" in catalog
    packet = resolve_material_requests(bundle, [need()])[0]
    assert all("fields" not in r and r["atom_ids"] for r in packet["scientific_records"])
    assert json.dumps(packet).count("SCIENCE-ONLY-") == 3000


def test_task_fields_named_like_archive_wrappers_are_not_erased():
    raw = book()
    task = raw["chapters"][0]["units"][0]["paragraph_tasks"][0]
    task["provenance"] = {"instruction": "Explain provenance uncertainty"}
    task["sources"] = {"instruction": "Discuss sources of error"}
    assert compile_guide_input(raw)["full_outline"]["chapters"][0]["units"][0]["paragraph_tasks"][0] == task


def test_tools_are_lightly_navigable_and_whole_semantic_read_packets():
    raw = book()
    tool = {"need_id": "OLD-N1", "unit_key": "optics:U1", "question": "Which boundary applies?",
        "usable_content": "COMPLETE TOOL SCIENCE " * 1000, "conditions": {"temperature": "cryogenic"},
        "limits": "Not transferable", "unknown_science": {"negative_result": "Null"},
        "sources": [{"original_source_handle": "P1", "identity_status": "conflicting_handle",
            "title": "Different source", "doi": "10.9/other"}]}
    raw["chapters"][0]["chapter_tool_materials"] = [tool, deepcopy(tool)]
    bundle = compile_guide_input(raw)
    initial = build_maker_payload(bundle)
    assert "COMPLETE TOOL SCIENCE" not in json.dumps(initial)
    assert len(initial["tool_catalog"]) == 1
    handle = initial["tool_catalog"][0]["tool_handle"]
    assert compile_guide_input(raw)["tool_catalog"][0]["tool_handle"] == handle
    request = {"need_id": "N1", "question": "Check the condition", "tool_handles": [handle]}
    packet = resolve_material_requests(bundle, [request])[0]
    assert packet["packet_kind"] == "tool" and packet["source_handle"] == handle
    science = packet["scientific_tool"]
    assert science["usable_content"] == tool["usable_content"]
    assert science["conditions"] == tool["conditions"] and science["limits"] == tool["limits"]
    assert science["sources"][0]["identity_status"] == "conflicting_handle"
    assert "need_id" not in science
    with pytest.raises(CandidateError):
        resolve_material_requests(bundle, [{**request, "tool_handles": ["tool_unknown"]}])
    g = guide(); g["chapters"][0]["source_handles"] = [handle]
    with pytest.raises(CandidateError):
        parse_maker_response(response(guide=g), raw, bundle)


def test_navigation_precedence_preserves_complete_distinct_existing_summaries():
    raw = book(); source = raw["chapters"][0]["sources"][0]
    source["study_summary_A"].update(work_summary="Complete first summary " * 300, research_scope="Read-only scope")
    variant = deepcopy(source)
    variant["study_summary_A"]["work_summary"] = "Complete complementary summary"
    source["material_record_variants"] = [variant]
    row = compile_guide_input(raw)["source_catalog"][0]
    assert row["content_clues"] == [
        {"field": "study_summary_A.work_summary", "value": "Complete first summary " * 300},
        {"field": "study_summary_A.work_summary", "value": "Complete complementary summary"}]
    packet = resolve_material_requests(compile_guide_input(raw), [need()])[0]
    assert "Read-only scope" in [a["value"] for a in packet["evidence_atoms"]]


def test_only_equal_task_index_wrapper_is_deduplicated():
    raw = book(); bundle = compile_guide_input(raw)
    raw["task_catalog"] = deepcopy(bundle["science_archive"]["tasks"])
    assert "task_catalog" not in compile_guide_input(raw)["full_outline"]
    raw["task_catalog"]["additional_intent"] = "Keep differing wrapper"
    assert compile_guide_input(raw)["full_outline"]["task_catalog"] == raw["task_catalog"]
