"""Mock-only concurrency tests for the central material cache sync path.

The tests interleave two workers without network or real embeddings: worker A
is blocked inside its mock embedder while worker B publishes a snapshot, so
the lock-time re-check and increment filtering are exercised deterministically.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from optomind_research.runtime.central_material_cache import (
    initialize_empty_cache,
    resolve_current_snapshot,
    sync_review_kbs_to_central,
)
from optomind_research.runtime.topic_scoped_kb_stage import create_empty_review_kb
from optomind_research.s2_kb_bridge import S2KnowledgeBaseBridge
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory (pytest default is ACL-blocked)."""

    base = Path(tempfile.gettempdir()) / "optomind-central-cache-concurrency-tmp"
    base.mkdir(exist_ok=True)
    path = base / f"{request.node.name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _chunk(chunk_id: str, text: str, paper_id: str | None = None) -> UnifiedTextChunk:
    paper_id = paper_id or f"paper-{chunk_id}"
    return UnifiedTextChunk(
        chunk_id=chunk_id,
        paper_id=paper_id,
        title=f"Paper {paper_id}",
        text=text,
        content_depth="fulltext",
        context_complete=True,
        use_permission="factual_support",
        allowed_claim_kinds=["method"],
        scope_fit="direct",
    )


def _run_kb(tmp_path, name: str, chunks) -> Path:
    kb = tmp_path / name
    create_empty_review_kb(kb)
    papers = []
    seen = set()
    for chunk in chunks:
        if chunk.paper_id in seen:
            continue
        seen.add(chunk.paper_id)
        papers.append(
            S2PaperRecord(
                paper_id=chunk.paper_id,
                title=chunk.title,
                content_depth="fulltext",
                use_permission="factual_support",
                scope_fit="direct",
            )
        )
    S2KnowledgeBaseBridge(kb).ingest(papers=papers, chunks=list(chunks))
    return kb


def _embedder(values):
    def embed(texts, usage_accumulator=None):
        if usage_accumulator is not None:
            usage_accumulator["input_tokens"] += len(texts)
            usage_accumulator["request_count"] += 1
        return [list(values) for _ in texts]

    return embed


def _units(snapshot: Path):
    payload = json.loads(
        (snapshot / "MATERIAL_UNITS_FINAL.json").read_text(encoding="utf-8")
    )
    return payload["units"]


def _pointer(cache_root: Path):
    return json.loads(
        (cache_root / "CURRENT.json").read_text(encoding="utf-8")
    )


def _interleaved_sync(
    tmp_path,
    cache_root,
    a_kb,
    b_kb,
    *,
    a_embedder,
    b_embedder,
):
    """Run worker A and worker B so B publishes while A is embedding.

    A starts first and blocks in its embedder.  The main thread then runs B to
    completion, after which A resumes, acquires the cache lock, re-checks the
    snapshot, filters its precomputed increment, and merges or reports
    ``no_new_units``.  Events make the interleaving deterministic.
    """

    a_embedding_started = threading.Event()
    b_published = threading.Event()

    def blocking_embedder(texts, usage_accumulator=None):
        a_embedding_started.set()
        if not b_published.wait(30):
            raise RuntimeError("concurrent worker did not finish in time")
        return a_embedder(texts, usage_accumulator)

    results = {}
    errors = []

    def run_a():
        try:
            results["a"] = sync_review_kbs_to_central(
                kb_paths=[a_kb],
                question="optical inverse design concurrency",
                run_id="run-a",
                source_stage="s2_literature_intelligence",
                work_dir=tmp_path / "work-a",
                cache_root=cache_root,
                embedder=blocking_embedder,
            )
        except BaseException as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    thread = threading.Thread(target=run_a, name="worker-a", daemon=True)
    thread.start()
    if not a_embedding_started.wait(30):
        raise RuntimeError("worker A never started embedding")

    b_error = None
    try:
        b_report = sync_review_kbs_to_central(
            kb_paths=[b_kb],
            question="optical inverse design concurrency",
            run_id="run-b",
            source_stage="s2_literature_intelligence",
            work_dir=tmp_path / "work-b",
            cache_root=cache_root,
            embedder=b_embedder,
        )
    except BaseException as exc:  # pragma: no cover - failure reporting
        b_error = exc
    finally:
        b_published.set()

    thread.join(30)
    if thread.is_alive():
        raise RuntimeError("worker A did not finish in time")
    if b_error:
        raise b_error
    if errors:
        raise errors[0]
    return results["a"], b_report


