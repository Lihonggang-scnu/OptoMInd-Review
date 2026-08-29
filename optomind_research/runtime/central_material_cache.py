"""Canonical long-term material cache integration for the Research Harness.

The durable store is a versioned ``MaterialUnit`` snapshot.  Harness runs
receive topic-scoped SQLite projections only; acquired text is converted back
to MaterialUnits, embedded, and atomically merged into the canonical store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from optomind_research.s2_kb_bridge import S2KnowledgeBaseBridge
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk

from .artifact_store import atomic_write_json
from .material_cache_merge import MaterialCacheIncrement, merge_material_cache
from .material_semantic_cache import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_REPRESENTATION_VERSION,
    MaterialSemanticCache,
    dashscope_embedder,
)
from .material_unit_store import (
    content_hash,
    material_unit_from_text_chunk,
    question_identity,
)
from .topic_scoped_kb_stage import create_empty_review_kb


SCHEMA_VERSION = "optomind.central_material_cache.v1"
POINTER_SCHEMA_VERSION = "optomind.central_material_cache_pointer.v1"
PROJECTION_SCHEMA_VERSION = "optomind.central_material_projection.v1"
SYNC_SCHEMA_VERSION = "optomind.central_material_sync.v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_ROOT = PROJECT_ROOT / "data" / "long_term_material_cache"
POINTER_FILENAME = "CURRENT.json"
UNITS_FILENAME = "MATERIAL_UNITS_FINAL.json"
VECTORS_FILENAME = "material_vectors.sqlite"
LOCK_FILENAME = ".central-cache.lock"


class CentralMaterialCacheError(RuntimeError):
    """Raised when the canonical cache cannot be read or published safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CentralMaterialCacheError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CentralMaterialCacheError(f"JSON root must be an object: {path}")
    return value


def _snapshot_audit(snapshot: Path) -> dict[str, Any]:
    units_path = snapshot / UNITS_FILENAME
    vectors_path = snapshot / VECTORS_FILENAME
    if not units_path.is_file() or not vectors_path.is_file():
        raise CentralMaterialCacheError(
            f"incomplete material snapshot: {snapshot}"
        )
    units = _read_json(units_path)
    unit_rows = units.get("units")
    if not isinstance(unit_rows, list):
        raise CentralMaterialCacheError(
            f"material snapshot has no units list: {units_path}"
        )
    uri = f"file:{vectors_path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(integrity_row[0]) if integrity_row else "failed"
        vector_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_vectors"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    if integrity.casefold() != "ok":
        raise CentralMaterialCacheError(
            f"material vector cache failed integrity_check: {integrity}"
        )
    if vector_count < len(unit_rows):
        raise CentralMaterialCacheError(
            "material snapshot has fewer vectors than units: "
            f"{vector_count} < {len(unit_rows)}"
        )
    return {
        "unit_count": len(unit_rows),
        "vector_count": vector_count,
        "units_sha256": _sha256_file(units_path),
        "vectors_sha256": _sha256_file(vectors_path),
        "integrity": integrity,
    }


class _CacheLock:
    def __init__(self, root: Path, timeout_seconds: float = 120.0) -> None:
        self.path = root / LOCK_FILENAME
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.acquired = False

    def __enter__(self) -> "_CacheLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        payload = _canonical_json(
            {"pid": os.getpid(), "created_at": _utc_now()}
        ).encode("utf-8")
        while True:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                try:
                    os.write(descriptor, payload)
                finally:
                    os.close(descriptor)
                self.acquired = True
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise CentralMaterialCacheError(
                        f"central material cache is locked: {self.path}"
                    ) from None
                time.sleep(0.2)

    def __exit__(self, *_: Any) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def resolve_current_snapshot(cache_root: str | Path = DEFAULT_CACHE_ROOT) -> Path:
    root = Path(cache_root).resolve()
    pointer_path = root / POINTER_FILENAME
    if not pointer_path.is_file():
        raise CentralMaterialCacheError(
            f"central material cache is not initialized: {pointer_path}"
        )
    pointer = _read_json(pointer_path)
    raw_snapshot = str(pointer.get("snapshot") or "").strip()
    if not raw_snapshot:
        raise CentralMaterialCacheError(
            f"central material cache pointer has no snapshot: {pointer_path}"
        )
    snapshot = (root / raw_snapshot).resolve()
    try:
        snapshot.relative_to(root)
    except ValueError as exc:
        raise CentralMaterialCacheError(
            f"central material cache pointer escapes its root: {snapshot}"
        ) from exc
    _snapshot_audit(snapshot)
    return snapshot


