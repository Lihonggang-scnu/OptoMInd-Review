from __future__ import annotations

import json

from optomind_research.runtime.llm_style_pipeline import load_protected_terms


def test_full_review_ledger_is_the_default_resolution_path(tmp_path):
    ledger = tmp_path / "authoring" / "full_review" / "TERMINOLOGY_LEDGER.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"terms": [{"canonical_term": "PDRC"}]}), encoding="utf-8")
    assert load_protected_terms(tmp_path) == ["PDRC"]


def test_missing_or_unreadable_mainline_ledger_fails_closed(tmp_path):
    assert load_protected_terms(tmp_path) == []
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    assert load_protected_terms(ledger_path=broken) == []
