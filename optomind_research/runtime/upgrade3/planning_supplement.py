"""Targeted, traceable literature supplementation for review planning.

This module is deliberately separate from the frozen M1--3 planning and
candidate-corpus stages.  It consumes an existing validated M1 plan and
``PLANNING_POOL.jsonl``, retrieves only planner-supplied or explicitly selected
plan queries, materializes each source in a new source-unit directory, runs the
existing A/B reading card, then asks a separate task-specific judge whether the
material closes one named planning gap.

The canonical pool is read-only.  A run writes a byte-for-byte base-pool copy,
a compatible derived ``PLANNING_POOL.jsonl``, and an append-only sidecar with
each raw hit, identity decision, acquisition receipt, card, and fulfillment
judgment.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from .practical_materials import decode_practical_json, load_practical_material

from .candidate_corpus import (
    KEYWORD_SOURCE,
    QUESTION_SOURCE,
    Hit,
    QuerySpec,
    harvest_query,
    plan_queries,
)
from .material_acquisition import match_identity, normalize_doi
from .query_plan import load_field_dictionary, validate_plan

REQUEST_SCHEMA = "optomind.planning_supplement.request.v1"
RESULT_SCHEMA = "optomind.planning_supplement.result.v1"
MAX_TARGETED_QUERIES = 3
MAX_CANDIDATES_HARD = 50
MAX_ACQUISITIONS_HARD = 8
MAX_PER_QUERY_HARD = 50
DEFAULT_MAX_CANDIDATES = 20
DEFAULT_MAX_ACQUISITIONS = 2
DEFAULT_PER_QUERY_LIMIT = 20
JUDGMENT_STATUSES = {"fulfilled", "partial", "unmet", "unjudgeable"}


class PlanningSupplementError(ValueError):
    """Invalid request, plan, or planning-pool data."""


class OutputDirectoryError(PlanningSupplementError):
    """The output directory is not new and empty."""


def allocate_supplement_attempt(output_root: str | Path) -> Path:
    """Reserve a fresh directory without replacing an earlier attempt.

    Allocation does not authorize or initiate a retry. The caller retains its
    existing retry and budget policy, and passes this empty directory to
    ``run_planning_supplement``. Atomic creation also isolates concurrent calls.
    Legacy artifacts at the logical root are left untouched.
    """

    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="attempt-", dir=root))


@dataclass
class _SearchResponse:
    status_code: int | None


class CompositeS2OpenAlexGateway:
    """Use existing S2 endpoints first and bounded OpenAlex paper-search fallback."""

    def __init__(self, s2_gateway: Any, openalex_backend: Any) -> None:
        self.s2_gateway = s2_gateway
        self.openalex_backend = openalex_backend
        self.last_provider_audit: list[dict[str, Any]] = []
        self.last_openalex_records: list[dict[str, Any]] = []
        self.last_search_provider = ""

    @staticmethod
    def _status(response: Any) -> int | None:
        value = getattr(response, "status_code", None)
        return int(value) if isinstance(value, int) else None

    def search_papers(self, query: str, *, limit: int = 20, **kwargs: Any):
        self.last_provider_audit = []
        self.last_openalex_records = []
        self.last_search_provider = ""
        s2_rows: list[Any] = []
        s2_status: int | None = None
        try:
            s2_rows, s2_response = self.s2_gateway.search_papers(query, limit=limit, **kwargs)
            s2_status = self._status(s2_response)
            self.last_provider_audit.append({
                "provider": "semantic_scholar",
                "status_code": s2_status,
                "returned": len(s2_rows or []),
                "retryable": s2_status is None or s2_status in {408, 425, 429} or s2_status >= 500,
            })
        except Exception as exc:
            self.last_provider_audit.append({
                "provider": "semantic_scholar",
                "status_code": None,
                "returned": 0,
                "error": type(exc).__name__,
                "retryable": True,
            })
        if s2_rows and s2_status is not None and 200 <= s2_status < 300:
            self.last_search_provider = "semantic_scholar"
            return s2_rows[:limit], _SearchResponse(s2_status)

        openalex_rows: list[dict[str, Any]] = []
        openalex_error = ""
        try:
            openalex_rows = list(self.openalex_backend.search(query, max_results=min(int(limit), 50)) or [])
            backend_status = str(getattr(self.openalex_backend, "last_status", "") or "").casefold()
            # OpenAlexBackend returns [] for both a successful empty search and
            # a failed/disabled transport. Its status field is the distinction.
            oa_status = None if backend_status in {"provider_error", "error"} else 200
            if oa_status is None:
                openalex_error = str(getattr(self.openalex_backend, "last_error", "") or "provider_error")
        except Exception as exc:
            oa_status = None
            openalex_error = type(exc).__name__
        self.last_openalex_records = [dict(row) for row in openalex_rows if isinstance(row, Mapping)]
        self.last_provider_audit.append({
            "provider": "openalex",
            "status_code": oa_status,
            "returned": len(self.last_openalex_records),
            "error": openalex_error,
            "retryable": oa_status is None,
        })
        if self.last_openalex_records:
            self.last_search_provider = "openalex"
            converted = [
                SimpleNamespace(
                    paper_id="",
                    corpus_id="",
                    doi=str(row.get("doi") or ""),
                    openalex_id=str(row.get("openalex_id") or row.get("source_id") or ""),
                    title=str(row.get("title") or ""),
                    authors=list(row.get("authors") or []),
                    year=row.get("year"),
                    venue=str(row.get("journal_or_venue") or row.get("venue") or ""),
                    abstract=str(row.get("abstract_or_snippet") or row.get("abstract") or ""),
                )
                for row in self.last_openalex_records[:limit]
            ]
            return converted, _SearchResponse(200)
        self.last_search_provider = ""
        # A successful empty result is exhaustion only if both providers worked.
        final_status = s2_status if s2_status is None or not 200 <= s2_status < 300 else oa_status
        return [], _SearchResponse(final_status)

    def search_snippets(self, query: str, *, limit: int = 20, **kwargs: Any):
        self.last_provider_audit = []
        self.last_openalex_records = []
        self.last_search_provider = "semantic_scholar_snippets"
        try:
            rows, response = self.s2_gateway.search_snippets(query, limit=limit, **kwargs)
            status = self._status(response)
            self.last_provider_audit = [{
                "provider": "semantic_scholar_snippets",
                "status_code": status,
                "returned": len(rows or []),
                "retryable": status is None or status in {408, 425, 429} or status >= 500,
            }]
            return rows, _SearchResponse(status)
        except Exception as exc:
            self.last_provider_audit = [{
                "provider": "semantic_scholar_snippets",
                "status_code": None,
                "returned": 0,
                "error": type(exc).__name__,
                "retryable": True,
            }]
            return [], _SearchResponse(None)


@dataclass(frozen=True)
class PlanningSupplementRequest:
    request_id: str
    topic_id: str
    gap_id: str
    gap_question: str
    success_criteria: tuple[str, ...]
    base_pool_path: Path
    plan_path: Path
    targeted_queries: tuple[dict[str, str], ...] = ()
    reuse_plan_facet_ids: tuple[str, ...] = ()
    known_papers: tuple[dict[str, Any], ...] = ()
    reviewed_references: tuple[dict[str, Any], ...] = ()
    required_outputs: tuple[str | dict[str, Any], ...] = ()
    reusable_material: str = ""
    still_missing: str = ""
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_acquisitions: int = DEFAULT_MAX_ACQUISITIONS
    per_query_limit: int = DEFAULT_PER_QUERY_LIMIT

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        base_dir: str | Path = ".",
    ) -> "PlanningSupplementRequest":
        if not isinstance(raw, Mapping):
            raise PlanningSupplementError("request_must_be_object")
        allowed = {
            "schema_version", "request_id", "topic_id", "gap_id",
            "gap_question", "success_criteria", "base_pool_path", "plan_path",
            "targeted_queries", "reuse_plan_facet_ids", "known_papers",
            "reviewed_references", "required_outputs", "reusable_material", "still_missing", "limits",
        }
        unknown = sorted(str(key) for key in raw if key not in allowed)
        if unknown:
            raise PlanningSupplementError("request_unknown_keys:" + ",".join(unknown))
        if raw.get("schema_version") != REQUEST_SCHEMA:
            raise PlanningSupplementError("request_schema_version")

        def required_text(key: str) -> str:
            value = str(raw.get(key) or "").strip()
            if not value:
                raise PlanningSupplementError("request_field_empty:" + key)
            return value

        root = Path(base_dir).resolve()

        def request_path(key: str) -> Path:
            value = str(raw.get(key) or "").strip()
            if not value:
                raise PlanningSupplementError("request_field_empty:" + key)
            path = Path(value)
            return (path if path.is_absolute() else root / path).resolve()

        criteria_raw = raw.get("success_criteria")
        if isinstance(criteria_raw, str):
            criteria = (criteria_raw.strip(),) if criteria_raw.strip() else ()
        elif isinstance(criteria_raw, Sequence) and not isinstance(criteria_raw, (str, bytes)):
            criteria = tuple(str(item or "").strip() for item in criteria_raw if str(item or "").strip())
        else:
            criteria = ()
        if not criteria:
            raise PlanningSupplementError("success_criteria_required")

        required_outputs_raw = raw.get("required_outputs") or ()
        if not isinstance(required_outputs_raw, Sequence) or isinstance(required_outputs_raw, (str, bytes)):
            raise PlanningSupplementError("required_outputs_must_be_array")
        required_outputs: list[str | dict[str, Any]] = []
        for item in required_outputs_raw:
            if isinstance(item, Mapping):
                required_outputs.append(dict(item))
            elif isinstance(item, str) and item.strip():
                required_outputs.append(item.strip())
            else:
                raise PlanningSupplementError("required_output_must_be_text_or_object")
        reusable_material = raw.get("reusable_material") or ""
        if not isinstance(reusable_material, str):
            raise PlanningSupplementError("reusable_material_must_be_text")
        still_missing = raw.get("still_missing") or ""
        if not isinstance(still_missing, str):
            raise PlanningSupplementError("still_missing_must_be_text")

        targeted: list[dict[str, str]] = []
        for index, item in enumerate(raw.get("targeted_queries") or (), start=1):
            if not isinstance(item, Mapping):
                raise PlanningSupplementError(f"targeted_query_not_object:{index}")
            text = str(item.get("query_text") or "").strip()
            kind = str(item.get("query_type") or "").strip().casefold()
            facet = str(item.get("facet_id") or "").strip()
            if not text or kind not in {"keyword", "question"} or not facet:
                raise PlanningSupplementError(f"targeted_query_fields:{index}")
            row = {"query_text": text, "query_type": kind, "facet_id": facet}
            # A narrow question raised by a planning unit (a gap) is attached to
            # the real plan facet it belongs to; the gap itself keeps its own id
            # and is never fabricated as a facet.
            gap_id = str(item.get("gap_id") or "").strip()
            if gap_id:
                row["gap_id"] = gap_id
            purpose = str(item.get("intended_use") or item.get("purpose") or "").strip()
            if purpose:
                row["intended_use"] = purpose
            targeted.append(row)
        if len(targeted) > MAX_TARGETED_QUERIES:
            raise PlanningSupplementError("too_many_targeted_queries")

        reused = tuple(dict.fromkeys(
            str(item or "").strip()
            for item in (raw.get("reuse_plan_facet_ids") or ())
            if str(item or "").strip()
        ))
        if len(reused) > MAX_TARGETED_QUERIES:
            raise PlanningSupplementError("too_many_reused_facets")

        def records(key: str) -> tuple[dict[str, Any], ...]:
            value = raw.get(key) or ()
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise PlanningSupplementError(key + "_must_be_array")
            rows = []
            for index, item in enumerate(value, start=1):
                if not isinstance(item, Mapping):
                    raise PlanningSupplementError(f"{key}_item_not_object:{index}")
                row = dict(item)
                doi = normalize_doi(row.get("doi"))
                has_handle = bool(
                    str(row.get("canonical_paper_id") or row.get("paper_id") or row.get("paperId") or "").strip()
                    or doi
                    or str(row.get("corpus_id") or row.get("corpusId") or "").strip()
                    or str(row.get("openalex_id") or row.get("openalexId") or "").strip()
                    or str(row.get("arxiv_id") or row.get("arxivId") or "").strip()
                )
                if not has_handle:
                    raise PlanningSupplementError(f"{key}_item_missing_identity:{index}")
                if key == "reviewed_references":
                    # Ordinary citation requests need the cited work and the
                    # review account; internal row handles are convenient but
                    # are not required input from the caller.
                    row.setdefault("reference_id", f"review-ref-{index:02d}")
                    row.setdefault("reviewed_source_unit_id", f"review:{row['reference_id']}")
                rows.append(row)
            return tuple(rows)

        limits = raw.get("limits") or {}
        if not isinstance(limits, Mapping):
            raise PlanningSupplementError("limits_must_be_object")
        if set(limits) - {"max_candidates", "max_acquisitions", "per_query_limit"}:
            raise PlanningSupplementError("limits_unknown_keys")

        def bounded_int(key: str, default: int, low: int, high: int) -> int:
            value = limits.get(key, default)
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise PlanningSupplementError("limit_not_integer:" + key) from exc
            if parsed < low or parsed > high:
                raise PlanningSupplementError("limit_out_of_range:" + key)
            return parsed

        return cls(
            request_id=required_text("request_id"),
            topic_id=required_text("topic_id"),
            gap_id=required_text("gap_id"),
            gap_question=required_text("gap_question"),
            success_criteria=criteria,
            base_pool_path=request_path("base_pool_path"),
            plan_path=request_path("plan_path"),
            targeted_queries=tuple(targeted),
            reuse_plan_facet_ids=reused,
            known_papers=records("known_papers"),
            reviewed_references=records("reviewed_references"),
            required_outputs=tuple(required_outputs),
            reusable_material=reusable_material,
            still_missing=still_missing,
            max_candidates=bounded_int("max_candidates", DEFAULT_MAX_CANDIDATES, 1, MAX_CANDIDATES_HARD),
            max_acquisitions=bounded_int("max_acquisitions", DEFAULT_MAX_ACQUISITIONS, 0, MAX_ACQUISITIONS_HARD),
            per_query_limit=bounded_int("per_query_limit", DEFAULT_PER_QUERY_LIMIT, 1, MAX_PER_QUERY_HARD),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REQUEST_SCHEMA,
            "request_id": self.request_id,
            "topic_id": self.topic_id,
            "gap_id": self.gap_id,
            "gap_question": self.gap_question,
            "success_criteria": list(self.success_criteria),
            "base_pool_path": str(self.base_pool_path),
            "plan_path": str(self.plan_path),
            "targeted_queries": [dict(item) for item in self.targeted_queries],
            "reuse_plan_facet_ids": list(self.reuse_plan_facet_ids),
            "known_papers": [dict(item) for item in self.known_papers],
            "reviewed_references": [dict(item) for item in self.reviewed_references],
            "required_outputs": list(self.required_outputs),
            "reusable_material": self.reusable_material,
            "still_missing": self.still_missing,
            "limits": {
                "max_candidates": self.max_candidates,
                "max_acquisitions": self.max_acquisitions,
                "per_query_limit": self.per_query_limit,
            },
        }

    def query_specs(self, plan: Mapping[str, Any]) -> list[QuerySpec]:
        """The exact queries this request will run, for audit and tests."""

        specs, _source = _query_specs(self, plan)
        return specs


@dataclass
class _Candidate:
    record: dict[str, Any]
    source_kind: str
    lineage: dict[str, Any]
    search_hits: list[dict[str, Any]] = field(default_factory=list)
    source_unit_id: str = ""
    identity_audit: dict[str, Any] = field(default_factory=dict)


def load_planning_supplement_request(path: str | Path) -> PlanningSupplementRequest:
    request_path = Path(path).resolve()
    try:
        raw = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanningSupplementError("request_file_unreadable") from exc
    return PlanningSupplementRequest.from_mapping(raw, base_dir=request_path.parent)


def _load_plan(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        raw_bytes = path.read_bytes()
        raw = json.loads(raw_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanningSupplementError("plan_file_unreadable") from exc
    if not isinstance(raw, Mapping):
        raise PlanningSupplementError("plan_must_be_object")
    wrapped = isinstance(raw.get("plan"), Mapping)
    plan_input = raw["plan"] if wrapped else raw
    report = validate_plan(plan_input, fields=load_field_dictionary())
    if not report.get("ok"):
        raise PlanningSupplementError("plan_invalid:" + json.dumps(report.get("errors") or [], ensure_ascii=False))
    wrapper_repairs = list(raw.get("repairs") or []) if wrapped else []
    wrapper_refused = list(raw.get("refused_queries") or []) if wrapped else []
    if wrapped and str(raw.get("status") or "").casefold() != "ok":
        raise PlanningSupplementError("plan_wrapper_status_not_ok:" + str(raw.get("status") or "missing"))
    if report.get("repairs") or report.get("refused_queries") or wrapper_repairs or wrapper_refused:
        raise PlanningSupplementError("plan_requires_repair_before_supplement")
    source_audit = {
        "source_format": "planning_run_wrapper" if wrapped else "plain_query_plan",
        "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }
    if wrapped:
        # Keep wrapper provenance, validation/degradation state, and refusal
        # audit visible in every supplement. Do not discard the package and
        # validate an inner object without recording its enclosing run state.
        for key in (
            "status", "first_hit", "revision_rounds", "revision_reason",
            "errors", "error_details", "transport", "degradation",
            "repairs", "prompt_hash", "validator_fingerprint", "refused_queries",
        ):
            source_audit[key] = raw.get(key)
    report = dict(report)
    report["source_audit"] = source_audit
    return dict(report["plan"]), report


def _load_pool(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise PlanningSupplementError("base_pool_unreadable") from exc
    rows: list[dict[str, Any]] = []
    canonical_seen: dict[str, int] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PlanningSupplementError(f"base_pool_invalid_json:{line_number}") from exc
        if not isinstance(row, Mapping):
            raise PlanningSupplementError(f"base_pool_row_not_object:{line_number}")
        value = dict(row)
        view = value.get("planning_view")
        if not isinstance(view, Mapping):
            raise PlanningSupplementError(f"base_pool_row_missing_planning_view:{line_number}")
        identity = view.get("paper_identity") if isinstance(view.get("paper_identity"), Mapping) else {}
        canonical = _norm_identity(identity.get("canonical_paper_id") or value.get("paper_id"))
        if not canonical:
            raise PlanningSupplementError(f"base_pool_row_missing_canonical_identity:{line_number}")
        if canonical in canonical_seen:
            raise PlanningSupplementError(
                f"base_pool_duplicate_canonical_identity:{canonical_seen[canonical]}:{line_number}"
            )
        canonical_seen[canonical] = line_number
        rows.append(value)
    return raw_bytes, rows


def _norm_identity(value: Any) -> str:
    return str(value or "").strip().casefold()


def _query_specs(request: PlanningSupplementRequest, plan: Mapping[str, Any]) -> tuple[list[QuerySpec], str]:
    facet_ids = {str(item.get("id") or "") for item in plan.get("facets") or () if isinstance(item, Mapping)}
    specs: list[QuerySpec] = []
    if request.targeted_queries:
        for index, row in enumerate(request.targeted_queries, start=1):
            facet_id = row["facet_id"]
            if facet_id not in facet_ids:
                raise PlanningSupplementError("targeted_query_unknown_facet:" + facet_id)
            kind = row["query_type"]
            specs.append(QuerySpec(
                query_id=f"SUP_{_safe_token(request.gap_id)}_{'KQ' if kind == 'keyword' else 'QQ'}_{index:02d}",
                facet_id=facet_id,
                query_type=kind,
                query_text=row["query_text"],
                retrieval_source=KEYWORD_SOURCE if kind == "keyword" else QUESTION_SOURCE,
            ))
        return specs, "planner_supplied_targeted_queries"
    if request.reuse_plan_facet_ids:
        unknown = set(request.reuse_plan_facet_ids) - facet_ids
        if unknown:
            raise PlanningSupplementError("reuse_unknown_plan_facet:" + ",".join(sorted(unknown)))
        selected = set(request.reuse_plan_facet_ids)
        specs = [spec for spec in plan_queries(plan) if spec.facet_id in selected]
        if specs:
            return specs[:MAX_TARGETED_QUERIES], "selected_existing_plan_facet_queries"
    return [], "no_targeted_query"


def _safe_token(value: Any) -> str:
    token = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_-")
    return (token[:36] or "gap")


def _query_audit_provider_state(audit: Mapping[str, Any]) -> tuple[bool, bool]:
    status = audit.get("status")
    issue = False
    retryable = False
    if isinstance(status, int):
        issue = status < 200 or status >= 300
        retryable = status in {408, 425, 429} or status >= 500
    normalized = str(status or "").casefold()
    if normalized in {"provider_error", "transport_error", "rate_limited", "timeout", "error"}:
        issue = True
        retryable = normalized != "error"
    elif status is None:
        issue, retryable = True, True
    for attempt in audit.get("provider_attempts", []) or ():
        if not isinstance(attempt, Mapping):
            continue
        attempt_status = attempt.get("status_code")
        if attempt_status is None or (isinstance(attempt_status, int) and (attempt_status < 200 or attempt_status >= 300)):
            issue = True
        retryable = retryable or bool(attempt.get("retryable"))
    return issue, retryable


def _interleave_query_channels(candidates: Sequence[_Candidate]) -> list[_Candidate]:
    """Keep keyword and semantic/snippet evidence represented under small caps."""

    queues: dict[str, list[_Candidate]] = {"keyword": [], "question": []}
    other: list[_Candidate] = []
    for candidate in candidates:
        query_types = set(candidate.lineage.get("query_types") or [])
        if "keyword" in query_types:
            queues["keyword"].append(candidate)
        if "question" in query_types:
            queues["question"].append(candidate)
        if not query_types:
            other.append(candidate)
    ordered: list[_Candidate] = []
    added: set[int] = set()
    indexes = {"keyword": 0, "question": 0}
    channel_turn = 0
    channels = ("keyword", "question")
    while indexes["keyword"] < len(queues["keyword"]) or indexes["question"] < len(queues["question"]):
        channel = channels[channel_turn % len(channels)]
        channel_turn += 1
        if indexes[channel] >= len(queues[channel]):
            other_channel = channels[channel_turn % len(channels)]
            if indexes[other_channel] >= len(queues[other_channel]):
                continue
            channel = other_channel
        candidate = queues[channel][indexes[channel]]
        indexes[channel] += 1
        marker = id(candidate)
        if marker not in added:
            ordered.append(candidate)
            added.add(marker)
    for candidate in [*other, *candidates]:
        marker = id(candidate)
        if marker not in added:
            ordered.append(candidate)
            added.add(marker)
    return ordered


def _retryable_exception(exc: BaseException) -> bool:
    """Classify transient transport failures without treating all errors alike."""

    transient = getattr(exc, "transient", None)
    if isinstance(transient, bool):
        return transient
    name = type(exc).__name__.casefold()
    return any(token in name for token in ("transport", "timeout", "connection", "temporar"))


def _acquisition_provider_state(acquisition: Any) -> tuple[bool, bool]:
    attempts = list(getattr(acquisition, "attempts", []) or [])
    identity = getattr(acquisition, "identity", None)
    provider_audit = getattr(identity, "provider_audit", []) if identity is not None else []
    issue = False
    retryable = False
    for item in [*attempts, *(provider_audit or [])]:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "").casefold()
        status_code = item.get("status_code")
        if status in {"provider_error", "transport_error", "rate_limited", "timeout", "error"}:
            issue = True
            retryable = retryable or status != "error"
        if isinstance(status_code, int) and (status_code < 200 or status_code >= 300):
            issue = True
            retryable = retryable or status_code in {408, 425, 429} or status_code >= 500
        retryable = retryable or bool(item.get("retryable"))
    return issue, retryable


def _query_specs_and_hits(
    request: PlanningSupplementRequest,
    plan: Mapping[str, Any],
    gateway: Any | None,
) -> tuple[list[dict[str, Any]], list[_Candidate], str]:
    specs, query_source = _query_specs(request, plan)
    query_audits: list[dict[str, Any]] = []
    grouped: dict[str, _Candidate] = {}
    corpus_owner: dict[str, str | None] = {}
    raw_counter = 0
    for spec in specs:
        if gateway is None:
            query_audits.append({
                "query_id": spec.query_id,
                "facet_id": spec.facet_id,
                "query_text": spec.query_text,
                "query_type": spec.query_type,
                "retrieval_source": spec.retrieval_source,
                "returned": 0,
                "status": "provider_error",
                "error": "gateway_not_configured",
                "provider_issue": True,
                "retryable": True,
            })
            continue
        try:
            hits, audit = harvest_query(
                gateway,
                spec,
                per_query_limit=request.per_query_limit,
                discovery_target=0,
            )
            row = dict(audit)
            provider_attempts = [
                dict(item) for item in getattr(gateway, "last_provider_audit", [])
                if isinstance(item, Mapping)
            ]
            if provider_attempts:
                row["provider_attempts"] = provider_attempts
            provider_issue, retryable = _query_audit_provider_state(row)
            row["provider_issue"] = provider_issue
            row["retryable"] = retryable
            query_audits.append(row)
        except Exception as exc:  # provider exceptions are auditable retryable failures
            query_audits.append({
                "query_id": spec.query_id,
                "facet_id": spec.facet_id,
                "query_text": spec.query_text,
                "query_type": spec.query_type,
                "retrieval_source": spec.retrieval_source,
                "returned": 0,
                "status": "provider_error",
                "error": type(exc).__name__,
                "provider_issue": True,
                "retryable": True,
            })
            continue
        openalex_records = list(getattr(gateway, "last_openalex_records", []) or [])
        if str(getattr(gateway, "last_search_provider", "")) == "openalex":
            for hit, openalex_row in zip(hits, openalex_records):
                openalex_id = str(openalex_row.get("openalex_id") or openalex_row.get("source_id") or "")
                if openalex_id:
                    hit.identifiers["openalex"] = openalex_id
                    hit.fields["openalex_id"] = openalex_id
                hit.row["provider"] = "openalex"
        elif getattr(gateway, "last_provider_audit", None):
            successful_provider = next(
                (row.get("provider") for row in gateway.last_provider_audit if row.get("returned")),
                "semantic_scholar",
            )
            for hit in hits:
                hit.row["provider"] = successful_provider
        for hit in hits:
            raw_counter += 1
            identifiers = dict(hit.identifiers or {})
            paper_id = str(identifiers.get("s2") or "").strip()
            corpus_id = str(identifiers.get("corpus") or "").strip()
            openalex_id = str(identifiers.get("openalex") or (hit.fields or {}).get("openalex_id") or "").strip()
            doi = normalize_doi(identifiers.get("doi"))
            # Distinct provider IDs remain distinct even when they share a DOI.
            # DOI is only a fallback grouping key when no provider handle exists.
            key = ""
            if paper_id:
                key = "s2:" + paper_id
                owner = corpus_owner.get(corpus_id) if corpus_id else None
                if owner and owner.startswith("corpus:") and owner in grouped and key not in grouped:
                    # A snippet hit often has only CorpusId.  It can be attached
                    # to the S2 row with the exact same corpus handle.
                    grouped[key] = grouped.pop(owner)
                    grouped[key].record["canonical_paper_id"] = paper_id
                    grouped[key].record["paper_id"] = paper_id
                    corpus_owner[corpus_id] = key
                elif owner and owner != key:
                    # A conflicting provider ID on one corpus handle stays as a
                    # separate candidate with the conflict visible in lineage.
                    candidate_key = key
                    if candidate_key in grouped:
                        key = candidate_key
                    else:
                        key = candidate_key
                    corpus_owner[corpus_id] = None
                else:
                    corpus_owner[corpus_id] = key
            elif openalex_id:
                key = "openalex:" + openalex_id
            elif corpus_id:
                owner = corpus_owner.get(corpus_id)
                key = owner if owner and owner in grouped else "corpus:" + corpus_id
                if owner is None:
                    key = "corpus-conflict:" + corpus_id + ":" + str(raw_counter)
                corpus_owner.setdefault(corpus_id, key)
            elif doi:
                key = "doi:" + doi
            else:
                key = "unresolved:" + str(raw_counter)
            candidate = grouped.get(key)
            if candidate is None:
                fields = dict(hit.fields or {})
                canonical = paper_id or ("OpenAlex:" + openalex_id if openalex_id else "") or ("CorpusId:" + corpus_id if corpus_id else "") or doi
                record = {
                    "canonical_paper_id": canonical,
                    "paper_id": paper_id,
                    "corpus_id": corpus_id,
                    "openalex_id": openalex_id,
                    "doi": doi,
                    "title": str(fields.get("title") or ""),
                    "authors": list(fields.get("authors") or []),
                    "year": fields.get("year"),
                    "venue": str(fields.get("venue") or ""),
                    "abstract": str(fields.get("abstract") or ""),
                    "snippets": [],
                }
                candidate = _Candidate(
                    record=record,
                    source_kind="search_candidate",
                    lineage={"kind": "retrieval_query", "query_ids": [], "facet_ids": []},
                )
                grouped[key] = candidate
            elif paper_id:
                # Enrichment from paper-search may arrive after a snippet hit.
                # Only exact provider/corpus handles are used for this merge.
                candidate.record["paper_id"] = candidate.record.get("paper_id") or paper_id
                candidate.record["canonical_paper_id"] = candidate.record.get("canonical_paper_id") or paper_id
                candidate.record["doi"] = candidate.record.get("doi") or doi
                candidate.record["corpus_id"] = candidate.record.get("corpus_id") or corpus_id
                for field_name in ("title", "authors", "year", "venue", "abstract"):
                    if not candidate.record.get(field_name) and (hit.fields or {}).get(field_name):
                        candidate.record[field_name] = (hit.fields or {}).get(field_name)
            if not candidate.record.get("abstract") and (hit.fields or {}).get("abstract"):
                candidate.record["abstract"] = str((hit.fields or {}).get("abstract") or "")
            candidate.lineage["query_ids"] = list(dict.fromkeys([
                *candidate.lineage.get("query_ids", []), str(hit.row.get("query_id") or "")
            ]))
            candidate.lineage["facet_ids"] = list(dict.fromkeys([
                *candidate.lineage.get("facet_ids", []), str(hit.row.get("facet_id") or "")
            ]))
            candidate.lineage["query_types"] = list(dict.fromkeys([
                *candidate.lineage.get("query_types", []), str(hit.row.get("query_type") or "")
            ]))
            candidate.lineage["providers"] = list(dict.fromkeys([
                *candidate.lineage.get("providers", []), str(hit.row.get("provider") or "semantic_scholar")
            ]))
            hit_id = f"SUP-H{raw_counter:06d}"
            locator = dict(hit.row.get("snippet_locator") or {})
            raw_text = str(hit.raw_snippet_text or "")
            hit_record = {
                "hit_id": hit_id,
                "paper_id": paper_id,
                "corpus_id": corpus_id,
                "openalex_id": openalex_id,
                "doi": doi,
                "title": str((hit.fields or {}).get("title") or ""),
                "authors": list((hit.fields or {}).get("authors") or []),
                "year": (hit.fields or {}).get("year"),
                "venue": str((hit.fields or {}).get("venue") or ""),
                "abstract_text": str((hit.fields or {}).get("abstract") or ""),
                "provider": str(hit.row.get("provider") or "semantic_scholar"),
                "facet_id": str(hit.row.get("facet_id") or ""),
                "query_id": str(hit.row.get("query_id") or ""),
                "query_type": str(hit.row.get("query_type") or ""),
                "query_text": str(hit.row.get("query_text") or ""),
                "retrieval_source": str(hit.row.get("retrieval_source") or ""),
                "result_rank": hit.row.get("result_rank"),
                "request_index": hit.row.get("request_index", 1),
                "score": hit.row.get("score"),
                "snippet_locator": locator,
                "text_hash": str(hit.row.get("text_hash") or ""),
                "raw_snippet_text": raw_text,
            }
            candidate.search_hits.append(hit_record)
            if raw_text:
                candidate.record["snippets"].append({
                    "hit_id": hit_id,
                    "text": raw_text,
                    "snippet_locator": locator,
                    "snippet_kind": str(locator.get("snippet_kind") or "body"),
                    "retrieval_source": str(hit.row.get("retrieval_source") or "snippet_search"),
                })
    return query_audits, _interleave_query_channels(list(grouped.values())), query_source


def _explicit_candidates(request: PlanningSupplementRequest) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for row in request.known_papers:
        record = dict(row)
        record.setdefault("canonical_paper_id", record.get("paper_id") or record.get("paperId") or record.get("doi"))
        candidates.append(_Candidate(
            record=record,
            source_kind="known_paper",
            lineage={
                "kind": "known_paper_material_upgrade",
                "source_unit_id": str(row.get("source_unit_id") or ""),
                "original_pool_paper_id": str(row.get("original_pool_paper_id") or ""),
            },
        ))
    for row in request.reviewed_references:
        record = {key: value for key, value in row.items() if key not in {
            "reference_id", "reviewed_source_unit_id", "citation_text", "source_claim",
            "lineage_note", "source_unit_id", "review_claim", "review_source_quote",
            "review_snapshot_dir", "review_card_path", "review_block_id",
        }}
        record.setdefault("canonical_paper_id", record.get("paper_id") or record.get("paperId") or record.get("doi"))
        # The cited review's prose is lineage, never the cited paper's abstract.
        record.pop("abstract", None)
        record.pop("snippets", None)
        candidates.append(_Candidate(
            record=record,
            source_kind="reviewed_reference",
            lineage={key: value for key, value in {
                "kind": "secondary_reference_to_direct_source",
                "reference_id": str(row.get("reference_id") or ""),
                "reviewed_source_unit_id": str(row.get("reviewed_source_unit_id") or ""),
                "citation_text": str(row.get("citation_text") or ""),
                "review_claim": str(row.get("review_claim") or row.get("source_claim") or ""),
                "review_source_quote": str(row.get("review_source_quote") or ""),
                "review_snapshot_dir": str(row.get("review_snapshot_dir") or ""),
                "review_card_path": str(row.get("review_card_path") or ""),
                "review_block_id": str(row.get("review_block_id") or ""),
                "lineage_note": str(row.get("lineage_note") or ""),
            }.items() if value not in (None, "")},
        ))
    return candidates


def _pool_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    view = row.get("planning_view") if isinstance(row.get("planning_view"), Mapping) else {}
    identity = view.get("paper_identity") if isinstance(view.get("paper_identity"), Mapping) else {}
    return {
        "canonical_paper_id": identity.get("canonical_paper_id") or row.get("paper_id"),
        "paper_id": row.get("paper_id"),
        "doi": identity.get("doi"),
        "title": identity.get("title"),
        "year": identity.get("year"),
        "publication_version": identity.get("publication_version"),
    }


def _candidate_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "canonical_paper_id": record.get("canonical_paper_id") or record.get("paper_id") or record.get("paperId"),
        "paper_id": record.get("paper_id") or record.get("paperId"),
        "doi": record.get("doi"),
        "corpus_id": record.get("corpus_id") or record.get("corpusId"),
        "semantic_scholar_paper_id": record.get("semantic_scholar_paper_id") or record.get("paperId"),
        "openalex_id": record.get("openalex_id") or record.get("openalexId"),
        "arxiv_id": record.get("arxiv_id") or record.get("arxivId"),
        "title": record.get("title"),
        "authors": record.get("authors") or record.get("author"),
        "year": record.get("year") or record.get("yearPublished"),
    }


def _merge_candidate_sources(candidates: Sequence[_Candidate]) -> list[_Candidate]:
    """Acquire a paper once while retaining its different discovery passages."""
    merged: list[_Candidate] = []
    for candidate in candidates:
        if candidate.source_kind == "reviewed_reference":
            merged.append(candidate)
            continue
        identity = _candidate_identity(candidate.record)
        target = None
        for previous in merged:
            if previous.source_kind == "reviewed_reference":
                continue
            other = _candidate_identity(previous.record)
            comparison = match_identity(other, identity)
            same_id = bool(identity.get("canonical_paper_id")) and (
                _norm_identity(identity["canonical_paper_id"]) == _norm_identity(other.get("canonical_paper_id"))
            )
            same_doi = bool(normalize_doi(identity.get("doi"))) and normalize_doi(identity.get("doi")) == normalize_doi(other.get("doi"))
            if not comparison.get("conflicts") and (same_id or same_doi or comparison.get("verified")):
                target = previous
                break
        if target is None:
            merged.append(candidate)
            continue
        for key, value in candidate.record.items():
            if value and not target.record.get(key):
                target.record[key] = value
        for key in ("snippets",):
            values = [*(target.record.get(key) or []), *(candidate.record.get(key) or [])]
            if values:
                target.record[key] = list({json.dumps(item, sort_keys=True, default=str): item for item in values}.values())
        hit_ids = {hit.get("hit_id") for hit in target.search_hits}
        target.search_hits.extend(hit for hit in candidate.search_hits if hit.get("hit_id") not in hit_ids)
        target.lineage.setdefault("additional_discoveries", []).append({"source_kind": candidate.source_kind, **candidate.lineage})
    return merged


def _identity_match(candidate: _Candidate, pool_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    identity = _candidate_identity(candidate.record)
    candidate_canonical = _norm_identity(identity.get("canonical_paper_id"))
    matches: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    canonical_conflicts: list[dict[str, Any]] = []
    for row in pool_rows:
        pool_identity = _pool_identity(row)
        same_canonical = candidate_canonical and candidate_canonical == _norm_identity(pool_identity.get("canonical_paper_id"))
        comparison = match_identity(pool_identity, identity)
        if same_canonical and not comparison.get("conflicts"):
            matches.append({"paper_id": row.get("paper_id"), "basis": "canonical_paper_id", "comparison": comparison})
        elif same_canonical and comparison.get("conflicts"):
            canonical_conflicts.append({"paper_id": row.get("paper_id"), "comparison": comparison})
        elif comparison.get("verified") and comparison.get("state") == "verified":
            matches.append({"paper_id": row.get("paper_id"), "basis": comparison.get("basis"), "comparison": comparison})
        elif comparison.get("conflicts") and (
            normalize_doi(pool_identity.get("doi"))
            and normalize_doi(pool_identity.get("doi")) == normalize_doi(identity.get("doi"))
        ):
            conflicts.append({"paper_id": row.get("paper_id"), "comparison": comparison})
    if len(matches) == 1:
        return {"decision": "matched_existing", "match": matches[0], "conflicts": conflicts}
    if len(matches) > 1:
        return {"decision": "ambiguous_existing_match", "matches": matches, "conflicts": conflicts}
    if conflicts or canonical_conflicts:
        return {"decision": "identity_conflict_unresolved", "matches": [], "conflicts": [*conflicts, *canonical_conflicts]}
    return {"decision": "new_identity", "matches": [], "conflicts": []}


def _snapshot_reading_text(snapshot: Any) -> str:
    text = str(getattr(snapshot, "reading_view", "") or "")
    if text:
        return text
    root = getattr(snapshot, "root", None)
    if root:
        for name in ("READING_VIEW.md", "reading_view.md", "READING_VIEW.txt"):
            path = Path(root) / name
            if path.is_file():
                try:
                    return path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
    return ""


def _card_body(card_result: Any) -> dict[str, Any]:
    result = dict(card_result) if isinstance(card_result, Mapping) else {}
    card = result.get("card") if isinstance(result.get("card"), Mapping) else result
    return dict(card) if isinstance(card, Mapping) else {}


_QUOTE_ELLIPSIS_RE = re.compile(r"(?:\.{3,}|…+|⋯+|\[\s*(?:\.{3,}|…+|⋯+)\s*\])")
_QUOTE_DASHES = str.maketrans({char: "-" for char in "‐‑‒–—―﹘﹣－"})


def _normalized_with_offsets(text: str) -> tuple[str, list[int], list[int]]:
    """Whitespace/dash-normalized text with offsets back into original text."""

    normalized: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for index, char in enumerate(text):
        mapped = char.translate(_QUOTE_DASHES).casefold()
        for output_char in mapped:
            if output_char.isspace():
                if not normalized or normalized[-1] == " ":
                    if normalized:
                        ends[-1] = index + 1
                    continue
                output_char = " "
            normalized.append(output_char)
            starts.append(index)
            ends.append(index + 1)
    return "".join(normalized), starts, ends


def _longest_quote_anchor(quote: str, reading_text: str) -> tuple[str, dict[str, Any]] | None:
    """Return the longest contiguous source span retained in an ellipsized quote."""

    required_chars = max(40, int(math.ceil(len(quote) * 0.2)))
    if quote in reading_text:
        start = reading_text.find(quote)
        return quote, {
            "method": "exact",
            "source_span": {"start": start, "end": start + len(quote)},
            "matched_chars": len(quote),
            "original_quote_chars": len(quote),
            "match_ratio": 1.0,
            "required_chars": required_chars,
            "passed_threshold": len(quote) >= required_chars,
        }
    source_normalized, source_starts, source_ends = _normalized_with_offsets(reading_text)
    fragments = [part.strip().strip(" \t\r\n,;:") for part in _QUOTE_ELLIPSIS_RE.split(quote)]
    fragments = [part for part in fragments if part]
    if not fragments:
        fragments = [quote.strip()]
    choices: list[tuple[int, int, int, str]] = []
    for fragment in fragments:
        normalized_fragment, _, _ = _normalized_with_offsets(fragment)
        normalized_fragment = normalized_fragment.strip()
        if not normalized_fragment:
            continue
        offset = source_normalized.find(normalized_fragment)
        if offset < 0:
            continue
        end_normalized = offset + len(normalized_fragment)
        if offset >= len(source_starts) or end_normalized - 1 >= len(source_ends):
            continue
        source_start = source_starts[offset]
        source_end = source_ends[end_normalized - 1]
        exact_source_span = reading_text[source_start:source_end]
        choices.append((len(normalized_fragment), source_start, source_end, exact_source_span))
    if not choices:
        return None
    matched_chars, start, end, exact_span = max(choices, key=lambda row: (row[0], row[2] - row[1]))
    ratio = (end - start) / max(1, len(quote))
    return exact_span, {
        "method": "longest_contiguous_ellipsis_segment",
        "source_span": {"start": start, "end": end},
        "matched_chars": end - start,
        "matched_normalized_chars": matched_chars,
        "original_quote_chars": len(quote),
        "match_ratio": ratio,
        "required_chars": required_chars,
        "passed_threshold": end - start >= required_chars,
        "matched_fragment_count": len(choices),
    }


def _validate_fulfillment_judgment_legacy(
    raw: Any,
    *,
    reading_text: str,
    gap_question: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return _unjudgeable("judge_returned_non_object", gap_question)
    status = str(raw.get("status") or "").strip().casefold()
    if status not in JUDGMENT_STATUSES:
        return _unjudgeable("judge_status_invalid", gap_question)
    evidence = raw.get("evidence") or []
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        return _unjudgeable("judge_evidence_must_be_array", gap_question)
    normalized_evidence = []
    quote_anchor_failures: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        quote = str(item.get("quote") or "").strip()
        if not quote:
            return _unjudgeable("judge_quote_empty", gap_question)
        anchor = _longest_quote_anchor(quote, reading_text)
        if anchor is None:
            quote_anchor_failures.append({
                "original_quote": quote,
                "repair_quote": "",
                "match_ratio": 0.0,
                "required_chars": max(40, int(math.ceil(len(quote) * 0.2))),
                "passed_threshold": False,
                "reason": "no_contiguous_quote_segment_found",
            })
            continue
        anchored_quote, quote_audit = anchor
        quote_audit["passed_threshold"] = bool(quote_audit.get("passed_threshold", True))
        quote_audit.update({
            "original_quote": quote,
            "repair_quote": anchored_quote if anchored_quote != quote else "",
        })
        if not quote_audit["passed_threshold"]:
            quote_anchor_failures.append(quote_audit)
            continue
        locator = item.get("locator") if isinstance(item.get("locator"), Mapping) else ({"description": item.get("locator")} if item.get("locator") else {})
        normalized_evidence.append({
            "quote": anchored_quote,
            "original_quote": quote,
            "quote_anchor_audit": quote_audit,
            "locator": dict(locator),
            "interpretation": str(item.get("interpretation") or "").strip(),
        })
    if quote_anchor_failures:
        return {
            **_unjudgeable("judge_quote_anchor_below_threshold", gap_question),
            "quote_anchor_failures": quote_anchor_failures,
        }
    if status in {"fulfilled", "partial"} and not normalized_evidence:
        return _unjudgeable("judge_claimed_support_without_snapshot_evidence", gap_question)
    criterion_assessments: list[dict[str, Any]] = []
    raw_criteria = raw.get("criterion_assessments")
    criterion_gap = ""
    if raw_criteria is not None:
        if not isinstance(raw_criteria, Sequence) or isinstance(raw_criteria, (str, bytes)):
            return _unjudgeable("judge_criterion_assessments_must_be_array", gap_question)
        allowed_criterion_statuses = {"supported", "partly_supported", "unresolved", "not_addressed", "contradicted"}
        for row in raw_criteria:
            if not isinstance(row, Mapping) or not str(row.get("criterion") or "").strip():
                continue
            criterion_status = str(row.get("status") or "").strip().casefold()
            if criterion_status not in allowed_criterion_statuses:
                return _unjudgeable("judge_criterion_status_invalid", gap_question)
            indices = row.get("evidence_indices") or []
            if not isinstance(indices, Sequence) or isinstance(indices, (str, bytes)):
                return _unjudgeable("judge_criterion_evidence_indices_must_be_array", gap_question)
            clean_indices = [
                value for value in indices
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(normalized_evidence)
            ]
            if criterion_status in {"supported", "partly_supported"} and not clean_indices:
                # A positive criterion verdict needs an explicit link to an
                # independently anchored evidence quote. Otherwise it is not
                # safe to count as a useful contribution toward the gap.
                criterion_status = "unresolved"
            criterion_assessments.append({
                "criterion": str(row.get("criterion") or "").strip(),
                "status": criterion_status,
                "rationale": str(row.get("rationale") or "").strip(),
                "evidence_indices": sorted(set(clean_indices)),
            })
        unresolved_criteria = [
            row["criterion"] for row in criterion_assessments
            if row["status"] in {"partly_supported", "unresolved", "not_addressed", "contradicted"}
        ]
        useful_criteria = [
            row for row in criterion_assessments
            if row["status"] in {"supported", "partly_supported"} and row["evidence_indices"]
        ]
        if status == "fulfilled" and unresolved_criteria:
            status = "partial" if useful_criteria else "unjudgeable"
            criterion_gap = "Remaining success criteria: " + "; ".join(unresolved_criteria)
        elif status == "partial" and not useful_criteria:
            status = "unjudgeable"
            criterion_gap = "No useful criterion contribution was linked to anchored evidence."
    limitations = raw.get("limitations") or []
    if isinstance(limitations, str):
        limitations = [limitations]
    if not isinstance(limitations, Sequence) or isinstance(limitations, (bytes, str)):
        return _unjudgeable("judge_limitations_must_be_array", gap_question)
    remaining_gap_value = raw.get("remaining_gap")
    remaining_gap = str(remaining_gap_value or "").strip()
    if criterion_gap:
        remaining_gap = criterion_gap
    remaining_gap_recovered_by_policy = status == "fulfilled" and "remaining_gap" in raw and remaining_gap_value is None
    if remaining_gap_recovered_by_policy:
        # For an explicit fulfilled judgment, JSON null is the model's
        # statement that no gap remains; preserve it as empty text rather than
        # inventing scientific content.
        remaining_gap = ""
    outline_action = str(raw.get("outline_action") or "").strip()
    if not remaining_gap and not remaining_gap_recovered_by_policy:
        return {
            "status": "unjudgeable",
            "reason": "judge_remaining_gap_missing",
            "rationale": str(raw.get("rationale") or "").strip(),
            "evidence": normalized_evidence,
            "limitations": [str(item or "").strip() for item in limitations if str(item or "").strip()],
            "remaining_gap": "",
            "outline_action": "Keep the gap open until the judgment supplies its remaining-gap assessment.",
            "evidence_checked_against_snapshot": True,
        }
    outline_action_recovered_by_policy = False
    if not outline_action and status == "fulfilled":
        outline_action = "Use this source for the specified gap only, retaining stated limitations."
        outline_action_recovered_by_policy = True
    elif not outline_action and status == "partial":
        outline_action = "Keep gap open."
        outline_action_recovered_by_policy = True
    if not outline_action:
        return _unjudgeable("judge_outline_action_missing", gap_question)
    result = {
        "status": status,
        "rationale": str(raw.get("rationale") or "").strip(),
        "evidence": normalized_evidence,
        "limitations": [str(item or "").strip() for item in limitations if str(item or "").strip()],
        "remaining_gap": remaining_gap,
        "remaining_gap_recovered_by_policy": remaining_gap_recovered_by_policy,
        "outline_action": outline_action,
        "outline_action_recovered_by_policy": outline_action_recovered_by_policy,
        "evidence_checked_against_snapshot": True,
    }
    if raw_criteria is not None:
        result["criterion_assessments"] = criterion_assessments
        result["status_adjusted_for_unresolved_criteria"] = bool(criterion_gap)
        result["material_ready"] = bool(useful_criteria) or status == "fulfilled"
    else:
        result["material_ready"] = status in {"fulfilled", "partial"}
    return result


def normalize_practical_fulfillment_judgment(
    raw: Any,
    *,
    gap_question: str = "",
) -> dict[str, Any]:
    """Normalize the useful writing material and gap judgment independently.

    This is deliberately a shape adapter, not a scientific validator: it does
    not match quotations, inspect source handles, or require every output field
    before retaining useful prose.
    """

    content, plain_text = decode_practical_json(raw)
    if not isinstance(content, Mapping):
        content = {}
    raw_status = str(content.get("primary_gap_status") or content.get("substantive_gap_status") or content.get("status") or "").strip().casefold()
    # A completed invocation is not itself an answer to the research question.
    aliases = {"partly_fulfilled": "partial", "not_fulfilled": "unmet"}
    status = aliases.get(raw_status, raw_status)
    if status not in {"fulfilled", "partial", "unmet", "unjudgeable"}:
        status = "fulfilled" if content.get("fulfilled") is True else "unjudgeable"

    useful = next((content[key] for key in (
        "useful_material", "usable_content", "direct_contribution", "contribution", "explanation", "answer", "response"
    ) if content.get(key)), "")
    if not useful:
        useful = {
            key: content[key]
            for key in ("explanation", "summary", "mechanisms", "details")
            if content.get(key)
        }
    auxiliary = content.get("auxiliary_material", content.get("supporting_material", content.get("examples", content.get("supporting_findings", content.get("evidence", [])))))
    if plain_text and not useful:
        useful = plain_text

    def present(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, Mapping):
            return any(present(item) for item in value.values())
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return any(present(item) for item in value)
        return value is not None and value is not False

    usefulness = str(content.get("material_usefulness") or "").strip().casefold()
    if usefulness not in {"direct", "supporting", "none"}:
        usefulness = "direct" if present(useful) else ("supporting" if present(auxiliary) else "none")
    material_ready = usefulness in {"direct", "supporting"} and (present(useful) or present(auxiliary))
    rationale = str(content.get("rationale") or "").strip()
    limitations_value = content.get("limitations") or []
    if isinstance(limitations_value, str):
        limitations_value = [limitations_value] if limitations_value.strip() else []
    elif not isinstance(limitations_value, Sequence) or isinstance(limitations_value, (bytes, bytearray)):
        limitations_value = [str(limitations_value)] if limitations_value else []
    remaining_gap = str(content.get("remaining_gap") or content.get("unresolved_gap") or content.get("still_missing") or "").strip()
    assessments = content.get("criterion_assessments") or []
    if isinstance(assessments, Mapping):
        assessments = [assessments]
    unresolved_criteria = any(
        isinstance(row, Mapping) and (
            str(row.get("status") or "").strip().casefold() in {"partial", "unmet", "unjudgeable", "failed", "not_fulfilled"}
            or row.get("fulfilled") is False
        )
        for row in assessments
    ) if isinstance(assessments, Sequence) and not isinstance(assessments, (str, bytes)) else False
    if status == "fulfilled":
        if not material_ready:
            status = "unjudgeable"
        elif remaining_gap or unresolved_criteria or content.get("fulfilled") is False or content.get("answers_requested_question") is False:
            status = "partial"
    if not remaining_gap and status in {"unmet", "partial", "unjudgeable"}:
        remaining_gap = str(gap_question or "").strip()
    outline_action = str(content.get("outline_action") or content.get("writing_action") or "").strip()
    if not outline_action:
        outline_action = {
            "fulfilled": "Use the direct contribution in the relevant section, preserving its stated scope.",
            "partial": "Use the useful contribution and leave the unresolved part of the gap open.",
            "unmet": "Keep any useful supporting material available; leave the main gap unresolved.",
            "unjudgeable": "Retain available material for review and assess the main gap separately.",
        }[status]

    def rows(value: Any) -> list[Any]:
        if isinstance(value, Mapping):
            return [dict(value)]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return list(value)
        if isinstance(value, str) and value.strip():
            return [value]
        return []

    normalized = {
        "status": status,
        "substantive_gap_status": status,
        "material_ready": material_ready,
        "material_usefulness": usefulness,
        "useful_material": useful,
        "auxiliary_material": auxiliary,
        "remaining_gap": remaining_gap,
        "outline_action": outline_action,
    }
    if rationale:
        normalized["rationale"] = rationale
    if "limitations" in content:
        normalized["limitations"] = list(limitations_value)
    if "criterion_assessments" in content:
        normalized["criterion_assessments"] = rows(content.get("criterion_assessments"))
    if "evidence" in content:
        normalized["evidence"] = rows(content.get("evidence"))
    if content.get("title"):
        normalized["title"] = content.get("title")
    # Transport/run diagnostics remain visible even when optional gap metadata
    # is absent. Useful material and research fulfillment stay independent.
    execution_status = str(content.get("execution_status") or content.get("status") or "").strip().casefold()
    if execution_status in {"complete", "completed", "failed", "provider_failed", "provider_error", "error"}:
        normalized["execution_status"] = execution_status
    for key in ("error", "reason", "failure_stage", "retryable", "provider_failed"):
        if key in content:
            normalized[key] = content[key]
    return normalized


def _validate_fulfillment_judgment(
    raw: Any,
    *,
    reading_text: str = "",
    gap_question: str = "",
) -> dict[str, Any]:
    """Historical call name for the default practical material normalizer."""

    return normalize_practical_fulfillment_judgment(raw, gap_question=gap_question)


def _unjudgeable(reason: str, gap_question: str) -> dict[str, Any]:
    return {
        "status": "unjudgeable",
        "reason": reason,
        "material_ready": False,
        "material_usefulness": "none",
        "useful_material": "",
        "auxiliary_material": [],
        "remaining_gap": gap_question,
        "outline_action": "Keep the gap open until practical material is available.",
    }


def _gap_requirement_terms(criteria: Sequence[str]) -> set[str]:
    """Content words that a success criterion is actually asking for.

    Diagnostic helper only.  Work order 03 measured that a bag-of-words gate on
    these terms both misses real fulfilments and lets wrong-object fulfilments
    through, so the aggregate verdict is *not* rewritten from them; the measured
    failure is recorded in ``03_single_gap/REPORT.md`` instead.
    """

    words: set[str] = set()
    for criterion in criteria:
        for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", str(criterion or "")):
            token = token.casefold()
            if token not in _REQUIREMENT_STOPWORDS:
                words.add(token)
    return words


_REQUIREMENT_STOPWORDS = {
    "that", "with", "from", "into", "when", "which", "what", "have", "been", "this",
    "these", "those", "study", "studies", "report", "reported", "state", "stated",
    "value", "values", "measure", "measured", "measurement", "assay", "data",
    "explicit", "statement", "completed", "and", "the", "for", "its", "which",
}


def _success_criteria_audit(
    judgment: Mapping[str, Any],
    *,
    criteria: Sequence[str],
    gap_question: str,
) -> dict[str, Any]:
    """Report whether a fulfilled verdict names the object the criteria ask for.

    This is recorded as an audit note next to the judgment; it never rewrites
    the fulfilment status.
    """

    if not criteria:
        return {"checked": False}
    terms = _gap_requirement_terms(criteria)
    if not terms:
        return {"checked": False}
    blob = " ".join(
        str(judgment.get(key) or "") for key in ("useful_material", "auxiliary_material")
    ).casefold()
    missing = sorted(term for term in terms if term not in blob)
    return {
        "checked": True,
        "requirement_terms": sorted(terms),
        "unmentioned_terms": missing,
        "gap_question": gap_question,
        "status_at_check": str(judgment.get("status") or ""),
        "note": (
            "Informational only: a term may be missing because the material paraphrases it. "
            "A fulfilled verdict whose unmentioned terms include the criterion's own object still "
            "needs human review."
        ),
    }


def _make_judge_messages(
    *,
    gap: Mapping[str, Any],
    reading_text: str,
    card: Mapping[str, Any],
    source_unit: Mapping[str, Any],
) -> list[dict[str, str]]:
    system = (
        "You are preparing practical writing material for one literature-planning gap. Read the supplied "
        "captured paper text, gap question, success criteria, and the paper card's B/review-planning content. "
        "Treat the captured text as the main material and the card as useful orientation. Explain any direct "
        "contribution, then preserve useful supporting material such as mechanisms, comparisons, examples, "
        "and relevant conditions even when an essential part of the gap remains unresolved. Keep the primary "
        "gap judgment separate from material usefulness: use status fulfilled, partial, unmet, or unjudgeable; "
        "use material_usefulness direct, supporting, or none. Do not call the whole gap fulfilled while a stated "
        "success criterion remains open. First describe what the supplied paper itself studied and what it found; "
        "then explain how that material may help with the requested gap. Refer to the paper as 'this study' or "
        "'the authors' unless its identity is explicitly given in the supplied bibliographic metadata. Do not infer "
        "the paper's identity from its reference list. Keep each finding attached to the study, research object or material, method, "
        "conditions, and limits actually reported. Do not rename the study's method to fit the gap. Multiple data "
        "sources or different results across subsets or groups do not by themselves establish transfer to an independent "
        "setting; call a transfer or validation evaluation only when the supplied material describes that design. "
        "Do not infer study design, validation approach, limitations, or results from an abbreviation or from "
        "silence; say when the supplied material does not specify a detail. "
        "The gap question and success criteria describe what the planner wants, not facts about this or any "
        "other study. Never borrow a requested stage, design, measurement or result to complete a source description. "
        "A study-design label is used only when explicitly stated. A study mentioned as planned or ongoing "
        "has only the design and status explicitly stated in the supplied material. Failure to find a matching "
        "study here does not establish its absence from the literature. In remaining_gap and outline_action, "
        "describe what this material supports and what remains unanswered, not an unsupported field-wide absence. "
        "Review-reported original studies remain usable writing material with their review attribution; do not "
        "claim the original full text was read. Return one JSON object with status, material_usefulness, "
        "useful_material, auxiliary_material, remaining_gap, and outline_action. Avoid repeating the same "
        "material in both material fields. Do not produce a quotation ledger, source handles, evidence indices, "
        "per-criterion rubric, rationale, limitations, or a passage-matching report."
    )
    review_planning = card.get("review_planning") if isinstance(card.get("review_planning"), Mapping) else {}
    payload = {
        "gap": dict(gap),
        "source_unit": {
            "source_kind": source_unit.get("source_kind"),
            "material_depth": source_unit.get("material_depth"),
        },
        "review_planning_B": dict(review_planning),
        "material_reading_text": reading_text,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


class QwenFulfillmentJudge:
    """One direct Qwen judgment using the same durable CNY ledger as A/B cards."""

    def __init__(
        self,
        *,
        key_file: str | Path,
        budget_ledger_path: str | Path,
        budget_limit_cny: float,
        max_output_tokens: int = 3072,
        thinking_budget: int = 2048,
        timeout_seconds: float = 300.0,
        raw_response_dir: str | Path | None = None,
        model: str = "qwen3.5-plus",
    ) -> None:
        if not key_file or not budget_ledger_path:
            raise PlanningSupplementError("live_judge_key_and_ledger_required")
        if not math.isfinite(float(budget_limit_cny)) or float(budget_limit_cny) <= 0:
            raise PlanningSupplementError("live_judge_finite_positive_budget_required")
        self.key_file = Path(key_file)
        self.budget_ledger_path = Path(budget_ledger_path)
        self.budget_limit_cny = float(budget_limit_cny)
        self.max_output_tokens = int(max_output_tokens)
        self.thinking_budget = int(thinking_budget)
        self.timeout_seconds = float(timeout_seconds)
        self.raw_response_dir = Path(raw_response_dir) if raw_response_dir else None
        self.model = model

    def __call__(
        self,
        *,
        gap: Mapping[str, Any],
        snapshot: Any,
        reading_text: str,
        card: Mapping[str, Any],
        source_unit: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        from .module4.runtime import GlobalBudgetLedger, QwenDirectClient, invoke_client

        output_dir = Path(str(source_unit.get("output_dir") or ".")) / "judge"
        output_dir.mkdir(parents=True, exist_ok=True)
        ledger = GlobalBudgetLedger(limit_cny=self.budget_limit_cny, path=self.budget_ledger_path)
        client = QwenDirectClient(
            model=self.model,
            key_file=self.key_file,
            timeout_seconds=self.timeout_seconds,
            max_output_tokens=self.max_output_tokens,
            max_retries=0,
            thinking=True,
            thinking_budget=self.thinking_budget,
            json_mode=False,
            raw_response_dir=self.raw_response_dir or output_dir / "raw_responses",
            budget_ledger=ledger,
        )
        messages = _make_judge_messages(
            gap=gap,
            reading_text=reading_text,
            card=card,
            source_unit=source_unit,
        )
        raw = invoke_client(
            client,
            messages,
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            thinking=True,
            thinking_budget=self.thinking_budget,
            call_id=f"planning-supplement-judge:{source_unit.get('source_unit_id')}:{gap.get('gap_id')}",
        )
        content = raw.get("content")
        if isinstance(content, Mapping):
            return dict(content)
        if isinstance(content, str):
            try:
                decoded = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE))
            except json.JSONDecodeError as exc:
                raise PlanningSupplementError("judge_response_not_json") from exc
            if isinstance(decoded, Mapping):
                return dict(decoded)
        if isinstance(raw, Mapping) and "status" in raw:
            return dict(raw)
        raise PlanningSupplementError("judge_response_shape_invalid")


def _source_unit_id(request_id: str, index: int, candidate: _Candidate) -> str:
    identity = _candidate_identity(candidate.record)
    stable = "|".join(str(identity.get(key) or "") for key in ("canonical_paper_id", "doi", "title"))
    digest = hashlib.sha256((request_id + "|" + candidate.source_kind + "|" + stable).encode("utf-8")).hexdigest()[:10]
    return f"SU{index:02d}_{_safe_token(candidate.source_kind)}_{digest}"


def _verify_review_passage(
    candidate: _Candidate,
    *,
    pool_rows: Sequence[Mapping[str, Any]],
    base_pool_path: Path,
) -> dict[str, Any]:
    lineage = candidate.lineage
    quote = str(lineage.get("review_source_quote") or "")
    if not quote.strip():
        return {"status": "pending_unverified_lineage", "reason": "review_source_quote_missing"}

    source_dir_text = str(lineage.get("review_snapshot_dir") or "").strip()
    card_path_text = str(lineage.get("review_card_path") or "").strip()
    source_unit_ref = str(lineage.get("reviewed_source_unit_id") or "")
    if not source_dir_text and not card_path_text:
        canonical = source_unit_ref.split(":", 1)[0]
        for row in pool_rows:
            identity = _pool_identity(row)
            if _norm_identity(identity.get("canonical_paper_id")) == _norm_identity(canonical):
                card_path_text = str(row.get("card_path") or "")
                break

    def resolve_source_path(value: str) -> Path:
        path = Path(value)
        return (path if path.is_absolute() else base_pool_path.parent / path).resolve()

    if source_dir_text:
        source_dir = resolve_source_path(source_dir_text)
        snapshot = SimpleNamespace(root=source_dir)
        source_text = _snapshot_reading_text(snapshot)
        if not source_text:
            return {"status": "pending_unverified_lineage", "reason": "review_snapshot_text_unavailable", "review_snapshot_dir": str(source_dir)}
        if quote not in source_text:
            return {
                "status": "pending_unverified_lineage", "reason": "review_source_quote_not_found",
                "review_snapshot_dir": str(source_dir),
                "snapshot_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            }
        start = source_text.find(quote)
        return {
            "status": "verified",
            "verification_method": "exact_quote_in_review_snapshot",
            "review_snapshot_dir": str(source_dir),
            "snapshot_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            "review_source_locator": {
                "start": start,
                "end": start + len(quote),
                "review_block_id": str(lineage.get("review_block_id") or ""),
                "reviewed_source_unit_id": source_unit_ref,
            },
        }

    if not card_path_text:
        return {"status": "pending_unverified_lineage", "reason": "review_card_path_unavailable"}
    card_path = resolve_source_path(card_path_text)
    packet_path = card_path.parent / "INPUT_PACKET.json"
    try:
        packet_bytes = packet_path.read_bytes()
        packet = json.loads(packet_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "pending_unverified_lineage", "reason": "review_input_packet_unavailable", "review_card_path": str(card_path)}
    packet_obj = packet if isinstance(packet, Mapping) else {}
    packet_source = packet_obj.get("model_reading_packet") or packet_obj.get("reading_packet") or {}
    observations = packet_source.get("observations", []) if isinstance(packet_source, Mapping) else []
    block_id = str(lineage.get("review_block_id") or "").strip()
    matched: list[dict[str, Any]] = []
    for observation in observations if isinstance(observations, Sequence) else ():
        if not isinstance(observation, Mapping):
            continue
        observation_id = str(observation.get("observation_id") or observation.get("block_id") or "")
        text = str(observation.get("text") or "")
        if quote in text and (not block_id or block_id in {observation_id, observation_id.removeprefix("block:")}):
            start = text.find(quote)
            matched.append({
                "observation_id": observation_id,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "start": start,
                "end": start + len(quote),
            })
    if not matched:
        return {
            "status": "pending_unverified_lineage",
            "reason": "review_source_quote_or_block_not_found",
            "review_card_path": str(card_path),
            "review_input_packet_path": str(packet_path),
            "review_input_packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
            "requested_review_block_id": block_id,
        }
    return {
        "status": "verified",
        "verification_method": "exact_quote_in_frozen_review_input_packet",
        "review_card_path": str(card_path),
        "review_input_packet_path": str(packet_path),
        "review_input_packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "review_snapshot_id": str(packet_obj.get("snapshot_id") or ""),
        "review_snapshot_sha256": str((packet_obj.get("material") or {}).get("snapshot_sha256") or ""),
        "review_source_locator": {"reviewed_source_unit_id": source_unit_ref, "matches": matched},
    }


def _review_account_context(candidate: _Candidate) -> str:
    """Keep the ordinary review-context path as a reading locator."""

    lineage = candidate.lineage
    return str(lineage.get("review_snapshot_dir") or lineage.get("review_card_path") or "")


def _citation_anchor(
    candidate: _Candidate,
    *,
    request: Any,
    review_context_path: str = "",
) -> dict[str, Any]:
    identity = _candidate_identity(candidate.record)
    lineage = candidate.lineage
    review_account_available = bool(str(lineage.get("review_claim") or lineage.get("review_source_quote") or "").strip())
    citation_text = str(lineage.get("citation_text") or "")
    author_match = re.match(r"\s*([A-Z][A-Za-z'’.-]+\s+[A-Z](?:\s+et\s+al\.)?)", citation_text)
    year_match = re.search(r"\b((?:19|20)\d{2})\b", citation_text)
    bibliography_identity = {
        key: identity.get(key)
        for key in ("doi", "title", "authors", "year", "openalex_id", "arxiv_id")
        if identity.get(key) not in (None, "", [])
    }
    if "authors" not in bibliography_identity and author_match:
        bibliography_identity["authors"] = author_match.group(1)
    if "year" not in bibliography_identity and year_match:
        bibliography_identity["year"] = int(year_match.group(1))
    bibliography_available = bool(bibliography_identity or citation_text)
    material_ready = review_account_available
    citation_completion = "included" if bibliography_available else "add bibliographic details"
    return {
        "schema_version": "optomind.planning_supplement.review_citation.v2",
        "source_unit_id": candidate.source_unit_id,
        "topic_id": request.topic_id,
        "gap_id": request.gap_id,
        "status": "material_ready" if material_ready else "review_account_needed",
        "material_ready": material_ready,
        "planning_use": "equal_use" if material_ready else "not_assessed",
        "source_kind": "review_reported_secondary",
        "review_source_unit_id": lineage.get("reviewed_source_unit_id", ""),
        "review_reference_id": lineage.get("reference_id", ""),
        "review_passage": lineage.get("review_source_quote", ""),
        "review_claim_note": lineage.get("review_claim", ""),
        "review_context_path": review_context_path,
        "citation": str(lineage.get("citation_text") or ""),
        "bibliography": bibliography_identity,
        "bibliography_completion": citation_completion,
    }


def _citation_anchor_status(anchors: Sequence[Mapping[str, Any]]) -> str:
    if not anchors:
        return "not_applicable"
    statuses = [str(anchor.get("status") or "") for anchor in anchors]
    if all(status == "material_ready" for status in statuses):
        return "material_ready"
    if any(status == "material_ready" for status in statuses):
        return "some_material_ready"
    return "review_account_needed"


def _material_depth_label(acquisition: Any) -> str:
    value = str(getattr(acquisition, "material_depth", "unknown") or "unknown").strip().casefold()
    return value if value in {"fulltext", "structured_partial", "abstract", "snippet", "metadata_only", "unknown"} else value


def _pool_row_from_card(
    candidate: _Candidate,
    card: Mapping[str, Any],
    *,
    paper_id: str,
    card_path: str,
    source_unit_id: str,
    judgment: Mapping[str, Any],
    material_depth: str,
    gap_id: str,
    judgment_path: str,
) -> dict[str, Any]:
    planning_view = card.get("planning_view") if isinstance(card.get("planning_view"), Mapping) else {}
    row = {
        "paper_id": paper_id or str(candidate.record.get("canonical_paper_id") or candidate.record.get("doi") or ""),
        "card_path": card_path,
        "planning_view": dict(planning_view),
        "supplement_source_unit_id": source_unit_id,
        "supplement_status": judgment.get("status"),
        "material_ready": bool(judgment.get("material_ready", judgment.get("status") in {"fulfilled", "partial"})),
        "material_depth_label": material_depth,
        "supplement_material_status": "ready" if judgment.get("material_ready") else "unassessed",
        "supplement_gap_material": {
            "gap_id": gap_id,
            "status": judgment.get("status"),
            "material_ready": bool(judgment.get("material_ready", judgment.get("status") in {"fulfilled", "partial"})),
            "material_usefulness": judgment.get("material_usefulness", ""),
            "useful_material": judgment.get("useful_material", ""),
            "auxiliary_material": judgment.get("auxiliary_material", []),
            "evidence": list(judgment.get("evidence") or []),
            "limitations": list(judgment.get("limitations") or []),
            "remaining_gap": str(judgment.get("remaining_gap") or ""),
            "remaining_gap_recovered_by_policy": bool(judgment.get("remaining_gap_recovered_by_policy")),
            "outline_action": str(judgment.get("outline_action") or ""),
            "outline_action_recovered_by_policy": bool(judgment.get("outline_action_recovered_by_policy")),
            "judgment_path": judgment_path,
        },
    }
    return row


def _apply_source_unit_to_pool(
    pool_rows: list[dict[str, Any]],
    candidate: _Candidate,
    card: Mapping[str, Any],
    judgment: Mapping[str, Any],
    *,
    card_path: str,
    material_depth: str,
    gap_id: str,
    judgment_path: str,
) -> dict[str, Any]:
    identity_audit = candidate.identity_audit or {"decision": "new_identity"}
    if identity_audit.get("decision") == "identity_conflict_unresolved" or identity_audit.get("decision") == "ambiguous_existing_match":
        return {"pool_action": "unresolved_identity", "identity_audit": identity_audit}
    status = str(judgment.get("status") or "unjudgeable")
    material_ready = bool(judgment.get("material_ready", status in {"fulfilled", "partial"}))
    if not material_ready:
        return {"pool_action": "not_admitted", "reason": "no_practical_material_returned"}
    matched_paper_id = str((identity_audit.get("match") or {}).get("paper_id") or "")
    existing_index = next((index for index, row in enumerate(pool_rows) if matched_paper_id and str(row.get("paper_id") or "") == matched_paper_id), None)
    if existing_index is not None:
        previous = dict(pool_rows[existing_index])
        previous_path = str(previous.get("card_path") or "")
        prior_gap_materials: list[dict[str, Any]] = []
        for material in [
            *(previous.get("prior_supplement_gap_materials") or []),
            *(previous.get("supplement_gap_materials") or []),
            previous.get("supplement_gap_material"),
        ]:
            if isinstance(material, Mapping) and material and material not in prior_gap_materials:
                prior_gap_materials.append(dict(material))
        if status == "fulfilled":
            new_row = _pool_row_from_card(
                candidate,
                card,
                paper_id=matched_paper_id,
                card_path=card_path,
                source_unit_id=candidate.source_unit_id,
                judgment=judgment,
                material_depth=material_depth,
                gap_id=gap_id,
                judgment_path=judgment_path,
            )
            # Preserve non-card review metadata and make the old A/B card visible.
            for key in ("root_review_note", "selection_note"):
                if key in previous:
                    new_row[key] = previous[key]
            new_row["prior_card_path"] = previous_path
            new_row["supplement_version"] = int(previous.get("supplement_version") or 1) + 1
            previous_units = list(previous.get("prior_source_unit_ids") or [])
            if previous.get("supplement_source_unit_id"):
                previous_units.append(previous["supplement_source_unit_id"])
            new_row["prior_source_unit_ids"] = list(dict.fromkeys(previous_units))
            if prior_gap_materials:
                new_row["prior_supplement_gap_materials"] = prior_gap_materials
            pool_rows[existing_index] = new_row
            return {"pool_action": "active_card_replaced_after_fulfillment_gate", "prior_card_path": previous_path}
        previous.setdefault("supplement_attempts", [])
        previous["supplement_attempts"] = list(previous["supplement_attempts"]) + [{
            "source_unit_id": candidate.source_unit_id,
            "status": status,
            "material_ready": material_ready,
            "candidate_card_path": card_path,
            "material_depth_label": material_depth,
            "gap_id": gap_id,
            "judgment_path": judgment_path,
        }]
        # The authoritative A/B card stays in place, but useful partial/unmet
        # prose must travel with the pool, not only a diagnostic file path.
        material = _pool_row_from_card(
            candidate, card, paper_id=matched_paper_id, card_path=card_path,
            source_unit_id=candidate.source_unit_id, judgment=judgment,
            material_depth=material_depth, gap_id=gap_id, judgment_path=judgment_path,
        )["supplement_gap_material"]
        previous["supplement_gap_materials"] = [*prior_gap_materials, material]
        pool_rows[existing_index] = previous
        return {"pool_action": "active_card_retained_material_candidate_linked" if status == "unmet" else "active_card_retained_partial_candidate_linked", "prior_card_path": previous_path}
    if not card:
        return {"pool_action": "not_admitted", "reason": "reading_card_missing"}
    new_row = _pool_row_from_card(
        candidate,
        card,
        paper_id=str(candidate.record.get("paper_id") or candidate.record.get("paperId") or candidate.record.get("canonical_paper_id") or candidate.record.get("doi") or ""),
        card_path=card_path,
        source_unit_id=candidate.source_unit_id,
        judgment=judgment,
        material_depth=material_depth,
        gap_id=gap_id,
        judgment_path=judgment_path,
    )
    new_row["supplement_version"] = 1
    new_row["identity_audit"] = identity_audit
    pool_rows.append(new_row)
    if status == "fulfilled":
        return {"pool_action": "new_candidate_added"}
    if status == "partial":
        return {"pool_action": "new_partial_candidate_added"}
    return {"pool_action": "new_material_ready_candidate_added"}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n" for row in rows)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(body, encoding="utf-8", newline="\n")
    temporary.replace(path)


def preflight_planning_supplement(
    request_path: str | Path,
    *,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Validate inputs and show the bounded plan without network/model calls."""

    request = load_planning_supplement_request(request_path)
    pool_bytes, pool_rows = _load_pool(request.base_pool_path)
    plan, plan_report = _load_plan(request.plan_path)
    specs, query_source = _query_specs(request, plan)
    target = Path(output_dir).resolve()
    return {
        "schema_version": RESULT_SCHEMA,
        "network_call": False,
        "model_call": False,
        "request_id": request.request_id,
        "topic_id": request.topic_id,
        "gap_id": request.gap_id,
        "base_pool_rows": len(pool_rows),
        "base_pool_sha256": hashlib.sha256(pool_bytes).hexdigest(),
        "plan_validation": {"ok": bool(plan_report.get("ok")), "repairs": list(plan_report.get("repairs") or [])},
        "plan_source_audit": dict(plan_report.get("source_audit") or {}),
        "query_source": query_source,
        "queries": [
            {"query_id": item.query_id, "facet_id": item.facet_id, "query_type": item.query_type,
             "retrieval_source": item.retrieval_source, "query_text": item.query_text}
            for item in specs
        ],
        "limits": {
            "max_candidates": request.max_candidates,
            "max_acquisitions": request.max_acquisitions,
            "per_query_limit": request.per_query_limit,
        },
        "explicit_source_count": len(request.known_papers) + len(request.reviewed_references),
        "output_dir": str(target),
        "output_dir_exists": target.exists(),
        "source_pool_is_read_only": True,
    }


