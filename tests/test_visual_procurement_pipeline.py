"""Offline tests for the additive visual procurement bridge.

Covers route guards, parent+new immutable snapshot publication, no-progress
paths, reviewer failure isolation, parent immutability/self-contained
snapshots, and the bundled production classifier helper.  No network calls.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from optomind_research.runtime.supplementary_retrieval_contract import (
    GAP_TYPE_REQUIRED_CONTEXT_FIELDS,
    SupplementaryRetrievalTask,
)
from optomind_research.runtime.supplementary_retrieval_service import (
    ROUTE_VISUAL,
    MaterializationOutcome,
    RetrievalOutcome,
)
from optomind_research.runtime.supplementary_retrieval_pipeline import (
    SupplementaryRetrievalPipeline,
    VisualPipelineUnsupportedError,
)
from optomind_research.runtime.article_visual_asset_planner import (
    ArticleVisualAssetPlannerConfig,
    plan_article_visual_assets,
)
from optomind_research.runtime.visual_asset_planner_adapter import (
    load_visual_cache_records,
)
from optomind_research.runtime.visual_cache_ingest import (
    ingest_visual_candidates,
)
from optomind_research.runtime.visual_cache_store import (
    VisualCachePublicationError,
    VisualCacheStore,
)
from optomind_research.runtime import (
    visual_procurement_pipeline as vpp_module,
)
from optomind_research.runtime.visual_procurement_pipeline import (
    VisualProcurementConfig,
    VisualProcurementContractError,
    VisualReviewBatch,
    make_visual_materialize_callback,
    make_visual_retrieve_callback,
    rank_review_candidates,
    resolve_next_snapshot_version,
    review_with_visual_argument_classifier,
    run_visual_procurement_to_planning,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


_OK_CLASSIFIER_RESULT = {
    "schema_version": "visual_argument_classification.v1",
    "classification_status": "ok",
    "visual_argument_type": "mechanism_anchor",
    "secondary_visual_argument_types": [],
    "visual_argument_claim": "Reviewed claim.",
    "supported_aspect": "mechanism",
    "argument_basis": ["caption", "image"],
    "confidence": 0.9,
    "risk_flags": [],
    "needs_human_review": False,
}


def _image(path: Path, color: str = "white") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 160), color=color).save(path)
    return path


@pytest.fixture()
def proc_tmp() -> Path:
    """Workspace-local scratch dir; avoids stale ACL-blocked tmp roots."""

    scratch = PROJECT_ROOT / ".codex-tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    root = scratch / f"visual-procurement-test-{uuid.uuid4().hex[:10]}"
    root.mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)
    try:
        scratch.rmdir()
    except OSError:
        pass


def _caption_band_image(path: Path, *, caption_h: int = 50) -> Path:
    """Figure image with a caption-like prose band baked into the pixels."""

    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 320, 240
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for x in range(20, width - 20, 12):
        draw.line(
            [(x, height - caption_h - 30), (x + 8, 30)],
            fill=(30, 90, 200),
            width=3,
        )
    draw.line(
        [
            (20, height - caption_h - 20),
            (width - 20, height - caption_h - 20),
        ],
        fill=(220, 120, 30),
        width=4,
    )
    y0 = height - caption_h
    for y in range(y0 + 8, height - 4, 10):
        x = 25
        while x < width - 60:
            draw.rectangle([x, y, x + 8, y + 6], fill=(20, 20, 20))
            x += 30
    image.save(path)
    return path


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate(
    chunk_id: str,
    image_path: Path,
    *,
    review_decision: str = "",
    visual_argument_type: str = "mechanism_anchor",
    caption: str = "Procurement figure caption.",
) -> dict:
    return {
        "chunk_id": chunk_id,
        "paper_id": "paper-proc",
        "doi": "10.1/proc",
        "title": "Procurement test paper",
        "chunk_kind": "single_figure",
        "local_image_path": str(image_path),
        "caption": caption,
        "visual_argument_type": visual_argument_type,
        "visual_argument_status": "pending_multimodal_review",
        "visual_argument_needs_human_review": 1,
        "review_decision": review_decision,
        "use_permission": "discovery_only",
    }


def _publish_parent(cache_root: Path, parent_image: Path) -> VisualCacheStore:
    store = VisualCacheStore(cache_root)
    units, report = ingest_visual_candidates(
        [_candidate("parent-chunk", parent_image)],
        source_root=parent_image.parent,
        copy_assets_to=cache_root / "parent_staging",
    )
    assert report["errors"] == []
    assert len(units) == 1
    store.publish_snapshot(
        version="snapshot-0001",
        units=units,
        assets_dir=cache_root / "parent_staging",
    )
    return store


def _runtime_kb(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE visual_chunks(
              chunk_id TEXT PRIMARY KEY,
              paper_id TEXT,
              doi TEXT,
              title TEXT,
              chunk_kind TEXT,
              local_image_path TEXT,
              caption TEXT,
              visual_argument_type TEXT,
              visual_argument_status TEXT,
              visual_argument_needs_human_review INTEGER,
              review_decision TEXT,
              use_permission TEXT,
              raw_json TEXT
            )
            """
        )
        for row in rows:
            conn.execute(
                "INSERT INTO visual_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*row, "{}"),
            )
        conn.commit()
    finally:
        conn.close()


def _task(
    gap_type: str = "visual_material_gap",
    *,
    task_id: str = "task-visual",
) -> SupplementaryRetrievalTask:
    return SupplementaryRetrievalTask(
        task_id=task_id,
        gap_type=gap_type,
        context_refs=GAP_TYPE_REQUIRED_CONTEXT_FIELDS[gap_type],
        priority=1,
        source_provenance={"producer": "test", "stage": "procurement"},
        success_criteria=("has_adequate_visual",),
        material_requirements=("visual_material",),
        retrieval_queries=("radiative cooling multilayer inverse design",),
        visual_route=gap_type == "visual_material_gap",
    )


def _meta(**overrides: str) -> dict:
    payload = {
        "idempotency_key": "ik-1",
        "task_fingerprint": "fp-1",
        "task_id": "task-visual",
        "attempt_id": "attempt-1",
        "route": ROUTE_VISUAL,
        "gap_type": "visual_material_gap",
    }
    payload.update(overrides)
    return payload


def _retrieval(
    kb_path: Path,
    work_dir: Path,
    *,
    route: str = ROUTE_VISUAL,
) -> RetrievalOutcome:
    return RetrievalOutcome(
        candidates=[],
        adequate=True,
        metadata={
            "runtime_kb_sqlite": str(kb_path),
            "work_dir": str(work_dir),
            "gap_type": "visual_material_gap",
        },
        route=route,
    )


