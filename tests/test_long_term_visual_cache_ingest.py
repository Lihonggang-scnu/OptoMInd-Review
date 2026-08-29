from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from optomind_research.runtime.visual_cache_ingest import (
    candidates_from_staging_kb,
    ingest_visual_candidates,
)
from optomind_research.runtime.visual_cache_schemas import validate_visual_unit
from optomind_research.runtime.visual_cache_store import VisualCacheStore
from optomind_research.runtime.visual_local_vectors import (
    LocalVisualIndex,
    local_image_feature_vector,
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


def _caption_image(path: Path, *, caption_h: int = 60) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 360, 260
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [
            (40, height - caption_h - 30),
            (120, 60),
            (200, height - caption_h - 60),
            (280, 80),
            (320, height - caption_h - 30),
        ],
        fill=(120, 180, 240),
        outline=(30, 90, 200),
        width=3,
    )
    draw.rectangle(
        [40, height - caption_h - 40, width - 40, height - caption_h - 20],
        fill=(240, 190, 120),
    )
    draw.ellipse([(width - 80, 40), (width - 40, 80)], fill=(40, 160, 80))
    y0 = height - caption_h
    for y in range(y0 + 8, height - 4, 10):
        x = 25
        while x < width - 60:
            draw.rectangle([x, y, x + 8, y + 6], fill=(20, 20, 20))
            x += 30
    image.save(path)
    return path


def _prose_image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 400, 520
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for y in range(20, height - 16, 8):
        x = 25
        while x < width - 60:
            draw.rectangle([x, y, x + 8, y + 6], fill=(30, 30, 30))
            x += 30
    image.save(path)
    return path


def _tiny_band_image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 360, 260
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for x in range(20, width - 20, 12):
        draw.line([(x, 40), (x + 8, 120)], fill=(30, 90, 200), width=3)
    for x in range(25, width - 60, 30):
        draw.rectangle(
            [x, height - 12, x + 8, height - 6],
            fill=(20, 20, 20),
        )
    image.save(path)
    return path


def _multiline_trailing_caption_image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height, trailing = 360, 360, 10
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [
            (40, 250),
            (120, 60),
            (200, 230),
            (280, 80),
            (320, 250),
        ],
        fill=(120, 180, 240),
        outline=(30, 90, 200),
        width=3,
    )
    draw.rectangle(
        [40, 250, width - 40, 270],
        fill=(240, 190, 120),
    )
    for line_top in (
        height - trailing - 20,
        height - trailing - 8,
    ):
        for x in range(25, width - 60, 20):
            draw.rectangle(
                [x, line_top, x + 8, line_top + 8],
                fill=(20, 20, 20),
            )
    image.save(path)
    return path


def _chunk_record(
    *,
    chunk_id: str,
    kind: str,
    subfigure_label: str,
    image_path: Path,
    parent_image_path: Path | None = None,
    review_decision: str = "",
    visual_argument_status: str = "pending_multimodal_review",
    extra: dict | None = None,
) -> dict:
    record = {
        "schema_version": "visual_chunk.v1",
        "chunk_id": chunk_id,
        "chunk_kind": kind,
        "paper_id": "p1",
        "doi": "10.1/visual-test",
        "paper_title": "Visual Cache Test Paper",
        "year": 2024,
        "venue": "Test Journal",
        "parent_asset_id": "p1-fig1",
        "parent_label": "Figure 1",
        "subfigure_label": subfigure_label,
        "subpanel_labels": ["a", "b"],
        "local_image_path": str(image_path),
        "parent_image_path": str(parent_image_path or image_path),
        "source_file": str(image_path),
        "caption": "Figure 1. a) spectrum of the emitter b) calculated trend.",
        "subfigure_caption_focus": "a) spectrum of the emitter",
        "nearby_text": "The selective emitter targets the atmospheric window.",
        "body_callout_texts": ["As shown in Figure 1"],
        "linked_text_chunk_ids": ["p1:c001"],
        "bbox_px": [0, 0, 328, 288],
        "bbox_original_px": [0, 0, 324, 285],
        "bbox_padding_ratio": 0.01,
        "visual_profile": {
            "schema_version": "visual_chunk_hq_profile.v1",
            "intrinsic_visual_labels": {
                "visual_role": "spectrum",
                "functional_visual_type": "graph",
                "visual_content_type": "emittance spectrum",
                "concise_label": "Emitter emittance spectrum",
            },
            "review_task_labels": {
                "review_utility": "high",
                "argument_function": "illustrate spectral design",
                "candidate_claims_supported_by_caption_or_text": [
                    "The selective emitter peaks in the atmospheric window."
                ],
            },
            "qa": {"needs_human_review": True, "confidence": "high"},
        },
        "visual_card": {
            "one_sentence_summary": "A spectrum plot of the selective emitter.",
            "best_use_in_review": "Direct use.",
        },
        "domain_hints": {
            "optical_asset_role": "spectrum",
            "wavelength_ranges": ["atmospheric"],
        },
        "quality": {"extraction_confidence": "high"},
        "visual_argument_type": "trend_or_parameter_map",
        "visual_argument_status": visual_argument_status,
        "visual_argument_confidence": "high",
        "visual_argument_claim": "Spectral selectivity drives cooling performance.",
        "visual_argument_needs_human_review": 1,
        "visual_argument_schema_version": "visual_argument_protocol.v1",
        "needs_human_review": True,
        "human_review_status": "pending",
        "review_decision": review_decision,
        "review_flags": [],
        "use_permission": "contextual_or_qualified_support",
        "allowed_claim_kinds": ["trend"],
        "license": "cc-by-4.0",
        "embedding_refs": [{"embedding_model": "mock-embedding", "status": "unindexed"}],
        "overlay_path": "overlays/fig1-a.png",
    }
    if extra:
        record.update(extra)
    return record


