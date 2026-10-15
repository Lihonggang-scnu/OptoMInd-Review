from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from optomind_research.runtime.upgrade3 import initial_retrieval as ir
from optomind_research.runtime.upgrade3.candidate_corpus import SnippetTextStore, build_candidate_corpus
from optomind_research.runtime.upgrade3.module4.runtime import QwenDirectClient, QwenTransportError


def plan():
    return {"research_object": "scoped optics", "question_en": "How do scoped optical methods compare?",
            "facets": [{"id": "F1", "ask": "Compare optical mechanisms and limits",
                        "keyword_queries": ["original keywords"],
                        "question_queries": ["Original optical question?"], "must_exclude": ["unrelated fields"]}]}


def paper(identity):
    return SimpleNamespace(paper_id="P" + str(identity), corpus_id=identity, doi="",
                           title="Scoped paper " + str(identity), authors=["Author"],
                           year=2025, venue="Journal", abstract="Full abstract")


class Gateway:
    def __init__(self, results=None, statuses=None, interrupt=None):
        self.results = results or {}
        self.statuses = statuses or {}
        self.interrupt = interrupt
        self.calls = []

    def _get(self, text):
        self.calls.append(text)
        if text == self.interrupt:
            self.interrupt = None
            raise KeyboardInterrupt("simulated interrupted query")
        return list(self.results.get(text, [])), SimpleNamespace(status_code=self.statuses.get(text, 200))

    def search_papers(self, text, **kwargs):
        ids, response = self._get(text)
        return [paper(i) for i in ids], response

    def search_snippets(self, text, **kwargs):
        ids, response = self._get(text)
        return [{"paper": {"corpusId": i, "title": paper(i).title}, "score": 0.9,
                 "snippet": {"text": f"Complete snippet {text} identity={i} " + "long evidence " * 500,
                             "section": "Results", "snippetOffset": {"start": 10, "end": 9000}}}
                for i in ids], response

    def batch_papers(self, ids):
        return [paper(str(i).removeprefix("CorpusId:").removeprefix("P")) for i in ids], SimpleNamespace(status_code=200)


