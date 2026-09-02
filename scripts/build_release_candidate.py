"""Build the reviewable public layer for the three historical E2E runs.

This script deliberately reads only the three explicitly named run folders
under ``outputs/research_harness_e2e``. It never reads ``api_keys`` and never
walks the separate TMM handoff line. The raw runs stay where they are; the
script creates a portable publication snapshot plus a path-relative,
SHA-256-complete inventory of every raw file.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / "outputs" / "research_harness_e2e"
PUBLIC_ROOT = ROOT / "artifacts" / "e2e"
FULL_MANIFEST_PATH = ROOT / "artifacts" / "e2e-full-manifest.json"

RUNS: tuple[dict[str, str], ...] = (
    {
        "number": "01",
        "slug": "01-optical-diffractive-neural-networks",
        "label": "Optical diffractive neural networks",
        "run_dir": "rhr_optical_diffractive_neural_networks_20260828_v2b",
    },
    {
        "number": "02",
        "slug": "02-metasurface-holography",
        "label": "Metasurface holography",
        "run_dir": "rhr_metasurface_holography_20260828_v1",
    },
    {
        "number": "03",
        "slug": "03-scalable-photonic-computing",
        "label": "Scalable photonic computing",
        "run_dir": "rhr_photonic_computing_20260829_v1",
    },
)

TEXT_SUFFIXES = {
    ".asc",
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

PUBLIC_SUBTREES = (
    "publication",
    "publication_mainline",
    "visual_editor/final",
)

PUBLIC_ROOT_FILES = (
    "DELIVERY_GATE.json",
    "FINAL_CITATION_MAP.json",
    "HARNESS_COST.json",
    "HARNESS_RUN_REPORT.md",
    "QWEN_CAPABILITY_STATUS.json",
    "REVIEW_CONTENT_PACKAGE.json",
    "REVIEW_HARNESS_QUALITY_REPORT.json",
    "REVIEW_HARNESS_QUALITY_REPORT.md",
    "TOPIC_IDENTITY.json",
    "query_planner/ORIGINAL_USER_QUESTION.json",
)


def _json_load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sanitize_text(text: str) -> str:
    """Remove local machine paths and credential path hints from public text."""

    root_variants = {
        str(ROOT),
        str(ROOT).replace("/", "\\"),
        str(ROOT).replace("\\", "/"),
    }
    for variant in sorted(root_variants, key=len, reverse=True):
        text = text.replace(variant, "<repository-root>")
    text = text.replace(
        "OptoMind-TMM-Article-Handoff-20260813", "<separate-line>"
    )
    text = re.sub(
        r"(?i)(?:api_keys|api-keys)[\\/][^\\/\s\"'<>]+",
        "<private-api-file>",
        text,
    )
    text = re.sub(
        r"(?i)(?:qwen-api-key|semantic-scholar-api-key|dashscope_api_key)",
        "<private-credential>",
        text,
    )
    text = re.sub(
        r"(?i)\b[A-Z]:[\\/][^\r\n\"'<>]+",
        "<local-path>",
        text,
    )
    return text


def _copy_public_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() in TEXT_SUFFIXES:
        raw = source.read_text(encoding="utf-8", errors="replace")
        target.write_text(_sanitize_text(raw), encoding="utf-8", newline="\n")
    else:
        shutil.copy2(source, target)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _raw_run_manifest(run: dict[str, str]) -> dict[str, Any]:
    source = RUN_ROOT / run["run_dir"]
    if not source.is_dir():
        raise FileNotFoundError(source)
    files: list[dict[str, Any]] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        files.append(
            {
                "path": path.relative_to(source).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return {
        "number": run["number"],
        "label": run["label"],
        "source_relative_dir": _relative(source),
        "file_count": len(files),
        "bytes": sum(int(item["bytes"]) for item in files),
        "files": files,
    }


def _delivery_summary(source: Path) -> dict[str, Any]:
    delivery = _json_load(source / "DELIVERY_GATE.json")
    checks = delivery.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    english = checks.get("english_review_pdf")
    english = english if isinstance(english, dict) else {}
    chinese = checks.get("chinese_review_pdf")
    chinese = chinese if isinstance(chinese, dict) else {}
    cost = _json_load(source / "HARNESS_COST.json")
    state = _json_load(source / "HARNESS_STATE.json")
    topic = _json_load(source / "TOPIC_IDENTITY.json")
    question = _json_load(source / "query_planner" / "ORIGINAL_USER_QUESTION.json")
    return {
        "question": question.get("user_question") or topic.get("normalized_question"),
        "topic": topic.get("normalized_question"),
        "delivery_status": delivery.get("status"),
        "english_deliverable": delivery.get("english_deliverable"),
        "degraded_checks": delivery.get("degraded_checks", []),
        "english_pdf": {
            "available": bool(english.get("ok")),
            "pages": english.get("pages"),
            "bytes": english.get("bytes"),
        },
        "chinese_pdf": {
            "available": bool(chinese.get("ok")),
            "pages": chinese.get("pages"),
            "bytes": chinese.get("bytes"),
        },
        "cost_cny": cost.get("cost_cny"),
        "budget_cny": cost.get("global_cost_budget_cny"),
        "remaining_budget_cny": cost.get("remaining_budget_cny"),
        "model_call_count": cost.get("model_call_count"),
        "error_count": cost.get("error_count"),
        "completed_stage": cost.get("completed_stage") or state.get("current_stage"),
        "run_status": state.get("status"),
    }


def _copy_public_layer(run: dict[str, str], summary: dict[str, Any]) -> list[dict[str, Any]]:
    source = RUN_ROOT / run["run_dir"]
    target_root = PUBLIC_ROOT / run["slug"]
    copied: list[dict[str, Any]] = []

    for subtree in PUBLIC_SUBTREES:
        subtree_source = source / subtree
        if not subtree_source.is_dir():
            continue
        for path in sorted(subtree_source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            target = target_root / relative
            _copy_public_file(path, target)
            copied.append(
                {
                    "path": target.relative_to(ROOT).as_posix(),
                    "source_relative_path": _relative(path),
                    "bytes": target.stat().st_size,
                    "sha256": _sha256(target),
                }
            )

    for relative_text in PUBLIC_ROOT_FILES:
        source_path = source / relative_text
        if not source_path.is_file():
            continue
        target = target_root / "reports" / relative_text
        _copy_public_file(source_path, target)
        copied.append(
            {
                "path": target.relative_to(ROOT).as_posix(),
                "source_relative_path": _relative(source_path),
                "bytes": target.stat().st_size,
                "sha256": _sha256(target),
            }
        )

    summary_path = target_root / "run-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    copied.append(
        {
            "path": summary_path.relative_to(ROOT).as_posix(),
            "source_relative_path": None,
            "bytes": summary_path.stat().st_size,
            "sha256": _sha256(summary_path),
        }
    )
    return copied


def build() -> dict[str, Any]:
    raw_runs: list[dict[str, Any]] = []
    public_runs: list[dict[str, Any]] = []
    for run in RUNS:
        raw = _raw_run_manifest(run)
        raw_runs.append(raw)
        source = RUN_ROOT / run["run_dir"]
        summary = _delivery_summary(source)
        public_runs.append(
            {
                "number": run["number"],
                "label": run["label"],
                "slug": run["slug"],
                "summary": summary,
                "files": _copy_public_layer(run, summary),
            }
        )

    raw_manifest = {
        "schema_version": "optomind.e2e_raw_artifacts_manifest.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_policy": (
            "Only the three named research_harness_e2e run directories are read; "
            "api_keys and the separate TMM line are outside the manifest scope."
        ),
        "runs": raw_runs,
        "totals": {
            "file_count": sum(int(run["file_count"]) for run in raw_runs),
            "bytes": sum(int(run["bytes"]) for run in raw_runs),
        },
    }
    FULL_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    FULL_MANIFEST_PATH.write_text(
        json.dumps(raw_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    release_manifest = {
        "schema_version": "optomind.e2e_public_layer_manifest.v1",
        "generated_at": raw_manifest["generated_at"],
        "full_raw_manifest": FULL_MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "runs": public_runs,
    }
    release_manifest_path = PUBLIC_ROOT.parent / "e2e-public-layer-manifest.json"
    release_manifest_path.write_text(
        json.dumps(release_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "raw_manifest": FULL_MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "public_manifest": release_manifest_path.relative_to(ROOT).as_posix(),
        "raw_totals": raw_manifest["totals"],
        "public_file_count": sum(len(run["files"]) for run in public_runs),
    }


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
