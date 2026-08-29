"""Final visual packages can be reused as remountable cache inputs."""

from __future__ import annotations

import json
from pathlib import Path

from optomind_research.runtime.visual_asset_planner_adapter import (
    load_visual_cache_records,
)


def test_final_visual_package_preserves_draft_permission_boundary(
    tmp_path: Path,
) -> None:
    source_image = tmp_path / "source.png"
    source_image.write_bytes(b"source")
    generated_image = tmp_path / "generated.png"
    generated_image.write_bytes(b"generated")
    package = {
        "schema_version": "research_harness.final_visual_package.v1",
        "figures": [
            {
                "figure_id": "FIG-SRC-001",
                "purpose": "A BIC momentum-space Q-factor map.",
                "figure_type": "source_single",
                "local_path": str(source_image),
                "caption_en": "Q-factor map for an optical BIC.",
                "generated_or_source": "source",
                "publication_eligible": False,
                "rights_notice": "Internal-study use only.",
                "review_decision": "timeout_accepted_for_draft",
                "source_audit": {"verdict": "approve"},
                "original_source_path": (
                    "cache/10.1038-s41377-022-01017-x/page001_Fig._1.png"
                ),
            },
            {
                "figure_id": "FIG-GEN-001",
                "purpose": "Explain BIC to quasi-BIC conversion.",
                "figure_type": "structured_explanatory_diagram",
                "local_path": str(generated_image),
                "caption_en": "AI-assisted mechanism diagram.",
                "generated_or_source": "generated",
                "review_decision": "accepted",
            },
        ],
    }
    package_path = tmp_path / "FINAL_VISUAL_PACKAGE.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")

    records = load_visual_cache_records(package_path)

    assert len(records) == 2
    source = records[0]
    assert source["visual_argument_status"] == "ok"
    assert source["permission"]["status"] == "requires_review"
    assert source["publication_eligible"] is False
    assert source["doi"] == "10.1038/s41377-022-01017-x"
    generated = records[1]
    assert generated["generated_visual"] is True
    assert generated["permission"]["status"] == "allowed"
    assert generated["publication_eligible"] is True
    assert generated["required_disclosure"] == (
        "AI-generated explanatory visual"
    )


def test_plain_keyed_json_records_remain_supported(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps({"legacy-id": {"caption": "Legacy visual."}}),
        encoding="utf-8",
    )

    records = load_visual_cache_records(path)

    assert records == [
        {"caption": "Legacy visual.", "chunk_id": "legacy-id"}
    ]
