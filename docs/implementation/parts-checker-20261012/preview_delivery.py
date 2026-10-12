"""Generate a free, synthetic production short chain in a NEW output directory.

Run from the repository: python docs/implementation/parts-checker-20261012/preview_delivery.py --output OUTPUT
This exercises actual serial assembly, checker replay, shared coordinator and
04/05 consumers. Scientific prose is an explicit synthetic response fixture.
"""
from pathlib import Path
import argparse
import contextlib
import hashlib
import io
import json
import sys
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from optomind_research.runtime.upgrade3.serial_manuscript_parts import run_serial_parts
from optomind_research.runtime.upgrade3.manuscript_checker import build_checker_messages
from scripts.upgrade3.manuscript_delivery import main


def generate(output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Evidence output must be new; do not overwrite prior evidence")
    output.mkdir(parents=True)
    source_files = ["optomind_research/runtime/upgrade3/" + name + ".py" for name in
        ("serial_manuscript_parts", "serial_parts_application", "manuscript_checker", "manuscript_delivery", "review_delivery", "delivery_citations", "post_body_revision_contracts")]
    source_files += ["scripts/upgrade3/manuscript_delivery.py", "prompts/manuscript_checker.md"]
    source_hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_files}
    def write(name, value):
        path = output / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    body = output / "BODY.md"
    body.write_text("# 合成材料比较\n\n## 比较条件\n\n电池材料的比较需要保持测量条件一致 [P0001]。\n", encoding="utf-8")
    part = {"purpose": "Explain the reader task", "focus": ["Actual supported conditions"],
        "boundary": ["No stronger conclusions"], "placement": {"mode": "standalone", "anchor": "manuscript_start"},
        "finalize_from": ["Actual BODY"]}
    fixture = {"conception": {"manuscript_parts_plan": {"context": "Synthetic battery review", **{k: part for k in ("abstract", "introduction", "conclusion")}}},
        "conclusion": {"conclusion": "比较以一致测量条件为前提。"},
        "introduction": {"introduction": "比较需要明确测量条件 [P0001]。本文组织相关条件。"},
        "abstract": {"title": "合成材料比较", "abstract": "本文本文解释比较条件。", "keywords": ["条件", "比较"]}}
    fixture_path = write("PARTS_FIXTURE.json", fixture)
    identity = {"P0001": {"paper_id": "synthetic-source", "title": "Synthetic comparison conditions", "doi": "10.1/synthetic"}}
    parts = run_serial_parts(draft_path=body, research_question="如何保留比较条件？", chapter_roles=[],
        out_dir=output / "03_front_back", context={"source_identity_map": identity}, parts_fixture_path=fixture_path)
    if parts["status"] != "generated":
        raise ValueError(parts)
    source = Path(parts["final_manuscript"])
    catalogs = [{"entries": [{"source_handle": "P0001", **identity["P0001"]}]}]
    config = write("delivery.json", {"schema": "review_v2_delivery.config.v1", "front_back": {"mode": "post_body"},
        "identity_catalogs": catalogs, "manuscript_checker": {"enabled": True}})
    messages = build_checker_messages(draft_text=source.read_bytes().decode("utf-8"), identity_catalogs=catalogs)
    payload = json.loads(messages[1]["content"])
    block = next(b for b in payload["blocks"] if "本文本文" in b["text"])
    responses = write("CHECKER_RESPONSES.json", {"responses": {"round_1": {
        "snapshot_id": payload["snapshot_id"], "changes": [{"target_block_id": block["block_id"],
            "operation": "replace", "original_text": "本文本文", "replacement_text": "本文", "reason": "Remove duplicated word in synthetic fixture."}],
        "unresolved_questions": []}}})
    report_path = output / "03_front_back/FRONT_BACK_REPORT.json"
    command = ["--parts-report", str(report_path), "--config", str(config), "--responses", str(responses), "--output", str(output / "delivery")]
    results = []
    for name in ("FIRST_CLI.json", "RESUME_CLI.json"):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = main(command)
        result = json.loads(captured.getvalue())
        write(name, result)
        results.append(result)
        if code:
            raise ValueError("CLI failed")
    preview_command = ["--parts-report", str(report_path), "--config", str(config), "--output", str(output / "preview")]
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(preview_command) == 0
    selected = results[0]["selected_manuscript"]
    publication = results[0]["stages"]["05_publication"]
    proof = {"kind": "synthetic response replay through production functions", "external_requests": 0,
        "command": ["python", "scripts/upgrade3/manuscript_delivery.py", *command],
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "selected_manuscript": selected,
        "consumer_artifacts": publication["manuscript_checker"]["consumer_artifacts"],
        "publication_metadata": publication["publication_metadata"],
        "resume_cache_hit": results[1]["stages"]["03_manuscript_checker"]["cache_hit"],
        "resume_model_calls": results[1]["model_calls"], "scientific_acceptance": False}
    write("DELIVERY_ACCEPTANCE.json", proof)
    if source_hashes != {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_files}:
        raise ValueError("Source changed during evidence generation")
    write("SOURCE_RECEIPT.json", {"created_at": datetime.now(timezone.utc).isoformat(),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_hashes": source_hashes, "working_tree_implementation": True,
        "paid_calls": 0, "note": "Exact working-tree file hashes, not a claim of a clean committed run."})
    for lock in output.rglob(".writer_candidates.lock"):
        lock.unlink()

    print(json.dumps(proof, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    generate(args.output)
