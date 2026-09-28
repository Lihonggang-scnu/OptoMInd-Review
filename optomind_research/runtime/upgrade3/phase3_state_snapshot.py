"""Strict, typed persistence for the Phase-3 coverage state (P3C).

The snapshot is deliberately a data boundary rather than an execution
boundary.  It records the complete state needed by the acceptance/handoff
consumer and reconstructs the two Phase-3 dataclasses and the canonical asset
graph on load.  No model, retrieval, or orchestration code belongs here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping


SNAPSHOT_SCHEMA = "optomind.upgrade3.p3c_state_snapshot.v1"
SNAPSHOT_SHA_FIELD = "snapshot_sha256"
P3C_STATE_SNAPSHOT_SCHEMA = SNAPSHOT_SCHEMA

_TOP_LEVEL_FIELDS = {
    "schema_version",
    "generation_id",
    "attempt_id",
    "blueprint",
    "states",
    "coverage_requests",
    "claim_graph",
    "relation_graph",
    "relation_audit",
    "coverage_atlas",
    "phase_run",
    "llm_summary",
    "blocked_sections",
    "upgrade3_claim_bindings_by_section",
    "upgrade3_verdicts",
    SNAPSHOT_SHA_FIELD,
}
_STATE_FIELDS = {
    "section",
    "contract",
    "claims",
    "bindings",
    "bundle",
    "argument_task_coverage",
    "status",
    "section_outcome",
    "runtime_failure",
    "records",
    "graph",
    "visual_bindings",
    "visual_needs",
    "binding_replacement_audit",
}
_GRAPH_FIELDS = {
    "graph_type",
    "papers",
    "chunks",
    "visuals",
    "expected_chunk_ids",
    "source_kbs",
    "diagnostics",
    "unresolved_asset_audit",
    "invalid_id_audit",
}
_ASSET_ID_FIELDS = {
    "PaperAsset": "paper_id",
    "ChunkAsset": "chunk_id",
    "VisualAsset": "visual_id",
}
_TUPLE_FIELDS = {
    "PaperAsset": {"not_usable_for", "allowed_claim_kinds", "route_events", "metadata_conflicts"},
    "ChunkAsset": {"not_usable_for", "allowed_claim_kinds", "relation_roles"},
    "VisualAsset": set(),
}
_CONTRACT_ALIASES = {
    "central_judgment": "central_thesis",
    "argument_role": "section_role",
    "scope_guardrails": "forbidden_overclaims",
    "unresolved_items": "open_questions",
}
_REQUIRED_CONTRACT_FIELDS = {
    "schema_version",
    "section_id",
    "core_question",
    "central_judgment",
    "argument_role",
}
_REQUIRED_REQUEST_FIELDS = {
    "request_id",
    "section_id",
    "iteration",
    "priority",
    "trigger",
}
W4_BINDING_SCHEMA = "optomind.upgrade3.claim_binding.v1"
_W4_ENVELOPE_FIELDS = {
    "schema_version",
    "binding",
    "writable",
    "gap",
    "gap_reasons",
    "span_texts",
}
_W4_BINDING_FIELDS = {
    "claim_id",
    "binding_hash",
    "support_span_ids",
    "context_span_ids",
    "counter_span_ids",
    "permission",
    "binding_status",
}


class Phase3StateSnapshotError(ValueError):
    """Raised whenever a snapshot cannot be trusted or reconstructed."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise Phase3StateSnapshotError(
            f"snapshot_not_canonical_json:{type(exc).__name__}"
        ) from exc


