"""WO-03 LOCAL_ONLY: real supplement/card/snapshot files; external boundaries only."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from optomind_research.runtime.upgrade3 import planning_supplement as supplement
from optomind_research.runtime.upgrade3.local_materials import LocalTeiMaterialProvider
from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
from optomind_research.runtime.upgrade3.paper_reading_card import run_paper_reading_card


QUESTION = "Which operating condition changes the synthetic device response?"
MATERIAL = "The synthetic device response doubles at the measured temperature."


def supplement_inputs(root):
    """Write a valid bounded request, plan and pool without mocking loaders."""
    root.mkdir(parents=True, exist_ok=True)
    plan = {
        "schema_version": "research_harness.query_plan.v2",
        "question_en": "How does a synthetic device operate?",
        "research_object": "synthetic device",
        "ambiguity": {"is_ambiguous": False, "default_reading": "", "needs_user_input": []},
        "facets": [{
            "id": "F1", "ask": QUESTION,
            "keyword_queries": ["synthetic device response"],
            "question_queries": [QUESTION],
            "filters": {"publication_type": [], "fields_of_study": [], "text_availability": []},
            "must_exclude": [],
        }],
        "seeds": [], "criteria": {"must_include_topic": [], "must_exclude_domain": [], "synonyms": {}},
        "additional_constraints": [],
    }
    (root / "PLAN.json").write_text(json.dumps(plan), encoding="utf-8")
    (root / "POOL.jsonl").write_text("", encoding="utf-8")
    request = {
        "schema_version": supplement.REQUEST_SCHEMA,
        "request_id": "wo03-f10-f15", "topic_id": "wo03-offline",
        "gap_id": "gap-1", "gap_question": QUESTION,
        "success_criteria": ["Explain the response conditions"],
        "base_pool_path": str(root / "POOL.jsonl"), "plan_path": str(root / "PLAN.json"),
        "known_papers": [{"paper_id": "fixture-paper", "title": "Synthetic device response"}],
        "limits": {"max_candidates": 1, "max_acquisitions": 1, "per_query_limit": 1},
    }
    request_path = root / "REQUEST.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    return request_path


class AcquisitionBoundary:
    """Substitute only acquisition; parse and validate its captured XML normally."""

    def __init__(self, *, fail_first=False):
        self.calls = []
        self.fail_first = fail_first

    def factory(self, output_root):
        def acquire(record):
            self.calls.append(Path(output_root))
            if self.fail_first and len(self.calls) == 1:
                raise TimeoutError("LOCAL_ONLY controlled acquisition outage")
            snapshot = LocalTeiMaterialProvider(output_root).build_from_bytes(
                b'<article><front><article-meta><title-group><article-title>Synthetic device response</article-title></title-group></article-meta></front><body><sec><title>Results</title><p>The synthetic device response doubles at the measured temperature.</p></sec></body></article>',
                canonical_paper_id="fixture-paper",
                metadata={"title": "Synthetic device response"},
                material_depth_override="fulltext",
            )
            return SimpleNamespace(snapshot=snapshot, status="acquired", material_depth="fulltext", attempts=[], known_gaps=[], errors=[])
        return SimpleNamespace(acquire=acquire)


class CardModelBoundary:
    def __init__(self):
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"content": {
            "general_understanding": {
                "paper_kind": "empirical", "research_scope": "Synthetic device temperature response",
                "work_summary": MATERIAL, "problem_or_question": QUESTION,
                "approach": "Measure the device at controlled temperature",
                "key_findings": [{"finding": MATERIAL, "conditions": "One fixed measured temperature"}],
                "contribution_and_limits": [{"contribution": MATERIAL, "limits": "Other temperatures untested"}],
            },
            "review_planning": {
                "planning_summary": MATERIAL, "topic_handles": ["Synthetic device response"],
                "scope_interpretation_cautions": ["LOCAL_ONLY fictional fixture"],
            },
        }}


def _run(request_path, output_dir, acquirer, client, judgment, **card_options):
    return supplement.run_planning_supplement(
        request_path, output_dir=output_dir, gateway=None,
        acquirer_factory=acquirer.factory, card_runner=run_paper_reading_card,
        fulfillment_judge=lambda **kwargs: judgment,
        card_options={"client": client, **card_options},
    )


def test_f10_failed_then_retry_preserves_attempt_files_and_returns_new_pool(tmp_path):
    request = supplement_inputs(tmp_path / "inputs")
    acquisition = AcquisitionBoundary(fail_first=True)
    client = CardModelBoundary()
    logical_root = tmp_path / "supplements" / "gap-1"
    old_dir = supplement.allocate_supplement_attempt(logical_root)
    first = _run(request, old_dir, acquisition, client, {"status": "fulfilled", "useful_material": MATERIAL})
    assert first["outcome"] == "retryable_provider_outage"
    frozen = {p.relative_to(old_dir): p.read_bytes() for p in old_dir.rglob("*") if p.is_file()}

    second_dir = supplement.allocate_supplement_attempt(logical_root)
    second = _run(request, second_dir, acquisition, client, {"status": "fulfilled", "useful_material": MATERIAL})
    assert second["status"] == "fulfilled"
    assert len(acquisition.calls) == 2 and len(client.calls) == 1
    assert first["output_dir"] != second["output_dir"] == str(second_dir)
    assert Path(second["derived_pool_path"]).parent == second_dir
    assert {p.relative_to(old_dir): p.read_bytes() for p in old_dir.rglob("*") if p.is_file()} == frozen
    assert json.loads((old_dir / "REQUEST.json").read_text())["request_id"] == "wo03-f10-f15"
    pool = [json.loads(line) for line in Path(second["derived_pool_path"]).read_text().splitlines()]
    assert pool[0]["supplement_gap_material"]["useful_material"] == MATERIAL
    assert Path(pool[0]["card_path"]).is_file()
    assert second_dir in Path(pool[0]["card_path"]).parents
    assert Path(second["materials_markdown_path"]).is_file()
    assert MATERIAL in Path(second["materials_markdown_path"]).read_text()
    assert (tmp_path / "inputs" / "POOL.jsonl").read_bytes() == b""


def test_attempt_allocation_preserves_legacy_root_and_is_concurrent_safe(tmp_path):
    logical_root = tmp_path / "legacy"
    logical_root.mkdir()
    (logical_root / "SUPPLEMENT_INDEX.json").write_text('{"status":"failed"}')
    with ThreadPoolExecutor(max_workers=4) as workers:
        attempts = list(workers.map(lambda _: supplement.allocate_supplement_attempt(logical_root), range(8)))
    assert len(set(attempts)) == 8
    assert all(path.parent == logical_root and not list(path.iterdir()) for path in attempts)
    assert (logical_root / "SUPPLEMENT_INDEX.json").read_text() == '{"status":"failed"}'


@pytest.mark.parametrize("status", ["partial", "failed", "unmet"])
def test_f15_missing_optional_feedback_fields_retains_material_and_gap(tmp_path, status):
    request = supplement_inputs(tmp_path / "inputs")
    raw = {"status": status, "usable_content": MATERIAL, "still_missing": "Other temperatures untested"}
    if status == "failed":
        raw.update(error="TimeoutError", failure_stage="fulfillment_judge", retryable=True)
    result = _run(request, tmp_path / "attempt", AcquisitionBoundary(), CardModelBoundary(), raw)
    assert result["status"] != "fulfilled"
    assert result["material_ready"] is True
    index = json.loads((Path(result["output_dir"]) / "SUPPLEMENT_INDEX.json").read_text())
    judgment = index["source_units"][0]["fulfillment_judgment"]
    assert judgment["remaining_gap"] == "Other temperatures untested"
    assert judgment["useful_material"] == MATERIAL
    assert MATERIAL in Path(result["materials_markdown_path"]).read_text()
    pool = [json.loads(line) for line in Path(result["derived_pool_path"]).read_text().splitlines()]
    assert pool[0]["supplement_gap_material"]["useful_material"] == MATERIAL
    if status == "failed":
        assert judgment["execution_status"] == "failed"
        assert judgment["error"] == "TimeoutError"
        assert index["fulfillment_judgment"]["failed_feedback"][0]["error"] == "TimeoutError"


@pytest.mark.parametrize("raw", [
    {"status": "complete"},
    {"status": "complete", "useful_material": MATERIAL},
    {"status": "fulfilled"},
    {"status": "fulfilled", "useful_material": MATERIAL, "fulfilled": False},
    {"status": "fulfilled", "useful_material": MATERIAL, "still_missing": "Other temperatures untested"},
    {"status": "fulfilled", "useful_material": MATERIAL, "criterion_assessments": [
        {"criterion": "condition", "status": "fulfilled"},
        {"criterion": "boundary", "status": "unmet"},
    ]},
])
def test_run_completion_or_explicit_open_gap_cannot_claim_fulfillment(raw):
    normalized = supplement.normalize_practical_fulfillment_judgment(raw, gap_question=QUESTION)
    assert normalized["status"] != "fulfilled"
    assert normalized["remaining_gap"]


def test_explicit_fulfillment_with_material_remains_supported():
    for raw in ({"status": "fulfilled", "useful_material": MATERIAL}, {"status": "complete", "fulfilled": True, "useful_material": MATERIAL}):
        normalized = supplement.normalize_practical_fulfillment_judgment(raw, gap_question=QUESTION)
        assert normalized["status"] == "fulfilled" and normalized["material_ready"] is True


def test_missing_outputs_and_reusable_material_reach_frozen_request_and_judge(tmp_path):
    request_path = supplement_inputs(tmp_path / "inputs")
    request = json.loads(request_path.read_text())
    request.update(required_outputs=["boundary"], reusable_material="Previously measured condition", still_missing="Boundary unmeasured", success_criteria=["boundary"])
    request_path.write_text(json.dumps(request))
    captured = []

    def judge(**kwargs):
        captured.append(kwargs["gap"])
        return {"status": "partial", "useful_material": MATERIAL, "still_missing": "Boundary still unresolved"}

    attempt = supplement.allocate_supplement_attempt(tmp_path / "supplements")
    supplement.run_planning_supplement(
        request_path, output_dir=attempt, gateway=None,
        acquirer_factory=AcquisitionBoundary().factory, card_runner=run_paper_reading_card,
        fulfillment_judge=judge, card_options={"client": CardModelBoundary()},
    )
    frozen = json.loads((attempt / "REQUEST.json").read_text())
    for row in (frozen, captured[0]):
        assert row["required_outputs"] == ["boundary"]
        assert row["success_criteria"] == ["boundary"]
        assert row["reusable_material"] == "Previously measured condition"
        assert row["still_missing"] == "Boundary unmeasured"


def test_attempts_preserve_existing_budget_reservations_and_request_limits(tmp_path):
    request = supplement_inputs(tmp_path / "inputs")
    ledger_path = tmp_path / "budget.sqlite"
    ledger = GlobalBudgetLedger(limit_cny=5, path=ledger_path)
    historical = ledger.reserve(0.7, "historical-settled")
    ledger.settle(historical["reservation_id"], 0.4)
    ledger.reserve(0.3, "historical-reserved")
    uncertain = ledger.reserve(0.2, "historical-uncertain")
    ledger.settle(uncertain["reservation_id"], None, uncertain=True)
    with sqlite3.connect(ledger_path) as db:
        before = list(db.execute("SELECT * FROM reservations ORDER BY reservation_id"))
    acquisition, client = AcquisitionBoundary(), CardModelBoundary()
    for _ in range(2):
        attempt = supplement.allocate_supplement_attempt(tmp_path / "supplements")
        result = _run(request, attempt, acquisition, client, {"status": "partial", "useful_material": MATERIAL}, budget_ledger_path=ledger_path, budget_limit_cny=5)
        index = json.loads((attempt / "SUPPLEMENT_INDEX.json").read_text())
        assert index["limits"] == {"max_candidates": 1, "max_acquisitions": 1, "per_query_limit": 1}
        assert result["fulfillment_judgment"]["acquisition_count"] == 1
    with sqlite3.connect(ledger_path) as db:
        assert list(db.execute("SELECT * FROM reservations ORDER BY reservation_id")) == before
    assert len(acquisition.calls) == len(client.calls) == 2


def test_low_level_runner_still_rejects_nonempty_attempt_directory(tmp_path):
    request = supplement_inputs(tmp_path / "inputs")
    attempt = supplement.allocate_supplement_attempt(tmp_path / "supplements")
    (attempt / "prior.txt").write_text("Do not replace")
    with pytest.raises(supplement.OutputDirectoryError, match="output_directory_must_be_new_or_empty"):
        _run(request, attempt, AcquisitionBoundary(), CardModelBoundary(), {})
    assert (attempt / "prior.txt").read_text() == "Do not replace"


@pytest.mark.parametrize("status", ["partial", "unmet", "failed"])
def test_existing_paper_preserves_useful_incomplete_material_in_pool_history(tmp_path, status):
    request = supplement_inputs(tmp_path / "inputs")
    acquisition, client = AcquisitionBoundary(), CardModelBoundary()
    first = _run(request, supplement.allocate_supplement_attempt(tmp_path / "supplements"), acquisition, client,
                 {"status": "fulfilled", "useful_material": "OLD measured condition"})
    old_pool = json.loads(Path(first["derived_pool_path"]).read_text())
    changed = json.loads(request.read_text())
    changed["base_pool_path"] = first["derived_pool_path"]
    changed["required_outputs"] = ["additional boundary"]
    request.write_text(json.dumps(changed))
    second = _run(request, supplement.allocate_supplement_attempt(tmp_path / "supplements"), acquisition, client,
                  {"status": status, "usable_content": "NEW useful partial condition", "still_missing": "Boundary unresolved"})
    pool = json.loads(Path(second["derived_pool_path"]).read_text())
    assert pool["card_path"] == old_pool["card_path"]
    assert pool["planning_view"] == old_pool["planning_view"]
    assert pool["supplement_gap_material"] == old_pool["supplement_gap_material"]
    materials = pool["supplement_gap_materials"]
    assert {row["useful_material"] for row in materials} == {"OLD measured condition", "NEW useful partial condition"}
    latest = next(row for row in materials if row["useful_material"].startswith("NEW"))
    assert latest["status"] != "fulfilled" and latest["remaining_gap"] == "Boundary unresolved"
    assert Path(latest["judgment_path"]).is_file()

    # A later fulfilled result must not erase the earlier useful partial note.
    changed["base_pool_path"] = second["derived_pool_path"]
    request.write_text(json.dumps(changed))
    third = _run(request, supplement.allocate_supplement_attempt(tmp_path / "supplements"), acquisition, client,
                 {"status": "fulfilled", "useful_material": "FINAL boundary condition"})
    final_pool = json.loads(Path(third["derived_pool_path"]).read_text())
    assert {row["useful_material"] for row in final_pool["prior_supplement_gap_materials"]} == {"OLD measured condition", "NEW useful partial condition"}
