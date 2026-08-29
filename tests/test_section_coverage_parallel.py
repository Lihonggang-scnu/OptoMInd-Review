"""Focused offline tests for bounded section-coverage fanout and merge."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import optomind_research.runtime.section_coverage_orchestrator as sco


def _write_blueprint(tmp_path: Path, section_ids: list[str]) -> Path:
    sections = []
    for index, section_id in enumerate(section_ids, 1):
        sections.append(
            {
                "section_id": section_id,
                "title": f"Parallel section {index}",
                "chapter_argument": "Establish the conceptual basis.",
                "scope_description": "Section-specific physics.",
                "required_roles": ["foundation"],
                "optional_roles": [],
                "section_role": "introduction",
                "target_word_range": {"min": 500, "max": 700},
                "visual_asset_required": False,
            }
        )
    path = tmp_path / "blueprint.json"
    path.write_text(
        json.dumps(
            {
                "topic_identity": {
                    "valid": False,
                    "scientific_object": "parallel section coverage",
                },
                "sections": sections,
            }
        ),
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path, blueprint: Path, **overrides: object):
    return sco.SectionCoverageOrchestratorConfig(
        blueprint_path=blueprint,
        base_kb_sqlite=None,
        output_root=tmp_path / "coverage",
        stage_cost_budget_cny=10.0,
        cost_budget_per_section_cny=2.0,
        max_materialized_papers_per_section=1,
        max_results_per_backend=2,
        short_path_mode=True,
        article_evidence_portfolio_path=(
            tmp_path / "coverage" / "ARTICLE_EVIDENCE_PORTFOLIO.json"
        ),
        global_coverage_ledger_path=(
            tmp_path / "coverage" / "COVERAGE_GLOBAL_LEDGER.json"
        ),
        cross_wave_state_path=(
            tmp_path / "coverage" / "COVERAGE_CROSS_WAVE_STATE.json"
        ),
        **overrides,
    )


def _write_worker_artifacts(
    orchestrator: sco.SectionCoverageOrchestrator,
    section_id: str,
) -> None:
    section_work_dir = orchestrator.work_dir / "sections" / section_id
    section_work_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(orchestrator.staging_kb))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers(
            paper_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            doi TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS text_chunks(
            chunk_id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL DEFAULT '',
            ordinal INTEGER,
            text TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.execute(
        "INSERT INTO papers VALUES(?,?,?)",
        (f"paper:{section_id}", f"Paper {section_id}", f"10.1/{section_id}"),
    )
    conn.execute(
        "INSERT INTO text_chunks VALUES(?,?,?,?)",
        (f"chunk:{section_id}", f"paper:{section_id}", 0, f"text {section_id}"),
    )
    conn.commit()
    conn.close()

    portfolio = {
        "schema_version": "phase2.article_evidence_portfolio.v1",
        "topic_fingerprint": "",
        "candidates": [
            {
                "material_identity": f"identity:{section_id}",
                "title": f"Candidate {section_id}",
                "decision": "approved",
                "scope_fit": "direct",
                "source_sections": [section_id],
                "roles": ["foundation"],
            }
        ],
        "audits": {
            f"identity:{section_id}": {
                "material_identity": f"identity:{section_id}",
                "decision": "approved",
                "scope_fit": "direct",
                "role_fit": ["foundation"],
                "source_sections": [section_id],
            }
        },
        "materials": {
            f"identity:{section_id}": {
                "material_identity": f"identity:{section_id}",
                "paper_id": f"paper:{section_id}",
                "chunk_ids": [f"chunk:{section_id}"],
                "source_sections": [section_id],
            }
        },
        "section_links": {},
        "telemetry": {
            "candidate_upserts": 1,
            "audit_reuse_hits": 0,
            "material_reuse_hits": 0,
        },
    }
    orchestrator.article_evidence_portfolio_path.write_text(
        json.dumps(portfolio, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    ledger = {
        "schema_version": "research_harness.coverage_ledger.v1",
        "queries": {
            f"q:{section_id}": {
                "topic_fingerprint": "",
                "role": "foundation",
                "query": f"query {section_id}",
                "candidates": [],
                "search_count": 1,
                "last_status": "completed",
            }
        },
        "audits": {},
        "materials": {},
        "stats": {"query_cache_writes": 1},
    }
    orchestrator.config.global_coverage_ledger_path.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    (section_work_dir / "SECTION_MATERIAL_PACKAGE.json").write_text(
        json.dumps(
            {
                "section_id": section_id,
                "status": "completed",
                "materialized_paper_ids": [f"paper:{section_id}"],
            }
        ),
        encoding="utf-8",
    )
    (section_work_dir / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps({"section_id": section_id, "sources": []}),
        encoding="utf-8",
    )


def _install_fake_run_one(
    monkeypatch,
    *,
    fail_section: str | None = None,
    resume: bool = False,
):
    """Install a deterministic fake _run_one with concurrency instrumentation."""

    state = {
        "active": 0,
        "max_active": 0,
        "lock": threading.Lock(),
        "started": threading.Event(),
        "calls": [],
    }

    def fake_run_one(
        self: sco.SectionCoverageOrchestrator,
        section: dict,
        *,
        remaining_stage_budget: float,
    ):
        section_id = str(section["section_id"])
        with state["lock"]:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            if state["active"] >= 2:
                state["started"].set()
        try:
            state["started"].wait(timeout=5)
            state["calls"].append(section_id)
            if section_id == fail_section:
                raise RuntimeError(f"boom:{section_id}")
            resume_path = (
                self.work_dir / "sections" / section_id / "RESULT.json"
            )
            if resume and resume_path.exists():
                record = {
                    "section_id": section_id,
                    "status": "completed",
                    "stop_reason": "reused_validated_package",
                    "stop_reason_category": "scientific_completion",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_cny": 0.0,
                    "work_dir": str(
                        self.work_dir / "sections" / section_id
                    ),
                    "reused": True,
                }
                return record, None
            _write_worker_artifacts(self, section_id)
            resume_path.write_text(
                json.dumps({"section_id": section_id, "status": "completed"}),
                encoding="utf-8",
            )
            bundle = sco.SectionMaterialBundle(
                material_package_path=(
                    self.work_dir
                    / "sections"
                    / section_id
                    / "SECTION_MATERIAL_PACKAGE.json"
                ),
                source_ledger_path=(
                    self.work_dir
                    / "sections"
                    / section_id
                    / "SECTION_SOURCE_LEDGER.json"
                ),
                kb_sqlite=None,
                staging_kb_sqlite=(
                    self.staging_kb if self.staging_kb.exists() else None
                ),
            )
            record = {
                "section_id": section_id,
                "status": "completed",
                "stop_reason": "deterministic_short_path_complete",
                "stop_reason_category": "scientific_completion",
                "input_tokens": 1,
                "output_tokens": 1,
                "cost_cny": 0.1,
                "work_dir": str(
                    self.work_dir / "sections" / section_id
                ),
                "reused": False,
                "coverage_budget": self._coverage_budget_metadata(),
            }
            return record, bundle
        finally:
            with state["lock"]:
                state["active"] -= 1

    monkeypatch.setattr(sco.SectionCoverageOrchestrator, "_run_one", fake_run_one)
    return state


def _read_manifest(work_dir: Path) -> dict:
    return json.loads(
        (work_dir / "SECTION_COVERAGE_RUN.json").read_text(encoding="utf-8")
    )


def test_section_workers_fanout_and_merge_deterministically(
    monkeypatch, tmp_path: Path
) -> None:
    blueprint = _write_blueprint(tmp_path, ["S01", "S02", "S03"])
    state = _install_fake_run_one(monkeypatch)
    merge_calls: list[tuple[str, str]] = []
    main_thread = threading.get_ident()
    real_merge = sco.merge_kb_sqlite_into

    def fake_merge(target, source):
        assert threading.get_ident() == main_thread
        merge_calls.append((str(Path(target)), str(Path(source))))
        real_merge(target, source)

    monkeypatch.setattr(sco, "merge_kb_sqlite_into", fake_merge)
    orchestrator = sco.SectionCoverageOrchestrator(
        _config(tmp_path, blueprint, max_section_workers=3)
    )
    result = orchestrator.run()

    assert state["max_active"] >= 2
    assert state["max_active"] <= 3
    assert [record["section_id"] for record in orchestrator.records] == [
        "S01",
        "S02",
        "S03",
    ]
    manifest = _read_manifest(orchestrator.work_dir)
    assert [record["section_id"] for record in manifest["sections"]] == [
        "S01",
        "S02",
        "S03",
    ]
    assert len(merge_calls) == 3
    assert all(str(Path(target)) == str(orchestrator.staging_kb) for target, _ in merge_calls)
    assert [Path(source).parent.name for _, source in merge_calls] == [
        "S01",
        "S02",
        "S03",
    ]
    with sqlite3.connect(str(orchestrator.staging_kb)) as conn:
        papers = sorted(
            row[0]
            for row in conn.execute(
                "SELECT paper_id FROM papers ORDER BY paper_id"
            ).fetchall()
        )
        chunks = sorted(
            row[0]
            for row in conn.execute(
                "SELECT chunk_id FROM text_chunks ORDER BY chunk_id"
            ).fetchall()
        )
    assert papers == ["paper:S01", "paper:S02", "paper:S03"]
    assert chunks == ["chunk:S01", "chunk:S02", "chunk:S03"]
    shared_portfolio = json.loads(
        orchestrator.article_evidence_portfolio_path.read_text(
            encoding="utf-8"
        )
    )
    assert {
        row["material_identity"]
        for row in shared_portfolio["candidates"]
    } == {"identity:S01", "identity:S02", "identity:S03"}
    shared_ledger = json.loads(
        orchestrator.config.global_coverage_ledger_path.read_text(
            encoding="utf-8"
        )
    )
    assert set(shared_ledger["queries"]) == {"q:S01", "q:S02", "q:S03"}
    assert shared_ledger["stats"]["query_cache_writes"] == 3
    assert all(
        str(bundle.staging_kb_sqlite) == str(orchestrator.staging_kb)
        for bundle in result.material_bundles.values()
    )


def test_worker_failure_is_fail_open_and_record_order_is_deterministic(
    monkeypatch, tmp_path: Path
) -> None:
    blueprint = _write_blueprint(tmp_path, ["S01", "S02", "S03"])
    state = _install_fake_run_one(monkeypatch, fail_section="S02")

    def fake_merge(_target, _source):
        return None

    monkeypatch.setattr(sco, "merge_kb_sqlite_into", fake_merge)
    orchestrator = sco.SectionCoverageOrchestrator(
        _config(tmp_path, blueprint, max_section_workers=3)
    )
    result = orchestrator.run()

    assert [record["section_id"] for record in orchestrator.records] == [
        "S01",
        "S02",
        "S03",
    ]
    assert orchestrator.records[1]["status"] == "failed"
    assert "parallel_worker_exception" in orchestrator.records[1]["stop_reason"]
    assert orchestrator.records[0]["status"] == "completed"
    assert orchestrator.records[2]["status"] == "completed"
    assert result.status == "partial"
    assert result.sections_completed == 2
    assert result.sections_failed == 1
    assert "S02" not in result.material_bundles
    assert set(result.material_bundles) == {"S01", "S03"}


def test_parallel_resume_reuses_section_artifacts_without_duplication(
    monkeypatch, tmp_path: Path
) -> None:
    blueprint = _write_blueprint(tmp_path, ["S01", "S02", "S03"])
    state = _install_fake_run_one(monkeypatch, resume=True)
    merge_calls: list[tuple[str, str]] = []
    real_merge = sco.merge_kb_sqlite_into

    def fake_merge(target, source):
        merge_calls.append((str(Path(target)), str(Path(source))))
        real_merge(target, source)

    monkeypatch.setattr(sco, "merge_kb_sqlite_into", fake_merge)
    config = _config(tmp_path, blueprint, max_section_workers=3)
    orchestrator = sco.SectionCoverageOrchestrator(
        config, run_dir=tmp_path / "coverage" / "run"
    )
    first = orchestrator.run()
    second = orchestrator.run()

    assert first.sections_completed == 3
    assert second.sections_completed == 3
    assert all(record["reused"] for record in orchestrator.records)
    manifest = _read_manifest(orchestrator.work_dir)
    assert len(manifest["sections"]) == 3
    with sqlite3.connect(str(orchestrator.staging_kb)) as conn:
        papers = conn.execute(
            "SELECT paper_id FROM papers ORDER BY paper_id"
        ).fetchall()
    assert len(papers) == 3
    # The resume run reuses validated packages, but the persisted worker
    # staging SQLite artifacts are re-merged idempotently so a crash between
    # worker completion and the shared merge cannot lose material.
    assert len(merge_calls) == 6
    assert sorted(state["calls"]) == [
        "S01",
        "S01",
        "S02",
        "S02",
        "S03",
        "S03",
    ]


def test_single_section_worker_mode_is_serial(
    monkeypatch, tmp_path: Path
) -> None:
    blueprint = _write_blueprint(tmp_path, ["S01", "S02", "S03"])
    state = _install_fake_run_one(monkeypatch)

    def fake_merge(_target, _source):
        return None

    monkeypatch.setattr(sco, "merge_kb_sqlite_into", fake_merge)
    orchestrator = sco.SectionCoverageOrchestrator(
        _config(tmp_path, blueprint, max_section_workers=1)
    )
    result = orchestrator.run()

    assert state["max_active"] == 1
    assert state["calls"] == ["S01", "S02", "S03"]
    assert [record["section_id"] for record in orchestrator.records] == [
        "S01",
        "S02",
        "S03",
    ]


def test_section_worker_env_var_controls_fanout(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPTOMIND_SECTION_COVERAGE_WORKERS", "2")
    blueprint = _write_blueprint(tmp_path, ["S01", "S02", "S03"])
    state = _install_fake_run_one(monkeypatch)

    def fake_merge(_target, _source):
        return None

    monkeypatch.setattr(sco, "merge_kb_sqlite_into", fake_merge)
    orchestrator = sco.SectionCoverageOrchestrator(_config(tmp_path, blueprint))
    result = orchestrator.run()
    assert result.sections_completed == 3
    assert 2 <= state["max_active"] <= 2


def test_parallel_manifest_records_cumulative_cost_accounting(
    monkeypatch, tmp_path: Path
) -> None:
    blueprint = _write_blueprint(tmp_path, ["S01", "S02", "S03"])
    _install_fake_run_one(monkeypatch)

    def fake_merge(_target, _source):
        return None

    monkeypatch.setattr(sco, "merge_kb_sqlite_into", fake_merge)
    orchestrator = sco.SectionCoverageOrchestrator(
        _config(tmp_path, blueprint, max_section_workers=3)
    )
    result = orchestrator.run()
    assert result.total_cost_cny == 0.3
    assert result.total_input_tokens == 3
    assert result.total_output_tokens == 3
    manifest = _read_manifest(orchestrator.work_dir)
    assert manifest["total_cost_cny"] == 0.3
    assert manifest["total_input_tokens"] == 3
    assert manifest["total_output_tokens"] == 3
