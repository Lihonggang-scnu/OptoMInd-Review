"""Focused tests for the local-first publication metadata resolver.

Covers DOI enrichment (mocked Crossref), S2 enrichment (mocked Semantic
Scholar), explanatory-ledger resolution, input-packet/chunk-id DOI recovery,
material-cache and S2-cache local resolution, deduplication with alias
retention, unresolved transparency, the no-1900 rule, title fallback
provenance, deterministic reruns, and clear input errors.  No test makes a
live network call.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from optomind_research.runtime.publication_metadata_resolver import (
    CATALOG_FILENAME,
    SCHEMA_VERSION,
    PublicationMetadataError,
    ResolverOptions,
    build_publication_metadata_catalog,
    infer_publication_project_root,
    doi_from_chunk_id,
    inventory_ref_identities,
    normalize_doi,
    parse_ref_identity,
)


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory under the repository basetemp."""

    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-publication-metadata"
    )
    root.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = root / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _make_project(
    tmp_path: Path,
    *,
    manuscript_text: str,
    ledger_records: list[dict[str, Any]] | None = None,
    packet_evidence: list[dict[str, Any]] | None = None,
    packet_sources: list[dict[str, Any]] | None = None,
    staged_context: dict[str, Any] | None = None,
    material_cache_units: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    asset = tmp_path / "outputs" / "enhanced_S01"
    asset.mkdir(parents=True, exist_ok=True)
    _write_json(
        asset / "EXPLANATORY_CITATION_LEDGER.json",
        {
            "schema_version": "chapter_asset_enhancer.v1",
            "section_id": "S01",
            "records": ledger_records or [],
        },
    )
    _write_json(
        asset / "input_packet.json",
        {
            "section_id": "S01",
            "evidence_packets": packet_evidence or [],
            "literature_coverage": {"sources": packet_sources or []},
        },
    )
    handoff_path = _write_json(
        tmp_path / "outputs" / "UNIFIED_MANUSCRIPT_HANDOFF.json",
        {
            "schema_version": "optomind.full_manuscript_handoff.v1",
            "section_order": ["S01"],
            "sections": {
                "S01": {
                    "section_id": "S01",
                    "explanatory_citation_ledger": {
                        "path": (
                            "outputs/enhanced_S01/"
                            "EXPLANATORY_CITATION_LEDGER.json"
                        )
                    },
                    "authoritative_input_packet": {
                        "path": "outputs/enhanced_S01/input_packet.json"
                    },
                }
            },
        },
    )
    manuscript_path = tmp_path / "STAGED_COMPLETE_REVIEW_EN.md"
    manuscript_path.write_text(manuscript_text, encoding="utf-8")
    staged_context_path = None
    if staged_context is not None:
        staged_context_path = _write_json(
            tmp_path / "outputs" / "staged_context_test" / "STAGED_GLOBAL_INPUTS.json",
            staged_context,
        )
    material_cache_path = None
    if material_cache_units is not None:
        material_cache_path = _write_json(
            tmp_path
            / "outputs"
            / "section_supplementary_test"
            / "long_term_material_cache"
            / "snapshot-0001"
            / "MATERIAL_UNITS_FINAL.json",
            {
                "schema_version": "optomind.material_cache_merge.v1",
                "units": material_cache_units,
            },
        )
    return {
        "root": tmp_path,
        "manuscript": manuscript_path,
        "handoff": handoff_path,
        "staged_context": staged_context_path,
        "material_cache": material_cache_path,
        "output": tmp_path / "out",
    }


def _run(
    project: dict[str, Any],
    *,
    options: ResolverOptions | None = None,
    staged_context_path: str | Path | None = None,
    material_cache_dirs: list[Path] | None = None,
    supplemental_metadata_paths: list[Path] | None = None,
    include_s2_cache: bool = False,
) -> dict[str, Any]:
    return build_publication_metadata_catalog(
        staged_manuscript_path=project["manuscript"],
        handoff_path=project["handoff"],
        project_root=project["root"],
        output_dir=project["output"],
        options=options,
        staged_context_path=(
            staged_context_path
            if staged_context_path is not None
            else project["staged_context"]
        ),
        material_cache_dirs=material_cache_dirs or [],
        scan_material_caches=False,
        supplemental_metadata_paths=supplemental_metadata_paths or [],
        include_s2_cache=include_s2_cache,
    )


def _read_catalog(project: dict[str, Any]) -> dict[str, Any]:
    path = project["output"] / CATALOG_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def _entry_by_marker(catalog: dict[str, Any], marker: str) -> dict[str, Any]:
    for entry in catalog["entries"]:
        if marker in entry["markers"]:
            return entry
    raise AssertionError(f"marker {marker} not found in catalog entries")


def test_project_root_is_inferred_from_handoff_file_anchors(tmp_path):
    project = _make_project(
        tmp_path,
        manuscript_text="## S01\n\nLocal [REF:doi:10.1000/inferred].\n",
        ledger_records=[
            _ledger_record(
                marker_id="doi:10.1000/inferred",
                title="Inferred Root Paper",
                authors=["Root Author"],
                year=2024,
                venue="Root Venue",
                doi="10.1000/inferred",
            )
        ],
    )

    assert infer_publication_project_root(project["handoff"]) == project["root"]
    summary = build_publication_metadata_catalog(
        staged_manuscript_path=project["manuscript"],
        handoff_path=project["handoff"],
        project_root=None,
        output_dir=project["output"],
        scan_material_caches=False,
        include_s2_cache=False,
    )

    assert summary["audit"]["resolution_status_counts"]["resolved"] == 1
    assert _entry_by_marker(
        _read_catalog(project), "doi:10.1000/inferred"
    )["title"] == "Inferred Root Paper"


def _ledger_record(
    *,
    marker_id: str,
    title: str,
    authors: list[str] | None = None,
    year: Any = None,
    venue: str = "",
    doi: str = "",
    paper_id: str = "",
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"title": title}
    if authors:
        metadata["authors"] = authors
    if year is not None:
        metadata["year"] = year
    if venue:
        metadata["venue"] = venue
    if doi:
        metadata["doi"] = doi
        metadata.setdefault("url", f"https://doi.org/{doi}")
    if paper_id:
        metadata["paper_id"] = paper_id
    return {
        "handle": "X01",
        "marker_id": marker_id,
        "permission": "background_explanation_only",
        "retrieval_origin": "local_metadata",
        "metadata": metadata,
    }


def test_explanatory_ledger_resolves_doi_identity_locally(tmp_path):
    project = _make_project(
        tmp_path,
        manuscript_text=(
            "## Introduction\n\n"
            "PINNs embed PDEs in the loss "
            "[REF:doi:10.1007/s11831-025-10448-9].\n"
        ),
        ledger_records=[
            _ledger_record(
                marker_id="doi:10.1007/s11831-025-10448-9",
                title="Physics-Informed Neural Networks Review",
                authors=["Alkmini Michaloglou", "Ioannis Papadimitriou"],
                year=2025,
                venue="Archives of Computational Methods in Engineering",
                doi="10.1007/s11831-025-10448-9",
            )
        ],
    )
    summary = _run(project)
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, "doi:10.1007/s11831-025-10448-9")
    assert entry["resolution_status"] == "resolved"
    assert entry["title"].startswith("Physics-Informed Neural Networks Review")
    assert entry["authors"] == ["Alkmini Michaloglou", "Ioannis Papadimitriou"]
    assert entry["year"] == "2025"
    assert entry["venue"] == "Archives of Computational Methods in Engineering"
    assert entry["doi"] == "10.1007/s11831-025-10448-9"
    assert entry["missing_fields"] == []
    assert entry["provenance"]["title"]["source"] == "explanatory_ledger"
    assert entry["provenance"]["title"]["confidence"] == "high"
    assert entry["sections"] == ["Introduction"]
    assert summary["audit"]["provider_calls"] == {
        "openalex": 0,
        "crossref": 0,
        "s2": 0,
    }
    assert summary["audit"]["enriched_by_crossref_count"] == 0
    # Renderer-compatible records keep marker mapping for every alias.
    record = catalog["records"]["doi:10.1007/s11831-025-10448-9"]
    assert record["paper_id"] == "doi:10.1007/s11831-025-10448-9"
    assert record["resolution_status"] == "resolved"


