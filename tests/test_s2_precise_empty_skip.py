"""Backend-fix ticket 1.1/1.2: zero-result precise lookups must not repeat.

Covers the delivery requirements (c) and (d): a candidate already known to
be empty in the S2 snippet index never reaches the gateway, and a freshly
confirmed zero result is persisted for later runs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from optomind_research.s2_cache import S2PersistentCache
from optomind_research.s2_intelligence_gateway import S2GatewayResponse
from optomind_research.s2_schemas import S2PaperRecord
from optomind_research.s2_text_chunk_retriever import S2TextChunkRetriever


class _CountingGateway:
    """Records snippet searches; returns an empty result set."""

    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.calls = 0
        self.queries: list[str] = []
        self._items = items or []
        self.cache = None  # replaced per-test with a persistent cache

    def search_snippets(
        self, query: str, **_: Any
    ) -> tuple[list[dict[str, Any]], S2GatewayResponse]:
        self.calls += 1
        self.queries.append(query)
        return self._items, S2GatewayResponse(
            ok=True,
            status_code=200,
            status_category="ok",
            cache_hit=False,
            endpoint="/graph/v1/snippet/search",
        )


def _gateway_with_cache(tmp_path: Path, **kwargs: Any):
    gateway = _CountingGateway(**kwargs)
    cache = S2PersistentCache(tmp_path / "s2_cache_test.sqlite")
    # The retriever walks gateway.transport.cache; expose the real cache so
    # the known-empty index is actually exercised end to end.
    gateway.transport = SimpleNamespace(cache=cache)
    return gateway, cache


def _paper(paper_id: str = "P1", **overrides: Any) -> S2PaperRecord:
    values: dict[str, Any] = {
        "paper_id": paper_id,
        "title": f"Passive radiative cooling study {paper_id}",
    }
    values.update(overrides)
    return S2PaperRecord(**values)


def test_known_empty_paper_never_reaches_the_gateway(tmp_path: Path) -> None:
    """(c) A persisted zero-result candidate is skipped before the request."""

    gateway, cache = _gateway_with_cache(tmp_path)
    retriever = S2TextChunkRetriever(gateway=gateway, min_chars=100)
    cache.record_precise_empty("P1", title="Passive radiative cooling study P1")

    result = retriever.retrieve_precise_missing_papers([_paper("P1")])

    assert gateway.calls == 0, "a known-empty paper must not be requested"
    skipped_rows = [
        run
        for run in result.query_runs
        if run.get("status_category") == "skipped_known_empty"
    ]
    assert len(skipped_rows) == 1
    assert skipped_rows[0]["target_paper_id"] == "P1"
    assert skipped_rows[0]["result_count"] == 0


def test_closed_access_without_open_pdf_is_skipped(tmp_path: Path) -> None:
    """Request-time signal: S2 itself declares no open full text exists."""

    gateway, _cache = _gateway_with_cache(tmp_path)
    retriever = S2TextChunkRetriever(gateway=gateway, min_chars=100)
    closed = _paper("P2", is_oa=False, s2_open_access_candidate_url="")
    open_paper = _paper("P3", is_oa=True)

    result = retriever.retrieve_precise_missing_papers([closed, open_paper])

    assert gateway.queries == [open_paper.title.strip()]
    assert gateway.calls == 1, "only the OA-positive paper may be requested"
    reasons = {
        run.get("status_category") for run in result.query_runs
    }
    assert "skipped_closed_no_open_pdf" in reasons


def test_unknown_oa_state_is_still_requested(tmp_path: Path) -> None:
    """A missing OA verdict must not silently drop coverage."""

    gateway, _cache = _gateway_with_cache(tmp_path)
    retriever = S2TextChunkRetriever(gateway=gateway, min_chars=100)
    unknown = _paper("P4", is_oa=None)

    retriever.retrieve_precise_missing_papers([unknown])

    assert gateway.calls == 1


def test_zero_result_response_is_persisted_for_later_runs(
    tmp_path: Path,
) -> None:
    """(d) A fresh empty answer lands in the shared known-empty index."""

    gateway, cache = _gateway_with_cache(tmp_path)
    retriever = S2TextChunkRetriever(gateway=gateway, min_chars=100)
    papers = [_paper("P9")]

    first = retriever.retrieve_precise_missing_papers(papers)
    assert gateway.calls == 1, "first sight must actually ask S2"
    rows = cache.precise_empty_confirmed_since(0.0)
    assert "P9" in rows, "zero-result response must be persisted"

    second = retriever.retrieve_precise_missing_papers(papers)
    assert gateway.calls == 1, "the next run must reuse the persisted fact"
    assert any(
        run.get("status_category") == "skipped_known_empty"
        for run in second.query_runs
    )


def test_nonempty_result_is_not_marked_empty(tmp_path: Path) -> None:
    """A paper that yields snippets must stay request-eligible."""

    snippet_item = {
        "score": 1.0,
        "paper": {"paperId": "P5", "corpusId": 7, "title": "T"},
        "snippet": {
            "text": (
                "A deterministic body snippet long enough for the quality "
                "contract. "
            )
            * 8,
            "snippetKind": "body",
            "section": "Methods",
            "snippetOffset": {"start": 0, "end": 200},
        },
    }
    gateway, cache = _gateway_with_cache(tmp_path, items=[snippet_item])
    retriever = S2TextChunkRetriever(gateway=gateway, min_chars=100)

    retriever.retrieve_precise_missing_papers([_paper("P5")])

    assert cache.precise_empty_confirmed_since(0.0) == set()


def test_labels_expose_both_concurrency_layers(tmp_path: Path) -> None:
    """Ticket 1.4: generic label tells the truth; layers are explicit."""

    snippet_item = {
        "score": 1.0,
        "paper": {"paperId": "P6", "corpusId": 8, "title": "T"},
        "snippet": {
            "text": "Deterministic body text for label checks. " * 10,
            "snippetKind": "body",
            "section": "Methods",
            "snippetOffset": {"start": 0, "end": 120},
        },
    }
    gateway, _cache = _gateway_with_cache(tmp_path, items=[snippet_item])
    retriever = S2TextChunkRetriever(gateway=gateway, min_chars=50, max_workers=3)
    papers = [_paper(f"P{i}") for i in range(6, 12)]

    result = retriever.retrieve_precise_missing_papers(
        papers, max_papers=len(papers)
    )

    precise_runs = [
        run
        for run in result.query_runs
        if run.get("query_category") == "precise_missing_paper"
        and run.get("request_index") is not None
    ]
    assert precise_runs, "expected at least one networked run row"
    for run in precise_runs:
        assert run["concurrency_workers"] >= 1
        assert run["concurrency_workers"] == run["paper_concurrency"]
        assert run["request_concurrency"] == 1
