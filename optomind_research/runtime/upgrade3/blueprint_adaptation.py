"""Blueprint-literature bidirectional adaptation (user directive 2026-09-09).

The initial writing plan and the discovered literature each hold HALF the
authority over the review outline:

- plan half (irreducible): the original question's required slots and
  question coverage stay absolute — adaptation may never delete or demote a
  question-required role just because literature is missing;
- literature half (this module): role emphasis, section feasibility and the
  article breadth targets must adapt to what the literature actually
  supplies, so abundant material that does not match a pre-assigned role
  reshapes the plan instead of being discarded.

Every adaptation carries provenance (ledger evidence, counts, audit reasons)
and is written to BLUEPRINT_ADAPTATION.json.  Nothing is deleted: the
original plan targets are preserved next to the adapted ones.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "optomind.upgrade3.blueprint_adaptation.v1"

# never adapt below these floors, even when literature is thinner
BREADTH_FLOOR_UNIQUE = 3
BREADTH_FLOOR_DIRECT = 2


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def collect_literature_signal(
    *,
    section_dirs: Dict[str, Path],
    question_required_roles: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Aggregate what the literature actually supplies, per section/role.

    Reads each section's LOCAL_CANDIDATE_LEDGER (audited candidates) and
    COVERAGE_DECISION (shortfalls).  Pure read-only; the signal hash covers
    the observed supply so consumers can detect drift.
    """
    question_required_roles = {
        str(r) for r in (question_required_roles or [])}
    per_section: Dict[str, Dict[str, Any]] = {}
    approved_papers_by_lane: Dict[str, set] = {
        "direct": set(), "adjacent": set(), "contextual": set()}
    rejected_theme_tokens: Dict[str, int] = {}
    for section_id, work_dir in sorted(section_dirs.items()):
        ledger_path = Path(work_dir) / "LOCAL_CANDIDATE_LEDGER.json"
        decision_path = Path(work_dir) / "COVERAGE_DECISION.json"
        rows: List[Dict[str, Any]] = []
        if ledger_path.is_file():
            try:
                data = json.loads(ledger_path.read_text(encoding="utf-8"))
                rows = [r for r in data.get("candidates", [])
                        if isinstance(r, dict)]
            except Exception:
                rows = []
        approved_by_lane: Dict[str, int] = {
            "direct": 0, "adjacent": 0, "contextual": 0}
        approved_roles: set = set()
        rejected = 0
        reasons: List[str] = []
        for row in rows:
            lane = str(row.get("scope_fit") or "").casefold()
            decision = str(row.get("decision") or "").casefold()
            if decision == "approved":
                if row.get("role"):
                    approved_roles.add(str(row["role"]))
                if lane in approved_by_lane:
                    approved_by_lane[lane] += 1
                    approved_papers_by_lane[lane].add(
                        str(row.get("paper_id") or ""))
            elif decision == "rejected":
                rejected += 1
                reason = str(row.get("audit_reason") or "")
                if reason:
                    reasons.append(reason)
        # theme tokens from rejection reasons: what the literature is ABOUT
        for reason in reasons:
            for token in re.findall(r"[a-z]{4,}", reason.casefold()):
                if token in {"that", "this", "with", "from", "than", "rather",
                             "which", "their", "been", "more", "paper",
                             "focuses", "targets", "provides", "presents"}:
                    continue
                rejected_theme_tokens[token] = (
                    rejected_theme_tokens.get(token, 0) + 1)
        outcome = ""
        limitations: List[str] = []
        if decision_path.is_file():
            try:
                decision = json.loads(
                    decision_path.read_text(encoding="utf-8"))
                outcome = str(decision.get("coverage_outcome") or "")
                adaptive = decision.get("adaptive_readiness") or {}
                limitations = [str(x) for x in (adaptive.get("limitations")
                                                or [])]
            except Exception:
                pass
        per_section[section_id] = {
            "outcome": outcome,
            "approved_by_lane": approved_by_lane,
            "approved_roles": sorted(approved_roles),
            "rejected": rejected,
            "top_rejection_reasons": reasons[:5],
            "limitations": limitations,
        }
    signal = {
        "schema_version": "optomind.upgrade3.literature_signal.v1",
        "per_section": per_section,
        "article_approved_papers_by_lane": {
            lane: sorted(pids - {""})
            for lane, pids in approved_papers_by_lane.items()},
        "rejection_theme_tokens": dict(sorted(
            rejected_theme_tokens.items(),
            key=lambda kv: -kv[1])[:40]),
        "question_required_roles": sorted(question_required_roles),
    }
    signal["signal_hash"] = _sha(_canonical(
        {k: v for k, v in signal.items() if k != "signal_hash"}))
    return signal


