from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path

import pytest

from optomind_research.runtime.latex_publication_renderer import (
    _clean_unicode,
    _copy_verified_figures,
    _collect_catalog_metadata,
    _citation_key,
    _drop_citation_tokens,
    _find_binary,
    _identify_dropped_references,
    _inject_figures,
    _load_reference_alias_map,
    _normalize_author_list,
    _normalize_metadata,
    _portable_audit_entry,
    _portable_ref,
    _portable_rejected_entry,
    _portable_report_refs,
    _prepare_scientific_markdown,
    _reference_bibtex,
    _replace_reference_markers,
    _resolve_artifact_path,
    _scientific_unicode_preflight,
    _strip_affiliation_marker,
    _strip_embedded_title_and_abstract,
    build_latex_publication,
    resolve_publication_metadata,
)
from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_fixture(
    tmp_path: Path,
    *,
    draft_only: bool = False,
    with_figure: bool = False,
) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    review_path = run_dir / "authoring" / "full_review" / "FINAL_REVIEW_EN.md"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    paragraph = (
        "A dielectric resonator confines electromagnetic energy while a "
        "controlled radiation channel permits far-field interrogation. "
        "The resulting balance connects resonance linewidth, coupling "
        "efficiency, and practical sensing stability. "
    )
    review_path.write_text(
        (
            "## Physical mechanism\n\n"
            + paragraph * 6
            + "[REF:doi:10.1234/example.1].\n\n"
            "### Design implication\n\n"
            + paragraph * 2
        ),
        encoding="utf-8",
    )
    citation_map = (
        run_dir
        / "authoring"
        / "full_review"
        / "FULL_REVIEW_CITATION_MAP.json"
    )
    _write_json(
        citation_map,
        {
            "schema_version": "test",
            "citations": [
                {
                    "paper_id": "doi:10.1234/example.1",
                    "trace_status": "verified",
                }
            ],
        },
    )
    visual_path = run_dir / "visual_editor" / "VISUAL_EDITORIAL_PLAN.json"
    placements = []
    if with_figure:
        from PIL import Image, ImageDraw

        image_path = run_dir / "visual_assets" / "verified.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (480, 220), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 60, 200, 160), outline="navy", width=5)
        draw.rectangle((280, 60, 440, 160), outline="darkred", width=5)
        draw.line((200, 110, 280, 110), fill="black", width=4)
        image.save(image_path)
        placements.append(
            {
                "section_id": "S01",
                "visual_chunk_id": "visual_test_01",
                "local_image_path": str(image_path),
                "caption_preview": "Verified resonator mechanism schematic.",
                "argumentative_purpose": (
                    "Show the relation between confinement and radiation loss."
                ),
                "status": "verified_existing",
            }
        )
    _write_json(
        visual_path,
        {
            "schema_version": "test",
            "placements": placements,
            "conceptual_figure_requests": [],
            "unfilled_visual_needs": [],
        },
    )
    blueprint_path = run_dir / "review_lead" / "REVIEW_BLUEPRINT.json"
    _write_json(
        blueprint_path,
        {
            "input_context": {
                "user_question": "dielectric resonators for optical sensing"
            },
            "review_thesis": (
                "Controlled radiation loss turns confined optical modes into "
                "practical sensing resonances."
            ),
            "full_review_argument": (
                "The review relates mechanism, design, and stability."
            ),
            "topic_identity": {
                "core_anchors": [
                    "dielectric resonator",
                    "optical sensing",
                ]
            },
            "sections": [
                {
                    "section_id": "S01",
                    "section_title": "Physical mechanism",
                }
            ],
        },
    )
    source_ledger = (
        run_dir
        / "section_coverage"
        / "sections"
        / "S01"
        / "SECTION_SOURCE_LEDGER.json"
    )
    _write_json(
        source_ledger,
        {
            "sources": [
                {
                    "paper_id": "doi:10.1234/example.1",
                    "doi": "10.1234/example.1",
                    "title": "A <i>Q</i>-factor study",
                    "authors": ["Alex Example", "Bailey Sample"],
                    "year": 2025,
                    "venue": "Journal of Optical Tests",
                    "acquisition_status": "fulltext",
                }
            ]
        },
    )
    package_path = run_dir / "REVIEW_CONTENT_PACKAGE.json"
    _write_json(
        package_path,
        {
            "schema_version": "test",
            "status": "completed",
            "final_review_path": str(review_path),
            "visual_editorial_plan_path": str(visual_path),
            "base_kb_sqlite": "",
            "artifacts": {
                "review_blueprint": str(blueprint_path),
                "authoring_work_dir": str(citation_map.parent),
            },
        },
    )
    metadata_path = run_dir / "publication_metadata.json"
    _write_json(
        metadata_path,
        {
            "title": "Dielectric Resonators for Optical Sensing",
            "authors": [
                {
                    "name": "Research Author",
                    "affiliation": "Optical Science Laboratory",
                }
            ],
            "abstract": (
                "This review connects radiation control, resonator design, "
                "and practical optical sensing constraints."
            ),
            "keywords": ["dielectric resonator", "optical sensing"],
            "date": "",
            "draft_only": draft_only,
        },
    )
    return package_path, metadata_path


def test_mojibake_and_html_are_cleaned() -> None:
    dirty = "Metasurface鈥怑nhanced <i>Q</i> sensing"
    cleaned = _clean_unicode(dirty)
    assert "鈥" not in cleaned
    assert "<i>" not in cleaned
    assert "Q" in cleaned


def test_review_markers_become_pandoc_citations() -> None:
    markdown, ids, keys = _replace_reference_markers(
        "Claim [REF:doi:10.1234/example.1]."
    )
    assert ids == ["doi:10.1234/example.1"]
    assert f"[@{keys[ids[0]]}]" in markdown
    assert "[REF:" not in markdown


