"""Focused tests for the unified full-manuscript handoff builder."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

import pytest

from optomind_research.runtime.full_manuscript_handoff import (
    SCHEMA_VERSION,
    REPAIR_REPORT_JSON,
    UNIFIED_HANDOFF_JSON,
    HandoffBuildError,
    build_full_manuscript_handoff,
)
from optomind_research.runtime.publication_metadata_resolver import (
    CATALOG_FILENAME,
    build_publication_metadata_catalog,
)


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory."""
    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-full-manuscript-handoff"
    )
    root.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = root / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _write_section(
    root: Path,
    section_id: str,
    title: str,
    *,
    refs: list[str] | None = None,
    plan_section_id: str | None = None,
    has_review: bool = True,
    plan_title: str | None = None,
    ledger_marker_id: str | None = None,
    ledger_paper_id: str = "doi:10.1000/x",
    explanation_section_id: str | None = None,
    packet_section_id: str | None = None,
) -> dict[str, str]:
    asset_dir = root / f"enhanced_{section_id}"
    asset_dir.mkdir(parents=True, exist_ok=True)
    markers = refs or ["doi:10.1000/x", "paper-1"]
    body = "\n\n".join(
        f"Paragraph {index} with [REF:{marker}]."
        for index, marker in enumerate(markers)
    )
    (asset_dir / "ENHANCED_CHAPTER.md").write_text(
        f"# {title}\n\n{body}\n", encoding="utf-8"
    )
    plan: dict[str, Any] = {
        "schema_version": "chapter_asset_enhancer.v1",
        "section_id": plan_section_id or section_id,
        "plan": {
            "chapter_thesis": "thesis",
            "reader_takeaway": "takeaway",
            "argument_sequence": [],
        },
        "warnings": [],
    }
    if plan_title is not None:
        plan["plan"]["title"] = plan_title
    (asset_dir / "CHAPTER_ARGUMENT_PLAN.json").write_text(
        json.dumps(plan), encoding="utf-8"
    )
    (asset_dir / "CLAIM_TO_PARAGRAPH_MAP.json").write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "section_id": section_id,
                "claim_to_paragraph": {},
                "block_to_claim": [],
            }
        ),
        encoding="utf-8",
    )
    explanation_blocks: dict[str, Any] = {
        "schema_version": "v1",
        "blocks": [],
    }
    if explanation_section_id is not None:
        explanation_blocks["section_id"] = explanation_section_id
    (asset_dir / "EXPLANATION_BLOCKS.json").write_text(
        json.dumps(explanation_blocks), encoding="utf-8"
    )
    ledger = {
        "schema_version": "v1",
        "section_id": section_id,
        "records": [
            {
                "handle": "X01",
                "role": "explanatory_context",
                "permission": "background_explanation_only",
                "metadata": {
                    "paper_id": ledger_paper_id,
                    "doi": "10.1000/x",
                },
            }
        ],
    }
    if ledger_marker_id is not None:
        ledger["records"][0]["marker_id"] = ledger_marker_id
    (asset_dir / "EXPLANATORY_CITATION_LEDGER.json").write_text(
        json.dumps(ledger), encoding="utf-8"
    )
    (asset_dir / "ENHANCEMENT_REPORT.json").write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "section_id": section_id,
                "status": "enhanced",
                "word_counts": {"enhanced": 40},
                "hard_defects": {},
            }
        ),
        encoding="utf-8",
    )
    if has_review:
        (asset_dir / "BLOCK_SCIENTIFIC_REVIEW.json").write_text(
            json.dumps(
                {
                    "schema_version": "v1",
                    "section_id": section_id,
                    "attempted": True,
                    "available": True,
                    "advisory_count": 0,
                    "blocking_count": 0,
                    "comments": [],
                }
            ),
            encoding="utf-8",
        )
    packet = {
        "schema_version": "s04_acceptance.v1",
        "section_id": packet_section_id or section_id,
        "claims": [],
        "evidence_packets": [
            {"paper_id": "paper-1", "chunk_id": "chunk-1"},
            {"paper_id": "doi:10.1000/x", "chunk_id": "chunk-2"},
        ],
        "literature_coverage": {
            "sources": [{"paper_id": "paper-1", "chunk_id": "chunk-1"}]
        },
        "manuscript_context": {"evidence_provenance": {}},
    }
    packet_path = root / f"packet_{section_id}.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    return {
        "enhanced_asset_dir": str(asset_dir),
        "authoritative_input_packet": str(packet_path),
    }


