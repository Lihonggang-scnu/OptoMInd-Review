"""Offline semantic evidence compilation and selective-writing contracts."""
from copy import deepcopy
import pytest

from optomind_research.runtime.upgrade3.writing_evidence import (
    compile_evidence, evidence_payload, protected_atom_ids, select_atoms,
)
from optomind_research.runtime.upgrade3.writer_candidates_contracts import CandidateError


def book():
    sources = [
        {"source_handle": "P1", "title": "Primary", "doi": "10.1/primary",
         "study_summary_A": {"key_findings": [{"finding": "X improves Y", "conditions": "mice only", "comparison": "vs placebo"}],
             "approach": "randomized mouse experiment", "contribution_and_limits": [{"limits": "no human data"}]},
         "review_planning_B": {"planning_summary": "Put this in chapter two",
             "facet_contributions": [{"contribution": "X improves Y", "boundaries": "mice only", "possible_uses": "write a comparison"}],
             "scope_interpretation_cautions": ["Do not extrapolate"]},
         "unknown_science": {"long": "x" * 900 + "FINAL-CONDITION"},
         "locator": {"source_handle": "P3"}, "review_source_handle": "P2"},
        {"source_handle": "P2", "title": "Review", "study_summary_A": {"key_findings": [{"finding": "Review-mediated", "conditions": "not directly read"}]}},
        {"source_handle": "P3", "title": "Incidental", "study_summary_A": {"key_findings": [{"finding": "unrelated"}]}},
    ]
    chapter = {"chapter_id": "C1", "chapter_frame": {"chapter_title": "Mechanisms", "chapter_purpose": "Explain"},
        "units": [{"unit_id": "U1", "paragraph_tasks": [{"paragraph_id": "T1", "point": "Explain X", "source_handles": ["P1"]},
            {"paragraph_id": "T2", "point": "Explain other", "source_handles": ["P3"]}], "table_tasks": [],
            "owner_unit_context": {"evidence_conditions": "keep conditions", "supporting_studies": [{"source_handle": "P3"}]}}],
        "sources": sources}
    return {"book_sha256": "accepted", "research_question": "How?", "chapters": [chapter]}


def test_complete_archive_immutable_and_deterministic():
    b = book(); original = deepcopy(b)
    pack = compile_evidence(b)
    assert b == original and pack == compile_evidence(b)
    assert pack["canonical_book"] == b
    pack["canonical_book"]["research_question"] = "changed"
    assert b["research_question"] == "How?"


def test_task_primary_scope_review_closure_and_no_incidental_explosion():
    pack = compile_evidence(book()); tid = next(iter(pack["tasks"]))
    assert pack["task_source_handles"][tid] == ["P1", "P2"]
    view = evidence_payload(pack, [tid])
    assert set(view["source_identities"]) == {"P1", "P2"}
    assert view["unit_contexts"]["C1"]["U1"]["owner_unit_context"]["evidence_conditions"] == "keep conditions"
    assert "P3" in view["reread_source_ids"]


def test_no_character_cut_conditions_atomic_and_editorial_fields_separate():
    pack = compile_evidence(book()); view = evidence_payload(pack)
    values = [a["value"] for a in view["evidence_atoms"]]
    assert any(isinstance(v, dict) and v.get("conditions") == "mice only" and v.get("comparison") == "vs placebo" for v in values)
    assert any(isinstance(v, dict) and v.get("long", "").endswith("FINAL-CONDITION") for v in values)
    assert not any(a["role"] == "planning" for a in view["evidence_atoms"])
    planning = [a for a in pack["atoms"].values() if a["role"] == "planning"]
    assert {a["value"] for a in planning} == {"write a comparison"}
    assert any(a["value"] == "Put this in chapter two" and a["role"] == "planner_interpretation" for a in view["evidence_atoms"])
    assert any(isinstance(v, dict) and v.get("boundaries") == "mice only" and "possible_uses" not in v for v in values)


def test_exact_facts_dedup_but_changed_conditions_and_origins_survive():
    b = book(); duplicate = deepcopy(b["chapters"][0]["sources"][0]); duplicate["locator"] = {"other": "snapshot2"}
    duplicate["study_summary_A"]["key_findings"].append({"finding": "X improves Y", "conditions": "humans", "comparison": "vs placebo"})
    b["chapters"][0]["sources"].append(duplicate)
    pack = compile_evidence(b)
    findings = [a for a in pack["atoms"].values() if a["source_handle"] == "P1" and a["field_path"] == ["study_summary_A", "key_findings"]]
    assert len(findings) == 2
    assert sorted(len(a["origins"]) for a in findings) == [1, 2]
    assert {a["value"]["conditions"] for a in findings} == {"mice only", "humans"}


def test_reader_no_evidence_and_explicit_reread_any_source():
    pack = compile_evidence(book()); tid = next(iter(pack["tasks"]))
    reader = evidence_payload(pack, [tid], [])
    assert reader["evidence_atoms"] == [] and reader["tasks"][tid]["task"]["point"] == "Explain X"
    reread = evidence_payload(pack, [tid], pack["source_atom_ids"]["P3"])
    assert set(reread["source_identities"]) == {"P3"}


