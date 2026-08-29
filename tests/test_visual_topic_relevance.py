"""Topic-identity gates for visual-editor candidate retrieval."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from PIL import Image

from optomind_research.runtime.visual_editor_tool_provider import (
    VisualEditorContext,
    VisualEditorToolProvider,
)


def _write_image(path: Path, color: str) -> None:
    Image.new("RGB", (160, 100), color=color).save(path)


def test_off_topic_neural_rendering_candidate_is_not_shortlisted(
    tmp_path: Path,
) -> None:
    on_topic_image = tmp_path / "diffractive.png"
    off_topic_image = tmp_path / "gtr.png"
    _write_image(on_topic_image, "navy")
    _write_image(off_topic_image, "orange")
    kb_path = tmp_path / "visual.sqlite"
    conn = sqlite3.connect(str(kb_path))
    try:
        conn.execute(
            """
            CREATE TABLE visual_chunks(
              chunk_id TEXT PRIMARY KEY,
              paper_id TEXT,
              doi TEXT,
              title TEXT,
              caption TEXT,
              chunk_kind TEXT,
              search_text TEXT,
              visual_argument_type TEXT,
              visual_argument_status TEXT,
              visual_argument_confidence TEXT,
              visual_argument_claim TEXT,
              visual_argument_needs_human_review INTEGER,
              visual_argument_schema_version TEXT,
              local_image_path TEXT,
              raw_json TEXT
            )
            """
        )
        rows = [
            (
                "diffractive-1",
                "paper-diffractive",
                "",
                "Diffractive phase network",
                "Diffractive phase modulation and wavefront propagation.",
                "single_figure",
                "diffractive phase modulation wavefront propagation neural network imaging",
                "mechanism_anchor",
                "pending_multimodal_review",
                "high",
                "",
                1,
                "visual_argument_protocol.v1",
                str(on_topic_image),
                "{}",
            ),
            (
                "gtr-1",
                "paper-gtr",
                "",
                "GTR neural rendering",
                "Neural network for 3D reconstruction and novel views.",
                "single_figure",
                "neural network 3D reconstruction novel view synthesis imaging",
                "mechanism_anchor",
                "pending_multimodal_review",
                "high",
                "",
                1,
                "visual_argument_protocol.v1",
                str(off_topic_image),
                "{}",
            ),
        ]
        conn.executemany(
            "INSERT INTO visual_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    blueprint = {
        "topic_identity": {
            "core_anchor_tokens": ["diffractive", "neural", "network"],
            "supporting_anchor_tokens": ["phase", "wavefront", "propagation"],
            "anchor_phrases": ["optical diffractive neural network"],
        },
        "review_thesis": "Compare optical diffractive neural network architectures.",
        "sections": [
            {
                "section_id": "S01",
                "title": "Diffractive neural network imaging",
                "argument_role": "Explain diffractive phase and wavefront propagation.",
                "visual_argument_slots": [{"purpose": "mechanism"}],
            }
        ],
    }
    provider = VisualEditorToolProvider(
        VisualEditorContext(
            blueprint=blueprint,
            review_work_dir=tmp_path / "review",
            work_dir=tmp_path / "editor",
            kb_sqlite_paths=[kb_path],
        )
    )

    candidates = provider._verified_candidates_for_section("S01", top_k=4)

    assert [candidate["chunk_id"] for candidate in candidates] == [
        "diffractive-1"
    ]
    assert candidates[0]["topic_relevance"] == "matched"
    assert "diffractive" in candidates[0]["topic_anchor_hits"]
    assert "gtr-1" in provider._topic_relevance_rejected_ids
