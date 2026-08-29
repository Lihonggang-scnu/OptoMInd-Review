from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from optomind_research.runtime.visual_crop_hygiene import (
    PREVIEW_MAX_DIM,
    QWEN_ADVISOR_CROP_METHOD,
    audit_crop_hygiene,
    create_cleaned_derivative,
    materialize_advisor_crop,
    validate_advisor_boxes,
)
from optomind_research.runtime.visual_qwen_crop_advisor import (
    advise_with_qwen,
    parse_qwen_crop_advice,
    repair_advisor_json,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def hygiene_tmp() -> Path:
    scratch = PROJECT_ROOT / ".codex-tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    root = scratch / f"visual-hygiene-test-{uuid.uuid4().hex[:10]}"
    root.mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)
    try:
        scratch.rmdir()
    except OSError:
        pass


def _draw_caption_figure(
    path: Path,
    *,
    caption_h: int = 60,
    width: int = 360,
    height: int = 260,
) -> Path:
    """Draw a colored figure area with a synthetic caption band at the bottom."""

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [
            (40, height - caption_h - 30),
            (120, 60),
            (200, height - caption_h - 60),
            (280, 80),
            (320, height - caption_h - 30),
        ],
        fill=(120, 180, 240),
        outline=(30, 90, 200),
        width=3,
    )
    draw.rectangle(
        [40, height - caption_h - 40, width - 40, height - caption_h - 20],
        fill=(240, 190, 120),
    )
    draw.ellipse([(width - 80, 40), (width - 40, 80)], fill=(40, 160, 80))
    y0 = height - caption_h
    for y in range(y0 + 8, height - 4, 10):
        x = 25
        while x < width - 60:
            draw.rectangle([x, y, x + 8, y + 6], fill=(20, 20, 20))
            x += 20
    image.save(path)
    return path


def _draw_clean_figure(path: Path, width: int = 360, height: int = 260) -> Path:
    return _draw_caption_figure(path, caption_h=0, width=width, height=height)


def _draw_prose_page(path: Path, width: int = 400, height: int = 520) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for y in range(20, height - 16, 8):
        x = 25
        while x < width - 60:
            draw.rectangle([x, y, x + 8, y + 6], fill=(30, 30, 30))
            x += 30
    image.save(path)
    return path


def _draw_tiny_bottom_band(
    path: Path,
    width: int = 360,
    height: int = 260,
) -> Path:
    """Clean figure with a low-confidence 6px text-like band at the bottom."""

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for x in range(20, width - 20, 12):
        draw.line([(x, 40), (x + 8, 120)], fill=(30, 90, 200), width=3)
    for x in range(25, width - 60, 30):
        draw.rectangle(
            [x, height - 12, x + 8, height - 6],
            fill=(20, 20, 20),
        )
    image.save(path)
    return path


