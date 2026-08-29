from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from PIL import Image

from optomind_research.runtime.visual_cache_ingest import (
    ingest_visual_candidates,
)
from optomind_research.runtime.visual_cache_schemas import (
    GENERATED_DISCLOSURE,
    validate_visual_unit,
)
from optomind_research.runtime.visual_cache_store import VisualCacheStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def cache_tmp() -> Path:
    scratch = PROJECT_ROOT / ".codex-tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    root = scratch / f"visual-generated-test-{uuid.uuid4().hex[:10]}"
    root.mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)
    try:
        scratch.rmdir()
    except OSError:
        pass


def _generated_image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (420, 300), color="lightblue").save(path)
    return path


def _generated_record(
    *,
    figure_id: str,
    image_path: Path,
    review_decision: str = "human_approved",
    generation_status: str = "model_approved_human_pending",
    extra: dict | None = None,
) -> dict:
    record = {
        "schema_version": "research_harness.final_visual_package.v1",
        "figure_id": figure_id,
        "section_id": "S01",
        "purpose": "Explain the resonant mechanism conceptually.",
        "figure_type": "conceptual_schematic",
        "local_path": str(image_path),
        "placement_anchor": "S01",
        "caption_en": "Conceptual mechanism schematic.",
        "source_route": "conceptual_generated",
        "generated_or_source": "generated",
        "review_decision": review_decision,
        "review_flags": [],
        "paper_id": "article-placeholder-123",
        "doi": "10.9999/do-not-use",
        "title": "Placeholder Article Title",
        "generation_result": {
            "generation_status": generation_status,
            "local_image_path": str(image_path),
            "generation_model_used": "qwen-image-2.0-pro",
            "generation_brief": (
                "Draw a mechanism schematic without empirical claims."
            ),
            "provenance_path": "artifacts/gen-001.json",
            "created_at": "2026-08-13T00:00:00+00:00",
            "model_review": {
                "verdict": "approve",
                "misleading_elements": [],
            },
            "review_history": [
                {
                    "decision": review_decision,
                    "reviewer": "human_review",
                    "reviewed_at": "2026-08-13T00:05:00+00:00",
                }
            ],
            "attempt_history": [
                {"attempt": 1, "generation_status": "failed"},
                {"attempt": 2, "generation_status": generation_status},
            ],
            "retry_history": [
                {"attempt": 1, "generation_status": "failed"}
            ],
        },
    }
    if extra:
        record.update(extra)
    return record


