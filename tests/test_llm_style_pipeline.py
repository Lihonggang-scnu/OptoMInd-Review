"""Mock-LLM tests for the style governance pipeline (P1-1 section 7.3).

No real model calls. The five cases pin the five hard constraints: fallback
on hard-verification failure, acceptance with measurable improvement,
byte-identical passthrough when disabled, budget exhaustion keeping accepted
rewrites, and the blocking_issues red line.
"""

from __future__ import annotations

import json

from optomind_research.runtime.llm_style_pipeline import (
    apply_deterministic_style_governance,
    load_protected_terms,
    run_style_pipeline,
)

P0 = (
    "While metalenses promise compact flat optics, most practical designs "
    "still suffer from narrow bandwidth that limits color imaging quality. "
    "[REF:s2:a1] Measured efficiency reaches approximately 80 percent at "
    "the design wavelength of 450 nm across the full aperture."
)

P1 = (
    "Building on coupled resonator arrays, several groups have pushed "
    "bandwidth beyond one octave, yet the underlying trade-off between "
    "efficiency and bandwidth remains severe. [REF:s3:b2] Recent "
    "demonstrations report nearly uniform response over visible frequencies."
)

DOCUMENT = P0 + "\n\n" + P1

P0_REWRITTEN = (
    "Metalens platforms promise compact flat optics, yet most practical "
    "designs still suffer from narrow bandwidth that limits color imaging "
    "quality. [REF:s2:a1] Measured efficiency reaches approximately 80 "
    "percent at the design wavelength of 450 nm across the full aperture."
)

CRITIC_ISSUES = [
    {"paragraph_index": 0, "issue_type": "template_opener",
     "severity": "high", "evidence": "While",
     "suggestion": "rewrite around the physical object"},
    {"paragraph_index": 1, "issue_type": "template_opener",
     "severity": "medium", "evidence": "Building",
     "suggestion": "rewrite around the physical object"},
]

_CHEAP_USAGE = {"model_name": "", "input_tokens": 40, "output_tokens": 10}
_HEAVY_USAGE = {
    "model_name": "", "input_tokens": 200000, "output_tokens": 20000,
}


class _ScriptedLLM:
    """Deterministic stand-in for call_qwen_chat; no network, no cost."""

    def __init__(self, rewrites):
        self._rewrites = list(rewrites)
        self.rewrite_calls = []

    def __call__(self, agent_name, system, payload, *, json_mode):
        if agent_name == "StyleCritic":
            return {
                "content": json.dumps({"issues": CRITIC_ISSUES}),
                "_llm_usage": dict(_CHEAP_USAGE),
            }
        if agent_name == "StyleRewriter":
            self.rewrite_calls.append(payload["paragraph"])
            usage = dict(_HEAVY_USAGE) if self._rewrites else dict(_CHEAP_USAGE)
            return {
                "content": self._rewrites.pop(0) if self._rewrites else "",
                "_llm_usage": usage,
            }
        raise AssertionError("unexpected agent " + str(agent_name))


def test_broken_rewrite_falls_back_and_records_violations():
    broken = P0_REWRITTEN.replace(" [REF:s2:a1]", "")
    llm = _ScriptedLLM([broken])
    report = run_style_pipeline(DOCUMENT, enabled=True, llm_call=llm)
    assert report["rewrites_accepted"] == 0
    assert report["rejections"][0]["paragraph_index"] == 0
    assert any(
        v["check"] == "citations"
        for v in report["rejections"][0]["violations"]
    )
    assert report["review_text"] == DOCUMENT


def test_legal_rewrite_is_accepted_and_changes_openers():
    llm = _ScriptedLLM([P0_REWRITTEN])
    report = run_style_pipeline(
        DOCUMENT, enabled=True, max_rewrites=1, llm_call=llm,
    )
    assert report["rewrites_accepted"] == 1
    before = report["opener_distribution_before"]
    after = report["opener_distribution_after"]
    assert before.get("While") == 1
    assert after.get("While") is None
    assert after != before
    assert "[REF:s2:a1]" in report["review_text"]
    assert "450 nm" in report["review_text"]


