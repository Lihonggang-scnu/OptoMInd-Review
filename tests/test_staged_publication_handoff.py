"""Focused tests for the staged-publication handoff builder."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from optomind_research.runtime.staged_publication_handoff import (
    CONTENT_PACKAGE_FILENAME,
    FINAL_REVIEW_FILENAME,
    FINAL_VISUAL_PACKAGE_FILENAME,
    METADATA_CATALOG_FILENAME,
    SECTION_SOURCE_LEDGER_RELPATH,
    StagedPublicationHandoffError,
    build_staged_publication_handoff,
)


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory under the repository."""

    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-staged-publication-handoff"
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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reviewed_manuscript(root: Path) -> Path:
    sections = [
        "Foundations of Physics-Constrained Inverse Design",
        "Forward Modeling: Accuracy, Scalability, and Mesh-Free Advantages",
        "Inverse Design: Gradient Fidelity and Optimization Dynamics",
        "Simulation Credibility: Error Sources and Validation Protocols",
        "Closing the Gap: Robust Design and Uncertainty Quantification",
        "Experimental Validation: Measurement Challenges and Discrepancies",
        "Synthesis: Decision Framework for Method Selection",
        "Future Directions and Unresolved Research Gaps",
    ]
    parts = [
        "## Abstract",
        "",
        "Old abstract body.",
        "",
        "## Introduction",
        "",
        "Old introduction body.",
        "",
    ]
    for index, title in enumerate(sections, start=1):
        parts.extend(
            [
                f"## {title}",
                "",
                f"# {title}",
                "",
                f"Scientific body {index}. [REF:body-{index}]",
                "",
            ]
        )
    parts.extend(["## Conclusion", "", "Old conclusion body.", ""])
    path = root / "inputs" / "reviewed.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _staged_artifact(root: Path, filename: str, text: str) -> Path:
    path = _write_json(
        root / "inputs" / filename,
        {
            "schema_version": "optomind.staged_article_completion.v1",
            "stage": filename.removesuffix(".json").removeprefix("staged_"),
            "status": "completed",
            "payload": {"draft": {"text": text}},
        },
    )
    return path


def _publication_metadata(root: Path) -> Path:
    return _write_json(
        root / "inputs" / "publication_metadata.json",
        {
            "title": "Test Staged Publication",
            "authors": ["Ada Lovelace", "Grace Hopper"],
            "keywords": ["physics", "inverse design", "AI"],
            "date": "2026-08-15",
        },
    )


def _commander(root: Path, order: list[str] | None = None) -> Path:
    order = order or [f"S{index:02d}" for index in range(1, 9)]
    return _write_json(
        root / "inputs" / "commander.json",
        {
            "schema_version": "optomind.global_manuscript_commander.work_order.v2",
            "section_order": [
                {"section_id": section_id, "position": index}
                for index, section_id in enumerate(order)
            ],
            "central_thesis": "Method selection is task-dependent.",
        },
    )


def _catalog(root: Path, markers: list[str]) -> Path:
    entries = []
    records = {}
    for marker in markers:
        entry = {
            "identity": marker,
            "canonical_identity": marker,
            "aliases": [marker, f"alias-{marker}"],
            "markers": [marker],
            "marker_count": 1,
            "sections": [],
            "title": f"Title for {marker}",
            "authors": ["Author One", "Author Two"],
            "year": "2024",
            "venue": "Test Journal",
            "doi": marker if marker.startswith("doi:") else "",
            "url": "https://example.org/" + marker,
            "s2_id": "",
            "provenance": {
                "title": {
                    "source": "local_test",
                    "confidence": "high",
                    "reason": "test fixture",
                }
            },
            "resolution_status": "resolved",
            "missing_fields": [],
            "resolution_notes": [],
        }
        entries.append(entry)
        for alias in entry["aliases"]:
            records[alias] = {
                "paper_id": alias,
                "title": entry["title"],
                "authors": entry["authors"],
                "year": entry["year"],
                "venue": entry["venue"],
                "doi": entry["doi"],
                "url": entry["url"],
                "reference_kind": "article",
                "resolution_status": "resolved",
                "missing_fields": [],
                "markers": entry["markers"],
                "marker_count": entry["marker_count"],
            }
    return _write_json(
        root / "inputs" / "catalog.json",
        {
            "schema_version": "optomind.publication_metadata_resolver.v1",
            "entries": entries,
            "records": records,
            "audit": {
                "total_ref_markers": len(markers),
                "unique_ref_identities": len(markers),
            },
            "catalog_fingerprint": "test-catalog-fingerprint",
        },
    )


