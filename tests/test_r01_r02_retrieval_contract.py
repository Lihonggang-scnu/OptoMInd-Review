"""Offline contract evidence only; fixture outputs make no scientific claims."""
import copy
import json
from types import SimpleNamespace as NS

import pytest

from optomind_research.runtime.upgrade3.plan_contract import load_plan
from optomind_research.runtime.upgrade3.query_plan import QUERY_PLAN_SCHEMA
from optomind_research.runtime.upgrade3.candidate_corpus import (
    SnippetTextStore, build_candidate_corpus, plan_queries, harvest_query)


def plan():
    return {"schema_version": QUERY_PLAN_SCHEMA, "question_en": "How do optical cavities improve sensing?",
            "research_object": "optical cavities", "ambiguity": {"is_ambiguous": False, "default_reading": "", "needs_user_input": []},
            "facets": [{"id": "F1", "ask": "How do optical cavities improve sensing?", "keyword_queries": ["optical cavities sensing"],
                        "question_queries": ["How do optical cavities improve sensing?"],
                        "filters": {"publication_type": [], "fields_of_study": [], "text_availability": []}, "must_exclude": []}],
            "seeds": [], "criteria": {"must_include_topic": [], "must_exclude_domain": [], "synonyms": {}}, "additional_constraints": []}


def response(ok=True, category="success", audit=None):
    return NS(ok=ok, status_code=200 if ok else 429, status_category=category, error="" if ok else "rate_limit", audit=audit or {})


def paper(id="a", doi="10/shared", corpus=1, title="optical sensing"):
    return NS(paper_id=id, doi=doi, corpus_id=corpus, title=title, authors=[], year=2024, venue="", abstract="cavity sensing")


class Gateway:
    def __init__(self):
        self.calls = []
        self.fail = False
    def search_papers(self, query, **kwargs):
        self.calls.append(("paper", query, kwargs))
        return ([paper(), paper("b", corpus=2)], response())
    def search_snippets(self, query, **kwargs):
        self.calls.append(("snippet", query, kwargs))
        return [], response(not self.fail)
    def batch_papers(self, ids):
        return [paper()], response()


def test_loader_equivalence_and_fail_closed():
    p = plan()
    bare, receipt = load_plan(p)
    wrapped, other = load_plan({"status": "ok", "plan": p, "original_question": p["question_en"]})
    assert bare == wrapped and receipt["plan_sha256"] == other["plan_sha256"]
    for bad in [{"status": "invalid", "plan": p}, {**p, "schema_version": "wrong"}]:
        with pytest.raises(ValueError): load_plan(bad)
    with pytest.raises(ValueError, match="question_mismatch"): load_plan(p, expected_question="wrong topic")
    with pytest.raises(ValueError, match="hash_mismatch"): load_plan(p, expected_hash="wrong")
    p["ambiguity"]["needs_user_input"] = ["which cavity?"]
    with pytest.raises(ValueError, match="clarification"): load_plan(p)
    assert load_plan(p, assumptions=["optical cavities"])[1]["assumptions"]


def test_filters_both_channels_and_explicit_unapplied():
    p = plan()
    p["facets"][0]["filters"].update(publication_type=["JournalArticle"], publication_date_or_year="2020:")
    p["facets"][0]["literal_postfilters"] = {"include_terms": ["sensing"], "exclude_terms": ["biology"]}
    p["additional_constraints"] = ["Only experimental evidence"]
    p["criteria"]["must_include_topic"] = ["optical cavities"]
    g = Gateway()
    result = build_candidate_corpus(p, gateway=g, enrich=False)
    assert g.calls[0][2]["publication_types"] == ["JournalArticle"]
    assert g.calls[0][2]["publication_date_or_year"] == g.calls[1][2]["publication_date_or_year"] == "2020:"
    assert "publication_types" not in g.calls[1][2]
    assert result["exclusions"] == []
    assert result["expected_queries"][1]["unapplied_constraints"]
    assert result["query_audit"][0]["postfilter"]["rejected"] == 0


