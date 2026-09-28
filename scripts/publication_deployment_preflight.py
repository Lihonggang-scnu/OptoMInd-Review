"""Dependency-light deployment preflight for the LaTeX publication path.

This script answers two separate questions:

1. Can this host run the deterministic publication command?
2. Is the supplied package currently publication-eligible?

Runtime prerequisites fail the preflight.  ``draft_only`` or
``publication_eligible=false`` is reported as a policy warning, not a runtime
failure.  The script uses only the Python standard library and does not import
the full Review Harness or scikit-learn.
"""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PACKAGE_SCHEMA = "research_harness.content_package.v1"
MIN_PYTHON = (3, 11)
COMPILE_TOOLS = ("pandoc", "latexmk", "xelatex")
PREVIEW_TOOLS = ("pdfinfo", "pdftoppm")
ALL_TOOLS = COMPILE_TOOLS + PREVIEW_TOOLS

REQUIRED_PUBLICATION_MODULES = {
    "ftfy": "ftfy",
    "numpy": "numpy",
    "pillow": "PIL",
    "pymupdf": "fitz",
    "latex_publication_renderer": (
        "optomind_research.runtime.latex_publication_renderer"
    ),
    "publication_figure_processor": (
        "optomind_research.runtime.publication_figure_processor"
    ),
    "publication_integrity": (
        "optomind_research.runtime.publication_integrity"
    ),
    "artifact_store": "optomind_research.runtime.artifact_store",
}

