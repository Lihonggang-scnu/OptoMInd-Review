"""Offline tests for the versioned long-term material-cache merge."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import struct
import tempfile
import uuid
from pathlib import Path

import pytest

from optomind_research.runtime.material_cache_merge import (
    MaterialCacheIncrement,
    MaterialCacheMergeError,
    merge_material_cache,
)


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory (pytest default is ACL-blocked)."""
    base = Path(tempfile.gettempdir()) / "optomind-cache-merge-tmp"
    base.mkdir(exist_ok=True)
    path = base / f"{request.node.name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _unit(
    unit_id: str,
    content_hash: str,
    *,
    annotations: list | None = None,
    refs: list | None = None,
    relations: list | None = None,
    kind: str = "text_chunk",
) -> dict:
    return {
        "schema_version": "optomind.material_unit.v1",
        "unit_id": unit_id,
        "work_id": f"work:{unit_id}",
        "unit_kind": kind,
        "identity": {"paper_id": f"paper:{unit_id}"},
        "durable_content": {
            "content_hash": content_hash,
            "normalized_text": f"text {unit_id}",
        },
        "durable_content_card": {"observable_content": f"card {unit_id}"},
        "query_annotations": annotations or [],
        "embedding_refs": refs or [],
        "relations": relations or [],
        "audit": {},
    }