class Model:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.requests = []
        self.factories = []

    def factory(self, directory):
        self.factories.append(directory)
        return self

    def __call__(self, messages, **kwargs):
        self.requests.append((messages, kwargs))
        content = next(self.outputs)
        return {"content": json.dumps(content) if not isinstance(content, str) else content,
                "returned_model": ir.MODEL, "complete": True, "finish_reason": "stop",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


def query(number, text, *, kind="keyword", facet="F1"):
    return {"query_id": f"SUP_R{number}_F1_KQ_01", "facet_id": facet,
            "query_type": kind, "query_text": text}


def run(tmp_path, gateway, model=None, **kwargs):
    return ir.run_initial_retrieval(plan(), output_dir=tmp_path, gateway=gateway,
                                    client_factory=model.factory if model else None,
                                    per_query_limit=400, enrich=False, snippet_discovery_target=0, **kwargs)


def test_500_bypasses_every_qwen_setup_and_preserves_snapshot(tmp_path):
    original = plan()
    gateway = Gateway({"original keywords": range(300), "Original optical question?": range(300, 500)})
    def forbidden(_):
        pytest.fail("500-paper branch constructed model/ledger/credentials")
    corpus = ir.run_initial_retrieval(original, output_dir=tmp_path, gateway=gateway,
                                      client_factory=forbidden, per_query_limit=400, enrich=False,
                                      snippet_discovery_target=0)
    assert corpus["statistics"]["distinct_papers"] == 500
    assert corpus["initial_retrieval"]["supplementary_rounds_used"] == 0
    assert gateway.calls == ["original keywords", "Original optical question?"]
    assert json.loads((tmp_path / "ORIGINAL_PLAN.json").read_text()) == original
    assert original == plan()


def test_feedback_counts_merge_evidence_and_stop_above_500(tmp_path):
    gateway = Gateway({"original keywords": [1, 2], "Original optical question?": [2, 2, 3],
                       "broader scoped query": range(3, 303), "complementary scoped query": range(303, 520)})
    model = Model([{"queries": [query(1, "broader scoped query")]},
                   {"queries": [query(2, "complementary scoped query")]}])
    corpus = run(tmp_path, gateway, model)
    request = json.loads(model.requests[0][0][1]["content"])
    assert request["original_user_subquestions"][0]["ask"] == plan()["facets"][0]["ask"]
    first, second = request["executed_queries"]
    assert (first["raw_provider_hits"], first["admitted_hits"], first["query_distinct_papers"], first["new_identities"]) == (2, 2, 2, 2)
    assert (second["raw_provider_hits"], second["admitted_hits"], second["query_distinct_papers"], second["new_identities"], second["overlap_identities"]) == (3, 3, 2, 1, 1)
    assert corpus["statistics"]["distinct_papers"] == 519  # no final trimming
    assert corpus["statistics"]["raw_hits"] == 522
    assert len(model.requests) == 2
    assert gateway.calls == ["original keywords", "Original optical question?", "broader scoped query", "complementary scoped query"]
    active = json.loads((tmp_path / "ACTIVE_QUERY_PLAN.json").read_text())
    assert [q["round_label"] for q in active["queries"]] == ["original", "original", "supplement round1", "supplement round2"]
    assert len(json.loads(model.requests[1][0][1]["content"])["executed_queries"]) == 3
    store = SnippetTextStore(tmp_path / "SNIPPET_TEXT.sqlite")
    try:
        assert store.count() == 3
        assert len(store.get("H000003")) > 5000
    finally:
        store.close()
    calls = list(gateway.calls)
    run(tmp_path, gateway, model)
    assert gateway.calls == calls and len(model.requests) == 2


def test_three_invalid_or_no_new_rounds_still_hand_off(tmp_path):
    gateway = Gateway({"original keywords": [1]})
    model = Model(["bad JSON", {"queries": [query(2, " ORIGINAL   KEYWORDS ")]}, {"queries": []}])
    corpus = run(tmp_path, gateway, model)
    state = corpus["initial_retrieval"]
    assert state["status"] == "complete" and state["downstream_allowed"]
    assert state["target_unmet"] and state["stop_reason"] == "round_limit"
    assert state["supplementary_rounds_used"] == len(model.requests) == 3
    assert len(gateway.calls) == 2
    assert (tmp_path / "supplement_round1" / "RAW_RESPONSE.json").is_file()
    assert json.loads((tmp_path / "supplement_round2" / "VALIDATION.json").read_text())["errors"] == ["queries[0]:duplicate_query_text"]


def test_invalid_facets_types_and_ids_are_visible_never_executed(tmp_path):
    bad = [query(1, "bad facet", facet="F99"), query(1, "bad type", kind="other"),
           {**query(1, "bad value"), "query_text": ["not text"]},
           {**query(1, "new text"), "query_id": " F1_KQ_01 "}]
    model = Model([{"queries": bad}, {"queries": []}, {"queries": []}])
    gateway = Gateway()
    run(tmp_path, gateway, model)
    validation = json.loads((tmp_path / "supplement_round1" / "VALIDATION.json").read_text())
    assert len(validation["errors"]) == 4
    assert len(gateway.calls) == 2


def test_stops_remaining_generated_queries_when_first_new_query_reaches_target(tmp_path):
    gateway = Gateway({"original keywords": range(300), "Original optical question?": range(300, 400),
                       "enough new scoped papers": range(400, 520), "unnecessary query": range(520, 700)})
    model = Model([{"queries": [query(1, "enough new scoped papers"),
                                {**query(1, "unnecessary query"), "query_id": "SUP_R1_F1_KQ_02"}]}])
    corpus = run(tmp_path, gateway, model)
    assert corpus["statistics"]["distinct_papers"] == 520
    assert "unnecessary query" not in gateway.calls
    active = json.loads((tmp_path / "ACTIVE_QUERY_PLAN.json").read_text())
    assert active["queries"][-1]["execution_status"] == "not_executed_target_met"
    assert active["queries"][-2]["execution_status"] == "complete"
    assert len(corpus["query_audit"]) == 3
    calls = list(gateway.calls)
    run(tmp_path, gateway, model)
    assert gateway.calls == calls and len(model.requests) == 1


def test_call_ids_are_stable_on_resume_and_distinct_between_runs(tmp_path):
    first, second = Model([{"queries": []}] * 3), Model([{"queries": []}] * 3)
    run(tmp_path / "one", Gateway(), first)
    ids = [kwargs["call_id"] for _, kwargs in first.requests]
    assert len(set(ids)) == 3
    run(tmp_path / "one", Gateway(), first)
    saved = json.loads((tmp_path / "one" / "supplement_round1" / "DISPATCHED.json").read_text())
    assert saved["call_id"] == ids[0] and len(first.requests) == 3
    run(tmp_path / "two", Gateway(), second)
    assert not set(ids) & {kwargs["call_id"] for _, kwargs in second.requests}


def test_http_failure_is_unavailable_not_low_scientific_yield(tmp_path):
    gateway = Gateway(statuses={"original keywords": 429})
    model = Model([{"queries": []}] * 3)
    corpus = run(tmp_path, gateway, model)
    feedback = json.loads(model.requests[0][0][1]["content"])["executed_queries"][0]
    assert feedback["status"] == 429 and feedback["transport_outcome"] == "unsuccessful"
    assert feedback["yield_outcome"] == "not_assessed"
    assert corpus["initial_retrieval"]["transport_unsuccessful_queries"] == ["F1_KQ_01"]


def test_parsing_interruption_reuses_paid_reply(tmp_path, monkeypatch):
    gateway, model = Gateway(), Model([{"queries": []}] * 3)
    original = ir._validate_response
    def interrupted(*args):
        raise KeyboardInterrupt("interrupted parsing")
    monkeypatch.setattr(ir, "_validate_response", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, gateway, model)
    assert (tmp_path / "supplement_round1" / "RAW_RESPONSE.json").exists()
    monkeypatch.setattr(ir, "_validate_response", original)
    run(tmp_path, gateway, model)
    assert len(model.requests) == 3 and len(gateway.calls) == 2


def test_query_interruption_reuses_successful_queries_and_paid_reply(tmp_path):
    gateway = Gateway(interrupt="second supplement")
    model = Model([{"queries": [query(1, "first supplement"), {**query(1, "second supplement"), "query_id": "SUP_R1_F1_KQ_02"}]},
                   {"queries": []}, {"queries": []}])
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, gateway, model)
    corpus = run(tmp_path, gateway, model)
    assert gateway.calls.count("original keywords") == gateway.calls.count("Original optical question?") == 1
    assert gateway.calls.count("first supplement") == 1
    assert gateway.calls.count("second supplement") == 2
    assert len(model.requests) == 3 and corpus["initial_retrieval"]["target_unmet"]
    assert list((tmp_path / "query_failures").glob("*.json"))


