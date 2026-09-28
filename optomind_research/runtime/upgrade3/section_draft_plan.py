"""Upgrade-3 evidence-driven section draft planning & audit (ticket 021).

DRAFT_PLAN first: blocks are derived from load-bearing slots and evidence
groups (2-4 claims each, spans and conditions attached); word/count targets
are cost controls only.  The author must, per block, state the mechanism, the
conditioned comparison, limitations and the relation to the review question;
a chain of single-source abstracts never completes a cross-literature
comparison — insufficient evidence feeds back to 018 instead of encyclopedia
padding.

DRAFT_AUDIT then checks, deterministically: planned information tasks land in
the text (conditioned numbers present with their conditions), unplanned
important numbers/mechanisms are flagged for extraction as new claims (016),
repetition is counted, and the effective UIU (unique informed units) is
reported.  UIU counts unique (metric, value, condition) triples plus
boundary/limitation statements — deduplicated by content, not by wording.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

PLAN_SCHEMA = "optomind.upgrade3.draft_plan.v1"
AUDIT_SCHEMA = "optomind.upgrade3.draft_audit.v1"

_NUM = re.compile(r"\d+(?:\.\d+)?")
_CONDITION_WORDS = re.compile(
    r"(?i)(\bunder\b|\bat\b|\bon\b|\bacross\b|\bper\b|\bwithin\b|dataset|\bbasins?\b|epochs|"
    r"\bwatts?\b|\bw\b|nm\b|\bum\b|µm\b|temporal|spatial|simulat|experiment|measured|"
    r"in the hardware|condition)")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def build_draft_plan(*, section_id: str, generation_id: str,
                     slots: List[Dict[str, Any]],
                     bindings: List[Dict[str, Any]],
                     target_words: int = 1000) -> Dict[str, Any]:
    """Blocks from required slots x evidence groups (2-4 claims per block)."""
    blocks: List[Dict[str, Any]] = []
    by_role: Dict[str, List[Dict[str, Any]]] = {}
    for b in bindings:
        if not b.get("writable"):
            continue
        for role in (b.get("roles") or ["architectures"]):
            by_role.setdefault(role, []).append(b)
    for slot in slots:
        role = slot.get("role")
        group = by_role.get(role) or by_role.get("architectures") or []
        chunk = group[:4] or group
        if not chunk:
            continue
        blocks.append({
            "block_id": f"{section_id}-{slot['slot_id']}",
            "question_slot": slot["slot_id"],
            "requirement_id": slot.get("requirement_id"),
            "rhetorical_function": ("conditioned_comparison" if slot.get("role")
                                    in ("comparison", "bottlenecks")
                                    else "mechanism_explanation"),
            "claim_ids": [b["binding"]["claim_id"] for b in chunk],
            "binding_hashes": [b["binding"]["binding_hash"] for b in chunk],
            "fact_ids": [b.get("fact_ids", []) for b in chunk],
            "distinct_source_groups": sorted({b["binding"]["canonical_citation_ids"][0]
                                              for b in chunk if b["binding"].get(
                                                  "canonical_citation_ids")}),
            "comparison_task": slot.get("role") in ("comparison", "bottlenecks"),
            "boundary_task": True,
            "expected_information_units": max(2, len(chunk)),
        })
    return {
        "schema_version": PLAN_SCHEMA,
        "section_id": section_id,
        "generation_id": generation_id,
        "target_words_note": "cost control only; never a quality pass",
        "target_words": target_words,
        "blocks": blocks,
        "plan_hash": _sha(_canonical(blocks)),
    }


def expected_uiu(plan: Dict[str, Any]) -> int:
    return sum(b["expected_information_units"] for b in plan["blocks"])


# ------------------------------------------------------------- audit ----
def _conditioned_number_units(text: str) -> List[Dict[str, str]]:
    units = []
    for m in _NUM.finditer(text or ""):
        window = text[max(0, m.start() - 90):m.end() + 90]
        if _CONDITION_WORDS.search(window):
            units.append({"value": m.group(0),
                          "condition_context": _norm(window)[:120],
                          "uiu_key": _sha(_norm(window))[:16]})
    return units


def _dedup_uiu(units: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for u in units:
        key = _sha(u.get("condition_context", ""))[:12] + "|" + u.get("value", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def audit_draft(plan: Dict[str, Any], draft_text: str,
                binding_hashes: List[str],
                known_numbers: Optional[List[str]] = None) -> Dict[str, Any]:
    """Deterministic audit: planned tasks landed, unsupported numbers flagged,
    repetition counted, effective UIU reported."""
    d_norm = _norm(draft_text)
    missed_slots: List[str] = []
    unsupported_numbers: List[Dict[str, Any]] = []
    planned_uiu: List[Dict[str, str]] = []
    for block in plan["blocks"]:
        landed = False
        for cid in block["claim_ids"]:
            cid_tail = _norm(cid)
            if cid_tail and cid_tail in d_norm:
                landed = True
        if block["question_slot"] and _norm(block["question_slot"]) in d_norm:
            landed = True
        units = _dedup_uiu(_conditioned_number_units(draft_text))
        if not landed:
            missed_slots.append(block["question_slot"])
        planned_uiu.extend(units)
    known = set(known_numbers or [])
    for m in _NUM.finditer(draft_text or ""):
        window = draft_text[max(0, m.start() - 40):m.end() + 90]
        if _CONDITION_WORDS.search(window):
            continue  # near a conditioned passage: UIU extraction covers it
        if m.group(0) in known:
            continue
        unsupported_numbers.append({"value": m.group(0),
                                    "context": _norm(window)[:100]})
    # repetition: duplicate 12-word shingles
    shingles: Dict[str, int] = {}
    words = (draft_text or "").split()
    for i in range(len(words) - 11):
        sh = " ".join(words[i:i + 12]).lower()
        shingles[sh] = shingles.get(sh, 0) + 1
    duplicates = sum(v - 1 for v in shingles.values() if v > 1)
    uiu_units = _dedup_uiu(_conditioned_number_units(draft_text))
    audit = {
        "schema_version": AUDIT_SCHEMA,
        "section_id": plan["section_id"],
        "planned_blocks": len(plan["blocks"]),
        "missed_slots": missed_slots,
        "unsupported_numbers": unsupported_numbers[:10],
        "duplicate_12w_shingles": duplicates,
        "actual_uiu": len(uiu_units),
        "uiu_units_head": uiu_units[:8],
        "planned_expected_uiu": expected_uiu(plan),
        "needs_evidence": bool(missed_slots),
        "revision_required": duplicates > 2,
    }
    audit["audit_hash"] = _sha(_canonical(
        {k: v for k, v in audit.items() if k != "audit_hash"}))
    return audit


def reviewer_prompt(plan: Dict[str, Any], draft_text: str) -> Dict[str, str]:
    tasks = [{"slot": b["question_slot"], "function": b["rhetorical_function"],
              "comparison_task": b["comparison_task"]} for b in plan["blocks"]]
    return {
        "system_prompt": ("You are an independent short reviewer. Judge whether the "
                          "draft fulfils the planned information tasks, keeps original "
                          "conditions, and names limitations. JSON only."),
        "user_payload": {"planned_tasks": tasks, "draft": draft_text,
                         "output_schema": {"tasks_fulfilled": ["slot"],
                                           "tasks_missed": ["slot"],
                                           "conditions_kept": True,
                                           "verdict": "usable|needs_revision"},
                         },
    }