def _manifest(root: Path, sections: list[Mapping[str, Any]]) -> Path:
    path = root / "manifest.json"
    path.write_text(
        json.dumps({"schema_version": "optomind.full_manuscript_handoff.manifest.v2", "project_root": str(root), "sections": list(sections)}),
        encoding="utf-8",
    )
    return path


def _section_spec(section_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    return {"section_id": section_id, **dict(fields)}


def test_happy_path_two_sections(tmp_path: Path) -> None:
    s01 = _write_section(tmp_path, "S01", "Foundations of Inverse Design")
    s02 = _write_section(tmp_path, "S02", "Credibility of Simulators")
    manifest = _manifest(
        tmp_path,
        [
            _section_spec("S01", s01),
            _section_spec("S02", s02),
        ],
    )
    output = tmp_path / "out"

    summary = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=output
    )

    assert summary["schema_version"] == SCHEMA_VERSION
    assert summary["section_order"] == ["S01", "S02"]
    assert summary["aggregate_counts"]["section_count"] == 2
    assert summary["aggregate_counts"]["total_word_count"] == 80
    unified = json.loads(
        (output / UNIFIED_HANDOFF_JSON).read_text(encoding="utf-8")
    )
    assert (output / REPAIR_REPORT_JSON).is_file()
    s01_entry = unified["sections"]["S01"]
    assert s01_entry["section_title"] == "Foundations of Inverse Design"
    assert s01_entry["chapter_status"] == "enhanced"
    assert s01_entry["word_count"] == 40
    assert s01_entry["enhanced_chapter"]["path"].startswith("enhanced_S01/")
    assert not Path(s01_entry["enhanced_chapter"]["path"]).is_absolute()
    assert s01_entry["enhanced_chapter"]["sha256"]
    assert s01_entry["reviewer_notes"]["status"] == "present"


def test_relocation_safe_fingerprint(tmp_path: Path) -> None:
    s01 = _write_section(tmp_path, "S01", "Same Title")
    manifest1 = _manifest(tmp_path, [_section_spec("S01", s01)])
    first = build_full_manuscript_handoff(
        manifest_path=manifest1, output_dir=tmp_path / "out1"
    )

    root2 = tmp_path / "other_root"
    root2.mkdir()
    shutil.copytree(tmp_path / "enhanced_S01", root2 / "enhanced_S01")
    shutil.copy2(tmp_path / "packet_S01.json", root2 / "packet_S01.json")
    manifest2 = _manifest(
        root2,
        [
            {
                "section_id": "S01",
                "enhanced_asset_dir": "enhanced_S01",
                "authoritative_input_packet": "packet_S01.json",
            }
        ],
    )
    second = build_full_manuscript_handoff(
        manifest_path=manifest2, output_dir=tmp_path / "out2"
    )

    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["section_order"] == second["section_order"]


def test_title_repair_from_plan_and_heading(tmp_path: Path) -> None:
    s01 = _write_section(
        tmp_path,
        "S01",
        "Heading Title",
        plan_title="Plan Title",
    )
    manifest = _manifest(tmp_path, [_section_spec("S01", s01)])
    summary = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=tmp_path / "out"
    )
    assert summary["aggregate_counts"]["title_repair_count"] == 1
    unified = json.loads(
        (tmp_path / "out" / UNIFIED_HANDOFF_JSON).read_text(encoding="utf-8")
    )
    assert unified["sections"]["S01"]["section_title"] == "Plan Title"
    assert unified["sections"]["S01"]["provenance"]["title_repair"][
        "source"
    ] == "chapter_argument_plan"

    s02 = _write_section(tmp_path, "S02", "Heading Only Title")
    manifest2 = _manifest(tmp_path, [_section_spec("S02", s02)])
    summary2 = build_full_manuscript_handoff(
        manifest_path=manifest2, output_dir=tmp_path / "out2"
    )
    unified2 = json.loads(
        (tmp_path / "out2" / UNIFIED_HANDOFF_JSON).read_text(encoding="utf-8")
    )
    entry = unified2["sections"]["S02"]
    assert entry["section_title"] == "Heading Only Title"
    assert entry["provenance"]["title_repair"]["source"] == (
        "enhanced_chapter_heading"
    )
    repair_report = json.loads(
        (tmp_path / "out2" / REPAIR_REPORT_JSON).read_text(encoding="utf-8")
    )
    assert repair_report["sections"]["S02"]["title_repair"]["source"] == (
        "enhanced_chapter_heading"
    )
    assert summary2["aggregate_counts"]["title_repair_count"] == 1


