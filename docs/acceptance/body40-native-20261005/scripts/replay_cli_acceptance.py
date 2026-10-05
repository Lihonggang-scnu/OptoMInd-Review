"""Native zero-call BODY40 replay through the production writer CLI and assembly."""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
import sys
from pathlib import Path

WORKTREE = Path(r"F:/OptoMind-Review-2/outputs/body40_identity_citations_local_acceptance_20261005/worktree")
ACCEPTANCE = Path(r"F:/OptoMind-Review-2/outputs/body40_identity_citations_local_acceptance_20261005/native")
ARCHIVE = WORKTREE / "docs/acceptance/body40-20261005"
LOCAL_RUN = Path(r"F:/OptoMind-Review-2/outputs/body_full_staged_acceptance_20261004_40cny")
REPLAY = ACCEPTANCE / "replay"

sys.path.insert(0, str(WORKTREE))

from scripts.upgrade3 import full_review_draft as assembly  # noqa: E402
from scripts.upgrade3 import review_unit_writer as cli  # noqa: E402
from optomind_research.runtime.upgrade3 import review_unit_writer as writer  # noqa: E402


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def deny_network(*args, **kwargs):
    raise AssertionError("network forbidden in native BODY40 replay")


def relocated_input(case_dir: Path, chapter_id: str, original: Path, source_root: Path) -> Path:
    if source_root == ARCHIVE:
        arrangement_path = source_root / "body/arrangement_repaired" / chapter_id / "CHAPTER_ARRANGEMENT.json"
        arrangement_input_path = arrangement_path.with_name("ARRANGEMENT_INPUT.json")
    else:
        arrangement_path = source_root / "body_assembly_final/arrangements" / chapter_id / "CHAPTER_ARRANGEMENT.json"
        arrangement_input_path = source_root / "arrangement_repaired" / chapter_id / "ARRANGEMENT_INPUT.json"
    arrangement = read_json(arrangement_path)
    for source in arrangement.get("source_catalog", {}).values():
        source.pop("locator", None)
    saved_input = read_json(original / "UNIT_INPUT.json")
    for material in saved_input.get("materials", []):
        handle = material["source_handle"]
        if handle in arrangement["source_catalog"]:
            arrangement["source_catalog"][handle].update(
                {key: value for key, value in material.items() if key != "locator"}
            )
    path = write_json(case_dir / "input/CHAPTER_ARRANGEMENT.json", arrangement)
    write_json(path.with_name("ARRANGEMENT_INPUT.json"), read_json(arrangement_input_path) if arrangement_input_path.is_file() else {"chapter_id": chapter_id})
    return path


def assemble(case_root: Path, arrangement_path: Path, result_path: Path, unit_id: str, chapter_id: str):
    arrangement = read_json(arrangement_path)
    arrangement["units"] = [u for u in arrangement.get("units", []) if u["unit_id"] == unit_id]
    arrangement["issues"] = []
    arrangement["status"] = "complete"
    arrangement_path = write_json(case_root / "assembly/arrangement.json", arrangement)
    manifest = write_json(case_root / "assembly/manifest.json", {
        "review_title": "BODY40 native zero-call replay",
        "planning_status": "complete",
        "arrangement_status": "complete",
        "chapters": [{"chapter_id": chapter_id, "arrangement_path": str(arrangement_path)}],
    })
    batch_root = case_root / "assembly/batch"
    write_json(batch_root / "BATCH_JOBS.json", [{
        "chapter_id": chapter_id,
        "unit_id": unit_id,
        "arrangement": str(arrangement_path),
        "reused_result": str(result_path),
        "output": str(result_path.parent),
    }])
    output = case_root / "assembly/output"
    rc = assembly.main(["--manifest", str(manifest), "--batch-root", str(batch_root), "--output-root", str(output)])
    if rc != 0:
        raise RuntimeError(f"assembly returned {rc} for {case_root.name}")
    return read_json(output / "ASSEMBLY_SUMMARY.json"), output


