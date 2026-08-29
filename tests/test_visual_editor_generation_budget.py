"""Generation portfolio budget tests for the Visual Editor submission tool."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from PIL import Image

from optomind_research.runtime.visual_editor_tool_provider import (
    VisualEditorContext,
    VisualEditorToolProvider,
    validate_visual_editorial_plan_file,
)

_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp-visual-budget-tests"


@pytest.fixture()
def work_dir() -> Path:
    _TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEMP_ROOT / f"run-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 200), color="navy").save(path)
    return path


def _blueprint() -> dict:
    return {
        "review_thesis": "Article-level visual portfolio.",
        "sections": [
            {
                "section_id": f"S{index:02d}",
                "title": (
                    "Optical resonance mechanism"
                    if index == 1
                    else f"Section {index}"
                ),
                "argument_role": (
                    "Explain the optical resonance mechanism."
                    if index == 1
                    else f"Explain section {index}."
                ),
                "visual_argument_slots": [
                    {"purpose": f"Visual need for section {index}"}
                ],
            }
            for index in range(1, 9)
        ],
    }


def _provider(work_dir: Path, index: int) -> VisualEditorToolProvider:
    image_path = _image(work_dir / f"source-{index}.png")
    kb_path = work_dir / f"legacy-{index}.sqlite"
    conn = sqlite3.connect(str(kb_path))
    try:
        conn.execute(
            """
            CREATE TABLE visual_chunks(
              chunk_id TEXT PRIMARY KEY,
              paper_id TEXT,
              doi TEXT,
              title TEXT,
              caption TEXT,
              chunk_kind TEXT,
              search_text TEXT,
              visual_argument_type TEXT,
              visual_argument_status TEXT,
              visual_argument_confidence TEXT,
              visual_argument_claim TEXT,
              visual_argument_needs_human_review INTEGER,
              visual_argument_schema_version TEXT,
              local_image_path TEXT,
              raw_json TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO visual_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-vis-1",
                "legacy-paper",
                "10.1/legacy",
                "Legacy Paper",
                "Optical resonance mechanism with field confinement.",
                "single_figure",
                "optical resonance field confinement mechanism",
                "mechanism_anchor",
                "pending_multimodal_review",
                "high",
                "",
                1,
                "visual_argument_protocol.v1",
                str(image_path),
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return VisualEditorToolProvider(
        VisualEditorContext(
            blueprint=_blueprint(),
            review_work_dir=work_dir / f"review-{index}",
            work_dir=work_dir / f"editor-{index}",
            kb_sqlite_paths=[kb_path],
            input_fingerprint="budget-test-fingerprint",
        )
    )


def _request(
    section_id: str,
    kind: str,
    priority: str = "medium",
) -> dict:
    return {
        "section_id": section_id,
        "figure_kind": kind,
        "argumentative_purpose": (
            f"Clarify the {kind.replace('_', ' ')} reasoning for "
            f"{section_id}."
        ),
        "generation_brief": (
            f"Draw a clean explanatory {kind.replace('_', ' ')} showing "
            "only conceptual relationships and no quantitative data."
        ),
        "placement_guidance": "end_of_section",
        "data_provenance_level": "schematic",
        "input_data": {},
        "approximate_data_allowed": True,
        "priority": priority,
    }


def _submit(
    provider: VisualEditorToolProvider,
    work_dir: Path,
    *,
    placements: list[dict],
    requests: list[dict],
) -> dict:
    tool = next(
        tool
        for tool in provider.get_tools(work_dir)
        if tool.name == "submit_visual_editorial_plan"
    )
    return json.loads(
        tool._func(
            json.dumps(
                {
                    "placements": placements,
                    "conceptual_figure_requests": requests,
                    "unfilled_visual_needs": [],
                }
            )
        )
    )


def _six_requests() -> list[dict]:
    return [
        _request("S01", "mechanism_schematic", "high"),
        _request("S02", "workflow_schematic", "high"),
        _request("S03", "concept_map", "medium"),
        _request("S04", "taxonomy_diagram", "medium"),
        _request("S05", "comparison_diagram", "low"),
        _request("S06", "trend_schematic", "low"),
    ]


def test_six_requests_trim_to_four(work_dir: Path) -> None:
    provider = _provider(work_dir, 1)
    result = _submit(
        provider,
        work_dir / "editor-1",
        placements=[],
        requests=_six_requests(),
    )
    assert result["status"] == "ok"
    assert result["conceptual_request_count"] == 4
    plan = json.loads(provider.plan_path.read_text(encoding="utf-8"))
    assert [
        request["section_id"]
        for request in plan["conceptual_figure_requests"]
    ] == ["S01", "S02", "S03", "S04"]
    dropped = [
        item
        for item in plan["unfilled_visual_needs"]
        if item["reason"]
        == "generation_portfolio_budget_or_low_incremental_value"
    ]
    assert [item["section_id"] for item in dropped] == ["S05", "S06"]
    validation = validate_visual_editorial_plan_file(
        provider.plan_path,
        provider.ctx.input_fingerprint,
        provider._expected_visual_section_ids(),
    )
    assert validation.startswith("VALIDATION_PASSED")


def test_zero_to_four_requests_accepted_unchanged(work_dir: Path) -> None:
    kinds = [
        "mechanism_schematic",
        "workflow_schematic",
        "concept_map",
        "taxonomy_diagram",
    ]
    for count in range(5):
        provider = _provider(work_dir, count + 10)
        requests = [
            _request(f"S{index + 1:02d}", kinds[index])
            for index in range(count)
        ]
        result = _submit(
            provider,
            work_dir / f"editor-{count + 10}",
            placements=[],
            requests=requests,
        )
        assert result["status"] == "ok"
        assert result["conceptual_request_count"] == count
        plan = json.loads(provider.plan_path.read_text(encoding="utf-8"))
        assert [
            request["section_id"]
            for request in plan["conceptual_figure_requests"]
        ] == [request["section_id"] for request in requests]


def test_at_most_one_generated_request_per_section(work_dir: Path) -> None:
    provider = _provider(work_dir, 20)
    requests = [
        _request("S01", "mechanism_schematic", "high"),
        _request("S01", "workflow_schematic", "medium"),
        _request("S02", "concept_map", "medium"),
    ]
    result = _submit(
        provider,
        work_dir / "editor-20",
        placements=[],
        requests=requests,
    )
    assert result["status"] == "ok"
    assert result["conceptual_request_count"] == 2
    plan = json.loads(provider.plan_path.read_text(encoding="utf-8"))
    by_section = {
        request["section_id"]: request["figure_kind"]
        for request in plan["conceptual_figure_requests"]
    }
    assert by_section["S01"] == "mechanism_schematic"
    assert by_section["S02"] == "concept_map"
    dropped = [
        item
        for item in plan["unfilled_visual_needs"]
        if item["reason"]
        == "generation_portfolio_budget_or_low_incremental_value"
    ]
    assert any(
        item["section_id"] == "S01"
        and item["figure_kind"] == "workflow_schematic"
        for item in dropped
    )


def test_source_placements_do_not_count_against_generation_budget(
    work_dir: Path,
) -> None:
    provider = _provider(work_dir, 30)
    provider._verified_candidates_for_section("S01", top_k=4)
    placements = [
        {
            "section_id": "S01",
            "visual_chunk_id": "legacy-vis-1",
            "argumentative_purpose": (
                "Show the optical resonance mechanism from a traceable "
                "source figure."
            ),
            "placement_guidance": "S01",
        }
    ]
    requests = _six_requests()
    result = _submit(
        provider,
        work_dir / "editor-30",
        placements=placements,
        requests=requests,
    )
    assert result["status"] == "ok"
    assert result["placement_count"] == 1
    assert result["conceptual_request_count"] == 4
    plan = json.loads(provider.plan_path.read_text(encoding="utf-8"))
    assert len(plan["placements"]) == 1
    assert len(plan["conceptual_figure_requests"]) == 4


def test_submission_ordering_is_deterministic(work_dir: Path) -> None:
    first = _provider(work_dir, 40)
    first_result = _submit(
        first,
        work_dir / "editor-40",
        placements=[],
        requests=_six_requests(),
    )
    first_plan = json.loads(first.plan_path.read_text(encoding="utf-8"))
    second = _provider(work_dir, 41)
    second_result = _submit(
        second,
        work_dir / "editor-41",
        placements=[],
        requests=_six_requests(),
    )
    second_plan = json.loads(second.plan_path.read_text(encoding="utf-8"))
    assert first_result == second_result
    assert first_plan == second_plan
