from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from optomind_research.s2_cache import S2PersistentCache
from optomind_research.s2_candidate_ranker import S2CandidateRanker
from optomind_research.s2_discovery import (
    S2DiscoveryPortfolioBuilder,
    ScholarFacetRequest,
)
from optomind_research.s2_intelligence_gateway import (
    S2GatewayResponse,
    S2IntelligenceGateway,
    S2Transport,
    classify_oa_candidate_from_headers,
)
from optomind_research.s2_key_router import S2KeyRouter
from optomind_research.s2_schemas import parse_paper_record


@pytest.fixture(autouse=True)
def _reset_process_wide_lanes() -> None:
    S2KeyRouter.reset_process_lanes()
    yield


class _FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_parse_paper_record_preserves_rich_s2_fields() -> None:
    rec = parse_paper_record(
        {
            "paperId": "s2-paper",
            "corpusId": 123,
            "title": "Optical metasurface review",
            "abstract": "A sufficiently detailed abstract.",
            "authors": [{"name": "A. Researcher"}],
            "year": 2025,
            "externalIds": {"DOI": "10.1000/example"},
            "citationCount": 42,
            "influentialCitationCount": 7,
            "referenceCount": 88,
            "isOpenAccess": True,
            "openAccessPdf": {
                "url": "https://example.org/paper",
                "status": "GOLD",
                "license": "CCBY",
            },
            "tldr": {"text": "A concise model-generated overview."},
            "embedding": {"model": "specter_v2", "vector": [0.1, 0.2]},
            "publicationTypes": ["Review"],
            "textAvailability": "fulltext",
            "citationStyles": {"bibtex": "@article{example}"},
        }
    )
    assert rec.paper_id == "s2-paper"
    assert rec.corpus_id == 123
    assert rec.doi == "10.1000/example"
    assert rec.tldr.startswith("A concise")
    assert rec.specter2_vector == [0.1, 0.2]
    assert rec.s2_open_access_candidate_url == "https://example.org/paper"
    assert rec.is_oa is True
    assert rec.raw_metadata["citationCount"] == 42


def test_persistent_cache_survives_new_instance(tmp_path: Path) -> None:
    path = tmp_path / "s2.sqlite"
    cache = S2PersistentCache(path)
    cache.put(
        "GET",
        "/graph/v1/paper/search",
        {"query": "metasurface"},
        None,
        status_code=200,
        payload={"data": [{"paperId": "p1"}]},
        ttl_seconds=3600,
    )
    reopened = S2PersistentCache(path)
    result = reopened.get(
        "GET", "/graph/v1/paper/search", {"query": "metasurface"}, None
    )
    assert result.hit
    assert result.payload["data"][0]["paperId"] == "p1"