def _parse_shortfall(limitation: str) -> Optional[tuple[str, int, int]]:
    """Parse any ``<kind>_shortfall:<observed>/<planned>`` limitation."""
    m = re.match(r"(\w*_?)shortfall:(\d+)/(\d+)$", str(limitation).strip())
    if not m:
        return None
    kind, observed, planned = (m.group(1).strip("_") or "breadth",
                               int(m.group(2)), int(m.group(3)))
    return kind, observed, planned


def adapt_targets(
    *,
    signal: Dict[str, Any],
    planned_unique: int,
    planned_direct: int,
    searched: bool,
    local_pool_exhausted: bool,
) -> Dict[str, Any]:
    """The literature's half: retarget article breadth to the observed,
    bounded supply.  Targets only ever move when the run actually searched
    (searched=True) and the local pool was exhausted — a target is never
    lowered merely because the run stopped looking early.  Floors keep the
    plan's minimum meaningful."""
    supply = signal.get("article_approved_papers_by_lane") or {}
    feasible_unique = len(set(
        supply.get("direct", []) + supply.get("adjacent", [])))
    feasible_direct = len(supply.get("direct", []))
    adaptations: List[Dict[str, Any]] = []
    targets_may_move = bool(searched and local_pool_exhausted)
    adapted_unique = planned_unique
    adapted_direct = planned_direct
    if targets_may_move and planned_unique > feasible_unique:
        adapted_unique = max(BREADTH_FLOOR_UNIQUE, feasible_unique)
        adaptations.append({
            "kind": "breadth_target_unique",
            "planned": planned_unique, "adapted": adapted_unique,
            "observed_supply": feasible_unique,
            "evidence": "article_approved_papers_by_lane",
        })
    if targets_may_move and planned_direct > max(feasible_direct,
                                                 BREADTH_FLOOR_DIRECT):
        adapted_direct = max(BREADTH_FLOOR_DIRECT, feasible_direct)
        adaptations.append({
            "kind": "breadth_target_direct",
            "planned": planned_direct, "adapted": adapted_direct,
            "observed_supply": feasible_direct,
            "evidence": "article_approved_papers_by_lane",
        })
    return {"adapted_unique": adapted_unique,
            "adapted_direct": adapted_direct,
            "feasible_unique": feasible_unique,
            "feasible_direct": feasible_direct,
            "targets_may_move": targets_may_move,
            "adaptations": adaptations}


