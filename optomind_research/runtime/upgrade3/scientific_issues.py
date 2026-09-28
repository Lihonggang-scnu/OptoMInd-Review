"""Upgrade-3 unified scientific issue ledger (ticket 024).

All scientific review findings — block review, section review, whole-manuscript
review, final audit — enter ONE ledger with the same issue types.  Rules:

- ANY open critical finding blocks, regardless of reviewer consensus; majority
  voting can never dilute severity.  A critical with an unresolved target stays
  blocking until located.
- a false positive may close ONLY with independent source evidence (hash
  recorded); the author of the text can never close their own finding.
- scientific types: unsupported_important_number, domain_error, wrong_citation,
  system_boundary_overreach, causal_overreach, counterevidence_concealment.
  Style/terminology findings are a separate class that never masquerades as a
  load-bearing blocker (the donor's ``undefined_term`` masking a domain error
  is exactly what this separation prevents).
- review coverage receipts are mandatory: a review pass WITHOUT a receipt is
  recorded as ``not_run`` — an empty findings list from a module that was off
  or timed out can never wash through as "no problems".
- idempotent: identical findings deduplicate by stable issue_id; a finding on
  changed text forms a new revision.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

LEDGER_SCHEMA = "optomind.upgrade3.scientific_issues.v1"

SCIENTIFIC_TYPES = {
    "unsupported_important_number", "domain_error", "wrong_citation",
    "system_boundary_overreach", "causal_overreach", "counterevidence_concealment",
}
STYLE_TYPES = {"style", "terminology", "undefined_term", "repetition"}
SEVERITIES = {"critical", "major", "minor"}
STATES = {"open", "triaged", "revision_pending", "recheck_pending",
          "closed_fixed", "closed_false_positive", "unresolved"}
BLOCKING_STATES = {"open", "triaged", "revision_pending", "recheck_pending"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def stable_issue_id(*, issue_type: str, target_hash: str,
                    finding_source: str) -> str:
    return "iss_" + _sha("|".join((issue_type, target_hash, finding_source)))[:20]


class ScientificIssueLedger:
    def __init__(self, path: str = ""):
        self.path = path
        self.events: List[Dict[str, Any]] = []

    def _append(self, event: Dict[str, Any]) -> None:
        self.events.append(event)
        if self.path:
            os_mkdir(os.path.dirname(self.path) or ".")
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False,
                                        sort_keys=True) + "\n")
                handle.flush()

    # ------------------------------------------------ findings ----
    def add_finding(self, *, issue_type: str, severity: str,
                    target_hash: str, finding_source: str,
                    receipt: Dict[str, Any],
                    source_spans: Optional[List[str]] = None,
                    statement: str = "",
                    scientific: Optional[bool] = None) -> Dict[str, Any]:
        if issue_type not in SCIENTIFIC_TYPES | STYLE_TYPES:
            raise ValueError("unknown issue type: " + issue_type)
        if severity not in SEVERITIES:
            raise ValueError("unknown severity: " + severity)
        if not receipt or not receipt.get("coverage_receipt_hash"):
            raise ValueError("finding without reviewer coverage receipt")
        if scientific is None:
            scientific = issue_type in SCIENTIFIC_TYPES
        issue_id = stable_issue_id(issue_type=issue_type, target_hash=target_hash,
                                   finding_source=finding_source)
        event = {
            "event": "finding",
            "issue_id": issue_id,
            "issue_type": issue_type,
            "scientific": scientific,
            "severity": severity,
            "target_hash": target_hash,
            "finding_source": finding_source,
            "coverage_receipt_hash": receipt["coverage_receipt_hash"],
            "source_spans": source_spans or [],
            "statement": statement[:400],
            "state": "open",
        }
        self._append(event)
        return event

    def close(self, issue_id: str, *, verdict: str, closer: str,
              independent_evidence_hash: str = "") -> Dict[str, Any]:
        if verdict not in ("closed_fixed", "closed_false_positive", "unresolved"):
            raise ValueError("bad close verdict")
        if verdict == "closed_false_positive" and not independent_evidence_hash:
            raise ValueError("false_positive_requires_independent_evidence")
        event = {"event": "close", "issue_id": issue_id, "verdict": verdict,
                 "closer": closer,
                 "independent_evidence_hash": independent_evidence_hash}
        self._append(event)
        return event

    # ------------------------------------------------ projection ----
    def project(self) -> Dict[str, Any]:
        state: Dict[str, Dict[str, Any]] = {}
        for evt in self.events:
            iid = evt.get("issue_id")
            if evt["event"] == "finding":
                prev = state.get(iid)
                if prev and _canonical(prev.get("original")) == _canonical(evt):
                    continue  # idempotent replay
                if prev:
                    prev["revisions"] = prev.get("revisions", []) + [evt]
                    # a later finding with higher severity on the same target
                    # restores blocking (critical never diluted)
                    prev["severity"] = max(prev["severity"], evt["severity"],
                                           key=lambda s: {"critical": 2, "major": 1,
                                                          "minor": 0}[s])
                    prev["state"] = "open"
                else:
                    state[iid] = dict(evt, revisions=[])
            elif evt["event"] == "close":
                cur = state.get(iid)
                if cur:
                    cur["state"] = evt["verdict"]
                    cur["closed_by"] = evt["closer"]
        issues = list(state.values())
        open_critical = [i for i in issues
                         if i["severity"] == "critical"
                         and i["state"] in BLOCKING_STATES]
        blocking = bool(open_critical)
        return {
            "schema_version": LEDGER_SCHEMA,
            "issues": issues,
            "open_critical": [i["issue_id"] for i in open_critical],
            "blocking": blocking,
            "counts": _counts(issues),
        }


def _counts(issues: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i in issues:
        key = i["state"] + ":" + i["severity"]
        out[key] = out.get(key, 0) + 1
    return out


def os_mkdir(path: str) -> None:
    import os
    os.makedirs(path, exist_ok=True)


def review_state(receipt: Optional[Dict[str, Any]],
                 ledger: Optional[ScientificIssueLedger] = None) -> Dict[str, Any]:
    """Review state for a candidate: passed requires a coverage receipt AND no
    open critical findings.  Missing receipt => not_run (never passed)."""
    if not receipt or not receipt.get("coverage_receipt_hash"):
        return {"review_state": "not_run", "passed": False,
                "reason": "review_coverage_receipt_missing"}
    if receipt.get("reviewer_error"):
        return {"review_state": "not_run", "passed": False,
                "reason": "reviewer_error:" + str(receipt.get("reviewer_error"))[:100]}
    blocking = False
    if ledger is not None:
        proj = ledger.project()
        blocking = proj["blocking"]
    state = "passed" if not blocking else "blocking"
    return {"review_state": state, "passed": state == "passed" and not blocking,
            "blocking": blocking}
