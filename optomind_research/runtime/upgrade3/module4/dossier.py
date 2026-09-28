"""Dossier validation and deterministic human-readable rendering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import DOSSIER_SCHEMA_VERSION, snapshot_payload
from .provenance import build_reverse_index, resolve_anchor


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _snapshot(snapshot: Any) -> dict[str, Any]:
    return snapshot_payload(snapshot) if not isinstance(snapshot, Mapping) or "blocks" not in snapshot else dict(snapshot)


def validate_dossier(dossier: Mapping[str, Any], snapshot: Any) -> dict[str, Any]:
    if not isinstance(dossier, Mapping):
        return {"schema_version": DOSSIER_SCHEMA_VERSION, "valid": False, "issues": [{"code": "dossier_object_required"}]}
    source = _snapshot(snapshot)
    snapshot_id = _text(source.get("snapshot_id"))
    block_ids = {_text(row.get("block_id")) for row in source.get("blocks") or () if isinstance(row, Mapping)}
    block_by_id = {_text(row.get("block_id")): row for row in source.get("blocks") or () if isinstance(row, Mapping)}
    anchor_ids = {_text(row.get("anchor_id")) for row in dossier.get("source_anchors") or () if isinstance(row, Mapping)}
    issues: list[dict[str, Any]] = []
    # Keep the exported JSON Schema executable.  The bespoke checks below
    # additionally bind every quote to the frozen snapshot, which JSON Schema
    # cannot express.
    try:
        import jsonschema
        schema_path = Path(__file__).with_name("schemas") / "PAPER_READING_DOSSIER.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        for error in jsonschema.Draft202012Validator(schema).iter_errors(dossier):
            path = ".".join(str(part) for part in error.absolute_path)
            issues.append({"code": "dossier_schema_invalid", "path": path, "reason": error.message})
    except Exception as exc:
        issues.append({"code": "dossier_schema_validation_unavailable", "reason": type(exc).__name__})
    required_top = ("schema_version", "dossier_id", "input_fingerprint", "paper_identity_ref", "paper_map", "content_units", "facet_analyses", "source_anchors", "coverage", "verification_summary", "status")
    for key in required_top:
        if key not in dossier:
            issues.append({"code": "dossier_field_missing", "field": key})
    if _text(dossier.get("schema_version")) != DOSSIER_SCHEMA_VERSION:
        issues.append({"code": "dossier_schema_version_invalid"})
    if not _text(dossier.get("dossier_id")) or not _text(dossier.get("input_fingerprint")):
        issues.append({"code": "dossier_identity_missing"})
    identity = dossier.get("paper_identity_ref") or {}
    if not isinstance(identity, Mapping) or not _text(identity.get("canonical_paper_id")):
        issues.append({"code": "dossier_canonical_identity_missing"})
    for collection in ("content_units", "facet_analyses", "model_interpretations", "unmapped_observations", "citation_observations", "source_anchors", "issues"):
        if not isinstance(dossier.get(collection), list):
            issues.append({"code": "dossier_array_required", "collection": collection})
    seen_anchor_ids: set[str] = set()
    for anchor in dossier.get("source_anchors") or ():
        if not isinstance(anchor, Mapping):
            issues.append({"code": "anchor_not_object"})
            continue
        if _text(anchor.get("snapshot_id")) != snapshot_id:
            issues.append({"code": "anchor_snapshot_mismatch", "anchor_id": anchor.get("anchor_id")})
        if _text(anchor.get("block_id")) not in block_ids:
            issues.append({"code": "anchor_block_missing", "anchor_id": anchor.get("anchor_id")})
            continue
        aid = _text(anchor.get("anchor_id"))
        if aid in seen_anchor_ids:
            issues.append({"code": "anchor_id_duplicate", "anchor_id": aid})
        seen_anchor_ids.add(aid)
        candidate = resolve_anchor(source, block_id=_text(anchor.get("block_id")), quote=_text(anchor.get("quote_original")), snapshot_id=snapshot_id, anchor_type=_text(anchor.get("anchor_type") or "text"))
        if _text(anchor.get("binding_status")) == "bound":
            if candidate.get("binding_status") != "bound":
                issues.append({"code": "forged_bound_anchor", "anchor_id": aid, "reason": candidate.get("binding_reason")})
            for key in ("char_start", "char_end", "quote_original", "text_hash"):
                if key in anchor and anchor.get(key) != candidate.get(key):
                    issues.append({"code": "anchor_provenance_mismatch", "anchor_id": aid, "field": key})
    for collection in ("content_units", "model_interpretations", "unmapped_observations"):
        seen_ids: set[str] = set()
        for item in dossier.get(collection) or ():
            if not isinstance(item, Mapping):
                issues.append({"code": "item_not_object", "collection": collection})
                continue
            item_id = _text(item.get("unit_id") or item.get("interpretation_id") or item.get("observation_id"))
            if item_id in seen_ids:
                issues.append({"code": "item_id_duplicate", "collection": collection, "id": item_id})
            seen_ids.add(item_id)
            for aid in item.get("source_anchor_ids") or ():
                if _text(aid) not in anchor_ids:
                    issues.append({"code": "dangling_anchor_reference", "collection": collection, "id": item.get("unit_id") or item.get("interpretation_id"), "anchor_id": aid})
            if collection == "content_units":
                if not item_id or not _text(item.get("statement")):
                    issues.append({"code": "content_unit_identity_or_statement_missing", "id": item_id})
                if _text(item.get("origin_type")) not in {"author_report", "author_interpretation", "cited_work_report"}:
                    issues.append({"code": "content_unit_origin_invalid", "id": item_id})
    input_facets = [
        _text(item.get("facet_id"))
        for item in (dossier.get("input") or {}).get("facets") or dossier.get("facets") or ()
        if isinstance(item, Mapping) and _text(item.get("facet_id"))
    ]
    output_facets = [_text(item.get("facet_id")) for item in dossier.get("facet_analyses") or () if isinstance(item, Mapping)]
    if len(output_facets) != len(set(output_facets)):
        issues.append({"code": "facet_id_duplicate"})
    if input_facets and input_facets != output_facets:
        issues.append({"code": "facet_coverage_mismatch", "expected": input_facets, "actual": output_facets})
    for facet in dossier.get("facet_analyses") or ():
        if not isinstance(facet, Mapping) or not _text(facet.get("facet_id")):
            issues.append({"code": "facet_identity_missing"})
            continue
        status = _text(facet.get("answer_status"))
        if status not in {"addressed", "partly_addressed", "not_addressed_in_read_material", "uncertain"}:
            issues.append({"code": "facet_answer_status_invalid", "facet_id": facet.get("facet_id")})
        if not isinstance(facet.get("answer_blocks"), list):
            issues.append({"code": "facet_answer_blocks_required", "facet_id": facet.get("facet_id")})
        for answer in facet.get("answer_blocks") or ():
            if not isinstance(answer, Mapping):
                issues.append({"code": "facet_answer_block_invalid", "facet_id": facet.get("facet_id")})
                continue
            if status in {"addressed", "partly_addressed"} and not answer.get("source_anchor_ids"):
                issues.append({"code": "facet_answer_unanchored", "facet_id": facet.get("facet_id")})
            if status in {"addressed", "partly_addressed"} and _text((answer.get("verification_ref") or {}).get("status")) not in {"supported_as_report", "supported_as_inference", "partly_supported"}:
                issues.append({"code": "facet_answer_unreviewed", "facet_id": facet.get("facet_id")})
            if not _text(answer.get("text") or answer.get("statement")):
                issues.append({"code": "facet_answer_text_missing", "facet_id": facet.get("facet_id")})
            answer_review = _text((answer.get("verification_ref") or {}).get("status"))
            if answer_review and answer_review not in {"supported_as_report", "supported_as_inference", "partly_supported", "unsupported", "unresolved", "unreviewed"}:
                issues.append({"code": "facet_answer_review_status_invalid", "facet_id": facet.get("facet_id"), "status": answer_review})
        if status == "not_addressed_in_read_material" and not (_text(facet.get("not_addressed_reason")) or facet.get("coverage_basis") or facet.get("limitations_and_counterpoints")):
            issues.append({"code": "facet_not_addressed_reason_missing", "facet_id": facet.get("facet_id")})
        facet_review = _text((facet.get("verification_ref") or {}).get("status"))
        if facet_review and facet_review not in {"supported_as_report", "supported_as_inference", "partly_supported", "unsupported", "unresolved", "unreviewed"}:
            issues.append({"code": "facet_review_status_invalid", "facet_id": facet.get("facet_id"), "status": facet_review})
    coverage = dossier.get("coverage") or {}
    expected = set(_text(value) for value in coverage.get("expected_research_block_ids") or ())
    actual_expected = {_text(row.get("block_id")) for row in source.get("blocks") or () if isinstance(row, Mapping) and row.get("research_content") is not False and _text(row.get("text_normalized") or row.get("text_raw")).strip()}
    if expected != actual_expected:
        issues.append({"code": "expected_block_inventory_mismatch", "missing": sorted(actual_expected - expected), "extra": sorted(expected - actual_expected)})
    unit_ids = {_text(row.get("unit_id")) for row in dossier.get("content_units") or () if isinstance(row, Mapping)}
    def check_references(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in {"content_unit_ids", "relevant_unit_ids", "basis_unit_ids", "unit_ids", "source_anchor_ids"} and isinstance(child, list):
                    known = anchor_ids if key == "source_anchor_ids" else unit_ids
                    for target in child:
                        if _text(target) not in known:
                            issues.append({"code": "dangling_dossier_reference", "path": path + "." + key, "target": target})
                elif key != "draft":
                    check_references(child, path + "." + key)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                check_references(child, path + f"[{i}]")
    for name in ("paper_map", "content_units", "facet_analyses", "model_interpretations", "citation_observations"):
        check_references(dossier.get(name), name)
    processed = set(_text(value) for value in coverage.get("successfully_processed_block_ids") or ())
    if coverage.get("reading_receipts") and not processed <= expected:
        issues.append({"code": "receipt_block_outside_expected"})
    receipt_processed: set[str] = set()
    for receipt in coverage.get("reading_receipts") or ():
        if not isinstance(receipt, Mapping):
            issues.append({"code": "receipt_not_object"})
            continue
        ids = {_text(value) for value in receipt.get("block_ids") or ()}
        if receipt.get("complete"):
            receipt_processed.update(ids)
        if receipt.get("complete") and not ids:
            issues.append({"code": "complete_receipt_without_blocks"})
    if receipt_processed != processed:
        issues.append({"code": "coverage_receipt_mismatch", "processed": sorted(processed), "receipt_processed": sorted(receipt_processed)})
    if _text((dossier.get("status") or {}).get("reading_state")) == "full_available_text_processed" and (not expected or processed != expected):
        issues.append({"code": "false_fulltext_completion"})
    status = dossier.get("status") or {}
    if _text(status.get("delivery_state")) in {"ready", "ready_with_limits"}:
        semantic_status = _text(((dossier.get("verification_summary") or {}).get("semantic_review") or {}).get("status"))
        if semantic_status != "completed" or not expected or processed != expected or not dossier.get("content_units"):
            issues.append({"code": "false_ready_status"})
        if any(_text(item.get("code")) in {"source_anchor_unresolved", "reader_page_incomplete", "semantic_verifier_failed", "semantic_verifier_not_run", "facet_response_missing", "no_usable_content_units", "section_strategy_experimental"} for item in dossier.get("issues") or () if isinstance(item, Mapping)):
            issues.append({"code": "ready_has_blocking_issue"})
    reverse = coverage.get("reverse_index") or build_reverse_index(dossier)
    return {
        "schema_version": DOSSIER_SCHEMA_VERSION,
        "valid": not issues,
        "issues": issues,
        "anchor_count": len(dossier.get("source_anchors") or ()),
        "content_unit_count": len(dossier.get("content_units") or ()),
        "reverse_index_count": len(reverse),
    }


def render_dossier(dossier: Mapping[str, Any], snapshot: Any | None = None) -> str:
    """Render only values already present in the dossier; never call a model."""

    identity = dossier.get("paper_identity_ref") or dossier.get("paper_identity") or {}
    status = dossier.get("status") or {}
    lines = [
        "# Paper Reading Dossier",
        "",
        f"- Dossier: `{_text(dossier.get('dossier_id'))}`",
        f"- Paper: `{_text(identity.get('canonical_paper_id'))}`",
        f"- Execution: `{_text(status.get('execution_state'))}`",
        f"- Reading: `{_text(status.get('reading_state'))}`",
        f"- Delivery: `{_text(status.get('delivery_state'))}`",
        "",
        "## Paper map",
        "",
    ]
    paper_map = dossier.get("paper_map") or {}
    for block in paper_map.get("overview_blocks") or ():
        if isinstance(block, Mapping):
            lines.append(f"- {_text(block.get('text') or block.get('statement'))}")
    lines.extend(["", "## Content ledger", ""])
    for unit in dossier.get("content_units") or ():
        if not isinstance(unit, Mapping):
            continue
        refs = ", ".join(_text(value) for value in unit.get("source_anchor_ids") or ())
        lines.append(f"- **{_text(unit.get('kind') or 'other')}** {_text(unit.get('statement'))} *(anchors: {refs or 'unresolved'})*")
    lines.extend(["", "## Facet analyses", ""])
    for facet in dossier.get("facet_analyses") or ():
        if not isinstance(facet, Mapping):
            continue
        lines.append(f"### {_text(facet.get('facet_id'))} — {_text(facet.get('answer_status'))}")
        for block in facet.get("answer_blocks") or ():
            if isinstance(block, Mapping):
                lines.append(f"- {_text(block.get('text') or block.get('statement'))}")
        for limitation in facet.get("limitations_and_counterpoints") or ():
            lines.append(f"- Limitation: {_text(limitation)}")
    lines.extend(["", "## Model interpretations", ""])
    for item in dossier.get("model_interpretations") or ():
        if isinstance(item, Mapping):
            lines.append(f"- **{_text(item.get('type') or 'interpretation')}** {_text(item.get('statement'))}")
    lines.extend(["", "## Coverage", "", "```json", json.dumps(dossier.get("coverage") or {}, ensure_ascii=False, sort_keys=True, indent=2), "```", ""])
    if dossier.get("issues"):
        lines.extend(["## Issues", ""])
        for issue in dossier.get("issues") or ():
            lines.append(f"- `{_text(issue.get('code') or issue.get('kind'))}` {_text(issue.get('message') or issue.get('reason'))}")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["validate_dossier", "render_dossier"]
