"""Backend-fix ticket 2.3/2.4: parallel commit must not orphan image paths.

Covers delivery requirement (b): after ``_commit_paper_outcome`` the shared
KB rows point at files that actually exist, because path rewriting and file
relocation happen inside the same commit pass.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from optomind_research.runtime.supplemental_visual_ingest import _ensure_schema
from optomind_research.s2_fulltext_acquisition import (
    S2FulltextAcquirer,
    S2FulltextAcquisitionResult,
)


def _make_shared_kb(tmp_path: Path) -> Path:
    shared = tmp_path / "shared" / "review_knowledge_base.s2.sqlite"
    shared.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(shared))
    try:
        # Same schema family the workers produce; merge would also create it.
        _ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return shared


def _make_staging_artifacts(tmp_path: Path) -> tuple[Path, Path, str]:
    worker_root = tmp_path / "s2_fulltext_workers_ab12cd34" / "paper_p1"
    visual_dir = worker_root / "visual_candidates"
    download_dir = worker_root / "downloads"
    image_path = visual_dir / "paper_p1" / "pymupdf" / "page3" / "fig1.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG fake image bytes")
    pdf_path = download_dir / "some-doi-slug.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-fake")

    staging_kb = worker_root / "kb.sqlite"
    conn = sqlite3.connect(str(staging_kb))
    try:
        _ensure_schema(conn)
        canonical_raw = {
            "chunk_id": "v1",
            "local_image_path": str(image_path),
            "source_path": str(pdf_path),
        }
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
                str(image_path),
                "caption",
                "search text",
                json.dumps(canonical_raw),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return staging_kb, worker_root, image_path.name


def test_committed_visual_paths_exist_after_commit(tmp_path: Path) -> None:
    """(b) Rows and relocated files agree inside one commit."""

    shared_kb = _make_shared_kb(tmp_path)
    staging_kb, worker_root, _name = _make_staging_artifacts(tmp_path)
    acquirer = S2FulltextAcquirer(
        kb_sqlite=shared_kb,
        download_dir=tmp_path / "downloads",
    )
    payload = {
        "paper_id": "P1",
        "title": "T",
        "doi": "",
        "status": "oa_fulltext_success",
        "success_wave": "arxiv_title",
        "route_count": 1,
        "new_chunk_ids": [],
        "reused_chunk_ids": ["tc-1"],
        "new_paper_ids": ["P1"],
        "materialized_chunk_count": 2,
        "abstract_enriched_source": "",
        "visual_report": {},
        "visual_candidate_count": 1,
        "waves": [],
        "resolver_waves": 0,
        "ingest_stats": {},
        "temp_paths": {
            "kb": str(staging_kb),
            "download_dir": str(worker_root / "downloads"),
            "visual_dir": str(worker_root / "visual_candidates"),
        },
    }
    aggregate_stats = acquirer._empty_aggregate_stats()
    result = S2FulltextAcquisitionResult()

    outcome, materialized = acquirer._commit_paper_outcome(
        payload,
        aggregate_stats=aggregate_stats,
        result=result,
        commit_files=True,
        temp_paths=payload["temp_paths"],
    )

    assert materialized is True
    integrity = outcome["visual_path_integrity"]
    assert integrity["checked"] == 1
    assert integrity["missing"] == 0
    assert integrity["missing_ratio"] == 0.0

    conn = sqlite3.connect(str(shared_kb))
    try:
        row = conn.execute(
            "SELECT local_image_path, raw_json FROM visual_chunks "
            "WHERE chunk_id=?",
            ("v1",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "visual row must be merged into the shared KB"
    committed_path = Path(row[0])
    expected_dir = tmp_path / "visual_candidates"
    assert committed_path == (
        expected_dir / "paper_p1" / "pymupdf" / "page3" / "fig1.png"
    )
    assert committed_path.is_file(), "committed path must point at a file"
    embedded = json.loads(row[1])
    embedded_source = Path(str(embedded["source_path"]))
    assert embedded_source.is_file(), "raw_json source_path must survive"
    assert not embedded_source.is_relative_to(worker_root)


def test_verify_method_reports_missing_ratio(tmp_path: Path) -> None:
    """2.4: the verifier exposes dangling ratios instead of hiding them."""

    shared_kb = tmp_path / "kb.sqlite"
    shared_kb.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(shared_kb))
    try:
        _ensure_schema(conn)
        for index in range(5):
            conn.execute(
                "INSERT INTO visual_chunks(chunk_id,paper_id,doi,title,"
                "chunk_kind,parent_asset_id,parent_label,subfigure_label,"
                "visual_role,review_utility,local_image_path,caption,"
                "search_text,raw_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"v{index}",
                    "PX",
                    "",
                    "T",
                    "single_figure",
                    f"v{index}",
                    "Figure",
                    "",
                    "",
                    "",
                    str(tmp_path / f"nowhere_{index}.png"),
                    "",
                    "",
                    "{}",
                ),
            )
        conn.commit()
    finally:
        conn.close()

    acquirer = S2FulltextAcquirer(
        kb_sqlite=shared_kb,
        download_dir=tmp_path / "downloads",
    )
    report = acquirer._verify_committed_visual_paths("PX")

    assert report["checked"] == 5
    assert report["missing"] == 5
    assert report["missing_ratio"] == 1.0


def test_verify_counts_rows_left_inside_the_staging_tree(
    tmp_path: Path,
) -> None:
    """2.4: an is_file() check alone cannot see the real failure mode.

    A row that leaked a worker-staging path resolves during the commit pass
    -- the temp root is still on disk -- and only starts dangling once
    _acquire_parallel removes it.  The be780761 run reported missing_ratio
    0.0 for all six papers while committing 260 doomed paths, so the
    verifier has to count the doomed-tree case separately.
    """

    shared_kb = tmp_path / "kb.sqlite"
    staging_root = tmp_path / "s2_fulltext_workers_ab12cd34" / "paper_px"
    leaked = staging_root / "visual_candidates" / "px" / "fig1.png"
    leaked.parent.mkdir(parents=True, exist_ok=True)
    leaked.write_bytes(b"\x89PNG still here, for now")

    conn = sqlite3.connect(str(shared_kb))
    try:
        _ensure_schema(conn)
        for index in range(4):
            conn.execute(
                "INSERT INTO visual_chunks(chunk_id,paper_id,doi,title,"
                "chunk_kind,parent_asset_id,parent_label,subfigure_label,"
                "visual_role,review_utility,local_image_path,caption,"
                "search_text,raw_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"v{index}",
                    "PX",
                    "",
                    "T",
                    "single_figure",
                    f"v{index}",
                    "Figure",
                    "",
                    "",
                    "",
                    str(leaked),
                    "",
                    "",
                    "{}",
                ),
            )
        conn.commit()
    finally:
        conn.close()

    acquirer = S2FulltextAcquirer(
        kb_sqlite=shared_kb,
        download_dir=tmp_path / "downloads",
    )
    report = acquirer._verify_committed_visual_paths(
        "PX", staging_root=staging_root
    )

    # Every path resolves right now, which is exactly why `missing` stays 0.
    assert report["checked"] == 4
    assert report["missing"] == 0
    assert report["staged"] == 4
    assert report["unresolvable_ratio"] == 1.0


def test_verify_ignores_staging_root_for_durable_paths(
    tmp_path: Path,
) -> None:
    """A correctly committed path must not be mistaken for a leak."""

    shared_kb = tmp_path / "kb.sqlite"
    staging_root = tmp_path / "s2_fulltext_workers_ab12cd34" / "paper_px"
    staging_root.mkdir(parents=True, exist_ok=True)
    durable = tmp_path / "visual_candidates" / "px" / "fig1.png"
    durable.parent.mkdir(parents=True, exist_ok=True)
    durable.write_bytes(b"\x89PNG committed for good")

    conn = sqlite3.connect(str(shared_kb))
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
                "PX",
                "",
                "T",
                "single_figure",
                "v1",
                "Figure",
                "",
                "",
                "",
                str(durable),
                "",
                "",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    acquirer = S2FulltextAcquirer(
        kb_sqlite=shared_kb,
        download_dir=tmp_path / "downloads",
    )
    report = acquirer._verify_committed_visual_paths(
        "PX", staging_root=staging_root
    )

    assert report["checked"] == 1
    assert report["missing"] == 0
    assert report["staged"] == 0
    assert report["unresolvable_ratio"] == 0.0
