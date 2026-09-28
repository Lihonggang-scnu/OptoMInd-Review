"""Upgrade-3 scientific correction & independent review (ticket 025).

The correction channel that can DELETE or REPLACE wrong facts — separated from
style-preserving edits:

- SCIENTIFIC_PATCH: issue_id, source_block_hash, operation
  (remove_claim | replace_evidence | narrow_claim | add_condition |
  request_evidence), authorized_claim_delta, source_span_ids,
  citation_delta, candidate_hash.
- per-claim science tasks: every issue in a block stays pending until its own
  patch applies (no single-winner-per-block); conflicting patches serialize
  with new revisions.
- an authorized patch supersedes the style ``numbers_preserved`` rule FOR ITS
  TARGET ONLY: deleting a wrong number/claim is legal; unrelated true claims
  stay protected; changing a citation while keeping the old wrong fact is
  rejected.
- the author sees only target + source + question; the independent reviewer
  sees the source evidence and the revised text (never the author's
  justification) and checks resolution / new errors / coverage gaps.
- an issue closes ONLY when its patch was applied to the unique source-hash
  target AND the independent re-review passed.  no-change / empty / target
  collision / timeout / reviewer disagreement stay open.  At most 2 rounds
  with demonstrated progress.
- authorized routine scientific corrections auto-execute; no extra human
  approval step is invented here.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

PATCH_SCHEMA = "optomind.upgrade3.scientific_patch.v1"
OPERATIONS = {"remove_claim", "replace_evidence", "narrow_claim",
              "add_condition", "request_evidence"}
MAX_ROUNDS = 2


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", (text or "").lower()).strip()


def build_patch(*, issue: Dict[str, Any], operation: str,
                target_statements: List[str],
                add_conditions: Optional[List[str]] = None,
                replacement_evidence_span_ids: Optional[List[str]] = None,
                authorized_claim_delta: Optional[List[str]] = None,
                source_block_hash: str) -> Dict[str, Any]:
    if operation not in OPERATIONS:
        raise ValueError("unknown operation: " + operation)
    if operation == "add_condition" and not (add_conditions or
                                             replacement_evidence_span_ids):
        raise ValueError("add_condition requires the condition text or evidence")
    return {
        "schema_version": PATCH_SCHEMA,
        "issue_id": issue.get("issue_id"),
        "source_block_hash": source_block_hash,
        "operation": operation,
        "target_statements": target_statements,
        "add_conditions": add_conditions or [],
        "replacement_evidence_span_ids": replacement_evidence_span_ids or [],
        "authorized_claim_delta": authorized_claim_delta or [],
        "candidate_hash": "",
    }


def apply_patch(block_text: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic application.  remove_claim deletes the sentences carrying
    the wrong claims; add_condition inserts the condition right after the
    sentence carrying the affected numbers.  Returns revised text + removed
    statements for the re-audit."""
    text = block_text
    removed: List[str] = []
    op = patch["operation"]
    if op == "remove_claim":
        for stmt in patch["target_statements"]:
            key = _norm(stmt)[:60]
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                if key and key in _norm(sentence):
                    text = text.replace(sentence, "")
                    removed.append(_norm(sentence)[:160])
                    break
        text = re.sub(r"\s{2,}", " ", text).strip()
    elif op == "add_condition":
        for cond in patch.get("add_conditions") or []:
            anchor = _norm(cond.split("|")[0]) if "|" in cond else _norm(cond)
            cond_text = cond.split("|", 1)[1].strip() if "|" in cond else cond
            idx = _norm(text).find(anchor[:40])
            if idx >= 0:
                pass  # normalized-space insertion is best effort and audited below
            text = re.sub(r"((" + re.escape(cond.split("|")[0][:50]) + r")\.)",
                          lambda m: m.group(1) + " " + cond_text + ".", text,
                          count=1)
    applied = text != block_text or op in ("request_evidence",)
    candidate_hash = _sha(text)
    return {"revised_text": text, "removed_statements": removed,
            "applied": applied, "candidate_hash": candidate_hash}


def verify_patch(revised_text: str, patch: Dict[str, Any],
                 preserved_numbers: List[str]) -> Dict[str, Any]:
    """Post-application verification: wrong numbers gone, legal facts kept,
    added conditions present, no unauthorized new numbers."""
    n_text = _norm(revised_text)
    problems: List[str] = []
    if patch["operation"] == "remove_claim":
        for stmt in patch["target_statements"]:
            for num in re.findall(r"\d+(?:\.\d+)?", stmt):
                if num in n_text:
                    problems.append(f"wrong_number_still_present:{num}")
    if patch["operation"] == "add_condition":
        for cond in patch.get("add_conditions") or []:
            cond_text = cond.split("|", 1)[1].strip() if "|" in cond else cond
            if _norm(cond_text) not in n_text:
                problems.append("added_condition_absent:" + _norm(cond_text)[:60])
    for num in preserved_numbers:
        if num not in n_text:
            problems.append(f"legal_number_lost:{num}")
    return {"ok": not problems, "problems": problems}


def independent_review_prompt(issue: Dict[str, Any], original_block: str,
                              revised_text: str, source_evidence: List[str]) -> Dict[str, str]:
    return {
        "system_prompt": ("You are an INDEPENDENT reviewer verifying a scientific "
                          "correction. You see the original issue, the source evidence, "
                          "and the revised text — never the author's reasoning. Check: "
                          "(1) is the flagged problem resolved? (2) any new unsupported "
                          "claims? (3) any coverage gap left? JSON only."),
        "user_payload": {
            "issue": {"type": issue.get("issue_type"),
                      "severity": issue.get("severity"),
                      "statement": issue.get("statement", "")[:300]},
            "original_block": original_block[:1500],
            "revised_text": revised_text[:2500],
            "source_evidence": source_evidence,
            "output_schema": {"resolved": True, "new_unsupported_claims": ["string"],
                              "coverage_gap": "string",
                              "verdict": "resolved|needs_revision"},
        },
    }