REQUIRED_FILE_FIELDS = (
    "final_review_path",
    "final_visual_package_path",
    "publication_metadata_path",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _module_import_status(module_name: str) -> tuple[bool, str]:
    try:
        importlib.import_module(module_name)
        return True, ""
    except Exception as exc:
        return False, type(exc).__name__


def check_python() -> tuple[dict[str, Any], list[str]]:
    version_info = sys.version_info
    version = f"{version_info.major}.{version_info.minor}.{version_info.micro}"
    ready = (version_info.major, version_info.minor) >= MIN_PYTHON
    report = {
        "implementation": platform.python_implementation(),
        "version": version,
        "required_minimum": f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        "ready": ready,
    }
    blockers = []
    if not ready:
        blockers.append(
            f"Python {version} is below required minimum "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
        )
    return report, blockers


def check_imports() -> tuple[dict[str, Any], list[str]]:
    modules: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    for key, module_name in REQUIRED_PUBLICATION_MODULES.items():
        available, error_type = _module_import_status(module_name)
        modules[key] = {
            "module": module_name,
            "available": available,
        }
        if not available:
            modules[key]["error_type"] = error_type
        if not available:
            blockers.append(f"required publication import is unavailable: {module_name}")
    return modules, blockers


def check_tools(
    *,
    no_compile: bool,
    no_preview: bool,
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    tools: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    warnings: list[str] = []
    for tool_name in ALL_TOOLS:
        found = shutil.which(tool_name)
        tools[tool_name] = {
            "name": tool_name,
            "available": bool(found),
        }
        if found:
            continue
        if tool_name in COMPILE_TOOLS and not no_compile:
            blockers.append(f"missing required compiler tool: {tool_name}")
        elif tool_name in PREVIEW_TOOLS and not no_preview:
            blockers.append(f"missing required preview tool: {tool_name}")
        else:
            warnings.append(f"optional publication tool unavailable: {tool_name}")
    return tools, blockers, warnings


def _safe_package_path(
    raw: Any,
    *,
    package_dir: Path,
    field: str,
) -> tuple[Path | None, list[str]]:
    errors: list[str] = []
    value = str(raw or "").strip()
    if not value:
        return None, [f"{field}: empty path"]
    candidate = Path(value)
    if candidate.is_absolute():
        return None, [f"{field}: absolute path rejected"]
    package_root = package_dir.resolve()
    resolved = (package_dir / candidate).resolve()
    try:
        resolved.relative_to(package_root)
    except ValueError:
        return None, [f"{field}: path escapes package root"]
    return resolved, errors


def _validate_package_paths(
    package: dict[str, Any],
    *,
    package_dir: Path,
) -> tuple[dict[str, Any], list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    required_files: dict[str, Any] = {}

    source_run_raw = package.get("source_run_dir")
    source_run, errors = _safe_package_path(
        source_run_raw,
        package_dir=package_dir,
        field="source_run_dir",
    )
    blockers.extend(errors)
    if source_run is not None:
        if not source_run.is_dir():
            blockers.append(
                f"source_run_dir is not a directory: {source_run_raw}"
            )
        else:
            required_files["source_run_dir"] = "present"

    for field in REQUIRED_FILE_FIELDS:
        raw = package.get(field)
        path, errors = _safe_package_path(
            raw,
            package_dir=package_dir,
            field=field,
        )
        blockers.extend(errors)
        if path is None:
            required_files[field] = "invalid"
            continue
        if not path.is_file():
            blockers.append(f"{field}: required local file missing: {raw}")
            required_files[field] = "missing"
        else:
            required_files[field] = "present"

    base_kb = str(package.get("base_kb_sqlite") or "").strip()
    if base_kb:
        kb_path, errors = _safe_package_path(
            base_kb,
            package_dir=package_dir,
            field="base_kb_sqlite",
        )
        blockers.extend(errors)
        if kb_path is not None and not kb_path.is_file():
            warnings.append("base_kb_sqlite is configured but missing")

    artifacts = package.get("artifacts")
    if isinstance(artifacts, dict):
        for artifact_name, raw in artifacts.items():
            if not isinstance(raw, (str, Path)):
                continue
            artifact_path, errors = _safe_package_path(
                raw,
                package_dir=package_dir,
                field=f"artifacts.{artifact_name}",
            )
            blockers.extend(errors)
            if artifact_path is not None and not artifact_path.is_file():
                warnings.append(
                    f"artifacts.{artifact_name}: optional artifact missing: {raw}"
                )

    return required_files, blockers, warnings


def _policy_warnings(
    package: dict[str, Any],
    *,
    package_dir: Path,
    metadata_path: Path | None,
) -> list[str]:
    warnings: list[str] = []
    if package.get("status") in {"internal_study_draft", "draft"}:
        warnings.append(
            f"package status is {package.get('status') or 'missing'} "
            "(internal draft)"
        )
    if not bool(package.get("publication_eligible")):
        warnings.append(
            "package publication_eligible is false or absent "
            "(internal draft, not publication-clear)"
        )
    publication_policy = package.get("publication_policy")
    if isinstance(publication_policy, dict):
        if not bool(publication_policy.get("publication_eligible")):
            warnings.append(
                "publication_policy.publication_eligible is false or absent"
            )

    if metadata_path is None or not metadata_path.is_file():
        warnings.append("publication_metadata.json is not readable")
        return warnings
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        warnings.append("publication_metadata.json is malformed")
        return warnings
    if not isinstance(metadata, dict):
        warnings.append("publication_metadata.json is not an object")
        return warnings
    if metadata.get("draft_only", True):
        warnings.append("publication metadata draft_only is true or absent")
    if not bool(metadata.get("publication_eligible")):
        warnings.append(
            "publication metadata publication_eligible is false or absent"
        )
    return warnings


def run_preflight(
    content_package: str | Path,
    *,
    no_compile: bool = False,
    no_preview: bool = False,
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Run the deployment preflight and return machine-readable JSON data."""

    package_path = Path(content_package).resolve()
    python_report, python_blockers = check_python()
    import_report, import_blockers = check_imports()
    tools_report, tool_blockers, tool_warnings = check_tools(
        no_compile=no_compile,
        no_preview=no_preview,
    )

    blockers = [*python_blockers, *import_blockers, *tool_blockers]
    warnings = [*tool_warnings]

    package_report: dict[str, Any] = {
        "name": package_path.name,
        "exists": package_path.is_file(),
        "schema_version": "",
        "required_local_inputs": {},
        "path_portability": {
            "portable_relative_paths": False,
            "errors": [],
        },
        "policy_warnings": [],
    }
    if not package_path.is_file():
        blockers.append(f"content package not found: {package_path.name}")
        package_report["path_portability"]["errors"].append(
            "content package file is missing"
        )
    else:
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"content package is malformed JSON: {exc}")
            package = None
        else:
            if not isinstance(package, dict):
                blockers.append("content package root is not a JSON object")
                package = None
        if package is not None:
            package_dir = package_path.parent
            package_report["schema_version"] = str(
                package.get("schema_version") or ""
            )
            if package.get("schema_version") != PACKAGE_SCHEMA:
                blockers.append(
                    "content package schema_version is not "
                    f"{PACKAGE_SCHEMA}"
                )
            required_files, path_blockers, path_warnings = _validate_package_paths(
                package,
                package_dir=package_dir,
            )
            blockers.extend(path_blockers)
            warnings.extend(path_warnings)
            package_report["required_local_inputs"] = required_files
            package_report["path_portability"]["portable_relative_paths"] = (
                not path_blockers
            )
            package_report["path_portability"]["errors"] = path_blockers

            metadata_raw = package.get("publication_metadata_path")
            metadata_path = None
            if isinstance(metadata_raw, str) and metadata_raw.strip():
                metadata_path, _ = _safe_package_path(
                    metadata_raw,
                    package_dir=package_dir,
                    field="publication_metadata_path",
                )
            package_report["policy_warnings"] = _policy_warnings(
                package,
                package_dir=package_dir,
                metadata_path=metadata_path,
            )
            warnings.extend(package_report["policy_warnings"])

    report = {
        "status": "",
        "ready": not bool(blockers),
        "blockers": blockers,
        "warnings": warnings,
        "python": {
            **python_report,
            "imports": import_report,
        },
        "tools": tools_report,
        "package": package_report,
        "checked_at": checked_at or _utc_now(),
    }
    report["status"] = "ready" if report["ready"] else "blocked"
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight the publication deployment without importing the full "
            "Review Harness."
        )
    )
    parser.add_argument("--content-package", required=True, type=Path)
    parser.add_argument("--no-compile", action="store_true")
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--output-report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    report = run_preflight(
        args.content_package,
        no_compile=args.no_compile,
        no_preview=args.no_preview,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.output_report:
        output = Path(args.output_report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PACKAGE_SCHEMA",
    "COMPILE_TOOLS",
    "PREVIEW_TOOLS",
    "check_python",
    "check_imports",
    "check_tools",
    "run_preflight",
    "build_arg_parser",
    "main",
]
