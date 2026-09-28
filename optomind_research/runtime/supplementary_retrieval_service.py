"""Resumable, idempotent, single-writer task service for supplementary retrieval.

The service persists tasks in SQLite with WAL journaling and explicit
``BEGIN IMMEDIATE`` transactions so every state transition is atomic and
serialized.  Network retrieval and materialization are injected callbacks; this
module never performs network or model calls itself.

Task states:
  queued -> running -> materializing -> committed
                                      -> no_progress (ran but no adequate material)
  running/materializing -> queued        (crash recovery)
  any execution error   -> failed        (explicit retry preserves history)

Re-submitting an identical committed or no_progress task returns reuse evidence
without invoking any callback.  A failed task requires an explicit retry and
keeps the full prior attempt history.  Visual tasks use the same queue and
contracts but dispatch to distinct visual callbacks.

Portfolio guardrails (max 200 references, background-only fraction <= 0.25)
belong to final review portfolio selection and are never enforced at
material-library commit: an adequate materialization reporting more than 200
accumulated library works still commits.

Historical queries can be imported durably through
``import_history_queries`` and participate in dedup together with queued and
running task queries.  Failed task queries never poison unrelated future
tasks as successful historical duplicates.

Execution metadata (idempotency key, task fingerprint, attempt ID, route) is
passed to every retrieval/materialization callback so external material
writers can upsert idempotently after recovery or retry.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .supplementary_query_dedup import (
    ACTION_MERGE,
    SOURCE_BATCH,
    SOURCE_HISTORICAL,
    SOURCE_QUEUED,
    KnownQuery,
    QueryCandidate,
    finalize_dedup,
    normalize_query,
    query_hash,
    stage1_deduplicate,
)
from .supplementary_retrieval_contract import (
    ContextRegistry,
    SupplementaryRetrievalTask,
    task_fingerprint,
    validate_materialization_policy,
    validate_task_context,
)


SERVICE_SCHEMA_VERSION = "supplementary_retrieval.service.v1"
EXECUTION_META_SCHEMA_VERSION = "supplementary_retrieval.execution_meta.v1"
RETRIEVAL_CHECKPOINT_SCHEMA_VERSION = (
    "supplementary_retrieval.retrieval_checkpoint.v1"
)
RETRIEVAL_CHECKPOINT_KIND = "retrieval_checkpoint"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_MATERIALIZING = "materializing"
STATUS_COMMITTED = "committed"
STATUS_FAILED = "failed"
STATUS_NO_PROGRESS = "no_progress"

VALID_STATUSES = {
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_MATERIALIZING,
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_NO_PROGRESS,
}

ROUTE_LITERATURE = "literature"
ROUTE_VISUAL = "visual"

EXECUTABLE_DECISIONS = {"unique", "same_task_replay", "keep"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _is_retrieval_checkpoint_payload(value: Any) -> bool:
    """Return whether a stored result payload is a valid-shape checkpoint."""

    return (
        isinstance(value, Mapping)
        and value.get("kind") == RETRIEVAL_CHECKPOINT_KIND
        and value.get("schema_version") == RETRIEVAL_CHECKPOINT_SCHEMA_VERSION
    )


@dataclass(slots=True)
class RetrievalOutcome:
    """Result of the injected retrieval callback."""

    candidates: list[dict[str, Any]] = field(default_factory=list)
    adequate: bool = False
    query_runs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    route: str = ROUTE_LITERATURE

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "adequate": self.adequate,
            "candidate_count": len(self.candidates),
            "query_runs": list(self.query_runs),
            "metadata": dict(self.metadata),
        }

    def to_checkpoint_dict(self) -> dict[str, Any]:
        """Full state needed to reconstruct this outcome after a crash."""

        return {
            "route": self.route,
            "adequate": self.adequate,
            "candidates": [
                dict(candidate)
                for candidate in self.candidates
                if isinstance(candidate, Mapping)
            ],
            "query_runs": [
                dict(run) for run in self.query_runs if isinstance(run, Mapping)
            ],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_checkpoint_dict(
        cls, payload: Mapping[str, Any]
    ) -> "RetrievalOutcome":
        """Reconstruct a retrieval outcome from checkpoint state."""

        return cls(
            candidates=[
                dict(candidate)
                for candidate in (payload.get("candidates") or [])
                if isinstance(candidate, Mapping)
            ],
            adequate=bool(payload.get("adequate")),
            query_runs=[
                dict(run)
                for run in (payload.get("query_runs") or [])
                if isinstance(run, Mapping)
            ],
            metadata=dict(payload.get("metadata") or {}),
            route=str(payload.get("route") or ROUTE_LITERATURE),
        )


@dataclass(slots=True)
class MaterializationOutcome:
    """Result of the injected materialization callback."""

    sources: list[dict[str, Any]] = field(default_factory=list)
    adequate: bool = False
    total_references: int = 0
    background_only_references: int = 0
    materialized_route: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adequate": self.adequate,
            "source_count": len(self.sources),
            "total_references": self.total_references,
            "background_only_references": self.background_only_references,
            "materialized_route": self.materialized_route,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ServiceCallbacks:
    """Injected offline-safe callbacks; the service never calls the network."""

    retrieve: Callable[..., Any] | None = None
    materialize: Callable[..., Any] | None = None
    visual_retrieve: Callable[..., Any] | None = None
    visual_materialize: Callable[..., Any] | None = None
    adjudicator: Callable[..., Any] | None = None


@dataclass(slots=True)
class SubmissionResult:
    """Outcome of ``submit``, including reuse evidence for replays."""

    reused: bool
    idempotency_key: str
    task_id: str
    status: str
    reuse_reason: str = ""
    errors: tuple[str, ...] = ()
    attempt_history: tuple[dict[str, Any], ...] = ()
    result: dict[str, Any] | None = None
    query_decisions: tuple[dict[str, Any], ...] = ()


@dataclass(slots=True)
class RunResult:
    """Outcome of one service execution attempt."""

    idempotency_key: str
    task_id: str
    status: str
    attempt_id: str
    route: str
    reason: str = ""
    result: dict[str, Any] | None = None
    error: str = ""


_DDL = """
CREATE TABLE IF NOT EXISTS supplementary_tasks (
    idempotency_key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    gap_type TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    visual_route INTEGER NOT NULL,
    context_refs_json TEXT NOT NULL,
    context_snapshot TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    queries_json TEXT NOT NULL,
    merge_refs_json TEXT NOT NULL DEFAULT '[]',
    dedup_outcome_json TEXT NOT NULL DEFAULT '{}',
    source_provenance_json TEXT NOT NULL,
    history_refs_json TEXT NOT NULL,
    success_criteria_json TEXT NOT NULL,
    material_requirements_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    attempts_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT,
    error TEXT,
    reuse_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_supplementary_tasks_status
    ON supplementary_tasks(status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_supplementary_tasks_task_id
    ON supplementary_tasks(task_id);

CREATE TABLE IF NOT EXISTS supplementary_history_queries (
    history_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    normalized TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    query_text TEXT NOT NULL,
    source_task_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_supplementary_history_normalized
    ON supplementary_history_queries(normalized, source);
"""


class SupplementaryRetrievalService:
    """SQLite-backed, resumable, idempotent single-writer task service."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        callbacks: ServiceCallbacks | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._callbacks = callbacks or ServiceCallbacks()
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_DDL)
        columns = {
            str(row[1])
            for row in self._conn.execute(
                "PRAGMA table_info(supplementary_tasks)"
            ).fetchall()
        }
        if "metadata_json" not in columns:
            self._conn.execute(
                "ALTER TABLE supplementary_tasks "
                "ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection."""

        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "SupplementaryRetrievalService":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    @contextmanager
    def _transaction(self):
        """Yield a connection inside one serialized immediate transaction."""

        connection = self._conn
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    @staticmethod
    def _row_to_task(row: Mapping[str, Any]) -> SupplementaryRetrievalTask:
        queries = _json_loads(row["queries_json"], [])
        return SupplementaryRetrievalTask(
            task_id=str(row["task_id"]),
            gap_type=str(row["gap_type"]),
            context_refs=tuple(_json_loads(row["context_refs_json"], [])),
            priority=int(row["priority"] or 0),
            source_provenance=_json_loads(row["source_provenance_json"], {}),
            history_refs=tuple(_json_loads(row["history_refs_json"], [])),
            success_criteria=tuple(_json_loads(row["success_criteria_json"], [])),
            material_requirements=tuple(
                _json_loads(row["material_requirements_json"], [])
            ),
            retrieval_queries=tuple(
                str(record.get("text") or "")
                for record in queries
                if isinstance(record, dict) and record.get("text")
            ),
            visual_route=bool(row["visual_route"]),
            metadata=_json_loads(row["metadata_json"], {}),
        )

    def import_history_queries(
        self,
        records: Iterable[Any],
        *,
        source: str = "external_history",
    ) -> dict[str, Any]:
        """Durably register historical queries for dedup without fake tasks.

        Each record is a mapping with ``text`` (or ``query``) and optional
        ``history_id``/``query_id`` and ``source_task_id``, or a plain query
        string.  Normalized query identity and its hash are persisted.
        Re-importing an existing history ID or an identical normalized query
        under the same source is an idempotent no-op.
        """

        imported = 0
        skipped = 0
        errors: list[str] = []
        source_label = str(source or "external_history").strip() or "external_history"
        with self._transaction() as connection:
            for raw in records:
                if isinstance(raw, Mapping):
                    item = raw
                else:
                    item = {"text": raw}
                text = str(
                    item.get("text") or item.get("query") or ""
                ).strip()
                normalized = normalize_query(text)
                if not normalized:
                    errors.append(f"empty_query:{raw!r}")
                    continue
                history_id = str(
                    item.get("history_id")
                    or item.get("query_id")
                    or ""
                ).strip()
                if not history_id:
                    history_id = (
                        "history:"
                        + hashlib.sha256(
                            f"{source_label}\0{normalized}".encode("utf-8")
                        ).hexdigest()[:16]
                    )
                exists = connection.execute(
                    "SELECT 1 FROM supplementary_history_queries WHERE history_id=?",
                    (history_id,),
                ).fetchone()
                duplicate_normalized = connection.execute(
                    "SELECT 1 FROM supplementary_history_queries "
                    "WHERE normalized=? AND source=?",
                    (normalized, source_label),
                ).fetchone()
                if exists is not None or duplicate_normalized is not None:
                    skipped += 1
                    continue
                connection.execute(
                    """
                    INSERT INTO supplementary_history_queries(
                        history_id, source, normalized, query_hash,
                        query_text, source_task_id, created_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        history_id,
                        source_label,
                        normalized,
                        query_hash(text),
                        text,
                        str(item.get("source_task_id") or item.get("task_id") or ""),
                        _now(),
                    ),
                )
                imported += 1
        return {
            "source": source_label,
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
        }

    def list_history_queries(self) -> list[dict[str, Any]]:
        """Return all durable imported historical queries."""

        rows = self._conn.execute(
            "SELECT * FROM supplementary_history_queries ORDER BY created_at, history_id"
        ).fetchall()
        return [
            {
                "history_id": str(row["history_id"]),
                "source": str(row["source"]),
                "normalized": str(row["normalized"]),
                "query_hash": str(row["query_hash"]),
                "query_text": str(row["query_text"]),
                "source_task_id": str(row["source_task_id"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def history_query_count(self) -> int:
        """Return the number of durable imported historical queries."""

        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM supplementary_history_queries"
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def _known_queries_for_dedup(self) -> list[KnownQuery]:
        """Collect historical, queued, and active queries from durable storage.

        Failed task queries are deliberately excluded so a failed callback
        cannot poison unrelated future tasks as successful historical
        duplicates.  Explicit retry of an identical failed task is handled by
        the idempotency-key path before dedup.
        """

        known: list[KnownQuery] = []
        history_rows = self._conn.execute(
            "SELECT * FROM supplementary_history_queries"
        ).fetchall()
        for row in history_rows:
            known.append(
                KnownQuery(
                    query_id=str(row["history_id"]),
                    text=str(row["query_text"]),
                    source_task_id=str(row["source_task_id"]) or str(row["history_id"]),
                    source=SOURCE_HISTORICAL,
                )
            )
        task_rows = self._conn.execute(
            "SELECT task_id, status, queries_json FROM supplementary_tasks"
        ).fetchall()
        for row in task_rows:
            status = str(row["status"])
            if status == STATUS_FAILED:
                continue
            source = (
                SOURCE_HISTORICAL
                if status in {STATUS_COMMITTED, STATUS_NO_PROGRESS}
                else SOURCE_QUEUED
            )
            queries = _json_loads(row["queries_json"], [])
            for record in queries:
                if not isinstance(record, dict) or not record.get("text"):
                    continue
                known.append(
                    KnownQuery(
                        query_id=str(record.get("query_id") or ""),
                        text=str(record["text"]),
                        source_task_id=str(
                            record.get("source_task_id") or row["task_id"]
                        ),
                        source=source,
                    )
                )
        return known

    def _idempotency_key(
        self,
        task: SupplementaryRetrievalTask,
        registry: ContextRegistry,
        query_texts: Sequence[str],
    ) -> str:
        payload = {
            "schema_version": SERVICE_SCHEMA_VERSION,
            "task_fingerprint": task_fingerprint(task, registry),
            "queries": sorted({str(q).strip() for q in query_texts if str(q).strip()}),
        }
        raw = _json_dumps(payload).encode("utf-8")
        return "supplementary:" + hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _normalized_unique_queries(values: Sequence[str]) -> list[str]:
        """Keep the first raw variant per normalized query form."""

        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            normalized = normalize_query(text)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(text)
        return result

    @staticmethod
    def _local_generation_metadata(
        records: Sequence[Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Aggregate local coverage metadata by normalized query text.

        Only ``query``/``text``, ``coverage_ids``, ``reason``, and
        ``generation_reasons`` are trusted.  Model-provided query_id,
        provenance, and dedup fields are deliberately ignored.
        """

        meta: dict[str, dict[str, Any]] = {}
        for record in records:
            if not isinstance(record, Mapping):
                continue
            text = str(
                record.get("query") or record.get("text") or ""
            ).strip()
            if not text:
                continue
            norm = normalize_query(text)
            entry = meta.setdefault(
                norm, {"coverage_ids": [], "generation_reasons": []}
            )
            raw_coverage = record.get("coverage_ids") or []
            if isinstance(raw_coverage, str):
                raw_coverage = [raw_coverage]
            for coverage_id in raw_coverage:
                value = str(coverage_id).strip()
                if value and value not in entry["coverage_ids"]:
                    entry["coverage_ids"].append(value)
            raw_reasons = record.get("generation_reasons") or record.get(
                "reason"
            ) or []
            if isinstance(raw_reasons, str):
                raw_reasons = [raw_reasons]
            for reason in raw_reasons:
                value = str(reason).strip()
                if value and value not in entry["generation_reasons"]:
                    entry["generation_reasons"].append(value)
        return meta

    @staticmethod
    def _merge_metadata_into(
        target: dict[str, Any],
        source: Mapping[str, Any],
    ) -> None:
        """Union coverage_ids and generation_reasons into a target record."""

        for key in ("coverage_ids", "generation_reasons"):
            existing = list(target.get(key) or [])
            for value in source.get(key) or []:
                value = str(value).strip()
                if value and value not in existing:
                    existing.append(value)
            target[key] = existing

    def submit(
        self,
        task: SupplementaryRetrievalTask,
        registry: ContextRegistry,
        queries: Sequence[str] | None = None,
        *,
        allow_retry: bool = False,
        query_records: Sequence[Mapping[str, Any]] | None = None,
    ) -> SubmissionResult:
        """Validate, deduplicate queries, and durably enqueue a task.

        Identical committed/no_progress tasks are replayed without callbacks.
        Identical failed tasks require ``allow_retry=True`` to enqueue again.
        Normalized-equivalent variants within one submission collapse to one
        retrieval query before stage-1 dedup.
        Optional local ``query_records`` carry Python-derived coverage_ids and
        generation reasons that are unioned by normalized text and persisted on
        the executable queries_json records (and transferred on same-batch
        merges).  String-only callers remain fully supported.
        """

        errors = list(task.validate())
        errors.extend(validate_task_context(task, registry))
        resolved_context = registry.resolve(task.context_refs)
        if "materialization_policy" in resolved_context:
            errors.extend(
                validate_materialization_policy(resolved_context["materialization_policy"])
            )
        if errors:
            raise ValueError("; ".join(errors))

        if queries is not None:
            raw_queries = [
                str(q).strip() for q in queries if str(q).strip()
            ]
        elif query_records:
            raw_queries = [
                str(record.get("query") or record.get("text") or "").strip()
                for record in query_records
                if isinstance(record, Mapping)
            ]
            raw_queries = [q for q in raw_queries if q]
        else:
            raw_queries = [
                str(q).strip()
                for q in task.retrieval_queries
                if str(q).strip()
            ]
        meta_by_norm = self._local_generation_metadata(
            query_records or ()
        )
        for query in raw_queries:
            meta_by_norm.setdefault(
                normalize_query(query),
                {"coverage_ids": [], "generation_reasons": []},
            )
        query_texts = self._normalized_unique_queries(raw_queries)
        idempotency_key = self._idempotency_key(task, registry, query_texts)
        existing = self._conn.execute(
            "SELECT * FROM supplementary_tasks WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return self._handle_existing(existing, allow_retry=allow_retry)

        candidates = [
            QueryCandidate(
                query_id=(
                    f"{task.task_id}:"
                    + hashlib.sha256(q.encode("utf-8")).hexdigest()[:16]
                ),
                text=q,
                source_task_id=task.task_id,
                batch_id="submit",
            )
            for q in query_texts
        ]
        meta_by_query_id = {
            candidate.query_id: meta_by_norm.get(
                normalize_query(candidate.text),
                {"coverage_ids": [], "generation_reasons": []},
            )
            for candidate in candidates
        }
        text_by_query_id = {candidate.query_id: candidate.text for candidate in candidates}
        known = self._known_queries_for_dedup()
        known.extend(
            KnownQuery(
                query_id=candidate.query_id,
                text=candidate.text,
                source_task_id=candidate.source_task_id,
                source=SOURCE_BATCH,
            )
            for candidate in candidates
        )
        known.extend(self._registry_queries_as_known(registry.fields, task.task_id))
        known_text_by_id = {item.query_id: item.text for item in known}

        stage1 = stage1_deduplicate(candidates, known)
        outcome = finalize_dedup(stage1, adjudicator=self._callbacks.adjudicator)
        decisions_by_id = {decision.query_id: decision for decision in outcome.decisions}

        execution_records: list[dict[str, Any]] = []
        seen_execution_texts: set[str] = set()
        for decision in outcome.kept_queries:
            text = text_by_query_id.get(decision.query_id, "")
            if not text or normalize_query(text) in seen_execution_texts:
                continue
            seen_execution_texts.add(normalize_query(text))
            execution_records.append(
                {
                    "query_id": decision.query_id,
                    "text": text,
                    "source_task_id": decision.source_task_id or task.task_id,
                    "decision": decision.decision,
                    "reasons": list(decision.reasons),
                    "needs_semantic_review": decision.needs_semantic_review,
                    "merged_into_query_id": decision.merged_into_query_id,
                    "preserved_task_ids": sorted(
                        set(decision.preserved_task_ids)
                        | {decision.source_task_id or task.task_id}
                    ),
                    "coverage_ids": list(
                        meta_by_query_id.get(decision.query_id, {}).get(
                            "coverage_ids", []
                        )
                    ),
                    "generation_reasons": list(
                        meta_by_query_id.get(decision.query_id, {}).get(
                            "generation_reasons", []
                        )
                    ),
                }
            )

        merge_refs: list[dict[str, Any]] = []
        for decision in outcome.merged_queries:
            target_id = decision.merged_into_query_id
            target_decision = decisions_by_id.get(target_id)
            target_text = known_text_by_id.get(target_id, "")
            preserved = sorted(set(decision.preserved_task_ids))
            target_in_submission = target_decision is not None
            merged_meta = meta_by_query_id.get(decision.query_id, {})
            if (
                target_in_submission
                and target_decision.decision in EXECUTABLE_DECISIONS
            ):
                record = next(
                    (
                        item
                        for item in execution_records
                        if item["query_id"] == target_id
                    ),
                    None,
                )
                if record is not None:
                    record["preserved_task_ids"] = sorted(
                        set(record["preserved_task_ids"]) | set(preserved)
                    )
                    self._merge_metadata_into(record, merged_meta)
                    target_text = record["text"]
            merge_refs.append(
                {
                    "query_id": decision.query_id,
                    "text": text_by_query_id.get(decision.query_id, ""),
                    "decision": "merge",
                    "reasons": list(decision.reasons),
                    "merged_into_query_id": target_id,
                    "merged_into_text": target_text,
                    "target_in_submission": target_in_submission,
                    "preserved_task_ids": preserved,
                    "coverage_ids": list(
                        merged_meta.get("coverage_ids", [])
                    ),
                    "generation_reasons": list(
                        merged_meta.get("generation_reasons", [])
                    ),
                }
            )

        no_progress_reason = ""
        status = STATUS_QUEUED
        if query_texts and not execution_records:
            status = STATUS_NO_PROGRESS
            no_progress_reason = (
                "all_queries_merged" if merge_refs else "all_queries_duplicate"
            )
        elif not query_texts:
            status = STATUS_NO_PROGRESS
            no_progress_reason = "no_queries"
        result_payload = (
            {
                "reason": no_progress_reason,
                "merge_refs": merge_refs,
                "dedup_outcome": outcome.to_dict(),
            }
            if status == STATUS_NO_PROGRESS
            else None
        )
        now = _now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO supplementary_tasks(
                    idempotency_key, task_id, gap_type, status, priority,
                    visual_route, context_refs_json, context_snapshot,
                    fingerprint, queries_json, merge_refs_json,
                    dedup_outcome_json, source_provenance_json,
                    history_refs_json, success_criteria_json,
                    material_requirements_json, metadata_json,
                    attempt_count, attempts_json,
                    result_json, error, reuse_reason, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    idempotency_key,
                    task.task_id,
                    task.gap_type,
                    status,
                    int(task.priority or 0),
                    1 if task.is_visual() else 0,
                    _json_dumps(sorted(set(task.context_refs))),
                    _json_dumps(resolved_context),
                    task_fingerprint(task, registry),
                    _json_dumps(execution_records),
                    _json_dumps(merge_refs),
                    _json_dumps(outcome.to_dict()),
                    _json_dumps(task.source_provenance),
                    _json_dumps(sorted(set(task.history_refs))),
                    _json_dumps(sorted(set(task.success_criteria))),
                    _json_dumps(sorted(set(task.material_requirements))),
                    _json_dumps(task.metadata),
                    0,
                    "[]",
                    _json_dumps(result_payload) if result_payload is not None else None,
                    None,
                    no_progress_reason or None,
                    now,
                    now,
                ),
            )
        return SubmissionResult(
            reused=False,
            idempotency_key=idempotency_key,
            task_id=task.task_id,
            status=status,
            reuse_reason=no_progress_reason or None,
            query_decisions=tuple(decision.to_dict() for decision in outcome.decisions),
        )

    @staticmethod
    def _registry_queries_as_known(
        registry_fields: Mapping[str, Any],
        task_id: str,
    ) -> list[KnownQuery]:
        """Convert registry historical/concurrent query cells into known queries."""

        known: list[KnownQuery] = []
        for index, item in enumerate(registry_fields.get("historical_queries") or []):
            text = item if isinstance(item, str) else str(item.get("text") or item.get("query") or "")
            if not text:
                continue
            source_task_id = (
                f"ctx-history:{index}"
                if isinstance(item, str)
                else str(
                    item.get("source_task_id")
                    or item.get("task_id")
                    or f"ctx-history:{index}"
                )
            )
            known.append(
                KnownQuery(
                    query_id=f"ctx-history:{index}",
                    text=text,
                    source_task_id=source_task_id,
                    source=SOURCE_HISTORICAL,
                )
            )
        for index, item in enumerate(registry_fields.get("concurrent_queries") or []):
            text = item if isinstance(item, str) else str(item.get("text") or item.get("query") or "")
            if not text:
                continue
            source_task_id = (
                f"ctx-concurrent:{index}"
                if isinstance(item, str)
                else str(
                    item.get("source_task_id")
                    or item.get("task_id")
                    or f"ctx-concurrent:{index}"
                )
            )
            known.append(
                KnownQuery(
                    query_id=f"ctx-concurrent:{index}",
                    text=text,
                    source_task_id=source_task_id,
                    source=SOURCE_QUEUED,
                )
            )
        return known

    def _handle_existing(
        self,
        row: Mapping[str, Any],
        *,
        allow_retry: bool,
    ) -> SubmissionResult:
        key = str(row["idempotency_key"])
        task_id = str(row["task_id"])
        status = str(row["status"])
        attempts = tuple(_json_loads(row["attempts_json"], []))
        result = _json_loads(row["result_json"], None)
        if status in {STATUS_COMMITTED, STATUS_NO_PROGRESS}:
            return SubmissionResult(
                reused=True,
                idempotency_key=key,
                task_id=task_id,
                status=status,
                reuse_reason=f"{status}_replay",
                attempt_history=attempts,
                result=result,
            )
        if status in {STATUS_QUEUED, STATUS_RUNNING, STATUS_MATERIALIZING}:
            return SubmissionResult(
                reused=True,
                idempotency_key=key,
                task_id=task_id,
                status=status,
                reuse_reason="already_active",
                attempt_history=attempts,
            )
        if not allow_retry:
            return SubmissionResult(
                reused=False,
                idempotency_key=key,
                task_id=task_id,
                status=STATUS_FAILED,
                reuse_reason="failed_requires_explicit_retry",
                errors=("explicit_retry_required",),
                attempt_history=attempts,
                result=result,
            )
        now = _now()
        with self._transaction() as connection:
            connection.execute(
                "UPDATE supplementary_tasks SET status=?, error=?, "
                "reuse_reason=?, updated_at=? WHERE idempotency_key=?",
                (STATUS_QUEUED, None, "explicit_retry", now, key),
            )
        return SubmissionResult(
            reused=False,
            idempotency_key=key,
            task_id=task_id,
            status=STATUS_QUEUED,
            reuse_reason="explicit_retry",
            attempt_history=attempts,
            result=result,
        )

    def replay_task(self, idempotency_key: str) -> SubmissionResult:
        """Return reuse evidence for a committed/no_progress task without callbacks."""

        row = self._conn.execute(
            "SELECT * FROM supplementary_tasks WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return SubmissionResult(
                reused=False,
                idempotency_key=idempotency_key,
                task_id="",
                status="",
                reuse_reason="unknown_task",
                errors=("unknown_idempotency_key",),
            )
        status = str(row["status"])
        if status not in {STATUS_COMMITTED, STATUS_NO_PROGRESS}:
            return SubmissionResult(
                reused=False,
                idempotency_key=idempotency_key,
                task_id=str(row["task_id"]),
                status=status,
                reuse_reason="not_replayable",
                errors=(f"status_not_replayable:{status}",),
            )
        return SubmissionResult(
            reused=True,
            idempotency_key=idempotency_key,
            task_id=str(row["task_id"]),
            status=status,
            reuse_reason=f"{status}_replay",
            attempt_history=tuple(_json_loads(row["attempts_json"], [])),
            result=_json_loads(row["result_json"], None),
        )

    def recover(self) -> int:
        """Reset interrupted running/materializing tasks to queued."""

        now = _now()
        recovered = 0
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT idempotency_key, status, attempts_json FROM supplementary_tasks "
                "WHERE status IN (?, ?)",
                (STATUS_RUNNING, STATUS_MATERIALIZING),
            ).fetchall()
            for row in rows:
                attempts = _json_loads(row["attempts_json"], [])
                attempts.append(
                    {
                        "event": "crash_recovery",
                        "from_status": str(row["status"]),
                        "to_status": STATUS_QUEUED,
                        "at": now,
                    }
                )
                connection.execute(
                    "UPDATE supplementary_tasks SET status=?, attempts_json=?, "
                    "updated_at=? WHERE idempotency_key=?",
                    (STATUS_QUEUED, _json_dumps(attempts), now, row["idempotency_key"]),
                )
                recovered += 1
        return recovered

    def _claim_next(self) -> tuple[dict[str, Any], str] | None:
        """Atomically claim the highest-priority queued task."""

        now = _now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM supplementary_tasks WHERE status=? "
                "ORDER BY priority DESC, created_at ASC LIMIT 1",
                (STATUS_QUEUED,),
            ).fetchone()
            if row is None:
                return None
            attempt_id = "attempt:" + uuid.uuid4().hex[:12]
            attempts = _json_loads(row["attempts_json"], [])
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "event": "started",
                    "status": STATUS_RUNNING,
                    "route": ROUTE_VISUAL if bool(row["visual_route"]) else ROUTE_LITERATURE,
                    "at": now,
                }
            )
            connection.execute(
                "UPDATE supplementary_tasks SET status=?, attempt_count=attempt_count+1, "
                "attempts_json=?, updated_at=? WHERE idempotency_key=?",
                (STATUS_RUNNING, _json_dumps(attempts), now, row["idempotency_key"]),
            )
            return dict(row), attempt_id

    def _set_status(
        self,
        idempotency_key: str,
        attempt_id: str,
        status: str,
        route: str,
        *,
        event: str,
    ) -> None:
        now = _now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT attempts_json FROM supplementary_tasks WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            attempts = _json_loads(row["attempts_json"] if row else None, [])
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "event": event,
                    "status": status,
                    "route": route,
                    "at": now,
                }
            )
            connection.execute(
                "UPDATE supplementary_tasks SET status=?, attempts_json=?, updated_at=? "
                "WHERE idempotency_key=?",
                (status, _json_dumps(attempts), now, idempotency_key),
            )

    def _append_attempt_event(
        self,
        idempotency_key: str,
        attempt_id: str,
        event: str,
        *,
        route: str = "",
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """Append an audit event without changing task status."""

        now = _now()
        record: dict[str, Any] = {
            "attempt_id": attempt_id,
            "event": event,
            "at": now,
        }
        if route:
            record["route"] = route
        if extra:
            record.update(dict(extra))
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT attempts_json FROM supplementary_tasks "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            attempts = _json_loads(row["attempts_json"] if row else None, [])
            attempts.append(record)
            connection.execute(
                "UPDATE supplementary_tasks SET attempts_json=?, updated_at=? "
                "WHERE idempotency_key=?",
                (_json_dumps(attempts), now, idempotency_key),
            )

    def _persist_retrieval_checkpoint(
        self,
        idempotency_key: str,
        attempt_id: str,
        row: Mapping[str, Any],
        retrieval: RetrievalOutcome,
    ) -> None:
        """Persist a versioned retrieval checkpoint before materialization."""

        route = ROUTE_VISUAL if bool(row["visual_route"]) else ROUTE_LITERATURE
        now = _now()
        payload = {
            "schema_version": RETRIEVAL_CHECKPOINT_SCHEMA_VERSION,
            "kind": RETRIEVAL_CHECKPOINT_KIND,
            "idempotency_key": idempotency_key,
            "task_fingerprint": str(row["fingerprint"]),
            "route": route,
            "created_at": now,
            "retrieval": retrieval.to_checkpoint_dict(),
        }
        with self._transaction() as connection:
            stored_row = connection.execute(
                "SELECT attempts_json FROM supplementary_tasks "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            attempts = _json_loads(
                stored_row["attempts_json"] if stored_row else None, []
            )
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "event": "checkpoint_created",
                    "route": route,
                    "checkpoint_schema_version": (
                        RETRIEVAL_CHECKPOINT_SCHEMA_VERSION
                    ),
                    "at": now,
                }
            )
            connection.execute(
                "UPDATE supplementary_tasks SET result_json=?, attempts_json=?, "
                "updated_at=? WHERE idempotency_key=?",
                (
                    _json_dumps(payload),
                    _json_dumps(attempts),
                    now,
                    idempotency_key,
                ),
            )

    def _parse_retrieval_checkpoint(
        self,
        stored: Mapping[str, Any],
        *,
        idempotency_key: str,
        row: Mapping[str, Any],
        route: str,
    ) -> RetrievalOutcome | None:
        """Return a reconstructed outcome only for a valid identity match."""

        if not _is_retrieval_checkpoint_payload(stored):
            return None
        if stored.get("idempotency_key") != idempotency_key:
            return None
        if stored.get("task_fingerprint") != str(row["fingerprint"]):
            return None
        if stored.get("route") != route:
            return None
        try:
            nested = stored.get("retrieval")
            if not isinstance(nested, Mapping):
                return None
            if str(nested.get("route") or "") != route:
                return None
            return RetrievalOutcome.from_checkpoint_dict(nested)
        except (TypeError, ValueError):
            return None

    def _clear_retrieval_checkpoint(
        self,
        idempotency_key: str,
        attempt_id: str,
        route: str,
        *,
        reason: str = "invalid_or_stale",
    ) -> None:
        """Clear an unusable checkpoint so it can never be resumed."""

        now = _now()
        with self._transaction() as connection:
            stored_row = connection.execute(
                "SELECT attempts_json FROM supplementary_tasks "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            attempts = _json_loads(
                stored_row["attempts_json"] if stored_row else None, []
            )
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "event": "checkpoint_cleared",
                    "route": route,
                    "reason": reason,
                    "at": now,
                }
            )
            connection.execute(
                "UPDATE supplementary_tasks SET result_json=?, attempts_json=?, "
                "updated_at=? WHERE idempotency_key=?",
                (None, _json_dumps(attempts), now, idempotency_key),
            )

    def _finish(
        self,
        idempotency_key: str,
        attempt_id: str,
        status: str,
        route: str,
        *,
        reason: str,
        error: str = "",
        result: dict[str, Any] | None = None,
    ) -> RunResult:
        now = _now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM supplementary_tasks WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            attempts = _json_loads(row["attempts_json"], [])
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "event": "finished",
                    "status": status,
                    "route": route,
                    "reason": reason,
                    "error": error,
                    "at": now,
                }
            )
            existing_result = _json_loads(row["result_json"], None)
            preserved_checkpoint = (
                str(row["result_json"])
                if (
                    result is None
                    and _is_retrieval_checkpoint_payload(existing_result)
                )
                else None
            )
            connection.execute(
                "UPDATE supplementary_tasks SET status=?, attempts_json=?, "
                "result_json=?, error=?, reuse_reason=?, updated_at=? "
                "WHERE idempotency_key=?",
                (
                    status,
                    _json_dumps(attempts),
                    (
                        _json_dumps(result)
                        if result is not None
                        else preserved_checkpoint
                    ),
                    error or None,
                    reason or None,
                    now,
                    idempotency_key,
                ),
            )
        return RunResult(
            idempotency_key=idempotency_key,
            task_id=str(row["task_id"]),
            status=status,
            attempt_id=attempt_id,
            route=route,
            reason=reason,
            result=result,
            error=error,
        )

    def process_once(self) -> RunResult | None:
        """Claim and execute one queued task; returns None when the queue is empty."""

        claimed = self._claim_next()
        if claimed is None:
            return None
        row, attempt_id = claimed
        return self._execute(row, attempt_id)

    def process_pending(self, *, max_tasks: int = 100) -> list[RunResult]:
        """Execute up to ``max_tasks`` queued tasks in priority order."""

        results: list[RunResult] = []
        for _ in range(max(1, int(max_tasks))):
            result = self.process_once()
            if result is None:
                break
            results.append(result)
        return results

    def _execute(
        self,
        row: Mapping[str, Any],
        attempt_id: str,
    ) -> RunResult:
        idempotency_key = str(row["idempotency_key"])
        task = self._row_to_task(row)
        route = ROUTE_VISUAL if bool(row["visual_route"]) else ROUTE_LITERATURE
        context = _json_loads(row["context_snapshot"], {})
        query_records = _json_loads(row["queries_json"], [])
        execution_meta = {
            "schema_version": EXECUTION_META_SCHEMA_VERSION,
            "idempotency_key": idempotency_key,
            "task_fingerprint": str(row["fingerprint"]),
            "task_id": task.task_id,
            "attempt_id": attempt_id,
            "route": route,
            "gap_type": task.gap_type,
        }

        retrieve_fn = (
            self._callbacks.visual_retrieve if route == ROUTE_VISUAL else self._callbacks.retrieve
        )
        materialize_fn = (
            self._callbacks.visual_materialize
            if route == ROUTE_VISUAL
            else self._callbacks.materialize
        )
        try:
            retrieval: RetrievalOutcome | None = None
            stored_result = _json_loads(row.get("result_json"), None)
            if _is_retrieval_checkpoint_payload(stored_result):
                retrieval = self._parse_retrieval_checkpoint(
                    stored_result,
                    idempotency_key=idempotency_key,
                    row=row,
                    route=route,
                )
                if retrieval is not None:
                    self._append_attempt_event(
                        idempotency_key,
                        attempt_id,
                        "checkpoint_reused",
                        route=route,
                        extra={
                            "checkpoint_schema_version": (
                                RETRIEVAL_CHECKPOINT_SCHEMA_VERSION
                            )
                        },
                    )
                else:
                    self._clear_retrieval_checkpoint(
                        idempotency_key,
                        attempt_id,
                        route,
                        reason="invalid_or_mismatched_checkpoint",
                    )
            if retrieval is None:
                if retrieve_fn is None:
                    return self._finish(
                        idempotency_key,
                        attempt_id,
                        STATUS_FAILED,
                        route,
                        reason="missing_retrieval_callback",
                        error="no retrieval callback registered for route "
                        + route,
                    )
                retrieval = retrieve_fn(
                    task, query_records, context, execution_meta
                )
                if not isinstance(retrieval, RetrievalOutcome):
                    raise TypeError(
                        f"retrieval callback returned "
                        f"{type(retrieval).__name__}, expected RetrievalOutcome"
                    )
                self._persist_retrieval_checkpoint(
                    idempotency_key, attempt_id, row, retrieval
                )
            self._set_status(
                idempotency_key,
                attempt_id,
                STATUS_MATERIALIZING,
                route,
                event="materializing_started",
            )
            if materialize_fn is None:
                return self._finish(
                    idempotency_key,
                    attempt_id,
                    STATUS_FAILED,
                    route,
                    reason="missing_materialization_callback",
                    error="no materialization callback registered for route " + route,
                )
            materialization = materialize_fn(task, retrieval, context, execution_meta)
            if not isinstance(materialization, MaterializationOutcome):
                raise TypeError(
                    f"materialization callback returned {type(materialization).__name__}, "
                    "expected MaterializationOutcome"
                )
            return self._commit_or_guardrail(
                idempotency_key,
                attempt_id,
                route,
                retrieval,
                materialization,
                context,
                row,
                execution_meta,
            )
        except Exception as exc:
            return self._finish(
                idempotency_key,
                attempt_id,
                STATUS_FAILED,
                route,
                reason="callback_error",
                error=f"{type(exc).__name__}:{exc}",
            )

    def _commit_or_guardrail(
        self,
        idempotency_key: str,
        attempt_id: str,
        route: str,
        retrieval: RetrievalOutcome,
        materialization: MaterializationOutcome,
        context: Mapping[str, Any],
        row: Mapping[str, Any],
        execution_meta: Mapping[str, Any],
    ) -> RunResult:
        merge_refs = _json_loads(row["merge_refs_json"], [])
        dedup_outcome = _json_loads(row["dedup_outcome_json"], {})
        dedup_meta = {
            "merge_refs": merge_refs,
            "dedup_outcome": dedup_outcome,
        }
        if not materialization.adequate:
            return self._finish(
                idempotency_key,
                attempt_id,
                STATUS_NO_PROGRESS,
                route,
                reason="no_adequate_material",
                result={
                    "execution_meta": dict(execution_meta),
                    "route": route,
                    "retrieval": retrieval.to_dict(),
                    "materialization": materialization.to_dict(),
                    **dedup_meta,
                },
            )
        return self._finish(
            idempotency_key,
            attempt_id,
            STATUS_COMMITTED,
            route,
            reason="committed",
            result={
                "execution_meta": dict(execution_meta),
                "route": route,
                "retrieval": retrieval.to_dict(),
                "materialization": materialization.to_dict(),
                **dedup_meta,
            },
        )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Return the latest task row for a task ID (or None)."""

        row = self._conn.execute(
            "SELECT * FROM supplementary_tasks WHERE task_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def get_task_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        """Return one task row by its stable idempotency key."""

        row = self._conn.execute(
            "SELECT * FROM supplementary_tasks WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        """List task rows, optionally filtered by status."""

        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM supplementary_tasks ORDER BY created_at"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM supplementary_tasks WHERE status=? ORDER BY created_at",
                (status,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_history(self, task_id: str) -> list[dict[str, Any]]:
        """Return the durable attempt history for a task ID."""

        row = self.get_task(task_id)
        if row is None:
            return []
        attempts = row.get("attempts") or []
        return [dict(item) for item in attempts]

    @staticmethod
    def _row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "idempotency_key": str(row["idempotency_key"]),
            "task_id": str(row["task_id"]),
            "gap_type": str(row["gap_type"]),
            "status": str(row["status"]),
            "priority": int(row["priority"] or 0),
            "visual_route": bool(row["visual_route"]),
            "context_refs": _json_loads(row["context_refs_json"], []),
            "context_snapshot": _json_loads(row["context_snapshot"], {}),
            "fingerprint": str(row["fingerprint"]),
            "queries": _json_loads(row["queries_json"], []),
            "merge_refs": _json_loads(row["merge_refs_json"], []),
            "dedup_outcome": _json_loads(row["dedup_outcome_json"], {}),
            "source_provenance": _json_loads(row["source_provenance_json"], {}),
            "history_refs": _json_loads(row["history_refs_json"], []),
            "success_criteria": _json_loads(row["success_criteria_json"], []),
            "material_requirements": _json_loads(row["material_requirements_json"], []),
            "metadata": _json_loads(row["metadata_json"], {}),
            "attempt_count": int(row["attempt_count"] or 0),
            "attempts": _json_loads(row["attempts_json"], []),
            "result": _json_loads(row["result_json"], None),
            "error": row["error"],
            "reuse_reason": row["reuse_reason"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }


__all__ = [
    "EXECUTION_META_SCHEMA_VERSION",
    "MaterializationOutcome",
    "ROUTE_LITERATURE",
    "ROUTE_VISUAL",
    "RetrievalOutcome",
    "RunResult",
    "SERVICE_SCHEMA_VERSION",
    "STATUS_COMMITTED",
    "STATUS_FAILED",
    "STATUS_MATERIALIZING",
    "STATUS_NO_PROGRESS",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "ServiceCallbacks",
    "SubmissionResult",
    "SupplementaryRetrievalService",
    "VALID_STATUSES",
]
