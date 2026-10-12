"""Offline checks for native Docling input, crop binding and acquisition reuse."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3.docling_adapter import (
    DoclingAdapterConfig, DoclingAdapterError, DoclingCliAdapter, config_sha256,
)
from optomind_research.runtime.upgrade3.material_acquisition import AcquisitionConfig, MaterialAcquirer
from optomind_research.runtime.upgrade3.practical_materials import load_practical_material
from optomind_research.runtime.upgrade3.directed_reading import build_prompt_source_packet


def _fixture(tmp_path):
    fitz = pytest.importorskip("fitz")
    with fitz.open() as pdf:
        page = pdf.new_page(width=300, height=400)
        page.insert_textbox(fitz.Rect(20, 20, 280, 370), "An offline body source with readable scientific material. " * 12, fontsize=10)
        page = pdf.new_page(width=300, height=400)
        page.insert_text((20, 30), "Header Value")
        page.insert_text((20, 50), "Design 500")
        page.insert_text((20, 110), "Table caption below the table.")
        page.insert_text((20, 140), "Table footnote below the caption.")
        data = pdf.tobytes()
    def location(page, box):
        return [{"page_no": page, "bbox": dict(zip(("l", "t", "r", "b"), box), coord_origin="TOPLEFT"), "charspan": [0, 20]}]
    texts = [
        {"self_ref": "#/texts/0", "label": "text", "text": "Recognized body text", "prov": location(1, [20, 20, 280, 200])},
        {"self_ref": "#/texts/1", "label": "caption", "text": "Table caption below the table.", "prov": location(2, [20, 95, 280, 115])},
        {"self_ref": "#/texts/2", "label": "footnote", "text": "Table footnote below the caption.", "prov": location(2, [20, 125, 280, 145])},
        {"self_ref": "#/texts/3", "label": "formula", "text": "", "orig": "flattened sqrt formula", "prov": location(2, [20, 160, 200, 180])},
    ]
    table = {"self_ref": "#/tables/0", "label": "table", "prov": location(2, [20, 15, 280, 65]),
             "captions": [{"$ref": "#/texts/1"}], "footnotes": [{"$ref": "#/texts/2"}],
             "data": {"num_rows": 2, "num_cols": 2, "table_cells": [
                 {"text": text, "start_row_offset_idx": r, "start_col_offset_idx": c,
                  "row_span": 1, "col_span": 1, "column_header": r == 0,
                  "bbox": {"l": 20 + c*100, "t": 15 + r*20, "r": 120 + c*100, "b": 35+r*20, "coord_origin": "TOPLEFT"}}
                 for r, c, text in ((0, 0, "Header"), (0, 1, "Value"), (1, 0, "Design"), (1, 1, "500"))],
                      "grid": [[{"text": "MUST NOT BE IMPORTED"}]]}}
    document = {"schema_name": "DoclingDocument", "version": "1.10.0", "texts": texts, "tables": [table], "pictures": [], "groups": [],
                "body": {"self_ref": "#/body", "children": [{"$ref": "#/texts/0"}, {"$ref": "#/tables/0"}, {"$ref": "#/texts/3"}]},
                "pages": {str(p): {"page_no": p, "size": {"width": 300, "height": 400}} for p in (1, 2)}}
    run = {"status": "success", "source_sha256": hashlib.sha256(data).hexdigest(), "packages": {"docling": "2.138.0"},
           "pipeline_options": {"do_ocr": False, "do_formula_enrichment": False}, "page_range": [1, 2]}
    doc_path = tmp_path / "document.json"; run_path = tmp_path / "run.json"
    doc_path.write_text(json.dumps(document), encoding="utf-8"); run_path.write_text(json.dumps(run), encoding="utf-8")
    return data, document, run, DoclingAdapterConfig.from_saved_run(doc_path, run_path, cache_root=tmp_path / "cache")


def test_native_import_preserves_cells_caption_footnote_pixels_and_warm_cache(tmp_path):
    data, _, _, config = _fixture(tmp_path)
    adapter = DoclingCliAdapter(config)
    first = adapter.prepare(data, source_document_id="retained-pdf")
    assert first == adapter.prepare(data, source_document_id="retained-pdf")
    layout = json.loads(first[0]["data"])
    table, formula = layout["regions"]
    assert len(table["cells"]) == 4
    assert table["cells"][3]["row_index"] == table["cells"][3]["col_index"] == 1
    assert table["cells"][0]["is_header"]
    assert table["captions"][0]["bbox"][1] > table["bbox"][3]
    crops = {spec["source_document_id"]: spec for spec in first if spec["role"] == "pdf_region_crop"}
    assert len(crops) == 4  # region table/formula and linked caption/footnote
    for item in (table, formula, table["captions"][0], table["footnotes"][0]):
        crop = crops[item["crop_source_document_id"]]
        assert crop["provenance"]["crop"]["bbox"] == item["bbox"]
        assert crop["provenance"]["crop"]["scale"] == 2.5
    assert formula["recognition_status"] == "missing" and not formula["raw_source"]
    assert any(gap["kind"] == "formula" and gap["status"] == "recognition_required" for gap in layout["known_gaps"])
    assert any(spec["role"] == "pdf_layout_raw" for spec in first)
    assert "MUST NOT BE IMPORTED" not in first[0]["data"].decode()


@pytest.mark.parametrize("mismatch", ["source", "config", "version"])
def test_native_import_rejects_mismatched_binding(tmp_path, mismatch):
    data, _, _, config = _fixture(tmp_path)
    expected = "source_sha256" if mismatch == "source" else "run_config" if mismatch == "config" else "parser_version"
    if mismatch == "source": data += b"different-source"
    elif mismatch == "config": config = replace(config, config={**config.config, "page_range": [2, 2]})
    else: config = replace(config, parser_version="2.139.0")
    with pytest.raises(DoclingAdapterError, match=expected + "_mismatch"):
        DoclingCliAdapter(config).prepare(data)


def test_corrupt_cache_fails_without_repeating_parser(tmp_path):
    data, _, _, config = _fixture(tmp_path)
    adapter = DoclingCliAdapter(config)
    adapter.prepare(data)
    root = config.cache_path(hashlib.sha256(data).hexdigest())
    manifest = json.loads((root / "CACHE_MANIFEST.json").read_bytes())
    (root / manifest["entries"][0]["file"]).write_bytes(b"corrupt")
    with pytest.raises(DoclingAdapterError, match="cache_content_mismatch"):
        adapter.prepare(data)


def test_exact_cli_output_and_cache_do_not_choose_an_unrelated_json(tmp_path):
    data, document, run, saved = _fixture(tmp_path)
    calls = []
    config = replace(saved, document_path="", run_metadata_path="", command=("offline-runner", "{input}", "{output}"))
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command[2])
        (output / "aaa-unrelated.json").write_text("{}")
        (output / "document.json").write_text(json.dumps(document))
        (output / "run.json").write_text(json.dumps(run))
    adapter = DoclingCliAdapter(config, runner=runner)
    first = adapter.prepare(data)
    assert first == adapter.prepare(data)
    assert len(calls) == 1
    assert json.loads(first[0]["data"])["regions"]


def test_partial_cli_keeps_attempt_outputs_and_does_not_publish_cache(tmp_path):
    data, document, run, saved = _fixture(tmp_path)
    run["status"] = "partial"
    run["conversion_status"] = "partial_success"
    config = replace(saved, document_path="", run_metadata_path="", command=("offline-runner", "{input}", "{output}"))
    def runner(command, **kwargs):
        output = Path(command[2])
        (output / "document.json").write_text(json.dumps(document))
        (output / "run.json").write_text(json.dumps(run))
    with pytest.raises(DoclingAdapterError, match="run_not_successful.*attempt=") as error:
        DoclingCliAdapter(config, runner=runner).prepare(data)
    attempt = Path(str(error.value).split("attempt=", 1)[1])
    assert (attempt / "output/document.json").is_file()
    assert (attempt / "output/run.json").is_file()
    assert (attempt / "command.json").is_file()
    assert not (config.cache_path(hashlib.sha256(data).hexdigest()) / "CACHE_MANIFEST.json").exists()


def test_generic_runner_retains_partial_conversion_status_and_errors(tmp_path, monkeypatch):
    from scripts.upgrade3 import parse_docling_pdf as runner
    artifacts = tmp_path / "models"; artifacts.mkdir()
    options = {"artifacts_path": str(artifacts), "do_ocr": False, "do_formula_enrichment": False}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"packages": {}, "pipeline_options": options, "page_range": [1, 1]}))
    source = tmp_path / "input.pdf"; source.write_bytes(b"%PDF mock local input")
    class Pipeline:
        @classmethod
        def model_validate(cls, value): return cls()
        def model_dump(self, **kwargs): return options
    class Document:
        def export_to_dict(self): return {"schema_name": "DoclingDocument"}
        def num_pages(self): return 1
    class Converter:
        def __init__(self, **kwargs): pass
        def convert(self, *args, **kwargs):
            return types.SimpleNamespace(document=Document(), status=types.SimpleNamespace(value="partial_success"), errors=["page 1 failed"])
    for name in ("docling", "docling.datamodel", "docling.datamodel.base_models", "docling.datamodel.pipeline_options", "docling.document_converter"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["docling.datamodel.base_models"].InputFormat = types.SimpleNamespace(PDF="pdf")
    sys.modules["docling.datamodel.pipeline_options"].PdfPipelineOptions = Pipeline
    sys.modules["docling.document_converter"].DocumentConverter = Converter
    sys.modules["docling.document_converter"].PdfFormatOption = lambda **kwargs: kwargs
    output = tmp_path / "result"
    assert runner.main(["--input", str(source), "--output", str(output), "--config", str(config_path)]) == 1
    run = json.loads((output / "run.json").read_bytes())
    assert run["status"] == "partial" and run["conversion_status"] == "partial_success"
    assert run["errors"] == ["page 1 failed"]
    assert (output / "document.json").is_file()


def test_absent_recognized_body_and_formula_stay_explicit_gaps(tmp_path):
    data, document, run, config = _fixture(tmp_path)
    document["body"]["children"] = []
    specs = DoclingCliAdapter(replace(config, document_path="", run_metadata_path="")).prepare(data, layout=document, run_metadata=run)
    layout = json.loads(specs[0]["data"])
    assert not layout["regions"]
    assert {gap["kind"] for gap in layout["known_gaps"]} >= {"coverage", "formula"}
    assert layout["coverage"]["recognized_body_blocks"] == 0


def test_changed_configured_runner_content_changes_cache_identity(tmp_path):
    data, _, _, config = _fixture(tmp_path)
    runner = tmp_path / "runner.py"
    runner.write_text("# first local runner\n")
    config = replace(config, command=(str(runner), "{input}", "{output}"))
    first = config.cache_path(hashlib.sha256(data).hexdigest())
    runner.write_text("# corrected local runner\n")
    assert config.cache_path(hashlib.sha256(data).hexdigest()) != first


def test_known_engine_set_order_is_stable_without_ignoring_members_or_page_order():
    from scripts.upgrade3.parse_docling_pdf import _canonical
    first = {"supported_engines": ["transformers", "api_openai"], "page_range": [2, 8]}
    reordered = {"supported_engines": ["api_openai", "transformers"], "page_range": [2, 8]}
    assert config_sha256(first) == config_sha256(reordered)
    assert _canonical(first) == _canonical(reordered)
    assert config_sha256(first) != config_sha256({**first, "supported_engines": ["transformers"]})
    assert config_sha256(first) != config_sha256({**first, "page_range": [8, 2]})


def test_formal_acquisition_binds_actual_deduplicated_pdf_and_native_xml_skips_adapter(tmp_path):
    data, _, _, config = _fixture(tmp_path)
    providers = {key: None for key in ("grobid", "openalex", "semantic_scholar", "crossref", "unpaywall", "core", "arxiv")}
    adapter = DoclingCliAdapter(config)
    providers["docling"] = adapter
    acquirer = MaterialAcquirer(tmp_path / "snapshots", config=AcquisitionConfig(cache_root=tmp_path / "acquisition", docling_enabled=True), providers=providers)
    result = acquirer.acquire_local({"canonical_paper_id": "offline-bridge", "title": "Offline bridge"}, pdf_bytes=data)
    assert result.snapshot is not None
    snapshot = result.snapshot
    sources = {source["source_document_id"]: source for source in snapshot.manifest["source_documents"]}
    layout_source = next(source for source in sources.values() if source["role"] == "pdf_layout_parser")
    layout = json.loads((snapshot.root / layout_source["raw_path"]).read_bytes())
    assert layout["source"]["source_document_id"].startswith("artifact-")
    assert sources[layout["source"]["source_document_id"]]["raw_sha256"] == hashlib.sha256(data).hexdigest()
    table = next(asset for asset in snapshot.assets if asset.get("parser") == "docling" and asset["type"] == "table")
    assert len(table["table_structure"]["rows"]) == 4
    for kind in ("table_caption", "footnote"):
        block = next(block for block in snapshot.blocks if block["block_type"] == kind)
        locator = block["locator"]
        assert locator["bbox"][1] > table["locator"]["bbox"][3]
        assert hashlib.sha256((snapshot.root / locator["crop_raw_path"]).read_bytes()).hexdigest() == locator["crop_sha256"]
    assert "fidelity_status=parser_candidate" in load_practical_material(snapshot.root)["structured_material"]
    legacy, _ = build_prompt_source_packet(snapshot)
    legacy_table = next(item for item in legacy["tables_by_rows"] if item["table_asset_id"] == table["asset_id"])
    assert sum(len(row["cells"]) for row in legacy_table["rows"]) == 4
    class Forbidden:
        def prepare(self, *args, **kwargs): raise AssertionError("native XML must not invoke Docling")
    acquirer.providers["docling"] = Forbidden()
    xml = b"<article><body><p>Native XML source body.</p></body></article>"
    assert acquirer.acquire_local({"canonical_paper_id": "native"}, xml_bytes=xml, pdf_bytes=data).snapshot


REAL_TRIAL = Path(os.environ["OPTOMIND_DOCLING_TRIAL"]) if os.environ.get("OPTOMIND_DOCLING_TRIAL") else None


@pytest.mark.skipif(REAL_TRIAL is None, reason="optional real saved Docling trial")
def test_actual_docling_raw_import_cache_and_consumers(tmp_path):
    assert REAL_TRIAL is not None
    run = json.loads((REAL_TRIAL / "run.json").read_bytes())
    data = Path(run["source_pdf"]).read_bytes()
    config = DoclingAdapterConfig.from_saved_run(REAL_TRIAL / "document.json", REAL_TRIAL / "run.json", cache_root=tmp_path / "cache")
    adapter = DoclingCliAdapter(config)
    specs = adapter.prepare(data)
    assert specs == adapter.prepare(data)
    layout = json.loads(specs[0]["data"])
    assert sum(len(region["cells"]) for region in layout["regions"] if region["type"] == "table") == 319
    assert len([region for region in layout["regions"] if region["type"] == "figure"]) == 4
    assert len([region for region in layout["regions"] if region["type"] == "formula" and region["recognition_status"] == "missing"]) == 15
    table = next(region for region in layout["regions"] if region["type"] == "table")
    crop_by_id = {spec["source_document_id"]: spec for spec in specs if spec["role"] == "pdf_region_crop"}
    assert crop_by_id[table["crop_source_document_id"]]["provenance"]["crop"]["image_size"] == [1295, 1103]
    assert table["captions"][0]["bbox"][1] > table["bbox"][3]
    assert table["captions"][0]["crop_source_document_id"] in crop_by_id
    from optomind_research.runtime.upgrade3.paper_reading_card import build_card_input, build_card_messages, load_plan
    from optomind_research.runtime.upgrade3.practical_materials import build_practical_reader_messages
    from optomind_research.runtime.upgrade3.document_snapshot import validate_snapshot
    output = Path(os.environ.get("OPTOMIND_DOCLING_ACCEPTANCE_OUTPUT") or tmp_path / "acceptance")
    providers = {key: None for key in ("grobid", "openalex", "semantic_scholar", "crossref", "unpaywall", "core", "arxiv")}
    providers["docling"] = adapter
    result = MaterialAcquirer(output / "snapshots", config=AcquisitionConfig(cache_root=output / "acquisition-cache", docling_enabled=True),
                              providers=providers).acquire_local({"canonical_paper_id": "docling-bridge-offline", "title": "Docling bridge offline acceptance"}, pdf_bytes=data)
    assert result.snapshot is not None
    snapshot = result.snapshot
    assert validate_snapshot(snapshot.root)["valid"]
    parser_tables = [asset for asset in snapshot.assets if asset.get("parser") == "docling" and asset["type"] == "table"]
    assert len(parser_tables) == 1 and len(parser_tables[0]["table_structure"]["rows"]) == 319
    formulas = [asset for asset in snapshot.assets if asset.get("parser") == "docling" and asset["type"] == "formula"]
    assert len(formulas) == 15 and all(asset["parsed_successfully"] is False for asset in formulas)
    sources = {source["source_document_id"]: source for source in snapshot.manifest["source_documents"]}
    layout_source = next(source for source in sources.values() if source["role"] == "pdf_layout_parser")
    published = json.loads((snapshot.root / layout_source["raw_path"]).read_bytes())
    assert sources[published["source"]["source_document_id"]]["raw_sha256"] == run["source_sha256"]
    crops = [source for source in sources.values() if source["role"] == "pdf_region_crop"]
    assert len(crops) == 25  # 20 regions and 5 referenced captions
    for source in crops:
        assert hashlib.sha256((snapshot.root / source["raw_path"]).read_bytes()).hexdigest() == source["raw_sha256"]
    caption = next(block for block in snapshot.blocks if block["block_id"] in parser_tables[0]["caption_block_ids"])
    assert caption["locator"]["bbox"][1] > parser_tables[0]["locator"]["bbox"][3]
    assert caption["locator"]["crop_source_document_id"] in sources
    output.mkdir(parents=True, exist_ok=True)
    practical = load_practical_material(snapshot.root)
    assert "fidelity_status=parser_candidate" in practical["structured_material"]
    messages = build_practical_reader_messages(title="Offline acceptance", questions=[], required_outputs=[], material=practical)
    (output / "practical-messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    legacy, _ = build_prompt_source_packet(snapshot)
    legacy_table = next(item for item in legacy["tables_by_rows"] if item["table_asset_id"] == parser_tables[0]["asset_id"])
    assert sum(len(row["cells"]) for row in legacy_table["rows"]) == 319
    assert any("recognition_required" in block["text"] for block in legacy["standalone_blocks"])
    assert legacy_table["parser_asset"]["fidelity_status"].startswith("parser_candidate")
    assert legacy_table["parser_asset"]["provided_to_multimodal"] is False
    assert legacy_table["caption"][0]["locator"]["crop_source_document_id"] in sources
    (output / "legacy-packet.json").write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
    plan_path = os.environ.get("OPTOMIND_JATS_FIXTURE_PLAN")
    if plan_path:
        card = build_card_input(snapshot, load_plan(Path(plan_path)))
        for profile, concise in (("standard", False), ("economy", False), ("economy", True)):
            messages = build_card_messages(card, input_profile=profile, concise_output=concise)
            wire = json.dumps(messages, ensure_ascii=False)
            assert "fidelity_status=parser_candidate" in wire and "recognition_required" in wire
            (output / f"card-{profile}-{concise}.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {"snapshot_path": str(snapshot.root), "snapshot_id": snapshot.snapshot_id, "pdf_sha256": run["source_sha256"],
              "retained_pdf_source_document_id": published["source"]["source_document_id"], "cache_identical": specs == adapter.prepare(data),
              "table_cells": 319, "figures": 4, "formula_recognition_gaps": 15, "crop_count": len(crops),
              "caption_locator": caption["locator"], "table_locator": parser_tables[0]["locator"], "known_gaps": snapshot.manifest["known_gaps"]}
    (output / "ACCEPTANCE.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
