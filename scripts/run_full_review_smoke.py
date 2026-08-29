#!/usr/bin/env python3
"""Minimal smoke runner for FullReviewOrchestrator.

Used by tests/test_phase4_smoke.py.  In production use run_review_harness.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_ROOT: Path = PROJECT_ROOT / "outputs" / "full_review_smoke"


def _run_minimal(run_id: str) -> int:
    """Run the minimal FullReviewOrchestrator smoke and return exit code."""
    from optomind_research.runtime.full_review_orchestrator import (
        FullReviewOrchestrator,
        OrchestratorConfig,
    )

    output_dir = OUTPUT_ROOT / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # OrchestratorConfig requires blueprint_path; use a placeholder for smoke
    placeholder_bp = output_dir / "SMOKE_BLUEPRINT.json"
    if not placeholder_bp.exists():
        import json as _json
        placeholder_bp.write_text(
            _json.dumps({"sections": [], "smoke": True}), encoding="utf-8"
        )

    config = OrchestratorConfig(
        blueprint_path=placeholder_bp,
        output_root=output_dir,
        max_revision_rounds=1,
    )
    orchestrator = FullReviewOrchestrator(config)
    result = orchestrator.run()

    work_dir = Path(result.work_dir)
    registry_path = work_dir / "SECTION_REGISTRY.json"
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        for sec in registry.get("sections") or []:
            wd = sec.get("work_dir")
            if wd:
                draft = Path(wd) / "SECTION_DRAFT_EN.md"
                if draft.exists():
                    text = draft.read_text(encoding="utf-8")
                    if len(text) < 5:
                        print(
                            f"WARNING: section {sec.get('section_id')} draft is very short",
                            flush=True,
                        )

    citation_map = work_dir / "FULL_REVIEW_CITATION_MAP.json"
    if citation_map.exists():
        data = json.loads(citation_map.read_text(encoding="utf-8"))
        total = len(data.get("citations") or [])
        print(f"Citation map: {total} citation(s)", flush=True)

    print(f"Smoke run {run_id} completed — status={result.status}", flush=True)
    return 0


if __name__ == "__main__":
    import uuid
    run_id = "smoke_" + uuid.uuid4().hex[:8]
    sys.exit(_run_minimal(run_id))
