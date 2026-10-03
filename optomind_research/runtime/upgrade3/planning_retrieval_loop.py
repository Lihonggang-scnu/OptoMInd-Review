"""Shared, bounded retrieval loop for review-planning information needs.

Work order 04 of ``docs/planning_work_orders/20260926_content_driven_retrieval``.

Several sections often need the same information.  This module keeps that work
in one light queue instead of letting every section run its own retrieval:

* one stable ``need_id`` per information need, derived from the need's own
  wording, so a resume does not depend on a model inventing a new id;
* one local-first round per attempt: the registered local material is searched
  again every round, and only a need that is still short goes to the bounded
  external closure of work order 03;
* a round that only retried a provider failure is not a research iteration;
* the loop stops as soon as a round adds nothing usable, after two consecutive
  empty rounds, or at the configured round cap;
* one paper is acquired once and its reading is shared by every owner, while
  each owner keeps its own use of that material;
* every attempt is appended to an append-only journal, so a resume reuses the
  successful attempts and only continues what is incomplete.

Deep reads stay on the existing shared counter; this module never re-grants the
per-level quotas and never resets the shared budget.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCHEMA_VERSION = "optomind.planning_retrieval_loop.v1"
JOURNAL_SCHEMA = "optomind.planning_retrieval_loop.journal.v1"

#: Central configuration for the loop's bounds (no scattered magic numbers).
MAX_ROUNDS_DEFAULT = 3
MAX_EMPTY_ROUNDS_DEFAULT = 2
MAX_QUERIES_PER_ROUND_DEFAULT = 3
LOCAL_TOP_PAPERS_DEFAULT = 6
LOCAL_PASSAGES_PER_PAPER_DEFAULT = 2
SHARED_DEEP_READ_DEFAULT = 40

#: Next actions the loop can take for one need.
ACTIONS = (
    "answer_from_local",       # the section can be written now
    "read_local_capture",      # bounded, question-shaped read of stored material
    "external_research",       # the gap needs material that is not local
    "provider_retry",          # infrastructure failure, not a research iteration
    "stop",                    # bounded out or answered
)

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "what", "which",
    "how", "does", "are", "was", "were", "has", "have", "its", "their", "when",
    "where", "why", "who", "than", "then", "they", "them", "there", "these",
}


class RetrievalLoopError(ValueError):
    """Invalid queue input or an unusable round specification."""


@dataclass
class InformationNeed:
    """One information need that may serve several owners (sections)."""

    need_id: str
    question: str
    owners: tuple[str, ...] = ()
    intended_use: str = "mechanism"
    user_scope: str = ""
    success_criteria: tuple[str, ...] = ()
    concepts: tuple[str, ...] = ()
    required_concepts: tuple[str, ...] = ()
    comparison_object: str = ""
    round_specs: tuple[Mapping[str, Any], ...] = ()
    existing_handles: tuple[str, ...] = ()
    # ``directed`` work may be sent to the reader without a search query.  The
    # default keeps the historical supplement behaviour unchanged.
    kind: str = "supplement"

    def __post_init__(self) -> None:
        if not str(self.question or "").strip():
            raise RetrievalLoopError("need_question_required")
        if not str(self.need_id or "").strip():
            raise RetrievalLoopError("need_id_required")
        if self.kind not in {"supplement", "directed"}:
            raise RetrievalLoopError("need_kind_invalid")
        # Keep queue entries on the same intended-use vocabulary as local
        # triage.  In particular, clinical_evidence is an application outcome
        # request and must reach the Plus judge route instead of falling back
        # to the mechanism default.
        from .planning_material_triage import normalize_intended_use
        self.intended_use = normalize_intended_use(self.intended_use)

    def to_dict(self) -> dict[str, Any]:
        return {
            "need_id": self.need_id,
            "question": self.question,
            "owners": list(self.owners),
            "intended_use": self.intended_use,
            "user_scope": self.user_scope,
            "success_criteria": list(self.success_criteria),
            "concepts": list(self.concepts),
            "required_concepts": list(self.required_concepts),
            "comparison_object": self.comparison_object,
            "round_specs": [dict(spec) for spec in self.round_specs],
            "existing_handles": list(self.existing_handles),
            "kind": self.kind,
        }


def need_id_for(question: str, *, comparison_object: str = "") -> str:
    """Stable id for an information need, derived from its own wording.

    The id must not change between rounds or after a resume, and renaming a gap
    must not hand the same need a fresh quota.  It is therefore a hash of the
    normalised question plus the comparison object, not a counter and not a
    model-chosen label.
    """

    text = re.sub(r"\s+", " ", str(question or "")).strip().casefold()
    if comparison_object:
        text = text + " || " + re.sub(r"\s+", " ", str(comparison_object)).strip().casefold()
    if not text:
        raise RetrievalLoopError("need_question_required")
    return "N" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _need_signature(need: InformationNeed) -> str:
    """Research/acceptance shape; chapter ownership is a separate binding."""
    payload = need.to_dict()
    payload.pop("owners", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _signature_matches(saved: Any, current: str) -> bool:
    if str(saved or "") == current:
        return True
    # Preserve pre-WO03 journals rather than charging for compatible work again.
    try:
        payload = json.loads(str(saved or ""))
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    payload.pop("owners", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) == current


def _merge_usable_content(previous: str, current: str) -> str:
    previous, current = previous.strip(), current.strip()
    if not current or current in previous:
        return previous
    if not previous or previous in current:
        return current
    return previous + "\n\n" + current


def _requirement_content(value: Any) -> Any:
    """Separate output/question routing labels from acceptance content."""
    if isinstance(value, Mapping):
        return {key: _requirement_content(item) for key, item in value.items()
                if key not in {"output_id", "question_id", "required_output_ids"}}
    if isinstance(value, (list, tuple)):
        return [_requirement_content(item) for item in value]
    return value


def _normalized_contract_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _external_raw_result(outcome: Mapping[str, Any]) -> dict[str, Any]:
    raw = outcome.get("raw_result")
    if isinstance(raw, Mapping):
        return dict(raw)
    return dict(outcome) if raw is None else {"value": raw}


def _is_scientific_attempt(entry: Mapping[str, Any]) -> bool:
    # A legacy first-round query failure did not cross the retrieval boundary.
    # Let the repaired initializer run instead of restoring that terminal state.
    return (
        entry.get("counts_as_round", True) is not False
        and str(entry.get("status") or "") != "provider_retry"
        and not (int(entry.get("round") or 0) == 1 and entry.get("status") == "stopped_no_query")
    )


def _need_terms(need: InformationNeed) -> set[str]:
    terms: set[str] = set()
    for source in (need.question, *need.concepts):
        for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", str(source or "")):
            token = token.casefold()
            if token not in _STOPWORDS:
                terms.add(token)
    return terms


def merge_similar_needs(needs: Sequence[InformationNeed]) -> list[InformationNeed]:
    """Serve the same information need once, without merging different questions.

    Owners may differ, but research content and acceptance must agree. Merely
    sharing a question (or a caller-supplied id) does not satisfy new outputs.
    """

    merged: dict[str, InformationNeed] = {}
    def contract(need: InformationNeed) -> str:
        return json.dumps({
            "question": _normalized_contract_text(need.question),
            "kind": need.kind,
            "intended_use": need.intended_use,
            "user_scope": _normalized_contract_text(need.user_scope),
            "comparison_object": _normalized_contract_text(need.comparison_object),
            "success_criteria": sorted({_normalized_contract_text(item) for item in need.success_criteria}),
            "required_concepts": sorted({_normalized_contract_text(item) for item in need.required_concepts}),
        }, ensure_ascii=False, sort_keys=True)

    for need in needs:
        key = need.need_id
        existing = merged.get(key)
        if existing is not None and contract(existing) != contract(need):
            key = need.need_id + "-" + hashlib.sha1(contract(need).encode("utf-8")).hexdigest()[:8]
            existing = merged.get(key)
        if existing is None:
            for candidate in merged.values():
                if candidate.kind == need.kind == "supplement" and contract(candidate) == contract(need):
                    existing = candidate
                    break
        if existing is None:
            merged[key] = InformationNeed(
                need_id=key,
                question=need.question,
                owners=tuple(dict.fromkeys(need.owners)),
                intended_use=need.intended_use,
                user_scope=need.user_scope,
                success_criteria=tuple(dict.fromkeys([*need.success_criteria])),
                concepts=tuple(dict.fromkeys(need.concepts)),
                required_concepts=tuple(dict.fromkeys(need.required_concepts)),
                comparison_object=need.comparison_object,
                round_specs=tuple(need.round_specs),
                existing_handles=tuple(dict.fromkeys(need.existing_handles)),
                kind=need.kind,
            )
            continue
        # Compatible needs bind every owner without repeating the research.
        existing.owners = tuple(dict.fromkeys([*existing.owners, *need.owners]))
        existing.success_criteria = tuple(dict.fromkeys([*existing.success_criteria, *need.success_criteria]))
        existing.concepts = tuple(dict.fromkeys([*existing.concepts, *need.concepts]))
        existing.required_concepts = tuple(dict.fromkeys([*existing.required_concepts, *need.required_concepts]))
        existing.existing_handles = tuple(dict.fromkeys([*existing.existing_handles, *need.existing_handles]))
        if not existing.round_specs and need.round_specs:
            existing.round_specs = tuple(need.round_specs)
    return list(merged.values())


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------


class RetrievalJournal:
    """Append-only attempt journal, so a resume never repeats paid work."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: list[dict[str, Any]] = []
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        self._entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        self._known: set[tuple[str, int]] = {
            (str(entry.get("need_id")), int(entry.get("round") or 0)) for entry in self._entries
        }

    @property
    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def has(self, need_id: str, round_index: int, *, signature: str | None = None) -> bool:
        matches = [
            entry for entry in self._entries
            if str(entry.get("need_id")) == str(need_id)
            and int(entry.get("round") or 0) == int(round_index)
            and _is_scientific_attempt(entry)
            and (signature is None or _signature_matches(entry.get("need_signature"), signature))
        ]
        return bool(matches)

    def record(self, entry: Mapping[str, Any]) -> None:
        row = {"schema_version": JOURNAL_SCHEMA, "recorded_at": time.time(), **dict(entry)}
        self._entries.append(row)
        self._known.add((str(row.get("need_id")), int(row.get("round") or 0)))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def entries_for(self, need_id: str) -> list[dict[str, Any]]:
        return [entry for entry in self._entries if str(entry.get("need_id")) == str(need_id)]

    def rounds_completed(self, need_id: str) -> int:
        return max(
            (
                int(entry.get("round") or 0)
                for entry in self.entries_for(need_id)
                if _is_scientific_attempt(entry)
            ),
            default=0,
        )