def test_catalog_aliases_share_one_canonical_bibtex_key(tmp_path: Path) -> None:
    package_path = tmp_path / "REVIEW_CONTENT_PACKAGE.json"
    _write_json(
        package_path,
        {
            "artifacts": {
                "metadata_catalog": "PUBLICATION_METADATA_CATALOG.json",
            }
        },
    )
    canonical = "doi:10.1000/example.1"
    s2_hash = "0123456789abcdef0123456789abcdef01234567"
    _write_json(
        tmp_path / "PUBLICATION_METADATA_CATALOG.json",
        {
            "entries": [
                {
                    "identity": canonical,
                    "canonical_identity": canonical,
                    "markers": [canonical, s2_hash],
                    "aliases": [canonical, f"s2:{s2_hash}", s2_hash],
                }
            ]
        },
    )

    aliases = _load_reference_alias_map(
        json.loads(package_path.read_text(encoding="utf-8")),
        package_path=package_path,
    )
    markdown, ids, keys = _replace_reference_markers(
        f"First [REF:{canonical}]. Again [REF:{s2_hash}].",
        canonical_id_by_alias=aliases,
    )

    assert ids == [canonical]
    assert markdown.count(f"[@{keys[canonical]}]") == 2
    assert "[REF:" not in markdown


def test_bibtex_locator_preserves_doi_underscores_without_tex_backslashes() -> None:
    bibtex = _reference_bibtex(
        ["paper-1"],
        {"paper-1": "paper_1_key"},
        {
            "paper-1": {
                "authors": ["Example Author"],
                "title": "A DOI-bearing paper",
                "year": 2026,
                "venue": "Example Journal",
                "doi": "10.1364/cleo_at.2026.jtu.75",
                "url": "https://doi.org/10.1364/cleo_at.2026.jtu.75",
            }
        },
    )

    assert "doi = {10.1364/cleo_at.2026.jtu.75}" in bibtex
    assert "url = {https://doi.org/10.1364/cleo_at.2026.jtu.75}" in bibtex
    assert "cleo\\_at" not in bibtex


def test_catalog_entries_feed_bibliography_metadata_merge(tmp_path: Path) -> None:
    package_path = tmp_path / "REVIEW_CONTENT_PACKAGE.json"
    _write_json(
        package_path,
        {"artifacts": {"metadata_catalog": "PUBLICATION_METADATA_CATALOG.json"}},
    )
    _write_json(
        tmp_path / "PUBLICATION_METADATA_CATALOG.json",
        {
            "entries": [
                {
                    "identity": "0123456789abcdef0123456789abcdef01234567",
                    "canonical_identity": (
                        "s2:0123456789abcdef0123456789abcdef01234567"
                    ),
                    "title": "Resolved title",
                    "authors": ["Resolved Author"],
                    "year": "2024",
                    "venue": "Journal",
                }
            ]
        },
    )

    records = _collect_catalog_metadata(
        json.loads(package_path.read_text(encoding="utf-8")),
        package_path=package_path,
    )

    assert records["s2:0123456789abcdef0123456789abcdef01234567"]["title"] == (
        "Resolved title"
    )


def test_heading_levels_are_promoted() -> None:
    markdown = _prepare_scientific_markdown(
        "## Main section\n\nText\n\n### Subsection\n\nMore"
    )
    assert markdown.startswith("# Main section")
    assert "\n## Subsection" in markdown


def test_internal_section_id_is_removed_from_rendered_heading() -> None:
    markdown = _prepare_scientific_markdown(
        "## S05: Noise and drift constraints\n\nText."
    )
    assert markdown.startswith("# Noise and drift constraints")
    assert "S05:" not in markdown


def test_chinese_embedded_abstract_is_not_rendered_twice() -> None:
    stripped = _strip_embedded_title_and_abstract(
        "# 标题\n\n## 摘要\n\n摘要正文。\n\n## 引言\n\n引言正文。"
    )
    assert "摘要正文" not in stripped
    assert "## 摘要" not in stripped
    assert "## 引言" in stripped


def test_keywords_fall_back_to_topic_identity_anchor_phrases() -> None:
    metadata, warnings = _normalize_metadata(
        {"title": "Review", "abstract": "Abstract", "authors": ["Author"]},
        blueprint={
            "topic_identity": {
                "anchor_phrases": [
                    "exceptional point photonics",
                    "non-Hermitian sensing",
                ]
            }
        },
    )
    assert metadata["keywords"] == [
        "exceptional point photonics",
        "non-Hermitian sensing",
    ]
    assert "keywords_inferred_from_topic_identity" in warnings


def test_scientific_unicode_is_normalized_by_class() -> None:
    markdown = _prepare_scientific_markdown(
        "For |ψ⟩, α² + βₙ ≤ √ε and A↔B while ∇×E≈0."
    )
    assert r"$\lvert \psi\rangle$" in markdown
    assert r"$^{2}$" in markdown
    assert r"$\beta_{n}$" in markdown
    assert r"$\sqrt{\epsilon}$" in markdown
    assert r"$\leftrightarrow$" in markdown
    assert r"$\nabla$" in markdown
    assert r"$\times$" in markdown
    assert not _scientific_unicode_preflight(markdown)[
        "remaining_risky_characters"
    ]


def test_existing_math_and_code_are_not_double_converted() -> None:
    markdown = _prepare_scientific_markdown(
        r"Existing $\gamma \leq 1$ and code `α²`; prose δ."
    )
    assert r"$\gamma \leq 1$" in markdown
    assert "`α²`" in markdown
    assert r"$\delta$" in markdown
    assert "$$" not in markdown


def test_uncovered_scientific_unicode_is_audited_not_silently_lost() -> None:
    audit = _scientific_unicode_preflight("unmapped symbol ⧖")
    assert audit["unicode_native_engine_required"] is True
    assert audit["remaining_risky_characters"][0]["codepoint"] == "U+29D6"


@pytest.mark.skipif(
    not (_find_binary("pandoc") and _find_binary("latexmk")),
    reason="Pandoc and TeX Live are required for the compile acceptance test",
)
def test_real_latex_compile_and_arxiv_archive(tmp_path: Path) -> None:
    package_path, metadata_path = _build_fixture(tmp_path)
    output_dir = tmp_path / "latex"
    report = build_latex_publication(
        content_package_path=package_path,
        output_dir=output_dir,
        metadata_path=metadata_path,
        enrich_crossref=False,
        compile_pdf=True,
        render_previews=False,
    )

    assert report["status"] == "submission_ready"
    assert report["reference_count"] == 1
    assert report["latex"]["undefined_citations"] == []
    assert report["pdf_validation"]["errors"] == []
    assert report["pdf_validation"]["page_count"] >= 1
    assert "<i>" not in (output_dir / "references.bib").read_text(
        encoding="utf-8"
    )
    assert "[REF:" not in (output_dir / "main.tex").read_text(
        encoding="utf-8"
    )
    with zipfile.ZipFile(output_dir / "arxiv-source.zip") as archive:
        names = set(archive.namelist())
    assert {"main.tex", "references.bib", "main.bbl", "00README.txt"} <= names
    assert "main.pdf" not in names
    assert not any(name.endswith((".aux", ".log", ".out")) for name in names)


