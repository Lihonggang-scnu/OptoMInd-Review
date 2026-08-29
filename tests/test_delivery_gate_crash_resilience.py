"""Regression tests for three defects found auditing the P0-2 rework (4a28500).

Each test pins a fix that the rework's own suites could not catch, because
``_finish`` wraps ``_finish_impl`` in a blanket ``except Exception`` that turns
any programming error into ``status="failed"`` -- a signature indistinguishable
from a legitimate fail-closed outcome.

Defect A: ``build_delivery_gate`` crashed on a non-UTF-8 partial transcript,
          because ``UnicodeDecodeError`` derives from ``ValueError``, not
          ``OSError``.  One unreadable partial took down every other check.
Defect B: ``translation_ok`` was bound only inside the translation ``try``, so
          an exception from ``translate_review_package`` raised
          ``UnboundLocalError`` before ``build_delivery_gate`` ran -- silently
          discarding the already-paid English deliverable and the gate itself.
Defect C: a degraded gate produced the terminal status
          ``completed_with_warnings``, which appears in no consumer whitelist:
          CLI exit code 1 (indistinguishable from hard failure) and r6
          ``failed_or_incomplete``.
"""

import json
from pathlib import Path

from optomind_research.runtime import review_harness_orchestrator as module
from optomind_research.runtime.delivery_contract import build_delivery_gate
from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)
from optomind_research.runtime.r6_cross_topic_regression import (
    NONTERMINAL_STATUSES,
)


def _write_pdf(path: Path) -> None:
    from pypdf import PdfWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)


def _base_package(tmp_path: Path) -> dict:
    review_pdf = tmp_path / "review.pdf"
    _write_pdf(review_pdf)
    return {
        "schema_version": "research_harness.content_package.v1",
        "run_id": "test",
        "status": "completed",
        "completed_stage": "packaging",
        "review_body_validation": {"status": "passed"},
        "quality_gate": {"status": "passed", "metrics": {}},
        "artifacts": {"latex_pdf": str(review_pdf)},
        "latex_pdf_path": str(review_pdf),
        "final_review_path": str(tmp_path / "review.md"),
    }


def test_undecodable_partial_does_not_crash_the_whole_gate(
    tmp_path: Path,
) -> None:
    """Defect A: a mojibake partial degrades its own check, nothing else."""
    package = _base_package(tmp_path)
    zh_pdf = tmp_path / "review_zh.pdf"
    _write_pdf(zh_pdf)
    package["chinese_latex_pdf_path"] = str(zh_pdf)
    package["artifacts"]["chinese_latex_pdf"] = str(zh_pdf)

    # A partial written with the wrong codec: valid bytes, not valid UTF-8.
    partial_md = tmp_path / "FINAL_REVIEW_ZH.partial.md"
    partial_md.write_bytes(b"\xff\xfe# \x2d\x4e\x87\x65")

    gate = build_delivery_gate(
        work_dir=tmp_path,
        package=package,
        quality_report={"status": "passed", "metrics": {}},
        latex_report={"status": "submission_ready"},
        chinese_translation_report={
            "status": "completed_with_warnings",
            "failed_unit_ids": ["B0001"],
            "citation_marker_count_translation": 10,
            "citation_marker_count_source": 10,
            "partial_translated_path": str(partial_md),
        },
        chinese_latex_report={"status": "compiled_awaiting_metadata"},
        research_plan_publication_report={},
        require_review=True,
        require_chinese_review=True,
        require_research_plan=False,
    )

    partial_check = gate["checks"]["chinese_translation_partial"]
    assert partial_check["ok"] is False
    assert partial_check["degraded"] is False
    assert partial_check["status"] == "claimed_but_unreadable"
    # The decisive assertion: the rest of the gate still evaluated, so the
    # paid-for English side is not lost to an unrelated decoding problem.
    assert gate["english_deliverable"] is True


def test_translation_exception_still_builds_the_gate(tmp_path: Path) -> None:
    """Defect B: a raising translator must not discard the English PDF."""
    query_plan = tmp_path / "query.json"
    query_plan.write_text(json.dumps({"output": {}}), encoding="utf-8")
    (tmp_path / "kb.sqlite").write_bytes(b"")

    run_dir = tmp_path / "run"
    review = run_dir / "authoring" / "FINAL_REVIEW_EN.md"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("# Review\n\nA complete review draft.", encoding="utf-8")
    visual = run_dir / "visual" / "VISUAL_PLAN.json"
    visual.parent.mkdir(parents=True, exist_ok=True)
    visual.write_text(json.dumps({"placements": []}), encoding="utf-8")

    def fake_latex(**kwargs):
        out = Path(str(kwargs["output_dir"]))
        out.mkdir(parents=True, exist_ok=True)
        pdf = out / "article.pdf"
        _write_pdf(pdf)
        return {
            "status": "submission_ready",
            "artifacts": {"compiled_pdf": str(pdf)},
        }

    def raising_translator(**_kwargs):
        raise RuntimeError("simulated translation crash")

    original_latex = module.build_latex_publication
    original_translate = module.translate_review_package
    original_evaluate = module.evaluate_review_content
    module.build_latex_publication = fake_latex
    module.translate_review_package = raising_translator
    module.evaluate_review_content = lambda **_: {
        "status": "passed",
        "metrics": {},
    }
    try:
        harness = ReviewHarnessOrchestrator(
            ReviewHarnessConfig(
                query_plan_path=query_plan,
                base_kb_sqlite=tmp_path / "kb.sqlite",
                output_root=tmp_path,
                produce_latex_publication=True,
                produce_chinese_publication=True,
                produce_research_plan_publication=False,
            ),
            run_dir=run_dir,
        )
        harness._review_body_validation = lambda path: {"status": "passed"}
        harness._validated_review_path = lambda path: path
        result = harness._finish("completed", "packaging", review, visual)
    finally:
        module.build_latex_publication = original_latex
        module.translate_review_package = original_translate
        module.evaluate_review_content = original_evaluate

    package = json.loads(result.package_path.read_text(encoding="utf-8"))

    # Before the fix this raised UnboundLocalError, which _finish converted
    # into status="failed" with no delivery gate written at all.
    assert harness.state.get("terminal_error") is None
    assert "delivery_gate" in package
    assert result.status != "failed"
    assert package["chinese_translation_status"] == "failed"
    # The English deliverable survives a Chinese-side crash.
    assert package["delivery_gate"]["english_deliverable"] is True


def test_degraded_terminal_status_is_understood_by_consumers() -> None:
    """Defect C: the degraded terminal status must be in a consumer whitelist.

    ``completed_with_warnings`` was in none, so it mapped to CLI exit code 1
    and to r6 ``failed_or_incomplete`` -- reporting a degraded-but-real
    delivery as a hard failure.
    """
    source = Path(
        "optomind_research/runtime/review_harness_orchestrator.py"
    ).read_text(encoding="utf-8")
    marker = 'if gate_state != "degraded"'
    assert marker in source
    tail = source.split(marker, 1)[1][:200]
    assert "completed_with_warnings" not in tail
    assert "awaiting_human_review" in tail
    # The status the orchestrator now emits is one r6 already classifies as a
    # candidate state rather than a failure.
    assert "awaiting_human_review" in NONTERMINAL_STATUSES
