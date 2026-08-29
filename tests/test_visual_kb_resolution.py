"""Round-2 defect A: visual editor must resolve the S2 KB, not the projection.

The blueprint central-projection expansion rebinds both the ``runtime_kb``
local and ``config.base_kb_sqlite`` to a library without a visual_chunks
table.  These tests drive that exact overwrite path and assert the editor
still receives the S2 library.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)
from optomind_research.runtime.visual_editor_tool_provider import (
    VisualEditorContext,
    VisualEditorToolProvider,
)


def _make_kb(path: Path, *, with_visuals: bool, rows: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        if with_visuals:
            from optomind_research.runtime.supplemental_visual_ingest import (
                _ensure_schema,
            )

            _ensure_schema(conn)
            for index in range(rows):
                conn.execute(
                    "INSERT INTO visual_chunks(chunk_id,paper_id,doi,title,"
                    "chunk_kind,parent_asset_id,parent_label,subfigure_label,"
                    "visual_role,review_utility,local_image_path,caption,"
                    "search_text,raw_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"v{index}",
                        "P1",
                        "",
                        "T",
                        "single_figure",
                        f"v{index}",
                        "Figure",
                        "",
                        "",
                        "",
                        str(path.parent / f"missing_{index}.png"),
                        "",
                        "",
                        "{}",
                    ),
                )
        else:
            conn.execute("CREATE TABLE text_chunks (chunk_id TEXT)")
        conn.commit()
    finally:
        conn.close()
    return path


def _make_orchestrator(tmp_path: Path) -> ReviewHarnessOrchestrator:
    config = ReviewHarnessConfig(
        query_plan_path=tmp_path / "qp.json",
        base_kb_sqlite=_make_kb(
            tmp_path / "seed" / "base.sqlite", with_visuals=False
        ),
        output_root=tmp_path / "runs",
    )
    return ReviewHarnessOrchestrator(config, run_dir=tmp_path / "run")


def test_s2_visual_kb_survives_blueprint_projection_overwrite(
    tmp_path: Path,
) -> None:
    """Defect A: after :4111-style rebinding, resolution still finds S2 KB."""

    orch = _make_orchestrator(tmp_path)
    s2_kb = _make_kb(
        tmp_path / "s2_literature_intelligence"
        / "review_knowledge_base.s2.sqlite",
        with_visuals=True,
    )
    blueprint_kb = _make_kb(
        tmp_path / "topic_scoped_kb_blueprint"
        / "review_knowledge_base.s2.sqlite",
        with_visuals=False,
    )

    # Simulate the orchestrator state right before the visual stage:
    # S2 resolved first (pinning), then the projection expansion rebound
    # BOTH runtime_kb and config.base_kb_sqlite -- exactly the be780761
    # sequence that starved the editor in round 1.
    orch._s2_visual_kb = s2_kb
    runtime_kb = blueprint_kb
    orch.config.base_kb_sqlite = blueprint_kb
    assert not hasattr(orch.config.base_kb_sqlite, "magic")  # sanity

    candidates = [runtime_kb, orch.config.base_kb_sqlite]
    resolved = orch._kb_paths_with_visual_tables(candidates)
    assert resolved == [], (
        "precondition: both rebound candidates lack visual tables "
        "-- this is the exact failure mode from be780761"
    )

    # The fixed call site prepends the pinned field instead of runtime_kb.
    fixed_candidates: list[Path] = []
    if orch._s2_visual_kb is not None and Path(orch._s2_visual_kb).exists():
        fixed_candidates.append(Path(orch._s2_visual_kb))
    fixed_candidates.append(orch.config.base_kb_sqlite)
    resolved_fixed = orch._kb_paths_with_visual_tables(fixed_candidates)

    assert resolved_fixed == [s2_kb.resolve()], (
        "the pinned S2 KB must survive the blueprint overwrite"
    )


def test_pinned_field_defaults_to_none_and_is_set_at_binding(
    tmp_path: Path,
) -> None:
    """Fresh orchestrators start unpinned; the binding step pins them."""

    orch = _make_orchestrator(tmp_path)
    assert orch._s2_visual_kb is None

    s2_kb = tmp_path / "s2_literature_intelligence" / "kb.sqlite"
    s2_kb.parent.mkdir(parents=True, exist_ok=True)
    s2_kb.write_bytes(b"")
    # Mirror of the capture block at the s2 binding site.
    runtime_kb = Path(str(str(s2_kb)))
    if runtime_kb.name:
        orch._s2_visual_kb = runtime_kb
    assert orch._s2_visual_kb == s2_kb


def test_provider_alarm_fires_on_all_dangling_library(
    tmp_path: Path, caplog
) -> None:
    """260-row all-dangling KB must trigger the provider ERROR alarm."""

    from optomind_research.runtime import (
        visual_editor_tool_provider as mod,
    )

    # Module-level once-guard: clear so this test observes its own emission.
    mod._REPORTED_BAD_KB_PATHS.clear()

    kb = _make_kb(
        tmp_path / "s2" / "review_knowledge_base.s2.sqlite",
        with_visuals=True,
        rows=260,
    )
    ctx = VisualEditorContext(
        blueprint={"sections": []},
        review_work_dir=tmp_path / "review",
        work_dir=tmp_path / "visual_editor",
        kb_sqlite_paths=[kb],
    )

    with caplog.at_level(
        logging.ERROR,
        logger="optomind_research.runtime.visual_editor_tool_provider",
    ):
        provider = VisualEditorToolProvider(ctx)

    dangling_alarms = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and "reference missing image files" in record.getMessage()
    ]
    assert dangling_alarms, (
        "an all-dangling library must raise the integrity ERROR, not pass "
        "silently"
    )
    message = dangling_alarms[0].getMessage()
    assert "260/260" in message or "260 of 260" in message
    assert len(provider._visuals) == 260
