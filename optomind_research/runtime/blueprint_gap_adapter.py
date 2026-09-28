"""Convert a review blueprint into deterministic follow-up retrieval tasks.

This module is deliberately an adapter, not a retriever.  It performs no
network, model, or first-round acquisition work.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "optomind.blueprint_gap_tasks.v1"
LITERATURE_ROUTES = (
    {
        "order": 1,
        "route": "s2_structured_body",
        "accept_when": "A non-empty structured body passage directly addresses the task target.",
        "maximum_permission": "contextual_or_qualified_support",
    },
    {
        "order": 2,
        "route": "public_oa_fulltext",
        "accept_when": "A non-empty public open-access full-text passage directly addresses the task target.",
        "maximum_permission": "factual_support",
    },
    {
        "order": 3,
        "route": "abstract_claim",
        "accept_when": "A non-empty abstract passage addresses the task target when earlier routes are empty.",
        "maximum_permission": "background_only",
        "factual_support_allowed": False,
    },
)

_VISUAL_WORDS = frozenset(
    "visual figure figures diagram schematic plot graph chart image micrograph photograph table panel".split()
)
_STOP_WORDS = frozenset(
    "a an and are as at be by for from in into is it of on or that the this to with one needs need check "
    "whether evidence related review source sources support supporting exact".split()
)
_SUFFICIENT = frozenset({"sufficient", "resolved", "closed", "complete", "covered", "no_gap"})
_PROCEDURAL_RISK_MARKERS = (
    "candidate anchor",
    "exact source-span verification",
    "before scientific writing",
    "source-span binding",
    "must be verified before writing",
)


def _blueprint(value: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("blueprint", "unified_blueprint", "review_blueprint"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and (nested.get("sections") or nested.get("high_value_gap_seeds")):
            return nested
    return value


def _text(value: Any) -> str:
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, Mapping):
        for key in ("gap", "risk", "description", "statement", "label", "axis_description", "query", "title"):
            if value.get(key):
                return _text(value[key])
    return ""


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) > 1 and token not in _STOP_WORDS
    )


def _similar(left: str, right: str) -> bool:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return left.lower() == right.lower()
    overlap = len(a & b) / len(a | b)
    containment = len(a & b) / min(len(a), len(b))
    sequence = SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()
    return overlap >= 0.62 or containment >= 0.82 or sequence >= 0.86


def _is_sufficient(value: Any, parent: Mapping[str, Any] | None = None) -> bool:
    fields: list[Any] = []
    if isinstance(value, Mapping):
        fields.extend(value.get(k) for k in ("status", "coverage_status", "resolution", "disposition", "action"))
        if value.get("evidence_sufficient") is True or value.get("resolved") is True:
            return True
    if parent:
        fields.extend(parent.get(k) for k in ("evidence_status", "coverage_status", "gap_status"))
        if parent.get("evidence_sufficient") is True:
            return True
    return any(str(field or "").strip().lower() in _SUFFICIENT for field in fields)


def _is_visual(value: Any, description: str) -> bool:
    if isinstance(value, Mapping):
        kind = str(value.get("task_type") or value.get("gap_type") or value.get("modality") or "").lower()
        if kind in {"visual", "visual_only", "figure", "image"} or value.get("visual_only") is True:
            return True
        if kind in {"text", "literature", "mixed"}:
            return False
    return bool(_tokens(description) & _VISUAL_WORDS)


def _is_procedural_risk(description: str) -> bool:
    """Exclude workflow reminders that cannot be fixed by more literature."""
    lowered = description.casefold()
    return any(marker in lowered for marker in _PROCEDURAL_RISK_MARKERS)


def _iter_dicts(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _iter_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_dicts(nested)


def _units(material_units: Mapping[str, Any] | list[Any] | None) -> list[Mapping[str, Any]]:
    if isinstance(material_units, list):
        return [item for item in material_units if isinstance(item, Mapping)]
    if not isinstance(material_units, Mapping):
        return []
    for key in ("units", "material_units", "items", "records"):
        value = material_units.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _permission(unit: Mapping[str, Any]) -> str:
    provenance = ((unit.get("audit") or {}).get("source_provenance") or {}) if isinstance(unit.get("audit"), Mapping) else {}
    quality = ((unit.get("durable_content_card") or {}).get("content_quality") or {}) if isinstance(unit.get("durable_content_card"), Mapping) else {}
    return str(provenance.get("use_permission") or quality.get("evidence_ceiling") or unit.get("use_permission") or "").lower()


def _propositions(unit: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in _iter_dicts(unit):
        proposition_id = str(item.get("proposition_id") or "")
        if proposition_id and proposition_id not in seen:
            found.append(item)
            seen.add(proposition_id)
    return found


def evidence_snapshot(material_units: Mapping[str, Any] | list[Any] | None) -> dict[str, Any]:
    """Return auditable proposition and evidence-permission counts."""
    units = _units(material_units)
    counts: Counter[str] = Counter()
    proposition_count = 0
    proposition_bound_units = 0
    for unit in units:
        permission = _permission(unit)
        if permission == "factual_support":
            counts["factual"] += 1
        elif permission in {"contextual_or_qualified_support", "contextual_support", "qualified_support"}:
            counts["contextual"] += 1
        else:
            counts["background"] += 1
        propositions = _propositions(unit)
        proposition_count += len(propositions)
        proposition_bound_units += bool(propositions)
    return {
        "material_units_provided": material_units is not None,
        "material_unit_count": len(units),
        "proposition_bound_unit_count": proposition_bound_units,
        "proposition_count": proposition_count,
        "permission_counts": {
            "factual_support": counts["factual"],
            "contextual_or_qualified_support": counts["contextual"],
            "background_only": counts["background"],
        },
        "factual_support_count": counts["factual"],
        "contextual_or_qualified_support_count": counts["contextual"],
        "background_only_count": counts["background"],
    }


def _material_index(material_units: Mapping[str, Any] | list[Any] | None) -> dict[str, list[Mapping[str, Any]]]:
    index: dict[str, list[Mapping[str, Any]]] = {}
    for unit in _units(material_units):
        identity = unit.get("identity") if isinstance(unit.get("identity"), Mapping) else {}
        keys = {str(unit.get("unit_id") or "")}
        keys.update(str(identity.get(k) or "") for k in ("chunk_id", "visual_chunk_id", "paper_id", "doi"))
        for key in keys - {""}:
            index.setdefault(key, []).append(unit)
    return index


def _missing_axes(bp: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    coverage = bp.get("scope_coverage_status") if isinstance(bp.get("scope_coverage_status"), Mapping) else {}
    seed = coverage.get("seed_axis_coverage") if isinstance(coverage.get("seed_axis_coverage"), Mapping) else {}
    missing = _as_list(seed.get("missing_axes") or seed.get("missing_axis_ids") or coverage.get("missing_seed_axes"))
    descriptions: dict[str, str] = {}
    for source in (bp.get("user_seed_axes"), (bp.get("input_context") or {}).get("user_seed_axes") if isinstance(bp.get("input_context"), Mapping) else None, seed.get("expected_axes")):
        if isinstance(source, list):
            for axis in source:
                if isinstance(axis, Mapping):
                    axis_id = str(axis.get("axis_id") or axis.get("id") or "")
                    descriptions[axis_id] = _text(axis)
    result = []
    for axis in missing:
        if isinstance(axis, Mapping):
            result.append(axis)
        else:
            axis_id = str(axis)
            result.append({"axis_id": axis_id, "description": descriptions.get(axis_id, axis_id)})
    return result


def _task_candidate(
    *, source_kind: str, value: Any, section_id: str = "", claim_id: str = "", parent: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    description = _text(value)
    if not description or _is_sufficient(value, parent):
        return None
    if source_kind == "section_evidence_risk" and _is_procedural_risk(description):
        return None
    query = _text(value.get("query")) if isinstance(value, Mapping) else description
    task_type = "visual_acquisition" if _is_visual(value, description) else "literature_retrieval"
    stop = _text(value.get("stop_condition")) if isinstance(value, Mapping) else ""
    bounded_stop = (
            "Stop after one directly relevant, traceable visual asset is accepted, or after the three named "
            "acquisition checks yield no eligible asset."
            if task_type == "visual_acquisition"
            else "Stop after one directly relevant passage with the required permission is accepted, or after all "
            "three ordered routes return no eligible evidence."
    )
    stop = f"{stop} {bounded_stop}".strip() if stop else bounded_stop
    return {
        "task_type": task_type,
        "source_kinds": [source_kind],
        "section_id": section_id or None,
        "claim_id": claim_id or None,
        "target": description,
        "query": query or description,
        "stop_condition": stop,
    }


def _permission_deficits(bp: Mapping[str, Any], material_units: Mapping[str, Any] | list[Any] | None) -> list[dict[str, Any]]:
    if material_units is None:
        return []
    index = _material_index(material_units)
    deficits = []
    for section in bp.get("sections") or []:
        if not isinstance(section, Mapping) or _is_sufficient(section):
            continue
        section_id = str(section.get("section_id") or "")
        for claim in section.get("claims") or []:
            if not isinstance(claim, Mapping) or _is_sufficient(claim, section):
                continue
            requirement = str(claim.get("evidence_requirement") or claim.get("required_permission") or "").lower()
            if requirement not in {"factual", "factual_support", "direct_fact"}:
                continue
            identifiers = [str(item) for item in claim.get("supporting_text_chunk_ids") or []]
            matched = [unit for identifier in identifiers for unit in index.get(identifier, [])]
            if any(_permission(unit) == "factual_support" for unit in matched):
                continue
            claim_id = str(claim.get("claim_id") or "")
            target = _text(claim) or f"Factual evidence for claim {claim_id}"
            deficits.append({
                "gap": target,
                "query": target,
                "gap_type": "literature",
                "stop_condition": "Stop after one proposition-bound passage with factual_support permission is accepted, or after all three ordered routes are exhausted; an abstract cannot close this deficit.",
                "section_id": section_id,
                "claim_id": claim_id,
            })
    return deficits


def _deduplicate(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        duplicate = next(
            (task for task in result if task["task_type"] == candidate["task_type"] and _similar(task["target"], candidate["target"])),
            None,
        )
        if duplicate is None:
            result.append(candidate)
            continue
        duplicate["source_kinds"] = sorted(set(duplicate["source_kinds"] + candidate["source_kinds"]))
        for field in ("section_id", "claim_id"):
            refs = duplicate.setdefault(f"{field}s", [])
            refs.extend(value for value in (duplicate.get(field), candidate.get(field)) if value)
            duplicate[f"{field}s"] = sorted(set(refs))
            if duplicate.get(field) != candidate.get(field):
                duplicate[field] = None
    for ordinal, task in enumerate(result, 1):
        fingerprint = "|".join((task["task_type"], " ".join(sorted(_tokens(task["target"])))))
        task["task_id"] = f"gap-task-{ordinal:03d}-{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:10]}"
        if task["task_type"] == "literature_retrieval":
            task["retrieval_policy"] = {
                "route_order": [dict(route) for route in LITERATURE_ROUTES],
                "route_acceptance": "first_non_empty_route",
                "abstract_policy": "Abstract evidence is background-only and must never be promoted to factual support.",
                "rerun_first_round_acquisition": False,
            }
        else:
            task["acquisition_policy"] = {
                "parallel_to_text_retrieval": True,
                "text_route_fallback_allowed": False,
                "rerun_first_round_acquisition": False,
            }
    return result


def build_blueprint_gap_tasks(
    blueprint: Mapping[str, Any], material_units: Mapping[str, Any] | list[Any] | None = None
) -> dict[str, Any]:
    """Build the English, auditable follow-up-task JSON contract."""
    bp = _blueprint(blueprint)
    candidates: list[dict[str, Any]] = []
    for seed in _as_list(bp.get("high_value_gap_seeds")):
        section_id = str(seed.get("section_id") or "") if isinstance(seed, Mapping) else ""
        claim_id = str(seed.get("claim_id") or "") if isinstance(seed, Mapping) else ""
        task = _task_candidate(source_kind="high_value_gap_seed", value=seed, section_id=section_id, claim_id=claim_id)
        if task:
            candidates.append(task)
    for section in bp.get("sections") or []:
        if not isinstance(section, Mapping) or _is_sufficient(section):
            continue
        section_id = str(section.get("section_id") or "")
        for risk in _as_list(section.get("evidence_risks")):
            claim_id = str(risk.get("claim_id") or "") if isinstance(risk, Mapping) else ""
            task = _task_candidate(source_kind="section_evidence_risk", value=risk, section_id=section_id, claim_id=claim_id, parent=section)
            if task:
                candidates.append(task)
        for deficit in _as_list(section.get("evidence_permission_deficits")):
            claim_id = str(deficit.get("claim_id") or "") if isinstance(deficit, Mapping) else ""
            task = _task_candidate(source_kind="evidence_permission_deficit", value=deficit, section_id=section_id, claim_id=claim_id, parent=section)
            if task:
                candidates.append(task)
    for deficit in _as_list(bp.get("evidence_permission_deficits")):
        section_id = str(deficit.get("section_id") or "") if isinstance(deficit, Mapping) else ""
        claim_id = str(deficit.get("claim_id") or "") if isinstance(deficit, Mapping) else ""
        task = _task_candidate(source_kind="evidence_permission_deficit", value=deficit, section_id=section_id, claim_id=claim_id)
        if task:
            candidates.append(task)
    for axis in _missing_axes(bp):
        task = _task_candidate(source_kind="missing_user_seed_axis", value=axis)
        if task:
            candidates.append(task)
    for deficit in _permission_deficits(bp, material_units):
        task = _task_candidate(
            source_kind="evidence_permission_deficit",
            value=deficit,
            section_id=str(deficit.get("section_id") or ""),
            claim_id=str(deficit.get("claim_id") or ""),
        )
        if task:
            candidates.append(task)
    tasks = _deduplicate(candidates)
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_language": "English",
        "source_blueprint_schema_version": bp.get("schema_version"),
        "adapter_scope": "task_creation_only",
        "execution_prohibitions": [
            "Do not call a network service.",
            "Do not call Qwen or another language model.",
            "Do not rerun first-round acquisition.",
        ],
        "existing_evidence_snapshot": evidence_snapshot(material_units),
        "portfolio_guardrails": {
            "final_review_reference_maximum": 200,
            "background_only_fraction_maximum": 0.25,
            "policy": "Guardrails constrain portfolio assembly; they are not retrieval quotas and do not force task creation.",
        },
        "task_count": len(tasks),
        "tasks": tasks,
    }


# A concise alias for callers that use adapter terminology.
adapt_blueprint_gaps = build_blueprint_gap_tasks