@pytest.mark.skipif(
    not (_find_binary("pandoc") and _find_binary("latexmk")),
    reason="Pandoc and TeX Live are required for the compile acceptance test",
)
def test_draft_metadata_is_not_called_submission_ready(tmp_path: Path) -> None:
    package_path, metadata_path = _build_fixture(
        tmp_path,
        draft_only=True,
    )
    report = build_latex_publication(
        content_package_path=package_path,
        output_dir=tmp_path / "latex",
        metadata_path=metadata_path,
        enrich_crossref=False,
        compile_pdf=True,
        render_previews=False,
    )

    assert report["status"] == "compiled_awaiting_metadata"
    assert "draft_test_metadata" in report["submission_blockers"]


@pytest.mark.skipif(
    not (_find_binary("pandoc") and _find_binary("latexmk")),
    reason="Pandoc and TeX Live are required for the compile acceptance test",
)
def test_verified_figure_is_copied_compiled_and_archived(
    tmp_path: Path,
) -> None:
    package_path, metadata_path = _build_fixture(
        tmp_path,
        with_figure=True,
    )
    output_dir = tmp_path / "latex"
    report = build_latex_publication(
        content_package_path=package_path,
        output_dir=output_dir,
        metadata_path=metadata_path,
        enrich_crossref=False,
        compile_pdf=True,
        render_previews=False,
    )

    assert report["status"] == "submission_ready"
    assert report["verified_figures_copied"] == 1
    assert report["pdf_validation"]["errors"] == []
    with zipfile.ZipFile(output_dir / "arxiv-source.zip") as archive:
        names = set(archive.namelist())
    assert any(name.startswith("figures/") for name in names)


def test_figures_resolve_relative_to_visual_package_after_relocation(
    tmp_path: Path,
) -> None:
    from PIL import Image

    pkg = tmp_path / "pkg"
    assets = pkg / "visual_assets"
    assets.mkdir(parents=True)
    image = assets / "FIG-INT-01.png"
    Image.new("RGB", (32, 32), "white").save(image)
    visual_plan = {
        "schema_version": "research_harness.final_visual_package.v1",
        "figures": [
            {
                "figure_id": "FIG-INT-01",
                "section_id": "S01",
                "local_path": "visual_assets/FIG-INT-01.png",
                "panel_manifest": [
                    {
                        "panel_id": "a",
                        "source_local_path": "visual_assets/FIG-INT-01.png",
                    }
                ],
                "caption_en": "A package-relative figure.",
                "review_decision": "system_approved_test_mode_with_warnings",
                "render_status": "ready",
                "publication_eligible": False,
            }
        ],
    }
    _write_json(pkg / "FINAL_VISUAL_PACKAGE.json", visual_plan)

    figures, rejected = _copy_verified_figures(
        visual_plan,
        output_dir=tmp_path / "out1",
        base_dir=pkg,
    )
    assert rejected == []
    assert len(figures) == 1
    copied = tmp_path / "out1" / "figures" / "01_FIG-INT-01.png"
    assert copied.is_file()
    assert copied.read_bytes() == image.read_bytes()

    # Copy the whole package to a different parent; the same relative figure
    # reference must keep resolving against the moved package directory.
    relocated = tmp_path / "relocated" / "package"
    shutil.copytree(pkg, relocated)
    relocated_plan = json.loads(
        (relocated / "FINAL_VISUAL_PACKAGE.json").read_text(encoding="utf-8")
    )
    figures2, rejected2 = _copy_verified_figures(
        relocated_plan,
        output_dir=tmp_path / "out2",
        base_dir=relocated,
    )
    assert rejected2 == []
    assert len(figures2) == 1
    copied2 = tmp_path / "out2" / "figures" / "01_FIG-INT-01.png"
    assert copied2.is_file()
    assert copied2.read_bytes() == (relocated / "visual_assets/FIG-INT-01.png").read_bytes()
    assert copied2.read_bytes() == image.read_bytes()


def test_resolve_artifact_path_prefers_package_dir_for_relative_refs(
    tmp_path: Path,
) -> None:
    pkg = tmp_path / "package"
    pkg.mkdir()
    package_json = pkg / "REVIEW_CONTENT_PACKAGE.json"
    visual_ref = pkg / "FINAL_VISUAL_PACKAGE.json"
    visual_ref.write_text("{}", encoding="utf-8")

    resolved = _resolve_artifact_path(
        "FINAL_VISUAL_PACKAGE.json",
        package_path=package_json,
    )
    assert resolved == visual_ref.resolve()

    missing = _resolve_artifact_path("future.json", package_path=package_json)
    assert missing == pkg / "future.json"

    # Absolute paths remain untouched for backward compatibility.
    absolute = tmp_path / "elsewhere" / "FINAL_VISUAL_PACKAGE.json"
    absolute.parent.mkdir()
    absolute.write_text("{}", encoding="utf-8")
    resolved_absolute = _resolve_artifact_path(
        str(absolute),
        package_path=package_json,
    )
    assert resolved_absolute == absolute.resolve()


