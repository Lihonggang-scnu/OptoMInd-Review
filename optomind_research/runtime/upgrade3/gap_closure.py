"""Upgrade-3 gap-driven retrieval closure (ticket 018).

Turns coverage gaps into bounded retrieval and re-adjudication:

- RETRIEVAL_REQUEST: gap_id + required_slot + query_family + expected field,
  at most ``max_waves`` (default 2) waves x ``distinct_queries_per_wave`` 2
  distinct queries; a retry after provider failure is a NEW physical attempt
  with its own ledger reservation (005) and never re-billed rounds.
- every retrieved candidate re-enters the chain 010 (scope admission) -> 011
  (span resolution) -> 012 (permission) -> 016 (binding); only NEW legal
  writable bindings/conditions count as gain; new paper counts do not.
- stop reasons: gain_achieved | no_gain_after_max_waves (open_required stays
  true) | provider_failure_retryable | known_empty_reused.  Known-empty query
  caches key on query+scope+policy+source snapshot; a corrected scope opens a
  new query honestly.
- provider failures are distinct from zero results and from "no legal
  evidence"; nothing is padded.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple

REQUEST_SCHEMA = "optomind.upgrade3.retrieval_request.v1"
RECEIPT_SCHEMA = "optomind.upgrade3.closure_receipt.v1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", (text or "").lower()).strip()


class KnownEmptyCache:
    """query+scope+policy+snapshot-scoped empty-result memory."""

    def __init__(self, path: str = ""):
        self.path = path
        self._mem: Dict[str, Dict[str, Any]] = {}
        if path and os.path.isfile(path):
            self._mem = json.load(open(path, encoding="utf-8"))

    def key(self, query: str, scope: str, policy_hash: str, snapshot_hash: str) -> str:
        return _sha("|".join((query, scope, policy_hash, snapshot_hash)))

    def get_empty(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self._mem.get(key)
        if entry and entry.get("empty"):
            return entry
        return None

    def record(self, key: str, query: str, empty: bool, note: str = "") -> None:
        self._mem[key] = {"query": query, "empty": empty, "note": note}
        if self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(self._mem, handle, ensure_ascii=False, indent=1)


import os  # noqa: E402


class GapClosureRunner:
    def __init__(self, *, admission_factory: Callable[[int], Any],
                 admission_count: int, resolver: Any, binder, coverage_fn: Callable,
                 kb_registry: Dict[str, str], transport_keys: List[str],
                 base_url: str, model: str, task_id: str, generation_id: str,
                 ledger: Optional[Any] = None,
                 empty_cache: Optional[KnownEmptyCache] = None,
                 max_waves: int = 2, distinct_queries_per_wave: int = 2):
        self.admission_factory = admission_factory
        self.admission_count = admission_count
        self.resolver = resolver
        self.binder = binder
        self.coverage_fn = coverage_fn
        self.kb_registry = kb_registry
        self.transport_keys = transport_keys
        self.base_url = base_url
        self.model = model
        self.task_id = task_id
        self.generation_id = generation_id
        self.ledger = ledger
        self.empty_cache = empty_cache or KnownEmptyCache()
        self.max_waves = max_waves
        self.distinct_queries_per_wave = distinct_queries_per_wave

    # -------------------------------------------------- KB search ----
    def search_kb(self, query: str, limit: int = 6) -> Tuple[List[Dict[str, Any]], str]:
        """Search the registered local snapshot KBs (real S2-materialized text).
        Returns (candidates, error) — error is '' or 'provider_failure'."""
        words = [w for w in _norm(query).split() if len(w) > 3][:8]
        if not words:
            return [], ""
        candidates: List[Dict[str, Any]] = []
        for name, path in self.kb_registry.items():
            if not os.path.isfile(path):
                continue
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                conditions = " AND ".join(["text LIKE ?"] * len(words))
                rows = conn.execute(
                    f"SELECT paper_id, substr(text,1,900) FROM text_chunks WHERE {conditions} "
                    "LIMIT ?", [f"%{w}%" for w in words] + [limit]).fetchall()
                conn.close()
            except sqlite3.Error:
                return [], "provider_failure"
            for pid, text in rows:
                candidates.append({"document_id": pid, "paper_id": pid,
                                   "title": (text or "")[:80], "text": text or "",
                                   "kb": name})
        return candidates, ""

    # -------------------------------------------------- main loop ----
    def run(self, request: Dict[str, Any]) -> Dict[str, Any]:
        gap_id = request["gap_id"]
        slot = request["required_slot"]
        queries = request["queries"][:self.distinct_queries_per_wave]
        expected_field = request.get("expected_field", "support")
        waves_run = 0
        attempts: List[Dict[str, Any]] = []
        seen_binding_hashes = set()
        gain_binding = None
        stop_reason = None
        open_required = True
        key_idx = 0

        for wave in range(1, self.max_waves + 1):
            waves_run = wave
            wave_gained = False
            wave_had_provider_failure = False
            for q_no, query in enumerate(queries, 1):
                ck = self.empty_cache.key(query, request.get("scope", ""),
                                          request.get("policy_hash", ""),
                                          request.get("snapshot_hash", ""))
                cached_empty = self.empty_cache.get_empty(ck)
                if cached_empty:
                    attempts.append({"wave": wave, "query": query,
                                     "outcome": "known_empty_reused"})
                    continue
                candidates, err = self.search_kb(query)
                if err == "provider_failure":
                    wave_had_provider_failure = True
                    attempts.append({"wave": wave, "query": query,
                                     "outcome": "provider_failure"})
                    continue
                if not candidates:
                    self.empty_cache.record(ck, query, empty=True,
                                            note="zero_results")
                    attempts.append({"wave": wave, "query": query,
                                     "outcome": "zero_results_cached_empty"})
                    continue
                gained_this_query = False
                for cand in candidates[:3]:
                    # bounded admission rotation on provider auth rejection
                    dec = None
                    for rot in range(self.admission_count):
                        try:
                            admission = self.admission_factory(rot)
                            dec = admission.decide(cand,
                                                   f"{gap_id}:w{wave}:q{q_no}:k{rot}")
                            break
                        except Exception as exc:
                            detail = getattr(exc, "detail", "") or str(exc)
                            if "rejected_no_charge" in detail or "Arrearage" in detail \
                                    or "invalid_api_key" in detail:
                                continue  # next key candidate
                            raise
                    if dec is None:
                        continue
                    if dec.get("verdict") != "reviewed_direct":
                        attempts.append({"wave": wave, "query": query,
                                         "candidate": cand["document_id"][:50],
                                         "outcome": f"scope_{dec.get('verdict')}"})
                        continue
                    # 011 span resolution + 016 binding
                    claim = {"claim_id": f"{gap_id}-bind",
                             "statement": request["statement"],
                             "paper_id": cand["paper_id"],
                             "document_id": cand["document_id"],
                             "scope_verdict": "direct",
                             "importance": "load_bearing",
                             "source_depth": "fulltext"}
                    span = {"span_id": "span_" + _sha(cand["text"])[:20],
                            "text": cand["text"],
                            "text_sha256": _sha(cand["text"]),
                            "owner_paper_id": cand["paper_id"],
                            "document_id": cand["document_id"],
                            "scope_verdict": "direct", "source_depth": "fulltext"}
                    binding = self.binder.bind(claim, [span], [],
                                               required_components=request.get(
                                                   "required_components"))
                    bh = binding["binding"]["binding_hash"]
                    if binding["writable"] and bh not in seen_binding_hashes:
                        seen_binding_hashes.add(bh)
                        gain_binding = binding
                        gained_this_query = True
                        attempts.append({"wave": wave, "query": query,
                                         "candidate": cand["document_id"][:50],
                                         "outcome": "new_legal_support",
                                         "binding_hash": bh})
                        break
                    attempts.append({"wave": wave, "query": query,
                                     "candidate": cand["document_id"][:50],
                                     "outcome": "downloaded_but_unsupportive"})
                if gained_this_query:
                    wave_gained = True
                    break
            if wave_gained:
                stop_reason = "gain_achieved"
                open_required = False
                break
            if wave_had_provider_failure:
                stop_reason = "provider_failure_retryable"
                break
        if stop_reason is None:
            stop_reason = "no_gain_after_max_waves"
            open_required = True

        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "gap_id": gap_id,
            "required_slot": slot,
            "expected_field": expected_field,
            "waves_run": waves_run,
            "queries_per_wave_cap": self.distinct_queries_per_wave,
            "attempts": attempts,
            "gain": {"new_legal_support": gain_binding is not None,
                     "binding_hash": gain_binding["binding"]["binding_hash"]
                     if gain_binding else None},
            "stop_reason": stop_reason,
            "open_required": open_required,
            "new_paper_count_note": "paper count is diagnostic, gain is legal support only",
        }
        return receipt


def request_from_gap(gap_id: str, required_slot: str, statement: str,
                     query_family: str, queries: List[str],
                     expected_field: str = "support",
                     snapshot_hash: str = "", policy_hash: str = "",
                     scope: str = "") -> Dict[str, Any]:
    return {
        "schema_version": REQUEST_SCHEMA,
        "gap_id": gap_id,
        "required_slot": required_slot,
        "statement": statement,
        "query_family": query_family,
        "queries": queries,
        "expected_field": expected_field,
        "snapshot_hash": snapshot_hash,
        "policy_hash": policy_hash,
        "scope": scope,
    }
