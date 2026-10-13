"""Regression coverage for source-structured material handoff."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3.local_materials import (
    LocalTeiMaterialProvider,
    PreparedSnapshot,
    PreparedSnapshotProvider,
)
from optomind_research.runtime.upgrade3.document_snapshot import validate_snapshot
from optomind_research.runtime.upgrade3.material_reading import (
    build_reading_packet,
    normalize_reading_result,
    project_docling_asset_for_reading,
)
from optomind_research.runtime.upgrade3.paper_reading_card import (
    build_card_input,
    build_card_messages,
    load_plan,
)
from optomind_research.runtime.upgrade3.directed_reading import build_prompt_source_packet
from optomind_research.runtime.upgrade3.docling_adapter import build_source_specs, canonical_layout
from optomind_research.runtime.latex_publication_renderer import _find_binary
from optomind_research.runtime.upgrade3.practical_materials import (
    _formula_asset_map,
    _project_formula_view,
    load_practical_material,
)


REAL_JATS_SNAPSHOT = Path(os.environ["OPTOMIND_JATS_FIXTURE_SNAPSHOT"]) if os.environ.get("OPTOMIND_JATS_FIXTURE_SNAPSHOT") else None
REAL_JATS_PLAN = Path(os.environ["OPTOMIND_JATS_FIXTURE_PLAN"]) if os.environ.get("OPTOMIND_JATS_FIXTURE_PLAN") else None
REAL_TABLE_SNAPSHOT = Path(os.environ["OPTOMIND_TABLE_FIXTURE_SNAPSHOT"]) if os.environ.get("OPTOMIND_TABLE_FIXTURE_SNAPSHOT") else None
HISTORICAL_V4_SNAPSHOT = Path(os.environ["OPTOMIND_HISTORICAL_V4_SNAPSHOT"]) if os.environ.get("OPTOMIND_HISTORICAL_V4_SNAPSHOT") else REAL_JATS_SNAPSHOT


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _manifest(*, depth: str = "fulltext") -> dict:
    return {
        "snapshot_id": "snapshot-synthetic-structured",
        "canonical_paper_id": "synthetic-paper",
        "publication_version": "v1",
        "producer_fulltext_claim": {"claimed": depth == "fulltext", "content_depth": depth},
        "identity_check": {
            "status": "verified",
            "provided": {"canonical_paper_id": "synthetic-paper", "title": "Structured source test", "year": "2026"},
            "observed": {"canonical_paper_id": "synthetic-paper", "title": "Structured source test", "year": "2026"},
        },
        "source_documents": [
            {
                "source_document_id": "main",
                "role": "main",
                "source_name": "synthetic.xml",
                "raw_path": "sources/main.xml",
                "raw_sha256": "raw-source-sha",
            }
        ],
        "known_gaps": [],
    }


def _body_blocks() -> tuple[dict, ...]:
    return (
        {
            "block_id": "block-body",
            "source_document_id": "main",
            "block_type": "paragraph",
            "section_path": ["Results", "Conditions"],
            "locator": {"xml_path": "/article/body[1]/sec[1]/p[1]"},
            "text_normalized": "The design condition is 500 nm and the measured efficiency is 0.95.",
            "text_hash": _hash("The design condition is 500 nm and the measured efficiency is 0.95."),
            "asset_refs": ["formula-1", "table-1", "figure-1"],
            "research_content": True,
        },
        {
            "block_id": "block-caption",
            "source_document_id": "main",
            "block_type": "table_caption",
            "section_path": ["Results", "Conditions"],
            "locator": {"xml_path": "/article/body[1]/sec[1]/table-wrap[1]/caption[1]"},
            "text_normalized": "Table 1. Efficiency under the design condition.",
            "text_hash": _hash("Table 1. Efficiency under the design condition."),
            "asset_refs": ["table-1"],
            "research_content": True,
        },
        {
            "block_id": "cell-header",
            "source_document_id": "main",
            "block_type": "table_cell",
            "section_path": ["Results", "Conditions"],
            "locator": {"xml_path": "/article/body[1]/sec[1]/table-wrap[1]/table[1]/tr[1]/th[1]"},
            "text_normalized": "Wavelength (nm)",
            "text_hash": _hash("Wavelength (nm)"),
            "asset_refs": ["table-1"],
            "research_content": True,
        },
        {
            "block_id": "cell-value",
            "source_document_id": "main",
            "block_type": "table_cell",
            "section_path": ["Results", "Conditions"],
            "locator": {
                "xml_path": "/article/body[1]/sec[1]/table-wrap[1]/table[1]/tr[2]/td[1]",
                "row_index": 1,
                "col_index": 0,
                "row_span": 1,
                "col_span": 2,
                "is_header": False,
            },
            "text_normalized": "500",
            "text_hash": _hash("500"),
            "asset_refs": ["table-1"],
            "research_content": True,
        },
    )


def _assets() -> tuple[dict, ...]:
    return (
        {
            "asset_id": "formula-1",
            "type": "formula",
            "source_document_id": "main",
            "locator": {"xml_path": "/article/body[1]/sec[1]/p[1]/disp-formula[1]"},
            "content_status": "available",
            "parsed_successfully": True,
            "raw_source": "<disp-formula><math><mfrac><mi>a</mi><mi>b</mi></mfrac><msqrt><mi>r</mi></msqrt><msub><mi>F</mi><mi>0</mi></msub></math></disp-formula>",
            "content_ref": {"source_form": "mathml", "raw_source": "<unused>"},
        },
        {
            "asset_id": "table-1",
            "type": "table",
            "source_document_id": "main",
            "locator": {"xml_path": "/article/body[1]/sec[1]/table-wrap[1]"},
            "caption_block_ids": ["block-caption"],
            "content_status": "available",
            "parsed_successfully": True,
            "table_structure": {
                "rows": [
                    {
                        "block_id": "cell-header",
                        "locator": {"row_index": 0, "col_index": 0, "row_span": 1, "col_span": 1, "is_header": True},
                        "text": "Wavelength (nm)",
                    },
                    {
                        "block_id": "cell-value",
                        "locator": {"row_index": 1, "col_index": 0, "row_span": 1, "col_span": 2, "is_header": False},
                        "text": "500",
                    },
                ],
                "columns": [
                    {"col_index": 0, "header_block_ids": ["cell-header"], "header_labels": ["Wavelength (nm)"]}
                ],
                "footnotes": [{"block_id": "footnote-1", "text": "Measured at normal incidence."}],
            },
        },
        {
            "asset_id": "figure-1",
            "type": "figure",
            "source_document_id": "main",
            "locator": {"xml_path": "/article/body[1]/sec[1]/fig[1]"},
            "content_status": "available",
            "parsed_successfully": True,
        },
        {
            "asset_id": "front-formula",
            "type": "formula",
            "source_document_id": "main",
            "locator": {"xml_path": "/article/front/article-meta/abstract[1]/p[1]/inline-formula[1]"},
            "content_status": "available",
            "parsed_successfully": True,
            "raw_source": "<inline-formula><math><msqrt><mi>front</mi></msqrt></math></inline-formula>",
        },
    )


def _synthetic_snapshot(tmp_path: Path) -> PreparedSnapshot:
    return PreparedSnapshot(
        root=tmp_path,
        manifest=_manifest(),
        blocks=_body_blocks(),
        assets=_assets(),
        references=(),
        reading_view="",
    )


def _plan() -> dict:
    return {"question_en": "How does the design perform?", "facets": []}


def test_structured_assets_reach_all_card_message_profiles_and_quotes_are_grounded(tmp_path):
    snapshot = _synthetic_snapshot(tmp_path)
    packet = snapshot.build_reading_packet()
    asset_observations = {
        row["source"]["asset_id"]: row
        for row in packet["observations"]
        if row.get("source_kind") in {"article_formula", "article_table"}
    }
    assert set(asset_observations) == {"formula-1", "table-1"}
    assert "<mfrac>" in asset_observations["formula-1"]["text"]
    assert '"row_span":1' in asset_observations["table-1"]["text"]
    assert '"col_span":2' in asset_observations["table-1"]["text"]
    assert "Measured at normal incidence." in asset_observations["table-1"]["text"]

    normalized = normalize_reading_result(
        {
            "observations": [
                {
                    "source_observation_id": "asset:formula-1",
                    "kind": "method",
                    "quote": "<mfrac><mi>a</mi><mi>b</mi></mfrac>",
                    "summary": "Formula structure is retained.",
                },
                {
                    "source_observation_id": "asset:table-1",
                    "kind": "reported_finding",
                    "quote": "Wavelength (nm)",
                    "summary": "Table header is retained.",
                },
            ]
        },
        packet,
    )
    assert len(normalized["observations"]) == 2
    assert not normalized["rejected_observations"]

    card_input = build_card_input(snapshot, _plan())
    for profile, concise_output in (("standard", False), ("economy", False), ("economy", True)):
        messages = build_card_messages(card_input, input_profile=profile, concise_output=concise_output)
        user = messages[-1]["content"]
        assert "asset_id=formula-1" in user
        assert "<mfrac>" in user
        assert "asset_id=table-1" in user
        assert "Wavelength (nm)" in user
        assert "col_span" in user
        assert "asset_id=figure-1" not in user


def test_native_snapshot_builder_preserves_formula_and_table_geometry(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:m="http://www.w3.org/1998/Math/MathML">
  <front><article-meta><title-group><article-title>Native structured fixture</article-title></title-group><pub-date><year>2026</year></pub-date></article-meta></front>
  <body><sec><title>Results</title>
    <p>The condition is 500 nm. <inline-formula><m:math><m:mfrac><m:mi>a</m:mi><m:mi>b</m:mi></m:mfrac><m:msqrt><m:mi>r</m:mi></m:msqrt></m:math></inline-formula></p>
    <table-wrap id="T1"><label>Table 1</label><caption><p>Efficiency at the design condition.</p></caption>
      <table><tr><th>Wavelength (nm)</th><th>Efficiency</th></tr><tr><td colspan="2">0.95</td></tr></table>
      <table-wrap-foot><fn id="F1"><p>Measured at normal incidence.</p></fn></table-wrap-foot>
    </table-wrap>
  </sec></body>
</article>"""
    snapshot = LocalTeiMaterialProvider(tmp_path / "snapshots").build_from_bytes(
        xml.encode("utf-8"),
        canonical_paper_id="native-structured-fixture",
        source_name="synthetic.xml",
    )
    formulas = [asset for asset in snapshot.assets if asset.get("type") == "formula"]
    tables = [asset for asset in snapshot.assets if asset.get("type") == "table"]
    assert len(formulas) == 1
    assert len(tables) == 1
    assert "mfrac" in formulas[0]["raw_source"]
    assert tables[0]["table_structure"]["rows"]
    assert any(cell["locator"].get("col_span") == 2 for cell in tables[0]["table_structure"]["rows"])
    packet = snapshot.build_reading_packet()
    structured = {row["source"]["asset_id"]: row for row in packet["observations"] if row.get("source_kind") in {"article_formula", "article_table"}}
    assert set(structured) == {formulas[0]["asset_id"], tables[0]["asset_id"]}
    card_input = build_card_input(snapshot, _plan())
    for profile, concise_output in (("standard", False), ("economy", False), ("economy", True)):
        user = build_card_messages(card_input, input_profile=profile, concise_output=concise_output)[-1]["content"]
        assert formulas[0]["asset_id"] in user
        assert tables[0]["asset_id"] in user
        assert "mfrac" in user
        assert "col_span" in user

    # The practical writing view gets a readable, batched Pandoc projection;
    # the source snapshot and structured raw formula remain the evidence.
    original_view = (snapshot.root / "READING_VIEW.md").read_bytes()
    practical = load_practical_material(snapshot.root)
    if _find_binary("pandoc"):
        assert r"\frac" in practical["body"]
        assert r"\sqrt" in practical["body"]
    else:
        assert formulas[0]["content_ref"]["raw_source"] in practical["body"]
        assert "view_conversion=pandoc_unavailable" in practical["body"]
    assert formulas[0]["asset_id"] in practical["body"]
    raw_source = formulas[0]["content_ref"]["raw_source"]
    assert raw_source == formulas[0]["raw_source"]
    assert raw_source in practical["structured_material"]
    assert "Wavelength (nm)" in practical["body"]
    assert "Efficiency" in practical["body"]
    assert (snapshot.root / "READING_VIEW.md").read_bytes() == original_view


