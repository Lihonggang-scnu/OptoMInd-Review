"""Supplementary claim revision and S04 section regeneration pipeline.

This module is the pure, additive second-round orchestrator for the six S04
claim-evidence gaps.  It consumes the original v19 probe report, the merged
v3/v4/v5 gap-closure state, the baseline finalized material cache, and the
final incremental material snapshot.  It produces per-claim revision
dossiers, runs a bounded author/reviewer revision loop with locally enforced
evidence checks, and emits a deep-copied revised probe report that the
existing acceptance bridge can consume.

The pipeline is deliberately local: it never starts a retrieval wave, never
calls a model itself, and never modifies any input.  Model interaction is
injected through ``author_callback``/``reviewer_callback`` so tests can use
fakes and live runs can use the existing Qwen client.  Model output only
fills informative fields; every schema field in the outputs is constructed
locally.

Retrieval outcome and scientific claim outcome stay separate: an
``improved_stop`` closure means retrieval stopped with candidate progress, not
that the claim is automatically ready to write.  A claim becomes ready only
after a locally validated revision passes the injected reviewer.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable, Mapping, Sequence

from .gap_closure_downstream import (
    merge_gap_closure_reports,
)
from .supplementary_gap_closure import v19_claim_evidence_gap_job_specs


SCHEMA_VERSION = "optomind.supplementary_claim_revision.v1"
DEFAULT_MODEL_TIER = "c_model"
MAX_PASSES = 3
MAX_REVIEWER_ATTEMPTS = 3

TARGET_CLAIM_IDS = frozenset(
    {"c1.3", "c2.2", "c4.2", "c5.3", "c10.2", "c14.2"}
)

_AUTHOR_ACTIONS = frozenset(
    {
        "small_revision",
        "medium_revision",
        "narrow",
        "qualify",
        "rewrite",
        "delete",
    }
)
_REVIEWER_VERDICTS = frozenset({"pass", "revise", "delete"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).casefold()


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _unit_task_ids(unit: Mapping[str, Any]) -> set[str]:
    task_ids: set[str] = set()
    for annotation in unit.get("query_annotations") or []:
        if not isinstance(annotation, Mapping):
            continue
        for reference in annotation.get("supplementary_task_references") or []:
            if not isinstance(reference, Mapping):
                continue
            task_id = _text(reference.get("task_id"))
            if task_id:
                task_ids.add(task_id)
    return task_ids


def _is_abstract_unit(unit: Mapping[str, Any]) -> bool:
    depth = _text(unit.get("content_depth")).casefold()
    source_kind = _text(unit.get("source_kind")).casefold()
    return "abstract" in depth or "abstract" in source_kind


def _depth_from_source_kind(source_kind: str) -> str:
    kind = source_kind.casefold()
    if "abstract" in kind:
        return "abstract_claim"
    if "snippet" in kind or "body" in kind:
        return "structured_snippet"
    return "fulltext"


def normalize_snapshot_unit(
    unit: Mapping[str, Any],
    *,
    origin: str = "incremental",
    task_id: str = "",
) -> dict[str, Any]:
    """Normalize a material-cache unit into the canonical dossier unit shape."""

    identity = _mapping(unit.get("identity"))
    content = _mapping(unit.get("durable_content"))
    card = _mapping(unit.get("durable_content_card"))
    quality = _mapping(card.get("content_quality"))
    audit = _mapping(unit.get("audit"))
    provenance = _mapping(audit.get("source_provenance"))
    unit_id = _text(unit.get("unit_id"))
    chunk_id = _text(
        identity.get("chunk_id") or unit.get("chunk_id")
    )
    paper_id = _text(
        identity.get("paper_id")
        or provenance.get("paper_id")
        or unit.get("paper_id")
    )
    raw_text = _text(content.get("raw_text") or unit.get("raw_text"))
    content_depth = _text(
        content.get("content_depth") or unit.get("content_depth")
    )
    source_kind = _text(
        quality.get("source_kind") or unit.get("source_kind")
    )
    permission = _text(
        quality.get("evidence_ceiling")
        or provenance.get("use_permission")
        or unit.get("use_permission")
        or unit.get("permission")
    )
    return {
        "unit_id": unit_id,
        "chunk_id": chunk_id,
        "paper_id": paper_id,
        "doi": _text(identity.get("doi") or unit.get("doi")),
        "title": _text(identity.get("title") or unit.get("title")),
        "raw_text": raw_text,
        "content_depth": content_depth,
        "source_kind": source_kind,
        "permission": permission,
        "origin": origin,
        "task_id": _text(task_id),
        "eligible": bool(paper_id and raw_text and not _is_abstract_unit(
            {
                "content_depth": content_depth,
                "source_kind": source_kind,
            }
        )),
    }


def normalize_original_evidence_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one ``current_evidence_summary`` row from the v19 record."""

    source_kind = _text(row.get("source_kind"))
    content_depth = _text(row.get("content_depth")) or _depth_from_source_kind(
        source_kind
    )
    return {
        "unit_id": _text(row.get("unit_id")) or f"orig:{_text(row.get('chunk_id'))}",
        "chunk_id": _text(row.get("chunk_id")),
        "paper_id": _text(row.get("paper_id")),
        "doi": _text(row.get("doi")),
        "title": _text(row.get("title")),
        "raw_text": _text(row.get("raw_text") or row.get("evidence")),
        "content_depth": content_depth,
        "source_kind": source_kind,
        "permission": _text(
            row.get("permission") or row.get("evidence_ceiling")
        ),
        "origin": "original_evidence",
        "task_id": "",
        "eligible": bool(
            _text(row.get("paper_id"))
            and _text(row.get("raw_text") or row.get("evidence"))
            and not _is_abstract_unit(
                {
                    "content_depth": content_depth,
                    "source_kind": source_kind,
                }
            )
        ),
    }