def test_wire_receipt_recovery_avoids_paid_regeneration(tmp_path):
    calls = []
    def factory(directory):
        def client(messages, **kwargs):
            calls.append(directory)
            wire = {"model": ir.MODEL, "id": "receipt", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "choices": [{"finish_reason": "stop", "message": {"content": '{"queries": []}'}}]}
            (directory / "http").mkdir()
            (directory / "http" / "receipt.raw").write_text(json.dumps(wire))
            raise KeyboardInterrupt("after HTTP receipt before normalized RAW save")
        return client
    gateway = Gateway()
    with pytest.raises(KeyboardInterrupt):
        ir.run_initial_retrieval(plan(), output_dir=tmp_path, gateway=gateway, client_factory=factory,
                                 enrich=False, snippet_discovery_target=0)
    model = Model([{"queries": []}] * 2)
    ir.run_initial_retrieval(plan(), output_dir=tmp_path, gateway=gateway, client_factory=model.factory,
                             enrich=False, snippet_discovery_target=0)
    assert len(calls) == 1 and len(model.requests) == 2 and len(gateway.calls) == 2
    saved = json.loads((tmp_path / "supplement_round1" / "RAW_RESPONSE.json").read_text())
    assert saved["request_id"] == "receipt" and saved["budget_reconciliation"] == "unchanged"


def test_unknown_dispatch_and_corrupt_checkpoint_do_not_rebuy_or_fake_empty(tmp_path):
    model = Model([])
    def factory(_):
        def interrupted(*a, **k):
            raise KeyboardInterrupt("unknown in-flight request")
        return interrupted
    with pytest.raises(KeyboardInterrupt):
        ir.run_initial_retrieval(plan(), output_dir=tmp_path, gateway=Gateway(), client_factory=factory, enrich=False)
    with pytest.raises(ir.InitialRetrievalError, match="uncertain"):
        ir.run_initial_retrieval(plan(), output_dir=tmp_path, gateway=Gateway(), client_factory=model.factory, enrich=False)
    assert not model.factories
    journal = next((tmp_path / "queries").glob("*.json"))
    saved = json.loads(journal.read_text())
    saved["harvest"]["hits"] = [{"invented": True}]
    journal.write_text(json.dumps(saved))
    with pytest.raises(ir.InitialRetrievalError, match="checkpoint_corrupt"):
        ir.run_initial_retrieval(plan(), output_dir=tmp_path, gateway=Gateway(), client_factory=model.factory, enrich=False)


def test_budget_refusal_is_truthful_successful_handoff(tmp_path):
    def factory(_):
        def refused(*args, **kwargs):
            raise QwenTransportError("global_budget_exceeded")
        return refused
    corpus = ir.run_initial_retrieval(plan(), output_dir=tmp_path, gateway=Gateway(),
                                      client_factory=factory, enrich=False)
    assert corpus["initial_retrieval"]["downstream_allowed"]
    assert corpus["initial_retrieval"]["stop_reason"] == "budget_limited"
    assert corpus["initial_retrieval"]["target_unmet"]
    assert (tmp_path / "supplement_round1" / "BUDGET_LIMITED.json").exists()


