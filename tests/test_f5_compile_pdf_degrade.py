"""F5 gate G4/G5: compile_pdf degrade + strict fast-fail without latexmk."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_latex_publication_renderer import _build_fixture  # noqa: E402

from optomind_research.runtime.latex_publication_renderer import (
    build_latex_publication,
)


@pytest.fixture()
def no_latexmk(monkeypatch: pytest.MonkeyPatch):
    """Simulate a machine without the LaTeX toolchain."""

    module = __import__(
        "optomind_research.runtime.latex_publication_renderer",
        fromlist=["_find_binary"],
    )

    import shutil

    original = module._find_binary

    def fake_find_binary(name: str):
        if name in {"latexmk", "xelatex", "pdflatex"}:
            return ""  # simulated absent toolchain
        return original(name)  # pandoc etc. resolve for real

    monkeypatch.setattr(module, "_find_binary", fake_find_binary)
    return module


def test_missing_latexmk_degrades_without_raising(
    tmp_path: Path, no_latexmk
) -> None:
    package_path, metadata_path = _build_fixture(tmp_path)
    report = build_latex_publication(
        content_package_path=package_path,
        output_dir=tmp_path / "latex",
        metadata_path=metadata_path,
        enrich_crossref=False,
        compile_pdf=True,
        render_previews=False,
    )
    # Gate G4 assertions (quoted verbatim into the work log):
    assert report["latex"]["pdf_skipped_reason"] == "latexmk_not_found"
    assert report["latex"]["compiled"] is False
    assert report["latex"]["message"] == "compile_pdf is False; no PDF was produced"
    assert report["status"] != "failed"
    assert Path(report["artifacts"]["main_tex"]).is_file()


def test_require_pdf_fast_fails_with_readable_error(
    tmp_path: Path, no_latexmk
) -> None:
    package_path, metadata_path = _build_fixture(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        build_latex_publication(
            content_package_path=package_path,
            output_dir=tmp_path / "latex",
            metadata_path=metadata_path,
            enrich_crossref=False,
            compile_pdf=True,
            pdf_strict=True,
            render_previews=False,
        )
    message = str(excinfo.value)
    assert "--require-pdf" in message and "latexmk" in message