def _canonical_hash(value: Mapping[str, Any]) -> str:
    body = {
        key: item for key, item in value.items()
        if key != SNAPSHOT_SHA_FIELD
    }
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _strict_json(value: Any, path: str = "") -> Any:
    """Copy only JSON values; never coerce an unknown object with ``str``."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise Phase3StateSnapshotError(f"non_finite_value:{path or '<root>'}")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise Phase3StateSnapshotError(
                    f"mapping_key_not_string:{path or '<root>'}:{type(key).__name__}"
                )
            result[key] = _strict_json(item, f"{path}.{key}" if path else key)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _strict_json(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, (set, frozenset)):
        normalized = [
            _strict_json(item, f"{path}{{{index}}}")
            for index, item in enumerate(value)
        ]
        return sorted(normalized, key=_canonical_json)
    raise Phase3StateSnapshotError(
        f"unsupported_snapshot_value:{path or '<root>'}:{type(value).__name__}"
    )


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Phase3StateSnapshotError(
            f"snapshot_mapping_required:{path}:{type(value).__name__}"
        )
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise Phase3StateSnapshotError(
            f"snapshot_list_required:{path}:{type(value).__name__}"
        )
    return value


def _require_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Phase3StateSnapshotError(f"snapshot_text_required:{path}")
    return value


def _canonical_equal(left: Any, right: Any) -> bool:
    try:
        return _canonical_json(left) == _canonical_json(right)
    except Phase3StateSnapshotError:
        return False


def _validate_contract_payload(payload: Mapping[str, Any], path: str) -> None:
    missing = sorted(
        field for field in _REQUIRED_CONTRACT_FIELDS
        if field not in payload and _CONTRACT_ALIASES.get(field) not in payload
    )
    if missing:
        raise Phase3StateSnapshotError(
            f"contract_fields_missing:{path}:{','.join(missing)}"
        )
    for field in _REQUIRED_CONTRACT_FIELDS:
        value = payload.get(field)
        if value is None and field in _CONTRACT_ALIASES:
            value = payload.get(_CONTRACT_ALIASES[field])
        _require_text(value, f"{path}.{field}")
    for field in ("argument_tasks", "material_requirements"):
        if not isinstance(payload.get(field), list):
            raise Phase3StateSnapshotError(f"contract_{field}_invalid:{path}")
    scope_value = payload.get("scope_guardrails")
    if scope_value is None:
        scope_value = payload.get("forbidden_overclaims")
    if not isinstance(scope_value, list):
        raise Phase3StateSnapshotError(f"contract_scope_guardrails_invalid:{path}")
    if "scope" in payload and not isinstance(payload.get("scope"), list):
        raise Phase3StateSnapshotError(f"contract_scope_invalid:{path}")
    if not isinstance(payload.get("transitions"), Mapping):
        raise Phase3StateSnapshotError(f"contract_transitions_invalid:{path}")


def _validate_section_contract_aliases(
    section: Mapping[str, Any],
    contract: Mapping[str, Any],
    path: str,
) -> None:
    for key in ("section_contract", "section_argument_contract"):
        value = section.get(key)
        if not isinstance(value, Mapping):
            raise Phase3StateSnapshotError(f"section_{key}_invalid:{path}")
        if not _canonical_equal(value, contract):
            raise Phase3StateSnapshotError(f"section_{key}_mismatch:{path}")


def _validate_request_payload(payload: Mapping[str, Any], path: str) -> None:
    for field in _REQUIRED_REQUEST_FIELDS:
        if field not in payload:
            raise Phase3StateSnapshotError(
                f"coverage_request_field_missing:{path}:{field}"
            )
    for field in ("request_id", "section_id", "priority", "trigger"):
        _require_text(payload.get(field), f"{path}.{field}")
    if isinstance(payload.get("iteration"), bool) or not isinstance(
        payload.get("iteration"), int
    ):
        raise Phase3StateSnapshotError(f"coverage_request_iteration_invalid:{path}")
    for field in (
        "missing_claim_ids",
        "missing_roles",
        "missing_relation_tasks",
        "non_blocking_gaps",
        "queries",
        "query_targets",
        "affected_section_ids",
    ):
        if not isinstance(payload.get(field), list):
            raise Phase3StateSnapshotError(
                f"coverage_request_{field}_invalid:{path}"
            )
    if not isinstance(payload.get("stop_condition"), Mapping):
        raise Phase3StateSnapshotError(
            f"coverage_request_stop_condition_invalid:{path}"
        )
    for field in (
        "expected_new_papers",
        "per_wave_paper_budget",
        "target_total_new_papers",
    ):
        if isinstance(payload.get(field), bool) or not isinstance(payload.get(field), int):
            raise Phase3StateSnapshotError(
                f"coverage_request_{field}_invalid:{path}"
            )


def _binding_body_hash_matches(binding: Mapping[str, Any], expected: str) -> bool:
    # ClaimBinder hashes its body before wiring adds the audit-only ``roles``
    # field and before the final writable marker is attached.  Accept that
    # real W4 serialization, while also accepting the compact canonical
    # spelling for fixtures produced at this boundary.
    body = {
        key: value for key, value in binding.items()
        if key not in {"binding_hash", "roles", "writable"}
    }
    try:
        strict_body = _strict_json(body, "binding")
        compact = hashlib.sha256(
            _canonical_json(strict_body).encode("utf-8")
        ).hexdigest()
        legacy = hashlib.sha256(
            json.dumps(
                strict_body,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    except Phase3StateSnapshotError:
        return False
    return expected in {compact, legacy}


def _binding_hashes_in_verdict(value: Any, path: str = "verdict") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if key_text == "binding_hash":
                found.append(_require_text(item, child_path))
            elif key_text in {"binding_hashes", "source_binding_hashes"}:
                values = _require_list(item, child_path)
                found.extend(
                    _require_text(raw, f"{child_path}[{index}]")
                    for index, raw in enumerate(values)
                )
            else:
                found.extend(_binding_hashes_in_verdict(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_binding_hashes_in_verdict(item, f"{path}[{index}]"))
    return found


def _production_blocked_sections(
    verdicts: Mapping[str, Any],
    section_ids: Iterable[str],
) -> set[str]:
    """Use the production W3 admission consumer as the sole blocked authority."""

    try:
        from .coverage_readiness import author_admission
    except Exception as exc:
        raise Phase3StateSnapshotError(
            f"coverage_admission_import_failed:{type(exc).__name__}"
        ) from exc
    blocked: set[str] = set()
    for section_id in section_ids:
        verdict = _require_mapping(
            verdicts[section_id], f"upgrade3_verdicts.{section_id}"
        )
        try:
            admission = author_admission(dict(verdict))
        except Exception as exc:
            raise Phase3StateSnapshotError(
                f"coverage_admission_failed:{section_id}:{type(exc).__name__}"
            ) from exc
        if not isinstance(admission, Mapping) or admission.get("admit") is not True:
            blocked.add(section_id)
    return blocked


def project_upgrade3_bindings_for_p3d(
    section_id: str,
    upgrade3_rows: Any,
) -> dict[str, Any]:
    """Project the real W4 list envelope into the sole P3D binding authority."""

    section_id = _require_text(section_id, "section_id")
    rows = _require_list(upgrade3_rows, f"upgrade3_claim_bindings_by_section[{section_id}]")
    claims: dict[str, dict[str, Any]] = {}
    source_hashes: list[str] = []
    for index, raw in enumerate(rows):
        path = f"upgrade3_claim_bindings_by_section[{section_id}][{index}]"
        row = _require_mapping(raw, path)
        missing = sorted(_W4_ENVELOPE_FIELDS - set(row))
        if missing:
            raise Phase3StateSnapshotError(
                f"w4_envelope_fields_missing:{path}:{','.join(missing)}"
            )
        if row.get("schema_version") != W4_BINDING_SCHEMA:
            raise Phase3StateSnapshotError(f"w4_schema_mismatch:{path}")
        if not isinstance(row.get("writable"), bool) or not isinstance(row.get("gap"), bool):
            raise Phase3StateSnapshotError(f"w4_boolean_fields_invalid:{path}")
        if not isinstance(row.get("gap_reasons"), list):
            raise Phase3StateSnapshotError(f"w4_gap_reasons_invalid:{path}")
        span_texts = _require_mapping(row.get("span_texts"), f"{path}.span_texts")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in span_texts.items()):
            raise Phase3StateSnapshotError(f"w4_span_texts_invalid:{path}")
        binding = _require_mapping(row.get("binding"), f"{path}.binding")
        missing_binding = sorted(_W4_BINDING_FIELDS - set(binding))
        if missing_binding:
            raise Phase3StateSnapshotError(
                f"w4_binding_fields_missing:{path}:{','.join(missing_binding)}"
            )
        claim_id = _require_text(binding.get("claim_id"), f"{path}.binding.claim_id")
        binding_hash = _require_text(
            binding.get("binding_hash"), f"{path}.binding.binding_hash"
        )
        if not _binding_body_hash_matches(binding, binding_hash):
            raise Phase3StateSnapshotError(f"w4_binding_hash_invalid:{path}")
        inner_writable = binding.get("writable")
        if inner_writable is not None and inner_writable is not row.get("writable"):
            raise Phase3StateSnapshotError(f"w4_writable_mismatch:{path}")
        for field in ("support_span_ids", "context_span_ids", "counter_span_ids"):
            values = binding.get(field, [])
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise Phase3StateSnapshotError(f"w4_{field}_invalid:{path}")
        if not isinstance(binding.get("permission"), str) or not binding.get("permission").strip():
            raise Phase3StateSnapshotError(f"w4_permission_invalid:{path}")
        if not isinstance(binding.get("binding_status"), str) or not binding.get("binding_status").strip():
            raise Phase3StateSnapshotError(f"w4_binding_status_invalid:{path}")
        referenced_spans = set(
            binding.get("support_span_ids", [])
            + binding.get("context_span_ids", [])
        )
        if not referenced_spans.issubset(set(span_texts)):
            raise Phase3StateSnapshotError(f"w4_span_texts_missing_reference:{path}")
        if row["gap"] is not (not row["writable"]):
            raise Phase3StateSnapshotError(f"w4_gap_writable_mismatch:{path}")
        if claim_id in claims:
            raise Phase3StateSnapshotError(f"w4_duplicate_claim_id:{section_id}:{claim_id}")
        projected = dict(_strict_json(binding, f"{path}.binding"))
        projected.update({
            "binding_authority": "upgrade3_claim_bindings",
            "source_binding_hash": binding_hash,
            "binding_hash": binding_hash,
            "supporting_chunk_ids": list(binding.get("support_span_ids") or []),
            "contextual_support_chunk_ids": list(binding.get("context_span_ids") or []),
            "counterevidence_chunk_ids": list(binding.get("counter_span_ids") or []),
            "writable": row["writable"],
            "write_status": "writable" if row["writable"] else "unresolved",
            "permission_status": (
                "bound"
                if row["writable"]
                else str(binding.get("permission") or "discovery_only")
            ),
            "span_texts": dict(span_texts),
            "gap": row["gap"],
            "gap_reasons": list(row["gap_reasons"]),
        })
        claims[claim_id] = projected
        source_hashes.append(binding_hash)
    return {
        "section_id": section_id,
        "binding_authority": "upgrade3_claim_bindings",
        "source_binding_hashes": source_hashes,
        "claims": claims,
    }


def _validate_binding_projection(
    value: Any,
    expected: Mapping[str, Any],
    path: str,
) -> dict[str, Any]:
    row = _require_mapping(value, path)
    if row.get("binding_authority") != "upgrade3_claim_bindings":
        raise Phase3StateSnapshotError(f"binding_authority_invalid:{path}")
    hashes = row.get("source_binding_hashes")
    if not isinstance(hashes, list) or any(not isinstance(item, str) for item in hashes):
        raise Phase3StateSnapshotError(f"source_binding_hashes_invalid:{path}")
    claims = _require_mapping(row.get("claims"), f"{path}.claims")
    if not _canonical_equal(hashes, expected.get("source_binding_hashes")):
        raise Phase3StateSnapshotError(f"binding_source_hashes_mismatch:{path}")
    if not _canonical_equal(claims, expected.get("claims")):
        raise Phase3StateSnapshotError(f"binding_claims_mismatch:{path}")
    if str(row.get("section_id") or "") != str(expected.get("section_id") or ""):
        raise Phase3StateSnapshotError(f"binding_section_id_mismatch:{path}")
    return dict(_strict_json(row, path))


def _binding_projection_matches(
    value: Any,
    expected: Mapping[str, Any],
    path: str,
) -> bool:
    try:
        _validate_binding_projection(value, expected, path)
    except Phase3StateSnapshotError:
        return False
    return True


def _binding_replacement_audit(
    row: Mapping[str, Any],
    expected: Mapping[str, Any],
    path: str,
) -> dict[str, Any]:
    present = "bindings" in row
    incoming = row.get("bindings")
    incoming_type = type(incoming).__name__ if present else "absent"
    incoming_authority: Any = None
    if isinstance(incoming, Mapping):
        raw_authority = incoming.get("binding_authority")
        incoming_authority = (
            raw_authority
            if isinstance(raw_authority, str)
            else type(raw_authority).__name__ if raw_authority is not None else None
        )
    accepted = present and _binding_projection_matches(
        incoming,
        expected,
        f"{path}.bindings",
    )
    return {
        "incoming_type": incoming_type,
        "incoming_authority": incoming_authority,
        "replacement_type": "dict",
        "replacement_authority": "upgrade3_claim_bindings",
        "action": (
            "accepted_idempotent_projection"
            if accepted
            else "discarded_replaced"
            if present
            else "created_from_global_w4"
        ),
    }


def _dataclass_to_dict(value: Any, path: str) -> dict[str, Any]:
    if not dataclasses.is_dataclass(value):
        raise Phase3StateSnapshotError(
            f"snapshot_typed_object_required:{path}:{type(value).__name__}"
        )
    result: dict[str, Any] = {}
    for field in dataclasses.fields(value):
        result[field.name] = _strict_json(
            getattr(value, field.name),
            f"{path}.{field.name}",
        )
    return result


def _to_dict_with_method(value: Any, path: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(_strict_json(value, path))
    method = getattr(value, "to_dict", None)
    if not callable(method):
        raise Phase3StateSnapshotError(
            f"snapshot_to_dict_required:{path}:{type(value).__name__}"
        )
    payload = method()
    if not isinstance(payload, Mapping):
        raise Phase3StateSnapshotError(f"snapshot_to_dict_not_mapping:{path}")
    return dict(_strict_json(payload, path))


def _serialize_asset_map(
    assets: Any,
    *,
    expected_class: type[Any],
    id_field: str,
    path: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(assets, Mapping):
        raise Phase3StateSnapshotError(f"graph_asset_map_required:{path}")
    result: dict[str, dict[str, Any]] = {}
    for identifier, asset in assets.items():
        if not isinstance(identifier, str) or not identifier:
            raise Phase3StateSnapshotError(f"graph_asset_id_invalid:{path}")
        if not isinstance(asset, expected_class):
            raise Phase3StateSnapshotError(
                f"graph_asset_type_invalid:{path}.{identifier}:"
                f"expected={expected_class.__name__}:actual={type(asset).__name__}"
            )
        payload = _dataclass_to_dict(asset, f"{path}.{identifier}")
        if str(payload.get(id_field) or "") != identifier:
            raise Phase3StateSnapshotError(
                f"graph_asset_id_mismatch:{path}.{identifier}"
            )
        payload["asset_type"] = expected_class.__name__
        result[identifier] = dict(
            _strict_json(payload, f"{path}.{identifier}")
        )
    return result


def _serialize_graph(graph: Any, path: str = "graph") -> dict[str, Any]:
    # Delayed import prevents this persistence module from becoming part of
    # the Phase-3 orchestrator's import cycle.
    from ..section_authoring_assets import (
        CanonicalAssetGraph,
        ChunkAsset,
        PaperAsset,
        VisualAsset,
    )

    if not isinstance(graph, CanonicalAssetGraph):
        raise Phase3StateSnapshotError(
            f"graph_type_invalid:{path}:expected=CanonicalAssetGraph:"
            f"actual={type(graph).__name__}"
        )
    if not isinstance(graph.expected_chunk_ids, (set, frozenset)):
        raise Phase3StateSnapshotError(f"graph_expected_chunk_ids_not_set:{path}")
    if not isinstance(graph.source_kbs, tuple):
        raise Phase3StateSnapshotError(f"graph_source_kbs_not_tuple:{path}")
    for field in ("diagnostics", "unresolved_asset_audit", "invalid_id_audit"):
        if not isinstance(getattr(graph, field), list):
            raise Phase3StateSnapshotError(f"graph_{field}_not_list:{path}")
    payload = {
        "graph_type": "CanonicalAssetGraph",
        "papers": _serialize_asset_map(
            graph.papers,
            expected_class=PaperAsset,
            id_field="paper_id",
            path=f"{path}.papers",
        ),
        "chunks": _serialize_asset_map(
            graph.chunks,
            expected_class=ChunkAsset,
            id_field="chunk_id",
            path=f"{path}.chunks",
        ),
        "visuals": _serialize_asset_map(
            graph.visuals,
            expected_class=VisualAsset,
            id_field="visual_id",
            path=f"{path}.visuals",
        ),
        "expected_chunk_ids": graph.expected_chunk_ids,
        "source_kbs": graph.source_kbs,
        "diagnostics": graph.diagnostics,
        "unresolved_asset_audit": graph.unresolved_asset_audit,
        "invalid_id_audit": graph.invalid_id_audit,
    }
    return dict(_strict_json(payload, path))


def _serialize_state(
    state: Any,
    index: int,
    *,
    authoritative_bindings: Mapping[str, Any],
) -> dict[str, Any]:
    path = f"states[{index}]"
    row = _require_mapping(state, path)
    missing = sorted({
        "section",
        "contract",
        "claims",
        "bundle",
        "argument_task_coverage",
        "status",
        "section_outcome",
        "runtime_failure",
        "records",
        "graph",
    } - set(row))
    if missing:
        raise Phase3StateSnapshotError(
            f"state_fields_missing:{path}:{','.join(missing)}"
        )
    section = dict(_strict_json(_require_mapping(row["section"], f"{path}.section"), f"{path}.section"))
    section_id = _require_text(section.get("section_id"), f"{path}.section.section_id")
    contract = _to_dict_with_method(row["contract"], f"{path}.contract")
    _validate_contract_payload(contract, f"{path}.contract")
    contract_id = contract.get("section_id")
    _require_text(contract_id, f"{path}.contract.section_id")
    if str(contract_id) != section_id:
        raise Phase3StateSnapshotError(f"state_contract_section_mismatch:{section_id}")
    _validate_section_contract_aliases(section, contract, path)
    claims = _strict_json(row["claims"], f"{path}.claims")
    if not isinstance(claims, list) or any(not isinstance(item, Mapping) for item in claims):
        raise Phase3StateSnapshotError(f"state_claims_invalid:{path}")
    claim_ids = [
        _require_text(item.get("claim_id"), f"{path}.claims[{index}].claim_id")
        for index, item in enumerate(claims)
    ]
    for index, item in enumerate(claims):
        if (
            item.get("section_id") is not None
            and str(item.get("section_id")) != section_id
        ):
            raise Phase3StateSnapshotError(
                f"state_claim_section_mismatch:{path}.claims[{index}]"
            )
    if len(set(claim_ids)) != len(claim_ids):
        raise Phase3StateSnapshotError(f"state_claim_ids_not_unique:{path}")
    authoritative_claim_ids = set(
        _require_mapping(authoritative_bindings.get("claims"), f"{path}.bindings.claims")
    )
    if set(claim_ids) != authoritative_claim_ids:
        raise Phase3StateSnapshotError(
            f"state_claim_binding_ids_mismatch:{path}:"
            f"claims={sorted(set(claim_ids))}:bindings={sorted(authoritative_claim_ids)}"
        )
    binding_replacement_audit = _binding_replacement_audit(
        row,
        authoritative_bindings,
        path,
    )
    bundle = _strict_json(row["bundle"], f"{path}.bundle")
    if not isinstance(bundle, Mapping):
        raise Phase3StateSnapshotError(f"state_bundle_invalid:{path}")
    argument_task_coverage = _strict_json(
        row["argument_task_coverage"], f"{path}.argument_task_coverage"
    )
    if not isinstance(argument_task_coverage, list):
        raise Phase3StateSnapshotError(f"state_argument_task_coverage_invalid:{path}")
    status = _require_text(row["status"], f"{path}.status")
    section_outcome = _require_text(row["section_outcome"], f"{path}.section_outcome")
    runtime_failure = _strict_json(row["runtime_failure"], f"{path}.runtime_failure")
    if not isinstance(runtime_failure, Mapping):
        raise Phase3StateSnapshotError(f"state_runtime_failure_invalid:{path}")
    records = _strict_json(row["records"], f"{path}.records")
    if not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records):
        raise Phase3StateSnapshotError(f"state_records_invalid:{path}")
    visual_bindings = _strict_json(
        row.get("visual_bindings", section.get("visual_bindings", {})),
        f"{path}.visual_bindings",
    )
    visual_needs = _strict_json(
        row.get("visual_needs", section.get("visual_needs", {})),
        f"{path}.visual_needs",
    )
    if not isinstance(visual_bindings, (Mapping, list)):
        raise Phase3StateSnapshotError(f"state_visual_bindings_invalid:{path}")
    if not isinstance(visual_needs, (Mapping, list)):
        raise Phase3StateSnapshotError(f"state_visual_needs_invalid:{path}")
    return {
        "section": section,
        "contract": contract,
        "claims": claims,
        "bindings": dict(_strict_json(authoritative_bindings, f"{path}.bindings")),
        "bundle": dict(bundle),
        "argument_task_coverage": argument_task_coverage,
        "status": status,
        "section_outcome": section_outcome,
        "runtime_failure": dict(runtime_failure),
        "records": records,
        "graph": _serialize_graph(row["graph"], f"{path}.graph"),
        "visual_bindings": visual_bindings,
        "visual_needs": visual_needs,
        "binding_replacement_audit": binding_replacement_audit,
    }


def _serialize_requests(coverage_requests: Iterable[Any]) -> list[dict[str, Any]]:
    try:
        values = list(coverage_requests)
    except TypeError as exc:
        raise Phase3StateSnapshotError("coverage_requests_not_iterable") from exc
    result: list[dict[str, Any]] = []
    for index, request in enumerate(values):
        payload = _to_dict_with_method(request, f"coverage_requests[{index}]")
        _validate_request_payload(payload, f"coverage_requests[{index}]")
        result.append(payload)
    return result


def build_phase3_state_snapshot(
    generation_id: str,
    attempt_id: str,
    blueprint: Mapping[str, Any],
    states: Iterable[Mapping[str, Any]],
    coverage_requests: Iterable[Any],
    claim_graph: Mapping[str, Any],
    relation_graph: Mapping[str, Any],
    relation_audit: Mapping[str, Any],
    coverage_atlas: Mapping[str, Any],
    phase_run: Mapping[str, Any],
    llm_summary: Mapping[str, Any],
    blocked_sections: Iterable[str],
    upgrade3_claim_bindings_by_section: Mapping[str, Any],
    upgrade3_verdicts: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a complete P3C snapshot without mutating any input object."""

    generation_id = _require_text(generation_id, "generation_id")
    attempt_id = _require_text(attempt_id, "attempt_id")
    top_mappings = {
        "blueprint": blueprint,
        "claim_graph": claim_graph,
        "relation_graph": relation_graph,
        "relation_audit": relation_audit,
        "coverage_atlas": coverage_atlas,
        "phase_run": phase_run,
        "llm_summary": llm_summary,
        "upgrade3_claim_bindings_by_section": upgrade3_claim_bindings_by_section,
        "upgrade3_verdicts": upgrade3_verdicts,
    }
    normalized_top: dict[str, Any] = {}
    for name, value in top_mappings.items():
        normalized = _strict_json(_require_mapping(value, name), name)
        normalized_top[name] = dict(normalized)

    try:
        state_values = list(states)
    except TypeError as exc:
        raise Phase3StateSnapshotError("states_not_iterable") from exc
    section_ids: list[str] = []
    for index, state in enumerate(state_values):
        state_row = _require_mapping(state, f"states[{index}]")
        section = _require_mapping(state_row.get("section"), f"states[{index}].section")
        section_ids.append(
            _require_text(section.get("section_id"), f"states[{index}].section.section_id")
        )
    if len(set(section_ids)) != len(section_ids):
        raise Phase3StateSnapshotError("state_section_ids_not_unique")
    try:
        blocked_values = (
            sorted(blocked_sections)
            if isinstance(blocked_sections, (set, frozenset))
            else list(blocked_sections)
        )
    except TypeError as exc:
        raise Phase3StateSnapshotError("blocked_sections_not_iterable") from exc
    blocked = _strict_json(blocked_values, "blocked_sections")
    if not isinstance(blocked, list) or any(
        not isinstance(item, str) or not item.strip() for item in blocked
    ):
        raise Phase3StateSnapshotError("blocked_sections_invalid")
    if len(set(blocked)) != len(blocked):
        raise Phase3StateSnapshotError("blocked_sections_not_unique")
    binding_rows_by_section = normalized_top["upgrade3_claim_bindings_by_section"]
    if set(binding_rows_by_section) != set(section_ids):
        raise Phase3StateSnapshotError(
            "w4_section_keys_mismatch:"
            f"expected={sorted(section_ids)}:actual={sorted(binding_rows_by_section)}"
        )
    authoritative_bindings = {
        section_id: project_upgrade3_bindings_for_p3d(
            section_id,
            binding_rows_by_section[section_id],
        )
        for section_id in section_ids
    }
    verdicts = normalized_top["upgrade3_verdicts"]
    if set(verdicts) != set(section_ids):
        raise Phase3StateSnapshotError(
            "verdict_section_keys_mismatch:"
            f"expected={sorted(section_ids)}:actual={sorted(verdicts)}"
        )
    for section_id in section_ids:
        verdict = _require_mapping(
            verdicts[section_id], f"upgrade3_verdicts.{section_id}"
        )
        if (
            verdict.get("section_id") is not None
            and str(verdict.get("section_id")) != section_id
        ):
            raise Phase3StateSnapshotError(
                f"verdict_section_id_mismatch:{section_id}"
            )
        references = _binding_hashes_in_verdict(
            verdict, f"upgrade3_verdicts.{section_id}"
        )
        available = set(authoritative_bindings[section_id]["source_binding_hashes"])
        missing_references = sorted(set(references) - available)
        if missing_references:
            raise Phase3StateSnapshotError(
                f"verdict_binding_hash_not_found:{section_id}:{','.join(missing_references)}"
            )
    expected_blocked = _production_blocked_sections(verdicts, section_ids)
    if set(blocked) != expected_blocked:
        raise Phase3StateSnapshotError(
            "blocked_sections_verdict_mismatch:"
            f"expected={sorted(expected_blocked)}:actual={sorted(blocked)}"
        )
    state_rows = [
        _serialize_state(
            state,
            index,
            authoritative_bindings=authoritative_bindings[section_ids[index]],
        )
        for index, state in enumerate(state_values)
    ]
    snapshot: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA,
        "generation_id": generation_id,
        "attempt_id": attempt_id,
        "blueprint": normalized_top["blueprint"],
        "states": state_rows,
        "coverage_requests": _serialize_requests(coverage_requests),
        "claim_graph": normalized_top["claim_graph"],
        "relation_graph": normalized_top["relation_graph"],
        "relation_audit": normalized_top["relation_audit"],
        "coverage_atlas": normalized_top["coverage_atlas"],
        "phase_run": normalized_top["phase_run"],
        "llm_summary": normalized_top["llm_summary"],
        "blocked_sections": blocked,
        "upgrade3_claim_bindings_by_section": normalized_top[
            "upgrade3_claim_bindings_by_section"
        ],
        "upgrade3_verdicts": normalized_top["upgrade3_verdicts"],
    }
    snapshot[SNAPSHOT_SHA_FIELD] = _canonical_hash(snapshot)
    return snapshot


