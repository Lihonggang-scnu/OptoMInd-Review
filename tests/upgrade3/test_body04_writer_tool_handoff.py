"""Offline packet -> arrangement -> persisted writer-message seam controls."""
import json

from optomind_research.runtime.upgrade3.chapter_arrangement import (
    build_chapter_view, build_source_catalog, compact_chapter_tool_materials,
)
from optomind_research.runtime.upgrade3.review_unit_writer import build_unit_view, unit_messages, write_unit_output


def _deliver(tmp_path, sources, *, identity=None, unit_key="", content=None, task_handles=None, row_tools=False):
    packet = {
        "chapter_id": "CH01", "chapter_plan": {"units": [{
            "substantive_point": "compare", "paragraph_briefs": [{
                "point": "comparison", "source_handles": ["P0001"],
            }],
        }]},
        "source_materials": [{"source_handle": "P0001", "paper_id": "review",
                              "study_summary_A": {"key_findings": "Review finding"}}],
        "source_identity_map": identity or {},
        "tool_materials": [{"need_id": "N1", "usable_content": "Original trial: intervention improved retention over control at six months.",
                            "unit_key": unit_key, "sources": sources}],
    }
    if content is not None:
        packet["tool_materials"][0]["usable_content"] = content
    if row_tools:
        packet["source_materials"][0]["tool_materials"] = packet.pop("tool_materials")
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet))
    chapter = build_chapter_view(path, id_map_path=tmp_path / "ids.json")
    unit_id = chapter.units[0].unit_id
    arrangement = {"chapter_id": "CH01", "units": [{"unit_id": unit_id, "paragraph_tasks": [{
        "paragraph_id": unit_id + "_P01", "source_uses": [
            {"source_handle": handle} for handle in (task_handles or ["P0001"])],
    }]}], "source_catalog": build_source_catalog(chapter),
        "chapter_tool_materials": compact_chapter_tool_materials(chapter)}
    arranged = tmp_path / "arrangement.json"
    arranged.write_text(json.dumps(arrangement))
    input_path = tmp_path / "ARRANGEMENT_INPUT.json"
    input_path.write_text(json.dumps(chapter.to_dict()))
    view = build_unit_view(arranged, unit_id)
    message = unit_messages(view)[-1]
    message_path = tmp_path / "WRITER_MESSAGE.json"
    message_path.write_text(json.dumps(message))
    return chapter, arrangement, view, json.loads(json.loads(message_path.read_text())["content"])


def test_tool_only_identity_and_synthesis_reach_writer(tmp_path):
    chapter, arrangement, view, payload = _deliver(tmp_path, [
        {"source_handle": "P0001", "paper_id": "review"},
        {"source_handle": "P0002", "paper_id": "trial", "title": "Retention trial", "doi": "10.1000/trial"},
    ])
    assert "P0002" in arrangement["source_catalog"]
    assert "P0002" in view.sources
    assert any(row["paper_id"] == "trial" for row in payload["sources"])
    assert "six months" in payload["chapter_tool_materials"][0]["usable_content"]
    assert not arrangement["source_catalog"]["P0002"].get("tool_supplement_materials")


def test_stable_identity_remaps_old_collision(tmp_path):
    _, arrangement, view, payload = _deliver(tmp_path, [
        {"source_handle": "P0001", "paper_id": "trial", "doi": "10.1000/trial"},
        {"paper_id": "unresolved", "title": "Unresolved study"},
    ], identity={"P0002": {"paper_id": "trial", "doi": "10.1000/trial", "title": "Retention trial"}})
    sources = payload["chapter_tool_materials"][0]["sources"]
    assert sources[0]["source_handle"] == "P0002"
    assert sources[1].get("source_handle", "") == ""
    assert "P0002" in view.sources
    assert arrangement["source_catalog"]["P0001"]["paper_id"] == "review"


def test_unresolved_collision_keeps_content_without_borrowing_handle(tmp_path):
    _, _, view, payload = _deliver(tmp_path, [
        {"source_handle": "P0001", "paper_id": "unknown_trial", "title": "Unknown trial"},
    ])
    source = payload["chapter_tool_materials"][0]["sources"][0]
    assert not source.get("source_handle")
    assert source["paper_id"] == "unknown_trial"
    assert source["identity_status"] == "conflicting_handle"
    assert "six months" in payload["chapter_tool_materials"][0]["usable_content"]
    assert list(view.sources) == ["P0001"]


