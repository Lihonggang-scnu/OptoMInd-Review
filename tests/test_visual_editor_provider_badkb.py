"""Backend-fix ticket 2.2: structural KB errors must be logged, not swallowed.

Covers delivery requirement (a): handing the visual editor a sqlite file
without a visual table is recorded loudly (ERROR) instead of silently
yielding zero candidates; legitimately absent index files stay WARNING.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from optomind_research.runtime.visual_editor_tool_provider import (
    VisualEditorContext,
    VisualEditorToolProvider,
)


def _make_provider(tmp_path: Path, kb_paths: list[Path]):
    ctx = VisualEditorContext(
        blueprint={"sections": []},
        review_work_dir=tmp_path / "review",
        work_dir=tmp_path / "visual_editor",
        kb_sqlite_paths=kb_paths,
    )
    return VisualEditorToolProvider(ctx)


def test_plain_sqlite_without_visual_tables_logs_error(
    tmp_path: Path, caplog
) -> None:
    """(a) A non-visual library is reported at ERROR, never silent."""

    bad_kb = tmp_path / "topic_scoped_kb" / "review_knowledge_base.s2.sqlite"
    bad_kb.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(bad_kb))
    try:
        conn.execute("CREATE TABLE text_chunks (chunk_id TEXT)")
        conn.commit()
    finally:
        conn.close()

    # Reset the module-level once-per-path guard so tests stay independent.
    from optomind_research.runtime import visual_editor_tool_provider as mod

    mod._REPORTED_BAD_KB_PATHS.clear()

    with caplog.at_level(logging.ERROR, logger="optomind_research.runtime.visual_editor_tool_provider"):
        provider = _make_provider(tmp_path, [bad_kb])

    assert provider._visuals == []
    error_records = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and "not a visual library" in record.getMessage()
    ]
    assert error_records, "structural KB mismatch must be logged at ERROR"
    assert str(bad_kb) in error_records[0].getMessage()


def test_good_visual_library_still_loads(tmp_path: Path) -> None:
    """A library with the visual_chunks table keeps working (no false alarm)."""

    from optomind_research.runtime.supplemental_visual_ingest import (
        _ensure_schema,
    )

    good_kb = tmp_path / "s2_kb" / "review_knowledge_base.s2.sqlite"
    good_kb.parent.mkdir(parents=True, exist_ok=True)
    image = tmp_path / "visual_candidates" / "p1" / "fig.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"png")
    conn = sqlite3.connect(str(good_kb))
    try:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO visual_chunks(chunk_id,paper_id,doi,title,"
            "chunk_kind,parent_asset_id,parent_label,subfigure_label,"
            "visual_role,review_utility,local_image_path,caption,"
            "search_text,raw_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "v1",
                "P1",
                "",
                "T",
                "single_figure",
                "v1",
                "Figure 1",
                "",
                "",
                "",
                str(image),
                "caption",
                "text",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    provider = _make_provider(tmp_path, [good_kb])

    assert len(provider._visuals) == 1
    assert provider._visuals[0]["chunk_id"] == "v1"
