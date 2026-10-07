"""Offline real input/projection/output seams for the additive candidate routes."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from optomind_research.runtime.upgrade3 import review_unit_writer as old_writer
from optomind_research.runtime.upgrade3.writer_candidates_contracts import (
    CandidateError, build_chapter_input, parse_candidate_response, project_chapter,
    render_blocks, task_catalog,
)

ROOT = Path(__file__).resolve().parents[2]
TABLE = "| Setting | Result |\n| --- | --- |\n| Low, 3 sessions | 12 [P0001] |"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Candidate contracts must not call the network")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def files(tmp_path):
    briefs = [{"paragraph_id": "B1", "point": "Owner first point", "development": "Original first intent",
               "source_handles": ["P0901"], "finding_conditions": {"setting": "low", "sessions": 3},
               "supporting_studies": [{"source_handle": "P0002", "outcome": "A nested independent case"}]},
              {"paragraph_id": "B2", "point": "Owner second point", "development": "Original second intent",
               "source_handles": ["P0002"], "inference_limits": ["No longer-term causal inference"]}]
    sources = {
        "P0001": {"source_handle": "P0001", "paper_id": "one", "doi": "10.1/one", "aliases": ["P0901"],
                  "study_summary_A": {"finding": "Long scientific material " * 500, "condition": "3 sessions"},
                  "study_summary_A_variants": [{"finding": "Independent complementary result"}],
                  "review_planning_B": {"planning_summary": "Role of source one"},
                  "review_planning_B_variants": [{"planning_summary": "Complementary scope"}],
                  "deep_read_material": {"question_material": "First deep result", "task_id": "bookkeeping"},
                  "deep_read_materials": [{"question_material": "Second deep result"}],
                  "local_passages": {"usable_content": "One local passage"},
                  "local_passages_variants": [{"usable_content": "Another local passage"}]},
        "P0002": {"source_handle": "P0002", "paper_id": "two", "study_summary_A": {"finding": "Nested case"}},
        "P0003": {"source_handle": "P0003", "paper_id": "three", "study_summary_A": {"finding": "Neighbor only science"}},
        "P0004": {"source_handle": "P0004", "paper_id": "original", "material_status": "resolved"},
        "P0005": {"source_handle": "P0005", "paper_id": "review", "study_summary_A": {"finding": "Reporting review"}},
    }
    unit1 = {"unit_id": "CH01:U1", "focus": "Explain the setting",
             "paragraph_tasks": [{"paragraph_id": "A1", "point": "Editor placement", "development": "Editor framing",
                                  "source_briefs": ["B1", "B2"], "portion": "Explain the joint result",
                                  "finding_conditions": {"model": "different experiments remain separate"},
                                  "argument_relations": [{"from": "B1", "to": "B2", "relation": "qualifies"}],
                                  "custom_scientific_field": {"verbatim": "Must not vanish"},
                                  "source_uses": [{"source_handle": "P0901", "role": "case", "condition": "low"}]},
                                 {"point": "Legacy task", "source_uses": [{"source_handle": "P0002"}]}],
             "table_tasks": [{"table_id": "T1", "purpose": "Compare settings", "columns": ["Setting", "Result"],
                              "inference_limits": {"no_comparison": "No controlled between-study ranking"},
                              "row_tasks": [{"row_id": "r1", "content": "low", "condition": "3 sessions",
                                             "source_uses": [{"source_handle": "P0001", "custom": "row role"}]}]}]}
    unit2 = {"unit_id": "CH01:U2", "focus": "Neighbor boundary", "paragraph_tasks": [
        {"paragraph_id": "A2", "point": "Neighbor responsibility", "development": "Neighbor complete task intent",
         "source_uses": [{"source_handle": "P0003"}]}], "table_tasks": []}
    arrangement = {"chapter_id": "CH01", "chapter_argument": "Accepted current chapter argument",
                   "validation": {"ok": True}, "units": [unit1, unit2], "source_catalog": sources,
                   "chapter_tool_materials": [
                       {"need_id": "N1", "unit_key": "CH01:CH01:U1", "question_id": "Q-ONE",
                        "usable_content": "The review reports an original study, not independently read.",
                        "sources": [{"source_handle": "P0004", "paper_id": "original"},
                                    {"source_handle": "P0005", "paper_id": "review"}],
                        "source_status": "review_reported_secondary", "direct_verified": False},
                       {"need_id": "N2", "unit_key": "CH01:CH01:U2", "usable_content": "Neighbor tool only",
                        "sources": [{"source_handle": "P0003"}]}]}
    view = {"chapter_id": "CH01", "title": "Chapter", "purpose": "Explain", "research_question": "Review question",
            "review_argument": "Review-wide argument", "thesis": "Old chapter argument",
            "other_chapters": [{"chapter_id": "CH02", "purpose": "Separate responsibility"}],
            "units": [{"unit_id": "CH01:U1", "paragraph_briefs": briefs,
                       "argument_relations": {"B2": "qualifies B1"}, "evidence_conditions": ["Keep settings attached"],
                       "synthesis": "Do not confuse independent observations"}]}
    arrangement_path = tmp_path / "ARRANGEMENT.json"
    view_path = tmp_path / "ARRANGEMENT_INPUT.json"
    arrangement_path.write_text(json.dumps(arrangement), encoding="utf-8")
    view_path.write_text(json.dumps(view), encoding="utf-8")
    return arrangement_path, view_path, arrangement, view


def chapter_from(files):
    return build_chapter_input(files[0], view_path=files[1], language="en")


def test_builder_carries_complete_original_tasks_conditions_identity_and_materials(files):
    chapter = chapter_from(files)
    prior = old_writer.build_unit_view(files[0], "CH01:U1", view_path=files[1])
    unit = chapter["units"][0]
    assert unit["paragraph_tasks"] == prior.paragraph_tasks
    assert unit["table_tasks"] == prior.table_tasks
    assert unit["owner_unit_context"] == prior.owner_unit_context
    assert chapter["chapter_frame"]["chapter_argument"] == "Accepted current chapter argument"
    assert chapter["chapter_frame"]["research_question"] == "Review question"
    assert chapter["chapter_frame"]["review_argument"] == "Review-wide argument"
    assert unit["paragraph_tasks"][0]["source_brief_details"][0]["finding_conditions"]["sessions"] == 3
    sources = {source["source_handle"]: source for source in chapter["sources"]}
    assert sources["P0001"]["study_summary_A"] == files[2]["source_catalog"]["P0001"]["study_summary_A"]
    assert sources["P0001"]["aliases"] == ["P0901"]
    assert "P0002" in sources  # Nested case, not only old unit_handles grammar
    assert sources["P0001"]["study_summary_A_variants"] == [{"finding": "Independent complementary result"}]
    assert sources["P0001"]["deep_read_materials"] == [{"question_material": "Second deep result"}]
    assert sources["P0001"]["local_passages_variants"] == [{"usable_content": "Another local passage"}]
    assert not sources["P0004"].get("study_summary_A")
    assert chapter["chapter_tool_materials"][0]["source_status"] == "review_reported_secondary"
    assert chapter["provenance"]["generated_task_ids"][0]["task_key"] == "CH01:U1::__legacy_paragraph_0002"
    assert "paragraph_id" not in unit["paragraph_tasks"][1]


def test_projection_preserves_whole_tasks_and_read_only_neighbor_roles_without_neighbor_sources(files):
    chapter = chapter_from(files)
    before = deepcopy(chapter)
    payload = project_chapter(chapter, ["CH01:U1::A1"])
    assert payload["editable_task_ids"] == ["CH01:U1::A1"]
    assert len(payload["units"]) == 1
    assert payload["units"][0]["paragraph_tasks"] == [chapter["units"][0]["paragraph_tasks"][0]]
    assert payload["units"][0]["table_tasks"] == []
    assert {source["source_handle"] for source in payload["sources"]} == {"P0001", "P0002", "P0004", "P0005"}
    assert "Neighbor only science" not in json.dumps(payload)
    assert "Neighbor tool only" not in json.dumps(payload)
    assert "Neighbor complete task intent" in json.dumps(payload)
    assert len(payload["shared_chapter_context"]["task_roles"]) == len(task_catalog(chapter))
    assert payload["shared_chapter_context"]["read_only"]
    assert payload["chapter_tool_materials"][0]["direct_verified"] is False
    assert chapter == before


def test_legacy_identity_survives_projected_subsets_and_is_deterministic(files):
    chapter = chapter_from(files)
    key = "CH01:U1::__legacy_paragraph_0002"
    subset = project_chapter(chapter, [key])
    assert list(task_catalog(subset)) == [key]
    assert task_catalog(chapter) == task_catalog(deepcopy(chapter))
    assert "paragraph_id" not in task_catalog(subset)[key]["task"]


@pytest.mark.parametrize("change,match", [
    (lambda c: c["units"].append(deepcopy(c["units"][0])), "unit_id_duplicated"),
    (lambda c: c["units"][0]["table_tasks"].append({"table_id": "A1"}), "task_id_duplicated"),
    (lambda c: c["sources"][1].update(aliases=["P0901"]), "source_alias_ambiguous"),
    (lambda c: c["sources"].append({"source_handle": "P0001", "doi": "10.1/foreign"}), "source_identity_conflict"),
])
def test_ambiguous_identity_fails_closed(files, change, match):
    chapter = chapter_from(files)
    change(chapter)
    with pytest.raises(CandidateError, match=match):
        project_chapter(chapter)


def test_complementary_same_source_view_is_preserved_not_last_write_wins(files):
    chapter = chapter_from(files)
    complementary = deepcopy(chapter["sources"][0])
    complementary["study_summary_A"] = {"finding": "Different complete view"}
    chapter["sources"] += [complementary, deepcopy(complementary)]
    payload = project_chapter(chapter)
    source = next(source for source in payload["sources"] if source["source_handle"] == "P0001")
    assert source["study_summary_A"] != complementary["study_summary_A"]
    assert source["material_record_variants"] == [complementary]


def test_rejected_arrangement_stops_at_real_read_boundary(files):
    raw = deepcopy(files[2]); raw["validation"] = {"ok": False}
    files[0].write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CandidateError, match="arrangement_not_ready"):
        chapter_from(files)


@pytest.mark.parametrize("ids,match", [(["unknown"], "task_ids_unknown"), ([], "task_ids_empty"),
                                      (["CH01:U1::A1"] * 2, "task_ids_duplicated"), ("CH01:U1::A1", "task_ids_not_list")])
def test_task_scope_is_checked(files, ids, match):
    with pytest.raises(CandidateError, match=match):
        project_chapter(chapter_from(files), ids)


def output(chapter, body="Connected discussion [P0901].\n\n" + TABLE):
    return {"blocks": [{"block_id": "free-shape", "task_ids": list(task_catalog(chapter)), "body_markdown": body}], "issues": []}


@pytest.mark.parametrize("wrap", [lambda x: x, json.dumps, lambda x: "```json\n" + json.dumps(x) + "\n```",
                                  lambda x: {"content": json.dumps(x), "complete": True, "finish_reason": "stop"},
                                  lambda x: {"choices": [{"message": {"content": json.dumps(x)}, "finish_reason": "stop"}]},
                                  lambda x: {"response": x}])
def test_flexible_cross_unit_blocks_round_trip_provider_envelopes(files, wrap):
    chapter = chapter_from(files)
    expected = output(chapter)
    result = parse_candidate_response(wrap(expected), chapter)
    assert result["complete"] and not result["pending_task_ids"]
    assert result["body_markdown"] == expected["blocks"][0]["body_markdown"]
    assert result["blocks"] == expected["blocks"]
    assert result["diagnostics"]["unknown_citations"] == []
    assert result["semantic_quality_unreviewed"]
    assert result["diagnostics"]["content_review_status"] == "not_reviewed"
    assert result["diagnostics"]["coverage_kind"] == "model_declared_structural_only"


@pytest.mark.parametrize("body", ["This text claims the table is complete.",
                                  "```markdown\n" + TABLE + "\n```",
                                  "| Setting | Result |\n|---|---|\n| wrong | extra | cells |"])
def test_table_claim_requires_actual_finished_markdown(files, body):
    chapter = chapter_from(files)
    result = parse_candidate_response(output(chapter, body), chapter)
    assert not result["complete"]
    assert result["pending_task_ids"] == ["CH01:U1::T1"]
    assert result["body_markdown"] == body


def test_unknown_or_out_of_scope_claims_do_not_discard_prose(files):
    chapter = chapter_from(files)
    response = {"blocks": [{"block_id": "b", "task_ids": ["CH01:U1::A1", "CH01:U2::A2", "made-up"],
                             "body_markdown": "Useful untrusted partial prose [P0001]."}]}
    result = parse_candidate_response(response, chapter, ["CH01:U1::A1"])
    assert not result["complete"]
    assert result["body_markdown"] == response["blocks"][0]["body_markdown"]
    assert result["blocks"][0]["task_ids"] == ["CH01:U1::A1"]
    assert result["blocks"][0]["reported_task_ids"] == response["blocks"][0]["task_ids"]
    issue = next(issue for issue in result["issues"] if issue["code"] == "candidate_task_reference_invalid")
    assert issue["unknown_task_ids"] == ["made-up"]
    assert issue["out_of_scope_task_ids"] == ["CH01:U2::A2"]


def test_citations_are_diagnosed_without_numeric_or_handle_guessing(files):
    chapter = chapter_from(files)
    body = "Finding [1] [P0901] [P9999] [Q-ONE]. Code `[88]`. Link [2](https://example.invalid).\n\n" + TABLE
    result = parse_candidate_response(output(chapter, body), chapter)
    assert result["body_markdown"] == body
    assert result["diagnostics"]["unknown_citations"] == ["P9999"]
    assert result["diagnostics"]["unresolved_numeric_citations"] == ["[1]"]
    assert result["diagnostics"]["non_source_identifier_citations"] == ["[Q-ONE]"]
    assert result["diagnostics"]["numeric_citation_repairs"] == []


def test_truncated_json_keeps_complete_blocks_and_unfinished_prose(files):
    chapter = chapter_from(files)
    first = {"block_id": "first", "task_ids": ["CH01:U1::A1"], "body_markdown": "First useful block."}
    second_prefix = '{"block_id":"second","task_ids":["CH01:U2::A2"],"body_markdown":"Useful unfinished \\nsecond'
    content = '{"blocks":[' + json.dumps(first) + ',' + second_prefix
    raw = {"content": content, "complete": False, "finish_reason": "length"}
    result = parse_candidate_response(raw, chapter)
    assert not result["complete"]
    assert result["body_markdown"] == "First useful block.\n\nUseful unfinished \nsecond"
    assert result["raw_response"] == raw
    assert "CH01:U1::A1" not in result["pending_task_ids"]
    assert "CH01:U2::A2" in result["pending_task_ids"]


def test_partial_transport_cannot_claim_success_even_with_complete_json(files):
    chapter = chapter_from(files)
    response = {"content": json.dumps(output(chapter)), "complete": False, "finish_reason": "length"}
    result = parse_candidate_response(response, chapter)
    assert result["body_markdown"]
    assert not result["complete"]
    assert result["diagnostics"]["transport_incomplete"]
    assert result["raw_response"] == response


def test_plain_prose_is_preserved_unassigned_and_not_called_complete(files):
    chapter = chapter_from(files)
    result = parse_candidate_response("A useful draft without an envelope [P0001].", chapter)
    assert result["body_markdown"] == "A useful draft without an envelope [P0001]."
    assert result["pending_task_ids"] == list(task_catalog(chapter))
    assert not result["complete"]


def test_render_never_manufactures_task_or_unit_headings():
    assert render_blocks([{"block_id": "b1", "body_markdown": "First flowing paragraph."},
                          {"block_id": "b2", "body_markdown": "Second flowing paragraph."}]) == (
        "First flowing paragraph.\n\nSecond flowing paragraph.")


def test_actual_archived_material_builder_and_full_tasks_reach_serialized_payload():
    root = ROOT / "docs/acceptance/preflight-materials-local-20261004/real_checks/results"
    chapter = build_chapter_input(root / "ARRANGEMENT_FROM_REAL_PACKET.json",
                                  view_path=root / "ARRANGEMENT_INPUT_FROM_REAL_PACKET.json")
    original = old_writer.build_unit_view(root / "ARRANGEMENT_FROM_REAL_PACKET.json", "CH02:U3",
                                          view_path=root / "ARRANGEMENT_INPUT_FROM_REAL_PACKET.json")
    sent = json.loads(json.dumps(project_chapter(chapter), ensure_ascii=False))
    assert sent["units"][0]["paragraph_tasks"] == original.paragraph_tasks
    assert sent["units"][0]["table_tasks"] == original.table_tasks
    assert sent["units"][0]["owner_unit_context"] == original.owner_unit_context
    assert sent["sources"] == original.materials


def test_explicit_catalog_identity_cannot_be_silently_reassigned(files):
    raw = deepcopy(files[2])
    raw["source_catalog"]["P0002"]["source_handle"] = "P9999"
    files[0].write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CandidateError, match="source_catalog_identity_conflict:P0002"):
        chapter_from(files)


@pytest.mark.parametrize("content,expected", [(r"value \ud83d\ude00", "value 😀"),
                                               (r"value \ud83d", r"value \ud83d")])
def test_truncated_unicode_remains_safe_to_save(files, content, expected):
    chapter = chapter_from(files)
    raw = '{"blocks":[{"block_id":"b","task_ids":["CH01:U1::A1"],"body_markdown":"' + content
    result = parse_candidate_response(raw, chapter)
    assert result["body_markdown"] == expected
    json.dumps(result, ensure_ascii=False).encode("utf-8")


def test_malformed_provider_choices_does_not_discard_usable_blocks(files):
    chapter = chapter_from(files)
    raw = output(chapter); raw["choices"] = None
    assert parse_candidate_response(raw, chapter)["complete"]


def test_selected_task_and_owner_context_have_one_authoritative_copy(files):
    chapter = chapter_from(files)
    payload = project_chapter(chapter, ["CH01:U1::A1"])
    serialized = json.dumps(payload)
    assert serialized.count("Must not vanish") == 1
    assert serialized.count("Keep settings attached") == 1
    assert serialized.count("Neighbor complete task intent") == 1
    roles = {role["task_id"]: role for role in payload["shared_chapter_context"]["task_roles"]}
    assert roles["CH01:U1::A1"]["editable_task_reference"] == "CH01:U1::A1"
    assert "task" not in roles["CH01:U1::A1"]
    assert roles["CH01:U2::A2"]["task"] == chapter["units"][1]["paragraph_tasks"][0]
    assert payload["shared_chapter_context"]["unit_contexts"][0] == {
        "unit_id": "CH01:U1", "editable_unit_reference": "CH01:U1"}


def test_nested_condition_and_source_lineage_material_closure(files):
    raw = deepcopy(files[2])
    raw["units"][0]["paragraph_tasks"][0]["finding_conditions"]["linked_case"] = {"source_handle": "P0006"}
    raw["source_catalog"]["P0006"] = {
        "source_handle": "P0006", "paper_id": "six", "study_summary_A": {
            "finding": "Only nested condition knows this source",
            "review_source_handle": "P0007"}}
    raw["source_catalog"]["P0007"] = {
        "source_handle": "P0007", "paper_id": "seven", "study_summary_A": {
            "finding": "Review source lineage support", "source_handle": "P0006"}}
    files[0].write_text(json.dumps(raw), encoding="utf-8")
    chapter = chapter_from(files)
    payload = project_chapter(chapter, ["CH01:U1::A1"])
    sources = {item["source_handle"]: item for item in payload["sources"]}
    assert sources["P0006"]["study_summary_A"]["finding"] == "Only nested condition knows this source"
    assert sources["P0007"]["study_summary_A"]["finding"] == "Review source lineage support"
    assert "P0003" not in sources


def test_rejected_original_handles_are_provenance_not_dependencies(files):
    raw = deepcopy(files[2])
    tool = raw["chapter_tool_materials"][0]
    tool["sources"] += [
        {"original_source_handle": "P9998", "identity_status": "conflicting_handle", "paper_id": "foreign"},
        {"original_source_handle": "P0003", "identity_status": "conflicting_identity", "paper_id": "foreign-neighbor"},
        {"source_handle": "P0004", "original_source_handle": "P9997", "identity_status": "remapped"},
        # Historical conflict status can survive a later successful resolution.
        {"source_handle": "P0005", "original_source_handle": "P9996", "identity_status": "conflicting_handle"},
    ]
    files[0].write_text(json.dumps(raw), encoding="utf-8")
    chapter = chapter_from(files)
    payload = project_chapter(chapter, ["CH01:U1::A1"])
    handles = {item["source_handle"] for item in payload["sources"]}
    assert {"P0004", "P0005"} <= handles
    assert not handles & {"P9998", "P9997", "P9996", "P0003"}
    assert payload["chapter_tool_materials"][0] == tool
    result = parse_candidate_response({"blocks": [{"block_id": "b", "task_ids": ["CH01:U1::A1"],
        "body_markdown": "Do not legitimize these [P9998][P9997][P9996][P0003]."}]}, chapter, ["CH01:U1::A1"])
    assert result["diagnostics"]["unknown_citations"] == ["P9998", "P9997", "P9996", "P0003"]


def test_provenance_hashes_actual_selected_view_and_arrangement(files):
    import hashlib
    chapter = chapter_from(files)
    assert chapter["provenance"]["view_path"] == str(files[1].resolve())
    assert chapter["provenance"]["view_sha256"] == hashlib.sha256(files[1].read_bytes()).hexdigest()
    assert chapter["provenance"]["arrangement_sha256"] == hashlib.sha256(files[0].read_bytes()).hexdigest()
    before = chapter["provenance"]["view_sha256"]
    view = deepcopy(files[3]); view["research_question"] = "Changed review question"
    files[1].write_text(json.dumps(view), encoding="utf-8")
    after = chapter_from(files)
    assert after["provenance"]["view_sha256"] != before
    assert after["provenance"]["arrangement_sha256"] == chapter["provenance"]["arrangement_sha256"]


def test_packet_fallback_hashes_used_packet_instead_of_nonexistent_view(files, tmp_path):
    import hashlib
    packet_root = tmp_path / "packet-root"
    (packet_root / "writer_packets").mkdir(parents=True)
    packet_file = packet_root / "writer_packets/CH01.json"
    packet = {"chapter_id": "CH01", "chapter": {"chapter_id": "CH01", "title": "Packet chapter"},
              "chapter_plan": {"units": deepcopy(files[3]["units"])},
              "source_materials": list(deepcopy(files[2]["source_catalog"]).values())}
    packet_file.write_text(json.dumps(packet), encoding="utf-8")
    files[1].unlink()
    chapter = build_chapter_input(files[0], view_path=files[1], packet_root=packet_root)
    assert chapter["provenance"]["view_path"] == ""
    assert "view_sha256" not in chapter["provenance"]
    assert chapter["provenance"]["packet_path"] == str(packet_file.resolve())
    assert chapter["provenance"]["packet_sha256"] == hashlib.sha256(packet_file.read_bytes()).hexdigest()