def demote_unsupplied_roles(
    *,
    signal: Dict[str, Any],
    section_required_roles: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """Roles the plan demanded but the literature does not supply.

    A section-required role is demoted to ``optional_with_documented_gap``
    only when no approved material anywhere in the article carries that role
    and it is not a question-required role.  This is the literature's half
    reshaping section planning; the review question's own slots are never
    demoted here.
    """
    all_approved_roles: set = set()
    for section_sig in (signal.get("per_section") or {}).values():
        all_approved_roles.update(section_sig.get("approved_roles") or [])
    question_roles = set(signal.get("question_required_roles") or [])
    demoted: Dict[str, List[str]] = {}
    for section_id, roles in (section_required_roles or {}).items():
        out: List[str] = []
        for role in roles or []:
            if not role or role in question_roles:
                continue
            if role not in all_approved_roles:
                out.append(role)
        if out:
            demoted[section_id] = sorted(set(out))
    return demoted


def reevaluate_section_outcome(
    *,
    outcome: str,
    limitations: List[str],
    adapted: Dict[str, Any],
    signal_section: Dict[str, Any],
    question_required_roles: List[str],
    demoted_roles: List[str],
) -> Dict[str, Any]:
    """Re-evaluate ONE section outcome under adapted targets.

    needs_more_literature caused ONLY by article-breadth shortfalls, or by
    uncovered roles that the adaptation legitimately demoted (zero literature
    supply article-wide, not question-required), becomes
    material_ready_with_limits.  A section with an uncovered
    question-required role, blocking gaps, transport failure, or zero direct
    material can never be flipped."""
    approved = signal_section.get("approved_by_lane") or {}
    if outcome != "needs_more_literature":
        return {"flipped": False, "reason": f"outcome_{outcome}_unchanged"}
    if not approved.get("direct") and not approved.get("adjacent"):
        return {"flipped": False,
                "reason": "no_direct_or_adjacent_material_cannot_adapt"}
    demoted = set(demoted_roles or [])
    question_roles = set(question_required_roles or [])
    uncovered_load_bearing: List[str] = []
    for limitation in limitations:
        parsed = _parse_shortfall(limitation)
        if parsed:
            _kind, observed, planned = parsed
            if observed >= planned:
                continue  # reported as a limitation label but actually met
            continue  # a supply-bound shortfall is exactly what adaptation addresses
        if limitation.startswith("load_bearing_roles_uncovered:"):
            uncovered_load_bearing.extend(
                r.strip() for r in
                limitation.split(":", 1)[1].split(",") if r.strip())
            continue
        if limitation.startswith("optional_roles_uncovered:"):
            continue
        # unknown limitation kinds never authorize a flip
        return {"flipped": False,
                "reason": f"unhandled_limitation:{limitation}"}
    blocking = [r for r in uncovered_load_bearing
                if r in question_roles or r not in demoted]
    if blocking:
        return {"flipped": False,
                "reason": "required_role_uncovered:" + ",".join(blocking)}
    if not adapted.get("targets_may_move"):
        return {"flipped": False,
                "reason": "search_capacity_remains_target_stands"}
    has_breadth_shortfall = any(_parse_shortfall(l) for l in limitations)
    if not has_breadth_shortfall and not uncovered_load_bearing:
        return {"flipped": False, "reason": "no_adaptable_shortfall_present"}
    return {"flipped": True,
            "new_outcome": "material_ready_with_limits",
            "note": "blueprint adapted to literature supply; question "
                    "slots unchanged"}


def build_adaptation(
    *,
    signal: Dict[str, Any],
    planned_unique: int,
    planned_direct: int,
    searched: bool,
    local_pool_exhausted: bool,
    generation_id: str,
    section_required_roles: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    adapted = adapt_targets(signal=signal, planned_unique=planned_unique,
                            planned_direct=planned_direct,
                            searched=searched,
                            local_pool_exhausted=local_pool_exhausted)
    demoted = demote_unsupplied_roles(
        signal=signal, section_required_roles=section_required_roles or {})
    section_results: Dict[str, Dict[str, Any]] = {}
    for section_id, sig in (signal.get("per_section") or {}).items():
        section_results[section_id] = reevaluate_section_outcome(
            outcome=str(sig.get("outcome") or ""),
            limitations=[str(x) for x in (sig.get("limitations") or [])],
            adapted=adapted,
            signal_section=sig,
            question_required_roles=list(
                signal.get("question_required_roles") or []),
            demoted_roles=list(demoted.get(section_id) or []),
        )
    adaptation = {
        "schema_version": SCHEMA,
        "generation_id": generation_id,
        "authority_split": {"plan": 0.5, "literature": 0.5},
        "question_slots_absolute": True,
        "targets": adapted,
        "demoted_roles": demoted,
        "section_reevaluation": section_results,
        "signal_hash": signal.get("signal_hash"),
    }
    adaptation["adaptation_hash"] = _sha(_canonical(
        {k: v for k, v in adaptation.items() if k != "adaptation_hash"}))
    return adaptation


def adapt_or_none(
    *,
    coverage_root: Path,
    planned_unique: int,
    planned_direct: int,
    searched: bool,
    local_pool_exhausted: bool,
    generation_id: str,
    question_required_roles: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Convenience production wrapper: build signal from a coverage run tree
    and return the adaptation (None when nothing qualified)."""
    sections_root = Path(coverage_root) / "sections"
    if not sections_root.is_dir():
        return None
    section_dirs = {p.name: p for p in sections_root.iterdir()
                    if p.is_dir()}
    if not section_dirs:
        return None
    signal = collect_literature_signal(
        section_dirs=section_dirs,
        question_required_roles=question_required_roles)
    adaptation = build_adaptation(
        signal=signal, planned_unique=planned_unique,
        planned_direct=planned_direct, searched=searched,
        local_pool_exhausted=local_pool_exhausted,
        generation_id=generation_id)
    if not adaptation["targets"]["adaptations"] and not any(
        r.get("flipped") for r in
        adaptation["section_reevaluation"].values()):
        return None
    return adaptation