def test_adapter_preview_agreement_and_consumer_corpus(tmp_path):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(plan()))
    outcome = tmp_path / "outcome.json"
    outcome.write_text(json.dumps({"plan": plan(), "status": "complete"}))
    preview = ir.execute_initial_m2(plan_path=source, outcome_path=outcome,
                                    output_dir=tmp_path / "preview", gateway_factory=lambda: pytest.fail("preview network"))
    assert preview["status"] == "preview"
    model = Model([{"queries": []}] * 3)
    corpus = ir.execute_initial_m2(plan_path=source, outcome_path=outcome,
                                   output_dir=tmp_path / "run", run=True,
                                   gateway_factory=lambda: Gateway({"original keywords": [1]}),
                                   client_factory=model.factory, enrich=False)
    assert corpus["schema_version"] == "optomind.candidate_corpus.v2"
    assert {"papers", "retrieval_hits", "statistics", "query_audit", "input_state"} <= set(corpus)
    assert json.loads(source.read_text()) == plan()
    outcome.write_text(json.dumps({"plan": {"different": True}}))
    with pytest.raises(ir.InitialRetrievalError, match="does_not_match"):
        ir.execute_initial_m2(plan_path=source, outcome_path=outcome, output_dir=tmp_path / "other")


def test_normal_cli_uses_runner_and_free_preview(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("candidate_cli", repo / "scripts/upgrade3/run_candidate_corpus.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    source = tmp_path / "plan.json"
    source.write_text(json.dumps(plan()))
    args = ["--schema", str(source), "--out", str(tmp_path / "CANDIDATE_CORPUS.json"),
            "--snippets", str(tmp_path / "SNIPPET_TEXT.sqlite"), "--per-query-limit", "400"]
    assert cli.main(args + ["--preview"]) == 0
    gateway = Gateway({"original keywords": range(300), "Original optical question?": range(300, 500)})
    import optomind_research.s2_intelligence_gateway as s2
    monkeypatch.setattr(s2, "S2IntelligenceGateway", lambda: gateway)
    assert cli.main(args + ["--run"]) == 0
    saved = json.loads((tmp_path / "CANDIDATE_CORPUS.json").read_text())
    assert saved["initial_retrieval"]["target_met"]
    # CLI output diagnostics must not invalidate the already published handoff.
    assert cli.main(args + ["--run"]) == 0
    assert len(gateway.calls) == 2


def test_cli_explicit_legacy_opt_out_calls_only_primitive(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("legacy_candidate_cli", repo / "scripts/upgrade3/run_candidate_corpus.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    source = tmp_path / "plan.json"
    source.write_text(json.dumps(plan()))
    gateway = Gateway({"original keywords": [1]})
    import optomind_research.s2_intelligence_gateway as s2
    monkeypatch.setattr(s2, "S2IntelligenceGateway", lambda: gateway)
    monkeypatch.setattr(ir, "execute_initial_m2", lambda **kw: pytest.fail("legacy route invoked supplementation runner"))
    assert cli.main(["--schema", str(source), "--out", str(tmp_path / "corpus.json"),
                     "--snippets", str(tmp_path / "text.sqlite"), "--no-initial-topup"]) == 0
    assert "initial_retrieval" not in json.loads((tmp_path / "corpus.json").read_text())
    assert len(gateway.calls) == 2


def test_unified_aliases_after_enrichment_preserve_prior_hit_identity(tmp_path):
    class AliasedGateway(Gateway):
        def search_papers(self, text, **kwargs):
            records, response = super().search_papers(text, **kwargs)
            for record in records:
                record.doi = "10.1/shared"
            return records, response
    gateway = AliasedGateway({"original keywords": [1, 2], "Original optical question?": [2]})
    corpus = build_candidate_corpus(plan(), gateway=gateway, enrich=True)
    assert corpus["statistics"]["distinct_papers"] == 1
    assert {h["paper_id"] for h in corpus["retrieval_hits"]} == {"P1"}
    assert corpus["query_audit"][1]["new_identities"] == 0


def test_legacy_primitive_and_real_client_profile_stay_valid(tmp_path):
    corpus = build_candidate_corpus(plan(), gateway=Gateway({"original keywords": [1]}), enrich=False)
    assert "initial_retrieval" not in corpus
    client = QwenDirectClient(**ir.PROFILE, key_file=tmp_path / "unused-key.txt")
    assert client.model == "qwen3.5-plus" and not client.json_mode
    with pytest.raises(ir.InitialRetrievalError, match="requires_key"):
        ir.make_topup_client_factory()(tmp_path)