def test_doi_identity_enriched_by_mocked_crossref(tmp_path):
    project = _make_project(
        tmp_path,
        manuscript_text=(
            "## S03\n\n"
            "Crossref-only identity [REF:doi:10.1002/adfm.202421051].\n"
        ),
    )

    def crossref_lookup(doi: str) -> dict[str, Any] | None:
        assert doi == "10.1002/adfm.202421051"
        return {
            "title": "Metasurface Inverse Design via Differentiable Simulation",
            "authors": ["Jane Author", "John Researcher"],
            "year": 2024,
            "venue": "Advanced Functional Materials",
            "doi": "10.1002/adfm.202421051",
            "url": "https://doi.org/10.1002/adfm.202421051",
        }

    summary = _run(
        project,
        options=ResolverOptions(
            allow_crossref=True,
            max_provider_calls=10,
            crossref_provider=crossref_lookup,
        ),
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, "doi:10.1002/adfm.202421051")
    assert entry["resolution_status"] == "resolved"
    assert entry["title"] == (
        "Metasurface Inverse Design via Differentiable Simulation"
    )
    assert entry["authors"] == ["Jane Author", "John Researcher"]
    assert entry["year"] == "2024"
    assert entry["venue"] == "Advanced Functional Materials"
    assert entry["provenance"]["title"]["source"] == "crossref"
    assert entry["provenance"]["title"]["confidence"] == "high"
    assert summary["audit"]["enriched_by_crossref_count"] == 1
    assert summary["audit"]["provider_calls"]["crossref"] == 1


def test_s2_identity_enriched_by_mocked_semantic_scholar(tmp_path):
    s2_hash = "2c5e4bccf8f358baca8b7c0ad8fb63279ad791f6"
    project = _make_project(
        tmp_path,
        manuscript_text=(
            "## S02\n\n"
            f"S2-only identity [REF:s2:{s2_hash}].\n"
        ),
    )

    def s2_lookup(paper_id: str) -> dict[str, Any] | None:
        assert paper_id == s2_hash
        return {
            "title": "Retain-Resample-Release Sampling for PINNs",
            "authors": ["Arka Daw", "P. Perdikaris"],
            "year": 2022,
            "venue": "International Conference on Machine Learning",
            "doi": "10.48550/arxiv.2202.10449",
            "url": f"https://www.semanticscholar.org/paper/{s2_hash}",
            "s2_id": s2_hash,
        }

    summary = _run(
        project,
        options=ResolverOptions(
            allow_s2=True,
            max_provider_calls=10,
            s2_provider=s2_lookup,
        ),
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, f"s2:{s2_hash}")
    assert entry["resolution_status"] == "resolved"
    assert entry["title"] == "Retain-Resample-Release Sampling for PINNs"
    assert entry["year"] == "2022"
    assert entry["s2_id"] == s2_hash
    assert entry["provenance"]["title"]["source"] == "s2_provider"
    assert summary["audit"]["enriched_by_s2_count"] == 1
    assert summary["audit"]["provider_calls"]["s2"] == 1


