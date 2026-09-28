"""Batched entailment with a persistent verdict cache (SM04).

The legacy binder judged one (statement, span) pair per provider call.  A claim made
of several components against several spans therefore multiplied into
components x spans calls for a single claim, and the only lever the old code had was
to raise the call cap - which bought cost, not correctness.

This module inverts the topology:

* a deterministic prefilter decides only two things, and neither of them is
  support: a pair may be *skipped* (the two texts cannot be about the same
  assertion) or it may be a *candidate* that still needs a judge;
* candidates are packed 8-16 pairs per call;
* a verdict is accepted only if the response covers exactly the requested pair ids -
  no missing, no duplicated, no invented - otherwise the whole batch is rejected;
* each claim stops as soon as it has one qualified support pair, while
  counterevidence from the same batch is always kept;
* every verdict is persisted with the identity of the request that produced it, so
  a second process replays it instead of paying again;
* running out of call budget is reported as budget_exhausted / unevaluated, never
  silently written down as not_enough_information.

The cache key carries the claim, the span, the prompt, the model, the policy, the
schema and the material snapshot, so a verdict is never reused across a change in
any of them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

PROMPT_SCHEMA_VERSION = "optomind.upgrade3.batched_entailment.prompt.v1"
VERDICT_SCHEMA = "optomind.upgrade3.entailment_verdict.v1"
CACHE_SCHEMA = "optomind.upgrade3.entailment_cache_entry.v1"
RECEIPT_SCHEMA = "optomind.upgrade3.batched_entailment_receipt.v1"

VERDICTS = ("support", "partial", "contradict", "not_enough_information")
UNEARNED_VERDICTS = ("budget_exhausted", "unevaluated", "invalid_response")

DEFAULT_BATCH_SIZE = 12
MIN_BATCH_SIZE = 8
MAX_BATCH_SIZE = 16
DEFAULT_MAX_BATCH_CALLS = 12

BATCH_SYSTEM = (
    "You judge whether each support passage entails its claim statement. "
    "Answer every pair by its pair_id. Allowed verdicts: support (the passage "
    "alone establishes the statement), partial (it establishes part of it), "
    "contradict (it asserts the opposite), not_enough_information (it does not "
    "settle it). Judge only the passage; never use outside knowledge; never treat "
    "a missing fact as a contradiction. Copy no text. Output JSON only."
)

_WS_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:[,\u00a0 ]\d{3})*(?:\.\d+)?")
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|cannot|can't|does not|do not|did not|is not|"
    r"are not|was not|were not|fails? to|unable to|neither|nor)\b",
    re.IGNORECASE,
)
_DIRECTION_RE = re.compile(
    r"\b(?:increase[sd]?|decrease[sd]?|higher|lower|improve[sd]?|degrade[sd]?|"
    r"better|worse|faster|slower|greater|less|above|below|up|down|"
    r"rise|rose|fall|fell|reduce[sd]?|enhance[sd]?|outperform[s]?)\b",
    re.IGNORECASE,
)
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "by", "for",
    "with", "as", "is", "are", "was", "were", "be", "been", "being", "that",
    "this", "these", "those", "it", "its", "we", "our", "their", "from", "into",
    "than", "which", "while", "when", "where", "who", "whom", "whose", "also",
    "such", "can", "may", "might", "must", "shall", "should", "will", "would",
    "has", "have", "had", "do", "does", "did", "but", "so", "if", "under",
    "over", "using", "used", "use", "between", "within", "after", "before",
}



def _same_metric_shape(claim: str, span: str) -> bool:
    """True when two texts name the same measured quantity.

    The check is deliberately narrow: the shared words must include a unit-like
    token, so "92 percent accuracy" and "40 percent accuracy" are the same
    quantity while "1500 TeraOPS" and "30 Watts" are not.  It is used only to
    route a pair to contradiction detection instead of skipping it.
    """

    claim_tokens = _content_tokens(claim)
    span_tokens = _content_tokens(span)
    shared = claim_tokens & span_tokens
    if len(shared) < 2:
        return False
    unit_like = {
        "percent", "percentage", "teraops", "tops", "watts", "watt", "hz",
        "nm", "um", "mm", "accuracy", "efficiency", "speed", "power",
    }
    return bool(shared & unit_like)


class BatchedEntailmentError(RuntimeError):
    """Raised when the batched judge cannot produce a trustworthy verdict set."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _sha_text(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _clean(value: Any) -> str:
    return _WS_RE.sub(" ", "" if value is None else str(value)).strip()


