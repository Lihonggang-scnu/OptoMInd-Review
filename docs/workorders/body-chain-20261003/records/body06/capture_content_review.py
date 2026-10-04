"""Capture parser transport separately from the accompanying reviewer content assessment.

All statements and measurements in the fixture are synthetic. No model runs.
"""
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

from optomind_research.runtime.upgrade3 import review_unit_writer as current


def deny(*args, **kwargs):
    raise AssertionError("WO06 fixture capture has no network boundary")


socket.create_connection = deny
socket.socket.connect = deny
ROOT = Path(__file__).resolve().parents[5]
HERE = Path(__file__).resolve().parent
BASE = "8582bb698700937b0b89db65b26c8cfa958b210b"
SOURCE = "optomind_research/runtime/upgrade3/review_unit_writer.py"


def observe(module, case):
    try:
        body = module.parse_unit_body(case["response"])
        return {"parsed_body": body, "table_structure": module._markdown_table_check(body)}
    except module.UnitWritingError as error:
        return {"parse_error": str(error)}


def main():
    fixture = json.loads((HERE / "CONTENT_REVIEW_FIXTURES.json").read_text(encoding="utf-8"))
    source = subprocess.check_output(["git", "show", BASE + ":" + SOURCE], cwd=ROOT, text=True)
    with tempfile.TemporaryDirectory(prefix="body06-parser-baseline-") as folder:
        path = Path(folder) / "baseline_writer.py"
        path.write_text(source, encoding="utf-8")
        name = "optomind_research.runtime.upgrade3._body06_parser_baseline"
        spec = importlib.util.spec_from_file_location(name, path)
        baseline = importlib.util.module_from_spec(spec)
        sys.modules[name] = baseline
        spec.loader.exec_module(baseline)
        results = [{"id": case["id"], "before": observe(baseline, case),
                    "after": observe(current, case), "separate_reviewer_assessment": case["reviewer_content_assessment"]}
                   for case in fixture["cases"]]
    result = {"notice": fixture["notice"], "baseline": BASE,
              "scope": "Actual parser/checker only. Assessment labels were written by the root/independent AI reviewers, not computed by these functions.",
              "results": results}
    (HERE / "CONTENT_REVIEW_CAPTURE.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Captured three synthetic examples; content assessments remain separate from parser checks.")


if __name__ == "__main__":
    main()
