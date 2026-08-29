from __future__ import annotations

from optomind_research.runtime.material_semantic_cache import MaterialSemanticCache, dashscope_embedder
from optomind_research.runtime.material_unit_store import material_unit_from_text_chunk


def _unit(chunk_id: str, text: str) -> dict:
    return material_unit_from_text_chunk({"paper_id": "p", "chunk_id": chunk_id, "title": "Paper", "text": text})


def test_cache_resumes_and_searches_local_cosine(tmp_path) -> None:
    units = [_unit("c1", "method solver"), _unit("c2", "fabrication tolerance")]
    calls = []

    def embedder(texts):
        calls.extend(texts)
        return [[1.0, 0.0] if "method" in t else [0.0, 1.0] for t in texts]

    path = tmp_path / "vectors.sqlite"
    with MaterialSemanticCache(path) as cache:
        assert cache.ensure_units(units, embedder, batch_size=1)["embedded"] == 2
        assert cache.ensure_units(units, embedder)["reused"] == 2
        assert len(calls) == 2
        results = cache.search([0.99, 0.05], top_k=1)
        assert results[0]["unit_id"] == units[0]["unit_id"]
        assert results[0]["score"] > 0.99


def test_cache_model_and_representation_versions_are_isolated(tmp_path) -> None:
    unit = _unit("c1", "method solver")
    calls = []

    def embedder(texts):
        calls.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    with MaterialSemanticCache(tmp_path / "vectors.sqlite") as cache:
        cache.ensure_units([unit], embedder, embedding_model="model-a", representation_version="v1")
        cache.ensure_units([unit], embedder, embedding_model="model-b", representation_version="v1")
        cache.ensure_units([unit], embedder, embedding_model="model-a", representation_version="v2")
        assert len(calls) == 3
        assert cache.count() == 3


def test_search_many_reads_shared_corpus_and_preserves_query_order(tmp_path) -> None:
    units = [_unit("c1", "method solver"), _unit("c2", "fabrication tolerance")]

    with MaterialSemanticCache(tmp_path / "vectors.sqlite") as cache:
        cache.put(units[0], [1.0, 0.0])
        cache.put(units[1], [0.0, 1.0])
        results = cache.search_many(
            [[0.99, 0.01], [0.01, 0.99]],
            top_k=1,
        )

    assert results[0][0]["unit_id"] == units[0]["unit_id"]
    assert results[1][0]["unit_id"] == units[1]["unit_id"]

    with MaterialSemanticCache(
        tmp_path / "vectors.sqlite",
        readonly=True,
    ) as cache:
        assert cache.count() == 2


def test_dashscope_embedder_orders_vectors_and_batches(monkeypatch) -> None:
    import json
    import urllib.request

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.payload

    requests = []

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        requests.append(body)
        rows = [
            {"index": index, "embedding": [float(index + 1), 0.0]}
            for index in range(len(body["input"]))
        ]
        rows.reverse()
        return Response(json.dumps({"data": rows}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = dashscope_embedder(["a", "b", "c"], api_key="test", batch_size=2)

    assert len(requests) == 2
    assert result == [[1.0, 0.0], [2.0, 0.0], [1.0, 0.0]]
