"""Network-free engineering replays; scientific relevance is not measured here."""
from dataclasses import replace
from types import SimpleNamespace as NS
import json
import pytest

from optomind_research.runtime.upgrade3 import coverage_repair as C
from optomind_research.runtime.upgrade3.article_evidence import EvidenceRequest, request_binding
from optomind_research.runtime.upgrade3.reading_router import validate_routing_rows, read_candidates
from optomind_research.runtime.upgrade3.query_plan import QUERY_PLAN_SCHEMA


def plan(domain="optical cavities"):
    return {"schema_version": QUERY_PLAN_SCHEMA, "question_en": f"How does {domain} work?", "research_object": domain,
        "ambiguity": {"is_ambiguous": False, "default_reading": "", "needs_user_input": []},
        "facets": [{"id": f"F{i}", "ask": f"How does {domain} work under condition {i}?", "keyword_queries": [f"{domain} mechanism {i}"],
            "question_queries": [f"How does {domain} work {i}?"], "filters": {"publication_type": [], "fields_of_study": [], "text_availability": []}, "must_exclude": []} for i in (1, 2)],
        "seeds": [], "criteria": {"must_include_topic": [], "must_exclude_domain": [], "synonyms": {}}, "additional_constraints": []}


class Gateway:
    def __init__(self, mode="zero"):
        self.mode, self.calls = mode, []
    def search_papers(self, query, **kwargs):
        self.calls.append(query)
        rows = [NS(paper_id="P1", corpus_id=1, doi="10/1", title="measured source", abstract="measurement", year=2025, venue="", authors=[])] if self.mode == "hit" else []
        return rows, NS(ok=self.mode != "failure", status_code=503 if self.mode == "failure" else 200)
    def search_snippets(self, query, **kwargs):
        self.calls.append(query)
        return [], NS(ok=self.mode != "failure", status_code=503 if self.mode == "failure" else 200)


def request():
    return EvidenceRequest("How does it work?", required_outputs=("mechanism", "control"), budget={"max_papers": 4, "max_supplementary_calls": 1})


def unit(req, answers=()):
    return {"canonical_paper_id": "P1", "snapshot_id": "S1", "source_hash": "a" * 64,
        "material_depth": "fulltext", "roles": ["method_algorithm"],
        "anchors": [{"source_block_id": "b1", "quote": "measurement"}],
        "fulfilled_outputs": list(answers), "request_binding": request_binding(req)}


def runner(gateway=None, **kw):
    return C.CoverageRepairRunner(plan=plan(), facet_outputs={"F1": ["mechanism"], "F2": ["control"]},
        gateway=gateway or Gateway(), source_loader=kw.pop("source_loader", lambda **k: None), **kw)


def test_zero_and_service_failure_have_distinct_revision_paths():
    revised = []
    def reviser(**kwargs):
        revised.append(kwargs)
        return []
    zero = runner(query_reviser=reviser)
    zero(request=request(), unmet_outputs=("mechanism",))
    assert revised and revised[0]["unmet_facets"] == ("F1",)
    assert revised[0]["feedback"]
    revised.clear()
    failure = runner(Gateway("failure"), query_reviser=reviser)
    failure(request=request(), unmet_outputs=("mechanism",))
    assert not revised
    assert failure.state["rounds"] == 1
    assert failure.state["queries"] == 4  # two logical queries, one transport retry each
    assert all(q["spec"]["facet_id"] == "F1" for q in failure.state["query_history"])


def test_zero_budget_prevents_calls_and_recovery(tmp_path):
    g = Gateway()
    r = runner(g, budget={"max_queries": 0}, state_path=tmp_path / "state.json")
    assert not r(request=request(), unmet_outputs=("mechanism",))
    assert not g.calls and r.state["stop_reason"] == "max_queries_reached"
    other = runner(g, budget={"max_queries": 0}, state_path=tmp_path / "state.json")
    assert not other(request=request(), unmet_outputs=("mechanism",)) and not g.calls


def test_fulfilled_facet_not_searched_and_actual_results_bound():
    g = Gateway("hit")
    r = runner(g, source_loader=lambda **k: unit(k["request"], ["mechanism"]))
    units = r(request=request(), unmet_outputs=("mechanism",))
    assert len(g.calls) == 1 and len(units) == 1
    assert units[0]["retrieval_result_id"].startswith("repair-hit-")
    result = next(x for x in r.state["receipts"] if x["operation"] == "result")
    assert result["provider_identifiers"]["s2"] == "P1"
    assert r.state["stop_reason"] == "coverage_satisfied"


