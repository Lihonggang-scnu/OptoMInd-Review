"""Human-in-the-loop decision gate (P2-1).

A minimal, file-backed mechanism for "a human needs to look at this":
  - request_decision  register a pending decision, return its id
  - resolve_decision  record the answer (append-only ledger)
  - list_pending      enumerate undecided items
  - decision_state    inspect one decision id

This is infrastructure only.  It does not notify anyone, has no UI
(that is P2-2), uses no database, and never rewrites history: the
decision ledger DECISION_LEDGER.jsonl is append-only by construction.

Formerly mandatory-human kinds (auto-accept forbidden, even under pytest):
  - delivery_gate          the quality master switch
  - reference_number_check academic-integrity failures
  - budget_overrun         spending must never be approved by a timer

POLICY CHANGE, round-3 backend ticket (explicit user authorization): the
mandatory set is now EMPTY so every kind can time out and default to the
accept option.  The user decided that a run stopped on an unanswered gate
must not be reported as a failure; a bounded 30 s wait that ends in a
documented auto-accept is preferred over an indefinite stall.  In the
current codebase only ``delivery_gate`` (review_harness_orchestrator) and
``visual_review`` (visual_evidence_factory) are ever registered, so the
relaxation is effective for exactly one formerly-mandatory kind; the other
two names were never wired to any call site.  ``_MANDATORY_HUMAN_KINDS`` is
kept as an (empty) constant so the enforcement code below stays readable.
Auto-acceptable kinds today:
  - visual_review          bounded blast radius, cost already capped
Unwired future kinds (TODO; do not wire without an architect ticket):
  - discovery_needs_more_literature   see research_program_runner.py
                                      :1229 (P1-4 left the TODO there)
  - style_governance_sample           P1-1 hard verifier already bounds it

Status vocabulary reuses P0-2 verbatim (_AWAITING_HUMAN_STATUSES in
delivery_contract.py); this module introduces no near-synonyms.
No model calls happen anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PENDING_DIR_NAME = "PENDING_DECISIONS"
LEDGER_NAME = "DECISION_LEDGER.jsonl"

# Per-run thread locks. Keyed by resolved run_dir string so different
# run directories never contend with each other. (P3-5: in-process only;
# multi-process scenarios are out of scope.)
_RUN_LOCKS: dict[str, threading.RLock] = {}
_RUN_LOCKS_META = threading.Lock()


def _get_run_lock(run_dir: Path) -> threading.RLock:
    """Return (creating if needed) the per-run RLock."""
    key = str(Path(run_dir).resolve())
    with _RUN_LOCKS_META:
        if key not in _RUN_LOCKS:
            _RUN_LOCKS[key] = threading.RLock()
        return _RUN_LOCKS[key]

# Kinds whose decisions may never be auto-accepted (ticket section 3.4).
# Round-3 policy change (see module docstring): emptied by explicit user
# authorization so gates time out into a documented auto-accept instead of
# stalling a finished run.
_MANDATORY_HUMAN_KINDS = frozenset()
_VISUAL_REVIEW_KIND = "visual_review"
_PYTEST_ENV_VAR = "PYTEST_CURRENT_TEST"


def _decision_id(kind: str, subject_id: str) -> str:
    digest = hashlib.sha256(
        f"{kind}:{subject_id}".encode("utf-8")
    ).hexdigest()
    return digest[:12]


def _ledger_path(run_dir: Path) -> Path:
    return Path(run_dir) / LEDGER_NAME


def _pending_path(run_dir: Path, decision_id: str) -> Path:
    return Path(run_dir) / PENDING_DIR_NAME / f"{decision_id}.json"


def _utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_ledger(run_dir: Path, row: Dict[str, Any]) -> None:
    """Append one JSON line.  Never seek, never rewrite existing lines."""
    path = _ledger_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def _write_pending_atomic(path: Path, payload: Dict[str, Any]) -> None:
    # P3-5: use a unique tmp name to avoid collisions under concurrent calls
    # targeting the same decision file.
    tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _read_pending(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"pending file {path.name} is not an object")
    return value


def request_decision(
    run_dir: Path,
    kind: str,
    subject_id: str,
    context: Dict[str, Any],
    options: List[str],
    auto_accept_after_seconds: Optional[float] = None,
    default_option: Optional[str] = None,
) -> str:
    """Register one pending decision and return its stable decision_id.

    Same kind+subject while still pending returns the existing id without
    writing another ledger row.  Mandatory-human kinds refuse any
    auto_accept_after_seconds (hard constraint, also enforced under
    pytest).  For kind="visual_review" only: when running under pytest
    with no explicit timeout, a zero-second auto-accept replaces an
    infinite wait so test suites can never hang on a human.
    """

    run_dir = Path(run_dir)
    kind = str(kind or "").strip()
    subject_id = str(subject_id or "")
    if not kind:
        raise ValueError("kind must be a non-empty string")
    if (
        not isinstance(options, list)
        or not options
        or any(not isinstance(item, str) or not item for item in options)
    ):
        raise ValueError("options must be a non-empty list of strings")
    if len(set(options)) != len(options):
        raise ValueError("options must not contain duplicates")
    if kind in _MANDATORY_HUMAN_KINDS and auto_accept_after_seconds is not None:
        raise ValueError(
            f"kind={kind!r} requires a human decision; "
            "auto_accept_after_seconds must stay None"
        )
    effective_seconds = auto_accept_after_seconds
    if (
        effective_seconds is None
        and kind == _VISUAL_REVIEW_KIND
        and os.environ.get(_PYTEST_ENV_VAR)
    ):
        # Section 5 hard guard: pytest must never block forever on a
        # visual review.  Scoped strictly to the auto-acceptable kind;
        # mandatory-human kinds above are unaffected.
        effective_seconds = 0
    if (
        effective_seconds is not None
        and default_option is not None
        and default_option not in options
    ):
        raise ValueError(
            "default_option must be one of options whenever a timeout is set"
        )
    # A timeout with default_option=None expires onto options[0]; this is
    # what the pytest hard guard above relies on so tests never need to
    # know the option vocabulary.
    # P3-3: registration is the sanctioned side-effect point for expiry —
    # list_pending/decision_state must stay pure reads.
    # P3-5: the check-write sequence below runs under the per-run lock so
    # concurrent registrations of the same kind+subject cannot race
    # (TOCTOU on pending existence, duplicate ledger rows).
    with _get_run_lock(run_dir):
        _expire_due(run_dir)
        decision_id = _decision_id(kind, subject_id)
        pending_path = _pending_path(run_dir, decision_id)
        if pending_path.exists():
            return decision_id
        now = time.time()
        payload = {
            "decision_id": decision_id,
            "kind": kind,
            "subject_id": subject_id,
            "context": dict(context or {}),
            "options": list(options),
            "auto_accept_after_seconds": effective_seconds,
            "requested_default_option": default_option,
            "created_ts": now,
        }
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        _write_pending_atomic(pending_path, payload)
        _append_ledger(
            run_dir,
            {
                "ts": _utc_ts(),
                "event": "requested",
                "decision_id": decision_id,
                "kind": kind,
                "subject_id": subject_id,
                "options": list(options),
                "chosen": None,
                "actor": None,
                "auto": False,
                "note": "",
                "auto_accept_after_seconds": effective_seconds,
            },
        )
        return decision_id


def _expire_due(run_dir: Path) -> None:
    """Auto-resolve every pending item whose timeout has passed."""

    pending_dir = Path(run_dir) / PENDING_DIR_NAME
    if not pending_dir.is_dir():
        return
    now = time.time()
    for path in sorted(pending_dir.glob("*.json")):
        try:
            payload = _read_pending(path)
        except Exception:
            continue
        seconds = payload.get("auto_accept_after_seconds")
        if seconds is None:
            continue
        try:
            due = float(payload.get("created_ts") or 0.0) + float(seconds)
        except Exception:
            continue
        if now < due:
            continue
        chosen = payload.get("requested_default_option")
        options = list(payload.get("options") or [])
        if chosen not in options:
            chosen = options[0] if options else None
        _append_ledger(
            run_dir,
            {
                "ts": _utc_ts(),
                "event": "resolved",
                "decision_id": payload.get("decision_id"),
                "kind": payload.get("kind"),
                "subject_id": payload.get("subject_id"),
                "options": options,
                "chosen": chosen,
                "actor": "auto",
                "auto": True,
                "note": "auto_accepted_after_timeout",
            },
        )
        path.unlink(missing_ok=True)


def resolve_decision(
    run_dir: Path,
    decision_id: str,
    chosen: str,
    actor: str,
    note: str = "",
) -> None:
    """Record the answer for one pending decision.

    Raises when the id is unknown or already resolved (double resolve is
    an error, never a silent overwrite) and when ``chosen`` is not among
    the registered options (the ledger stays untouched then).
    """

    run_dir = Path(run_dir)
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("actor must be a non-empty string (human or auto)")
    # P3-5: read-validate-append-delete runs under the per-run lock so
    # concurrent resolves of one decision produce exactly one ledger row.
    with _get_run_lock(run_dir):
        pending_path = _pending_path(run_dir, decision_id)
        if not pending_path.exists():
            raise KeyError(
                f"no pending decision {decision_id!r}: unknown or already resolved"
            )
        payload = _read_pending(pending_path)
        options = list(payload.get("options") or [])
        if chosen not in options:
            raise ValueError(
                f"chosen={chosen!r} is not among the registered options {options!r}"
            )
        _append_ledger(
            run_dir,
            {
                "ts": _utc_ts(),
                "event": "resolved",
                "decision_id": decision_id,
                "kind": payload.get("kind"),
                "subject_id": payload.get("subject_id"),
                "options": options,
                "chosen": chosen,
                "actor": actor,
                "auto": actor == "auto",
                "note": str(note or ""),
            },
        )
        pending_path.unlink()


def list_pending(run_dir: Path) -> List[Dict[str, Any]]:
    """Return all still-pending payloads, oldest decision first.

    Pure read — does NOT auto-expire.  Call expire_due_decisions()
    explicitly when expiry is needed.
    """

    run_dir = Path(run_dir)
    # P3-3: _expire_due removed — this function must remain side-effect-free
    # (GET /decisions in the local UI depends on that contract).
    pending_dir = run_dir / PENDING_DIR_NAME
    if not pending_dir.is_dir():
        return []
    items: List[Dict[str, Any]] = []
    for path in sorted(pending_dir.glob("*.json")):
        try:
            items.append(_read_pending(path))
        except Exception:
            continue
    return items


def decision_state(run_dir: Path, decision_id: str) -> Dict[str, Any]:
    """Return {"state": "pending"| "resolved", ...} for one decision id.

    Pure read — does NOT auto-expire (P3-3).  Unknown ids raise KeyError;
    callers can catch that to distinguish a typo from a genuinely open
    question.
    """

    run_dir = Path(run_dir)
    pending_path = _pending_path(run_dir, decision_id)
    if pending_path.exists():
        payload = _read_pending(pending_path)
        return {"state": "pending", **payload}
    ledger_path = _ledger_path(run_dir)
    if ledger_path.is_file():
        resolved_row = None
        for raw in ledger_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if (
                isinstance(row, dict)
                and row.get("decision_id") == decision_id
                and row.get("event") == "resolved"
            ):
                resolved_row = row
        if resolved_row is not None:
            return {
                "state": "resolved",
                "decision_id": decision_id,
                "chosen": resolved_row.get("chosen"),
                "actor": resolved_row.get("actor"),
                "auto": bool(resolved_row.get("auto")),
                "ts": resolved_row.get("ts"),
                "note": resolved_row.get("note"),
            }
    raise KeyError(f"unknown decision id {decision_id!r}")


def decision_history(run_dir: Path, limit: int = 500) -> List[Dict[str, Any]]:
    """Return the newest-first ledger rows (requested + resolved), capped.

    Read-only view for consumers such as the P2-2 local UI.  The ledger
    format stays owned by this module: callers must not parse the JSONL
    themselves.  Appends are never rewritten, so a plain reverse scan is
    the whole implementation.
    """

    ledger_path = _ledger_path(Path(run_dir))
    if not ledger_path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    with ledger_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    rows.reverse()
    return rows[: max(0, int(limit))]


def expire_due_decisions(run_dir: Path) -> None:
    """Explicitly expire all pending decisions whose timeout has passed.

    Call this from harness loops or a dedicated UI endpoint; do NOT rely
    on list_pending/decision_state side effects (P3-3 made those pure
    reads).
    """
    # P3-5: _expire_due itself stays lock-free (internal helper); callers
    # hold the per-run lock. request_decision already holds it.
    run_dir = Path(run_dir)
    with _get_run_lock(run_dir):
        _expire_due(run_dir)
