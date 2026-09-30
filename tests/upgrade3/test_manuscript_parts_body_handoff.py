"""Offline BODY preservation checks, using newly authored synthetic fixtures.

These test information flow, not model quality. Publication headings never
classify a deep task as a lightweight manuscript responsibility.
"""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from optomind_research.runtime.upgrade3.chapter_arrangement import (
    build_chapter_view, build_source_catalog, validate_arrangement, write_view,
)
from optomind_research.runtime.upgrade3.review_unit_writer import (
    build_unit_view, unit_payload,
)
from optomind_research.runtime.upgrade3.manuscript_parts import (
    boundary_projection, validate_body_tasks, validate_manuscript_parts_plan,
)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("BODY handoff tests must not use network or paid models")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def _parts(mode="standalone"):
    def part(purpose, focus, boundary, anchor):
        return {
            "purpose": purpose, "focus": [focus], "boundary": [boundary],
            "placement": {"mode": "standalone", "anchor": anchor},
            "finalize_from": ["actual BODY", "final review_argument", "shared material pool"],
        }
    result = {
        "context": "Synthetic tutorial for readers familiar with probability but not inverse problems.",
        "abstract": part("Explain the tutorial's useful conditional result", "Identifiability governs confidence",
                         "Do not reproduce the derivation", "article abstract"),
        "introduction": part("Establish why uncertainty needs a separate treatment", "ENTRY_ONLY_MARKER",
                             "Motivate the question; CH01 develops the mathematical derivation", "reader entrance"),
        "conclusion": part("Calibrate what the comparison establishes", "Separate justified extrapolation from open questions",
                           "CH01 owns the substantive methodological comparison", "CH01 final synthesis"),
    }
    result["conclusion"]["placement"]["mode"] = mode
    return result


def _packet(title):
    return {
        "research_question": "When does a sparse observation support an inverse estimate?",
        "review_argument": "The answer depends on identifiability and noise assumptions.",
        "chapter": {"chapter_id": "CH01", "title": title,
                    "purpose": "Derive and compare uncertainty estimates",
                    "scope": "A substantive tutorial and cross-method evidence comparison"},
        "chapter_plan": {
            "thesis": "Posterior concentration does not alone establish identifiability.",
            "units": [{
                "substantive_point": "Derive the conditional posterior and test the limiting assumptions",
                "evidence_conditions_and_limits": "Synthetic fixture: independent Gaussian noise; sigma > 0.",
                "cross_paper_synthesis_and_conflicts": "Two estimators disagree when the forward map is non-injective.",
                "transition": "The remaining evidence gap motivates the next validation task.",
                "paragraph_briefs": [
                    {"point": "Derive the posterior",
                     "development": "p(theta|y) is proportional to p(y|theta)p(theta); derive normalization before interpreting width.",
                     "source_handles": ["P0001"]},
                    {"point": "Compare identifiable and non-identifiable regimes",
                     "development": "Retain the synthetic comparison at sigma=0.1 and sigma=1.0; neither establishes out-of-domain calibration.",
                     "source_handles": ["P0001", "P0002"]},
                ],
                "supporting_studies": [{"source_handle": "P0003",
                                        "contribution": "Independent synthetic benchmark comparison",
                                        "conditions_limits": "Only the fixed observation operator is compared"}],
            }],
        },
        "source_materials": [
            {"source_handle": handle, "paper_id": f"synthetic-{handle}",
             "doi": f"10.0000/synthetic.{handle.lower()}", "title": f"Synthetic source {handle}",
             "study_summary_A": {"finding": f"A finding {handle}"},
             "review_planning_B": {"planning_summary": f"B usage {handle}"},
             "deep_read_material": {"analysis": f"Deep derivation {handle}"},
             "local_passages": {"text": f"Local passage {handle}"},
             "supplement_material": {"content": f"Supplement condition {handle}"},
             "tool_supplement_materials": [{"need_id": f"need-{handle}", "content": f"Tool result {handle}"}]}
            for handle in ("P0001", "P0002", "P0003")
        ],
        "source_identity_map": {
            handle: {"paper_id": f"synthetic-{handle}"}
            for handle in ("P0001", "P0002", "P0003")
        },
    }


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _handoff(tmp_path, packet):
    packet_path = tmp_path / "WRITER_PACKET.json"
    _write(packet_path, packet)
    view = build_chapter_view(packet_path, shared_outline=[packet["chapter"]],
                              id_map_path=tmp_path / "ID_MAP.json")
    unit = view.units[0]
    arranged = validate_arrangement({
        "chapter_id": "CH01", "chapter_argument": "Should not replace the owner thesis",
        "units": [{
            "unit_id": unit.unit_id,
            "paragraph_tasks": [{
                "paragraph_id": unit.unit_id + "_MERGED",
                "source_briefs": [brief.paragraph_id for brief in unit.paragraph_briefs],
                "point": "Deliberately shortened by synthetic editor",
                "development": "Shortened details must be restored locally",
                "source_uses": [],
            }],
            "table_tasks": [{"table_id": "CH01_T01", "purpose": "Benchmark comparison",
                             "columns": ["operator", "condition"],
                             "row_tasks": [{"content": "Fixed observation operator only",
                                            "source_uses": [{"source_handle": "P0003", "role": "comparison", "use": "retain limitation"}]}]}],
        }],
    }, view, planning_revision=True)
    assert arranged["validation"]["ok"], arranged["validation"]
    arranged["source_catalog"] = build_source_catalog(view, arranged)
    output = tmp_path / "arranged"
    output.mkdir(exist_ok=True)
    write_view(view, output / "ARRANGEMENT_INPUT.json")
    _write(output / "CHAPTER_ARRANGEMENT.json", arranged)
    writing_view = build_unit_view(output / "CHAPTER_ARRANGEMENT.json", unit.unit_id)
    return unit_payload(writing_view), arranged


