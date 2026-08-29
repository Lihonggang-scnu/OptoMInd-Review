"""F5 gates G7/G8/G9: preflight doctor correctness and key safety."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import optomind_ui.preflight as preflight
from optomind_ui.preflight import _blocking_failures, check_all


def test_real_machine_all_blocking_ok() -> None:
    """G7 first half: this dev box has TeX Live; nothing blocking may fail."""
    results = check_all()
    failures = [item.key for item in _blocking_failures(results)]
    assert failures == []


def test_missing_latexmk_is_degraded_but_not_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_which = __import__("shutil").which

    def fake_which(name):
        return "" if name in {"latexmk", "xelatex"} else real_which(name)

    monkeypatch.setattr(preflight.shutil, "which", fake_which)
    results = check_all()
    latex = next(item for item in results if item.key == "latex")
    assert latex.status == "degraded"
    assert "跳过 PDF 编译" in latex.detail
    # non-blocking absence must keep the doctor exit code at 0
    assert _blocking_failures(results) == []


def test_missing_api_key_is_blocking(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(preflight, "PROJECT_ROOT", tmp_path)
    results = check_all()
    failures = [item.key for item in _blocking_failures(results)]
    assert "api_key" in failures
