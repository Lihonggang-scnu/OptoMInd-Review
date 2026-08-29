from __future__ import annotations

import sqlite3
from pathlib import Path


def _blueprint(*claims: dict) -> dict:
    return {
        "sections": [
            {
                "section_id": "S01",
                "title": "Test section",
                "argument_role": "Test role",
                "claims": list(claims),
            }
        ]
    }


def _claim(claim_id: str, saturation_score: float, **extra: object) -> dict:
    return {
        "claim_id": claim_id,
        "statement": f"Statement for {claim_id}.",
        "saturation_score": saturation_score,
        "supporting_text_chunk_ids": [],
        **extra,
    }


def _patch_fake_pipeline(monkeypatch, *, readiness_by_claim: dict[str, str] | None = None):
    import optomind_research.m3_real_gap_loop as module

    calls: list[str] = []
    readiness_by_claim = readiness_by_claim or {}

    class FakeExpander:
        def __init__(self, **kwargs):
            pass

        def expand_claim(self, claim, section, **kwargs):
            claim_id = str(claim["claim_id"])
            calls.append(claim_id)
            action = readiness_by_claim.get(claim_id)
            if action:
                claim["_readiness_check"] = {"action": action}
            return {
                "selected_oa_candidates": [{"candidate_id": f"candidate-{claim_id}"}],
                "candidate_stats": {},
                "download_summary": {},
                "citation_chase": {},
                "metadata_updates": {},
            }

    monkeypatch.setattr(module, "GapOAEvidenceExpander", FakeExpander)
    monkeypatch.setattr(
        module,
        "classify_gap",
        lambda **kwargs: {
            "gap_type": "retrievable",
            "retrieval_priority": 10,
            "reasoning": "test gap",
            "action": "retrieve",
        },
    )
    return calls


def _run(module, blueprint: dict, output_dir: Path, **kwargs):
    return module.run_m3_real_gap_loop(
        blueprint,
        output_dir=output_dir,
        max_queries=1,
        results_per_backend=1,
        top_k=1,
        use_openalex=False,
        use_semantic_scholar=False,
        use_unpaywall=False,
        **kwargs,
    )


def test_second_round_stays_on_initial_target(monkeypatch, tmp_path: Path):
    import optomind_research.m3_real_gap_loop as module

    calls = _patch_fake_pipeline(monkeypatch)
    blueprint = _blueprint(
        _claim("S03-C01", 0.2),
        _claim("S02-C02", 0.4),
    )

    _, report = _run(
        module,
        blueprint,
        tmp_path,
        max_rounds=2,
        max_claims=1,
    )

    assert report["target_claim_ids"] == ["S03-C01"]
    assert calls == ["S03-C01", "S03-C01"]
    assert {row["claim_id"] for row in report["round_reports"]} == {"S03-C01"}


def test_proceed_is_not_selected_when_saturation_is_low():
    from optomind_research.m3_real_gap_loop import collect_low_saturation_claims

    blueprint = _blueprint(
        _claim("S01-C01", 0.1, _readiness_check={"action": "proceed"}),
        _claim("S02-C01", 0.2),
    )

    selected = collect_low_saturation_claims(blueprint, threshold=1.5, max_claims=2)

    assert [claim["claim_id"] for _, claim in selected] == ["S02-C01"]


def test_supplement_target_continues_into_next_round(monkeypatch, tmp_path: Path):
    import optomind_research.m3_real_gap_loop as module

    calls = _patch_fake_pipeline(monkeypatch)
    blueprint = _blueprint(_claim("S01-C01", 2.0, _readiness_check={"action": "supplement"}))

    _, report = _run(
        module,
        blueprint,
        tmp_path,
        max_rounds=2,
        max_claims=1,
    )

    assert report["target_claim_ids"] == ["S01-C01"]
    assert calls == ["S01-C01", "S01-C01"]


def test_two_initial_targets_iterate_independently(monkeypatch, tmp_path: Path):
    import optomind_research.m3_real_gap_loop as module

    calls = _patch_fake_pipeline(
        monkeypatch,
        readiness_by_claim={"S01-C01": "proceed", "S02-C01": "supplement"},
    )
    blueprint = _blueprint(
        _claim("S01-C01", 0.1),
        _claim("S02-C01", 0.2),
        _claim("S03-C01", 0.3),
    )

    _, report = _run(
        module,
        blueprint,
        tmp_path,
        max_rounds=2,
        max_claims=2,
    )

    assert report["target_claim_ids"] == ["S01-C01", "S02-C01"]
    assert calls == ["S01-C01", "S02-C01", "S02-C01"]
    assert report["summary"]["stop_reason"] == "max_rounds_reached"