def promote_snapshot(
    *,
    source_snapshot: str | Path,
    cache_root: str | Path = DEFAULT_CACHE_ROOT,
) -> dict[str, Any]:
    """Promote one validated historical snapshot into the stable cache root."""

    source = Path(source_snapshot).resolve()
    source_audit = _snapshot_audit(source)
    root = Path(cache_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with _CacheLock(root):
        pointer_path = root / POINTER_FILENAME
        if pointer_path.is_file():
            current = resolve_current_snapshot(root)
            audit = _snapshot_audit(current)
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "reused",
                "snapshot": str(current),
                **audit,
            }
        generation = 1
        name = f"snapshot-{generation:06d}"
        target = root / name
        staging = root / f".{name}.staging-{uuid.uuid4().hex[:8]}"
        shutil.copytree(source, staging)
        audit = _snapshot_audit(staging)
        staging.rename(target)
        pointer = {
            "schema_version": POINTER_SCHEMA_VERSION,
            "generation": generation,
            "snapshot": name,
            "published_at": _utc_now(),
            "source": str(source),
            **audit,
        }
        atomic_write_json(pointer_path, pointer)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "promoted",
            "snapshot": str(target),
            "source_audit": source_audit,
            **audit,
        }


def initialize_empty_cache(
    cache_root: str | Path = DEFAULT_CACHE_ROOT,
) -> dict[str, Any]:
    """Create the first empty canonical snapshot for a fresh deployment."""

    root = Path(cache_root).resolve()
    if (root / POINTER_FILENAME).is_file():
        snapshot = resolve_current_snapshot(root)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "reused",
            "snapshot": str(snapshot),
            **_snapshot_audit(snapshot),
        }
    source = root.parent / f".{root.name}.empty-{uuid.uuid4().hex[:8]}"
    source.mkdir(parents=True, exist_ok=False)
    try:
        atomic_write_json(
            source / UNITS_FILENAME,
            {
                "schema_version": "optomind.material_unit_store.v1",
                "created_at": _utc_now(),
                "query_annotation_policy": (
                    "separate_by_query_id_and_question_hash"
                ),
                "text_unit_count": 0,
                "unit_count": 0,
                "visual_unit_count": 0,
                "units": [],
            },
        )
        with MaterialSemanticCache(source / VECTORS_FILENAME):
            pass
        return promote_snapshot(
            source_snapshot=source,
            cache_root=root,
        )
    finally:
        shutil.rmtree(source, ignore_errors=True)


def _query_texts(
    query_plan: Mapping[str, Any],
    max_queries: int,
    extra_query_texts: Iterable[str] = (),
) -> list[str]:
    output = query_plan.get("output")
    output = output if isinstance(output, Mapping) else {}
    input_row = query_plan.get("input")
    input_row = input_row if isinstance(input_row, Mapping) else {}
    scope = output.get("scope_definition")
    scope = scope if isinstance(scope, Mapping) else {}
    keywords = output.get("keyword_decomposition")
    keywords = keywords if isinstance(keywords, Mapping) else {}
    candidates: list[str] = [
        str(input_row.get("user_query") or ""),
        str(output.get("problem_understanding") or ""),
        str(scope.get("main_scope") or ""),
    ]
    candidates.extend(str(value) for value in extra_query_texts)
    candidates.extend(
        str(value) for value in (scope.get("scope_items") or [])
    )
    keyword_rows = [
        str(value).strip()
        for value in (keywords.get("keywords") or [])
        if str(value).strip()
    ]
    for start in range(0, len(keyword_rows), 5):
        candidates.append("; ".join(keyword_rows[start : start + 5]))
    deduped: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        text = " ".join(value.split()).strip()
        key = text.casefold()
        if len(text) < 8 or key in seen:
            continue
        seen.add(key)
        deduped.append(text)
        if len(deduped) >= max(1, int(max_queries)):
            break
    return deduped


