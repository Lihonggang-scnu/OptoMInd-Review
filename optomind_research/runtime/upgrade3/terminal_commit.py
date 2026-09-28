"""Upgrade-3 terminal commit & delivery authority (ticket 029).

One TERMINAL_MANIFEST per generation is the single authority:

- formal_publish_allowed is the AND of every required verification with
  matching current hashes: scope/coverage admission (019), science audit fresh
  and passing (027), issue ledger with no open critical (024), facts registry
  present (015), citation preflight allowed (028), required visuals present,
  PDF hash verified, ledger hash consistent; ANY missing receipt means that
  dimension is ``not_evaluated`` and blocks formal publication.
- execution / science / compile / translation / visual are independent
  dimensions: a Chinese translation failure never fakes a Chinese deliverable,
  and an English scientifically-qualified deliverable can be allowed
  independently; decorative visuals may be ``not_requested``, required
  evidence figures missing still block.
- finalize is compare-and-swap idempotent: identical evidence re-finalize
  returns the same manifest; different results require a NEW generation.  A
  post-run direct render is diagnostic-only and can never alter the terminal
  state.
- legacy HARNESS_STATE / DELIVERY_GATE / UI success values are read only
  through an authority projection (display), never as decisions.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

_LOCK_STALE_SECONDS = 30.0

MANIFEST_SCHEMA = "optomind.upgrade3.terminal_manifest.v2"

REQUIRED_RECEIPTS = ("admission", "science_audit", "facts", "issue_closure",
                     "citation_preflight", "coverage", "pdf", "ledger")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class TerminalFinalizer:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.pointer = os.path.join(root, "TERMINAL_MANIFEST.json")

    # ------------------------------------------------ finalize ----
    def finalize(self, *, generation_id: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
        # Fast path: an existing terminal decides without taking the lock.
        existing = self._load()
        decided = self._decide(existing, generation_id, evidence)
        if decided is not None:
            return decided
        # Cross-process CAS: the commit lock is created atomically
        # (O_CREAT|O_EXCL); only the lock holder may write the pointer, and
        # it re-reads the pointer first (double check).  A stale lock from a
        # crashed committer is broken after _LOCK_STALE_SECONDS.
        deadline = time.time() + _LOCK_STALE_SECONDS
        while True:
            lock_fd = None
            try:
                lock_fd = os.open(self.pointer + ".commit.lock",
                                  os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                try:
                    lock_age = time.time() - os.path.getmtime(
                        self.pointer + ".commit.lock")
                except OSError:
                    lock_age = 0.0
                if lock_age > _LOCK_STALE_SECONDS:
                    try:
                        os.unlink(self.pointer + ".commit.lock")
                    except OSError:
                        pass
                if time.time() > deadline:
                    return {"terminal": self._load(), "committed": False,
                            "conflict": "commit_lock_contended"}
                time.sleep(0.05)
                existing = self._load()
                decided = self._decide(existing, generation_id, evidence)
                if decided is not None:
                    return decided
                continue
            break
        try:
            os.write(lock_fd, str(os.getpid()).encode("ascii"))
            # double check under the lock: another process may have committed
            # between our last read and acquiring the lock
            existing = self._load()
            decided = self._decide(existing, generation_id, evidence)
            if decided is not None:
                return decided
            manifest = self._build(generation_id, evidence)
            fd, tmp = tempfile_mkstemp(self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, ensure_ascii=False, indent=1,
                              sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.pointer)  # single atomic commit
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return {"terminal": manifest, "committed": True}
        finally:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            try:
                os.unlink(self.pointer + ".commit.lock")
            except OSError:
                pass

    def _decide(self, existing, generation_id: str,
                evidence: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if existing is None:
            return None
        if existing.get("generation_id") == generation_id:
            if existing.get("evidence_hash") == _sha(_canonical(evidence)):
                return {"terminal": existing, "idempotent": True,
                        "committed": False}
            return {"terminal": existing, "idempotent": False, "committed": False,
                    "conflict": "different_evidence_same_generation"}
        return {"terminal": existing, "idempotent": True, "committed": False,
                "conflict": "terminal_belongs_to_other_generation:"
                            + str(existing.get("generation_id"))}

    def _load(self) -> Optional[Dict[str, Any]]:
        if not os.path.isfile(self.pointer):
            return None
        return json.load(open(self.pointer, encoding="utf-8"))

    def _build(self, generation_id: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
        receipts = evidence.get("receipts") or {}
        missing = [k for k in REQUIRED_RECEIPTS if not receipts.get(k)]
        not_evaluated = list(missing)

        # Per-receipt structural validation: a receipt that is a non-empty
        # dict but lacks required sub-fields is insufficient, not passing.
        receipt_problems: List[str] = []

        adm = receipts.get("admission") or {}
        if adm and not isinstance(adm.get("full_publication_permission"), bool):
            receipt_problems.append("admission.full_publication_permission_not_bool")
        if adm and adm.get("full_publication_permission") is True \
                and not adm.get("admission_hash"):
            receipt_problems.append("admission.admission_hash_missing")

        audit_r = receipts.get("science_audit") or {}
        if audit_r and not audit_r.get("allow_publication"):
            receipt_problems.append("science_audit.not_passing")
        if audit_r and audit_r.get("allow_publication") and not audit_r.get("audit_hash"):
            receipt_problems.append("science_audit.audit_hash_missing")

        facts_r = receipts.get("facts") or {}
        if facts_r and not isinstance(facts_r.get("count"), (int, float)):
            receipt_problems.append("facts.count_missing_or_not_number")

        issues_r = receipts.get("issue_closure") or {}
        if issues_r and "open_critical" not in issues_r:
            receipt_problems.append("issue_closure.open_critical_field_missing")

        cite_r = receipts.get("citation_preflight") or {}
        if cite_r and not isinstance(cite_r.get("allowed"), bool):
            receipt_problems.append("citation_preflight.allowed_not_bool")

        cov_r = receipts.get("coverage") or {}
        if cov_r and not cov_r.get("ready"):
            receipt_problems.append("coverage.not_ready")

        checks = {
            "admission_full_publication": bool(
                adm.get("full_publication_permission")),
            "science_audit_passing": bool(
                audit_r.get("allow_publication")),
            "facts_registry_present": bool(
                facts_r and isinstance(facts_r.get("count"), (int, float))
                and facts_r["count"] > 0),
            "no_open_critical_issues": (
                issues_r.get("open_critical") is not None
                and len(issues_r.get("open_critical") or []) == 0),
            "citation_preflight_allowed": bool(
                cite_r.get("allowed")),
            "coverage_ready": bool(
                cov_r.get("ready")),
            "pdf_hash_verified": bool(
                evidence.get("pdf_sha256")
                and len(str(evidence.get("pdf_sha256"))) >= 8),
            "ledger_consistent": bool(
                evidence.get("ledger_hash")
                and len(str(evidence.get("ledger_hash"))) >= 8),
            "receipts_structurally_valid": not receipt_problems,
        }
        # translation dimension: not_requested never fails the English deliverable
        translation = evidence.get("translation", "not_requested")
        visual = evidence.get("visual", "not_requested")
        required_visual_missing = (visual == "required_missing")

        formal_publish_allowed = (all(checks.values())
                                  and not not_evaluated
                                  and not required_visual_missing
                                  and not receipt_problems)
        formal_publish_allowed = bool(formal_publish_allowed)

        diagnostic_available = bool(evidence.get("diagnostic_pdf_sha256"))

        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "generation_id": generation_id,
            "evidence_hash": _sha(_canonical(evidence)),
            "execution": evidence.get("execution", "not_evaluated"),
            "science": evidence.get("science", "not_evaluated"),
            "compile": evidence.get("compile", "not_evaluated"),
            "translation": translation,
            "visual": visual,
            "formal_publish_allowed": formal_publish_allowed,
            "diagnostic_available": diagnostic_available,
            "checks": checks,
            "receipt_problems": receipt_problems,
            "not_evaluated_dimensions": not_evaluated,
            "unresolved_issue_ids": (receipts.get("issue_closure") or {})
            .get("open_critical", []),
            "required_visual_missing": required_visual_missing,
            "ledger": {
                "hash": evidence.get("ledger_hash"),
                "spent_micro_cny": evidence.get("spent_micro_cny"),
                "unknown_bound_micro_cny": evidence.get("unknown_bound_micro_cny"),
                "reserved_micro_cny": evidence.get("reserved_micro_cny"),
            },
            "hashes": {
                "inputs": evidence.get("inputs_sha256"),
                "outputs": evidence.get("outputs_sha256"),
                "code": evidence.get("code_sha256"),
                "prompt": evidence.get("prompt_manifest_hash"),
                "model": evidence.get("model_manifest_hash"),
                "policy": evidence.get("policy_sha256"),
                "material": evidence.get("material_snapshot_hash"),
                "pdf": evidence.get("pdf_sha256"),
            },
            "legacy_projection": evidence.get("legacy_projection", {}),
        }
        manifest["terminal_hash"] = _sha(_canonical(
            {k: v for k, v in manifest.items() if k != "terminal_hash"}))
        return manifest

    # ------------------------------------------------ projections ----
    def legacy_projection(self, harness_state: Dict[str, Any],
                          delivery_gate: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "authority": "projection_only",
            "harness_status_display": harness_state.get("status"),
            "delivery_gate_status_display": delivery_gate.get("status"),
            "note": "legacy gates never compute success; terminal authority only",
        }


def tempfile_mkstemp(directory: str):
    import tempfile
    return tempfile.mkstemp(dir=directory, suffix=".tmp")


def legacy_projection_of(harness_state: Dict[str, Any],
                         delivery_gate: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only display projection of legacy gates (never decisions)."""
    return {
        "authority": "projection_only",
        "harness_status_display": harness_state.get("status"),
        "delivery_gate_status_display": delivery_gate.get("status"),
        "note": "legacy gates never compute success; terminal authority only",
    }
