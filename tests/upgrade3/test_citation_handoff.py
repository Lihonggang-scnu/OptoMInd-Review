"""Offline citation handoff checks for the manuscript BODY path.

These tests exercise the mechanical source inventory and citation handling only;
they never call a model and never rewrite an original model response.
"""

import json
from pathlib import Path

from optomind_research.runtime.upgrade3.review_unit_writer import (
    UnitWritingView,
    build_unit_view,
    normalize_numeric_citations,
    unit_payload,
    write_unit_output,
)
from scripts.upgrade3.full_review_draft import Job, load_unit_document


def _minimal_view(tmp_path: Path, handles: list[str]) -> UnitWritingView:
    return UnitWritingView(
        chapter_id="CH05",
        unit_id="CH05_U01",
        focus="citation handoff",
        unit_index=1,
        sibling_units=[],
        unit_count=1,
        chapter_frame={"chapter_title": "Synthetic chapter"},
        other_chapters=[],
        paragraph_tasks=[],
        table_tasks=[],
        materials=[],
        sources={handle: {"source_handle": handle} for handle in handles},
        arrangement_path=str(tmp_path / "CHAPTER_ARRANGEMENT.json"),
        view_path=str(tmp_path / "ARRANGEMENT_INPUT.json"),
    )


def test_explicit_numeric_map_is_derived_for_assembler_without_changing_original(tmp_path):
    view = _minimal_view(tmp_path, ["P0400", "P0583", "P0578"])
    body = "证据显示关联仍需谨慎[1]。\n\n[1] P0400\n"
    output = tmp_path / "writer" / "CH05_CH05_U01"
    result = write_unit_output(
        view,
        body,
        output,
        model="fixture",
        language="zh",
        mode="run",
        used_messages=[],
        estimate={},
        response_path=str(tmp_path / "raw.raw"),
    )

    assert result["citation_normalization"]["status"] == "normalized"
    assert result["used_source_handles"] == ["P0400"]
    assert Path(result["body_path"]).read_text(encoding="utf-8").endswith("[1] P0400\n")
    derived = Path(result["citation_normalized_body_path"])
    assert derived.read_text(encoding="utf-8").strip() == "证据显示关联仍需谨慎[P0400]。"

    arrangement = {
        "chapter_id": "CH05",
        "chapter_argument": "Synthetic",
        "units": [{"unit_id": "CH05_U01", "focus": "citation handoff", "paragraph_tasks": []}],
    }
    arrangement_path = tmp_path / "CHAPTER_ARRANGEMENT.json"
    arrangement_path.write_text(json.dumps(arrangement), encoding="utf-8")
    document = load_unit_document(
        Job("CH05", "CH05_U01", arrangement_path, output, None),
        output / "UNIT_RESULT.json",
        arrangement,
    )
    assert "[P0400]" in document.body
    assert "[1] P0400" not in document.body


def test_unmapped_numeric_citations_remain_visible_and_unresolved(tmp_path):
    view = _minimal_view(tmp_path, ["P0092", "P0595", "P0590"])
    body = "外周代谢物与局部机制仍有缺口[1]。"
    output = tmp_path / "writer" / "CH02_CH02_U02"
    result = write_unit_output(
        view,
        body,
        output,
        model="fixture",
        language="zh",
        mode="run",
        used_messages=[],
        estimate={},
    )

    assert result["citation_normalization"]["status"] == "unresolved"
    assert {issue["code"] for issue in result["issues"]} == {"numeric_citations_unresolved"}
    assert not result["citation_normalized_body_path"]
    assert Path(result["body_path"]).read_text(encoding="utf-8").strip() == body


def test_unique_allowed_handle_suffix_alias_is_normalized_without_source_order(tmp_path):
    view = _minimal_view(tmp_path, ["P0097", "P0328", "P0415", "P0511"])
    body = "黑色素瘤队列[415]，TNBC队列[328]，代谢通路[511]，NSCLC模型[97]。"
    output = tmp_path / "writer" / "CH02_CH02_U02"
    result = write_unit_output(
        view,
        body,
        output,
        model="fixture",
        language="zh",
        mode="run",
        used_messages=[],
        estimate={},
    )

    assert result["citation_normalization"] == {
        "status": "normalized",
        "body": "黑色素瘤队列[P0415]，TNBC队列[P0328]，代谢通路[P0511]，NSCLC模型[P0097]。",
        "mapping": {"97": "P0097", "328": "P0328", "415": "P0415", "511": "P0511"},
        "issues": [],
    }
    assert result["used_source_handles"] == ["P0415", "P0328", "P0511", "P0097"]
    assert Path(result["citation_normalized_body_path"]).read_text(encoding="utf-8").strip() == result["citation_normalization"]["body"]
    assert Path(result["body_path"]).read_text(encoding="utf-8").strip() == body


