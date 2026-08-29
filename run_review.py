#!/usr/bin/env python3
"""run_review — thin entry-point helpers used by the review harness CLI and tests.

This module exposes the validation-gate, supervisor-scoring, blueprint-state-sync,
and feedback-revision helpers that are imported directly by tests and by the
review harness orchestrator.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Blueprint validation gate
# ---------------------------------------------------------------------------

def _check_blueprint_validation(blueprint: dict, allow_unvalidated: bool = False) -> bool:
    """Return True if the blueprint passes validation (or validation is absent).

    Prints a BLOCKED message and returns False when validation.passed is False
    and allow_unvalidated is False.
    """
    validation = blueprint.get("validation")
    if not validation:
        return True  # no validation field → don't block
    if allow_unvalidated:
        return True
    if not validation.get("passed", True):
        issues = validation.get("issues") or []
        issues_str = "; ".join(str(i) for i in issues[:5])
        print(f"BLOCKED: blueprint validation failed — {issues_str or '(no issue details)'}")
        return False
    return True


# ---------------------------------------------------------------------------
# Supervisor quality score
# ---------------------------------------------------------------------------

_SEVERITY_SCORE: dict[str, int] = {
    "critical": 1000,
    "high": 100,
    "medium": 10,
    "low": 1,
}


def _supervisor_quality_score(
    supervisor: Any,
    section_ids: set[str],
    claim_ids: set[str],
    include_global: bool = True,
) -> tuple[int, dict[str, int]]:
    """Compute a weighted quality score from supervisor suggestions.

    Suggestions targeting blueprint-level issues are only counted when
    include_global=True.  Suggestions for sections or claims outside the
    provided sets are ignored.

    Returns (score, counts) where counts maps severity → count.
    """
    counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}

    for suggestion in getattr(supervisor, "suggestions", []):
        row: dict = (
            suggestion.to_dict()
            if hasattr(suggestion, "to_dict")
            else dict(suggestion)
        )
        target = row.get("target", "")
        target_id = str(row.get("target_id", ""))
        severity = row.get("severity", "low")
        if severity not in counts:
            counts[severity] = 0

        if target == "blueprint":
            if not include_global:
                continue
            counts[severity] += 1
        elif target == "section_draft":
            if target_id in section_ids:
                counts[severity] += 1
        elif target in ("claims", "claim", "section_claim"):
            if target_id in claim_ids:
                counts[severity] += 1

    score = sum(_SEVERITY_SCORE.get(sev, 0) * cnt for sev, cnt in counts.items())
    return score, counts


# ---------------------------------------------------------------------------
# Blueprint state sync after feedback
# ---------------------------------------------------------------------------

_NON_FACTUAL_STATES = frozenset(
    {"open_question", "boundary", "off_scope", "retracted", "rejected"}
)


def _apply_safe_claim_state_updates(
    blueprint: dict,
    updates: list[dict],
    revision_name: str = "feedback_revision",
) -> list[dict]:
    """Apply claim-state updates to blueprint.sections in-place.

    Returns the list of successfully applied update records.
    """
    # Build claim lookup: claim_id → (section_index, claim_index)
    claim_map: dict[str, tuple[int, int]] = {}
    for si, section in enumerate(blueprint.get("sections") or []):
        for ci, claim in enumerate(section.get("claims") or []):
            cid = claim.get("claim_id")
            if cid:
                claim_map[cid] = (si, ci)

    applied: list[dict] = []
    for update in updates:
        cid = update.get("claim_id")
        if not cid or cid not in claim_map:
            continue
        si, ci = claim_map[cid]
        claim = blueprint["sections"][si]["claims"][ci]
        old_state = claim.get("claim_state", "factual")
        # Apply update fields (except claim_id, which is the key)
        for k, v in update.items():
            if k != "claim_id":
                claim[k] = v
        if revision_name:
            claim.setdefault("revision_history", []).append(
                {
                    "revision_name": revision_name,
                    "previous_state": old_state,
                    "new_state": claim.get("claim_state", old_state),
                }
            )
        applied.append({"claim_id": cid, **update})
    return applied


def _synchronize_blueprint_after_feedback(blueprint: dict) -> None:
    """Remove non-factual DAG edges and update topological order in-place.

    Populates blueprint["feedback_state_sync_status"] with a summary.
    """
    sections = blueprint.get("sections") or []
    claim_states: dict[str, str] = {}
    for section in sections:
        for claim in section.get("claims") or []:
            cid = claim.get("claim_id")
            if cid:
                claim_states[cid] = claim.get("claim_state", "factual")

    dag = blueprint.get("argument_dag")
    if not dag:
        blueprint["feedback_state_sync_status"] = {
            "removed_nonfactual_dag_edges": 0,
            "synchronized": True,
        }
        return

    edges = dag.get("edges") or []
    kept: list[dict] = []
    removed = 0
    for edge in edges:
        src_state = claim_states.get(edge.get("source_claim_id", ""), "factual")
        tgt_state = claim_states.get(edge.get("target_claim_id", ""), "factual")
        if src_state in _NON_FACTUAL_STATES or tgt_state in _NON_FACTUAL_STATES:
            removed += 1
        else:
            kept.append(edge)
    dag["edges"] = kept

    topo = dag.get("topological_order") or []
    dag["topological_order"] = [
        cid for cid in topo
        if claim_states.get(cid, "factual") not in _NON_FACTUAL_STATES
    ]

    blueprint["feedback_state_sync_status"] = {
        "removed_nonfactual_dag_edges": removed,
        "synchronized": True,
    }


# ---------------------------------------------------------------------------
# Feedback revision runner
# ---------------------------------------------------------------------------

def _run_feedback_revision(
    blueprint: dict,
    drafts: list,
    suggestions: list[dict],
    kb_path: Optional[Path],
    output_dir: Path,
    revision_name: str = "feedback_revision",
    minimum_severity: str = "medium",
) -> list:
    """Route feedback suggestions to the owning sections and revise drafts.

    Returns the list of revised drafts.
    """
    import optomind_research.review_writer as rw

    sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    min_rank = sev_rank.get(minimum_severity, 1)

    filtered = [
        s for s in suggestions
        if sev_rank.get(s.get("severity", "low"), 1) >= min_rank
    ]

    # Build claim-id → section-id map
    claim_to_section: dict[str, str] = {}
    section_ids: set[str] = set()
    for section in blueprint.get("sections") or []:
        sid = section.get("section_id", "")
        section_ids.add(sid)
        for claim in section.get("claims") or []:
            cid = claim.get("claim_id")
            if cid:
                claim_to_section[cid] = sid

    draft_map = {d.section_id: d for d in drafts}

    # Route suggestions to sections
    section_suggestions: dict[str, list[dict]] = {}
    for s in filtered:
        target_id = str(s.get("target_id", ""))
        sid = claim_to_section.get(target_id)
        if sid is None and target_id in section_ids:
            sid = target_id
        if sid:
            section_suggestions.setdefault(sid, []).append(s)

    agent = rw.EvidenceAwareRevisionAgent(real_llm=False)
    revised: list = []
    for sid, sug_list in section_suggestions.items():
        draft = draft_map.get(sid)
        if draft is None:
            continue
        packet = rw.SectionMaterialPacket(section_id=sid, claims=[], evidence_packets=[])
        result = agent.revise(draft, packet, sug_list)
        revised.append(result)
    return revised
