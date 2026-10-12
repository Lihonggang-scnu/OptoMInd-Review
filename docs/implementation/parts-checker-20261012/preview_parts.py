"""Offline fixtures only: export exact new messages, never contact a model.

Run from the repository root with --output pointing at a NEW directory.
Historical answers test wiring, not the quality of the changed instructions.
"""
import argparse
import copy
import hashlib
import json
import shutil
from pathlib import Path

from optomind_research.runtime.upgrade3 import serial_manuscript_parts as serial
from optomind_research.runtime.upgrade3.serial_parts_application import extract_body


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def synthetic_cases():
    return [
        ("materials_map", "Which distinctions help select a surface?",
         "# Surfaces\r\n\r\n## Mechanism map\r\n\r\nPorous and smooth surfaces differ in interface geometry [lab::P0001].\r\n\r\n## Outlook\r\n\r\nDurability under repeated cleaning still needs direct comparison [lab::P0001].\r\n",
         {"audience": "Materials researchers", "article_type": "Classification review"},
         "lab::P0001", "Geometry distinguishes surfaces; durability remains conditional."),
        ("education_judgment", "When does delayed feedback support retention?",
         "# Feedback\n\n## Comparative evidence\n\nIn the synthetic classroom comparison, delayed feedback improved retention only when students first attempted retrieval [P0002].\n",
         {"audience": "Education researchers", "article_type": "Narrative evidence review",
          "confirmed_format": "Structured abstract with Scope and Interpretation; no abstract citations."},
         "P0002", "The judgment is conditional on a prior retrieval attempt."),
    ]


def make_fixture(statement, handle):
    part = {"purpose": "Support the reader's task", "focus": [statement],
            "boundary": ["Actual BODY evidence only"],
            "placement": {"mode": "standalone", "anchor": "manuscript_start"},
            "finalize_from": ["Actual BODY and its existing ending"]}
    return {"conception": {"manuscript_parts_plan": {"context": "Synthetic fixture, not model output",
             **{name: copy.deepcopy(part) for name in ("abstract", "introduction", "conclusion")}}},
            "conclusion": {"conclusion": statement + f" [{handle}]"},
            "introduction": {"introduction": "The reported distinction motivates the comparison. " + f"[{handle}]"},
            "abstract": {"title": "A conditional comparison", "abstract": statement,
                         "keywords": ["Comparison", "Conditions"]}}


def run_case(out, name, body, question, context, fixture, language, provenance):
    case = out / name
    case.mkdir(parents=True)
    draft = case / "BODY.md"
    draft.write_bytes(body.encode("utf8"))
    write(case / "CONTEXT.json", context)
    write(case / "FIXTURE.json", fixture)
    write(case / "PROVENANCE.json", provenance)
    report = serial.run_serial_parts(draft_path=draft, research_question=question,
        chapter_roles=[], out_dir=case / "run", context=context,
        parts_fixture_path=case / "FIXTURE.json", language=language)
    assert report["status"] == "generated", report
    assert report["external_requests"] == report["model_calls"] == 0
    final = Path(report["final_manuscript"]).read_bytes().decode("utf8")
    assert extract_body(final) == extract_body(body)
    prior = []
    hashes = {}
    for stage in serial.STAGE_ORDER:
        messages = json.loads((case / "run/messages" / f"{stage}.json").read_text())
        payload = json.loads(messages[1]["content"].split("【本轮输入】\n", 1)[1].split("\n【本轮任务】", 1)[0])
        assert payload["actual_body"] == extract_body(body)
        assert list(payload.get("prior_generated_parts_secondary_not_evidence", {})) == prior
        if stage == "conclusion":
            prior.append("conclusion")
        elif stage == "introduction":
            prior.append("introduction")
        hashes[stage] = serial.messages_sha256(messages)
    return {"case": name, "status": report["status"], "model_calls": 0,
            "body_preserved": True, "messages_sha256": hashes, "scientific_acceptance": False}