def _draw_multiline_trailing_caption(
    path: Path,
    width: int = 360,
    height: int = 360,
    trailing: int = 10,
) -> Path:
    """Multiline bottom caption that ends above the image bottom (whitespace)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [
            (40, 250),
            (120, 60),
            (200, 230),
            (280, 80),
            (320, 250),
        ],
        fill=(120, 180, 240),
        outline=(30, 90, 200),
        width=3,
    )
    draw.rectangle(
        [40, 250, width - 40, 270],
        fill=(240, 190, 120),
    )
    for line_top in (
        height - trailing - 20,
        height - trailing - 8,
    ):
        for x in range(25, width - 60, 20):
            draw.rectangle(
                [x, line_top, x + 8, line_top + 8],
                fill=(20, 20, 20),
            )
    image.save(path)
    return path


def _draw_realistic_large_figure(
    path: Path,
    width: int = 1151,
    height: int = 1076,
) -> Path:
    """S01-like figure: colored plot + labels + ~100px bottom caption."""

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for y in range(100, 700, 40):
        draw.line([(60, y), (1050, y)], fill=(200, 210, 230), width=2)
    for x in range(100, 1050, 60):
        draw.line([(x, 100), (x, 700)], fill=(210, 215, 225), width=2)
    draw.polygon(
        [(120, 680), (400, 300), (700, 520), (1000, 220)],
        outline=(30, 120, 220),
        width=4,
    )
    draw.line([(100, 760), (1050, 760)], fill=(30, 120, 220), width=5)
    for label_x, label_y in ((180, 250), (520, 420), (900, 180)):
        for x in range(label_x, label_x + 90, 24):
            draw.rectangle(
                [x, label_y, x + 6, label_y + 9],
                fill=(30, 30, 30),
            )
    for line_top in range(height - 176, height - 62, 24):
        for x in range(120, 1000, 30):
            draw.rectangle(
                [x, line_top, x + 8, line_top + 12],
                fill=(20, 20, 20),
            )
    image.save(path)
    return path


def _draw_multiline_caption_light_first_line(path: Path) -> Path:
    """Multi-line bottom caption whose first line is lighter than ink."""

    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 360, 180
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([40, 30, width - 40, 110], fill=(120, 180, 240))
    draw.rectangle([40, 110, width - 40, 130], fill=(240, 190, 120))
    # Lighter first caption line (sparse, low transitions).
    for x in (90, 150, 210, 270, 330):
        draw.rectangle([x, 150, x + 4, 156], fill=(30, 30, 30))
    # Dense second caption line.
    for x in range(60, 300, 20):
        draw.rectangle([x, 160, x + 8, 166], fill=(20, 20, 20))
    image.save(path)
    return path


def _draw_two_column_region(path: Path) -> Path:
    """Draw a two-column PDF region with a narrow, right-offset caption."""

    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 800, 400
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for x in range(40, 360, 16):
        draw.line([(x, 40), (x + 10, 260)], fill=(30, 90, 200), width=4)
    for x in range(440, 760, 16):
        draw.line([(x, 40), (x + 10, 260)], fill=(220, 120, 30), width=4)
    for y in range(330, 372, 12):
        x = 545
        while x < 735:
            draw.rectangle([x, y, x + 10, y + 7], fill=(20, 20, 20))
            x += 26
    image.save(path)
    return path


def _draw_centered_caption_figure(path: Path) -> Path:
    """Draw a figure with a short but horizontally centered caption."""

    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 800, 400
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([60, 60, width - 60, 300], fill=(120, 180, 240))
    draw.ellipse([(560, 80), (740, 280)], fill=(40, 160, 80))
    draw.rectangle([60, 300, width - 60, 320], fill=(240, 190, 120))
    for y in range(330, 372, 12):
        x = 305
        while x < 495:
            draw.rectangle([x, y, x + 10, y + 7], fill=(20, 20, 20))
            x += 26
    image.save(path)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_clean_embedded_raster_image_passes_with_audited_reason(
    hygiene_tmp: Path,
) -> None:
    image = _draw_clean_figure(hygiene_tmp / "clean.png")
    before = image.read_bytes()
    audit = audit_crop_hygiene(
        {"extraction_method": "embedded_raster_image"},
        image,
    )
    assert audit["status"] == "clean"
    assert audit["source_kind"] == "embedded_raster_image"
    assert audit["derivative"] is None
    assert "embedded_or_html_image" in audit["reason"]
    assert image.read_bytes() == before


def test_caption_bbox_audit_creates_derivative_without_overwriting_source(
    hygiene_tmp: Path,
) -> None:
    image = _draw_caption_figure(hygiene_tmp / "caption.png", caption_h=60)
    before = image.read_bytes()
    before_sha = _sha256(image)
    record = {
        "extraction_method": "rendered_region_from_caption",
        "bbox_pdf": [30, 40, 390, 300],
        "caption_bbox": [40, 200, 320, 260],
    }
    audit = audit_crop_hygiene(
        record,
        image,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    assert audit["status"] == "derived_clean"
    derivative = audit["derivative"]
    assert derivative is not None
    assert derivative["crop_bbox_px"] == [0, 0, 360, 160]
    assert derivative["parent_sha256"] == before_sha
    assert derivative["height"] == 160
    derivative_path = hygiene_tmp / "derivatives" / derivative["filename"]
    assert derivative_path.is_file()
    assert derivative_path != image
    assert image.read_bytes() == before

    audit_without_materialization = audit_crop_hygiene(record, image)
    assert audit_without_materialization["status"] == "needs_review"
    assert audit_without_materialization["derivative"] is None


def test_high_confidence_heuristic_removes_bottom_caption_band(
    hygiene_tmp: Path,
) -> None:
    image = _draw_caption_figure(hygiene_tmp / "heuristic.png", caption_h=60)
    audit = audit_crop_hygiene(
        {"extraction_method": "rendered_region_from_caption"},
        image,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    assert audit["status"] == "derived_clean"
    assert audit["evidence"]["caption_band"]["confidence"] == "high"
    derivative = audit["derivative"]
    assert derivative is not None
    assert derivative["crop_bbox_px"] == [0, 0, 360, 208]
    assert derivative["height"] < 260


def test_ambiguous_prose_page_is_never_auto_approved(hygiene_tmp: Path) -> None:
    image = _draw_prose_page(hygiene_tmp / "prose.png")
    audit = audit_crop_hygiene(
        {"extraction_method": "rendered_region_from_caption"},
        image,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    assert audit["status"] in {"needs_review", "rejected"}
    assert audit["status"] != "clean"
    assert audit["status"] != "derived_clean"
    assert audit["derivative"] is None
    assert audit["evidence"]["prose_contamination"]["verdict"] != "none"


def test_rendered_region_without_confident_separation_is_not_clean(
    hygiene_tmp: Path,
) -> None:
    image = _draw_clean_figure(hygiene_tmp / "no-caption.png")
    audit = audit_crop_hygiene(
        {"extraction_method": "rendered_region_from_caption"},
        image,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    assert audit["status"] == "needs_review"
    assert audit["derivative"] is None


def test_derivative_creation_is_reusable_and_never_touches_source(
    hygiene_tmp: Path,
) -> None:
    source = _draw_caption_figure(hygiene_tmp / "reuse.png", caption_h=60)
    before = source.read_bytes()
    first = create_cleaned_derivative(
        source,
        output_dir=hygiene_tmp / "derivatives",
        crop_bbox_px=[0, 0, 360, 200],
    )
    second = create_cleaned_derivative(
        source,
        output_dir=hygiene_tmp / "derivatives",
        crop_bbox_px=[0, 0, 360, 200],
    )
    assert first["filename"] == second["filename"]
    assert first["sha256"] == second["sha256"]
    assert first["parent_sha256"] == _sha256(source)
    assert source.read_bytes() == before


def test_off_center_two_column_caption_is_not_derived_clean(
    hygiene_tmp: Path,
) -> None:
    image = _draw_two_column_region(hygiene_tmp / "two-column.png")
    before = image.read_bytes()
    record = {
        "extraction_method": "rendered_region_from_caption",
        "bbox_pdf": [0, 0, 800, 400],
        "caption_bbox": [540, 320, 740, 380],
    }
    audit = audit_crop_hygiene(
        record,
        image,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    assert audit["status"] == "needs_review"
    assert audit["derivative"] is None
    mapped = audit["evidence"]["mapped_caption_band"]
    assert mapped["multi_column_ambiguity"] is True
    assert mapped["horizontal_coverage"] < 0.60
    assert mapped["center_offset"] > 0.25
    assert "multi_column_or_off_center_ambiguity" in audit["reason"]
    assert image.read_bytes() == before


def test_centered_short_caption_still_derived_clean(
    hygiene_tmp: Path,
) -> None:
    image = _draw_centered_caption_figure(hygiene_tmp / "centered.png")
    record = {
        "extraction_method": "rendered_region_from_caption",
        "bbox_pdf": [0, 0, 800, 400],
        "caption_bbox": [300, 320, 500, 380],
    }
    audit = audit_crop_hygiene(
        record,
        image,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    assert audit["status"] == "derived_clean"
    derivative = audit["derivative"]
    assert derivative is not None
    mapped = audit["evidence"]["mapped_caption_band"]
    assert mapped["multi_column_ambiguity"] is False
    assert mapped["horizontal_coverage"] < 0.60
    assert mapped["center_offset"] <= 0.25
    assert derivative["crop_bbox_px"] == [0, 0, 800, 320]


def test_full_width_caption_still_derived_clean(hygiene_tmp: Path) -> None:
    image = _draw_caption_figure(hygiene_tmp / "full-width.png", caption_h=60)
    record = {
        "extraction_method": "rendered_region_from_caption",
        "bbox_pdf": [0, 0, 360, 260],
        "caption_bbox": [20, 200, 340, 260],
    }
    audit = audit_crop_hygiene(
        record,
        image,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    assert audit["status"] == "derived_clean"
    mapped = audit["evidence"]["mapped_caption_band"]
    assert mapped["multi_column_ambiguity"] is False
    assert mapped["horizontal_coverage"] >= 0.60


def test_low_confidence_tiny_bottom_band_is_clean(
    hygiene_tmp: Path,
) -> None:
    image = _draw_tiny_bottom_band(hygiene_tmp / "tiny.png")
    audit = audit_crop_hygiene({}, image)
    assert audit["status"] == "clean"
    band = audit["evidence"]["caption_band"]
    assert band["found"] is True
    assert band["confidence"] == "low"
    assert audit["evidence"]["prose_contamination"]["verdict"] == "none"
    assert audit["derivative"] is None


def test_rendered_region_multiline_caption_trailing_whitespace_derived_clean(
    hygiene_tmp: Path,
) -> None:
    image = _draw_multiline_trailing_caption(
        hygiene_tmp / "trailing.png"
    )
    record = {"source_provenance": {"parser": "pymupdf_caption_crop"}}
    audit = audit_crop_hygiene(
        record,
        image,
        derivative_dir=hygiene_tmp / "deriv",
        create_derivative=True,
    )
    assert audit["status"] == "derived_clean"
    band = audit["evidence"]["caption_band"]
    assert band["found"] is True
    assert band["confidence"] == "high"
    assert audit["derivative"] is not None
    # Figure area is preserved: the crop keeps every row above the band.
    assert audit["derivative"]["crop_bbox_px"] == [
        0,
        0,
        360,
        band["band_top"],
    ]


def test_realistic_large_figure_with_bottom_caption_derived_clean(
    hygiene_tmp: Path,
) -> None:
    image = _draw_realistic_large_figure(hygiene_tmp / "s01-like.png")
    before = image.read_bytes()
    audit = audit_crop_hygiene(
        {"extraction_method": "rendered_region_from_caption"},
        image,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    assert audit["status"] == "derived_clean"
    band = audit["evidence"]["caption_band"]
    assert band["found"] is True
    assert band["confidence"] == "high"
    assert band["band_height"] >= 60
    derivative = audit["derivative"]
    assert derivative is not None
    x0, y0, x1, y1 = derivative["crop_bbox_px"]
    assert [x0, y0, x1] == [0, 0, 1151]
    assert y1 <= 950  # the ~100px bottom caption is excluded
    assert y1 >= 800  # the figure area above is preserved
    assert derivative["height"] < 1076
    assert audit["evidence"]["preview"]["max_dim"] == PREVIEW_MAX_DIM
    assert image.read_bytes() == before


def test_large_image_audit_uses_bounded_preview(
    hygiene_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.runtime.visual_crop_hygiene as hygiene_module

    image = _draw_realistic_large_figure(hygiene_tmp / "preview.png")
    seen: dict = {}
    original_stats = hygiene_module._image_stats

    def recording_stats(pil_image):
        seen["size"] = pil_image.size
        return original_stats(pil_image)

    monkeypatch.setattr(hygiene_module, "_image_stats", recording_stats)
    audit = audit_crop_hygiene(
        {"extraction_method": "rendered_region_from_caption"},
        image,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    assert seen["size"][0] <= PREVIEW_MAX_DIM
    assert seen["size"][1] <= PREVIEW_MAX_DIM
    assert audit["status"] == "derived_clean"


def test_real_oa_fixture_page_prose_flagged_fail_open(
    hygiene_tmp: Path,
) -> None:
    """Regression: the real 1151x1076 rendered S01 figure contains page prose."""

    fixture = (
        PROJECT_ROOT
        / "outputs/visual_procurement_multi_oa_noqwen_20260814_v3"
        / "visual_cache/snapshot-0001/assets"
        / "23ea8302602e3fb132d70fe4af70f8ef1c66a2d017b3c8f0b81c2a9bacd3d155.png"
    )
    if not fixture.is_file():
        pytest.skip("real OA fixture not present in this checkout")
    audit = audit_crop_hygiene(
        {"extraction_method": "rendered_region_from_caption"},
        fixture,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    prose = audit["evidence"]["prose_contamination"]
    assert prose is not None
    assert prose["verdict"] != "none"
    assert audit["status"] in {"needs_review", "rejected"}
    # Fail-open: never rejected/removed solely by this signal beyond the
    # review flag; the original file stays untouched.
    assert fixture.read_bytes()


def test_real_qwen_oa_fixture_short_bottom_caption_page_prose(
    hygiene_tmp: Path,
) -> None:
    """Regression: real 1122x907 Qwen figure has prose + a short caption."""

    fixture = (
        PROJECT_ROOT
        / "outputs/visual_procurement_multi_oa_qwen37flash_20260814_v5"
        / "visual_cache/snapshot-0001/assets"
        / "d7ede5769a3b03031cb93cd50ef84d5066426a43ee7bfc2d947c03eaa3dd18ca.png"
    )
    if not fixture.is_file():
        pytest.skip("real Qwen OA fixture not present in this checkout")
    audit = audit_crop_hygiene(
        {"extraction_method": "rendered_region_from_caption"},
        fixture,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    prose = audit["evidence"]["prose_contamination"]
    assert prose is not None
    assert prose["verdict"] != "none"
    assert audit["status"] in {"needs_review", "rejected"}
    band = audit["evidence"]["caption_band"]
    assert band["found"] is True
    assert band["confidence"] == "high"
    assert band["band_transition_avg"] >= 24


def test_real_qwen_v6_two_column_prose_flagged(hygiene_tmp: Path) -> None:
    """Regression: real v6 rendered region with a full text column."""

    fixture = (
        PROJECT_ROOT
        / "outputs/visual_procurement_multi_oa_qwen37flash_20260814_v6"
        / "visual_cache/snapshot-0001/assets"
        / "82ba785eadd2d4710995935415827e2a4f2641581d58ace16af4fbb34c6f802f.png"
    )
    if not fixture.is_file():
        pytest.skip("real Qwen v6 OA fixture not present in this checkout")
    audit = audit_crop_hygiene(
        {"extraction_method": "rendered_region_from_caption"},
        fixture,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    prose = audit["evidence"]["prose_contamination"]
    assert prose is not None
    assert prose["verdict"] != "none"
    assert audit["status"] in {"needs_review", "rejected"}


def test_multiline_caption_light_first_line_removed_entirely(
    hygiene_tmp: Path,
) -> None:
    """The whole multi-line caption block is removed, including a light
    first line that falls below the dense-ink threshold."""

    image = _draw_multiline_caption_light_first_line(
        hygiene_tmp / "light-first-line.png"
    )
    before = image.read_bytes()
    audit = audit_crop_hygiene(
        {"extraction_method": "rendered_region_from_caption"},
        image,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    assert audit["status"] == "derived_clean"
    band = audit["evidence"]["caption_band"]
    assert band["found"] is True
    assert band["confidence"] == "high"
    assert band["band_top"] == 150  # the lighter first line is included
    assert band["band_bottom"] == 166
    derivative = audit["derivative"]
    assert derivative is not None
    x0, y0, x1, y1 = derivative["crop_bbox_px"]
    assert [x0, y0, x1] == [0, 0, 360]
    assert y1 <= 150  # ends above the first (light) caption line
    assert y1 < 160  # and above the dense second line
    assert derivative["height"] < 180
    assert image.read_bytes() == before


def test_real_v10_derived_clean_removes_entire_caption(
    hygiene_tmp: Path,
) -> None:
    """Regression on the real v10 original whose old derivative kept the
    caption's first line."""

    fixture = (
        PROJECT_ROOT
        / "outputs/visual_procurement_multi_oa_qwen37flash_20260814_v10"
        / "visual_cache/snapshot-0001/assets"
        / "00ec8e2e8ac6ffd06783f993ff3ff9a92c66f6bca91620cc1af17cf0b8e73fef.png"
    )
    if not fixture.is_file():
        pytest.skip("real v10 OA fixture not present in this checkout")
    audit = audit_crop_hygiene(
        {"extraction_method": "rendered_region_from_caption"},
        fixture,
        derivative_dir=hygiene_tmp / "derivatives",
        create_derivative=True,
    )
    assert audit["status"] == "derived_clean"
    band = audit["evidence"]["caption_band"]
    assert band["found"] is True
    assert band["confidence"] == "high"
    assert band["band_top"] <= 390  # above the caption first line (~387)
    derivative = audit["derivative"]
    assert derivative is not None
    x0, y0, x1, y1 = derivative["crop_bbox_px"]
    assert [x0, y0, x1] == [0, 0, 1013]
    assert y1 <= 390  # ends above all caption rows


