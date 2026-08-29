from __future__ import annotations

from optomind_research.runtime.research_plan_quality import (
    audit_plan_quality,
    build_source_terminology_ledger,
    normalize_plan_quality,
    normalize_quantitative_program_text,
)


def test_source_ledger_recovers_canonical_optics_terms():
    ledger = build_source_terminology_ledger(
        "Gires-Tournois interferometer (GTI); group delay dispersion (GDD); "
        "laser-induced damage threshold (LIDT).",
        "The review uses Gires-Tournois interferometers and GTI mirrors.",
        {},
    )
    canonical = ledger["canonical_expansions"]
    assert canonical["GTI"] == "Gires-Tournois interferometer"
    assert canonical["GDD"] == "group delay dispersion"
    assert canonical["LIDT"] == "laser-induced damage threshold"


def test_source_ledger_does_not_invent_acronym_expansions_from_nearby_words():
    ledger = build_source_terminology_ledger(
        (
            "Adaptive optics (AO) corrects aberrations. The absence of a "
            "guide star and assessment of output quality are separate ideas."
        ),
        "AO is reused without another definition.",
        {},
    )
    entry = next(item for item in ledger["entries"] if item["acronym"] == "AO")
    assert entry["status"] == "unambiguous"
    assert entry["canonical_expansion"] == "Adaptive optics"
    assert entry["candidates"] == [
        {
            "expansion": "Adaptive optics",
            "source_kinds": ["blueprint"],
        }
    ]


def test_source_context_is_not_rewritten_or_rejected_as_generated_plan_prose():
    ledger = build_source_terminology_ledger(
        "Adaptive optics (AO) is the canonical term.",
        "",
        {},
    )
    plan = {
        "source_context": {
            "raw_question": "An intentionally ambiguous phrase (AO)."
        },
        "paper_abstract": "Adaptive optics (AO) is evaluated.",
    }
    normalized, audits, errors = normalize_plan_quality(plan, ledger)
    assert errors == []
    assert normalized["source_context"] == plan["source_context"]
    assert audits == []


def test_unambiguous_wrong_expansion_is_repaired_and_audited():
    ledger = build_source_terminology_ledger(
        "Gires-Tournois interferometer (GTI).", "", {}
    )
    plan = {
        "paper_abstract": (
            "This plan studies Group Delay Dispersion compensated "
            "Grating-Tuned Interferometer (GTI)."
        )
    }
    normalized, audits, errors = normalize_plan_quality(plan, ledger)
    assert not errors
    assert "Grating-Tuned" not in normalized["paper_abstract"]
    assert "Gires-Tournois interferometer (GTI)" in normalized["paper_abstract"]
    assert any(item["acronym"] == "GTI" for item in audits)


def test_wrong_expansion_is_detected_without_correction():
    ledger = build_source_terminology_ledger(
        "Gires-Tournois interferometer (GTI).", "", {}
    )
    errors = audit_plan_quality(
        {"paper_abstract": "A Grating-Tuned Interferometer (GTI) is used."},
        ledger,
    )
    assert any("incompatible_acronym_expansion" in item for item in errors)


def test_ambiguous_source_expansion_fails_closed():
    ledger = build_source_terminology_ledger(
        "Alpha Beta (AB). Alternative Basis (AB).", "", {}
    )
    normalized, audits, errors = normalize_plan_quality(
        {"paper_abstract": "Alternative Basis (AB) is considered."},
        ledger,
    )
    assert ledger["entries"][0]["status"] == "ambiguous"
    assert any("ambiguous_acronym_expansion" in item for item in errors)
    assert normalized["paper_abstract"] == "Alternative Basis (AB) is considered."
    assert audits == []


def test_unreferenced_quantitative_scope_and_distribution_are_deferred():
    text = (
        "The proposed study covers a wavelength range of 800-900 nm with "
        "sigma=0.5-2% nominal."
    )
    normalized = normalize_quantitative_program_text(text)
    assert "Proposed program scope" in normalized
    assert "Proposed calibration distribution" in normalized
    assert "verification_deferred" in normalized


def test_cited_quantitative_fact_is_not_relabelled():
    text = "The paper reported operation at 800-900 nm [REF:paper-1]."
    assert normalize_quantitative_program_text(text) == text


def _metric_token_plan(metrics, text):
    return {
        "unified_evaluation": {"metrics": metrics},
        "objectives": [text],
        "work_packages": [
            {
                "work_package_id": "WP01",
                "metric_ids": [row["metric_id"] for row in metrics],
                "evaluation_metrics": [text],
            }
        ],
    }


def test_unique_metric_token_near_miss_is_repaired_and_audited():
    plan = _metric_token_plan(
        [
            {
                "metric_id": "M01",
                "name": "Manufacturing-Tolerant Figure of Merit (MT-FOM)",
            }
        ],
        "Compare MT-FIM improvements against the baseline.",
    )
    normalized, audits, errors = normalize_plan_quality(plan)
    assert not errors
    assert normalized["objectives"] == [
        "Compare MT-FOM improvements against the baseline."
    ]
    assert normalized["work_packages"][0]["evaluation_metrics"] == [
        "Compare MT-FOM improvements against the baseline."
    ]
    replacements = [
        item
        for item in audits
        if item["action"] == "replace_noncanonical_canonical_token"
    ]
    assert len(replacements) == 2
    assert all(item["previous_token"] == "MT-FIM" for item in replacements)
    assert all(item["canonical_token"] == "MT-FOM" for item in replacements)


def test_ambiguous_metric_token_near_miss_is_left_unchanged():
    plan = _metric_token_plan(
        [
            {"metric_id": "M01", "name": "Metric One (MT-FOM)"},
            {"metric_id": "M02", "name": "Metric Two (MT-FXN)"},
        ],
        "Compare MT-FXM improvements against the baseline.",
    )
    normalized, audits, errors = normalize_plan_quality(plan)
    assert normalized["objectives"] == [
        "Compare MT-FXM improvements against the baseline."
    ]
    assert audits == []
    assert any("ambiguous_canonical_token" in item for item in errors)


def test_metric_token_normalization_is_idempotent():
    plan = _metric_token_plan(
        [
            {
                "metric_id": "M01",
                "name": "Manufacturing-Tolerant Figure of Merit (MT-FOM)",
            }
        ],
        "Compare MT-FIM improvements against the baseline.",
    )
    first, first_audits, first_errors = normalize_plan_quality(plan)
    second, second_audits, second_errors = normalize_plan_quality(first)
    assert first_errors == []
    assert first_audits
    assert second == first
    assert second_audits == []
    assert second_errors == []
