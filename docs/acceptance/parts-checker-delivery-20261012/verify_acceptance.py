"""Read-only audit of real provider artifacts and selected-manuscript consumers.

Writes audit receipts only; never calls a model or changes generated material.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
WORKTREE = Path(r"F:\OptoMind-Review-2\outputs\outline_to_body_astra_20261010_60cny\worktree")
sys.path.insert(0, str(WORKTREE))
from optomind_research.runtime.upgrade3.serial_parts_application import extract_body
from optomind_research.runtime.upgrade3.serial_parts_live import ledger_snapshot


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def text_digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


parts = read(ROOT / "live_parts/LIVE_REPORT.json")["runtime_report"]
source = Path(parts["source_draft"])
assembled = Path(parts["final_manuscript"])
assert digest(source) == "8ef37120cc8842cb06d5a583a135e136676e4d778221d14f7c068cc1d26a8245"
original_text = source.read_bytes().decode("utf-8")
assembled_text = assembled.read_bytes().decode("utf-8")
assert parts["body_preserved"] is True
assert extract_body(assembled_text) == extract_body(original_text)
assert assembled_text.index("## 结语") < assembled_text.index("## 参考文献")

delivery_path = ROOT / "live_checker_after_seam_fix/MANUSCRIPT_DELIVERY_REPORT.json"
delivery = read(delivery_path)
checker = read(ROOT / "live_checker_after_seam_fix/03_manuscript_checker/CHECKER_REPORT.json")
selected = delivery["selected_manuscript"]
assert delivery["downstream_status"] == "complete"
assert digest(selected["path"]) == selected["sha256"] == checker["selected_manuscript"]["sha256"]
assert checker["source_sha256"] == digest(assembled)
assert digest(checker["original_snapshot"]) == digest(assembled)

current = assembled_text
for step in checker["rounds"]:
    folder = Path(step["response_path"]).parent
    receipt = read(folder / "EXECUTION_RECEIPT.json")
    assert text_digest(current) == receipt["input_sha256"]
    for patch in sorted(receipt["applied"], key=lambda p: p["start"], reverse=True):
        assert current[patch["start"]:patch["end"]] == patch["original_text"]
        current = current[:patch["start"]] + patch["replacement_text"] + current[patch["end"]:]
    assert text_digest(current) == receipt["output_sha256"]
assert current.encode("utf-8") == Path(selected["path"]).read_bytes()
identity = checker["source_identity_changes"]
assert not identity["removed_tokens"] and not identity["added_tokens"]
assert not identity["unknown_handles"]
assert {v["canonical"] for v in identity["before"].values()} == {v["canonical"] for v in identity["after"].values()}

stage4 = delivery["stages"]["04_figures_citations"]
stage5 = delivery["stages"]["05_publication"]
assert stage4["selected_manuscript"]["sha256"] == selected["sha256"]
assert stage5["selected_manuscript"]["sha256"] == selected["sha256"]
sidecar = read(stage5["manuscript_selection_path"])
assert sidecar["selected_manuscript"]["sha256"] == selected["sha256"]
refs = read(stage4["references_path"])
assert len(refs["references"]) == stage4["reference_count"] == 187
assert not stage4["unknown_citation_tokens"] and not stage4["unknown_identity_handles"]
reader = Path(stage4["reader_draft"])
published = Path(stage5["published_markdown"])
assert reader.read_bytes() == published.read_bytes()
assert published.read_text(encoding="utf-8").index("## 结语") < published.read_text(encoding="utf-8").index("## 参考文献")

project = ROOT.parent / "outline_to_body_astra_20261010_60cny/budget.sqlite"
account = Path(r"F:\OptoMind-Review-2\local_accounting\qwen_first_20261010_300cny\budget.sqlite")
ps = ledger_snapshot(project, 60)
acs = ledger_snapshot(account, 300)
assert ps["held_cny"] == acs["held_cny"] == 0
spent = ps["actual_cny"] - 17.9225288
assert spent <= 20
assert abs(spent - (acs["actual_cny"] - 7.8400716)) < 1e-8
connection = sqlite3.connect(project.as_uri() + "?mode=ro", uri=True)
calls = []
for group, paths in [
    ("parts", sorted((ROOT / "live_parts/cache").glob("*/RAW_RESPONSE.json"))),
    ("checker_before_seam_fix", sorted((ROOT / "live_checker/03_manuscript_checker").glob("round_*/RAW_RESPONSE.json"))),
    ("checker_after_seam_fix", sorted((ROOT / "live_checker_after_seam_fix/03_manuscript_checker").glob("round_*/RAW_RESPONSE.json"))),
]:
    for path in paths:
        raw = read(path)
        prefix = raw["call_id"] + ":"
        fees = connection.execute("select actual_cny,status,call_id from reservations where substr(call_id,1,?)=? and substr(call_id,-8)=':project' and request_id=?", (len(prefix), prefix, raw["request_id"])).fetchall()
        assert len(fees) == 1
        fee = fees[0]
        assert fee[1] == "settled"
        calls.append({"group": group, "stage": path.parent.name, "call_id": raw["call_id"],
                      "ledger_call_id": fee[2], "actual_cny": fee[0], "returned_model": raw["returned_model"],
                      "usage": raw["usage"], "elapsed_seconds": raw["elapsed_seconds"],
                      "raw_path": str(path), "raw_sha256": digest(path)})
connection.close()
assert len({c["call_id"] for c in calls}) == len(calls)
assert all(c["returned_model"] == "qwen3.5-plus" for c in calls)
assert abs(sum(c["actual_cny"] for c in calls) - spent) < 1e-8

receipt = {
    "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=WORKTREE, text=True).strip(),
    "original_body": {"path": str(source), "sha256": digest(source), "unchanged": True},
    "parts": {"path": str(assembled), "sha256": digest(assembled), "body_projection_preserved": True,
              "conclusion_before_references": True},
    "checker": {"status": checker["status"], "applied_count": len(checker["applied"]),
                "pending_count": len(checker["pending"]), "unresolved_count": len(checker["unresolved_questions"]),
                "selected": selected, "exact_patch_reconstruction": True, "source_identities_preserved": True},
    "downstream": {"selected_path_and_hash_consumed": True, "reference_count": 187,
                   "reader_equals_published": True, "published_path": str(published),
                   "published_sha256": digest(published), "pdf_requested": False},
    "budget": {"session_limit_cny": 20, "session_initial_actual_cny": 17.9225288,
               "session_actual_cny": spent, "session_held_cny": 0, "session_remaining_cny": 20-spent,
               "project": ps, "account": acs, "physical_calls": calls,
               "note": "Project and account ledgers mirror the same physical fees; never sum them. Interleaved stage report deltas are not standalone stage fees."},
}
(ROOT / "FINAL_ACCEPTANCE.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
print(json.dumps({"source_sha":receipt["source_sha"],"checker":{k:v for k,v in receipt['checker'].items() if k!='selected'},
                  "physical_calls":len(calls),"spent_cny":spent,"remaining_cny":20-spent,
                  "published_path":str(published)},ensure_ascii=False,indent=2))