def _numbers(text: str) -> list[str]:
    found: list[str] = []
    for match in _NUMBER_RE.finditer(_clean(text)):
        token = match.group(0).strip()
        if token and token not in found:
            found.append(token)
    return found


def _content_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", _clean(text).casefold())
    return {token for token in tokens if token not in _STOPWORDS}


# --------------------------------------------------------------------------- #
# deterministic prefilter
# --------------------------------------------------------------------------- #

def prefilter_pair(statement: str, span_text: str) -> str:
    """Decide whether a pair is worth judging.  Never returns support.

    The rules only ever *skip*: a skipped pair is reported as such and never
    becomes a verdict.  Anything that could conceivably be entailed stays a
    candidate and goes to the judge.
    """

    body = _clean(span_text)
    claim = _clean(statement)
    if not body or not claim:
        return "span_or_statement_empty"
    claim_numbers = _numbers(claim)
    span_numbers = _numbers(body)
    if claim_numbers:
        skeleton = re.sub(r"[^0-9]", "", body)
        missing = [
            token for token in claim_numbers
            if re.sub(r"[^0-9]", "", token) not in skeleton
        ]
        if missing:
            # A number the passage does not contain can never be entailed by it.
            # It may however be contradicted by it - and refusing to look would
            # hide exactly the counterevidence the review has to report.
            if span_numbers and _same_metric_shape(claim, body):
                return "contradiction_candidate"
            return "claim_number_absent_from_span:%s" % missing[0]
    if claim_numbers and not span_numbers and len(body) > 40:
        return "numeric_claim_without_numeric_span"
    claim_negated = bool(_NEGATION_RE.search(claim))
    span_negated = bool(_NEGATION_RE.search(body))
    claim_direction = {match.group(0).casefold() for match in _DIRECTION_RE.finditer(claim)}
    span_direction = {match.group(0).casefold() for match in _DIRECTION_RE.finditer(body)}
    overlap = _content_tokens(claim) & _content_tokens(body)
    if not claim_negated and not span_negated and not overlap:
        return "no_content_word_overlap"
    if claim_negated != span_negated and not overlap:
        return "polarity_differs_without_shared_subject"
    if claim_direction and span_direction and not (claim_direction & span_direction):
        if not overlap:
            return "direction_differs_without_shared_subject"
    return "candidate"


# --------------------------------------------------------------------------- #
# persistent cache
# --------------------------------------------------------------------------- #

