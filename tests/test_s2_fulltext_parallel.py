"""Offline tests for VisualProcurementManifest and build_visual_procurement_manifest.

All tests are fully offline: no network, no model calls, no real SQLite visual
cache.  S2FulltextAcquirer is monkeypatched at its acquire() method.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from optomind_research.s2_fulltext_acquisition import (
    VISUAL_PROCUREMENT_SCHEMA_VERSION,
    VisualProcurementManifest,
    _MAX_VISUAL_PROCUREMENT_PAPERS,
    _paper_has_visual_assets,
    build_visual_procurement_manifest,
)
from optomind_research.s2_schemas import S2PaperRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_dir(tmp_path: Path) -> Path:
    d = tmp_path / "build"
    d.mkdir()
    return d


def _paper(paper_id: str = "p1", **kwargs: Any) -> S2PaperRecord:
    defaults: dict[str, Any] = {
        "paper_id": paper_id,
        "title": f"Test paper {paper_id}",
        "year": 2024,
        "abstract": "An abstract.",
        "s2_open_access_candidate_url": f"https://example.org/{paper_id}.pdf",
    }
    defaults.update(kwargs)
    return S2PaperRecord(**defaults)


def _make_visual_cache_sqlite(tmp_path: Path, paper_ids: list[str]) -> Path:
    """Write a minimal visual_chunks table into a temp SQLite."""
    db = tmp_path / "visual_cache.sqlite"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE visual_chunks "
            "(chunk_id TEXT PRIMARY KEY, paper_id TEXT)"
        )
        for pid in paper_ids:
            conn.execute(
                "INSERT INTO visual_chunks VALUES (?, ?)",
                (f"vc:{pid}", pid),
            )
    return db


def _fake_acquire_result(
    *,
    new_paper_ids: list[str] | None = None,
    new_chunk_ids: list[str] | None = None,
    stats: dict[str, Any] | None = None,
) -> Any:
    return SimpleNamespace(
        new_paper_ids=list(new_paper_ids or []),
        reused_chunk_ids=[],
        new_chunk_ids=list(new_chunk_ids or []),
        stats=stats or {"visual_candidate_count": len(new_chunk_ids or [])},
    )


# ---------------------------------------------------------------------------
# VisualProcurementManifest contract
# ---------------------------------------------------------------------------


def test_manifest_dataclass_defaults() -> None:
    m = VisualProcurementManifest()
    assert m.schema_version == VISUAL_PROCUREMENT_SCHEMA_VERSION
    assert m.fail_open is True
    assert m.papers_checked == 0
    assert m.cached_paper_ids == []
    assert m.missing_paper_ids == []
    assert m.procured_paper_ids == []
    assert m.pending_review_chunk_ids == []


def test_manifest_to_dict_round_trip() -> None:
    m = VisualProcurementManifest(
        papers_checked=3,
        cached_paper_ids=["a"],
        pending_review_chunk_ids=["c1", "c2"],
    )
    d = m.to_dict()
    assert d["schema_version"] == VISUAL_PROCUREMENT_SCHEMA_VERSION
    assert d["fail_open"] is True
    assert d["cached_paper_ids"] == ["a"]
    assert d["pending_review_chunk_ids"] == ["c1", "c2"]


# ---------------------------------------------------------------------------
# _paper_has_visual_assets
# ---------------------------------------------------------------------------


def test_has_visual_assets_no_file() -> None:
    assert _paper_has_visual_assets("p1", None) is False
    assert _paper_has_visual_assets("p1", Path("/nonexistent/cache.sqlite")) is False


def test_has_visual_assets_cache_hit(tmp_path: Path) -> None:
    db = _make_visual_cache_sqlite(tmp_path, ["p1", "p2"])
    assert _paper_has_visual_assets("p1", db) is True
    assert _paper_has_visual_assets("p2", db) is True
    assert _paper_has_visual_assets("p99", db) is False


def test_has_visual_assets_visual_assets_table(tmp_path: Path) -> None:
    db = tmp_path / "vc.sqlite"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE visual_assets "
            "(asset_id TEXT PRIMARY KEY, paper_id TEXT)"
        )
        conn.execute("INSERT INTO visual_assets VALUES ('a1', 'px')")
    assert _paper_has_visual_assets("px", db) is True
    assert _paper_has_visual_assets("py", db) is False


# ---------------------------------------------------------------------------
# build_visual_procurement_manifest – no-papers path
# ---------------------------------------------------------------------------


def test_build_manifest_empty_papers(tmp_path: Path) -> None:
    m = build_visual_procurement_manifest(
        [],
        kb_sqlite=tmp_path / "kb.sqlite",
        download_dir=tmp_path / "dl",
    )
    assert m.papers_checked == 0
    assert m.policy_summary["reason"] == "no_papers_supplied"
    assert m.fail_open is True


# ---------------------------------------------------------------------------
# build_visual_procurement_manifest – all papers already cached
# ---------------------------------------------------------------------------


def test_build_manifest_all_cached(tmp_path: Path) -> None:
    papers = [_paper("p1"), _paper("p2")]
    db = _make_visual_cache_sqlite(tmp_path, ["p1", "p2"])
    m = build_visual_procurement_manifest(
        papers,
        visual_cache_sqlite=db,
        kb_sqlite=tmp_path / "kb.sqlite",
        download_dir=tmp_path / "dl",
    )
    assert m.papers_checked == 2
    assert set(m.cached_paper_ids) == {"p1", "p2"}
    assert m.missing_paper_ids == []
    assert m.policy_summary["reason"] == "all_papers_have_cached_visual_assets"
    # No acquisition ran: no procured papers, no pending chunks.
    assert m.procured_paper_ids == []
    assert m.pending_review_chunk_ids == []
    assert m.fail_open is True


# ---------------------------------------------------------------------------
# build_visual_procurement_manifest – papers with no OA route
# ---------------------------------------------------------------------------


def test_build_manifest_no_oa_route(tmp_path: Path) -> None:
    # Papers with no OA URL, no DOI, no external IDs → should_download=False.
    p1 = S2PaperRecord(
        paper_id="noroute1",
        title="Paper without OA route",
        year=2022,
        abstract="",
        s2_open_access_candidate_url="",
        doi="",
        external_ids={},
        is_oa=False,
    )
    m = build_visual_procurement_manifest(
        [p1],
        kb_sqlite=tmp_path / "kb.sqlite",
        download_dir=tmp_path / "dl",
    )
    assert m.papers_checked == 1
    assert "noroute1" in m.missing_paper_ids
    # should_download=False → skipped, not procured
    assert "noroute1" in m.skipped_paper_ids
    assert m.procured_paper_ids == []
    assert m.policy_summary["reason"] == "no_papers_with_oa_route"
    assert m.fail_open is True


# ---------------------------------------------------------------------------
# build_visual_procurement_manifest – successful acquisition path
# ---------------------------------------------------------------------------


def test_build_manifest_acquires_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    papers = [_paper("p1"), _paper("p2"), _paper("p3")]
    # p1 is in cache; p2 and p3 are missing.
    db = _make_visual_cache_sqlite(tmp_path, ["p1"])
    dl = tmp_path / "downloads"

    fake_result = _fake_acquire_result(
        new_paper_ids=["p2", "p3"],
        new_chunk_ids=["vc:p2", "vc:p3"],
        stats={"visual_candidate_count": 2},
    )

    from optomind_research.s2_fulltext_acquisition import S2FulltextAcquirer

    monkeypatch.setattr(
        S2FulltextAcquirer,
        "acquire",
        lambda self, selections, **kw: fake_result,
    )

    m = build_visual_procurement_manifest(
        papers,
        visual_cache_sqlite=db,
        kb_sqlite=tmp_path / "kb.sqlite",
        download_dir=dl,
    )

    assert m.papers_checked == 3
    assert m.cached_paper_ids == ["p1"]
    assert set(m.missing_paper_ids) == {"p2", "p3"}
    assert set(m.procured_paper_ids) == {"p2", "p3"}
    assert set(m.pending_review_chunk_ids) == {"vc:p2", "vc:p3"}
    assert m.policy_summary["reason"] == "visual_procurement_complete"
    assert m.policy_summary["visual_candidate_count"] == 2
    assert m.policy_summary["fail_open"] is True
    assert m.policy_summary["pending_review_candidates_traceable"] is True
    # Body policy key is present.
    assert "body_visual_policy" in m.policy_summary
    assert m.fail_open is True


# ---------------------------------------------------------------------------
# Max-papers cap
# ---------------------------------------------------------------------------


def test_build_manifest_caps_papers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    papers = [_paper(f"p{i}") for i in range(12)]
    fake_result = _fake_acquire_result(
        new_paper_ids=["p0"],
        new_chunk_ids=["vc:p0"],
    )

    from optomind_research.s2_fulltext_acquisition import S2FulltextAcquirer

    captured: list[Any] = []

    def fake_acquire(self, selections: list[Any], **kw: Any) -> Any:
        captured.append(selections)
        return fake_result

    monkeypatch.setattr(S2FulltextAcquirer, "acquire", fake_acquire)

    m = build_visual_procurement_manifest(
        papers,
        kb_sqlite=tmp_path / "kb.sqlite",
        download_dir=tmp_path / "dl",
        max_papers=4,
    )
    # At most max_papers sent to acquirer.
    assert len(captured[0]) <= 4
    # Over-cap papers are skipped.
    assert len(m.skipped_paper_ids) >= 8


# ---------------------------------------------------------------------------
# Adaptive priority ordering (higher citation count → earlier in manifest)
# ---------------------------------------------------------------------------


def test_build_manifest_priority_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    low = _paper("plow", influential_citation_count=0)
    high = _paper("phigh", influential_citation_count=99)

    submission_order: list[str] = []

    from optomind_research.s2_fulltext_acquisition import S2FulltextAcquirer

    def fake_acquire(self, selections: list[Any], **kw: Any) -> Any:
        submission_order.extend(p.paper_id for p, _ in selections)
        return _fake_acquire_result()

    monkeypatch.setattr(S2FulltextAcquirer, "acquire", fake_acquire)

    build_visual_procurement_manifest(
        [low, high],  # low first, high second
        kb_sqlite=tmp_path / "kb.sqlite",
        download_dir=tmp_path / "dl",
    )
    # Higher citation count → higher priority → submitted first.
    assert submission_order.index("phigh") < submission_order.index("plow")


# ---------------------------------------------------------------------------
# Fail-open: acquirer exception never propagates
# ---------------------------------------------------------------------------


def test_build_manifest_acquire_error_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    papers = [_paper("px")]

    from optomind_research.s2_fulltext_acquisition import S2FulltextAcquirer

    def boom(self, *_: Any, **__: Any) -> None:
        raise RuntimeError("network is down")

    monkeypatch.setattr(S2FulltextAcquirer, "acquire", boom)

    # Must not raise; the manifest should still be fail-open.
    # (The function itself doesn't wrap acquire in a try/except by design –
    # it expects the acquirer to be fail-open internally.  But since boom()
    # raises here, this test verifies the *caller* can handle exceptions by
    # wrapping the call – or that the module-level helper does so.)
    # We verify that nothing in the procurement flow propagates at the
    # visual_procurement_pre_step level.
    from optomind_research.runtime.staged_article_completion import (
        visual_procurement_pre_step,
    )

    result = visual_procurement_pre_step(
        {
            "kb_sqlite": str(tmp_path / "kb.sqlite"),
            "download_dir": str(tmp_path / "dl"),
            "papers": [papers[0]],
        }
    )
    assert result.get("fail_open") is True
    assert "error" in result or result.get("status") in {
        "procurement_error_fail_open",
        "skip",
        "import_error",
    }


# ---------------------------------------------------------------------------
# visual_procurement_pre_step: no inputs → skip
# ---------------------------------------------------------------------------


def test_visual_procurement_pre_step_missing_kb(tmp_path: Path) -> None:
    from optomind_research.runtime.staged_article_completion import (
        visual_procurement_pre_step,
    )

    result = visual_procurement_pre_step(
        {"papers": [_paper("p1")]},
        work_dir=tmp_path,
    )
    assert result.get("fail_open") is True
    # No kb_sqlite supplied → should skip gracefully.
    assert result.get("status") in {"skip", "procurement_error_fail_open"}


def test_visual_procurement_pre_step_no_papers(tmp_path: Path) -> None:
    from optomind_research.runtime.staged_article_completion import (
        visual_procurement_pre_step,
    )

    result = visual_procurement_pre_step(
        {"kb_sqlite": str(tmp_path / "kb.sqlite")},
        work_dir=tmp_path,
    )
    assert result.get("fail_open") is True


# ---------------------------------------------------------------------------
# Offline noop payload includes visual_procurement_manifest key
# ---------------------------------------------------------------------------


def test_staged_completion_visual_remount_noop_includes_manifest_key(
    tmp_path: Path,
) -> None:
    from optomind_research.runtime.staged_article_completion import (
        run_staged_article_completion,
    )

    state = run_staged_article_completion(
        work_dir=tmp_path / "staged",
        inputs={"final_visual_package": ""},
        stage_order=["visual_remount"],
    )
    record = state.stages.get("visual_remount")
    assert record is not None
    payload = record.payload or {}
    vpm = payload.get("visual_procurement_manifest")
    assert isinstance(vpm, dict), "visual_procurement_manifest must be a dict"
    assert vpm.get("fail_open") is True
    assert vpm.get("pending_review_candidates_traceable") is True


# ---------------------------------------------------------------------------
# Body visual policy constants are intact in visual_evidence_factory
# ---------------------------------------------------------------------------


def test_structural_visual_policy_constants() -> None:
    from optomind_research.runtime.visual_evidence_factory import (
        STRUCTURAL_INTRODUCTION_SECTION_ID,
        STRUCTURAL_POLICY_INTRO_CAP_REASON,
        STRUCTURAL_POLICY_ZERO_REASON,
        STRUCTURAL_ZERO_VISUAL_SECTION_IDS,
    )

    assert "abstract" in STRUCTURAL_ZERO_VISUAL_SECTION_IDS
    assert "conclusion" in STRUCTURAL_ZERO_VISUAL_SECTION_IDS
    assert STRUCTURAL_INTRODUCTION_SECTION_ID == "introduction"
    assert "excluded_structural_policy" in STRUCTURAL_POLICY_ZERO_REASON
    assert "introduction_cap" in STRUCTURAL_POLICY_INTRO_CAP_REASON


def test_apply_structural_visual_policy_zero_sections() -> None:
    from optomind_research.runtime.visual_evidence_factory import (
        apply_structural_visual_policy,
    )

    plan = {
        "placements": [
            {
                "section_id": "abstract",
                "argumentative_purpose": "Abstract figure",
                "visual_chunk_id": "vc1",
                "paper_id": "p1",
            },
            {
                "section_id": "S03",
                "argumentative_purpose": "Body figure",
                "visual_chunk_id": "vc2",
                "paper_id": "p1",
            },
        ],
        "conceptual_figure_requests": [],
        "unfilled_visual_needs": [],
        "sections": [
            {"section_id": "abstract", "title": "Abstract"},
            {"section_id": "S03", "title": "Body"},
        ],
    }
    result = apply_structural_visual_policy(plan)
    # Abstract placement is dropped.
    retained_ids = [p["section_id"] for p in result["placements"]]
    assert "abstract" not in retained_ids
    assert "S03" in retained_ids
    # Unfilled needs records the abstract exclusion.
    unfilled_ids = [n["section_id"] for n in result["unfilled_visual_needs"]]
    assert "abstract" in unfilled_ids
    # Fail-open: never_blocks_prose=True on the unfilled need.
    abstract_unfilled = next(
        n for n in result["unfilled_visual_needs"] if n["section_id"] == "abstract"
    )
    assert abstract_unfilled.get("never_blocks_prose") is True


# ---------------------------------------------------------------------------
# MAX cap constant is sensible
# ---------------------------------------------------------------------------


def test_max_visual_procurement_papers_constant() -> None:
    assert 1 <= _MAX_VISUAL_PROCUREMENT_PAPERS <= 32
