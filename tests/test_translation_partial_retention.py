"""Tests for translation partial retention (P0-1).

These tests verify that validated translation units are never deleted,
even when other units in the same batch fail.
"""

from pathlib import Path

import pytest

from optomind_research.runtime.scientific_chinese_translator import ScientificChineseTranslator


# Chinese text with enough CJK characters to pass validation
# Need at least ~8 CJK chars for short text, or prose_chars * 0.08
TRANSLATIONS_ALL_SUCCESS = {
    "B0001": "这是第一段的中文翻译内容，长度足够通过验证。",
    "B0002": "这是第二段的中文翻译内容，长度足够通过验证。",
    "B0003": "这是第三段的中文翻译内容，长度足够通过验证。",
    "B0004": "这是第四段的中文翻译内容，长度足够通过验证。",
}

TRANSLATIONS_PARTIAL_SUCCESS = {
    "B0001": "这是第一段的中文翻译内容，长度足够通过验证。",
    "B0003": "这是第三段的中文翻译内容，长度足够通过验证。",
}


def make_fake_call_batch(translations_map: dict[str, str]):
    """Create a fake _call_batch that returns translations."""
    def fake_call_batch(self, batch, *, tier):
        translations = {}
        for unit in batch:
            trans_text = translations_map.get(unit.unit_id, "")
            if not trans_text:
                translations[unit.unit_id] = ""  # Empty -> validation fails
            else:
                translations[unit.unit_id] = trans_text
        return translations, {}
    return fake_call_batch