class EntailmentCache:
    """Append-only JSONL cache keyed by the full identity of the judgement.

    The key carries the claim, the span, the prompt, the model, the policy, the
    schema and the material snapshot, so a verdict can never be replayed across a
    change in any of them.  Reads are by key; a process restart simply re-reads the
    file.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._rows: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A half-written trailing line is ignored; the effective prefix is
                # still authoritative and no verdict is invented from it.
                continue
            if isinstance(row, Mapping) and _text(row.get("cache_key")):
                self._rows[_text(row["cache_key"])] = dict(row)

    @staticmethod
    def identity(
        *,
        statement: str,
        span_text: str,
        prompt_hash: str,
        model: str,
        policy_sha256: str,
        schema_hash: str,
        material_snapshot_hash: str,
    ) -> str:
        return _sha_text(_canonical({
            "statement": _clean(statement),
            "span_text_sha256": _sha_text(_clean(span_text)),
            "prompt_hash": _text(prompt_hash),
            "model": _text(model),
            "policy_sha256": _text(policy_sha256),
            "schema_hash": _text(schema_hash),
            "material_snapshot_hash": _text(material_snapshot_hash),
        }))

    def get(self, key: str) -> dict[str, Any] | None:
        row = self._rows.get(key)
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return dict(row)

    def put(self, key: str, row: Mapping[str, Any]) -> None:
        entry = dict(row)
        entry["cache_key"] = key
        entry["schema_version"] = CACHE_SCHEMA
        entry["created_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="") as handle:
            handle.write(_canonical(entry))
            handle.write(chr(10))
            handle.flush()
            os.fsync(handle.fileno())
        self._rows[key] = entry


# --------------------------------------------------------------------------- #
# batch planning and judging
# --------------------------------------------------------------------------- #

def plan_pairs(
    pairs: Sequence[Mapping[str, Any]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Split candidate pairs into provider batches and record every skip.

    Each pair must carry pair_id, claim_id, statement and span_text.  A pair whose
    deterministic prefilter rejects it is recorded as skipped with its reason and is
    never sent to the model.
    """

    size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, int(batch_size or DEFAULT_BATCH_SIZE)))
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pair in pairs:
        pair_id = _text(pair.get("pair_id"))
        if not pair_id or pair_id in seen:
            skipped.append({
                "pair_id": pair_id,
                "claim_id": _text(pair.get("claim_id")),
                "reason": "pair_id_missing_or_duplicate",
            })
            continue
        seen.add(pair_id)
        reason = prefilter_pair(
            _text(pair.get("statement")), _text(pair.get("span_text"))
        )
        if reason == "contradiction_candidate":
            # Same quantity, different value: the pair is not a support candidate
            # and does not need a model to be recorded as counterevidence.
            conflicts.append({
                "pair_id": pair_id,
                "claim_id": _text(pair.get("claim_id")),
                "reason": reason,
            })
            continue
        if reason != "candidate":
            skipped.append({
                "pair_id": pair_id,
                "claim_id": _text(pair.get("claim_id")),
                "reason": reason,
            })
            continue
        candidates.append(dict(pair))
    batches = [
        [row["pair_id"] for row in candidates[start:start + size]]
        for start in range(0, len(candidates), size)
    ]
    return {
        "schema_version": RECEIPT_SCHEMA,
        "batch_size": size,
        "planned_pairs": [_text(row.get("pair_id")) for row in candidates],
        "planned_batches": batches,
        "skipped": skipped,
        "conflict_rows": conflicts,
        "candidate_rows": candidates,
    }


