from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from optomind_research.runtime.latex_publication_renderer import (
    _contains_likely_mojibake,
    _collect_kb_metadata,
    _merge_reference_metadata,
    _reference_bibtex,
)


def test_mojibake_detector_does_not_reject_legitimate_chinese() -> None:
    assert _contains_likely_mojibake("这些类别之间的界限并非总是泾渭分明。") is False
    assert _contains_likely_mojibake("broken \ufffd text") is True
from optomind_research.runtime.publication_figure_processor import (
    prepare_publication_figure,
)
from optomind_research.runtime.publication_integrity import (
    prepare_publication_markdown,
)


def test_formula_protocol_preserves_whole_scientific_expressions() -> None:
    source = (
        "E\u00b1 = \u00b1\u221a\u03b4; "
        "n = 1 + \u03a3_\u03b1(n_\u03b1 \u2212 1); "
        "\u0394R \u223c \u03b5^(2/3)."
    )
    output, audit = prepare_publication_markdown(source)
    assert r"$E_{\pm} = \pm\sqrt{\delta}$" in output
    assert r"$n = 1 + \sum_\alpha(n_\alpha - 1)$" in output
    assert r"$\Delta R \sim \epsilon^{(2/3)}$" in output
    assert len(audit["equation_store"]) == 3
    assert "$\\epsilon$ $^{" not in output


def test_existing_math_repairs_json_damaged_tex_and_unicode_symbols() -> None:
    source = (
        "The material uses $"
        + "\t"
        + "au = n + jκ$ and the measured index is $n ≈ 1.72$."
    )
    output, _audit = prepare_publication_markdown(source)

    assert r"$\tau = n + j\kappa$" in output
    assert r"$n \approx 1.72$" in output
    assert "\t" not in output


def test_formula_protocol_normalizes_common_ascii_llm_math() -> None:
    source = (
        "delta^(1/N); 10^6; K = ||r||^2||l||^2; "
        "H_EP2 = [[0, 1], [0, 0]]; Delta R; n_ alpha; "
        "chi = partial measurand/partial epsilon."
    )
    output, audit = prepare_publication_markdown(source)
    assert r"$\delta^{(1/N)}$" in output
    assert r"$10^{6}$" in output
    assert r"$K = \lVert r\rVert^{2}\lVert l\rVert^{2}$" in output
    assert r"$H_{\mathrm{EP2}} = [[0, 1], [0, 0]]$" in output
    assert r"$\Delta R$" in output
    assert r"$n_{\alpha}$" in output
    assert r"$\chi = \frac{\partial \mathrm{measurand}}{\partial \epsilon}$" in output
    assert audit["unresolved_formula_hazards"] == []


def test_formula_protocol_normalizes_inline_big_o_complexity_notation() -> None:
    output, audit = prepare_publication_markdown(
        "The architecture requires O(N^2) comb lines at scale."
    )

    assert r"$O(N^{2})$" in output
    assert audit["unresolved_formula_hazards"] == []


def test_formula_protocol_handles_real_nonhermitian_photonics_notation() -> None:
    source = (
        "The eigenenergies are E = ±√(κ² − σ²), where κ is coupling. "
        "The exponent α = 1/N applies when the perturbation is favorable. "
        "The response follows ε^(1/N). Resonances ω₁,₂ have losses γ_c1."
    )
    output, audit = prepare_publication_markdown(source)

    assert r"$E = \pm\sqrt{\kappa^{2} - \sigma^{2}}$" in output
    assert r"$\alpha = 1/N$ applies when" in output
    assert r"$\epsilon^{(1/N)}$" in output
    assert r"$\omega_{1,2}$" in output
    assert r"$\gamma_{c1}$" in output
    assert "$$" not in output
    assert "$\\epsilon$ $^{" not in output
    assert audit["unresolved_formula_hazards"] == []


