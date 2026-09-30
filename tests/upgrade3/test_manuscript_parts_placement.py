"""Publication preflight preserves unowned BODY and never classifies planner tasks."""
import json
import socket

import pytest

from optomind_research.runtime.upgrade3 import manuscript_front_back as fb


def context():
    return {"manuscript_parts_plan": {"context": "Technical tutorial", **{
        name: {"purpose": "Clarify conditional scope", "focus": ["Conditions matter"],
               "boundary": ["Keep derivation in BODY"],
               "placement": {"mode": "standalone", "anchor": "Article boundary"},
               "finalize_from": ["Actual BODY"]}
        for name in fb.STAGE_ORDER}}}


def parts():
    return {"abstract": "Scope and finding", "introduction": "Reader entrance",
            "conclusion": "Conditional synthesis", "keywords": ["method"]}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network forbidden in offline placement tests")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.mark.parametrize("heading,part", [
    ("## Introduction", "introduction"), ("# 引言", "introduction"),
    ("## 绪论", "introduction"), ("## Abstract", "abstract"),
    ("## 摘要", "abstract"), ("## Conclusion", "conclusion"),
    ("## 结语", "conclusion"), ("## 结论", "conclusion"),
    ("## 总结", "conclusion"), ("## Introduction ##", "introduction"),
])
def test_exact_unowned_heading_blocks_without_touching_body(tmp_path, monkeypatch, heading, part):
    body = ("# Review\r\n\r\n" + heading + "\r\nDeep derivation: E=mc².\r\n").encode()
    draft = tmp_path / "body.md"
    draft.write_bytes(body)
    def forbidden(*args, **kwargs):
        pytest.fail("Conflict must stop before generation or application")
    monkeypatch.setattr(fb, "build_stage_messages", forbidden)
    monkeypatch.setattr(fb, "_apply_owned_parts", forbidden)
    report = fb.run_front_back_stage(draft_path=draft, research_question="q", chapter_roles=[],
        out_dir=tmp_path / "out", parts_fixture_path=tmp_path / "must-not-read.json", planning_context=context())
    assert report["status"] == "placement_conflict"
    assert report["conflicts"][0] == {"part": part, "requested_mode": "standalone",
        "existing_location": heading, "reason": "existing_unowned_body_location"}
    assert draft.read_bytes() == body
    assert report["generated"] == [] and report["application_log"] == []
    assert report["final_manuscript"] == ""
    assert report["model_calls"] == report["external_requests"] == 0
    assert not (tmp_path / "out/messages").exists()
    assert not (tmp_path / "out/MANUSCRIPT_FINAL.md").exists()


@pytest.mark.parametrize("part,role,heading,title", [
    ("introduction", "introduction", "## 第1章 Introduction: mathematical tutorial", "Introduction: mathematical tutorial"),
    ("introduction", "opening", "## CH01 Mathematical foundations", ""),
    ("introduction", "introduction", "## 第1章 Introduction: mathematical tutorial", ""),
    ("conclusion", "closing", "## Chapter 1 Final outlook", ""),
    ("conclusion", "closing", "## 第1章 Final outlook", "Final outlook"),
])
def test_explicit_resolved_role_blocks_deep_body(tmp_path, part, role, heading, title):
    original = "# Review\n\n" + heading + "\n\nFull mathematical proof.\n"
    draft = tmp_path / "body.md"
    draft.write_text(original, encoding="utf-8")
    roles = [{"chapter_id": "CH01", "title": title, "role": role}]
    report = fb.run_front_back_stage(draft_path=draft, research_question="q", chapter_roles=roles,
        out_dir=tmp_path / "out", planning_context=context())
    assert report["status"] == "placement_conflict"
    assert report["conflicts"][0]["part"] == part
    assert draft.read_text(encoding="utf-8") == original
    with pytest.raises(fb.FrontBackError, match="placement_conflict"):
        fb.apply_front_back(original, parts(), roles, context())


def test_no_conflict_and_owned_rerun_are_idempotent(tmp_path):
    original = "# Review\n\n## Physical principles\n\nDerivation.\n\n## Methods\n\nComparison.\n"
    draft = tmp_path / "body.md"
    draft.write_text(original)
    fixture = {part: {part: parts()[part]} for part in fb.STAGE_ORDER}
    fixture["abstract"]["keywords"] = ["method"]
    fixture_path = tmp_path / "parts.json"
    fixture_path.write_text(json.dumps(fixture))
    report = fb.run_front_back_stage(draft_path=draft, research_question="q", chapter_roles=[],
        out_dir=tmp_path / "out", parts_fixture_path=fixture_path, planning_context=context())
    assert report["status"] == "generated"
    from pathlib import Path
    applied = Path(report["final_manuscript"]).read_text()
    again, _ = fb.apply_front_back(applied, parts(), planning_context=context())
    assert applied == again
    assert "## Physical principles\n\nDerivation.\n\n## Methods\n\nComparison." in applied
    for part in fb.STAGE_ORDER:
        assert applied.count(f"<!-- manuscript-part:{part}:start -->") == 1


@pytest.mark.parametrize("heading", ["## Introduction to Bayesian optimization", "### Introduction",
    "### Concluding remarks on one subsection", "## Concluding remarks on one subsection",
    "```markdown\n## Introduction\n```", "~~~markdown\n## Conclusion\n~~~"])
def test_technical_titles_subsections_and_code_examples_do_not_block(heading):
    original = "# Review\n\n" + heading + "\n\nTechnical BODY."
    applied, _ = fb.apply_front_back(original, parts(), planning_context=context())
    assert heading in applied and "Technical BODY." in applied


def test_unresolved_role_warns_without_guessing_a_location(tmp_path):
    original = "# Review\n\n## Physical principles\n\nDerivation."
    conflicts, warnings = fb._placement_conflicts(original,
        [{"chapter_id": "CH010", "title": "Missing title", "role": "introduction"}], context())
    assert conflicts == []
    assert warnings[0]["status"] == "unresolved_part_role"


def test_ambiguous_technical_heading_warns_without_blocking():
    conflicts, warnings = fb._placement_conflicts(
        "# Review\n\n## Introduction to Bayesian optimization\n\nDerivation.", [], context())
    assert conflicts == []
    assert warnings[0]["status"] == "ambiguous_part_heading"


def test_conflict_never_exposes_stale_final_manuscript(tmp_path):
    draft = tmp_path / "body.md"
    draft.write_text("# Review\n\n## Introduction\n\nKeep all derivation.")
    out = tmp_path / "out"
    out.mkdir()
    stale = out / "MANUSCRIPT_FINAL.md"
    stale.write_bytes(b"Previously generated manuscript, not this run.")
    before = stale.read_bytes()
    report = fb.run_front_back_stage(draft_path=draft, research_question="q", chapter_roles=[],
        out_dir=out, planning_context=context())
    assert report["status"] == "placement_conflict"
    assert report["final_manuscript"] == ""
    assert stale.read_bytes() == before


def test_role_without_location_does_not_guess_from_list_position():
    original = "# Review\n\n## 第1章 Physical principles\n\nDerivation."
    conflicts, warnings = fb._placement_conflicts(original, [{"role": "introduction"}], context())
    assert conflicts == []
    assert warnings[0]["status"] == "unresolved_part_role"