def _load_raw_snapshot(snapshot: Any) -> dict[str, Any]:
    if isinstance(snapshot, (str, os.PathLike, Path)):
        path = Path(snapshot)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Phase3StateSnapshotError(
                f"snapshot_unreadable:{path}:{type(exc).__name__}"
            ) from exc
    else:
        raw = snapshot
    if not isinstance(raw, Mapping):
        raise Phase3StateSnapshotError("snapshot_root_not_mapping")
    copied = _strict_json(raw, "snapshot")
    if not isinstance(copied, dict):
        raise Phase3StateSnapshotError("snapshot_root_not_object")
    return copied


def _restore_dataclass(
    raw: Mapping[str, Any],
    cls: type[Any],
    *,
    path: str,
    required_fields: set[str],
    aliases: Mapping[str, str] | None = None,
) -> Any:
    payload = dict(raw)
    missing = sorted(field for field in required_fields if field not in payload)
    if aliases:
        missing = sorted(
            field for field in missing
            if aliases.get(field) not in payload
        )
    if missing:
        raise Phase3StateSnapshotError(
            f"typed_fields_missing:{path}:{','.join(missing)}"
        )
    values: dict[str, Any] = {}
    for field in dataclasses.fields(cls):
        if field.name in payload:
            values[field.name] = payload[field.name]
        elif aliases and aliases.get(field.name) in payload:
            values[field.name] = payload[aliases[field.name]]
        elif field.default is not dataclasses.MISSING:
            continue
        elif field.default_factory is not dataclasses.MISSING:  # type: ignore[comparison-overlap]
            continue
        else:
            raise Phase3StateSnapshotError(
                f"typed_fields_missing:{path}:{field.name}"
            )
    try:
        return cls(**values)
    except (TypeError, ValueError) as exc:
        raise Phase3StateSnapshotError(
            f"typed_object_rebuild_failed:{path}:{type(exc).__name__}"
        ) from exc


