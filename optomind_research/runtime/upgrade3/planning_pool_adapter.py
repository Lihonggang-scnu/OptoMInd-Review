"""Explicit, offline adoption of committed A/B cards into the planning pool.

Batch views are navigation, never a substitute for the complete card. Selection
is by work key (input version), not a directory timestamp or a paper's last row.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import shutil
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "planning_pool.v2"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def resolve(value, root):
    path = Path(value)
    return (path if path.is_absolute() else Path(root) / path).resolve()


def pool_row_from_card(card, *, paper_id, card_path, validate=True):
    """Shared projection; supplement-specific judgments remain with its caller."""
    for field in (("general_understanding", "review_planning", "planning_view") if validate else ()):
        if not isinstance(card.get(field), Mapping) or not card[field]:
            raise ValueError("complete_card_required:" + field)
    identity = card.get("paper_identity") or {}
    canonical = identity.get("canonical_paper_id")
    if validate and not canonical:
        raise ValueError("canonical_paper_identity_required")
    return {"paper_id": paper_id, "canonical_paper_id": canonical,
            "card_path": str(card_path), "planning_view": dict(card.get("planning_view") or {})}


def validate_pool_row(row, *, root="."):
    for field in ("card_sha256", "snapshot_dir", "snapshot_id", "snapshot_sha256",
                  "plan_path", "plan_sha256", "topic_sha256"):
        if not row.get(field):
            raise ValueError("selected_card_version_missing:" + field)
    path = resolve(row["card_path"], root)
    if not path.is_file():
        raise ValueError("selected_card_missing:" + str(path))
    if row.get("card_sha256") and digest(path) != row["card_sha256"]:
        raise ValueError("selected_card_hash_mismatch")
    card = read(path)
    projected = pool_row_from_card(card, paper_id=row["paper_id"], card_path=path)
    for key in ("canonical_paper_id", "planning_view"):
        if row.get(key) != projected[key]:
            raise ValueError("selected_card_projection_mismatch:" + key)
    result = {**row, "card_path": str(path)}
    for name in ("snapshot_dir", "plan_path"):
        if row.get(name):
            result[name] = str(resolve(row[name], root))
    if result.get("snapshot_dir"):
        from .local_materials import PreparedSnapshotProvider
        snapshot = PreparedSnapshotProvider(result["snapshot_dir"]).load()
        if snapshot.manifest["snapshot_id"] != row.get("snapshot_id"):
            raise ValueError("selected_snapshot_identity_mismatch")
    if digest(result["plan_path"]) != row["plan_sha256"]:
        raise ValueError("selected_plan_hash_mismatch")
    from .paper_reading_card import build_card_input, load_plan, validate_card_output
    current = build_card_input(snapshot, load_plan(result["plan_path"]))
    if current["snapshot_sha256"] != row["snapshot_sha256"]:
        raise ValueError("selected_snapshot_hash_mismatch")
    if current["topic_sha256"] != row["topic_sha256"]:
        raise ValueError("selected_topic_hash_mismatch")
    for field in ("card_id", "snapshot_id", "snapshot_sha256", "topic_sha256"):
        if card.get(field) != current[field]:
            raise ValueError("selected_card_input_version_mismatch:" + field)
    validate_card_output(card, current)
    # Only this verified consumer may issue read proof; stored row flags and
    # pre-reading router judgments are not trusted for these fields.
    result.update(read_confirmed=True, source_validated=True)
    return result


def publish_planning_pool(*, batch_state_path, output_dir, selected_work_keys=None,
                          legacy_pool_path=None):
    """Publish only after validating every selected source and successful commit.

    Multiple versions of one canonical paper require explicit selection. Legacy
    rows are retained verbatim unless replaced through canonical identity.
    """
    from .local_materials import PreparedSnapshotProvider
    from .paper_reading_card import build_card_input, load_plan, validate_card_output

    state_path = Path(batch_state_path).resolve()
    state = read(state_path)
    items = list((state.get("items") or {}).values())
    selected = set(selected_work_keys) if selected_work_keys is not None else None
    if selected is not None and selected - {i.get("work_key") for i in items}:
        raise ValueError("selected_work_key_unknown")
    rows, excluded, receipts = [], [], []
    identities = set()
    for item in items:
        key = item.get("work_key")
        if item.get("status") not in {"success", "reused"} or (selected is not None and key not in selected):
            excluded.append({"paper_id": item.get("paper_id"), "work_key": key,
                             "reason": item.get("status") if item.get("status") not in {"success", "reused"} else "not_selected"})
            continue
        output = resolve(item["output_dir"], state_path.parent)
        card_path = output / "PAPER_READING_CARD.json"
        commit = read(output / "COMMIT.json")
        card = read(card_path)
        if commit.get("status") != "success" or commit.get("files", {}).get(card_path.name) != digest(card_path):
            raise ValueError("selected_card_commit_invalid")
        for name, expected_hash in (commit.get("files") or {}).items():
            artifact = output / name
            if not artifact.is_file() or digest(artifact) != expected_hash:
                raise ValueError("selected_card_commit_file_mismatch:" + name)
        snapshot = resolve(item["snapshot_dir"], state_path.parent)
        plan = resolve(item["plan_path"], state_path.parent)
        current = build_card_input(PreparedSnapshotProvider(snapshot).load(), load_plan(plan))
        for field in ("card_id", "snapshot_id", "snapshot_sha256", "topic_sha256"):
            if not card.get(field) or card[field] != commit.get(field) or card[field] != current[field]:
                raise ValueError("selected_card_input_version_mismatch:" + field)
        validate_card_output(card, current)
        row = pool_row_from_card(card, paper_id=item["paper_id"], card_path=card_path)
        canonical = row["canonical_paper_id"]
        if canonical in identities:
            raise ValueError("ambiguous_card_versions_select_work_keys:" + canonical)
        identities.add(canonical)
        row.update(schema_version=SCHEMA_VERSION, card_sha256=digest(card_path),
                   snapshot_dir=str(snapshot), snapshot_id=current["snapshot_id"],
                   snapshot_sha256=current["snapshot_sha256"], plan_path=str(plan),
                   plan_sha256=digest(plan), topic_sha256=current["topic_sha256"],
                   material_depth_label=current["material"]["material_scope"],
                   material_ready=True, work_key=key, attempt=item.get("attempt"))
        rows.append(row)
        receipts.append({k: row[k] for k in ("paper_id", "canonical_paper_id", "card_path", "card_sha256", "snapshot_id", "snapshot_sha256", "topic_sha256", "plan_sha256", "work_key", "attempt")})
    legacy_map = []
    if legacy_pool_path:
        old = [json.loads(line) for line in Path(legacy_pool_path).read_text().splitlines() if line.strip()]
        new_by_id = {row["canonical_paper_id"]: row for row in rows}
        merged = []
        for row in old:
            canonical = row.get("canonical_paper_id") or (row.get("planning_view", {}).get("paper_identity") or {}).get("canonical_paper_id")
            replacement = new_by_id.pop(canonical, None)
            if replacement:
                replacement["paper_id"] = row["paper_id"]  # Preserve legacy P-order and ID.
            merged.append(replacement or row)
            legacy_map.append({"paper_id": row["paper_id"], "canonical_paper_id": canonical, "replaced": bool(replacement)})
        rows = merged + list(new_by_id.values())
    if len({row["paper_id"] for row in rows}) != len(rows):
        raise ValueError("pool_duplicate_legacy_paper_id")
    selected_ids = {row.get("canonical_paper_id"): row["paper_id"] for row in rows}
    for receipt in receipts:
        receipt["paper_id"] = selected_ids[receipt["canonical_paper_id"]]
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("pool_publication_requires_new_output_directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".planning-pool-", dir=output.parent))
    try:
        pool = staging / "PLANNING_POOL.jsonl"
        pool.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        report = {"schema_version": SCHEMA_VERSION, "pool_path": str(output / pool.name), "pool_sha256": digest(pool),
                  "selected_cards": receipts, "excluded": excluded, "legacy_mapping": legacy_map,
                  "manifest_finalized": bool(state.get("manifest_finalized")), "model_calls": 0}
        for name, payload in (("SELECTED_CARDS.json", receipts), ("MATERIAL_VERSION_INDEX.json", report)):
            (staging / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, output)
        return report
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def acquisition_to_reading_manifest(checkpoint, *, plan_path, bundle_root=".", finalized=False):
    """Map acquisition checkpoint paths explicitly, retaining unavailable entries."""
    items = checkpoint.get("items") or checkpoint.get("results") or ([checkpoint] if checkpoint.get("snapshot_path") or checkpoint.get("canonical_paper_id") or checkpoint.get("identity") else [])
    if isinstance(items, Mapping):
        items = list(items.values())
    return {"finalized": bool(finalized), "records": [
        {**item, "paper_id": item.get("paper_id") or item.get("canonical_paper_id") or (item.get("identity") or {}).get("canonical_paper_id"),
         "snapshot_dir": str(resolve(item["snapshot_path"], bundle_root)) if item.get("snapshot_path") else str(resolve("pending/" + str(item.get("paper_id", "unknown")), bundle_root)),
         "plan_path": str(resolve(plan_path, bundle_root))} for item in items]}


def reading_manifest_from_navigation(*, navigation_manifest_path, checkpoint, plan_path,
                                     bundle_root=None, finalized=False, tier="deep_analysis_corpus"):
    """R02 navigation selects reading work; acquisition supplies actual material.

    Navigation text is retained for audit only and is never injected into the
    snapshot/card evidence channel. Missing acquisition stays waiting-material.
    """
    from .plan_contract import load_plan
    path = Path(navigation_manifest_path).resolve()
    navigation = read(path)
    if navigation.get("schema_version") != "optomind.reading_navigation.v1":
        raise ValueError("reading_navigation_version_invalid")
    load_plan(plan_path, expected_hash=navigation.get("plan_hash"))
    for name in ("cards", "portfolio"):
        ref = navigation.get(name)
        if not isinstance(ref, Mapping):
            raise ValueError("navigation_artifact_missing:" + name)
        artifact = resolve(ref["path"], bundle_root or path.parent)
        if digest(artifact) != ref.get("sha256"):
            raise ValueError("navigation_artifact_hash_mismatch:" + name)
        if ref.get("run_id") != navigation.get("run_id") or ref.get("plan_hash") != navigation.get("plan_hash"):
            raise ValueError("navigation_artifact_identity_mismatch:" + name)
    cards = read(resolve(navigation["cards"]["path"], bundle_root or path.parent))
    portfolio = read(resolve(navigation["portfolio"]["path"], bundle_root or path.parent))
    if tier not in {"deep_analysis_corpus", "priority_portfolio"}:
        raise ValueError("navigation_tier_invalid")
    nominated = portfolio.get(tier)
    if not isinstance(nominated, list):
        raise ValueError("navigation_tier_missing")
    ids = [str(x.get("paper_id")) if isinstance(x, Mapping) else str(x) for x in nominated]
    if len(ids) != len(set(ids)) or any(x not in cards for x in ids):
        raise ValueError("navigation_identity_missing_or_duplicate")
    raw_items = checkpoint.get("items") or checkpoint.get("results") or []
    if isinstance(raw_items, Mapping):
        raw_items = list(raw_items.values())
    by_id = {}
    for item in raw_items:
        aliases = {str(value) for value in (item.get("paper_id"), item.get("canonical_paper_id"),
                   (item.get("identity") or {}).get("canonical_paper_id")) if value}
        for ident in aliases:
            if ident in by_id and by_id[ident] != item:
                raise ValueError("ambiguous_acquisition_checkpoint:" + str(ident))
            by_id[ident] = item
    selected = []
    for ident in ids:
        selected.append({**by_id.get(ident, {}), "paper_id": ident,
                         "navigation_identity": cards[ident].get("paper_identity", {})})
    manifest = acquisition_to_reading_manifest({"items": selected}, plan_path=plan_path,
                                               bundle_root=bundle_root or path.parent, finalized=finalized)
    selected_items = [by_id[x] for x in ids if x in by_id]
    manifest["excluded_checkpoint_ids"] = sorted(str(x) for x in set(by_id) - set(ids) if by_id[x] not in selected_items)
    manifest["navigation"] = {"tier": tier, "selected_ids": ids, "path": str(path), "sha256": digest(path),
                              "run_id": navigation["run_id"], "plan_hash": navigation["plan_hash"],
                              "scope": "navigation_only_not_source_evidence"}
    return manifest
