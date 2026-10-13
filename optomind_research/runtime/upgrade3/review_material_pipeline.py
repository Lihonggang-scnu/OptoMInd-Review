"""Thin navigation -> existing acquisition/card batch -> immutable pool entry."""
from __future__ import annotations
import json
from pathlib import Path
from .planning_pool_adapter import reading_manifest_from_navigation, publish_planning_pool, read, resolve, digest
from .offline_replay import canonical_hash, RecordedClient


def run_material_pipeline(config, *, root=".", client_factory=None):
    mode = config.get("mode", "preview")
    if mode not in {"preview", "replay", "run"}:
        raise ValueError("material_pipeline_mode_invalid")
    root = Path(root).resolve()
    output = resolve(config["output_dir"], root)
    plan = resolve(config["plan_path"], root)
    navigation = resolve(config["navigation_manifest_path"], root)
    checkpoint = read(resolve(config["checkpoint_path"], root)) if config.get("checkpoint_path") else {"items": []}
    manifest = reading_manifest_from_navigation(navigation_manifest_path=navigation, checkpoint=checkpoint,
        plan_path=plan, finalized=bool(config.get("finalized", True)), tier=config.get("tier", "deep_analysis_corpus"))
    records = {str(r.get("canonical_paper_id") or r.get("paper_id")): r for r in config.get("source_records", [])}
    receipt = {"schema_version": "review.material_pipeline.v1", "mode": mode,
        "config_sha256": canonical_hash({k:v for k,v in config.items() if k != "finalized"}), "navigation_sha256": digest(navigation),
        "plan_sha256": digest(plan), "selected_ids": manifest["navigation"]["selected_ids"],
        "excluded_checkpoint_ids": manifest["excluded_checkpoint_ids"], "network_calls": 0,
        "adapters": ["MaterialAcquirer.acquire_local/acquire", "run_paper_reading_batch", "publish_planning_pool", "load_planning_pool"],
        "reading_manifest": manifest}
    if mode == "preview":
        receipt["status"] = "preview"
        return receipt
    if client_factory is not None and mode != "replay":
        raise ValueError("injected_client_requires_replay_mode")
    output.mkdir(parents=True, exist_ok=True)
    receipt_path = output / "PIPELINE_BINDING.json"
    binding = {k: receipt[k] for k in ("config_sha256", "navigation_sha256", "plan_sha256")}
    if receipt_path.exists() and read(receipt_path) != binding:
        raise ValueError("material_pipeline_binding_changed_use_new_output")
    receipt_path.write_text(json.dumps(binding, indent=2))
    from .local_materials import PreparedSnapshotProvider
    from .material_acquisition import MaterialAcquirer, AcquisitionConfig
    acquirer = MaterialAcquirer(output / "acquired", config=AcquisitionConfig(
        cache_root=output / "acquisition_cache", rescue_enabled=False))
    acquired = []
    for row in manifest["records"]:
        ident = row["paper_id"]
        snapshot = Path(row["snapshot_dir"])
        if snapshot.is_dir():
            actual = PreparedSnapshotProvider(snapshot).load()
            if actual.manifest["canonical_paper_id"] != ident:
                raise ValueError("navigation_snapshot_identity_mismatch:" + ident)
            acquired.append({"paper_id": ident, "snapshot_path": str(snapshot)})
            continue
        record = records.get(ident)
        if not record:
            acquired.append({"paper_id": ident, "status": "waiting_material"})
            continue
        record = {**record, "canonical_paper_id": ident}
        local_xml = resolve(record["local_xml"], root) if record.get("local_xml") else None
        local_pdf = resolve(record["local_pdf"], root) if record.get("local_pdf") else None
        if local_pdf and mode == "replay":
            # PDF extraction may contact the configured GROBID service. A replay
            # must supply its already parsed snapshot/XML instead.
            raise ValueError("offline_pdf_requires_preparsed_snapshot_or_xml")
        if local_xml or local_pdf:
            result = acquirer.acquire_local(record, xml_bytes=local_xml.read_bytes() if local_xml else None,
                pdf_bytes=local_pdf.read_bytes() if local_pdf else None,
                source_name=(local_xml or local_pdf).name)
        elif mode == "run":
            result = acquirer.acquire(record)
            receipt["network_calls"] = None  # Existing acquirer owns detailed transport receipts.
        else:
            acquired.append({"paper_id": ident, "status": "waiting_material"})
            continue
        if result.identity.canonical_paper_id != ident:
            raise ValueError("acquired_identity_mismatch:" + ident)
        acquired.append({"paper_id": ident, "snapshot_path": str(result.snapshot.root) if result.snapshot else "",
                         "status": result.status, "errors": result.errors})
    manifest = reading_manifest_from_navigation(navigation_manifest_path=navigation,
        checkpoint={"items": acquired}, plan_path=plan, finalized=bool(config.get("finalized", True)),
        tier=config.get("tier", "deep_analysis_corpus"))
    manifest_path = output / "READING_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    if not manifest["finalized"]:
        receipt.update(status="waiting_material_manifest_not_finalized", reading_manifest=manifest,
                       manifest_path=str(manifest_path), planner_validated_rows=0)
        (output / "MATERIAL_PIPELINE_REPORT.json").write_text(json.dumps(receipt, indent=2))
        return receipt
    if mode == "replay" and client_factory is None:
        if not config.get("card_replay_path"):
            raise ValueError("card_replay_path_required")
        replay_path = resolve(config["card_replay_path"], root)
        client_factory = lambda item, attempt: RecordedClient(replay_path)
    from .paper_reading_batch import run_paper_reading_batch
    options = dict(config.get("batch_options") or {})
    forbidden = {"client_factory", "manifest_path", "output_root", "mode"} & set(options)
    if forbidden:
        raise ValueError("batch_options_reserved:" + ",".join(sorted(forbidden)))
    for name in ("key_file", "budget_ledger_path"):
        if options.get(name): options[name] = str(resolve(options[name], root))
    batch = run_paper_reading_batch(manifest_path=manifest_path, output_root=output / "batch",
        mode="run", client_factory=client_factory, **options)
    pool_dir = output / "pools" / digest(output / "batch" / "BATCH_STATE.json")[:24]
    if pool_dir.exists():
        report = read(pool_dir / "MATERIAL_VERSION_INDEX.json")
        if digest(report["pool_path"]) != report["pool_sha256"]:
            raise ValueError("published_pool_hash_changed")
    else:
        report = publish_planning_pool(batch_state_path=output / "batch" / "BATCH_STATE.json", output_dir=pool_dir,
            selected_work_keys=config.get("selected_work_keys"),
            legacy_pool_path=resolve(config["legacy_pool_path"], root) if config.get("legacy_pool_path") else None)
    from .progressive_review_plan import load_planning_pool
    verified = load_planning_pool(report["pool_path"]) if Path(report["pool_path"]).read_text().strip() else []
    receipt.update(status="insufficient_material" if not verified else "complete" if not report["excluded"] else "complete_with_diagnostics",
        batch_counts=batch["counts"], planning_pool=report, planner_validated_rows=len(verified))
    (output / "MATERIAL_PIPELINE_REPORT.json").write_text(json.dumps(receipt, indent=2))
    return receipt