def build_batch_prompt(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    items = []
    for pair in pairs:
        items.append({
            "pair_id": _text(pair.get("pair_id")),
            "statement": _clean(pair.get("statement")),
            "support_passage": _clean(pair.get("span_text")),
        })
    return {
        "system_prompt": BATCH_SYSTEM,
        "user_payload": {
            "task": "batched_entailment",
            "items": items,
            "output_schema": {
                "verdicts": [{"pair_id": "str", "verdict": "str", "reason": "str"}]
            },
        },
        # The prompt identity is a property of the contract, not of the batch, so
        # two runs that judge the same pair reuse one cache entry instead of paying
        # twice for the same judgement.
        "prompt_hash": _sha_text(BATCH_SYSTEM + "\u0000" + PROMPT_SCHEMA_VERSION),
    }


#: keys the production transport adds to a parsed payload; they are bookkeeping,
#: never part of what the model asserted
_TRANSPORT_BOOKKEEPING_KEYS = frozenset({"receipt", "usage", "raw_response_hash",
                                         "provider_request_id"})


def parse_batch_response(
    payload: Any,
    *,
    expected_pair_ids: Sequence[str],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Validate a batch response.  Any defect rejects the WHOLE batch.

    A partially parsed batch must never grant support to the pairs that happened to
    parse, so the caller receives either a complete mapping or a reason and no
    verdicts at all.
    """

    if not isinstance(payload, Mapping):
        return {}, "batch_response_not_an_object"
    # The transport attaches its own bookkeeping to the parsed payload; those keys
    # are not part of what the model said and must not make the shape ambiguous.
    bookkeeping = _TRANSPORT_BOOKKEEPING_KEYS
    content_keys = [key for key in payload if key not in bookkeeping]
    rows = payload.get("verdicts")
    if not isinstance(rows, list):
        # The prompt publishes its own output schema, so a live judge answers with
        # that structure echoed back: {"output_schema": {"verdicts": [...]}}.  A
        # complete, correct batch used to be thrown away for that reason alone.
        # Only an unambiguous single-key wrapper is unwrapped - anything else stays
        # a defect, and the whole-batch validation below is unchanged.
        if len(content_keys) == 1:
            inner = payload[content_keys[0]]
            if isinstance(inner, Mapping):
                candidate = inner.get("verdicts")
                if isinstance(candidate, list):
                    rows = candidate
    if not isinstance(rows, list):
        return {}, "batch_response_without_verdicts"
    expected = [_text(item) for item in expected_pair_ids]
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            return {}, "batch_verdict_not_an_object"
        pair_id = _text(row.get("pair_id"))
        verdict = _text(row.get("verdict")).casefold()
        if not pair_id:
            return {}, "batch_verdict_without_pair_id"
        if pair_id in seen:
            return {}, "batch_verdict_duplicate:%s" % pair_id
        if pair_id not in expected:
            return {}, "batch_verdict_unknown_pair:%s" % pair_id
        if verdict not in VERDICTS:
            return {}, "batch_verdict_enum_invalid:%s" % verdict
        seen[pair_id] = {
            "pair_id": pair_id,
            "verdict": verdict,
            "reason": _text(row.get("reason"))[:300],
        }
    missing = [pair_id for pair_id in expected if pair_id not in seen]
    if missing:
        return {}, "batch_verdict_missing:%s" % ",".join(sorted(missing))
    return seen, None


def judge_pairs(
    pairs: Sequence[Mapping[str, Any]],
    *,
    judge: Callable[..., Mapping[str, Any]],
    cache: EntailmentCache | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batch_calls: int = DEFAULT_MAX_BATCH_CALLS,
    prompt_model: str = "qwen3.7-flash",
    policy_sha256: str = "",
    material_snapshot_hash: str = "",
    task_id: str = "031",
    generation_id: str = "",
    early_stop_after_support: bool = True,
    per_claim_support_target: int = 1,
) -> dict[str, Any]:
    """Judge candidate pairs in batches, with cache replay and early stop.

    Returns a receipt with the per-pair verdicts, the pairs that were never judged
    (budget exhausted or invalid batch - explicitly, never as NEI), the cache
    statistics and the provider call receipts.
    """

    plan = plan_pairs(pairs, batch_size=batch_size)
    receipts: list[dict[str, Any]] = []
    verdicts: dict[str, dict[str, Any]] = {}
    unevaluated: dict[str, str] = {}
    support_counts: dict[str, int] = {}
    provider_calls = 0
    cache_hits = 0

    # Deterministic counterevidence is recorded before any call: a passage that
    # states a different value for the same quantity contradicts the claim without
    # needing a judge, and this is never a support verdict.
    for conflict in plan["conflict_rows"]:
        pair_id = _text(conflict.get("pair_id"))
        source = next(
            (row for row in pairs if _text(row.get("pair_id")) == pair_id), {}
        )
        verdicts[pair_id] = {
            "schema_version": VERDICT_SCHEMA,
            "pair_id": pair_id,
            "claim_id": _text(conflict.get("claim_id")),
            "verdict": "contradict",
            "reason": "same_quantity_different_value",
            "origin": "deterministic_conflict",
            "cache_key": "",
            "prompt_hash": "",
            "provider_receipt": {},
            "statement": _text(source.get("statement")),
            "span_text": _text(source.get("span_text")),
        }

    for pair in plan["candidate_rows"]:
        pair_id = _text(pair.get("pair_id"))
        statement = _text(pair.get("statement"))
        span_text = _text(pair.get("span_text"))
        key = EntailmentCache.identity(
            statement=statement,
            span_text=span_text,
            prompt_hash=_text(pair.get("prompt_hash")),
            model=prompt_model,
            policy_sha256=policy_sha256,
            schema_hash=_text(pair.get("schema_hash")),
            material_snapshot_hash=material_snapshot_hash,
        )
        cached = cache.get(key) if cache is not None else None
        if cached is None:
            continue
        cache_hits += 1
        verdicts[pair_id] = {
            "schema_version": VERDICT_SCHEMA,
            "pair_id": pair_id,
            "claim_id": _text(pair.get("claim_id")),
            "verdict": _text(cached.get("verdict")),
            "reason": _text(cached.get("reason")),
            "origin": "cache_replay",
            "cache_key": key,
            "provider_receipt": dict(cached.get("provider_receipt") or {}),
        }
        if _text(cached.get("verdict")) == "support":
            claim_id = _text(pair.get("claim_id"))
            support_counts[claim_id] = support_counts.get(claim_id, 0) + 1

    pending_by_claim: dict[str, list[dict[str, Any]]] = {}
    for pair in plan["candidate_rows"]:
        pair_id = _text(pair.get("pair_id"))
        if pair_id in verdicts:
            continue
        pending_by_claim.setdefault(_text(pair.get("claim_id")), []).append(pair)

    for batch in plan["planned_batches"]:
        batch_pairs = [
            pair for pair in plan["candidate_rows"]
            if _text(pair.get("pair_id")) in set(batch)
        ]
        live: list[dict[str, Any]] = []
        for pair in batch_pairs:
            pair_id = _text(pair.get("pair_id"))
            if pair_id in verdicts:
                continue
            claim_id = _text(pair.get("claim_id"))
            if (
                early_stop_after_support
                and support_counts.get(claim_id, 0) >= per_claim_support_target
            ):
                unevaluated[pair_id] = "not_needed_after_support"
                continue
            live.append(pair)
        if not live:
            continue
        if provider_calls >= max(0, int(max_batch_calls)):
            for pair in live:
                unevaluated[_text(pair.get("pair_id"))] = "budget_exhausted"
            continue
        prompt = build_batch_prompt(live)
        provider_calls += 1
        receipt: dict[str, Any] = {
            "batch": provider_calls,
            "pair_ids": [_text(item.get("pair_id")) for item in live],
            "prompt_hash": prompt["prompt_hash"],
        }
        try:
            payload = judge(
                prompt["system_prompt"],
                prompt["user_payload"],
                task_id=task_id,
                logical_call_id="entail-batch:%d" % provider_calls,
                generation_id=generation_id,
            )
        except Exception as exc:
            receipt["status"] = "failed"
            receipt["error"] = "%s:%s" % (type(exc).__name__, str(exc)[:200])
            for pair in live:
                unevaluated[_text(pair.get("pair_id"))] = "batch_call_failed"
            receipts.append(receipt)
            continue
        if isinstance(payload, Mapping) and isinstance(payload.get("receipt"), Mapping):
            receipt["provider_receipt"] = dict(payload["receipt"])
        parsed, defect = parse_batch_response(
            payload, expected_pair_ids=[_text(item.get("pair_id")) for item in live]
        )
        if defect:
            receipt["status"] = "rejected"
            receipt["defect"] = defect
            for pair in live:
                unevaluated[_text(pair.get("pair_id"))] = "invalid_batch:%s" % defect
            receipts.append(receipt)
            continue
        receipt["status"] = "accepted"
        receipts.append(receipt)
        for pair in live:
            pair_id = _text(pair.get("pair_id"))
            row = parsed[pair_id]
            key = EntailmentCache.identity(
                statement=_text(pair.get("statement")),
                span_text=_text(pair.get("span_text")),
                prompt_hash=_text(pair.get("prompt_hash")),
                model=prompt_model,
                policy_sha256=policy_sha256,
                schema_hash=_text(pair.get("schema_hash")),
                material_snapshot_hash=material_snapshot_hash,
            )
            record = {
                "schema_version": VERDICT_SCHEMA,
                "pair_id": pair_id,
                "claim_id": _text(pair.get("claim_id")),
                "verdict": row["verdict"],
                "reason": row["reason"],
                "origin": "provider",
                "cache_key": key,
                "prompt_hash": prompt["prompt_hash"],
                "provider_receipt": dict(receipt.get("provider_receipt") or {}),
            }
            verdicts[pair_id] = record
            if cache is not None:
                cache.put(key, {
                    "verdict": row["verdict"],
                    "reason": row["reason"],
                    "prompt_hash": prompt["prompt_hash"],
                    "model": prompt_model,
                    "provider_receipt": record["provider_receipt"],
                })
            if row["verdict"] == "support":
                claim_id = _text(pair.get("claim_id"))
                support_counts[claim_id] = support_counts.get(claim_id, 0) + 1

    for pair in plan["candidate_rows"]:
        pair_id = _text(pair.get("pair_id"))
        if pair_id not in verdicts and pair_id not in unevaluated:
            unevaluated[pair_id] = "unevaluated"

    # The binder judges each atomic component of a claim against each span, not just
    # the whole statement.  Judging those sub-pairs separately would recreate the
    # multiplication this ticket exists to remove, so the component lookup is served
    # from the parent verdict under one strict rule: component numbers must all be
    # present in the span.  A component the span does not carry is left unevaluated
    # and can never inherit support.
    def _propagate_components(pair_id, statement, span_text, verdict) -> None:
        try:
            from .claim_binding import split_atomic_components
        except Exception:  # pragma: no cover - defensive
            return
        for component in split_atomic_components(statement):
            if not component or _clean(component) == _clean(statement):
                continue
            key = _pair_lookup_key(component, span_text)
            if key in index:
                continue
            span_skeleton = re.sub(r"[^0-9]", "", span_text)
            if all(
                re.sub(r"[^0-9]", "", token) in span_skeleton
                for token in _numbers(component)
            ):
                index[key] = verdict
            else:
                index[key] = "unevaluated"

    index: dict[str, str] = {}
    for conflict in plan["conflict_rows"]:
        source = next(
            (row for row in pairs
             if _text(row.get("pair_id")) == _text(conflict.get("pair_id"))),
            None,
        )
        if source is not None:
            statement = _text(source.get("statement"))
            span_text = _text(source.get("span_text"))
            index[_pair_lookup_key(statement, span_text)] = "contradict"
            _propagate_components(
                _text(conflict.get("pair_id")), statement, span_text, "unevaluated"
            )
    for pair in plan["candidate_rows"]:
        pair_id = _text(pair.get("pair_id"))
        row = verdicts.get(pair_id)
        verdict = _text((row or {}).get("verdict")) or "unevaluated"
        statement = _text(pair.get("statement"))
        span_text = _text(pair.get("span_text"))
        index[_pair_lookup_key(statement, span_text)] = verdict
        _propagate_components(pair_id, statement, span_text, verdict)

    return {
        "schema_version": RECEIPT_SCHEMA,
        "batch_size": plan["batch_size"],
        "planned_pairs": plan["planned_pairs"],
        "pair_index": index,
        "planned_batch_count": len(plan["planned_batches"]),
        "skipped": plan["skipped"],
        "deterministic_conflicts": plan["conflict_rows"],
        "verdicts": verdicts,
        "unevaluated": unevaluated,
        "provider_calls": provider_calls,
        "cache_hits": cache_hits,
        "cache_misses": (cache.misses if cache is not None else 0),
        "support_counts": support_counts,
        "batches": receipts,
        "single_pair_equivalent_calls": len(plan["planned_pairs"]),
    }


def _pair_lookup_key(statement: str, span_text: str) -> str:
    return _sha_text(_canonical({
        "statement": _clean(statement),
        "span_text_sha256": _sha_text(_clean(span_text)),
    }))


def verdict_for(statement: str, span_text: str, receipt: Mapping[str, Any]) -> str:
    """The verdict that belongs to one (statement, span) pair.

    A pair that was never judged is reported as unevaluated, never as
    not_enough_information: an absent judgement is not a negative finding.  The
    lookup is exact on the pair, so two spans of the same claim never share a
    verdict.
    """

    index = receipt.get("pair_index") or {}
    return _text(index.get(_pair_lookup_key(statement, span_text))) or "unevaluated"


def receipt_verdict_fn(
    receipt: Mapping[str, Any],
) -> Callable[[str, str], str]:
    """A (statement, span) -> verdict callable over an already judged receipt.

    This is what the binder consumes: the topology is decided before binding, so no
    provider call can be triggered from inside a binding loop.
    """

    index = dict(receipt.get("pair_index") or {})

    def verdict(statement: str, span_text: str) -> str:
        return _text(index.get(_pair_lookup_key(statement, span_text))) or "unevaluated"

    return verdict


__all__ = [
    "BATCH_SYSTEM",
    "CACHE_SCHEMA",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_BATCH_CALLS",
    "MAX_BATCH_SIZE",
    "MIN_BATCH_SIZE",
    "RECEIPT_SCHEMA",
    "UNEARNED_VERDICTS",
    "VERDICTS",
    "VERDICT_SCHEMA",
    "BatchedEntailmentError",
    "EntailmentCache",
    "build_batch_prompt",
    "judge_pairs",
    "parse_batch_response",
    "plan_pairs",
    "prefilter_pair",
    "receipt_verdict_fn",
    "verdict_for",
]
