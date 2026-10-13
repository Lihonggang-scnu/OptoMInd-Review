"""Formula sidecars must reach the current practical consumer, not legacy tools."""
import json
import hashlib
from pathlib import Path

import pytest

from test_docling_primary_material import _primary
from optomind_research.runtime.upgrade3 import directed_reading as dr
from optomind_research.runtime.upgrade3.local_formula_materials import (
    load_region_index, resolve_region, publish_formula_materials, load_formula_materials,
)


def _sidecar(tmp_path, snapshot, text, selected_refs=None):
    _, index, _ = load_region_index(snapshot.root)
    source = next(s for s in snapshot.manifest["source_documents"] if s["source_document_id"] == index["raw_document_source_document_id"])
    native = json.loads((snapshot.root / source["raw_path"]).read_bytes())
    selected_refs = selected_refs if selected_refs is not None else [t["self_ref"] for t in native["texts"] if t["label"] == "formula"]
    for item in native["texts"]:
        if item["self_ref"] in selected_refs: item["text"] = text
    enriched = tmp_path / "enhanced.json"
    enriched.write_text(json.dumps(native), encoding="utf-8")
    receipt = tmp_path / "execution.json"
    receipt.write_text(json.dumps({"status": "success", "mode": "formula", "source_sha256": index["pdf_sha256"],
        "raw_document_sha256": index["raw_document_sha256"], "enhanced_document_sha256": hashlib.sha256(enriched.read_bytes()).hexdigest(),
        "processed_refs": selected_refs,
        "model": {"name": "CodeFormulaV2", "execution": "offline_test_boundary"}}), encoding="utf-8")
    return publish_formula_materials(snapshot.root, enriched, tmp_path / "LOCAL_FORMULAS.json",
        execution_receipt_path=receipt)


def test_local_formula_reaches_real_practical_task_and_cache_identity(tmp_path):
    _, _, _, _, record, snapshot = _primary(tmp_path)
    ordinary = (snapshot.root / "DOCUMENT_BLOCKS.jsonl").read_bytes()
    path = _sidecar(tmp_path, snapshot, r"\frac{x^2}{y+1}")
    store = dr.DirectedReadingStore(tmp_path / "store.sqlite", core_cap=1)
    paper = {**record, "material_scope": "fulltext", "approve_core": True,
             "nomination_reason": "Read the source formula", "expected_information_gain": "Recover the formula structure",
             "core_justification": "Needed for the current formula task", "knowledge_gap": "Explain the source formula structure", "required_outputs": ["O1"]}
    request = dr.build_directed_request(review_id="review", topic="Example", chapter={"chapter_id": "C1", "title": "Example"},
        questions=[{"question_id": "Q1", "question": "What is the formula?", "purpose": "Explain", "required_output_ids": ["O1"]}],
        required_outputs=[{"output_id": "O1", "output_type": "explanation", "description": "Formula"}])
    store.admit_candidate(request["review_id"], request["topic_binding"], paper)
    class Client:
        calls = []
        def complete(self, messages, **kwargs):
            self.calls.append(messages)
            return {"content": {"question_material": [{"question_id": "Q1", "examples": [], "explanation": "The source formula has a denominator.", "remaining_points": []}]}}
    client = Client()
    kwargs = dict(request=request, paper=paper, snapshot_dir=snapshot.root, output_dir=tmp_path / "read", store=store, client=client,
                  formula_enhancement_path=path)
    first = dr.run_directed_reading(**kwargs)
    assert r"\frac{x^2}{y+1}" in json.loads(client.calls[-1][-1]["content"])["structured_source_material"]
    assert "[region d" in json.loads(client.calls[-1][-1]["content"])["structured_source_material"]
    assert dr.run_directed_reading(**kwargs)["reused"]
    assert len(client.calls) == 1
    changed = _sidecar(tmp_path, snapshot, r"\frac{x^3}{y+1}")
    second = dr.run_directed_reading(**{**kwargs, "formula_enhancement_path": changed})
    assert second["output"]["task_id"] != first["output"]["task_id"]
    assert len(client.calls) == 2
    assert (snapshot.root / "DOCUMENT_BLOCKS.jsonl").read_bytes() == ordinary


def test_formula_binding_and_ordinary_prose_resolution(tmp_path):
    _, _, _, _, _, snapshot = _primary(tmp_path)
    path = _sidecar(tmp_path, snapshot, r"x^2")
    _, index, _ = load_region_index(snapshot.root)
    prose = next(r for r in index["regions"] if r["source_ref"] == "#/texts/0")
    resolved = resolve_region(snapshot.root, prose["region_id"])
    assert resolved["blocks"][0]["text_normalized"] == "Recognized body text"
    assert resolved["region"]["locations"] and Path(resolved["pdf_path"]).is_file()
    payload = json.loads(path.read_bytes()); payload["binding"]["pdf_sha256"] = "0" * 64
    bad = tmp_path / "bad.json"; bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="source_mismatch"):
        load_formula_materials(snapshot.root, bad)
    payload = json.loads(path.read_bytes()); payload["regions"][0]["region_id"] = prose["region_id"]
    bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="region_mismatch"):
        load_formula_materials(snapshot.root, bad)


def test_selected_formula_receipt_does_not_promote_untouched_flat_text(tmp_path):
    from optomind_research.runtime.upgrade3.practical_materials import load_practical_material, build_practical_reader_messages
    _, _, _, _, _, snapshot = _primary(tmp_path, second_formula=True)
    path = _sidecar(tmp_path, snapshot, r"\sqrt{x}", selected_refs=["#/texts/3"])
    sidecar = load_formula_materials(snapshot.root, path)
    assert len(sidecar["regions"]) == 1 and sidecar["regions"][0]["source_ref"] == "#/texts/3"
    material = load_practical_material(snapshot.root, formula_enhancement_path=path)
    messages = build_practical_reader_messages(title="Fixture", questions=[], required_outputs=[], material=material)
    user = json.loads(messages[-1]["content"])
    assert "Unenhanced formula parser_text: flat second 2 x" in user["source_material"]
    assert "flat second 2 x" not in user["structured_source_material"]
    assert r"\sqrt{x}" in user["structured_source_material"]