# --------------------------------------------------------------------------
# Round specifications
# --------------------------------------------------------------------------


def _spec_for_round(need: InformationNeed, round_index: int) -> dict[str, Any]:
    """The external request specification for one round.

    Round 1 uses the need's own narrow queries.  A later round must change the
    query direction: it names a different object or relation instead of asking
    the same thing again.
    """

    for spec in need.round_specs:
        if int(spec.get("round") or 0) == round_index:
            return dict(spec)
    return {}


def _queries_from_spec(
    spec: Mapping[str, Any],
    *,
    limit: int = MAX_QUERIES_PER_ROUND_DEFAULT,
) -> list[dict[str, str]]:
    queries: list[dict[str, str]] = []
    for row in spec.get("targeted_queries") or ():
        if isinstance(row, Mapping):
            query_text = str(row.get("query_text") or "").strip()
            query_type = str(row.get("query_type") or "keyword").strip().casefold()
            facet_id = str(row.get("facet_id") or "").strip()
        else:
            query_text, query_type, facet_id = str(row or "").strip(), "keyword", ""
        if query_text and query_type in {"keyword", "question"}:
            queries.append({"query_text": query_text, "query_type": query_type, "facet_id": facet_id})
    return queries[:max(0, int(limit))]


def _spec_from_refined_queries(value: Any) -> dict[str, Any]:
    """Normalize a refine callback's compact query return value."""

    if isinstance(value, Mapping):
        if "targeted_queries" in value:
            return dict(value)
        if "query_text" in value:
            return {"targeted_queries": [dict(value)]}
    if isinstance(value, (str, bytes)):
        return {"targeted_queries": [str(value)]}
    if isinstance(value, Sequence):
        return {"targeted_queries": list(value)}
    return {}


