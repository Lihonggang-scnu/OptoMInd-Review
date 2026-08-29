"""Focused tests for optional sklearn import and fallback duplication."""

from __future__ import annotations

import pytest

import optomind_research.runtime.global_review_auditor as auditor_module


def test_global_review_auditor_imports_without_sklearn_startup_requirement(
    monkeypatch,
):
    monkeypatch.setattr(auditor_module, "_SKLEARN_LOADER_CACHE", (None, None))

    auditor = auditor_module.GlobalReviewAuditor()
    flags = auditor._detect_duplicates(
        {
            "S01": (
                "The same near-verbatim sentence is repeated across sections "
                "to validate deterministic fallback duplicate detection."
            ),
            "S02": (
                "The same near-verbatim sentence is repeated across sections "
                "to validate deterministic fallback duplicate detection."
            ),
            "S03": (
                "A completely different scientific argument about material "
                "synthesis and optical characterization remains distinct."
            ),
        }
    )

    assert any(flag["type"] == "duplicate_content" for flag in flags)
    assert {"S01", "S02"} in {
        frozenset(flag["section_ids"]) for flag in flags
    }


def test_fallback_does_not_flag_obvious_nonduplicates(monkeypatch):
    monkeypatch.setattr(auditor_module, "_SKLEARN_LOADER_CACHE", (None, None))

    auditor = auditor_module.GlobalReviewAuditor()
    flags = auditor._detect_duplicates(
        {
            "S01": (
                "The section explains a fabrication method for optical "
                "metasurfaces using electron-beam lithography."
            ),
            "S02": (
                "This different section compares measured quality factors "
                "across several dielectric resonator platforms."
            ),
        }
    )

    assert flags == []


def test_optional_sklearn_path_still_available():
    loader = auditor_module._load_sklearn_duplicate_accelerator()
    if loader == (None, None):
        pytest.skip("sklearn optional accelerator is unavailable in this runtime")

    auditor = auditor_module.GlobalReviewAuditor()
    flags = auditor._detect_duplicates(
        {
            "S01": "The exact same long sentence appears in two sections.",
            "S02": "The exact same long sentence appears in two sections.",
        }
    )

    assert flags


def test_module_import_does_not_populate_sklearn_loader_cache(monkeypatch):
    monkeypatch.setattr(auditor_module, "_SKLEARN_LOADER_CACHE", None)

    assert auditor_module._SKLEARN_LOADER_CACHE is None
