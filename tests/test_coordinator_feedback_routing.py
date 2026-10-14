"""Offline tests for coordinator adjustment routing."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressiveReviewPlanner,
    _route_coordinator_adjustments,
)


CHAPTERS = ["CH1", "CH2", "CH3", "CH4", "CH5"]


def _cross_entries_by_chapter(improvement):
    """Group routed cross entries through the production consumer normalizer."""
    entries = ProgressiveReviewPlanner._improvement_entries(improvement)
    return {
        chapter_id: [entry for entry in entries if entry.get("chapter_id") == chapter_id]
        for chapter_id in CHAPTERS
    }


def _cross_entries(improvement):
    return [
        entry for entry in ProgressiveReviewPlanner._improvement_entries(improvement)
        if entry.get("marker", "").startswith("coord-")
    ]


def _saved_coord5_fixture():
    """Five local-only records shaped like the saved coordinator response.

    The declared primary/reappearance references intentionally yield the real
    5 -> 13 distribution: CH1/CH2/CH3/CH4/CH5 = 3/3/5/1/1.
    """
    return {
        "improvement_notes": ["Keep all changes conditional."],
        "cross_chapter_adjustments": [
            {
                "marker": "coord-1",
                "concept": "TBP boundary",
                "primary_chapter": "CH1",
                "re_appearance_value": "Reappear in CH2 and CH3.",
            },
            {
                "marker": "coord-2",
                "concept": "efficiency loss",
                "primary_chapter": "CH3",
                "re_appearance_value": "Reappear in CH1 and CH2.",
            },
            {
                "marker": "coord-3",
                "concept": "fabrication tolerance",
                "primary_chapter": "CH2",
                "re_appearance_value": "Reappear in CH3.",
            },
            {
                "marker": "coord-4",
                "concept": "coherence limit",
                "primary_chapter": "CH4",
                "re_appearance_value": "Reappear in CH1 and CH3.",
            },
            {
                "marker": "coord-5",
                "concept": "OPM versus MDL",
                "primary_chapter": "CH5",
                "re_appearance_value": "Reappear in CH3 only as context.",
            },
        ],
    }


def test_primary_and_reappearance_chapters_receive_unchanged_copies():
    improvement = {
        "improvement_notes": ["A bounded editorial note."],
        "cross_chapter_adjustments": [
            {
                "marker": "shared",
                "concept": "alignment tolerance",
                "primary_chapter": "CH1",
                "re_appearance_value": "Revisit in CH2 and CH3.",
            }
        ],
    }
    before = copy.deepcopy(improvement)

    routed = _route_coordinator_adjustments(improvement, ["CH1", "CH2", "CH3"])
    grouped = _cross_entries_by_chapter(routed)

    assert improvement == before
    assert routed["improvement_notes"] == before["improvement_notes"]
    original = before["cross_chapter_adjustments"][0]
    for chapter_id in ("CH1", "CH2", "CH3"):
        assert grouped[chapter_id] == [{**original, "chapter_id": chapter_id}]
    assert not grouped["CH4"]


def test_ch1_token_does_not_match_ch10():
    improvement = {
        "cross_chapter_adjustments": [
            {
                "marker": "ch10-only",
                "concept": "A later chapter owns this comparison.",
                "re_appearance_value": "Keep the discussion in CH10.",
            }
        ]
    }

    routed = _route_coordinator_adjustments(improvement, ["CH1", "CH10"])
    entries = ProgressiveReviewPlanner._improvement_entries(routed)

    assert [item for item in entries if item.get("chapter_id") == "CH1"] == []
    assert [item for item in entries if item.get("chapter_id") == "CH10"] == [
        {**improvement["cross_chapter_adjustments"][0], "chapter_id": "CH10"}
    ]


def test_unknown_chapter_reference_stays_in_cross_list_without_guessing():
    improvement = {
        "cross_chapter_adjustments": [
            {"marker": "unknown", "concept": "No chapter identifier is present."}
        ]
    }
    before = copy.deepcopy(improvement)

    routed = _route_coordinator_adjustments(improvement, CHAPTERS)

    assert improvement == before
    assert routed["cross_chapter_adjustments"] == before["cross_chapter_adjustments"]
    assert "chapter_id" not in routed["cross_chapter_adjustments"][0]
    assert _cross_entries_by_chapter(routed) == {chapter_id: [] for chapter_id in CHAPTERS}
    assert ProgressiveReviewPlanner._improvement_entries(routed) == before["cross_chapter_adjustments"]


def test_existing_chapter_feedback_entries_remain_unchanged():
    direct = {
        "marker": "already-scoped",
        "chapter_id": "CH2",
        "concept": "A chapter-specific adjustment",
        "feedback": "Keep this direct owner feedback unchanged.",
    }
    improvement = {
        "chapter_feedback": [direct],
        "cross_chapter_adjustments": [
            {"marker": "unknown", "concept": "No declared chapter."}
        ],
    }
    before = copy.deepcopy(improvement)

    routed = _route_coordinator_adjustments(improvement, CHAPTERS)

    assert improvement == before
    assert routed["chapter_feedback"] == before["chapter_feedback"]
    entries = ProgressiveReviewPlanner._improvement_entries(routed)
    assert [item for item in entries if item.get("marker") == "already-scoped"] == [direct]
    assert routed["cross_chapter_adjustments"] == before["cross_chapter_adjustments"]


def test_saved_coord5_records_reach_five_local_chapter_inputs(tmp_path: Path):
    saved_path = tmp_path / "coord5.json"
    saved = _saved_coord5_fixture()
    saved_path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    raw_before = saved_path.read_text(encoding="utf-8")
    loaded = json.loads(raw_before)
    improvement_before = copy.deepcopy(loaded)

    # Local evidence markers model the five existing chapter inputs without
    # embedding any private absolute paths or production material.
    chapter_inputs = {
        chapter_id: {
            "chapter_id": chapter_id,
            "source_materials": [{"source_handle": f"fixture-{chapter_id}"}],
        }
        for chapter_id in CHAPTERS
    }
    routed = _route_coordinator_adjustments(loaded, list(chapter_inputs))
    grouped = _cross_entries_by_chapter(routed)
    routed_cross = routed["cross_chapter_adjustments"]

    assert loaded == improvement_before
    assert saved_path.read_text(encoding="utf-8") == raw_before
    assert len(saved["cross_chapter_adjustments"]) == 5
    assert len(routed_cross) == 13
    assert {chapter_id: len(grouped[chapter_id]) for chapter_id in CHAPTERS} == {
        "CH1": 3,
        "CH2": 3,
        "CH3": 5,
        "CH4": 1,
        "CH5": 1,
    }
    original_by_marker = {
        item["marker"]: item for item in saved["cross_chapter_adjustments"]
    }
    for entry in routed_cross:
        assert entry["chapter_id"] in chapter_inputs
        assert {
            key: value for key, value in entry.items() if key != "chapter_id"
        } == original_by_marker[entry["marker"]]
    for chapter_id, chapter_input in chapter_inputs.items():
        assert chapter_input["source_materials"]
        assert grouped[chapter_id]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))