def test_tool_only_citation_is_known_in_persisted_writer_output(tmp_path):
    _, _, view, _ = _deliver(tmp_path, [
        {"source_handle": "P0001", "paper_id": "review"},
        {"source_handle": "P0002", "paper_id": "trial"},
    ])
    result = write_unit_output(view, "Supported trial [P0002]", tmp_path / "output",
        model="offline", language="en", mode="fake", used_messages=unit_messages(view), estimate={})
    assert result["unknown_citations"] == []


def test_doi_only_resolution_and_unit_routing(tmp_path):
    _, _, view, payload = _deliver(tmp_path, [{"doi": "https://doi.org/10.1000/trial"}],
        identity={"P0002": {"paper_id": "trial", "doi": "10.1000/trial"}}, unit_key="CH01:CH01_U02")
    assert "P0002" not in view.sources
    assert not payload["chapter_tool_materials"]


def test_conflicting_identifiers_do_not_borrow_either_paper(tmp_path):
    _, _, view, payload = _deliver(tmp_path, [
        {"source_handle": "P0001", "paper_id": "review", "doi": "10.1000/trial"},
    ], identity={"P0002": {"paper_id": "trial", "doi": "10.1000/trial"}})
    source = payload["chapter_tool_materials"][0]["sources"][0]
    assert not source.get("source_handle")
    assert source["identity_status"] == "conflicting_identity"
    assert list(view.sources) == ["P0001"]


def test_same_old_handle_different_studies_are_distinct():
    from optomind_research.runtime.upgrade3.chapter_arrangement import distinct_tool_material_sources
    assert len(distinct_tool_material_sources({"sources": [
        {"source_handle": "P0001", "paper_id": "first"},
        {"source_handle": "P0001", "paper_id": "second"},
    ]})) == 2


def test_single_source_review_derived_material_needs_no_own_cards(tmp_path):
    _, arrangement, view, payload = _deliver(tmp_path, [
        {"source_handle": "P0002", "paper_id": "trial", "title": "Retention trial"},
    ], task_handles=["P0002"])
    source = payload["sources"][0]
    assert source["paper_id"] == "trial"
    assert source["material_status"] == "resolved"
    assert "six months" in payload["chapter_tool_materials"][0]["usable_content"]
    assert not source.get("study_summary_A")
    assert not source.get("review_planning_B")
    assert not view.material_notes[0]["issues"]


def test_identity_only_tool_does_not_create_study_material(tmp_path):
    _, arrangement, view, _ = _deliver(tmp_path, [
        {"source_handle": "P0002", "paper_id": "trial"},
    ], content="")
    assert "P0002" not in arrangement["source_catalog"]
    assert "P0002" not in view.sources


def test_adopted_row_tool_relationship_reaches_writer(tmp_path):
    _, _, view, payload = _deliver(tmp_path, [
        {"source_handle": "P0001", "paper_id": "review"},
        {"source_handle": "P0002", "paper_id": "trial"},
    ], row_tools=True)
    assert "P0002" in view.sources
    assert "six months" in payload["chapter_tool_materials"][0]["usable_content"]


def test_completion_carries_tool_only_citation_identity(tmp_path):
    from optomind_research.runtime.upgrade3.review_unit_writer import completion_messages
    _, _, view, _ = _deliver(tmp_path, [
        {"source_handle": "P0001", "paper_id": "review"},
        {"source_handle": "P0002", "paper_id": "trial"},
    ])
    message = completion_messages(view, "existing", [view.paragraph_tasks[0]["paragraph_id"]])[-1]
    payload = json.loads(message["content"])
    assert "P0002" in payload["requested_source_handles"]
    assert any(row["paper_id"] == "trial" for row in payload["sources"])


def test_single_tool_only_target_unit_without_owner_task_reaches_writer(tmp_path):
    _, _, view, payload = _deliver(tmp_path, [
        {"source_handle": "P0002", "paper_id": "trial", "title": "Retention trial"},
    ], unit_key="CH01:CH01_U01")
    assert "P0002" in view.sources
    assert "six months" in payload["chapter_tool_materials"][0]["usable_content"]
    assert payload["chapter_tool_materials"][0]["unit_key"] == "CH01:CH01_U01"


