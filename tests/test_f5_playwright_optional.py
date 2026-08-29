"""F5 gate G6: playwright unimportable must not affect the OA route."""

from __future__ import annotations

import importlib
import builtins
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optomind_research.literature_resource_builder import (
    LiteratureResourceBuilder,
)


@pytest.fixture()
def playwright_blocked(monkeypatch: pytest.MonkeyPatch):
    """Make playwright unimportable through every import pathway."""

    real_import = builtins.__import__
    real_import_module = importlib.import_module

    def guarded_import(name, *args, **kwargs):
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError("playwright is not installed (simulated)")
        return real_import(name, *args, **kwargs)

    def guarded_import_module(name, *args, **kwargs):
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError("playwright is not installed (simulated)")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(importlib, "import_module", guarded_import_module)
    for cached in [m for m in list(sys.modules) if m.startswith("playwright")]:
        monkeypatch.delitem(sys.modules, cached, raising=False)
    return None


def _make_builder(tmp_path: Path) -> LiteratureResourceBuilder:
    return LiteratureResourceBuilder(
        output_root=tmp_path / "out",
        fulltext_root=tmp_path / "fulltext",
        enable_institutional_access=True,  # branch ON: still must not crash
    )


def test_backend_reports_missing_playwright_as_failure_structure(
    tmp_path: Path, playwright_blocked
) -> None:
    builder = _make_builder(tmp_path)
    backend = builder.create_institution_backend()
    assert backend is not None  # lazy import of the backend module itself ok
    assert backend._playwright_available is False
    result = backend.fetch_url("https://example.org/paper.pdf")
    assert result.ok is False
    assert result.error == "playwright is not installed"


def test_oa_route_ignores_missing_playwright(
    tmp_path: Path, playwright_blocked
) -> None:
    builder = _make_builder(tmp_path)
    # The default OA pipeline never touches the institutional backend;
    # construction and diagnostics stay clean with playwright blocked.
    assert isinstance(builder.diagnostics, list)