def test_optional_reviewer_missing_is_fail_open(tmp_path: Path) -> None:
    s01 = _write_section(tmp_path, "S01", "Title", has_review=False)
    manifest = _manifest(tmp_path, [_section_spec("S01", s01)])
    summary = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=tmp_path / "out"
    )
    unified = json.loads(
        (tmp_path / "out" / UNIFIED_HANDOFF_JSON).read_text(encoding="utf-8")
    )
    entry = unified["sections"]["S01"]
    assert entry["reviewer_notes"] is None
    assert entry["optional_file_status"]["BLOCK_SCIENTIFIC_REVIEW.json"] == (
        "missing_fail_open"
    )
    assert summary["aggregate_counts"]["optional_missing_count"] == 2
    assert summary["reused"] is False


def test_section_mismatch_and_missing_required_files_fail(tmp_path: Path) -> None:
    s01 = _write_section(tmp_path, "S01", "Title", plan_section_id="S99")
    manifest = _manifest(tmp_path, [_section_spec("S01", s01)])
    with pytest.raises(HandoffBuildError, match="section_id mismatch"):
        build_full_manuscript_handoff(
            manifest_path=manifest, output_dir=tmp_path / "out"
        )

    s02 = _write_section(tmp_path, "S02", "Title")
    (Path(s02["enhanced_asset_dir"]) / "ENHANCED_CHAPTER.md").unlink()
    manifest2 = _manifest(tmp_path, [_section_spec("S02", s02)])
    with pytest.raises(
        HandoffBuildError, match="required ENHANCED_CHAPTER.md missing or empty"
    ):
        build_full_manuscript_handoff(
            manifest_path=manifest2, output_dir=tmp_path / "out2"
        )


def test_core_and_explanatory_trust_boundaries_preserved(
    tmp_path: Path,
) -> None:
    s01 = _write_section(tmp_path, "S01", "Title")
    manifest = _manifest(tmp_path, [_section_spec("S01", s01)])
    build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=tmp_path / "out"
    )
    unified = json.loads(
        (tmp_path / "out" / UNIFIED_HANDOFF_JSON).read_text(encoding="utf-8")
    )
    entry = unified["sections"]["S01"]
    assert entry["authoritative_input_packet"]["role"] == "core_evidence"
    assert entry["explanatory_citation_ledger"]["trust_boundary"] == (
        "background_explanation_only"
    )
    ledger_file = (
        tmp_path
        / Path(s01["enhanced_asset_dir"])
        / "EXPLANATORY_CITATION_LEDGER.json"
    )
    ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    assert ledger["records"][0]["permission"] == "background_explanation_only"
    assert entry["provenance"]["explanatory_trust_boundary"] == (
        "background_explanation_only"
    )


def test_unknown_ref_marker_is_hard_defect_without_rewrite(
    tmp_path: Path,
) -> None:
    s01 = _write_section(
        tmp_path,
        "S01",
        "Title",
        refs=["doi:10.1000/x", "paper-1", "unknown-paper"],
    )
    manifest = _manifest(tmp_path, [_section_spec("S01", s01)])
    summary = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=tmp_path / "out"
    )
    assert summary["aggregate_counts"]["hard_defect_count"] == 1
    assert "S01:unknown_ref_marker:unknown-paper" in summary["hard_defects"]
    unified = json.loads(
        (tmp_path / "out" / UNIFIED_HANDOFF_JSON).read_text(encoding="utf-8")
    )
    assert unified["sections"]["S01"]["hard_defects"] == [
        "unknown_ref_marker:unknown-paper"
    ]
    markdown = (
        Path(s01["enhanced_asset_dir"]) / "ENHANCED_CHAPTER.md"
    ).read_text(encoding="utf-8")
    assert "[REF:unknown-paper]" in markdown  # prose never rewritten