def test_openalex_doi_identity_enriched_by_mocked_provider(tmp_path):
    marker = "doi:10.1000/openalex-doi"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S03\n\nOpenAlex DOI [REF:{marker}].\n",
    )
    calls: list[dict[str, str]] = []

    def openalex_lookup(request: dict[str, str]) -> dict[str, Any] | None:
        calls.append(dict(request))
        assert request["kind"] == "doi"
        assert request["value"] == "10.1000/openalex-doi"
        return {
            "title": "OpenAlex Resolved Paper",
            "authors": ["OpenAlex Author"],
            "year": 2023,
            "venue": "OpenAlex Venue",
            "doi": "10.1000/openalex-doi",
            "url": "https://doi.org/10.1000/openalex-doi",
        }

    summary = _run(
        project,
        options=ResolverOptions(
            allow_openalex=True,
            max_provider_calls=10,
            openalex_provider=openalex_lookup,
        ),
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert calls == [{"kind": "doi", "value": "10.1000/openalex-doi"}]
    assert entry["resolution_status"] == "resolved"
    assert entry["title"] == "OpenAlex Resolved Paper"
    assert entry["authors"] == ["OpenAlex Author"]
    assert entry["year"] == "2023"
    assert entry["venue"] == "OpenAlex Venue"
    assert entry["doi"] == "10.1000/openalex-doi"
    assert entry["provenance"]["title"]["source"] == "openalex"
    assert entry["provenance"]["title"]["confidence"] == "high"
    assert "openalex" in entry["candidate_sources"]
    assert summary["audit"]["enriched_by_openalex_count"] == 1
    assert summary["audit"]["provider_calls"] == {
        "openalex": 1,
        "crossref": 0,
        "s2": 0,
    }


def test_openalex_title_lookup_enriches_title_only_identity(tmp_path):
    marker = "hash:1111222233334444"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S04\n\nTitle only [REF:{marker}].\n",
        staged_context={
            "schema_version": "optomind.staged_manuscript_context.v1",
            "citation_inventory": [
                {
                    "citation_id": marker,
                    "title": "Recoverable Title Paper",
                    "trust_type": "core_evidence",
                    "provenance": {"section_id": "S04"},
                }
            ],
            "local_background_candidates": [],
        },
    )
    calls: list[dict[str, str]] = []

    def openalex_lookup(request: dict[str, str]) -> dict[str, Any] | None:
        calls.append(dict(request))
        assert request["kind"] == "title"
        assert request["value"] == "Recoverable Title Paper"
        return {
            "title": "Recoverable Title Paper",
            "authors": ["Title Author"],
            "year": 2022,
            "venue": "Title Venue",
            "doi": "10.1000/title-match",
            "url": "https://doi.org/10.1000/title-match",
        }

    summary = _run(
        project,
        staged_context_path=project["staged_context"],
        options=ResolverOptions(
            allow_openalex=True,
            max_provider_calls=5,
            openalex_provider=openalex_lookup,
        ),
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert calls == [{"kind": "title", "value": "Recoverable Title Paper"}]
    assert entry["resolution_status"] == "resolved"
    assert entry["title"] == "Recoverable Title Paper"
    assert entry["authors"] == ["Title Author"]
    assert entry["year"] == "2022"
    assert entry["venue"] == "Title Venue"
    assert entry["doi"] == "10.1000/title-match"
    assert entry["provenance"]["title"]["source"] == "openalex"
    assert summary["audit"]["enriched_by_openalex_count"] == 1
    assert summary["audit"]["provider_calls"]["openalex"] == 1


def test_openalex_conflict_precedence_over_crossref(tmp_path):
    marker = "doi:10.1000/precedence"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S05\n\nPrecedence [REF:{marker}].\n",
    )

    def openalex_lookup(request: dict[str, str]) -> dict[str, Any] | None:
        assert request == {"kind": "doi", "value": "10.1000/precedence"}
        return {
            "title": "OpenAlex Title",
            "authors": ["OpenAlex Author"],
            "year": 2021,
            "venue": "OpenAlex Venue",
            "doi": "10.1000/precedence",
        }

    def crossref_lookup(doi: str) -> dict[str, Any] | None:
        assert doi == "10.1000/precedence"
        return {
            "title": "Crossref Title",
            "authors": ["Crossref Author"],
            "year": 2022,
            "venue": "Crossref Venue",
            "doi": "10.1000/precedence",
        }

    summary = _run(
        project,
        options=ResolverOptions(
            allow_openalex=True,
            allow_crossref=True,
            max_provider_calls=10,
            openalex_provider=openalex_lookup,
            crossref_provider=crossref_lookup,
        ),
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == "OpenAlex Title"
    assert entry["authors"] == ["OpenAlex Author"]
    assert entry["year"] == "2021"
    assert entry["venue"] == "OpenAlex Venue"
    assert entry["provenance"]["title"]["source"] == "openalex"
    assert entry["provenance"]["venue"]["source"] == "openalex"
    assert summary["audit"]["provider_calls"]["openalex"] == 1
    assert summary["audit"]["provider_calls"]["crossref"] == 1


def test_provider_enrichment_does_not_overwrite_stronger_local_fields(tmp_path):
    marker = "doi:10.1000/local-strong"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S06\n\nLocal strong [REF:{marker}].\n",
        ledger_records=[
            _ledger_record(
                marker_id=marker,
                title="Clean Local Title",
                authors=["Local Author"],
                year=2020,
                venue="",
                doi="10.1000/local-strong",
            )
        ],
    )

    def openalex_lookup(request: dict[str, str]) -> dict[str, Any] | None:
        return {
            "title": "OpenAlex Override Attempt",
            "authors": ["OpenAlex Author"],
            "year": 1999,
            "venue": "OpenAlex Venue",
            "doi": "10.1000/local-strong",
        }

    def crossref_lookup(doi: str) -> dict[str, Any] | None:
        return {
            "title": "Crossref Override Attempt",
            "authors": ["Crossref Author"],
            "year": 1998,
            "venue": "Crossref Venue",
            "doi": "10.1000/local-strong",
        }

    summary = _run(
        project,
        options=ResolverOptions(
            allow_openalex=True,
            allow_crossref=True,
            max_provider_calls=10,
            openalex_provider=openalex_lookup,
            crossref_provider=crossref_lookup,
        ),
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == "Clean Local Title"
    assert entry["authors"] == ["Local Author"]
    assert entry["year"] == "2020"
    assert entry["venue"] == "OpenAlex Venue"
    assert entry["provenance"]["title"]["source"] == "explanatory_ledger"
    assert entry["provenance"]["authors"]["source"] == "explanatory_ledger"
    assert entry["provenance"]["year"]["source"] == "explanatory_ledger"
    assert entry["provenance"]["venue"]["source"] == "openalex"
    assert entry["missing_fields"] == []
    assert summary["audit"]["enriched_by_openalex_count"] == 1


def test_openalex_provider_error_falls_through_to_crossref(tmp_path):
    marker = "doi:10.1000/openalex-error"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S07\n\nError [REF:{marker}].\n",
    )

    def openalex_lookup(request: dict[str, str]) -> dict[str, Any] | None:
        raise RuntimeError("openalex transport failure")

    def crossref_lookup(doi: str) -> dict[str, Any] | None:
        return {
            "title": "Crossref Recovered Paper",
            "authors": ["Recovered Author"],
            "year": 2024,
            "venue": "Recovered Venue",
            "doi": "10.1000/openalex-error",
        }

    summary = _run(
        project,
        options=ResolverOptions(
            allow_openalex=True,
            allow_crossref=True,
            max_provider_calls=10,
            openalex_provider=openalex_lookup,
            crossref_provider=crossref_lookup,
        ),
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["resolution_status"] == "resolved"
    assert entry["title"] == "Crossref Recovered Paper"
    assert entry["provenance"]["title"]["source"] == "crossref"
    assert summary["audit"]["provider_errors"]["openalex"] == 1
    assert summary["audit"]["provider_calls"]["openalex"] == 1
    assert summary["audit"]["provider_calls"]["crossref"] == 1
    assert "openalex" in entry["candidate_sources"]


def test_provider_call_budget_is_bounded_across_identities(tmp_path):
    markers = [f"doi:10.1000/bounded-{index}" for index in range(3)]
    project = _make_project(
        tmp_path,
        manuscript_text=(
            "## S08\n\n"
            + " ".join(f"[REF:{marker}]" for marker in markers)
            + ".\n"
        ),
    )
    openalex_calls: list[str] = []
    crossref_calls: list[str] = []

    def openalex_lookup(request: dict[str, str]) -> dict[str, Any] | None:
        doi = request["value"]
        openalex_calls.append(doi)
        return {
            "title": f"OpenAlex {doi}",
            "authors": ["OpenAlex Author"],
            "year": 2024,
            "venue": "OpenAlex Venue",
            "doi": doi,
        }

    def crossref_lookup(doi: str) -> dict[str, Any] | None:
        crossref_calls.append(doi)
        return {
            "title": f"Crossref {doi}",
            "authors": ["Crossref Author"],
            "year": 2024,
            "venue": "Crossref Venue",
            "doi": doi,
        }

    summary = _run(
        project,
        options=ResolverOptions(
            allow_openalex=True,
            allow_crossref=True,
            max_provider_calls=4,
            max_workers=4,
            openalex_provider=openalex_lookup,
            crossref_provider=crossref_lookup,
        ),
    )
    assert summary["audit"]["provider_calls"] == {
        "openalex": 2,
        "crossref": 2,
        "s2": 0,
    }
    assert openalex_calls == [
        "10.1000/bounded-0",
        "10.1000/bounded-1",
    ]
    assert crossref_calls == [
        "10.1000/bounded-0",
        "10.1000/bounded-1",
    ]


def test_parallel_provider_enrichment_rerun_is_deterministic(tmp_path):
    project = _make_project(
        tmp_path,
        manuscript_text=(
            "## S09\n\n"
            "One [REF:doi:10.1000/parallel-one] and "
            "[REF:doi:10.1000/parallel-two].\n"
        ),
    )

    openalex_rows = {
        "10.1000/parallel-one": {
            "title": "Parallel One",
            "authors": ["P1 Author"],
            "year": 2021,
            "venue": "P1 Venue",
            "doi": "10.1000/parallel-one",
        },
        "10.1000/parallel-two": {
            "title": "Parallel Two",
            "authors": ["P2 Author"],
            "year": 2022,
            "venue": "P2 Venue",
            "doi": "10.1000/parallel-two",
        },
    }
    crossref_rows = {
        doi: dict(row, title=row["title"] + " (Crossref)")
        for doi, row in openalex_rows.items()
    }

    def openalex_lookup(request: dict[str, str]) -> dict[str, Any] | None:
        return dict(openalex_rows[request["value"]])

    def crossref_lookup(doi: str) -> dict[str, Any] | None:
        return dict(crossref_rows[doi])

    options = ResolverOptions(
        allow_openalex=True,
        allow_crossref=True,
        max_provider_calls=20,
        max_workers=4,
        openalex_provider=openalex_lookup,
        crossref_provider=crossref_lookup,
    )
    _run(project, options=options)
    first = (project["output"] / CATALOG_FILENAME).read_bytes()
    second_project = {**project, "output": project["root"] / "out2"}
    _run(second_project, options=options)
    second = (second_project["output"] / CATALOG_FILENAME).read_bytes()
    assert first == second
    catalog = json.loads(first)
    assert catalog["audit"]["provider_calls"] == {
        "openalex": 2,
        "crossref": 2,
        "s2": 0,
    }


def test_basic_internal_completeness_is_title_authors_year(tmp_path):
    marker = "hash:2222333344445555"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S10\n\nBasic [REF:{marker}].\n",
        ledger_records=[
            _ledger_record(
                marker_id=marker,
                title="Basic Complete Paper",
                authors=["Basic Author"],
                year=2023,
                venue="",
                doi="",
            )
        ],
    )
    summary = _run(project)
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == "Basic Complete Paper"
    assert entry["authors"] == ["Basic Author"]
    assert entry["year"] == "2023"
    assert entry["resolution_status"] == "resolved"
    assert entry["missing_fields"] == ["venue", "doi", "url"]
    assert any(note.startswith("venue:") for note in entry["resolution_notes"])
    assert summary["audit"]["resolution_status_counts"]["resolved"] == 1
    assert summary["audit"]["missing_field_counts"]["venue"] == 1
    assert summary["audit"]["missing_field_counts"]["doi"] == 1
    assert summary["audit"]["missing_field_counts"]["url"] == 1


