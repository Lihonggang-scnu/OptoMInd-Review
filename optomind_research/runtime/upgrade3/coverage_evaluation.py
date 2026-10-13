"""Offline scoring of frozen runs against separately maintained annotations.

This is evaluation code, never prompt input. Reports engineering replay metrics;
real scientific quality claims require independently reviewed live samples.
"""
from __future__ import annotations
import math


def score_frozen_run(prediction, annotation):
    """No hit count, first-hit format, rank or graph-centrality proxy is used."""
    if not annotation.get("annotation_id") or not annotation.get("annotator"):
        raise ValueError("independent_annotation_provenance_required")
    if prediction.get("case_id") != annotation.get("case_id"):
        raise ValueError("evaluation_case_mismatch")
    truth = annotation["papers"]
    rows = prediction.get("papers") or {}
    if set(rows) - set(truth):
        raise ValueError("unannotated_prediction_ids")
    facets = annotation["facets"]
    coverage = {}
    selected = {pid for pid, row in rows.items() if row.get("label") in {"directly_relevant", "background_or_method"}}
    confirmed = {pid for pid, row in rows.items() if row.get("read_confirmed") is True and row.get("source_validated") is True}
    for facet in facets:
        expected = {pid for pid, row in truth.items() if facet in row.get("facets", [])}
        actual = {pid for pid in confirmed if facet in rows[pid].get("fulfilled_facets", [])}
        coverage[facet] = {"expected": len(expected), "confirmed_correct": len(expected & actual),
                           "coverage": len(expected & actual) / len(expected) if expected else None,
                           "false_confirmations": len(actual - expected)}
    methods = {pid for pid, row in truth.items() if row.get("method_candidate")}
    counter = {pid for pid, row in truth.items() if row.get("counter_evidence")}
    relevant = {pid for pid, row in truth.items() if row.get("facets") or row.get("method_candidate") or row.get("counter_evidence")}
    killed = {pid for pid, row in rows.items() if row.get("label") == "clearly_irrelevant"}
    costs = []
    cumulative = 0.0
    for row in prediction.get("cost_receipts", []):
        cost = float(row["actual_cny"])
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("invalid_evaluation_cost")
        cumulative += cost
        costs.append({"round": row["round"], "cumulative_cny": cumulative,
                      "confirmed_facets": list(row.get("confirmed_facets") or [])})
    expected_requirements = set(annotation.get("requirements", []))
    understood = set(prediction.get("understood_requirements", []))
    return {"case_id": annotation["case_id"], "annotation_id": annotation["annotation_id"],
        "understanding_recall": len(understood & expected_requirements) / len(expected_requirements) if expected_requirements else None,
        "facet_evidence": coverage,
        "method_candidate_recall": len(selected & methods) / len(methods) if methods else None,
        "counter_evidence_recall": len(selected & counter) / len(counter) if counter else None,
        "false_exclusion_rate": len(killed & relevant) / len(relevant) if relevant else None,
        "cost_curve": costs, "scientific_quality_certified": False}


def compare_frozen_runs(baseline, repaired, annotation):
    return {"baseline": score_frozen_run(baseline, annotation),
            "repaired": score_frozen_run(repaired, annotation),
            "evaluation_kind": "frozen_offline_replay", "live_quality_improvement": "not_assessed"}


def evaluate_manifest(path):
    """Evaluate explicit hashed old/new artifacts against independent annotations.

    A benchmark manifest is maintained outside production prompts. It may name
    authentic frozen runs or clearly labeled synthetic software fixtures; the
    label survives the result and never becomes a scientific quality claim.
    """
    import hashlib
    import json
    from pathlib import Path
    path = Path(path).resolve()
    manifest = json.loads(path.read_text())
    if manifest.get("schema_version") != "review.coverage_benchmark.v1":
        raise ValueError("coverage_benchmark_version_invalid")
    cases = manifest.get("cases") or []
    if not cases or len({row["case_id"] for row in cases}) != len(cases):
        raise ValueError("coverage_benchmark_cases_missing_or_duplicate")
    results = []
    for case in cases:
        if not case.get("domain") or case.get("evidence_kind") not in {"synthetic_software_fixture", "authentic_frozen_run"}:
            raise ValueError("coverage_benchmark_provenance_required")
        values = {}
        for name in ("baseline", "repaired", "annotation"):
            ref = case[name]
            file = Path(ref["path"])
            if not file.is_absolute(): file = path.parent / file
            data = file.read_bytes()
            if hashlib.sha256(data).hexdigest() != ref["sha256"]:
                raise ValueError("coverage_benchmark_hash_mismatch:" + name)
            values[name] = json.loads(data)
            if values[name].get("case_id") != case["case_id"]:
                raise ValueError("coverage_benchmark_case_mismatch")
        result = compare_frozen_runs(values["baseline"], values["repaired"], values["annotation"])
        results.append({**result, "domain": case["domain"], "evidence_kind": case["evidence_kind"],
                        "artifact_receipts": {k: case[k] for k in ("baseline", "repaired", "annotation")}})
    return {"schema_version": "review.coverage_benchmark_report.v1", "cases": results,
            "domains": sorted({case["domain"] for case in cases}),
            "scientific_quality_certified": False, "live_quality_improvement": "not_assessed"}