def _restore_contract(raw: Any, section_id: str, path: str) -> Any:
    from ..phase3_argument_orchestrator import SectionArgumentContract

    payload = _require_mapping(raw, path)
    _validate_contract_payload(payload, path)
    contract = _restore_dataclass(
        payload,
        SectionArgumentContract,
        path=path,
        required_fields=_REQUIRED_CONTRACT_FIELDS,
        aliases=_CONTRACT_ALIASES,
    )
    for field in _REQUIRED_CONTRACT_FIELDS:
        value = getattr(contract, field)
        _require_text(value, f"{path}.{field}")
    if contract.section_id != section_id:
        raise Phase3StateSnapshotError(
            f"contract_section_id_mismatch:{section_id}:{contract.section_id}"
        )
    return contract


def _restore_request(raw: Any, path: str) -> Any:
    from ..phase3_argument_orchestrator import CoverageRequest

    payload = _require_mapping(raw, path)
    _validate_request_payload(payload, path)
    return _restore_dataclass(
        payload,
        CoverageRequest,
        path=path,
        required_fields=_REQUIRED_REQUEST_FIELDS,
    )


def _restore_asset(
    raw: Any,
    *,
    expected_class: type[Any],
    identifier: str,
    path: str,
) -> Any:
    if not isinstance(raw, Mapping):
        raise Phase3StateSnapshotError(f"graph_asset_row_invalid:{path}")
    if raw.get("asset_type") != expected_class.__name__:
        raise Phase3StateSnapshotError(
            f"graph_asset_type_invalid:{path}:expected={expected_class.__name__}:"
            f"actual={raw.get('asset_type')!r}"
        )
    id_field = _ASSET_ID_FIELDS[expected_class.__name__]
    if str(raw.get(id_field) or "") != identifier:
        raise Phase3StateSnapshotError(f"graph_asset_id_mismatch:{path}")
    payload = {
        key: value for key, value in raw.items()
        if key != "asset_type"
    }
    field_names = {field.name for field in dataclasses.fields(expected_class)}
    values = {key: value for key, value in payload.items() if key in field_names}
    for field in dataclasses.fields(expected_class):
        if field.name in values:
            continue
        if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            raise Phase3StateSnapshotError(f"graph_asset_field_missing:{path}:{field.name}")
    for field_name in _TUPLE_FIELDS[expected_class.__name__]:
        if field_name in values:
            value = values[field_name]
            if not isinstance(value, list):
                raise Phase3StateSnapshotError(
                    f"graph_asset_tuple_field_invalid:{path}.{field_name}"
                )
            values[field_name] = tuple(value)
    try:
        return expected_class(**values)
    except (TypeError, ValueError) as exc:
        raise Phase3StateSnapshotError(
            f"graph_asset_rebuild_failed:{path}:{type(exc).__name__}"
        ) from exc


