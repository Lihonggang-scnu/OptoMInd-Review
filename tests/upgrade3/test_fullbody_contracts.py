"""Offline complete-BODY input identities, lossless windows and free-form output."""
from copy import deepcopy
import hashlib
import json
import socket

import pytest

from optomind_research.runtime.upgrade3.fullbody_contracts import (
    CandidateError, build_fullbody_input, fullbody_task_catalog,
    parse_fullbody_response, project_fullbody,
)
from optomind_research.runtime.upgrade3.writer_candidates_contracts import (
    INPUT_SCHEMA, build_chapter_input,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Full-BODY contracts must not use the network")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def chapter(chapter_id="CH01", source="P0001", *, table=False):
    return {
        "schema_version": INPUT_SCHEMA, "chapter_id": chapter_id, "language": "zh",
        "chapter_frame": {"title": "Chapter " + chapter_id, "research_question": "Original question",
                          "review_argument": "Review-wide argument", "case_guidance": "Explain independent observations"},
        "other_chapters": [{"chapter_id": "CH02", "purpose": "Keep future responsibility visible"}],
        "units": [{"unit_id": "U1", "focus": "Explain, do not list", "unit_notes": "Keep this note",
                   "paragraph_tasks": [{"paragraph_id": "A1", "point": "Explain this source",
                       "source_handles": [source], "finding_conditions": {"sessions": 3, "setting": "low"},
                       "argument_relations": [{"from": "A1", "to": "A2", "relation": "qualifies"}],
                       "case_guidance": {"role": "independent case, not causal confirmation"}}],
                   "table_tasks": ([{"table_id": "T1", "purpose": "Keep table task", "source_handles": [source]}] if table else []),
                   "source_handles": [source], "owner_unit_context": {"evidence_conditions": ["Keep settings attached"],
                         "argument_relations": {"A2": "qualifies A1"}}}],
        "sources": [{"source_handle": source, "paper_id": "paper-" + source, "doi": "10.1/" + source,
                     "study_summary_A": {"finding": ("Complete scientific material " * 500) + "FINAL-SCIENCE-SENTINEL"}}],
        "chapter_tool_materials": [], "warnings": [],
        "provenance": {"arrangement_path": "/accepted/" + chapter_id + ".json",
                       "arrangement_sha256": "a" * 64, "packet_path": "/accepted/packets/" + chapter_id + ".json",
                       "packet_sha256": "b" * 64, "material_builder": "review_unit_writer.build_unit_view"},
    }


def book_fixture(*, table=False):
    return build_fullbody_input([chapter(table=table), chapter("CH02", "P0002")])


def response_for(book, body="One flowing article [P0001].\n\nIts argument continues [P0002]."):
    return {"body_markdown": body, "task_dispositions": [
        {"task_id": key, "status": "completed", "location": "one paragraph may address many tasks"}
        for key in fullbody_task_catalog(book)], "complete": True}


def test_global_ids_stay_distinct_without_mutating_repeated_local_ids():
    chapters = [chapter(), chapter("CH02", "P0002")]
    before = deepcopy(chapters)
    book = build_fullbody_input(chapters)
    catalog = fullbody_task_catalog(book)
    assert list(catalog) == ["CH01::U1::A1", "CH02::U1::A1"]
    assert [item["local_task_id"] for item in catalog.values()] == ["U1::A1"] * 2
    assert chapters == before
    assert book["chapters"] == before
    assert fullbody_task_catalog(build_fullbody_input(list(reversed(chapters)))) == catalog
    book["chapters"][0]["units"][0]["paragraph_tasks"][0]["point"] = "caller edit"
    assert chapters == before


def test_global_ids_escape_separators_and_legacy_ids_stay_stable():
    first, second = chapter("a::b"), chapter("a")
    first["units"][0]["unit_id"] = "c"
    second["units"][0]["unit_id"] = "b::c"
    first["units"][0]["paragraph_tasks"].append({"point": "No identifier"})
    second["units"][0]["paragraph_tasks"][0]["paragraph_id"] = "%A1"
    book = build_fullbody_input([first, second])
    keys = list(fullbody_task_catalog(book))
    assert keys == ["a%3A%3Ab::c::A1", "a%3A%3Ab::c::__legacy_paragraph_0002", "a::b%3A%3Ac::%25A1"]
    projected = project_fullbody(book, [keys[1]])
    assert projected["editable_task_ids"] == [keys[1]]
    assert projected["chapters"][0]["units"][0]["task_keys"]["paragraph_tasks"] == ["c::__legacy_paragraph_0002"]
    assert projected["chapters"][0]["units"][0]["fullbody_task_keys"]["paragraph_tasks"] == [keys[1]]


def test_exact_record_dedup_preserves_complementary_records_and_identity():
    first, second = chapter(), chapter("CH02")
    complement = deepcopy(second["sources"][0])
    complement["study_summary_A"] = {"finding": "Complementary evidence remains independent"}
    second["sources"] += [complement, deepcopy(complement)]
    book = build_fullbody_input([first, second])
    assert book["sources"] == [first["sources"][0], complement]
    identity = book["source_identities"]["P0001"]
    assert identity["chapter_ids"] == ["CH01", "CH02"]
    assert len(identity["record_ids"]) == 2
    payload = project_fullbody(book, ["CH01::U1::A1"])
    assert payload["sources"] == book["sources"]
    assert "material_record_variants" not in payload["sources"][0]
    assert "FINAL-SCIENCE-SENTINEL" in json.dumps(payload)


@pytest.mark.parametrize("mutate, error", [
    (lambda row: row["sources"][0].update(doi="10.1/foreign"), "source_identity_conflict:P0001"),
    (lambda row: row["sources"][0].update(doi="", paper_id="foreign"), "source_identity_conflict:P0001"),
    (lambda row: row["sources"][0].update(aliases=["P0002"]), "source_alias_ambiguous:P0002"),
])
def test_conflicting_source_identity_fails_closed_across_chapters(mutate, error):
    first, second = chapter(), chapter("CH02")
    first["sources"].append({"source_handle": "P0002", "paper_id": "two"})
    mutate(second)
    with pytest.raises(CandidateError, match=error):
        build_fullbody_input([first, second])


def test_conflict_hidden_by_missing_first_identity_or_variant_is_reported():
    first, second, third = chapter(), chapter("CH02"), chapter("CH03")
    first["sources"][0].pop("doi"); first["sources"][0].pop("paper_id")
    third["sources"][0]["doi"] = "10.1/different"
    with pytest.raises(CandidateError, match="source_identity_conflict:P0001"):
        build_fullbody_input([first, second, third])
    third["sources"][0]["doi"] = "10.1/P0001"
    third["sources"][0]["material_record_variants"] = [{"source_handle": "P0001", "doi": "10.1/foreign"}]
    with pytest.raises(CandidateError, match="source_identity_conflict:P0001"):
        build_fullbody_input([first, third])


def test_same_doi_allows_historical_paper_ids_without_merging_records():
    first, second = chapter(), chapter("CH02")
    second["sources"][0]["paper_id"] = "old-record-id"
    book = build_fullbody_input([first, second])
    assert len(book["sources"]) == 2
    assert book["sources"][1]["paper_id"] == "old-record-id"


def test_projection_is_independent_full_outline_with_selected_complete_evidence():
    book = book_fixture()
    before = deepcopy(book)
    payload = project_fullbody(book, ["CH01::U1::A1"])
    assert {row["source_handle"] for row in payload["sources"]} == {"P0001"}
    assert payload["chapters"][0]["units"][0]["paragraph_tasks"] == book["chapters"][0]["units"][0]["paragraph_tasks"]
    shared = payload["shared_fullbody_context"]
    assert shared["read_only"] is True
    assert shared["intent_is_not_scientific_evidence"] is True
    assert [row["chapter_id"] for row in shared["chapters"]] == ["CH01", "CH02"]
    assert shared["chapters"][1]["units"][0]["paragraph_tasks"] == book["chapters"][1]["units"][0]["paragraph_tasks"]
    assert len(shared["task_roles"]) == 2
    assert len(payload["sources"][0]["study_summary_A"]["finding"]) > 12000
    payload["sources"][0]["study_summary_A"]["finding"] = "changed"
    shared["chapters"][1]["units"][0]["owner_unit_context"].clear()
    assert book == before


def test_alias_review_mediated_case_and_nested_conditions_participate_normally():
    first = chapter()
    first["sources"][0]["aliases"] = ["P0901"]
    task = first["units"][0]["paragraph_tasks"][0]
    task["source_handles"] = ["P0901"]
    task["finding_conditions"]["case"] = {"source_handle": "P0004", "condition": "reported under this setting"}
    first["sources"] += [
        {"source_handle": "P0004", "paper_id": "original-study", "material_status": "review_reported_secondary",
         "review_source_handle": "P0005", "direct_verified": False,
         "local_passages": {"usable_content": "Original result available through reporting review"}},
        {"source_handle": "P0005", "paper_id": "review", "study_summary_A": {"finding": "Full reporting review material"}},
    ]
    first["chapter_tool_materials"] = [{"unit_key": "U1", "question_id": "Q-ONE", "usable_content": "Case guidance",
        "source_status": "review_reported_secondary", "direct_verified": False,
        "sources": [{"source_handle": "P0004"}, {"source_handle": "P0005"}]}]
    book = build_fullbody_input([first, chapter("CH02", "P0002")])
    payload = project_fullbody(book, ["CH01::U1::A1"])
    sources = {row["source_handle"]: row for row in payload["sources"]}
    assert set(sources) == {"P0001", "P0004", "P0005"}
    assert not sources["P0004"].get("study_summary_A")
    assert sources["P0004"]["direct_verified"] is False
    assert payload["source_aliases"] == {"P0901": "P0001"}
    assert payload["chapters"][0]["chapter_tool_materials"] == first["chapter_tool_materials"]
    result = parse_fullbody_response({"body_markdown": "A reported result [P0901][P0004][P0005].",
        "completed_task_ids": ["CH01::U1::A1"]}, book, ["CH01::U1::A1"])
    assert result["complete"]
    assert result["diagnostics"]["unknown_citations"] == []


def test_complementary_record_lineage_is_included_in_selected_evidence():
    first, second = chapter(), chapter("CH02")
    second["sources"][0]["review_source_handle"] = "P0002"
    second["sources"].append({"source_handle": "P0002", "paper_id": "review", "study_summary_A": {"finding": "Full review"}})
    payload = project_fullbody(build_fullbody_input([first, second]), ["CH01::U1::A1"])
    assert {row["source_handle"] for row in payload["sources"]} == {"P0001", "P0002"}


def test_manifest_copies_actual_accepted_provenance_and_hashes_current_content():
    first = chapter()
    book = build_fullbody_input([first])
    row = book["input_manifest"]["chapters"][0]
    assert row["provenance"] == first["provenance"]
    assert book["input_manifest"]["chapter_order"] == ["CH01"]
    expected = hashlib.sha256(json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert row["normalized_chapter_sha256"] == expected
    first["provenance"]["packet_sha256"] = "c" * 64
    changed = build_fullbody_input([first])
    assert changed["book_sha256"] != book["book_sha256"]
    assert changed["input_manifest"]["manifest_sha256"] != book["input_manifest"]["manifest_sha256"]


def test_global_requirements_preserve_explicit_context_and_reject_unexplained_conflict():
    first, second = chapter(), chapter("CH02")
    second["chapter_frame"]["research_question"] = "Different question"
    with pytest.raises(CandidateError, match="fullbody_research_question_conflict"):
        build_fullbody_input([first, second])
    book = build_fullbody_input([first, second], research_question={"question": "Actual original request", "audience": "Researcher"},
                               review_argument="Accepted full argument", language="en")
    assert book["research_question"]["audience"] == "Researcher"
    assert book["chapters"][1]["chapter_frame"]["research_question"] == "Different question"
    assert project_fullbody(book)["review_argument"] == "Accepted full argument"


@pytest.mark.parametrize("wrap", [lambda x: x, json.dumps, lambda x: "```json\n" + json.dumps(x) + "\n```",
    lambda x: {"content": json.dumps(x), "complete": True, "finish_reason": "stop"},
    lambda x: {"choices": [{"message": {"content": json.dumps(x)}, "finish_reason": "stop"}]},
    lambda x: {"response": {"message": {"content": x}}}])
def test_free_form_prose_plus_many_to_many_sidecar_round_trips(wrap):
    book = book_fixture()
    response = response_for(book)
    result = parse_fullbody_response(wrap(response), book)
    assert result["complete"]
    assert result["body_markdown"] == response["body_markdown"]
    assert result["task_dispositions"] == response["task_dispositions"]
    assert result["completed_task_ids"] == list(fullbody_task_catalog(book))
    assert result["pending_task_ids"] == []
    assert "blocks" not in result
    assert result["diagnostics"]["task_dispositions_are_not_proof"] is True
    assert result["diagnostics"]["scientific_fidelity_verified"] is False
    assert result["diagnostics"]["coverage_kind"] == "model_declared_structural_only"


def test_markdown_with_metadata_fence_and_shorthand_completion():
    book = book_fixture()
    metadata = {"completed_task_ids": list(fullbody_task_catalog(book)), "complete": True}
    result = parse_fullbody_response("# One whole article\n\nParagraph.\n```fullbody_metadata\n" + json.dumps(metadata) + "\n```", book)
    assert result["complete"]
    assert result["body_markdown"] == "# One whole article\n\nParagraph."


def test_bare_prose_is_preserved_but_complete_claim_alone_is_insufficient():
    book = book_fixture()
    body = "Useful full prose without sidecar [P0001]."
    result = parse_fullbody_response(body, book)
    assert result["body_markdown"] == body
    assert not result["complete"]
    assert result["pending_task_ids"] == list(fullbody_task_catalog(book))
    assert not parse_fullbody_response({"body_markdown": body, "complete": True}, book)["complete"]


def test_task_selfclaim_with_unknown_out_of_scope_and_conflicting_ids_is_not_complete():
    book = book_fixture()
    response = response_for(book)
    response["task_dispositions"] += [{"task_id": "unknown", "status": "completed"},
                                      {"task_id": "CH01::U1::A1", "status": "pending"}]
    result = parse_fullbody_response(response, book, ["CH01::U1::A1"])
    assert not result["complete"]
    assert result["body_markdown"] == response["body_markdown"]
    assert result["pending_task_ids"] == ["CH01::U1::A1"]
    invalid = [issue for issue in result["issues"] if issue["code"] == "fullbody_task_reference_invalid"]
    assert [issue["out_of_scope"] for issue in invalid] == [True, False]


def test_partial_output_and_transport_finish_reason_are_preserved():
    book = book_fixture()
    raw = {"choices": [{"message": {"content": '{"body_markdown":"Useful unfinished \\nprose \\ud83d\\ude00'}, "finish_reason": "length"}]}
    result = parse_fullbody_response(raw, book)
    assert result["body_markdown"] == "Useful unfinished \nprose 😀"
    assert not result["complete"]
    assert result["diagnostics"]["transport_incomplete"]
    assert result["finish_reason"] == "length"
    assert result["raw_response"] == raw
    assert result["pending_task_ids"] == list(fullbody_task_catalog(book))
    json.dumps(result, ensure_ascii=False).encode("utf-8")


def test_length_transport_prevents_complete_even_when_all_selfclaims_parse():
    book = book_fixture()
    raw = {"content": json.dumps(response_for(book)), "complete": False, "finish_reason": "length"}
    result = parse_fullbody_response(raw, book)
    assert result["body_markdown"]
    assert not result["complete"]
    assert result["diagnostics"]["finish_reasons"] == ["length"]
    assert not result["pending_task_ids"]


def test_markdown_table_claim_needs_table_and_still_is_not_semantic_verification():
    book = book_fixture(table=True)
    no_table = parse_fullbody_response(response_for(book), book)
    assert not no_table["complete"]
    assert no_table["pending_task_ids"] == ["CH01::U1::T1"]
    response = response_for(book, "Connected explanation.\n\n| Condition | Result |\n| --- | --- |\n| Low, three sessions | Reported [P0001] |")
    result = parse_fullbody_response(response, book)
    assert result["complete"]
    assert result["diagnostics"]["table_task_mapping_verified"] is False
    assert result["semantic_quality_unreviewed"] is True


def test_citations_are_diagnosed_not_silently_repaired():
    book = book_fixture()
    body = "Finding [P0001][P9999][1]. Code `[2]`."
    result = parse_fullbody_response(response_for(book, body), book)
    assert result["body_markdown"] == body
    assert result["diagnostics"]["unknown_citations"] == ["P9999"]
    assert result["diagnostics"]["unresolved_numeric_citations"] == ["[1]"]
    assert result["diagnostics"]["numeric_citation_repairs"] == []


@pytest.mark.parametrize("ids,match", [("CH01::U1::A1", "task_ids_not_list"), ([], "task_ids_empty"),
    (["missing"], "task_ids_unknown"), (["CH01::U1::A1"] * 2, "task_ids_duplicated")])
def test_projection_rejects_bad_task_scope(ids, match):
    with pytest.raises(CandidateError, match=match):
        project_fullbody(book_fixture(), ids)


def test_duplicate_chapters_and_unnormalized_inputs_fail_closed():
    with pytest.raises(CandidateError, match="fullbody_chapter_id_duplicated"):
        build_fullbody_input([chapter(), chapter()])
    raw = chapter(); raw.pop("schema_version")
    with pytest.raises(CandidateError, match="fullbody_requires_normalized_chapter"):
        build_fullbody_input([raw])


def test_original_context_is_kept_and_absence_is_explicit():
    from optomind_research.runtime.upgrade3.fullbody_contracts import seal_fullbody_input
    original = {"text": "Keep the original request", "audience": "Materials researchers"}
    plan = {"chapters": [{"chapter_id": "CH01", "source_materials": [{"science": "Complete original plan material"}]}]}
    book = build_fullbody_input([chapter()], original_user_request=original, target_reader="Researcher",
                               shared_scope=["complete BODY"], approved_plan=plan)
    payload = project_fullbody(book)
    assert payload["user_request"] == original
    assert payload["target_reader"] == "Researcher"
    assert payload["review_scope"] == ["complete BODY"]
    assert book["approved_plan"] == plan
    assert payload["approved_plan"]["canonical_book_reference"] == "approved_plan"
    assert "source_materials" not in json.dumps(payload["approved_plan"])
    assert payload["missing_original_context_fields"] == []
    assert book_fixture()["missing_original_context_fields"] == ["user_request", "target_reader", "review_scope"]
    old_hash = book["book_sha256"]
    book["input_manifest"]["approved_plan_path"] = "/accepted/current-plan.json"
    sealed = seal_fullbody_input(book)
    assert sealed["book_sha256"] != old_hash
    assert seal_fullbody_input(sealed) == sealed
    assert book["book_sha256"] == old_hash


def test_model_projection_uses_exact_references_not_repeated_scientific_task_text():
    book = book_fixture()
    task = book["chapters"][0]["units"][0]["paragraph_tasks"][0]
    task["unique_critical_condition"] = "UNIQUE CONDITION SENTINEL"
    book["chapters"][0]["units"][0]["owner_unit_context"]["unique_owner_context"] = "UNIQUE OWNER SENTINEL"
    book["chapters"][0]["provenance"]["private_local_path"] = "/accepted/not-needed-in-prompt"
    serialized = json.dumps(project_fullbody(book, ["CH01::U1::A1"]))
    assert serialized.count("UNIQUE CONDITION SENTINEL") == 1
    assert serialized.count("UNIQUE OWNER SENTINEL") == 1
    assert "/accepted/not-needed-in-prompt" not in serialized
    assert "fullbody_task_keys" in serialized
    assert "editable_task_reference" in serialized


def test_literal_inner_quotes_repair_preserves_all_complete_paid_prose():
    book = book_fixture()
    expected = response_for(book, 'The literal "quoted" concept explains two linked observations [P0001][P0002].')
    expected["issues"] = [{"code": "writer_note", "detail": 'A second "literal" quote.'}]
    raw = json.dumps(expected, ensure_ascii=False).replace('\\"', '"')
    result = parse_fullbody_response(raw, book)
    assert result["complete"]
    assert result["body_markdown"] == expected["body_markdown"]
    assert result["diagnostics"]["format_repair"][0]["inserted_escape_characters"] == 4
    assert result["diagnostics"]["format_repair"][0]["method"] == "bounded_inner_json_quote_escape"


def test_plain_markdown_preserves_boundary_whitespace_for_continuation():
    book = book_fixture()
    prose = " continuation after an unfinished sentence.\n\n"
    result = parse_fullbody_response(prose, book)
    assert result["body_markdown"] == prose
    metadata = {"completed_task_ids": list(fullbody_task_catalog(book)), "complete": True}
    result = parse_fullbody_response(prose + "\n```fullbody_metadata\n" + json.dumps(metadata) + "\n```", book)
    assert result["body_markdown"] == prose
    assert result["complete"]


def test_length_cutoff_in_sidecar_keeps_metadata_out_of_body():
    book = book_fixture()
    raw = {"content": "Useful completed body.\n```fullbody_metadata\n{\"completed_task_ids\":[", "finish_reason": "length"}
    result = parse_fullbody_response(raw, book)
    assert result["body_markdown"] == "Useful completed body."
    assert not result["complete"]
    assert result["pending_task_ids"] == list(fullbody_task_catalog(book))
    assert result["raw_response"] == raw


def test_known_book_source_outside_window_is_context_diagnostic_not_unknown_identity():
    book = book_fixture()
    result = parse_fullbody_response({"body_markdown": "Reread the later source [P0002][P9999].",
        "completed_task_ids": ["CH01::U1::A1"]}, book, ["CH01::U1::A1"])
    assert result["diagnostics"]["unknown_citations"] == ["P9999"]
    assert result["diagnostics"]["known_source_citations_outside_projection"] == ["P0002"]


@pytest.mark.parametrize("field,value,code", [
    ("task_dispositions", {}, "fullbody_task_dispositions_not_list"),
    ("completed_task_ids", {}, "fullbody_completed_task_ids_invalid"),
    ("complete", "true", "fullbody_complete_flag_invalid"),
    ("pending_task_ids", [None], "fullbody_pending_task_ids_invalid"),
])
def test_malformed_metadata_never_discards_prose_or_crashes(field, value, code):
    book = book_fixture()
    response = {"body_markdown": "Keep this usable prose.", "completed_task_ids": list(fullbody_task_catalog(book))}
    response[field] = value
    result = parse_fullbody_response(response, book)
    assert result["body_markdown"] == response["body_markdown"]
    assert not result["complete"]
    assert any(issue["code"] == code for issue in result["issues"])


def test_disposition_status_is_type_checked():
    book = book_fixture()
    result = parse_fullbody_response({"body_markdown": "Keep this useful draft.",
        "task_dispositions": [{"task_id": "CH01::U1::A1", "status": []}]}, book)
    assert not result["complete"]
    assert any(issue["code"] == "fullbody_task_disposition_status_invalid" for issue in result["issues"])


def test_citation_led_markdown_is_not_mistaken_for_broken_json():
    prose = "[P0001] reports a conditional result, interpreted here without guessing."
    result = parse_fullbody_response(prose, book_fixture())
    assert result["body_markdown"] == prose
    assert not result["complete"]


def test_source_navigation_has_complete_bibliographic_identity_without_scientific_duplicates():
    first, second = chapter(), chapter("CH02")
    title = "Complete long original study title " * 25
    for row in (first, second):
        row["sources"][0].update(title=title, canonical_paper_id="canonical-one")
    second["sources"][0]["title"] = "Alternate catalog title for the same DOI"
    second["sources"][0]["paper_id"] = "historical-record-one"
    book = build_fullbody_input([first, second])
    identity = book["source_identities"]["P0001"]
    assert identity["title"] == title
    assert len(identity["title"]) > 600
    assert identity["doi"] == "10.1/P0001"
    assert identity["canonical_paper_id"] == "canonical-one"
    assert identity["paper_id"] == "paper-P0001"
    assert identity["identity_metadata_variants"] == {
        "title": [second["sources"][0]["title"]], "paper_id": ["historical-record-one"]}
    assert "study_summary_A" not in identity
    assert "FINAL-SCIENCE-SENTINEL" not in json.dumps(identity)
    projected = project_fullbody(book)["source_identities"]["P0001"]
    assert projected["title"] == title
    assert projected["canonical_paper_id"] == "canonical-one"
    assert "record_ids" not in projected
    changed = deepcopy(first); changed["sources"][0]["title"] = "Corrected original catalog title"
    changed_book = build_fullbody_input([changed, second])
    assert changed_book["book_sha256"] != book["book_sha256"]
    assert changed_book["input_manifest"]["manifest_sha256"] != book["input_manifest"]["manifest_sha256"]


def test_cross_chapter_explicit_alias_same_doi_reconciles_without_rewriting_records():
    old, current = chapter("CH01", "P0584"), chapter("CH02", "P0333")
    doi = "10.3389/fimmu.2026.1803970"
    old["sources"][0].update(doi=doi, paper_id="CorpusId:290154100", title="The same publication")
    current["sources"][0].update(doi=doi, paper_id="8e25ec1d06aea0a73a479436c268bc6fab267259",
                                title="The same publication", aliases=["P0584"])
    current["sources"][0]["study_summary_A"] = {"finding": "Complementary current chapter material"}
    before = deepcopy([old, current])
    book = build_fullbody_input([old, current])
    assert book["source_aliases"] == {"P0584": "P0333"}
    assert list(book["source_identities"]) == ["P0333"]
    identity = book["source_identities"]["P0333"]
    assert identity["equivalent_source_handles"] == ["P0333", "P0584"]
    assert len(identity["record_ids"]) == 2
    assert book["sources"] == [old["sources"][0], current["sources"][0]]
    assert [old, current] == before
    assert build_fullbody_input([current, old])["source_aliases"] == book["source_aliases"]
    for key in ("CH01::U1::A1", "CH02::U1::A1"):
        payload = project_fullbody(book, [key])
        assert payload["sources"] == book["sources"]
        assert payload["source_aliases"] == {"P0584": "P0333"}
        result = parse_fullbody_response({"body_markdown": "Both original identities cite the same publication [P0584][P0333].",
            "completed_task_ids": [key]}, book, [key])
        assert result["complete"]
        assert result["diagnostics"]["unknown_citations"] == []


def test_equal_doi_without_explicit_alias_never_merges_sources():
    first, second = chapter(), chapter("CH02", "P0002")
    second["sources"][0]["doi"] = first["sources"][0]["doi"]
    book = build_fullbody_input([first, second])
    assert set(book["source_identities"]) == {"P0001", "P0002"}
    assert not book["source_aliases"]
    assert {row["source_handle"] for row in project_fullbody(book, ["CH01::U1::A1"])["sources"]} == {"P0001"}


@pytest.mark.parametrize("changes", [{"doi": "10.1/other"}, {"doi": "", "paper_id": "unknown"}])
def test_explicit_alias_does_not_reconcile_uncorroborated_or_foreign_canonical(changes):
    first, second = chapter(), chapter("CH02", "P0002")
    second["sources"][0].update(aliases=["P0001"], **changes)
    with pytest.raises(CandidateError, match="source_alias_ambiguous:P0001"):
        build_fullbody_input([first, second])


def test_reconciled_alias_retains_complementary_lineage_and_review_mediated_record():
    first, second = chapter(), chapter("CH02", "P0002")
    second["sources"][0].update(aliases=["P0001"], doi=first["sources"][0]["doi"])
    first["sources"][0]["review_source_handle"] = "P0003"
    first["sources"].append({"source_handle": "P0003", "paper_id": "review", "material_status": "review_reported_secondary",
                              "local_passages": {"usable_content": "Full review-mediated passage"}})
    book = build_fullbody_input([first, second])
    payload = project_fullbody(book, ["CH02::U1::A1"])
    assert {row["source_handle"] for row in payload["sources"]} == {"P0001", "P0002", "P0003"}
    assert next(row for row in payload["sources"] if row["source_handle"] == "P0003")["local_passages"]["usable_content"] == "Full review-mediated passage"


def test_alias_chain_cannot_hide_conflicting_dois_behind_missing_middle_doi():
    first, middle, last = chapter(), chapter("CH02", "P0002"), chapter("CH03", "P0003")
    for row in (first, middle, last):
        row["sources"][0]["paper_id"] = "shared-stable-record"
    middle["sources"][0].update(doi="", aliases=["P0001"])
    last["sources"][0].update(aliases=["P0002"], doi="10.1/foreign")
    with pytest.raises(CandidateError, match="source_identity_conflict"):
        build_fullbody_input([first, middle, last])
