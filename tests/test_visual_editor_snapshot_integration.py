"""Visual Editor integration tests for snapshot cache formats and hygiene."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from PIL import Image

from optomind_research.runtime.visual_asset_planner_adapter import (
    load_visual_cache_records,
)
from optomind_research.runtime.visual_cache_store import (
    VisualCacheStore,
)
from optomind_research.runtime.visual_editor_runner import (
    visual_editor_input_fingerprint,
)
from optomind_research.runtime.visual_editor_tool_provider import (
    VisualEditorContext,
    VisualEditorToolProvider,
    validate_visual_editorial_plan_file,
)

_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp-visual-editor-tests"


@pytest.fixture()
def work_dir() -> Path:
    _TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEMP_ROOT / f"run-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _image(path: Path, color: str = "navy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 200), color=color).save(path)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unit(
    *,
    unit_id: str,
    image_name: str,
    hygiene_status: str,
    assets_dir: Path,
    derivative_name: str = "",
    original_name: str = "",
) -> dict:
    selected = assets_dir / image_name
    selected_sha = _sha256(selected)
    derivative = None
    original_ref = None
    crop: dict = {
        "bbox_px": [],
        "bbox_original_px": [],
        "bbox_padding_ratio": None,
        "overlay_ref": None,
        "crop_quality": {},
    }
    if hygiene_status == "derived_clean":
        derivative = {
            "filename": derivative_name,
            "relpath": f"assets/{derivative_name}",
            "sha256": _sha256(assets_dir / derivative_name),
            "parent_sha256": _sha256(assets_dir / original_name),
            "crop_bbox_px": [0, 0, 120, 90],
            "width": 120,
            "height": 90,
            "reason": "caption region removed",
        }
        original_ref = {
            "root": "snapshot",
            "relative": f"assets/{original_name}",
        }
        crop = {
            "crop_bbox_px": [0, 0, 120, 90],
            "parent_image_hash": _sha256(assets_dir / original_name),
            "parent_image_ref": original_ref,
            "derivative": derivative,
        }
    crop_hygiene = {
        "schema_version": "optomind.visual_crop_hygiene.v1",
        "status": hygiene_status,
        "source_kind": "pdf_pymupdf",
        "extraction_method": "pymupdf_caption_crop",
        "reason": "test fixture",
        "evidence": {},
        "derivative": derivative,
        "created_at": "",
    }
    return {
        "schema_version": "optomind.visual_unit.v1",
        "unit_id": unit_id,
        "unit_kind": "single_figure",
        "unit_role": "review_asset",
        "source_identity": {
            "paper_id": "hygiene-paper",
            "doi": "10.1/hygiene",
            "title": "Hygiene Paper",
        },
        "figure_identity": {
            "asset_id": unit_id,
            "figure_label": "Figure H",
            "subfigure_label": "",
            "parent_label": "",
            "subpanel_labels": [],
        },
        "caption": {
            "clean": "Optical resonance mechanism with field confinement.",
            "original": "",
            "subfigure_focus": "",
            "confidence": "high",
        },
        "semantic": {
            "description": (
                "Optical resonance mechanism with field confinement in a "
                "resonant cavity."
            ),
            "tags": ["mechanism", "resonance", "field-confinement"],
            "domain_hints": {},
            "visual_card": {},
            "visual_profile": {},
            "quality": {},
            "nearby_text": "",
            "body_callout_texts": [],
            "linked_text_chunk_ids": [],
        },
        "argumentative_roles": {
            "primary": "mechanism_anchor",
            "secondary": [],
            "claim": "Optical resonance mechanism",
            "supported_aspect": "",
            "basis": [],
            "confidence": "high",
            "needs_human_review": True,
            "review_utility": "high",
            "schema_version": "visual_argument_protocol.v1",
        },
        "provenance": {
            "schema_version": "visual_asset.v1.1",
            "extraction_method": "pymupdf_caption_crop",
            "source_format": "pdf_pymupdf",
            "parser": "pymupdf_caption_crop",
            "parser_version": "",
            "extraction_run_id": "",
            "source_url": "",
            "page": None,
            "checksum": "",
            "source_file_ref": None,
            "ingested_at": "",
        },
        "permission_state": {
            "use_permission": "factual_support",
            "allowed_claim_kinds": ["mechanism"],
            "license": "",
            "is_oa": None,
            "permission_source": "",
            "notes": [],
        },
        "hashes": {
            "image_sha256": selected_sha,
            "content_hash": "sha256:" + selected_sha,
            "record_sha256": "sha256:x",
            "source_checksum": "",
        },
        "vector_refs": {
            "schema_version": "optomind.visual_vector_refs.v1",
            "entries": [],
            "indexed": False,
        },
        "lineage": {
            "generation_status": "source_derived",
            "parent_unit_id": "",
            "parent_unavailable": False,
            "crop": crop,
            "enhancement_history": [],
            "generation": {},
        },
        "use_history": {
            "schema_version": "optomind.visual_use_history.v1",
            "used_in_run_ids": [],
            "citations": [],
            "last_used_at": "",
            "notes": [],
        },
        "review": {
            "review_decision": "",
            "visual_argument_status": "pending_multimodal_review",
            "human_review_status": "pending",
            "needs_human_review": True,
            "review_flags": [],
        },
        "approval": {
            "state": "pending",
            "source_marker": "",
            "approved_at": "",
            "approver": "",
            "note": "",
        },
        "crop_hygiene": crop_hygiene,
        "paths": {
            "image_ref": {
                "root": "snapshot",
                "relative": f"assets/{image_name}",
            },
            "original_image_ref": original_ref,
            "source_ref": None,
            "parent_image_ref": None,
            "overlay_ref": None,
        },
        "created_at": "",
    }


def _publish_hygiene_snapshot(work_dir: Path) -> Path:
    asset_root = work_dir / "assets" / "assets"
    asset_root.mkdir(parents=True)
    _image(asset_root / "clean.png", "navy")
    _image(asset_root / "orig.png", "red")
    _image(asset_root / "deriv.png", "blue")
    _image(asset_root / "review.png", "purple")
    _image(asset_root / "reject.png", "darkred")
    units = [
        _unit(
            unit_id="unit:visual:clean",
            image_name="clean.png",
            hygiene_status="clean",
            assets_dir=asset_root,
        ),
        _unit(
            unit_id="unit:visual:derived",
            image_name="deriv.png",
            hygiene_status="derived_clean",
            derivative_name="deriv.png",
            original_name="orig.png",
            assets_dir=asset_root,
        ),
        _unit(
            unit_id="unit:visual:review",
            image_name="review.png",
            hygiene_status="needs_review",
            assets_dir=asset_root,
        ),
        _unit(
            unit_id="unit:visual:reject",
            image_name="reject.png",
            hygiene_status="rejected",
            assets_dir=asset_root,
        ),
    ]
    store = VisualCacheStore(work_dir / "cache")
    store.publish_snapshot(
        version="v1",
        units=units,
        assets_dir=work_dir / "assets",
    )
    return store.root / "v1"


def _blueprint() -> dict:
    return {
        "review_thesis": "Optical resonance mechanism and sensing.",
        "sections": [
            {
                "section_id": "S01",
                "title": "Resonance mechanism",
                "argument_role": (
                    "Explain the optical resonance mechanism and field "
                    "confinement."
                ),
                "visual_argument_slots": [
                    {"purpose": "Explain the resonant mechanism."}
                ],
            }
        ],
    }


def _provider(
    work_dir: Path,
    cache_paths: list[Path],
) -> VisualEditorToolProvider:
    return VisualEditorToolProvider(
        VisualEditorContext(
            blueprint=_blueprint(),
            review_work_dir=work_dir / "review",
            work_dir=work_dir / "editor",
            kb_sqlite_paths=list(cache_paths),
            input_fingerprint="test-fingerprint",
        )
    )


def _tool(
    provider: VisualEditorToolProvider,
    work_dir: Path,
    name: str,
):
    return next(
        tool for tool in provider.get_tools(work_dir) if tool.name == name
    )


def test_no_circular_import_between_editor_and_adapter() -> None:
    import optomind_research.runtime.visual_asset_planner_adapter as adapter
    import optomind_research.runtime.visual_editor_tool_provider as provider

    assert (
        provider.load_visual_cache_records is adapter.load_visual_cache_records
    )


def test_editor_accepts_all_snapshot_formats_and_filters_hygiene(
    work_dir: Path,
) -> None:
    snapshot = _publish_hygiene_snapshot(work_dir)
    eligible = {"unit:visual:clean", "unit:visual:derived"}
    for cache_path in (
        snapshot,
        snapshot / "visual_cache.sqlite",
        snapshot / "units.json",
    ):
        provider = _provider(work_dir, [cache_path])
        visuals = provider._load_visuals()
        assert {visual["chunk_id"] for visual in visuals} == eligible
        candidates = provider._verified_candidates_for_section(
            "S01",
            top_k=4,
        )
        assert {candidate["chunk_id"] for candidate in candidates} == eligible
        inspect = _tool(
            provider,
            work_dir,
            "inspect_article_visual_candidates",
        )
        payload = json.loads(
            inspect._func(top_k_per_section=2, draft_excerpt_characters=220)
        )
        assert payload["status"] == "ok"
        assert payload["sections"][0]["candidate_count"] <= 2


def test_derived_clean_never_exposes_caption_region_original(
    work_dir: Path,
) -> None:
    snapshot = _publish_hygiene_snapshot(work_dir)
    records = load_visual_cache_records(snapshot)
    by_id = {record["chunk_id"]: record for record in records}
    derived = by_id["unit:visual:derived"]
    assert derived["crop_hygiene"]["status"] == "derived_clean"
    assert Path(derived["local_image_path"]).name == "deriv.png"
    assert Path(derived["original_image_path"]).name == "orig.png"
    assert derived["local_image_path"] != derived["original_image_path"]

    provider = _provider(work_dir, [snapshot])
    candidates = provider._verified_candidates_for_section("S01", top_k=4)
    compact = {candidate["chunk_id"]: candidate for candidate in candidates}
    assert compact["unit:visual:derived"]["local_image_path"] == (
        derived["local_image_path"]
    )
    assert compact["unit:visual:derived"]["local_image_path"] != (
        derived["original_image_path"]
    )
    assert all(
        candidate["local_image_path"] != derived["original_image_path"]
        for candidate in candidates
    )


def test_isolated_new_schema_sqlite_preserves_hygiene_and_paths(
    work_dir: Path,
) -> None:
    snapshot = _publish_hygiene_snapshot(work_dir)
    isolated = work_dir / "isolated"
    shutil.copytree(snapshot, isolated)
    (isolated / "units.json").unlink()
    (isolated / "manifest.json").unlink()
    records = load_visual_cache_records(isolated / "visual_cache.sqlite")
    by_id = {record["chunk_id"]: record for record in records}
    assert by_id["unit:visual:clean"]["crop_hygiene"]["status"] == "clean"
    derived = by_id["unit:visual:derived"]
    assert derived["crop_hygiene"]["status"] == "derived_clean"
    assert Path(derived["local_image_path"]).name == "deriv.png"
    assert Path(derived["local_image_path"]).is_file()
    assert Path(derived["original_image_path"]).name == "orig.png"
    assert Path(derived["original_image_path"]).is_file()
    provider = _provider(work_dir, [isolated / "visual_cache.sqlite"])
    visuals = provider._load_visuals()
    assert {visual["chunk_id"] for visual in visuals} == {
        "unit:visual:clean",
        "unit:visual:derived",
    }
    candidates = provider._verified_candidates_for_section("S01", top_k=4)
    assert {candidate["chunk_id"] for candidate in candidates} == {
        "unit:visual:clean",
        "unit:visual:derived",
    }


def test_legacy_sqlite_schema_without_new_columns_loads_conservatively(
    work_dir: Path,
) -> None:
    snapshot = _publish_hygiene_snapshot(work_dir)
    legacy_dir = work_dir / "legacy-isolated"
    legacy_dir.mkdir()
    shutil.copytree(snapshot / "assets", legacy_dir / "assets")
    new_conn = sqlite3.connect(snapshot / "visual_cache.sqlite")
    old_conn = sqlite3.connect(legacy_dir / "visual_cache.sqlite")
    old_columns = [
        "unit_id",
        "schema_version",
        "unit_kind",
        "unit_role",
        "source_identity_json",
        "figure_identity_json",
        "caption_json",
        "semantic_json",
        "argumentative_roles_json",
        "provenance_json",
        "permission_state_json",
        "hashes_json",
        "vector_refs_json",
        "lineage_json",
        "use_history_json",
        "review_json",
        "approval_json",
        "paths_json",
        "parent_unit_id",
        "image_relpath",
        "approval_state",
        "review_decision",
        "search_text",
        "created_at",
    ]
    try:
        old_conn.execute(
            """
            CREATE TABLE units(
              unit_id TEXT PRIMARY KEY,
              schema_version TEXT NOT NULL,
              unit_kind TEXT NOT NULL,
              unit_role TEXT NOT NULL,
              source_identity_json TEXT NOT NULL,
              figure_identity_json TEXT NOT NULL,
              caption_json TEXT NOT NULL,
              semantic_json TEXT NOT NULL,
              argumentative_roles_json TEXT NOT NULL,
              provenance_json TEXT NOT NULL,
              permission_state_json TEXT NOT NULL,
              hashes_json TEXT NOT NULL,
              vector_refs_json TEXT NOT NULL,
              lineage_json TEXT NOT NULL,
              use_history_json TEXT NOT NULL,
              review_json TEXT NOT NULL,
              approval_json TEXT NOT NULL,
              paths_json TEXT NOT NULL,
              parent_unit_id TEXT,
              image_relpath TEXT NOT NULL,
              approval_state TEXT NOT NULL,
              review_decision TEXT NOT NULL,
              search_text TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        selected = ", ".join(old_columns)
        placeholders = ", ".join("?" for _ in old_columns)
        rows = new_conn.execute(
            f"SELECT {selected} FROM units"
        ).fetchall()
        old_conn.executemany(
            f"INSERT INTO units({selected}) VALUES({placeholders})",
            rows,
        )
        old_conn.commit()
    finally:
        new_conn.close()
        old_conn.close()

    # Without a companion units.json, the old schema loads but the editor
    # fails closed: snapshot-format rows with no hygiene never shortlist.
    records = load_visual_cache_records(legacy_dir / "visual_cache.sqlite")
    assert records
    assert all(
        record["cache_format"] == "snapshot"
        and not record.get("crop_hygiene")
        for record in records
    )
    provider = _provider(work_dir, [legacy_dir / "visual_cache.sqlite"])
    assert provider._load_visuals() == []

    # With the companion payload, the old schema is enriched and usable.
    shutil.copy2(snapshot / "units.json", legacy_dir / "units.json")
    enriched = load_visual_cache_records(legacy_dir / "visual_cache.sqlite")
    assert all(
        record["crop_hygiene"]["status"]
        in {"clean", "derived_clean", "needs_review", "rejected"}
        for record in enriched
    )
    enriched_provider = _provider(
        work_dir,
        [legacy_dir / "visual_cache.sqlite"],
    )
    assert {
        visual["chunk_id"] for visual in enriched_provider._load_visuals()
    } == {
        "unit:visual:clean",
        "unit:visual:derived",
    }


def test_missing_and_unsafe_images_fail_open_as_unfilled_need(
    work_dir: Path,
) -> None:
    snapshot = _publish_hygiene_snapshot(work_dir)
    (snapshot / "assets" / "clean.png").unlink()
    (snapshot / "assets" / "deriv.png").unlink()
    provider = _provider(work_dir, [snapshot])
    assert provider._verified_candidates_for_section("S01", top_k=4) == []

    submit = _tool(provider, work_dir, "submit_visual_editorial_plan")
    result = json.loads(
        submit._func(
            json.dumps(
                {
                    "placements": [],
                    "conceptual_figure_requests": [],
                    "unfilled_visual_needs": [],
                }
            )
        )
    )
    assert result["status"] == "ok"
    assert result["placement_count"] == 0
    validation = validate_visual_editorial_plan_file(
        provider.plan_path,
        provider.ctx.input_fingerprint,
        provider._expected_visual_section_ids(),
    )
    assert validation.startswith("VALIDATION_PASSED")
    plan = json.loads(provider.plan_path.read_text(encoding="utf-8"))
    assert any(
        item["section_id"] == "S01"
        for item in plan["unfilled_visual_needs"]
    )


def test_legacy_visual_chunks_sqlite_still_supported(work_dir: Path) -> None:
    image_path = _image(work_dir / "legacy.png", "teal")
    kb_path = work_dir / "legacy.sqlite"
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

    provider = _provider(work_dir, [kb_path])
    visuals = provider._load_visuals()
    assert len(visuals) == 1
    assert visuals[0]["chunk_id"] == "legacy-vis-1"
    candidates = provider._verified_candidates_for_section("S01", top_k=4)
    assert [candidate["chunk_id"] for candidate in candidates] == [
        "legacy-vis-1"
    ]


def test_fingerprint_tracks_snapshot_directory_contents(work_dir: Path) -> None:
    snapshot = _publish_hygiene_snapshot(work_dir)
    common = {
        "blueprint": _blueprint(),
        "review_work_dir": work_dir / "review",
        "kb_sqlite_paths": [snapshot],
        "role_prompt": "role",
    }
    first = visual_editor_input_fingerprint(**common)
    repeated = visual_editor_input_fingerprint(**common)
    assert first == repeated
    asset = next((snapshot / "assets").glob("*.png"))
    asset.write_bytes(asset.read_bytes() + b"\x00")
    changed = visual_editor_input_fingerprint(**common)
    assert changed != first