def test_formula_protocol_never_rewrites_reference_identifiers() -> None:
    source = (
        "The result was independently confirmed "
        "[REF:doi:10.1038/s41467-026-69889-w]."
    )
    output, _ = prepare_publication_markdown(source)

    assert "[REF:doi:10.1038/s41467-026-69889-w]" in output
    assert "$w$" not in output


def test_formula_protocol_handles_formula_before_chinese_punctuation() -> None:
    source = (
        "本征能量为 E = ±√(κ² − σ²)，其中 κ 为耦合常数。"
        "另一表达 E = ±√(κ² − σ²) 完全为实数。"
    )
    output, _ = prepare_publication_markdown(source)

    assert r"$E = \pm\sqrt{\kappa^{2} - \sigma^{2}}$，其中" in output
    assert r"$E = \pm\sqrt{\kappa^{2} - \sigma^{2}}$ 完全" in output
    assert "$\\kappa$ $^{" not in output


def test_public_prose_and_acronym_protocol_are_reader_facing() -> None:
    source = (
        "Bound state in the continuum (BIC) is useful. "
        "Bound state in the continuum (BIC) can be tuned. "
        "We commit to six deliverables for the review. "
        "The coverage_sufficient label is internal."
    )
    output, audit = prepare_publication_markdown(source)
    assert output.count("Bound state in the continuum (BIC)") == 1
    assert "BIC can be tuned" in output
    assert "deliverables" not in output.lower()
    assert "coverage_sufficient" not in output
    assert audit["public_prose_audit"]["status"] == "passed"


def test_acronym_ledger_does_not_swallow_sentence_grammar() -> None:
    source = (
        "Photonic platforms must satisfy several conditions to realize exceptional-point "
        "(EP) physics. Exceptional point (EP) sensing is then compared."
    )
    output, audit = prepare_publication_markdown(source)
    assert "Photonic platforms must satisfy" in output
    assert output.count("Exceptional point (EP)") == 1
    assert any(item["long_form"] == "Exceptional point" for item in audit["terminology_ledger"])


def test_s2_chunk_raw_metadata_is_first_class_bibliography_input(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kb.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE text_chunks (paper_id TEXT, doi TEXT, title TEXT, raw_json TEXT)"
        )
        raw = {
            "raw_metadata": {
                "s2_item": {
                    "paper": {
                        "title": "Structured S2 paper",
                        "authors": ["A. Author", "B. Author"],
                    }
                }
            }
        }
        connection.execute(
            "INSERT INTO text_chunks VALUES (?,?,?,?)",
            ("CorpusId:42", "", "Structured S2 paper", json.dumps(raw)),
        )
    records = _collect_kb_metadata(database, ["CorpusId:42"])
    assert records["CorpusId:42"]["title"] == "Structured S2 paper"
    assert records["CorpusId:42"]["authors"] == ["A. Author", "B. Author"]


def test_incomplete_reference_is_rejected_instead_of_placeholder() -> None:
    with pytest.raises(ValueError, match="incomplete bibliographic metadata"):
        _reference_bibtex(
            ["CorpusId:404"],
            {"CorpusId:404": "ref"},
            {"CorpusId:404": {"title": "Only a title"}},
        )


