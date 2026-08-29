import json
import sqlite3
from pathlib import Path

from optomind_research.visual_argument_alignment import VisualArgumentAligner
from optomind_research.visual_evidence_reranker import VisualEvidenceReranker


def test_legacy_kb_visual_profile_is_bridged_to_m4(tmp_path: Path) -> None:
    image = tmp_path / "figure.png"
    image.write_bytes(b"not-a-real-image-but-path-exists")
    db = tmp_path / "kb.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE visual_chunks (chunk_id TEXT PRIMARY KEY, paper_id TEXT, "
        "caption TEXT, local_image_path TEXT, raw_json TEXT NOT NULL)"
    )
    raw = {
        "chunk_id": "V1",
        "paper_id": "P1",
        "caption": "Measured TE and TM transmission spectra versus angle.",
        "local_image_path": str(image),
        "visual_profile": {
            "intrinsic_visual_labels": {
                "visual_role": "spectrum",
                "functional_visual_type": "transmission_spectrum",
            },
            "review_task_labels": {
                "review_utility": "high",
                "argument_function": "quantitative comparison",
            },
            "qa": {"confidence": "high", "needs_human_review": False},
        },
    }
    conn.execute(
        "INSERT INTO visual_chunks VALUES(?,?,?,?,?)",
        ("V1", "P1", raw["caption"], str(image), json.dumps(raw)),
    )
    conn.commit()
    conn.close()

    rows = VisualArgumentAligner().load_visual_chunks_from_sqlite(db)
    assert len(rows) == 1
    assert rows[0]["visual_argument_status"] == "ok"
    assert rows[0]["visual_argument_type"] == "quantitative_comparison"
    assert rows[0]["visual_argument_confidence"] == "high"


def test_rerank_cache_parent_is_created_eagerly(tmp_path: Path) -> None:
    cache = tmp_path / "new" / "nested" / "visual_rerank.jsonl"
    VisualEvidenceReranker(cache_path=cache)
    assert cache.parent.is_dir()