def _fake_reviewer(
    *,
    per_candidate_error_ids: set[str] | None = None,
    raise_for_ids: set[str] | None = None,
):
    per_candidate_error_ids = set(per_candidate_error_ids or ())
    raise_for_ids = set(raise_for_ids or ())

    def reviewer(candidates):
        if any(
            str(candidate.get("chunk_id") or "") in raise_for_ids
            for candidate in candidates
        ):
            raise RuntimeError("reviewer exploded")
        records = []
        errors = []
        for candidate in candidates:
            updated = dict(candidate)
            candidate_id = str(candidate.get("chunk_id") or "")
            if candidate_id in per_candidate_error_ids:
                errors.append(
                    {
                        "candidate_id": candidate_id,
                        "reason": "classifier_failed",
                    }
                )
                updated["visual_argument_status"] = (
                    "pending_multimodal_review"
                )
            else:
                updated["visual_argument_type"] = "method_or_workflow"
                updated["visual_argument_claim"] = (
                    "Reviewed visual claim."
                )
                updated["visual_argument_confidence"] = "0.92"
                updated["visual_argument_needs_human_review"] = False
                updated["visual_argument_schema_version"] = (
                    "visual_argument_classification.v1"
                )
                updated["visual_argument_status"] = "ok"
            records.append(updated)
        return VisualReviewBatch(
            records=records,
            errors=errors,
            usage=[
                {
                    "agent": "fake_reviewer",
                    "input_tokens": 7,
                }
            ],
        )

    return reviewer


def _fake_literature_materialize(calls: list):
    def materialize(task, retrieval, context, meta):
        calls.append(str(meta.get("idempotency_key") or ""))
        return MaterializationOutcome(
            sources=[{"id": "text-source-1"}],
            adequate=True,
            total_references=1,
            materialized_route="task_local_increment",
            metadata={
                "reason": "committed",
                "work_dir": str(meta.get("work_dir") or ""),
                "qwen_usage": [
                    {"model": "qwen", "input_tokens": 10}
                ],
                "embedding_usage": {
                    "input_tokens": 5,
                    "request_count": 1,
                },
            },
        )

    return materialize


def _assert_stable_no_progress_shape(metadata: dict, reason: str) -> None:
    for key in (
        "review",
        "usage",
        "candidate_counts",
        "parent",
        "snapshot",
        "errors",
        "warnings",
    ):
        assert key in metadata, f"missing metadata key: {key}"
    assert metadata["reason"] == reason
    assert metadata["adequate"] is False
    assert "cap" in metadata["review"]
    assert "skipped" in metadata["review"]
    assert metadata["review"]["usage"] == metadata["usage"]["visual_review"]
    assert set(metadata["usage"]) == {
        "text_materialization",
        "visual_review",
    }
    assert metadata["snapshot"] == {}
    assert "version" in metadata["parent"]
    assert "snapshot_dir" in metadata["parent"]
    for key in (
        "discovered",
        "reviewed",
        "review_skipped",
        "review_errors",
        "duplicates_against_parent",
        "ingested",
        "ingest_errors",
        "published_new_units",
    ):
        assert key in metadata["candidate_counts"], f"missing count: {key}"


def test_visual_retrieve_callback_enforces_route_identity(tmp_path: Path) -> None:
    lit_calls: list[str] = []

    def literature_retrieve(task, queries, context, meta):
        lit_calls.append(task.task_id)
        return RetrievalOutcome(
            candidates=[{"id": "admitted-1"}],
            adequate=True,
            metadata={"runtime_kb_sqlite": str(tmp_path / "kb.sqlite")},
            route="literature",
        )

    bridge = make_visual_retrieve_callback(literature_retrieve)
    with pytest.raises(VisualProcurementContractError):
        bridge(
            _task(gap_type="claim_evidence_gap", task_id="task-text"),
            [],
            {},
            _meta(),
        )
    with pytest.raises(VisualProcurementContractError):
        bridge(_task(), [], {}, _meta(route="literature"))

    outcome = bridge(_task(), [], {}, _meta())
    assert isinstance(outcome, RetrievalOutcome)
    assert outcome.route == ROUTE_VISUAL
    assert outcome.adequate is True
    assert lit_calls == ["task-visual"]

    with pytest.raises(VisualProcurementContractError):
        make_visual_retrieve_callback(None)


def test_visual_materialize_callback_enforces_route_identity(
    tmp_path: Path,
) -> None:
    store = VisualCacheStore(tmp_path / "cache")
    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=_fake_reviewer(),
    )
    with pytest.raises(VisualProcurementContractError):
        materialize(
            _task(gap_type="claim_evidence_gap", task_id="task-text"),
            _retrieval(tmp_path / "kb.sqlite", tmp_path),
            {},
            _meta(),
        )
    with pytest.raises(VisualProcurementContractError):
        materialize(
            _task(),
            _retrieval(
                tmp_path / "kb.sqlite",
                tmp_path,
                route="literature",
            ),
            {},
            _meta(),
        )
    with pytest.raises(VisualProcurementContractError):
        make_visual_materialize_callback(
            cache_store=store,
            config=VisualProcurementConfig(review_cap=-1),
        )
    with pytest.raises(VisualProcurementContractError):
        make_visual_materialize_callback()


