"""Source-bound local formula sidecars; ordinary snapshots stay immutable."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Mapping

from .local_materials import PreparedSnapshotProvider

_LOCAL_FORMULA_GPU_QUEUE = threading.Lock()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_region_index(snapshot_dir: str | Path):
    snapshot = PreparedSnapshotProvider(snapshot_dir).load()
    source = next((s for s in snapshot.manifest["source_documents"] if s.get("role") == "pdf_primary_region_index"), None)
    if source is None:
        raise ValueError("ordinary_pdf_region_index_required")
    index = json.loads((snapshot.root / source["raw_path"]).read_bytes())
    pdf = next((s for s in snapshot.manifest["source_documents"] if s["source_document_id"] == index["source_document_id"]), None)
    if pdf is None or pdf["raw_sha256"] != index["pdf_sha256"]:
        raise ValueError("region_original_pdf_binding_mismatch")
    return snapshot, index, pdf


def resolve_region(snapshot_dir: str | Path, region_id: str) -> dict[str, Any]:
    """Resolve ordinary prose, formulas, tables or images without deep reading."""
    snapshot, index, pdf = load_region_index(snapshot_dir)
    region = next((r for r in index["regions"] if r["region_id"] == region_id), None)
    if region is None:
        raise ValueError("unknown_pdf_region")
    blocks = [dict(b) for b in snapshot.blocks if (b.get("locator") or {}).get("region_id") == region_id
              or (b.get("locator") or {}).get("owner_region_id") == region_id]
    return {"snapshot_id": snapshot.snapshot_id, "paper_id": snapshot.manifest["canonical_paper_id"],
            "publication_version": snapshot.manifest["publication_version"], "pdf_path": str(snapshot.root / pdf["raw_path"]),
            "pdf_sha256": pdf["raw_sha256"], "region": region, "blocks": blocks}


def _binding(snapshot, index):
    return {"canonical_paper_id": snapshot.manifest["canonical_paper_id"],
            "publication_version": snapshot.manifest["publication_version"],
            "pdf_sha256": index["pdf_sha256"], "raw_document_sha256": index["raw_document_sha256"],
            "projection_version": index["projection_version"]}


def publish_formula_materials(snapshot_dir: str | Path, enriched_document: str | Path, output_path: str | Path,
                              *, execution_receipt_path: str | Path, previous_materials_path: str | Path | None = None) -> Path:
    """Import a completed local batch result with explicit source proof.

    The executor owns the model and its cache. This adapter only verifies the
    original formula refs/provenance and writes consumer-ready material.
    """
    snapshot, index, _pdf = load_region_index(snapshot_dir)
    receipt_bytes = Path(execution_receipt_path).read_bytes()
    receipt = json.loads(receipt_bytes)
    if receipt.get("status") != "success" or receipt.get("mode") != "formula" or not isinstance(receipt.get("processed_refs"), list) or not receipt.get("model"):
        raise ValueError("completed_local_formula_execution_receipt_required")
    if receipt.get("source_sha256") != index["pdf_sha256"] or receipt.get("raw_document_sha256") != index["raw_document_sha256"]:
        raise ValueError("formula_batch_source_binding_mismatch")
    native_source = next(s for s in snapshot.manifest["source_documents"] if s["source_document_id"] == index["raw_document_source_document_id"])
    native = json.loads((snapshot.root / native_source["raw_path"]).read_bytes())
    original = {t["self_ref"]: t for t in native.get("texts") or ()}
    enhanced_bytes = Path(enriched_document).read_bytes()
    if receipt.get("enhanced_document_sha256") != _sha(enhanced_bytes):
        raise ValueError("formula_batch_output_binding_mismatch")
    enhanced = {t["self_ref"]: t for t in json.loads(enhanced_bytes).get("texts") or ()}
    processed = set(receipt["processed_refs"])
    if len(processed) != len(receipt["processed_refs"]) or not processed.issubset(
            {r["source_ref"] for r in index["regions"] if r["kind"] == "formula"}):
        raise ValueError("formula_batch_processed_refs_invalid")
    previous = load_formula_materials(snapshot_dir, previous_materials_path) if previous_materials_path else None
    if previous is not None and previous["model"] != receipt["model"]:
        raise ValueError("formula_previous_model_mismatch")
    by_ref = {r["source_ref"]: r for r in (previous or {}).get("regions") or () if r.get("status") == "recognized"}
    for region in index["regions"]:
        if region["kind"] != "formula" or region["source_ref"] not in processed:
            continue
        before = original[region["source_ref"]]
        after = enhanced.get(region["source_ref"])
        if after is None or after.get("label") != "formula" or after.get("prov") != before.get("prov") or after.get("orig") != before.get("orig"):
            raise ValueError("formula_batch_region_provenance_mismatch")
        text = str(after.get("text") or "").strip()
        by_ref[region["source_ref"]] = {"region_id": region["region_id"], "source_ref": region["source_ref"],
                                      "status": "recognized" if text else "missing", "latex": text}
    regions = [by_ref[r["source_ref"]] for r in index["regions"] if r["source_ref"] in by_ref]
    payload = {"schema_version": "optomind.local_formula_materials.v1", "binding": _binding(snapshot, index),
               "model": dict(receipt["model"]), "execution_receipt_sha256": _sha(receipt_bytes),
               "enhanced_document_sha256": _sha(enhanced_bytes), "regions": regions,
               "fidelity_status": "local_transcription_candidate_not_scientific_verification"}
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        # Immutable derived results coexist when model or output changes.
        path = path.with_name(path.stem + "-" + _sha(data)[:16] + path.suffix)
    if not path.exists():
        path.write_bytes(data)
    return path


def load_formula_materials(snapshot_dir: str | Path, path: str | Path) -> dict[str, Any]:
    snapshot, index, _pdf = load_region_index(snapshot_dir)
    data = Path(path).read_bytes()
    payload = json.loads(data)
    if payload.get("schema_version") != "optomind.local_formula_materials.v1" or payload.get("binding") != _binding(snapshot, index):
        raise ValueError("local_formula_material_source_mismatch")
    by_id = {r["region_id"]: r for r in index["regions"]}
    seen = set()
    for row in payload.get("regions") or ():
        region = by_id.get(row.get("region_id"))
        if region is None or region["kind"] != "formula" or row.get("source_ref") != region["source_ref"] or row["region_id"] in seen:
            raise ValueError("local_formula_material_region_mismatch")
        seen.add(row["region_id"])
    payload["content_sha256"] = _sha(data)
    return payload


def formula_material_text(payload: Mapping[str, Any]) -> str:
    lines = ["Local formula enhancement; transcription candidates; ordinary source regions remain available."]
    for row in payload.get("regions") or ():
        lines.append(f"[region {row['region_id']}] local_formula_status={row.get('status')}")
        if row.get("latex"):
            lines.append(str(row["latex"]))
    return "\n".join(lines)


def local_formula_profile_hash(config_path: str | Path) -> str:
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_bytes())
    driver_config = Path(config["driver_config"])
    if not driver_config.is_absolute(): driver_config = config_path.parent / driver_config
    return _sha(config_path.read_bytes() + driver_config.read_bytes())


def make_local_formula_enhancer(config_path: str | Path, *, cache_root: str | Path):
    """Build the selected-paper hook over the shared resident batch CLI.

    There is one local queue for GPU formula jobs. CPU snapshots and ordinary
    A/B do not enter this queue or wait for formula results.
    """
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_bytes())
    driver_config = Path(config["driver_config"])
    if not driver_config.is_absolute(): driver_config = config_path.parent / driver_config
    profile_hash = local_formula_profile_hash(config_path)
    command_prefix = list(config.get("command") or [config["python"], "-B", "-X", "utf8",
        str(Path(__file__).resolve().parents[3] / "scripts/upgrade3/parse_docling_batch.py")])
    root = Path(cache_root).resolve()

    def enhance(*, snapshot_dir: str | Path, output_dir: str | Path):
        prepared = PreparedSnapshotProvider(snapshot_dir).load()
        if not any(s.get("role") == "pdf_primary_region_index" for s in prepared.manifest["source_documents"]):
            return None
        snapshot, index, pdf = load_region_index(snapshot_dir)
        refs = {r["source_ref"] for r in index["regions"] if r["kind"] == "formula"}
        if not refs:
            return None
        label = index["pdf_sha256"][:20]
        job = root / _sha((index["pdf_sha256"] + index["raw_document_sha256"] + profile_hash).encode())
        job.mkdir(parents=True, exist_ok=True)
        pointer = job / "CURRENT.json"
        with _LOCAL_FORMULA_GPU_QUEUE:
            previous_path = None
            completed = set()
            if pointer.is_file():
                previous_path = Path(json.loads(pointer.read_bytes())["materials_path"])
                previous = load_formula_materials(snapshot_dir, previous_path)
                completed = {r["source_ref"] for r in previous["regions"] if r.get("status") == "recognized"}
            pending = sorted(refs - completed)
            if not pending:
                return previous_path
            source = next(s for s in snapshot.manifest["source_documents"] if s["source_document_id"] == index["raw_document_source_document_id"])
            raw_root = job / "raw"
            raw = raw_root / label / "document.json"
            raw.parent.mkdir(parents=True, exist_ok=True)
            if not raw.exists(): raw.write_bytes((snapshot.root / source["raw_path"]).read_bytes())
            selection_hash = _sha(json.dumps(pending).encode())[:16]
            stage = job / ("selection-" + selection_hash)
            stage.mkdir(exist_ok=True)
            manifest = stage / "MANIFEST.json"
            manifest.write_text(json.dumps({"records": [{"label": label, "canonical_paper_id": snapshot.manifest["canonical_paper_id"],
                "local_pdf": str(snapshot.root / pdf["raw_path"]), "pdf_sha256": index["pdf_sha256"]}]}), encoding="utf-8")
            selection = stage / "SELECTION.json"
            selection.write_text(json.dumps({"records": {label: {"region_ids": pending}}}), encoding="utf-8")
            output = stage / "results"
            args = command_prefix + ["--mode", "formula", "--manifest", str(manifest), "--config", str(driver_config),
                "--output-root", str(output), "--raw-doc-root", str(raw_root), "--selection", str(selection), "--resume"]
            env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}
            with (stage / "stdout.log").open("wb") as stdout, (stage / "stderr.log").open("wb") as stderr:
                # The batch supervisor owns its worker deadline and cleanup.
                # An outer timeout could kill only the supervisor and strand
                # its GPU child, so do not create a second shorter deadline.
                result = subprocess.run(args, stdout=stdout, stderr=stderr,
                    env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode:
                raise ValueError(f"local_formula_batch_failed:{result.returncode}; logs={stage}")
            materials = publish_formula_materials(snapshot_dir, output / label / "document.json", job / "LOCAL_FORMULAS.json",
                execution_receipt_path=output / label / "run.json", previous_materials_path=previous_path)
            temporary = pointer.with_suffix(".tmp")
            temporary.write_text(json.dumps({"materials_path": str(materials), "profile_sha256": profile_hash}), encoding="utf-8")
            os.replace(temporary, pointer)
            return materials
    return enhance