def test_input_packet_chunk_id_doi_recovery(tmp_path):
    marker = "identity-fallback:cb326fdcbd36f94d"
    project = _make_project(
        tmp_path,
        manuscript_text=(
            "## S01\n\n"
            f"Conservation laws [REF:{marker}].\n"
        ),
        packet_evidence=[
            {
                "claim_id": "S01-C001",
                "chunk_id": "m3gap:10.1016-j.physd.2023.133952:0003",
                "paper_id": marker,
                "source_title": "Learning physical models that can respect conservation laws",
            }
        ],
        packet_sources=[
            {
                "paper_id": marker,
                "title": "Learning physical models that can respect conservation laws",
                "doi": "10.1016/j.physd.2023.133952",
                "chunk_id": "m3gap:10.1016-j.physd.2023.133952:0003",
            }
        ],
    )
    _run(project)
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == "Learning physical models that can respect conservation laws"
    assert entry["doi"] == "10.1016/j.physd.2023.133952"
    assert entry["resolution_status"] == "partial"
    assert entry["missing_fields"] == ["authors", "year", "venue"]
    assert entry["provenance"]["title"]["source"] == "input_packet"
    assert entry["provenance"]["doi"]["source"] == "input_packet"
    assert entry["resolution_notes"], "partial resolution must carry notes"
    # Chunk-id recovery is deterministic and validated.
    assert doi_from_chunk_id("m3gap:10.1109-ojap.2022.3190224:0046") == (
        "10.1109/ojap.2022.3190224"
    )
    assert doi_from_chunk_id("m3gap:10.1007-s10462-025-11322-7:0007") == (
        "10.1007/s10462-025-11322-7"
    )
    assert doi_from_chunk_id("not-a-chunk") == ""


def test_dedupe_by_doi_then_s2_then_title_retains_aliases(tmp_path):
    s2_hash = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    project = _make_project(
        tmp_path,
        manuscript_text=(
            "## S03\n\n"
            "First [REF:doi:10.1000/sample.1] second "
            f"[REF:s2:{s2_hash}].\n"
            "Third [REF:hash:cccccccccccccccc] fourth "
            "[REF:hash:dddddddddddddddd].\n"
        ),
        ledger_records=[
            _ledger_record(
                marker_id=f"s2:{s2_hash}",
                title="Same Identical Paper",
                authors=["Alice", "Bob"],
                year=2020,
                venue="Sample Venue",
                doi="10.1000/sample.1",
                paper_id=f"s2:{s2_hash}",
            )
        ],
        staged_context={
            "schema_version": "optomind.staged_manuscript_context.v1",
            "citation_inventory": [
                {
                    "citation_id": "hash:cccccccccccccccc",
                    "title": "Same Identical Paper",
                    "trust_type": "core_evidence",
                    "provenance": {"section_id": "S03"},
                },
                {
                    "citation_id": "hash:dddddddddddddddd",
                    "title": "Same Identical Paper",
                    "trust_type": "core_evidence",
                    "provenance": {"section_id": "S03"},
                },
            ],
            "local_background_candidates": [],
        },
    )
    _run(project, staged_context_path=project["staged_context"])
    catalog = _read_catalog(project)
    assert catalog["audit"]["catalog_entry_count"] == 2
    assert catalog["audit"]["deduplicated_identity_count"] == 2
    doi_entry = _entry_by_marker(catalog, "doi:10.1000/sample.1")
    s2_entry = _entry_by_marker(catalog, f"s2:{s2_hash}")
    assert doi_entry is s2_entry
    assert doi_entry["canonical_identity"] == "doi:10.1000/sample.1"
    assert doi_entry["markers"] == [
        "doi:10.1000/sample.1",
        f"s2:{s2_hash}",
    ]
    assert doi_entry["marker_count"] == 2
    for alias in ("doi:10.1000/sample.1", f"s2:{s2_hash}"):
        assert alias in doi_entry["aliases"]
    title_entry = _entry_by_marker(catalog, "hash:cccccccccccccccc")
    assert title_entry is _entry_by_marker(catalog, "hash:dddddddddddddddd")
    assert title_entry["canonical_identity"].startswith("title:")
    assert set(title_entry["markers"]) == {
        "hash:cccccccccccccccc",
        "hash:dddddddddddddddd",
    }
    assert title_entry["resolution_status"] == "partial"
    assert title_entry["provenance"]["title"]["source"] == "title_fallback"
    assert title_entry["provenance"]["title"]["base_source"] == "staged_context"
    assert title_entry["provenance"]["title"]["confidence"] == "low"
    assert "title recovered from a local title-only record" in (
        title_entry["provenance"]["title"]["reason"]
    )


def test_unresolved_identity_is_transparent(tmp_path):
    marker = "s2:1111111111111111111111111111111111111111"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S05\n\nNothing known [REF:{marker}].\n",
    )
    summary = _run(project)
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["resolution_status"] == "unresolved"
    assert entry["title"] == ""
    assert entry["authors"] == []
    assert entry["year"] == ""
    assert entry["venue"] == ""
    assert entry["doi"] == ""
    assert entry["url"] == ""
    assert entry["missing_fields"] == [
        "title",
        "authors",
        "year",
        "venue",
        "doi",
        "url",
    ]
    assert any("unresolved" in note for note in entry["resolution_notes"])
    assert entry["provenance"]["title"]["status"] == "missing"
    assert summary["audit"]["resolution_status_counts"]["unresolved"] == 1
    assert summary["audit"]["provider_calls"] == {
        "openalex": 0,
        "crossref": 0,
        "s2": 0,
    }


def test_no_1900_placeholder_substitution(tmp_path):
    marker = "doi:10.1000/legacy"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S01\n\nLegacy [REF:{marker}].\n",
        ledger_records=[
            _ledger_record(
                marker_id=marker,
                title="Legacy paper with placeholder year",
                authors=["Old Author"],
                year=1900,
                venue="Somewhere",
                doi="10.1000/legacy",
            )
        ],
    )
    summary = _run(project)
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["year"] == ""
    assert "year" in entry["missing_fields"]
    assert any("1900" in note for note in entry["resolution_notes"])
    assert any(
        "placeholder" in reason
        for reason in entry["provenance"]["year"].get("reasons", [])
    )
    assert summary["audit"]["placeholder_year_1900_rejected_count"] == 1
    assert all(e["year"] != "1900" for e in catalog["entries"])