def test_procurement_publishes_parent_plus_new_snapshot(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    parent_image = _image(tmp_path / "source" / "parent.png", "navy")
    store = _publish_parent(cache_root, parent_image)
    parent_payload_before = store.load_snapshot("snapshot-0001")
    parent_units_json_before = _sha256_file(
        store.snapshot_path("snapshot-0001") / "units.json"
    )
    parent_asset_names = {
        path.name
        for path in (store.snapshot_path("snapshot-0001") / "assets").iterdir()
        if path.is_file()
    }

    source = tmp_path / "source"
    new1 = _image(source / "new1.png", "lime")
    new2 = _image(source / "new2.png", "teal")
    approved = _image(source / "approved.png", "cyan")
    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "new-1",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(new1),
                "New caption one.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            ),
            (
                "new-2",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(new2),
                "New caption two.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            ),
            (
                "approved-1",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(approved),
                "Approved caption.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "human_approved",
                "discovery_only",
            ),
        ],
    )
    lit_calls: list[str] = []
    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=_fake_reviewer(),
        literature_materialize=_fake_literature_materialize(lit_calls),
        config=VisualProcurementConfig(review_cap=8),
    )
    outcome = materialize(
        _task(),
        _retrieval(kb, tmp_path / "work"),
        {},
        _meta(),
    )
    assert outcome.adequate is True
    metadata = outcome.metadata
    assert metadata["reason"] == "committed"
    assert metadata["candidate_counts"] == {
        "discovered": 3,
        "reviewed": 3,
        "review_skipped": 0,
        "review_errors": 0,
        "duplicates_against_parent": 0,
        "ingested": 3,
        "ingest_errors": 0,
        "published_new_units": 3,
    }
    assert metadata["parent"]["version"] == "snapshot-0001"
    assert metadata["snapshot"]["version"] == "snapshot-0002"
    assert Path(metadata["snapshot"]["path"]).is_dir()
    assert metadata["version_selection"]["mode"] == "deterministic_next"
    assert metadata["text_materialization"]["delegated"] is True
    assert metadata["text_materialization"]["adequate"] is True
    assert lit_calls == ["ik-1"]
    assert len(metadata["usage"]["visual_review"]) == 1
    assert len(outcome.sources) == 3

    new_snapshot = store.load_snapshot("snapshot-0002")
    assert len(new_snapshot["units"]) == 4
    states = {
        unit["approval"]["state"] for unit in new_snapshot["units"]
    }
    assert states == {"pending", "approved"}
    approved_units = [
        unit
        for unit in new_snapshot["units"]
        if unit["approval"]["state"] == "approved"
    ]
    assert len(approved_units) == 1
    assert approved_units[0]["figure_identity"]["asset_id"] == "approved-1"
    assert store.verify_snapshot("snapshot-0002")["status"] == "passed"
    assert store.latest_version() == "snapshot-0002"

    # Parent snapshot is immutable.
    assert store.load_snapshot("snapshot-0001") == parent_payload_before
    assert (
        _sha256_file(store.snapshot_path("snapshot-0001") / "units.json")
        == parent_units_json_before
    )
    # New snapshot is self-contained: parent assets are copied in.
    new_asset_names = {
        path.name
        for path in (store.snapshot_path("snapshot-0002") / "assets").iterdir()
        if path.is_file()
    }
    assert parent_asset_names <= new_asset_names
    parent_unit = next(
        unit
        for unit in parent_payload_before["units"]
        if unit["unit_id"]
    )
    parent_hash = parent_unit["hashes"]["image_sha256"]
    assert any(
        unit["hashes"]["image_sha256"] == parent_hash
        for unit in new_snapshot["units"]
    )


def test_no_candidates_is_no_progress(tmp_path: Path) -> None:
    store = VisualCacheStore(tmp_path / "cache")
    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=_fake_reviewer(),
    )
    outcome = materialize(
        _task(),
        RetrievalOutcome(
            adequate=True,
            metadata={"work_dir": str(tmp_path / "work")},
            route=ROUTE_VISUAL,
        ),
        {},
        _meta(),
    )
    assert outcome.adequate is False
    assert outcome.metadata["reason"] == "no_visual_candidates"
    assert outcome.metadata["candidate_counts"]["discovered"] == 0
    assert "runtime_kb_sqlite_missing" in outcome.metadata["warnings"]
    _assert_stable_no_progress_shape(
        outcome.metadata,
        "no_visual_candidates",
    )
    assert store.list_versions() == []


def test_duplicate_only_is_no_progress(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    parent_image = _image(tmp_path / "source" / "parent.png", "navy")
    store = _publish_parent(cache_root, parent_image)
    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "same-image-candidate",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(parent_image),
                "Same image as parent.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            )
        ],
    )
    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=_fake_reviewer(),
    )
    outcome = materialize(
        _task(),
        _retrieval(kb, tmp_path / "work"),
        {},
        _meta(),
    )
    assert outcome.adequate is False
    assert outcome.metadata["reason"] == "duplicate_only_no_new_units"
    assert (
        outcome.metadata["candidate_counts"]["duplicates_against_parent"]
        == 1
    )
    assert outcome.metadata["dedupe"]["duplicates_against_parent"][0][
        "candidate_id"
    ] == "same-image-candidate"
    _assert_stable_no_progress_shape(
        outcome.metadata,
        "duplicate_only_no_new_units",
    )
    assert store.list_versions() == ["snapshot-0001"]


def test_reviewer_failure_is_isolated_per_candidate(tmp_path: Path) -> None:
    store = VisualCacheStore(tmp_path / "cache")
    source = tmp_path / "source"
    good = _image(source / "good.png", "lime")
    bad = _image(source / "bad.png", "teal")
    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "good-1",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(good),
                "Good caption.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            ),
            (
                "bad-1",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(bad),
                "Bad caption.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            ),
        ],
    )
    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=_fake_reviewer(per_candidate_error_ids={"bad-1"}),
    )
    outcome = materialize(
        _task(),
        _retrieval(kb, tmp_path / "work"),
        {},
        _meta(),
    )
    assert outcome.adequate is True
    metadata = outcome.metadata
    assert [error["candidate_id"] for error in metadata["review"]["errors"]] == [
        "bad-1"
    ]
    assert metadata["candidate_counts"]["review_errors"] == 1
    assert metadata["candidate_counts"]["published_new_units"] == 2
    units = {
        unit["figure_identity"]["asset_id"]: unit
        for unit in store.load_snapshot("snapshot-0001")["units"]
    }
    assert units["good-1"]["argumentative_roles"]["primary"] == (
        "method_or_workflow"
    )
    assert units["good-1"]["approval"]["state"] == "pending"
    # Failed candidate keeps its original type and stays pending.
    assert units["bad-1"]["argumentative_roles"]["primary"] == (
        "mechanism_anchor"
    )
    assert units["bad-1"]["approval"]["state"] == "pending"

    # A batch-level reviewer exception is isolated too: every candidate is
    # reported and still ingested with pending approval (fail-open).
    store2 = VisualCacheStore(tmp_path / "cache2")
    materialize2 = make_visual_materialize_callback(
        cache_store=store2,
        reviewer=_fake_reviewer(raise_for_ids={"good-1", "bad-1"}),
    )
    outcome2 = materialize2(
        _task(),
        _retrieval(kb, tmp_path / "work2"),
        {},
        _meta(idempotency_key="ik-2"),
    )
    assert outcome2.adequate is True
    assert len(outcome2.metadata["review"]["errors"]) == 2
    assert all(
        "review_batch_failed" in error["reason"]
        for error in outcome2.metadata["review"]["errors"]
    )
    assert len(store2.load_snapshot("snapshot-0001")["units"]) == 2


