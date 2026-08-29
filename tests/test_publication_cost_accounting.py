"""Focused regression tests for publication-mainline cost accounting."""

from __future__ import annotations

import pytest

from optomind_research.runtime.cost_ledger import estimate_call_cost_cny
from optomind_research.runtime.publication_mainline_adapter import (
    _estimate_usage_cost_cny,
)


def test_detailed_calls_are_priced_before_aggregate_tokens() -> None:
    calls = [
        {
            "model_name": "qwen3.7-flash",
            "input_tokens": 20_000,
            "output_tokens": 1_000,
        },
        {
            "model_name": "qwen3.7-flash",
            "input_tokens": 20_000,
            "output_tokens": 1_000,
        },
    ]
    usage = {
        "total_input_tokens": 40_000,
        "total_output_tokens": 2_000,
        "calls": calls,
    }

    cost, priced = _estimate_usage_cost_cny(
        usage,
        author_tier="c_model",
        reviewer_tier="c2_model",
    )
    expected = sum(
        estimate_call_cost_cny(
            call["model_name"],
            call["input_tokens"],
            call["output_tokens"],
        )
        for call in calls
    )
    aggregated = estimate_call_cost_cny(
        "qwen3.7-flash",
        usage["total_input_tokens"],
        usage["total_output_tokens"],
    )

    assert priced is True
    assert cost == pytest.approx(expected, abs=1e-9)
    assert cost < aggregated


def test_editorial_records_keep_author_and_verifier_model_rates() -> None:
    record = {
        "author": {
            "model_name": "qwen3.5-plus",
            "input_tokens": 1_500,
            "output_tokens": 6_700,
        },
        "verifier": {
            "model_name": "qwen3.7-flash",
            "input_tokens": 1_000,
            "output_tokens": 1_600,
        },
    }
    usage = {
        "total_input_tokens": 2_500,
        "total_output_tokens": 8_300,
        "author": {"input_tokens": 1_500, "output_tokens": 6_700},
        "verifier": {"input_tokens": 1_000, "output_tokens": 1_600},
        "records": {"ER-001": record},
    }

    cost, priced = _estimate_usage_cost_cny(
        usage,
        author_tier="c_model",
        reviewer_tier="c2_model",
    )
    expected = estimate_call_cost_cny(
        "qwen3.5-plus", 1_500, 6_700
    ) + estimate_call_cost_cny("qwen3.7-flash", 1_000, 1_600)

    assert priced is True
    assert cost == pytest.approx(expected, abs=1e-9)