def test_ingest_parent_and_subfigure_preserves_source_and_creates_lineage(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source" / "papers" / "p1"
    source.mkdir(parents=True)
    parent_image = _image(source / "fig1.png", "red")
    child_image = _image(source / "fig1-a.png", "blue")
    parent_bytes_before = parent_image.read_bytes()

    records = [
        _chunk_record(
            chunk_id="p1-fig1-parent",
            kind="parent_figure",
            subfigure_label="",
            image_path=parent_image,
            parent_image_path=parent_image,
            review_decision="system_approved_test_mode",
        ),
        _chunk_record(
            chunk_id="p1-fig1-subfig-a",
            kind="subfigure",
            subfigure_label="a",
            image_path=child_image,
            parent_image_path=parent_image,
            review_decision="timeout_accepted_for_draft",
        ),
    ]
    assets = cache_tmp / "assets"
    units, report = ingest_visual_candidates(
        records,
        source_root=source,
        copy_assets_to=assets,
    )
    assert report["status"] == "ok"
    assert report["errors"] == []
    assert len(units) == 2

    by_kind = {unit["unit_kind"]: unit for unit in units}
    parent_unit = by_kind["parent_figure"]
    child_unit = by_kind["subfigure"]
    assert parent_unit["unit_role"] == "parent_context"
    assert child_unit["unit_role"] == "review_asset"
    assert child_unit["lineage"]["parent_unit_id"] == parent_unit["unit_id"]
    assert child_unit["lineage"]["parent_unavailable"] is False

    assert parent_image.read_bytes() == parent_bytes_before
    parent_ref = parent_unit["paths"]["image_ref"]["relative"]
    child_ref = child_unit["paths"]["image_ref"]["relative"]
    assert parent_ref.startswith("assets/")
    assert child_ref.startswith("assets/")
    assert (assets / parent_ref).is_file()
    assert (assets / child_ref).is_file()
    assert (assets / parent_ref) != (assets / child_ref)
    assert parent_unit["paths"]["source_ref"] == {
        "root": "source",
        "relative": "fig1.png",
    }
    serialized = json.dumps(units, ensure_ascii=False)
    assert str(source.resolve()).replace("\\", "/") not in serialized


def test_ingest_preserves_semantics_permission_hashes_and_placeholders(
    cache_tmp: Path,
) -> None:
    image = _image(cache_tmp / "source" / "fig.png", "green")
    units, report = ingest_visual_candidates(
        [
            _chunk_record(
                chunk_id="p1-fig1-subfig-a",
                kind="subfigure",
                subfigure_label="a",
                image_path=image,
                parent_image_path=image,
            )
        ],
        source_root=cache_tmp / "source",
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    unit = units[0]
    assert unit["schema_version"] == "optomind.visual_unit.v1"
    assert unit["unit_id"].startswith("unit:visual:")
    assert unit["source_identity"]["paper_id"] == "p1"
    assert unit["source_identity"]["doi"] == "10.1/visual-test"
    assert unit["figure_identity"]["figure_label"] == "Figure 1"
    assert unit["figure_identity"]["subfigure_label"] == "a"
    assert unit["caption"]["clean"].startswith("Figure 1.")
    assert unit["caption"]["subfigure_focus"] == "a) spectrum of the emitter"
    assert unit["semantic"]["description"] == (
        "A spectrum plot of the selective emitter."
    )
    assert "spectrum" in unit["semantic"]["tags"]
    assert unit["semantic"]["tags"][0] == "spectrum"
    assert unit["argumentative_roles"]["primary"] == "trend_or_parameter_map"
    assert unit["argumentative_roles"]["claim"] == (
        "Spectral selectivity drives cooling performance."
    )
    assert unit["argumentative_roles"]["confidence"] == "high"
    assert unit["argumentative_roles"]["needs_human_review"] is True
    assert unit["permission_state"]["use_permission"] == (
        "contextual_or_qualified_support"
    )
    assert unit["permission_state"]["allowed_claim_kinds"] == ["trend"]
    assert unit["permission_state"]["license"] == "cc-by-4.0"
    expected_sha = hashlib.sha256(image.read_bytes()).hexdigest()
    assert unit["hashes"]["image_sha256"] == expected_sha
    assert unit["hashes"]["content_hash"] == "sha256:" + expected_sha
    assert unit["hashes"]["record_sha256"].startswith("sha256:")
    assert unit["vector_refs"]["entries"] == [
        {"embedding_model": "mock-embedding", "status": "unindexed"}
    ]
    assert unit["vector_refs"]["indexed"] is False
    assert unit["use_history"]["used_in_run_ids"] == []
    assert unit["use_history"]["citations"] == []
    assert unit["lineage"]["generation_status"] == "source_derived"
    assert unit["lineage"]["enhancement_history"] == []
    assert unit["provenance"]["schema_version"] == "visual_chunk.v1"
    assert unit["provenance"]["source_file_ref"]["relative"].endswith(
        "fig.png"
    )


def test_pending_candidates_never_masquerade_as_approved(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    records = [
        _chunk_record(
            chunk_id="p1-pending-system",
            kind="single_figure",
            subfigure_label="",
            image_path=_image(source / "sys.png", "red"),
            review_decision="system_approved_test_mode",
        ),
        _chunk_record(
            chunk_id="p1-pending-timeout",
            kind="single_figure",
            subfigure_label="",
            image_path=_image(source / "timeout.png", "orange"),
            review_decision="timeout_accepted_for_draft",
        ),
        _chunk_record(
            chunk_id="p1-pending-no-decision",
            kind="single_figure",
            subfigure_label="",
            image_path=_image(source / "plain.png", "yellow"),
            visual_argument_status="pending_multimodal_review",
        ),
        _chunk_record(
            chunk_id="p1-approved",
            kind="single_figure",
            subfigure_label="",
            image_path=_image(source / "approved.png", "blue"),
            review_decision="human_approved",
            visual_argument_status="ok",
        ),
        _chunk_record(
            chunk_id="p1-rejected",
            kind="single_figure",
            subfigure_label="",
            image_path=_image(source / "rejected.png", "gray"),
            review_decision="human_rejected",
        ),
    ]
    units, report = ingest_visual_candidates(
        records,
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    states = {unit["unit_id"]: unit["approval"]["state"] for unit in units}
    by_decision = {
        unit["review"]["review_decision"]: unit for unit in units
    }
    assert by_decision["system_approved_test_mode"]["approval"]["state"] == (
        "pending"
    )
    assert by_decision["timeout_accepted_for_draft"]["approval"]["state"] == (
        "pending"
    )
    assert by_decision[""]["approval"]["state"] == "pending"
    for unit in units:
        if unit["approval"]["state"] == "pending":
            assert unit["approval"]["approved_at"] == ""
            assert unit["approval"]["source_marker"] == ""
    approved = by_decision["human_approved"]
    assert approved["approval"]["state"] == "approved"
    assert approved["approval"]["source_marker"] == "human_approved"
    assert approved["approval"]["approved_at"]
    assert by_decision["human_rejected"]["approval"]["state"] == "rejected"


def test_ingest_fails_open_per_bad_asset_but_keeps_good_units(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    good_image = _image(source / "good.png", "teal")
    records = [
        _chunk_record(
            chunk_id="p1-good",
            kind="single_figure",
            subfigure_label="",
            image_path=good_image,
        ),
        {
            "chunk_id": "p1-missing",
            "paper_id": "p1",
            "local_image_path": str(cache_tmp / "does-not-exist.png"),
        },
        "not-a-mapping",
    ]
    units, report = ingest_visual_candidates(
        records,
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert len(units) == 1
    assert report["status"] == "degraded"
    reasons = [error["reason"] for error in report["errors"]]
    assert "image_path_unresolved" in reasons
    assert "candidate_not_object" in reasons
    assert report["candidates_seen"] == 3


def test_ingest_dedupes_same_image_and_rejects_hash_collision(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _image(source / "same.png", "purple")
    units, report = ingest_visual_candidates(
        [
            _chunk_record(
                chunk_id="p1-a",
                kind="subfigure",
                subfigure_label="a",
                image_path=image,
                parent_image_path=image,
            ),
            _chunk_record(
                chunk_id="p1-a-copy",
                kind="subfigure",
                subfigure_label="a",
                image_path=image,
                parent_image_path=image,
            ),
        ],
        source_root=source,
        copy_assets_to=cache_tmp / "assets-dupe",
    )
    assert len(units) == 1
    assert report["duplicates_skipped"] == 1

    units, report = ingest_visual_candidates(
        [
            _chunk_record(
                chunk_id="p1-a",
                kind="subfigure",
                subfigure_label="a",
                image_path=image,
                parent_image_path=image,
            ),
            _chunk_record(
                chunk_id="p1-b",
                kind="subfigure",
                subfigure_label="b",
                image_path=image,
                parent_image_path=image,
            ),
        ],
        source_root=source,
        copy_assets_to=cache_tmp / "assets-collision",
    )
    assert len(units) == 1
    assert report["errors"]
    assert any(
        "duplicate_image_hash_different_identity" in error["reason"]
        for error in report["errors"]
    )


def test_ingest_staging_kb_pending_queue(cache_tmp: Path) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    pending_image = _image(source / "pending.png", "lime")
    approved_image = _image(source / "approved.png", "cyan")
    db_path = cache_tmp / "staging.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE visual_chunks(
              chunk_id TEXT PRIMARY KEY,
              paper_id TEXT,
              doi TEXT,
              title TEXT,
              chunk_kind TEXT,
              parent_asset_id TEXT,
              parent_label TEXT,
              subfigure_label TEXT,
              local_image_path TEXT,
              caption TEXT,
              search_text TEXT,
              visual_argument_type TEXT,
              visual_argument_status TEXT,
              visual_argument_needs_human_review INTEGER,
              review_decision TEXT,
              raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE visual_candidate_queue(
              candidate_visual_id TEXT PRIMARY KEY,
              paper_id TEXT,
              local_image_path TEXT,
              status TEXT,
              exclusion_reason TEXT,
              raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO visual_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "p1-pending",
                "p1",
                "10.1/x",
                "Paper",
                "single_figure",
                "p1-fig1",
                "Figure 1",
                "",
                str(pending_image),
                "Pending spectrum.",
                "spectrum pending",
                "trend_or_parameter_map",
                "pending_multimodal_review",
                1,
                "",
                "{}",
            ),
        )
        conn.execute(
            """
            INSERT INTO visual_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "p1-approved",
                "p1",
                "10.1/x",
                "Paper",
                "single_figure",
                "p1-fig1",
                "Figure 1",
                "",
                str(approved_image),
                "Approved spectrum.",
                "spectrum approved",
                "trend_or_parameter_map",
                "ok",
                0,
                "human_approved",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    candidates = candidates_from_staging_kb(db_path)
    assert len(candidates) == 2
    units, report = ingest_visual_candidates(
        candidates,
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    assert len(units) == 2
    states = {unit["unit_id"]: unit["approval"]["state"] for unit in units}
    assert set(states.values()) == {"pending", "approved"}


def test_ingest_visual_asset_protocol_record(cache_tmp: Path) -> None:
    source = cache_tmp / "source"
    image = _image(source / "fig2.png", "maroon")
    record = {
        "schema_version": "visual_asset.v1.1",
        "paper": {
            "paper_id": "p2",
            "doi": "10.2/asset-test",
            "title": "Asset Test Paper",
            "year": 2023,
            "venue": "Optics",
        },
        "asset_identity": {
            "asset_id": "p2-fig2",
            "asset_type": "figure",
            "label": "Figure 2",
            "subpanel_labels": [],
            "caption_original": "Figure 2. Measured transmittance.",
            "caption_clean": "Figure 2. Measured transmittance.",
            "caption_confidence": "high",
        },
        "source_provenance": {
            "source_format": "pdf_pymupdf",
            "source_file": str(image),
            "source_url": "https://example.test/paper",
            "parser": "pymupdf",
            "parser_version": "1.27",
            "extraction_run_id": "run-42",
            "page": 3,
            "bbox": [0, 0, 100, 100],
            "checksum": "abc123",
        },
        "local_resources": {
            "local_image_path": str(image),
            "mime_type": "image/png",
            "width": 360,
            "height": 240,
        },
        "document_context": {
            "section_role": "result",
            "nearby_text": "Measured transmittance is shown.",
        },
        "text_linkage": {
            "body_callouts": [],
            "linked_chunk_ids": ["p2:c7"],
        },
        "domain_hints": {
            "optical_asset_role": "spectrum",
            "wavelength_ranges": ["visible"],
        },
        "quality": {
            "extraction_confidence": "high",
            "failure_reason": "",
            "warnings": [],
        },
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    unit = units[0]
    assert unit["figure_identity"]["asset_id"] == "p2-fig2"
    assert unit["figure_identity"]["figure_label"] == "Figure 2"
    assert unit["caption"]["clean"] == "Figure 2. Measured transmittance."
    assert unit["provenance"]["parser"] == "pymupdf"
    assert unit["provenance"]["parser_version"] == "1.27"
    assert unit["provenance"]["extraction_run_id"] == "run-42"
    assert unit["provenance"]["checksum"] == "abc123"
    assert unit["provenance"]["source_file_ref"]["relative"].endswith(
        "fig2.png"
    )
    assert unit["semantic"]["domain_hints"]["optical_asset_role"] == (
        "spectrum"
    )
    assert unit["semantic"]["linked_text_chunk_ids"] == ["p2:c7"]


def test_ingest_empty_caption_uses_neutral_fallback_and_publishes(
    cache_tmp: Path,
) -> None:
    """Reproduces the S06 v2 empty-caption failure and proves fail-open."""

    source = cache_tmp / "source"
    with_label = _image(source / "fig3.png", "olive")
    no_label = _image(source / "fig4.png", "purple")
    records = [
        {
            "schema_version": "visual_asset.v1.1",
            "paper": {
                "paper_id": "p6",
                "doi": "10.6/empty-caption",
                "title": "Empty Caption Paper",
            },
            "asset_identity": {
                "asset_id": "p6-fig3",
                "asset_type": "figure",
                "label": "Fig. 3",
                "subpanel_labels": [],
                "caption_original": "",
                "caption_clean": "",
            },
            "source_provenance": {
                "source_format": "pdf_pymupdf",
                "source_file": str(with_label),
                "page": 3,
            },
            "local_resources": {
                "local_image_path": str(with_label),
            },
        },
        {
            "schema_version": "visual_asset.v1.1",
            "paper": {
                "paper_id": "p6",
                "doi": "10.6/empty-caption",
                "title": "Empty Caption Paper",
            },
            "asset_identity": {
                "asset_id": "p6-fig4",
                "asset_type": "figure",
                "subpanel_labels": [],
                "caption_original": "",
                "caption_clean": "",
            },
            "source_provenance": {
                "source_format": "pdf_pymupdf",
                "source_file": str(no_label),
                "page": 4,
            },
            "local_resources": {
                "local_image_path": str(no_label),
            },
        },
    ]
    units, report = ingest_visual_candidates(
        records,
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    assert report["caption_missing_count"] == 2
    assert any(
        "caption_missing:p6-fig3" in warning
        for warning in report["warnings"]
    )
    assert any(
        "caption_missing:p6-fig4" in warning
        for warning in report["warnings"]
    )
    assert len(units) == 2
    by_asset = {
        unit["figure_identity"]["asset_id"]: unit for unit in units
    }
    labelled = by_asset["p6-fig3"]
    assert labelled["caption"]["clean"] == (
        "Fig. 3; caption unavailable; inspect the source figure."
    )
    unlabelled = by_asset["p6-fig4"]
    assert unlabelled["caption"]["clean"] == (
        "Caption unavailable; inspect the source figure."
    )
    for unit in units:
        assert unit["caption"]["missing"] is True
        assert unit["caption"]["fallback_reason"] == (
            "caption_unavailable_non_claim_placeholder"
        )
        assert unit["provenance"]["caption_status"] == (
            "missing_needs_review"
        )
        assert "caption_missing" in unit["review"]["review_flags"]
        assert unit["approval"]["state"] == "pending"
        assert validate_visual_unit(unit) == []

    # The batch is publishable and verifies (assets + hashes).
    store = VisualCacheStore(cache_tmp / "cache")
    store.publish_snapshot(
        version="snapshot-0001",
        units=units,
        assets_dir=cache_tmp / "assets",
    )
    verification = store.verify_snapshot("snapshot-0001")
    assert verification["status"] == "passed"
    assert verification["unit_count"] == 2
    for unit in units:
        asset_rel = unit["paths"]["image_ref"]["relative"]
        asset_path = store.snapshot_path("snapshot-0001") / asset_rel
        assert asset_path.is_file()
        assert (
            hashlib.sha256(asset_path.read_bytes()).hexdigest()
            == unit["hashes"]["image_sha256"]
        )


def test_ingest_rendered_region_stores_crop_hygiene_and_derivative_lineage(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _caption_image(source / "region.png", caption_h=60)
    before = image.read_bytes()
    record = {
        "schema_version": "visual_asset.v1.1",
        "paper": {"paper_id": "p3", "doi": "10.3/render", "title": "Render"},
        "asset_identity": {
            "asset_id": "p3-fig1",
            "label": "Figure 1",
            "caption_clean": "Figure 1. Measured spectrum.",
        },
        "source_provenance": {
            "source_format": "pdf_pymupdf",
            "parser": "pymupdf_caption_crop",
            "bbox": [30, 40, 390, 300],
        },
        "local_resources": {"local_image_path": str(image)},
        "caption_bbox": [40, 200, 320, 260],
        "quality": {"extraction_confidence": "high"},
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    unit = units[0]
    assert unit["crop_hygiene"]["status"] == "derived_clean"
    assert "_cleaned_" in unit["paths"]["image_ref"]["relative"]
    assert unit["paths"]["original_image_ref"]["relative"].startswith(
        "assets/"
    )
    assert unit["lineage"]["crop"]["crop_bbox"] == [0, 0, 360, 160]
    assert unit["lineage"]["crop"]["parent_image_hash"] == hashlib.sha256(
        before
    ).hexdigest()
    assert unit["lineage"]["crop"]["parent_image_ref"] == unit["paths"][
        "original_image_ref"
    ]
    assert validate_visual_unit(unit) == []
    asset_root = cache_tmp / "assets" / "assets"
    image_name = Path(unit["paths"]["image_ref"]["relative"]).name
    original_name = Path(unit["paths"]["original_image_ref"]["relative"]).name
    assert (asset_root / image_name).is_file()
    assert (asset_root / original_name).is_file()
    assert image_name != original_name
    assert image.read_bytes() == before


def test_ingest_ambiguous_page_stays_needs_review_and_keeps_original(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _prose_image(source / "prose.png")
    record = {
        "schema_version": "visual_asset.v1.1",
        "paper": {"paper_id": "p4", "doi": "10.4/prose", "title": "Prose"},
        "asset_identity": {
            "asset_id": "p4-fig1",
            "label": "Figure 1",
            "caption_clean": "Figure 1. Caption.",
        },
        "source_provenance": {
            "source_format": "pdf_pymupdf",
            "parser": "pymupdf_caption_crop",
        },
        "local_resources": {"local_image_path": str(image)},
        "quality": {"extraction_confidence": "low"},
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    unit = units[0]
    assert unit["crop_hygiene"]["status"] in {"needs_review", "rejected"}
    assert unit["crop_hygiene"]["derivative"] is None
    assert unit["paths"]["original_image_ref"] is None
    assert unit["lineage"]["crop"]["derivative"] is None
    assert unit["lineage"]["crop"]["parent_image_hash"] == ""
    assert unit["paths"]["image_ref"]["relative"].startswith("assets/")
    assert validate_visual_unit(unit) == []


def test_caption_contamination_detected_recorded_and_publishable(
    cache_tmp: Path,
) -> None:
    """A rendered figure with caption/page prose is recorded, not hidden."""

    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _prose_image(source / "contaminated.png")
    record = {
        "schema_version": "visual_asset.v1.1",
        "paper": {"paper_id": "p9", "doi": "10.9/contam", "title": "Prose"},
        "asset_identity": {
            "asset_id": "p9-fig1",
            "label": "Figure 1",
            "caption_clean": "Figure 1. Caption.",
        },
        "source_provenance": {
            "source_format": "pdf_pymupdf",
            "parser": "pymupdf_caption_crop",
        },
        "local_resources": {"local_image_path": str(image)},
        "quality": {"extraction_confidence": "low"},
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    assert report["caption_contamination_count"] == 1
    assert any(
        "caption_contamination:p9-fig1" in warning
        for warning in report["warnings"]
    )
    unit = units[0]
    contamination = unit["crop_hygiene"]["caption_contamination"]
    assert contamination["detected"] is True
    assert contamination["requires_review"] is True
    assert "caption_in_pixels" in unit["review"]["review_flags"]
    assert validate_visual_unit(unit) == []
    store = VisualCacheStore(cache_tmp / "cache")
    store.publish_snapshot(
        version="snapshot-0001",
        units=units,
        assets_dir=cache_tmp / "assets",
    )
    assert store.verify_snapshot("snapshot-0001")["status"] == "passed"


def test_relative_source_root_and_permission_normalization(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    (source / "figs").mkdir(parents=True)
    image = _image(source / "figs" / "rel.png", "cyan")
    record = {
        "schema_version": "visual_asset.v1.1",
        "paper": {
            "paper_id": "p8",
            "doi": "10.8/rel",
            "title": "Relative Paper",
        },
        "asset_identity": {
            "asset_id": "p8-fig1",
            "asset_type": "figure",
            "label": "Fig. 1",
            "subpanel_labels": [],
            "caption_original": "Figure 1. Caption.",
            "caption_clean": "Figure 1. Caption.",
        },
        "source_provenance": {
            "source_format": "pdf_pymupdf",
            "source_file": "figs/rel.png",
            "page": 1,
        },
        "local_resources": {"local_image_path": "figs/rel.png"},
        "use_permission": "factual_support",
        "license": "cc-by-4.0",
        "allowed_claim_kinds": ["mechanism"],
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    assert len(units) == 1
    unit = units[0]
    assert unit["permission_state"]["use_permission"] == "factual_support"
    assert unit["permission_state"]["license"] == "cc-by-4.0"
    assert unit["permission_state"]["allowed_claim_kinds"] == ["mechanism"]
    asset_rel = unit["paths"]["image_ref"]["relative"]
    assert (cache_tmp / "assets" / asset_rel).is_file()
    assert validate_visual_unit(unit) == []


def test_local_vector_refs_persist_and_local_retrieval(cache_tmp: Path) -> None:
    source = cache_tmp / "source"
    image = _image(source / "vec.png", "teal")
    record = {
        "schema_version": "visual_asset.v1.1",
        "paper": {"paper_id": "p10", "doi": "10.10/vec", "title": "Vec"},
        "asset_identity": {
            "asset_id": "p10-fig1",
            "label": "Figure 1",
            "caption_clean": "Figure 1. Caption.",
        },
        "source_provenance": {
            "source_format": "pdf_pymupdf",
            "source_file": str(image),
        },
        "local_resources": {"local_image_path": str(image)},
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    unit = units[0]
    refs = unit["vector_refs"]
    assert refs["indexed"] is True
    assert refs["semantic"] is False
    assert refs["model"] == "local_content_features_v1"
    assert len(refs["entries"]) == 1
    entry = refs["entries"][0]
    assert entry["semantic"] is False
    assert entry["embedding"] is None
    assert len(entry["vector"]) == 12
    assert entry["image_sha256"] == unit["hashes"]["image_sha256"]

    store = VisualCacheStore(cache_tmp / "cache")
    store.publish_snapshot(
        version="snapshot-0001",
        units=units,
        assets_dir=cache_tmp / "assets",
    )
    loaded_units = store.load_snapshot("snapshot-0001")["units"]
    index = LocalVisualIndex.from_units(loaded_units)
    assert len(index.entries) == 1
    query = local_image_feature_vector(image)
    result = index.retrieve(query, top_k=1)
    assert result["status"] == "local_content_features"
    assert result["semantic"] is False
    assert result["matches"][0]["unit_id"] == unit["unit_id"]
    assert result == index.retrieve(query, top_k=1)

    semantic = index.retrieve(query, top_k=1, require_semantic=True)
    assert semantic["status"] == "no_semantic_embeddings"
    assert semantic["matches"] == []
    assert LocalVisualIndex().retrieve(query)["matches"] == []


def test_low_confidence_tiny_band_not_flagged_caption_in_pixels(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _tiny_band_image(source / "tiny.png")
    record = {
        "schema_version": "visual_asset.v1.1",
        "paper": {"paper_id": "p11", "doi": "10.11/tiny", "title": "Tiny"},
        "asset_identity": {
            "asset_id": "p11-fig1",
            "label": "Figure 1",
            "caption_clean": "Figure 1. Caption.",
        },
        "source_provenance": {"source_format": "pdf_pymupdf"},
        "local_resources": {"local_image_path": str(image)},
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    assert report["caption_contamination_count"] == 0
    unit = units[0]
    assert unit["crop_hygiene"]["status"] == "clean"
    assert "caption_contamination" not in unit["crop_hygiene"]
    assert "caption_in_pixels" not in unit["review"]["review_flags"]


def test_rendered_region_trailing_whitespace_derived_clean_publishable(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _multiline_trailing_caption_image(source / "trailing.png")
    record = {
        "schema_version": "visual_asset.v1.1",
        "paper": {"paper_id": "p12", "doi": "10.12/trail", "title": "Trail"},
        "asset_identity": {
            "asset_id": "p12-fig1",
            "label": "Figure 1",
            "caption_clean": "Figure 1. Caption.",
        },
        "source_provenance": {
            "source_format": "pdf_pymupdf",
            "parser": "pymupdf_caption_crop",
        },
        "local_resources": {"local_image_path": str(image)},
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    assert report["caption_contamination_count"] == 1
    unit = units[0]
    assert unit["crop_hygiene"]["status"] == "derived_clean"
    assert unit["crop_hygiene"]["caption_contamination"]["detected"] is True
    assert "_cleaned_" in unit["paths"]["image_ref"]["relative"]
    assert validate_visual_unit(unit) == []
    store = VisualCacheStore(cache_tmp / "cache")
    store.publish_snapshot(
        version="snapshot-0001",
        units=units,
        assets_dir=cache_tmp / "assets",
    )
    assert store.verify_snapshot("snapshot-0001")["status"] == "passed"


def test_page_prose_contamination_flagged_and_publishable(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _prose_image(source / "prose-flag.png")
    record = {
        "schema_version": "visual_asset.v1.1",
        "paper": {"paper_id": "p13", "doi": "10.13/prose", "title": "Prose"},
        "asset_identity": {
            "asset_id": "p13-fig1",
            "label": "Figure 1",
            "caption_clean": "Figure 1. Caption.",
        },
        "source_provenance": {
            "source_format": "pdf_pymupdf",
            "parser": "pymupdf_caption_crop",
        },
        "local_resources": {"local_image_path": str(image)},
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    assert report["caption_contamination_count"] == 1
    unit = units[0]
    contamination = unit["crop_hygiene"]["caption_contamination"]
    assert contamination["detected"] is True
    assert contamination["page_prose"] is True
    assert "page_prose" in unit["review"]["review_flags"]
    assert "caption_in_pixels" in unit["review"]["review_flags"]
    assert unit["crop_hygiene"]["status"] in {"needs_review", "rejected"}
    assert validate_visual_unit(unit) == []
    store = VisualCacheStore(cache_tmp / "cache")
    store.publish_snapshot(
        version="snapshot-0001",
        units=units,
        assets_dir=cache_tmp / "assets",
    )
    assert store.verify_snapshot("snapshot-0001")["status"] == "passed"


def test_local_vector_features_bounded_and_deterministic(
    cache_tmp: Path,
) -> None:
    from optomind_research.runtime.visual_local_vectors import (
        LOCAL_VECTOR_THUMBNAIL_MAX,
        local_image_feature_vector,
    )

    big = cache_tmp / "big.png"
    Image.new("RGB", (1200, 800), "navy").save(big)
    small = cache_tmp / "small.png"
    with Image.open(big) as opened:
        thumbnail = opened.copy()
        thumbnail.thumbnail(
            (LOCAL_VECTOR_THUMBNAIL_MAX, LOCAL_VECTOR_THUMBNAIL_MAX)
        )
        thumbnail.save(small)
    big_vector = local_image_feature_vector(big)
    small_vector = local_image_feature_vector(small)
    assert len(big_vector) == 12
    assert big_vector == small_vector


def test_staging_kb_merges_semantic_chunk_with_raw_asset_for_same_image(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image_a = _image(source / "a.png", "red")
    image_b = _image(source / "b.png", "blue")
    sha_a = hashlib.sha256(image_a.read_bytes()).hexdigest()
    db_path = cache_tmp / "staging.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE visual_chunks(
              chunk_id TEXT PRIMARY KEY,
              paper_id TEXT, doi TEXT, title TEXT, chunk_kind TEXT,
              local_image_path TEXT, caption TEXT,
              visual_argument_type TEXT, visual_argument_status TEXT,
              visual_argument_needs_human_review INTEGER,
              review_decision TEXT, raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE visual_assets(
              asset_id TEXT PRIMARY KEY,
              paper_id TEXT, doi TEXT, title TEXT, asset_type TEXT,
              label TEXT, caption TEXT, local_image_path TEXT, raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE visual_candidate_queue(
              candidate_visual_id TEXT PRIMARY KEY,
              paper_id TEXT, local_image_path TEXT, status TEXT,
              exclusion_reason TEXT, raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO visual_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "semantic-a",
                "p1",
                "10.1/merge",
                "Merge Paper",
                "single_figure",
                str(image_a),
                "Figure 1. Semantic caption.",
                "trend_or_parameter_map",
                "ok",
                0,
                "human_approved",
                json.dumps(
                    {
                        "chunk_id": "semantic-a",
                        "visual_profile": {
                            "intrinsic_visual_labels": {
                                "visual_role": "spectrum"
                            }
                        },
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO visual_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "semantic-b",
                "p1",
                "10.1/merge",
                "Merge Paper",
                "single_figure",
                str(image_b),
                "Figure 2. Unrelated caption.",
                "representative_example",
                "ok",
                0,
                "",
                "{}",
            ),
        )
        conn.execute(
            """
            INSERT INTO visual_assets VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "raw-a",
                "p1",
                "10.1/merge",
                "Merge Paper",
                "figure",
                "Figure 1",
                "Figure 1. Raw caption.",
                str(image_a),
                json.dumps(
                    {
                        "asset_id": "raw-a",
                        "chunk_id": "semantic-a",
                        "candidate_visual_id": "queue-a",
                        "local_image_path": str(image_a),
                        "image_sha256": sha_a,
                        "extraction_method": (
                            "rendered_region_from_caption"
                        ),
                        "bbox_pdf": [30, 40, 390, 300],
                        "caption_bbox": [40, 200, 320, 260],
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO visual_assets VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "raw-b",
                "p1",
                "10.1/merge",
                "Merge Paper",
                "figure",
                "Figure 2",
                "Figure 2. Raw caption.",
                str(image_b),
                json.dumps(
                    {
                        "asset_id": "raw-b",
                        "local_image_path": str(image_b),
                        "image_sha256": hashlib.sha256(
                            image_b.read_bytes()
                        ).hexdigest(),
                        "extraction_method": "embedded_raster_image",
                        "bbox_pdf": [10, 20, 200, 220],
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO visual_candidate_queue VALUES(?,?,?,?,?,?)
            """,
            (
                "queue-a",
                "p1",
                str(image_a),
                "pending_multimodal_review",
                "",
                json.dumps(
                    {
                        "candidate_visual_id": "queue-a",
                        "chunk_id": "semantic-a",
                        "asset_id": "raw-a",
                        "image_sha256": sha_a,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    candidates = candidates_from_staging_kb(db_path)
    assert len(candidates) == 2
    by_chunk = {
        str(row.get("chunk_id") or ""): row
        for row in candidates
    }
    merged_a = by_chunk["semantic-a"]
    assert merged_a["visual_argument_type"] == "trend_or_parameter_map"
    assert merged_a["visual_argument_status"] == "ok"
    assert merged_a["review_decision"] == "human_approved"
    assert (
        merged_a["visual_profile"]["intrinsic_visual_labels"]["visual_role"]
        == "spectrum"
    )
    assert merged_a["caption"] == "Figure 1. Semantic caption."
    assert merged_a["bbox_pdf"] == [30, 40, 390, 300]
    assert merged_a["caption_bbox"] == [40, 200, 320, 260]
    assert merged_a["extraction_method"] == "rendered_region_from_caption"
    assert merged_a["local_image_path"] == str(image_a)
    assert merged_a["image_sha256"] == sha_a
    assert merged_a["chunk_id"] == "semantic-a"

    merged_b = by_chunk["semantic-b"]
    assert merged_b["extraction_method"] == "embedded_raster_image"
    assert merged_b["bbox_pdf"] == [10, 20, 200, 220]
    assert merged_b["local_image_path"] == str(image_b)
    assert merged_b["chunk_id"] == "semantic-b"
    assert set(by_chunk) == {"semantic-a", "semantic-b"}


def _table_advisor_advice(**overrides: dict) -> dict:
    payload = {
        "schema_version": "optomind.visual_qwen_crop_advisor.v1",
        "ok": True,
        "needs_review": False,
        "asset_kind": "table",
        "content_bbox": [0.1, 0.1, 0.9, 0.75],
        "caption_bbox": [0.05, 0.8, 0.95, 0.98],
        "panel_boxes": [],
        "caption_text": "Table 1. Summary of radiative cooling performance.",
        "confidence": 0.92,
        "contamination_notes": [],
        "advisor": {"model": "qwen-test"},
    }
    payload.update(overrides)
    return payload


def test_ingest_table_with_advisor_crop_typing_and_publication_policy(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _image(source / "table1.png", "slateblue")
    before = image.read_bytes()
    record = {
        "schema_version": "visual_chunk.v1",
        "chunk_id": "p1-table-1",
        "chunk_kind": "table",
        "paper_id": "p1",
        "doi": "10.1/table",
        "paper_title": "Table Paper",
        "figure_label": "Table 1",
        "local_image_path": str(image),
        "caption": "Table 1. Summary of radiative cooling performance.",
        "use_permission": "discovery_only",
        "visual_argument_status": "pending_multimodal_review",
        "qwen_crop_advisor": _table_advisor_advice(),
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    assert report["asset_kind_counts"] == {"table": 1}
    unit = units[0]
    assert unit["figure_identity"]["asset_kind"] == "table"
    assert unit["asset_typing"] == {
        "asset_kind": "table",
        "table": True,
        "source": "qwen_advisor",
        "notes": [],
    }
    assert unit["crop_hygiene"]["status"] == "derived_clean"
    assert unit["crop_hygiene"]["source_kind"] == (
        "qwen_advisor_content_crop"
    )
    assert unit["paths"]["original_image_ref"]["relative"].startswith(
        "assets/"
    )
    assert unit["lineage"]["crop"]["parent_image_hash"] == hashlib.sha256(
        before
    ).hexdigest()
    assert unit["source_map"]["asset_kind"] == "table"
    assert unit["source_map"]["nodes"][0]["node_type"] == "table"
    assert unit["source_map"]["nodes"][0]["asset_kind"] == "table"
    assert unit["permission_state"]["publication_eligible"] is False
    assert unit["permission_state"]["publication_eligible_reason"] == (
        "external_or_discovery_only_not_publication_eligible"
    )
    assert unit["permission_state"]["external_discovery_only"] is True
    assert unit["vector_refs"]["semantic"] is False
    assert len(unit["vector_refs"]["entries"][0]["vector"]) == 12
    assert validate_visual_unit(unit) == []
    assert image.read_bytes() == before

    store = VisualCacheStore(cache_tmp / "cache")
    store.publish_snapshot(
        version="snapshot-0001",
        units=units,
        assets_dir=cache_tmp / "assets",
    )
    verification = store.verify_snapshot("snapshot-0001")
    assert verification["status"] == "passed"
    loaded = store.get_unit("snapshot-0001", unit["unit_id"])
    assert loaded is not None
    assert loaded["figure_identity"]["asset_kind"] == "table"
    assert loaded["permission_state"]["publication_eligible"] is False
    conn = sqlite3.connect(
        store.snapshot_path("snapshot-0001") / "visual_cache.sqlite"
    )
    try:
        row = conn.execute(
            "SELECT asset_kind, asset_typing_json FROM units "
            "WHERE unit_id=?",
            (unit["unit_id"],),
        ).fetchone()
        assert row is not None
        assert row[0] == "table"
        assert json.loads(row[1])["table"] is True
    finally:
        conn.close()


def test_ingest_advisor_failure_preserves_original_with_needs_review(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _image(source / "pending.png", "navy")
    before = image.read_bytes()
    record = {
        "schema_version": "visual_chunk.v1",
        "chunk_id": "p1-advice-fail",
        "chunk_kind": "single_figure",
        "paper_id": "p1",
        "doi": "10.1/advice-fail",
        "figure_label": "Figure 1",
        "local_image_path": str(image),
        "caption": "Figure 1. Caption.",
        "use_permission": "discovery_only",
        "visual_argument_status": "pending_multimodal_review",
        "qwen_crop_advisor": {
            "schema_version": "optomind.visual_qwen_crop_advisor.v1",
            "ok": False,
            "needs_review": True,
            "asset_kind": "unknown",
            "confidence": 0.0,
            "errors": ["malformed_json:ValueError:unparseable"],
        },
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    unit = units[0]
    assert unit["crop_hygiene"]["status"] == "needs_review"
    assert unit["crop_hygiene"]["derivative"] is None
    assert "qwen_advisor_unavailable_or_low_confidence" in (
        unit["crop_hygiene"]["reason"]
    )
    assert unit["paths"]["image_ref"]["relative"].startswith("assets/")
    assert unit["paths"]["original_image_ref"] is None
    assert unit["figure_identity"]["asset_kind"] == "unknown"
    assert unit["asset_typing"]["source"] == "qwen_advisor"
    assert unit["permission_state"]["publication_eligible"] is False
    assert validate_visual_unit(unit) == []
    assert image.read_bytes() == before


def test_ingest_legacy_record_without_asset_kind_remains_figure(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _image(source / "legacy.png", "olive")
    record = {
        "schema_version": "visual_asset.v1.1",
        "paper": {
            "paper_id": "p-legacy",
            "doi": "10.1/legacy",
            "title": "Legacy Paper",
        },
        "asset_identity": {
            "asset_id": "p-legacy-fig1",
            "asset_type": "figure",
            "label": "Figure 1",
            "caption_clean": "Figure 1. Caption.",
        },
        "source_provenance": {
            "source_format": "pdf_pymupdf",
            "source_file": str(image),
        },
        "local_resources": {"local_image_path": str(image)},
        "use_permission": "factual_support",
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    unit = units[0]
    assert unit["figure_identity"]["asset_kind"] == "figure"
    assert unit["asset_typing"]["table"] is False
    assert unit["asset_typing"]["source"] == "candidate_or_heuristic"
    assert unit["permission_state"]["publication_eligible"] is False
    assert validate_visual_unit(unit) == []


def test_generic_image_asset_type_is_a_figure_not_a_photo(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _image(source / "generic-image.png", "olive")
    record = {
        "schema_version": "visual_chunk.v1",
        "chunk_id": "p-generic-image-fig1",
        "chunk_kind": "single_figure",
        "asset_type": "image",
        "paper_id": "p-generic-image",
        "figure_label": "Figure 1",
        "local_image_path": str(image),
        "caption": "Figure 1. Generic image asset with a scientific plot.",
        "use_permission": "factual_support",
        "visual_argument_status": "ok",
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    assert units[0]["figure_identity"]["asset_kind"] == "figure"


def test_ingest_local_table_label_wins_over_advisor_misclassification(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _image(source / "table-mislabel.png", "teal")
    record = {
        "schema_version": "visual_chunk.v1",
        "chunk_id": "p1-table-mislabel",
        "chunk_kind": "single_figure",
        "paper_id": "p1",
        "doi": "10.1/table-mislabel",
        "figure_label": "Table 1",
        "local_image_path": str(image),
        "caption": "Table 1. Summary of radiative cooling performance.",
        "use_permission": "factual_support",
        "visual_argument_status": "ok",
        "review_decision": "human_approved",
        "qwen_crop_advisor": _table_advisor_advice(asset_kind="figure"),
    }
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    unit = units[0]
    assert unit["figure_identity"]["asset_kind"] == "table"
    assert unit["asset_typing"]["asset_kind"] == "table"
    assert unit["asset_typing"]["source"] == "local_table_label"
    assert unit["source_map"]["nodes"][0]["node_type"] == "table"
    assert validate_visual_unit(unit) == []
