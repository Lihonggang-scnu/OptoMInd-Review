from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from PIL import Image

from optomind_research.runtime.article_visual_asset_planner import (
    ArticleVisualAssetPlannerConfig,
    plan_article_visual_assets,
)
from optomind_research.runtime.visual_asset_planner_adapter import (
    load_visual_cache_records,
)
from optomind_research.runtime.visual_cache_ingest import (
    ingest_visual_candidates,
)
from optomind_research.runtime.visual_cache_store import VisualCacheStore
from optomind_research.runtime.visual_source_contracts import (
    build_figure_contract,
    build_visual_source_map,
    validate_figure_contract,
    validate_visual_source_map,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def source_tmp() -> Path:
    """Workspace-local scratch dir; avoids pytest's mode=0o700 tmp ACLs."""

    scratch = PROJECT_ROOT / ".codex-tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    root = scratch / f"visual-source-contracts-test-{uuid.uuid4().hex[:10]}"
    root.mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)
    try:
        scratch.rmdir()
    except OSError:
        pass


def _image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), color="white").save(path)
    return path


def _candidate(path: Path) -> dict:
    return {
        "schema_version": "visual_asset.v1.1",
        "chunk_id": "paper-1:fig-2:a",
        "asset_id": "paper-1:fig-2",
        "paper_id": "paper-1",
        "doi": "10.1000/source-map",
        "title": "A traceable visual paper",
        "figure_label": "Figure 2",
        "subfigure_label": "a",
        "local_image_path": str(path),
        "caption_original": (
            "Figure 2. Measured response under the stated test condition."
        ),
        "caption_clean": (
            "Measured response under the stated test condition."
        ),
        "caption_confidence": "high",
        "semantic_description": "Measured response and its operating range.",
        "body_callout_texts": ["Figure 2 shows the measured response."],
        "linked_text_chunk_ids": ["paper-1:text:17"],
        "visual_argument_type": "quantitative_comparison",
        "visual_argument_claim": "The response changes under the test condition.",
        "visual_argument_confidence": "high",
        "review_utility": "high",
        "review_decision": "human_approved",
        "visual_argument_status": "ok",
        "use_permission": "factual_support",
        "allowed_claim_kinds": ["comparison"],
        "license": "cc-by-4.0",
        "source_provenance": {
            "source_format": "publisher_html",
            "parser": "html_dom",
            "page": 4,
            "source_url": "https://example.invalid/paper-1",
        },
    }


def test_source_map_ids_are_stable_and_links_are_bidirectional() -> None:
    kwargs = {
        "unit_id": "unit:visual:one",
        "source_identity": {
            "paper_id": "paper-1",
            "doi": "10.1000/source-map",
        },
        "figure_identity": {
            "asset_id": "paper-1:fig-2",
            "figure_label": "Figure 2",
            "subfigure_label": "a",
            "page": 4,
        },
        "caption": {
            "original": "Figure 2. Measured response.",
            "clean": "Measured response.",
            "confidence": "high",
        },
        "semantic": {
            "linked_text_chunk_ids": ["paper-1:text:17"],
            "body_callout_texts": ["As shown in Figure 2."],
        },
        "provenance": {"page": 4},
        "paths": {
            "image_ref": {"root": "snapshot", "relative": "assets/x.png"}
        },
    }
    first = build_visual_source_map(**kwargs)
    repeated = build_visual_source_map(**kwargs)
    assert first == repeated
    assert validate_visual_source_map(first) == []
    assert {node["node_type"] for node in first["nodes"]} == {
        "figure",
        "caption",
        "text_chunk",
        "body_callout",
    }
    assert all(link["bidirectional_lookup"] for link in first["links"])
    assert all(link["inverse_relation"] for link in first["links"])


def test_ingest_publishes_source_map_and_bidirectional_sqlite_index(
    source_tmp: Path,
) -> None:
    source = source_tmp / "source"
    image = _image(source / "figure.png")
    units, report = ingest_visual_candidates(
        [_candidate(image)],
        source_root=source,
        copy_assets_to=source_tmp / "staging",
    )
    assert report["errors"] == []
    assert len(units) == 1
    unit = units[0]
    source_map = unit["source_map"]
    assert source_map["unit_id"] == unit["unit_id"]
    assert validate_visual_source_map(source_map) == []

    store = VisualCacheStore(source_tmp / "cache")
    store.publish_snapshot(
        version="v1",
        units=units,
        assets_dir=source_tmp / "staging",
    )
    database = store.snapshot_path("v1") / "visual_cache.sqlite"
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT COUNT(*) FROM source_nodes").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM source_links").fetchone()[0] == 3
    finally:
        conn.close()

    caption_id = source_map["caption_node_ids"][0]
    figure_id = source_map["root_visual_node_id"]
    from_caption = store.related_source_nodes(
        "v1", caption_id, relation="caption_of"
    )
    from_figure = store.related_source_nodes(
        "v1", figure_id, relation="caption_of"
    )
    assert from_caption[0]["related_node"]["node_type"] == "figure"
    assert from_figure[0]["related_node"]["node_type"] == "caption"


