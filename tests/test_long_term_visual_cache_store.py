from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from PIL import Image

from optomind_research.runtime.visual_cache_ingest import ingest_visual_candidates
from optomind_research.runtime.visual_cache_store import (
    VisualCachePublicationError,
    VisualCacheStore,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def cache_tmp() -> Path:
    """Workspace-local scratch dir; avoids pytest's mode=0o700 tmp ACLs."""

    scratch = PROJECT_ROOT / ".codex-tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    root = scratch / f"visual-cache-test-{uuid.uuid4().hex[:10]}"
    root.mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)
    try:
        scratch.rmdir()
    except OSError:
        pass


def _image(path: Path, color: str = "navy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (360, 240), color=color).save(path)
    return path


def _record(
    *,
    chunk_id: str,
    kind: str,
    subfigure_label: str,
    image_path: Path,
    parent_image_path: Path,
    review_decision: str = "",
    extra: dict | None = None,
) -> dict:
    record = {
        "schema_version": "visual_chunk.v1",
        "chunk_id": chunk_id,
        "chunk_kind": kind,
        "paper_id": "p1",
        "doi": "10.1/visual-store",
        "paper_title": "Visual Store Paper",
        "parent_asset_id": "p1-fig1",
        "parent_label": "Figure 1",
        "subfigure_label": subfigure_label,
        "subpanel_labels": ["a", "b"],
        "local_image_path": str(image_path),
        "parent_image_path": str(parent_image_path),
        "caption": "Figure 1. a) spectrum of emitter b) trend of cooling power.",
        "subfigure_caption_focus": "a) spectrum of emitter",
        "nearby_text": "The emitter is selective in the atmospheric window.",
        "visual_profile": {
            "intrinsic_visual_labels": {"visual_role": "spectrum"}
        },
        "visual_card": {
            "one_sentence_summary": "Emitter spectrum with selective window."
        },
        "visual_argument_type": "trend_or_parameter_map",
        "visual_argument_status": "pending_multimodal_review",
        "visual_argument_needs_human_review": 1,
        "needs_human_review": True,
        "human_review_status": "pending",
        "review_decision": review_decision,
        "use_permission": "contextual_or_qualified_support",
        "allowed_claim_kinds": ["trend"],
    }
    if extra:
        record.update(extra)
    return record


def _ingested(cache_tmp: Path) -> tuple[Path, Path, list[dict], Path]:
    source = cache_tmp / "source" / "papers" / "p1"
    source.mkdir(parents=True)
    parent_image = _image(source / "fig1.png", "red")
    child_image = _image(source / "fig1-a.png", "blue")
    records = [
        _record(
            chunk_id="p1-fig1-parent",
            kind="parent_figure",
            subfigure_label="",
            image_path=parent_image,
            parent_image_path=parent_image,
        ),
        _record(
            chunk_id="p1-fig1-subfig-a",
            kind="subfigure",
            subfigure_label="a",
            image_path=child_image,
            parent_image_path=parent_image,
        ),
    ]
    assets = cache_tmp / "assets"
    units, report = ingest_visual_candidates(
        records,
        source_root=source,
        copy_assets_to=assets,
    )
    assert report["errors"] == []
    return source, assets, units, assets


def test_publish_creates_versioned_snapshot_and_searchable_index(
    cache_tmp: Path,
) -> None:
    source, assets, units, _ = _ingested(cache_tmp)
    store = VisualCacheStore(cache_tmp / "cache")
    report = store.publish_snapshot(
        version="v1",
        units=units,
        assets_dir=assets,
        path_roots={"source": source},
    )
    assert report["status"] == "published"
    assert report["unit_count"] == 2

    snapshot = store.root / "v1"
    assert (snapshot / "units.json").is_file()
    assert (snapshot / "visual_cache.sqlite").is_file()
    assert (snapshot / "manifest.json").is_file()
    assert len(list((snapshot / "assets").iterdir())) == 2
    assert store.latest_version() == "v1"

    payload = store.load_snapshot("v1")
    assert payload["unit_count"] == 2
    child = next(
        unit
        for unit in payload["units"]
        if unit["unit_kind"] == "subfigure"
    )
    parent = next(
        unit
        for unit in payload["units"]
        if unit["unit_kind"] == "parent_figure"
    )
    assert child["lineage"]["parent_unit_id"] == parent["unit_id"]

    verification = store.verify_snapshot("v1", path_roots={"source": source})
    assert verification["status"] == "passed"
    assert verification["unit_count"] == 2

    hits = store.search("v1", "spectrum")
    assert len(hits) == 2
    assert all(
        Path(hit["resolved_paths"]["image_path"]).is_file() for hit in hits
    )
    assert all(
        hit["resolved_paths"]["image_path"].startswith(str(snapshot))
        for hit in hits
    )


def test_publish_fails_closed_on_invalid_unit(cache_tmp: Path) -> None:
    _, assets, units, _ = _ingested(cache_tmp)
    units[0]["caption"] = {"clean": "", "original": ""}
    store = VisualCacheStore(cache_tmp / "cache")
    with pytest.raises(VisualCachePublicationError) as exc:
        store.publish_snapshot(version="v1", units=units, assets_dir=assets)
    assert any("caption.empty" in error for error in exc.value.errors)
    assert not (store.root / "v1").exists()
    assert list(store.root.glob(".staging-*")) == []


def test_publish_fails_closed_when_asset_hash_does_not_match(
    cache_tmp: Path,
) -> None:
    _, assets, units, _ = _ingested(cache_tmp)
    asset_file = next((assets / "assets").iterdir())
    asset_file.write_bytes(b"tampered")
    store = VisualCacheStore(cache_tmp / "cache")
    with pytest.raises(VisualCachePublicationError) as exc:
        store.publish_snapshot(version="v1", units=units, assets_dir=assets)
    assert any("asset_hash_mismatch" in error for error in exc.value.errors)
    assert not (store.root / "v1").exists()
    assert list(store.root.glob(".staging-*")) == []


def test_integrity_check_detects_snapshot_tampering(cache_tmp: Path) -> None:
    source, assets, units, _ = _ingested(cache_tmp)
    store = VisualCacheStore(cache_tmp / "cache")
    store.publish_snapshot(
        version="v1",
        units=units,
        assets_dir=assets,
        path_roots={"source": source},
    )
    assert store.verify_snapshot("v1")["status"] == "passed"

    units_path = store.root / "v1" / "units.json"
    original_units = units_path.read_bytes()
    units_path.write_bytes(original_units + b"\n# tampered")
    verification = store.verify_snapshot("v1")
    assert verification["status"] == "failed"
    assert "hash_mismatch:units.json" in verification["errors"]

    units_path.write_bytes(original_units)
    sqlite_path = store.root / "v1" / "visual_cache.sqlite"
    original_sqlite = sqlite_path.read_bytes()
    sqlite_path.write_bytes(original_sqlite + b"corrupt")
    verification = store.verify_snapshot("v1")
    assert verification["status"] == "failed"
    assert "hash_mismatch:visual_cache.sqlite" in verification["errors"]


def test_snapshot_is_portable_and_rebases_source_paths(cache_tmp: Path) -> None:
    source_a, assets, units, _ = _ingested(cache_tmp)
    cache_a = VisualCacheStore(cache_tmp / "cache-a")
    cache_a.publish_snapshot(
        version="v1",
        units=units,
        assets_dir=assets,
        path_roots={"source": source_a},
    )

    cache_b_root = cache_tmp / "cache-b"
    shutil.copytree(cache_a.root, cache_b_root)
    source_b = cache_tmp / "rebase" / "source" / "papers" / "p1"
    source_b.mkdir(parents=True)
    _image(source_b / "fig1.png", "green")
    _image(source_b / "fig1-a.png", "yellow")

    cache_b = VisualCacheStore(cache_b_root)
    verification = cache_b.verify_snapshot(
        "v1", path_roots={"source": source_b}
    )
    assert verification["status"] == "passed"
    assert verification["warnings"] == []
    hit = cache_b.search("v1", "spectrum", path_roots={"source": source_b})[0]
    assert hit["resolved_paths"]["source_path"].startswith(str(source_b))
    assert hit["resolved_paths"]["image_path"].startswith(str(cache_b_root))
    assert Path(hit["resolved_paths"]["image_path"]).is_file()


def test_sqlite_index_queries_by_caption_role_and_approval(cache_tmp: Path) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    records = [
        _record(
            chunk_id="p1-approved",
            kind="single_figure",
            subfigure_label="",
            image_path=_image(source / "approved.png", "blue"),
            parent_image_path=_image(source / "parent-approved.png", "navy"),
            review_decision="human_approved",
            extra={"visual_argument_status": "ok"},
        ),
        _record(
            chunk_id="p1-pending",
            kind="single_figure",
            subfigure_label="",
            image_path=_image(source / "pending.png", "purple"),
            parent_image_path=_image(source / "parent-pending.png", "pink"),
            review_decision="timeout_accepted_for_draft",
        ),
    ]
    assets = cache_tmp / "assets"
    units, report = ingest_visual_candidates(
        records,
        source_root=source,
        copy_assets_to=assets,
    )
    assert report["errors"] == []
    store = VisualCacheStore(cache_tmp / "cache")
    store.publish_snapshot(
        version="v1",
        units=units,
        assets_dir=assets,
        path_roots={"source": source},
    )

    conn = sqlite3.connect(store.root / "v1" / "visual_cache.sqlite")
    try:
        rows = conn.execute(
            "SELECT approval_state, unit_kind FROM units ORDER BY unit_id"
        ).fetchall()
        assert sorted(row[0] for row in rows) == ["approved", "pending"]
        assert all(row[1] == "single_figure" for row in rows)
        approved = conn.execute(
            "SELECT unit_id FROM units WHERE approval_state='approved'"
        ).fetchall()
        assert len(approved) == 1
        assert approved[0][0].startswith("unit:visual:")
        fts_rows = conn.execute(
            "SELECT unit_id FROM units_fts WHERE units_fts MATCH ?",
            ('"spectrum"',),
        ).fetchall()
        assert len(fts_rows) == 2
    finally:
        conn.close()

    assert len(store.search("v1", "emitter")) == 2


def test_sqlite_index_preserves_crop_hygiene_and_original_image_ref(
    cache_tmp: Path,
) -> None:
    source, assets, units, _ = _ingested(cache_tmp)
    store = VisualCacheStore(cache_tmp / "cache")
    store.publish_snapshot(
        version="v1",
        units=units,
        assets_dir=assets,
        path_roots={"source": source},
    )
    snapshot = store.root / "v1"
    conn = sqlite3.connect(snapshot / "visual_cache.sqlite")
    try:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(units)").fetchall()
        }
        assert {"crop_hygiene_json", "original_image_relpath"}.issubset(
            columns
        )
        rows = conn.execute(
            """
            SELECT unit_id, crop_hygiene_json, image_relpath,
                   original_image_relpath, paths_json
            FROM units
            """
        ).fetchall()
        assert rows
        for unit_id, hygiene_json, image_rel, original_rel, paths_json in rows:
            hygiene = json.loads(hygiene_json)
            assert hygiene["status"] in {
                "clean",
                "derived_clean",
                "needs_review",
                "rejected",
            }
            paths = json.loads(paths_json)
            assert paths["image_ref"]["relative"] == image_rel
            original_ref = paths.get("original_image_ref") or {}
            assert original_rel == (original_ref.get("relative") or "")
    finally:
        conn.close()


def test_publish_rejects_existing_version_without_touching_it(
    cache_tmp: Path,
) -> None:
    source, assets, units, _ = _ingested(cache_tmp)
    store = VisualCacheStore(cache_tmp / "cache")
    first = store.publish_snapshot(
        version="v1",
        units=units,
        assets_dir=assets,
        path_roots={"source": source},
    )
    assert first["status"] == "published"
    with pytest.raises(VisualCachePublicationError) as exc:
        store.publish_snapshot(version="v1", units=units, assets_dir=assets)
    assert "already exists" in str(exc.value)
    assert store.verify_snapshot("v1", path_roots={"source": source})[
        "status"
    ] == "passed"