def test_transport_uses_cache_on_second_identical_request(tmp_path: Path) -> None:
    calls: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        calls.append(request.full_url)
        return _FakeResponse({"data": [{"paperId": "p1"}]})

    transport = S2Transport(
        keys=["secret"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
    )
    first = transport.request_json(
        "GET",
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={"query": "BIC"},
    )
    second = transport.request_json(
        "GET",
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={"query": "BIC"},
    )
    assert first.ok and not first.cache_hit
    assert second.ok and second.cache_hit
    assert len(calls) == 1


def test_transport_cools_429_key_and_retries_another_without_quality_failure(
    tmp_path: Path,
) -> None:
    calls = 0
    waits: list[float] = []
    keys_seen: list[str] = []

    def opener(request: Any, **__: Any) -> _FakeResponse:
        nonlocal calls
        calls += 1
        keys_seen.append(request.headers.get("X-api-key", ""))
        if calls == 1:
            raise urllib.error.HTTPError(
                "https://example.test",
                429,
                "Too Many Requests",
                {"Retry-After": "2"},
                io.BytesIO(b"rate limited"),
            )
        return _FakeResponse({"ok": True})

    transport = S2Transport(
        keys=["k1", "k2"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=waits.append,
        min_interval_seconds=0,
        max_attempts=3,
    )
    result = transport.request_json("GET", "https://example.test/value")
    assert result.ok
    assert result.retry_count == 1
    assert calls == 2
    assert keys_seen == ["k1", "k2"]
    # The cooldown is not slept when another lane is available, so the
    # affected key is isolated rather than blocking the retry.
    assert result.wait_seconds < 2
    assert waits == []
    # The cooled lane stays unavailable for the next request.
    keys_seen.clear()
    transport.request_json("GET", "https://example.test/next")
    assert keys_seen == ["k2"]


def test_transport_rotates_key_only_after_authentication_failure(
    tmp_path: Path,
) -> None:
    keys_seen: list[str] = []

    def opener(request: Any, **__: Any) -> _FakeResponse:
        keys_seen.append(request.headers.get("X-api-key", ""))
        if len(keys_seen) == 1:
            raise urllib.error.HTTPError(
                "https://example.test",
                401,
                "Unauthorized",
                {},
                io.BytesIO(b"invalid key"),
            )
        return _FakeResponse({"ok": True})

    transport = S2Transport(
        keys=["k1", "k2"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=3,
    )
    result = transport.request_json("GET", "https://example.test/auth")
    assert result.ok
    assert keys_seen == ["k1", "k2"]


def test_transports_share_one_cross_endpoint_rate_gate(tmp_path: Path) -> None:
    waits: list[float] = []
    path = tmp_path / "shared.sqlite"

    def opener(_: Any, **__: Any) -> _FakeResponse:
        return _FakeResponse({"ok": True})

    first = S2Transport(
        keys=["k1"],
        cache_path=path,
        opener=opener,
        sleep_fn=waits.append,
        min_interval_seconds=1.1,
    )
    second = S2Transport(
        keys=["k1"],
        cache_path=path,
        opener=opener,
        sleep_fn=waits.append,
        min_interval_seconds=1.1,
    )
    assert first.request_json(
        "GET",
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={"query": "BIC"},
    ).ok
    assert second.request_json(
        "GET",
        "https://api.semanticscholar.org/recommendations/v1/papers/forpaper/p1",
    ).ok
    # SQLite setup and the first fake request consume a small amount of wall
    # time before the second transport reserves its slot.
    assert sum(waits) >= 0.8


def test_gateway_batch_normalizes_records(tmp_path: Path) -> None:
    class StubTransport:
        def request_json(self, method: str, url: str, **_: Any) -> S2GatewayResponse:
            assert method == "POST"
            assert url.endswith("/paper/batch")
            return S2GatewayResponse(
                ok=True,
                status_code=200,
                payload=[
                    {
                        "paperId": "p1",
                        "corpusId": 11,
                        "title": "Photonic bound states",
                        "authors": [],
                    }
                ],
            )

    gateway = S2IntelligenceGateway(transport=StubTransport())  # type: ignore[arg-type]
    records, response = gateway.batch_papers(["p1"])
    assert response.ok
    assert len(records) == 1
    assert records[0].title == "Photonic bound states"


def test_snippet_search_batches_long_paper_id_filter_without_dropping_ids() -> None:
    requested_ids = [f"{index:040x}" for index in range(130)]
    observed_batches: list[list[str]] = []
    observed_url_lengths: list[int] = []

    class StubTransport:
        def request_json(
            self, method: str, url: str, *, params: dict[str, Any], **_: Any
        ) -> S2GatewayResponse:
            assert method == "GET"
            assert url.endswith("/snippet/search")
            batch = str(params.get("paperIds") or "").split(",")
            observed_batches.append(batch)
            observed_url_lengths.append(
                len(url + "?" + __import__("urllib").parse.urlencode(params))
            )
            return S2GatewayResponse(
                ok=True,
                status_code=200,
                status_category="ok",
                payload={
                    "data": [
                        {"paper": {"paperId": paper_id}, "snippet": {"text": paper_id}}
                        for paper_id in batch
                    ]
                },
            )

    gateway = S2IntelligenceGateway(transport=StubTransport())  # type: ignore[arg-type]
    rows, response = gateway.search_snippets(
        "nanophotonic inverse design", paper_ids=requested_ids, limit=300
    )

    assert len(observed_batches) > 1
    assert [item for batch in observed_batches for item in batch] == requested_ids
    assert all(length <= 3500 for length in observed_url_lengths)
    assert [row["paper"]["paperId"] for row in rows] == requested_ids
    assert response.ok is True
    assert response.audit["requested_paper_id_count"] == 130
    assert response.audit["paper_id_batch_count"] == len(observed_batches)


def test_oa_candidate_is_typed_from_content_not_url_suffix() -> None:
    pdf = classify_oa_candidate_from_headers(
        url="https://publisher.example/article",
        content_type="application/pdf",
        first_bytes=b"%PDF-1.7",
    )
    html = classify_oa_candidate_from_headers(
        url="https://repository.example/document.pdf",
        content_type="text/html; charset=utf-8",
        first_bytes=b"<html>",
    )
    assert pdf["detected_kind"] == "pdf"
    assert pdf["is_direct_fulltext_candidate"] is True
    assert html["detected_kind"] == "html_requires_structure_check"
    assert html["is_direct_fulltext_candidate"] is False


def test_candidate_matrix_preserves_landmark_review_frontier_and_oa_channels() -> None:
    ranker = S2CandidateRanker(current_year=2026)
    query = ["inverse design optical multilayer thin films"]
    roles = ["foundation", "review", "frontier", "method"]
    papers = [
        parse_paper_record(
            {
                "paperId": "landmark",
                "title": "Fundamental theory of optical multilayer design",
                "year": 1990,
                "citationCount": 2200,
                "authors": [],
            }
        ),
        parse_paper_record(
            {
                "paperId": "review",
                "title": "A review of inverse design for optical thin films",
                "year": 2024,
                "citationCount": 12,
                "publicationTypes": ["Review"],
                "authors": [],
            }
        ),
        parse_paper_record(
            {
                "paperId": "frontier",
                "title": "Data-driven inverse design of multilayer photonic structures",
                "year": 2026,
                "citationCount": 1,
                "isOpenAccess": True,
                "openAccessPdf": {"url": "https://example.org/frontier.pdf"},
                "authors": [],
            }
        ),
    ]
    candidates = [
        ranker.build_candidate(
            paper,
            facet_id="facet_design",
            queries=query,
            requested_roles=roles,
            discovery_channel="s2_relevance_search",
        )
        for paper in papers
    ]
    pools = {item.paper.paper_id: set(item.assigned_pools) for item in candidates}
    assert "citation_landmark_pool" in pools["landmark"]
    assert "review_perspective_pool" in pools["review"]
    assert "recent_frontier_pool" in pools["frontier"]
    assert "oa_fulltext_candidate_pool" in pools["frontier"]
    assert all(item.decision == "retain" for item in candidates)


def test_discovery_portfolio_merges_duplicate_papers_without_losing_roles() -> None:
    record = parse_paper_record(
        {
            "paperId": "shared",
            "title": "Optical bound states in the continuum",
            "abstract": "Mechanisms and applications of photonic bound states.",
            "year": 2025,
            "authors": [],
        }
    )

    class StubGateway:
        def search_papers(self, query: str, **_: Any):
            return [record], S2GatewayResponse(
                ok=True, status_code=200, status_category="ok"
            )

    builder = S2DiscoveryPortfolioBuilder(gateway=StubGateway())  # type: ignore[arg-type]
    portfolio = builder.discover(
        [
            ScholarFacetRequest(
                facet_id="mechanism",
                queries=["BIC mechanism", "bound state radiation coupling"],
                requested_roles=["mechanism"],
            )
        ]
    )
    assert len(portfolio.candidates) == 1
    candidate = portfolio.candidates[0]
    assert candidate.query_texts == [
        "BIC mechanism",
        "bound state radiation coupling",
    ]
    assert len(candidate.discovery_channels) == 1
    assert len(portfolio.query_runs) == 2


def test_mixed_facets_apply_direct_only_per_facet() -> None:
    seen: list[str] = []

    class StubGateway:
        def search_papers(self, query: str, *, limit: int, open_access_pdf: bool = False):
            seen.append(query)
            return [], S2GatewayResponse(
                ok=True, status_code=200, status_category="ok"
            )

    direct_query = "measured cooling power multilayer inverse design"
    broad_query = "fabrication tolerance radiative cooling multilayer"
    builder = S2DiscoveryPortfolioBuilder(gateway=StubGateway())  # type: ignore[arg-type]
    builder.discover(
        [
            ScholarFacetRequest(
                facet_id="direct",
                queries=[direct_query],
                requested_roles=["review", "foundation"],
                max_results_per_query=5,
                direct_only=True,
            ),
            ScholarFacetRequest(
                facet_id="broad",
                queries=[broad_query],
                requested_roles=["review", "foundation"],
                max_results_per_query=5,
                direct_only=False,
            ),
        ]
    )
    assert seen == [
        direct_query,
        broad_query,
        f"{broad_query} review perspective roadmap",
        f"{broad_query} fundamental theory origin",
    ]


def test_transport_can_isolate_cache_for_cold_environment_smoke(
    monkeypatch,
    tmp_path: Path,
) -> None:
    isolated_cache = tmp_path / "cold-smoke" / "s2-cache.sqlite"
    monkeypatch.setenv("OPTOMIND_S2_CACHE_PATH", str(isolated_cache))

    transport = S2Transport(keys=[])

    assert transport.cache.path == isolated_cache
