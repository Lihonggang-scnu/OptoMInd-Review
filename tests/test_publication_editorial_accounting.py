"""Regression tests for assembly-backed editorial closure accounting."""

from __future__ import annotations

from types import SimpleNamespace

from optomind_research.runtime.publication_mainline_adapter import (
    _editorial_accounting,
)


def test_equal_word_rewrite_counts_as_applied_when_assembly_records_it() -> None:
    state = SimpleNamespace(
        stages={
            "editorial_revision": SimpleNamespace(
                payload={
                    "audit": {
                        "work_item_count": 1,
                        "blocking_unresolved": [],
                        "applied_revision_ids": ["ER-001"],
                        "unapplied_accepted_revision_ids": [],
                        "records": [
                            {
                                "work_item_id": "ER-001",
                                "status": "accepted",
                                "application_status": "applied",
                                "original_text": "old phrase remains one two",
                                "revised_text": "new phrase remains one two",
                            }
                        ],
                    }
                }
            )
        }
    )

    report = _editorial_accounting(state)

    assert report["accepted_applied_count"] == 1
    assert report["accepted_count"] == 1
    assert report["closure_completed"] is True


def test_unapplied_accepted_revision_does_not_close_editorial_stage() -> None:
    state = SimpleNamespace(
        stages={
            "editorial_revision": SimpleNamespace(
                payload={
                    "audit": {
                        "work_item_count": 1,
                        "blocking_unresolved": [],
                        "applied_revision_ids": [],
                        "unapplied_accepted_revision_ids": ["ER-001"],
                        "records": [
                            {
                                "work_item_id": "ER-001",
                                "status": "accepted",
                                "application_status": "unapplied",
                                "original_text": "same length text",
                                "revised_text": "other length text",
                            }
                        ],
                    }
                }
            )
        }
    )

    report = _editorial_accounting(state)

    assert report["accepted_applied_count"] == 0
    assert report["unapplied_accepted_count"] == 1
    assert report["closure_completed"] is False