def test_production_classifier_helper_merges_without_approval() -> None:
    class FakeClassifier:
        def __init__(self, result, *, raise_error: bool = False):
            self.result = result
            self.raise_error = raise_error

        def classify_chunk(self, chunk):
            if self.raise_error:
                raise RuntimeError("classifier exploded")
            return dict(self.result), {
                "chunk_id": chunk.get("chunk_id", ""),
                "status": "ok",
                "_llm_usage": {
                    "model": "fake",
                    "input_tokens": 3,
                },
            }

    result = {
        "schema_version": "visual_argument_classification.v1",
        "classification_status": "ok",
        "visual_argument_type": "mechanism_anchor",
        "secondary_visual_argument_types": ["method_or_workflow"],
        "visual_argument_claim": "Model reviewed claim.",
        "supported_aspect": "mechanism",
        "argument_basis": ["caption", "image"],
        "confidence": 0.9,
        "risk_flags": [],
        "needs_human_review": False,
    }
    candidate = _candidate("c1", Path("unused.png"))
    batch = review_with_visual_argument_classifier(
        [candidate],
        classifier=FakeClassifier(result),
        review_cap=1,
    )
    record = batch.records[0]
    assert record["visual_argument_type"] == "mechanism_anchor"
    assert record["visual_argument_claim"] == "Model reviewed claim."
    assert record["visual_argument_confidence"] == "0.9"
    assert record["visual_argument_status"] == "ok"
    assert record["visual_argument_needs_human_review"] is False
    assert record.get("review_decision") == ""
    assert batch.errors == []
    assert batch.usage[0]["candidate_id"] == "c1"

    needs_review = {**result, "needs_human_review": True}
    batch2 = review_with_visual_argument_classifier(
        [candidate],
        classifier=FakeClassifier(needs_review),
    )
    assert batch2.records[0]["visual_argument_status"] == (
        "pending_multimodal_review"
    )

    batch3 = review_with_visual_argument_classifier(
        [candidate],
        classifier=FakeClassifier(result, raise_error=True),
    )
    assert batch3.errors[0]["candidate_id"] == "c1"
    assert batch3.records[0]["visual_argument_status"] == (
        "pending_multimodal_review"
    )


def test_config_version_contract_and_existing_snapshot(
    tmp_path: Path,
) -> None:
    assert resolve_next_snapshot_version("snapshot-0003") == "snapshot-0004"
    assert resolve_next_snapshot_version("v1") == "v2"
    assert resolve_next_snapshot_version(None) == "snapshot-0001"
    assert (
        resolve_next_snapshot_version(None, configured="snapshot-0009")
        == "snapshot-0009"
    )

    cache_root = tmp_path / "cache"
    parent_image = _image(tmp_path / "source" / "parent.png", "navy")
    store = _publish_parent(cache_root, parent_image)
    new_image = _image(tmp_path / "source" / "new.png", "lime")
    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "new-only",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(new_image),
                "New caption.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            )
        ],
    )
    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=_fake_reviewer(),
        config=VisualProcurementConfig(snapshot_version="snapshot-0001"),
    )
    with pytest.raises(VisualCachePublicationError):
        materialize(
            _task(),
            _retrieval(kb, tmp_path / "work"),
            {},
            _meta(),
        )


def test_relative_project_root_path_resolves_and_publishes_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative candidate path (repo-root style) must resolve and publish."""

    repo_root = tmp_path / "repo"
    relative_path = (
        "outputs/section_supplementary_20260812/probe/visual_candidates/"
        "relative.png"
    )
    image = _image(repo_root / relative_path, "gold")
    monkeypatch.setattr(vpp_module, "PROJECT_ROOT", repo_root)
    monkeypatch.chdir(repo_root)

    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "relative-candidate",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                relative_path,
                "Relative path caption.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            )
        ],
    )
    store = VisualCacheStore(tmp_path / "cache")
    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=_fake_reviewer(),
    )
    outcome = materialize(
        _task(),
        _retrieval(kb, tmp_path / "work"),
        {},
        _meta(),
    )
    assert outcome.adequate is True
    metadata = outcome.metadata
    assert metadata["reason"] == "committed"
    assert metadata["candidate_counts"]["discovered"] == 1
    assert metadata["candidate_counts"]["ingested"] == 1
    assert metadata["candidate_counts"]["ingest_errors"] == 0
    assert metadata["candidate_counts"]["published_new_units"] == 1
    assert metadata["snapshot"]["version"] == "snapshot-0001"
    units = store.load_snapshot("snapshot-0001")["units"]
    assert len(units) == 1
    assert units[0]["figure_identity"]["asset_id"] == "relative-candidate"
    assert store.verify_snapshot("snapshot-0001")["status"] == "passed"
    assert image.exists()


def test_unresolved_relative_path_preserves_ingest_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unresolved candidate paths are reported, not silently masked."""

    (tmp_path / "empty-cwd").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path / "empty-cwd")
    monkeypatch.setattr(vpp_module, "PROJECT_ROOT", tmp_path / "empty-cwd")
    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "missing-image",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                "outputs/does-not-exist/visual_candidates/missing.png",
                "Missing image caption.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            )
        ],
    )
    store = VisualCacheStore(tmp_path / "cache")
    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=_fake_reviewer(),
    )
    outcome = materialize(
        _task(),
        _retrieval(kb, tmp_path / "work"),
        {},
        _meta(),
    )
    assert outcome.adequate is False
    metadata = outcome.metadata
    assert metadata["reason"] == "ingest_all_candidates_failed"
    assert metadata["candidate_counts"]["discovered"] == 1
    assert metadata["candidate_counts"]["ingested"] == 0
    assert metadata["candidate_counts"]["ingest_errors"] == 1
    assert metadata["candidate_counts"]["published_new_units"] == 0
    ingest_errors = metadata["ingest"]["errors"]
    assert len(ingest_errors) == 1
    assert ingest_errors[0]["candidate_id"] == "missing-image"
    assert "image_path_unresolved" in ingest_errors[0]["reason"]
    assert any(
        error["candidate_id"] == "missing-image"
        and error["reason"].startswith("ingest:")
        for error in metadata["errors"]
    )
    _assert_stable_no_progress_shape(
        metadata,
        "ingest_all_candidates_failed",
    )
    assert store.list_versions() == []


def test_qwen_usage_propagation(tmp_path: Path) -> None:
    store = VisualCacheStore(tmp_path / "cache")
    image = _image(tmp_path / "source" / "usage.png", "lime")
    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "usage-candidate",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(image),
                "Usage caption.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            )
        ],
    )

    def reviewer(candidates):
        records = [dict(candidate) for candidate in candidates]
        for record in records:
            record["visual_argument_type"] = "mechanism_anchor"
            record["visual_argument_claim"] = "Reviewed claim."
            record["visual_argument_confidence"] = "0.9"
            record["visual_argument_needs_human_review"] = False
            record["visual_argument_schema_version"] = (
                "visual_argument_classification.v1"
            )
            record["visual_argument_status"] = "ok"
        return VisualReviewBatch(
            records=records,
            errors=[],
            usage=[
                {
                    "model": "qwen-vl",
                    "input_tokens": 42,
                    "output_tokens": 7,
                }
            ],
        )

    def literature_materialize(task, retrieval, context, meta):
        return MaterializationOutcome(
            sources=[],
            adequate=True,
            total_references=1,
            materialized_route="task_local_increment",
            metadata={
                "reason": "committed",
                "qwen_usage": [
                    {"model": "qwen", "input_tokens": 10}
                ],
                "embedding_usage": {
                    "input_tokens": 5,
                    "request_count": 1,
                },
            },
        )

    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=reviewer,
        literature_materialize=literature_materialize,
    )
    outcome = materialize(
        _task(),
        _retrieval(kb, tmp_path / "work"),
        {},
        _meta(),
    )
    assert outcome.adequate is True
    usage = outcome.metadata["usage"]
    assert usage["visual_review"] == [
        {
            "model": "qwen-vl",
            "input_tokens": 42,
            "output_tokens": 7,
        }
    ]
    assert usage["text_materialization"]["qwen_usage"] == [
        {"model": "qwen", "input_tokens": 10}
    ]
    assert usage["text_materialization"]["embedding_usage"] == {
        "input_tokens": 5,
        "request_count": 1,
    }
    assert outcome.metadata["review"]["usage"] == usage["visual_review"]


