"""Two-stage deterministic query deduplication for supplementary retrieval.

Stage 1 is fully offline and deterministic: normalized exact hashes plus
conservative lexical/containment similarity against historical, queued, and
batch queries.  Only obvious duplicates are auto-rejected; ambiguous queries
are grouped for stage 2.

Stage 2 accepts an injected batch adjudicator callback (the future
qwen3.7-flash route).  The callback receives only ambiguous groups and is
called at most once per dedup run.  It may keep, reject, or merge queries while
preserving source task IDs and reasons.  When no adjudicator is provided,
ambiguous queries are conservatively kept and flagged
``needs_semantic_review`` -- never silently rejected.

No network calls, model calls, or credentials are accessed here.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence


DEDUP_SCHEMA_VERSION = "supplementary_retrieval.query_dedup.v1"

DECISION_UNIQUE = "unique"
DECISION_DUPLICATE = "duplicate"
DECISION_AMBIGUOUS = "ambiguous"
DECISION_SAME_TASK_REPLAY = "same_task_replay"

ACTION_KEEP = "keep"
ACTION_REJECT = "reject"
ACTION_MERGE = "merge"

SOURCE_HISTORICAL = "historical"
SOURCE_QUEUED = "queued"
SOURCE_BATCH = "batch"

# Conservative thresholds.  Stage 1 rejects only near-identical queries; all
# other overlap is deferred to semantic adjudication.
DEFAULT_THRESHOLDS = {
    "obvious_jaccard": 0.9,
    "obvious_containment": 0.9,
    "ambiguous_jaccard": 0.35,
    "ambiguous_containment": 0.6,
    "min_shared_tokens": 2,
}

_STOPWORDS = frozenset(
    {
        "about", "after", "also", "among", "and", "are", "based", "between",
        "both", "but", "can", "for", "from", "have", "how", "into", "its",
        "more", "paper", "review", "should", "study", "than", "that", "the",
        "their", "these", "this", "through", "using", "what", "when", "where",
        "which", "with",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+./_\-]{1,}")


def normalize_query(text: Any) -> str:
    """Return a conservative normalized query string for hashing."""

    value = unicodedata.normalize("NFKC", str(text or ""))
    return " ".join(value.casefold().split()).strip(" .,;:!?")


def query_hash(text: Any) -> str:
    """Return the normalized exact hash used by stage 1."""

    raw = normalize_query(text).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def query_tokens(text: Any) -> frozenset[str]:
    """Tokenize a query for conservative lexical/containment similarity."""

    return frozenset(
        token.casefold()
        for token in _TOKEN_RE.findall(str(text or ""))
        if token.casefold() not in _STOPWORDS
    )


def token_similarity(
    left_tokens: frozenset[str],
    right_tokens: frozenset[str],
) -> dict[str, float] | None:
    """Compute jaccard and containment similarity; None when either side is empty."""

    if not left_tokens or not right_tokens:
        return None
    shared = left_tokens & right_tokens
    jaccard = len(shared) / len(left_tokens | right_tokens)
    return {
        "shared_tokens": len(shared),
        "jaccard": jaccard,
        "containment_left": len(shared) / len(left_tokens),
        "containment_right": len(shared) / len(right_tokens),
    }


@dataclass(frozen=True, slots=True)
class QueryCandidate:
    """A query submitted for deduplication."""

    query_id: str
    text: str
    source_task_id: str
    batch_id: str = ""


@dataclass(frozen=True, slots=True)
class KnownQuery:
    """A previously seen query from history, the queue, or the same batch."""

    query_id: str
    text: str
    source_task_id: str
    source: str


@dataclass(frozen=True, slots=True)
class Similarity:
    """Lexical similarity metrics between a candidate and a known query."""

    shared_tokens: int
    jaccard: float
    containment_left: float
    containment_right: float


@dataclass(frozen=True, slots=True)
class MatchRef:
    """A reference to the known query a candidate collided with."""

    query_id: str
    source_task_id: str
    source: str
    similarity: Similarity | None = None


@dataclass(frozen=True, slots=True)
class QueryDecision:
    """Stage-1 or adjudicated decision for one candidate query."""

    query_id: str
    decision: str
    reasons: tuple[str, ...]
    matched_refs: tuple[MatchRef, ...] = ()
    needs_semantic_review: bool = False
    source_task_id: str = ""
    merged_into_query_id: str = ""
    preserved_task_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "matched_refs": [
                {
                    "query_id": ref.query_id,
                    "source_task_id": ref.source_task_id,
                    "source": ref.source,
                    "similarity": (
                        {
                            "shared_tokens": ref.similarity.shared_tokens,
                            "jaccard": round(ref.similarity.jaccard, 6),
                            "containment_left": round(ref.similarity.containment_left, 6),
                            "containment_right": round(ref.similarity.containment_right, 6),
                        }
                        if ref.similarity is not None
                        else None
                    ),
                }
                for ref in self.matched_refs
            ],
            "needs_semantic_review": self.needs_semantic_review,
            "source_task_id": self.source_task_id,
            "merged_into_query_id": self.merged_into_query_id,
            "preserved_task_ids": list(self.preserved_task_ids),
        }


@dataclass(frozen=True, slots=True)
class AmbiguousGroup:
    """One batch for the stage-2 semantic adjudicator."""

    group_id: str
    queries: tuple[QueryCandidate, ...]
    refs: tuple[MatchRef, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "reason": self.reason,
            "queries": [
                {
                    "query_id": q.query_id,
                    "text": q.text,
                    "source_task_id": q.source_task_id,
                    "batch_id": q.batch_id,
                }
                for q in self.queries
            ],
            "refs": [
                {
                    "query_id": ref.query_id,
                    "text_hint": "",
                    "source_task_id": ref.source_task_id,
                    "source": ref.source,
                }
                for ref in self.refs
            ],
        }


@dataclass(frozen=True, slots=True)
class Stage1Result:
    """Stage-1 decisions plus ambiguous groups for stage 2."""

    decisions: tuple[QueryDecision, ...]
    ambiguous_groups: tuple[AmbiguousGroup, ...]
    stats: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class AdjudicationDecision:
    """A validated decision produced by the batch adjudicator."""

    query_id: str
    action: str
    reason: str
    merged_into_query_id: str = ""
    needs_semantic_review: bool = False


@dataclass(frozen=True, slots=True)
class DedupOutcome:
    """Final per-query decisions after stage 1 and (optionally) stage 2."""

    decisions: tuple[QueryDecision, ...]
    kept_queries: tuple[QueryDecision, ...]
    rejected_queries: tuple[QueryDecision, ...]
    merged_queries: tuple[QueryDecision, ...] = ()
    adjudicator_calls: int = 0
    adjudicator_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DEDUP_SCHEMA_VERSION,
            "adjudicator_calls": self.adjudicator_calls,
            "adjudicator_error": self.adjudicator_error,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "kept_queries": [decision.to_dict() for decision in self.kept_queries],
            "rejected_queries": [decision.to_dict() for decision in self.rejected_queries],
            "merged_queries": [decision.to_dict() for decision in self.merged_queries],
        }


def _obvious_duplicate(
    sim: Similarity,
    thresholds: Mapping[str, Any],
) -> bool:
    min_shared = max(0, int(thresholds.get("min_shared_tokens", 2)))
    if sim.jaccard >= float(thresholds.get("obvious_jaccard", 0.9)):
        return True
    containment = float(thresholds.get("obvious_containment", 0.9))
    return (
        sim.shared_tokens >= min_shared
        and sim.containment_left >= containment
        and sim.containment_right >= containment
    )


def _ambiguous(
    sim: Similarity,
    thresholds: Mapping[str, Any],
) -> bool:
    min_shared = max(0, int(thresholds.get("min_shared_tokens", 2)))
    if sim.shared_tokens < min_shared:
        return False
    jaccard = float(thresholds.get("ambiguous_jaccard", 0.35))
    containment = float(thresholds.get("ambiguous_containment", 0.6))
    return sim.jaccard >= jaccard or max(
        sim.containment_left, sim.containment_right
    ) >= containment


def _to_similarity(raw: Mapping[str, Any] | None) -> Similarity | None:
    if raw is None:
        return None
    return Similarity(
        shared_tokens=int(raw.get("shared_tokens") or 0),
        jaccard=float(raw.get("jaccard") or 0.0),
        containment_left=float(raw.get("containment_left") or 0.0),
        containment_right=float(raw.get("containment_right") or 0.0),
    )


def stage1_deduplicate(
    candidates: Iterable[QueryCandidate],
    known_queries: Iterable[KnownQuery],
    *,
    thresholds: Mapping[str, Any] | None = None,
) -> Stage1Result:
    """Run deterministic stage-1 dedup; ambiguous queries become groups."""

    merged_thresholds = dict(DEFAULT_THRESHOLDS)
    if isinstance(thresholds, Mapping):
        merged_thresholds.update({k: v for k, v in thresholds.items() if v is not None})
    candidates = list(candidates)
    known = list(known_queries)
    candidate_index = {
        candidate.query_id: index for index, candidate in enumerate(candidates)
    }
    known_by_hash: dict[str, list[KnownQuery]] = {}
    for known_query in known:
        known_by_hash.setdefault(query_hash(known_query.text), []).append(known_query)

    decisions: list[QueryDecision] = []
    ambiguous_entries: list[tuple[QueryCandidate, list[MatchRef]]] = []

    for candidate in candidates:
        reasons: list[str] = []
        matched: list[MatchRef] = []
        exact_refs = [
            item
            for item in known_by_hash.get(query_hash(candidate.text), [])
            if item.query_id != candidate.query_id
        ]
        cross_historical = [
            item
            for item in exact_refs
            if item.source_task_id != candidate.source_task_id
            and item.source != SOURCE_BATCH
        ]
        batch_refs = [item for item in exact_refs if item.source == SOURCE_BATCH]
        same_task_other = [
            item
            for item in exact_refs
            if item.source_task_id == candidate.source_task_id
            and item.source != SOURCE_BATCH
        ]
        my_index = candidate_index.get(candidate.query_id, len(candidates))
        earlier_batch_refs = [
            item
            for item in batch_refs
            if candidate_index.get(item.query_id, len(candidates)) < my_index
        ]
        if cross_historical:
            matched.extend(
                MatchRef(item.query_id, item.source_task_id, item.source, None)
                for item in cross_historical
            )
            decisions.append(
                QueryDecision(
                    query_id=candidate.query_id,
                    decision=DECISION_DUPLICATE,
                    reasons=("exact_normalized_duplicate",),
                    matched_refs=tuple(matched),
                    source_task_id=candidate.source_task_id,
                )
            )
            continue
        if earlier_batch_refs:
            decisions.append(
                QueryDecision(
                    query_id=candidate.query_id,
                    decision=DECISION_DUPLICATE,
                    reasons=("same_batch_normalized_duplicate",),
                    matched_refs=tuple(
                        MatchRef(item.query_id, item.source_task_id, item.source, None)
                        for item in earlier_batch_refs
                    ),
                    source_task_id=candidate.source_task_id,
                )
            )
            continue
        if same_task_other:
            decisions.append(
                QueryDecision(
                    query_id=candidate.query_id,
                    decision=DECISION_SAME_TASK_REPLAY,
                    reasons=("same_task_exact_replay",),
                    matched_refs=tuple(
                        MatchRef(item.query_id, item.source_task_id, item.source, None)
                        for item in same_task_other
                    ),
                    source_task_id=candidate.source_task_id,
                )
            )
            continue
        if batch_refs:
            decisions.append(
                QueryDecision(
                    query_id=candidate.query_id,
                    decision=DECISION_UNIQUE,
                    reasons=("first_normalized_variant",),
                    source_task_id=candidate.source_task_id,
                )
            )
            continue

        candidate_tokens = query_tokens(candidate.text)
        for known_query in known:
            if known_query.query_id == candidate.query_id:
                continue
            raw = token_similarity(candidate_tokens, query_tokens(known_query.text))
            if raw is None:
                continue
            sim = Similarity(
                shared_tokens=int(raw["shared_tokens"]),
                jaccard=float(raw["jaccard"]),
                containment_left=float(raw["containment_left"]),
                containment_right=float(raw["containment_right"]),
            )
            if _obvious_duplicate(sim, merged_thresholds):
                matched.append(
                    MatchRef(known_query.query_id, known_query.source_task_id, known_query.source, sim)
                )
        if matched:
            decisions.append(
                QueryDecision(
                    query_id=candidate.query_id,
                    decision=DECISION_DUPLICATE,
                    reasons=("lexical_obvious_duplicate",),
                    matched_refs=tuple(matched),
                    source_task_id=candidate.source_task_id,
                )
            )
            continue

        ambiguous_refs: list[MatchRef] = []
        for known_query in known:
            if known_query.query_id == candidate.query_id:
                continue
            raw = token_similarity(candidate_tokens, query_tokens(known_query.text))
            if raw is None:
                continue
            sim = Similarity(
                shared_tokens=int(raw["shared_tokens"]),
                jaccard=float(raw["jaccard"]),
                containment_left=float(raw["containment_left"]),
                containment_right=float(raw["containment_right"]),
            )
            if _ambiguous(sim, merged_thresholds):
                ambiguous_refs.append(
                    MatchRef(known_query.query_id, known_query.source_task_id, known_query.source, sim)
                )
        if ambiguous_refs:
            decisions.append(
                QueryDecision(
                    query_id=candidate.query_id,
                    decision=DECISION_AMBIGUOUS,
                    reasons=("needs_semantic_adjudication",),
                    matched_refs=tuple(ambiguous_refs),
                    source_task_id=candidate.source_task_id,
                )
            )
            ambiguous_entries.append((candidate, ambiguous_refs))
        else:
            decisions.append(
                QueryDecision(
                    query_id=candidate.query_id,
                    decision=DECISION_UNIQUE,
                    reasons=("no_duplicate_found",),
                    source_task_id=candidate.source_task_id,
                )
            )

    groups = _build_ambiguous_groups(ambiguous_entries)
    stats = {
        "candidates": len(candidates),
        "known_queries": len(known),
        "unique": sum(1 for d in decisions if d.decision == DECISION_UNIQUE),
        "duplicate": sum(1 for d in decisions if d.decision == DECISION_DUPLICATE),
        "ambiguous": sum(1 for d in decisions if d.decision == DECISION_AMBIGUOUS),
        "same_task_replay": sum(1 for d in decisions if d.decision == DECISION_SAME_TASK_REPLAY),
        "ambiguous_groups": len(groups),
    }
    return Stage1Result(
        decisions=tuple(decisions),
        ambiguous_groups=groups,
        stats=stats,
    )


def _build_ambiguous_groups(
    entries: Sequence[tuple[QueryCandidate, list[MatchRef]]],
) -> tuple[AmbiguousGroup, ...]:
    """Group ambiguous candidates by their matched-ref set (one batch per set)."""

    grouped: dict[tuple[str, ...], list[tuple[QueryCandidate, list[MatchRef]]]] = {}
    for candidate, refs in entries:
        key = tuple(sorted(ref.query_id for ref in refs))
        grouped.setdefault(key, []).append((candidate, refs))
    groups: list[AmbiguousGroup] = []
    for index, (_, entries_for_key) in enumerate(sorted(grouped.items()), start=1):
        queries = tuple(candidate for candidate, _ in entries_for_key)
        all_refs: list[MatchRef] = []
        seen_ref_ids: set[str] = set()
        for _, refs in entries_for_key:
            for ref in refs:
                if ref.query_id not in seen_ref_ids:
                    seen_ref_ids.add(ref.query_id)
                    all_refs.append(ref)
        groups.append(
            AmbiguousGroup(
                group_id=f"ambiguous_group:{index:03d}",
                queries=queries,
                refs=tuple(all_refs),
                reason="lexical_overlap_below_obvious_duplicate_threshold",
            )
        )
    return tuple(groups)


BatchAdjudicator = Callable[
    [Sequence[AmbiguousGroup]],
    Any,
]


def _validate_adjudicator_output(
    raw: Any,
    groups: Sequence[AmbiguousGroup],
) -> tuple[list[AdjudicationDecision], list[str]]:
    """Normalize and validate adjudicator output; returns (decisions, errors)."""

    errors: list[str] = []
    if isinstance(raw, AdjudicationDecision):
        raw = [raw]
    if isinstance(raw, Mapping) and isinstance(raw.get("decisions"), list):
        raw = raw.get("decisions")
    if not isinstance(raw, (list, tuple)):
        return [], ["adjudicator_output_must_be_list_or_decisions_mapping"]
    allowed_queries = {
        candidate.query_id
        for group in groups
        for candidate in group.queries
    }
    allowed_refs = {
        ref.query_id
        for group in groups
        for ref in group.refs
    }
    seen: set[str] = set()
    decisions: list[AdjudicationDecision] = []
    for item in raw:
        if isinstance(item, AdjudicationDecision):
            mapping = {
                "query_id": item.query_id,
                "action": item.action,
                "reason": item.reason,
                "merged_into_query_id": item.merged_into_query_id,
                "needs_semantic_review": item.needs_semantic_review,
            }
        elif isinstance(item, Mapping):
            mapping = dict(item)
        else:
            errors.append("adjudicator_decision_must_be_mapping")
            continue
        query_id = str(mapping.get("query_id") or "")
        action = str(mapping.get("action") or "").strip()
        reason = str(mapping.get("reason") or "").strip()
        if not query_id:
            errors.append("adjudicator_decision_missing_query_id")
            continue
        if query_id in seen:
            errors.append(f"adjudicator_duplicate_query_id:{query_id}")
            continue
        if query_id not in allowed_queries:
            errors.append(f"adjudicator_unknown_query_id:{query_id}")
            continue
        if action not in {ACTION_KEEP, ACTION_REJECT, ACTION_MERGE}:
            errors.append(f"adjudicator_invalid_action:{action}")
            continue
        if not reason:
            errors.append(f"adjudicator_missing_reason:{query_id}")
            continue
        merged_into = str(mapping.get("merged_into_query_id") or "")
        if action == ACTION_MERGE and (
            not merged_into or merged_into not in allowed_queries | allowed_refs
        ):
            errors.append(f"adjudicator_merge_target_invalid:{query_id}")
            continue
        seen.add(query_id)
        decisions.append(
            AdjudicationDecision(
                query_id=query_id,
                action=action,
                reason=reason,
                merged_into_query_id=merged_into,
                needs_semantic_review=bool(mapping.get("needs_semantic_review")),
            )
        )
    missing = sorted(allowed_queries - seen)
    if missing:
        errors.append("adjudicator_missing_decisions:" + ",".join(missing))
    return decisions, errors


def adjudicate_groups(
    groups: Sequence[AmbiguousGroup],
    adjudicator: BatchAdjudicator | None = None,
) -> tuple[QueryDecision, ...]:
    """Run stage-2 batch adjudication with conservative no-callback behavior."""

    if not groups:
        return ()
    if adjudicator is None:
        return tuple(
            QueryDecision(
                query_id=candidate.query_id,
                decision=ACTION_KEEP,
                reasons=("no_adjudicator_conservative_keep",),
                needs_semantic_review=True,
                source_task_id=candidate.source_task_id,
                preserved_task_ids=(candidate.source_task_id,),
            )
            for group in groups
            for candidate in group.queries
        )
    try:
        raw = adjudicator(list(groups))
        decisions, errors = _validate_adjudicator_output(raw, groups)
    except Exception as exc:
        return tuple(
            QueryDecision(
                query_id=candidate.query_id,
                decision=ACTION_KEEP,
                reasons=(f"adjudicator_error_conservative_keep:{type(exc).__name__}",),
                needs_semantic_review=True,
                source_task_id=candidate.source_task_id,
                preserved_task_ids=(candidate.source_task_id,),
            )
            for group in groups
            for candidate in group.queries
        )
    if errors:
        return tuple(
            QueryDecision(
                query_id=candidate.query_id,
                decision=ACTION_KEEP,
                reasons=("adjudicator_invalid_fallback_conservative_keep",),
                needs_semantic_review=True,
                source_task_id=candidate.source_task_id,
                preserved_task_ids=(candidate.source_task_id,),
            )
            for group in groups
            for candidate in group.queries
        )

    by_query: dict[str, QueryDecision] = {}
    for group in groups:
        group_task_ids = tuple(
            dict.fromkeys(candidate.source_task_id for candidate in group.queries)
        )
        for candidate in group.queries:
            decision = next(
                (item for item in decisions if item.query_id == candidate.query_id),
                None,
            )
            if decision is None:
                continue
            refs = tuple(ref for ref in group.refs)
            preserved = tuple(
                dict.fromkeys(
                    [
                        *group_task_ids,
                        *[ref.source_task_id for ref in refs],
                    ]
                )
            )
            if decision.action == ACTION_KEEP:
                by_query[candidate.query_id] = QueryDecision(
                    query_id=candidate.query_id,
                    decision=ACTION_KEEP,
                    reasons=(decision.reason,),
                    matched_refs=refs,
                    needs_semantic_review=decision.needs_semantic_review,
                    source_task_id=candidate.source_task_id,
                    preserved_task_ids=preserved,
                )
            elif decision.action == ACTION_REJECT:
                by_query[candidate.query_id] = QueryDecision(
                    query_id=candidate.query_id,
                    decision=ACTION_REJECT,
                    reasons=(decision.reason,),
                    matched_refs=refs,
                    source_task_id=candidate.source_task_id,
                    preserved_task_ids=preserved,
                )
            else:
                by_query[candidate.query_id] = QueryDecision(
                    query_id=candidate.query_id,
                    decision=ACTION_MERGE,
                    reasons=(decision.reason,),
                    matched_refs=refs,
                    source_task_id=candidate.source_task_id,
                    merged_into_query_id=decision.merged_into_query_id,
                    preserved_task_ids=preserved,
                )
    return tuple(by_query.values())


def finalize_dedup(
    stage1: Stage1Result,
    adjudicator: BatchAdjudicator | None = None,
) -> DedupOutcome:
    """Combine stage 1 with stage-2 adjudication into final per-query decisions."""

    adjudicated: dict[str, QueryDecision] = {}
    adjudicator_calls = 0
    adjudicator_error = ""
    if stage1.ambiguous_groups:
        if adjudicator is not None:
            adjudicator_calls = 1
        try:
            for decision in adjudicate_groups(stage1.ambiguous_groups, adjudicator):
                adjudicated[decision.query_id] = decision
        except Exception as exc:
            adjudicator_error = f"{type(exc).__name__}:{exc}"
            for group in stage1.ambiguous_groups:
                for candidate in group.queries:
                    adjudicated[candidate.query_id] = QueryDecision(
                        query_id=candidate.query_id,
                        decision=ACTION_KEEP,
                        reasons=("adjudicator_error_conservative_keep",),
                        needs_semantic_review=True,
                        source_task_id=candidate.source_task_id,
                        preserved_task_ids=(candidate.source_task_id,),
                    )

    final: list[QueryDecision] = []
    for decision in stage1.decisions:
        if decision.decision == DECISION_AMBIGUOUS:
            final.append(
                adjudicated.get(
                    decision.query_id,
                    QueryDecision(
                        query_id=decision.query_id,
                        decision=ACTION_KEEP,
                        reasons=("no_adjudicator_conservative_keep",),
                        needs_semantic_review=True,
                        source_task_id=decision.source_task_id,
                        preserved_task_ids=(decision.source_task_id,),
                    ),
                )
            )
        else:
            final.append(decision)

    kept = tuple(
        decision
        for decision in final
        if decision.decision
        in {DECISION_UNIQUE, DECISION_SAME_TASK_REPLAY, ACTION_KEEP}
    )
    rejected = tuple(
        decision
        for decision in final
        if decision.decision in {DECISION_DUPLICATE, ACTION_REJECT}
    )
    merged = tuple(
        decision for decision in final if decision.decision == ACTION_MERGE
    )
    return DedupOutcome(
        decisions=tuple(final),
        kept_queries=kept,
        rejected_queries=rejected,
        merged_queries=merged,
        adjudicator_calls=adjudicator_calls,
        adjudicator_error=adjudicator_error,
    )


def known_query_from_history(
    query_id: str,
    text: str,
    source_task_id: str,
    *,
    source: str = SOURCE_HISTORICAL,
) -> KnownQuery:
    """Build a ``KnownQuery`` record for stage-1 input."""

    return KnownQuery(
        query_id=query_id,
        text=text,
        source_task_id=source_task_id,
        source=source,
    )


__all__ = [
    "ACTION_KEEP",
    "ACTION_MERGE",
    "ACTION_REJECT",
    "AmbiguousGroup",
    "BatchAdjudicator",
    "DECISION_AMBIGUOUS",
    "DECISION_DUPLICATE",
    "DECISION_SAME_TASK_REPLAY",
    "DECISION_UNIQUE",
    "DEFAULT_THRESHOLDS",
    "DEDUP_SCHEMA_VERSION",
    "DedupOutcome",
    "KnownQuery",
    "MatchRef",
    "QueryCandidate",
    "QueryDecision",
    "SOURCE_BATCH",
    "SOURCE_HISTORICAL",
    "SOURCE_QUEUED",
    "Similarity",
    "Stage1Result",
    "adjudicate_groups",
    "finalize_dedup",
    "known_query_from_history",
    "normalize_query",
    "query_hash",
    "query_tokens",
    "stage1_deduplicate",
    "token_similarity",
]
