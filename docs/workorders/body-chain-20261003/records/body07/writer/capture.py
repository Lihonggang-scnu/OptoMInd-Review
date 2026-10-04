"""Capture only WO07 chain-4 tests and archive actual production artifacts.

Usage: PYTHONPATH="$DEPS:." PYTHONUTF8=1 python <this file>
All provider responses are synthetic. No network, paid calls, or full workflow.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RECORD = Path(__file__).resolve().parent
REPO = RECORD.parents[5]
TEST = "tests/upgrade3/test_body07_arrangement_writer_short_chain.py"
SCENARIOS = ("complete", "pending_table", "pending_needs_arrangement", "pending_contract_failed", "normal")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def consolidate(source: Path) -> None:
    """Read already-persisted files; replace only the temporary root prefix."""
    manifest = {"schema": "body07.short-chain-4.evidence.v1", "baseline": "841e914",
        "synthetic": True, "paid_model_calls": 0, "external_requests": 0,
        "production_code_modified_by_this_slice": [],
        "redaction": {"rule": "Replace capture-root and repository absolute prefixes throughout UTF-8 content, including embedded JSON strings",
            "replacements": {"capture_root": "$TEMP", "repository_root": "$REPO"},
            "evidence_kind": "Derived path-normalized deterministic-fixture evidence; not byte-original supplier requests",
            "original_byte_hashes": "Hashes of original persisted production files before root redaction",
            "archived_byte_hashes": "Hashes of archived UTF-8 content after root redaction"},
        "archives": []}
    for scenario in SCENARIOS:
        scenario_root = source / scenario
        files = []
        for path in sorted(scenario_root.rglob("*")):
            if not path.is_file():
                continue
            raw = path.read_bytes()
            text = raw.decode("utf-8")
            archived = text.replace(str(source), "$TEMP").replace(str(REPO), "$REPO")
            files.append({"path": str(path.relative_to(scenario_root)), "original_bytes": len(raw),
                "original_sha256": sha(raw), "archived_sha256": sha(archived.encode("utf-8")),
                "root_redactions": text.count(str(source)), "repository_redactions": text.count(str(REPO)), "content_utf8": archived})
        lookup = {row["path"]: row["content_utf8"] for row in files}
        report = json.loads(lookup["delivery/DELIVERY_REPORT.json"])
        summary = {"delivery_mode": report["delivery_mode"], "pending": report.get("pending", []),
            "assembly": {key: report.get("assembly", {}).get(key) for key in
                ("status", "loaded_units", "problems_resolved", "table_count", "pending_problems")},
            "boundary_counts": json.loads(lookup["BOUNDARY_COUNTS.json"]) if "BOUNDARY_COUNTS.json" in lookup else
                json.loads(lookup["NORMAL_COMPARISON.json"])}
        payload = {"schema": "body07.short-chain-4.artifact-archive.v1", "scenario": scenario,
            "synthetic": True, "warning": "Production mode=run is necessary for assembly; every model response was substituted offline.",
            "redaction": manifest["redaction"], "summary": summary, "files": files}
        target = RECORD / (scenario.upper() + "_ARTIFACTS.json")
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest["archives"].append({"path": str(target.relative_to(REPO)), "file_count": len(files),
            "original_total_bytes": sum(row["original_bytes"] for row in files),
            "archive_sha256": sha(target.read_bytes()), "summary": summary})
    manifest["file_count"] = sum(row["file_count"] for row in manifest["archives"])
    (RECORD / "EVIDENCE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="body07-writer-") as temporary:
        source = Path(temporary).resolve()
        env = {**os.environ, "BODY07_WRITER_EVIDENCE_ROOT": str(source), "PYTHONUTF8": "1"}
        command = [sys.executable, "-m", "pytest", "-q", TEST]
        result = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
        output = result.stdout + result.stderr
        (RECORD / "CAPTURE_PYTEST.txt").write_text(output, encoding="utf-8")
        print(output, end="")
        if result.returncode:
            return result.returncode
        consolidate(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