def test_pipeline_default_preserves_unsupported_visual_callbacks(
    tmp_path: Path,
) -> None:
    pipeline = SupplementaryRetrievalPipeline(
        tmp_path / "svc.sqlite",
        work_root=tmp_path / "work",
    )
    callbacks = pipeline.make_service_callbacks()
    with pytest.raises(VisualPipelineUnsupportedError):
        pipeline.visual_retrieve(_task(), [], {}, _meta())
    with pytest.raises(VisualPipelineUnsupportedError):
        pipeline.visual_materialize(
            _task(),
            _retrieval(tmp_path / "kb.sqlite", tmp_path),
            {},
            _meta(),
        )
    # Literature callbacks stay wired for the text route.
    assert callbacks.retrieve is pipeline.retrieve_callback
    assert callbacks.materialize is pipeline.materialize_callback
    assert pipeline.visual_reviewer is None
    assert pipeline.visual_procurement_config is None


def test_pipeline_visual_cache_root_wires_bridge_callbacks(
    tmp_path: Path,
) -> None:
    image = _image(tmp_path / "source" / "wired.png", "lime")
    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "wired-candidate",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(image),
                "Wired caption.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            )
        ],
    )
    lit_calls: list[str] = []
    injected_reviewer = _fake_reviewer()

    def fake_retrieve(task, queries, context, meta):
        lit_calls.append("retrieve")
        return RetrievalOutcome(
            candidates=[],
            adequate=True,
            metadata={
                "runtime_kb_sqlite": str(kb),
                "work_dir": str(tmp_path / "work"),
            },
            route="literature",
        )

    def fake_materialize(task, retrieval, context, meta):
        lit_calls.append("materialize")
        return MaterializationOutcome(
            sources=[],
            adequate=True,
            total_references=1,
            metadata={"reason": "committed"},
        )

    pipeline = SupplementaryRetrievalPipeline(
        tmp_path / "svc.sqlite",
        work_root=tmp_path / "work",
        retrieve_callback=fake_retrieve,
        materialize_callback=fake_materialize,
        visual_cache_root=tmp_path / "vcache",
        visual_reviewer=injected_reviewer,
        enable_visual_review=False,
    )
    callbacks = pipeline.make_service_callbacks()
    assert callbacks.retrieve is fake_retrieve
    assert callbacks.materialize is fake_materialize
    # Injected reviewer wins even when review is disabled by default.
    assert pipeline.visual_reviewer is injected_reviewer

    retrieval = pipeline.visual_retrieve(_task(), [], {}, _meta())
    assert isinstance(retrieval, RetrievalOutcome)
    assert retrieval.route == ROUTE_VISUAL
    assert lit_calls == ["retrieve"]

    outcome = pipeline.visual_materialize(
        _task(),
        retrieval,
        {},
        _meta(idempotency_key="ik-wired"),
    )
    assert isinstance(outcome, MaterializationOutcome)
    assert outcome.adequate is True
    assert lit_calls == ["retrieve", "materialize"]
    assert outcome.metadata["review"]["usage"] == [
        {"agent": "fake_reviewer", "input_tokens": 7}
    ]
    store = VisualCacheStore(tmp_path / "vcache")
    assert store.latest_version() == "snapshot-0001"
    assert len(store.load_snapshot("snapshot-0001")["units"]) == 1
    # The injected reviewer actually classified (no real network call).
    assert (
        store.load_snapshot("snapshot-0001")["units"][0][
            "argumentative_roles"
        ]["primary"]
        == "method_or_workflow"
    )


def test_pipeline_enable_visual_procurement_flag_wires_callbacks(
    tmp_path: Path,
) -> None:
    image = _image(tmp_path / "source" / "flag.png", "lime")
    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "flag-candidate",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(image),
                "Flag caption.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            )
        ],
    )

    def fake_retrieve(task, queries, context, meta):
        return RetrievalOutcome(
            candidates=[],
            adequate=True,
            metadata={
                "runtime_kb_sqlite": str(kb),
                "work_dir": str(tmp_path / "work"),
            },
            route="literature",
        )

    def fake_materialize(task, retrieval, context, meta):
        return MaterializationOutcome(
            sources=[],
            adequate=True,
            total_references=1,
            metadata={"reason": "committed"},
        )

    pipeline = SupplementaryRetrievalPipeline(
        tmp_path / "svc.sqlite",
        work_root=tmp_path / "work",
        retrieve_callback=fake_retrieve,
        materialize_callback=fake_materialize,
        enable_visual_procurement=True,
        enable_visual_review=False,
    )
    assert pipeline.visual_reviewer is None
    assert pipeline.visual_procurement_config is not None
    retrieval = pipeline.visual_retrieve(_task(), [], {}, _meta())
    assert retrieval.route == ROUTE_VISUAL
    outcome = pipeline.visual_materialize(
        _task(),
        retrieval,
        {},
        _meta(idempotency_key="ik-flag"),
    )
    assert outcome.adequate is True
    assert outcome.metadata["review"]["usage"] == []
    # Default cache root lives under the pipeline work root.
    default_store = VisualCacheStore(tmp_path / "work" / "long_term_visual_cache")
    assert default_store.latest_version() == "snapshot-0001"
    callbacks = pipeline.make_service_callbacks()
    assert callbacks.visual_retrieve is pipeline.visual_retrieve
    assert callbacks.visual_materialize is pipeline.visual_materialize