def _write_units(path: Path, units: list[dict]) -> None:
    payload = {
        "schema_version": "optomind.material_unit_store.v1",
        "unit_count": len(units),
        "text_unit_count": len(units),
        "visual_unit_count": 0,
        "units": units,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _vector_row(
    unit_id: str,
    content_hash: str,
    *,
    dimension: int = 4,
    model: str = "text-embedding-v4",
    version: str = "material-unit-surrogate.v1",
) -> tuple:
    vector_blob = struct.pack(
        "<" + "f" * dimension, *([0.1] * dimension)
    )
    return (
        unit_id,
        content_hash,
        model,
        version,
        dimension,
        vector_blob,
        f"surrogate {unit_id}",
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00",
    )


def _write_vectors(path: Path, rows: list[tuple]) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE semantic_vectors("
            "unit_id TEXT NOT NULL, content_hash TEXT NOT NULL, "
            "embedding_model TEXT, representation_version TEXT, "
            "dimension INTEGER, vector BLOB, surrogate TEXT, "
            "created_at TEXT, updated_at TEXT, "
            "PRIMARY KEY (unit_id, content_hash, embedding_model, "
            "representation_version))"
        )
        connection.executemany(
            "INSERT INTO semantic_vectors VALUES (?,?,?,?,?,?,?,?,?)", rows
        )
        connection.commit()
    finally:
        connection.close()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _no_staging_left(parent: Path) -> None:
    assert not list(parent.glob("*.staging-*"))


def test_merge_adds_new_units_reuses_duplicates_and_preserves_base(
    tmp_path,
) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    base_units = [
        _unit("u1", "sha256:aaa"),
        _unit(
            "u2",
            "sha256:bbb",
            annotations=[{"query_id": "q-base", "question_hash": "h-base"}],
        ),
    ]
    _write_units(base_units_path, base_units)
    _write_vectors(
        base_vectors_path,
        [_vector_row("u1", "sha256:aaa"), _vector_row("u2", "sha256:bbb")],
    )
    base_sha = _file_sha256(base_units_path)
    base_vector_sha = _file_sha256(base_vectors_path)

    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    inc_units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
    inc_vectors_path = inc_dir / "material_vectors.sqlite"
    inc_units = [
        _unit(
            "u2",
            "sha256:bbb",
            annotations=[
                {"query_id": "q-base", "question_hash": "h-base"},
                {"query_id": "q-new", "question_hash": "h-new"},
            ],
        ),
        _unit("u3", "sha256:ccc"),
    ]
    _write_units(inc_units_path, inc_units)
    _write_vectors(
        inc_vectors_path,
        [_vector_row("u2", "sha256:bbb"), _vector_row("u3", "sha256:ccc")],
    )

    snapshot = tmp_path / "long_term_material_cache"
    report = merge_material_cache(
        base_units_path=base_units_path,
        base_vectors_path=base_vectors_path,
        increments=[
            MaterialCacheIncrement(
                units_path=inc_units_path,
                vectors_path=inc_vectors_path,
            )
        ],
        output_root=snapshot,
    )
    assert report["status"] == "completed"
    assert report["integrity"] == "ok"
    assert report["counts"]["base_units"] == 2
    assert report["counts"]["added_units"] == 1
    assert report["counts"]["reused_units"] == 1
    assert report["counts"]["conflict_units"] == 0
    assert report["counts"]["missing_vector_units"] == 0
    assert report["counts"]["output_vectors"] == 3
    assert snapshot.is_dir()
    assert (snapshot / "LONG_TERM_CACHE_MERGE_REPORT.json").is_file()
    # Base files are read-only inputs: bytes unchanged.
    assert _file_sha256(base_units_path) == base_sha
    assert _file_sha256(base_vectors_path) == base_vector_sha
    merged = json.loads(
        (snapshot / "MATERIAL_UNITS_FINAL.json").read_text(encoding="utf-8")
    )
    by_id = {unit["unit_id"]: unit for unit in merged["units"]}
    assert set(by_id) == {"u1", "u2", "u3"}
    assert {
        item.get("query_id")
        for item in by_id["u2"]["query_annotations"]
    } == {"q-base", "q-new"}
    with sqlite3.connect(str(snapshot / "material_vectors.sqlite")) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = {
            row[0]: row
            for row in conn.execute(
                "SELECT unit_id, content_hash FROM semantic_vectors"
            ).fetchall()
        }
        u3_blob = conn.execute(
            "SELECT vector FROM semantic_vectors WHERE unit_id='u3'"
        ).fetchone()[0]
    assert set(rows) == {"u1", "u2", "u3"}
    assert u3_blob == _vector_row("u3", "sha256:ccc")[5]


def test_merge_is_idempotent_when_snapshot_is_reused_as_base(tmp_path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    _write_units(base_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(base_vectors_path, [_vector_row("u1", "sha256:aaa")])

    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    inc_units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
    inc_vectors_path = inc_dir / "material_vectors.sqlite"
    _write_units(inc_units_path, [_unit("u2", "sha256:bbb")])
    _write_vectors(inc_vectors_path, [_vector_row("u2", "sha256:bbb")])
    increment = MaterialCacheIncrement(
        units_path=inc_units_path, vectors_path=inc_vectors_path
    )

    first = tmp_path / "snapshot_one"
    merge_material_cache(
        base_units_path=base_units_path,
        base_vectors_path=base_vectors_path,
        increments=[increment],
        output_root=first,
    )
    second = tmp_path / "snapshot_two"
    report = merge_material_cache(
        base_units_path=first / "MATERIAL_UNITS_FINAL.json",
        base_vectors_path=first / "material_vectors.sqlite",
        increments=[increment],
        output_root=second,
    )
    assert report["status"] == "completed"
    assert report["counts"]["added_units"] == 0
    assert report["counts"]["reused_units"] == 1
    assert report["counts"]["added_vectors"] == 0
    assert report["counts"]["reused_vectors"] == 1
    first_units = (first / "MATERIAL_UNITS_FINAL.json").read_bytes()
    second_units = (second / "MATERIAL_UNITS_FINAL.json").read_bytes()
    assert json.loads(first_units)["units"] == json.loads(
        second_units
    )["units"]
    with sqlite3.connect(str(first / "material_vectors.sqlite")) as a:
        with sqlite3.connect(str(second / "material_vectors.sqlite")) as b:
            a_rows = a.execute(
                "SELECT unit_id, content_hash, dimension FROM semantic_vectors"
            ).fetchall()
            b_rows = b.execute(
                "SELECT unit_id, content_hash, dimension FROM semantic_vectors"
            ).fetchall()
    assert sorted(a_rows) == sorted(b_rows)


def test_content_hash_collision_fails_closed(tmp_path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    _write_units(base_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(base_vectors_path, [_vector_row("u1", "sha256:aaa")])

    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    inc_units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
    inc_vectors_path = inc_dir / "material_vectors.sqlite"
    _write_units(inc_units_path, [_unit("u1", "sha256:DIFFERENT")])
    _write_vectors(
        inc_vectors_path, [_vector_row("u1", "sha256:DIFFERENT")]
    )

    snapshot = tmp_path / "long_term_material_cache"
    with pytest.raises(MaterialCacheMergeError) as excinfo:
        merge_material_cache(
            base_units_path=base_units_path,
            base_vectors_path=base_vectors_path,
            increments=[
                MaterialCacheIncrement(
                    units_path=inc_units_path,
                    vectors_path=inc_vectors_path,
                )
            ],
            output_root=snapshot,
        )
    assert excinfo.value.report["status"] == "failed"
    assert excinfo.value.report["counts"]["conflict_units"] == 1
    assert not snapshot.exists()
    _no_staging_left(tmp_path)


def test_supplementary_conflict_policy_skips_divergent_duplicate(
    tmp_path,
) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    _write_units(base_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(base_vectors_path, [_vector_row("u1", "sha256:aaa")])

    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    inc_units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
    inc_vectors_path = inc_dir / "material_vectors.sqlite"
    _write_units(inc_units_path, [_unit("u1", "sha256:DIFFERENT")])
    _write_vectors(
        inc_vectors_path, [_vector_row("u1", "sha256:DIFFERENT")]
    )

    snapshot = tmp_path / "long_term_material_cache"
    report = merge_material_cache(
        base_units_path=base_units_path,
        base_vectors_path=base_vectors_path,
        increments=[
            MaterialCacheIncrement(
                units_path=inc_units_path,
                vectors_path=inc_vectors_path,
            )
        ],
        output_root=snapshot,
        supplementary_conflict_policy=True,
    )

    assert report["status"] == "completed"
    assert report["counts"]["supplementary_skipped_duplicate_units"] == 1
    assert report["counts"]["supplementary_skipped_duplicate_vectors"] == 1
    assert report["counts"]["conflict_units"] == 0
    assert report["counts"]["conflict_vectors"] == 0
    assert snapshot.is_dir()
    payload = json.loads(
        (snapshot / "MATERIAL_UNITS_FINAL.json").read_text(encoding="utf-8")
    )
    assert len(payload["units"]) == 1
    assert payload["units"][0]["durable_content"]["content_hash"] == (
        "sha256:aaa"
    )
    connection = sqlite3.connect(str(snapshot / "material_vectors.sqlite"))
    try:
        row = connection.execute(
            "SELECT content_hash FROM semantic_vectors WHERE unit_id='u1'"
        ).fetchone()
    finally:
        connection.close()
    assert row[0] == "sha256:aaa"
    reasons = {
        item["reason"]
        for item in report["supplementary_skipped_duplicates"]
    }
    assert reasons == {
        "content_hash_collision_skipped",
        "vector_content_hash_mismatch_skipped",
    }
    _no_staging_left(tmp_path)


def test_supplementary_conflict_policy_missing_unit_id_still_fails(
    tmp_path,
) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    _write_units(base_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(base_vectors_path, [_vector_row("u1", "sha256:aaa")])

    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    inc_units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
    inc_vectors_path = inc_dir / "material_vectors.sqlite"
    missing_id = _unit("u2", "sha256:bbb")
    missing_id["unit_id"] = ""
    _write_units(inc_units_path, [missing_id])
    _write_vectors(inc_vectors_path, [_vector_row("u2", "sha256:bbb")])

    snapshot = tmp_path / "long_term_material_cache"
    with pytest.raises(MaterialCacheMergeError) as excinfo:
        merge_material_cache(
            base_units_path=base_units_path,
            base_vectors_path=base_vectors_path,
            increments=[
                MaterialCacheIncrement(
                    units_path=inc_units_path,
                    vectors_path=inc_vectors_path,
                )
            ],
            output_root=snapshot,
            supplementary_conflict_policy=True,
        )
    assert excinfo.value.report["counts"]["conflict_units"] == 1
    assert not snapshot.exists()
    _no_staging_left(tmp_path)


def test_missing_vector_fails_closed(tmp_path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    _write_units(base_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(base_vectors_path, [_vector_row("u1", "sha256:aaa")])

    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    inc_units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
    inc_vectors_path = inc_dir / "material_vectors.sqlite"
    _write_units(inc_units_path, [_unit("u2", "sha256:bbb")])
    _write_vectors(inc_vectors_path, [])

    snapshot = tmp_path / "long_term_material_cache"
    with pytest.raises(MaterialCacheMergeError) as excinfo:
        merge_material_cache(
            base_units_path=base_units_path,
            base_vectors_path=base_vectors_path,
            increments=[
                MaterialCacheIncrement(
                    units_path=inc_units_path,
                    vectors_path=inc_vectors_path,
                )
            ],
            output_root=snapshot,
        )
    assert excinfo.value.report["counts"]["missing_vector_units"] == 1
    assert "u2" in excinfo.value.report["missing_vector_units"]
    assert not snapshot.exists()
    _no_staging_left(tmp_path)


def test_schema_mismatch_fails_closed(tmp_path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    _write_units(base_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(base_vectors_path, [_vector_row("u1", "sha256:aaa")])

    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    inc_units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
    inc_vectors_path = inc_dir / "material_vectors.sqlite"
    _write_units(inc_units_path, [_unit("u2", "sha256:bbb")])
    with sqlite3.connect(str(inc_vectors_path)) as connection:
        connection.execute(
            "CREATE TABLE semantic_vectors("
            "unit_id TEXT PRIMARY KEY, content_hash TEXT)"
        )

    snapshot = tmp_path / "long_term_material_cache"
    with pytest.raises(MaterialCacheMergeError, match="missing columns"):
        merge_material_cache(
            base_units_path=base_units_path,
            base_vectors_path=base_vectors_path,
            increments=[
                MaterialCacheIncrement(
                    units_path=inc_units_path,
                    vectors_path=inc_vectors_path,
                )
            ],
            output_root=snapshot,
        )
    assert not snapshot.exists()
    _no_staging_left(tmp_path)


def test_dimension_mismatch_fails_closed(tmp_path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    _write_units(base_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(base_vectors_path, [_vector_row("u1", "sha256:aaa")])

    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    inc_units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
    inc_vectors_path = inc_dir / "material_vectors.sqlite"
    _write_units(inc_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(
        inc_vectors_path,
        [_vector_row("u1", "sha256:aaa", dimension=8)],
    )

    snapshot = tmp_path / "long_term_material_cache"
    with pytest.raises(MaterialCacheMergeError) as excinfo:
        merge_material_cache(
            base_units_path=base_units_path,
            base_vectors_path=base_vectors_path,
            increments=[
                MaterialCacheIncrement(
                    units_path=inc_units_path,
                    vectors_path=inc_vectors_path,
                )
            ],
            output_root=snapshot,
        )
    assert excinfo.value.report["counts"]["conflict_vectors"] == 1
    assert not snapshot.exists()
    _no_staging_left(tmp_path)


def test_alternate_model_and_representation_rows_are_not_conflicts(
    tmp_path,
) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    _write_units(base_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(
        base_vectors_path,
        [_vector_row("u1", "sha256:aaa")],
    )

    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    inc_units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
    inc_vectors_path = inc_dir / "material_vectors.sqlite"
    _write_units(inc_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(
        inc_vectors_path,
        [
            _vector_row("u1", "sha256:aaa"),
            _vector_row(
                "u1",
                "sha256:aaa",
                model="text-embedding-v5",
                version="material-unit-surrogate.v2",
            ),
        ],
    )

    snapshot = tmp_path / "long_term_material_cache"
    report = merge_material_cache(
        base_units_path=base_units_path,
        base_vectors_path=base_vectors_path,
        increments=[
            MaterialCacheIncrement(
                units_path=inc_units_path,
                vectors_path=inc_vectors_path,
            )
        ],
        output_root=snapshot,
    )
    assert report["status"] == "completed"
    assert report["counts"]["conflict_vectors"] == 0
    assert report["counts"]["reused_vectors"] == 1
    assert report["counts"]["added_vectors"] == 1
    with sqlite3.connect(str(snapshot / "material_vectors.sqlite")) as conn:
        rows = conn.execute(
            "SELECT unit_id, embedding_model, representation_version "
            "FROM semantic_vectors WHERE unit_id='u1'"
        ).fetchall()
    assert len(rows) == 2
    assert {row[1] for row in rows} == {
        "text-embedding-v4",
        "text-embedding-v5",
    }


def test_vector_content_hash_mismatch_fails_closed(tmp_path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    _write_units(base_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(base_vectors_path, [_vector_row("u1", "sha256:aaa")])

    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    inc_units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
    inc_vectors_path = inc_dir / "material_vectors.sqlite"
    _write_units(inc_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(
        inc_vectors_path,
        [_vector_row("u1", "sha256:WRONG_HASH")],
    )

    snapshot = tmp_path / "long_term_material_cache"
    with pytest.raises(MaterialCacheMergeError) as excinfo:
        merge_material_cache(
            base_units_path=base_units_path,
            base_vectors_path=base_vectors_path,
            increments=[
                MaterialCacheIncrement(
                    units_path=inc_units_path,
                    vectors_path=inc_vectors_path,
                )
            ],
            output_root=snapshot,
        )
    assert excinfo.value.report["counts"]["conflict_vectors"] == 1
    assert any(
        item.get("reason") == "vector_content_hash_mismatch"
        for item in excinfo.value.report["conflicts"]
    )
    assert not snapshot.exists()
    _no_staging_left(tmp_path)


def test_supplementary_conflict_policy_new_id_vector_mismatch_still_fails(
    tmp_path,
) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    _write_units(base_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(base_vectors_path, [_vector_row("u1", "sha256:aaa")])

    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    inc_units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
    inc_vectors_path = inc_dir / "material_vectors.sqlite"
    _write_units(inc_units_path, [_unit("u2", "sha256:bbb")])
    _write_vectors(
        inc_vectors_path,
        [_vector_row("u2", "sha256:WRONG_HASH")],
    )

    snapshot = tmp_path / "long_term_material_cache"
    with pytest.raises(MaterialCacheMergeError) as excinfo:
        merge_material_cache(
            base_units_path=base_units_path,
            base_vectors_path=base_vectors_path,
            increments=[
                MaterialCacheIncrement(
                    units_path=inc_units_path,
                    vectors_path=inc_vectors_path,
                )
            ],
            output_root=snapshot,
            supplementary_conflict_policy=True,
        )
    assert excinfo.value.report["counts"]["conflict_vectors"] == 1
    assert any(
        item.get("reason") == "unit_vector_content_hash_mismatch"
        for item in excinfo.value.report["conflicts"]
    )
    assert not snapshot.exists()
    _no_staging_left(tmp_path)


def test_merge_preserves_distinct_supplementary_task_references(
    tmp_path,
) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    base_unit = _unit("u1", "sha256:aaa")
    base_unit["query_annotations"] = []
    _write_units(base_units_path, [base_unit])
    _write_vectors(base_vectors_path, [_vector_row("u1", "sha256:aaa")])

    def annotation(reference: dict) -> dict:
        return {
            "query_id": "q1",
            "question_hash": "sha256:q",
            "supplementary_task_references": [reference],
            "propositions": [],
        }

    def make_increment(index: int, reference: dict) -> MaterialCacheIncrement:
        inc_dir = tmp_path / f"inc{index}"
        inc_dir.mkdir()
        units_path = inc_dir / "MATERIAL_UNITS_FINAL.json"
        vectors_path = inc_dir / "material_vectors.sqlite"
        unit = _unit("u1", "sha256:aaa")
        unit["query_annotations"] = [annotation(reference)]
        _write_units(units_path, [unit])
        _write_vectors(vectors_path, [_vector_row("u1", "sha256:aaa")])
        return MaterialCacheIncrement(
            units_path=units_path,
            vectors_path=vectors_path,
        )

    first_ref = {
        "task_id": "task-a",
        "gap_type": "claim_evidence_gap",
        "coverage_ids": ["F1"],
        "context_sha256": "sha256:ref-a",
    }
    second_ref = {
        "task_id": "task-b",
        "gap_type": "review_structure_gap",
        "coverage_ids": ["S01"],
        "context_sha256": "sha256:ref-b",
    }
    snapshot = tmp_path / "long_term_material_cache"
    report = merge_material_cache(
        base_units_path=base_units_path,
        base_vectors_path=base_vectors_path,
        increments=[
            make_increment(1, first_ref),
            make_increment(2, second_ref),
        ],
        output_root=snapshot,
    )
    assert report["status"] == "completed"
    assert report["counts"]["reused_units"] == 2
    assert report["counts"]["reused_vectors"] == 2
    assert report["counts"]["conflict_units"] == 0
    assert report["counts"]["conflict_vectors"] == 0
    merged = json.loads(
        (snapshot / "MATERIAL_UNITS_FINAL.json").read_text(encoding="utf-8")
    )
    unit = next(item for item in merged["units"] if item["unit_id"] == "u1")
    references = [
        reference
        for annotation in unit["query_annotations"]
        for reference in annotation.get("supplementary_task_references") or []
    ]
    assert references == [first_ref, second_ref]


def test_refuses_existing_snapshot_output(tmp_path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_units_path = base_dir / "MATERIAL_UNITS_FINAL.json"
    base_vectors_path = base_dir / "material_vectors.sqlite"
    _write_units(base_units_path, [_unit("u1", "sha256:aaa")])
    _write_vectors(base_vectors_path, [_vector_row("u1", "sha256:aaa")])

    snapshot = tmp_path / "long_term_material_cache"
    merge_material_cache(
        base_units_path=base_units_path,
        base_vectors_path=base_vectors_path,
        output_root=snapshot,
    )
    with pytest.raises(MaterialCacheMergeError, match="refusing to overwrite"):
        merge_material_cache(
            base_units_path=base_units_path,
            base_vectors_path=base_vectors_path,
            output_root=snapshot,
        )