def test_numeric_suffix_collision_and_nonmatching_ordinal_stay_unresolved():
    collision = normalize_numeric_citations("证据[97]。", ["P0097", "P097"])
    assert collision["status"] == "unresolved"
    assert collision["mapping"] == {}
    assert collision["issues"][0]["code"] == "numeric_citations_unresolved"

    ordinal = normalize_numeric_citations("证据[1]。", ["P0097", "P0328"])
    assert ordinal["status"] == "unresolved"
    assert ordinal["mapping"] == {}
    assert ordinal["issues"][0]["numbers"] == [1]


def test_chapter_tool_source_is_allowed_only_when_current_catalog_resolves_it(tmp_path):
    packet = {
        "chapter": {"chapter_id": "CH03", "title": "Synthetic"},
        "chapter_plan": {
            "thesis": "Synthetic thesis",
            "units": [{
                "substantive_point": "Synthetic point",
                "paragraph_briefs": [{"point": "Evidence", "development": "Use it", "source_handles": ["P0100"]}],
            }],
        },
        "source_materials": [{
            "source_handle": "P0100", "paper_id": "paper-0100", "doi": "10.0000/0100",
            "title": "Task source", "study_summary_A": {"finding": "A"},
        }, {
            "source_handle": "P0101", "paper_id": "paper-0101", "doi": "10.0000/0101",
            "title": "Tool source", "study_summary_A": {"finding": "B"},
        }],
        "tool_materials": [{
            "unit_key": "CH03:CH03_U01",
            "sources": [
                {"source_handle": "P0101", "paper_id": "paper-0101", "doi": "10.0000/0101", "title": "Tool source"},
                {"paper_id": "paper-0999", "doi": "10.0000/0999", "title": "Outside source"},
            ],
            "usable_content": "Tool evidence",
        }],
    }
    packet_path = tmp_path / "writer_packets" / "CH03.json"
    packet_path.parent.mkdir()
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    from optomind_research.runtime.upgrade3.chapter_arrangement import build_chapter_view, build_source_catalog

    view = build_chapter_view(packet_path)
    catalog = build_source_catalog(view)
    assert "P0101" in catalog
    assert "P0999" not in catalog

    arrangement_dir = tmp_path / "arranged"
    arrangement_dir.mkdir()
    arrangement = {
        "chapter_id": "CH03",
        "chapter_argument": "Synthetic",
        "chapter_tool_materials": view.chapter_tool_materials,
        "source_catalog": catalog,
        "units": [{
            "unit_id": "CH03_U01", "focus": "Synthetic point",
            "paragraph_tasks": [{"paragraph_id": "CH03_U01_P01", "source_uses": [{"source_handle": "P0100"}]}],
            "table_tasks": [],
        }],
    }
    arrangement_path = arrangement_dir / "CHAPTER_ARRANGEMENT.json"
    arrangement_path.write_text(json.dumps(arrangement), encoding="utf-8")
    (arrangement_dir / "ARRANGEMENT_INPUT.json").write_text(json.dumps(view.to_dict(include_material=False)), encoding="utf-8")
    writing_view = build_unit_view(arrangement_path, "CH03_U01")
    assert "P0101" in writing_view.citation_handles()
    assert "P0999" not in writing_view.citation_handles()
    tool_sources = writing_view.chapter_tool_materials[0]["sources"]
    assert tool_sources[1]["paper_id"] == "paper-0999"
    assert writing_view.chapter_tool_materials[0]["unresolved_source_identities"][0]["doi"] == "10.0000/0999"
    payload = unit_payload(writing_view)
    assert payload["chapter_tool_materials"][0]["sources"][1]["paper_id"] == "paper-0999"
    assert payload["chapter_tool_materials"][0]["citation_eligible_source_handles"] == ["P0101"]