def test_pipeline_default_reviewer_is_production_classifier(
    tmp_path: Path,
) -> None:
    def fake_retrieve(task, queries, context, meta):
        return RetrievalOutcome(
            candidates=[],
            adequate=True,
            metadata={
                "runtime_kb_sqlite": str(tmp_path / "kb.sqlite"),
                "work_dir": str(tmp_path / "work"),
            },
            route="literature",
        )

    def fake_materialize(task, retrieval, context, meta):
        return MaterializationOutcome(
            sources=[],
            adequate=True,
            total_references=1,
            metadata={"reason": "committed"},
        )

    pipeline = SupplementaryRetrievalPipeline(
        tmp_path / "svc.sqlite",
        work_root=tmp_path / "work",
        retrieve_callback=fake_retrieve,
        materialize_callback=fake_materialize,
        visual_cache_root=tmp_path / "vcache",
    )
    reviewer = pipeline.visual_reviewer
    assert reviewer is not None
    assert getattr(reviewer, "func", None) is (
        review_with_visual_argument_classifier
    )
    assert reviewer.keywords.get("review_cap") == 8
    assert pipeline.visual_procurement_config.review_cap == 8

    # Caller-provided config controls the review cap.
    pipeline2 = SupplementaryRetrievalPipeline(
        tmp_path / "svc2.sqlite",
        work_root=tmp_path / "work2",
        retrieve_callback=fake_retrieve,
        materialize_callback=fake_materialize,
        visual_cache_root=tmp_path / "vcache2",
        visual_procurement_config=VisualProcurementConfig(review_cap=3),
    )
    assert pipeline2.visual_procurement_config.review_cap == 3
    assert pipeline2.visual_reviewer.keywords.get("review_cap") == 3


def test_review_cap_limits_paid_review_not_ingestion(
    tmp_path: Path,
) -> None:
    store = VisualCacheStore(tmp_path / "cache")
    source = tmp_path / "source"
    images = [
        _image(source / f"img{i}.png", color)
        for i, color in enumerate(("red", "green", "blue", "yellow"))
    ]
    kb = tmp_path / "runtime_kb.sqlite"
    rows = []
    for index, image in enumerate(images):
        rows.append(
            (
                f"cap-{index}",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(image),
                f"Caption {index}.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            )
        )
    _runtime_kb(kb, rows)
    reviewed_ids: list[str] = []

    def reviewer(candidates):
        ids = [
            str(candidate.get("chunk_id") or "") for candidate in candidates
        ]
        reviewed_ids.extend(ids)
        records = [dict(candidate) for candidate in candidates]
        for record in records:
            record["visual_argument_type"] = "method_or_workflow"
            record["visual_argument_claim"] = "Reviewed."
            record["visual_argument_confidence"] = "0.9"
            record["visual_argument_needs_human_review"] = False
            record["visual_argument_schema_version"] = (
                "visual_argument_classification.v1"
            )
            record["visual_argument_status"] = "ok"
        return VisualReviewBatch(
            records=records,
            errors=[],
            usage=[
                {"candidate_id": candidate_id, "agent": "fake_reviewer"}
                for candidate_id in ids
            ],
        )

    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=reviewer,
        config=VisualProcurementConfig(review_cap=2),
    )
    outcome = materialize(
        _task(),
        _retrieval(kb, tmp_path / "work"),
        {},
        _meta(),
    )
    assert outcome.adequate is True
    counts = outcome.metadata["candidate_counts"]
    assert counts["discovered"] == 4
    assert counts["reviewed"] == 2
    assert counts["review_skipped"] == 2
    assert counts["review_errors"] == 0
    assert counts["ingested"] == 4
    assert counts["ingest_errors"] == 0
    assert counts["published_new_units"] == 4
    assert len(reviewed_ids) == 2
    assert outcome.metadata["review"]["skipped"] == 2
    usage_ids = {
        row["candidate_id"]
        for row in outcome.metadata["review"]["usage"]
    }
    assert usage_ids == set(reviewed_ids)

    units = store.load_snapshot("snapshot-0001")["units"]
    assert len(units) == 4
    by_asset = {
        unit["figure_identity"]["asset_id"]: unit for unit in units
    }
    reviewed_units = [by_asset[candidate_id] for candidate_id in reviewed_ids]
    unreviewed_ids = {f"cap-{index}" for index in range(4)} - set(
        reviewed_ids
    )
    assert all(
        unit["argumentative_roles"]["primary"] == "method_or_workflow"
        for unit in reviewed_units
    )
    assert all(
        by_asset[candidate_id]["argumentative_roles"]["primary"]
        == "mechanism_anchor"
        for candidate_id in unreviewed_ids
    )
    # Model review never grants approval: every unit stays pending.
    assert all(unit["approval"]["state"] == "pending" for unit in units)


def test_procurement_publishes_empty_caption_candidate_fail_open(
    tmp_path: Path,
) -> None:
    """A real candidate without a caption publishes with neutral fallback."""

    store = VisualCacheStore(tmp_path / "cache")
    image = _image(tmp_path / "source" / "nocap.png", "orange")
    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "nocap-1",
                "paper-proc",
                "10.1/proc",
                "Procurement test paper",
                "single_figure",
                str(image),
                "",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            )
        ],
    )
    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=_fake_reviewer(),
        config=VisualProcurementConfig(review_cap=8),
    )
    outcome = materialize(
        _task(),
        _retrieval(kb, tmp_path / "work"),
        {},
        _meta(),
    )
    assert outcome.adequate is True
    metadata = outcome.metadata
    assert any(
        "caption_missing:nocap-1" in warning
        for warning in metadata["warnings"]
    )
    assert any(
        "caption_missing:nocap-1" in warning
        for warning in metadata["ingest"]["warnings"]
    )
    units = store.load_snapshot("snapshot-0001")["units"]
    assert len(units) == 1
    unit = units[0]
    assert unit["caption"]["clean"] == (
        "Caption unavailable; inspect the source figure."
    )
    assert unit["caption"]["missing"] is True
    assert unit["provenance"]["caption_status"] == "missing_needs_review"
    assert "caption_missing" in unit["review"]["review_flags"]
    assert unit["approval"]["state"] == "pending"
    assert store.verify_snapshot("snapshot-0001")["status"] == "passed"