def _restore_asset_map(
    raw: Any,
    *,
    expected_class: type[Any],
    path: str,
) -> dict[str, Any]:
    rows = _require_mapping(raw, path)
    result: dict[str, Any] = {}
    for identifier, value in rows.items():
        if not isinstance(identifier, str) or not identifier:
            raise Phase3StateSnapshotError(f"graph_asset_id_invalid:{path}")
        result[identifier] = _restore_asset(
            value,
            expected_class=expected_class,
            identifier=identifier,
            path=f"{path}.{identifier}",
        )
    return result


def _restore_graph(raw: Any, path: str) -> Any:
    from ..section_authoring_assets import (
        CanonicalAssetGraph,
        ChunkAsset,
        PaperAsset,
        VisualAsset,
    )

    graph = _require_mapping(raw, path)
    missing = sorted(_GRAPH_FIELDS - set(graph))
    if missing:
        raise Phase3StateSnapshotError(
            f"graph_fields_missing:{path}:{','.join(missing)}"
        )
    if graph.get("graph_type") != "CanonicalAssetGraph":
        raise Phase3StateSnapshotError(f"graph_type_invalid:{path}")
    expected_chunk_ids = _require_list(graph.get("expected_chunk_ids"), f"{path}.expected_chunk_ids")
    if any(not isinstance(item, str) for item in expected_chunk_ids):
        raise Phase3StateSnapshotError(f"graph_expected_chunk_ids_invalid:{path}")
    source_kbs = _require_list(graph.get("source_kbs"), f"{path}.source_kbs")
    if any(not isinstance(item, str) for item in source_kbs):
        raise Phase3StateSnapshotError(f"graph_source_kbs_invalid:{path}")
    diagnostics = _require_list(graph.get("diagnostics"), f"{path}.diagnostics")
    if any(not isinstance(item, str) for item in diagnostics):
        raise Phase3StateSnapshotError(f"graph_diagnostics_invalid:{path}")
    unresolved = _require_list(
        graph.get("unresolved_asset_audit"),
        f"{path}.unresolved_asset_audit",
    )
    invalid_ids = _require_list(graph.get("invalid_id_audit"), f"{path}.invalid_id_audit")
    for name, values in (("unresolved_asset_audit", unresolved), ("invalid_id_audit", invalid_ids)):
        if any(not isinstance(item, Mapping) for item in values):
            raise Phase3StateSnapshotError(f"graph_{name}_invalid:{path}")
    return CanonicalAssetGraph(
        papers=_restore_asset_map(
            graph.get("papers"), expected_class=PaperAsset, path=f"{path}.papers"
        ),
        chunks=_restore_asset_map(
            graph.get("chunks"), expected_class=ChunkAsset, path=f"{path}.chunks"
        ),
        visuals=_restore_asset_map(
            graph.get("visuals"), expected_class=VisualAsset, path=f"{path}.visuals"
        ),
        expected_chunk_ids=set(expected_chunk_ids),
        source_kbs=tuple(source_kbs),
        diagnostics=list(diagnostics),
        unresolved_asset_audit=[dict(item) for item in unresolved],
        invalid_id_audit=[dict(item) for item in invalid_ids],
    )