def _render_material_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return value.strip().splitlines() if value.strip() else []
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, str):
                lines.append(f"- **{key}:** {item.strip()}")
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                lines.append(f"- **{key}:**")
                lines.extend("  " + line for line in _render_material_value(item))
            elif isinstance(item, Mapping):
                lines.append(f"- **{key}:**")
                lines.extend("  " + line for line in _render_material_value(item))
            elif item not in (None, ""):
                lines.append(f"- **{key}:** {item}")
        return lines
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        lines = []
        for item in value:
            if isinstance(item, (Mapping, list, tuple)):
                lines.extend(["-", *["  " + line for line in _render_material_value(item)]])
            elif str(item or "").strip():
                lines.append("- " + str(item).strip())
        return lines
    return [str(value)] if value not in (None, "") else []


def _render_supplement_materials(
    *,
    gap: Mapping[str, Any],
    source_units: Sequence[Mapping[str, Any]],
    citation_anchors: Sequence[Mapping[str, Any]],
) -> str:
    lines = ["# Supplement writing materials", "", f"**Gap:** {gap.get('gap_question', '')}", ""]
    for unit in source_units:
        judgment = unit.get("fulfillment_judgment") if isinstance(unit.get("fulfillment_judgment"), Mapping) else {}
        if unit.get("source_kind") == "reviewed_reference" and not judgment:
            continue
        identity = unit.get("record_identity") if isinstance(unit.get("record_identity"), Mapping) else {}
        title = str(identity.get("title") or unit.get("source_unit_id") or "Source material")
        lines.extend([f"## {title}", "", f"- Gap status: {judgment.get('status') or unit.get('status') or 'unassessed'}", f"- Material use: {judgment.get('material_usefulness') or 'unassessed'}", ""])
        for heading, field in (("Useful material", "useful_material"), ("Supporting material", "auxiliary_material")):
            value = judgment.get(field)
            rendered = _render_material_value(value)
            if rendered:
                lines.extend([f"### {heading}", "", *rendered, ""])
        open_gap = str(judgment.get("remaining_gap") or "").strip()
        if open_gap:
            lines.extend([f"Open point: {open_gap}", ""])
    for anchor in citation_anchors:
        identity = anchor.get("bibliography") if isinstance(anchor.get("bibliography"), Mapping) else {}
        title = str(identity.get("title") or anchor.get("review_reference_id") or "Review-reported reference")
        lines.extend([f"## Review-reported: {title}", "", f"- Citation: {anchor.get('citation') or ''}", f"- Review claim: {anchor.get('review_claim_note') or ''}", "- Use with the review attribution.", ""])
    return "\n".join(lines).rstrip() + "\n"


