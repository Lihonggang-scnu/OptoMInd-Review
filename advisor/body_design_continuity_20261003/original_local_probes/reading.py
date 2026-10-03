from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree")
sys.path.insert(0, str(ROOT))

from optomind_research.runtime.upgrade3 import directed_reading as dr
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig,
    _directed_material_compatible,
    _directed_task_requirements,
    _directed_task_signature,
    make_directed_reading_runner,
    make_retrieval_loop_runner,
)


class FakeQwen:
    mode = "answer"
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def complete(self, messages, **kwargs):
        type(self).calls += 1
        if type(self).mode == "empty":
            content = {
                "question_material": [
                    {
                        "question_id": "Q1",
                        "examples": [],
                        "explanation": "",
                        "remaining_points": ["Not found"],
                    }
                ]
            }
        else:
            content = {
                "question_material": [
                    {
                        "question_id": "Q1",
                        "examples": [
                            {
                                "attribution": "本研究",
                                "use_in_review": "answer the directed question",
                                "finding": "A concrete finding",
                                "conditions": "demo conditions",
                                "reference_ids": [],
                            }
                        ],
                        "explanation": "Concrete explanation",
                        "remaining_points": [],
                    }
                ]
            }
        return {"content": content, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def write_snapshot(root: Path) -> tuple[Path, dict[str, object]]:
    snapshot = root / "snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "READING_VIEW.md").write_text(
        "# Demo\n\n## Abstract\n\nA small offline snapshot with substantive text.\n",
        encoding="utf-8",
    )
    (snapshot / "REFERENCES.json").write_text(
        json.dumps({"references": []}, ensure_ascii=False), encoding="utf-8"
    )
    card_dir = root / "card"
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "SOURCE_UNIT.json").write_text(
        json.dumps({"snapshot_path": str(snapshot)}, ensure_ascii=False), encoding="utf-8"
    )
    card = {
        "paper_identity": {"title": "Demo Paper", "paper_kind": "study"},
        "material": {"material_scope": "fulltext"},
        "general_understanding": {"summary": "demo"},
        "review_planning": {"use": "demo"},
    }
    card_path = card_dir / "P1_CARD.json"
    card_path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    candidate = {
        "_paper_id": "P1",
        "card_path": str(card_path),
        "title": "Demo Paper",
        "paper_kind": "study",
        "_b_summary": {"declared_content_depth": "fulltext"},
    }
    return snapshot, candidate


def task(question: object, gap: str, output_id: str = "O1") -> dict[str, object]:
    return {
        "paper_id": "P1",
        "chapter_ids": ["CH1"],
        "questions": [
            {
                "question_id": "Q1",
                "question": question,
                "purpose": "Supply a concrete attributed case.",
                "gap_key": gap,
            }
        ],
        "required_outputs": [
            {"output_id": output_id, "output_type": "practical_material", "description": "Answer"}
        ],
        "knowledge_gap": gap,
        "reason": gap,
    }


def config_for(root: Path, name: str) -> ProgressivePlannerConfig:
    out = root / name
    out.mkdir(parents=True, exist_ok=True)
    return ProgressivePlannerConfig(
        topic_id="review-demo",
        pool_path=out / "pool.json",
        plan_path=out / "plan.json",
        output_dir=out,
        shared_deep_read_budget=40,
        reader_workers=1,
        thinking_budget=1,
        chapter_output_tokens=128,
    )


def run_adapter_cases(root: Path, candidate: dict[str, object]) -> dict[str, object]:
    FakeQwen.mode = "answer"
    FakeQwen.calls = 0
    config = config_for(root, "r2_adapter")
    key_file = root / "fake-key.txt"
    key_file.write_text("offline-fake-boundary\n", encoding="utf-8")
    runner = make_directed_reading_runner(
        config,
        key_file=key_file,
        budget_ledger_path=config.output_dir / "budget.json",
        budget_limit_cny=1.0,
    )
    phase_dir = config.output_dir / "phase"
    common = {"phase": "directed", "output_dir": phase_dir, "plan": {"research_question": "demo"}, "pool_by_id": {"P1": candidate}}
    first = runner([task("What mechanism is reported?", "gap-a concrete evidence need")], **common)
    second = runner([task("Which limitation is reported?", "gap-b distinct evidence need")], **common)
    third = runner([task("What mechanism is reported?", "gap-a concrete evidence need", output_id="O2")], **common)
    string_task = task("A legal string question", "gap-string concrete evidence need")
    string_task["questions"] = ["A legal string question"]
    try:
        string_result = runner([string_task], **common)
    except Exception as exc:
        string_result = {"raised": f"{type(exc).__name__}:{exc}"}
    store = dr.DirectedReadingStore(config.output_dir / "directed_reading.sqlite")
    return {
        "first": first,
        "same_paper_new_question": second,
        "same_gap_changed_outputs": third,
        "purpose_string_runner": string_result,
        "fake_calls": FakeQwen.calls,
        "store_summary": store.summary(config.topic_id),
        "readings": store.readings(config.topic_id, "P1"),
        "output_dir": str(phase_dir / "directed" / "P1"),
    }