def test_structural_target_does_not_make_room_for_unselected_claim(monkeypatch, tmp_path: Path):
    import optomind_research.m3_real_gap_loop as module

    calls = _patch_fake_pipeline(monkeypatch)
    monkeypatch.setattr(
        module,
        "classify_gap",
        lambda **kwargs: {
            "gap_type": "structural",
            "retrieval_priority": 0,
            "reasoning": "test structural gap",
            "action": "skip",
        },
    )
    blueprint = _blueprint(_claim("S01-C01", 0.1), _claim("S02-C01", 0.2))

    _, report = _run(module, blueprint, tmp_path, max_rounds=2, max_claims=1)

    assert report["target_claim_ids"] == ["S01-C01"]
    assert calls == []
    assert report["round_reports"] == []
    assert report["summary"]["stop_reason"] == "all_initial_targets_completed"


def test_all_targets_completed_has_explicit_stop_reason(monkeypatch, tmp_path: Path):
    import optomind_research.m3_real_gap_loop as module

    _patch_fake_pipeline(
        monkeypatch,
        readiness_by_claim={"S01-C01": "proceed", "S02-C01": "proceed"},
    )
    blueprint = _blueprint(_claim("S01-C01", 0.1), _claim("S02-C01", 0.2))

    _, report = _run(module, blueprint, tmp_path, max_rounds=3, max_claims=2)

    assert report["summary"]["stop_reason"] == "all_initial_targets_completed"


def test_explicit_target_overrides_automatic_priority(monkeypatch, tmp_path: Path):
    import optomind_research.m3_real_gap_loop as module

    calls = _patch_fake_pipeline(monkeypatch)
    blueprint = _blueprint(
        _claim("S01-C01", 0.1),
        _claim("S03-C01", 0.9),
    )

    _, report = _run(
        module,
        blueprint,
        tmp_path,
        max_rounds=1,
        max_claims=1,
        target_claim_ids=["S03-C01"],
    )

    assert report["targeting_mode"] == "explicit"
    assert report["target_claim_ids"] == ["S03-C01"]
    assert calls == ["S03-C01"]


def test_unknown_explicit_target_stops_cleanly(monkeypatch, tmp_path: Path):
    import optomind_research.m3_real_gap_loop as module

    calls = _patch_fake_pipeline(monkeypatch)
    blueprint = _blueprint(_claim("S01-C01", 0.1))

    _, report = _run(
        module,
        blueprint,
        tmp_path,
        max_rounds=1,
        max_claims=1,
        target_claim_ids=["DOES-NOT-EXIST"],
    )

    assert report["targeting_mode"] == "explicit"
    assert report["target_claim_ids"] == []
    assert report["summary"]["stop_reason"] == "no_initial_targets"
    assert calls == []


def test_external_gap_retrieval_excludes_dois_already_supporting_claim(
    monkeypatch, tmp_path: Path
):
    import optomind_research.m3_real_gap_loop as module

    db = tmp_path / "review_knowledge_base.sqlite"
    connection = sqlite3.connect(str(db))
    connection.execute(
        "CREATE TABLE text_chunks(chunk_id TEXT PRIMARY KEY, doi TEXT)"
    )
    connection.execute(
        "INSERT INTO text_chunks(chunk_id, doi) VALUES(?, ?)",
        ("doi-existing:hybrid:s0001", "10.1000/existing"),
    )
    connection.commit()
    connection.close()

    observed: dict = {}

    class FakeExpander:
        def __init__(self, **kwargs):
            pass

        def expand_claim(self, claim, section, **kwargs):
            observed["exclude_dois"] = set(kwargs.get("exclude_dois") or set())
            return {
                "selected_oa_candidates": [],
                "candidate_stats": {},
                "download_summary": {},
                "citation_chase": {},
                "metadata_updates": {},
            }

    monkeypatch.setattr(module, "GapOAEvidenceExpander", FakeExpander)
    monkeypatch.setattr(
        module,
        "classify_gap",
        lambda **kwargs: {
            "gap_type": "direct_retrievable",
            "retrieval_ready": True,
            "retrieval_priority": 10,
            "reasoning": "test gap",
            "action": "retrieve",
        },
    )
    blueprint = _blueprint(
        _claim(
            "S01-C01",
            0.1,
            supporting_text_chunk_ids=["doi-existing:hybrid:s0001"],
        )
    )

    _run(
        module,
        blueprint,
        tmp_path / "run",
        max_rounds=1,
        max_claims=1,
        kb_sqlite=db,
        download_top_n=0,
    )

    assert observed["exclude_dois"] == {"10.1000/existing"}


def test_m3_real_gap_loop_emits_claim_level_progress(monkeypatch, tmp_path: Path):
    import optomind_research.m3_real_gap_loop as module

    _patch_fake_pipeline(monkeypatch)
    events: list[tuple[str, dict]] = []
    blueprint = _blueprint(_claim("S01-C01", 0.1))
    _run(
        module,
        blueprint,
        tmp_path,
        max_rounds=1,
        max_claims=1,
        progress_callback=lambda event, details: events.append((event, details)),
    )
    names = [event for event, _ in events]
    assert "claim_started" in names
    assert "claim_completed" in names
    assert names[-1] == "completed"
    assert (tmp_path / "m3_real_gap_progress.json").exists()
    assert (tmp_path / "m3_real_gap_loop_report.partial.json").exists()