def run_gap_local_triage(
    gap: Mapping[str, Any],
    *,
    index_path: str | Path,
    intended_use: str = "mechanism",
    user_scope: str = "",
    judge: Any | None = None,
    output_dir: str | Path | None = None,
    top_papers: int = 6,
    passages_per_paper: int = 2,
    max_passages: int = 5,
    read_local: bool = True,
) -> dict[str, Any]:
    """Answer one gap from already-registered local material before any retrieval.

    Work order 02: the planning chain must decide *direct use / bounded local
    read / external research* before it asks a provider for anything.  This
    helper is the seam the orchestrators call; it performs I/O inside a
    read-only SQLite connection and never constructs a gateway or an acquirer.
    """

    from .planning_material_search import PlanningMaterialIndex
    from .planning_material_triage import LocalGap, triage_gap, read_local_capture, build_writer_material

    question = str(gap.get("gap_question") or gap.get("question") or "").strip()
    if not question:
        raise PlanningSupplementError("local_triage_requires_gap_question")
    concepts: list[str] = []
    for query in gap.get("targeted_queries") or ():
        if isinstance(query, Mapping):
            text = str(query.get("query_text") or "").strip()
        else:
            text = str(query or "").strip()
        if text:
            concepts.append(text)
    raw_criteria = gap.get("success_criteria") or []
    if isinstance(raw_criteria, str):
        raw_criteria = [raw_criteria]
    local_gap = LocalGap(
        gap_id=str(gap.get("gap_id") or "gap"),
        question=question,
        intended_use=str(gap.get("intended_use") or intended_use),
        user_scope=str(gap.get("user_scope") or user_scope),
        success_criteria=tuple(str(item) for item in raw_criteria if str(item).strip()),
        chapter_ids=tuple(str(item) for item in (gap.get("chapter_ids") or ()) if str(item).strip()),
        concepts=tuple(concepts[:6]),
        required_concepts=tuple(str(item) for item in (gap.get("required_concepts") or ()) if str(item).strip()),
        existing_handles=tuple(str(item) for item in (gap.get("known_paper_handles") or ()) if str(item).strip()),
    )
    with PlanningMaterialIndex(Path(index_path), readonly=True) as index:
        judgment = triage_gap(
            index, local_gap,
            judge=judge,
            top_papers=top_papers,
            passages_per_paper=passages_per_paper,
            max_passages=max_passages,
        )
        if read_local and judge is not None and judgment.decision == "local_deep_read":
            judgment = read_local_capture(index, judgment, judge=judge)
    payload = judgment.to_dict()
    payload["writer_material"] = build_writer_material(judgment)
    payload["gap_id"] = local_gap.gap_id
    payload["external_request"] = judgment.external_request()
    if output_dir is not None:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        _write_json(root / "LOCAL_TRIAGE.json", payload)
    return payload