def _validate_raw_state(
    raw: Any,
    index: int,
    *,
    authoritative_bindings: Mapping[str, Any],
) -> dict[str, Any]:
    path = f"states[{index}]"
    row = _require_mapping(raw, path)
    missing = sorted(_STATE_FIELDS - set(row))
    if missing:
        raise Phase3StateSnapshotError(
            f"state_fields_missing:{path}:{','.join(missing)}"
        )
    section = _require_mapping(row.get("section"), f"{path}.section")
    section_id = _require_text(section.get("section_id"), f"{path}.section.section_id")
    contract_payload = _require_mapping(row.get("contract"), f"{path}.contract")
    _validate_contract_payload(contract_payload, f"{path}.contract")
    _validate_section_contract_aliases(section, contract_payload, path)
    # Validate plain fields before invoking constructors so malformed JSON is
    # rejected with a stable snapshot error instead of a downstream exception.
    claims = _require_list(row.get("claims"), f"{path}.claims")
    if any(not isinstance(item, Mapping) for item in claims):
        raise Phase3StateSnapshotError(f"state_claims_invalid:{path}")
    claim_ids = [
        _require_text(item.get("claim_id"), f"{path}.claims[{index}].claim_id")
        for index, item in enumerate(claims)
    ]
    for index, item in enumerate(claims):
        if (
            item.get("section_id") is not None
            and str(item.get("section_id")) != section_id
        ):
            raise Phase3StateSnapshotError(
                f"state_claim_section_mismatch:{path}.claims[{index}]"
            )
    if len(set(claim_ids)) != len(claim_ids):
        raise Phase3StateSnapshotError(f"state_claim_ids_not_unique:{path}")
    for field in ("bindings", "bundle", "runtime_failure"):
        _require_mapping(row.get(field), f"{path}.{field}")
    _validate_binding_projection(
        row.get("bindings"), authoritative_bindings, f"{path}.bindings"
    )
    if set(claim_ids) != set(authoritative_bindings["claims"]):
        raise Phase3StateSnapshotError(
            f"state_claim_binding_ids_mismatch:{path}:"
            f"claims={sorted(set(claim_ids))}:"
            f"bindings={sorted(authoritative_bindings['claims'])}"
        )
    replacement_audit = _require_mapping(
        row.get("binding_replacement_audit"),
        f"{path}.binding_replacement_audit",
    )
    for field in ("incoming_type", "replacement_type", "action"):
        _require_text(
            replacement_audit.get(field),
            f"{path}.binding_replacement_audit.{field}",
        )
    if replacement_audit.get("action") not in {
        "accepted_idempotent_projection",
        "discarded_replaced",
        "created_from_global_w4",
        "replaced_by_w4_projection",
    }:
        raise Phase3StateSnapshotError(
            f"binding_replacement_action_invalid:{path}"
        )
    _require_text(
        replacement_audit.get("replacement_authority"),
        f"{path}.binding_replacement_audit.replacement_authority",
    )
    task_coverage = _require_list(
        row.get("argument_task_coverage"), f"{path}.argument_task_coverage"
    )
    records = _require_list(row.get("records"), f"{path}.records")
    if any(not isinstance(item, Mapping) for item in records):
        raise Phase3StateSnapshotError(f"state_records_invalid:{path}")
    _require_text(row.get("status"), f"{path}.status")
    _require_text(row.get("section_outcome"), f"{path}.section_outcome")
    if not isinstance(row.get("visual_bindings"), (Mapping, list)):
        raise Phase3StateSnapshotError(f"state_visual_bindings_invalid:{path}")
    if not isinstance(row.get("visual_needs"), (Mapping, list)):
        raise Phase3StateSnapshotError(f"state_visual_needs_invalid:{path}")
    contract = _restore_contract(row.get("contract"), section_id, f"{path}.contract")
    graph = _restore_graph(row.get("graph"), f"{path}.graph")
    return {
        "section": dict(section),
        "contract": contract,
        "claims": [dict(item) for item in claims],
        "bindings": dict(row["bindings"]),
        "bundle": dict(row["bundle"]),
        "argument_task_coverage": list(task_coverage),
        "status": row["status"],
        "section_outcome": row["section_outcome"],
        "runtime_failure": dict(row["runtime_failure"]),
        "records": [dict(item) for item in records],
        "graph": graph,
        "visual_bindings": (
            dict(row["visual_bindings"])
            if isinstance(row["visual_bindings"], Mapping)
            else list(row["visual_bindings"])
        ),
        "visual_needs": (
            dict(row["visual_needs"])
            if isinstance(row["visual_needs"], Mapping)
            else list(row["visual_needs"])
        ),
        "binding_replacement_audit": dict(row["binding_replacement_audit"]),
    }