def test_synonym_revision_preserves_lineage_and_constraints():
    r = runner()
    parent = r.specs[0]
    def revise(**kwargs):
        return [{"parent_query_id": parent.query_id, "spec": replace(parent, query_id="revision-1", query_text="cavity resonator measurement")},
                {"parent_query_id": parent.query_id, "spec": replace(parent, query_id="bad", query_text="bad", server_params={"year": "1700"})}]
    r.reviser = revise
    r(request=request(), unmet_outputs=("mechanism",))
    assert r.state["rounds"] == 2
    rec = next(x for x in r.state["receipts"] if x["operation"] == "revision")
    assert [x["spec"]["query_id"] for x in rec["lineage"]] == ["revision-1"]
    assert "cavity resonator measurement" in r.gateway.calls


def test_read_budget_and_no_repeat_material():
    r = runner(Gateway("hit"), budget={"max_reads": 0})
    assert not r(request=request(), unmet_outputs=("mechanism",))
    assert r.state["reads"] == 0 and r.state["stop_reason"] == "max_reads_reached"
    r = runner(Gateway("hit"))
    r(request=request(), unmet_outputs=("mechanism", "control"))
    assert r.state["reads"] == 1


def test_uncertain_crash_never_redispatches(tmp_path):
    path = tmp_path / "state.json"
    r = runner(state_path=path)
    r._admit("read", paper_id="P1")
    restored = runner(state_path=path)
    assert not restored(request=request(), unmet_outputs=("mechanism",))
    assert restored.state["stop_reason"] == "uncertain_inflight_operation"


def packet(pid="P1"):
    return {"paper_id": pid, "material": {"title": "Method", "abstract": "A calibrated measurement of the mechanism."}, "material_sources": ["title", "abstract"]}


def row(**kwargs):
    return {"paper_id": "P1", "label": "background_or_method", "use": "measurement control", "evidence": "calibrated measurement", "evidence_source": "abstract", **kwargs}


def test_router_exact_identity_quote_method_and_missing_contract():
    good, _ = validate_routing_rows([packet()], [row()])
    assert good["P1"]["routing_validated"] and not good["P1"]["read_confirmed"]
    for rows in ([row(paper_id="OTHER")], [row(evidence="invented quote")], [row(use="")], [], [row(), row()]):
        out, _ = validate_routing_rows([packet()], rows)
        assert set(out) == {"P1"} and out["P1"]["label"] == "needs_verification"
    p = packet(); p["material"] = {"title": "Method", "citing_sentence": "calibrated measurement"}
    out, _ = validate_routing_rows([p], [row(label="directly_relevant", evidence_source="citing_sentence")])
    assert not out["P1"]["routing_validated"]


def test_router_budget_reserved_before_fake_call_and_unknown_usage_frozen():
    from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
    ledger = GlobalBudgetLedger(limit_cny=1)
    def call(*args):
        assert ledger.reserved_cny > 0
        return {"content": {"papers": [row()]}, "usage": {"input_tokens": 100, "output_tokens": 20}}
    result = read_candidates("q", [], [packet()], {}, call=call, budget={"ledger": ledger})
    assert result["judgements"]["P1"]["routing_validated"]
    assert ledger.actual_cny > 0 and ledger.reserved_cny == 0
    called = []
    low = read_candidates("q", [], [packet()], {}, call=lambda *a: called.append(a), budget={"cost_cap_cny": 1e-9})
    assert not called and low["accounting"]["stop_reason"] == "cost_cap_reached"
    unknown = GlobalBudgetLedger(limit_cny=1)
    read_candidates("q", [], [packet()], {}, call=lambda *a: {"content": "{}"}, budget={"ledger": unknown})
    assert unknown.reservations[0]["status"] == "uncertain"


@pytest.mark.parametrize("domain", ["optical cavities", "soil microbiology", "protein folding", "educational measurement"])
def test_crossdomain_frozen_failure_replay(domain):
    r = C.CoverageRepairRunner(plan=plan(domain), facet_outputs={"F1": ["mechanism"], "F2": ["control"]},
        gateway=Gateway(), source_loader=lambda **k: None)
    r(request=request(), unmet_outputs=("mechanism",))
    assert r.state["coverage"]["F1"]["unmet_outputs"] == ["mechanism"]
    assert not r.state["coverage"]["F2"]["unmet_outputs"]
    assert r.state["queries"] == 2


def test_real_article_consumer_wires_repair(monkeypatch):
    from optomind_research.runtime.upgrade3 import planning_material_search as search
    monkeypatch.setattr(search, "search", lambda *a, **k: NS(hits=[]))
    r = runner(Gateway("hit"), source_loader=lambda **k: unit(k["request"], ["mechanism", "control"]))
    bundle = C.collect_evidence_with_repair(request(), repair=r, index="unused", source_resolver=lambda hit: None)
    assert bundle.gap_fulfilled and bundle.source_units[0]["canonical_paper_id"] == "P1"


