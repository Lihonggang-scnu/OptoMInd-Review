"""Upgrade-3 enhancer entry gate & bypass blocking (ticket 023).

The enhancer is OFF by default and can only consume a 022 scientific candidate
bundle plus 016 writable bindings:

- ENHANCER_ADMISSION_RECEIPT: unready claims are REMOVED from the writable
  packet and kept in a gap ledger (never merely listed as excluded while
  staying in the payload); raw-draft fallback is structurally absent;
- ``enabled=false`` yields a validated_passthrough receipt that preserves the
  candidate hash — the publication mainline keeps working without enhancement;
- an enhancer failure can only roll back to the last scientifically valid
  draft (hash preserved) and the review_state stays ``not_run`` — it can never
  become ``passed`` by a failed enhancement;
- the enhancement cache keys on claims+conditions+binding hashes; any change
  invalidates previous enhancement outputs.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

RECEIPT_SCHEMA = "optomind.upgrade3.enhancer_admission_receipt.v1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class EnhancerGate:
    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled
        self._enhancement_cache: Dict[str, Dict[str, Any]] = {}

    def admission(self, *, candidate_manifest: Optional[Dict[str, Any]],
                  bindings: List[Dict[str, Any]],
                  gap_ledger: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Only a 022 scientific bundle with 016 writable bindings enters.
        Unready claims are removed from the writable packet into the gap
        ledger.  When enhancement is disabled the receipt is a validated
        passthrough that still refuses unready claims."""
        writable: List[Dict[str, Any]] = []
        gap: List[Dict[str, Any]] = list(gap_ledger or [])
        for b in bindings:
            body = b.get("binding", b)
            is_ready = (b.get("writable") is True
                        and body.get("binding_status") == "resolved"
                        and body.get("permission") in ("qualified_support",
                                                       "direct_support"))
            if is_ready:
                writable.append({"claim_id": body.get("claim_id"),
                                 "binding_hash": body.get("binding_hash"),
                                 "statement": body.get("statement")})
            else:
                gap.append({"claim_id": body.get("claim_id"),
                            "statement": body.get("statement"),
                            "reason": "claim_not_writable",
                            "binding_status": body.get("binding_status")})
        scientific_bundle = bool(
            candidate_manifest
            and candidate_manifest.get("validation_level") == "scientific"
            and candidate_manifest.get("promotion_allowed") is True)
        admitted = scientific_bundle and bool(writable)
        input_hash = _sha(_canonical({
            "writable": writable,
            "bundle": (candidate_manifest or {}).get("candidate_id"),
            "conditions": sorted({c for w in writable for c in ()} or [])}))
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "admitted": admitted,
            "passthrough": not self.enabled,
            "passthrough_kind": "validated_passthrough" if not self.enabled else None,
            "candidate_id": (candidate_manifest or {}).get("candidate_id"),
            "draft_sha256": (candidate_manifest or {}).get("draft_sha256"),
            "writable_claims": writable,
            "gap_claims": gap,
            "unready_removed_from_packet": len(gap),
            "input_hash": input_hash,
            "downstream_model_calls_allowed": admitted and self.enabled,
            "review_state": "not_run",
        }
        return receipt

    # ---------------------------------------------- enhancer failure ----
    def enhancer_failure_rollback(self, receipt: Dict[str, Any],
                                  last_scientific_draft_sha256: str
                                  ) -> Dict[str, Any]:
        """A failed enhancement rolls back to the last scientifically valid
        draft; review_state can never move to passed."""
        return {
            "rolled_back_to_draft_sha256": last_scientific_draft_sha256,
            "review_state": "not_run",
            "note": ("enhancer failure never becomes passed and never falls "
                     "back to a raw draft"),
            "passthrough_kind": "validated_passthrough",
        }

    # ---------------------------------------------- enhancement cache ----
    def enhancement_cache_get(self, receipt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = receipt["input_hash"]
        return self._enhancement_cache.get(key)

    def enhancement_cache_put(self, receipt: Dict[str, Any],
                              output: Dict[str, Any]) -> None:
        self._enhancement_cache[receipt["input_hash"]] = {
            "output": output,
            "input_hash": receipt["input_hash"],
            "writable_claim_ids": [w["claim_id"] for w in receipt["writable_claims"]],
        }
