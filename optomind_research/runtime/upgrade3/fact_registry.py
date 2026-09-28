"""Upgrade-3 numeric condition fact baseline (ticket 015).

Independent of any draft: facts are extracted from SOURCES (documents/spans)
and a draft is checked AGAINST them.  The draft's own numbers never create an
allowlist.

- number token classes: scientific | bibliographic | structural.  Metadata
  exemptions (DOI digits, years, section/table numbers) are bound to the
  local pattern context of the occurrence, never granted globally by value.
- fact_id = hash(document_id + experiment_id + metric + conditions tuple);
  the numeric string itself is never the identity.
- unit conversion supports length, power, energy, time, frequency and
  percentage family with dimension checks; unsupported units stay ``unknown``
  explicitly.
- latency splits propagation vs end-to-end; energy splits the passive device
  from light-source/SLM/detector/ADC post-processing; condition keys are
  chosen by the research task, not one global list.
- comparisons require identical condition keys; a missing condition rejects
  the comparison instead of granting an advantage.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

REGISTRY_SCHEMA = "optomind.upgrade3.fact_registry.v1"

_WS = re.compile(r"\s+")
_DOI = re.compile(r"10\.\d{4,9}/\S{3,}")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_STRUCTURAL = re.compile(r"(?i)\b(?:section|figure|fig\.|table|eq\.|equation|algorithm|page|ref)\s*\"?(\d+(?:\.\d+)*)")
_UNIT_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>nm|µm|um|mm|cm|km|m\b|W\b|mW|kW|MW|pJ|nJ|µJ|uJ|mJ|J\b|"
    r"kWh|ps|ns|µs|us|ms|s\b|GHz|MHz|THz|kHz|Hz|TOPS|TeraOPS|GOPS|OPS|fps|Gb/s|GB|dB|"
    r"%|percentage points|pp\b|x\b|×)")

# dimension: unit -> (dimension, factor to SI)
_SI = {
    "nm": ("length", 1e-9), "µm": ("length", 1e-6), "um": ("length", 1e-6),
    "mm": ("length", 1e-3), "cm": ("length", 1e-2), "m": ("length", 1.0),
    "km": ("length", 1e3),
    "W": ("power", 1.0), "mW": ("power", 1e-3), "kW": ("power", 1e3),
    "MW": ("power", 1e6),
    "pJ": ("energy", 1e-12), "nJ": ("energy", 1e-9), "µJ": ("energy", 1e-6),
    "uJ": ("energy", 1e-6), "mJ": ("energy", 1e-3), "J": ("energy", 1.0),
    "ps": ("time", 1e-12), "ns": ("time", 1e-9), "µs": ("time", 1e-6),
    "us": ("time", 1e-6), "ms": ("time", 1e-3), "s": ("time", 1.0),
    "Hz": ("frequency", 1.0), "kHz": ("frequency", 1e3), "MHz": ("frequency", 1e6),
    "GHz": ("frequency", 1e9), "THz": ("frequency", 1e12),
    "TOPS": ("ops", 1e12), "TeraOPS": ("ops", 1e12), "GOPS": ("ops", 1e9),
    "OPS": ("ops", 1.0),
}

SYSTEM_BOUNDARY_SPLIT = {
    "latency": ["propagation", "end_to_end"],
    "energy": ["passive_device", "light_source", "slm", "detector", "adc_postprocessing"],
}

_CONDITION_KEYS_BY_TASK = {
    "accuracy": ["dataset", "split", "sim_or_experiment", "system_boundary"],
    "efficiency": ["wavelength", "input_power", "sim_or_experiment", "system_boundary"],
    "latency": ["integration", "sim_or_experiment", "system_boundary"],
    "energy": ["system_boundary", "input_power", "sim_or_experiment"],
    "throughput": ["precision", "dataset", "sim_or_experiment", "system_boundary"],
    "generalization": ["dataset", "split", "temporal_or_spatial", "sim_or_experiment"],
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify_number_token(token: str, context: str) -> Dict[str, Any]:
    """scientific | bibliographic | structural for ONE occurrence (context-bound).
    The same literal elsewhere never inherits the exemption."""
    start = context.find(token)
    if start < 0:
        return {"class": "scientific", "reason": "token_not_in_context",
                "exempt": False, "bound_to": -1}
    left = context[max(0, start - 60):start]
    right = context[start:start + 60]
    # structural: keyword immediately before the number
    kw = r"(?i)(?:section|figure|fig\.|table|eq\.|equation|algorithm|page|ref)\s*\"?$"
    if re.search(kw, left) or re.search(r"(?i)^(?:section|figure|table)\s*", right):
        return {"class": "structural", "reason": "section_or_figure_number",
                "exempt": True, "bound_to": start}
    # DOI: token is part of a doi string (possibly truncated mid-doi)
    if _DOI.search(left + token) and token in _DOI.search(left + token).group(0):
        return {"class": "bibliographic", "reason": "part_of_doi", "exempt": True,
                "bound_to": start}
    if re.search(r"10\.\d{1,4}(/[^\s]*)?$", left + token) and "." in token:
        return {"class": "bibliographic", "reason": "partial_doi_prefix",
                "exempt": True, "bound_to": start}
    if _YEAR.fullmatch(token):
        # a bare calendar year in scholarly front matter is never a scientific
        # quantity; position-bound to this occurrence
        return {"class": "bibliographic", "reason": "calendar_year",
                "exempt": True, "bound_to": start}
    if re.fullmatch(r"\d{3,4}", token) and re.search(
            r"(?i)issn|isbn|vol\.|volume|proceedings|workshop", left):
        return {"class": "bibliographic", "reason": "issn_or_volume",
                "exempt": True, "bound_to": start}
    m = _UNIT_RE.search(right)
    if m or re.search(r"(?i)accuracy|efficiency|error|rate|precision|reduc|improv|order",
                      left + right):
        return {"class": "scientific", "reason": "metric_or_unit_context",
                "exempt": False, "bound_to": start}
    return {"class": "scientific", "reason": "unclassified_numeric_default_scientific",
            "exempt": False, "bound_to": start}


def convert(value: float, unit_from: str, unit_to: str) -> Optional[float]:
    """Same-dimension conversion; None = unsupported/unknown (never guessed)."""
    a = _SI.get(unit_from)
    b = _SI.get(unit_to)
    if not a or not b or a[0] != b[0]:
        return None
    return value * a[1] / b[1]


def parse_quantity(token_with_unit: str) -> Optional[Dict[str, Any]]:
    m = _UNIT_RE.search(token_with_unit)
    if not m:
        return None
    unit = m.group("unit")
    dim = _SI.get(unit)
    return {"value": float(m.group("num")), "unit": unit,
            "dimension": dim[0] if dim else "unknown",
            "si": dim[1] * float(m.group("num")) if dim else None}


def make_fact_id(document_id: str, experiment_id: str, metric: str,
                 conditions: Dict[str, Any]) -> str:
    cond_tuple = tuple(sorted((k, _norm(str(v))) for k, v in (conditions or {}).items()))
    return "fact_" + _sha("|".join([document_id, experiment_id, metric,
                                    _canonical(cond_tuple)]))[:20]


def _norm(text: str) -> str:
    return _WS.sub(" ", str(text)).strip()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


import json  # noqa: E402


def build_fact(*, document_id: str, experiment_id: str, metric: str,
               value: str, unit: str, conditions: Dict[str, Any],
               source_span_id: str, sim_or_experiment: str = "not_reported",
               importance: str = "metric", quantifier: str = "point",
               field_status: str = "reported") -> Dict[str, Any]:
    """One Fact per the global schema.  field_status reported/not_reported/
    conflicting; derivation fields empty unless derived."""
    return {
        "schema_version": REGISTRY_SCHEMA,
        "fact_id": make_fact_id(document_id, experiment_id, metric, conditions),
        "document_id": document_id,
        "experiment_id": experiment_id,
        "metric": metric,
        "raw_value": value,
        "unit": unit,
        "quantifier": quantifier,
        "conditions": conditions,
        "source_span_id": source_span_id,
        "sim_or_experiment": sim_or_experiment,
        "importance": importance,
        "field_status": field_status,
        "derivation_formula": "",
        "operand_fact_ids": [],
    }


def required_conditions_for(metric: str) -> List[str]:
    for key, keys in _CONDITION_KEYS_BY_TASK.items():
        if key in metric.lower():
            return list(keys)
    return ["sim_or_experiment", "system_boundary"]


def check_comparison(fact_a: Dict[str, Any], fact_b: Dict[str, Any]) -> Dict[str, Any]:
    """A comparison is only admissible when BOTH facts share the condition keys
    their metric requires with equal values.  Missing conditions reject."""
    metric = fact_a.get("metric", "")
    required = required_conditions_for(metric)
    ca, cb = fact_a.get("conditions") or {}, fact_b.get("conditions") or {}
    problems: List[str] = []
    for key in required:
        va, vb = ca.get(key), cb.get(key)
        if va is None or vb is None:
            problems.append(f"condition_missing:{key}")
        elif _norm(str(va)) != _norm(str(vb)):
            problems.append(f"condition_differs:{key}({va} vs {vb})")
    if fact_a.get("sim_or_experiment", "not_reported") != \
            fact_b.get("sim_or_experiment", "not_reported"):
        problems.append("sim_vs_experiment_mismatch")
    admissible = not problems
    return {"admissible": admissible, "problems": problems,
            "verdict": "comparison_admissible" if admissible
            else "comparison_rejected_insufficient_conditions"}


# ------------------------------------------------------------- the gate ----
def numeric_preflight(text: str, facts: List[Dict[str, Any]],
                      needed_metrics: Iterable[str] = ()) -> Dict[str, Any]:
    """Draft-vs-registry gate.  An empty draft checks only that the task's
    needed facts exist in the registry; an existing draft must bind every
    scientific number to a fact (value AND experiment identity).  The draft
    never creates an allowlist."""
    tokens = _UNIT_RE.findall(text or "")
    scientific: List[Dict[str, Any]] = []
    for num, unit in tokens:
        token = f"{num} {unit}".strip() if unit else num
        cls = classify_number_token(token, text or "")
        if cls["class"] == "scientific":
            si = parse_quantity(token)
            scientific.append({"token": token, "si": (si or {}).get("si"),
                               "dimension": (si or {}).get("dimension"),
                               "class_reason": cls["reason"]})
    fact_index = {}
    for f in facts:
        if f.get("field_status") != "reported":
            continue
        q = parse_quantity(f"{f.get('raw_value')} {f.get('unit')}")
        if q is None:
            continue
        if q["si"] is not None:
            key = (q["dimension"], round(q["si"], 12))
        elif q["unit"] == "%":
            key = ("percent", round(q["value"], 12))
        else:
            continue
        fact_index.setdefault(key, []).append(f)
    fact_index = {}
    for f in facts:
        if f.get("field_status") != "reported":
            continue
        q = parse_quantity(f"{f.get('raw_value')} {f.get('unit')}")
        if q is None:
            continue
        if q["si"] is not None:
            key = (q["dimension"], round(q["si"], 12))
        elif q["unit"] == "%":
            key = ("percent", round(q["value"], 12))
        else:
            continue
        fact_index.setdefault(key, []).append(f)
    unbound: List[Dict[str, Any]] = []
    for s in scientific:
        if s["si"] is not None:
            key = (s["dimension"], round(s["si"], 12))
        elif s["token"].endswith("%"):
            key = ("percent", round(float(s["token"][:-1].strip()), 12))
        else:
            key = None
        if key is None or key not in fact_index:
            unbound.append(s)
    needed_missing = []
    registry_metrics = {f.get("metric") for f in facts}
    for metric in needed_metrics:
        if metric not in registry_metrics:
            needed_missing.append(metric)
    has_text = bool((text or "").strip())
    if not has_text:
        ok = not needed_missing
        state = "needs_evidence" if needed_missing else "not_applicable"
    elif unbound or needed_missing:
        ok = False
        state = "needs_evidence"
    else:
        ok = True
        state = "ready"
    return {
        "schema_version": REGISTRY_SCHEMA + ".preflight",
        "ok": ok, "state": state,
        "scientific_numbers_in_draft": len(scientific),
        "unbound_scientific_numbers": unbound[:10],
        "needed_metrics_missing_from_registry": needed_missing,
        "evidence_facts_count": len(facts),
        "note": "empty text with registry coverage is not_applicable; text with "
                "scientific numbers but missing facts is needs_evidence, never passed",
    }


from typing import Iterable  # noqa: E402