def _advisor_advice(**overrides: object) -> dict:
    payload: dict = {
        "schema_version": "optomind.visual_qwen_crop_advisor.v1",
        "ok": True,
        "needs_review": False,
        "asset_kind": "table",
        "content_bbox": [0.1, 0.1, 0.9, 0.75],
        "caption_bbox": [0.05, 0.8, 0.95, 0.98],
        "panel_boxes": [],
        "caption_text": "Table 1. Summary of performance.",
        "confidence": 0.92,
        "contamination_notes": [],
        "advisor": {"model": "qwen-test"},
    }
    payload.update(overrides)
    return payload


def test_qwen_advisor_parser_repairs_and_normalizes_semantic_boxes() -> None:
    raw = (
        "```json\n"
        "{'asset_kind': 'table', 'content_bbox': [36, 26, 324, 195],\n"
        " 'caption_bbox': [18, 208, 342, 255],\n"
        " 'caption_text': 'Table 1. Caption.', 'confidence': 0.9,\n"
        " 'contamination_notes': [],}\n"
        "```"
    )
    parsed = repair_advisor_json(raw)
    assert parsed["asset_kind"] == "table"
    advice = parse_qwen_crop_advice(
        raw,
        image_width=360,
        image_height=260,
    )
    assert advice["ok"] is True
    assert advice["asset_kind"] == "table"
    assert advice["content_bbox"] == [0.1, 0.1, 0.9, 0.75]
    assert advice["caption_bbox"][3] == round(255 / 260, 6)
    assert advice["errors"] == []