def test_same_unit_id_concurrent_workers_publish_one_copy(tmp_path):
    cache_root = tmp_path / "central"
    initialize_empty_cache(cache_root)
    text = "shared optical inverse design evidence"
    a_kb = _run_kb(
        tmp_path, "kb-a.sqlite", [_chunk("c-shared", text, paper_id="p-a")]
    )
    b_kb = _run_kb(
        tmp_path, "kb-b.sqlite", [_chunk("c-shared", text, paper_id="p-b")]
    )

    a_report, b_report = _interleaved_sync(
        tmp_path,
        cache_root,
        a_kb,
        b_kb,
        a_embedder=_embedder([0.1, 0.2]),
        b_embedder=_embedder([0.3, 0.4]),
    )

    assert b_report["status"] == "published"
    assert a_report["status"] == "no_new_units"
    assert a_report["exported_unit_count"] == 1
    assert a_report["embedding_usage"]["input_tokens"] > 0
    assert not list((tmp_path / "work-a").glob("increment-*"))

    snapshot = resolve_current_snapshot(cache_root)
    units = _units(snapshot)
    assert len(units) == 1
    assert {unit["identity"]["chunk_id"] for unit in units} == {"c-shared"}
    assert _pointer(cache_root)["generation"] == 2
    assert sorted(path.name for path in cache_root.glob("snapshot-*")) == [
        "snapshot-000001",
        "snapshot-000002",
    ]


def test_different_ids_same_content_hash_keep_one_content(tmp_path):
    cache_root = tmp_path / "central"
    initialize_empty_cache(cache_root)
    text = "identical inverse design body text"
    a_kb = _run_kb(
        tmp_path,
        "kb-a.sqlite",
        [_chunk("c-content-a", text, paper_id="p-a")],
    )
    b_kb = _run_kb(
        tmp_path,
        "kb-b.sqlite",
        [_chunk("c-content-b", text, paper_id="p-b")],
    )

    a_report, b_report = _interleaved_sync(
        tmp_path,
        cache_root,
        a_kb,
        b_kb,
        a_embedder=_embedder([0.1, 0.2]),
        b_embedder=_embedder([0.3, 0.4]),
    )

    assert b_report["status"] == "published"
    assert a_report["status"] == "no_new_units"
    assert a_report["duplicate_content_count"] == 1
    assert a_report["embedding_usage"]["input_tokens"] > 0
    assert not list((tmp_path / "work-a").glob("increment-*"))

    snapshot = resolve_current_snapshot(cache_root)
    units = _units(snapshot)
    assert len(units) == 1
    assert len(
        {unit["durable_content"]["content_hash"] for unit in units}
    ) == 1
    assert {unit["identity"]["chunk_id"] for unit in units} == {
        "c-content-b"
    }
    assert _pointer(cache_root)["generation"] == 2


def _run_disjoint_interleaved(tmp_path):
    cache_root = tmp_path / "central"
    initialize_empty_cache(cache_root)
    a_text = "unique first content from worker A"
    b_text = "unique second content from worker B"
    a_kb = _run_kb(
        tmp_path,
        "kb-a.sqlite",
        [
            _chunk("c-a", a_text, paper_id="p-a"),
            _chunk("c-b", b_text, paper_id="p-b"),
        ],
    )
    b_kb = _run_kb(
        tmp_path,
        "kb-b.sqlite",
        [_chunk("c-b", b_text, paper_id="p-b")],
    )
    a_report, b_report = _interleaved_sync(
        tmp_path,
        cache_root,
        a_kb,
        b_kb,
        a_embedder=_embedder([0.1, 0.2]),
        b_embedder=_embedder([0.3, 0.4]),
    )
    return cache_root, a_report, b_report


def test_different_ids_different_content_keep_both(tmp_path):
    cache_root, a_report, b_report = _run_disjoint_interleaved(tmp_path)

    assert b_report["status"] == "published"
    assert a_report["status"] == "published"
    assert a_report["exported_unit_count"] == 2
    assert a_report["new_unit_count"] == 1
    assert not list((tmp_path / "work-a").glob("increment-*"))

    snapshot = resolve_current_snapshot(cache_root)
    units = _units(snapshot)
    assert {unit["identity"]["chunk_id"] for unit in units} == {"c-a", "c-b"}
    assert len(
        {unit["durable_content"]["content_hash"] for unit in units}
    ) == 2
    assert _pointer(cache_root)["generation"] == 3
    assert sorted(path.name for path in cache_root.glob("snapshot-*")) == [
        "snapshot-000001",
        "snapshot-000002",
        "snapshot-000003",
    ]
    assert not list(cache_root.glob("*.staging-*"))


def test_pointer_and_vector_integrity_after_concurrent_merge(tmp_path):
    cache_root, a_report, b_report = _run_disjoint_interleaved(tmp_path)

    assert a_report["status"] == "published"
    assert b_report["status"] == "published"
    current = resolve_current_snapshot(cache_root)
    pointer = _pointer(cache_root)
    assert pointer["generation"] == 3
    assert pointer["snapshot"] == current.name

    units = _units(current)
    assert len(units) == 2
    with sqlite3.connect(current / "material_vectors.sqlite") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = connection.execute(
            "SELECT unit_id, content_hash FROM semantic_vectors"
        ).fetchall()
    assert len(rows) == 2
    assert {row[0] for row in rows} == {unit["unit_id"] for unit in units}
    assert {row[1] for row in rows} == {
        unit["durable_content"]["content_hash"] for unit in units
    }