def _permission(unit: Mapping[str, Any]) -> tuple[str, list[str], bool, str]:
    card = unit.get("durable_content_card")
    card = card if isinstance(card, Mapping) else {}
    quality = card.get("content_quality")
    quality = quality if isinstance(quality, Mapping) else {}
    audit = unit.get("audit")
    audit = audit if isinstance(audit, Mapping) else {}
    provenance = audit.get("source_provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    use_permission = str(
        provenance.get("use_permission")
        or quality.get("evidence_ceiling")
        or "contextual_or_qualified_support"
    )
    allowed = provenance.get("allowed_claim_kinds") or quality.get(
        "allowed_claim_kinds"
    ) or []
    context_complete = bool(
        provenance.get(
            "context_complete",
            quality.get("context_complete", False),
        )
    )
    source_kind = str(
        quality.get("source_kind")
        or provenance.get("materialization_route")
        or "other_verified_source"
    )
    return use_permission, [str(value) for value in allowed], context_complete, source_kind


def _paper_id(unit: Mapping[str, Any]) -> str:
    identity = unit.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    paper_id = str(identity.get("paper_id") or "").strip()
    if paper_id:
        return paper_id
    doi = str(identity.get("doi") or "").strip().casefold()
    if doi:
        return "doi:" + doi
    work_id = str(unit.get("work_id") or "").strip()
    if work_id:
        return work_id
    return "material:" + hashlib.sha1(
        str(unit.get("unit_id") or "").encode("utf-8")
    ).hexdigest()


def _text_provenance(content_depth: str, source_kind: str) -> str:
    combined = f"{content_depth} {source_kind}".casefold()
    if "abstract" in combined:
        return "s2_abstract_snippet"
    if "s2" in combined or "semantic_scholar" in combined:
        return "s2_body_snippet"
    if "html" in combined:
        return "local_publisher_html"
    if "jats" in combined or "xml" in combined:
        return "local_jats_xml"
    if "pdf" in combined or "fulltext" in combined:
        return "local_pdf_parse"
    return "other_verified_source"


def _unit_to_chunk(unit: Mapping[str, Any]) -> UnifiedTextChunk | None:
    identity = unit.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    durable = unit.get("durable_content")
    durable = durable if isinstance(durable, Mapping) else {}
    text = str(
        durable.get("normalized_text") or durable.get("raw_text") or ""
    ).strip()
    if not text:
        return None
    use_permission, allowed, context_complete, source_kind = _permission(unit)
    content_depth = str(durable.get("content_depth") or "fulltext")
    locator = identity.get("locator")
    locator = dict(locator) if isinstance(locator, Mapping) else {}
    locator.setdefault("central_material_unit_id", str(unit.get("unit_id") or ""))
    chunk_id = str(identity.get("chunk_id") or unit.get("unit_id") or "").strip()
    if not chunk_id:
        chunk_id = "central:" + hashlib.sha1(text.encode("utf-8")).hexdigest()
    return UnifiedTextChunk(
        chunk_id=chunk_id,
        paper_id=_paper_id(unit),
        text=text,
        title=str(identity.get("title") or ""),
        doi=str(identity.get("doi") or ""),
        section=str(durable.get("section_path") or ""),
        content_kind=(
            "abstract" if "abstract" in content_depth.casefold() else "text_chunk"
        ),
        text_provenance=_text_provenance(content_depth, source_kind),
        source_locator=locator,
        query_links=["central_long_term_material_cache"],
        quality_status="accepted",
        route_provenance={
            "discovery_route": "central_long_term_material_cache",
            "materialization_route": source_kind,
            "central_material_unit_id": str(unit.get("unit_id") or ""),
        },
        content_depth=content_depth,
        context_complete=context_complete,
        use_permission=use_permission,
        allowed_claim_kinds=allowed,
        scope_fit="unreviewed",
        raw_metadata={"central_material_unit_id": str(unit.get("unit_id") or "")},
    )


