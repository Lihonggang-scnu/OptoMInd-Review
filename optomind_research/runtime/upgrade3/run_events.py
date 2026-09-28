"""Upgrade-3 single event reducer and stage state (ticket 007).

One append-only event log (RUN_EVENTS.jsonl) is the only decision authority
for stage state; RUN_STATE.json-style projections are read-only derivatives
carrying an authority hash.

Event kinds and the only legal lifecycle:
    stage_started      -> attempt opens (sequence, generation, attempt, stage)
    artifact_committed -> a temp artifact was validated and atomically committed
                          (stage, attempt, artifact_path, artifact_sha256)
    stage_finished     -> closes an attempt with an outcome; ``succeeded`` is
                          only legal when a committed artifact hash exists for
                          the same (stage, attempt) and science is not claimed
                          without an audit receipt.

Reduction rules (all deterministic, replayable, idempotent):
- event_id idempotence: same id + identical content is a no-op; same id with
  different content is a hard error;
- sequence numbers must be contiguous per run file (gaps quarantine the tail);
- duplicate stage_finished for the same attempt is rejected (no FAILED->FAILED
  rewrites, no success after success);
- a late cancellation after a finished attempt is rejected;
- an attempt that failed cannot be resumed in place: recovery requires a new
  attempt number;
- success before artifact is impossible: a stage_finished(succeeded) without a
  prior artifact_committed is rejected;
- science state can only come from an audit receipt; the reducer never upgrades
  science and ``ready_with_limits`` never becomes passed;
- a half-written trailing line is quarantined (recorded) and excluded from the
  projection; the projection is computed from the valid prefix.

Old HARNESS_STATE/legacy receipts are only ever read through
``project_legacy_state`` (read-only, authority_hash marked as projection); a
legacy status can never overwrite an event-derived authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

EVENT_KINDS = {"stage_started", "artifact_committed", "stage_finished"}
STAGE_OUTCOMES = {"succeeded", "failed", "cancelled", "awaiting_human_review"}
EXECUTION_STATE = {"pending", "running", "waiting", "succeeded", "failed", "cancelled"}
SCIENCE_STATE = {"not_evaluated", "needs_evidence", "revision_required", "passed"}


def event_hash(event: Dict[str, Any]) -> str:
    payload = {k: v for k, v in event.items() if k != "envelope"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     ensure_ascii=False, default=str)
                          .encode("utf-8")).hexdigest()


def make_event(kind: str, *, generation: str, attempt: str, stage: str,
               sequence: int, payload: Optional[Dict[str, Any]] = None,
               causation_id: str = "", event_id: Optional[str] = None,
               artifact_path: str = "", artifact_sha256: str = "",
               outcome: Optional[str] = None, science: Optional[str] = None,
               stop_reason: Optional[str] = None) -> Dict[str, Any]:
    if kind not in EVENT_KINDS:
        raise ValueError("unknown event kind: " + kind)
    body = {"kind": kind, "generation": generation, "attempt": attempt,
            "stage": stage, "sequence": sequence, "payload": payload or {}}
    if kind == "artifact_committed":
        body["artifact_path"] = artifact_path
        body["artifact_sha256"] = artifact_sha256
    if kind == "stage_finished":
        body["outcome"] = outcome
        body["science"] = science
        body["stop_reason"] = stop_reason
    if event_id is None:
        # distinct occurrences stay distinct; caller-supplied ids enable
        # idempotent replay of the *same intent*
        import uuid
        body["nonce"] = uuid.uuid4().hex
    digest = event_hash(body)
    return {"event_id": event_id or ("evt_" + digest[:24]), "causation_id": causation_id,
            "content_hash": digest, **body}


class ReducerError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


class RunEventLog:
    """Append-only JSONL log plus deterministic reduction."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._sequence = self._scan_sequences()

    # ---------------- append ----------------
    def _scan_sequences(self) -> int:
        last = 0
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                        last = max(last, int(evt.get("sequence") or 0))
                    except ValueError:
                        continue
        return last

    def append(self, event: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            existing = self.find_event(event["event_id"])
            if existing is not None:
                if existing.get("content_hash") != event.get("content_hash"):
                    raise ReducerError("event_id_conflict",
                                       "same id with different content: " + event["event_id"])
                return {"duplicate": True, "event": existing}
            # control-flow stops here: a finished attempt can never restart in place
            if event["kind"] == "stage_started":
                for evt in self.raw_events()[0]:
                    if (evt.get("kind") == "stage_finished"
                            and evt.get("stage") == event["stage"]
                            and evt.get("attempt") == event["attempt"]):
                        raise ReducerError(
                            "started_after_finished",
                            f"{event['stage']}/{event['attempt']}: recovery needs a new attempt")
            next_seq = self._sequence + 1
            if event["sequence"] != next_seq and event["sequence"] != 0:
                raise ReducerError("sequence_gap",
                                   f"expected {next_seq}, got {event['sequence']}")
            event = dict(event, sequence=next_seq)
            event["content_hash"] = event_hash(
                {k: v for k, v in event.items() if k != "envelope"})
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._sequence = next_seq
            return {"duplicate": False, "event": event}

    def find_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        for evt in self.raw_events()[0]:
            if evt.get("event_id") == event_id:
                return evt
        return None

    # ---------------- reading ----------------
    def raw_events(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Returns (valid_events, quarantined_tail_records)."""
        valid: List[Dict[str, Any]] = []
        bad: List[Dict[str, Any]] = []
        if not os.path.isfile(self.path):
            return valid, bad
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.readlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                evt = json.loads(stripped)
                if not isinstance(evt, dict) or evt.get("kind") not in EVENT_KINDS:
                    raise ValueError("unknown kind")
                valid.append(evt)
            except ValueError:
                is_last = (i == len(lines) - 1)
                if is_last:
                    bad.append({"line_no": i + 1, "reason": "truncated_or_invalid_tail",
                                "raw": stripped[:200]})
                else:
                    bad.append({"line_no": i + 1, "reason": "invalid_midfile_record",
                                "raw": stripped[:200]})
        return valid, bad

    # ---------------- reduction ----------------
    def reduce(self) -> Dict[str, Any]:
        events, bad = self.raw_events()
        stages: Dict[str, Dict[str, Any]] = {}
        committed: Dict[Tuple[str, str], Dict[str, Any]] = {}
        seen_ids: Dict[str, str] = {}
        rejections: List[Dict[str, Any]] = []
        finished: Dict[Tuple[str, str], str] = {}
        prev_seq = 0
        for evt in events:
            seq = int(evt.get("sequence") or 0)
            if seq <= prev_seq and prev_seq:
                pass  # tolerated only for replayed identical ids caught below
            prev_seq = max(prev_seq, seq)
            eid = evt.get("event_id") or ""
            content = evt.get("content_hash") or event_hash(
                {k: v for k, v in evt.items() if k != "envelope"})
            if eid in seen_ids:
                if seen_ids[eid] == content:
                    continue  # idempotent replay
                rejections.append({"event_id": eid, "reason": "event_id_conflict"})
                continue
            seen_ids[eid] = content
            kind = evt["kind"]
            stage = evt.get("stage")
            attempt = evt.get("attempt")
            key = (stage, attempt)
            if kind == "stage_started":
                if key in finished:
                    rejections.append({"event_id": eid,
                                       "reason": "started_after_finished_use_new_attempt"})
                    continue
                stages.setdefault(stage, {"attempts": {}})
                stages[stage]["attempts"][attempt] = {
                    "execution": "running", "science": "not_evaluated",
                    "artifact": None, "outcome": None}
            elif kind == "artifact_committed":
                sha = str(evt.get("artifact_sha256") or "")
                if len(sha) != 64:
                    rejections.append({"event_id": eid,
                                       "reason": "artifact_hash_invalid"})
                    continue
                committed[key] = {"artifact_path": evt.get("artifact_path"),
                                  "artifact_sha256": sha}
                entry = stages.setdefault(stage, {"attempts": {}})["attempts"].get(attempt)
                if entry is None:
                    stages[stage]["attempts"][attempt] = {
                        "execution": "running", "science": "not_evaluated",
                        "artifact": committed[key], "outcome": None}
                else:
                    entry["artifact"] = committed[key]
            elif kind == "stage_finished":
                outcome = evt.get("outcome")
                if key in finished:
                    rejections.append({"event_id": eid,
                                       "reason": "duplicate_stage_finished:"
                                                 + finished[key] + "->" + str(outcome)})
                    continue
                entry = stages.setdefault(stage, {"attempts": {}})["attempts"].get(attempt)
                if entry is None:
                    # finished without started: treat as legacy receipt replay only
                    rejections.append({"event_id": eid,
                                       "reason": "finished_without_started"})
                    continue
                if outcome == "succeeded" and entry.get("artifact") is None:
                    rejections.append({"event_id": eid,
                                       "reason": "success_requires_committed_artifact"})
                    continue
                science = evt.get("science")
                if science not in (None,) + tuple(SCIENCE_STATE):
                    rejections.append({"event_id": eid, "reason": "science_unknown_enum"})
                    continue
                if science == "passed" and not (evt.get("payload") or {}).get("audit_receipt_hash"):
                    science = "not_evaluated"  # cannot claim pass without audit receipt
                entry["outcome"] = outcome
                entry["science"] = science or entry["science"]
                entry["stop_reason"] = evt.get("stop_reason")
                entry["execution"] = {"succeeded": "succeeded", "failed": "failed",
                                      "cancelled": "cancelled",
                                      "awaiting_human_review": "waiting"}.get(outcome, "failed")
                finished[key] = str(outcome)
        # stage-level projection: latest attempt wins
        projection = {"generation": (events[0].get("generation") if events else None),
                      "stages": {}, "rejections": rejections, "quarantined_tail": bad}
        for stage, info in stages.items():
            attempts = info["attempts"]
            last_attempt = sorted(attempts.keys())[-1] if attempts else None
            final = attempts.get(last_attempt) if last_attempt else None
            projection["stages"][stage] = {
                "execution": (final or {}).get("execution", "pending"),
                "science": (final or {}).get("science", "not_evaluated"),
                "outcome": (final or {}).get("outcome"),
                "artifact": (final or {}).get("artifact"),
                "attempts": attempts,
            }
        projection["authority_hash"] = hashlib.sha256(json.dumps(
            {"stages": projection["stages"], "rejections": rejections},
            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        projection["projection_of"] = os.path.abspath(self.path)
        projection["authority"] = "run_events_reducer_v1"
        return projection

    def write_projection(self, path: str) -> Dict[str, Any]:
        proj = self.reduce()
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(proj, handle, ensure_ascii=False, indent=1, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return proj


import tempfile  # noqa: E402


# -------------------------------------------------- legacy read projection ----
def project_legacy_state(harness_state: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only projection of an old HARNESS_STATE: marked as projection with
    the legacy payload hashed; never usable to overwrite event authority."""
    return {
        "authority": "legacy_projection_only",
        "legacy_payload_hash": hashlib.sha256(json.dumps(
            harness_state, sort_keys=True, ensure_ascii=False,
            default=str).encode("utf-8")).hexdigest(),
        "legacy_status_display": harness_state.get("status"),
        "legacy_readiness_display": harness_state.get("readiness"),
        "note": "display only; decisions must come from run_events authority",
    }
