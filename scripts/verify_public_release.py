"""Verify the public release boundary without reading private credentials."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from build_release_candidate import ROOT, RUNS, RUN_ROOT, PUBLIC_ROOT


TEXT_SUFFIXES = {
    ".aux",
    ".bib",
    ".bbl",
    ".blg",
    ".fdb_latexmk",
    ".fls",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".out",
    ".tex",
    ".txt",
    ".yaml",
    ".yml",
}

FORBIDDEN_TEXT_PATTERNS = (
    ("separate TMM line", re.compile(r"OptoMind-TMM-Article-Handoff-20260813")),
    ("Windows absolute path", re.compile(r"(?i)\b[A-Z]:[\\/]")),
    ("Qwen credential filename", re.compile(r"(?i)qwen-api-key\.txt")),
    ("provider credential filename", re.compile(r"(?i)semantic-scholar-api-key\.txt")),
    ("credential environment variable", re.compile(r"(?i)(DASHSCOPE_API_KEY|QWEN_API_KEY)")),
    ("dotenv secret marker", re.compile(r"(?i)(^|[\s\"'])\.env(?:\.|[\s\"'])")),
    ("key-shaped token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _check_required(failures: list[str], *, public_only: bool) -> None:
    required = (
        "README.md",
        "docs/MAINLINE_FILE_MAP.md",
        "docs/OPEN_SOURCE_RELEASE_CHECKLIST.md",
        "docs/E2E_ARTIFACTS_INDEX.md",
        "run_review_harness.py",
        "config/model_policy.yaml",
        "replay/index.html",
        "replay/replay-manifest.json",
        "artifacts/e2e-full-manifest.json",
        "artifacts/e2e-public-layer-manifest.json",
    )
    for relative in required:
        if not (ROOT / relative).is_file():
            failures.append(f"missing required file: {relative}")

    for run in RUNS:
        raw = RUN_ROOT / run["run_dir"]
        public = PUBLIC_ROOT / run["slug"]
        if not public_only:
            for relative in (
                "DELIVERY_GATE.json",
                "HARNESS_COST.json",
                "HARNESS_STATE.json",
                "publication/latex/main.pdf",
                "publication/latex_zh/main.pdf",
            ):
                if not (raw / relative).is_file():
                    failures.append(
                        f"missing raw artifact: {raw.relative_to(ROOT) / relative}"
                    )
        for relative in (
            "publication/latex/main.pdf",
            "publication/latex/main.tex",
            "publication/latex/references.bib",
            "publication/latex_zh/main.pdf",
            "publication/latex_zh/main.tex",
            "publication/latex_zh/references.bib",
            "run-summary.json",
        ):
            if not (public / relative).is_file():
                failures.append(f"missing public artifact: {public.relative_to(ROOT) / relative}")


def _scan_public_text(failures: list[str]) -> None:
    scan_roots = (ROOT / "artifacts", ROOT / "replay")
    for scan_root in scan_roots:
        if not scan_root.is_dir():
            continue
        for path in scan_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                failures.append(f"cannot read public text {path.relative_to(ROOT)}: {exc}")
                continue
            for label, pattern in FORBIDDEN_TEXT_PATTERNS:
                if pattern.search(text):
                    failures.append(f"{label} found in {path.relative_to(ROOT)}")


def _check_summary_consistency(
    failures: list[str], *, public_only: bool
) -> None:
    manifest = _read_json(ROOT / "artifacts/e2e-full-manifest.json")
    if manifest.get("totals", {}).get("file_count", 0) <= 0:
        failures.append("raw artifact manifest is empty")
    if len(manifest.get("runs", [])) != len(RUNS):
        failures.append("raw artifact manifest does not contain exactly three runs")

    public_manifest = _read_json(ROOT / "artifacts/e2e-public-layer-manifest.json")
    if len(public_manifest.get("runs", [])) != len(RUNS):
        failures.append("public artifact manifest does not contain exactly three runs")

    replay_manifest = _read_json(ROOT / "replay/replay-manifest.json")
    if len(replay_manifest.get("runs", [])) != len(RUNS):
        failures.append("static replay manifest does not contain exactly three runs")


def main() -> int:
    failures: list[str] = []
    public_only = not RUN_ROOT.is_dir()
    _check_required(failures, public_only=public_only)
    _scan_public_text(failures)
    _check_summary_consistency(failures, public_only=public_only)
    if failures:
        print("PUBLIC RELEASE CHECK: FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("PUBLIC RELEASE CHECK: PASSED")
    print("- private api_keys directory was not read")
    print("- separate TMM line was not traversed")
    if public_only:
        print("- public snapshot mode: raw E2E run trees are intentionally absent")
    else:
        print("- three raw runs and three replay entries are present")
    print("- public text layer contains no detected credential/path markers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