def test_approved_generated_visual_ingests_with_explanatory_contract(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _generated_image(source / "gen.png")
    record = _generated_record(figure_id="FIG-GEN-001", image_path=image)
    units, report = ingest_visual_candidates(
        [record],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    unit = units[0]

    assert unit["unit_kind"] == "generated_visual"
    assert unit["unit_role"] == "review_asset"
    assert unit["source_identity"]["source_kind"] == (
        "ai_generated_explanatory_visual"
    )
    assert unit["source_identity"]["doi"] == ""
    assert unit["source_identity"]["paper_id"] == "article-placeholder-123"
    assert unit["source_identity"]["title"] == "Placeholder Article Title"
    assert GENERATED_DISCLOSURE in unit["source_identity"]["rights"][
        "disclosure"
    ]
    assert GENERATED_DISCLOSURE in unit["caption"]["disclosure"]
    assert GENERATED_DISCLOSURE in unit["caption"]["clean"]
    assert unit["permission_state"]["evidence_ceiling"] == "explanatory_only"
    assert unit["permission_state"]["empirical_evidence_allowed"] is False
    assert unit["permission_state"]["quantitative_evidence_allowed"] is False

    generation = unit["lineage"]["generation"]
    assert unit["lineage"]["generation_status"] == "ai_generated"
    assert generation["prompt"].startswith("Draw a mechanism schematic")
    assert generation["model_version"] == "qwen-image-2.0-pro"
    assert generation["created_at"] == "2026-08-13T00:00:00+00:00"
    assert GENERATED_DISCLOSURE in generation["disclosure"]
    assert generation["review_history"][0]["decision"] == "human_approved"
    assert len(generation["attempt_history"]) == 2
    assert unit["approval"]["state"] == "approved"
    assert unit["approval"]["source_marker"] == "human_approved"
    assert unit["review"]["review_decision"] == "human_approved"
    assert unit["review"]["human_review_status"] == "approved"
    assert validate_visual_unit(unit) == []


def test_generated_visual_schema_rejects_doi_and_empirical_claims(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _generated_image(source / "gen.png")
    units, _ = ingest_visual_candidates(
        [
            _generated_record(
                figure_id="FIG-GEN-001",
                image_path=image,
            )
        ],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    unit = units[0]
    assert validate_visual_unit(unit) == []

    unit["source_identity"]["doi"] = "10.1/fake"
    assert any(
        "generated_visual.must_not_have_source_doi"
        in error
        for error in validate_visual_unit(unit)
    )
    unit["source_identity"]["doi"] = ""

    unit["permission_state"]["empirical_evidence_allowed"] = True
    assert any(
        "generated_visual.empirical_evidence_not_allowed"
        in error
        for error in validate_visual_unit(unit)
    )
    unit["permission_state"]["empirical_evidence_allowed"] = False

    unit["approval"]["state"] = "pending"
    assert any(
        "generated_visual.must_be_approved" in error
        for error in validate_visual_unit(unit)
    )
    unit["approval"]["state"] = "approved"

    unit["lineage"]["generation"]["review_history"] = []
    assert any(
        "generated_visual.review_history_required" in error
        for error in validate_visual_unit(unit)
    )


def test_rejected_or_unapproved_generated_visuals_are_not_ingested(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    approved = _generated_image(source / "approved.png")
    pending = _generated_image(source / "pending.png")
    rejected = _generated_image(source / "rejected.png")
    records = [
        _generated_record(figure_id="FIG-GEN-001", image_path=approved),
        _generated_record(
            figure_id="FIG-GEN-002",
            image_path=pending,
            review_decision="system_approved_test_mode",
        ),
        _generated_record(
            figure_id="FIG-GEN-003",
            image_path=rejected,
            review_decision="human_approved",
            generation_status="model_rejected_or_revision_required",
        ),
        _generated_record(
            figure_id="FIG-GEN-004",
            image_path=cache_tmp / "missing.png",
            review_decision="human_approved",
        ),
    ]
    units, report = ingest_visual_candidates(
        records,
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert len(units) == 1
    assert units[0]["figure_identity"]["figure_label"] == "FIG-GEN-001"
    reasons = [error["reason"] for error in report["errors"]]
    assert "generated_visual_not_approved" in reasons
    assert any(
        reason.startswith("generated_visual_rejected_or_exhausted:")
        for reason in reasons
    )
    assert "image_path_unresolved" in reasons
    assert report["status"] == "degraded"


def test_generated_visual_snapshot_and_sqlite_round_trip(
    cache_tmp: Path,
) -> None:
    source = cache_tmp / "source"
    source.mkdir(parents=True)
    image = _generated_image(source / "gen.png")
    units, report = ingest_visual_candidates(
        [
            _generated_record(
                figure_id="FIG-GEN-001",
                image_path=image,
            )
        ],
        source_root=source,
        copy_assets_to=cache_tmp / "assets",
    )
    assert report["errors"] == []
    store = VisualCacheStore(cache_tmp / "cache")
    store.publish_snapshot(
        version="v1",
        units=units,
        assets_dir=cache_tmp / "assets",
        path_roots={"source": source},
    )

    payload = store.load_snapshot("v1")
    assert payload["unit_count"] == 1
    unit = payload["units"][0]
    assert unit["unit_kind"] == "generated_visual"
    assert unit["source_identity"]["source_kind"] == (
        "ai_generated_explanatory_visual"
    )

    conn = sqlite3.connect(store.root / "v1" / "visual_cache.sqlite")
    try:
        row = conn.execute(
            """
            SELECT unit_kind, source_kind, approval_state, lineage_json,
                   permission_state_json, caption_json
            FROM units WHERE unit_kind='generated_visual'
            """
        ).fetchone()
        assert row is not None
        assert row[0] == "generated_visual"
        assert row[1] == "ai_generated_explanatory_visual"
        assert row[2] == "approved"
        lineage = json.loads(row[3])
        assert lineage["generation_status"] == "ai_generated"
        assert lineage["generation"]["model_version"] == (
            "qwen-image-2.0-pro"
        )
        assert GENERATED_DISCLOSURE in lineage["generation"]["disclosure"]
        permission = json.loads(row[4])
        assert permission["evidence_ceiling"] == "explanatory_only"
        assert permission["empirical_evidence_allowed"] is False
        caption = json.loads(row[5])
        assert GENERATED_DISCLOSURE in caption["disclosure"]
    finally:
        conn.close()

    assert store.verify_snapshot("v1", path_roots={"source": source})[
        "status"
    ] == "passed"