def _units_to_review_records(
    units: Sequence[Mapping[str, Any]],
) -> tuple[list[S2PaperRecord], list[UnifiedTextChunk]]:
    chunks = [chunk for unit in units if (chunk := _unit_to_chunk(unit))]
    grouped: dict[str, list[UnifiedTextChunk]] = defaultdict(list)
    for chunk in chunks:
        grouped[chunk.paper_id].append(chunk)
    papers: list[S2PaperRecord] = []
    for paper_id, paper_chunks in sorted(grouped.items()):
        representative = paper_chunks[0]
        strongest = next(
            (
                chunk
                for chunk in paper_chunks
                if chunk.use_permission == "factual_support"
            ),
            representative,
        )
        papers.append(
            S2PaperRecord(
                paper_id=paper_id,
                doi=representative.doi,
                title=representative.title,
                sources=["central_long_term_material_cache"],
                discovery_route="central_long_term_material_cache",
                materialization_route=str(
                    strongest.route_provenance.get("materialization_route")
                    or "material_cache_projection"
                ),
                content_depth=strongest.content_depth,
                use_permission=strongest.use_permission,
                scope_fit="unreviewed",
                route_events=[
                    {
                        "event": "projected_from_central_material_cache",
                        "chunk_count": len(paper_chunks),
                    }
                ],
            )
        )
    return papers, chunks


