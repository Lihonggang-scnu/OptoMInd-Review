from __future__ import annotations

import json

from optomind_research.runtime.final_citation_map import build_final_citation_map


def test_final_map_uses_final_text_not_stale_authoring_inventory(tmp_path):
    manuscript = tmp_path / "final.md"
    manuscript.write_text(
        "A [REF:p1] finding. A second [REF:p2] finding. Again [REF:p1].",
        encoding="utf-8",
    )
    intermediate = tmp_path / "authoring_map.json"
    intermediate.write_text(
        json.dumps(
            {
                "citations": [
                    {"paper_id": "p1", "doi": "10.1000/one", "trace_status": "verified"},
                    {"paper_id": "stale", "doi": "10.1000/stale", "trace_status": "verified"},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = build_final_citation_map(
        markdown_path=manuscript,
        output_path=tmp_path / "FINAL_CITATION_MAP.json",
        intermediate_map_path=intermediate,
    )

    assert result["citation_count"] == 2
    assert [row["paper_id"] for row in result["citations"]] == ["p1", "p2"]
    assert result["citations"][0]["citation_identity"] == "doi:10.1000/one"
    assert result["citations"][1]["trace_status"] == "final_text_only"


def test_final_map_keeps_distinct_missing_doi_title_year_fallbacks(tmp_path):
    manuscript = tmp_path / "final.md"
    manuscript.write_text("[REF:a] [REF:b]", encoding="utf-8")
    intermediate = tmp_path / "source.json"
    intermediate.write_text(
        json.dumps(
            {"citations": [
                {"paper_id": "a", "title": "Shared title", "year": 2020},
                {"paper_id": "b", "title": "Shared title", "year": 2021},
            ]}
        ),
        encoding="utf-8",
    )

    result = build_final_citation_map(
        markdown_path=manuscript,
        output_path=tmp_path / "map.json",
        intermediate_map_path=intermediate,
    )

    assert result["citation_count"] == 2
    assert result["citations"][0]["citation_identity"] != result["citations"][1]["citation_identity"]


def test_final_map_deduplicates_explicit_doi_s2_and_x_aliases(tmp_path):
    manuscript = tmp_path / "final.md"
    manuscript.write_text(
        "[REF:doi:10.1000/one] [REF:s2:abc123] [REF:X01] [REF:doi.org/10.1000/two]",
        encoding="utf-8",
    )
    intermediate = tmp_path / "source.json"
    intermediate.write_text(json.dumps({"citations": [
        {"paper_id": "X01", "doi": "https://doi.org/10.1000/one", "s2_id": "abc123", "aliases": ["X01"]},
        {"paper_id": "doi.org/10.1000/two", "doi": "10.1000/two"},
        {"paper_id": "stale", "doi": "10.1000/stale"},
    ]}), encoding="utf-8")
    result = build_final_citation_map(
        markdown_path=manuscript, output_path=tmp_path / "map.json",
        intermediate_map_path=intermediate,
    )
    assert result["citation_count"] == 2
    assert [row["citation_identity"] for row in result["citations"]] == [
        "doi:10.1000/one", "doi:10.1000/two"
    ]


def test_final_map_resolves_hash_intermediate_through_metadata_catalog(tmp_path):
    manuscript = tmp_path / "final.md"
    manuscript.write_text(
        "[REF:doi:10.1000/hash-paper] [REF:s2:abc123] [REF:X01] "
        "[REF:doi:10.1000/second]",
        encoding="utf-8",
    )
    intermediate = tmp_path / "authoring_map.json"
    intermediate.write_text(
        json.dumps({"citations": [{"paper_id": "hash-anchor-001"}]}),
        encoding="utf-8",
    )
    catalog = tmp_path / "PUBLICATION_METADATA_CATALOG.json"
    catalog.write_text(
        json.dumps(
            {
                "schema_version": "optomind.publication_metadata_resolver.v1",
                "records": {
                    "hash-anchor-001": {
                        "metadata": {
                            "doi": "10.1000/hash-paper",
                            "s2_id": "abc123",
                            "aliases": ["X01"],
                            "title": "Hash paper",
                            "year": "2024",
                        }
                    },
                    "stale-hash": {
                        "metadata": {
                            "doi": "10.1000/stale",
                            "s2_id": "stale-s2",
                            "aliases": ["STALE"],
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = build_final_citation_map(
        markdown_path=manuscript,
        output_path=tmp_path / "map.json",
        intermediate_map_path=intermediate,
        metadata_catalog_path=catalog,
    )

    assert result["citation_count"] == 2
    assert [row["citation_identity"] for row in result["citations"]] == [
        "doi:10.1000/hash-paper",
        "doi:10.1000/second",
    ]
    assert result["citations"][0]["s2_id"] == "abc123"
    assert all("stale" not in json.dumps(row) for row in result["citations"])


def test_final_map_merges_catalog_metadata_and_bibtex_identity_keys(tmp_path):
    manuscript = tmp_path / "main.tex"
    manuscript.write_text(
        r"""Text \citep{doi_10_1002_advs_202201190_9f3bb563}.
        More \citep{s2_5b4a078b190c48e53cc96425a7919d91d8bcd99f_8d7cacd5}.""",
        encoding="utf-8",
    )
    intermediate = tmp_path / "authoring.json"
    intermediate.write_text(
        json.dumps(
            {
                "citations": [
                    {"paper_id": "e7cda7beaa70f65e43b46d813b939ffd92b9b0eb", "trace_status": "verified"},
                    {"paper_id": "5b4a078b190c48e53cc96425a7919d91d8bcd99f", "trace_status": "verified"},
                ]
            }
        ),
        encoding="utf-8",
    )
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "identity": "e7cda7beaa70f65e43b46d813b939ffd92b9b0eb",
                        "canonical_identity": "doi:10.1002/advs.202201190",
                        "aliases": [
                            "e7cda7beaa70f65e43b46d813b939ffd92b9b0eb",
                            "doi:10.1002/advs.202201190",
                        ],
                        "doi": "10.1002/advs.202201190",
                    },
                    {
                        "identity": "5b4a078b190c48e53cc96425a7919d91d8bcd99f",
                        "canonical_identity": "s2:5b4a078b190c48e53cc96425a7919d91d8bcd99f",
                        "aliases": [
                            "5b4a078b190c48e53cc96425a7919d91d8bcd99f",
                            "s2:5b4a078b190c48e53cc96425a7919d91d8bcd99f",
                        ],
                        "s2_id": "5b4a078b190c48e53cc96425a7919d91d8bcd99f",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = build_final_citation_map(
        markdown_path=manuscript,
        output_path=tmp_path / "map.json",
        intermediate_map_path=intermediate,
        metadata_catalog_path=catalog,
    )

    assert result["citation_count"] == 2
    assert [row["citation_identity"] for row in result["citations"]] == [
        "doi:10.1002/advs.202201190",
        "s2:5b4a078b190c48e53cc96425a7919d91d8bcd99f",
    ]
    assert all(not row["citation_identity"].startswith("marker:") for row in result["citations"])
    assert result["citations"][0]["trace_status"] == "verified"
