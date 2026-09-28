"""The three-section V4 slice: identity freeze, preflight and scorecard (SM10).

The slice is the core-mainline freeze gate that runs before any full E2E.  It is
not a second orchestrator: it freezes one identity, refuses to start unless the
offline counterexamples hold, drives the formal Phase-3 stage entry over exactly
three sections, then scores what the real run produced against the frozen
baseline.

The three things it makes impossible:

* starting a paid slice without the P0 counterexamples having passed;
* silently widening the run to nine sections, or into publication/PDF;
* reporting a section as covered when its own material blocked it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

V4_IDENTITY_SCHEMA = "optomind.upgrade3.v4_run_identity.v1"
V4_PREFLIGHT_SCHEMA = "optomind.upgrade3.v4_slice_preflight.v1"
V4_SCORECARD_SCHEMA = "optomind.upgrade3.v4_slice_scorecard.v1"

#: the three sections of the core-mainline slice, by argument role
SLICE_ROLES = ("foundation", "training", "bottleneck")

#: stages a slice must never enter
FORBIDDEN_STAGES = ("publication", "latex", "pdf", "chinese_translation",
                    "visual_editor", "full_e2e")

CLAIM_TARGET_MIN = 16
CLAIM_TARGET_MAX = 32


class V4SliceError(RuntimeError):
    """The slice is not allowed to start, or its result is not admissible."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def freeze_identity(
    *,
    run_id: str,
    generation_id: str,
    attempt_id: str,
    topic: str,
    sections: Sequence[Mapping[str, Any]],
    code_paths: Iterable[str | Path],
    prompt_paths: Iterable[str | Path] = (),
    model: str,
    policy_sha256: str,
    material_paths: Iterable[str | Path] = (),
    budget_cap_cny: float,
    predecessor_run: str = "",
) -> dict[str, Any]:
    """One identity for one generation, before a single paid call.

    The slice may not reuse a predecessor's identity: a new generation is a new
    run, and the frozen file hashes are what a later reader compares against.
    """

    section_rows: list[dict[str, Any]] = []
    for section in sections:
        section_id = _text(section.get("section_id"))
        if not section_id:
            raise V4SliceError("slice_section_id_missing")
        section_rows.append({
            "section_id": section_id,
            "title": _text(section.get("title")),
            # slice_role is the ticket's name for the leg (foundation / training /
            # bottleneck); argument_role is what the frozen blueprint itself
            # declares.  Both are kept so the mapping is auditable and nobody has
            # to rewrite the blueprint to match a work order's vocabulary.
            "slice_role": _text(section.get("slice_role")
                                or section.get("argument_role")),
            "argument_role": _text(section.get("argument_role")),
            "required_roles": [_text(item) for item in
                               (section.get("required_roles") or [])],
        })
    if not section_rows:
        raise V4SliceError("slice_has_no_sections")
    if len(section_rows) > 3:
        raise V4SliceError("slice_must_not_widen:%d" % len(section_rows))
    roles = {row["slice_role"] for row in section_rows}
    missing = [role for role in SLICE_ROLES if role not in roles]
    if missing:
        raise V4SliceError("slice_roles_missing:%s" % ",".join(missing))
    lowered = {_text(path).casefold() for path in code_paths}
    forbidden = sorted(
        item for item in lowered
        for stage in FORBIDDEN_STAGES
        if stage in item.replace(chr(92), "/").split("/")[-1]
    )
    if forbidden:
        raise V4SliceError("slice_must_not_enter:%s" % ",".join(forbidden))
    if not _text(model) or not _text(policy_sha256):
        raise V4SliceError("slice_model_or_policy_identity_missing")
    if predecessor_run and _text(predecessor_run) == _text(run_id):
        raise V4SliceError("slice_reused_a_predecessor_identity")

    body = {
        "schema_version": V4_IDENTITY_SCHEMA,
        "run_id": _text(run_id),
        "generation_id": _text(generation_id),
        "attempt_id": _text(attempt_id),
        "topic": _text(topic),
        "is_the_hidden_topic": False,
        "sections": section_rows,
        "slice_roles": list(SLICE_ROLES),
        "model": _text(model),
        "policy_sha256": _text(policy_sha256),
        "budget_cap_cny": float(budget_cap_cny),
        "predecessor_run": _text(predecessor_run),
        "code": {},
        "prompts": {},
        "material": {},
    }
    for label, targets in (("code", code_paths), ("prompts", prompt_paths),
                           ("material", material_paths)):
        for target in targets:
            path = Path(target)
            if not path.is_file():
                raise V4SliceError("slice_%s_missing:%s" % (label, path))
            body[label][str(path).replace(chr(92), "/")] = _sha_file(path)
    body["identity_hash"] = _sha(body)
    return body