def test_formula_view_fallback_keeps_unquoted_raw_mathml(monkeypatch):
    raw = "<inline-formula><math><mfrac><mi>a</mi><mi>b</mi></mfrac><msqrt><mi>r</mi></msqrt></math></inline-formula>"
    view = "> [raw formula asset formula-1; representation=mathml]\n> ```xml\n" + raw + "\n> ```\n"
    monkeypatch.setattr(
        "optomind_research.runtime.latex_publication_renderer._find_binary",
        lambda _name: "",
    )
    projected, audit = _project_formula_view(view, _formula_asset_map(
        [{"asset_id": "formula-1", "type": "formula", "content_ref": {"raw_source": raw}}], view
    ))
    assert audit[0]["status"] == "raw_fallback"
    assert audit[0]["reason"] == "pandoc_unavailable"
    assert raw in projected
    assert "view_conversion=pandoc_unavailable" in projected


def test_native_nested_table_geometry_and_caption_formula_linking(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:m="http://www.w3.org/1998/Math/MathML">
  <front><article-meta><title-group><article-title>Nested structured fixture</article-title></title-group><pub-date><year>2026</year></pub-date></article-meta></front>
  <body><sec><title>Results</title><p>Introductory result text.</p>
    <table-wrap id="T1"><caption><p>Table caption with <inline-formula><m:math><m:mfrac><m:mi>x</m:mi><m:mi>y</m:mi></m:mfrac></m:math></inline-formula>.</p></caption>
      <table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>outer <table><caption><p>Inner table caption.</p></caption><tr><td>inner-one</td></tr><tr><td>inner-two</td></tr><table-wrap-foot><fn><p>Inner footnote with <inline-formula><m:math><m:msqrt><m:mi>z</m:mi></m:msqrt></m:math></inline-formula>.</p></fn></table-wrap-foot></table> tail</td></tr></table>
    </table-wrap>
  </sec></body>
</article>"""
    snapshot = LocalTeiMaterialProvider(tmp_path / "snapshots").build_from_bytes(
        xml.encode("utf-8"),
        canonical_paper_id="nested-structured-fixture",
        source_name="nested.xml",
    )
    tables = [asset for asset in snapshot.assets if asset.get("type") == "table"]
    table = next(asset for asset in tables if any(row["text"] == "A" for row in asset["table_structure"]["rows"]))
    nested_table = next(asset for asset in tables if asset["asset_id"] != table["asset_id"])
    formula = next(asset for asset in snapshot.assets if asset.get("type") == "formula")
    rows = table["table_structure"]["rows"]
    assert [row["text"] for row in rows[:3]] == ["A", "B", "1"]
    assert all(token in rows[3]["text"] for token in ("outer", "inner-one", "inner-two", "tail"))
    assert all("inner-one" not in row["text"] for row in rows[:3])
    assert [row["text"] for row in nested_table["table_structure"]["rows"]] == ["inner-one", "inner-two"]
    assert formula["caption_block_ids"]
    caption_id = formula["caption_block_ids"][0]
    assert caption_id in table["caption_block_ids"]
    caption = next(block for block in snapshot.blocks if block["block_id"] == caption_id)
    assert formula["asset_id"] in caption["asset_refs"]
    inner_caption = next(block for block in snapshot.blocks if block["block_type"] == "table_caption" and block["block_id"] != caption_id)
    inner_footnote = next(block for block in snapshot.blocks if block["block_type"] == "footnote")
    inner_table_id = nested_table["asset_id"]
    assert inner_table_id in inner_caption["asset_refs"]
    inner_formula = next(asset for asset in snapshot.assets if asset.get("type") == "formula" and asset["asset_id"] != formula["asset_id"])
    assert inner_formula["asset_id"] in inner_footnote["asset_refs"]
    assert "table_structure" not in inner_formula

    packet = snapshot.build_reading_packet()
    formula_observation = next(row for row in packet["observations"] if row["observation_id"] == f"asset:{formula['asset_id']}")
    assert "captions:" in formula_observation["text"]
    assert "mfrac" in formula_observation["text"]
    assert sum(row["observation_id"] == f"asset:{table['asset_id']}" for row in packet["observations"]) == 1
    # Table cells and the caption are represented by the structured table once;
    # retaining them as ordinary model observations would double-count them.
    structured_block_ids = {
        row["block_id"]
        for row in snapshot.blocks
        if row["block_type"] in {"table_cell", "table_caption", "formula"}
    }
    assert not any(
        row.get("source", {}).get("block_id") in structured_block_ids
        for row in packet["observations"]
        if row.get("observation_id", "").startswith("block:")
    )
    source_packet, _ = build_prompt_source_packet(snapshot)
    table_packet = next(row for row in source_packet["tables_by_rows"] if row["table_asset_id"] == table["asset_id"])
    caption_packet = table_packet["caption"][0]
    assert caption_packet["structured_assets"][0]["asset_id"] == formula["asset_id"]
    assert "mfrac" in caption_packet["structured_assets"][0]["raw_source"]
    nested_packet = next(row for row in source_packet["tables_by_rows"] if row["table_asset_id"] == inner_table_id)
    assert nested_packet["caption"]
    assert nested_packet["footnotes"]
    assert nested_packet["footnotes"][0]["structured_assets"][0]["asset_id"] == inner_formula["asset_id"]

    # Older snapshots may omit asset_refs on captions, cells, and notes.  The
    # immutable XML locator/caption IDs still recover the same raw structures.
    legacy_blocks = tuple(
        {
            **block,
            "asset_refs": []
            if block["block_type"] in {"table_cell", "table_caption", "footnote"}
            else list(block.get("asset_refs") or ()),
        }
        for block in snapshot.blocks
    )
    legacy_snapshot = PreparedSnapshot(
        root=snapshot.root,
        manifest=snapshot.manifest,
        blocks=legacy_blocks,
        assets=snapshot.assets,
        references=snapshot.references,
        reading_view=snapshot.reading_view,
    )
    legacy_packet, _ = build_prompt_source_packet(legacy_snapshot)
    legacy_nested = next(row for row in legacy_packet["tables_by_rows"] if row["table_asset_id"] == inner_table_id)
    assert legacy_nested["caption"]
    assert legacy_nested["footnotes"][0]["structured_assets"][0]["asset_id"] == inner_formula["asset_id"]


def test_partial_table_keeps_cell_and_caption_fallback_observations():
    blocks = _body_blocks()
    assets = [dict(asset) for asset in _assets()]
    table = next(asset for asset in assets if asset["asset_id"] == "table-1")
    table["table_structure"] = dict(table["table_structure"])
    table["table_structure"]["rows"] = table["table_structure"]["rows"][:1]
    packet = build_reading_packet(_manifest(), blocks, assets=assets)
    observation_ids = {row.get("observation_id") for row in packet["observations"]}
    assert "asset:table-1" in observation_ids
    assert "block:cell-value" in observation_ids
    assert "block:block-caption" in observation_ids


def test_abstract_scope_does_not_promote_front_matter_assets(tmp_path):
    manifest = _manifest(depth="abstract_only")
    blocks = (
        {
            "block_id": "abstract",
            "source_document_id": "main",
            "block_type": "paragraph",
            "section_path": ["Abstract"],
            "locator": {"xml_path": "/article/front/article-meta/abstract[1]/p[1]"},
            "text_normalized": "The abstract reports a design condition.",
            "text_hash": _hash("The abstract reports a design condition."),
            "asset_refs": ["formula-front"],
            "research_content": True,
        },
    )
    assets = (
        {
            "asset_id": "formula-front",
            "type": "formula",
            "source_document_id": "main",
            "locator": {"xml_path": "/article/front/article-meta/abstract[1]/p[1]/inline-formula[1]"},
            "content_status": "available",
            "parsed_successfully": True,
            "raw_source": "<inline-formula><math><msqrt><mi>r</mi></msqrt></math></inline-formula>",
        },
    )
    packet = build_reading_packet(manifest, blocks, assets=assets, root=tmp_path)
    assert packet["material_scope"] == "abstract_only"
    assert not any(row.get("observation_id") == "asset:formula-front" for row in packet["observations"])


def test_opt_in_docling_regions_reach_shared_consumers_with_provenance(tmp_path):
    xml = b"""<article><front><article-meta><article-title>Docling handoff</article-title><pub-date><year>2026</year></pub-date></article-meta></front><body><sec><title>Results</title><p>Native body anchor.</p></sec></body></article>"""
    layout = canonical_layout(
        [
            {
                "region_id": "table-p8-1",
                "type": "table",
                "page": 8,
                "bbox": [10, 20, 300, 400],
                "caption": {"text": "Table 1. Parser candidate."},
                "cells": [
                    {"row_index": 0, "col_index": 0, "text": "F / μm", "is_header": True},
                    {"row_index": 0, "col_index": 1, "text": "L", "is_header": True},
                    {"row_index": 1, "col_index": 0, "text": "1.0"},
                    {"row_index": 1, "col_index": 1, "text": "550 μm"},
                ],
            },
            {
                "region_id": "formula-p2-4",
                "type": "formula",
                "page": 2,
                "bbox": [12, 30, 180, 60],
                "text": r"\\Delta T_{max}=F c(\\sqrt{1+(R/F)^2}-1)",
            },
        ],
        pdf_sha256="pdf-source-sha",
        source_document_id="original-pdf",
        parser_version="2.138.0",
        config={"do_ocr": False, "do_table_structure": True},
        cache_key_value="cache-key-docling-test",
    )
    extras = build_source_specs(
        layout,
        source_document_id="docling-layout-test",
        derived_from="original-pdf",
        crops=[{"region_id": "table-p8-1", "source_name": "table-p8-1.png", "data": b"PNG-CROP"}],
    )
    snapshot = LocalTeiMaterialProvider(tmp_path / "snapshots").build_from_bytes(
        xml,
        canonical_paper_id="docling-handoff",
        source_name="native.xml",
        additional_sources=extras,
    )
    assert any(source.get("role") == "pdf_layout_parser" for source in snapshot.manifest["source_documents"])
    assert any(source.get("role") == "parser_audit" for source in snapshot.manifest["source_documents"])
    assert any(source.get("role") == "pdf_region_crop" for source in snapshot.manifest["source_documents"])
    parser_tables = [asset for asset in snapshot.assets if asset.get("parser") == "docling" and asset.get("type") == "table"]
    parser_formulas = [asset for asset in snapshot.assets if asset.get("parser") == "docling" and asset.get("type") == "formula"]
    assert len(parser_tables) == len(parser_formulas) == 1
    assert parser_tables[0]["locator"]["page"] == 8
    assert parser_tables[0]["fidelity_status"].startswith("parser_candidate")
    assert parser_formulas[0]["raw_source"]
    assert "pdf_layout_parser" in {source.get("role") for source in snapshot.manifest["source_documents"]}

    packet = snapshot.build_reading_packet()
    table_observations = [row for row in packet["observations"] if row.get("source", {}).get("asset_id") == parser_tables[0]["asset_id"]]
    formula_observations = [row for row in packet["observations"] if row.get("source", {}).get("asset_id") == parser_formulas[0]["asset_id"]]
    assert len(table_observations) == len(formula_observations) == 1
    assert "F / μm" in table_observations[0]["text"]
    assert parser_formulas[0]["raw_source"] in formula_observations[0]["text"]

    practical = load_practical_material(snapshot.root)
    assert "F / μm" in practical["body"]
    assert any(
        item.get("source", {}).get("asset_id") == parser_tables[0]["asset_id"]
        for item in practical.get("structured_assets") or ()
    )
    assert parser_formulas[0]["raw_source"] in practical.get("structured_material", "")
    source_packet, _ = build_prompt_source_packet(snapshot)
    docling_table = next(item for item in source_packet["tables_by_rows"] if item["table_asset_id"] == parser_tables[0]["asset_id"])
    assert any(cell["text"] == "550 μm" for row in docling_table["rows"] for cell in row["cells"])
    assert any(
        asset["asset_id"] == parser_formulas[0]["asset_id"]
        for block in source_packet["standalone_blocks"]
        for asset in block.get("structured_assets") or ()
    )


def test_docling_prompt_projection_is_lossless_and_keeps_unknown_science(tmp_path):
    asset = deepcopy(_assets()[1])
    asset.update(parser="docling", parser_version="2.test", recognition_status="uncertain",
                 recognition_provenance={"uncertain_regions": ["unit_not_verified"]})
    common = {"parser": "docling", "page": 8, "docling_prov": [{"bbox": [1, 2, 3, 4]}],
              "raw_path": "sources/recognized.json", "raw_document_json_pointer": "cell-audit"}
    asset["locator"].update(common, raw_document_json_pointer="table-audit")
    for i, cell in enumerate(asset["table_structure"]["rows"]):
        cell["locator"].update(common, source_json_pointer=f"/cells/{i}",
                               unknown_scientific_condition={"angle": "15 degrees"})
        cell["cell_id"] = f"cell-{i}"
        cell["unfamiliar_measurement"] = "0.95 ± 0.02"
    asset["table_structure"]["conditions"] = ["normal incidence; 500 nm"]
    asset["parser_provenance"] = {"region": {"cells": deepcopy(asset["table_structure"]["rows"])},
                                  "unknown_science": "efficiency denominator is incident power"}
    original = deepcopy(asset)
    projected = project_docling_asset_for_reading(asset)
    structure = projected["table_structure"]
    defaults = structure["cell_locator_defaults"]
    assert "inherit" in structure["cell_locator_inheritance"]
    assert defaults["docling_prov"] == common["docling_prov"]
    assert "raw_document_json_pointer" not in defaults  # asset locator disagrees
    assert "unknown_scientific_condition" not in defaults
    for original_cell, cell in zip(original["table_structure"]["rows"], structure["rows"]):
        restored = deepcopy(cell)
        restored["locator"] = {**defaults, **cell["locator"]}
        assert restored == original_cell
        assert "source_json_pointer" in cell["locator"]
    assert asset == original
    assert projected["parser_provenance"]["unknown_science"] == original["parser_provenance"]["unknown_science"]
    assert projected["parser_provenance"]["region"]["cells"]["audit_reference"]["asset_json_pointer"] == "/table_structure/rows"
    native = _assets()[1]
    assert project_docling_asset_for_reading(native) is native

    snapshot = PreparedSnapshot(root=tmp_path, manifest=_manifest(), blocks=_body_blocks(),
                                assets=(asset,), references=(), reading_view="")
    card_input = build_card_input(snapshot, _plan())
    for profile, concise in (("standard", False), ("economy", False), ("economy", True)):
        user = build_card_messages(card_input, input_profile=profile, concise_output=concise)[-1]["content"]
        for text in ("Wavelength (nm)", "500", "normal incidence", "unit_not_verified", "15 degrees", "0.95 ± 0.02"):
            assert text in user
        assert "cell_locator_defaults" in user
    packet, _ = build_prompt_source_packet(snapshot)
    table = packet["tables_by_rows"][0]
    assert "table_structure" not in table and "table_context" not in table
    assert [cell["text"] for row in table["rows"] for cell in row["cells"]] == ["Wavelength (nm)", "500"]
    assert table["parser_asset"]["parser_provenance"] == projected["parser_provenance"]


@pytest.mark.skipif(
    HISTORICAL_V4_SNAPSHOT is None or not HISTORICAL_V4_SNAPSHOT.is_dir(),
    reason="optional historical v4 snapshot is unavailable",
)
def test_historical_v4_snapshot_validation_replays_recorded_reading_view():
    assert HISTORICAL_V4_SNAPSHOT is not None
    manifest = json.loads((HISTORICAL_V4_SNAPSHOT / "DOCUMENT_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest.get("normalizer_version") == "optomind.xml_material_normalizer.v4"
    result = validate_snapshot(HISTORICAL_V4_SNAPSHOT)
    assert result["valid"] is True


@pytest.mark.skipif(
    REAL_JATS_SNAPSHOT is None or not REAL_JATS_SNAPSHOT.is_dir() or REAL_JATS_PLAN is None or not REAL_JATS_PLAN.is_file(),
    reason="optional local JATS fixture and plan are unavailable",
)
def test_real_jats_formula_assets_reach_standard_economy_and_concise_messages():
    assert REAL_JATS_SNAPSHOT is not None and REAL_JATS_PLAN is not None
    snapshot = PreparedSnapshotProvider(REAL_JATS_SNAPSHOT).load()
    card_input = build_card_input(
        snapshot,
        load_plan(REAL_JATS_PLAN),
    )
    formula_ids = [asset["asset_id"] for asset in snapshot.assets if asset.get("type") == "formula"]
    formula_observations = [
        row for row in card_input["reading_packet"]["observations"] if row.get("source_kind") == "article_formula"
    ]
    assert len(formula_ids) == 14
    assert len(formula_observations) == len(formula_ids)
    assert {row["source"]["asset_id"] for row in formula_observations} == set(formula_ids)

    for profile, concise in (("standard", False), ("economy", False), ("economy", True)):
        user = build_card_messages(card_input, input_profile=profile, concise_output=concise)[-1]["content"]
        assert all(asset_id in user for asset_id in formula_ids)
        assert "mfrac" in user
        assert "msqrt" in user
        assert "mtable" in user
        assert not any(
            asset.get("type") == "figure" and asset.get("asset_id") in user
            for asset in snapshot.assets
        )

    practical = load_practical_material(snapshot.root)
    practical_formula_ids = {
        item.get("source", {}).get("asset_id")
        for item in practical.get("structured_assets") or ()
        if item.get("source_kind") == "article_formula"
    }
    assert practical_formula_ids == set(formula_ids)
    assert all(
        asset.get("raw_source") in practical.get("structured_material", "")
        for asset in snapshot.assets
        if asset.get("type") == "formula" and asset.get("raw_source")
    )

    source_packet, _ = build_prompt_source_packet(snapshot)
    directed_formula_ids = set()
    for table in source_packet.get("tables_by_rows") or ():
        for caption in table.get("caption") or ():
            directed_formula_ids.update(asset.get("asset_id") for asset in caption.get("structured_assets") or ())
        for footnote in table.get("footnotes") or ():
            directed_formula_ids.update(asset.get("asset_id") for asset in footnote.get("structured_assets") or ())
        for row in table.get("rows") or ():
            for cell in row.get("cells") or ():
                directed_formula_ids.update(asset.get("asset_id") for asset in cell.get("structured_assets") or ())
    for block in source_packet.get("standalone_blocks") or ():
        directed_formula_ids.update(asset.get("asset_id") for asset in block.get("structured_assets") or ())
    assert directed_formula_ids == set(formula_ids)


@pytest.mark.skipif(
    REAL_TABLE_SNAPSHOT is None or not REAL_TABLE_SNAPSHOT.is_dir(),
    reason="optional local six-table fixture is unavailable",
)
def test_real_table_fixture_reaches_shared_card_practical_and_legacy_consumers():
    assert REAL_TABLE_SNAPSHOT is not None
    snapshot = PreparedSnapshotProvider(REAL_TABLE_SNAPSHOT).load()
    table_assets = [asset for asset in snapshot.assets if asset.get("type") == "table"]
    table_cells = [block for block in snapshot.blocks if block.get("block_type") == "table_cell"]
    assert len(table_assets) == 6
    assert len(table_cells) == 92

    packet = snapshot.build_reading_packet()
    packet_tables = [row for row in packet["observations"] if row.get("source_kind") == "article_table"]
    assert {row["source"]["asset_id"] for row in packet_tables} == {
        asset["asset_id"] for asset in table_assets
    }

    practical = load_practical_material(snapshot.root)
    practical_table_ids = {
        item.get("source", {}).get("asset_id")
        for item in practical.get("structured_assets") or ()
        if item.get("source_kind") == "article_table"
    }
    assert practical_table_ids == {asset["asset_id"] for asset in table_assets}

    source_packet, _ = build_prompt_source_packet(snapshot)
    assert len(source_packet.get("tables_by_rows") or ()) == 6
    assert sum(
        len(row.get("cells") or ())
        for table in source_packet.get("tables_by_rows") or ()
        for row in table.get("rows") or ()
    ) == 92
    assert any(table.get("caption") for table in source_packet.get("tables_by_rows") or ())
    # The fixture's fn is a publisher note under article/back/fn-group,
    # with no table ancestor or table asset link. Keep it standalone.
    standalone_ids = {block.get("block_id") for block in source_packet.get("standalone_blocks") or ()}
    footnote_ids = {block["block_id"] for block in snapshot.blocks if block.get("block_type") == "footnote" and not block.get("asset_refs")}
    assert footnote_ids and footnote_ids <= standalone_ids
