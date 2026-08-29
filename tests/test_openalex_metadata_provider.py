"""Network-free focused tests for the OpenAlex final-metadata adapter.

The live backend is replaced with an in-memory fake, so these tests exercise
DOI lookup, guarded title lookup, and request validation without network I/O.
"""

from __future__ import annotations

from typing import Any

import pytest

import tools.academic_backends.openalex_backend as openalex_backend

from optomind_research.runtime.openalex_metadata_provider import (
    make_default_openalex_provider,
    normalize_openalex_record,
)


class FakeOpenAlexBackend:
    """Minimal stand-in for the shared OpenAlex backend."""

    def __init__(
        self,
        *,
        doi_record: dict[str, Any] | None = None,
        search_records: list[dict[str, Any]] | None = None,
    ) -> None:
        self.doi_record = doi_record
        self.search_records = search_records or []
        self.calls: list[tuple[str, Any]] = []

    def get_work(self, identifier: str) -> dict[str, Any] | None:
        self.calls.append(("doi", identifier))
        return self.doi_record

    def search(
        self,
        query: str,
        max_results: int = 10,
        from_year: int | None = None,
        sort: str = "relevance_score:desc",
    ) -> list[dict[str, Any]]:
        self.calls.append(("title", query, max_results))
        return list(self.search_records[:max_results])


def _backend_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "title": "OpenAlex Paper",
        "authors": ["A. Author", "B. Author"],
        "year": 2024,
        "doi": "10.1000/openalex",
        "url_or_doi": "https://doi.org/10.1000/openalex",
        "journal_or_venue": "OpenAlex Journal",
        "source_url": "https://doi.org/10.1000/openalex",
        "openalex_id": "W1234567890",
    }
    row.update(overrides)
    return row


def test_normalize_openalex_record_maps_backend_fields():
    normalized = normalize_openalex_record(_backend_row())
    assert normalized == {
        "title": "OpenAlex Paper",
        "authors": ["A. Author", "B. Author"],
        "year": "2024",
        "venue": "OpenAlex Journal",
        "doi": "10.1000/openalex",
        "url": "https://doi.org/10.1000/openalex",
        "openalex_id": "W1234567890",
    }


def test_default_provider_doi_lookup_is_network_free(monkeypatch):
    fake = FakeOpenAlexBackend(doi_record=_backend_row())
    monkeypatch.setattr(
        openalex_backend,
        "OpenAlexBackend",
        lambda **kwargs: fake,
    )

    result = make_default_openalex_provider()(
        {"kind": "doi", "value": "10.1000/openalex"}
    )

    assert result is not None
    assert result["title"] == "OpenAlex Paper"
    assert result["venue"] == "OpenAlex Journal"
    assert result["doi"] == "10.1000/openalex"
    assert fake.calls == [("doi", "10.1000/openalex")]


def test_default_provider_title_lookup_accepts_normalized_match(monkeypatch):
    fake = FakeOpenAlexBackend(
        search_records=[
            _backend_row(title="physics-informed  neural networks")
        ]
    )
    monkeypatch.setattr(
        openalex_backend,
        "OpenAlexBackend",
        lambda **kwargs: fake,
    )

    result = make_default_openalex_provider()(
        {
            "kind": "title",
            "value": "Physics-Informed Neural Networks!",
        }
    )

    assert result is not None
    assert result["title"] == "physics-informed  neural networks"
    assert fake.calls == [
        ("title", "Physics-Informed Neural Networks!", 1)
    ]


def test_default_provider_title_lookup_rejects_unrelated_top_hit(monkeypatch):
    fake = FakeOpenAlexBackend(
        search_records=[_backend_row(title="Unrelated Paper")]
    )
    monkeypatch.setattr(
        openalex_backend,
        "OpenAlexBackend",
        lambda **kwargs: fake,
    )

    result = make_default_openalex_provider()(
        {"kind": "title", "value": "Requested Paper Title"}
    )

    assert result is None


def test_default_provider_rejects_unknown_kind_without_network(monkeypatch):
    fake = FakeOpenAlexBackend(doi_record=_backend_row())
    monkeypatch.setattr(
        openalex_backend,
        "OpenAlexBackend",
        lambda **kwargs: fake,
    )

    result = make_default_openalex_provider()(
        {"kind": "query", "value": "not supported"}
    )

    assert result is None
    assert fake.calls == []


@pytest.mark.parametrize("missing_request", [{"kind": "doi"}, {"value": "x"}])
def test_default_provider_requires_kind_and_value(monkeypatch, missing_request):
    fake = FakeOpenAlexBackend(doi_record=_backend_row())
    monkeypatch.setattr(
        openalex_backend,
        "OpenAlexBackend",
        lambda **kwargs: fake,
    )

    assert make_default_openalex_provider()(missing_request) is None
    assert fake.calls == []
