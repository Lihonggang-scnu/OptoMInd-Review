"""Request/source-bound required-output judgments, not scientific truth claims.

The existing directed reader may emit this optional structured assessment in its
normal response. Missing rows remain unresolved. No answer is inferred from a
paper, nonempty prose, retrieval rank, or navigation label.
"""
from __future__ import annotations
from collections.abc import Mapping
from .article_evidence import request_binding, validate_reader_result

SCHEMA = "review.output_assessments.v1"


def assessment_instructions(request, source_unit):
    return {"schema_version": SCHEMA, "instructions":
        "In the normal JSON response, optionally include output_assessments. Give one row per requested output. "
        "Use output_id, status (fulfilled/partial/unresolved/conflict), answer, applicability (direct/contextual/unclear), "
        "conditions, reason, and source_refs [{source_block_id, quote}]. Quote exact supplied original source text. "
        "Fulfilled requires every requested object condition to be applicable; missing values remain unresolved. "
        "Do not copy a requested condition unless the source establishes it. This is a source-attributed interpretation, not physical truth.",
        "outputs": [{"output_id": f"O{i:02d}", "description": value} for i, value in enumerate(request.required_outputs, 1)],
        "requested_conditions": dict(request.object_conditions),
        "source_blocks": [{"source_block_id": a["source_block_id"], "prefix": a["quote"][:160]} for a in source_unit["anchors"]]}


def apply_output_assessments(request, source_unit, rows, *, origin="directed_reader"):
    if not isinstance(rows, list):
        raise ValueError("output_assessments_list_required")
    outputs = {f"O{i:02d}": value for i, value in enumerate(request.required_outputs, 1)}
    anchors = {a["source_block_id"]: a for a in source_unit.get("anchors", [])}
    seen, assessments, fulfilled = set(), [], []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("output_assessment_object_required")
        ident = raw.get("output_id")
        if ident not in outputs or ident in seen:
            raise ValueError("output_assessment_unknown_or_duplicate")
        seen.add(ident)
        status = raw.get("status", "unresolved")
        if status not in {"fulfilled", "partial", "unresolved", "conflict"}:
            raise ValueError("output_assessment_status_invalid")
        refs = []
        for ref in raw.get("source_refs") or []:
            actual = anchors.get(ref.get("source_block_id"))
            quote = ref.get("quote")
            if actual is None or not isinstance(quote, str) or not quote or quote not in actual["quote"]:
                raise ValueError("output_assessment_quote_not_in_source")
            refs.append({**actual, "quote": quote})
        conditions = raw.get("conditions") or {}
        if not isinstance(conditions, Mapping):
            raise ValueError("output_assessment_conditions_invalid")
        applicability = raw.get("applicability", "unclear")
        if applicability not in {"direct", "contextual", "unclear"}:
            raise ValueError("output_assessment_applicability_invalid")
        if status == "fulfilled":
            if not refs or not str(raw.get("answer") or "").strip() or not str(raw.get("reason") or "").strip():
                raise ValueError("fulfilled_assessment_requires_answer_reason_anchors")
            if applicability != "direct" or any(conditions.get(k) != v for k, v in request.object_conditions.items()):
                status = "partial"
            elif source_unit["material_depth"] in {"metadata_only", "navigation"}:
                raise ValueError("navigation_is_not_evidence")
            else:
                fulfilled.append(outputs[ident])
        assessments.append({**dict(raw), "output_id": ident, "required_output": outputs[ident],
            "status": status, "source_refs": refs, "conditions": dict(conditions), "applicability": applicability})
    for ident, description in outputs.items():
        if ident not in seen:
            assessments.append({"output_id": ident, "required_output": description, "status": "unresolved",
                "reason": "No anchored assessment returned", "source_refs": [], "conditions": {}, "applicability": "unclear"})
    result = {**source_unit, "request_binding": request_binding(request), "fulfilled_outputs": fulfilled,
        "output_assessment": {"schema_version": SCHEMA, "request_binding": request_binding(request),
            "canonical_paper_id": source_unit["canonical_paper_id"], "snapshot_id": source_unit["snapshot_id"],
            "source_hash": source_unit["source_hash"], "origin": origin, "rows": assessments,
            "scientific_truth_validated": False}}
    conflicts = [a for a in assessments if a["status"] == "conflict"]
    if conflicts:
        result["counter_evidence"] = list(source_unit.get("counter_evidence") or []) + conflicts
    implications = [implication for a in assessments for implication in (a.get("implications") or [])]
    if implications:
        result["implications"] = implications  # C04 owns typed semantic validation.
    return validate_reader_result(source_unit, result)


def validate_assessed_unit(request, source_unit, assessment):
    for field in ("canonical_paper_id", "snapshot_id", "source_hash"):
        if assessment.get(field) != source_unit.get(field):
            raise ValueError("output_assessment_source_mismatch:" + field)
    if assessment.get("schema_version") != SCHEMA or assessment.get("request_binding") != request_binding(request):
        raise ValueError("output_assessment_request_mismatch")
    return apply_output_assessments(request, source_unit, assessment.get("rows"), origin=assessment.get("origin", "saved_assessment"))