def test_existing_source_unit_scoped_tool_is_not_folded_across_units(tmp_path):
    _, _, _, payload = _deliver(tmp_path, [
        {"source_handle": "P0001", "paper_id": "review"},
    ], unit_key="CH01:CH01_U02")
    assert not payload["chapter_tool_materials"]
    assert not payload["sources"][0].get("tool_supplement_materials")


def test_packet_current_map_conflict_fails_before_content_is_spliced(tmp_path):
    import pytest
    from optomind_research.runtime.upgrade3.chapter_arrangement import ChapterArrangementError
    with pytest.raises(ChapterArrangementError, match="source_identity_conflict:P0001"):
        _deliver(tmp_path, [], identity={"P0001": {"paper_id": "different_current_study"}})


def test_foreign_locator_card_cannot_backfill_science(tmp_path):
    _, _, view, _ = _deliver(tmp_path, [])
    arrangement_path = tmp_path / "arrangement.json"
    arrangement = json.loads(arrangement_path.read_text())
    card = tmp_path / "foreign_card.json"
    card.write_text(json.dumps({"paper_identity": {"canonical_paper_id": "foreign"},
        "general_understanding": {"approach": "FOREIGN_SCIENCE_SHOULD_NOT_ENTER"}}))
    arrangement["source_catalog"]["P0001"]["locator"]["card_path"] = str(card)
    arrangement_path.write_text(json.dumps(arrangement))
    actual = build_unit_view(arrangement_path, view.unit_id)
    message = unit_messages(actual)[-1]["content"]
    assert "FOREIGN_SCIENCE_SHOULD_NOT_ENTER" not in message
    assert "Review finding" in message
    assert "foreign_locator_card_ignored" in message
    assert any("locator_identity_conflict" in issue for issue in actual.material_notes[0]["issues"])


def test_legacy_card_without_identity_still_backfills(tmp_path):
    _, _, view, _ = _deliver(tmp_path, [])
    arrangement_path = tmp_path / "arrangement.json"
    arrangement = json.loads(arrangement_path.read_text())
    card = tmp_path / "legacy_card.json"
    card.write_text(json.dumps({"general_understanding": {"approach": "Legacy usable finding"}}))
    arrangement["source_catalog"]["P0001"]["locator"]["card_path"] = str(card)
    arrangement_path.write_text(json.dumps(arrangement))
    actual = build_unit_view(arrangement_path, view.unit_id)
    assert "Legacy usable finding" in unit_messages(actual)[-1]["content"]


def test_nested_card_study_identity_cannot_splice_foreign_science(tmp_path):
    _, _, view, _ = _deliver(tmp_path, [])
    arrangement_path = tmp_path / "arrangement.json"
    arrangement = json.loads(arrangement_path.read_text())
    card = tmp_path / "nested_foreign_card.json"
    card.write_text(json.dumps({"general_understanding": {
        "paper_identity": {"canonical_paper_id": "new", "doi": "10.1/new"},
        "finding": "NEW_RESULT"}}))
    source = arrangement["source_catalog"]["P0001"]
    source["paper_id"] = "old"
    source["doi"] = "10.1/old"
    source["locator"]["card_path"] = str(card)
    arrangement_path.write_text(json.dumps(arrangement))
    actual = build_unit_view(arrangement_path, view.unit_id)
    message = unit_messages(actual)[-1]["content"]
    assert "NEW_RESULT" not in message
    assert "Review finding" in message
    assert "foreign_locator_card_ignored" in message


def test_cited_reference_identity_is_not_the_card_identity(tmp_path):
    from optomind_research.runtime.upgrade3.review_unit_writer import _read_card_material
    card = tmp_path / "references.json"
    card.write_text(json.dumps({"general_understanding": {
        "finding": "Legitimate study finding",
        "references": [{"paper_identity": {"canonical_paper_id": "cited_other_paper"}}]}}))
    entry = {"paper_id": "own_paper"}
    _read_card_material(card, entry)
    assert entry["study_summary_A"]["finding"] == "Legitimate study finding"