@pytest.mark.parametrize("title, mode", [
    ("Introduction", "standalone"),
    ("Outlook", "embedded"),
    ("Conclusion and Perspectives", "distributed"),
])
def test_substantive_headings_preserve_body_with_separate_parts(tmp_path, title, mode):
    packet = _packet(title)
    body_before, arranged_before = _handoff(tmp_path, packet)
    parts = validate_manuscript_parts_plan(_parts(mode))
    parts_snapshot = deepcopy(parts)
    validate_body_tasks([packet["chapter"]])
    packet["manuscript_parts_plan"] = parts
    packet["manuscript_parts_boundary"] = boundary_projection(parts)
    body_after, arranged_after = _handoff(tmp_path, packet)

    # Parts neither add a unit nor alter a substantive task because of a title.
    assert body_after == body_before
    assert arranged_after == arranged_before
    assert len(arranged_after["units"]) == 1
    assert parts == parts_snapshot
    assert parts["conclusion"]["placement"]["mode"] == mode
    assert "ENTRY_ONLY_MARKER" not in json.dumps(body_after)
    assert body_after["chapter_frame"]["chapter_title"] == title
    assert body_after["chapter_frame"]["chapter_thesis"] == packet["chapter_plan"]["thesis"]

    expected_briefs = packet["chapter_plan"]["units"][0]["paragraph_briefs"]
    actual_briefs = body_after["paragraph_tasks"][0]["source_brief_details"]
    assert len(actual_briefs) == len(expected_briefs)
    for actual, expected in zip(actual_briefs, expected_briefs):
        for key in ("point", "development", "source_handles"):
            assert actual[key] == expected[key]
    assert body_after["owner_unit_context"]["evidence_conditions"] == packet["chapter_plan"]["units"][0]["evidence_conditions_and_limits"]
    assert body_after["owner_unit_context"]["synthesis"] == packet["chapter_plan"]["units"][0]["cross_paper_synthesis_and_conflicts"]
    assert body_after["table_tasks"][0]["row_tasks"][0]["source_uses"][0]["source_handle"] == "P0003"

    actual_sources = {row["source_handle"]: row for row in body_after["sources"]}
    assert set(actual_sources) == {"P0001", "P0002", "P0003"}
    for expected in packet["source_materials"]:
        actual = actual_sources[expected["source_handle"]]
        for key in ("paper_id", "doi", "study_summary_A", "review_planning_B",
                    "deep_read_material", "local_passages", "supplement_material", "tool_supplement_materials"):
            assert actual[key] == expected[key]


def test_boundary_projection_is_detached_and_does_not_expand_parts():
    parts = validate_manuscript_parts_plan(_parts("embedded"))
    before = deepcopy(parts)
    projection = boundary_projection(parts)
    assert set(projection) == {"introduction", "conclusion"}
    for part in projection.values():
        assert set(part) == {"purpose", "focus", "boundary"}
    projection["introduction"]["focus"].append("A BODY editor must not mutate the planning contract")
    assert parts == before


def test_tracked_23_subsection_body_survives_placement_conflict(tmp_path):
    """Real historical text is preserved by blocking, never by removing BODY."""
    import re
    from optomind_research.runtime.upgrade3.manuscript_front_back import (
        apply_front_back, run_front_back_stage, FrontBackError,
    )

    historical = (Path(__file__).resolve().parents[2] / "advisor/20260930/delivery/"
                  "real_manuscript/REVIEW_DRAFT_HANDLES.md")
    original_bytes = historical.read_bytes()
    original = original_bytes.decode("utf-8")
    chapters = re.findall(r"^## 第\d+章 .+$", original, re.MULTILINE)
    subsections = re.findall(r"^### .+$", original, re.MULTILINE)
    handles = re.findall(r"\[P\d{4}\]", original)
    assert len(chapters) == 6
    assert len(subsections) == 23
    assert len(set(handles)) == 178
    context = {"manuscript_parts_plan": _parts("standalone")}
    roles = [{"chapter_id": "CH01", "title": chapters[0][3:], "role": "introduction"}]
    for _ in range(2):
        report = run_front_back_stage(draft_path=historical, research_question="fixture", chapter_roles=roles,
            planning_context=context, out_dir=tmp_path / "out", parts_fixture_path=tmp_path / "must-not-read.json")
        assert report["status"] == "placement_conflict"
        assert report["generated"] == [] and report["application_log"] == []
        assert report["final_manuscript"] == ""
        assert historical.read_bytes() == original_bytes
    assert not (tmp_path / "out/messages").exists()
    assert not (tmp_path / "out/MANUSCRIPT_FINAL.md").exists()
    with pytest.raises(FrontBackError, match="placement_conflict"):
        apply_front_back(original, {"introduction": "Manual entrance"}, roles, planning_context=context)
    assert historical.read_bytes() == original_bytes
