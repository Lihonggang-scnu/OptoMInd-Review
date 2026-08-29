"""Tests for delivery gate three-state logic (P0-2).

These tests verify that the delivery gate correctly classifies checks into
blocking / degraded / awaiting_human buckets and that english_deliverable
correctly indicates when the English side is fully deliverable.
"""

import json
import tempfile
from pathlib import Path

import pytest

from optomind_research.runtime.delivery_contract import (
    build_delivery_gate,
    validate_pdf,
)


def _write_pdf(path: Path) -> None:
    from pypdf import PdfWriter
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)


def _make_base_package(tmp_path: Path) -> dict:
    """Create a minimal package dict with valid English artifacts."""
    review_pdf = tmp_path / "review.pdf"
    _write_pdf(review_pdf)
    return {
        "schema_version": "research_harness.content_package.v1",
        "run_id": "test",
        "status": "completed",
        "completed_stage": "packaging",
        "query_plan_path": str(tmp_path / "query.json"),
        "base_kb_sqlite": str(tmp_path / "kb.sqlite"),
        "topic_identity_path": str(tmp_path / "TOPIC_IDENTITY.json"),
        "topic_fingerprint": "",
        "final_review_path": str(tmp_path / "review.md"),
        "metadata_catalog_path": "",
        "metadata_audit_path": "",
        "review_body_validation": {"status": "passed"},
        "visual_editorial_plan_path": str(tmp_path / "visual.json"),
        "final_visual_package_path": "",
        "research_plan_path": "",
        "cost_cny": 0.0,
        "total_cost_cny": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "stage_status": {},
        "quality_summary": {},
        "quality_gate": {"status": "passed", "metrics": {}},
        "artifacts": {
            "latex_pdf": str(review_pdf),
        },
        "latex_pdf_path": str(review_pdf),
        "publication_metadata_path": "",
    }


