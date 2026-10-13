"""Versioned, offline PLAN loading and retrieval execution contracts.

Formatting validity never asserts scientific or semantic completeness. Free-text
requirements remain visible and unapplied unless an explicit executable form exists.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def load_plan(source, *, expected_question=None, expected_hash=None, assumptions=()):
    from .query_plan import validate_plan, load_field_dictionary
    raw = json.loads(Path(source).read_text(encoding="utf-8-sig")) if isinstance(source, (str, Path)) else dict(source)
    if not isinstance(raw, Mapping):
        raise ValueError("plan_must_be_object")
    wrapper = raw
    if "query_plan_v2" in raw:
        if raw.get("status") not in {"primary_valid_v2", "primary_valid_v2_after_revision"}:
            raise ValueError("legacy_wrapper_not_valid")
        raw = raw["query_plan_v2"]
    wrapped = "plan" in raw
    if wrapped and (raw.get("status") != "ok" or (raw.get("degradation") or {}).get("degraded")):
        raise ValueError("plan_wrapper_not_valid")
    plan = raw.get("plan") if wrapped else raw
    if not isinstance(plan, Mapping):
        raise ValueError("plan_must_be_object")
    report = validate_plan(plan, fields=load_field_dictionary())
    if not report.get("ok"):
        raise ValueError("plan_invalid:" + json.dumps(report.get("errors")))
    if wrapped and raw.get("refused_queries"):
        raise ValueError("plan_has_refused_queries")
    original = str(raw.get("original_question") or wrapper.get("original_question") or plan.get("original_question") or plan.get("question_en") or "")
    if plan.get("original_question") is not None and plan["original_question"] != original:
        raise ValueError("plan_original_question_conflicts_with_wrapper")
    if expected_question is not None and original != expected_question:
        raise ValueError("plan_question_mismatch")
    digest = fingerprint(plan)
    if expected_hash is not None and digest != expected_hash:
        raise ValueError("plan_hash_mismatch")
    if (plan.get("ambiguity") or {}).get("needs_user_input") and not assumptions:
        raise ValueError("plan_needs_clarification_or_explicit_assumptions")
    receipt = {
        "schema_version": "optomind.plan_receipt.v1", "original_question": original,
        "plan_sha256": digest, "selected_version": plan.get("schema_version"),
        "validator_fingerprint": report.get("validator_fingerprint"),
        "planner_status": raw.get("status", "bare_plan_validated"),
        "format_valid": True, "semantic_coverage": "not_assessed",
        "assumptions": list(assumptions), "source_sha256": fingerprint(wrapper),
        "source_format": "outcome" if wrapped else "bare_plan",
        "round_usages": raw.get("round_usages", []),
        "requirements": {"original_question": original, "declared": plan.get("requirements", []), "facets": [
            {"facet_id": f["id"], "ask": f["ask"], "requirement_mapping": f.get("requirement_mapping", []),
             "mapping_status": "declared" if f.get("requirement_mapping") else "unverified"}
            for f in plan.get("facets", [])]},
    }
    return dict(plan), receipt


# Exact gateway capabilities, not a claim that arbitrary natural language filters
# are understood. Metadata filters unsupported by snippet search remain visible.
PROVIDER_CAPABILITIES = {
    "paper_search": {"publication_type": "publication_types", "fields_of_study": "fields_of_study",
                     "publication_date_or_year": "publication_date_or_year", "year": "year"},
    "snippet_search": {"publication_date_or_year": "publication_date_or_year"},
}


def compile_constraints(plan, facet, source):
    params, postfilters, unsupported = {}, {}, []
    for key, value in (facet.get("filters") or {}).items():
        if not value:
            continue
        parameter = PROVIDER_CAPABILITIES[source].get(key)
        if parameter:
            params[parameter] = value
        elif key == "text_availability" and source == "paper_search" and value == ["open_access"]:
            params["open_access_pdf"] = True
        else:
            unsupported.append({"field": key, "value": value, "reason": "provider_capability_unavailable"})
    # Only explicitly typed literal matching is safe to execute locally.
    for key, value in (facet.get("literal_postfilters") or {}).items():
        if key in {"include_terms", "exclude_terms"}:
            postfilters[key] = list(value)
        else:
            unsupported.append({"field": key, "value": value, "reason": "unknown_postfilter"})
    criteria = plan.get("criteria") or {}
    for key, value in {"additional_constraints": plan.get("additional_constraints"),
                       "must_exclude": facet.get("must_exclude"),
                       "must_include_topic": criteria.get("must_include_topic"),
                       "must_exclude_domain": criteria.get("must_exclude_domain"),
                       "synonyms": criteria.get("synonyms"), "seeds": plan.get("seeds")}.items():
        if value:
            unsupported.append({"field": key, "value": value,
                                "reason": "semantic_requirement_requires_explicit_execution_policy"})
    return params, postfilters, unsupported


def aggregate_planner_usage(package, *, last_usage, last_repair_usage, cost_function):
    """Count every v2 attempt once, including rejected/revision rounds."""
    rounds = (package.get("query_plan_v2") or {}).get("round_usages")
    usages = list(rounds) if rounds else [last_usage or {}, last_repair_usage or {}]
    costs = [cost_function(usage) for usage in usages]
    return {"cost_cny": round(sum(r[0] for r in costs), 6),
            "input_tokens": sum(r[1] for r in costs),
            "output_tokens": sum(r[2] for r in costs),
            "usage_rounds": len(rounds) if rounds else sum(bool(u) for u in usages)}
