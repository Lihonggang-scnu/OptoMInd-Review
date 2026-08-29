from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import run_review_harness as harness_cli

from run_review_harness import (
    DEFAULT_M1,
    _base_kb_for_run,
    _resolve_m1_library_path,
    _write_asset_roles,
    build_parser,
)


def test_explicit_m1_directory_resolves_canonical_library(tmp_path: Path) -> None:
    library_dir = tmp_path / "final_canonical"
    library_dir.mkdir()
    library = library_dir / "intellectual_moves_active_by_category.json"
    library.write_text("{}", encoding="utf-8")

    resolved, reason = _resolve_m1_library_path(library_dir)

    assert resolved == library.resolve()
    assert reason == "resolved_from_canonical_directory"


def test_m1_missing_or_unusable_path_stays_disabled(tmp_path: Path) -> None:
    resolved, reason = _resolve_m1_library_path(tmp_path / "missing")
    assert resolved is None
    assert reason == "path_not_found"


def test_cli_starts_without_a_historical_paper_database() -> None:
    parser = build_parser()
    args = parser.parse_args(["--question", "a new optical question"])
    assert args.base_kb is None
    assert args.m1_library == DEFAULT_M1


def test_empty_task_seed_has_schema_but_no_research_records(tmp_path: Path) -> None:
    seed, role = _base_kb_for_run(
        None,
        run_dir=tmp_path / "run",
        allow_historical_test_assets=False,
        materialize_empty_seed=True,
    )
    assert role == "empty_task_seed"
    assert seed.is_file()
    with sqlite3.connect(seed) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0] == 0

    # A resumed run must not silently turn the seed into a cache containing
    # papers.  The helper fails closed if another process contaminated it.
    with sqlite3.connect(seed) as connection:
        connection.execute(
            "INSERT INTO papers(paper_id, raw_json) VALUES (?, ?)",
            ("unexpected-paper", "{}"),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="already contains research material"):
        _base_kb_for_run(
            None,
            run_dir=tmp_path / "run",
            allow_historical_test_assets=False,
            materialize_empty_seed=True,
        )


def test_normal_run_projects_the_central_material_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_root = tmp_path / "central"
    cache_root.mkdir()
    (cache_root / "CURRENT.json").write_text("{}", encoding="utf-8")
    query_plan = tmp_path / "query_plan.json"
    query_plan.write_text("{}", encoding="utf-8")
    calls = []

    def fake_projection(**kwargs):
        calls.append(kwargs)
        from optomind_research.runtime.topic_scoped_kb_stage import (
            create_empty_review_kb,
        )

        create_empty_review_kb(kwargs["output_kb_path"])
        return {"status": "completed"}

    monkeypatch.setattr(harness_cli, "project_to_review_kb", fake_projection)
    projected, role = _base_kb_for_run(
        None,
        run_dir=tmp_path / "run-central",
        allow_historical_test_assets=False,
        materialize_empty_seed=True,
        query_plan_path=query_plan,
        long_term_material_cache_root=cache_root,
    )

    assert projected.is_file()
    assert role == "central_long_term_material_cache_projection"
    assert calls[0]["cache_root"] == cache_root


def test_core58_requires_explicit_historical_opt_in(tmp_path: Path) -> None:
    historical = tmp_path / "core58-copy.sqlite"
    historical.write_bytes(b"placeholder")
    with pytest.raises(ValueError, match="historical first-test"):
        _base_kb_for_run(
            historical,
            run_dir=tmp_path / "run",
            allow_historical_test_assets=False,
            materialize_empty_seed=False,
        )

    resolved, role = _base_kb_for_run(
        historical,
        run_dir=tmp_path / "run",
        allow_historical_test_assets=True,
        materialize_empty_seed=False,
    )
    assert resolved == historical
    assert role == "historical_test_asset"


def test_asset_roles_separate_stable_guidance_from_mutable_material(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    seed = run_dir / "task_material" / "EMPTY_TASK_SEED.sqlite"
    _write_asset_roles(
        run_dir=run_dir,
        base_kb=seed,
        base_role="empty_task_seed",
        mentor_library=DEFAULT_M1,
    )
    payload = json.loads((run_dir / "ASSET_ROLES.json").read_text(encoding="utf-8"))
    assert payload["historical_test_assets"][0]["default_for_new_research"] is False
    guidance = payload["stable_guidance"][0]
    assert guidance["may_supply_scientific_facts"] is False
    assert payload["current_task_material"][0]["role"] == "empty_task_seed"
    assert payload["mutable_run_cache"][0]["role"] == "mutable_run_cache"