def run_empty_case(root: Path, candidate: dict[str, object]) -> dict[str, object]:
    FakeQwen.mode = "empty"
    FakeQwen.calls = 0
    config = config_for(root, "r3_reader")
    key_file = root / "fake-key.txt"
    runner = make_directed_reading_runner(
        config,
        key_file=key_file,
        budget_ledger_path=config.output_dir / "budget.json",
        budget_limit_cny=1.0,
    )
    phase_dir = config.output_dir / "phase"
    common = {"phase": "directed", "output_dir": phase_dir, "plan": {"research_question": "demo"}, "pool_by_id": {"P1": candidate}}
    t = task("What answer is available?", "gap-empty")
    first = runner([t], **common)
    second = runner([t], **common)
    output_dir = phase_dir / "directed" / "P1"
    artifact = (output_dir / "DIRECTED_READING.json").read_text(encoding="utf-8")
    material = first["results"][0].get("material") if first.get("results") else {}
    compatible = _directed_material_compatible(t, material)
    practical = dr.has_practical_content(material)
    raw_exists = (output_dir / "RAW_RESPONSE.json").is_file()
    # Remove only the derived result in this temporary directory. Keeping RAW_RESPONSE
    # demonstrates that the raw cache still prevents a fresh fake-reader call.
    (output_dir / "DIRECTED_READING.json").unlink()
    third = runner([t], **common)

    # Exercise the real external_closure path through the bounded retrieval runner.
    loop_config = config_for(root, "r3_loop")
    prior = dict(material)
    prior["paper_id"] = "P1"
    prior["_progressive_task_signature"] = _directed_task_signature(t)
    directed_calls = {"count": 0}

    def should_not_run(*args, **kwargs):
        directed_calls["count"] += 1
        return {"status": "fulfilled", "materials": []}

    loop_runner = make_retrieval_loop_runner(
        loop_config,
        allow_external=True,
        directed_reader=should_not_run,
        prior_readings=[prior],
    )
    loop = loop_runner(
        phase="directed",
        directed_requests=[t],
        pool_rows=[],
        plan={"research_question": "demo"},
        output_dir=loop_config.output_dir / "loop",
        resume=False,
    )
    return {
        "first": first,
        "second_same_output": second,
        "third_raw_only": third,
        "fake_calls": FakeQwen.calls,
        "raw_exists_before_retry": raw_exists,
        "has_practical_content_on_empty_material": practical,
        "directed_material_compatible_on_empty_material": compatible,
        "external_closure_loop": loop,
        "external_reader_calls": directed_calls["count"],
        "artifact_after_first": json.loads(artifact),
        "output_dir": str(output_dir),
    }


def main() -> None:
    base = Path(r"F:\OptoMind-Review-2\outputs\body_repair_review_verification_20261003\reading")
    base.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    snapshot, candidate = write_snapshot(run_root)
    original_qwen = dr.QwenDirectClient
    dr.QwenDirectClient = FakeQwen
    try:
        # Direct build path confirms the purpose compatibility regression independently.
        req = _directed_task_requirements({"questions": ["A legal string question"], "required_outputs": [{"output_id": "O1", "output_type": "practical_material", "description": "Answer"}]})
        purpose_error = ""
        try:
            dr.build_directed_request(
                review_id="review-demo",
                topic="demo",
                chapter={"chapter_id": "CH1", "title": "Demo"},
                questions=req["questions"],
                required_outputs=req["required_outputs"],
                candidates=[],
                topic_binding="progressive-review:review-demo",
            )
        except Exception as exc:
            purpose_error = f"{type(exc).__name__}:{exc}"
        result = {
            "run_root": str(run_root),
            "snapshot": str(snapshot),
            "purpose_requirements": req,
            "purpose_build_error": purpose_error,
            "r2": run_adapter_cases(run_root, candidate),
            "r3": run_empty_case(run_root, candidate),
        }
    finally:
        dr.QwenDirectClient = original_qwen
    (run_root / "RESULT.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
