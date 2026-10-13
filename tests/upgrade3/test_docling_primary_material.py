"""Ordinary Docling source wiring; all model and GPU calls are forbidden."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

from test_docling_native_bridge import _fixture
from optomind_research.runtime.upgrade3.docling_adapter import DoclingAdapterConfig, DoclingCliAdapter, native_document_to_jats
from optomind_research.runtime.upgrade3.material_acquisition import AcquisitionConfig, MaterialAcquirer
from optomind_research.runtime.upgrade3.local_materials import PreparedSnapshotProvider
from optomind_research.runtime.upgrade3.paper_reading_card import build_card_input, build_card_messages
from optomind_research.runtime.upgrade3.practical_materials import load_practical_material, build_practical_reader_messages
from scripts.upgrade3 import build_local_materials as cli


def _primary(tmp_path, *, second_formula=False):
    data, document, run, config = _fixture(tmp_path)
    location = document["texts"][0]["prov"]
    document["texts"].append({"self_ref": "#/texts/4", "label": "text", "text": "AFTER_TABLE_NATIVE_PROSE", "prov": location})
    document["texts"].extend([
        {"self_ref": "#/texts/5", "label": "section_header", "text": "Source design", "prov": location},
        {"self_ref": "#/texts/6", "label": "section_header", "text": "Limitations", "prov": location}])
    document["pictures"] = [{"self_ref": "#/pictures/0", "label": "picture", "prov": location}]
    document["body"]["children"] = [{"$ref": "#/texts/5"}, {"$ref": "#/texts/0"}, {"$ref": "#/tables/0"},
        {"$ref": "#/texts/6"}, {"$ref": "#/texts/4"}, {"$ref": "#/texts/3"}, {"$ref": "#/pictures/0"}]
    if second_formula:
        document["texts"].append({"self_ref": "#/texts/7", "label": "formula", "text": "flat second 2 x", "orig": "flat second 2 x", "prov": location})
        document["body"]["children"].append({"$ref": "#/texts/7"})
    Path(config.document_path).write_text(json.dumps(document), encoding="utf-8")
    class Forbidden:
        def prepare(self, *args, **kwargs):
            raise AssertionError("ordinary reading must not wait for recognition")
        def parse_pdf(self, *args, **kwargs):
            raise AssertionError("Docling primary must supply the actual body")
    adapter = DoclingCliAdapter(config, recognizer=Forbidden())
    acquirer = MaterialAcquirer(tmp_path / "snapshots", config=AcquisitionConfig(
        docling_enabled=True, docling_primary=True, cache_root=tmp_path / "acquisition", rescue_enabled=False),
        providers={"docling": adapter, "grobid": Forbidden()})
    record = {"canonical_paper_id": "doi:10.1234/native", "doi": "10.1234/native", "title": "Native ordinary source"}
    result = acquirer.acquire_local(record, pdf_bytes=data, publication_version="v1")
    assert result.status == "published", result.to_dict()
    return data, config, adapter, acquirer, record, result.snapshot


def test_primary_body_order_positions_and_real_consumer_messages(tmp_path):
    data, config, adapter, acquirer, record, snapshot = _primary(tmp_path)
    assert snapshot is not None
    snapshot = PreparedSnapshotProvider(snapshot.root).load()
    packet = snapshot.build_reading_packet()
    observations = [item for item in packet["observations"] if item["source"].get("region_id")]
    assert [item["source_kind"] for item in observations] == [
        "article_body", "article_table", "article_body", "article_formula", "article_figure"]
    assert observations[0]["text"] == "Recognized body text"
    assert observations[2]["text"] == "AFTER_TABLE_NATIVE_PROSE"
    assert "An offline body source" not in json.dumps(packet)
    assert len(snapshot.assets) == 3
    assert observations[1]["text"].count("Table footnote below the caption.") == 1
    formula = next(a for a in snapshot.assets if a["type"] == "formula")
    assert formula["recognition_status"] == "missing"
    assert "flattened sqrt formula" in observations[3]["text"]
    for item in observations:
        source = item["source"]
        loc = source.get("locator") or source.get("asset_locator")
        assert loc["pdf_sha256"] and loc["page"] and loc["bbox"] and loc["locations"]
    sources = snapshot.manifest["source_documents"]
    region_source = next(s for s in sources if s["role"] == "pdf_primary_region_index")
    index = json.loads((snapshot.root / region_source["raw_path"]).read_text(encoding="utf-8"))
    pdf_source = next(s for s in sources if s["source_document_id"] == index["source_document_id"])
    assert pdf_source["raw_sha256"] == index["pdf_sha256"]
    assert all(item["source"]["region_id"] in {r["region_id"] for r in index["regions"]} for item in observations)
    assert any(r["kind"] == "page" for r in index["regions"])
    card_input = build_card_input(snapshot, {"question_en": "How does the design perform?", "facets": []})
    handles = [item["source"]["region_id"] for item in observations]
    for profile in ("standard", "economy"):
        messages = build_card_messages(card_input, input_profile=profile)
        text = json.dumps(messages)
        assert all(handle in text for handle in handles)
        assert text.index("Recognized body text") < text.index("table_structure_json") < text.index("AFTER_TABLE_NATIVE_PROSE") < text.index("flattened sqrt formula")
    material = load_practical_material(snapshot.root)
    practical = build_practical_reader_messages(title=record["title"], questions=[{"question_id": "q1", "question": "What is shown?"}], required_outputs=[], material=material)
    user = json.loads(practical[-1]["content"])
    assert user["structured_source_material"] == ""
    assert all(f"[region {handle}]" in user["source_material"] for handle in handles)
    assert user["source_material"].index("table_structure_json") < user["source_material"].index("AFTER_TABLE_NATIVE_PROSE")
    assert "## Source design" in user["source_material"] and "## Limitations" in user["source_material"]
    # A valid ordinary checkpoint does not require mathematical enhancement.
    assert cli._regional_completion(snapshot)[0] is True


def test_primary_warm_cache_preserves_snapshot_identity(tmp_path, monkeypatch):
    data, config, adapter, acquirer, record, snapshot = _primary(tmp_path)
    monkeypatch.setattr(adapter, "_run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("warm cache must not parse")))
    again = acquirer.acquire_local(record, pdf_bytes=data, publication_version="v1")
    assert again.snapshot.snapshot_id == snapshot.snapshot_id
    assert (again.snapshot.root / "DOCUMENT_BLOCKS.jsonl").read_bytes() == (snapshot.root / "DOCUMENT_BLOCKS.jsonl").read_bytes()


def test_formal_basic_cli_free_snapshot_and_warm_checkpoint(tmp_path, capsys):
    data, _, _, config = _fixture(tmp_path)
    source = tmp_path / "input.pdf"; source.write_bytes(data)
    output = tmp_path / "formal"
    argv = ["--input", str(source), "--canonical-paper-id", "local:native", "--material-parser-mode", "docling_basic",
        "--docling-document-json", str(config.document_path), "--docling-run-json", str(config.run_metadata_path),
        "--cache-root", str(tmp_path / "cache"), "--output-root", str(output), "--disable-rescue"]
    assert cli.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    snapshot = PreparedSnapshotProvider(result["snapshot_path"]).load()
    assert snapshot.manifest["producer_fulltext_claim"]["scope"] == "docling_basic"
    assert "Recognized body text" in snapshot.reading_view
    assert "An offline body source" not in snapshot.reading_view


def test_basic_snapshot_defers_crops_but_keeps_region_index_and_pdf_binding(tmp_path):
    data, document, run, config = _fixture(tmp_path)
    adapter_config = DoclingAdapterConfig.from_saved_run(
        config.document_path, config.run_metadata_path,
        cache_root=tmp_path / "deferred-cache", crop_scale=2.5, render_crops=False)
    prepared = DoclingCliAdapter(adapter_config).prepare_document(data, title="Deferred crops")
    roles = [item["role"] for item in prepared["additional_sources"]]
    assert "pdf_primary_region_index" in roles
    assert "pdf_region_crop" not in roles
    assert "pdf_crop_manifest" in roles
    index = prepared["region_index"]
    assert index["pdf_sha256"] == hashlib.sha256(data).hexdigest()
    assert any(region.get("bbox") for region in index["regions"] if region.get("kind") != "page")
    assert any(gap.get("status") == "deferred" for gap in prepared["known_gaps"])


def test_flattened_formula_and_multiple_provenance_are_not_latex(tmp_path):
    _, document, _, _ = _fixture(tmp_path)
    document["texts"][3]["text"] = "x 2 / y + 1"
    document["texts"][0]["prov"].append({"page_no": 2, "bbox": {"l": 10, "t": 350, "r": 90, "b": 330, "coord_origin": "BOTTOMLEFT"}})
    xml, index = native_document_to_jats(document, pdf_sha256="a" * 64, parser_version="2.138.0", config={})
    assert b"<tex-math>" not in xml
    assert b"Unenhanced formula parser_text: x 2 / y + 1" in xml
    formula = next(row for row in index["regions"] if row["kind"] == "formula")
    assert formula["recognition_status"] == "parser_candidate"
    prose = next(row for row in index["regions"] if row["source_ref"] == "#/texts/0")
    assert len(prose["locations"]) == 2
    assert prose["locations"][1]["bbox"] == [10.0, 50.0, 90.0, 70.0]


def test_scanned_document_without_ocr_is_not_silently_fulltext(tmp_path):
    import hashlib
    import fitz
    data, document, run, config = _fixture(tmp_path)
    with fitz.open(stream=data, filetype="pdf") as source:
        pixels = source[0].get_pixmap().tobytes("png")
    with fitz.open() as scan:
        page = scan.new_page(width=300, height=400)
        page.insert_image(page.rect, stream=pixels)
        data = scan.tobytes()
    document["texts"] = []; document["tables"] = []; document["pictures"] = []
    document["body"]["children"] = []; document["pages"] = {"1": document["pages"]["1"]}
    run["source_sha256"] = hashlib.sha256(data).hexdigest(); run["page_range"] = [1, 1]
    Path(config.document_path).write_text(json.dumps(document), encoding="utf-8")
    Path(config.run_metadata_path).write_text(json.dumps(run), encoding="utf-8")
    from optomind_research.runtime.upgrade3.docling_adapter import DoclingAdapterConfig
    config = DoclingAdapterConfig.from_saved_run(config.document_path, config.run_metadata_path, cache_root=config.cache_root)
    acquirer = MaterialAcquirer(tmp_path / "snapshots", config=AcquisitionConfig(
        docling_enabled=True, docling_primary=True, cache_root=tmp_path / "acquisition", rescue_enabled=False),
        providers={"docling": DoclingCliAdapter(config), "grobid": None})
    result = acquirer.acquire_local({"canonical_paper_id": "local:scan", "title": "Scan"}, pdf_bytes=data)
    assert result.material_depth != "fulltext"
    assert any("requires_text_or_ocr" in row.get("reason", "") for row in result.known_gaps)