def test_default_classifier_uses_vision_premium_model_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default reviewer constructs VisualArgumentClassifier with the Qwen
    3.7 Flash tier (vision_premium_model); injected classifiers unchanged."""

    from optomind_research import visual_argument_classifier as vac_module

    constructed: list[dict] = []

    class RecordingClassifier:
        def __init__(self, **kwargs):
            constructed.append(dict(kwargs))

        def classify_chunk(self, chunk):
            return dict(_OK_CLASSIFIER_RESULT), {
                "chunk_id": chunk.get("chunk_id", ""),
                "status": "ok",
                "_llm_usage": {
                    "model": "qwen3.7-flash",
                    "input_tokens": 3,
                },
            }

    monkeypatch.setattr(
        vac_module,
        "VisualArgumentClassifier",
        RecordingClassifier,
    )
    candidate = _candidate("tier-1", Path("unused.png"))
    batch = review_with_visual_argument_classifier([candidate], review_cap=1)
    assert constructed == [{"model_tier": "vision_premium_model"}]
    assert batch.errors == []
    assert batch.records[0]["visual_argument_status"] == "ok"
    assert batch.usage[0]["model"] == "qwen3.7-flash"

    # Caller-injected classifiers bypass the default constructor entirely.
    constructed.clear()

    class InjectedClassifier:
        def __init__(self, **kwargs):
            # Constructed by the test itself; the helper must never call it.
            self._kwargs = kwargs

        def classify_chunk(self, chunk):
            return dict(_OK_CLASSIFIER_RESULT), {
                "chunk_id": chunk.get("chunk_id", ""),
                "status": "ok",
                "_llm_usage": {
                    "model": "injected",
                    "input_tokens": 1,
                },
            }

    batch2 = review_with_visual_argument_classifier(
        [candidate],
        classifier=InjectedClassifier(),
        review_cap=1,
    )
    assert constructed == []
    assert batch2.usage[0]["model"] == "injected"
    assert batch2.records[0]["visual_argument_type"] == "mechanism_anchor"


def test_review_ranking_is_deterministic_and_relevance_aware() -> None:
    candidates = [
        {
            "chunk_id": "irr-missing",
            "paper_id": "p1",
            "title": "Unrelated finance paper",
            "caption": "",
            "search_text": "",
            "labels": [],
            "visual_argument_type": "",
            "visual_argument_confidence": "",
            "review_utility": "",
            "permission": {"status": "requires_review"},
        },
        {
            "chunk_id": "rel-missing",
            "paper_id": "p2",
            "title": "radiative cooling multilayer inverse design",
            "caption": "",
            "search_text": "",
            "labels": [],
            "visual_argument_type": "mechanism_anchor",
            "visual_argument_confidence": "high",
            "review_utility": "high",
            "permission": {"status": "allowed"},
        },
        {
            "chunk_id": "irr-caption",
            "paper_id": "p3",
            "title": "Unrelated stock market paper",
            "caption": "A real caption about something unrelated.",
            "search_text": "",
            "labels": [],
            "visual_argument_type": "mechanism_anchor",
            "visual_argument_confidence": "high",
            "review_utility": "high",
            "permission": {"status": "allowed"},
        },
        {
            "chunk_id": "rel-caption",
            "paper_id": "p4",
            "title": "radiative cooling multilayer",
            "caption": "Measured cooling power multilayer inverse design.",
            "search_text": "",
            "labels": [],
            "visual_argument_type": "mechanism_anchor",
            "visual_argument_confidence": "high",
            "review_utility": "high",
            "permission": {"status": "allowed"},
        },
    ]
    ranked = rank_review_candidates(
        candidates,
        task=_task(),
        context={
            "user_question": "radiative cooling multilayer inverse design"
        },
        review_cap=2,
    )
    # Relevance first, then caption quality: not raw DB order.
    assert [candidate["chunk_id"] for candidate in ranked] == [
        "rel-caption",
        "rel-missing",
    ]
    # Deterministic across calls.
    again = rank_review_candidates(
        candidates,
        task=_task(),
        context={
            "user_question": "radiative cooling multilayer inverse design"
        },
        review_cap=2,
    )
    assert [candidate["chunk_id"] for candidate in again] == [
        candidate["chunk_id"] for candidate in ranked
    ]


def test_review_ranking_applies_source_diversity() -> None:
    candidates = [
        {
            "chunk_id": f"{prefix}",
            "paper_id": paper,
            "title": "radiative cooling multilayer inverse design",
            "caption": f"Real caption {prefix}.",
            "search_text": "",
            "labels": [],
            "visual_argument_type": "mechanism_anchor",
            "visual_argument_confidence": "high",
            "review_utility": "high",
            "permission": {"status": "allowed"},
        }
        for prefix, paper in (("a1", "p1"), ("a2", "p1"), ("b1", "p2"))
    ]
    ranked = rank_review_candidates(
        candidates,
        task=_task(),
        review_cap=3,
        max_per_source=1,
    )
    # Diverse first pass (a1, b1), then backfill uses the remaining budget.
    assert [candidate["chunk_id"] for candidate in ranked] == [
        "a1",
        "b1",
        "a2",
    ]


def test_visual_procurement_to_planning_end_to_end(tmp_path: Path) -> None:
    source = tmp_path / "source"
    captioned_image = _image(source / "a.png", "lime")
    missing_image = _image(source / "b.png", "teal")
    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "cap-1",
                "paper-proc",
                "10.1/proc",
                "Optical resonance mechanism paper",
                "single_figure",
                str(captioned_image),
                "Optical resonance mechanism showing field confinement.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            ),
            (
                "nocap-1",
                "paper-proc",
                "10.1/proc",
                "Optical resonance mechanism paper",
                "single_figure",
                str(missing_image),
                "",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            ),
        ],
    )
    cache_root = tmp_path / "vcache"

    def fake_retrieve(task, queries, context, meta):
        return RetrievalOutcome(
            candidates=[],
            adequate=True,
            metadata={
                "runtime_kb_sqlite": str(kb),
                "work_dir": str(tmp_path / "work"),
            },
            route="literature",
        )

    def fake_materialize(task, retrieval, context, meta):
        return MaterializationOutcome(
            sources=[],
            adequate=True,
            total_references=1,
            metadata={"reason": "committed"},
        )

    pipeline = SupplementaryRetrievalPipeline(
        tmp_path / "svc.sqlite",
        work_root=tmp_path / "work",
        retrieve_callback=fake_retrieve,
        materialize_callback=fake_materialize,
        visual_cache_root=cache_root,
        visual_reviewer=_fake_reviewer(),
        enable_visual_review=False,
    )
    sections = [
        {
            "section_id": "S01",
            "title": "Optical resonance mechanism",
            "text": (
                "The optical resonance mechanism confines the field in the "
                "resonant cavity and enables sensing."
            ),
            "argument_role": "Explain the optical resonance mechanism.",
            "claims": [
                {
                    "claim_id": "CL-1",
                    "statement": (
                        "The optical resonance mechanism confines the field."
                    ),
                    "status": "approved",
                }
            ],
            "expected_visual_arguments": ["mechanism_anchor"],
        }
    ]
    report = run_visual_procurement_to_planning(
        pipeline=pipeline,
        task=_task(),
        context={
            "user_question": "radiative cooling multilayer inverse design"
        },
        execution_meta=_meta(idempotency_key="ik-e2e"),
        sections=sections,
        output_dir=tmp_path / "plans",
        cache_root=cache_root,
    )
    assert report["stages"]["retrieval"]["adequate"] is True
    assert report["stages"]["materialization"]["adequate"] is True
    assert report["stages"]["snapshot_resolution"]["source"] == "published"
    assert report["stages"]["snapshot_resolution"]["version"] == (
        "snapshot-0001"
    )
    assert report["stages"]["planning"]["validation_status"] == "passed"
    assert report["stages"]["planning"]["placement_count"] == 2
    assert report["ok"] is True
    # Materialization usage (fake reviewer rows) survives the workflow stage.
    assert report["stages"]["materialization"]["usage"]["visual_review"] == [
        {"agent": "fake_reviewer", "input_tokens": 7}
    ]
    for name in (
        "VISUAL_CONSTRUCTION_PLAN.json",
        "VISUAL_EDITORIAL_PLAN.json",
        "ARTICLE_VISUAL_IMAGE_REVIEW_QUEUE.json",
    ):
        assert (tmp_path / "plans" / name).is_file()

    plan = json.loads(
        (tmp_path / "plans" / "VISUAL_CONSTRUCTION_PLAN.json").read_text(
            encoding="utf-8"
        )
    )
    store = VisualCacheStore(cache_root)
    units = store.load_snapshot("snapshot-0001")["units"]
    asset_by_unit = {
        unit["unit_id"]: unit["figure_identity"]["asset_id"]
        for unit in units
    }
    placed_assets = [
        asset_by_unit[placement["visual_chunk_id"]]
        for placement in plan["placements"]
    ]
    # Captioned candidate placed first; missing-caption still placed.
    assert placed_assets == ["cap-1", "nocap-1"]
    missing_unit = next(
        unit
        for unit in units
        if unit["figure_identity"]["asset_id"] == "nocap-1"
    )
    assert missing_unit["caption"]["missing"] is True
    assert missing_unit["provenance"]["caption_status"] == (
        "missing_needs_review"
    )


def test_visual_procurement_to_planning_fail_open_no_snapshot(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "empty_cache"

    def fake_retrieve(task, queries, context, meta):
        return RetrievalOutcome(
            candidates=[],
            adequate=True,
            metadata={
                "runtime_kb_sqlite": str(tmp_path / "missing.sqlite"),
                "work_dir": str(tmp_path / "work"),
            },
            route="literature",
        )

    def fake_materialize(task, retrieval, context, meta):
        return MaterializationOutcome(
            sources=[],
            adequate=False,
            metadata={"reason": "no_visual_candidates"},
        )

    pipeline = SupplementaryRetrievalPipeline(
        tmp_path / "svc.sqlite",
        work_root=tmp_path / "work",
        retrieve_callback=fake_retrieve,
        materialize_callback=fake_materialize,
        visual_cache_root=cache_root,
        enable_visual_review=False,
    )
    report = run_visual_procurement_to_planning(
        pipeline=pipeline,
        task=_task(),
        context={},
        execution_meta=_meta(idempotency_key="ik-none"),
        sections=[
            {
                "section_id": "S01",
                "title": "Radiative cooling",
                "text": (
                    "Radiative cooling multilayer inverse design is reviewed."
                ),
                "argument_role": "Explain radiative cooling.",
            }
        ],
        output_dir=tmp_path / "plans",
        cache_root=cache_root,
    )
    assert report["stages"]["materialization"]["adequate"] is False
    assert "usage" in report["stages"]["materialization"]
    assert report["stages"]["snapshot_resolution"]["source"] == "none"
    assert report["stages"]["planning"]["placement_count"] == 0
    assert report["stages"]["planning"]["validation_status"] == "passed"
    assert report["ok"] is True


def test_procurement_caption_contamination_recorded_and_downranked(
    tmp_path: Path,
) -> None:
    store = VisualCacheStore(tmp_path / "cache")
    source = tmp_path / "source"
    clean_image = _image(source / "clean.png", "lime")
    contam_image = _caption_band_image(source / "contam.png")
    kb = tmp_path / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "clean-cap",
                "paper-proc",
                "10.1/proc",
                "Optical resonance mechanism paper",
                "single_figure",
                str(clean_image),
                "Optical resonance mechanism showing field confinement.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            ),
            (
                "contam-cap",
                "paper-proc",
                "10.1/proc",
                "Optical resonance mechanism paper",
                "single_figure",
                str(contam_image),
                "Optical resonance mechanism showing field confinement.",
                "mechanism_anchor",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            ),
        ],
    )
    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=_fake_reviewer(),
        config=VisualProcurementConfig(review_cap=8),
    )
    outcome = materialize(
        _task(),
        _retrieval(kb, tmp_path / "work"),
        {},
        _meta(),
    )
    assert outcome.adequate is True
    metadata = outcome.metadata
    assert any(
        "caption_contamination:contam-cap" in warning
        for warning in metadata["warnings"]
    )
    units = store.load_snapshot("snapshot-0001")["units"]
    contam_unit = next(
        unit
        for unit in units
        if unit["figure_identity"]["asset_id"] == "contam-cap"
    )
    assert contam_unit["crop_hygiene"]["caption_contamination"]["detected"] is True
    assert "caption_in_pixels" in contam_unit["review"]["review_flags"]

    # Adapter preserves the signal and the planner downranks it.
    records = load_visual_cache_records(
        store.snapshot_path("snapshot-0001")
    )
    by_legacy = {record["legacy_chunk_id"]: record for record in records}
    assert by_legacy["contam-cap"]["crop_contamination"] is True
    assert by_legacy["clean-cap"]["crop_contamination"] is False
    section = {
        "section_id": "S01",
        "title": "Optical resonance mechanism",
        "text": (
            "The optical resonance mechanism confines the field in the "
            "resonant cavity and enables sensing."
        ),
        "argument_role": "Explain the optical resonance mechanism.",
        "expected_visual_arguments": ["mechanism_anchor"],
    }
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
        config=ArticleVisualAssetPlannerConfig(
            max_placements_per_section=1
        ),
    )
    assert plan["placements"][0]["visual_chunk_id"] == by_legacy[
        "clean-cap"
    ]["chunk_id"]


def test_procurement_external_discovery_only_table_never_publication_eligible(
    proc_tmp: Path,
) -> None:
    store = VisualCacheStore(proc_tmp / "cache")
    source = proc_tmp / "source"
    image = _image(source / "table.png", "silver")
    kb = proc_tmp / "runtime_kb.sqlite"
    _runtime_kb(
        kb,
        [
            (
                "table-1",
                "paper-proc",
                "10.1/table",
                "Procurement table paper",
                "table",
                str(image),
                "Table 1. Summary of radiative cooling performance.",
                "quantitative_comparison",
                "pending_multimodal_review",
                1,
                "",
                "discovery_only",
            )
        ],
    )
    materialize = make_visual_materialize_callback(
        cache_store=store,
        reviewer=_fake_reviewer(),
    )
    outcome = materialize(
        _task(),
        _retrieval(kb, proc_tmp / "work"),
        {},
        _meta(),
    )
    assert outcome.adequate is True
    metadata = outcome.metadata
    assert metadata["asset_kind_counts"] == {"table": 1}
    assert metadata["publication_eligible_count"] == 0
    assert outcome.sources[0]["asset_kind"] == "table"
    assert outcome.sources[0]["publication_eligible"] is False
    unit = store.load_snapshot("snapshot-0001")["units"][0]
    assert unit["figure_identity"]["asset_kind"] == "table"
    assert unit["asset_typing"]["table"] is True
    assert unit["permission_state"]["publication_eligible"] is False
    assert store.verify_snapshot("snapshot-0001")["status"] == "passed"
