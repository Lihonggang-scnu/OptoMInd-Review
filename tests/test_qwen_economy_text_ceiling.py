"""Focused tests for the run-scoped economy text-model ceiling."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from config.qwen_config import (
    ECONOMY_TEXT_CEILING_ENV,
    ECONOMY_TEXT_CEILING_MODEL,
    get_fallback_model_names,
    get_model_name,
    set_economy_text_ceiling_enabled,
)


@pytest.fixture(autouse=True)
def _clear_ceiling_env(monkeypatch):
    monkeypatch.delenv(ECONOMY_TEXT_CEILING_ENV, raising=False)
    yield
    os.environ.pop(ECONOMY_TEXT_CEILING_ENV, None)


def test_non_harness_default_preserves_premium_text_models():
    assert get_model_name("premium_model") == "qwen3.8-max"
    assert get_fallback_model_names("premium_model")[0] == (
        "qwen3.7-max-2026-06-08"
    )


def test_economy_ceiling_downgrades_premium_tier_and_direct_text_ids(
    monkeypatch,
):
    set_economy_text_ceiling_enabled(True)

    assert get_model_name("premium_model") == ECONOMY_TEXT_CEILING_MODEL
    assert get_model_name("qwen3.8-max") == ECONOMY_TEXT_CEILING_MODEL
    assert get_model_name("qwen3.7-max-2026-06-08") == (
        ECONOMY_TEXT_CEILING_MODEL
    )
    assert os.environ[ECONOMY_TEXT_CEILING_ENV] == "1"


def test_economy_ceiling_keeps_nonpremium_text_and_vision_image_models(
    monkeypatch,
):
    set_economy_text_ceiling_enabled(True)

    assert get_model_name("c_model") == "qwen3.5-plus"
    assert get_model_name("b_plus_model") == "qwen3.7-flash"
    assert get_model_name("b_model") == "qwen3.5-plus"
    assert get_model_name("vision_premium_model") == "qwen3.7-flash"
    assert get_model_name("vision_model") == "qwen3.6-plus"
    assert get_model_name("qwen-image-2.0-pro") == "qwen-image-2.0-pro"


def test_economy_ceiling_filters_premium_text_fallbacks(monkeypatch):
    set_economy_text_ceiling_enabled(True)

    assert get_fallback_model_names("premium_model") == [
        "qwen3.5-plus",
    ]
    assert "qwen3.7-max-2026-06-08" not in get_fallback_model_names(
        "premium_model"
    )
    assert "qwen3.8-max" not in get_fallback_model_names("premium_model")


def test_explicit_opt_out_restores_existing_behavior(monkeypatch):
    set_economy_text_ceiling_enabled(True)
    set_economy_text_ceiling_enabled(False)

    assert get_model_name("premium_model") == "qwen3.8-max"
    assert get_fallback_model_names("premium_model")[0] == (
        "qwen3.7-max-2026-06-08"
    )


def test_harness_source_policy_is_opt_in_premium_text(monkeypatch):
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "run_review_harness.py").read_text(
        encoding="utf-8"
    )

    assert "--allow-premium-text-models" in source
    assert (
        "set_economy_text_ceiling_enabled(not args.allow_premium_text_models)"
        in source
    )
