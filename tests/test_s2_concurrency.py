from __future__ import annotations

import threading
import time
from typing import Any

from optomind_research.s2_discovery import (
    S2DiscoveryPortfolioBuilder,
    ScholarFacetRequest,
)
from optomind_research.s2_intelligence_gateway import S2GatewayResponse
from optomind_research.s2_schemas import parse_paper_record
from optomind_research.s2_text_chunk_retriever import S2TextChunkRetriever


class _ConcurrentGateway:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.started = threading.Event()

    def _enter(self) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active >= 2:
                self.started.set()

    def _leave(self) -> None:
        with self.lock:
            self.active -= 1

    def search_papers(self, query: str, **_: Any) -> tuple[list[Any], S2GatewayResponse]:
        self._enter()
        try:
            self.started.wait(timeout=2)
            return [
                parse_paper_record({
                    "paperId": f"paper-{query}",
                    "title": f"Paper for {query}",
                    "abstract": "A sufficiently long abstract for testing.",
                    "year": 2024,
                })
            ], S2GatewayResponse(ok=True, status_code=200, status_category="ok")
        finally:
            self._leave()

    def search_snippets(self, query: str, **_: Any) -> tuple[list[dict[str, Any]], S2GatewayResponse]:
        self._enter()
        try:
            self.started.wait(timeout=2)
            return [{
                "score": 1.0,
                "paper": {"paperId": f"paper-{query}", "corpusId": 1, "title": query},
                "snippet": {
                    "text": (
                        f"{query} reports a structured optical mechanism with enough "
                        "context for a deterministic snippet test. " * 4
                    ),
                    "snippetKind": "body",
                    "section": "Methods",
                    "snippetOffset": {"start": 0, "end": 120},
                },
            }], S2GatewayResponse(ok=True, status_code=200, status_category="ok")
        finally:
            self._leave()


def test_discovery_queries_run_concurrently_and_keep_audit_order() -> None:
    gateway = _ConcurrentGateway()
    builder = S2DiscoveryPortfolioBuilder(gateway=gateway, max_workers=2)
    result = builder.discover([
        ScholarFacetRequest(
            facet_id="test",
            queries=["q1", "q2"],
            max_results_per_query=2,
        )
    ])
    assert gateway.max_active >= 2
    assert [run["query"] for run in result.query_runs] == ["q1", "q2"]
    assert all(run["concurrency_workers"] == 2 for run in result.query_runs)


def test_snippet_queries_run_concurrently_and_preserve_chunk_order() -> None:
    gateway = _ConcurrentGateway()
    retriever = S2TextChunkRetriever(
        gateway=gateway,
        min_chars=100,
        max_workers=2,
    )
    result = retriever.retrieve(["q1", "q2"], limit_per_query=2)
    assert gateway.max_active >= 2
    assert [run["query"] for run in result.query_runs] == ["q1", "q2"]
    assert all(run["concurrency_workers"] == 2 for run in result.query_runs)
    assert [chunk.query_links[0] for chunk in result.accepted_chunks] == ["q1", "q2"]