def project_to_review_kb(
    *,
    query_plan_path: str | Path,
    output_kb_path: str | Path,
    cache_root: str | Path = DEFAULT_CACHE_ROOT,
    report_path: str | Path | None = None,
    embedder: Callable[..., list[list[float]]] = dashscope_embedder,
    extra_query_texts: Iterable[str] = (),
    max_queries: int = 24,
    top_k_per_query: int = 360,
    max_selected_works: int = 600,
    max_projected_units: int = 16_000,
) -> dict[str, Any]:
    """Build a disposable ReviewKnowledgeBase view from the central cache."""

    snapshot = resolve_current_snapshot(cache_root)
    query_plan = _read_json(Path(query_plan_path))
    query_texts = _query_texts(
        query_plan,
        max_queries,
        extra_query_texts=extra_query_texts,
    )
    if not query_texts:
        raise CentralMaterialCacheError("query plan produced no semantic queries")
    output_path = Path(output_kb_path)
    report_file = Path(report_path) if report_path else output_path.with_suffix(
        ".projection.json"
    )
    fingerprint = _sha256_bytes(
        _canonical_json(
            {
                "snapshot_units_sha256": _sha256_file(snapshot / UNITS_FILENAME),
                "queries": query_texts,
                "top_k_per_query": top_k_per_query,
                "max_selected_works": max_selected_works,
                "max_projected_units": max_projected_units,
            }
        ).encode("utf-8")
    )
    if output_path.is_file() and report_file.is_file():
        prior = _read_json(report_file)
        if prior.get("input_fingerprint") == fingerprint:
            return {**prior, "reused": True}
    units_payload = _read_json(snapshot / UNITS_FILENAME)
    units = [
        dict(value)
        for value in (units_payload.get("units") or [])
        if isinstance(value, Mapping)
        and str(value.get("unit_kind") or "") == "text_chunk"
    ]
    units_by_id = {str(unit.get("unit_id") or ""): unit for unit in units}
    usage = {"input_tokens": 0, "request_count": 0}
    query_vectors = embedder(query_texts, usage_accumulator=usage)
    if len(query_vectors) != len(query_texts):
        raise CentralMaterialCacheError(
            "query embedder returned a different number of vectors"
        )
    with MaterialSemanticCache(
        snapshot / VECTORS_FILENAME,
        readonly=True,
    ) as semantic:
        ranked_by_query = semantic.search_many(
            query_vectors,
            top_k=max(1, int(top_k_per_query)),
        )
    unit_scores: dict[str, float] = {}
    unit_query_hits: dict[str, int] = defaultdict(int)
    for rows in ranked_by_query:
        for row in rows:
            unit_id = str(row.get("unit_id") or "")
            if unit_id not in units_by_id:
                continue
            unit_scores[unit_id] = max(
                unit_scores.get(unit_id, -1.0),
                float(row.get("score") or 0.0),
            )
            unit_query_hits[unit_id] += 1
    work_scores: dict[str, float] = {}
    work_hits: dict[str, int] = defaultdict(int)
    for unit_id, score in unit_scores.items():
        work_id = str(units_by_id[unit_id].get("work_id") or unit_id)
        work_scores[work_id] = max(work_scores.get(work_id, -1.0), score)
        work_hits[work_id] += unit_query_hits[unit_id]
    ranked_works = sorted(
        work_scores,
        key=lambda work_id: (
            -(
                work_scores[work_id]
                + min(0.08, 0.005 * max(0, work_hits[work_id] - 1))
            ),
            work_id,
        ),
    )[: max(1, int(max_selected_works))]
    selected_work_set = set(ranked_works)
    selected_units = [
        unit
        for unit in units
        if str(unit.get("work_id") or unit.get("unit_id") or "")
        in selected_work_set
    ]
    if len(selected_units) > max_projected_units:
        work_rank = {work_id: index for index, work_id in enumerate(ranked_works)}
        selected_units.sort(
            key=lambda unit: (
                0 if str(unit.get("unit_id") or "") in unit_scores else 1,
                -unit_scores.get(str(unit.get("unit_id") or ""), -1.0),
                work_rank.get(str(unit.get("work_id") or ""), len(work_rank)),
                str(unit.get("unit_id") or ""),
            )
        )
        selected_units = selected_units[: max(1, int(max_projected_units))]

    if output_path.exists():
        archived = output_path.with_name(
            output_path.name + f".invalid-{uuid.uuid4().hex[:8]}"
        )
        output_path.replace(archived)
    create_empty_review_kb(output_path)
    papers, chunks = _units_to_review_records(selected_units)
    ingest = S2KnowledgeBaseBridge(output_path).ingest(
        papers=papers,
        chunks=chunks,
    )
    report = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "status": "completed",
        "reused": False,
        "cache_root": str(Path(cache_root).resolve()),
        "snapshot": str(snapshot),
        "output_kb_path": str(output_path),
        "input_fingerprint": fingerprint,
        "selection_policy": "semantic_ranking_without_hard_similarity_cutoff",
        "query_count": len(query_texts),
        "query_texts": query_texts,
        "semantic_hits": len(unit_scores),
        "selected_work_count": len(
            {str(unit.get("work_id") or "") for unit in selected_units}
        ),
        "selected_unit_count": len(selected_units),
        "embedding_usage": usage,
        "ingest": ingest,
        "created_at": _utc_now(),
    }
    atomic_write_json(report_file, report)
    return report


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def export_review_kb_units(
    kb_sqlite: str | Path,
    *,
    question: str,
    run_id: str,
    source_stage: str,
) -> list[dict[str, Any]]:
    """Convert all materialized text rows in a run KB to MaterialUnits."""

    path = Path(kb_sqlite)
    if not path.is_file():
        return []
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"papers", "text_chunks"}.issubset(tables):
            return []
        papers = {
            str(row["paper_id"]): dict(row)
            for row in connection.execute("SELECT * FROM papers")
        }
        chunks = [dict(row) for row in connection.execute("SELECT * FROM text_chunks")]
    finally:
        connection.close()
    query_ref = question_identity(None, question)
    exported: dict[str, dict[str, Any]] = {}
    for row in chunks:
        if not str(row.get("text") or "").strip():
            continue
        paper = papers.get(str(row.get("paper_id") or ""), {})
        provenance = _json_value(row.get("route_provenance_json"), {})
        if not provenance:
            provenance = _json_value(row.get("provenance_json"), {})
        allowed = _json_value(row.get("allowed_claim_kinds_json"), [])
        mapped = {
            **row,
            "allowed_claim_kinds": allowed,
            "provenance": {
                **provenance,
                "use_permission": str(row.get("use_permission") or ""),
                "content_depth": str(row.get("content_depth") or ""),
                "context_complete": bool(row.get("context_complete")),
                "allowed_claim_kinds": allowed,
                "central_sync_stage": source_stage,
                "central_sync_run_id": run_id,
            },
        }
        unit = material_unit_from_text_chunk(mapped, paper)
        unit["query_annotations"] = [
            {
                **query_ref,
                "run_id": run_id,
                "source_stage": source_stage,
            }
        ]
        exported[str(unit["unit_id"])] = unit
    return sorted(exported.values(), key=lambda unit: str(unit["unit_id"]))