def test_corpusid_with_record_doi_uses_crossref_to_complete_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sparse S2 CorpusId must be enriched via its record DOI, not skipped."""

    def fake_crossref(doi: str, **_: object) -> dict[str, object]:
        assert doi == "10.1209/0295-5075/120/64001"
        return {
            "doi": doi,
            "title": "Parity-time symmetry meets photonics",
            "authors": ["S. Longhi"],
            "year": 2017,
            "venue": "Europhysics Letters",
            "metadata_source": "crossref",
        }

    monkeypatch.setattr(
        "optomind_research.runtime.latex_publication_renderer._crossref_metadata",
        fake_crossref,
    )
    records, audit = _merge_reference_metadata(
        ["CorpusId:119360704"],
        local_records={
            "CorpusId:119360704": {
                "doi": "https://doi.org/10.1209/0295-5075/120/64001",
                "title": "Parity-time symmetry meets photonics",
                "authors": ["S. Longhi"],
                "year": 2017,
            }
        },
        kb_records={},
        enrich_crossref=True,
        max_crossref_requests=1,
        enrich_s2=False,
    )
    assert records["CorpusId:119360704"]["venue"] == "Europhysics Letters"
    assert records["CorpusId:119360704"]["doi"] == "10.1209/0295-5075/120/64001"
    assert audit[0]["metadata_complete"] is True


def test_sparse_s2_record_can_use_verified_bibliographic_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_bibliographic(record: dict[str, object], **_: object) -> dict[str, object]:
        assert record["title"] == "Sparse S2 record"
        return {
            "doi": "10.9999/sparse.1",
            "title": "Sparse S2 record",
            "authors": ["A. Author"],
            "year": 2025,
            "venue": "Journal of Sparse Metadata",
            "metadata_source": "crossref_bibliographic_match",
        }

    monkeypatch.setattr(
        "optomind_research.runtime.latex_publication_renderer."
        "_crossref_metadata_by_bibliographic_record",
        fake_bibliographic,
    )
    records, audit = _merge_reference_metadata(
        ["CorpusId:55"],
        local_records={
            "CorpusId:55": {
                "title": "Sparse S2 record",
                "authors": ["A. Author"],
                "year": 2025,
            }
        },
        kb_records={},
        enrich_crossref=True,
        max_crossref_requests=1,
        enrich_s2=False,
    )
    assert records["CorpusId:55"]["venue"] == "Journal of Sparse Metadata"
    assert records["CorpusId:55"]["reference_kind"] == "article"
    assert audit[0]["metadata_complete"] is True


def test_verified_s2_record_without_venue_is_transparent_misc_not_placeholder() -> None:
    bibtex = _reference_bibtex(
        ["CorpusId:56"],
        {"CorpusId:56": "ref"},
        {
            "CorpusId:56": {
                "title": "Verified preprint record",
                "authors": ["A. Author"],
                "year": 2025,
                "url": "https://api.semanticscholar.org/graph/v1/paper/CorpusId%3A56",
                "reference_kind": "misc",
            }
        },
    )
    assert "@misc{ref" in bibtex
    assert "journal" not in bibtex
    assert "metadata unavailable" not in bibtex.lower()


def test_bibliography_record_without_locator_is_misc_with_pending_note() -> None:
    paper_id = "68287d50e506f3dfcb9e2a3eb56bed75f81df3c9"
    bibtex = _reference_bibtex(
        [paper_id],
        {paper_id: "bic_record"},
        {
            paper_id: {
                "title": "Verified BIC record",
                "authors": ["A. Author", "B. Author"],
                "year": 2024,
                "reference_kind": "misc",
            }
        },
    )
    assert "@misc{bic_record" in bibtex
    assert "Stable locator pending." in bibtex
    assert "doi =" not in bibtex
    assert "url =" not in bibtex


def test_merge_marks_stable_locator_pending_without_exception() -> None:
    paper_id = "68287d50e506f3dfcb9e2a3eb56bed75f81df3c9"
    records, audit = _merge_reference_metadata(
        [paper_id],
        local_records={
            paper_id: {
                "title": "Verified BIC record",
                "authors": ["A. Author", "B. Author"],
                "year": 2024,
            }
        },
        kb_records={},
        enrich_crossref=False,
        max_crossref_requests=0,
        enrich_s2=False,
    )
    row = audit[0]
    assert row["paper_id"] == paper_id
    assert row["stable_locator_pending"] is True
    assert row["identity_incomplete"] is False
    assert row["metadata_complete"] is False
    assert records[paper_id]["reference_kind"] == "misc"
    assert records[paper_id]["title"] == "Verified BIC record"


def test_publication_figure_crops_a_synthetic_embedded_caption(
    tmp_path: Path,
) -> None:
    from PIL import Image, ImageDraw

    source = tmp_path / "source.png"
    image = Image.new("RGB", (520, 260), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((45, 30, 475, 138), outline="navy", width=5)
    draw.line((60, 118, 460, 55), fill="darkred", width=5)
    # Deliberate whitespace separator followed by three caption-like text rows.
    for y, line in zip(
        (185, 202, 219),
        ("Publisher source caption line one", "caption line two", "caption line three"),
    ):
        draw.text((70, y), line, fill="black")
    image.save(source)
    destination = tmp_path / "publication.png"
    report = prepare_publication_figure(
        source,
        destination,
        {"caption_en": "Source figure context: Fig. 1. A source caption. Source: publisher."},
    )
    assert destination.is_file()
    assert report["caption_crop_status"] == "heuristic_embedded_caption_crop"
    assert "Source figure context" not in report["caption"]
    cropped = Image.open(destination)
    assert cropped.height < image.height
    assert report["publication_asset_eligible"] is True


def test_publication_figure_crops_page_screenshot_before_caption_and_body(
    tmp_path: Path,
) -> None:
    from PIL import Image, ImageDraw

    source = tmp_path / "publisher-page.png"
    image = Image.new("RGB", (600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((35, 35, 565, 245), outline="navy", width=6)
    draw.line((60, 210, 540, 70), fill="darkred", width=6)
    for y in (300, 325, 350, 420, 445, 470, 495, 520):
        draw.text((45, y), "Publisher caption or body copy", fill="black")
    image.save(source)

    destination = tmp_path / "publisher-page-publication.png"
    report = prepare_publication_figure(
        source,
        destination,
        {"caption_en": "A clean external caption."},
    )

    cropped = Image.open(destination)
    assert report["caption_crop_status"] == "heuristic_embedded_caption_crop"
    assert report["publication_asset_eligible"] is True
    assert 245 <= cropped.height < 300


def test_publication_figure_preserves_article_owned_conceptual_diagram(
    tmp_path: Path,
) -> None:
    from PIL import Image, ImageDraw

    source = tmp_path / "conceptual.png"
    image = Image.new("RGB", (640, 520), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((180, 60, 460, 160), outline="navy", width=5)
    draw.rectangle((180, 300, 460, 430), outline="darkred", width=5)
    draw.line((320, 160, 320, 300), fill="black", width=5)
    draw.text((210, 475), "AI-assisted explanatory diagram", fill="black")
    image.save(source)

    destination = tmp_path / "conceptual-publication.png"
    report = prepare_publication_figure(
        source,
        destination,
        {
            "figure_type": "structured_explanatory_diagram",
            "source_route": "conceptual_generated",
            "caption_en": "A generated explanatory diagram.",
        },
    )

    cropped = Image.open(destination)
    assert report["caption_crop_status"] == "preserved_no_caption_signal"
    assert cropped.size == image.size


def test_publication_figure_rejects_caption_only_strip(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    source = tmp_path / "caption-only.png"
    image = Image.new("RGB", (1000, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 20), "Figure captions", fill="black")
    draw.text((40, 125), "Figure 1. Caption without image pixels.", fill="black")
    image.save(source)

    report = prepare_publication_figure(
        source,
        tmp_path / "caption-only-publication.png",
        {"caption_en": "A claimed scientific figure."},
    )

    assert report["publication_asset_eligible"] is False
    assert report["publication_asset_rejection_reason"] in {
        "publication_crop_too_small",
        "publication_crop_extreme_aspect_ratio",
    }


def test_public_caption_strips_abbreviated_doi_provenance() -> None:
    from optomind_research.runtime.publication_figure_processor import clean_public_caption

    caption = clean_public_caption(
        "Explains a mechanism anchor for a photonic device. 1038/s41467-024-45226-x"
    )
    assert "1038/s41467" not in caption
    assert caption == "Explains a mechanism anchor for a photonic device."
