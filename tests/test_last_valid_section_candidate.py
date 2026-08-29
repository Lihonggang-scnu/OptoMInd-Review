from __future__ import annotations

import json
from pathlib import Path

from optomind_research.runtime.section_authoring_tool_registry import (
    _has_durable_section_candidate,
    _persist_last_valid_section_candidate,
    _restore_last_valid_section_candidate,
)
from optomind_research.runtime.tool_provider import SectionAuthoringContext


def _context(work_dir: Path) -> SectionAuthoringContext:
    return SectionAuthoringContext(
        section_id="S04",
        section_data={
            "title": "A test section",
            "chapter_argument": "A bounded test argument.",
        },
        kb_sqlite=None,
        temp_kb_sqlite=None,
        work_dir=work_dir,
    )


def _write_candidate(work_dir: Path, text: str) -> None:
    (work_dir / "SECTION_DRAFT_EN.md").write_text(text, encoding="utf-8")
    for name, payload in {
        "SECTION_ARGUMENT_PLAN.json": {"paragraphs": []},
        "SECTION_EVIDENCE_PACKET.json": {"items": []},
        "SECTION_CITATION_MAP.json": {"papers_cited": []},
    }.items():
        (work_dir / name).write_text(
            json.dumps(payload), encoding="utf-8"
        )


def test_last_valid_candidate_survives_runtime_cleanup(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    first = "First durable sentence. " * 30
    _write_candidate(tmp_path, first)
    saved = _persist_last_valid_section_candidate(
        ctx, summary="first", validation_level="syntax"
    )
    assert saved["saved"] is True
    assert saved["candidate_index"] == 0

    for name in (
        "SECTION_DRAFT_EN.md",
        "SECTION_ARGUMENT_PLAN.json",
        "SECTION_EVIDENCE_PACKET.json",
        "SECTION_CITATION_MAP.json",
    ):
        (tmp_path / name).unlink()
    assert _restore_last_valid_section_candidate(tmp_path) is True
    assert (tmp_path / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8") == first
    assert _has_durable_section_candidate(tmp_path) is True


def test_candidate_index_is_monotonic_and_invalid_draft_is_not_saved(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    _write_candidate(tmp_path, "Valid sentence. " * 30)
    assert _persist_last_valid_section_candidate(ctx)["candidate_index"] == 0
    _write_candidate(tmp_path, "Second valid sentence. " * 30)
    assert _persist_last_valid_section_candidate(ctx)["candidate_index"] == 1
    _write_candidate(tmp_path, "too short")
    rejected = _persist_last_valid_section_candidate(ctx)
    assert rejected == {"saved": False, "reason": "draft_not_minimally_valid"}