def test_qwen_advisor_parser_rejects_overlap_and_low_confidence() -> None:
    advice = parse_qwen_crop_advice(
        json.dumps(
            _advisor_advice(
                caption_bbox=[0.1, 0.7, 0.9, 0.95],
                confidence=0.1,
            )
        )
    )
    assert advice["needs_review"] is True
    assert "caption_bbox_overlaps_content_bbox" in advice["errors"]
    assert any(
        error.startswith("confidence_low:0.1") for error in advice["errors"]
    )
    assert "caption_overlaps_content" in advice["contamination_notes"]


def test_materialize_advisor_crop_creates_immutable_derivative_and_audits(
    hygiene_tmp: Path,
) -> None:
    source = _draw_clean_figure(hygiene_tmp / "advisor-source.png")
    before = source.read_bytes()
    result = materialize_advisor_crop(
        source,
        output_dir=hygiene_tmp / "derivatives",
        advice=_advisor_advice(),
    )
    assert result["status"] == "derived_clean"
    assert result["source_kind"] == QWEN_ADVISOR_CROP_METHOD
    derivative = result["derivative"]
    assert derivative is not None
    assert derivative["parent_sha256"] == hashlib.sha256(before).hexdigest()
    assert len(derivative["crop_bbox_px"]) == 4
    assert Path(derivative["path"]).is_file()
    assert result["evidence"]["derivative_audit"]["status"] == "clean"
    assert source.read_bytes() == before