def recorded_cli(case_name: str, chapter_id: str, unit_id: str, response_file: Path, source_root: Path):
    case_root = REPLAY / case_name
    if case_root.exists():
        shutil.rmtree(case_root)
    case_root.mkdir(parents=True)
    original = response_file.parent.parent
    arrangement_path = relocated_input(case_root, chapter_id, original, source_root)
    original_messages = original / "UNIT_MESSAGES.json"
    original_input = original / "UNIT_INPUT.json"
    response_before = response_file.read_bytes()
    response = read_json(response_file)
    calls = []

    def replay(messages, **kwargs):
        calls.append({"message_count": len(messages), "kwargs": sorted(kwargs)})
        return response

    old_client = cli._real_client
    old_counter = cli._default_qwen_token_counter
    old_connect = socket.socket.connect
    old_create = socket.create_connection
    try:
        cli._real_client = lambda *args, **kwargs: replay
        cli._default_qwen_token_counter = lambda: None
        socket.socket.connect = deny_network
        socket.create_connection = deny_network
        output_root = case_root / "writer"
        rc = cli.main([
            "--arrangement", str(arrangement_path), "--unit", unit_id,
            "--output-root", str(output_root), "--run", "--planning-revision",
            "--max-material-chars-per-source", "0",
        ])
    finally:
        cli._real_client = old_client
        cli._default_qwen_token_counter = old_counter
        socket.socket.connect = old_connect
        socket.create_connection = old_create
    if rc != 0 or len(calls) != 1:
        raise RuntimeError(f"writer CLI failed: rc={rc}, calls={len(calls)}")
    report = read_json(output_root / "UNIT_WRITING_RUN.json")
    result_path = Path(report["units"][0]["result_path"])
    result = read_json(result_path)
    generated_messages = result_path.parent / "UNIT_MESSAGES.json"
    raw_body = writer.parse_unit_body(response)
    summary, assembly_output = assemble(case_root, arrangement_path, result_path, unit_id, chapter_id)
    assert response_file.read_bytes() == response_before
    return {
        "case": case_name,
        "provider_mode": "recorded_response_adapter",
        "live_calls": 0,
        "paid_calls": 0,
        "network_calls": 0,
        "recorded_provider_calls": len(calls),
        "raw_response": str(response_file),
        "raw_sha256": sha256(response_file),
        "source_record_root": str(original),
        "original_unit_input": str(original_input),
        "original_unit_input_sha256": sha256(original_input),
        "original_unit_messages": str(original_messages),
        "original_unit_messages_sha256": sha256(original_messages),
        "generated_unit_messages": str(generated_messages),
        "generated_unit_messages_sha256": sha256(generated_messages),
        "original_vs_generated_messages_byte_equal": original_messages.read_bytes() == generated_messages.read_bytes(),
        "raw_body_sha256": hashlib.sha256(raw_body.encode("utf-8")).hexdigest(),
        "result_body_sha256": hashlib.sha256(result["body_markdown"].encode("utf-8")).hexdigest(),
        "body_equals_parsed_raw": result["body_markdown"] == raw_body,
        "result_path": str(result_path),
        "result": {
            key: result.get(key)
            for key in (
                "body_markdown", "complete", "citation_problems", "unresolved_numeric_citations",
                "numeric_citation_repairs", "citation_mapping_diagnostics", "known_tool_identifiers",
                "non_source_identifier_citations", "citation_number_map_origin",
            )
        },
        "assembly_summary_path": str(assembly_output / "ASSEMBLY_SUMMARY.json"),
        "assembly": {
            "status": summary.get("status"),
            "problems_resolved": summary.get("problems_resolved"),
            "pending_problem_count": len(summary.get("pending_problems") or []),
            "unit_rows": summary.get("unit_rows"),
            "run_report": str(assembly_output / "RUN_REPORT.md"),
        },
    }


