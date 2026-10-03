"""WO03 local lookup contract: synthetic SQLite, real adapters and messages.

These fixtures are invented engineering/education examples. They are NOT the
local acceptance SQLite, paper prose, a live provider replay or scientific QA.
Only model construction/invocation and the network boundary are substituted.
"""
from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from optomind_research.runtime.upgrade3 import planning_material_search as search_module
from optomind_research.runtime.upgrade3 import planning_material_triage as triage
from optomind_research.runtime.upgrade3 import planning_supplement as supplement
from optomind_research.runtime.upgrade3 import progressive_review_plan as planner
from optomind_research.runtime.upgrade3.module4 import runtime

BASELINE_SHA = "91543f7c7e43515eb8e3c8cc86ee09ed54e486c5"
DOMAINS = {
    "coatings": {
        "query": "thermal fatigue coating",
        "answer": "SYNTHETIC_COUPON_RESULT: In the cycled coupon, a porous interlayer delayed crack initiation at 600 C.",
        "partial": "Synthetic coating observation: the uncycled coupon provides a baseline, not the cyclic boundary.",
    },
    "education": {
        "query": "retrieval practice learning",
        "answer": "SYNTHETIC_QUIZ_RESULT: Spaced quizzes improved delayed vocabulary recall, but not transfer to unseen grammar.",
        "partial": "Synthetic learning observation: immediate recall was measured, not delayed transfer.",
    },
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def material_text(payload):
    return "\n".join(str(row.get("text", "")) for row in payload["material_found"])


@pytest.fixture(autouse=True)
def deny_live_connections(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("WO03 offline contract forbids network connections")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


class ModelBoundary:
    """Capture exact constructed requests; never infer answers from fixture IDs."""

    def __init__(self, monkeypatch, domain, *, answers=None):
        self.calls = []
        self.domain = domain
        self.answers = list(answers or [])
        self.constructors = []

        def deny_network(*args, **kwargs):
            raise AssertionError("Synthetic message capture forbids network connections")
        monkeypatch.setattr(socket.socket, "connect", deny_network)
        monkeypatch.setattr(socket, "create_connection", deny_network)

        def construct(**kwargs):
            self.constructors.append({k: str(v) for k, v in kwargs.items() if k != "budget_ledger"})
            return object()  # avoids opening even the intentionally nonexistent key path

        def invoke(client, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.calls.append({"messages": copy.deepcopy(messages), "payload": payload,
                               "call_id": kwargs.get("call_id")})
            if self.answers:
                answer = self.answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                if callable(answer):
                    answer = answer(payload)
            else:
                answered = self.domain["answer"] in material_text(payload)
                answer = {
                    "decision": "direct_use" if answered else "external_research",
                    "answers_requested_question": answered,
                    "usable_content": self.domain["answer"] if answered else self.domain["partial"],
                    "still_missing": "" if answered else "Need the measured boundary.",
                    "external_ask": "" if answered else "Find the measured boundary.",
                    "reason": "Synthetic boundary reads only actual supplied material.",
                }
            return {"content": json.dumps(answer, ensure_ascii=False)}

        monkeypatch.setattr(runtime, "QwenDirectClient", construct)
        monkeypatch.setattr(runtime, "invoke_client", invoke)

    @property
    def payloads(self):
        return [call["payload"] for call in self.calls]


def make_fixture(tmp_path, *, discipline="coatings", distractors=6, target_matches=True,
                 target_handle="TGT07", unrelated=False):
    """Real FTS ranking puts six two-kind neighbors ahead of the target."""
    domain = DOMAINS[discipline]
    index_path = tmp_path / "material.sqlite"
    pool = []
    with search_module.PlanningMaterialIndex(index_path) as index:
        for position in range(1, distractors + 2):
            is_target = position == distractors + 1
            paper_id = "stable-target" if is_target else f"neighbor-{position:02d}"
            handle = target_handle if is_target else f"N{position:04d}"
            title = "Synthetic focused experiment" if is_target else f"Synthetic adjacent study {position}"
            card_path = tmp_path / "cards" / paper_id / "PAPER_READING_CARD.json"
            card_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot = tmp_path / "snapshots" / paper_id
            snapshot.mkdir(parents=True, exist_ok=True)
            indexed_text = (
                (domain["query"] + ". " if target_matches else "Unrelated vocabulary. ")
                + ("Synthetic unrelated observation: only instrument packaging was tested." if unrelated else domain["answer"])
                if is_target else
                f"{domain['query']}. {domain['query']}. N{position:02d}_BODY: Only the neighboring topic was described."
            )
            card = {"paper_identity": {"canonical_paper_id": paper_id, "title": title},
                    "general_understanding": {"finding": indexed_text},
                    "review_planning": {"planning_summary": "Synthetic fixture material."},
                    "material": {"snapshot_id": paper_id, "material_scope": "fulltext"}}
            card_path.write_text(json.dumps(card), encoding="utf-8")
            (snapshot / "READING_VIEW.md").write_text(indexed_text, encoding="utf-8")
            (snapshot / "manifest.json").write_text(json.dumps({"snapshot_id": paper_id}), encoding="utf-8")
            index.upsert_paper(search_module.PaperRecord(
                paper_id=paper_id, source_handle=handle, title=title, year="2025", doi="",
                card_path=str(card_path), snapshot_path=str(snapshot), material_depth="fulltext",
                identity_status="pool_identity", pool_action="already_in_pool"))
            segments = [{"segment_kind": "document_block", "ordinal": 0,
                         "section_path": ["Results"], "text": indexed_text}]
            if not is_target:
                segments.append({"segment_kind": "card_key_finding", "ordinal": 1,
                                 "section_path": ["Summary"],
                                 "text": f"{domain['query']}. N{position:02d}_CARD: Necessary original qualifier remains untested."})
            index.add_segments(paper_id, segments)
            index.set_paper_terms(paper_id, [domain["query"]] if target_matches or not is_target else [])
            pool.append({"paper_id": paper_id, "_paper_id": paper_id, "_source_handle": handle,
                         "card_path": str(card_path), "planning_view": card,
                         "_b_summary": {"declared_content_depth": "fulltext"}})
        index.record_term_document_frequency()
        index.commit()
    config = planner.ProgressivePlannerConfig(topic_id="wo03-synthetic-" + discipline,
        pool_path=tmp_path / "POOL.jsonl", plan_path=tmp_path / "PLAN.json", output_dir=tmp_path / "run",
        chapter_workers=1, reader_workers=1)
    config.pool_path.write_text("".join(json.dumps(row) + "\n" for row in pool), encoding="utf-8")
    plan = {"question_en": domain["query"], "facets": []}
    planner._atomic_json(config.plan_path, plan)
    return SimpleNamespace(domain=domain, index_path=index_path, pool=pool, plan=plan, config=config,
                           target_id="stable-target", target_handle=target_handle,
                           handle_map={row["_source_handle"]: row["paper_id"] for row in pool if row["_source_handle"]})


def request(fixture, *, owner="CH01", outputs=("measured boundary",), known=None, handles=None):
    row = {"gap_id": "G-synthetic", "gap_question": fixture.domain["query"], "intended_use": "mechanism",
           "chapter_ids": [owner], "success_criteria": ["measured relation"], "required_outputs": list(outputs),
           "targeted_queries": [{"query_text": fixture.domain["query"], "query_type": "keyword"}]}
    if known is not None:
        row["known_papers"] = known
    if handles is not None:
        row["known_paper_handles"] = handles
    return row


def actual_judge(fixture):
    return triage.QwenLocalTriageJudge(key_file=fixture.config.output_dir / "DOES_NOT_EXIST.key",
        budget_ledger_path=fixture.config.output_dir / "offline-ledger.sqlite", budget_limit_cny=1)


def direct(fixture, gap, *, name="local", **kwargs):
    return supplement.run_gap_local_triage(gap, index_path=fixture.index_path, judge=actual_judge(fixture),
        output_dir=fixture.config.output_dir / name, **kwargs)


def run_loop(fixture, gap, *, phase="level2", **kwargs):
    runner = planner.make_retrieval_loop_runner(fixture.config,
        key_file=fixture.config.output_dir / "DOES_NOT_EXIST.key",
        budget_ledger_path=fixture.config.output_dir / "offline-ledger.sqlite", budget_limit_cny=1,
        local_index_path=fixture.index_path, allow_external=False)
    return runner(phase=phase, supplement_requests=[gap], pool_rows=fixture.pool, plan=fixture.plan,
                  source_handle_map=fixture.handle_map, **kwargs)


def owner_messages(fixture, tool_result, owner):
    """Production chapter packet AND final request constructor, no planner substitute."""
    captured = []

    def boundary(stage, payload):
        assert stage == "chapter_details"
        messages = planner._messages_for(stage, payload)
        captured.append({"messages": messages, "payload": copy.deepcopy(payload)})
        return {"chapter_plan": {"thesis": "Explain measured boundary", "units": [
            {"substantive_point": "Measured relation", "paragraph_briefs": [
                {"point": "Boundary", "development": "Preserve conditions in supplied material."}]}]}}

    engine = planner.ProgressiveReviewPlanner(fixture.config, planner=boundary)
    engine._chapter_details(chapters=[{"chapter_id": owner, "title": "Measured boundary", "source_ids": []}],
        shared_outline={}, topic=fixture.domain["query"], candidates={}, level1_tools={},
        level2_tools=tool_result, tool_materials_by_chapter=tool_result["tool_materials_by_chapter"],
        resume=False, state={})
    return captured[0]


def assert_rank_seven(fixture):
    with search_module.PlanningMaterialIndex(fixture.index_path, readonly=True) as index:
        result = search_module.search(index, fixture.domain["query"], top_papers=12, passages_per_paper=2)
    ids = list(dict.fromkeys(hit.paper_id for hit in result.hits))
    assert ids.index(fixture.target_id) == 6
    return result


@pytest.mark.parametrize("discipline", ["coatings", "education"])
def test_rank_seven_material_reaches_expansion_and_actual_owner(tmp_path, monkeypatch, discipline):
    f = make_fixture(tmp_path, discipline=discipline)
    assert_rank_seven(f)
    model = ModelBoundary(monkeypatch, f.domain)
    result = run_loop(f, request(f))
    assert len(model.calls) == 2
    first, expanded = model.payloads
    assert f.domain["answer"] not in material_text(first)
    assert f.target_handle not in {row["source_handle"] for row in first["material_found"]}
    assert f.domain["answer"] in material_text(expanded)
    assert f.target_handle in {row["source_handle"] for row in expanded["material_found"]}
    assert result["retrieval_loop"]["needs"][0]["status"] == "answered"
    material = result["tool_materials_by_chapter"]["CH01"][0]
    assert f.domain["answer"] in material["usable_content"]
    assert f.target_id in {row["paper_id"] for row in material["sources"]}
    actual_owner = owner_messages(f, result, "CH01")
    assert f.domain["answer"] in json.dumps(actual_owner["messages"])
    assert f.target_handle in json.dumps(actual_owner["messages"])


def test_expansion_beats_global_prefix_and_keeps_original_qualifier(tmp_path, monkeypatch):
    f = make_fixture(tmp_path)
    wide = assert_rank_seven(f)
    # Merely raising top_papers retains this crowded prefix and still misses.
    assert f.target_id not in {hit.paper_id for hit in wide.hits[:5]}
    model = ModelBoundary(monkeypatch, f.domain)
    result = direct(f, request(f))
    assert len(model.calls) == 2
    first, expanded = model.payloads
    assert len({row["source_handle"] for row in first["material_found"]}) <= 3
    assert len({row["source_handle"] for row in expanded["material_found"]}) == 7
    assert "N01_BODY" in material_text(expanded)
    assert "N01_CARD" in material_text(expanded)
    assert sum(len(row["text"]) for row in expanded["material_found"]) - sum(len(row["text"]) for row in first["material_found"]) <= 24000
    assert expanded["paper_context"] == first["paper_context"]
    assert result["model_calls"] == 2
    assert result["writer_material"]["provenance"]["downloads"] == 0
    assert result["writer_material"]["provenance"]["whole_paper_rereads"] == 0


@pytest.mark.parametrize("identity_key", ["paper_id", "canonical_paper_id"])
def test_stable_known_paper_without_handle_is_actually_read(tmp_path, monkeypatch, identity_key):
    f = make_fixture(tmp_path)
    model = ModelBoundary(monkeypatch, f.domain)
    result = run_loop(f, request(f, known=[{identity_key: f.target_id}]))
    assert f.domain["answer"] in material_text(model.payloads[0])
    assert result["retrieval_loop"]["needs"][0]["status"] == "answered"
    assert f.target_handle in json.dumps(owner_messages(f, result, "CH01")["messages"])


@pytest.mark.parametrize("mapping", [{}, {"TGT07": "missing-current-paper"}])
def test_unresolved_or_stale_handle_never_borrows_index_identity(tmp_path, monkeypatch, mapping):
    f = make_fixture(tmp_path, target_matches=False)
    model = ModelBoundary(monkeypatch, f.domain)
    result = direct(f, request(f, handles=[f.target_handle]), source_handle_map=mapping)
    assert all(f.domain["answer"] not in material_text(payload) for payload in model.payloads)
    assert result["decision"] == "external_research"
    assert not result["answers_requested_question"]
    assert f.target_id not in {row["paper_id"] for row in result["writer_material"]["sources"]}


def test_known_unrelated_material_never_forces_fulfillment(tmp_path, monkeypatch):
    f = make_fixture(tmp_path, unrelated=True)
    model = ModelBoundary(monkeypatch, f.domain)
    result = direct(f, request(f, known=[{"paper_id": f.target_id}]), source_handle_map=f.handle_map)
    assert "only instrument packaging" in material_text(model.payloads[0])
    assert result["decision"] == "external_research"
    assert result["still_missing"] == "Need the measured boundary."
    assert f.domain["partial"] in result["usable_content"]
    assert not result["answers_requested_question"]


def test_fulfilled_rebinds_two_owners_without_second_model_call(tmp_path, monkeypatch):
    f = make_fixture(tmp_path, distractors=0)
    model = ModelBoundary(monkeypatch, f.domain)
    first = run_loop(f, request(f))
    second = run_loop(f, request(f, owner="CH02"), phase="chapters")
    assert len(model.calls) == 1
    for result, owner in ((first, "CH01"), (second, "CH02")):
        material = result["tool_materials_by_chapter"][owner][0]
        assert f.domain["answer"] in material["usable_content"]
        assert f.target_id in {row["paper_id"] for row in material["sources"]}
        assert f.domain["answer"] in json.dumps(owner_messages(f, result, owner)["messages"])
    assert second["retrieval_loop"]["needs"][0]["reused_answer"]


def test_added_outputs_only_process_new_gap_and_keep_useful_partial(tmp_path, monkeypatch):
    f = make_fixture(tmp_path, distractors=0)
    model = ModelBoundary(monkeypatch, f.domain, answers=[
        {"decision": "direct_use", "answers_requested_question": True, "usable_content": "OLD measured relation.", "still_missing": ""},
        {"decision": "external_research", "answers_requested_question": False, "usable_content": "NEW useful partial boundary.", "still_missing": "Missing long-term comparison."},
    ])
    first = run_loop(f, request(f, outputs=("condition",)))
    second = run_loop(f, request(f, owner="CH02", outputs=("condition", "long-term comparison")), phase="chapters")
    assert len(model.calls) == 2
    latest = model.payloads[-1]
    assert "long-term comparison" in json.dumps(latest["success_criteria"])
    assert "condition" not in json.dumps(latest["success_criteria"])
    material = second["tool_materials_by_chapter"]["CH02"][0]
    assert "OLD measured relation." in material["usable_content"]
    assert "NEW useful partial boundary." in material["usable_content"]
    assert "Missing long-term comparison." in material["still_missing"]
    assert second["retrieval_loop"]["needs"][0]["status"] != "answered"
    messages = json.dumps(owner_messages(f, second, "CH02")["messages"])
    assert "OLD measured relation." in messages and "NEW useful partial boundary." in messages
    assert first["retrieval_loop"]["needs"][0]["status"] == "answered"


def test_sufficient_first_read_keeps_original_message_and_one_call(tmp_path, monkeypatch):
    f = make_fixture(tmp_path, distractors=0)
    model = ModelBoundary(monkeypatch, f.domain)
    gap = request(f)
    gap.pop("required_outputs")  # unmodified original first-read contract
    with search_module.PlanningMaterialIndex(f.index_path, readonly=True) as index:
        local_gap = triage.LocalGap(gap_id=gap["gap_id"], question=gap["gap_question"],
            intended_use="mechanism", success_criteria=tuple(gap["success_criteria"]),
            chapter_ids=("CH01",), concepts=(f.domain["query"],))
        bundle, _ = triage.prepare_local_reading(index, local_gap)
        expected = triage._triage_payload(local_gap, bundle)
    result = direct(f, gap)
    assert len(model.calls) == 1
    assert model.payloads[0] == expected
    assert model.calls[0]["messages"][0]["content"] == triage.TRIAGE_SYSTEM_PROMPT
    assert result["decision"] == "direct_use"


def test_same_input_resume_does_not_repeat_expansion(tmp_path, monkeypatch):
    f = make_fixture(tmp_path, unrelated=True)
    model = ModelBoundary(monkeypatch, f.domain)
    first = direct(f, request(f), name="resume")
    saved = copy.deepcopy(model.calls)
    second = direct(f, request(f), name="resume")
    assert len(saved) == 2 and model.calls == saved
    assert second["usable_content"] == first["usable_content"]
    assert second["still_missing"] == first["still_missing"]
    assert second["decision"] == "external_research"


def test_expansion_failure_is_preserved_and_explicit_retry_only_retries_failed_read(tmp_path, monkeypatch):
    f = make_fixture(tmp_path)
    partial = {"decision": "external_research", "answers_requested_question": False,
               "usable_content": "USEFUL initial partial measurement.", "still_missing": "Missing boundary."}
    model = ModelBoundary(monkeypatch, f.domain, answers=[partial, TimeoutError("synthetic model outage")])
    first = run_loop(f, request(f), phase="failure")
    assert len(model.calls) == 2
    state = first["retrieval_loop"]["needs"][0]
    assert state["status"] != "answered"
    assert state["local_triage"]["provider_failed"]
    material = first["tool_materials_by_chapter"]["CH01"][0]
    assert "USEFUL initial partial measurement." in material["usable_content"]
    messages = json.dumps(owner_messages(f, first, "CH01")["messages"])
    assert "USEFUL initial partial measurement." in messages and "synthetic model outage" in messages
    root = f.config.output_dir / "local_lookup"
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.json")}
    assert before and "synthetic model outage" in "\n".join(value.decode() for value in before.values())
    second = run_loop(f, request(f), phase="failure")
    assert len(model.calls) == 2
    assert second["retrieval_loop"]["needs"][0]["status"] != "answered"
    result = run_loop(f, {**request(f), "retry_empty_result": True}, phase="failure")
    assert len(model.calls) == 3
    assert f.domain["answer"] in material_text(model.payloads[-1])
    assert result["retrieval_loop"]["needs"][0]["status"] == "answered"
    recovered = result["tool_materials_by_chapter"]["CH01"][0]
    assert "USEFUL initial partial measurement." in recovered["usable_content"]
    assert f.domain["answer"] in json.dumps(owner_messages(f, result, "CH01")["messages"])
    assert all((root / name).read_bytes() == value for name, value in before.items())
    retried = [bucket for bucket in root.iterdir() if len(list(bucket.glob("attempt-*"))) >= 2]
    assert len(retried) == 1
    assert any((path / "FAILED.json").is_file() for path in retried[0].glob("attempt-*"))
    assert any((path / "RESULT.json").is_file() for path in retried[0].glob("attempt-*"))


def test_stable_identity_overrides_stale_planner_handle(tmp_path, monkeypatch):
    f = make_fixture(tmp_path)
    model = ModelBoundary(monkeypatch, f.domain)
    gap = request(f, known=[{"paper_id": f.target_id, "source_handle": "N0001"}])
    normalized = planner._resolve_planner_handles({"supplement_requests": [gap]}, f.handle_map)
    result = run_loop(f, normalized["supplement_requests"][0])
    assert f.domain["answer"] in material_text(model.payloads[0])
    target = next(row for row in result["tool_materials_by_chapter"]["CH01"][0]["sources"]
                  if row["paper_id"] == f.target_id)
    assert target["source_handle"] == f.target_handle


def test_added_resolvable_handle_invalidates_stopped_same_phase_resume(tmp_path, monkeypatch):
    f = make_fixture(tmp_path)
    # Force a useful but unmet judgment from both first and expanded reads.
    partial = {"decision": "external_research", "answers_requested_question": False,
               "usable_content": "Initial bounded observation.", "still_missing": "Need nominated comparison."}
    model = ModelBoundary(monkeypatch, f.domain, answers=[partial, partial])
    first = run_loop(f, request(f), phase="same_phase")
    assert first["retrieval_loop"]["needs"][0]["status"] != "answered"
    assert len(model.calls) == 2
    second = run_loop(f, request(f, handles=[f.target_handle]), phase="same_phase")
    assert len(model.calls) == 3
    assert f.domain["answer"] in material_text(model.payloads[-1])
    assert second["retrieval_loop"]["needs"][0]["status"] == "answered"


def test_stable_known_id_can_read_lexically_unmatched_summary(tmp_path, monkeypatch):
    f = make_fixture(tmp_path, target_matches=False)
    model = ModelBoundary(monkeypatch, f.domain)
    result = run_loop(f, request(f, known=[{"paper_id": f.target_id}]))
    assert len(model.calls) == 1
    assert f.domain["answer"] in material_text(model.payloads[0])
    assert result["retrieval_loop"]["needs"][0]["status"] == "answered"
    assert f.target_handle in json.dumps(owner_messages(f, result, "CH01")["messages"])


def capture_fixed(destination):
    import tempfile
    monkeypatch = pytest.MonkeyPatch()
    try:
        with tempfile.TemporaryDirectory(prefix="wo03-synthetic-fixed-") as directory:
            f = make_fixture(Path(directory))
            model = ModelBoundary(monkeypatch, f.domain)
            ranking = assert_rank_seven(f)
            result = run_loop(f, request(f))
            second = run_loop(f, request(f, owner="CH02"), phase="chapters")
            first_owner = owner_messages(f, result, "CH01")
            second_owner = owner_messages(f, second, "CH02")
            evidence = {"evidence_kind": "synthetic fixture, actual repaired production requests; no live/scientific acceptance",
                        "baseline_sha": BASELINE_SHA, "loaded_production_module": str(supplement.__file__),
                        "ranking": [{"paper_id": row.paper_id, "source_handle": row.source_handle} for row in ranking.hits],
                        "boundary_calls": model.calls, "model_calls_after_two_owners": len(model.calls),
                        "first_owner": first_owner, "second_owner": second_owner,
                        "retained_material": result["tool_materials_by_chapter"]["CH01"],
                        "first_status": result["retrieval_loop"]["needs"][0]["status"],
                        "second_status": second["retrieval_loop"]["needs"][0]["status"],
                        "second_reused_answer": second["retrieval_loop"]["needs"][0]["reused_answer"],
                        "target_text": f.domain["answer"]}
            Path(destination).write_text(json.dumps(evidence, ensure_ascii=False, indent=2).replace(directory, "<SYNTHETIC_ROOT>") + "\n", encoding="utf-8")
    finally:
        monkeypatch.undo()


def capture_baseline(destination):
    """Run with PYTHONPATH set to exact baseline to retain actual input evidence."""
    import tempfile
    monkeypatch = pytest.MonkeyPatch()
    try:
        with tempfile.TemporaryDirectory(prefix="wo03-synthetic-baseline-") as directory:
            f = make_fixture(Path(directory))
            model = ModelBoundary(monkeypatch, f.domain)
            ranking = assert_rank_seven(f)
            normal = direct(f, request(f), name="normal")
            wider_only = direct(f, request(f), name="wider-only", top_papers=12)
            direct_calls = copy.deepcopy(model.calls)
            loop_result = run_loop(f, request(f))
            loop_calls = copy.deepcopy(model.calls[len(direct_calls):])
            original_owner = owner_messages(f, loop_result, "CH01")
            result = {"evidence_kind": "synthetic fixture, actual baseline production messages; no live/scientific acceptance",
                      "baseline_sha": BASELINE_SHA, "loaded_production_module": str(supplement.__file__),
                      "ranking": [{"paper_id": row.paper_id, "source_handle": row.source_handle} for row in ranking.hits],
                      "boundary_calls": direct_calls,
                      "loop_boundary_calls": loop_calls, "loop_model_calls": len(loop_calls),
                      "loop_status": loop_result["retrieval_loop"]["needs"][0]["status"],
                      "first_owner": original_owner,
                      "normal_decision": normal["decision"], "wider_only_decision": wider_only["decision"],
                      "target_text": f.domain["answer"], "model_calls": len(model.calls)}
            # The synthetic temp root has no real private paths, but keep output portable.
            serialized = json.dumps(result, ensure_ascii=False, indent=2).replace(directory, "<SYNTHETIC_ROOT>")
            Path(destination).write_text(serialized + "\n", encoding="utf-8")
    finally:
        monkeypatch.undo()


if __name__ == "__main__":
    import sys
    (capture_fixed if len(sys.argv) > 2 and sys.argv[2] == "fixed" else capture_baseline)(sys.argv[1])