def queries_changed(previous: Sequence[Mapping[str, str]], current: Sequence[Mapping[str, str]]) -> bool:
    """True when a later round actually changed the query direction."""

    old = {str(row.get("query_text") or "").strip().casefold() for row in previous}
    new = {str(row.get("query_text") or "").strip().casefold() for row in current}
    return bool(new - old)


# --------------------------------------------------------------------------
# Loop
# --------------------------------------------------------------------------


@dataclass
class NeedState:
    need: InformationNeed
    round_index: int = 0
    empty_rounds: int = 0
    action: str = "external_research"
    status: str = "pending"
    local_decision: str = ""
    usable_content: str = ""
    still_missing: str = ""
    read_focus: str = ""
    external_ask: str = ""
    new_handles: tuple[str, ...] = ()
    new_passages: int = 0
    local_triage: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    external_results: list[dict[str, Any]] = field(default_factory=list)
    consumed_paper_ids: tuple[str, ...] = ()
    last_error: str = ""
    queries: list[dict[str, str]] = field(default_factory=list)
    query_status: str = ""
    query_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "need": self.need.to_dict(),
            "need_id": self.need.need_id,
            "owners": list(self.need.owners),
            "rounds": self.round_index,
            "empty_rounds": self.empty_rounds,
            "action": self.action,
            "status": self.status,
            "local_decision": self.local_decision,
            "usable_content": self.usable_content,
            "still_missing": self.still_missing,
            "read_focus": self.read_focus,
            "external_ask": self.external_ask,
            "new_handles": list(self.new_handles),
            "new_passages": self.new_passages,
            "local_triage": dict(self.local_triage),
            "attempts": list(self.attempts),
            "external_results": list(self.external_results),
            "consumed_paper_ids": list(self.consumed_paper_ids),
            "last_error": self.last_error,
            "queries": list(self.queries),
            "query_status": self.query_status,
            "query_source": self.query_source,
        }

    def content_for_owner(self, owner: str) -> dict[str, Any]:
        """What one section gets: the same material, interpreted for its use."""

        result = {
            "need_id": self.need.need_id,
            "owner": owner,
            "question": self.need.question,
            "intended_use": self.need.intended_use,
            "usable_content": self.usable_content,
            "still_missing": self.still_missing,
            "read_focus": self.read_focus,
            "external_ask": self.external_ask,
            "handles": list(self.new_handles),
            "decision": self.local_decision,
            "status": self.status,
            "is_owner_request": owner in self.need.owners,
            "local_triage": dict(self.local_triage),
            "query_status": self.query_status,
            "query_error": self.last_error if self.query_status == "query_not_formed" else "",
        }
        if "answers_requested_question" in self.local_triage:
            result["answers_requested_question"] = self.local_triage["answers_requested_question"]
        return result


