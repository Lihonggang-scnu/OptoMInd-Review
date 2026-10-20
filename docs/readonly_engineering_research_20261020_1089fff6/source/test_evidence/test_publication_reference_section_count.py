from optomind_research.runtime.upgrade3.review_delivery import _verify_reader_draft


def test_body_and_appendix_citations_are_not_bibliography_entries():
    text = "# Title\n\n[1] describes the device.\n\n[2] discusses its limits.\n\n## 参考文献\n[1] First paper.\n[2] Second paper.\n\n## 附录\n[1] is reused here.\n"
    result = _verify_reader_draft(text, expected_refs=2)
    assert result["all_pass"]
    assert result["numbered_reference_entries"] == 2


def test_body_citation_cannot_hide_a_missing_reference():
    text = "# Title\n[2] starts the paragraph.\n\n## 参考文献\n[1] First paper.\n"
    result = _verify_reader_draft(text, expected_refs=2)
    assert not result["all_pass"]
    assert "numbered_refs_mismatch:1!=2" in result["failures"]


def test_duplicate_bibliographies_still_fail_closed():
    result = _verify_reader_draft("# Title\n## 参考文献\n[1] A\n## 参考文献\n[2] B\n", expected_refs=2)
    assert not result["all_pass"]
    assert "reference_sections_not_unique:2" in result["failures"]