def test_disabled_pipeline_keeps_text_byte_identical():
    llm = _ScriptedLLM([P0_REWRITTEN])
    report = run_style_pipeline(DOCUMENT, enabled=False, llm_call=llm)
    assert report["review_text"] == DOCUMENT
    assert report["review_text"] is DOCUMENT
    assert report["rewrites_attempted"] == 0
    assert report["estimated_cost_cny"] == 0.0


def test_legacy_terms_mapping_is_loaded_as_protected_vocabulary(tmp_path):
    ledger = tmp_path / "TERMINOLOGY_LEDGER.json"
    ledger.write_text(
        json.dumps(
            {
                "terms": {
                    "ODNN": "acronym (S05)",
                    "Von Neumann": "technical phrase (S01)",
                }
            }
        ),
        encoding="utf-8",
    )

    assert load_protected_terms(ledger_path=ledger) == ["ODNN", "Von Neumann"]


def test_budget_exhaustion_stops_but_keeps_accepted():
    # Each rewrite costs about CNY 3.48 under conservative fallback rates;
    # a CNY 1 budget accepts paragraph one, then stops before paragraph two.
    llm = _ScriptedLLM([P0_REWRITTEN, P0_REWRITTEN])
    report = run_style_pipeline(
        DOCUMENT, enabled=True, cost_budget_cny=1.0, llm_call=llm,
    )
    assert report["budget_exhausted"] is True
    assert report["rewrites_accepted"] == 1
    assert len(llm.rewrite_calls) == 1
    assert report["review_text"].startswith("Metalens platforms")
    assert P1 in report["review_text"]


def test_style_never_enters_blocking_issues():
    llm = _ScriptedLLM([""])
    report = run_style_pipeline(DOCUMENT, enabled=True, llm_call=llm)
    assert report["issues_found"] >= 2
    assert report["blocking_issues"] == []
    assert set(report["warnings"]) <= {
        "template_opener", "abstract_subject", "repetitive_summary",
    }


def test_deterministic_publication_guard_closes_mechanical_style_defects():
    text = (
        "While phase-only modulation improves manufacturability, the design "
        "is not merely a passive analog computer but a constrained learning "
        "system. This approach preserves measured performance at 450 nm "
        "under the stated test conditions [REF:s2:a1] and supports stable "
        "inference across the evaluated optical path."
        "\n\n"
        "While multiplexing expands the available channels, the field is "
        "not merely a collection of isolated demonstrations but a growing "
        "engineering discipline. Researchers have reported practical "
        "implementations, and this approach still requires calibration "
        "before deployment across multiple tasks [REF:s2:b2]."
        "\n\n"
        "Building on coupled resonator arrays, several groups have pushed "
        "bandwidth beyond one octave, yet the underlying trade-off between "
        "efficiency and bandwidth remains severe. [REF:s3:b2] Recent "
        "demonstrations report nearly uniform response over visible "
        "frequencies."
    )
    report = apply_deterministic_style_governance(text, protected_terms=[])
    assert report["changed"] is True
    assert report["rewrites_rejected"] == 0
    assert report["strawman_not_but_before"] == 2
    assert report["strawman_not_but_after"] == 0
    assert report["metrics_after"]["while_paragraphs"] == 0
    assert report["metrics_after"]["building_on_paragraphs"] == 0
    assert report["rewrite_kind_counts"]["building_on_opener"] == 1
    assert report["metrics_after"]["paragraph_opener_max_share"] < report[
        "metrics_before"
    ]["paragraph_opener_max_share"]
    assert sum(report["abstract_subject_hits_after"].values()) < sum(
        report["abstract_subject_hits_before"].values()
    )
    assert "[REF:s2:a1]" in report["review_text"]
    assert "450 nm" in report["review_text"]