def _next_generation(pointer: Mapping[str, Any]) -> int:
    return max(1, int(pointer.get("generation") or 0) + 1)


def _select_new_units(
    exported: Mapping[str, dict[str, Any]],
    current_payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Partition exported units against one snapshot payload.

    Pure and deterministic: callers may run it both outside the cache lock
    (candidate selection) and inside the lock (authoritative re-check)
    without any change in semantics.  Returns ``(new_units, conflict_ids,
    duplicate_content_count)``.
    """

    current = {
        str(unit.get("unit_id") or ""): str(
            (unit.get("durable_content") or {}).get("content_hash") or ""
        )
        for unit in (current_payload.get("units") or [])
        if isinstance(unit, Mapping)
    }
    current_hashes = {value for value in current.values() if value}
    new_units: list[dict[str, Any]] = []
    conflicts: list[str] = []
    duplicate_content_count = 0
    pending_hashes: set[str] = set()
    for unit_id, unit in exported.items():
        unit_hash = str(
            (unit.get("durable_content") or {}).get("content_hash") or ""
        )
        if unit_id not in current:
            if unit_hash and (
                unit_hash in current_hashes or unit_hash in pending_hashes
            ):
                duplicate_content_count += 1
                continue
            new_units.append(unit)
            if unit_hash:
                pending_hashes.add(unit_hash)
        elif current[unit_id] != unit_hash:
            conflicts.append(unit_id)
    return new_units, conflicts, duplicate_content_count


def _unit_cache_key(unit: Mapping[str, Any]) -> tuple[str, str]:
    """Return the canonical merge key for one material unit."""

    unit_id = str(unit.get("unit_id") or "").strip()
    unit_hash = str(
        (unit.get("durable_content") or {}).get("content_hash") or ""
    ).strip()
    return unit_id, unit_hash


def _filter_increment_to_final_units(
    increment: Path,
    final_units: Sequence[Mapping[str, Any]],
) -> None:
    """Rewrite one precomputed increment to exactly the final unit set.

    Must be called while holding the central cache lock.  Units embedded
    before the lock may have been published by another worker while this
    process waited; filtering both the units JSON and the vector rows to the
    authoritative final ``(unit_id, content_hash)`` keys makes the merge
    deterministic even when the lock-time re-check selects a smaller set.
    """

    final_units = [dict(unit) for unit in final_units]
    final_keys = {_unit_cache_key(unit) for unit in final_units}
    if not final_keys:
        raise CentralMaterialCacheError(
            "refusing to filter an increment to an empty unit set"
        )

    atomic_write_json(
        increment / UNITS_FILENAME,
        {
            "schema_version": "optomind.material_unit_store.v1",
            "created_at": _utc_now(),
            "query_annotation_policy": (
                "separate_by_query_id_and_question_hash"
            ),
            "text_unit_count": len(final_units),
            "unit_count": len(final_units),
            "visual_unit_count": 0,
            "units": final_units,
        },
    )

    vectors_path = increment / VECTORS_FILENAME
    uri = f"file:{vectors_path.resolve().as_posix()}?mode=ro"
    source = sqlite3.connect(uri, uri=True)
    try:
        source.row_factory = sqlite3.Row
        rows = source.execute(
            "SELECT * FROM semantic_vectors"
        ).fetchall()
        columns = [str(column) for column in rows[0].keys()] if rows else []
    finally:
        source.close()

    def row_key(row: Any) -> tuple[str, str]:
        return (
            str(row["unit_id"] or "").strip(),
            str(row["content_hash"] or "").strip(),
        )

    covered = {row_key(row) for row in rows}
    missing = sorted(
        unit_id
        for unit_id, unit_hash in final_keys
        if (unit_id, unit_hash) not in covered
    )
    if missing:
        raise CentralMaterialCacheError(
            "increment vectors are missing for final units: "
            + ", ".join(missing)
        )

    kept_rows = [
        tuple(row[column] for column in columns)
        for row in rows
        if row_key(row) in final_keys
    ]
    connection = sqlite3.connect(str(vectors_path))
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM semantic_vectors")
        if kept_rows:
            placeholders = ", ".join("?" for _ in columns)
            connection.executemany(
                "INSERT INTO semantic_vectors ("
                + ", ".join(columns)
                + ") VALUES ("
                + placeholders
                + ")",
                kept_rows,
            )
        connection.commit()
    finally:
        connection.close()


def sync_review_kbs_to_central(
    *,
    kb_paths: Iterable[str | Path],
    question: str,
    run_id: str,
    source_stage: str,
    work_dir: str | Path,
    cache_root: str | Path = DEFAULT_CACHE_ROOT,
    embedder: Callable[..., list[list[float]]] = dashscope_embedder,
) -> dict[str, Any]:
    """Persist new run material and publish one new canonical snapshot."""

    root = Path(cache_root).resolve()
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    exported: dict[str, dict[str, Any]] = {}
    sources: list[str] = []
    for raw_path in kb_paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        sources.append(str(path))
        for unit in export_review_kb_units(
            path,
            question=question,
            run_id=run_id,
            source_stage=source_stage,
        ):
            exported[str(unit["unit_id"])] = unit
    # ---- 锁外阶段一：只读当前快照，先做一轮候选去重 --------------------
    # resolve/read 均为只读操作，不持锁；真正的并发安全性由下方锁内的
    # "重读 + 重去重 + 增量过滤" 保证。
    snapshot = resolve_current_snapshot(root)
    current_payload = _read_json(snapshot / UNITS_FILENAME)
    (
        candidate_units,
        conflicts,
        duplicate_content_count,
    ) = _select_new_units(exported, current_payload)
    if not candidate_units:
        report = {
            "schema_version": SYNC_SCHEMA_VERSION,
            "status": "no_new_units",
            "source_stage": source_stage,
            "source_kbs": sources,
            "exported_unit_count": len(exported),
            "new_unit_count": 0,
            "content_conflict_count": len(conflicts),
            "duplicate_content_count": duplicate_content_count,
            "snapshot": str(snapshot),
            "created_at": _utc_now(),
        }
        atomic_write_json(work / "CENTRAL_CACHE_SYNC_REPORT.json", report)
        return report

    # ---- 锁外阶段二：为候选新单元计算 embedding（幂等的网络 IO）--------
    # ensure_units_parallel 对相同单元重复嵌入结果相同，锁外执行安全；
    # DashScope 网络耗时不再阻塞其他进程获取缓存锁（原 bug：持锁期间
    # 调用远程 API，其他进程自旋 120 秒后超时）。
    increment = work / f"increment-{uuid.uuid4().hex[:10]}"
    increment.mkdir(parents=True, exist_ok=False)
    try:
        increment_payload = {
            "schema_version": "optomind.material_unit_store.v1",
            "created_at": _utc_now(),
            "query_annotation_policy": "separate_by_query_id_and_question_hash",
            "text_unit_count": len(candidate_units),
            "unit_count": len(candidate_units),
            "visual_unit_count": 0,
            "units": candidate_units,
        }
        atomic_write_json(increment / UNITS_FILENAME, increment_payload)
        usage = {"input_tokens": 0, "request_count": 0}
        with MaterialSemanticCache(increment / VECTORS_FILENAME) as semantic:
            vector_result = semantic.ensure_units_parallel(
                candidate_units,
                lambda texts: embedder(texts, usage_accumulator=usage),
                embedding_model=DEFAULT_EMBEDDING_MODEL,
                representation_version=DEFAULT_REPRESENTATION_VERSION,
                batch_size=10,
                workers=4,
            )

        # ---- 锁内阶段三：重读最新快照、重新去重、过滤增量后合并发布 ----
        # 拿到锁后必须重新解析快照：锁外期间其他进程可能已发布新代快照，
        # 其中可能已包含本次导出的部分或全部单元。
        with _CacheLock(root):
            try:
                snapshot = resolve_current_snapshot(root)
                current_payload = _read_json(snapshot / UNITS_FILENAME)
                (
                    final_units,
                    conflicts,
                    duplicate_content_count,
                ) = _select_new_units(exported, current_payload)
                if not final_units:
                    # 候选单元在等待锁期间已被并发进程写入缓存：如实上报，
                    # 不发布空代快照。本次 embedding 已花费的用量照实记账，
                    # 并清理临时增量目录。
                    report = {
                        "schema_version": SYNC_SCHEMA_VERSION,
                        "status": "no_new_units",
                        "source_stage": source_stage,
                        "source_kbs": sources,
                        "exported_unit_count": len(exported),
                        "new_unit_count": 0,
                        "content_conflict_count": len(conflicts),
                        "duplicate_content_count": duplicate_content_count,
                        "embedding_usage": usage,
                        "snapshot": str(snapshot),
                        "created_at": _utc_now(),
                    }
                    atomic_write_json(
                        work / "CENTRAL_CACHE_SYNC_REPORT.json", report
                    )
                    return report
                # 锁外预计算的增量可能包含等待锁期间已被其他进程发布的
                # 单元：合并前将 units JSON 与 vectors SQLite 精确过滤为
                # 权威最终单元集合，防止陈旧/重复单元进入新快照。
                _filter_increment_to_final_units(increment, final_units)
                pointer = _read_json(root / POINTER_FILENAME)
                generation = _next_generation(pointer)
                next_snapshot = root / f"snapshot-{generation:06d}"
                merge_report = merge_material_cache(
                    base_units_path=snapshot / UNITS_FILENAME,
                    base_vectors_path=snapshot / VECTORS_FILENAME,
                    increments=[
                        MaterialCacheIncrement(
                            units_path=increment / UNITS_FILENAME,
                            vectors_path=increment / VECTORS_FILENAME,
                        )
                    ],
                    output_root=next_snapshot,
                    supplementary_conflict_policy=True,
                )
                audit = _snapshot_audit(next_snapshot)
                next_pointer = {
                    "schema_version": POINTER_SCHEMA_VERSION,
                    "generation": generation,
                    "snapshot": next_snapshot.name,
                    "published_at": _utc_now(),
                    "previous_snapshot": snapshot.name,
                    "source_stage": source_stage,
                    "run_id": run_id,
                    **audit,
                }
                atomic_write_json(root / POINTER_FILENAME, next_pointer)
            finally:
                shutil.rmtree(increment, ignore_errors=True)
    except Exception:
        shutil.rmtree(increment, ignore_errors=True)
        raise

    report = {
        "schema_version": SYNC_SCHEMA_VERSION,
        "status": "published",
        "source_stage": source_stage,
        "source_kbs": sources,
        "exported_unit_count": len(exported),
        "new_unit_count": len(final_units),
        "content_conflict_count": len(conflicts),
        "duplicate_content_count": duplicate_content_count,
        "embedding_usage": usage,
        "vector_result": vector_result,
        "merge_counts": merge_report.get("counts") or {},
        "previous_snapshot": str(snapshot),
        "snapshot": str(next_snapshot),
        "generation": generation,
        "created_at": _utc_now(),
    }
    atomic_write_json(work / "CENTRAL_CACHE_SYNC_REPORT.json", report)
    return report


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_CACHE_ROOT",
    "CentralMaterialCacheError",
    "resolve_current_snapshot",
    "promote_snapshot",
    "initialize_empty_cache",
    "project_to_review_kb",
    "export_review_kb_units",
    "sync_review_kbs_to_central",
]