def test_material_cache_local_resolution(tmp_path):
    marker = "037e66f37189104d61c0d000de0cee202b7eec9e"
    project = _make_project(
        tmp_path,
        manuscript_text=(
            "## S02\n\n"
            f"Cache identity [REF:{marker}].\n"
        ),
        material_cache_units=[
            {
                "unit_id": "unit:text:0001",
                "work_id": "work:0001",
                "identity": {
                    "paper_id": marker,
                    "doi": "10.1103/physrevapplied.17.014037",
                    "title": "Physics-Informed Neural Networks in Electromagnetic Design",
                },
                "durable_content": {"content_hash": "x"},
            }
        ],
    )
    _run(
        project,
        material_cache_dirs=[
            project["root"]
            / "outputs"
            / "section_supplementary_test"
            / "long_term_material_cache"
        ],
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == "Physics-Informed Neural Networks in Electromagnetic Design"
    assert entry["doi"] == "10.1103/physrevapplied.17.014037"
    assert entry["provenance"]["title"]["source"] == "material_cache"
    assert entry["resolution_status"] == "partial"


def test_s2_sqlite_cache_local_resolution(tmp_path):
    marker = "4cd6a0528e4502ce839ab595b537badcd8347082"
    cache_path = tmp_path / "database" / "s2_cache" / "s2_online_cache.sqlite"
    cache_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(str(cache_path))
    connection.executescript(
        """
        CREATE TABLE s2_cache (
            cache_key TEXT PRIMARY KEY,
            method TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            params_json TEXT NOT NULL,
            body_hash TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            response_json TEXT NOT NULL,
            negative INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            schema_version TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO s2_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "k",
            "GET",
            "/graph/v1/paper/search",
            '{"query":"x"}',
            "h",
            200,
            json.dumps(
                {
                    "data": [
                        {
                            "paperId": marker,
                            "title": "Every base matters: rRNA primers",
                            "year": 2016,
                            "venue": "Molecular Ecology Resources",
                            "authors": [
                                {"name": "A. Parada"},
                                {"name": "D. Needham"},
                            ],
                            "externalIds": {"DOI": "10.1111/1462-2920.13023"},
                        }
                    ]
                }
            ),
            0,
            1.0,
            2.0,
            "v1",
        ),
    )
    connection.commit()
    connection.close()
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S06\n\nCached S2 [REF:{marker}].\n",
    )
    _run(
        project,
        include_s2_cache=True,
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == "Every base matters: rRNA primers"
    assert entry["year"] == "2016"
    assert entry["authors"] == ["A. Parada", "D. Needham"]
    assert entry["doi"] == "10.1111/1462-2920.13023"
    assert entry["provenance"]["title"]["source"] == "s2_cache"


def test_deterministic_rerun_produces_identical_catalog(tmp_path):
    project = _make_project(
        tmp_path,
        manuscript_text=(
            "## S01\n\n"
            "A [REF:doi:10.1000/one] and "
            "[REF:doi:10.1000/two].\n"
        ),
        ledger_records=[
            _ledger_record(
                marker_id="doi:10.1000/one",
                title="First Paper",
                authors=["A. One"],
                year=2021,
                venue="Venue One",
                doi="10.1000/one",
            ),
            _ledger_record(
                marker_id="doi:10.1000/two",
                title="Second Paper",
                authors=["B. Two"],
                year=2022,
                venue="Venue Two",
                doi="10.1000/two",
            ),
        ],
    )
    _run(project)
    first = (project["output"] / CATALOG_FILENAME).read_bytes()
    second_project = {
        **project,
        "output": project["root"] / "out2",
    }
    _run(second_project)
    second = (second_project["output"] / CATALOG_FILENAME).read_bytes()
    assert first == second
    first_catalog = json.loads(first)
    second_catalog = json.loads(second)
    assert first_catalog["catalog_fingerprint"] == second_catalog["catalog_fingerprint"]
    assert first_catalog["input_fingerprint"] == second_catalog["input_fingerprint"]
    # Relocation-safe: all source paths are project-relative.
    for source in first_catalog["input"]["input_files"]:
        assert not Path(source["path"]).is_absolute()


def test_clear_errors_for_missing_and_malformed_inputs(tmp_path):
    project = _make_project(
        tmp_path,
        manuscript_text="## S01\n\nNothing [REF:doi:10.1000/x].\n",
    )
    with pytest.raises(PublicationMetadataError, match="staged manuscript not found"):
        build_publication_metadata_catalog(
            staged_manuscript_path=tmp_path / "missing.md",
            handoff_path=project["handoff"],
            project_root=project["root"],
            output_dir=project["root"] / "out",
            scan_material_caches=False,
            include_s2_cache=False,
        )
    malformed_handoff = tmp_path / "malformed.json"
    malformed_handoff.write_text("{not json", encoding="utf-8")
    with pytest.raises(PublicationMetadataError, match="cannot read/parse"):
        build_publication_metadata_catalog(
            staged_manuscript_path=project["manuscript"],
            handoff_path=malformed_handoff,
            project_root=project["root"],
            output_dir=project["root"] / "out",
            scan_material_caches=False,
            include_s2_cache=False,
        )
    missing_ledger_handoff = _write_json(
        tmp_path / "handoff_missing_ledger.json",
        {
            "schema_version": "optomind.full_manuscript_handoff.v1",
            "section_order": ["S01"],
            "sections": {
                "S01": {
                    "section_id": "S01",
                    "explanatory_citation_ledger": {
                        "path": "outputs/enhanced_S01/MISSING_LEDGER.json"
                    },
                    "authoritative_input_packet": {
                        "path": "outputs/enhanced_S01/input_packet.json"
                    },
                }
            },
        },
    )
    with pytest.raises(PublicationMetadataError, match="file not found"):
        build_publication_metadata_catalog(
            staged_manuscript_path=project["manuscript"],
            handoff_path=missing_ledger_handoff,
            project_root=project["root"],
            output_dir=project["root"] / "out",
            scan_material_caches=False,
            include_s2_cache=False,
        )


def test_inventory_parsing_and_empty_manuscript(tmp_path):
    text = (
        "## A\n\n[REF:doi:10.1000/x] [REF:10.1000/y] "
        "[REF:s2:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa] "
        "[REF:identity-fallback:cb326fdcbd36f94d] "
        "[REF:doi:10.1000/x] [REF:]\n"
    )
    inventory = inventory_ref_identities(text)
    assert inventory["total_occurrences"] == 6
    assert inventory["unique_tokens"] == [
        "doi:10.1000/x",
        "10.1000/y",
        "s2:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "identity-fallback:cb326fdcbd36f94d",
    ]
    assert inventory["counts"]["doi:10.1000/x"] == 2
    assert len(inventory["malformed"]) == 1
    assert inventory["malformed"][0]["token"] == ""
    empty = inventory_ref_identities("no markers here")
    assert empty["total_occurrences"] == 0
    assert empty["unique_tokens"] == []
    assert normalize_doi("HTTPS://DOI.ORG/10.1000/ABC") == "10.1000/abc"
    assert parse_ref_identity("10.1000/x").kind == "doi"
    assert parse_ref_identity("abcd").kind == "other"


def test_offline_default_never_calls_providers(tmp_path):
    marker = "s2:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    called: list[str] = []
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S07\n\nUnknown [REF:{marker}].\n",
    )

    def crossref_lookup(doi: str) -> dict[str, Any] | None:
        called.append("crossref")
        return None

    def openalex_lookup(request: dict[str, str]) -> dict[str, Any] | None:
        called.append("openalex")
        return None

    def s2_lookup(paper_id: str) -> dict[str, Any] | None:
        called.append("s2")
        return None

    _run(
        project,
        options=ResolverOptions(
            allow_openalex=False,
            allow_crossref=False,
            allow_s2=False,
            openalex_provider=openalex_lookup,
            crossref_provider=crossref_lookup,
            s2_provider=s2_lookup,
        ),
    )
    assert called == []
    catalog = _read_catalog(project)
    assert catalog["audit"]["provider_calls"] == {
        "openalex": 0,
        "crossref": 0,
        "s2": 0,
    }


def test_corrupt_english_title_rejected_clean_provider_wins(tmp_path):
    marker = "doi:10.1000/corrupt-title"
    corrupt_title = "Far\ufffd\ufffdield: Metasurface Inverse Design"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S01\n\nCorrupt title [REF:{marker}].\n",
        ledger_records=[
            _ledger_record(
                marker_id=marker,
                title=corrupt_title,
                authors=["Local Author"],
                year=2023,
                venue="Local Venue",
                doi="10.1000/corrupt-title",
            )
        ],
    )

    def crossref_lookup(doi: str) -> dict[str, Any] | None:
        assert doi == "10.1000/corrupt-title"
        return {
            "title": "Far-field Metasurface Inverse Design",
            "authors": ["Provider Author"],
            "year": 2023,
            "venue": "Provider Venue",
            "doi": "10.1000/corrupt-title",
        }

    summary = _run(
        project,
        options=ResolverOptions(
            allow_crossref=True,
            max_provider_calls=10,
            crossref_provider=crossref_lookup,
        ),
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == "Far-field Metasurface Inverse Design"
    assert entry["title"] != corrupt_title
    assert entry["provenance"]["title"]["source"] == "crossref"
    assert entry["resolution_status"] == "resolved"
    assert entry["quality_rejections"] == [
        {
            "field": "title",
            "reason": (
                "corrupt metadata rejected: "
                "replacement_character_in_latin_dominant_text"
            ),
            "source": "explanatory_ledger",
            "source_path": (
                "outputs/enhanced_S01/EXPLANATORY_CITATION_LEDGER.json"
            ),
        }
    ]
    assert summary["audit"]["corrupt_metadata_field_rejections"] == 1
    assert (
        summary["audit"]["corrupt_metadata_field_rejections_by_field"]["title"]
        == 1
    )


def test_corrupt_mixed_author_rejected_clean_provider_wins(tmp_path):
    marker = "doi:10.1000/corrupt-authors"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S02\n\nCorrupt authors [REF:{marker}].\n",
        ledger_records=[
            _ledger_record(
                marker_id=marker,
                title="Clean Title Stays",
                authors=["Alice Clean", "\u941a\u581d\u6093Bob"],
                year=2021,
                venue="Clean Venue",
                doi="10.1000/corrupt-authors",
            )
        ],
    )

    def crossref_lookup(doi: str) -> dict[str, Any] | None:
        assert doi == "10.1000/corrupt-authors"
        return {
            "title": "Clean Title Stays",
            "authors": ["Alice Clean", "Bob Smith"],
            "year": 2021,
            "venue": "Clean Venue",
            "doi": "10.1000/corrupt-authors",
        }

    summary = _run(
        project,
        options=ResolverOptions(
            allow_crossref=True,
            max_provider_calls=10,
            crossref_provider=crossref_lookup,
        ),
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["authors"] == ["Alice Clean", "Bob Smith"]
    assert entry["provenance"]["authors"]["source"] == "crossref"
    # Clean local title is not overridden by the provider.
    assert entry["title"] == "Clean Title Stays"
    assert entry["provenance"]["title"]["source"] == "explanatory_ledger"
    assert any(
        rejection["field"] == "authors"
        and "corrupt metadata rejected" in rejection["reason"]
        for rejection in entry["quality_rejections"]
    )
    assert summary["audit"]["corrupt_metadata_field_rejections_by_field"]["authors"] == 1


def test_corrupt_venue_rejected_clean_provider_fills_without_override(tmp_path):
    marker = "doi:10.1000/corrupt-venue"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S03\n\nCorrupt venue [REF:{marker}].\n",
        ledger_records=[
            _ledger_record(
                marker_id=marker,
                title="Clean Local Title",
                authors=["Local Author"],
                year=2022,
                venue="Jou\u9369?nal of Optics",
                doi="10.1000/corrupt-venue",
            )
        ],
    )

    def crossref_lookup(doi: str) -> dict[str, Any] | None:
        assert doi == "10.1000/corrupt-venue"
        return {
            "title": "Provider Title Must Not Win",
            "authors": ["Provider Author"],
            "year": 2022,
            "venue": "Journal of Optics",
            "doi": "10.1000/corrupt-venue",
        }

    summary = _run(
        project,
        options=ResolverOptions(
            allow_crossref=True,
            max_provider_calls=10,
            crossref_provider=crossref_lookup,
        ),
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["venue"] == "Journal of Optics"
    assert entry["provenance"]["venue"]["source"] == "crossref"
    # Clean local fields remain untouched: provider cannot override them.
    assert entry["title"] == "Clean Local Title"
    assert entry["provenance"]["title"]["source"] == "explanatory_ledger"
    assert entry["authors"] == ["Local Author"]
    assert entry["provenance"]["authors"]["source"] == "explanatory_ledger"
    assert entry["year"] == "2022"
    assert summary["audit"]["corrupt_metadata_field_rejections_by_field"]["venue"] == 1


def test_accented_latin_authors_survive(tmp_path):
    marker = "doi:10.1000/accented"
    authors = ["Jos\u00e9 \u00c1lvarez", "Fr\u00e9d\u00e9ric M\u00fcller"]
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S04\n\nAccented names [REF:{marker}].\n",
        ledger_records=[
            _ledger_record(
                marker_id=marker,
                title="Accented Author Paper",
                authors=authors,
                year=2020,
                venue="Accented Venue",
                doi="10.1000/accented",
            )
        ],
    )
    summary = _run(project)
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["authors"] == authors
    assert entry["provenance"]["authors"]["source"] == "explanatory_ledger"
    assert "authors" not in entry["missing_fields"]
    assert entry["quality_rejections"] == []
    assert summary["audit"]["corrupt_metadata_field_rejections"] == 0


def test_genuine_chinese_title_survives(tmp_path):
    marker = "doi:10.1000/chinese"
    chinese_title = (
        "\u57fa\u4e8e\u7269\u7406\u4fe1\u606f\u7684\u795e\u7ecf"
        "\u7f51\u7edc\u9006\u8bbe\u8ba1\u7efc\u8ff0"
    )
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S05\n\nChinese title [REF:{marker}].\n",
        ledger_records=[
            _ledger_record(
                marker_id=marker,
                title=chinese_title,
                authors=["\u5f20\u4e09", "\u674e\u56db"],
                year=2024,
                venue="\u4e2d\u56fd\u6fc0\u5149",
                doi="10.1000/chinese",
            )
        ],
    )
    summary = _run(project)
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == chinese_title
    assert entry["authors"] == ["\u5f20\u4e09", "\u674e\u56db"]
    assert entry["provenance"]["title"]["source"] == "explanatory_ledger"
    assert entry["quality_rejections"] == []
    assert summary["audit"]["corrupt_metadata_field_rejections"] == 0


def test_unresolved_corruption_transparent_no_question_marks(tmp_path):
    marker = "doi:10.1000/still-corrupt"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S06\n\nCorrupt and offline [REF:{marker}].\n",
        ledger_records=[
            _ledger_record(
                marker_id=marker,
                title="End\u9366?nd-to-End Learning",
                authors=["\u941a\u581d\u6093Bad Name"],
                year=2023,
                venue="Jou\u9369?nal of Optics",
                doi="10.1000/still-corrupt",
            )
        ],
    )
    summary = _run(project)  # offline: no providers available
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == ""
    assert entry["authors"] == []
    assert entry["venue"] == ""
    assert "??" not in entry["title"]
    assert "?" not in "".join(str(item) for item in entry["authors"])
    assert "title" in entry["missing_fields"]
    assert any(
        "corrupt metadata rejected" in note for note in entry["resolution_notes"]
    )
    assert any(
        rejection["field"] == "title"
        for rejection in entry["quality_rejections"]
    )
    assert summary["audit"]["corrupt_metadata_field_rejections"] == 3
    assert summary["audit"]["corrupt_metadata_field_rejections_by_field"] == {
        "title": 1,
        "authors": 1,
        "venue": 1,
    }


def _supplemental_record(
    *,
    identities: list[str] | None = None,
    title: str,
    authors: list[str] | None = None,
    year: Any = None,
    venue: str = "",
    doi: str = "",
    url: str = "",
    source: str = "review_bibliography",
    source_path_or_url: str = "outputs/review/REVIEW_BIBLIOGRAPHY.json",
    reason: str = "recovered from reviewed bibliography",
) -> dict[str, Any]:
    record: dict[str, Any] = {"title": title}
    if identities:
        record["identities"] = identities
    if authors:
        record["authors"] = authors
    if year is not None:
        record["year"] = year
    if venue:
        record["venue"] = venue
    if doi:
        record["doi"] = doi
    if url:
        record["url"] = url
    record["provenance"] = {
        "source": source,
        "source_path_or_url": source_path_or_url,
        "reason": reason,
    }
    return record


def test_supplemental_metadata_fills_title_only_entry(tmp_path):
    marker = "hash:cccccccccccccccc"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S03\n\nTitle-only [REF:{marker}].\n",
        staged_context={
            "schema_version": "optomind.staged_manuscript_context.v1",
            "citation_inventory": [
                {
                    "citation_id": marker,
                    "title": "Reviewed Bibliography Paper",
                    "trust_type": "core_evidence",
                    "provenance": {"section_id": "S03"},
                }
            ],
            "local_background_candidates": [],
        },
    )
    supplemental = _write_json(
        tmp_path / "supplemental.json",
        {
            "schema_version": "optomind.publication_metadata_supplement.v1",
            "records": [
                _supplemental_record(
                    identities=[marker],
                    title="Reviewed Bibliography Paper",
                    authors=["Review Author"],
                    year=2024,
                    venue="Review Venue",
                    doi="10.1000/reviewed",
                    url="https://doi.org/10.1000/reviewed",
                )
            ],
        },
    )
    summary = _run(
        project,
        staged_context_path=project["staged_context"],
        supplemental_metadata_paths=[supplemental],
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == "Reviewed Bibliography Paper"
    assert entry["authors"] == ["Review Author"]
    assert entry["year"] == "2024"
    assert entry["venue"] == "Review Venue"
    assert entry["doi"] == "10.1000/reviewed"
    assert entry["url"] == "https://doi.org/10.1000/reviewed"
    assert entry["resolution_status"] == "resolved"
    assert entry["missing_fields"] == []
    assert entry["provenance"]["title"]["source"] == "supplemental_metadata"
    assert entry["provenance"]["title"]["base_source"] == "review_bibliography"
    assert entry["provenance"]["title"]["source_path"] == (
        "outputs/review/REVIEW_BIBLIOGRAPHY.json"
    )
    assert entry["provenance"]["title"]["confidence"] == "high"
    assert "supplemental_metadata" in entry["candidate_sources"]
    assert summary["audit"]["source_counts"]["supplemental_metadata"] == 1
    assert summary["audit"]["supplemental_metadata_file_count"] == 1
    assert summary["audit"]["supplemental_metadata_record_count"] == 1
    assert summary["audit"]["provider_calls"] == {
        "openalex": 0,
        "crossref": 0,
        "s2": 0,
    }


def test_supplemental_metadata_file_count_excludes_ledgers(tmp_path):
    marker = "hash:1111111111111111"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S01\n\nSupp [REF:{marker}].\n",
        staged_context={
            "schema_version": "optomind.staged_manuscript_context.v1",
            "citation_inventory": [
                {
                    "citation_id": marker,
                    "title": "Supp Count Paper",
                    "trust_type": "core_evidence",
                    "provenance": {"section_id": "S01"},
                }
            ],
            "local_background_candidates": [],
        },
    )
    # Add a second handoff section with its own ledger + packet to prove
    # ledgers are source files but never supplemental files.
    asset2 = tmp_path / "outputs" / "enhanced_S02"
    asset2.mkdir(parents=True, exist_ok=True)
    _write_json(
        asset2 / "EXPLANATORY_CITATION_LEDGER.json",
        {
            "schema_version": "chapter_asset_enhancer.v1",
            "section_id": "S02",
            "records": [],
        },
    )
    _write_json(
        asset2 / "input_packet.json",
        {
            "section_id": "S02",
            "evidence_packets": [],
            "literature_coverage": {"sources": []},
        },
    )
    handoff = json.loads(project["handoff"].read_text(encoding="utf-8"))
    handoff["section_order"] = ["S01", "S02"]
    handoff["sections"]["S02"] = {
        "section_id": "S02",
        "explanatory_citation_ledger": {
            "path": "outputs/enhanced_S02/EXPLANATORY_CITATION_LEDGER.json"
        },
        "authoritative_input_packet": {
            "path": "outputs/enhanced_S02/input_packet.json"
        },
    }
    project["handoff"].write_text(
        json.dumps(handoff, ensure_ascii=False),
        encoding="utf-8",
    )
    supplemental = _write_json(
        tmp_path / "supplemental.json",
        [
            _supplemental_record(
                identities=[marker],
                title="Supp Count Paper",
                authors=["Supp Author"],
                year=2023,
                venue="Supp Venue",
                doi="10.1000/supp-count",
            )
        ],
    )
    summary = _run(
        project,
        staged_context_path=project["staged_context"],
        supplemental_metadata_paths=[supplemental],
    )
    catalog = _read_catalog(project)
    assert summary["audit"]["supplemental_metadata_file_count"] == 1
    assert summary["audit"]["supplemental_metadata_record_count"] == 1
    input_paths = {
        source["path"] for source in catalog["input"]["input_files"]
    }
    assert "outputs/enhanced_S01/EXPLANATORY_CITATION_LEDGER.json" in input_paths
    assert "outputs/enhanced_S02/EXPLANATORY_CITATION_LEDGER.json" in input_paths
    assert "outputs/enhanced_S02/input_packet.json" in input_paths
    assert any(path.endswith("supplemental.json") for path in input_paths)
    entry = _entry_by_marker(catalog, marker)
    assert entry["provenance"]["title"]["source"] == "supplemental_metadata"


def test_supplemental_metadata_title_match_fills_missing_fields(tmp_path):
    marker = "hash:dddddddddddddddd"
    title = "Title-Only Paper Recovered From Review"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S04\n\nTitle-only [REF:{marker}].\n",
        staged_context={
            "schema_version": "optomind.staged_manuscript_context.v1",
            "citation_inventory": [
                {
                    "citation_id": marker,
                    "title": title,
                    "trust_type": "core_evidence",
                    "provenance": {"section_id": "S04"},
                }
            ],
            "local_background_candidates": [],
        },
    )
    supplemental = _write_json(
        tmp_path / "supplemental.json",
        [
            _supplemental_record(
                title=title,
                authors=["Recovered Author"],
                year=2023,
                venue="Recovered Venue",
                doi="10.1000/recovered",
            )
        ],
    )
    summary = _run(
        project,
        staged_context_path=project["staged_context"],
        supplemental_metadata_paths=[supplemental],
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == title
    assert entry["authors"] == ["Recovered Author"]
    assert entry["year"] == "2023"
    assert entry["venue"] == "Recovered Venue"
    assert entry["doi"] == "10.1000/recovered"
    assert entry["resolution_status"] == "resolved"
    assert entry["provenance"]["title"]["source"] == "supplemental_metadata"
    assert entry["provenance"]["title"]["confidence"] == "medium"
    assert "match: title" in entry["provenance"]["title"]["reason"]
    assert summary["audit"]["supplemental_metadata_record_count"] == 1


def test_supplemental_metadata_does_not_override_clean_local_fields(tmp_path):
    marker = "doi:10.1000/local-clean"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S05\n\nLocal clean [REF:{marker}].\n",
        ledger_records=[
            _ledger_record(
                marker_id=marker,
                title="Clean Local Title",
                authors=["Local Author"],
                year=2021,
                venue="Local Venue",
                doi="10.1000/local-clean",
            )
        ],
    )
    supplemental = _write_json(
        tmp_path / "supplemental.json",
        [
            _supplemental_record(
                identities=[marker],
                title="Supplemental Title Must Not Win",
                authors=["Supplemental Author"],
                year=1999,
                venue="Supplemental Venue",
                doi="10.1000/local-clean",
            )
        ],
    )
    summary = _run(
        project,
        supplemental_metadata_paths=[supplemental],
    )
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == "Clean Local Title"
    assert entry["authors"] == ["Local Author"]
    assert entry["year"] == "2021"
    assert entry["venue"] == "Local Venue"
    assert entry["provenance"]["title"]["source"] == "explanatory_ledger"
    assert entry["provenance"]["authors"]["source"] == "explanatory_ledger"
    assert summary["audit"]["supplemental_metadata_record_count"] == 1


def test_supplemental_metadata_malformed_or_missing_provenance_refuses(tmp_path):
    marker = "doi:10.1000/prov"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S06\n\nProv [REF:{marker}].\n",
    )
    missing_file = tmp_path / "missing-supplemental.json"
    with pytest.raises(PublicationMetadataError, match="supplemental metadata not found"):
        build_publication_metadata_catalog(
            staged_manuscript_path=project["manuscript"],
            handoff_path=project["handoff"],
            project_root=project["root"],
            output_dir=project["root"] / "out",
            supplemental_metadata_paths=[missing_file],
            scan_material_caches=False,
            include_s2_cache=False,
        )
    no_provenance = _write_json(
        tmp_path / "no-provenance.json",
        [{"title": "No Provenance", "doi": "10.1000/prov"}],
    )
    with pytest.raises(PublicationMetadataError, match="requires a provenance object"):
        build_publication_metadata_catalog(
            staged_manuscript_path=project["manuscript"],
            handoff_path=project["handoff"],
            project_root=project["root"],
            output_dir=project["root"] / "out",
            supplemental_metadata_paths=[no_provenance],
            scan_material_caches=False,
            include_s2_cache=False,
        )
    missing_reason = _write_json(
        tmp_path / "missing-reason.json",
        [
            {
                "title": "Missing Reason",
                "provenance": {"source": "x", "source_path_or_url": "y"},
            }
        ],
    )
    with pytest.raises(PublicationMetadataError, match="provenance is missing 'reason'"):
        build_publication_metadata_catalog(
            staged_manuscript_path=project["manuscript"],
            handoff_path=project["handoff"],
            project_root=project["root"],
            output_dir=project["root"] / "out",
            supplemental_metadata_paths=[missing_reason],
            scan_material_caches=False,
            include_s2_cache=False,
        )
    no_identity = _write_json(
        tmp_path / "no-identity.json",
        [
            {
                "provenance": {"source": "x", "source_path_or_url": "y", "reason": "z"},
            }
        ],
    )
    with pytest.raises(PublicationMetadataError, match="at least one identity or a title"):
        build_publication_metadata_catalog(
            staged_manuscript_path=project["manuscript"],
            handoff_path=project["handoff"],
            project_root=project["root"],
            output_dir=project["root"] / "out",
            supplemental_metadata_paths=[no_identity],
            scan_material_caches=False,
            include_s2_cache=False,
        )


def test_supplemental_metadata_aliases_and_doi_dedupe_preserved(tmp_path):
    hash_marker = "hash:eeeeeeeeeeeeeeee"
    doi_marker = "doi:10.1000/dedupe-supp"
    project = _make_project(
        tmp_path,
        manuscript_text=(
            "## S07\n\n"
            f"Hash ref [REF:{hash_marker}] and doi ref [REF:{doi_marker}].\n"
        ),
    )
    supplemental = _write_json(
        tmp_path / "supplemental.json",
        [
            _supplemental_record(
                identities=[hash_marker],
                title="Dedupe Supplemental Paper",
                authors=["Dedupe Author"],
                year=2022,
                venue="Dedupe Venue",
                doi="10.1000/dedupe-supp",
            )
        ],
    )
    summary = _run(
        project,
        supplemental_metadata_paths=[supplemental],
    )
    catalog = _read_catalog(project)
    assert catalog["audit"]["catalog_entry_count"] == 1
    assert catalog["audit"]["deduplicated_identity_count"] == 1
    entry = catalog["entries"][0]
    assert entry["canonical_identity"] == doi_marker
    assert entry["markers"] == [hash_marker, doi_marker]
    assert entry["marker_count"] == 2
    assert hash_marker in entry["aliases"]
    assert doi_marker in entry["aliases"]
    assert entry["provenance"]["title"]["source"] == "supplemental_metadata"
    assert summary["audit"]["source_counts"]["supplemental_metadata"] == 1


def test_supplemental_metadata_deterministic_rerun(tmp_path):
    marker = "hash:ffffffffffffffff"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S08\n\nSupp [REF:{marker}].\n",
        staged_context={
            "schema_version": "optomind.staged_manuscript_context.v1",
            "citation_inventory": [
                {
                    "citation_id": marker,
                    "title": "Deterministic Supplemental Paper",
                    "trust_type": "core_evidence",
                    "provenance": {"section_id": "S08"},
                }
            ],
            "local_background_candidates": [],
        },
    )
    supplemental = _write_json(
        tmp_path / "supplemental.json",
        [
            _supplemental_record(
                title="Deterministic Supplemental Paper",
                authors=["D Author"],
                year=2020,
                venue="D Venue",
                doi="10.1000/deterministic",
            )
        ],
    )
    _run(
        project,
        staged_context_path=project["staged_context"],
        supplemental_metadata_paths=[supplemental],
    )
    first = (project["output"] / CATALOG_FILENAME).read_bytes()
    second_project = {**project, "output": project["root"] / "out2"}
    _run(
        second_project,
        staged_context_path=project["staged_context"],
        supplemental_metadata_paths=[supplemental],
    )
    second = (second_project["output"] / CATALOG_FILENAME).read_bytes()
    assert first == second
    first_catalog = json.loads(first)
    assert any(
        source["path"].endswith("supplemental.json")
        for source in first_catalog["input"]["input_files"]
    )


def test_cli_supplemental_metadata_flag(tmp_path):
    marker = "hash:abababababababab"
    project = _make_project(
        tmp_path,
        manuscript_text=f"## S01\n\nCLI supp [REF:{marker}].\n",
        staged_context={
            "schema_version": "optomind.staged_manuscript_context.v1",
            "citation_inventory": [
                {
                    "citation_id": marker,
                    "title": "CLI Supplemental Paper",
                    "trust_type": "core_evidence",
                    "provenance": {"section_id": "S01"},
                }
            ],
            "local_background_candidates": [],
        },
    )
    supplemental = _write_json(
        tmp_path / "supplemental.json",
        [
            _supplemental_record(
                title="CLI Supplemental Paper",
                authors=["CLI Author"],
                year=2025,
                venue="CLI Venue",
                doi="10.1000/cli-supp",
            )
        ],
    )
    output_dir = project["root"] / "cli-out"
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.resolve_publication_metadata import main

    exit_code = main(
        [
            "--staged-manuscript",
            str(project["manuscript"]),
            "--handoff",
            str(project["handoff"]),
            "--project-root",
            str(project["root"]),
            "--output-dir",
            str(output_dir),
            "--no-material-caches",
            "--no-s2-cache",
            "--supplemental-metadata",
            str(supplemental),
        ]
    )
    assert exit_code == 0
    catalog = json.loads(
        (output_dir / CATALOG_FILENAME).read_text(encoding="utf-8")
    )
    entry = _entry_by_marker(catalog, marker)
    assert entry["title"] == "CLI Supplemental Paper"
    assert entry["doi"] == "10.1000/cli-supp"
    assert catalog["audit"]["source_counts"]["supplemental_metadata"] == 1
    assert catalog["audit"]["supplemental_metadata_record_count"] == 1


def test_catalog_schema_and_audit_shape(tmp_path):
    project = _make_project(
        tmp_path,
        manuscript_text="## S01\n\nA [REF:doi:10.1000/x].\n",
        ledger_records=[
            _ledger_record(
                marker_id="doi:10.1000/x",
                title="Schema Paper",
                authors=["Schema Author"],
                year=2023,
                venue="Schema Venue",
                doi="10.1000/x",
            )
        ],
    )
    summary = _run(project)
    catalog = _read_catalog(project)
    assert catalog["schema_version"] == SCHEMA_VERSION
    assert set(catalog.keys()) >= {
        "schema_version",
        "entries",
        "records",
        "audit",
        "malformed_refs",
        "input",
        "input_fingerprint",
        "catalog_fingerprint",
    }
    audit = catalog["audit"]
    for key in (
        "total_ref_markers",
        "unique_ref_identities",
        "catalog_entry_count",
        "deduplicated_identity_count",
        "resolution_status_counts",
        "identity_kind_counts",
        "missing_field_counts",
        "source_counts",
        "enriched_by_openalex_count",
        "enriched_by_crossref_count",
        "enriched_by_s2_count",
        "placeholder_year_1900_rejected_count",
        "corrupt_metadata_field_rejections",
        "corrupt_metadata_field_rejections_by_field",
        "supplemental_metadata_file_count",
        "supplemental_metadata_record_count",
        "malformed_ref_count",
        "with_doi_count",
        "with_title_count",
        "with_authors_count",
        "with_year_count",
        "with_venue_count",
        "with_url_count",
    ):
        assert key in audit
    assert audit["total_ref_markers"] == 1
    assert audit["unique_ref_identities"] == 1
    assert audit["catalog_entry_count"] == 1
    assert audit["with_year_count"] == 1
    assert (project["output"] / "PUBLICATION_METADATA_AUDIT.json").is_file()
    assert summary["audit"]["total_ref_markers"] == 1


def test_explanatory_ledger_representative_application_resolves_locally(
    tmp_path,
):
    """Application records merged into top-level ledger records resolve."""

    marker = "doi:10.1000/app-case"
    project = _make_project(
        tmp_path,
        manuscript_text=(
            "## Introduction\n\n"
            "One representative application recovered boundary values "
            f"[REF:{marker}].\n"
        ),
        ledger_records=[
            {
                "handle": "X02_PINN_APPLICATION",
                "role": "explanatory_context",
                "permission": "background_explanation_only",
                "benefit_types": ["representative_application"],
                "marker_id": marker,
                "retrieval_origin": "local_metadata",
                "metadata": {
                    "paper_id": marker,
                    "doi": "10.1000/app-case",
                    "title": "PINN Application Case Study",
                    "authors": ["A. Author"],
                    "year": 2024,
                    "venue": "Applied Optics",
                },
            }
        ],
    )
    _run(project)
    catalog = _read_catalog(project)
    entry = _entry_by_marker(catalog, marker)
    assert entry["resolution_status"] == "resolved"
    assert entry["title"] == "PINN Application Case Study"
    assert entry["authors"] == ["A. Author"]
    assert entry["year"] == "2024"
    assert entry["venue"] == "Applied Optics"
    assert entry["provenance"]["title"]["source"] == "explanatory_ledger"