def test_materialize_advisor_crop_rejects_overlap_and_preserves_source(
    hygiene_tmp: Path,
) -> None:
    source = _draw_clean_figure(hygiene_tmp / "advisor-overlap.png")
    before = source.read_bytes()
    result = materialize_advisor_crop(
        source,
        output_dir=hygiene_tmp / "derivatives",
        advice=_advisor_advice(
            caption_bbox=[0.1, 0.7, 0.9, 0.95],
        ),
    )
    assert result["status"] == "needs_review"
    assert result["derivative"] is None
    assert "caption_bbox_overlaps_content_bbox" in result["reason"]
    assert source.read_bytes() == before


def test_validate_advisor_boxes_rejects_overlapping_panels() -> None:
    validated, errors = validate_advisor_boxes(
        _advisor_advice(
            panel_boxes=[
                [0.1, 0.1, 0.6, 0.7],
                [0.5, 0.1, 0.9, 0.7],
            ],
        ),
        image_width=360,
        image_height=260,
    )
    assert validated["content_bbox_px"] == [36, 26, 324, 195]
    assert any("panel_boxes_overlap" in error for error in errors)


def test_advise_with_qwen_fails_open_and_never_writes_pixels(
    hygiene_tmp: Path,
) -> None:
    image = _draw_clean_figure(hygiene_tmp / "advisor-mock.png")
    before = image.read_bytes()

    def exploding(prompt, image_path):
        raise RuntimeError("qwen unavailable")

    advice = advise_with_qwen(
        exploding,
        image,
        advisor_model="qwen-test",
    )
    assert advice["needs_review"] is True
    assert any(
        error.startswith("qwen_call_failed:RuntimeError")
        for error in advice["errors"]
    )
    assert image.read_bytes() == before
    assert not list((hygiene_tmp / "derivatives").glob("*"))
