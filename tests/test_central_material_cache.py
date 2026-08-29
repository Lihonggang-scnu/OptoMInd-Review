from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from optomind_research.runtime.central_material_cache import (
    initialize_empty_cache,
    project_to_review_kb,
    promote_snapshot,
    resolve_current_snapshot,
    sync_review_kbs_to_central,
)
from optomind_research.runtime.material_semantic_cache import MaterialSemanticCache
from optomind_research.runtime.material_unit_store import material_unit_from_text_chunk
from optomind_research.runtime.topic_scoped_kb_stage import create_empty_review_kb
from optomind_research.s2_kb_bridge import S2KnowledgeBaseBridge
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory (pytest default is ACL-blocked)."""

    base = Path(tempfile.gettempdir()) / "optomind-central-cache-tmp"
    base.mkdir(exist_ok=True)
    path = base / f"{request.node.name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _unit(paper_id: str, chunk_id: str, text: str) -> dict:
    return material_unit_from_text_chunk(
        {
            "paper_id": paper_id,
            "chunk_id": chunk_id,
            "title": f"Paper {paper_id}",
            "text": text,
            "content_depth": "fulltext",
            "context_complete": True,
            "use_permission": "factual_support",
            "allowed_claim_kinds": ["method", "mechanism"],
        }
    )


def _source_snapshot(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    units = [
        _unit("p1", "c1", "inverse design method"),
        _unit("p1", "c2", "fabrication context"),
        _unit("p2", "c3", "unrelated hydrology"),
    ]
    (source / "MATERIAL_UNITS_FINAL.json").write_text(
        json.dumps(
            {
                "schema_version": "optomind.material_unit_store.v1",
                "unit_count": len(units),
                "text_unit_count": len(units),
                "visual_unit_count": 0,
                "units": units,
            }
        ),
        encoding="utf-8",
    )
    with MaterialSemanticCache(source / "material_vectors.sqlite") as cache:
        cache.put(units[0], [1.0, 0.0])
        cache.put(units[1], [0.7, 0.3])
        cache.put(units[2], [0.0, 1.0])
    return source, units


def _query_plan(tmp_path):
    path = tmp_path / "query_plan.json"
    path.write_text(
        json.dumps(
            {
                "input": {"user_query": "optical inverse design"},
                "output": {
                    "problem_understanding": "Review inverse optical design methods.",
                    "scope_definition": {
                        "main_scope": "Nanophotonic inverse design",
                        "scope_items": ["fabrication-aware optimization"],
                    },
                    "keyword_decomposition": {
                        "keywords": ["adjoint optical inverse design"]
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_projection_uses_semantic_ranking_and_restores_paper_context(tmp_path):
    source, _units = _source_snapshot(tmp_path)
    cache_root = tmp_path / "central"
    promoted = promote_snapshot(
        source_snapshot=source,
        cache_root=cache_root,
    )
    assert promoted["status"] == "promoted"

    def embedder(texts, usage_accumulator=None):
        if usage_accumulator is not None:
            usage_accumulator["input_tokens"] = len(texts)
            usage_accumulator["request_count"] = 1
        return [[1.0, 0.0] for _ in texts]

    output_kb = tmp_path / "projection.sqlite"
    report = project_to_review_kb(
        query_plan_path=_query_plan(tmp_path),
        output_kb_path=output_kb,
        cache_root=cache_root,
        embedder=embedder,
        top_k_per_query=1,
        max_selected_works=1,
    )

    assert report["status"] == "completed"
    assert report["selected_work_count"] == 1
    assert report["selected_unit_count"] == 2
    assert any(
        "fabrication-aware optimization" in query
        for query in report["query_texts"]
    )
    with sqlite3.connect(output_kb) as connection:
        assert connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0] == 2
        assert {
            row[0]
            for row in connection.execute("SELECT chunk_id FROM text_chunks")
        } == {"c1", "c2"}


def test_empty_cache_initialization_is_canonical_and_reusable(tmp_path):
    cache_root = tmp_path / "empty-central"
    first = initialize_empty_cache(cache_root)
    second = initialize_empty_cache(cache_root)

    assert first["status"] == "promoted"
    assert first["unit_count"] == 0
    assert first["vector_count"] == 0
    assert second["status"] == "reused"
    assert resolve_current_snapshot(cache_root).name == "snapshot-000001"


def test_sync_publishes_one_new_snapshot_and_keeps_previous(tmp_path):
    source, _units = _source_snapshot(tmp_path)
    cache_root = tmp_path / "central"
    promote_snapshot(source_snapshot=source, cache_root=cache_root)
    previous = resolve_current_snapshot(cache_root)

    run_kb = tmp_path / "run.sqlite"
    create_empty_review_kb(run_kb)
    paper = S2PaperRecord(
        paper_id="p3",
        title="New optical evidence",
        content_depth="structured_snippet",
        use_permission="factual_support",
        scope_fit="direct",
    )
    chunk = UnifiedTextChunk(
        chunk_id="c4",
        paper_id="p3",
        title=paper.title,
        text="Newly retrieved optical full-text evidence.",
        content_depth="structured_snippet",
        context_complete=True,
        use_permission="factual_support",
        allowed_claim_kinds=["method"],
        scope_fit="direct",
    )
    S2KnowledgeBaseBridge(run_kb).ingest(papers=[paper], chunks=[chunk])

    def embedder(texts, usage_accumulator=None):
        if usage_accumulator is not None:
            usage_accumulator["input_tokens"] = len(texts)
            usage_accumulator["request_count"] = 1
        return [[0.8, 0.2] for _ in texts]

    report = sync_review_kbs_to_central(
        kb_paths=[run_kb],
        question="optical inverse design",
        run_id="run-1",
        source_stage="s2_literature_intelligence",
        work_dir=tmp_path / "sync",
        cache_root=cache_root,
        embedder=embedder,
    )

    current = resolve_current_snapshot(cache_root)
    assert report["status"] == "published"
    assert report["new_unit_count"] == 1
    assert current != previous
    assert previous.is_dir()
    pointer = json.loads((cache_root / "CURRENT.json").read_text(encoding="utf-8"))
    assert pointer["generation"] == 2
    units = json.loads(
        (current / "MATERIAL_UNITS_FINAL.json").read_text(encoding="utf-8")
    )["units"]
    assert len(units) == 4
    with sqlite3.connect(current / "material_vectors.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM semantic_vectors"
        ).fetchone()[0] == 4