def _dedupe_units(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in units:
        key = _text(unit.get("unit_id")) or _text(unit.get("chunk_id"))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(copy.deepcopy(dict(unit)))
    return result


def _blueprint_sibling_locations(
    blueprint: Mapping[str, Any],
    sibling_ids: Sequence[str],
) -> list[dict[str, Any]]:
    siblings = set(sibling_ids)
    locations: list[dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                walk(child, child_path)
        elif isinstance(value, list):
            ids = [_text(item) for item in value]
            if ids and all(isinstance(item, str) for item in value):
                if siblings & set(ids):
                    locations.append({"path": path, "claim_ids": ids})
            else:
                for index, child in enumerate(value):
                    walk(child, f"{path}[{index}]")

    walk(blueprint, "")
    return locations


def _blueprint_placement_choices(
    blueprint: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """List explicit argument-arc and subsection placement slots."""

    argument_arc: list[dict[str, Any]] = []
    for index, entry in enumerate(blueprint.get("argument_arc") or []):
        if not isinstance(entry, Mapping):
            continue
        argument_arc.append(
            {
                "path": f"argument_arc[{index}].claim_ids",
                "index": index,
                "step": _text(entry.get("step")),
                "claim_ids": [
                    _text(value)
                    for value in entry.get("claim_ids") or []
                    if _text(value)
                ],
            }
        )
    subsection_blueprint: list[dict[str, Any]] = []
    for index, entry in enumerate(blueprint.get("subsection_blueprint") or []):
        if not isinstance(entry, Mapping):
            continue
        subsection_blueprint.append(
            {
                "path": f"subsection_blueprint[{index}].claim_ids",
                "index": index,
                "title": _text(entry.get("title")),
                "purpose": _text(entry.get("purpose")),
                "claim_ids": [
                    _text(value)
                    for value in entry.get("claim_ids") or []
                    if _text(value)
                ],
            }
        )
    return {
        "argument_arc": argument_arc,
        "subsection_blueprint": subsection_blueprint,
    }


def _normalize_placement(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        argument = raw.get("argument_arc")
        if argument is None:
            argument = raw.get("argument_arc_step")
        if argument is None:
            argument = raw.get("argument_arc_path")
        subsection = raw.get("subsection")
        if subsection is None:
            subsection = raw.get("subsection_title")
        if subsection is None:
            subsection = raw.get("subsection_blueprint")
        return {
            "argument_arc": argument,
            "subsection_blueprint": subsection,
        }
    text = _text(raw)
    if text.startswith("argument_arc"):
        return {"argument_arc": raw, "subsection_blueprint": None}
    if text.startswith("subsection_blueprint"):
        return {"argument_arc": None, "subsection_blueprint": raw}
    return {"argument_arc": None, "subsection_blueprint": None}


def _resolve_placement_kind(
    raw: Any,
    choices: Sequence[Mapping[str, Any]],
    kind: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not choices:
        return None, [f"no available {kind} locations"]
    if raw is None or (isinstance(raw, str) and not _text(raw)):
        return None, []
    if isinstance(raw, Mapping):
        if raw.get("index") is not None:
            index = raw["index"]
            if isinstance(index, str):
                if not index.isdigit():
                    return None, [f"{kind} index must be an integer"]
                index = int(index)
            if not isinstance(index, int) or isinstance(index, bool):
                return None, [f"{kind} index must be an integer"]
            if not 0 <= index < len(choices):
                return None, [f"{kind} index out of range: {index}"]
            return dict(choices[index]), []
        raw = (
            raw.get("path")
            or raw.get("title")
            or raw.get("step")
            or raw.get("purpose")
        )
    text = _text(raw)
    if isinstance(raw, int) or (text and text.isdigit()):
        return None, [
            f"bare numeric {kind} placement is ambiguous; use an exact path "
            f"(e.g. {kind}[0].claim_ids), a unique title/step, or "
            "{'index': N}"
        ]
    match = re.fullmatch(
        re.escape(kind) + r"\[(\d+)\]\.claim_ids", text
    )
    if match:
        index = int(match.group(1))
        if 0 <= index < len(choices):
            return dict(choices[index]), []
        return None, [f"{kind} index out of range: {index}"]
    matches = [
        choice
        for choice in choices
        if _normalized_text(choice.get("title") or choice.get("step"))
        == _normalized_text(text)
    ]
    if len(matches) == 1:
        return dict(matches[0]), []
    if len(matches) > 1:
        return None, [f"ambiguous {kind} placement text: {text}"]
    return None, [f"unknown {kind} placement: {text}"]


def _unique_sibling_choice(
    choices: Sequence[Mapping[str, Any]],
    sibling_ids: Sequence[str],
) -> dict[str, Any] | None:
    siblings = set(sibling_ids)
    matches = [
        dict(choice)
        for choice in choices
        if siblings & set(choice.get("claim_ids") or [])
    ]
    return matches[0] if len(matches) == 1 else None


def resolve_blueprint_placement(
    placement: Any,
    dossier: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve and validate an author's target blueprint placement.

    Accepts index, path, step/title, or nested mapping forms.  When the author
    omits a slot and exactly one argument-arc/subsection entry contains a
    sibling claim, the local code fills that slot.  When no unique sibling
    match exists, a non-delete proposal must supply the missing slot.
    """

    choices = _mapping(dossier.get("blueprint_placement_choices"))
    arc_choices = choices.get("argument_arc") or []
    subsection_choices = choices.get("subsection_blueprint") or []
    normalized = _normalize_placement(placement)
    errors: list[str] = []
    argument, arc_errors = _resolve_placement_kind(
        normalized["argument_arc"], arc_choices, "argument_arc"
    )
    errors.extend(arc_errors)
    subsection, subsection_errors = _resolve_placement_kind(
        normalized["subsection_blueprint"],
        subsection_choices,
        "subsection_blueprint",
    )
    errors.extend(subsection_errors)
    filled: dict[str, bool] = {
        "argument_arc": False,
        "subsection_blueprint": False,
    }
    sibling_ids = list(dossier.get("sibling_claim_ids") or [])
    if argument is None and not arc_errors:
        argument = _unique_sibling_choice(arc_choices, sibling_ids)
        if argument is not None:
            filled["argument_arc"] = True
        else:
            errors.append(
                "non-delete revision requires an explicit argument_arc "
                "placement or a unique sibling match"
            )
    if subsection is None and not subsection_errors:
        subsection = _unique_sibling_choice(
            subsection_choices, sibling_ids
        )
        if subsection is not None:
            filled["subsection_blueprint"] = True
        else:
            errors.append(
                "non-delete revision requires an explicit subsection "
                "placement or a unique sibling match"
            )
    return {
        "valid": not errors,
        "errors": errors,
        "argument_arc": argument,
        "subsection_blueprint": subsection,
        "filled_by_sibling_match": filled,
    }


def build_claim_revision_dossiers(
    probe_report: Mapping[str, Any],
    *,
    closure_context: Mapping[str, Any] | None = None,
    closure_reports: Any = None,
    baseline_units: Sequence[Mapping[str, Any]] | None = None,
    snapshot_units: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the per-claim revision dossier for every S04 target."""

    if closure_context is None:
        closure_context = merge_gap_closure_reports(closure_reports)
    specs = v19_claim_evidence_gap_job_specs(probe_report)
    claims_by_id = {
        _text(claim.get("claim_id")): claim
        for claim in probe_report.get("final_claims") or []
        if isinstance(claim, Mapping) and _text(claim.get("claim_id"))
    }
    baseline_ids = {
        _text(unit.get("unit_id"))
        for unit in baseline_units or []
        if isinstance(unit, Mapping) and _text(unit.get("unit_id"))
    }
    closures = _mapping(closure_context.get("claim_closures"))
    blueprint = _mapping(probe_report.get("verified_blueprint"))
    dossiers: list[dict[str, Any]] = []
    for spec in specs:
        record = _mapping(spec.get("record"))
        claim_id = _text(
            spec.get("claim_id")
            or record.get("claim_id")
            or record.get("component_id")
        )
        if not claim_id:
            continue
        claim = _mapping(claims_by_id.get(claim_id))
        task_id = _text(spec.get("task_id"))
        original_units = [
            normalize_original_evidence_row(row)
            for row in record.get("current_evidence_summary") or []
            if isinstance(row, Mapping)
        ]
        incremental_units: list[dict[str, Any]] = []
        for unit in snapshot_units or []:
            if not isinstance(unit, Mapping):
                continue
            unit_id = _text(unit.get("unit_id"))
            if unit_id and unit_id in baseline_ids:
                continue
            if task_id in _unit_task_ids(unit):
                incremental_units.append(
                    normalize_snapshot_unit(
                        unit, origin="incremental", task_id=task_id
                    )
                )
        evidence_units = _dedupe_units(original_units + incremental_units)
        closure = _mapping(closures.get(claim_id))
        retrieval_status = _text(closure.get("status"))
        mode = (
            "small_revision"
            if retrieval_status == "improved_stop"
            else "medium_revision"
        )
        parent_id = _text(claim.get("parent_claim_id"))
        sibling_ids = sorted(
            sibling_id
            for sibling_id, sibling in claims_by_id.items()
            if _text(sibling.get("parent_claim_id")) == parent_id
            and sibling_id != claim_id
        )
        dossiers.append(
            {
                "schema_version": SCHEMA_VERSION,
                "claim_id": claim_id,
                "component_id": claim_id,
                "role": _text(claim.get("role")),
                "parent_claim_id": parent_id,
                "task_id": task_id,
                "original_claim": copy.deepcopy(dict(claim)),
                "original_component_verification": copy.deepcopy(
                    claim.get("component_verification")
                    if isinstance(claim.get("component_verification"), list)
                    else []
                ),
                "original_verified_quotes": copy.deepcopy(
                    claim.get("verified_quotes")
                    if isinstance(claim.get("verified_quotes"), list)
                    else []
                ),
                "evidence_gap_record": copy.deepcopy(dict(record)),
                "reviewer_reason": _text(
                    record.get("failure_reason")
                    or record.get("why_current_evidence_fails")
                ),
                "missing_fact_units": [
                    _text(value)
                    for value in record.get("missing_fact_units") or []
                ],
                "revision_requirement": _text(
                    record.get("required_revision_or_qualification")
                    or record.get("author_revision_suggestion")
                ),
                "closure": copy.deepcopy(dict(closure)),
                "retrieval_status": retrieval_status,
                "revision_mode": mode,
                "evidence_units": evidence_units,
                "sibling_claim_ids": sibling_ids,
                "blueprint_locations": _blueprint_sibling_locations(
                    blueprint, sibling_ids
                ),
                "blueprint_placement_choices": _blueprint_placement_choices(
                    blueprint
                ),
            }
        )
    dossiers.sort(key=lambda dossier: dossier["claim_id"])
    return dossiers


def validate_evidence_selections(
    selections: Any,
    evidence_units: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Locally validate every selected quote against real material units.

    Hard checks: each selection resolves to a supplied unit; the quote is a
    contiguous substring of that unit's raw text after whitespace/case
    normalization; the unit has paper identity; and abstract-only material is
    rejected as direct factual support.  S2 body snippets and OA/fulltext are
    treated as equally strong.
    """

    units = [dict(unit) for unit in evidence_units if isinstance(unit, Mapping)]
    by_unit_id = {
        _text(unit.get("unit_id")): unit for unit in units if _text(unit.get("unit_id"))
    }
    by_chunk_id = {
        _text(unit.get("chunk_id")): unit for unit in units if _text(unit.get("chunk_id"))
    }
    errors: list[str] = []
    validated: list[dict[str, Any]] = []
    if not isinstance(selections, list):
        errors.append("evidence_selections must be a list")
        return {"valid": False, "errors": errors, "selections": []}
    for raw in selections:
        if not isinstance(raw, Mapping):
            errors.append("evidence selection must be an object")
            continue
        unit_id = _text(raw.get("unit_id"))
        chunk_id = _text(raw.get("chunk_id"))
        quote = _text(raw.get("quote") or raw.get("verbatim_quote"))
        unit = by_unit_id.get(unit_id) or by_chunk_id.get(chunk_id)
        if unit is None:
            errors.append(
                "unknown evidence unit "
                f"(unit_id={unit_id or '-'}, chunk_id={chunk_id or '-'})"
            )
            continue
        if not quote:
            errors.append("evidence selection is missing a quote")
            continue
        if not _text(unit.get("paper_id")):
            errors.append(
                f"unit {unit.get('unit_id') or unit.get('chunk_id')} "
                "has no paper identity"
            )
            continue
        if _is_abstract_unit(unit):
            errors.append(
                "abstract material cannot directly support a factual claim "
                f"(unit {unit.get('unit_id') or unit.get('chunk_id')})"
            )
            continue
        if _normalized_text(quote) not in _normalized_text(unit.get("raw_text")):
            errors.append(
                "quote is not a contiguous substring of unit "
                f"{unit.get('unit_id') or unit.get('chunk_id')}"
            )
            continue
        validated.append(
            {
                "unit_id": _text(unit.get("unit_id")),
                "chunk_id": _text(unit.get("chunk_id")),
                "paper_id": _text(unit.get("paper_id")),
                "doi": _text(unit.get("doi")),
                "title": _text(unit.get("title")),
                "quote": quote,
                "content_depth": _text(unit.get("content_depth")),
                "source_kind": _text(unit.get("source_kind")),
                "permission": _text(unit.get("permission")),
                "origin": _text(unit.get("origin")),
            }
        )
    return {"valid": not errors, "errors": errors, "selections": validated}


def validate_author_proposal(
    output: Mapping[str, Any],
    dossier: Mapping[str, Any],
    mode: str = "",
) -> dict[str, Any]:
    """Validate and normalize one author proposal against local hard checks."""

    errors: list[str] = []
    action = _text(output.get("action") or output.get("decision")).casefold()
    if action == "revise" or not action:
        action = mode or "medium_revision"
    if action not in _AUTHOR_ACTIONS:
        errors.append(f"unsupported author action: {action or 'missing'}")
        return {"valid": False, "errors": errors}
    revised_claim = _text(output.get("revised_claim") or output.get("statement"))
    rationale = _text(output.get("rationale"))
    if action == "delete":
        if revised_claim:
            errors.append("delete must not produce ready claim text")
        return {
            "valid": not errors,
            "errors": errors,
            "action": action,
            "revised_claim": "",
            "selections": [],
            "rationale": rationale,
            "placement": None,
        }
    if not revised_claim:
        errors.append("revised_claim is required for a non-delete revision")
    if not rationale:
        errors.append("rationale is required")
    selections_raw = output.get("evidence_selections") or output.get("evidence")
    if not isinstance(selections_raw, list) or not selections_raw:
        errors.append(
            "non-delete revision requires at least one evidence selection"
        )
        selections = []
    else:
        selection_validation = validate_evidence_selections(
            selections_raw, dossier.get("evidence_units") or []
        )
        errors.extend(selection_validation["errors"])
        selections = selection_validation["selections"]
    placement_resolution = resolve_blueprint_placement(
        output.get("target_blueprint_placement"), dossier
    )
    errors.extend(placement_resolution["errors"])
    return {
        "valid": not errors,
        "errors": errors,
        "action": action,
        "revised_claim": revised_claim,
        "selections": selections,
        "rationale": rationale,
        "placement": (
            {
                "argument_arc": placement_resolution["argument_arc"],
                "subsection_blueprint": placement_resolution[
                    "subsection_blueprint"
                ],
                "filled_by_sibling_match": placement_resolution[
                    "filled_by_sibling_match"
                ],
            }
            if placement_resolution["valid"]
            else None
        ),
    }


def validate_reviewer_output(
    output: Mapping[str, Any],
    dossier: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and normalize one reviewer output."""

    errors: list[str] = []
    verdict = _text(output.get("verdict") or output.get("decision")).casefold()
    if verdict not in _REVIEWER_VERDICTS:
        errors.append(f"unsupported reviewer verdict: {verdict or 'missing'}")
        return {"valid": False, "errors": errors}
    reason = _text(output.get("reason"))
    if not reason:
        errors.append("reviewer must provide a concise reason")
    corrected_claim = _text(
        output.get("corrected_claim") or output.get("revised_claim")
    )
    selections: list[dict[str, Any]] | None = None
    raw_selections = output.get("evidence_selections")
    if raw_selections is None:
        raw_selections = output.get("evidence")
    if raw_selections is not None:
        selection_validation = validate_evidence_selections(
            raw_selections,
            dossier.get("evidence_units") or [],
        )
        errors.extend(selection_validation["errors"])
        selections = selection_validation["selections"]
    return {
        "valid": not errors,
        "errors": errors,
        "verdict": verdict,
        "reason": reason,
        "corrected_claim": corrected_claim or None,
        "selections": selections,
    }


def _pop_usage(output: dict[str, Any]) -> dict[str, Any] | None:
    if "_llm_usage" not in output:
        return None
    usage = output.pop("_llm_usage")
    return dict(usage) if isinstance(usage, Mapping) else None


def _permission_for(selections: Sequence[Mapping[str, Any]]) -> str:
    if selections and all(
        _text(selection.get("permission")) == "factual_support"
        for selection in selections
    ):
        return "factual_support"
    return "qualified_only"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _revise_claim(
    dossier: Mapping[str, Any],
    *,
    author_callback: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    reviewer_callback: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    max_passes: int,
    usage_records: list[dict[str, Any]],
) -> dict[str, Any]:
    claim_id = _text(dossier.get("claim_id"))
    mode = _text(dossier.get("revision_mode")) or "medium_revision"
    closure = _mapping(dossier.get("closure"))
    outcome: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "claim_id": claim_id,
        "mode": mode,
        "role": _text(dossier.get("role")),
        "parent_claim_id": _text(dossier.get("parent_claim_id")),
        "sibling_claim_ids": list(dossier.get("sibling_claim_ids") or []),
        "final_status": "no_progress",
        "final_action": None,
        "final_reason": "",
        "ready_for_write": False,
        "final_claim_text": None,
        "final_selections": [],
        "final_placement": None,
        "permission": "qualified_only",
        "round_count": 0,
        "reviewer_comments": list(closure.get("reviewer_comments") or []),
        "rounds": [],
    }
    previous_reviewer: dict[str, Any] | None = None
    previous_text: str | None = None
    local_feedback: list[str] = []
    previous_invalid_author_key: str | None = None
    for round_index in range(1, max_passes + 1):
        author_input = {
            "schema_version": SCHEMA_VERSION,
            "dossier": dossier,
            "mode": mode,
            "round": round_index,
            "previous_reviewer_output": previous_reviewer,
            "local_feedback": list(local_feedback),
        }
        raw_author = author_callback(author_input)
        author_output = dict(raw_author) if isinstance(raw_author, Mapping) else {}
        usage = _pop_usage(author_output)
        if usage:
            usage_records.append(
                {
                    "agent": "author",
                    "claim_id": claim_id,
                    "round": round_index,
                    "usage": usage,
                }
            )
        validation = validate_author_proposal(
            author_output, dossier, mode=mode
        )
        round_record: dict[str, Any] = {
            "round": round_index,
            "mode": mode,
            "author_output": author_output,
            "author_validation": validation,
        }
        outcome["rounds"].append(round_record)
        outcome["round_count"] = round_index
        if not validation["valid"]:
            message = "author_output_invalid: " + "; ".join(
                validation["errors"]
            )
            round_record["local_feedback"] = message
            key = _canonical(author_output)
            if (
                previous_invalid_author_key is not None
                and key == previous_invalid_author_key
            ):
                outcome["final_status"] = "no_progress"
                outcome["final_action"] = validation.get("action")
                outcome["final_reason"] = "repeated_invalid_author_output"
                break
            previous_invalid_author_key = key
            local_feedback.append(message)
            continue
        action = validation["action"]
        if action == "delete":
            outcome["final_status"] = "deleted"
            outcome["final_action"] = "delete"
            outcome["final_reason"] = "Author chose delete; no ready claim text."
            break
        revised_text = validation["revised_claim"]
        if previous_text is not None and _normalized_text(
            revised_text
        ) == _normalized_text(previous_text):
            outcome["final_status"] = "no_progress"
            outcome["final_action"] = action
            outcome["final_reason"] = (
                "author_repeated_previous_revision_without_progress"
            )
            break
        previous_text = revised_text
        max_reviewer_attempts = min(max_passes, MAX_REVIEWER_ATTEMPTS)
        reviewer_attempts: list[dict[str, Any]] = []
        reviewer_local_feedback: list[str] = []
        previous_invalid_reviewer_key: str | None = None
        terminal = False
        for attempt in range(1, max_reviewer_attempts + 1):
            reviewer_input = {
                "schema_version": SCHEMA_VERSION,
                "dossier": dossier,
                "mode": mode,
                "round": round_index,
                "author_output": author_output,
                "reviewer_attempt": attempt,
                "local_feedback": list(reviewer_local_feedback),
            }
            raw_reviewer = reviewer_callback(reviewer_input)
            reviewer_output = (
                dict(raw_reviewer)
                if isinstance(raw_reviewer, Mapping)
                else {}
            )
            usage = _pop_usage(reviewer_output)
            if usage:
                usage_records.append(
                    {
                        "agent": "reviewer",
                        "claim_id": claim_id,
                        "round": round_index,
                        "attempt": attempt,
                        "usage": usage,
                    }
                )
            review_validation = validate_reviewer_output(
                reviewer_output, dossier
            )
            attempt_record: dict[str, Any] = {
                "attempt": attempt,
                "reviewer_output": reviewer_output,
                "reviewer_validation": review_validation,
                "local_feedback": None,
            }
            reviewer_attempts.append(attempt_record)
            if not review_validation["valid"]:
                message = "reviewer_output_invalid: " + "; ".join(
                    review_validation["errors"]
                )
                attempt_record["local_feedback"] = message
                key = _canonical(reviewer_output)
                if (
                    previous_invalid_reviewer_key is not None
                    and key == previous_invalid_reviewer_key
                ):
                    outcome["final_status"] = "no_progress"
                    outcome["final_action"] = action
                    outcome["final_reason"] = (
                        "repeated_invalid_reviewer_output"
                    )
                    terminal = True
                    break
                previous_invalid_reviewer_key = key
                reviewer_local_feedback.append(message)
                continue
            verdict = review_validation["verdict"]
            if verdict == "delete":
                outcome["final_status"] = "deleted"
                outcome["final_action"] = "delete"
                outcome["final_reason"] = (
                    "Reviewer chose delete; no ready claim text."
                )
                terminal = True
                break
            if verdict == "pass":
                final_selections = (
                    review_validation["selections"]
                    if review_validation["selections"]
                    else validation["selections"]
                )
                final_text = (
                    review_validation["corrected_claim"] or revised_text
                )
                outcome["final_status"] = "passed"
                outcome["final_action"] = action
                outcome["final_reason"] = review_validation["reason"]
                outcome["ready_for_write"] = True
                outcome["final_claim_text"] = final_text
                outcome["final_selections"] = final_selections
                outcome["final_placement"] = validation["placement"]
                outcome["permission"] = _permission_for(final_selections)
                terminal = True
                break
            # verdict == revise: scientific feedback goes back to the author.
            previous_reviewer = reviewer_output
            break
        else:
            outcome["final_status"] = "no_progress"
            outcome["final_action"] = action
            outcome["final_reason"] = "max_reviewer_attempts_exceeded"
            terminal = True
        round_record["reviewer_attempts"] = reviewer_attempts
        if reviewer_attempts:
            last_attempt = reviewer_attempts[-1]
            round_record["reviewer_output"] = last_attempt["reviewer_output"]
            round_record["reviewer_validation"] = last_attempt[
                "reviewer_validation"
            ]
        if terminal:
            break
    else:
        outcome["final_status"] = "no_progress"
        outcome["final_reason"] = "max_passes_exceeded"
    return outcome


def _summarize_usage(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_agent: dict[str, dict[str, int]] = {}
    for record in records:
        agent = _text(record.get("agent")) or "unknown"
        usage = _mapping(record.get("usage"))
        entry = by_agent.setdefault(
            agent, {"call_count": 0, "input_tokens": 0, "output_tokens": 0}
        )
        entry["call_count"] += 1
        entry["input_tokens"] += _int_or_zero(usage.get("input_tokens"))
        entry["output_tokens"] += _int_or_zero(usage.get("output_tokens"))
    total = {
        "call_count": sum(
            entry["call_count"] for entry in by_agent.values()
        ),
        "input_tokens": sum(
            entry["input_tokens"] for entry in by_agent.values()
        ),
        "output_tokens": sum(
            entry["output_tokens"] for entry in by_agent.values()
        ),
    }
    return {
        "author": dict(by_agent.get("author", {
            "call_count": 0, "input_tokens": 0, "output_tokens": 0,
        })),
        "reviewer": dict(by_agent.get("reviewer", {
            "call_count": 0, "input_tokens": 0, "output_tokens": 0,
        })),
        "total": total,
        "calls": [dict(record) for record in records],
    }


def _update_claim_components(
    claim: dict[str, Any],
    *,
    component_id: str,
    text: str,
    selections: Sequence[Mapping[str, Any]],
    rationale: str,
    permission: str,
) -> None:
    bindings = [
        {
            "chunk_id": _text(selection.get("chunk_id")),
            "verbatim_quote": _text(selection.get("quote")),
            "paper_id": _text(selection.get("paper_id")),
        }
        for selection in selections
    ]
    for key in ("claim_components", "component_verification"):
        container = claim.get(key)
        if not isinstance(container, list):
            claim[key] = container = []
        entry = next(
            (
                item
                for item in container
                if isinstance(item, dict)
                and _text(item.get("component_id") or item.get("claim_id"))
                == component_id
            ),
            None,
        )
        if entry is None:
            entry = {
                "component_id": component_id,
                "statement": text,
                "support_assessment": "direct",
                "reason": rationale,
                "bindings": [],
            }
            container.append(entry)
        entry["statement"] = text
        entry["support_assessment"] = "direct"
        entry["reason"] = rationale or "Revised under supplementary claim revision."
        entry["bindings"] = copy.deepcopy(bindings)
        if key == "component_verification":
            entry["ready"] = True
            entry["quote_exact"] = True
            entry["chunk_id_valid"] = True
            entry["permission"] = permission


def _upsert_claim_contract(
    report: dict[str, Any],
    *,
    claim_id: str,
    role: str,
    text: str,
    selections: Sequence[Mapping[str, Any]],
    permission: str,
) -> None:
    contracts = report.get("claim_scope_contracts")
    if not isinstance(contracts, list):
        report["claim_scope_contracts"] = contracts = []
    sources: list[dict[str, Any]] = []
    for selection in selections:
        sources.append(
            {
                "chunk_id": _text(selection.get("chunk_id")),
                "paper_id": _text(selection.get("paper_id")),
                "title": _text(selection.get("title")),
                "doi": _text(selection.get("doi")),
                "source_kind": _text(selection.get("source_kind")),
                "content_depth": _text(selection.get("content_depth")),
                "use_permission": _text(selection.get("permission")),
                "allowed_claim_kinds": [],
            }
        )
    envelope = {
        "paper_ids": sorted(
            {_text(selection.get("paper_id")) for selection in selections}
        ),
        "independent_source_count": len(
            {_text(selection.get("paper_id")) for selection in selections}
        ),
        "chunk_ids": sorted(
            {_text(selection.get("chunk_id")) for selection in selections}
        ),
        "source_kinds": sorted(
            {_text(selection.get("source_kind")) for selection in selections}
        ),
        "content_depths": sorted(
            {_text(selection.get("content_depth")) for selection in selections}
        ),
        "permissions": sorted(
            {_text(selection.get("permission")) for selection in selections}
        ),
        "attribution_required": True,
        "sources": sources,
    }
    existing = next(
        (
            contract
            for contract in contracts
            if isinstance(contract, dict)
            and _text(contract.get("claim_id")) == claim_id
        ),
        None,
    )
    if existing is not None:
        existing["verified_statement"] = text
        existing["allowed_assertion"] = text
        existing["source_envelope"] = envelope
        existing["revised_by"] = "supplementary_claim_revision"
        return
    contracts.append(
        {
            "claim_id": claim_id,
            "verified_statement": text,
            "allowed_assertion": text,
            "applicability": {},
            "generality_ceiling": "bounded_evidence",
            "attribution_required": True,
            "recommended_evidence_role": (
                "load_bearing" if role == "load_bearing" else "supporting"
            ),
            "editorial_weight_ceiling": (
                "load_bearing_candidate"
                if role == "load_bearing"
                else "supporting_candidate"
            ),
            "question_relevance": "direct",
            "prohibited_extrapolations": [],
            "contract_rationale": (
                "Supplementary claim revision source envelope."
            ),
            "source_envelope": envelope,
            "revised_by": "supplementary_claim_revision",
        }
    )


def _attach_resolved_placement(
    blueprint: dict[str, Any],
    claim_id: str,
    placement: Any,
) -> None:
    """Add a passed claim only to the resolved arc/subsection/adopted lists."""

    if not isinstance(placement, Mapping):
        return
    argument = placement.get("argument_arc")
    if isinstance(argument, Mapping):
        index = argument.get("index")
        arc = blueprint.get("argument_arc")
        if (
            isinstance(arc, list)
            and isinstance(index, int)
            and 0 <= index < len(arc)
            and isinstance(arc[index], dict)
        ):
            ids = arc[index].setdefault("claim_ids", [])
            if claim_id not in ids:
                ids.append(claim_id)
    subsection = placement.get("subsection_blueprint")
    if isinstance(subsection, Mapping):
        index = subsection.get("index")
        subsections = blueprint.get("subsection_blueprint")
        if (
            isinstance(subsections, list)
            and isinstance(index, int)
            and 0 <= index < len(subsections)
            and isinstance(subsections[index], dict)
        ):
            ids = subsections[index].setdefault("claim_ids", [])
            if claim_id not in ids:
                ids.append(claim_id)
    claim_usage = blueprint.get("claim_usage")
    if isinstance(claim_usage, dict):
        adopted = claim_usage.get("adopted_claim_ids")
        if isinstance(adopted, list) and claim_id not in adopted:
            adopted.append(claim_id)


def apply_revision_outcomes_to_probe(
    probe_report: Mapping[str, Any],
    outcomes: Mapping[str, Mapping[str, Any]],
    *,
    model_tier: str = DEFAULT_MODEL_TIER,
) -> dict[str, Any]:
    """Deep-copy the probe and apply revision outcomes to claims/blueprint."""

    revised = copy.deepcopy(dict(probe_report))
    claims = revised.get("final_claims")
    if not isinstance(claims, list):
        revised["final_claims"] = claims = []
    by_id = {
        _text(claim.get("claim_id")): claim
        for claim in claims
        if isinstance(claim, dict) and _text(claim.get("claim_id"))
    }
    blueprint = revised.get("verified_blueprint")
    if not isinstance(blueprint, dict):
        revised["verified_blueprint"] = blueprint = {}
    for claim_id, outcome in outcomes.items():
        claim = by_id.get(claim_id)
        if claim is None or not isinstance(outcome, Mapping):
            continue
        status = _text(outcome.get("final_status"))
        action = _text(outcome.get("final_action"))
        claim["supplementary_revision"] = {
            "schema_version": SCHEMA_VERSION,
            "applied": True,
            "final_status": status,
            "final_action": action or None,
            "ready_for_write": bool(outcome.get("ready_for_write")),
            "round_count": _int_or_zero(outcome.get("round_count")),
            "mode": _text(outcome.get("mode")),
            "reviewer_comments": list(outcome.get("reviewer_comments") or []),
        }
        if status == "passed":
            selections = outcome.get("final_selections") or []
            text = _text(outcome.get("final_claim_text"))
            permission = _text(outcome.get("permission")) or "qualified_only"
            claim["statement"] = text
            claim["candidate_chunk_ids"] = sorted(
                {
                    _text(selection.get("chunk_id"))
                    for selection in selections
                }
            )
            claim["verified_quotes"] = [
                {
                    "chunk_id": _text(selection.get("chunk_id")),
                    "quote": _text(selection.get("quote")),
                }
                for selection in selections
            ]
            claim["ready_for_write"] = True
            claim["quote_verified"] = True
            claim["verified_quote"] = (
                _text(selections[0].get("quote")) if selections else ""
            )
            claim["permission"] = permission
            claim["qualified_support_only"] = permission != "factual_support"
            claim["qualified_wording_present"] = (
                bool(claim.get("qualified_wording_present"))
                or permission != "factual_support"
            )
            original_caveats = [
                _text(caveat)
                for caveat in claim.get("caveats") or []
                if _text(caveat)
            ]
            revision_caveat = (
                "Revised after supplementary claim revision; scope bounded "
                "to the selected supplied evidence."
            )
            caveats = original_caveats + [revision_caveat]
            if not any(caveat == revision_caveat for caveat in caveats):
                caveats.append(revision_caveat)
            claim["caveats"] = list(dict.fromkeys(caveats))
            last_round = (outcome.get("rounds") or [{}])[-1]
            author_validation = last_round.get("author_validation") or {}
            rationale = _text(
                author_validation.get("rationale")
            ) or _text(outcome.get("final_reason"))
            _update_claim_components(
                claim,
                component_id=claim_id,
                text=text,
                selections=selections,
                rationale=rationale,
                permission=permission,
            )
            _upsert_claim_contract(
                revised,
                claim_id=claim_id,
                role=_text(outcome.get("role")),
                text=text,
                selections=selections,
                permission=permission,
            )
            _attach_resolved_placement(
                blueprint, claim_id, outcome.get("final_placement")
            )
        else:
            claim["ready_for_write"] = False
            if status == "deleted":
                claim["verified_quotes"] = []
                claim["candidate_chunk_ids"] = []
                claim["quote_verified"] = False
    counts = {
        "passed": sum(
            1
            for outcome in outcomes.values()
            if _text(outcome.get("final_status")) == "passed"
        ),
        "deleted": sum(
            1
            for outcome in outcomes.values()
            if _text(outcome.get("final_status")) == "deleted"
        ),
        "no_progress": sum(
            1
            for outcome in outcomes.values()
            if _text(outcome.get("final_status")) == "no_progress"
        ),
        "failed": sum(
            1
            for outcome in outcomes.values()
            if _text(outcome.get("final_status")) == "failed"
        ),
    }
    revised["supplementary_revision"] = {
        "schema_version": SCHEMA_VERSION,
        "model_tier": model_tier,
        "targets": sorted(outcomes.keys()),
        "outcomes": {
            claim_id: {
                "final_status": _text(outcome.get("final_status")),
                "final_action": _text(outcome.get("final_action")) or None,
                "ready_for_write": bool(outcome.get("ready_for_write")),
                "round_count": _int_or_zero(outcome.get("round_count")),
                "mode": _text(outcome.get("mode")),
                "permission": _text(outcome.get("permission")),
            }
            for claim_id, outcome in sorted(outcomes.items())
        },
        "next_command": {
            "module": "experiments.blueprint_writing_acceptance",
            "arguments": [
                "--probe-report",
                "revised_probe_report.json",
                "--model-tier",
                model_tier,
                "--live",
                "--policy-recheck",
            ],
            "note": (
                "SectionWriter is invoked separately by the acceptance "
                "runner; this pipeline never calls it."
            ),
        },
        "counts": counts,
    }
    return revised


def run_supplementary_claim_revision(
    *,
    probe_report: Mapping[str, Any],
    closure_reports: Any = None,
    closure_context: Mapping[str, Any] | None = None,
    baseline_units: Sequence[Mapping[str, Any]] | None = None,
    snapshot_units: Sequence[Mapping[str, Any]] | None = None,
    author_callback: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    reviewer_callback: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    model_tier: str = DEFAULT_MODEL_TIER,
    max_passes: int = MAX_PASSES,
) -> dict[str, Any]:
    """Run the full supplementary claim revision pipeline for all targets."""

    if closure_context is None:
        closure_context = merge_gap_closure_reports(closure_reports)
    dossiers = build_claim_revision_dossiers(
        probe_report,
        closure_context=closure_context,
        baseline_units=baseline_units,
        snapshot_units=snapshot_units,
    )
    usage_records: list[dict[str, Any]] = []
    outcomes: dict[str, dict[str, Any]] = {}
    for dossier in dossiers:
        outcome = _revise_claim(
            dossier,
            author_callback=author_callback,
            reviewer_callback=reviewer_callback,
            max_passes=max_passes,
            usage_records=usage_records,
        )
        outcomes[_text(dossier.get("claim_id"))] = outcome
    revised_probe = apply_revision_outcomes_to_probe(
        probe_report,
        outcomes,
        model_tier=model_tier,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "model_tier": model_tier,
        "targets": [dossier["claim_id"] for dossier in dossiers],
        "dossiers": dossiers,
        "outcomes": outcomes,
        "provider_usage": _summarize_usage(usage_records),
        "revised_probe_report": revised_probe,
        "counts": {
            "target_count": len(dossiers),
            "passed_count": sum(
                1
                for outcome in outcomes.values()
                if outcome["final_status"] == "passed"
            ),
            "deleted_count": sum(
                1
                for outcome in outcomes.values()
                if outcome["final_status"] == "deleted"
            ),
            "no_progress_count": sum(
                1
                for outcome in outcomes.values()
                if outcome["final_status"] == "no_progress"
            ),
            "failed_count": sum(
                1
                for outcome in outcomes.values()
                if outcome["final_status"] == "failed"
            ),
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_MODEL_TIER",
    "MAX_PASSES",
    "MAX_REVIEWER_ATTEMPTS",
    "TARGET_CLAIM_IDS",
    "normalize_snapshot_unit",
    "normalize_original_evidence_row",
    "build_claim_revision_dossiers",
    "validate_evidence_selections",
    "validate_author_proposal",
    "validate_reviewer_output",
    "resolve_blueprint_placement",
    "apply_revision_outcomes_to_probe",
    "run_supplementary_claim_revision",
]