def test_figure_contract_keeps_source_and_editorial_captions_separate() -> None:
    contract = build_figure_contract(
        contract_id="FC-1",
        section_id="S01",
        figure_kind="source_figure",
        argumentative_purpose="Compare the two operating regimes.",
        claim_bindings=[
            {"claim_id": "C1", "binding_type": "direct"},
            {"claim_id": "C2", "binding_type": "contextual"},
        ],
        source_caption="Figure 2. Original source caption.",
        editorial_caption="Comparison of the operating regimes.",
        attribution={"paper_id": "paper-1", "doi": "10.1000/source-map"},
        permission={"status": "allowed", "use_permission": "factual_support"},
        review_state="verified_existing",
    )
    assert validate_figure_contract(contract) == []
    assert contract["caption_contract"]["source_caption"] != contract[
        "caption_contract"
    ]["editorial_caption"]
    assert contract["evidence_layers"]["primary_claim_ids"] == ["C1"]
    assert contract["evidence_layers"]["supporting_claim_ids"] == ["C2"]
    assert contract["review"]["fail_open_for_optional_fields"] is True


def test_lightweight_tournament_adds_traceability_without_changing_selection(
    source_tmp: Path,
) -> None:
    image = _image(source_tmp / "candidate.png")
    record = {
        **_candidate(image),
        "caption": "Measured response under the stated test condition.",
        "permission": {"status": "allowed", "license": "cc-by-4.0"},
        "supporting_claim_ids": ["CL-1"],
    }
    section = {
        "section_id": "S01",
        "title": "Measured response",
        "text": "The measured response changes under the test condition.",
        "argument_role": "Compare measured response regimes.",
        "claims": [
            {
                "claim_id": "CL-1",
                "statement": "The response changes under the test condition.",
                "status": "approved",
            }
        ],
        "expected_visual_arguments": ["quantitative_comparison"],
    }
    baseline = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[record],
        config=ArticleVisualAssetPlannerConfig(
            emit_source_maps=False,
            emit_figure_contracts=False,
            separate_caption_attribution=False,
        ),
    )
    enhanced = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[record],
        config=ArticleVisualAssetPlannerConfig(),
    )
    baseline_placement = baseline["placements"][0]
    enhanced_placement = enhanced["placements"][0]
    assert baseline_placement["visual_chunk_id"] == enhanced_placement[
        "visual_chunk_id"
    ]
    assert "Source:" in baseline_placement["caption_proposal"]
    assert "Source:" not in enhanced_placement["caption_proposal"]
    assert enhanced_placement["source_attribution"]["paper_id"] == "paper-1"
    assert validate_visual_source_map(enhanced_placement["source_map"]) == []
    assert validate_figure_contract(enhanced_placement["figure_contract"]) == []
    assert enhanced["validation"]["status"] == "passed"


