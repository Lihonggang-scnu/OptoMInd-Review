"""Round-2 defect B: manifest quality_flags must read the real audit keys.

Round 1 read a nonexistent ``audit["quality"]`` and reported missing=0 on
an all-dangling library -- the opposite of the truth.  These tests pin the
correct ``quality_signals`` / ``counts`` sources.
"""

from __future__ import annotations

import json
from pathlib import Path

from optomind_research.review_knowledge_base import (
    ReviewKnowledgeBaseBuilder,
    ReviewKnowledgeBaseInputs,
)


def _write_source_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    return path


def _build_kb_with_dangling_visuals(tmp_path: Path, dangling: int):
    src = tmp_path / "inputs"
    out = tmp_path / "kb_out"
    papers = [
        {
            "paper_id": "P0",
            "doi": "",
            "title": "Paper zero",
            "year": 2024,
            "authors": [],
            "venue": "",
            "abstract": "An abstract.",
            "source_membership": {"paper_cards": True},
        }
    ]
    text_chunks = [
        {
            "chunk_id": "t0",
            "paper_id": "P0",
            "chunk_kind": "body",
            "text": "Body text.",
            "search_text": "Body text.",
            "source_membership": {},
        }
    ]
    visual_chunks = [
        {
            "chunk_id": f"v{index}",
            "paper_id": "P0",
            "chunk_kind": "single_figure",
            "parent_asset_id": f"v{index}",
            "local_image_path": str(tmp_path / f"dangling_{index}.png"),
            "caption": "",
            "search_text": "",
            "source_membership": {},
        }
        for index in range(dangling)
    ]
    inputs = ReviewKnowledgeBaseInputs(
        paper_cards_jsonl=_write_source_jsonl(
            src / "papers.jsonl", papers
        ),
        text_chunks_jsonl=_write_source_jsonl(
            src / "text_chunks.jsonl", text_chunks
        ),
        visual_assets_jsonl=_write_source_jsonl(
            src / "visual_assets.jsonl", []
        ),
        visual_chunks_jsonl=_write_source_jsonl(
            src / "visual_chunks.jsonl", visual_chunks
        ),
    )
    result = ReviewKnowledgeBaseBuilder(inputs=inputs, output_dir=out).build()
    manifest = json.loads(
        Path(result.manifest_path).read_text(encoding="utf-8")
    )
    audit = json.loads(Path(result.audit_path).read_text(encoding="utf-8"))
    return audit, manifest


def test_manifest_quality_flags_report_truthful_missing_count(
    tmp_path: Path,
) -> None:
    """Defect B: missing>0 must survive into the manifest, not flatten to 0."""

    audit, manifest = _build_kb_with_dangling_visuals(tmp_path, dangling=4)

    assert (
        audit["quality_signals"]["visual_chunks_missing_image_count"] == 4
    ), "precondition: every fixture image is dangling"
    assert audit["counts"]["visual_chunks"] == 4

    flags = manifest.get("quality_flags") or {}
    assert flags.get("visual_chunks_missing_image_count") == 4, (
        "manifest must surface the true missing count, not a flattened 0"
    )
    assert flags.get("visual_chunks_with_image_count") == 0, (
        "with-image count must not equal the total when everything dangles"
    )


def test_manifest_quality_flags_zero_when_all_present(
    tmp_path: Path,
) -> None:
    """Healthy libraries keep flags at zero (no false alarms either way)."""

    src = tmp_path / "inputs"
    out = tmp_path / "kb_out"
    image = tmp_path / "visual_candidates" / "fig.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"png")
    papers = [
        {
            "paper_id": "P0",
            "doi": "",
            "title": "Paper zero",
            "year": 2024,
            "authors": [],
            "venue": "",
            "abstract": "An abstract.",
            "source_membership": {"paper_cards": True},
        }
    ]
    visual_chunks = [
        {
            "chunk_id": "v0",
            "paper_id": "P0",
            "chunk_kind": "single_figure",
            "parent_asset_id": "v0",
            "local_image_path": str(image),
            "caption": "",
            "search_text": "",
            "source_membership": {},
        }
    ]
    inputs = ReviewKnowledgeBaseInputs(
        paper_cards_jsonl=_write_source_jsonl(src / "papers.jsonl", papers),
        text_chunks_jsonl=_write_source_jsonl(src / "text_chunks.jsonl", []),
        visual_assets_jsonl=_write_source_jsonl(
            src / "visual_assets.jsonl", []
        ),
        visual_chunks_jsonl=_write_source_jsonl(
            src / "visual_chunks.jsonl", visual_chunks
        ),
    )
    result = ReviewKnowledgeBaseBuilder(inputs=inputs, output_dir=out).build()
    manifest = json.loads(
        Path(result.manifest_path).read_text(encoding="utf-8")
    )

    flags = manifest.get("quality_flags") or {}
    assert flags.get("visual_chunks_missing_image_count") == 0
    assert flags.get("visual_chunks_with_image_count") == 1