class QwenCandidateSelector:
    """Spend a short abstract read to avoid downloading irrelevant studies."""
    def __init__(self, *, key_file: str | Path, budget_ledger_path: str | Path, budget_limit_cny: float):
        self.key_file = key_file
        self.budget_ledger_path = budget_ledger_path
        self.budget_limit_cny = budget_limit_cny

    def __call__(self, *, gap: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]], limit: int) -> list[int]:
        if not candidates:
            return []
        from .module4.runtime import GlobalBudgetLedger, QwenDirectClient, invoke_client
        ledger = GlobalBudgetLedger(limit_cny=self.budget_limit_cny, path=self.budget_ledger_path)
        client = QwenDirectClient(model="qwen3.7-flash", key_file=self.key_file, timeout_seconds=300,
            max_output_tokens=1800, max_retries=0, thinking=True, thinking_budget=1024,
            json_mode=True, budget_ledger=ledger)
        rows = [{"index": i, "title": c.get("title"), "abstract": str(c.get("abstract") or "")[:4500],
                 "snippets": (c.get("snippets") or [])[:4]} for i,c in enumerate(candidates)]
        prompt = "你为一个具体综述缺口挑选值得获取和阅读的论文。读标题、摘要与片段，按研究对象或材料、研究设置、方法、比较、结果或论证、验证方式是否对题及新增信息排序。仅出现相同技术词或设计标签不够；另一对象、设置或方法的研究不能直接回答本问题。优先选择能补足当前核心缺口的新材料，而不是再收同主题背景。问题明确要求的研究对象、材料、设置、方法或测量类型应能在摘要或片段中对上；明显只有另一对象或另一类型数据的研究不要占本轮名额。选择互补且能带回具体内容的少量论文，可以少于上限或为0；有价值的反例是回答本问题相反方向的结果或论证，不是其他问题的材料。缺摘要可按真实片段判断，未知不是假定相关。返回JSON selected_indices（0起始、最多limit个、最值得先读、可为空）以及简短reason。"
        raw = invoke_client(client,[{"role":"system","content":prompt},{"role":"user","content":json.dumps({"gap":gap,"candidates":rows,"limit":limit},ensure_ascii=False)}],
            model="qwen3.7-flash",max_output_tokens=1800,thinking=True,thinking_budget=1024,call_id="planning-candidate-selection")
        content = raw.get("content")
        value = content if isinstance(content,Mapping) else json.loads(str(content))
        return list(value.get("selected_indices") or [])