def test_portable_ref_relativizes_internal_paths_and_keeps_external_absolute(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "latex"
    (output_dir / "figures").mkdir(parents=True)
    pdf = output_dir / "main.pdf"
    pdf.write_bytes(b"%PDF-test")
    preview = output_dir / "preview" / "page_001.png"
    preview.parent.mkdir()
    preview.write_bytes(b"preview")
    external = tmp_path / "upstream_cache" / "figure.png"
    external.parent.mkdir()
    external.write_bytes(b"png")

    assert _portable_ref(pdf, output_dir=output_dir) == "main.pdf"
    assert (
        _portable_ref(output_dir / "figures" / "01_fig.png", output_dir=output_dir)
        == "figures/01_fig.png"
    )
    assert (
        _portable_ref(preview, output_dir=output_dir)
        == "preview/page_001.png"
    )
    # Targets outside the output package stay absolute so legacy inputs remain
    # inspectable, and already-relative refs pass through unchanged.
    assert (
        _portable_ref(external, output_dir=output_dir)
        == str(external.resolve())
    )
    assert _portable_ref("", output_dir=output_dir) == ""
    assert (
        _portable_ref("figures/01_fig.png", output_dir=output_dir)
        == "figures/01_fig.png"
    )

    # Self-contained package model: package_root owns the content JSON and the
    # visual inputs, while the report lives under publication/latex.
    package_root = tmp_path / "package"
    report_dir = package_root / "publication" / "latex"
    report_dir.mkdir(parents=True)
    content_package = package_root / "REVIEW_CONTENT_PACKAGE.json"
    content_package.write_text("{}", encoding="utf-8")
    visual_asset = package_root / "visual_assets" / "fig.png"
    visual_asset.parent.mkdir()
    visual_asset.write_bytes(b"png")
    assert (
        _portable_ref(
            content_package,
            output_dir=report_dir,
            package_root=package_root,
        )
        == "../../REVIEW_CONTENT_PACKAGE.json"
    )
    assert (
        _portable_ref(
            visual_asset,
            output_dir=report_dir,
            package_root=package_root,
        )
        == "../../visual_assets/fig.png"
    )
    # Truly external targets stay absolute even with a package root.
    assert (
        _portable_ref(
            external,
            output_dir=report_dir,
            package_root=package_root,
        )
        == str(external.resolve())
    )


def test_portable_report_refs_resolve_from_report_directory(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    output_dir = package_root / "publication" / "latex"
    output_dir.mkdir(parents=True)
    pdf = output_dir / "main.pdf"
    pdf.write_bytes(b"%PDF-test")
    archive = output_dir / "arxiv-source.zip"
    archive.write_bytes(b"PK-test")
    content_package = package_root / "REVIEW_CONTENT_PACKAGE.json"
    content_package.write_text("{}", encoding="utf-8")
    visual_asset = package_root / "visual_assets" / "fig.png"
    visual_asset.parent.mkdir()
    visual_asset.write_bytes(b"png")
    report = {
        "schema_version": "research_harness.latex_build_report.v3",
        "content_package_path": str(content_package),
        "preview_paths": [str(output_dir / "preview" / "page_001.png")],
        "rejected_visual_placements": [
            {
                "visual_chunk_id": "unit:visual:missing",
                "local_image_path": str(visual_asset),
                "reason": "source_image_missing",
            }
        ],
        "pandoc": {
            "path": str(
                Path("X:/legacy-fixture/Pandoc/pandoc.exe")
            ),
            "returncode": 0,
        },
        "artifacts": {
            "main_tex": str(output_dir / "main.tex"),
            "compiled_pdf": str(pdf),
            "arxiv_source_zip": str(archive),
            "figure_asset_audit": str(output_dir / "FIGURE_ASSET_AUDIT.json"),
        },
    }

    portable = _portable_report_refs(
        report,
        output_dir=output_dir,
        package_root=package_root,
    )

    assert portable["content_package_path"] == "../../REVIEW_CONTENT_PACKAGE.json"
    assert portable["preview_paths"] == ["preview/page_001.png"]
    assert portable["rejected_visual_placements"] == [
        {
            "visual_chunk_id": "unit:visual:missing",
            "local_image_path": "../../visual_assets/fig.png",
            "reason": "source_image_missing",
        }
    ]
    assert portable["artifacts"]["main_tex"] == "main.tex"
    assert portable["artifacts"]["compiled_pdf"] == "main.pdf"
    assert portable["artifacts"]["arxiv_source_zip"] == "arxiv-source.zip"
    assert (
        portable["artifacts"]["figure_asset_audit"]
        == "FIGURE_ASSET_AUDIT.json"
    )
    # Serialized tool metadata is portable: basename only, no machine location.
    assert portable["pandoc"]["path"] == "pandoc.exe"
    assert portable["pandoc"]["returncode"] == 0
    assert not re.match(r"^[A-Za-z]:[\\/]", portable["pandoc"]["path"])
    assert "Users" not in portable["pandoc"]["path"]
    # The original in-memory report is untouched, preserving runtime reads.
    assert report["artifacts"]["compiled_pdf"] == str(pdf)
    assert report["pandoc"]["path"] == str(
        Path("X:/legacy-fixture/Pandoc/pandoc.exe")
    )

    # Serialized internal refs must resolve from the report directory back to
    # their packaged targets and carry no drive-letter or leading-slash prefix.
    internal_refs = [
        portable["content_package_path"],
        *portable["preview_paths"],
        portable["rejected_visual_placements"][0]["local_image_path"],
        *portable["artifacts"].values(),
    ]
    for ref in internal_refs:
        assert ref == ref.replace("\\", "/")
        assert not re.match(r"^[A-Za-z]:[\\/]", ref)
        assert not ref.startswith("/")
        assert (output_dir / ref).resolve().is_absolute()
    assert (
        output_dir / portable["content_package_path"]
    ).resolve() == content_package.resolve()
    assert (
        output_dir
        / portable["rejected_visual_placements"][0]["local_image_path"]
    ).resolve() == visual_asset.resolve()


def test_portable_figure_audit_relativizes_packaged_paths_and_preserves_external(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    output_dir = package_root / "publication" / "latex"
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True)
    destination = figures_dir / "01_fig.png"
    destination.write_bytes(b"png")
    packaged_source = package_root / "visual_assets" / "source.png"
    packaged_source.parent.mkdir()
    packaged_source.write_bytes(b"png")
    external = tmp_path / "visual_cache" / "legacy.png"
    external.parent.mkdir()
    external.write_bytes(b"png")

    portable_audit = _portable_audit_entry(
        {
            "source_path": str(packaged_source),
            "publication_path": str(destination),
            "caption_crop_status": "preserved_no_caption_signal",
        },
        output_dir=output_dir,
        package_root=package_root,
    )

    assert portable_audit["publication_path"] == "figures/01_fig.png"
    assert portable_audit["source_path"] == "../../visual_assets/source.png"
    assert portable_audit["caption_crop_status"] == "preserved_no_caption_signal"

    external_audit = _portable_audit_entry(
        {
            "source_path": str(external),
            "publication_path": str(destination),
        },
        output_dir=output_dir,
        package_root=package_root,
    )
    assert external_audit["source_path"] == str(external.resolve())
    assert external_audit["publication_path"] == "figures/01_fig.png"

    rejected = _portable_rejected_entry(
        {
            "visual_chunk_id": "unit:visual:missing",
            "local_image_path": str(packaged_source),
            "reason": "source_image_missing",
        },
        output_dir=output_dir,
        package_root=package_root,
    )
    assert rejected["local_image_path"] == "../../visual_assets/source.png"

    external_rejected = _portable_rejected_entry(
        {
            "visual_chunk_id": "legacy-1",
            "local_image_path": str(external),
        },
        output_dir=output_dir,
        package_root=package_root,
    )
    assert external_rejected["local_image_path"] == str(external.resolve())


def test_harness_records_latex_as_terminal_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from optomind_research.runtime import review_harness_orchestrator as module

    query_plan = tmp_path / "query.json"
    kb_path = tmp_path / "kb.sqlite"
    _write_json(query_plan, {"output": {}})
    kb_path.write_bytes(b"")
    run_dir = tmp_path / "run"
    review_path = run_dir / "authoring" / "full_review" / "FINAL_REVIEW_EN.md"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("# Section\n\nSubstantial review text.", encoding="utf-8")
    visual_path = run_dir / "visual_editor" / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(visual_path, {"placements": []})

    monkeypatch.setattr(
        module,
        "evaluate_review_content",
        lambda **_: {"status": "passed", "metrics": {}},
    )

    def fake_latex(**kwargs: object) -> dict:
        output_dir = Path(str(kwargs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf = output_dir / "main.pdf"
        archive = output_dir / "arxiv-source.zip"
        report_path = output_dir / "LATEX_BUILD_REPORT.json"
        pdf.write_bytes(b"%PDF-test")
        archive.write_bytes(b"PK-test")
        _write_json(report_path, {"status": "compiled_awaiting_metadata"})
        return {
            "status": "compiled_awaiting_metadata",
            "artifacts": {
                "compiled_pdf": str(pdf),
                "arxiv_source_zip": str(archive),
            },
        }

    monkeypatch.setattr(module, "build_latex_publication", fake_latex)
    orchestrator = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan,
            base_kb_sqlite=kb_path,
            output_root=tmp_path,
            produce_latex_publication=True,
        ),
        run_dir=run_dir,
    )
    result = orchestrator._finish(
        "completed",
        "packaging",
        review_path,
        visual_path,
        None,
    )
    package = json.loads(result.package_path.read_text(encoding="utf-8"))

    assert result.completed_stage == "latex_publication"
    assert result.latex_pdf_path is not None
    assert package["latex_publication_status"] == "compiled_awaiting_metadata"
    assert package["completed_stage"] == "latex_publication"


@pytest.mark.skipif(
    not (_find_binary("pandoc") and _find_binary("latexmk")),
    reason="Pandoc and TeX Live are required for the compile acceptance test",
)
def test_real_chinese_latex_compile(tmp_path: Path) -> None:
    package_path, metadata_path = _build_fixture(tmp_path)
    chinese_markdown = tmp_path / "FINAL_REVIEW_ZH.md"
    chinese_paragraph = (
        "介电谐振器通过受控辐射通道实现远场读出，同时保持较强的"
        "电磁场局域。BIC、quasi-BIC、Q-factor 和 FOM 等术语保留"
        "英文形式，以避免不必要的术语歧义。该机制把共振线宽、"
        "耦合效率与实际传感稳定性联系起来，并为器件比较提供统一"
        "的物理基础。"
    )
    chinese_markdown.write_text(
        (
            "## 物理机制\n\n"
            + chinese_paragraph * 6
            + " "
            "[REF:doi:10.1234/example.1]。\n"
        ),
        encoding="utf-8",
    )
    chinese_metadata = tmp_path / "metadata_zh.json"
    _write_json(
        chinese_metadata,
        {
            "title": "用于光学传感的介电谐振器",
            "authors": [
                {
                    "name": "Research Author",
                    "affiliation": "Optical Science Laboratory",
                }
            ],
            "abstract": "本文综述介电谐振器的辐射调控与传感机制。",
            "keywords": ["介电谐振器", "BIC", "光学传感"],
            "draft_only": True,
        },
    )
    report = build_latex_publication(
        content_package_path=package_path,
        output_dir=tmp_path / "latex_zh",
        metadata_path=chinese_metadata,
        source_markdown_path=chinese_markdown,
        language="zh-CN",
        enrich_crossref=False,
        compile_pdf=True,
        render_previews=False,
    )

    assert report["status"] == "compiled_awaiting_metadata"
    assert report["language"] == "zh-CN"
    assert report["latex"]["undefined_citations"] == []
    assert report["pdf_validation"]["errors"] == []
    assert Path(report["artifacts"]["compiled_pdf"]).is_file()


def test_harness_records_bilingual_terminal_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from optomind_research.runtime import review_harness_orchestrator as module

    query_plan = tmp_path / "query.json"
    kb_path = tmp_path / "kb.sqlite"
    _write_json(query_plan, {"output": {}})
    kb_path.write_bytes(b"")
    run_dir = tmp_path / "run"
    review_path = run_dir / "authoring" / "full_review" / "FINAL_REVIEW_EN.md"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("# Section\n\nSubstantial review text.", encoding="utf-8")
    visual_path = run_dir / "visual_editor" / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(visual_path, {"placements": []})

    monkeypatch.setattr(
        module,
        "evaluate_review_content",
        lambda **_: {"status": "passed", "metrics": {}},
    )

    def fake_translation(**kwargs: object) -> dict:
        output_dir = Path(str(kwargs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        translated = output_dir / "FINAL_REVIEW_ZH.md"
        metadata = output_dir / "PUBLICATION_METADATA_ZH.json"
        translated.write_text("# 章节\n\n完整中文综述内容。", encoding="utf-8")
        _write_json(metadata, {"title": "中文综述"})
        _write_json(output_dir / "TRANSLATION_REPORT.json", {"status": "completed"})
        return {
            "status": "completed",
            "translated_path": str(translated),
            "translated_metadata_path": str(metadata),
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 80,
            "cumulative_estimated_cost_cny": 0.02,
            "failed_unit_ids": [],
        }

    def fake_latex(**kwargs: object) -> dict:
        output_dir = Path(str(kwargs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf = output_dir / "main.pdf"
        archive = output_dir / "arxiv-source.zip"
        pdf.write_bytes(b"%PDF-test")
        archive.write_bytes(b"PK-test")
        _write_json(
            output_dir / "LATEX_BUILD_REPORT.json",
            {"status": "compiled_awaiting_metadata"},
        )
        return {
            "status": "compiled_awaiting_metadata",
            "artifacts": {
                "compiled_pdf": str(pdf),
                "arxiv_source_zip": str(archive),
            },
        }

    monkeypatch.setattr(module, "translate_review_package", fake_translation)
    monkeypatch.setattr(module, "build_latex_publication", fake_latex)
    orchestrator = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan,
            base_kb_sqlite=kb_path,
            output_root=tmp_path,
            produce_latex_publication=True,
            produce_chinese_publication=True,
        ),
        run_dir=run_dir,
    )
    result = orchestrator._finish(
        "completed",
        "packaging",
        review_path,
        visual_path,
        None,
    )
    package = json.loads(result.package_path.read_text(encoding="utf-8"))

    assert result.completed_stage == "latex_publication_zh"
    assert result.chinese_review_path is not None
    assert result.chinese_latex_pdf_path is not None
    assert package["chinese_translation_status"] == "completed"
    assert package["chinese_latex_publication_status"] == (
        "compiled_awaiting_metadata"
    )
    assert package["total_input_tokens"] == 100
    assert package["total_output_tokens"] == 80
    assert package["total_cost_cny"] == pytest.approx(0.02)


def test_bilingual_default_preflight_includes_translation_budget(
    tmp_path: Path,
) -> None:
    orchestrator = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=tmp_path / "query.json",
            base_kb_sqlite=tmp_path / "kb.sqlite",
            output_root=tmp_path,
            produce_latex_publication=True,
            produce_chinese_publication=True,
            upstream_cost_cny=1.0,
        ),
        run_dir=tmp_path / "run",
    )

    report = orchestrator.preflight()

    # Legacy compatibility caps were raised: translation 1→3,
    # article_completion 2→18, authoring 17.5→28, section_coverage 10→14,
    # visual 2.5→5, global 49→120.
    # With produce_chinese_publication=True and upstream_cost_cny=1.0 the
    # allocation sum is 84.0, well within the 120 CNY global ceiling.
    assert report["within_budget"] is True
    assert report["allocated_max_cny"] == pytest.approx(84.0)
    assert report["stage_hard_caps_cny"]["chinese_translation"] == pytest.approx(3.0)


def test_figure_transitions_replace_visual_guide_prose() -> None:
    blueprint = {
        "sections": [
            {"section_id": "S01", "section_title": "Physical mechanism"},
            {"section_id": "S02", "section_title": "Numerical comparison"},
        ]
    }
    markdown = (
        "## Physical mechanism\n\n"
        "First paragraph with a sustained claim.\n\n"
        "Second paragraph continues the section.\n\n"
        "## Numerical comparison\n\n"
        "Table paragraph with numerical evidence.\n\n"
    )
    figures = [
        {
            "figure_number": 1,
            "figure_label": "fig:visual_01",
            "latex_path": "figures/01_mech.png",
            "section_id": "S01",
            "caption_en": "Mechanism caption.",
            "argumentative_purpose": (
                "Show the relation between confinement and radiation loss."
            ),
            "figure_contract": {"is_table": False},
        },
        {
            "figure_number": 2,
            "figure_label": "fig:visual_02",
            "latex_path": "figures/02_table.png",
            "section_id": "S02",
            "caption_en": "Comparison table caption.",
            "argumentative_purpose": (
                "This table quantitatively compares the accuracy of a "
                "physics-informed neural network model against numerical "
                "simulations."
            ),
            "figure_contract": {"is_table": True, "asset_kind": "table"},
        },
    ]

    rendered = _inject_figures(markdown, figures=figures, blueprint=blueprint)

    assert "Visual guide" not in rendered
    assert "visual guide" not in rendered
    assert "the figure below supports" not in rendered
    assert (
        "Figure \\ref{fig:visual_01} illustrates the relation between "
        "confinement and "
        "radiation loss." in rendered
    )
    assert (
        "Figure \\ref{fig:visual_02} illustrates the accuracy of a "
        "physics-informed neural "
        "network model" in rendered
    )
    assert "Table \\ref" not in rendered
    transition_lines = [
        line
        for line in rendered.splitlines()
        if line.startswith("Figure \\ref")
    ]
    for internal_phrase in (
        "placement",
        "argumentative purpose",
        "source-derived",
        "internal study visual",
        "figure contract",
        "explanatory thread",
    ):
        assert all(
            internal_phrase not in line for line in transition_lines
        )


def test_figure_transition_falls_back_to_neutral_academic_sentence() -> None:
    blueprint = {
        "sections": [
            {"section_id": "S01", "section_title": "Physical mechanism"}
        ]
    }
    markdown = (
        "## Physical mechanism\n\n"
        "First paragraph with a sustained claim.\n\n"
        "Second paragraph continues the section.\n\n"
    )
    figures = [
        {
            "figure_number": 4,
            "figure_label": "fig:visual_04",
            "latex_path": "figures/04_plain.png",
            "section_id": "S01",
            "caption_en": "Plain visual caption.",
            "argumentative_purpose": "",
            "figure_contract": {"is_table": False},
        }
    ]

    rendered = _inject_figures(markdown, figures=figures, blueprint=blueprint)

    assert "Visual guide" not in rendered
    assert "the figure below supports" not in rendered
    assert (
        "Figure \\ref{fig:visual_04} presents representative evidence "
        "discussed in this section."
        in rendered
    )


def test_chinese_figure_transition_does_not_leak_english_callout() -> None:
    blueprint = {
        "sections": [
            {"section_id": "S01", "section_title": "物理机制"}
        ]
    }
    markdown = "## 物理机制\n\n第一段。\n\n第二段。\n"
    figures = [
        {
            "figure_number": 1,
            "figure_label": "fig:visual_01",
            "latex_path": "figures/01.png",
            "section_id": "S01",
            "caption_en": "Mechanism caption.",
            "argumentative_purpose": "Show the physical mechanism.",
        }
    ]

    rendered = _inject_figures(
        markdown,
        figures=figures,
        blueprint=blueprint,
        language="zh-CN",
    )

    assert "图 \\ref{fig:visual_01}展示了本节讨论的代表性视觉证据。" in rendered
    assert "Figure \\ref" not in rendered


def test_source_figure_does_not_use_mismatched_purpose() -> None:
    figure = {
        "figure_id": "FIG-SRC-003",
        "figure_label": "fig:visual_03",
        "purpose": "The numerical reflectance spectra illustrate the spectral signatures.",
    }

    from optomind_research.runtime.latex_publication_renderer import (
        _figure_transition_sentence,
    )

    sentence = _figure_transition_sentence(figure)

    assert sentence == (
        "Figure \\ref{fig:visual_03} provides source-derived visual context "
        "for this section."
    )


def test_long_figure_transition_does_not_end_with_dangling_preposition() -> None:
    from optomind_research.runtime.latex_publication_renderer import (
        _figure_transition_sentence,
    )

    figure = {
        "figure_id": "FIG-GEN-LONG",
        "figure_label": "fig:visual_long",
        "argumentative_purpose": (
            "Show the physical geometry and trainable-operator interpretation "
            + ("of the cascaded diffractive topology " * 30)
        ),
    }

    sentence = _figure_transition_sentence(figure)

    assert len(sentence) <= 260
    assert sentence.endswith(".")
    assert "..." not in sentence


def test_figure_transition_uses_manuscript_order_label_not_package_number() -> None:
    blueprint = {
        "sections": [
            {"section_id": "S01", "section_title": "Physical mechanism"},
            {"section_id": "S02", "section_title": "Numerical comparison"},
        ]
    }
    markdown = (
        "## Physical mechanism\n\n"
        "First mechanism paragraph.\n\n"
        "## Numerical comparison\n\n"
        "First comparison paragraph.\n\n"
    )
    figures = [
        {
            "figure_number": 6,
            "figure_label": "fig:visual_06",
            "latex_path": "figures/06_second.png",
            "section_id": "S02",
            "caption_en": "Second rendered visual caption.",
            "argumentative_purpose": (
                "Compare the numerical accuracy of the two solver families."
            ),
            "figure_contract": {"is_table": False},
        },
        {
            "figure_number": 3,
            "figure_label": "fig:visual_03",
            "latex_path": "figures/03_first.png",
            "section_id": "S01",
            "caption_en": "First rendered visual caption.",
            "argumentative_purpose": (
                "Show the physical mechanism behind the observed confinement."
            ),
            "figure_contract": {"is_table": False},
        },
    ]

    rendered = _inject_figures(markdown, figures=figures, blueprint=blueprint)

    assert "Figure 3" not in rendered
    assert "Figure 6" not in rendered
    assert "Figure \\ref{fig:visual_03}" in rendered
    assert "Figure \\ref{fig:visual_06}" in rendered
    assert rendered.index("Figure \\ref{fig:visual_03}") < rendered.index(
        "Figure \\ref{fig:visual_06}"
    )


def test_figure_transition_neutralizes_imperative_help_readers_purpose() -> None:
    blueprint = {
        "sections": [
            {"section_id": "S01", "section_title": "Physical mechanism"}
        ]
    }
    markdown = (
        "## Physical mechanism\n\n"
        "First paragraph with a sustained claim.\n\n"
        "Second paragraph continues the section.\n\n"
    )
    figures = [
        {
            "figure_number": 8,
            "figure_label": "fig:visual_08",
            "latex_path": "figures/08_choice.png",
            "section_id": "S01",
            "caption_en": "A clean reader-facing caption.",
            "argumentative_purpose": (
                "Help readers choose between PINNs and differentiable solvers."
            ),
            "figure_contract": {"is_table": False},
        }
    ]

    rendered = _inject_figures(markdown, figures=figures, blueprint=blueprint)

    assert "Help readers choose" not in rendered
    assert "illustrates Help readers" not in rendered
    assert (
        "Figure \\ref{fig:visual_08} presents representative evidence "
        "discussed in this section."
        in rendered
    )


def test_figure_transition_for_unknown_section_is_omitted_not_appended() -> None:
    blueprint = {"sections": []}
    markdown = "## Physical mechanism\n\nParagraph one.\n\n"
    figures = [
        {
            "figure_number": 5,
            "figure_label": "fig:visual_05",
            "latex_path": "figures/05_orphan.png",
            "section_id": "S99",
            "caption_en": "Orphan visual caption.",
            "argumentative_purpose": (
                "Visual guide: the figure below supports the explanatory "
                "thread at this point in the review."
            ),
            "figure_contract": {"is_table": False},
        }
    ]
    omitted: list[dict[str, Any]] = []

    rendered = _inject_figures(
        markdown,
        figures=figures,
        blueprint=blueprint,
        omitted_figures=omitted,
    )

    assert "Visual guide" not in rendered
    assert "the figure below supports" not in rendered
    assert "explanatory thread" not in rendered
    assert "![Orphan visual caption.](figures/05_orphan.png)" not in rendered
    assert rendered == markdown
    assert omitted == [
        {
            "visual_chunk_id": "",
            "figure_id": "",
            "section_id": "S99",
            "local_image_path": "figures/05_orphan.png",
            "reason": "unknown_section_id_no_blueprint_heading",
        }
    ]


def test_figure_injection_maps_translated_headings_by_section_order() -> None:
    blueprint = {
        "sections": [
            {"section_id": "S01", "section_title": "Physical mechanism"},
            {"section_id": "S02", "section_title": "Experimental limits"},
        ]
    }
    markdown = (
        "# 引言\n\n背景段落。\n\n"
        "# 物理机制\n\n第一节正文。\n\n后续解释。\n\n"
        "# 实验限制\n\n第二节正文。\n\n后续解释。\n\n"
        "# 挑战与未来展望\n\n展望。\n\n"
        "# 结论\n\n结论。\n"
    )
    figures = [
        {
            "figure_id": "FIG-ZH-01",
            "figure_number": 1,
            "figure_label": "fig:zh_01",
            "latex_path": "figures/01.png",
            "section_id": "S01",
            "caption_en": "Mechanism caption.",
            "argumentative_purpose": "Show the physical mechanism.",
            "figure_contract": {"is_table": False},
        },
        {
            "figure_id": "FIG-ZH-02",
            "figure_number": 2,
            "figure_label": "fig:zh_02",
            "latex_path": "figures/02.png",
            "section_id": "S02",
            "caption_en": "Limits caption.",
            "argumentative_purpose": "Compare experimental limits.",
            "figure_contract": {"is_table": False},
        },
    ]
    omitted: list[dict[str, Any]] = []

    rendered = _inject_figures(
        markdown,
        figures=figures,
        blueprint=blueprint,
        omitted_figures=omitted,
    )

    assert omitted == []
    assert rendered.index("figures/01.png") > rendered.index("# 物理机制")
    assert rendered.index("figures/01.png") < rendered.index("# 实验限制")
    assert rendered.index("figures/02.png") > rendered.index("# 实验限制")
    assert rendered.index("figures/02.png") < rendered.index("# 挑战与未来展望")


def test_figure_transition_neutralizes_commander_prefixed_purpose() -> None:
    blueprint = {
        "sections": [
            {"section_id": "S01", "section_title": "Physical mechanism"}
        ]
    }
    markdown = (
        "## Physical mechanism\n\n"
        "First paragraph with a sustained claim.\n\n"
        "Second paragraph continues the section.\n\n"
    )
    figures = [
        {
            "figure_number": 7,
            "figure_label": "fig:visual_07",
            "latex_path": "figures/07_visual.png",
            "section_id": "S01",
            "caption_en": "A clean reader-facing caption.",
            "argumentative_purpose": (
                "commander:reposition | Must shed 'Inverse Design' and "
                "'Gradient Pathology' content to avoid overlap with S03/S04. "
                "Focus on 'What are PINNs and Solvers?'"
            ),
            "figure_contract": {"is_table": False},
        }
    ]

    rendered = _inject_figures(markdown, figures=figures, blueprint=blueprint)

    assert "commander" not in rendered.lower()
    assert "reposition" not in rendered.lower()
    assert "Must shed" not in rendered
    assert "Gradient Pathology" not in rendered
    assert (
        "Figure \\ref{fig:visual_07} presents representative evidence "
        "discussed in this section."
        in rendered
    )


@pytest.mark.parametrize("use_artifacts_fallback", [False, True])
def test_resolve_publication_metadata_auto_loads_registered_package_path(
    tmp_path: Path,
    use_artifacts_fallback: bool,
) -> None:
    blueprint_path = tmp_path / "REVIEW_BLUEPRINT.json"
    metadata_path = tmp_path / "publication_metadata.json"
    package_path = tmp_path / "REVIEW_CONTENT_PACKAGE.json"
    _write_json(
        blueprint_path,
        {
            "review_thesis": "A blueprint thesis.",
            "topic_identity": {"core_anchors": ["anchor phrase"]},
            "sections": [],
        },
    )
    _write_json(
        metadata_path,
        {
            "title": "Auto Metadata Title",
            "abstract": "Auto metadata abstract.",
            "authors": [
                {"name": "Auto Author", "affiliation": "Auto Laboratory"}
            ],
            "keywords": ["auto keyword"],
            "draft_only": True,
        },
    )
    package = {
        "status": "internal_study_draft",
        "artifacts": {"review_blueprint": str(blueprint_path)},
    }
    if use_artifacts_fallback:
        package["artifacts"]["publication_metadata"] = str(metadata_path)
    else:
        package["publication_metadata_path"] = str(metadata_path)
    _write_json(package_path, package)

    metadata, warnings = resolve_publication_metadata(
        content_package_path=package_path
    )

    assert metadata["title"] == "Auto Metadata Title"
    assert metadata["abstract"] == "Auto metadata abstract."
    assert metadata["authors"][0]["name"] == "Auto Author"
    assert "author_metadata_pending" not in warnings


def test_resolve_publication_metadata_explicit_path_takes_precedence(
    tmp_path: Path,
) -> None:
    blueprint_path = tmp_path / "REVIEW_BLUEPRINT.json"
    package_metadata_path = tmp_path / "publication_metadata.json"
    explicit_metadata_path = tmp_path / "explicit_metadata.json"
    package_path = tmp_path / "REVIEW_CONTENT_PACKAGE.json"
    _write_json(
        blueprint_path,
        {"review_thesis": "A blueprint thesis.", "sections": []},
    )
    _write_json(
        package_metadata_path,
        {
            "title": "Package Registered Title",
            "abstract": "Package registered abstract.",
            "authors": [{"name": "Package Author"}],
        },
    )
    _write_json(
        explicit_metadata_path,
        {
            "title": "Explicit Caller Title",
            "abstract": "Explicit caller abstract.",
            "authors": [{"name": "Explicit Author"}],
        },
    )
    _write_json(
        package_path,
        {
            "artifacts": {"review_blueprint": str(blueprint_path)},
            "publication_metadata_path": str(package_metadata_path),
        },
    )

    metadata, _ = resolve_publication_metadata(
        content_package_path=package_path,
        metadata_path=explicit_metadata_path,
    )

    assert metadata["title"] == "Explicit Caller Title"
    assert metadata["abstract"] == "Explicit caller abstract."
    assert metadata["authors"][0]["name"] == "Explicit Author"


def test_resolve_publication_metadata_missing_path_falls_back_to_blueprint(
    tmp_path: Path,
) -> None:
    blueprint_path = tmp_path / "REVIEW_BLUEPRINT.json"
    package_path = tmp_path / "REVIEW_CONTENT_PACKAGE.json"
    _write_json(
        blueprint_path,
        {
            "review_thesis": (
                "Controlled radiation loss turns confined optical modes into "
                "practical sensing resonances."
            ),
            "topic_identity": {"core_anchors": ["optical sensing"]},
            "sections": [],
        },
    )
    _write_json(
        package_path,
        {
            "artifacts": {"review_blueprint": str(blueprint_path)},
            "publication_metadata_path": str(
                tmp_path / "missing_publication_metadata.json"
            ),
        },
    )

    metadata, warnings = resolve_publication_metadata(
        content_package_path=package_path
    )

    assert metadata["title"]
    assert metadata["abstract"]
    assert "abstract_inferred_from_review_blueprint" in warnings
    assert "author_metadata_pending" in warnings