def test_nested_ledger_application_identity_is_accepted(
    tmp_path: Path,
) -> None:
    doi = "doi:10.1002/adpr.202400191"
    s01 = _write_section(tmp_path, "S01", "Title", refs=[doi])
    ledger_path = (
        Path(s01["enhanced_asset_dir"]) / "EXPLANATORY_CITATION_LEDGER.json"
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["records"][0]["applications"] = [{"marker_id": doi}]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    manifest = _manifest(tmp_path, [_section_spec("S01", s01)])
    summary = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=tmp_path / "out"
    )

    assert summary["aggregate_counts"]["hard_defect_count"] == 0
    assert f"S01:unknown_ref_marker:{doi}" not in summary["hard_defects"]


def test_repeated_run_is_idempotent(tmp_path: Path) -> None:
    s01 = _write_section(tmp_path, "S01", "Title")
    manifest = _manifest(tmp_path, [_section_spec("S01", s01)])
    output = tmp_path / "out"
    first = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=output
    )
    unified_bytes = (output / UNIFIED_HANDOFF_JSON).read_bytes()

    second = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=output
    )

    assert second["reused"] is True
    assert second["input_fingerprint"] == first["input_fingerprint"]
    assert (output / UNIFIED_HANDOFF_JSON).read_bytes() == unified_bytes