def test_zero_failure_partial_and_resume(tmp_path):
    p, g = plan(), Gateway()
    g.fail = True
    first = build_candidate_corpus(p, gateway=g, enrich=False)
    assert first["degraded"] and first["query_audit"][1]["execution_status"] == "failed"
    assert len(first["facets"]) == 1 and len(first["expected_queries"]) == 2
    g.fail = False
    g.calls.clear()
    resumed = build_candidate_corpus(p, gateway=g, enrich=False, resume=first)
    assert not resumed["degraded"] and len(g.calls) == 1 and g.calls[0][0] == "snippet"
    assert resumed["query_audit"][1]["execution_status"] == "zero_results"
    with pytest.raises(ValueError, match="resume_plan_hash"): build_candidate_corpus({**p, "research_object": "other"}, gateway=g, resume=first)
    class Partial(Gateway):
        def search_papers(self, query, **kw): return [paper()], response(category="partial_availability", audit={"page_runs": [{"status_code": 429}]})
    partial = build_candidate_corpus(p, gateway=Partial(), enrich=False)
    assert partial["query_audit"][0]["execution_status"] == "partial"
    assert partial["query_audit"][0]["responses"][0]["audit"]["page_runs"]


def test_aliases_survive_enrichment_and_failures_are_recorded():
    result = build_candidate_corpus(plan(), gateway=Gateway(), enrich=True)
    assert len(result["papers"]) == 1
    assert {h["paper_id"] for h in result["retrieval_hits"]} == {"a"}
    assert result["work_identity"]["alias_to_canonical"]["s2:b"] == "a"
    assert result["work_identity"]["merge_history"]
    class Broken(Gateway):
        def batch_papers(self, ids): raise RuntimeError("offline enrichment failure")
    result = build_candidate_corpus(plan(), gateway=Broken())
    assert result["degraded"] and not result["enrichment_audit"][0]["ok"]


def test_store_binding_and_hash(tmp_path):
    p, g = plan(), Gateway()
    store = SnippetTextStore(tmp_path / "snippets.sqlite")
    result = build_candidate_corpus(p, gateway=g, enrich=False, snippet_store=store)
    assert result["bundle"]["artifacts"]["snippet_store"]["sha256"]
    with pytest.raises(ValueError, match="wrong_run"):
        build_candidate_corpus(p, gateway=g, enrich=False, snippet_store=store)
    store.close()


def test_literal_positive_and_exclusion_applied():
    p = plan()
    p["facets"][0]["literal_postfilters"] = {"include_terms": ["sensing"], "exclude_terms": ["optical"]}
    hits, audit = harvest_query(Gateway(), plan_queries(p)[0], per_query_limit=10)
    assert not hits and audit["postfilter"]["rejected"] == 2