def preflight(
    *,
    identity: Mapping[str, Any],
    counterexamples: Sequence[Mapping[str, Any]],
    required: int = 0,
) -> dict[str, Any]:
    """Every P0 counterexample must hold BEFORE the first paid call.

    A counterexample row names what it blocks and reports whether the block
    still happens.  A row that reports blocked false is a real regression: the
    slice must not start.
    """

    rows = []
    failures = []
    for row in counterexamples:
        name = _text(row.get("name"))
        if not name:
            failures.append("counterexample_without_a_name")
            continue
        ok = row.get("blocked") is True
        rows.append({
            "name": name,
            "blocks": _text(row.get("blocks")),
            "blocked": ok,
            "evidence": _text(row.get("evidence")),
        })
        if not ok:
            failures.append("counterexample_regressed:%s" % name)
    if len(rows) < int(required or 0):
        failures.append("counterexample_count:%d<%d" % (len(rows), int(required)))
    return {
        "schema_version": V4_PREFLIGHT_SCHEMA,
        "identity_hash": _text(identity.get("identity_hash")),
        "started": not failures,
        "counterexamples": rows,
        "failures": failures,
        "rule": "no paid slice begins until every P0 counterexample still blocks",
    }


def _section_yield(section: Mapping[str, Any]) -> dict[str, Any]:
    claims = int(section.get("claims") or 0)
    status = _text(section.get("status"))
    if claims >= CLAIM_TARGET_MIN:
        verdict = "within_target"
    elif claims > 0:
        verdict = "below_target_with_material"
    elif status in ("needs_evidence", "blocked", "needs_more_literature",
                    "unavailable"):
        verdict = "correctly_blocked_by_material"
    else:
        verdict = "no_claims_and_no_named_material_reason"
    return {
        "section_id": _text(section.get("section_id")),
        "claims": claims,
        "status": status,
        "verdict": verdict,
        "blocking_reasons": list(section.get("blocking_reasons") or []),
    }


def scorecard(
    *,
    identity: Mapping[str, Any],
    stage_results: Mapping[str, Any],
    per_section: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, Any] | None = None,
    cost_cny: float = 0.0,
    out_of_domain_writable: int = 0,
    load_bearing_bound_percent: float = 0.0,
    recovery_recomputed_p3abc: int = 1,
    literature_driven_improvements: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """What the slice proved, in the ticket's own terms."""

    yields = [_section_yield(section) for section in per_section]
    unmet = [row["section_id"] for row in yields
             if row["verdict"] == "no_claims_and_no_named_material_reason"]
    violations = []
    if out_of_domain_writable:
        violations.append("out_of_domain_writable:%d" % out_of_domain_writable)
    if load_bearing_bound_percent < 100.0:
        violations.append("load_bearing_not_fully_bound:%.2f"
                          % load_bearing_bound_percent)
    if unmet:
        violations.append("section_yield_unexplained:%s" % ",".join(unmet))
    if recovery_recomputed_p3abc:
        violations.append("recovery_recomputed_p3abc:%d" % recovery_recomputed_p3abc)
    if not literature_driven_improvements:
        violations.append("no_literature_driven_improvement")
    for stage in FORBIDDEN_STAGES:
        if stage_results.get(stage):
            violations.append("forbidden_stage_ran:%s" % stage)
    if float(cost_cny) > float(identity.get("budget_cap_cny") or 0):
        violations.append("over_budget:%.6f" % float(cost_cny))
    card = {
        "schema_version": V4_SCORECARD_SCHEMA,
        "identity_hash": _text(identity.get("identity_hash")),
        "sections": yields,
        "claims_total": sum(row["claims"] for row in yields),
        "sections_within_target": [
            row["section_id"] for row in yields if row["verdict"] == "within_target"],
        "sections_correctly_blocked": [
            row["section_id"] for row in yields
            if row["verdict"] == "correctly_blocked_by_material"],
        "out_of_domain_writable": out_of_domain_writable,
        "load_bearing_bound_percent": load_bearing_bound_percent,
        "literature_driven_improvements": [dict(row) for row in
                                           literature_driven_improvements],
        "recovery_recomputed_p3abc": recovery_recomputed_p3abc,
        "cost_cny": float(cost_cny),
        "stage_results": dict(stage_results or {}),
        "baseline": dict(baseline or {}),
        "violations": violations,
        "passed": not violations,
    }
    if baseline:
        card["information_gain_vs_baseline"] = (
            int(card["claims_total"]) - int(baseline.get("claims_total") or 0))
    card["scorecard_hash"] = _sha({k: v for k, v in card.items()
                                   if k != "scorecard_hash"})
    return card


__all__ = [
    "CLAIM_TARGET_MAX",
    "CLAIM_TARGET_MIN",
    "FORBIDDEN_STAGES",
    "SLICE_ROLES",
    "V4_IDENTITY_SCHEMA",
    "V4_PREFLIGHT_SCHEMA",
    "V4_SCORECARD_SCHEMA",
    "V4SliceError",
    "freeze_identity",
    "preflight",
    "scorecard",
]
