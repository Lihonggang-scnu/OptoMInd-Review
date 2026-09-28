"""Guardrails for JSON-only LLM outputs."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping


TASK_TYPES = {"explicit", "semi_explicit", "ambiguous"}
PROPERTIES = {"T", "R", "A", "emissivity"}
TARGET_TYPES = {"lower_bound", "upper_bound", "equal", "range"}
SOURCES = {"user", "literature", "default_rule", "agent_inferred"}
SEVERITIES = {"none", "minor", "major", "blocking"}


def extract_json_from_text(text: str) -> Dict[str, Any]:
    """Extract a JSON object from plain or fenced model text."""

    raw = str(text or "").strip()
    if not raw:
        raise ValueError("Empty LLM output.")
    fenced = re.search(r"```(?:json)?\s*(?P<body>.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        raw = fenced.group("body").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(raw[start : end + 1])
    if isinstance(parsed, list):
        return {"items": parsed}
    if not isinstance(parsed, dict):
        raise ValueError("LLM JSON output must be an object or list.")
    return parsed


def _num(value: Any, name: str, errors: List[str]) -> float | None:
    try:
        return float(value)
    except Exception:
        errors.append(f"{name} must be numeric.")
        return None


def _validate_unit(value: Any, name: str, errors: List[str]) -> None:
    number = _num(value, name, errors)
    if number is None:
        return
    if number < -1e-9 or number > 1.0 + 1e-9:
        errors.append(f"{name} must be in [0, 1].")


def _validate_band(value: Any, name: str, errors: List[str]) -> None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        errors.append(f"{name} must contain two numeric wavelengths.")
        return
    lo = _num(value[0], f"{name}[0]", errors)
    hi = _num(value[1], f"{name}[1]", errors)
    if lo is not None and hi is not None and hi <= lo:
        errors.append(f"{name} end must be greater than start.")


def _items(data: Any, key: str = "items") -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [dict(x) for x in data if isinstance(x, Mapping)]
    if isinstance(data, Mapping):
        value = data.get(key, data.get("evidence_items", data.get("band_objectives", [])))
        if isinstance(value, list):
            return [dict(x) for x in value if isinstance(x, Mapping)]
    return []


def validate_task_planner_output(data: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    task_type = str(data.get("task_type", "")).strip()
    if task_type not in TASK_TYPES:
        errors.append(f"task_type must be one of {sorted(TASK_TYPES)}.")
    if not str(data.get("application_profile", data.get("application", ""))).strip():
        errors.append("application_profile is required.")
    if not isinstance(data.get("needs_literature", False), bool):
        errors.append("needs_literature must be boolean.")
    if not isinstance(data.get("needs_human_review", False), bool):
        errors.append("needs_human_review must be boolean.")
    raw_constraints = data.get("key_constraints", data.get("band_objectives", []))
    if raw_constraints and not isinstance(raw_constraints, list):
        errors.append("key_constraints must be a list.")
    if isinstance(raw_constraints, list):
        for idx, item in enumerate(raw_constraints):
            if not isinstance(item, Mapping):
                errors.append(f"key_constraints[{idx}] must be an object.")
    for idx, objective in enumerate(_items(data, "key_constraints")):
        errors.extend(_validate_objective(objective, f"key_constraints[{idx}]"))
    return errors


def validate_evidence_items(data: Dict[str, Any] | List[Any]) -> List[str]:
    errors: List[str] = []
    if isinstance(data, Mapping) and data.get("status") == "evidence_insufficient":
        return errors
    for idx, item in enumerate(_items(data, "evidence_items")):
        prefix = f"evidence_items[{idx}]"
        if not item.get("source_id"):
            errors.append(f"{prefix}.source_id is required.")
        if not str(item.get("title", item.get("source_title", ""))).strip():
            errors.append(f"{prefix}.title is required.")
        if not str(item.get("extracted_claim", "")).strip():
            errors.append(f"{prefix}.extracted_claim is required.")
        _validate_unit(item.get("confidence", 0.0), f"{prefix}.confidence", errors)
        band = item.get("related_band_nm")
        if band is not None:
            _validate_band(band, f"{prefix}.related_band_nm", errors)
        else:
            errors.append(f"{prefix}.related_band_nm is required.")
        suggested = item.get("suggested_objective")
        if isinstance(suggested, Mapping):
            errors.extend(_validate_evidence_suggestion(dict(suggested), f"{prefix}.suggested_objective"))
    return errors


def _validate_evidence_suggestion(suggested: Mapping[str, Any], prefix: str) -> List[str]:
    """Validate a weak evidence suggestion.

    Evidence may support a direction/mechanism without a numeric threshold, so
    unlike BandObjective this does not require value/value_range.
    """

    errors: List[str] = []
    prop = str(suggested.get("property", "")).strip()
    if prop and prop not in PROPERTIES:
        errors.append(f"{prefix}.property must be one of {sorted(PROPERTIES)}.")
    target_type = str(suggested.get("target_type", "")).strip()
    if target_type and target_type not in TARGET_TYPES and target_type != "context_dependent":
        errors.append(f"{prefix}.target_type must be one of {sorted(TARGET_TYPES)} or context_dependent.")
    if suggested.get("band_nm") is not None:
        _validate_band(suggested.get("band_nm"), f"{prefix}.band_nm", errors)
    if suggested.get("value") is not None:
        _validate_unit(suggested.get("value"), f"{prefix}.value", errors)
    return errors


def validate_band_objectives(data: Dict[str, Any] | List[Any]) -> List[str]:
    errors: List[str] = []
    for idx, objective in enumerate(_items(data, "band_objectives")):
        errors.extend(_validate_objective(objective, f"band_objectives[{idx}]"))
    return errors


def _validate_objective(objective: Mapping[str, Any], prefix: str) -> List[str]:
    errors: List[str] = []
    prop = str(objective.get("property", "")).strip()
    if prop not in PROPERTIES:
        errors.append(f"{prefix}.property must be one of {sorted(PROPERTIES)}.")
    target_type = str(objective.get("target_type", "")).strip()
    if target_type not in TARGET_TYPES:
        errors.append(f"{prefix}.target_type must be one of {sorted(TARGET_TYPES)}.")
    source = str(objective.get("source", "user")).strip()
    if source not in SOURCES:
        errors.append(f"{prefix}.source must be one of {sorted(SOURCES)}.")
    _validate_band(objective.get("band_nm"), f"{prefix}.band_nm", errors)
    if target_type == "range":
        _validate_band(objective.get("value_range"), f"{prefix}.value_range", errors)
        vr = objective.get("value_range")
        if isinstance(vr, (list, tuple)) and len(vr) == 2:
            _validate_unit(vr[0], f"{prefix}.value_range[0]", errors)
            _validate_unit(vr[1], f"{prefix}.value_range[1]", errors)
    else:
        _validate_unit(objective.get("value"), f"{prefix}.value", errors)
    weight = _num(objective.get("weight", 1.0), f"{prefix}.weight", errors)
    if weight is not None and weight < 0:
        errors.append(f"{prefix}.weight must be >= 0.")
    _validate_unit(objective.get("confidence", 0.0), f"{prefix}.confidence", errors)
    return errors


def validate_llm_review_output(data: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(data.get("review_passed", False), bool):
        errors.append("review_passed must be boolean.")
    severity = str(data.get("severity", "none")).strip()
    if severity not in SEVERITIES:
        errors.append(f"severity must be one of {sorted(SEVERITIES)}.")
    if not isinstance(data.get("issues", []), list):
        errors.append("issues must be a list.")
    if "route_back_to" not in data:
        errors.append("route_back_to is required.")
    return errors


def repair_json_with_llm_or_rules(raw_text: str, schema_name: str, agent_name: str) -> Dict[str, Any]:
    """Best-effort JSON repair without trusting prose.

    This rule-first repair intentionally does not call the model recursively in
    the current implementation. The caller should fall back to deterministic
    mock logic when this still fails.
    """

    try:
        return extract_json_from_text(raw_text)
    except Exception:
        cleaned = str(raw_text or "").strip()
        cleaned = cleaned.replace("“", '"').replace("”", '"').replace("'", '"')
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        try:
            return extract_json_from_text(cleaned)
        except Exception as exc:
            return {
                "error": "json_repair_failed",
                "schema_name": schema_name,
                "agent_name": agent_name,
                "error_type": type(exc).__name__,
            }