def _visual_package(root: Path, *, escaped_path: str | None = None) -> Path:
    package_dir = root / "inputs"
    assets_dir = package_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    ok_image = assets_dir / "ok.png"
    ok_image.write_bytes(b"fake-png-bytes")
    figures = [
        {
            "figure_id": "FIG-OK",
            "local_path": "assets/ok.png",
            "review_decision": "system_approved_test_mode_with_warnings",
            "render_status": "ready",
            "panel_manifest": [{"source_local_path": "assets/ok.png"}],
            "permission_state": {
                "publication_eligible": False,
                "publication_eligible_reason": "source-derived internal study",
            },
        },
        {
            "figure_id": "FIG-MISSING",
            "local_path": "assets/missing.png",
            "review_decision": "system_approved_test_mode_with_warnings",
            "render_status": "ready",
        },
        {
            "figure_id": "FIG-REJECT",
            "local_path": "assets/reject.png",
            "review_decision": "rejected",
            "render_status": "ready",
        },
    ]
    if escaped_path is not None:
        figures[0]["local_path"] = escaped_path
        figures[0]["panel_manifest"][0]["source_local_path"] = escaped_path
    return _write_json(
        package_dir / "visual_package.json",
        {
            "schema_version": "research_harness.final_visual_package.v1",
            "run_id": "visual-test",
            "mode": "reader_explanation",
            "figures": figures,
            "validation": {"status": "passed", "figure_count": len(figures)},
        },
    )


def _build(
    root: Path,
    *,
    output_dir: Path,
    reviewed_path: Path | None = None,
    abstract_text: str = "New abstract.",
    introduction_text: str = "New introduction.",
    conclusion_text: str = "New conclusion.",
    catalog_markers: list[str] | None = None,
    commander_path: Path | None = None,
    section_source_path: Path | None = None,
    visual_path: Path | None = None,
    run_id: str = "handoff-test",
) -> dict[str, Any]:
    reviewed_path = reviewed_path or _reviewed_manuscript(root)
    catalog_markers = catalog_markers or [
        "body-1",
        "body-2",
        "body-3",
        "body-4",
        "body-5",
        "body-6",
        "body-7",
        "body-8",
    ]
    return build_staged_publication_handoff(
        reviewed_manuscript_path=reviewed_path,
        conclusion_artifact_path=_staged_artifact(
            root, "staged_conclusion.json", conclusion_text
        ),
        introduction_artifact_path=_staged_artifact(
            root, "staged_introduction.json", introduction_text
        ),
        abstract_artifact_path=_staged_artifact(
            root, "staged_abstract.json", abstract_text
        ),
        metadata_catalog_path=_catalog(root, catalog_markers),
        visual_package_path=visual_path or _visual_package(root),
        commander_path=commander_path or _commander(root),
        section_source_path=section_source_path,
        publication_metadata_path=_publication_metadata(root),
        project_root=root,
        output_dir=output_dir,
        run_id=run_id,
    )


def test_merge_and_duplicate_heading_removal(tmp_path: Path) -> None:
    output = tmp_path / "out"
    summary = _build(
        tmp_path,
        output_dir=output,
        abstract_text="New abstract with [REF:body-1].",
        introduction_text="New introduction with [REF:body-2].",
        conclusion_text="New conclusion with [REF:body-3].",
    )

    final_review = (output / FINAL_REVIEW_FILENAME).read_text(encoding="utf-8")
    assert "New abstract with [REF:body-1]." in final_review
    assert "New introduction with [REF:body-2]." in final_review
    assert "New conclusion with [REF:body-3]." in final_review
    assert "Old abstract body." not in final_review
    assert "Old introduction body." not in final_review
    assert "Old conclusion body." not in final_review
    assert "Scientific body 1. [REF:body-1]" in final_review

    headings = [
        line for line in final_review.splitlines() if line.startswith("## ")
    ]
    assert len(headings) == 11
    assert sum(1 for heading in headings if heading == "## Abstract") == 1
    assert sum(1 for heading in headings if heading == "## Introduction") == 1
    assert sum(1 for heading in headings if heading == "## Conclusion") == 1
    assert not any(line.startswith("# ") for line in final_review.splitlines())

    assert summary["reviewed_body_ref_marker_sequence"] == [
        f"body-{index}" for index in range(1, 9)
    ]