def controlled_unique_title():
    case_root = REPLAY / "controlled_unique_title"
    if case_root.exists():
        shutil.rmtree(case_root)
    arrangement = {
        "chapter_id": "ChX",
        "title": "controlled",
        "source_catalog": {"P0011": {
            "source_handle": "P0011", "paper_id": "controlled-stable",
            "title": "Stellar Oscillations in Red Giants",
            "study_summary_A": {"finding": "Controlled evidence."},
        }},
        "units": [{"unit_id": "U1", "focus": "controlled", "paragraph_tasks": [
            {"paragraph_id": "P1", "source_uses": [{"source_handle": "P0011"}]}], "table_tasks": []}],
    }
    arrangement_path = write_json(case_root / "input/CHAPTER_ARRANGEMENT.json", arrangement)
    write_json(arrangement_path.with_name("ARRANGEMENT_INPUT.json"), {"chapter_id": "ChX", "title": "controlled"})
    response = {"content": json.dumps({
        "body_markdown": "Controlled finding [1].\n\n[1] Stellar Oscillations in Red Giants"
    }, ensure_ascii=False), "complete": True, "finish_reason": "stop"}
    calls = []

    def replay(messages, **kwargs):
        calls.append(len(messages))
        return response

    old_client = cli._real_client
    old_counter = cli._default_qwen_token_counter
    old_connect = socket.socket.connect
    old_create = socket.create_connection
    try:
        cli._real_client = lambda *args, **kwargs: replay
        cli._default_qwen_token_counter = lambda: None
        socket.socket.connect = deny_network
        socket.create_connection = deny_network
        output_root = case_root / "writer"
        rc = cli.main([
            "--arrangement", str(arrangement_path), "--unit", "U1",
            "--output-root", str(output_root), "--run", "--planning-revision",
            "--max-material-chars-per-source", "0",
        ])
    finally:
        cli._real_client = old_client
        cli._default_qwen_token_counter = old_counter
        socket.socket.connect = old_connect
        socket.create_connection = old_create
    if rc != 0 or len(calls) != 1:
        raise RuntimeError(f"controlled writer CLI failed: rc={rc}, calls={len(calls)}")
    report = read_json(output_root / "UNIT_WRITING_RUN.json")
    result_path = Path(report["units"][0]["result_path"])
    result = read_json(result_path)
    summary, output = assemble(case_root, arrangement_path, result_path, "U1", "ChX")
    return {
        "provider_mode": "recorded_response_adapter",
        "live_calls": 0, "paid_calls": 0, "network_calls": 0,
        "result_path": str(result_path),
        "body_markdown": result["body_markdown"],
        "numeric_citation_repairs": result["numeric_citation_repairs"],
        "bibliography_title_citation_map": result["bibliography_title_citation_map"],
        "citation_problems": result["citation_problems"],
        "assembly_summary_path": str(output / "ASSEMBLY_SUMMARY.json"),
        "assembly_status": summary.get("status"),
        "assembly_problems_resolved": summary.get("problems_resolved"),
    }


def main():
    REPLAY.mkdir(parents=True, exist_ok=True)
    ch6 = next((ARCHIVE / "writer/live/Ch6/Ch6_Ch6_U4/raw_responses").glob("*.raw"))
    ch7 = next((ARCHIVE / "writer/live/Ch7/Ch7_Ch7_U02/raw_responses").glob("*.raw"))
    cases = [
        recorded_cli("public_archive_Ch6_U4", "Ch6", "Ch6_U4", ch6, ARCHIVE),
        recorded_cli("public_archive_Ch7_U02", "Ch7", "Ch7_U02", ch7, ARCHIVE),
        recorded_cli("original_local_Ch6_U4", "Ch6", "Ch6_U4", LOCAL_RUN / "writer_live/Ch6/Ch6_Ch6_U4/raw_responses/Ch6_Ch6_U4_20261005T081431.raw", LOCAL_RUN),
        recorded_cli("original_local_Ch7_U02", "Ch7", "Ch7_U02", LOCAL_RUN / "writer_live/Ch7/Ch7_Ch7_U02/raw_responses/Ch7_Ch7_U02_20261005T082327.raw", LOCAL_RUN),
    ]
    report = {
        "mode": "native_windows_zero_call_replay",
        "python": sys.executable,
        "hash_seed": "0",
        "network_denied": True,
        "paid_calls": 0,
        "network_calls": 0,
        "cases": cases,
        "controlled_unique_title": controlled_unique_title(),
    }
    write_json(ACCEPTANCE / "native_replay_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
