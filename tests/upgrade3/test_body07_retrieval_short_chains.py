"""WO07 short chains 1/2: synthetic edges, real adapters and durable artifacts.

Never call live providers. The first planner probe stops on arrival at the actual
case model boundary; it deliberately does not finish a planning or BODY run.
These tests establish delivery of scientific content/qualifiers, not model
adherence or scientific correctness of generated prose.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arrangement
from optomind_research.runtime.upgrade3 import material_acquisition, paper_reading_card
from optomind_research.runtime.upgrade3 import planning_supplement as supplement
from optomind_research.runtime.upgrade3 import progressive_review_plan as p
from optomind_research.runtime.upgrade3 import review_unit_writer as writer
from optomind_research.runtime.upgrade3.local_materials import LocalTeiMaterialProvider
from optomind_research.runtime.upgrade3.module4 import runtime
from optomind_research.runtime.upgrade3.planning_material_search import PlanningMaterialIndex

FIXTURES = {
    "engineering": {
        "question": "Which thermal cycling condition changes coating crack onset?",
        "query": "thermal cycling porous coating crack onset",
        "finding": "ENG_RESULT: porous coupons delayed crack onset under laboratory thermal cycling.",
        "limit": "ENG_BOUNDARY: service-load transfer was not measured.",
    },
    "education": {
        "question": "Which retrieval spacing condition changes delayed vocabulary recall?",
        "query": "retrieval spacing delayed vocabulary recall",
        "finding": "EDU_RESULT: spaced quizzes improved delayed vocabulary recall over restudy in novices.",
        "limit": "EDU_BOUNDARY: transfer to unseen grammar problems was not measured.",
    },
}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("WO07 forbids all network connections")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


class Edges:
    """Only search, acquisition transport and raw model completion substitutes."""
    def __init__(self, root, monkeypatch, domain="engineering", *, partial=False):
        self.root, self.domain, self.partial = root, FIXTURES[domain], partial
        self.calls = {key: [] for key in ("search", "acquire", "query", "card", "judge", "triage", "arrange")}
        edge = self

        class Gateway:
            def search_papers(self, query, **kwargs):
                journals = list(edge.root.glob("run/**/retrieval_loop.jsonl"))
                assert journals, "query initialization must be durable before search"
                assert any(json.loads(path.read_text().splitlines()[-1])["status"] == "query_ready" for path in journals)
                edge.calls["search"].append({"query": query, "options": kwargs, "query_checkpoint_seen": True})
                # The explicitly nominated synthetic paper supplies identity;
                # search does not invent or inject a ready-made supplement.
                return [], SimpleNamespace(status_code=200)

        class Acquisition:
            def __init__(self, output_root, **kwargs):
                self.output_root = output_root
            def acquire(self, record):
                edge.calls["acquire"].append(copy.deepcopy(record))
                body = edge.domain["finding"] + " " + edge.domain["limit"]
                xml = f'<article><front><article-meta><title-group><article-title>Synthetic comparison</article-title></title-group></article-meta></front><body><sec><title>Results</title><p>{body}</p></sec></body></article>'
                snapshot = LocalTeiMaterialProvider(self.output_root).build_from_bytes(
                    xml.encode(), canonical_paper_id="synthetic-new", metadata={"title": "Synthetic comparison"},
                    material_depth_override="fulltext")
                return SimpleNamespace(snapshot=snapshot, status="acquired", material_depth="fulltext", attempts=[], known_gaps=[], errors=[])

        class Model:
            def __init__(self, **kwargs):
                pass
            def complete(self, messages, **kwargs):
                call = kwargs.get("call_id", "")
                captured = {"messages": copy.deepcopy(messages), "options": kwargs}
                if call.startswith("query-refinement:"):
                    edge.calls["query"].append(captured)
                    payload = json.loads(messages[-1]["content"])
                    result = {"targeted_queries": ([{"query_type": "keyword", "query_text": edge.domain["query"]}]
                              if payload["next_round"] == 1 else [])}
                elif call.startswith("paper-card:"):
                    edge.calls["card"].append(captured)
                    assert edge.domain["finding"] in json.dumps(messages)
                    result = {
                        "general_understanding": {"paper_kind": "empirical", "research_scope": "LOCAL_ONLY synthetic controlled comparison",
                            "work_summary": edge.domain["finding"], "problem_or_question": edge.domain["question"],
                            "approach": "Compare manipulated conditions with a control",
                            "key_findings": [{"finding": edge.domain["finding"], "conditions": "Only the stated synthetic setting"}],
                            "contribution_and_limits": [{"contribution": edge.domain["finding"], "limits": edge.domain["limit"]}]},
                        "review_planning": {"planning_summary": edge.domain["finding"], "topic_handles": [edge.domain["query"]],
                            "scope_interpretation_cautions": [edge.domain["limit"], "LOCAL_ONLY fictional fixture"]},
                    }
                elif call.startswith("planning-supplement-judge:"):
                    edge.calls["judge"].append(captured)
                    assert edge.domain["question"] in json.dumps(messages)
                    assert edge.domain["finding"] in json.dumps(messages)
                    result = {"status": "partial" if edge.partial else "fulfilled",
                              "useful_material": edge.domain["finding"] + " " + edge.domain["limit"],
                              "remaining_gap": "UNJUDGEABLE_PROVIDER_OUTAGE: the second setting cannot be assessed." if edge.partial else ""}
                elif call.startswith("planning-material-triage:"):
                    edge.calls["triage"].append(captured)
                    result = {"decision": "external_research", "answers_requested_question": False,
                              "usable_content": "", "still_missing": edge.domain["question"]}
                else:
                    raise AssertionError(f"unexpected model call {call}")
                return {"content": json.dumps(result), "finish_reason": "stop", "complete": True}

        monkeypatch.setattr(supplement, "CompositeS2OpenAlexGateway", lambda *args: Gateway())
        monkeypatch.setattr(material_acquisition, "MaterialAcquirer", Acquisition)
        monkeypatch.setattr(runtime, "QwenDirectClient", Model)
        monkeypatch.setattr(paper_reading_card, "QwenDirectClient", Model)

    def counts(self):
        return {key: len(value) for key, value in self.calls.items()}


def fixture(root, monkeypatch, domain="engineering", *, partial=False, index=False, early=False):
    root.mkdir(parents=True, exist_ok=True)
    edges = Edges(root, monkeypatch, domain, partial=partial)
    d = edges.domain
    plan = {"schema_version": "research_harness.query_plan.v2", "question_en": d["question"],
        "research_object": "Synthetic controlled comparison", "ambiguity": {"is_ambiguous": False, "default_reading": "", "needs_user_input": []},
        "facets": [{"id": "F1", "ask": d["question"], "keyword_queries": [d["query"]], "question_queries": [d["question"]],
                    "filters": {"publication_type": [], "fields_of_study": [], "text_availability": []}, "must_exclude": []}],
        "seeds": [], "criteria": {"must_include_topic": [], "must_exclude_domain": [], "synonyms": {}}, "additional_constraints": []}
    config = p.ProgressivePlannerConfig(topic_id="WO07-synthetic-"+domain, pool_path=root/"POOL.jsonl", plan_path=root/"PLAN.json",
        output_dir=root/"run", chapter_workers=1, reader_workers=1, planning_revision_enabled=True)
    p._atomic_json(config.plan_path, plan)
    rows = [{"paper_id": "synthetic-early", "planning_view": {"paper_identity": {"canonical_paper_id": "synthetic-early", "title": "Synthetic baseline"},
             "planning_summary": "NORMAL_BASELINE: the control setting remains unchanged."}}] if early else []
    p._write_jsonl(config.pool_path, rows)
    if index:
        with PlanningMaterialIndex(config.output_dir/"planning_material_index.sqlite") as store:
            store.commit()
    # Deliberately absent; model constructors above cannot read a real credential.
    adapter = p.make_planning_supplement_runner(config, key_file=root/"NO_CREDENTIAL", budget_ledger_path=root/"ledger.sqlite", budget_limit_cny=1)
    runner = p.make_retrieval_loop_runner(config, key_file=root/"NO_CREDENTIAL", budget_ledger_path=root/"ledger.sqlite", budget_limit_cny=1, supplement_runner=adapter)
    return config, plan, edges, runner


def gap(edges, owner="CH01", *, query=False):
    return {"gap_id": "synthetic-gap", "gap_question": edges.domain["question"], "chapter_ids": [owner],
            "success_criteria": ["Report the measured comparison and its limits"], "required_outputs": ["condition"],
            "targeted_queries": [{"query_type": "keyword", "query_text": edges.domain["query"], "facet_id": "F1"}] if query else [],
            "known_papers": [{"paper_id": "synthetic-new", "title": "Synthetic comparison"}]}


class StopAtCaseInput(BaseException):
    """Test stop, deliberately bypassing production retry/error fallbacks."""


class PlanningEdge:
    def __init__(self, edges, *, request=True):
        self.edges, self.request, self.calls = edges, request, []
        self.chapter = {"chapter_id": "CH01", "title": "Controlled comparison", "purpose": "Explain the observed condition"}

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(payload)))
        if stage == "provisional_scope":
            return {"provisional_outline": [self.chapter]}
        if stage == "level1_outline":
            return {"shared_outline": [self.chapter]}
        if stage == "source_routing":
            return {"source_routes": [{"source_handle": row["source_handle"], "chapter_ids": ["CH01"],
                      "specific_usable_material": "NORMAL_BASELINE: the control setting remains unchanged."} for row in payload["candidate_batch"]]}
        if stage == "chapter_proposals":
            return {"chapter_proposals": [self.chapter]}
        if stage in {"harmonize_scope", "finalize_chapter_scope"}:
            result = {"shared_outline": [self.chapter], "chapters": [self.chapter]}
            if stage == "harmonize_scope" and self.request:
                result["supplement_requests"] = [gap(self.edges)]
            return result
        if stage in {"chapter_need_analysis", "whole_plan_improvement"}:
            return {}
        if stage == "affected_chapter_revision":
            return {"status": "no_changes_needed", "rationale": "Keep the bounded synthetic task unchanged."}
        if stage == "chapter_details":
            return {"chapter_plan": {"thesis": "Compare only the supplied settings", "units": [{"unit_id": "U1", "substantive_point": "Observed condition",
                "source_handles": ["P0001"], "paragraph_briefs": [{"paragraph_id": "T1", "point": "Compare measured settings",
                "development": "Preserve the source-specific condition and limit", "source_handles": ["P0001"]}]}]}}
        if stage == "case_groups":
            raise StopAtCaseInput()
        raise AssertionError(stage)


def writer_handoff(root, config, edges, result, pool, *, owner="CH02", handle="P0009"):
    engine = p.ProgressiveReviewPlanner(config, planner=lambda *args: pytest.fail("no planning call in writer handoff"))
    engine.tool_materials_by_chapter = copy.deepcopy(result["tool_materials_by_chapter"])
    engine._bind_tool_material_source_handles(pool)
    packet = {"chapter": {"chapter_id": owner}, "research_question": edges.domain["question"],
              "chapter_plan": {"units": [{"unit_id": "U1", "substantive_point": "Keep the observation and boundary",
                  "paragraph_briefs": [{"paragraph_id": "TASK_ORIGINAL", "point": "Explain measured comparison",
                                         "source_handles": [handle]}]}]}, "source_materials": [],
              "source_identity_map": engine._source_identity_map(pool)}
    materials = engine.tool_materials_by_chapter[owner]
    packet = p.merge_tool_materials_into_packets([packet], materials)[0]
    p._atomic_json(root/"PACKET.json", packet)
    view = arrangement.build_chapter_view(root/"PACKET.json", id_map_path=root/"ID_MAP.json")

    class ArrangementEdge:
        def complete(self, messages, **kwargs):
            edges.calls["arrange"].append({"messages": copy.deepcopy(messages), "options": kwargs})
            return {"content": json.dumps({"chapter_id": owner, "units": [{"unit_id": view.units[0].unit_id,
                "paragraph_tasks": [{"paragraph_id": view.units[0].paragraph_briefs[0].paragraph_id,
                    "point": view.units[0].paragraph_briefs[0].point, "source_uses": [{"source_handle": handle, "use": "Keep reported setting"}]}]}]}),
                "finish_reason": "stop", "complete": True}

    arranged = arrangement.run_arrangement(view, client=ArrangementEdge(), model="synthetic-offline", planning_revision=True)
    assert arranged["validation"]["ok"], arranged["validation"]
    arranged["source_catalog"] = arrangement.build_source_catalog(view, arranged)
    arranged["chapter_tool_materials"] = arrangement.compact_chapter_tool_materials(view)
    p._atomic_json(root/"ARRANGEMENT.json", arranged)
    arrangement.write_view(view, root/"ARRANGEMENT_INPUT.json")
    unit = writer.build_unit_view(root/"ARRANGEMENT.json", view.units[0].unit_id, view_path=root/"ARRANGEMENT_INPUT.json")
    messages = writer.unit_messages(unit, planning_revision=True)
    p._atomic_json(root/"WRITER_MESSAGES.json", messages)
    return packet, arranged, json.loads(read(root/"WRITER_MESSAGES.json")[-1]["content"])


def archive(name, root, edges, **observations):
    destination = os.environ.get("BODY07_RETRIEVAL_EVIDENCE_DIR")
    if not destination:
        return
    artifacts = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".md"}:
            artifacts[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    database = {}
    for path in root.rglob("*.sqlite"):
        with sqlite3.connect(path) as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            database[str(path.relative_to(root))] = {table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
    data = {"fixture_status": "SYNTHETIC_OFFLINE", "paid_calls": 0,
            "boundary_counts": edges.counts(), "boundary_calls": edges.calls, "observations": observations,
            "persisted_file_contents": artifacts, "sqlite_table_row_counts": database,
            "normalization": "Temporary absolute root is replaced with RUN_ROOT; root prefixes truncated by production excerpts become RUN_ROOT_TRUNCATED. No other content transformation."}
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str).replace(str(root), "RUN_ROOT")
    # The production late-route preview cuts at 4,000 chars and can cut
    # through an absolute path; normalize its remaining root prefix as well.
    for prefix_length in range(len(str(root)) - 1, 11, -1):
        text = text.replace(str(root)[:prefix_length], "RUN_ROOT_TRUNCATED")
    path = Path(destination)/f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("domain,partial", [("engineering", False), ("education", False), ("education", True)])
def test_empty_query_real_supplement_pool_late_route_actual_case_input(tmp_path, monkeypatch, domain, partial):
    config, plan, edges, runner = fixture(tmp_path, monkeypatch, domain, partial=partial, index=True, early=True)
    model = PlanningEdge(edges)
    engine = p.ProgressiveReviewPlanner(config, planner=model, retrieval_loop_runner=runner)
    with pytest.raises(StopAtCaseInput):
        engine.run(resume=True)
    case = next(payload for stage, payload in model.calls if stage == "case_groups")
    tools = read(config.output_dir/"stages/level2_tools.json")
    state = tools["retrieval_loop"]["needs"][0]
    if partial:
        assert state["status"] != "answered"
        assert "UNJUDGEABLE_PROVIDER_OUTAGE" in state["still_missing"]
    else:
        assert state["status"] == "answered" and state["query_status"] == "ready"
    assert state["query_source"] == "refiner" and state["empty_rounds"] == (1 if partial else 0)
    assert edges.counts() == {"search": 1, "acquire": 1, "query": 2 if partial else 1, "card": 1, "judge": 1, "triage": 1 if partial else 0, "arrange": 0}
    journal = [json.loads(line) for line in (config.output_dir/"level2/retrieval_loop.jsonl").read_text().splitlines()]
    ready = next(row for row in journal if row["status"] == "query_ready")
    assert ready["counts_as_round"] is False and ready["queries"][0]["query_text"] == edges.domain["query"]
    actual = tools["supplement_results"][0]["results"][0]
    request = read(Path(actual["output_dir"])/"REQUEST.json")
    assert request["targeted_queries"] == [{"query_type": "keyword", "query_text": edges.domain["query"], "facet_id": "F1"}]
    pool = [json.loads(line) for line in Path(actual["derived_pool_path"]).read_text().splitlines()]
    fresh = next(row for row in pool if row["paper_id"] == "synthetic-new")
    assert edges.domain["finding"] in read(fresh["card_path"])["general_understanding"]["work_summary"]
    assert edges.domain["limit"] in fresh["supplement_gap_material"]["useful_material"]
    routes = read(config.output_dir/"stages/source_routing_summary.json")
    assert [row["source_handle"] for row in routes["late_source_routes"]] == ["P0002"]
    assert routes["late_source_routes"][0]["chapter_ids"] == ["CH01"]
    assert edges.domain["finding"] in routes["late_source_routes"][0]["specific_usable_material"]
    delivered = next(row for row in case["source_materials"] if row["source_handle"] == "P0002")
    assert edges.domain["finding"] in json.dumps(delivered)
    assert edges.domain["limit"] in json.dumps(delivered)
    if partial:
        assert "UNJUDGEABLE_PROVIDER_OUTAGE" in json.dumps(delivered)
        local_message = json.loads(edges.calls["triage"][0]["messages"][-1]["content"])
        assert edges.domain["finding"] in json.dumps(local_message["material_found"])
        assert "UNJUDGEABLE_PROVIDER_OUTAGE" in json.dumps(local_message["material_found"])
        assert state["query_status"] == "exhausted"
        assert state["status"] == "partial"
    assert case["research_question"] == edges.domain["question"]
    assert case["source_routing_batch_count"] == 1
    assert len([stage for stage, _ in model.calls if stage == "source_routing"]) == 1
    assert model.calls[-1][0] == "case_groups"
    assert not (config.output_dir/"PROGRESSIVE_REVIEW_PLAN.json").exists()
    with PlanningMaterialIndex(config.output_dir/"planning_material_index.sqlite", readonly=True) as store:
        assert "synthetic-new" in {row["paper_id"] for row in store.papers()}
    archive("chain1_"+domain+("_partial" if partial else ""), tmp_path, edges, case_input=case, planner_calls=model.calls, reuse_key=state["reuse_key"], stop="arrival_at_case_input")


def test_cross_stage_completion_reuse_current_identity_actual_writer_messages(tmp_path, monkeypatch):
    config, plan, edges, runner = fixture(tmp_path, monkeypatch)
    first = runner(phase="level2", supplement_requests=[gap(edges)], pool_rows=[], plan=plan)
    original_attempt = Path(first["supplement_results"][0]["results"][0]["output_dir"])
    frozen = {str(path.relative_to(original_attempt)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in original_attempt.rglob("*") if path.is_file()}
    pool = p._merge_supplement_pool_updates([], first)
    assert pool[0]["_source_handle"] == "P0001"
    # A different current chapter/handle namespace must never spend again or
    # confuse the new paper with an unrelated source occupying its old handle.
    pool[0]["_source_handle"] = "P0009"
    pool.append({"paper_id": "foreign", "_paper_id": "foreign", "_source_handle": "P0001",
                 "planning_view": {"planning_summary": "FOREIGN_RESULT_MUST_NOT_ENTER"}})
    second_runner = p.make_retrieval_loop_runner(config, supplement_runner=p.make_planning_supplement_runner(
        config, key_file=tmp_path/"NO_CREDENTIAL", budget_ledger_path=tmp_path/"ledger.sqlite", budget_limit_cny=1))
    second = second_runner(phase="chapters", supplement_requests=[gap(edges, "CH02")], pool_rows=pool, plan=plan,
                           source_handle_map={"P0009": "synthetic-new", "P0001": "foreign"})
    a, b = first["retrieval_loop"]["needs"][0], second["retrieval_loop"]["needs"][0]
    assert b["status"] == "answered" and b["reused_answer"] is True
    assert a["reuse_key"] == b["reuse_key"] and not b["still_missing"]
    assert edges.counts() == {"search": 1, "acquire": 1, "query": 1, "card": 1, "judge": 1, "triage": 0, "arrange": 0}
    cache = read(config.output_dir/"RETRIEVAL_NEED_CACHE.json")["records"][a["reuse_key"]]
    assert cache["owner_bindings"] == [{"phase": "level2", "owners": ["CH01"]}, {"phase": "chapters", "owners": ["CH02"]}]
    assert cache["source_fingerprints"]["synthetic-new"]
    assert second["supplement_results"][0]["results"][0]["chapter_ids"] == ["CH02"]
    assert {str(path.relative_to(original_attempt)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in original_attempt.rglob("*") if path.is_file()} == frozen
    packet, arranged, payload = writer_handoff(tmp_path/"handoff", config, edges, second, pool)
    assert arranged["source_catalog"]["P0009"]["paper_id"] == "synthetic-new"
    assert "P0001" not in arranged["source_catalog"]
    tool = payload["chapter_tool_materials"][0]
    assert tool["question"] == edges.domain["question"]
    assert tool["chapter_ids"] == ["CH02"]
    assert tool["sources"][0]["source_handle"] == "P0009"
    assert edges.domain["finding"] in tool["usable_content"] and edges.domain["limit"] in tool["usable_content"]
    assert "FOREIGN_RESULT_MUST_NOT_ENTER" not in json.dumps(payload)
    assert edges.counts()["arrange"] == 1
    archive("chain2_completion_reuse", tmp_path, edges, first=first, second=second, writer_payload=payload, original_attempt_hashes_unchanged=frozen)


def test_partial_need_qualifier_stays_pending_through_writer_input(tmp_path, monkeypatch):
    config, plan, edges, runner = fixture(tmp_path, monkeypatch, "education", partial=True)
    result = runner(phase="partial", supplement_requests=[gap(edges, "CH02", query=True)], pool_rows=[], plan=plan)
    state = result["retrieval_loop"]["needs"][0]
    assert state["status"] != "answered" and not state["reused_answer"]
    assert "UNJUDGEABLE_PROVIDER_OUTAGE" in state["still_missing"]
    pool = p._merge_supplement_pool_updates([], result)
    pool[0]["_source_handle"] = "P0009"
    _, _, payload = writer_handoff(tmp_path/"handoff", config, edges, result, pool)
    assert "UNJUDGEABLE_PROVIDER_OUTAGE" in payload["chapter_tool_materials"][0]["still_missing"]
    assert payload["chapter_tool_materials"][0]["question"] == edges.domain["question"]
    assert edges.domain["finding"] in payload["chapter_tool_materials"][0]["usable_content"]
    assert edges.counts() == {"search": 1, "acquire": 1, "query": 1, "card": 1, "judge": 1, "triage": 0, "arrange": 1}
    archive("chain2_pending_qualifier", tmp_path, edges, state=state, writer_payload=payload,
            caveat="Delivery only; no claim that a live writer would retain the qualifier.")


def test_normal_no_new_material_no_recovery_single_batch_and_cached_resume(tmp_path, monkeypatch):
    config, plan, edges, runner = fixture(tmp_path, monkeypatch, early=True)
    model = PlanningEdge(edges, request=False)
    engine = p.ProgressiveReviewPlanner(config, planner=model, retrieval_loop_runner=runner)
    with pytest.raises(StopAtCaseInput):
        engine.run()
    first_case = next(payload for stage, payload in model.calls if stage == "case_groups")
    first_routes = (config.output_dir/"stages/source_routing_summary.json").read_bytes()
    model.calls.clear()
    with pytest.raises(StopAtCaseInput):
        engine.run(resume=True)
    assert [stage for stage, _ in model.calls] == ["case_groups"]
    assert model.calls[0][1] == first_case
    assert (config.output_dir/"stages/source_routing_summary.json").read_bytes() == first_routes
    assert "late_source_routes" not in json.loads(first_routes)
    assert first_case["pool_sources"] == 1 and first_case["source_routing_batch_count"] == 1
    assert not config.recovery_from
    assert edges.counts() == dict.fromkeys(edges.calls, 0)
    assert "NORMAL_BASELINE" in json.dumps(first_case)
    archive("normal_unchanged", tmp_path, edges, first_case=first_case, resumed_case=model.calls[0][1], resume_model_stages=[stage for stage, _ in model.calls])