@dataclass
class LoopConfig:
    index_path: Path
    journal_path: Path
    max_rounds: int = MAX_ROUNDS_DEFAULT
    max_empty_rounds: int = MAX_EMPTY_ROUNDS_DEFAULT
    max_queries_per_round: int = MAX_QUERIES_PER_ROUND_DEFAULT
    local_top_papers: int = LOCAL_TOP_PAPERS_DEFAULT
    local_passages_per_paper: int = LOCAL_PASSAGES_PER_PAPER_DEFAULT
    shared_deep_read_budget: int = SHARED_DEEP_READ_DEFAULT
    max_provider_retries: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_path": str(self.index_path),
            "journal_path": str(self.journal_path),
            "max_rounds": self.max_rounds,
            "max_empty_rounds": self.max_empty_rounds,
            "max_queries_per_round": self.max_queries_per_round,
            "local_top_papers": self.local_top_papers,
            "local_passages_per_paper": self.local_passages_per_paper,
            "shared_deep_read_budget": self.shared_deep_read_budget,
            "max_provider_retries": self.max_provider_retries,
        }


LocalTriage = Callable[..., Any]
ExternalClosure = Callable[..., Any]
LocalSearch = Callable[..., Any]


def run_retrieval_loop(
    needs: Sequence[InformationNeed],
    config: LoopConfig,
    *,
    local_triage: LocalTriage | None = None,
    local_search: LocalSearch | None = None,
    external_closure: ExternalClosure | None = None,
    resume: bool = True,
    budget_available_cny: float | None = None,
    budget_floor_cny: float = 0.0,
    refine_queries: Callable[..., Any] | None = None,
    prior_consumed_paper_ids: Sequence[str] = (),
    verbose: bool = False,
) -> dict[str, Any]:
    """Run the shared queue, one bounded round per need at a time."""

    from .planning_material_search import PlanningMaterialIndex
    from .planning_material_triage import LocalGap, triage_gap

    merged = merge_similar_needs(needs)
    journal = RetrievalJournal(config.journal_path) if resume else RetrievalJournal(Path(config.journal_path).with_suffix(".fresh.jsonl"))
    states = {need.need_id: NeedState(need=need) for need in merged}
    shared_reads: dict[str, int] = {}
    paper_owners: dict[str, set[str]] = {}
    consumed_paper_ids: set[str] = {
        str(item) for item in prior_consumed_paper_ids if str(item).strip()
    }

    def acquire_index():
        return PlanningMaterialIndex(Path(config.index_path), readonly=True)

    def local_pass(need: InformationNeed, round_index: int) -> dict[str, Any]:
        gap = LocalGap(
            gap_id=need.need_id,
            question=need.question,
            intended_use=need.intended_use,
            user_scope=need.user_scope,
            success_criteria=need.success_criteria,
            chapter_ids=need.owners,
            concepts=need.concepts,
            required_concepts=need.required_concepts,
            existing_handles=need.existing_handles,
        )
        if local_triage is not None:
            return dict(local_triage(gap=gap, round_index=round_index))
        with acquire_index() as index:
            judgment = triage_gap(
                index, gap,
                top_papers=config.local_top_papers,
                passages_per_paper=config.local_passages_per_paper,
            )
        return judgment.to_dict()

    def call_external_closure(
        need: InformationNeed,
        round_index: int,
        queries: Sequence[Mapping[str, str]],
        spec: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Call old four-argument adapters and new budget-aware adapters."""

        all_kwargs = {
            "need": need,
            "round_index": round_index,
            "queries": queries,
            "spec": spec,
            "remaining_deep_read_budget": max(
                0, int(config.shared_deep_read_budget) - len(consumed_paper_ids)
            ),
            "prior_consumed_paper_ids": sorted(consumed_paper_ids),
        }
        try:
            parameters = inspect.signature(external_closure).parameters  # type: ignore[arg-type]
        except (TypeError, ValueError):
            parameters = {}
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            return external_closure(**all_kwargs)  # type: ignore[misc]
        accepted = {name for name, parameter in parameters.items()
                    if parameter.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}}
        return external_closure(**{key: value for key, value in all_kwargs.items() if key in accepted})  # type: ignore[misc]

    # Always inspect from round one on resume.  Successful rounds are restored
    # from the journal without being paid or counted as current attempts; this
    # also lets a changed request shape invalidate just its old round one entry.
    for round_index in range(1, max(1, int(config.max_rounds)) + 1):
        progressed = False
        if verbose:
            print("ROUND", round_index, "states", {k: (v.status, v.round_index) for k, v in states.items()})
        for need_id, state in states.items():
            if state.status in {"answered", "stopped"} or (state.status == "partial" and state.action == "stop"):
                continue
            # A provider retry is infrastructure work, not a reason to move to
            # the next scientific round.  Leave it pending so a later resume
            # can retry the same round with the same query direction.
            if state.status == "pending" and state.action == "provider_retry":
                continue
            need = state.need
            signature = _need_signature(need)
            # Infrastructure failures are not completed research rounds, but
            # their material, actual attempt paths and consumed paper slots
            # still belong to the need after an interrupted or completed retry.
            for diagnostic in journal.entries_for(need_id):
                if (int(diagnostic.get("round") or 0) != round_index
                        or diagnostic.get("status") != "provider_retry"
                        or not _signature_matches(diagnostic.get("need_signature"), signature)):
                    continue
                raw_rows = diagnostic.get("external_results") or [diagnostic.get("external_result")]
                for raw in raw_rows:
                    if isinstance(raw, Mapping) and raw not in state.external_results:
                        state.external_results.append(dict(raw))
                state.usable_content = _merge_usable_content(state.usable_content, str(diagnostic.get("usable_content") or ""))
                if diagnostic.get("still_missing"):
                    state.still_missing = str(diagnostic["still_missing"])
                consumed = {str(item) for item in diagnostic.get("consumed_paper_ids") or () if str(item).strip()}
                consumed_paper_ids.update(consumed)
                state.consumed_paper_ids = tuple(sorted(set(state.consumed_paper_ids).union(consumed)))
            if journal.has(need_id, round_index, signature=signature):
                saved = next(
                    entry for entry in reversed(journal.entries_for(need_id))
                    if int(entry.get("round") or 0) == round_index
                    and _signature_matches(entry.get("need_signature"), signature)
                    and _is_scientific_attempt(entry)
                )
                state.round_index = max(state.round_index, round_index)
                state.local_decision = str(saved.get("local_decision") or state.local_decision)
                state.usable_content = _merge_usable_content(state.usable_content, str(saved.get("usable_content") or ""))
                state.still_missing = str(saved.get("still_missing", state.still_missing) or "")
                state.queries = list(saved.get("queries") or [])
                state.query_status = str(saved.get("query_status") or ("ready" if state.queries else ""))
                state.query_source = str(saved.get("query_source") or "")
                state.read_focus = str(saved.get("read_focus") or state.read_focus)
                state.external_ask = str(saved.get("external_ask") or state.external_ask)
                if isinstance(saved.get("local_triage"), Mapping):
                    state.local_triage = dict(saved["local_triage"])
                state.new_handles = tuple(saved.get("new_handles") or state.new_handles)
                saved_external = saved.get("external_results")
                if isinstance(saved_external, list):
                    state.external_results.extend(
                        item for item in saved_external if isinstance(item, Mapping)
                    )
                elif isinstance(saved.get("external_result"), Mapping):
                    state.external_results.append(dict(saved["external_result"]))
                saved_consumed = saved.get("consumed_paper_ids") or ()
                consumed_paper_ids.update(str(item) for item in saved_consumed if str(item).strip())
                state.consumed_paper_ids = tuple(sorted(set(state.consumed_paper_ids).union(
                    str(item) for item in saved_consumed if str(item).strip()
                )))
                for handle in state.new_handles:
                    shared_reads.setdefault(handle, round_index)
                    paper_owners.setdefault(handle, set()).update(need.owners)
                state.status = str(saved.get("status") or state.status)
                state.action = str(saved.get("action") or state.action)
                progressed = True
                continue
            progressed = True
            state.round_index = round_index
            local = local_pass(need, round_index)
            decision = str(local.get("decision") or "external_research")
            state.local_decision = decision
            state.local_triage = dict(local)
            usable = str(local.get("usable_content") or "").strip()
            if usable:
                state.usable_content = _merge_usable_content(state.usable_content, usable)
            local_missing = str(local.get("still_missing") or "").strip()
            if local_missing and (not state.still_missing or state.still_missing == need.question):
                state.still_missing = local_missing
            state.read_focus = str(local.get("read_focus") or state.read_focus)
            state.external_ask = str(local.get("external_ask") or state.external_ask)
            passages = local.get("passages")
            passages = passages if isinstance(passages, list) else []
            handles = tuple(
                dict.fromkeys(
                    str(item.get("source_handle") or "")
                    for item in passages
                    if isinstance(item, Mapping) and item.get("source_handle")
                )
            )
            new_handles = tuple(handle for handle in handles if handle and handle not in shared_reads)
            for handle in handles:
                shared_reads.setdefault(handle, round_index)
                paper_owners.setdefault(handle, set()).update(need.owners)
            state.new_handles = tuple(dict.fromkeys([*state.new_handles, *new_handles]))

            entry: dict[str, Any] = {
                "need_id": need_id,
                "need_signature": signature,
                "round": round_index,
                "owners": list(need.owners),
                "local_decision": decision,
                "usable_content": state.usable_content,
                "still_missing": state.still_missing,
                "new_handles": list(new_handles),
                "new_passages": len(passages),
                "queries": [],
                "action": "answer_from_local" if usable and decision == "direct_use" else (
                    "read_local_capture" if decision == "local_deep_read" else "external_research"
                ),
                "status": "in_progress",
                "local_triage": dict(local),
            }

            if local.get("provider_failed"):
                # An unread local attempt is not evidence that the library is
                # insufficient. Retain its partial material and await an explicit
                # retry; do not spend an external-search round on this failure.
                entry.update({"status": "provider_retry", "action": "provider_retry",
                              "counts_as_round": False, "error": str(local.get("error") or "local_judge_failed")})
                state.status, state.action = "pending", "provider_retry"
                journal.record(entry)
                state.attempts.append(entry)
                continue

            if decision == "direct_use" and usable:
                entry["status"] = "answered"
                state.status = "answered"
                state.action = "answer_from_local"
                journal.record(entry)
                state.attempts.append(entry)
                continue

            if decision == "local_deep_read":
                # A focused read can return useful prose while still saying that
                # the need is not fully answered.  Keep that material for the
                # writer, but leave the need open until the judge returns
                # direct_use.  This prevents a pending local_deep_read from
                # being treated as ready merely because it contains text.
                entry["status"] = "needs_external"
                entry["local_material_waiting_review"] = [
                    str(item.get("source_handle") or "")
                    for item in passages
                    if isinstance(item, Mapping)
                ]
                state.action = "external_research"

            if budget_available_cny is not None and float(budget_available_cny) <= float(budget_floor_cny):
                entry.update({"status": "stopped_budget", "action": "stop",
                              "note": "known budget is at or below the configured floor; acquired content is kept"})
                state.status = "partial" if state.usable_content else "stopped"
                state.action = "stop"
                journal.record(entry)
                state.attempts.append(entry)
                continue

            explicit_spec = _spec_for_round(need, round_index)
            spec = explicit_spec
            queries = _queries_from_spec(spec, limit=config.max_queries_per_round)
            query_source = "need" if queries else ""
            # The checkpoint precedes the external boundary, so a provider
            # failure or interrupted run resumes the same initialized query.
            prepared = next((item for item in reversed(journal.entries_for(need_id))
                             if int(item.get("round") or 0) == round_index
                             and _signature_matches(item.get("need_signature"), signature)
                             and item.get("status") == "query_ready"), None)
            if prepared is not None:
                spec = dict(prepared.get("query_spec") or {})
                queries = _queries_from_spec({"targeted_queries": prepared.get("queries") or []},
                                             limit=config.max_queries_per_round)
                query_source = str(prepared.get("query_source") or "journal")
            if round_index == 1 and not queries and need.kind != "directed":
                reason = "query_refiner_unavailable"
                if refine_queries is not None:
                    try:
                        refined = refine_queries(need, round_index, [], state.usable_content,
                                                 state.still_missing or need.question)
                        spec = {**explicit_spec, **_spec_from_refined_queries(refined)}
                        queries = _queries_from_spec(spec, limit=config.max_queries_per_round)
                        query_source = "refiner"
                        reason = "query_refiner_returned_no_usable_query"
                    except Exception as exc:
                        reason = "query_refiner_failed:" + type(exc).__name__
                if not queries:
                    state.query_status = "query_not_formed"
                    state.last_error = reason
                    state.still_missing = state.still_missing or need.question
                    state.status = "partial" if state.usable_content else "stopped"
                    state.action = "stop"
                    entry.update({"status": "query_not_formed", "action": "stop", "counts_as_round": False,
                                  "query_status": state.query_status, "error": reason,
                                  "error_stage": "query_initialization", "question": need.question,
                                  "still_missing": state.still_missing,
                                  "note": "query not formed; external retrieval was not attempted"})
                    journal.record(entry)
                    state.attempts.append(entry)
                    continue
            previous_queries: list[dict[str, str]] = []
            if round_index > 1:
                prior_entries = [
                    item for item in journal.entries_for(need_id)
                    if int(item.get("round") or 0) < round_index
                    and _signature_matches(item.get("need_signature"), signature)
                    and _is_scientific_attempt(item)
                ]
                if prior_entries:
                    previous_queries = _queries_from_spec(
                        {"targeted_queries": prior_entries[-1].get("queries") or ()},
                        limit=config.max_queries_per_round,
                    )
                if not previous_queries:
                    previous_queries = _queries_from_spec(
                        _spec_for_round(need, round_index - 1),
                        limit=config.max_queries_per_round,
                    )
                if prepared is None and not explicit_spec and refine_queries is not None:
                    try:
                        refined = refine_queries(
                            need,
                            round_index,
                            previous_queries,
                            state.usable_content,
                            state.still_missing,
                        )
                    except Exception as exc:
                        entry.update({
                            "status": "provider_retry",
                            "action": "provider_retry",
                            "counts_as_round": False,
                            "error": type(exc).__name__,
                            "error_stage": "refine_queries",
                        })
                        journal.record(entry)
                        state.attempts.append(entry)
                        state.status = "pending"
                        state.action = "provider_retry"
                        state.last_error = type(exc).__name__
                        continue
                    spec = _spec_from_refined_queries(refined)
                    queries = _queries_from_spec(spec, limit=config.max_queries_per_round)
                    query_source = "refiner"
                    # Facets are local routing metadata, not something the
                    # query rewriter has to regenerate on every search.
                    default_facet = next((row.get("facet_id") for row in previous_queries if row.get("facet_id")), "")
                    if default_facet:
                        for query in queries:
                            if not query.get("facet_id"):
                                query["facet_id"] = default_facet
            entry["queries"] = list(queries)
            state.queries = list(queries)
            state.query_status = "ready" if queries else "not_required" if need.kind == "directed" and round_index == 1 else "exhausted"
            state.query_source = query_source
            entry.update({"query_status": state.query_status, "query_source": query_source})
            if round_index > 1 and queries and not queries_changed(previous_queries, queries):
                entry["status"] = "stopped_same_query"
                entry["action"] = "stop"
                # Preserve a substantive partial result so a fulfilled source
                # mixed with an unresolved gap cannot look fully closed or be
                # flattened into a generic stopped state.
                state.status = "partial" if state.usable_content.strip() else "stopped"
                state.action = "stop"
                journal.record(entry)
                state.attempts.append(entry)
                continue
            if not queries and not (need.kind == "directed" and round_index == 1):
                # The need has no further query direction for this round: that is
                # one more round without new material, recorded as such.
                state.empty_rounds += 1
                entry["status"] = "stopped_no_query"
                entry["action"] = "stop"
                entry["empty_rounds"] = state.empty_rounds
                state.status = "partial" if state.usable_content.strip() else "stopped"
                state.action = "stop"
                journal.record(entry)
                state.attempts.append(entry)
                continue

            if queries and prepared is None:
                journal.record({**entry, "status": "query_ready", "counts_as_round": False,
                                "query_spec": {**spec, "targeted_queries": list(queries)}})

            if external_closure is None:
                entry["status"] = "pending_external"
                entry["counts_as_round"] = False
                journal.record(entry)
                state.attempts.append(entry)
                state.status = "pending"
                continue

            provider_retries = 0
            while True:
                try:
                    outcome = dict(call_external_closure(need, round_index, queries, spec))
                except Exception as exc:  # provider problems are not research iterations
                    error = type(exc).__name__
                    retry_entry = dict(entry)
                    retry_entry.update({
                        "status": "provider_retry",
                        "action": "provider_retry",
                        "counts_as_round": False,
                        "error": error,
                    })
                    journal.record(retry_entry)
                    state.attempts.append(retry_entry)
                    state.action = "provider_retry"
                    state.status = "pending"
                    state.last_error = error
                    if provider_retries < max(0, int(config.max_provider_retries)):
                        provider_retries += 1
                        continue
                    break

                provider_failed = bool(outcome.get("provider_failed"))
                status = str(outcome.get("status") or "")
                if provider_failed or status in {"failed", "provider_error"}:
                    error = str(outcome.get("error") or "provider_failed")
                    raw_result = _external_raw_result(outcome)
                    state.external_results.append(raw_result)
                    state.usable_content = _merge_usable_content(state.usable_content, str(outcome.get("usable_content") or ""))
                    state.still_missing = str(outcome.get("still_missing") or state.still_missing or need.question)
                    consumed = {str(item) for item in outcome.get("consumed_paper_ids") or () if str(item).strip()}
                    consumed_paper_ids.update(consumed)
                    state.consumed_paper_ids = tuple(sorted(set(state.consumed_paper_ids).union(consumed)))
                    retry_entry = dict(entry)
                    retry_entry.update({
                        "status": "provider_retry",
                        "action": "provider_retry",
                        "counts_as_round": False,
                        "error": error,
                        "external_result": raw_result,
                        "external_results": [raw_result],
                        "usable_content": state.usable_content,
                        "still_missing": state.still_missing,
                        "consumed_paper_ids": sorted(consumed),
                    })
                    journal.record(retry_entry)
                    state.attempts.append(retry_entry)
                    state.action = "provider_retry"
                    state.status = "pending"
                    state.last_error = error
                    if provider_retries < max(0, int(config.max_provider_retries)):
                        provider_retries += 1
                        continue
                    break
                state.status = "in_progress"
                state.action = "external_research"
                break

            # All retry attempts failed.  Keep this need pending at this same
            # scientific round; do not advance to a new query direction.
            if state.status == "pending" and state.action == "provider_retry":
                continue

            added_handles = tuple(str(item) for item in (outcome.get("new_handles") or ()) if str(item))
            useful = str(outcome.get("usable_content") or "").strip()
            if useful:
                state.usable_content = _merge_usable_content(state.usable_content, useful)
            state.new_handles = tuple(dict.fromkeys([*state.new_handles, *added_handles]))
            for handle in added_handles:
                shared_reads.setdefault(handle, round_index)
                paper_owners.setdefault(handle, set()).update(need.owners)
            consumed = tuple(
                dict.fromkeys(
                    str(item) for item in (outcome.get("consumed_paper_ids") or ()) if str(item).strip()
                )
            )
            consumed_paper_ids.update(consumed)
            state.consumed_paper_ids = tuple(sorted(set(state.consumed_paper_ids).union(consumed)))
            raw_result = _external_raw_result(outcome)
            state.external_results.append(raw_result)
            entry["new_handles"] = list(dict.fromkeys([*entry.get("new_handles", []), *added_handles]))
            entry["usable_content"] = state.usable_content
            entry["still_missing"] = state.still_missing
            entry["external_result"] = raw_result
            entry["external_results"] = [raw_result]
            entry["consumed_paper_ids"] = list(consumed)
            entry["external_status"] = status
            entry["external_outcome"] = str(outcome.get("outcome") or "")
            entry["external_usable_content"] = bool(useful)
            if str(outcome.get("still_missing") or "").strip():
                state.still_missing = str(outcome["still_missing"])
            elif status == "fulfilled" and useful:
                state.still_missing = ""
            if str(outcome.get("external_ask") or "").strip():
                state.external_ask = str(outcome["external_ask"])

            # A round only counts as progress when it produced content the section
            # can use.  Merely returning handle identifiers is not new material.
            gained = (status in {"fulfilled", "partial"} and bool(useful)) or bool(added_handles and useful)
            if gained:
                state.empty_rounds = 0
            else:
                state.empty_rounds += 1
            entry["gained"] = gained
            entry["empty_rounds"] = state.empty_rounds

            if status == "fulfilled" and useful and not state.still_missing.strip():
                entry["status"] = "answered"
                state.status = "answered"
                state.action = "external_research"
            elif state.empty_rounds >= max(1, int(config.max_empty_rounds)):
                entry["status"] = "stopped_no_new_content"
                entry["action"] = "stop"
                state.status = "stopped"
                state.action = "stop"
            else:
                entry["status"] = "partial"
                state.status = "partial"
                state.action = "external_research"
            entry["still_missing"] = state.still_missing
            journal.record(entry)
            state.attempts.append(entry)
        if not progressed:
            break

    for state in states.values():
        if (
            state.status in {"pending", "partial"}
            and state.action not in {"stop", "provider_retry"}
            and state.round_index >= int(config.max_rounds)
        ):
            # Reported as bounded-out, not as a scientific conclusion: a resume
            # with a higher round cap continues exactly where this stopped.
            state.status = "stopped_rounds_exhausted"

    result = {
        "schema_version": SCHEMA_VERSION,
        "config": config.to_dict(),
        "needs": [state.to_dict() for state in states.values()],
        "shared_reads": {handle: round_index for handle, round_index in sorted(shared_reads.items())},
        "paper_owners": {handle: sorted(owners) for handle, owners in sorted(paper_owners.items())},
        "shared_deep_read_budget": config.shared_deep_read_budget,
        "consumed_paper_ids": sorted(consumed_paper_ids),
        "shared_deep_reads_used": len(consumed_paper_ids),
        "shared_deep_reads_remaining": max(0, int(config.shared_deep_read_budget) - len(consumed_paper_ids)),
        "external_results": {
            state.need.need_id: list(state.external_results)
            for state in states.values() if state.external_results
        },
        "owner_content": {
            state.need.need_id: {
                owner: state.content_for_owner(owner) for owner in state.need.owners
            }
            for state in states.values()
        },
        "journal_entries": len(journal.entries),
        "external_calls": sum(
            1 for entry in journal.entries if entry.get("external_status") is not None
        ),
    }
    return result


def write_loop_result(result: Mapping[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def needs_from_planner_gap_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    intended_use: str = "mechanism",
    user_scope: str = "",
) -> list[InformationNeed]:
    """Build queue entries from the planner's gap rows (thin wrapper)."""

    from .planning_material_triage import normalize_intended_use

    out: list[InformationNeed] = []
    for index, raw in enumerate(rows, start=1):
        question = str(raw.get("gap_question") or raw.get("question") or "").strip()
        if not question:
            continue
        comparison = str(raw.get("comparison_object") or "").strip()
        owners = tuple(str(item) for item in (raw.get("chapter_ids") or raw.get("owners") or ()) if str(item).strip())
        concepts: list[str] = []
        for query in raw.get("targeted_queries") or ():
            if isinstance(query, Mapping):
                text = str(query.get("query_text") or "").strip()
            else:
                text = str(query or "").strip()
            if text:
                concepts.append(text)
        round_specs = [dict(spec) for spec in raw.get("round_specs") or () if isinstance(spec, Mapping)]
        inherited_queries = _queries_from_spec(raw)
        first_spec = next((spec for spec in round_specs if int(spec.get("round") or 0) == 1), None)
        if inherited_queries and (first_spec is None or not _queries_from_spec(first_spec)):
            if first_spec is None:
                round_specs.insert(0, {"round": 1, "targeted_queries": inherited_queries})
            else:
                first_spec["targeted_queries"] = inherited_queries
        criteria = [str(item) for item in raw.get("success_criteria") or () if str(item).strip()]
        # Acceptance changes invalidate the round state even when the question
        # text stays the same. Keep structured outputs canonical and distinct.
        criteria.extend(
            json.dumps(_requirement_content(item), ensure_ascii=False, sort_keys=True, default=str)
            if isinstance(item, Mapping) else str(item).strip()
            for item in raw.get("required_outputs") or () if item
        )
        out.append(InformationNeed(
            need_id=str(raw.get("need_id") or need_id_for(question, comparison_object=comparison)),
            question=question,
            owners=owners,
            intended_use=normalize_intended_use(str(raw.get("intended_use") or intended_use)),
            user_scope=str(raw.get("user_scope") or user_scope),
            success_criteria=tuple(dict.fromkeys(criteria)),
            concepts=tuple(concepts[:MAX_QUERIES_PER_ROUND_DEFAULT]),
            required_concepts=tuple(str(item) for item in (raw.get("required_concepts") or ()) if str(item).strip()),
            comparison_object=comparison,
            round_specs=tuple(dict(spec) for spec in round_specs if isinstance(spec, Mapping)),
            existing_handles=tuple(str(item) for item in (raw.get("existing_handles") or ()) if str(item).strip()),
            kind=str(raw.get("kind") or "supplement"),
        ))
    return out


__all__ = [
    "ACTIONS",
    "JOURNAL_SCHEMA",
    "LoopConfig",
    "MAX_EMPTY_ROUNDS_DEFAULT",
    "MAX_ROUNDS_DEFAULT",
    "NeedState",
    "RetrievalJournal",
    "RetrievalLoopError",
    "SCHEMA_VERSION",
    "InformationNeed",
    "merge_similar_needs",
    "need_id_for",
    "needs_from_planner_gap_rows",
    "queries_changed",
    "run_retrieval_loop",
    "write_loop_result",
]