def run_planning_supplement(
    request_path: str | Path,
    *,
    output_dir: str | Path,
    gateway: Any | None,
    acquirer_factory: Callable[[Path], Any],
    card_runner: Callable[..., Any],
    fulfillment_judge: Callable[..., Mapping[str, Any]] | None,
    card_options: Mapping[str, Any] | None = None,
    candidate_selector: Callable[..., Sequence[int]] | None = None,
    reuse_material: Callable[..., Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Run one bounded supplement; all external collaborators are injectable.

    ``acquirer_factory`` is called once per source unit with a fresh output root,
    preventing a failed refresh from overwriting an earlier paper snapshot.
    ``card_runner`` is invoked using ``run_paper_reading_card``'s keyword
    signature.  ``fulfillment_judge`` receives the actual snapshot reading text
    and card B, plus the gap, source lineage, and plan.
    """

    request = load_planning_supplement_request(request_path)
    base_bytes, base_rows = _load_pool(request.base_pool_path)
    plan, plan_report = _load_plan(request.plan_path)
    specs, query_source = _query_specs(request, plan)
    root = Path(output_dir).resolve()
    if root.exists():
        if any(root.iterdir()):
            raise OutputDirectoryError("output_directory_must_be_new_or_empty")
    else:
        root.mkdir(parents=True, exist_ok=False)
    base_digest = hashlib.sha256(base_bytes).hexdigest()
    (root / "BASE_PLANNING_POOL.jsonl").write_bytes(base_bytes)
    _write_json(root / "REQUEST.json", request.to_dict())
    _write_json(root / "PLAN_SOURCE_AUDIT.json", dict(plan_report.get("source_audit") or {}))

    query_audits, search_candidates, actual_query_source = _query_specs_and_hits(request, plan, gateway)
    if not specs:
        actual_query_source = query_source
    candidates = _merge_candidate_sources([*_explicit_candidates(request), *search_candidates])
    candidates = candidates[:request.max_candidates]
    if candidate_selector is not None:
        ordinary = [c for c in candidates if c.source_kind != "reviewed_reference"]
        selected = candidate_selector(
            gap={"question": request.gap_question, "success_criteria": list(request.success_criteria)},
            candidates=[{**c.record, "snippets": [h.get("snippet_text") or h.get("raw_snippet_text") or h.get("text") or "" for h in c.search_hits]} for c in ordinary],
            limit=request.max_acquisitions,
        )
        positions = list(dict.fromkeys(i for i in selected if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(ordinary)))
        candidates = [*[c for c in candidates if c.source_kind == "reviewed_reference"], *[ordinary[i] for i in positions[:request.max_acquisitions]]]
    selected_hit_ids = {hit.get("hit_id") for candidate in candidates for hit in candidate.search_hits}
    all_search_hits = []
    for candidate in search_candidates:
        for hit in candidate.search_hits:
            all_search_hits.append({
                **hit,
                "candidate_canonical_paper_id": candidate.record.get("canonical_paper_id"),
                "selected_for_materialization": hit.get("hit_id") in selected_hit_ids,
            })
    _write_jsonl(root / "SEARCH_HITS.jsonl", all_search_hits)

    pool_rows = [dict(row) for row in base_rows]
    source_units: list[dict[str, Any]] = []
    citation_anchors: list[dict[str, Any]] = []
    acquisition_count = 0
    provider_issue_seen = any(bool(item.get("provider_issue")) for item in query_audits)
    retryable_provider_seen = any(bool(item.get("retryable")) for item in query_audits)
    acquisition_or_judgment_error_seen = False
    local_judgment_validation_error_seen = False
    network_call_seen = bool(specs)
    model_call_seen = False
    gap = {
        "topic_id": request.topic_id,
        "gap_id": request.gap_id,
        "gap_question": request.gap_question,
        "success_criteria": list(request.success_criteria),
        "required_outputs": list(request.required_outputs),
        "reusable_material": request.reusable_material,
        "still_missing": request.still_missing,
    }
    options = dict(card_options or {})

    for index, candidate in enumerate(candidates, start=1):
        candidate.source_unit_id = _source_unit_id(request.request_id, index, candidate)
        candidate.identity_audit = {} if candidate.source_kind == "reviewed_reference" else _identity_match(candidate, base_rows)
        unit_dir = root / "source_units" / candidate.source_unit_id
        unit_dir.mkdir(parents=True, exist_ok=False)
        record = dict(candidate.record)
        identity = _candidate_identity(record)
        record.setdefault("canonical_paper_id", identity.get("canonical_paper_id"))
        if candidate.source_kind == "reviewed_reference":
            # Preserve the reviewed source as citation lineage only.  Do not put
            # its narrative claim into the C paper's abstract/snippet fields.
            record.pop("abstract", None)
            record.pop("snippets", None)
        _write_json(unit_dir / "SOURCE_UNIT.json", {
            "source_unit_id": candidate.source_unit_id,
            "source_kind": candidate.source_kind,
            "record_identity": identity,
            "lineage": candidate.lineage,
            "identity_audit": candidate.identity_audit,
            "search_hit_ids": [row["hit_id"] for row in candidate.search_hits],
            "search_hits_path": "SEARCH_HITS.jsonl" if candidate.search_hits else "",
        })
        _write_json(unit_dir / "CANDIDATE_RECORD.json", record)
        if candidate.search_hits:
            _write_jsonl(unit_dir / "SEARCH_HITS.jsonl", candidate.search_hits)
        source_unit: dict[str, Any] = {
            "source_unit_id": candidate.source_unit_id,
            "source_kind": candidate.source_kind,
            "lineage": dict(candidate.lineage),
            "record_identity": identity,
            "output_dir": str(unit_dir),
            "candidate_record_path": "CANDIDATE_RECORD.json",
            "search_hit_ids": [row["hit_id"] for row in candidate.search_hits],
            "search_hits_path": "SEARCH_HITS.jsonl" if candidate.search_hits else "",
        }
        if candidate.identity_audit:
            source_unit["identity_audit"] = dict(candidate.identity_audit)

        if candidate.source_kind == "reviewed_reference":
            review_context_path = _review_account_context(candidate)
            compact_lineage = {
                key: value for key, value in candidate.lineage.items()
                if key not in {"review_snapshot_dir", "review_card_path"} and value not in (None, "")
            }
            if review_context_path:
                compact_lineage["review_context_path"] = review_context_path
            candidate.lineage = compact_lineage
            source_unit["lineage"]["review_context_path"] = review_context_path
            source_unit["lineage"] = dict(candidate.lineage)
            anchor = _citation_anchor(candidate, request=request, review_context_path=review_context_path)
            citation_anchors.append(anchor)
            source_unit.update({
                "status": anchor["status"],
                "citation_anchor_path": "../../CITATION_ANCHORS.jsonl",
                "material_depth": "not_acquired",
                "pool_action": {"pool_action": "citation_anchor_only", "reason": "secondary_source_reference"},
            })
            _write_json(unit_dir / "CITATION_ANCHOR.json", anchor)
            source_units.append(source_unit)
            _write_json(unit_dir / "SOURCE_UNIT.json", source_unit)
            continue

        reused = reuse_material(record=record, pool_rows=pool_rows) if reuse_material is not None else None
        if acquisition_count >= request.max_acquisitions and not reused:
            source_unit.update({"status": "not_attempted_limit", "reason": "max_acquisitions_reached"})
            source_units.append(source_unit)
            _write_json(unit_dir / "SOURCE_UNIT.json", {**source_unit, "search_hits_path": "SEARCH_HITS.jsonl" if candidate.search_hits else ""})
            continue
        material_root = unit_dir / "materials"
        try:
            if reused and reused.get("snapshot") is not None:
                snapshot = reused["snapshot"]
                if isinstance(snapshot, (str, Path)):
                    snapshot = SimpleNamespace(root=Path(snapshot), snapshot_id=Path(snapshot).name)
                acquisition = SimpleNamespace(snapshot=snapshot, status="reused", material_depth=reused.get("material_depth") or "fulltext", attempts=[], known_gaps=[], errors=[])
            else:
                acquisition_count += 1
                network_call_seen = True
                acquirer = acquirer_factory(material_root)
                acquisition = acquirer.acquire(record)
        except Exception as exc:
            acquisition_or_judgment_error_seen = True
            retryable_provider_seen = retryable_provider_seen or _retryable_exception(exc)
            source_unit.update({
                "status": "acquisition_error",
                "failure_stage": "material_acquisition",
                "error": type(exc).__name__,
                "retryable": _retryable_exception(exc),
                "material_depth": "unknown",
                "fulfillment_judgment": _unjudgeable("material_acquisition_failed", request.gap_question),
            })
            _write_json(unit_dir / "FULFILLMENT_JUDGMENT.json", source_unit["fulfillment_judgment"])
            _write_json(unit_dir / "SOURCE_UNIT.json", source_unit)
            source_units.append(source_unit)
            continue

        acquisition_provider_issue, acquisition_provider_retryable = _acquisition_provider_state(acquisition)
        provider_issue_seen = provider_issue_seen or acquisition_provider_issue
        retryable_provider_seen = retryable_provider_seen or acquisition_provider_retryable
        acquisition_dict = acquisition.to_dict() if callable(getattr(acquisition, "to_dict", None)) else {
            "status": getattr(acquisition, "status", "unknown"),
            "material_depth": getattr(acquisition, "material_depth", "unknown"),
            "attempts": list(getattr(acquisition, "attempts", []) or []),
            "known_gaps": list(getattr(acquisition, "known_gaps", []) or []),
            "errors": list(getattr(acquisition, "errors", []) or []),
        }
        _write_json(unit_dir / "ACQUISITION.json", acquisition_dict)
        material_depth = _material_depth_label(acquisition)
        snapshot = getattr(acquisition, "snapshot", None)
        reading_text = ""
        if snapshot is not None:
            snapshot_root = getattr(snapshot, "root", None)
            if snapshot_root:
                try:
                    reading_text = str(load_practical_material(snapshot_root).get("body") or "")
                except (OSError, UnicodeError, ValueError):
                    reading_text = ""
            if not reading_text:
                reading_text = _snapshot_reading_text(snapshot)
        source_unit.update({
            "status": str(getattr(acquisition, "status", "unknown")),
            "material_depth": material_depth,
            "snapshot_id": str(getattr(snapshot, "snapshot_id", "") or ""),
            "snapshot_path": str(getattr(snapshot, "root", "") or ""),
            "reading_text_chars": len(reading_text),
            "acquisition_path": "ACQUISITION.json",
        })
        if not snapshot or not reading_text.strip() or material_depth == "metadata_only":
            judgment = _unjudgeable("no_usable_snapshot_content", request.gap_question)
            source_unit["status"] = "unjudgeable"
            source_unit["fulfillment_judgment"] = judgment
            _write_json(unit_dir / "FULFILLMENT_JUDGMENT.json", judgment)
            _write_json(unit_dir / "SOURCE_UNIT.json", source_unit)
            source_units.append(source_unit)
            continue

        card_dir = unit_dir / "card"
        try:
            if reused and reused.get("card"):
                card_result = {"card": dict(reused["card"]), "output_dir": str(Path(reused["card_path"]).parent)}
                source_unit["material_reused"] = True
            else:
                model_call_seen = True
                card_result = card_runner(
                    snapshot_dir=getattr(snapshot, "root", snapshot),
                    plan_path=request.plan_path,
                    output_dir=card_dir,
                    **options,
                )
            card = _card_body(card_result)
            if not card:
                raise PlanningSupplementError("card_runner_returned_no_card")
        except Exception as exc:
            acquisition_or_judgment_error_seen = True
            source_unit.update({
                "status": "card_error",
                "failure_stage": "reading_card",
                "error": type(exc).__name__,
                "retryable": _retryable_exception(exc),
                "fulfillment_judgment": _unjudgeable("reading_card_provider_or_validation_error", request.gap_question),
            })
            retryable_provider_seen = retryable_provider_seen or _retryable_exception(exc)
            _write_json(unit_dir / "FULFILLMENT_JUDGMENT.json", source_unit["fulfillment_judgment"])
            _write_json(unit_dir / "SOURCE_UNIT.json", source_unit)
            source_units.append(source_unit)
            continue

        card_output_dir = str((card_result.get("output_dir") if isinstance(card_result, Mapping) else "") or card_dir)
        source_unit["card_path"] = str(Path(card_output_dir) / "PAPER_READING_CARD.json")
        source_unit["card_output_dir"] = card_output_dir
        source_unit["card_id"] = str(card.get("card_id") or "")
        if fulfillment_judge is None:
            judgment = _unjudgeable("fulfillment_judge_not_configured", request.gap_question)
        else:
            try:
                model_call_seen = True
                raw_judgment = fulfillment_judge(
                    gap=gap,
                    snapshot=snapshot,
                    reading_text=reading_text,
                    card=card,
                    source_unit=source_unit,
                    plan=plan,
                )
                judgment = _validate_fulfillment_judgment(
                    raw_judgment,
                    reading_text=reading_text,
                    gap_question=request.gap_question,
                )
                if judgment.get("execution_status") in {"failed", "provider_failed", "provider_error", "error"}:
                    acquisition_or_judgment_error_seen = True
                    provider_issue_seen = True
                    retryable_provider_seen = retryable_provider_seen or judgment.get("retryable") is True
                    source_unit["failure_stage"] = judgment.get("failure_stage") or "fulfillment_judge"
                if judgment.get("reason") in {
                    "judge_quote_not_found_in_snapshot",
                    "judge_quote_anchor_below_threshold",
                }:
                    local_judgment_validation_error_seen = True
            except Exception as exc:
                acquisition_or_judgment_error_seen = True
                retryable_provider_seen = retryable_provider_seen or _retryable_exception(exc)
                source_unit["failure_stage"] = "fulfillment_judge"
                judgment = _unjudgeable("fulfillment_judge_provider_error:" + type(exc).__name__, request.gap_question)
        source_unit["fulfillment_judgment"] = judgment
        source_unit["judgment_path"] = "FULFILLMENT_JUDGMENT.json"
        source_unit["status"] = judgment["status"]
        source_unit["success_criteria_audit"] = _success_criteria_audit(
            judgment,
            criteria=request.success_criteria,
            gap_question=request.gap_question,
        )
        _write_json(unit_dir / "FULFILLMENT_JUDGMENT.json", judgment)
        pool_action = _apply_source_unit_to_pool(
            pool_rows,
            candidate,
            card,
            judgment,
            card_path=source_unit["card_path"],
            material_depth=material_depth,
            gap_id=request.gap_id,
            judgment_path=str(unit_dir / "FULFILLMENT_JUDGMENT.json"),
        )
        source_unit["pool_action"] = pool_action
        source_units.append(source_unit)
        _write_json(unit_dir / "SOURCE_UNIT.json", source_unit)

    judgments = [item.get("fulfillment_judgment") or {} for item in source_units]
    statuses = [str(item.get("status") or "") for item in judgments]
    # An irrelevant or incomplete study does not contradict another study
    # that actually answers the question; judge relationships from content.
    if "fulfilled" in statuses:
        overall_status = "fulfilled"
        outcome = "gap_fulfilled"
    elif "partial" in statuses:
        overall_status = "partial"
        outcome = "gap_partially_fulfilled"
    elif local_judgment_validation_error_seen:
        overall_status = "unjudgeable"
        outcome = "judgment_quote_unanchored_local_repair_available"
    elif source_units and all(item.get("status") in {"material_ready", "review_account_needed"} for item in source_units):
        overall_status = "unjudgeable"
        outcome = "citation_anchor_only_secondary_source"
    elif retryable_provider_seen:
        overall_status = "unjudgeable"
        outcome = "retryable_provider_outage"
    elif provider_issue_seen:
        overall_status = "unjudgeable"
        outcome = "provider_access_error"
    elif specs and query_audits and not search_candidates:
        overall_status = "unmet"
        outcome = "search_exhausted_narrow_or_omit"
    elif specs and statuses and all(item in {"unmet", "unjudgeable"} for item in statuses):
        overall_status = "unmet" if "unmet" in statuses else "unjudgeable"
        outcome = "content_reviewed_gap_unmet_narrow_or_omit" if overall_status == "unmet" else "judgment_unavailable"
    elif not specs and not candidates:
        overall_status = "unjudgeable"
        outcome = "no_targeted_query"
    else:
        overall_status = "unjudgeable"
        outcome = "acquisition_or_judgment_failed" if acquisition_or_judgment_error_seen else "no_content_grounded_judgment"

    if overall_status in {"unmet", "partial"}:
        default_outline_action = "Narrow the gap to a claim supported by retrieved evidence, or omit it if no source supports it."
    elif overall_status == "fulfilled":
        default_outline_action = "Add the supported point with the evidence scope and limitations recorded in this supplement."
    elif outcome == "citation_anchor_only_secondary_source":
        default_outline_action = "Use the review-reported account with its bibliographic citation in the relevant discussion."
    elif outcome == "judgment_quote_unanchored_local_repair_available":
        default_outline_action = "Repair the model quote against the captured snapshot or revalidate the saved raw judgment before any provider retry."
    elif outcome in {"retryable_provider_outage", "provider_access_error"}:
        default_outline_action = "Retry after the provider or judgment issue is resolved; do not treat this attempt as scientific absence."
    else:
        default_outline_action = "Resolve the acquisition or judgment issue before making a scientific absence claim."
    citation_readiness = _citation_anchor_status(citation_anchors)
    citation_material_ready = any(bool(anchor.get("material_ready")) for anchor in citation_anchors)
    source_material_ready = any(
        bool((unit.get("fulfillment_judgment") or {}).get("material_ready", unit.get("status") in {"fulfilled", "partial"}))
        for unit in source_units
    )
    material_ready = citation_material_ready or source_material_ready
    citation_use_contract = {
        "status": "equal_use" if material_ready else "not_assessed",
    }
    substantive_gap_status = "unassessed" if outcome == "citation_anchor_only_secondary_source" else overall_status
    aggregate_judgment = {
        "status": overall_status,
        "outcome": outcome,
        "material_ready": material_ready,
        "planning_use_contract": citation_use_contract,
        "citation_anchor_status": citation_readiness,
        "substantive_gap_status": substantive_gap_status,
        "useful_material": [judgment.get("useful_material") for judgment in judgments if judgment.get("material_ready") and judgment.get("useful_material")],
        "auxiliary_material": [judgment.get("auxiliary_material") for judgment in judgments if judgment.get("material_ready") and judgment.get("auxiliary_material")],
        "evidence": [evidence for judgment in judgments for evidence in judgment.get("evidence", []) if isinstance(evidence, Mapping)],
        "limitations": [limit for judgment in judgments for limit in judgment.get("limitations", []) if str(limit or "").strip()],
        "failed_feedback": [
            {"source_unit_id": unit["source_unit_id"], **dict(unit["fulfillment_judgment"])}
            for unit in source_units
            if isinstance(unit.get("fulfillment_judgment"), Mapping)
            and (unit.get("failure_stage") or unit["fulfillment_judgment"].get("execution_status") in {"failed", "provider_failed", "provider_error", "error"})
        ],
        "remaining_gap": next((str(judgment.get("remaining_gap")) for judgment in judgments if judgment.get("status") == overall_status and "remaining_gap" in judgment), request.gap_question),
        "outline_action": next((str(judgment.get("outline_action")) for judgment in judgments if judgment.get("status") == overall_status and judgment.get("outline_action")), default_outline_action),
        "outline_action_recovered_by_policy": any(bool(judgment.get("outline_action_recovered_by_policy")) for judgment in judgments),
        "remaining_gap_recovered_by_policy": any(bool(judgment.get("remaining_gap_recovered_by_policy")) for judgment in judgments),
        "query_source": actual_query_source,
        "query_audits": query_audits,
        "candidate_count": len(candidates),
        "acquisition_count": acquisition_count,
        "citation_anchor_count": len(citation_anchors),
        "network_call": network_call_seen,
        "model_call": model_call_seen,
        "note": "The gap judgment and ready writing material are reported separately.",
    }
    _write_jsonl(root / "PLANNING_POOL.jsonl", pool_rows)
    _write_jsonl(root / "CITATION_ANCHORS.jsonl", citation_anchors)
    materials_path = root / "SUPPLEMENT_MATERIALS.md"
    materials_temp = materials_path.with_name(materials_path.name + ".tmp")
    materials_temp.write_text(
        _render_supplement_materials(gap=gap, source_units=source_units, citation_anchors=citation_anchors),
        encoding="utf-8",
        newline="\n",
    )
    materials_temp.replace(materials_path)
    _write_json(root / "SUPPLEMENT_INDEX.json", {
        "schema_version": RESULT_SCHEMA,
        "request_id": request.request_id,
        "topic_id": request.topic_id,
        "gap_id": request.gap_id,
        "base_pool_path": str(request.base_pool_path),
        "base_pool_sha256": base_digest,
        "base_pool_rows": len(base_rows),
        "plan_source_audit_path": "PLAN_SOURCE_AUDIT.json",
        "plan_source_audit": dict(plan_report.get("source_audit") or {}),
        "derived_pool_path": "PLANNING_POOL.jsonl",
        "materials_markdown_path": "SUPPLEMENT_MATERIALS.md",
        "search_hits_path": "SEARCH_HITS.jsonl",
        "citation_anchors_path": "CITATION_ANCHORS.jsonl",
        "source_units": source_units,
        "citation_anchors": citation_anchors,
        "query_audits": query_audits,
        "fulfillment_judgment": aggregate_judgment,
        "citation_anchor_status": citation_readiness,
        "material_ready": material_ready,
        "planning_use_contract": citation_use_contract,
        "substantive_gap_status": substantive_gap_status,
        "network_call": network_call_seen,
        "model_call": model_call_seen,
        "limits": {
            "max_candidates": request.max_candidates,
            "max_acquisitions": request.max_acquisitions,
            "per_query_limit": request.per_query_limit,
        },
        "created_at_unix": time.time(),
    })
    return {
        "schema_version": RESULT_SCHEMA,
        "request_id": request.request_id,
        "output_dir": str(root),
        "derived_pool_path": str(root / "PLANNING_POOL.jsonl"),
        "materials_markdown_path": str(materials_path),
        "base_pool_copy_path": str(root / "BASE_PLANNING_POOL.jsonl"),
        "base_pool_sha256": base_digest,
        "source_units": len(source_units),
        "status": overall_status,
        "outcome": outcome,
        "citation_anchor_status": citation_readiness,
        "material_ready": material_ready,
        "planning_use_contract": citation_use_contract,
        "substantive_gap_status": substantive_gap_status,
        "network_call": network_call_seen,
        "model_call": model_call_seen,
        "fulfillment_judgment": aggregate_judgment,
    }


def _read_json_object(path: Path, *, error_code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanningSupplementError(error_code) from exc
    if not isinstance(value, Mapping):
        raise PlanningSupplementError(error_code)
    return dict(value)


def _parse_raw_judgment_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, Mapping) and "status" in payload and "evidence" in payload:
        return dict(payload)
    content: Any = None
    if isinstance(payload, Mapping):
        choices = payload.get("choices")
        if isinstance(choices, Sequence) and choices and isinstance(choices[0], Mapping):
            message = choices[0].get("message")
            if isinstance(message, Mapping):
                content = message.get("content")
        content = content or payload.get("content")
    if isinstance(content, Mapping) and "status" in content:
        return dict(content)
    if isinstance(content, str):
        candidate_text = content.strip()
        if candidate_text.startswith("```"):
            candidate_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate_text, flags=re.IGNORECASE)
        try:
            decoded = json.loads(candidate_text)
        except json.JSONDecodeError:
            return None
        return dict(decoded) if isinstance(decoded, Mapping) and "status" in decoded else None
    return None


def _latest_raw_judgment(unit_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[Path] = []
    for name in ("RAW_FULFILLMENT_JUDGMENT.json", "FULFILLMENT_JUDGMENT_RAW.json", "raw_judgment.json"):
        path = unit_dir / "judge" / name
        if path.is_file():
            candidates.append(path)
    raw_dir = unit_dir / "judge" / "raw_responses"
    if raw_dir.is_dir():
        candidates.extend(raw_dir.glob("*.raw"))
    for path in sorted(candidates, key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True):
        judgment = _parse_raw_judgment_file(path)
        if judgment is not None:
            return path, judgment
    return None


def _revalidation_aggregate(
    source_units: Sequence[Mapping[str, Any]],
    *,
    gap_question: str,
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    judged = [dict(row.get("fulfillment_judgment") or {}) for row in source_units if row.get("fulfillment_judgment")]
    statuses = [str(item.get("status") or "") for item in judged]
    if "fulfilled" in statuses:
        status, outcome = "fulfilled", "gap_fulfilled"
    elif "partial" in statuses:
        status, outcome = "partial", "gap_partially_fulfilled"
    elif source_units and all(row.get("status") in {"material_ready", "review_account_needed"} for row in source_units):
        status, outcome = "unjudgeable", "citation_anchor_only_secondary_source"
    elif any(item.get("reason") in {"judge_quote_not_found_in_snapshot", "judge_quote_anchor_below_threshold"} for item in judged):
        status, outcome = "unjudgeable", "judgment_quote_unanchored_local_repair_available"
    elif "unmet" in statuses:
        status, outcome = "unmet", "content_reviewed_gap_unmet_narrow_or_omit"
    elif judged:
        status, outcome = "unjudgeable", "judgment_unavailable"
    else:
        return dict(previous)
    default_action = (
        "Add the supported point with its evidence scope and limitations."
        if status == "fulfilled" else
        "Narrow the gap to supported evidence or omit it if no source supports it."
        if status in {"partial", "unmet"} else
        "Use the claim as reported in the review and preserve its citation lineage; do not claim direct review of the cited original study's full text."
        if outcome == "citation_anchor_only_secondary_source" else
        "Repair or revalidate the saved judgment locally before classifying this as a provider outage."
        if outcome == "judgment_quote_unanchored_local_repair_available" else
        "Resolve the missing judgment before making a scientific absence claim."
    )
    return {
        **dict(previous),
        "status": status,
        "outcome": outcome,
        "evidence": [evidence for item in judged for evidence in item.get("evidence", []) if isinstance(evidence, Mapping)],
        "limitations": [limit for item in judged for limit in item.get("limitations", []) if str(limit or "").strip()],
        "remaining_gap": next((str(item.get("remaining_gap")) for item in judged if item.get("status") == status and "remaining_gap" in item), gap_question),
        "outline_action": next((str(item.get("outline_action")) for item in judged if item.get("status") == status and item.get("outline_action")), default_action),
        "outline_action_recovered_by_policy": any(bool(item.get("outline_action_recovered_by_policy")) for item in judged),
        "remaining_gap_recovered_by_policy": any(bool(item.get("remaining_gap_recovered_by_policy")) for item in judged),
        "note": "Revalidated offline against the captured snapshot; no retrieval, acquisition, card, or model call was made.",
    }


def revalidate_planning_supplement_judgments(
    output_dir: str | Path,
    *,
    source_unit_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Re-validate saved model judgments against existing snapshots, without external calls.

    Original request, acquisition, snapshot, card, raw HTTP, and first judgment
    files are left untouched. New judgments and pool/index backups are written
    to a unique append-only revalidation directory.
    """

    root = Path(output_dir).resolve()
    index_path = root / "SUPPLEMENT_INDEX.json"
    request_path = root / "REQUEST.json"
    base_path = root / "BASE_PLANNING_POOL.jsonl"
    if not root.is_dir() or not index_path.is_file() or not request_path.is_file() or not base_path.is_file():
        raise PlanningSupplementError("revalidation_run_artifacts_missing")
    index = _read_json_object(index_path, error_code="revalidation_index_invalid")
    request_raw = _read_json_object(request_path, error_code="revalidation_request_invalid")
    gap_question = str(request_raw.get("gap_question") or "").strip()
    if not gap_question:
        raise PlanningSupplementError("revalidation_gap_question_missing")
    topic_id = str(request_raw.get("topic_id") or "")
    gap_id = str(request_raw.get("gap_id") or "")
    _, base_rows = _load_pool(base_path)
    pool_path = root / "PLANNING_POOL.jsonl"
    if pool_path.is_file():
        current_pool_bytes, current_pool_rows = _load_pool(pool_path)
    else:
        current_pool_bytes, current_pool_rows = base_path.read_bytes(), [dict(row) for row in base_rows]
    rows = [dict(item) for item in index.get("source_units", []) if isinstance(item, Mapping)]
    wanted = set(str(item) for item in (source_unit_ids or ()) if str(item))
    if wanted:
        rows = [row for row in rows if str(row.get("source_unit_id") or "") in wanted]
        missing = wanted - {str(row.get("source_unit_id") or "") for row in rows}
        if missing:
            raise PlanningSupplementError("revalidation_unknown_source_unit:" + ",".join(sorted(missing)))
    if not rows:
        raise PlanningSupplementError("revalidation_no_source_units")

    revalidation_id = time.strftime("run-%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + hashlib.sha256(
        (str(index.get("request_id") or "") + str(time.time_ns())).encode("utf-8")
    ).hexdigest()[:8]
    revalidation_root = root / "judgment_revalidations" / revalidation_id
    revalidation_root.mkdir(parents=True, exist_ok=False)
    index_bytes = index_path.read_bytes()
    pool_bytes = pool_path.read_bytes() if pool_path.is_file() else b""
    (revalidation_root / "PREVIOUS_SUPPLEMENT_INDEX.json").write_bytes(index_bytes)
    if pool_path.is_file():
        (revalidation_root / "PREVIOUS_PLANNING_POOL.jsonl").write_bytes(pool_bytes)

    selected_rows: list[dict[str, Any]] = []
    citation_anchors: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    local_quote_failure = False
    for original_index_row in rows:
        row = dict(original_index_row)
        source_unit_id = str(row.get("source_unit_id") or "")
        if not source_unit_id or Path(source_unit_id).name != source_unit_id:
            raise PlanningSupplementError("revalidation_source_unit_id_invalid")
        unit_dir = root / "source_units" / source_unit_id
        source_file = unit_dir / "SOURCE_UNIT.json"
        source = _read_json_object(source_file, error_code="revalidation_source_unit_missing")
        source_kind = str(row.get("source_kind") or source.get("source_kind") or "")
        candidate_path = unit_dir / str(source.get("candidate_record_path") or "CANDIDATE_RECORD.json")
        candidate_record = _read_json_object(candidate_path, error_code="revalidation_candidate_record_missing")
        candidate = _Candidate(
            record=candidate_record,
            source_kind=source_kind,
            lineage=dict(source.get("lineage") or row.get("lineage") or {}),
            source_unit_id=source_unit_id,
            identity_audit=dict(source.get("identity_audit") or row.get("identity_audit") or {}),
        )
        per_unit_root = revalidation_root / "source_units" / source_unit_id
        per_unit_root.mkdir(parents=True, exist_ok=True)
        prior_judgment_path = unit_dir / "FULFILLMENT_JUDGMENT.json"
        prior_judgment_bytes = prior_judgment_path.read_bytes() if prior_judgment_path.is_file() else b""
        audit: dict[str, Any] = {
            "source_unit_id": source_unit_id,
            "source_kind": source_kind,
            "original_judgment_path": str(prior_judgment_path),
            "original_judgment_sha256": hashlib.sha256(prior_judgment_bytes).hexdigest() if prior_judgment_bytes else "",
            "network_call": False,
            "model_call": False,
            "acquisition_call": False,
            "card_call": False,
        }
        if source_kind == "reviewed_reference":
            citation_request = SimpleNamespace(topic_id=topic_id, gap_id=gap_id)
            review_context_path = _review_account_context(candidate)
            candidate.lineage["review_context_path"] = review_context_path
            row["lineage"] = {**dict(row.get("lineage") or {}), "review_context_path": review_context_path}
            anchor = _citation_anchor(candidate, request=citation_request, review_context_path=review_context_path)
            citation_anchors.append(anchor)
            _write_json(per_unit_root / "CITATION_ANCHOR.json", anchor)
            row["status"] = anchor["status"]
            row["citation_anchor_path"] = str((per_unit_root / "CITATION_ANCHOR.json").relative_to(root))
            row.pop("fulfillment_judgment", None)
            row["revalidated_at"] = revalidation_id
            selected_rows.append(row)
            audit.update({"result": row["status"], "reason": "secondary_source_citation_anchor_only"})
            audit_rows.append(audit)
            continue

        # Successful prior judgments are immutable and do not need replay.
        if str(row.get("status") or "") in {"fulfilled", "partial", "unmet"}:
            selected_rows.append(row)
            audit.update({"result": "skipped", "reason": "source_unit_already_judged"})
            audit_rows.append(audit)
            continue
        raw_result = _latest_raw_judgment(unit_dir)
        if raw_result is None:
            selected_rows.append(row)
            audit.update({"result": "skipped", "reason": "no_saved_raw_model_judgment"})
            audit_rows.append(audit)
            continue
        snapshot_path = Path(str(source.get("snapshot_path") or ""))
        snapshot = SimpleNamespace(root=snapshot_path, snapshot_id=source.get("snapshot_id", ""))
        reading_text = _snapshot_reading_text(snapshot)
        card_path = Path(str(source.get("card_path") or ""))
        if not card_path.is_file():
            fallback_card_dir = Path(str(source.get("card_output_dir") or unit_dir / "card"))
            card_path = fallback_card_dir / "PAPER_READING_CARD.json"
        try:
            card = _read_json_object(card_path, error_code="revalidation_card_missing")
        except PlanningSupplementError:
            card = {}
        if not reading_text.strip() or not card:
            judgment = _unjudgeable("revalidation_snapshot_card_or_raw_judgment_missing", gap_question)
            raw_path = None
            raw_judgment = None
        else:
            raw_path, raw_judgment = raw_result
            judgment = _validate_fulfillment_judgment(
                raw_judgment,
                reading_text=reading_text,
                gap_question=gap_question,
            )
        if judgment.get("reason") in {"judge_quote_not_found_in_snapshot", "judge_quote_anchor_below_threshold"}:
            local_quote_failure = True
        judgment_path = per_unit_root / "FULFILLMENT_JUDGMENT.json"
        if raw_judgment is not None:
            _write_json(per_unit_root / "MODEL_JUDGMENT_EXTRACTED.json", raw_judgment)
        _write_json(judgment_path, judgment)
        row.update({
            "status": judgment["status"],
            "fulfillment_judgment": judgment,
            "judgment_path": str(judgment_path.relative_to(root)),
            "revalidated_at": revalidation_id,
            "snapshot_path": str(snapshot_path),
            "snapshot_sha256": hashlib.sha256(reading_text.encode("utf-8")).hexdigest() if reading_text else "",
            "card_path": str(card_path),
        })
        if raw_path is not None:
            audit.update({
                "raw_model_response_path": str(raw_path),
                "raw_model_response_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                "parsed_model_judgment_path": str((per_unit_root / "MODEL_JUDGMENT_EXTRACTED.json").relative_to(root)),
            })
        if judgment["status"] in {"fulfilled", "partial"}:
            candidate.identity_audit = _identity_match(candidate, current_pool_rows)
            pool_action = _apply_source_unit_to_pool(
                current_pool_rows,
                candidate,
                card,
                judgment,
                card_path=str(card_path),
                material_depth=str(source.get("material_depth") or "unknown"),
                gap_id=gap_id,
                judgment_path=str(judgment_path),
            )
            row["pool_action"] = pool_action
        audit.update({
            "result": judgment["status"],
            "reason": judgment.get("reason", ""),
            "judgment_path": str(judgment_path.relative_to(root)),
            "quote_anchor_audits": [item.get("quote_anchor_audit") for item in judgment.get("evidence", []) if isinstance(item, Mapping)],
            "quote_anchor_failures": judgment.get("quote_anchor_failures", []),
        })
        selected_rows.append(row)
        audit_rows.append(audit)

    _write_jsonl(revalidation_root / "CITATION_ANCHORS.jsonl", citation_anchors)
    updated_index = dict(index)
    if wanted:
        untouched = [dict(row) for row in index.get("source_units", []) if isinstance(row, Mapping) and str(row.get("source_unit_id") or "") not in wanted]
        new_source_rows = [*untouched, *selected_rows]
        ordering = {str(row.get("source_unit_id") or ""): index for index, row in enumerate(index.get("source_units", [])) if isinstance(row, Mapping)}
        new_source_rows.sort(key=lambda row: ordering.get(str(row.get("source_unit_id") or ""), len(ordering)))
    else:
        selected_ids = {str(row.get("source_unit_id") or "") for row in selected_rows}
        by_id = {str(row.get("source_unit_id") or ""): row for row in selected_rows}
        new_source_rows = [by_id.get(str(row.get("source_unit_id") or ""), dict(row)) for row in index.get("source_units", []) if isinstance(row, Mapping)]
    updated_index["source_units"] = new_source_rows
    updated_index["citation_anchors_path"] = str((revalidation_root / "CITATION_ANCHORS.jsonl").relative_to(root))
    updated_index["citation_anchors"] = citation_anchors
    history = list(updated_index.get("revalidation_history") or [])
    history.append({
        "revalidation_id": revalidation_id,
        "audit_path": str((revalidation_root / "REVALIDATION_AUDIT.json").relative_to(root)),
        "source_unit_count": len(audit_rows),
        "previous_index_sha256": hashlib.sha256(index_bytes).hexdigest(),
        "previous_pool_sha256": hashlib.sha256(current_pool_bytes).hexdigest(),
        "network_call": False,
        "model_call": False,
        "acquisition_call": False,
        "card_call": False,
    })
    updated_index["revalidation_history"] = history
    aggregate = _revalidation_aggregate(
        new_source_rows,
        gap_question=gap_question,
        previous=updated_index.get("fulfillment_judgment") or {},
    )
    if local_quote_failure and aggregate.get("outcome") not in {"gap_fulfilled", "gap_partially_fulfilled"}:
        aggregate.update({"status": "unjudgeable", "outcome": "judgment_quote_unanchored_local_repair_available"})
    citation_readiness = _citation_anchor_status(citation_anchors)
    citation_material_ready = any(bool(row.get("material_ready")) for row in citation_anchors)
    source_material_ready = any(
        bool((row.get("fulfillment_judgment") or {}).get("material_ready", row.get("status") in {"fulfilled", "partial"}))
        for row in new_source_rows
    )
    material_ready = citation_material_ready or source_material_ready
    planning_use_contract = {
        "status": "equal_for_ready_review_reported_claims" if citation_material_ready else "pending_or_not_applicable",
        "review_lineage_preserved": True,
        "original_fulltext_required": False,
        "direct_original_content_verified": False,
    }
    substantive_gap_status = "unassessed" if aggregate.get("outcome") == "citation_anchor_only_secondary_source" else aggregate.get("status", "unjudgeable")
    aggregate["citation_anchor_status"] = citation_readiness
    aggregate["material_ready"] = material_ready
    aggregate["planning_use_contract"] = planning_use_contract
    aggregate["substantive_gap_status"] = substantive_gap_status
    updated_index["fulfillment_judgment"] = aggregate
    updated_index["citation_anchor_status"] = citation_readiness
    updated_index["material_ready"] = material_ready
    updated_index["planning_use_contract"] = planning_use_contract
    updated_index["substantive_gap_status"] = substantive_gap_status
    updated_index["latest_derived_pool_sha256"] = hashlib.sha256(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n" for row in current_pool_rows).encode("utf-8")
    ).hexdigest()
    _write_jsonl(pool_path, current_pool_rows)
    _write_json(revalidation_root / "REVALIDATION_AUDIT.json", {
        "schema_version": "optomind.planning_supplement.judgment_revalidation.v1",
        "revalidation_id": revalidation_id,
        "request_id": index.get("request_id"),
        "source_units": audit_rows,
        "citation_anchors_path": str((revalidation_root / "CITATION_ANCHORS.jsonl").relative_to(root)),
        "previous_index_backup": str((revalidation_root / "PREVIOUS_SUPPLEMENT_INDEX.json").relative_to(root)),
        "previous_pool_backup": str((revalidation_root / "PREVIOUS_PLANNING_POOL.jsonl").relative_to(root)) if pool_path.is_file() else "",
        "previous_pool_sha256": hashlib.sha256(current_pool_bytes).hexdigest(),
        "current_pool_sha256": hashlib.sha256(pool_path.read_bytes()).hexdigest(),
        "network_call": False,
        "model_call": False,
        "acquisition_call": False,
        "card_call": False,
    })
    _write_json(index_path, updated_index)
    return {
        "schema_version": "optomind.planning_supplement.judgment_revalidation.v1",
        "revalidation_id": revalidation_id,
        "output_dir": str(root),
        "audit_path": str(revalidation_root / "REVALIDATION_AUDIT.json"),
        "derived_pool_path": str(pool_path),
        "source_unit_count": len(audit_rows),
        "citation_anchor_count": len(citation_anchors),
        "status": aggregate.get("status", "unjudgeable"),
        "outcome": aggregate.get("outcome", "judgment_unavailable"),
        "citation_anchor_status": citation_readiness,
        "material_ready": material_ready,
        "planning_use_contract": planning_use_contract,
        "substantive_gap_status": substantive_gap_status,
        "network_call": False,
        "model_call": False,
        "acquisition_call": False,
        "card_call": False,
    }


def run_qwen_fulfillment_judge(
    *,
    key_file: str | Path,
    budget_ledger_path: str | Path,
    budget_limit_cny: float,
    max_output_tokens: int = 3072,
    thinking_budget: int = 2048,
    timeout_seconds: float = 300.0,
) -> QwenFulfillmentJudge:
    """Factory spelling for callers that want the live Qwen judge."""

    return QwenFulfillmentJudge(
        key_file=key_file,
        budget_ledger_path=budget_ledger_path,
        budget_limit_cny=budget_limit_cny,
        max_output_tokens=max_output_tokens,
        thinking_budget=thinking_budget,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "REQUEST_SCHEMA",
    "RESULT_SCHEMA",
    "PlanningSupplementError",
    "OutputDirectoryError",
    "PlanningSupplementRequest",
    "allocate_supplement_attempt",
    "load_planning_supplement_request",
    "preflight_planning_supplement",
    "run_planning_supplement",
    "normalize_practical_fulfillment_judgment",
    "revalidate_planning_supplement_judgments",
    "QwenFulfillmentJudge",
    "run_qwen_fulfillment_judge",
]