def test_unknown_front_ref_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StagedPublicationHandoffError, match="not present in metadata catalog"):
        _build(
            tmp_path,
            output_dir=tmp_path / "out",
            introduction_text="New introduction with [REF:not-in-catalog].",
        )


def test_metadata_ledger_aliases_map_to_canonical_record(tmp_path: Path) -> None:
    output = tmp_path / "out"
    _build(
        tmp_path,
        output_dir=output,
        abstract_text="Abstract [REF:doi:10.1000/test].",
        catalog_markers=["body-1", "doi:10.1000/test"],
    )

    ledger = json.loads(
        (output / SECTION_SOURCE_LEDGER_RELPATH).read_text(encoding="utf-8")
    )
    rows_by_paper_id = {row["paper_id"]: row for row in ledger["sources"]}
    assert "doi:10.1000/test" in rows_by_paper_id
    assert "alias-doi:10.1000/test" in rows_by_paper_id
    for paper_id in ("doi:10.1000/test", "alias-doi:10.1000/test"):
        row = rows_by_paper_id[paper_id]
        assert row["canonical_paper_id"] == "doi:10.1000/test"
        assert row["title"] == "Title for doi:10.1000/test"
        assert row["authors"] == ["Author One", "Author Two"]
        assert row["year"] == "2024"


def test_visual_copy_path_rewrite_and_missing_fail_open(tmp_path: Path) -> None:
    output = tmp_path / "out"
    summary = _build(tmp_path, output_dir=output)

    package = json.loads(
        (output / FINAL_VISUAL_PACKAGE_FILENAME).read_text(encoding="utf-8")
    )
    figures = {item["figure_id"]: item for item in package["figures"]}
    ok = figures["FIG-OK"]
    assert ok["local_path"] == "visual_assets/FIG-OK.png"
    assert ok["copy_status"] == "copied_ready"
    assert ok["render_status"] == "ready"
    assert ok["publication_eligible"] is False
    assert ok["permission_state"]["publication_eligible"] is False
    copied = output / ok["local_path"]
    assert copied.is_file()
    assert ok["source_sha256"] == _sha256_bytes(copied.read_bytes())

    audit = package["internal_study_audit"]
    assert audit["copied_asset_count"] == 1
    reasons = {item["figure_id"]: item["reason"] for item in audit["missing_or_rejected"]}
    assert reasons["FIG-MISSING"] == "missing_source_file_fail_open"
    assert reasons["FIG-REJECT"] == "rejected_not_accepted_render_ready"
    assert not (output / "visual_assets" / "FIG-MISSING.png").exists()
    assert not (output / "visual_assets" / "FIG-REJECT.png").exists()
    assert summary["visual_asset_audit"]["missing_or_rejected"]


def test_visual_path_escape_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{uuid.uuid4().hex}.png"
    visual_path = _visual_package(tmp_path, escaped_path=str(outside))
    with pytest.raises(StagedPublicationHandoffError, match="unsafe visual asset path"):
        _build(
            tmp_path,
            output_dir=tmp_path / "out",
            visual_path=visual_path,
        )


def test_missing_duplicate_section_ids_are_refused(tmp_path: Path) -> None:
    duplicate = ["S01", "S01", "S02", "S03", "S04", "S05", "S06", "S07"]
    commander_path = _commander(tmp_path, duplicate)
    with pytest.raises(StagedPublicationHandoffError, match="duplicate section_id"):
        _build(
            tmp_path,
            output_dir=tmp_path / "out",
            commander_path=commander_path,
        )

    incomplete = _commander(tmp_path, ["S01", "S02", "S03", "S04", "S05", "S06", "S07"])
    with pytest.raises(StagedPublicationHandoffError, match="missing scientific section IDs"):
        _build(
            tmp_path,
            output_dir=tmp_path / "out2",
            commander_path=incomplete,
        )


def test_deterministic_rerun_byte_identical(tmp_path: Path) -> None:
    output = tmp_path / "out"
    _build(tmp_path, output_dir=output)
    files = [
        CONTENT_PACKAGE_FILENAME,
        FINAL_REVIEW_FILENAME,
        FINAL_VISUAL_PACKAGE_FILENAME,
        METADATA_CATALOG_FILENAME,
        "publication_metadata.json",
        "REVIEW_BLUEPRINT.json",
        SECTION_SOURCE_LEDGER_RELPATH,
    ]
    first = {name: (output / name).read_bytes() for name in files}

    _build(tmp_path, output_dir=output)

    second = {name: (output / name).read_bytes() for name in files}
    assert first == second