def test_snapshot_adaptation_preserves_original_caption_and_figure_label(
    source_tmp: Path,
) -> None:
    """Old snapshots must keep caption.original and figure_label explicitly.

    The source caption written into the placement and Figure Contract must be
    the original caption when present, while the clean caption stays the
    editorial/preview caption and selection is unchanged.
    """

    original = (
        "Figure 2. Original source caption with full detail and "
        "measurement context."
    )
    clean = "Clean source caption for search and preview."
    snapshot = source_tmp / "snapshot"
    image = _image(snapshot / "assets" / "orig.png")
    unit = {
        "unit_id": "unit:visual:orig-caption",
        "schema_version": "optomind.visual_unit.v1",
        "unit_kind": "single_figure",
        "unit_role": "review_asset",
        "caption": {
            "original": original,
            "clean": clean,
            "subfigure_focus": "",
            "confidence": "high",
        },
        "source_identity": {
            "paper_id": "paper-orig",
            "doi": "10.1000/orig",
            "title": "Original caption paper",
        },
        "figure_identity": {
            "asset_id": "paper-orig:fig-2",
            "figure_label": "Fig. 2",
            "parent_label": "PARENT-LABEL-FALLBACK",
            "subfigure_label": "",
            "page": 3,
        },
        "semantic": {
            "tags": [],
            "linked_text_chunk_ids": [],
            "body_callout_texts": [],
        },
        "provenance": {},
        "permission_state": {"use_permission": "discovery_only"},
        "approval": {"state": "pending", "source_marker": ""},
        "argumentative_roles": {
            "primary": "mechanism_anchor",
            "confidence": "high",
            "claim": "Mechanism schematic claim.",
        },
        "paths": {
            "image_ref": {
                "root": "snapshot",
                "relative": "assets/orig.png",
            }
        },
        "review": {},
        "quality": {},
    }
    (snapshot / "units.json").write_text(
        json.dumps(
            {"units": [unit]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    records = load_visual_cache_records(snapshot)
    assert len(records) == 1
    record = records[0]
    assert record["caption_original"] == original
    assert record["caption"] == clean
    assert record["caption_preview"] == clean
    assert record["figure_label"] == "Fig. 2"

    section = {
        "section_id": "S01",
        "title": "Mechanism schematic",
        "text": (
            "The mechanism schematic is described in the source figure."
        ),
        "argument_role": "Explain the mechanism schematic.",
        "claims": [
            {
                "claim_id": "CL-1",
                "statement": (
                    "The mechanism schematic is described in the source "
                    "figure."
                ),
                "status": "approved",
            }
        ],
        "expected_visual_arguments": ["mechanism_anchor"],
    }
    enhanced = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
        config=ArticleVisualAssetPlannerConfig(),
    )
    baseline = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
        config=ArticleVisualAssetPlannerConfig(
            emit_source_maps=False,
            emit_figure_contracts=False,
            separate_caption_attribution=False,
        ),
    )
    assert len(enhanced["placements"]) == 1
    placement = enhanced["placements"][0]
    assert placement["visual_chunk_id"] == baseline["placements"][0][
        "visual_chunk_id"
    ]
    # Original caption flows into the placement and the Figure Contract.
    assert placement["source_caption"] == original
    assert (
        placement["figure_contract"]["caption_contract"]["source_caption"]
        == original
    )
    # Editorial caption and preview still use the clean caption.
    assert placement["caption_preview"] == clean
    assert placement["caption_proposal"] == "Mechanism schematic claim."
    assert original not in placement["caption_proposal"]
    assert placement["source_attribution"]["figure_label"] == "Fig. 2"
    # Source Map caption node carries the original caption.
    caption_nodes = [
        node
        for node in placement["source_map"]["nodes"]
        if node["node_type"] == "caption"
    ]
    assert len(caption_nodes) == 1
    assert caption_nodes[0]["content"] == original
    assert validate_visual_source_map(placement["source_map"]) == []
    assert validate_figure_contract(placement["figure_contract"]) == []


def test_figure_contract_carries_asset_kind_and_table_flag() -> None:
    contract = build_figure_contract(
        contract_id="FC-T1",
        section_id="S01",
        figure_kind="source_table",
        asset_kind="table",
        argumentative_purpose="Summarize measured cooling performance.",
        source_caption="Table 1. Original caption.",
        editorial_caption="Cooling performance summary.",
    )
    assert validate_figure_contract(contract) == []
    assert contract["figure_kind"] == "source_table"
    assert contract["asset_kind"] == "table"
    assert contract["is_table"] is True
    assert contract["panel_map"][0]["visual_form"] == "source_table"

    # Old-style contracts without asset_kind stay valid and default to figure.
    legacy = build_figure_contract(
        contract_id="FC-L1",
        section_id="S01",
        figure_kind="source_figure",
        argumentative_purpose="Explain the mechanism.",
    )
    assert validate_figure_contract(legacy) == []
    assert legacy["asset_kind"] == "figure"
    assert legacy["is_table"] is False


def test_source_map_marks_table_node_and_asset_kind() -> None:
    source_map = build_visual_source_map(
        unit_id="unit:visual:table-1",
        source_identity={"paper_id": "paper-1", "doi": "10.1/table"},
        figure_identity={
            "asset_id": "paper-1:table-1",
            "asset_kind": "table",
            "figure_label": "Table 1",
        },
        caption={
            "original": "Table 1. Summary.",
            "clean": "Summary.",
        },
        semantic={},
        provenance={},
        paths={"image_ref": {"root": "snapshot", "relative": "assets/t.png"}},
    )
    assert validate_visual_source_map(source_map) == []
    assert source_map["asset_kind"] == "table"
    root = next(
        node
        for node in source_map["nodes"]
        if node["node_id"] == source_map["root_visual_node_id"]
    )
    assert root["node_type"] == "table"
    assert root["asset_kind"] == "table"