def test_cli_preview_never_imports_gateway(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    source = tmp_path / "PLAN.json"
    source.write_text(json.dumps(plan()))
    script = Path(__file__).resolve().parents[1] / "scripts/upgrade3/run_candidate_corpus.py"
    run = subprocess.run([sys.executable, str(script), "--schema", str(source), "--preview"], capture_output=True, text=True, check=True)
    payload = json.loads(run.stdout)
    assert payload["network_calls"] == 0 and len(payload["queries"]) == 2


def test_gateway_sends_parameters_to_transport():
    from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway, S2GatewayResponse
    class Transport:
        def __init__(self): self.calls = []
        def request_json(self, method, url, **kwargs):
            self.calls.append(kwargs["params"])
            return S2GatewayResponse(ok=True, status_code=200, status_category="success", payload={"data": []})
    transport = Transport()
    S2IntelligenceGateway(transport).search_papers("sensing", publication_types=["Review"], fields_of_study=["Physics"], year="2020-2025")
    assert transport.calls[0]["publicationTypes"] == "Review"
    assert transport.calls[0]["fieldsOfStudy"] == "Physics"
    assert transport.calls[0]["year"] == "2020-2025"


@pytest.mark.parametrize("question", [
    "Compare optical cavity sensing with interferometry after 2020, excluding biological samples.",
    "Compare battery cycle life at low temperatures, using only experimental results.",
    "How does irrigation affect wheat yield under drought compared with rainfed crops?",
])
def test_declared_cross_domain_requirements_preserved_without_semantic_claim(question):
    p = plan()
    p["original_question"] = question
    p["requirements"] = [{"id": "R1", "source_text": question, "object": "user-declared", "comparison": "user-declared", "conditions": [], "outcomes": [], "positive_constraints": [], "exclusions": []}]
    p["facets"][0]["requirement_mapping"] = ["R1"]
    loaded, receipt = load_plan(p, expected_question=question)
    assert loaded["requirements"] == p["requirements"]
    assert receipt["semantic_coverage"] == "not_assessed"
    p["facets"][0]["requirement_mapping"] = []
    with pytest.raises(ValueError, match="requirement_mapping_incomplete"): load_plan(p)


def test_snippet_checkpoint_resume_and_hash_rejection(tmp_path):
    class Snippets(Gateway):
        def search_snippets(self, query, **kwargs):
            self.calls.append(("snippet", query, kwargs))
            return [{"paper": {"corpusId": 1, "title": "optical sensing"}, "snippet": {"text": "exact offline fixture passage"}}], response()
    g, checkpoints = Snippets(), []
    store = SnippetTextStore(tmp_path / "text.sqlite")
    first = build_candidate_corpus(plan(), gateway=g, enrich=False, snippet_store=store, checkpoint_callback=checkpoints.append)
    assert len(checkpoints) == 2
    assert "exact offline fixture passage" not in json.dumps(first)
    g.calls.clear()
    resumed = build_candidate_corpus(plan(), gateway=g, enrich=False, snippet_store=store, resume=checkpoints[-1])
    assert not g.calls
    assert [h["hit_id"] for h in first["retrieval_hits"]] == [h["hit_id"] for h in resumed["retrieval_hits"]]
    snippet = next(h for h in first["retrieval_hits"] if h["text_hash"])
    store.add(snippet["hit_id"], "tampered")
    with pytest.raises(ValueError, match="snippet_hash_mismatch"):
        build_candidate_corpus(plan(), gateway=g, enrich=False, snippet_store=store, resume=first)
    store.close()


def test_two_round_usage_counts_all_attempts_once():
    from optomind_research.runtime.upgrade3.plan_contract import aggregate_planner_usage
    first, second = {"cost": 1.25, "in": 10, "out": 2}, {"cost": 2.5, "in": 20, "out": 3}
    result = aggregate_planner_usage({"query_plan_v2": {"round_usages": [first, second]}},
        last_usage=second, last_repair_usage=second,
        cost_function=lambda u: (u["cost"], u["in"], u["out"]))
    assert result == {"cost_cny": 3.75, "input_tokens": 30, "output_tokens": 5, "usage_rounds": 2}


def test_failed_retry_preserves_partial_hits_and_receipts():
    class Partial(Gateway):
        def search_papers(self, query, **kw):
            return [paper()], response(category="partial_availability")
    first = build_candidate_corpus(plan(), gateway=Partial(), enrich=False)
    class Failed(Gateway):
        def search_papers(self, query, **kw): return [], response(False)
    retry = build_candidate_corpus(plan(), gateway=Failed(), enrich=False, resume=first)
    assert retry["papers"] == first["papers"]
    assert retry["query_audit"][0]["execution_status"] == "partial"
    assert retry["query_audit"][0]["prior_attempts"]
    assert retry["query_audit"][0]["retained_prior_hits"] == 1
