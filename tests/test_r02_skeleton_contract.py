"""Offline R02 contracts for canonical navigation, coverage and bound sidecars."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from optomind_research.runtime.upgrade3 import scholarly_skeleton as sk
from optomind_research.runtime.upgrade3.skeleton_edges import collect


def _hit(paper="p1", query="q1", facet="F1", rank=1, hit_id="H000001", **extra):
    return {"paper_id": paper, "query_id": query, "facet_id": facet,
            "query_type": "question", "result_rank": rank, "hit_id": hit_id, **extra}


def _corpus():
    return {"run_id": "run-1", "plan_hash": "plan-1", "research_object": "optical networks",
            "facets": [{"id": "F1", "ask": "What improves optical networks?"},
                       {"id": "F2", "ask": "What limits optical networks?"}],
            "expected_queries": [{"query_id": "q1", "facet_id": "F1"},
                                 {"query_id": "q2", "facet_id": "F1"},
                                 {"query_id": "q3", "facet_id": "F2"}],
            "papers": [{"paper_id": "p1", "title": "Optical networks with learned transformations",
                        "year": 2023, "abstract": "Optical networks improve the measured optical signal."}],
            "retrieval_hits": [_hit()]}


def _store(path, text="An optical passage."):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE snippet_text (hit_id TEXT PRIMARY KEY, raw_snippet_text TEXT)")
        connection.execute("INSERT INTO snippet_text VALUES (?, ?)", ("H000001", text))
    return hashlib.sha256(text.encode()).hexdigest()


def _bundle(corpus, path):
    corpus["bundle"] = {"run_id": corpus["run_id"], "plan_hash": corpus["plan_hash"],
                        "artifacts": {"snippet_store": {
                            "path": str(path), "sha256": sk._file_hash(path),
                            "run_id": corpus["run_id"], "plan_hash": corpus["plan_hash"]}}}


def test_full_denominator_and_independent_query_rrf():
    corpus = _corpus()
    corpus["retrieval_hits"] += [_hit(rank=2, hit_id="H000002"), _hit(rank=10, hit_id="H000003")]
    ff = sk.facet_features(corpus, {})
    row = ff["features"]["p1"]["F1"]
    assert ff["facets"] == ["F1", "F2"]
    assert row["query_coverage"] == 0.5
    assert row["snippet_hit_count"] == 3
    assert row["RRF_score"] == round(1 / 61, 6)
    assert ff["coverage"]["F2"]["expected_queries"] == 1
    assert ff["coverage"]["F2"]["retrieval_coverage"] == 0
    coverage = sk._coverage_with_reading(ff, {})
    assert coverage["retrieval_facet_coverage"] == 0.5
    assert coverage["read_facet_coverage"] == coverage["evidence_sufficient_facet_coverage"] == 0
    read = sk._coverage_with_reading(ff, {"p1": {"read_confirmed": True, "source_validated": True}})
    assert read["read_facet_coverage"] == 0.5
    assert read["evidence_sufficient_facet_coverage"] == 0


def test_legacy_query_audit_keeps_zero_hit_query():
    corpus = _corpus()
    corpus["query_audit"] = corpus.pop("expected_queries")
    assert sk.facet_features(corpus, {})["features"]["p1"]["F1"]["query_coverage"] == 0.5


def test_canonical_view_merges_hits_edges_metadata_vectors_and_reading():
    corpus = _corpus()
    corpus["papers"][0]["doi"] = "10.123/example"
    corpus["papers"].append({"paper_id": "p2", "doi": "10.123/example", "abstract": "Alias abstract"})
    corpus["work_identity"] = {"alias_to_canonical": {"old": "p2", "s2:old": "p2"}}
    corpus["retrieval_hits"] += [_hit(paper="old", query="q2", hit_id="H000002")]
    raw = {"old": {"source_paper": "old", "references": [{"paper_id": "outside"}], "citations": []}}
    view, edges, identity = sk.canonical_view(corpus, raw)
    assert [p["paper_id"] for p in view["papers"]] == ["p1"]
    assert {h["paper_id"] for h in view["retrieval_hits"]} == {"p1"}
    assert view["retrieval_hits"][1]["provider_paper_id"] == "old"
    assert set(edges) == {"p1"}
    ff = sk.facet_features(view, {})
    assert ff["features"]["p1"]["F1"]["query_coverage"] == 1
    assert sk._canonical_mapping({"old": {"abstract": "Alias"}}, identity["alias"]) == {"p1": {"abstract": "Alias"}}
    assert set(sk._canonical_mapping({"old": np.array([1., 0.])}, identity["alias"])) == {"p1"}
    assert set(sk._canonical_mapping({"old": {"label": "directly_relevant"}}, identity["alias"])) == {"p1"}
    pytest.importorskip("igraph")
    edge_set = sk.build_layers(edges, view, ff, sk.load_config())
    graphs = sk.build_graphs(edge_set, corpus, sk.load_config())
    assert graphs["core_ids"] == {"p1"}
    assert "p2" not in graphs["nodes"] and "old" not in graphs["nodes"]


def test_metadata_survives_without_vectors_and_abstract_fallback(tmp_path):
    (tmp_path / "NODE_META.json").write_text(json.dumps({"p1": {"abstract": "Available abstract"}}))
    meta, vectors = sk._load_enrichment(tmp_path)
    assert meta["p1"]["abstract"] == "Available abstract"
    assert vectors == {}
    assert sk._meta_from(_corpus(), {})["p1"]["abstract"].startswith("Optical networks")


def test_snippet_store_resolves_corpus_bundle_and_checks_text_hash(tmp_path):
    source, output = tmp_path / "QP02", tmp_path / "SKELETON"
    source.mkdir(); output.mkdir()
    path = source / "SNIPPET_TEXT.sqlite"
    corpus = _corpus()
    corpus["retrieval_hits"][0]["text_hash"] = _store(path)
    _bundle(corpus, path)
    corpus["bundle"]["artifacts"]["snippet_store"]["path"] = path.name
    corpus["_bundle_base"] = str(source)
    assert sk._snippet_text_by_paper(output, corpus) == {"p1": "An optical passage."}
    corpus["retrieval_hits"][0]["text_hash"] = "wrong-hash"
    assert sk._snippet_text_by_paper(output, corpus) == {}


def test_wrong_run_or_modified_snippet_store_is_rejected(tmp_path):
    path = tmp_path / "SNIPPET_TEXT.sqlite"
    corpus = _corpus()
    corpus["retrieval_hits"][0]["text_hash"] = _store(path)
    _bundle(corpus, path)
    descriptor = corpus["bundle"]["artifacts"]["snippet_store"]
    descriptor["run_id"] = "wrong-run"
    with pytest.raises(sk.ArtifactBindingError, match="run_id"):
        sk._snippet_text_by_paper(tmp_path, corpus)
    descriptor["run_id"] = corpus["run_id"]
    descriptor["sha256"] = "wrong"
    with pytest.raises(sk.ArtifactBindingError, match="sha256"):
        sk._snippet_text_by_paper(tmp_path, corpus)


def test_legacy_snippet_id_alone_cannot_adopt_wrong_text(tmp_path):
    _store(tmp_path / "SNIPPET_TEXT.sqlite")
    assert sk._snippet_text_by_paper(tmp_path, _corpus()) == {}


class _Gateway:
    def __init__(self):
        self.calls = []
        self.fail = True

    def _paper_edges(self, paper_id, *, relation, limit):
        self.calls.append((paper_id, relation))
        ok = relation == "references" or not self.fail
        rows = [] if not ok else [{"citingPaper" if relation == "citations" else "citedPaper":
                                  {"paperId": "outside", "title": "An outside work"}}]
        return rows, SimpleNamespace(ok=ok, status_code=200 if ok else 429,
                                     status_category="success" if ok else "rate_limited",
                                     error="" if ok else "429", audit={}, payload={})


def test_relation_resume_retries_only_failures(tmp_path):
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(_corpus()))
    gateway = _Gateway()
    collect(corpus_path, tmp_path, gateway=gateway, max_workers=1)
    assert gateway.calls == [("p1", "references"), ("p1", "citations")]
    gateway.fail = False
    collect(corpus_path, tmp_path, gateway=gateway, max_workers=1)
    assert gateway.calls[-1] == ("p1", "citations") and len(gateway.calls) == 3
    collect(corpus_path, tmp_path, gateway=gateway, max_workers=1)
    assert len(gateway.calls) == 3
    row = sk.load_raw_edges(tmp_path / "GRAPH_EDGES.jsonl")["p1"]
    assert row["relation_status"]["references"]["attempts"] == 1
    assert row["relation_status"]["citations"]["attempts"] == 2
    assert row["references"] and row["citations"]


@pytest.mark.parametrize("legacy", [False, True])
def test_tiny_complete_offline_skeleton_and_manifest(tmp_path, legacy):
    pytest.importorskip("igraph"); pytest.importorskip("leidenalg")
    corpus_path, edge_path = tmp_path / "corpus.json", tmp_path / "edges.jsonl"
    corpus_path.write_text(json.dumps(_corpus()))
    edge_path.write_text("")
    output = tmp_path / "output"
    if legacy:
        skeleton = sk.run_skeleton(corpus_path, edge_path, output, sk.load_config(), dump_edges=False)
    else:
        skeleton, _, _, _ = sk.run_skeleton_v2(corpus_path, edge_path, output, sk.load_config(), dump_edges=False)
    assert skeleton["facets"] == ["F1", "F2"]
    assert skeleton["audit"]["graph_nodes"] == 1
    assert skeleton["coverage"]["retrieval_facet_coverage"] == 0.5
    bundle = json.loads((output / "SKELETON_BUNDLE.json").read_text())
    assert bundle["run_id"] == "run-1"
    assert bundle["artifacts"]["vectors"] is None
    assert bundle["artifacts"]["skeleton"]["sha256"] == sk._file_hash(output / "SCHOLARLY_SKELETON.json")
    assert (output / "READING_NAVIGATION_MANIFEST.json").exists()


def test_internal_database_binding_rejects_relabelled_wrong_run(tmp_path):
    path = tmp_path / "SNIPPET_TEXT.sqlite"
    corpus = _corpus()
    corpus["retrieval_hits"][0]["text_hash"] = _store(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE run_binding (run_id TEXT, plan_hash TEXT)")
        connection.execute("INSERT INTO run_binding VALUES ('other-run', 'plan-1')")
    _bundle(corpus, path)  # Even a freshly recomputed file hash cannot rename a run.
    with pytest.raises(sk.ArtifactBindingError, match="database run binding"):
        sk._snippet_text_by_paper(tmp_path, corpus)


def test_explicit_empty_query_universe_does_not_invent_denominator():
    corpus = _corpus()
    corpus["expected_queries"] = []
    ff = sk.facet_features(corpus, {})
    assert ff["features"]["p1"]["F1"]["query_coverage"] == 0
    assert ff["queries_per_facet"] == {"F1": [], "F2": []}


def test_edges_from_another_run_are_not_used(tmp_path):
    path = tmp_path / "edges.jsonl"
    path.write_text(json.dumps({"source_paper": "p1", "run_id": "other-run", "references": [{"paper_id": "wrong"}]}) + "\n")
    assert sk.load_raw_edges(path, run_id="run-1") == {}


def test_empty_corpus_remains_complete_offline(tmp_path):
    pytest.importorskip("igraph"); pytest.importorskip("leidenalg")
    corpus = _corpus()
    corpus["papers"] = []; corpus["retrieval_hits"] = []
    corpus_path, edge_path = tmp_path / "corpus.json", tmp_path / "edges.jsonl"
    corpus_path.write_text(json.dumps(corpus)); edge_path.write_text("")
    skeleton, _, _, _ = sk.run_skeleton_v2(corpus_path, edge_path, tmp_path / "out", sk.load_config(), dump_edges=False)
    assert skeleton["facets"] == ["F1", "F2"]
    assert skeleton["coverage"]["retrieval_facet_coverage"] == 0
    assert skeleton["audit"]["graph_nodes"] == 0


def test_full_canonical_sidecar_pipeline_with_parquet(tmp_path):
    pytest.importorskip("igraph"); pytest.importorskip("leidenalg"); pytest.importorskip("pyarrow")
    source, output = tmp_path / "QP02", tmp_path / "SKELETON"
    source.mkdir(); output.mkdir()
    corpus = _corpus()
    corpus["papers"][0]["doi"] = "10.123/shared"
    corpus["papers"].append({"paper_id": "p2", "doi": "10.123/shared", "title": "Duplicate identity"})
    corpus["retrieval_hits"][0]["paper_id"] = "p2"
    snippet_path = source / "SNIPPET_TEXT.sqlite"
    corpus["retrieval_hits"][0]["text_hash"] = _store(snippet_path)
    _bundle(corpus, snippet_path)
    (output / "NODE_META.json").write_text(json.dumps({"p2": {"abstract": "Optical networks improve the measured optical signal."}}))
    (output / "READING.json").write_text(json.dumps({"judgements": {"p2": {"label": "directly_relevant", "evidence": "optical passage", "read_confirmed": True, "source_validated": True}}}))
    corpus_path, edge_path = source / "CANDIDATE_CORPUS.json", output / "GRAPH_EDGES.jsonl"
    corpus_path.write_text(json.dumps(corpus)); edge_path.write_text("")
    skeleton, _, cards, _ = sk.run_skeleton_v2(corpus_path, edge_path, output, sk.load_config())
    assert skeleton["audit"]["graph_nodes"] == 1
    assert set(skeleton["paper_facet_features"]) == {"p1"}
    assert skeleton["coverage"]["read_facet_coverage"] == 0  # Routing labels are not verified card reads.
    assert skeleton["coverage"]["evidence_sufficient_facet_coverage"] == 0
    assert (output / "PAPER_FACET_FEATURES.parquet").exists()
    bundle = json.loads((output / "SKELETON_BUNDLE.json").read_text())
    assert bundle["artifacts"]["snippet_store"]["path"] == str(snippet_path)
    assert bundle["artifacts"]["metadata"] is not None and bundle["artifacts"]["vectors"] is None
    domain = json.loads((output / "DOMAIN_RELEVANCE.json").read_text())
    assert set(domain) == {"p1"}


def test_selection_does_not_claim_unhit_queries():
    ff = sk.facet_features(_corpus(), {})
    dimensions = sk._paper_dimensions("p1", {}, {}, {}, {}, ff, {}, {"core_ids": {"p1"}})
    assert "query:q1" in dimensions
    assert "query:q2" not in dimensions and "query:q3" not in dimensions


def test_zero_edge_success_is_cached_but_partial_edge_success_retries(tmp_path):
    class EmptyGateway:
        def __init__(self):
            self.calls = []
            self.partial = True
        def _paper_edges(self, paper_id, *, relation, limit):
            self.calls.append(relation)
            partial = self.partial and relation == "citations"
            return [], SimpleNamespace(ok=True, status_code=200,
                status_category="partial_availability" if partial else "success",
                error="", audit={}, payload={})
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(_corpus()))
    gateway = EmptyGateway()
    collect(corpus_path, tmp_path, gateway=gateway, max_workers=1)
    gateway.partial = False
    collect(corpus_path, tmp_path, gateway=gateway, max_workers=1)
    collect(corpus_path, tmp_path, gateway=gateway, max_workers=1)
    assert gateway.calls == ["references", "citations", "citations"]
    row = sk.load_raw_edges(tmp_path / "GRAPH_EDGES.jsonl")["p1"]
    assert row["references"] == row["citations"] == []
    assert row["relation_status"]["references"]["state"] == "success"


def test_bound_edges_cannot_be_overridden_by_unrelated_file(tmp_path):
    declared, other = tmp_path / "declared.jsonl", tmp_path / "other.jsonl"
    declared.write_text(""); other.write_text("")
    corpus = _corpus()
    corpus["bundle"] = {"artifacts": {"edges": {"path": str(declared), "sha256": sk._file_hash(declared)}}}
    assert sk._read_bound_edges(corpus, declared, tmp_path) == {}
    with pytest.raises(sk.ArtifactBindingError, match="edge artifact differs"):
        sk._read_bound_edges(corpus, other, tmp_path)