class TestTranslationPartialRetention:
    """Test that partial translations are retained and never deleted."""

    def test_all_units_validated_writes_canonical_file(self, tmp_path: Path, monkeypatch):
        """All units succeed -> FINAL_REVIEW_ZH.md exists, no .partial.md."""
        # Create source with multiple paragraphs (each becomes a unit) - NO REF markers
        source = tmp_path / "review.md"
        source.write_text(
            "First paragraph for translation.\n\n"
            "Second paragraph for translation.\n\n"
            "Third paragraph for translation.\n\n"
            "Fourth paragraph for translation.",
            encoding="utf-8",
        )
        
        # All units get successful translations
        monkeypatch.setattr(
            ScientificChineseTranslator, 
            "_call_batch", 
            make_fake_call_batch(TRANSLATIONS_ALL_SUCCESS)
        )
        
        report = ScientificChineseTranslator(
            source_markdown_path=source,
            output_dir=tmp_path / "out",
            semantic_audit=False,
            cost_budget_cny=10.0,
        ).translate(allow_partial_output=False)

        # Verify canonical file exists
        canonical = tmp_path / "out" / "FINAL_REVIEW_ZH.md"
        assert canonical.exists(), "Canonical translation file should exist"
        assert canonical.stat().st_size > 0, "File should not be empty"

        # Verify no partial file
        partial = tmp_path / "out" / "FINAL_REVIEW_ZH.partial.md"
        assert not partial.exists(), "Partial file should not exist when all units validated"

        # Verify status
        assert report["status"] == "completed"
        assert report["translated_path"] == str(canonical)
        assert report["partial_translated_path"] == ""
        assert report["partial_output"] is False

    def test_failed_units_strict_mode_writes_partial_beside(self, tmp_path: Path, monkeypatch):
        """Some units fail, strict mode -> .partial.md exists with content."""
        source = tmp_path / "review.md"
        source.write_text(
            "First paragraph for translation.\n\n"
            "Second paragraph for translation.\n\n"
            "Third paragraph for translation.\n\n"
            "Fourth paragraph for translation.",
            encoding="utf-8",
        )
        
        # Second and fourth paragraphs fail (empty string -> validation fails)
        partial_translations = {
            "B0001": "这是第一段的中文翻译内容，长度足够通过验证。",
            "B0003": "这是第三段的中文翻译内容，长度足够通过验证。",
        }
        
        monkeypatch.setattr(
            ScientificChineseTranslator, 
            "_call_batch", 
            make_fake_call_batch(partial_translations)
        )
        
        report = ScientificChineseTranslator(
            source_markdown_path=source,
            output_dir=tmp_path / "out",
            semantic_audit=False,
            cost_budget_cny=10.0,
        ).translate(allow_partial_output=False)  # strict mode

        # In strict mode with failures, partial file should exist
        canonical = tmp_path / "out" / "FINAL_REVIEW_ZH.md"
        partial = tmp_path / "out" / "FINAL_REVIEW_ZH.partial.md"
        
        assert partial.exists(), "Partial file should exist in strict mode with failures"
        assert partial.stat().st_size > 0, "Partial file should not be empty"
        assert not canonical.exists(), "Canonical file should NOT exist in strict mode with failures"

        # Verify status
        assert report["status"] == "completed_with_warnings"
        assert report["partial_translated_path"] == str(partial)
        assert report["partial_output"] is True

    def test_existing_translation_survives_failed_rerun(self, tmp_path: Path, monkeypatch):
        """Pre-existing translation file survives a failed rerun (regression test)."""
        source = tmp_path / "review.md"
        source.write_text(
            "First paragraph for translation.\n\n"
            "Second paragraph for translation.",
            encoding="utf-8",
        )
        
        # First paragraph fails
        partial_translations = {
            "B0002": "这是第二段的中文翻译内容，长度足够通过验证。",
        }
        
        monkeypatch.setattr(
            ScientificChineseTranslator, 
            "_call_batch", 
            make_fake_call_batch(partial_translations)
        )
        
        # Pre-create a translation file (simulating previous successful run)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        canonical = out_dir / "FINAL_REVIEW_ZH.md"
        canonical.write_text("Pre-existing translation.", encoding="utf-8")
        
        report = ScientificChineseTranslator(
            source_markdown_path=source,
            output_dir=out_dir,
            semantic_audit=False,
            cost_budget_cny=10.0,
        ).translate(allow_partial_output=False)

        # File should still exist (not deleted by unlink)
        assert canonical.exists(), "Pre-existing file should survive failed rerun"

        # Partial file should exist
        partial = out_dir / "FINAL_REVIEW_ZH.partial.md"
        assert partial.exists(), "Partial file should be created on failed rerun"

        # Freshness contract (acceptance finding 7): the pre-existing file
        # belongs to an earlier run, so this run's report must NOT claim it
        # as its own output.
        assert report["translated_path"] == "", (
            "Stale canonical translation must not be reported as this run's output"
        )
        assert report["partial_translated_path"] == str(partial)

    def test_default_fail_open_writes_canonical_fresh(self, tmp_path: Path, monkeypatch):
        """Default CLI semantics (fail-open) with failures: the canonical
        filename is written fresh THIS run and claimed; no partial file."""
        source = tmp_path / "review.md"
        source.write_text(
            "First paragraph for translation.\n\n"
            "Second paragraph for translation.\n\n"
            "Third paragraph for translation.\n\n"
            "Fourth paragraph for translation.",
            encoding="utf-8",
        )
        partial_translations = {
            "B0001": "这是第一段的中文翻译内容，长度足够通过验证。",
            "B0003": "这是第三段的中文翻译内容，长度足够通过验证。",
        }
        monkeypatch.setattr(
            ScientificChineseTranslator,
            "_call_batch",
            make_fake_call_batch(partial_translations),
        )

        report = ScientificChineseTranslator(
            source_markdown_path=source,
            output_dir=tmp_path / "out",
            semantic_audit=False,
            cost_budget_cny=10.0,
        ).translate(allow_partial_output=True)

        canonical = tmp_path / "out" / "FINAL_REVIEW_ZH.md"
        partial = tmp_path / "out" / "FINAL_REVIEW_ZH.partial.md"
        assert canonical.exists() and canonical.stat().st_size > 0
        assert not partial.exists()
        assert report["status"] == "completed_with_warnings"
        assert report["translated_path"] == str(canonical)
        assert report["partial_translated_path"] == ""
        assert report["partial_output"] is True

    def test_stale_partial_not_claimed_after_full_success(self, tmp_path: Path, monkeypatch):
        """A .partial.md left over from an earlier failed attempt must not be
        claimed by a later fully successful run."""
        source = tmp_path / "review.md"
        source.write_text(
            "First paragraph for translation.\n\nSecond paragraph.",
            encoding="utf-8",
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        stale_partial = out_dir / "FINAL_REVIEW_ZH.partial.md"
        stale_partial.write_text("旧的降级译稿", encoding="utf-8")

        monkeypatch.setattr(
            ScientificChineseTranslator,
            "_call_batch",
            make_fake_call_batch(TRANSLATIONS_ALL_SUCCESS),
        )

        report = ScientificChineseTranslator(
            source_markdown_path=source,
            output_dir=out_dir,
            semantic_audit=False,
            cost_budget_cny=10.0,
        ).translate(allow_partial_output=False)

        assert report["status"] == "completed"
        assert report["translated_path"] == str(out_dir / "FINAL_REVIEW_ZH.md")
        # The stale partial still exists on disk (never deleted), but this
        # run did not write it and must not claim it.
        assert stale_partial.exists()
        assert report["partial_translated_path"] == ""
        assert report["partial_output"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])