def export(output, repo):
    if output.exists():
        raise ValueError("Use a new output directory; existing evidence is never overwritten")
    output.mkdir(parents=True)
    reports = []
    for name, question, body, requirements, handle, statement in synthetic_cases():
        context = {"writing_requirements": requirements,
                   "source_identity_map": {handle: {"title": "Synthetic source", "paper_id": name}}}
        reports.append(run_case(output, name, body, question, context,
            make_fixture(statement, handle), "en", {"kind": "labeled_synthetic_fixture", "not_model_output": True}))
    archive = repo / "docs/acceptance/serial-parts-live-20261011"
    attempt = archive / "03_front_back/attempts/attempt_003"
    plan = json.loads((attempt / "MANUSCRIPT_PARTS_PLAN.json").read_text())
    parts = json.loads((attempt / "GENERATED_PARTS.json").read_text())
    original_messages = json.loads((archive / "03_front_back/cache/conception/MESSAGES.json").read_text())
    payload = json.loads(original_messages[1]["content"].split("【本轮输入】\n")[1].split("\n【本轮任务】")[0])
    context = {k: v for k, v in payload["context"].items() if k in serial._CONTEXT_KEYS}
    fixture = {"conception": {"manuscript_parts_plan": plan},
        "conclusion": {"conclusion": parts["conclusion"]},
        "introduction": {"introduction": parts["introduction"]},
        "abstract": {k: parts[k] for k in ("title", "abstract", "keywords")}}
    body = (archive / "input/SELECTED_BODY_HANDLES.md").read_bytes().decode("utf8")
    reports.append(run_case(output, "historical_response_fixture", body,
        payload["context"]["research_question"], context, fixture, "zh",
        {"kind": "historical_answers_reused_as_labeled_fixture",
         "source": str(attempt.relative_to(repo)),
         "warning": "Old answers were NOT generated from these new messages. Wiring only; no quality claim.",
         "source_body_sha256": hashlib.sha256(body.encode("utf8")).hexdigest()}))
    write(output / "PARTS_ACCEPTANCE.json", {"prompt_contract_sha256": serial.prompt_contract_sha256(),
        "external_requests": 0, "scientific_acceptance": False, "cases": reports})
    return reports


def summarize(full_output, summary_output):
    """Persist compact review evidence; full_output retains exact runtime requests."""
    if summary_output.exists():
        raise ValueError("Use a new summary directory")
    summary_output.mkdir(parents=True)
    for name, *_ in synthetic_cases():
        shutil.copytree(full_output / name, summary_output / name)
    history = full_output / "historical_response_fixture"
    target = summary_output / "historical_response_fixture"
    target.mkdir()
    for stage in serial.STAGE_ORDER:
        messages = json.loads((history / f"run/messages/{stage}.json").read_text())
        payload_text, task = messages[1]["content"].split("\n【本轮任务】\n", 1)
        payload = json.loads(payload_text.split("【本轮输入】\n", 1)[1])
        write(target / f"{stage}_MESSAGE_PREVIEW.json", {
            "kind": "abridged_preview_not_exact_request", "stage": stage,
            "actual_messages_sha256": serial.messages_sha256(messages),
            "system": messages[0]["content"], "task": task,
            "body_reference": "docs/acceptance/serial-parts-live-20261011/input/SELECTED_BODY_HANDLES.md",
            "actual_body_sha256": payload["body_sha256"],
            "actual_body_chars": len(payload["actual_body"]),
            "context_keys": list(payload["context"]),
            "source_identity_count": len(payload["context"]["source_identity_map"]),
            "material_records_count": len(payload["context"]["material_records"]),
            "has_current_conception": "manuscript_parts_plan" in payload,
            "prior_parts": list(payload.get("prior_generated_parts_secondary_not_evidence", {})),
            "full_payload_reproduction": "Run preview_parts.py --output NEW_DIRECTORY"})
    shutil.copy2(history / "PROVENANCE.json", target / "PROVENANCE.json")
    shutil.copy2(full_output / "PARTS_ACCEPTANCE.json", summary_output / "PARTS_ACCEPTANCE.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, help="Optional NEW compact evidence directory")
    args = parser.parse_args()
    export(args.output.resolve(), Path.cwd().resolve())

    if args.summary_output:
        summarize(args.output.resolve(), args.summary_output.resolve())