def load_phase3_state_snapshot(
    snapshot: Mapping[str, Any] | str | os.PathLike[str] | Path,
    expected_generation_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> dict[str, Any]:
    """Validate and reconstruct a P3C snapshot with typed state objects."""

    raw = _load_raw_snapshot(snapshot)
    missing = sorted(_TOP_LEVEL_FIELDS - set(raw))
    if missing:
        raise Phase3StateSnapshotError(
            f"snapshot_fields_missing:{','.join(missing)}"
        )
    if raw.get("schema_version") != SNAPSHOT_SCHEMA:
        raise Phase3StateSnapshotError(
            f"snapshot_schema_mismatch:{raw.get('schema_version')!r}"
        )
    generation_id = _require_text(raw.get("generation_id"), "generation_id")
    attempt_id = _require_text(raw.get("attempt_id"), "attempt_id")
    if expected_generation_id is not None and generation_id != str(expected_generation_id):
        raise Phase3StateSnapshotError(
            f"snapshot_generation_mismatch:expected={expected_generation_id}:actual={generation_id}"
        )
    if expected_attempt_id is not None and attempt_id != str(expected_attempt_id):
        raise Phase3StateSnapshotError(
            f"snapshot_attempt_mismatch:expected={expected_attempt_id}:actual={attempt_id}"
        )
    expected_hash = raw.get(SNAPSHOT_SHA_FIELD)
    if not isinstance(expected_hash, str) or not expected_hash:
        raise Phase3StateSnapshotError("snapshot_hash_missing")
    actual_hash = _canonical_hash(raw)
    if expected_hash != actual_hash:
        raise Phase3StateSnapshotError(
            f"snapshot_hash_mismatch:expected={expected_hash}:actual={actual_hash}"
        )
    blueprint = _require_mapping(raw.get("blueprint"), "blueprint")
    states_raw = _require_list(raw.get("states"), "states")
    requests_raw = _require_list(raw.get("coverage_requests"), "coverage_requests")
    for name in (
        "claim_graph",
        "relation_graph",
        "relation_audit",
        "coverage_atlas",
        "phase_run",
        "llm_summary",
        "upgrade3_verdicts",
    ):
        _require_mapping(raw.get(name), name)
    blocked = _require_list(raw.get("blocked_sections"), "blocked_sections")
    if any(not isinstance(item, str) or not item.strip() for item in blocked):
        raise Phase3StateSnapshotError("blocked_sections_invalid")
    if len(set(blocked)) != len(blocked):
        raise Phase3StateSnapshotError("blocked_sections_not_unique")
    section_ids: list[str] = []
    for index, raw_state in enumerate(states_raw):
        state_row = _require_mapping(raw_state, f"states[{index}]")
        section = _require_mapping(state_row.get("section"), f"states[{index}].section")
        section_ids.append(
            _require_text(section.get("section_id"), f"states[{index}].section.section_id")
        )
    if len(set(section_ids)) != len(section_ids):
        raise Phase3StateSnapshotError("state_section_ids_not_unique")
    binding_rows_by_section = _require_mapping(
        raw.get("upgrade3_claim_bindings_by_section"),
        "upgrade3_claim_bindings_by_section",
    )
    if set(binding_rows_by_section) != set(section_ids):
        raise Phase3StateSnapshotError(
            "w4_section_keys_mismatch:"
            f"expected={sorted(section_ids)}:actual={sorted(binding_rows_by_section)}"
        )
    authoritative_bindings = {
        section_id: project_upgrade3_bindings_for_p3d(
            section_id,
            binding_rows_by_section[section_id],
        )
        for section_id in section_ids
    }
    verdicts = _require_mapping(raw.get("upgrade3_verdicts"), "upgrade3_verdicts")
    if set(verdicts) != set(section_ids):
        raise Phase3StateSnapshotError(
            "verdict_section_keys_mismatch:"
            f"expected={sorted(section_ids)}:actual={sorted(verdicts)}"
        )
    for section_id in section_ids:
        verdict = _require_mapping(
            verdicts[section_id], f"upgrade3_verdicts.{section_id}"
        )
        if (
            verdict.get("section_id") is not None
            and str(verdict.get("section_id")) != section_id
        ):
            raise Phase3StateSnapshotError(
                f"verdict_section_id_mismatch:{section_id}"
            )
        references = _binding_hashes_in_verdict(
            verdict, f"upgrade3_verdicts.{section_id}"
        )
        available = set(authoritative_bindings[section_id]["source_binding_hashes"])
        missing_references = sorted(set(references) - available)
        if missing_references:
            raise Phase3StateSnapshotError(
                f"verdict_binding_hash_not_found:{section_id}:{','.join(missing_references)}"
            )
    expected_blocked = _production_blocked_sections(verdicts, section_ids)
    if set(blocked) != expected_blocked:
        raise Phase3StateSnapshotError(
            "blocked_sections_verdict_mismatch:"
            f"expected={sorted(expected_blocked)}:actual={sorted(blocked)}"
        )
    states = [
        _validate_raw_state(
            item,
            index,
            authoritative_bindings=authoritative_bindings[section_ids[index]],
        )
        for index, item in enumerate(states_raw)
    ]
    coverage_requests = [
        _restore_request(item, f"coverage_requests[{index}]")
        for index, item in enumerate(requests_raw)
    ]
    result = dict(raw)
    result["blueprint"] = dict(blueprint)
    result["states"] = states
    result["coverage_requests"] = coverage_requests
    result["claim_graph"] = dict(raw["claim_graph"])
    result["relation_graph"] = dict(raw["relation_graph"])
    result["relation_audit"] = dict(raw["relation_audit"])
    result["coverage_atlas"] = dict(raw["coverage_atlas"])
    result["phase_run"] = dict(raw["phase_run"])
    result["llm_summary"] = dict(raw["llm_summary"])
    result["upgrade3_claim_bindings_by_section"] = dict(binding_rows_by_section)
    result["upgrade3_verdicts"] = dict(verdicts)
    result["blocked_sections"] = list(blocked)
    return result


def _projection_graph(graph: Any, path: str) -> dict[str, Any]:
    if isinstance(graph, Mapping):
        return dict(_strict_json(graph, path))
    return _serialize_graph(graph, path)


def _projection_state(state: Any, index: int) -> dict[str, Any]:
    path = f"states[{index}]"
    row = _require_mapping(state, path)
    section = dict(_strict_json(_require_mapping(row.get("section"), f"{path}.section"), f"{path}.section"))
    contract = _to_dict_with_method(row.get("contract"), f"{path}.contract")
    claims = _strict_json(row.get("claims"), f"{path}.claims")
    bindings = _strict_json(row.get("bindings"), f"{path}.bindings")
    bundle = _strict_json(row.get("bundle"), f"{path}.bundle")
    task_coverage = _strict_json(row.get("argument_task_coverage"), f"{path}.argument_task_coverage")
    runtime_failure = _strict_json(row.get("runtime_failure"), f"{path}.runtime_failure")
    records = _strict_json(row.get("records"), f"{path}.records")
    visual_bindings = _strict_json(row.get("visual_bindings", {}), f"{path}.visual_bindings")
    visual_needs = _strict_json(row.get("visual_needs", {}), f"{path}.visual_needs")
    return {
        "section": section,
        "contract": contract,
        "claims": claims,
        "bindings": bindings,
        "bundle": bundle,
        "argument_task_coverage": task_coverage,
        "status": row.get("status"),
        "section_outcome": row.get("section_outcome"),
        "runtime_failure": runtime_failure,
        "records": records,
        "graph": _projection_graph(row.get("graph"), f"{path}.graph"),
        "visual_bindings": visual_bindings,
        "visual_needs": visual_needs,
    }


def p3d_consumer_audit_projection(
    snapshot: Mapping[str, Any] | None = None,
    **components: Any,
) -> dict[str, Any]:
    """Return an audit-only canonical projection of P3D input fields.

    The projection intentionally excludes transient M2A portfolios and other
    producer-only state.  It accepts both the plain object returned by the
    builder and the typed object returned by the loader.  The result is for
    canonical equality checks only and must never be used as an execution
    input to acceptance or handoff.
    """

    if snapshot is None:
        if not components:
            raise Phase3StateSnapshotError("projection_snapshot_required")
        try:
            snapshot = build_phase3_state_snapshot(**components)
        except TypeError as exc:
            raise Phase3StateSnapshotError(
                f"projection_components_invalid:{exc}"
            ) from exc
    elif components:
        raise Phase3StateSnapshotError("projection_snapshot_and_components_conflict")
    raw = _require_mapping(snapshot, "snapshot")
    states_raw = _require_list(raw.get("states"), "snapshot.states")
    requests = []
    for index, request in enumerate(_require_list(raw.get("coverage_requests"), "snapshot.coverage_requests")):
        requests.append(_to_dict_with_method(request, f"coverage_requests[{index}]"))
    projection = {
        "blueprint": dict(_strict_json(_require_mapping(raw.get("blueprint"), "snapshot.blueprint"), "snapshot.blueprint")),
        "states": [_projection_state(item, index) for index, item in enumerate(states_raw)],
        "coverage_requests": requests,
        "claim_graph": dict(_strict_json(_require_mapping(raw.get("claim_graph"), "snapshot.claim_graph"), "snapshot.claim_graph")),
        "relation_graph": dict(_strict_json(_require_mapping(raw.get("relation_graph"), "snapshot.relation_graph"), "snapshot.relation_graph")),
        "relation_audit": dict(_strict_json(_require_mapping(raw.get("relation_audit"), "snapshot.relation_audit"), "snapshot.relation_audit")),
        "coverage_atlas": dict(_strict_json(_require_mapping(raw.get("coverage_atlas"), "snapshot.coverage_atlas"), "snapshot.coverage_atlas")),
        "phase_run": dict(_strict_json(_require_mapping(raw.get("phase_run"), "snapshot.phase_run"), "snapshot.phase_run")),
        "llm_summary": dict(_strict_json(_require_mapping(raw.get("llm_summary"), "snapshot.llm_summary"), "snapshot.llm_summary")),
        "blocked_sections": list(_strict_json(_require_list(raw.get("blocked_sections"), "snapshot.blocked_sections"), "snapshot.blocked_sections")),
        "upgrade3_claim_bindings_by_section": dict(_strict_json(_require_mapping(raw.get("upgrade3_claim_bindings_by_section"), "snapshot.upgrade3_claim_bindings_by_section"), "snapshot.upgrade3_claim_bindings_by_section")),
        "upgrade3_verdicts": dict(_strict_json(_require_mapping(raw.get("upgrade3_verdicts"), "snapshot.upgrade3_verdicts"), "snapshot.upgrade3_verdicts")),
    }
    return projection


def p3d_consumer_projection(
    snapshot: Mapping[str, Any] | None = None,
    **components: Any,
) -> dict[str, Any]:
    """Deprecated audit-only alias for :func:`p3d_consumer_audit_projection`.

    This compatibility spelling exists solely for canonical comparison and is
    never an execution input.
    """

    return p3d_consumer_audit_projection(snapshot, **components)


def rehydrated_p3d_inputs(
    snapshot: Mapping[str, Any] | str | os.PathLike[str] | Path,
    expected_generation_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> dict[str, Any]:
    """Return the loader's typed execution inputs for P3D consumers."""

    loaded = load_phase3_state_snapshot(
        snapshot,
        expected_generation_id=expected_generation_id,
        expected_attempt_id=expected_attempt_id,
    )
    return {
        key: loaded[key]
        for key in (
            "generation_id",
            "attempt_id",
            "blueprint",
            "states",
            "coverage_requests",
            "claim_graph",
            "relation_graph",
            "relation_audit",
            "coverage_atlas",
            "phase_run",
            "llm_summary",
            "blocked_sections",
            "upgrade3_claim_bindings_by_section",
            "upgrade3_verdicts",
        )
    }


# Readable compatibility spellings for callers that name the producer node.
build_p3c_state_snapshot = build_phase3_state_snapshot
load_p3c_state_snapshot = load_phase3_state_snapshot
build_p3c_snapshot = build_phase3_state_snapshot
load_p3c_snapshot = load_phase3_state_snapshot
rehydrate_p3d_inputs = rehydrated_p3d_inputs


__all__ = [
    "SNAPSHOT_SCHEMA",
    "P3C_STATE_SNAPSHOT_SCHEMA",
    "Phase3StateSnapshotError",
    "build_phase3_state_snapshot",
    "build_p3c_state_snapshot",
    "build_p3c_snapshot",
    "load_phase3_state_snapshot",
    "load_p3c_state_snapshot",
    "load_p3c_snapshot",
    "rehydrated_p3d_inputs",
    "rehydrate_p3d_inputs",
    "p3d_consumer_audit_projection",
    "p3d_consumer_projection",
]