def test_selector_protects_design_limits_and_review_attribution():
    pack = compile_evidence(book()); tid = next(iter(pack["tasks"]))
    chosen = next(a["atom_id"] for a in pack["atoms"].values() if a["source_handle"] == "P1" and a["field_path"] == ["study_summary_A", "key_findings"])
    view = select_atoms(pack, [tid], [chosen])
    assert view["selection"]["requested_atom_ids"] == [chosen]
    assert view["selection"]["protected_atom_ids"] == protected_atom_ids(pack, [chosen])
    assert {"P1", "P2"} == set(view["source_identities"])
    assert any(a["value"] == "randomized mouse experiment" for a in view["evidence_atoms"])
    assert any(a["value"] == {"limits": "no human data"} for a in view["evidence_atoms"])


@pytest.mark.parametrize("atoms", [["missing"], "bad", [None]])
def test_unknown_or_invalid_atom_selection_fails(atoms):
    pack = compile_evidence(book())
    with pytest.raises(CandidateError):
        select_atoms(pack, list(pack["tasks"]), atoms)


def test_duplicate_and_unknown_task_fail():
    pack = compile_evidence(book()); aid = next(iter(pack["atoms"]))
    with pytest.raises(CandidateError): select_atoms(pack, list(pack["tasks"]), [aid, aid])
    with pytest.raises(CandidateError): evidence_payload(pack, ["missing"])


def test_explicit_variants_and_alias_identity_preserved():
    b = book(); first = b["chapters"][0]["sources"][0]
    first["aliases"] = ["OLD1"]
    variant = deepcopy(first); variant["study_summary_A"]["key_findings"][0]["conditions"] = "other setting"
    first["material_record_variants"] = [variant]
    b["chapters"][0]["units"][0]["paragraph_tasks"][0]["source_handles"] = ["OLD1"]
    pack = compile_evidence(b)
    assert pack["source_aliases"]["OLD1"] == "P1"
    assert any(a["value"].get("conditions") == "other setting" for a in pack["atoms"].values() if isinstance(a["value"], dict))
    assert any("parent_record_id" in o for a in pack["atoms"].values() for o in a["origins"])


def test_missing_review_dependency_fails_closed():
    b = book(); b["chapters"][0]["sources"][0]["review_source_handle"] = "MISSING"
    with pytest.raises(CandidateError, match="evidence_source_missing"):
        compile_evidence(b)


def test_planner_summary_trimmed_answers_and_qa_are_not_silently_hidden():
    b = book(); source = b["chapters"][0]["sources"][0]
    source["deep_read_material_trimmed"] = {"answer": "only readable answer", "conditions": "late followup"}
    source["deep_read_material"] = {"questions": [{"question": "when?", "answer": "at 20 days"}]}
    pack = compile_evidence(b); view = evidence_payload(pack)
    assert any(a["value"] == {"answer": "only readable answer", "conditions": "late followup"} for a in view["evidence_atoms"])
    assert any(a["value"] == {"question": "when?", "answer": "at 20 days"} for a in view["evidence_atoms"])
    for a in view["evidence_atoms"]:
        for snapshot in a["snapshot_ids"]:
            assert pack["snapshot_record_ids"][view["snapshot_index"][snapshot]] in pack["source_records"]


def test_empty_task_handles_fallback_and_owner_case_scope_preserved():
    b = book(); u = b["chapters"][0]["units"][0]
    u["paragraph_tasks"][0].pop("source_handles");u["source_handles"] = ["P1"]
    u["owner_unit_context"]["case_objects"] = [{"source_handle": "P3", "conditions": "required control"}]
    pack = compile_evidence(b); tid = next(iter(pack["tasks"]))
    assert set(pack["task_source_handles"][tid]) == {"P1", "P2", "P3"}


def test_generic_frame_names_produce_global_chapter_roles():
    b = book();b["chapters"][0]["chapter_frame"] = {"title": "T", "purpose": "P"}
    view = evidence_payload(compile_evidence(b))
    assert view["chapter_roles"] == [{"chapter_id": "C1", "title": "T", "purpose": "P"}]


def test_tool_evidence_retained_once_by_scope_and_absent_from_reader():
    b = book(); tool = {"unit_key": "C1:U1", "answer": "unique tool experiment", "conditions": "tool setting",
        "source_handle": "P3"}
    b["chapters"][0]["chapter_tool_materials"] = [tool, deepcopy(tool), {"unit_key": "OTHER", "answer": "irrelevant"}]
    pack = compile_evidence(b); tid = next(iter(pack["tasks"]))
    view = evidence_payload(pack, [tid])
    assert len(view["tool_materials"]) == 1
    assert view["tool_materials"][0]["value"] == tool
    assert evidence_payload(pack, [tid], [])['tool_materials'] == []
    assert set(view["source_identities"]) == {"P1", "P2"}


def test_rejected_review_lineage_does_not_resurrect_source():
    b = book();b["chapters"][0]["sources"][0]["rejected_source_handles"] = [{"review_source_handle": "MISSING"}]
    pack = compile_evidence(b)
    assert pack["review_dependencies"]["P1"] == ["P2"]


def test_compact_snapshot_labels_stable_across_windows_and_model_does_not_claim_conflicts():
    pack = compile_evidence(book()); first = next(iter(pack["tasks"]))
    full = evidence_payload(pack); partial = evidence_payload(pack, [first])
    for label, snapshot in partial["snapshot_index"].items():
        assert full["snapshot_index"][label] == snapshot
    assert all("multiple_values_in_field" not in a for a in full["evidence_atoms"])
    assert all("multiple_values_in_field" in a for a in pack["atoms"].values())
