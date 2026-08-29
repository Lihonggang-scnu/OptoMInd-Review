"""Regression coverage for planned-section preservation and ID-based audits."""

from __future__ import annotations

import json

from optomind_research.runtime.article_structure_auditor import (
    _actual_body_section_ids,
)
from optomind_research.runtime.full_manuscript_handoff import (
    build_full_manuscript_handoff,
)


def test_handoff_keeps_explicitly_missing_planned_section(tmp_path):
    manifest_path = tmp_path / "full_manuscript_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "optomind.full_manuscript_handoff.manifest.v2",
                "project_root": str(tmp_path),
                "sections": [
                    {
                        "section_id": "S09",
                        "title": "Reliability limits",
                        "content_status": "explicitly_missing",
                        "failure": {"reason": "authoring_failed"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = build_full_manuscript_handoff(
        manifest_path=manifest_path,
        output_dir=tmp_path / "handoff",
    )
    handoff = json.loads((tmp_path / "handoff" / "UNIFIED_MANUSCRIPT_HANDOFF.json").read_text(encoding="utf-8"))

    assert result["section_order"] == ["S09"]
    assert handoff["sections"]["S09"]["content_status"] == "explicitly_missing"
    assert handoff["sections"]["S09"]["word_count"] == 0


def test_identity_audit_names_missing_and_unexpected_sections():
    planned = [
        {"section_id": "S01", "title": "Mechanisms", "content_status": "enhanced"},
        {"section_id": "S09", "title": "Reliability limits", "content_status": "explicitly_missing"},
    ]

    actual, unexpected = _actual_body_section_ids(
        "## Mechanisms\n\nBody\n\n## S11: unrelated\n\nBody",
        planned,
    )

    assert actual == ["S01"]
    assert unexpected == ["S11: unrelated"]
