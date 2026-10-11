"""Invoke production final citations/publication from actual serial output."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parent
WORK = Path("F:/OptoMind-Review-2/outputs/outline_to_body_astra_20261010_60cny/worktree")
sys.path.insert(0, str(WORK))
from optomind_research.runtime.upgrade3.dual_budget import read_json, write_json
from optomind_research.runtime.upgrade3.review_delivery import (
    _post_body_delivery_catalogs, _post_body_numbering_input, run_publication_delivery,
)
from optomind_research.runtime.upgrade3.delivery_citations import run_figures_citations_stage

parser = argparse.ArgumentParser()
parser.add_argument("--stage", choices=("citations", "publication"), required=True)
args = parser.parse_args()
parts = read_json(ROOT / "REPLAY_REPORT.json")
if parts["status"] != "generated" or not parts["runtime_report"]["body_preserved"]:
    raise SystemExit("serial parts not ready")
if args.stage == "citations":
    source = Path(parts["final_manuscript"])
    actual, projection = _post_body_numbering_input(source, ROOT, [])
    catalogs = _post_body_delivery_catalogs(
        read_json(ROOT / "03_front_back/IDENTITY_CATALOGS.json"),
        parts["runtime_report"]["source_identity_map"])
    result = run_figures_citations_stage(final_draft_path=actual,
        identity_catalogs=catalogs, out_dir=ROOT / "04_figures_citations", figure_assets=[])
    result["input_projection"] = projection
    result["source_body_delivery"] = parts["source_body_delivery"]
    result["scientific_acceptance"] = False
    write_json(ROOT / "04_figures_citations/STAGE_REPORT.json", result)
else:
    numbering = read_json(ROOT / "04_figures_citations/STAGE_REPORT.json")
    if numbering["status"] != "complete":
        raise SystemExit("final citations not ready")
    result = run_publication_delivery(reader_draft_path=numbering["reader_draft"],
        references_path=numbering["references_path"], figure_map_path=numbering["mapping_path"],
        assets_dir=numbering["assets_dir"], out_dir=ROOT / "05_publication",
        language="zh", compile_pdf=True)
    result["source_body_delivery"] = parts["source_body_delivery"]
    result["scientific_acceptance"] = False
    result["quality_designation"] = "本轮真实链路实测样稿；保留首尾内容问题和原 BODY 科学诊断，不是质量定稿"
    write_json(ROOT / "05_publication/DELIVERY_REPORT.json", result)
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result["status"] == "complete" else 1)
