"""Offline controls for the managed default planning material index."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import planning_material_search as search_module
from optomind_research.runtime.upgrade3 import planning_supplement
from optomind_research.runtime.upgrade3 import progressive_review_plan as planner


def _config(tmp_path: Path) -> planner.ProgressivePlannerConfig:
    return planner.ProgressivePlannerConfig(
        topic_id="default-index-test",
        pool_path=tmp_path / "pool.jsonl",
        plan_path=tmp_path / "plan.json",
        output_dir=tmp_path / "run",
    )


def _row(tmp_path: Path, paper_id: str = "paper-a", *, with_card: bool = True,
         supplement: bool = False) -> dict:
    row = {
        "paper_id": paper_id,
        "_paper_id": paper_id,
        "_source_handle": "P0001",
        "planning_view": {
            "paper_identity": {"canonical_paper_id": paper_id, "title": paper_id, "year": "2026"},
            "declared_content_depth": "fulltext",
        },
        "_b_summary": {"declared_content_depth": "fulltext"},
    }
    if with_card:
        card = tmp_path / f"{paper_id}.json"
        card.write_text(json.dumps({
            "paper_identity": {"canonical_paper_id": paper_id, "title": paper_id},
            "material": {"declared_content_depth": "fulltext"},
            "review_planning": {"planning_summary": f"CARD_SIGNAL_{paper_id}"},
        }), encoding="utf-8")
        row["card_path"] = str(card)
    if supplement:
        row["supplement_gap_materials"] = [{
            "gap_id": "GAP_SUPPORT",
            "material_usefulness": "supporting",
            "useful_material": "UNIQUE_SUPPORTING_REVIEW_SIGNAL",
            "outline_action": "Keep as supporting review context.",
        }]
    return row


def test_default_index_is_built_before_local_triage(tmp_path, monkeypatch):
    row = _row(tmp_path)
    captured = []

    def fake_triage(gap, **kwargs):
        captured.append(kwargs["index_path"])
        assert Path(kwargs["index_path"]).is_file()
        return {"decision": "external_research", "usable_content": "", "still_missing": "unresolved"}

    monkeypatch.setattr(planning_supplement, "run_gap_local_triage", fake_triage)
    config = _config(tmp_path)
    runner = planner.make_retrieval_loop_runner(
        config,
        key_file=tmp_path / "missing.key",
        budget_ledger_path=tmp_path / "budget.sqlite",
        budget_limit_cny=1,
        allow_external=False,
    )
    runner(phase="level1", supplement_requests=[{"gap_id": "G", "gap_question": "CARD_SIGNAL_paper-a"}],
           pool_rows=[row])

    assert captured == [config.output_dir / "planning_material_index.sqlite"]
    with search_module.PlanningMaterialIndex(captured[0], readonly=True) as index:
        assert index.counts()["papers"] == 1


def test_saved_supporting_review_material_is_indexed_without_a_card(tmp_path):
    config = _config(tmp_path)
    path = planner._ensure_default_planning_material_index(
        config, [_row(tmp_path, with_card=False, supplement=True)]
    )

    with search_module.PlanningMaterialIndex(path, readonly=True) as index:
        result = search_module.search(index, "UNIQUE_SUPPORTING_REVIEW_SIGNAL")
        assert result.found
        assert result.hits[0].paper_id == "paper-a"
        assert result.hits[0].segment_kind == "card_review_use"
        assert index.paper("paper-a")["snapshot_path"] == ""
        assert index.paper("paper-a")["directed_reading_path"] == ""


def test_default_index_binds_pool_declared_snapshot_and_directed_file(tmp_path):
    snapshot_root = tmp_path / "snapshots" / "paper-a"
    old_snapshot = snapshot_root / "snapshot-000-old"
    new_snapshot = snapshot_root / "snapshot-999-new"
    for path, marker in ((old_snapshot, "EXACT_SNAPSHOT_OLD"), (new_snapshot, "EXACT_SNAPSHOT_NEW")):
        path.mkdir(parents=True)
        (path / "READING_VIEW.md").write_text(
            f"{marker} is the selected paper material with enough prose for indexing and retrieval.",
            encoding="utf-8",
        )
        (path / "manifest.json").write_text(json.dumps({"snapshot_id": path.name}), encoding="utf-8")
    directed_root = tmp_path / "directed" / "paper-a"
    directed_root.mkdir(parents=True)
    old_directed = directed_root / "old-DIRECTED_READING.json"
    new_directed = directed_root / "DIRECTED_READING.json"
    for path, marker in ((old_directed, "EXACT_DIRECTED_OLD"), (new_directed, "EXACT_DIRECTED_NEW")):
        path.write_text(json.dumps({"question_material": [{
            "question": f"Which setting contains {marker}?",
            "answer": f"The direct answer reports {marker} with its stated conditions and method.",
        }]}), encoding="utf-8")
    row = _row(tmp_path)
    row["snapshot_dir"] = str(new_snapshot)
    row["directed_reading_path"] = str(new_directed)
    path = planner._ensure_default_planning_material_index(_config(tmp_path), [row])

    with search_module.PlanningMaterialIndex(path, readonly=True) as index:
        assert search_module.search(index, "EXACT_SNAPSHOT_NEW").found
        assert all("EXACT_SNAPSHOT_OLD" not in hit.text for hit in search_module.search(index, "EXACT_SNAPSHOT_OLD").hits)
        assert search_module.search(index, "EXACT_DIRECTED_NEW").found
        assert all("EXACT_DIRECTED_OLD" not in hit.text for hit in search_module.search(index, "EXACT_DIRECTED_OLD").hits)
        paper = next(item for item in index.papers() if item["paper_id"] == "paper-a")
        assert paper["snapshot_path"] == str(new_snapshot)
        assert paper["directed_reading_path"] == str(new_directed)


def test_supplement_overlay_uses_safe_path_and_quarantines_mismatched_card(tmp_path):
    row = _row(tmp_path, paper_id="doi:foo/bar", with_card=False, supplement=True)
    wrong_card = tmp_path / "wrong-card.json"
    wrong_card.write_text(json.dumps({
        "paper_identity": {"canonical_paper_id": "different-paper"},
        "review_planning": {"planning_summary": "WRONG_CARD_MUST_NOT_BE_INDEXED"},
    }), encoding="utf-8")
    row["card_path"] = str(wrong_card)
    config = _config(tmp_path)
    path = planner._ensure_default_planning_material_index(config, [row])
    cards = list((config.output_dir / "planning_material_index_sources").rglob("cards/*.json"))
    assert cards and all(card.stem != "doi:foo/bar" for card in cards)
    with search_module.PlanningMaterialIndex(path, readonly=True) as index:
        assert search_module.search(index, "UNIQUE_SUPPORTING_REVIEW_SIGNAL").found
        assert not search_module.search(index, "WRONG_CARD_MUST_NOT_BE_INDEXED").found


def test_default_refresh_archives_old_index_and_explicit_index_is_untouched(tmp_path):
    config = _config(tmp_path)
    old_path = planner._ensure_default_planning_material_index(config, [_row(tmp_path)])
    explicit = tmp_path / "user.sqlite"
    with search_module.PlanningMaterialIndex(explicit) as index:
        index.upsert_paper(search_module.PaperRecord("user", "U1", "user", "2026", ""))
        index.add_segments("user", [{"segment_kind": "user", "ordinal": 0, "text": "USER_ONLY"}])
        index.commit()
    before = explicit.read_bytes()

    new_path = planner._ensure_default_planning_material_index(
        config, [_row(tmp_path, paper_id="paper-b")]
    )
    assert new_path == old_path
    with search_module.PlanningMaterialIndex(new_path, readonly=True) as index:
        assert [row["paper_id"] for row in index.papers()] == ["paper-b"]
    assert list((config.output_dir / "planning_material_index_history").glob("*.sqlite"))

    assert planner._ensure_default_planning_material_index(
        config, [_row(tmp_path)], index_path=explicit
    ) == explicit
    assert explicit.read_bytes() == before


def test_default_index_build_failure_is_saved_and_not_converted_to_external(tmp_path, monkeypatch):
    config = _config(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("offline build failure")

    monkeypatch.setattr(search_module, "build_index", fail)
    with pytest.raises(planner.ProgressivePlanError, match="default_planning_material_index_failed"):
        planner._ensure_default_planning_material_index(config, [_row(tmp_path)])

    failure = config.output_dir / "planning_material_index.sqlite.FAILURE.json"
    payload = json.loads(failure.read_text(encoding="utf-8"))
    assert payload["error_type"] == "RuntimeError"
    assert "offline build failure" in payload["error"]
    assert not (config.output_dir / "planning_material_index.sqlite").exists()