class TestDeliveryGateTristate:
    """Tests for the three-state delivery gate logic."""

    def test_all_green_passes(self, tmp_path: Path):
        """All checks green -> status=passed, passed=True, all buckets empty."""
        package = _make_base_package(tmp_path)
        
        gate = build_delivery_gate(
            work_dir=tmp_path,
            package=package,
            quality_report={"status": "passed", "metrics": {}},
            latex_report={"status": "submission_ready"},
            chinese_translation_report={},
            chinese_latex_report={},
            research_plan_publication_report={},
            require_review=True,
            require_chinese_review=False,
            require_research_plan=False,
        )

        assert gate["schema_version"] == "research_harness.delivery_gate.v2"
        assert gate["status"] == "passed"
        assert gate["passed"] is True
        assert gate["blocking_checks"] == []
        assert gate["degraded_checks"] == []
        assert gate["awaiting_human_checks"] == []
        assert gate["english_deliverable"] is True

    def test_degraded_chinese_keeps_english_deliverable(self, tmp_path: Path):
        """Chinese degraded (completed_with_warnings) + English all green
        -> status=degraded, degraded_checks has chinese_translation_audit,
        blocking_checks empty, english_deliverable=True."""
        package = _make_base_package(tmp_path)
        
        # Add Chinese artifacts (partial translation)
        zh_pdf = tmp_path / "review_zh.pdf"
        _write_pdf(zh_pdf)
        package["chinese_latex_pdf_path"] = str(zh_pdf)
        package["artifacts"]["chinese_latex_pdf"] = str(zh_pdf)

        # The claimed Markdown partial must really exist on disk and be
        # readable, otherwise the partial check correctly refuses to treat
        # it as degraded evidence of paid-for work.
        partial_md = tmp_path / "FINAL_REVIEW_ZH.partial.md"
        partial_md.write_text(
            "# 中文译稿（部分）\n\n这是已验证单元的中文译文，含 [REF:paper_a]。",
            encoding="utf-8",
        )

        gate = build_delivery_gate(
            work_dir=tmp_path,
            package=package,
            quality_report={"status": "passed", "metrics": {}},
            latex_report={"status": "submission_ready"},
            chinese_translation_report={
                "status": "completed_with_warnings",
                "failed_unit_ids": ["B0001", "B0002"],
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

        assert gate["status"] == "degraded"
        assert gate["passed"] is False  # strict: degraded means not fully passed
        assert "chinese_translation_audit" in gate["degraded_checks"]
        assert "chinese_translation_partial" in gate["degraded_checks"]
        assert gate["blocking_checks"] == []
        assert gate["english_deliverable"] is True

    def test_awaiting_human_research_plan_not_blocking(self, tmp_path: Path):
        """Research plan awaiting_human -> check goes to awaiting_human_checks,
        NOT blocking_checks."""
        package = _make_base_package(tmp_path)
        
        plan_pdf = tmp_path / "plan.pdf"
        _write_pdf(plan_pdf)
        package["research_plan_latex_pdf_path"] = str(plan_pdf)
        package["artifacts"]["research_plan_latex_pdf"] = str(plan_pdf)
        
        # Create research plan audit file with awaiting_human status
        audit_dir = tmp_path / "research_program"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_path = audit_dir / "RESEARCH_PLAN_AUDIT.json"
        audit_path.write_text(json.dumps({"status": "waiting_for_human"}), encoding="utf-8")
        package["research_plan_audit_path"] = str(audit_path)
        
        gate = build_delivery_gate(
            work_dir=tmp_path,
            package=package,
            quality_report={"status": "passed", "metrics": {}},
            latex_report={"status": "submission_ready"},
            chinese_translation_report={},
            chinese_latex_report={},
            research_plan_publication_report={
                "status": "waiting_for_human",
            },
            require_review=True,
            require_chinese_review=False,
            require_research_plan=True,
        )

        assert "research_plan_audit" in gate["awaiting_human_checks"]
        assert "research_plan_publication_audit" in gate["awaiting_human_checks"]
        assert "research_plan_audit" not in gate["blocking_checks"]
        assert "research_plan_publication_audit" not in gate["blocking_checks"]
        # English deliverable should still be True
        assert gate["english_deliverable"] is True

    def test_missing_english_pdf_still_blocks(self, tmp_path: Path):
        """Missing English PDF -> must go to blocking_checks, status=failed."""
        package = _make_base_package(tmp_path)
        # Remove the English PDF
        package["artifacts"] = {}
        package["latex_pdf_path"] = ""
        
        gate = build_delivery_gate(
            work_dir=tmp_path,
            package=package,
            quality_report={"status": "passed", "metrics": {}},
            latex_report={"status": "submission_ready"},
            chinese_translation_report={},
            chinese_latex_report={},
            research_plan_publication_report={},
            require_review=True,
            require_chinese_review=False,
            require_research_plan=False,
        )

        assert gate["status"] == "failed"
        assert "english_review_pdf" in gate["blocking_checks"]
        assert gate["english_deliverable"] is False

    def test_partial_md_is_never_validated_as_pdf(self, tmp_path: Path):
        """Guard (acceptance finding 1): a Markdown partial must never be fed
        into validate_pdf().  With no Chinese PDF on disk the PDF check blocks
        for the honest reason (missing artifact) while the readable partial
        still counts as degraded evidence."""
        package = _make_base_package(tmp_path)
        partial_md = tmp_path / "FINAL_REVIEW_ZH.partial.md"
        partial_md.write_text(
            "# 中文译稿（部分）\n\n已验证单元的译文，含 [REF:paper_a]。",
            encoding="utf-8",
        )

        gate = build_delivery_gate(
            work_dir=tmp_path,
            package=package,
            quality_report={"status": "passed", "metrics": {}},
            latex_report={"status": "submission_ready"},
            chinese_translation_report={
                "status": "completed_with_warnings",
                "failed_unit_ids": ["B0001"],
                "citation_marker_count_translation": 5,
                "citation_marker_count_source": 5,
                "partial_translated_path": str(partial_md),
            },
            chinese_latex_report={"status": "disabled_translation_failed"},
            research_plan_publication_report={},
            require_review=True,
            require_chinese_review=True,
            require_research_plan=False,
        )

        pdf_check = gate["checks"]["chinese_review_pdf"]
        assert pdf_check["ok"] is False
        assert pdf_check["reason"] == "missing_path"  # not invalid_pdf_header
        assert "chinese_review_pdf" in gate["blocking_checks"]
        # The collateral latex-disable is degraded, not an extra blocking item.
        assert "chinese_latex_audit" in gate["degraded_checks"]
        assert "chinese_translation_partial" in gate["degraded_checks"]

    def test_claimed_but_unreadable_partial_blocks(self, tmp_path: Path):
        """A claimed partial that is not on disk must block instead of
        masquerading as degraded evidence."""
        package = _make_base_package(tmp_path)
        zh_pdf = tmp_path / "review_zh.pdf"
        _write_pdf(zh_pdf)
        package["chinese_latex_pdf_path"] = str(zh_pdf)

        gate = build_delivery_gate(
            work_dir=tmp_path,
            package=package,
            quality_report={"status": "passed", "metrics": {}},
            latex_report={"status": "submission_ready"},
            chinese_translation_report={
                "status": "completed_with_warnings",
                "failed_unit_ids": ["B0001"],
                "citation_marker_count_translation": 5,
                "citation_marker_count_source": 5,
                "partial_translated_path": str(tmp_path / "ghost.partial.md"),
            },
            chinese_latex_report={"status": "completed"},
            research_plan_publication_report={},
            require_review=True,
            require_chinese_review=True,
            require_research_plan=False,
        )

        assert "chinese_translation_partial" in gate["blocking_checks"]
        assert gate["status"] == "failed"

    def test_no_requirements_is_not_an_english_deliverable(self, tmp_path: Path):
        """Guard (acceptance finding 5): with nothing required, nothing was
        produced — english_deliverable must be False, not vacuously True."""
        gate = build_delivery_gate(
            work_dir=tmp_path,
            package=_make_base_package(tmp_path),
            quality_report={},
            latex_report={},
            chinese_translation_report={},
            chinese_latex_report={},
            research_plan_publication_report={},
            require_review=False,
            require_chinese_review=False,
            require_research_plan=False,
        )

        assert gate["status"] == "passed"
        assert gate["passed"] is True
        assert gate["english_deliverable"] is False

    def test_corrupt_chinese_pdf_still_blocks(self, tmp_path: Path):
        """Corrupt Chinese PDF (0 bytes / bad header) -> blocking_checks,
        validate_pdf must NOT be relaxed by degraded logic."""
        package = _make_base_package(tmp_path)
        
        # Add a corrupt Chinese "PDF" (empty file)
        zh_pdf = tmp_path / "review_zh.pdf"
        zh_pdf.parent.mkdir(parents=True, exist_ok=True)
        zh_pdf.write_bytes(b"not a pdf")
        package["chinese_latex_pdf_path"] = str(zh_pdf)
        package["artifacts"]["chinese_latex_pdf"] = str(zh_pdf)
        
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
            },
            chinese_latex_report={"status": "compiled_awaiting_metadata"},
            research_plan_publication_report={},
            require_review=True,
            require_chinese_review=True,
            require_research_plan=False,
        )

        # The Chinese PDF is corrupt, so chinese_review_pdf should be blocking
        assert "chinese_review_pdf" in gate["blocking_checks"]
        # The translation audit is degraded, but the PDF corruption is blocking
        assert "chinese_translation_audit" in gate["degraded_checks"]
        assert gate["status"] == "failed"  # blocking exists -> failed


    def test_stage_status_alone_exempts_a_stopped_research_plan(
        self, tmp_path: Path
    ):
        """Regression (rhr_be780761): a research_plan that stops for a human
        writes no audit file at all, so the exemption cannot be keyed on the
        audit file's contents.

        On that run research_plan stopped at ``waiting_for_human``
        (initial_discovery_focus_not_completed_needs_more_literature), so
        ``research_program/RESEARCH_PLAN_AUDIT.json`` was never written and the
        publication step never ran.  Reading only the absent audit yielded
        ``missing`` and turned one correct human-decision stop into four
        blocking failures -- reporting status=failed for a run that had already
        produced a 41-page English and a 39-page Chinese PDF.  The stage's own
        status is the authoritative signal that the absence was a decision.
        """

        package = _make_base_package(tmp_path)
        # Exactly as on the real run: no audit file, no publication output, no
        # research-plan PDF -- only the stage status says why.
        package["research_plan_audit_path"] = ""
        package["stage_status"] = {
            "research_plan": {
                "status": "waiting_for_human",
                "stop_reason": (
                    "initial_discovery_focus_not_completed_needs_more_literature"
                ),
            },
        }
        assert not (tmp_path / "research_program").exists()

        gate = build_delivery_gate(
            work_dir=tmp_path,
            package=package,
            quality_report={"status": "passed", "metrics": {}},
            latex_report={"status": "submission_ready"},
            chinese_translation_report={},
            chinese_latex_report={},
            research_plan_publication_report={"status": "disabled"},
            require_review=True,
            require_chinese_review=False,
            require_research_plan=True,
        )

        # A human decision is not a delivery failure: degraded, never failed.
        assert gate["status"] == "degraded"
        assert gate["blocking_checks"] == []
        for plan_key in (
            "english_research_plan_pdf",
            "research_plan_publication_audit",
            "research_plan_audit",
        ):
            assert plan_key in gate["awaiting_human_checks"], plan_key
            assert plan_key not in gate["blocking_checks"], plan_key
        # The English PDF was delivered and must stay deliverable.
        assert gate["english_deliverable"] is True
        assert (
            gate["checks"]["research_plan_audit"]["awaiting_human_reason"]
            == "research_plan_waiting_for_human"
        )

    def test_stage_status_exemption_does_not_mask_a_real_plan_failure(
        self, tmp_path: Path
    ):
        """The exemption must key on awaiting-human statuses only.

        A research_plan that genuinely failed still has no audit file, so if the
        exemption keyed on "stage status present" it would swallow real
        failures.  This pins the boundary.
        """

        package = _make_base_package(tmp_path)
        package["research_plan_audit_path"] = ""
        package["stage_status"] = {"research_plan": {"status": "failed"}}

        gate = build_delivery_gate(
            work_dir=tmp_path,
            package=package,
            quality_report={"status": "passed", "metrics": {}},
            latex_report={"status": "submission_ready"},
            chinese_translation_report={},
            chinese_latex_report={},
            research_plan_publication_report={"status": "failed"},
            require_review=True,
            require_chinese_review=False,
            require_research_plan=True,
        )

        assert gate["status"] == "failed"
        assert "research_plan_audit" in gate["blocking_checks"]
        assert "research_plan_audit" not in gate["awaiting_human_checks"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])