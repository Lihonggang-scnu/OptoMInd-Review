from __future__ import annotations

import fitz

from optomind_research.visual_asset_extractor import _region_for_caption


def test_stacked_figure_crop_does_not_include_previous_figure() -> None:
    document = fitz.open()
    try:
        page = document.new_page(width=595, height=842)
        visual_rects = [
            fitz.Rect(119, 63, 552, 257),
            fitz.Rect(129, 73, 543, 228),
            fitz.Rect(119, 274, 552, 435),
            fitz.Rect(129, 284, 543, 401),
        ]
        first_caption = {
            "kind": "figure",
            "caption_bbox": (170, 239, 502, 247),
        }
        second_caption = {
            "kind": "figure",
            "caption_bbox": (129, 408, 533, 425),
        }

        first_region = _region_for_caption(page, first_caption, visual_rects)
        second_region = _region_for_caption(
            page,
            second_caption,
            visual_rects,
            min_visual_y=first_caption["caption_bbox"][3],
        )

        assert first_region is not None
        assert second_region is not None
        assert first_region.y0 < 100
        assert second_region.y0 > 250
    finally:
        document.close()
