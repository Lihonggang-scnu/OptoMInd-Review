"""Focused tests for the stdlib-only publication deployment preflight."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

import scripts.publication_deployment_preflight as preflight


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory under the repository."""

    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-publication-deployment-preflight"
    )
    root.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = root / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


@pytest.fixture
def available_runtime(monkeypatch):
    monkeypatch.setattr(
        preflight.shutil,
        "which",
        lambda name: f"{name}.exe",
    )
    monkeypatch.setattr(
        preflight,
        "_module_import_status",
        lambda name: (True, ""),
    )


def _write_package(
    tmp_path: Path,
    *,
    overrides: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "FINAL_REVIEW_EN.md").write_text(
        "# Internal review\n\nBody text.\n",
        encoding="utf-8",
    )
    (package_dir / "FINAL_VISUAL_PACKAGE.json").write_text(
        json.dumps({"figures": []}),
        encoding="utf-8",
    )
    (package_dir / "publication_metadata.json").write_text(
        json.dumps(
            {
                "title": "Test publication",
                "draft_only": True,
                "publication_eligible": False,
                **(metadata or {}),
            }
        ),
        encoding="utf-8",
    )
    content = {
        "schema_version": preflight.PACKAGE_SCHEMA,
        "run_id": "preflight-test",
        "status": "internal_study_draft",
        "source_run_dir": ".",
        "final_review_path": "FINAL_REVIEW_EN.md",
        "final_visual_package_path": "FINAL_VISUAL_PACKAGE.json",
        "publication_metadata_path": "publication_metadata.json",
        "base_kb_sqlite": "",
        "publication_eligible": False,
        "artifacts": {
            "publication_metadata": "publication_metadata.json",
        },
        "publication_policy": {
            "publication_eligible": False,
            "reason": "internal study",
        },
    }
    if overrides:
        content.update(overrides)
    package_path = package_dir / "REVIEW_CONTENT_PACKAGE.json"
    package_path.write_text(json.dumps(content), encoding="utf-8")
    return package_path


def test_valid_package_is_ready_and_reports_draft_policy_warning(
    tmp_path: Path,
    available_runtime,
) -> None:
    package_path = _write_package(tmp_path)

    report = preflight.run_preflight(package_path)

    assert report["status"] == "ready"
    assert report["ready"] is True
    assert report["blockers"] == []
    assert report["python"]["ready"] is True
    assert report["tools"]["pandoc"]["available"] is True
    assert report["package"]["required_local_inputs"] == {
        "source_run_dir": "present",
        "final_review_path": "present",
        "final_visual_package_path": "present",
        "publication_metadata_path": "present",
    }
    assert any(
        "draft_only" in warning
        for warning in report["package"]["policy_warnings"]
    )
    assert any(
        "publication_eligible" in warning
        for warning in report["package"]["policy_warnings"]
    )


def test_absolute_package_path_is_blocked(
    tmp_path: Path,
    available_runtime,
) -> None:
    package_path = _write_package(
        tmp_path,
        overrides={"final_review_path": str(tmp_path / "outside.md")},
    )

    report = preflight.run_preflight(package_path)

    assert report["ready"] is False
    assert report["status"] == "blocked"
    assert any(
        "absolute path rejected" in blocker for blocker in report["blockers"]
    )
    assert str(tmp_path) not in json.dumps(report)


def test_escaping_package_path_is_blocked(
    tmp_path: Path,
    available_runtime,
) -> None:
    package_path = _write_package(
        tmp_path,
        overrides={"final_visual_package_path": "../outside-visual.json"},
    )

    report = preflight.run_preflight(package_path)

    assert report["ready"] is False
    assert any(
        "escapes package root" in blocker for blocker in report["blockers"]
    )


def test_no_compile_allows_missing_compiler_tools_with_warnings(
    tmp_path: Path,
    available_runtime,
    monkeypatch,
) -> None:
    package_path = _write_package(tmp_path)

    def fake_which(name: str) -> str | None:
        if name in preflight.COMPILE_TOOLS:
            return None
        return f"{name}.exe"

    monkeypatch.setattr(preflight.shutil, "which", fake_which)

    report = preflight.run_preflight(package_path, no_compile=True)

    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["tools"]["pandoc"]["available"] is False
    assert any(
        "optional publication tool unavailable: pandoc" in warning
        for warning in report["warnings"]
    )
    assert not any(
        "missing required compiler tool" in blocker
        for blocker in report["blockers"]
    )


def test_no_preview_allows_missing_preview_tools_with_warnings(
    tmp_path: Path,
    available_runtime,
    monkeypatch,
) -> None:
    package_path = _write_package(tmp_path)

    def fake_which(name: str) -> str | None:
        if name in preflight.PREVIEW_TOOLS:
            return None
        return f"{name}.exe"

    monkeypatch.setattr(preflight.shutil, "which", fake_which)

    report = preflight.run_preflight(package_path, no_preview=True)

    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["tools"]["pdfinfo"]["available"] is False
    assert any(
        "optional publication tool unavailable: pdfinfo" in warning
        for warning in report["warnings"]
    )
    assert not any(
        "missing required preview tool" in blocker
        for blocker in report["blockers"]
    )


def test_malformed_package_is_blocked(
    tmp_path: Path,
    available_runtime,
) -> None:
    package_path = tmp_path / "REVIEW_CONTENT_PACKAGE.json"
    package_path.write_text("{not-json", encoding="utf-8")

    report = preflight.run_preflight(package_path)

    assert report["ready"] is False
    assert any(
        "malformed JSON" in blocker for blocker in report["blockers"]
    )


def test_python_minimum_is_3_11() -> None:
    assert preflight.MIN_PYTHON == (3, 11)
    python_report, blockers = preflight.check_python()
    assert python_report["required_minimum"] == "3.11"
    assert blockers == []


def test_actual_import_failure_reports_exception_class_only(
    tmp_path: Path,
    available_runtime,
    monkeypatch,
) -> None:
    package_path = _write_package(tmp_path)

    def fake_import_status(module_name: str) -> tuple[bool, str]:
        if module_name == preflight.REQUIRED_PUBLICATION_MODULES["numpy"]:
            return False, "ImportError"
        return True, ""

    monkeypatch.setattr(preflight, "_module_import_status", fake_import_status)

    report = preflight.run_preflight(package_path)

    assert report["ready"] is False
    assert any(
        "required publication import is unavailable: numpy"
        in blocker
        for blocker in report["blockers"]
    )
    assert report["python"]["imports"]["numpy"] == {
        "module": "numpy",
        "available": False,
        "error_type": "ImportError",
    }
    assert "Traceback" not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report)