def test_reader_cannot_substitute_identity_or_invent_quotes():
    req = request()
    for change in ({"canonical_paper_id": "other"}, {"anchors": [{"source_block_id": "b1", "quote": "invented"}]}):
        r = runner(Gateway("hit"), source_loader=lambda **k: unit(k["request"]),
                   reader=lambda **k: {**k["source_unit"], **change, "fulfilled_outputs": ["mechanism"]})
        assert not r(request=req, unmet_outputs=("mechanism",))
        assert r.state["reads"] == 1
        assert any(x.get("status") == "unusable_material" for x in r.state["receipts"])


def test_conflict_expression_and_material_classification():
    assert C.classify_gap(conflict=True) == "evidence_conflict"
    assert C.classify_gap(expression_mismatch=True) == "expression_mismatch"
    assert C.classify_gap() == "material_insufficient"
    assert C.classify_gap(material_ready=True) == "unresolved"


def test_router_returns_unread_rows_for_budget_or_missing_material():
    p = packet("thin"); p["material"] = {"title": "Title only"}
    out = read_candidates("q", [], [packet(), p], {}, call=lambda *a: pytest.fail("must not call"), budget={"cost_cap_cny": 0})
    assert set(out["judgements"]) == {"P1", "thin"}
    assert all(not x["read_confirmed"] and not x["routing_validated"] for x in out["judgements"].values())
    validated, _ = validate_routing_rows([packet()], [row(sub_question="unknown")], [{"id": "F1", "ask": "Q"}])
    assert "unknown_or_missing_sub_question" in validated["P1"]["validation_errors"]


def test_frozen_evaluation_separates_navigation_evidence_counter_and_cost():
    from optomind_research.runtime.upgrade3.coverage_evaluation import compare_frozen_runs
    annotation = {"case_id": "fixture", "annotation_id": "independent-fixture-v1", "annotator": "test_fixture_maintainer",
        "facets": ["F1", "F2"], "requirements": ["object", "comparison"],
        "papers": {"P1": {"facets": ["F1"], "method_candidate": True, "counter_evidence": True}}}
    old = {"case_id": "fixture", "papers": {"P1": {"label": "clearly_irrelevant"}}}
    new = {"case_id": "fixture", "understood_requirements": ["object"],
        "papers": {"P1": {"label": "background_or_method", "read_confirmed": False}},
        "cost_receipts": [{"round": 1, "actual_cny": 0.01}]}
    result = compare_frozen_runs(old, new, annotation)
    assert result["baseline"]["false_exclusion_rate"] == 1
    assert result["repaired"]["method_candidate_recall"] == 1
    assert result["repaired"]["counter_evidence_recall"] == 1
    assert result["repaired"]["facet_evidence"]["F1"]["coverage"] == 0
    assert result["repaired"]["understanding_recall"] == 0.5
    assert result["live_quality_improvement"] == "not_assessed"


def test_remaining_capacity_before_failed_read_dispatch(monkeypatch):
    from optomind_research.runtime.upgrade3 import planning_material_search as search
    from optomind_research.runtime.upgrade3.candidate_corpus import Hit
    monkeypatch.setattr(search, "search", lambda *a, **k: NS(hits=[NS(paper_id="local")]))
    monkeypatch.setattr(C, "harvest_query", lambda *a, **k: ([Hit({"s2": p}, {}) for p in ["P1", "P2", "P3"]], {"execution_status": "success", "returned": 3}))
    calls = []
    def loader(**kwargs):
        calls.append(kwargs["hit"].identifiers)
        raise OSError("failed acquisition")
    req = EvidenceRequest("Q", required_outputs=("mechanism", "control"), budget={"max_papers": 2, "max_supplementary_calls": 1})
    r = runner(source_loader=loader)
    bundle = C.collect_evidence_with_repair(req, repair=r, index="unused", source_resolver=lambda hit: None)
    assert len(calls) == 1  # one local failed attempt + one supplement failed attempt
    assert r.state["reads"] == 1 and r.state["stop_reason"] == "remaining_paper_capacity_reached"
    assert not bundle.gap_fulfilled


def test_router_run_cap_cannot_be_bypassed_by_larger_project_ledger():
    from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
    ledger = GlobalBudgetLedger(limit_cny=100)
    out = read_candidates("q", [], [packet()], {}, call=lambda *a: pytest.fail("must stop before dispatch"),
                          budget={"cost_cap_cny": 1e-9, "ledger": ledger})
    assert out["accounting"]["stop_reason"] == "cost_cap_reached"
    assert not ledger.reservations