def test_manifest_validation_errors(tmp_path: Path) -> None:
    manifest = tmp_path / "bad.json"
    manifest.write_text(
        json.dumps(
                {
                    "schema_version": "optomind.full_manuscript_handoff.manifest.v2",
                    "project_root": str(tmp_path),
                "sections": [{"section_id": "S01"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(HandoffBuildError, match="enhanced_asset_dir"):
        build_full_manuscript_handoff(
            manifest_path=manifest, output_dir=tmp_path / "out"
        )


def test_ledger_marker_id_exact_match_not_flagged(tmp_path: Path) -> None:
    s01 = _write_section(
        tmp_path,
        "S01",
        "Title",
        refs=["rawhash123"],
        ledger_marker_id="rawhash123",
        ledger_paper_id="s2:abc",
    )
    manifest = _manifest(tmp_path, [_section_spec("S01", s01)])
    summary = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=tmp_path / "out"
    )
    assert summary["aggregate_counts"]["hard_defect_count"] == 0


def test_packet_and_explanation_blocks_section_id_mismatch_blocking(
    tmp_path: Path,
) -> None:
    s01 = _write_section(tmp_path, "S01", "Title", packet_section_id="S99")
    manifest = _manifest(tmp_path, [_section_spec("S01", s01)])
    with pytest.raises(
        HandoffBuildError,
        match="section_id mismatch.*authoritative input packet",
    ):
        build_full_manuscript_handoff(
            manifest_path=manifest, output_dir=tmp_path / "out"
        )

    s02 = _write_section(
        tmp_path, "S02", "Title", explanation_section_id="S98"
    )
    manifest2 = _manifest(tmp_path, [_section_spec("S02", s02)])
    with pytest.raises(
        HandoffBuildError,
        match="section_id mismatch.*EXPLANATION_BLOCKS",
    ):
        build_full_manuscript_handoff(
            manifest_path=manifest2, output_dir=tmp_path / "out2"
        )


def test_duplicate_section_ids_rejected(tmp_path: Path) -> None:
    s01 = _write_section(tmp_path, "S01", "Title")
    manifest = tmp_path / "dup.json"
    manifest.write_text(
        json.dumps(
                {
                    "schema_version": "optomind.full_manuscript_handoff.manifest.v2",
                    "project_root": str(tmp_path),
                "sections": [
                    _section_spec("S01", s01),
                    _section_spec("S01", s01),
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(HandoffBuildError, match="duplicate section_id"):
        build_full_manuscript_handoff(
            manifest_path=manifest, output_dir=tmp_path / "out"
        )


def test_relative_project_root_resolved_against_manifest_parent(
    tmp_path: Path,
) -> None:
    s01 = _write_section(tmp_path, "S01", "Title")
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    manifest = manifests_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
                {
                    "schema_version": "optomind.full_manuscript_handoff.manifest.v2",
                    "project_root": "..",
                "sections": [
                    {
                        "section_id": "S01",
                        "enhanced_asset_dir": "enhanced_S01",
                        "authoritative_input_packet": "packet_S01.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    summary = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=tmp_path / "out"
    )
    assert summary["section_order"] == ["S01"]
    assert (tmp_path / "out" / UNIFIED_HANDOFF_JSON).is_file()


def test_reused_unified_rewrites_missing_repair_report(
    tmp_path: Path,
) -> None:
    s01 = _write_section(tmp_path, "S01", "Title")
    manifest = _manifest(tmp_path, [_section_spec("S01", s01)])
    output = tmp_path / "out"
    first = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=output
    )
    repair_path = output / REPAIR_REPORT_JSON
    assert repair_path.is_file()
    repair_path.unlink()

    second = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=output
    )
    assert second["reused"] is True
    assert repair_path.is_file()
    repair = json.loads(repair_path.read_text(encoding="utf-8"))
    assert repair["input_fingerprint"] == first["input_fingerprint"]


def test_application_ledger_records_survive_handoff_and_resolve_metadata(
    tmp_path: Path,
) -> None:
    """Application REF markers survive handoff and bibliography resolution."""

    s01 = _write_section(tmp_path, "S01", "Foundations of Inverse Design")
    asset_dir = tmp_path / "enhanced_S01"
    application_marker = "doi:10.1000/app-case"
    # Rewrite the enhanced chapter with the application marker.
    (asset_dir / "ENHANCED_CHAPTER.md").write_text(
        (
            "# Foundations of Inverse Design\n\n"
            "PINNs embed PDEs in the loss [REF:doi:10.1000/x]. "
            "One application recovered boundary values "
            f"[REF:{application_marker}].\n"
        ),
        encoding="utf-8",
    )
    # Canonical top-level ledger records include the representative
    # application record exactly as the enhancer now emits it.
    ledger = {
        "schema_version": "chapter_asset_enhancer.v1",
        "section_id": "S01",
        "records": [
            {
                "handle": "X01_SHARED_PAPER",
                "role": "explanatory_context",
                "permission": "background_explanation_only",
                "marker_id": "doi:10.1000/x",
                "metadata": {
                    "paper_id": "doi:10.1000/x",
                    "doi": "10.1000/x",
                    "title": "Shared Paper",
                },
            },
            {
                "handle": "X02_PINN_APPLICATION",
                "role": "explanatory_context",
                "permission": "background_explanation_only",
                "benefit_types": ["representative_application"],
                "marker_id": application_marker,
                "metadata": {
                    "paper_id": "doi:10.1000/app-case",
                    "doi": "10.1000/app-case",
                    "title": "PINN Application Case Study",
                    "authors": ["A. Author"],
                    "year": 2024,
                    "venue": "Applied Optics",
                },
            },
        ],
    }
    (asset_dir / "EXPLANATORY_CITATION_LEDGER.json").write_text(
        json.dumps(ledger, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest = _manifest(
        tmp_path,
        [
            _section_spec(
                "S01",
                {
                    "enhanced_asset_dir": str(asset_dir),
                    "authoritative_input_packet": str(
                        tmp_path / "packet_S01.json"
                    ),
                },
            )
        ],
    )
    output = tmp_path / "out"
    summary = build_full_manuscript_handoff(
        manifest_path=manifest, output_dir=output
    )

    assert summary["aggregate_counts"]["hard_defect_count"] == 0
    assert summary["hard_defects"] == []

    manuscript = tmp_path / "STAGED_COMPLETE_REVIEW_EN.md"
    manuscript.write_text(
        (
            "## Introduction\n\n"
            f"One application recovered boundary values "
            f"[REF:{application_marker}].\n"
        ),
        encoding="utf-8",
    )
    catalog = build_publication_metadata_catalog(
        staged_manuscript_path=manuscript,
        handoff_path=output / UNIFIED_HANDOFF_JSON,
        project_root=tmp_path,
        output_dir=tmp_path / "catalog_out",
        scan_material_caches=False,
        include_s2_cache=False,
    )
    catalog_path = tmp_path / "catalog_out" / CATALOG_FILENAME
    catalog_data = json.loads(catalog_path.read_text(encoding="utf-8"))
    entry = next(
        (
            row
            for row in catalog_data["entries"]
            if application_marker in row["markers"]
        ),
        None,
    )
    assert entry is not None
    assert entry["resolution_status"] == "resolved"
    assert entry["title"] == "PINN Application Case Study"
    assert entry["authors"] == ["A. Author"]
    assert entry["year"] == "2024"
    assert entry["venue"] == "Applied Optics"
    assert entry["provenance"]["title"]["source"] == "explanatory_ledger"
