"""Direct tests for the reusable staged runner; no network or Qwen client."""
import importlib.util
import json
from pathlib import Path

from optomind_research.runtime.upgrade3 import serial_manuscript_parts as serial


RUNNER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "upgrade3" / "post_body_runner.py"
spec = importlib.util.spec_from_file_location("post_body_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(runner)


BODY = "# 题目\n\n## 正文\n\n关系 A 在条件 B 下成立 [P0001]。\n"


def context():
    return {"research_question": "什么决定关系 A？", "shared_scope": {"statement": "关系 A 的实际正文范围"},
            "source_identity_map": {"P0001": {"paper_id": "one", "title": "One"}}}


def plan():
    part = {"purpose": "完成读者认识任务", "focus": ["正文认识"], "boundary": ["保留条件"],
            "placement": {"mode": "standalone", "anchor": "正文"}, "finalize_from": ["正文"]}
    return {"context": "读者与用途", "abstract": dict(part), "introduction": dict(part), "conclusion": dict(part)}


def response(stage, *, scalar_conception=False):
    if stage == "conception":
        card = plan()
        if scalar_conception:
            for name in ("abstract", "introduction", "conclusion"):
                card[name]["boundary"] = card[name]["boundary"][0]
        value = {"manuscript_parts_plan": card}
    elif stage == "conclusion":
        value = {"conclusion": "条件 B 下的关系 A。 [P0001]"}
    elif stage == "introduction":
        value = {"introduction": "问题与范围由条件 B 限定。 [P0001]"}
    else:
        value = {"title": "关系 A", "abstract": "条件 B 下的关系 A。 [P0001]", "keywords": ["关系 A"]}
    return {"content": json.dumps(value, ensure_ascii=False), "finish_reason": "stop"}


def execute(tmp_path, *, stop_after=None, mode="full", fake=None):
    draft = tmp_path / "BODY.md"
    draft.write_text(BODY, encoding="utf-8")
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context(), ensure_ascii=False), encoding="utf-8")
    return runner.execute_live(draft=draft, context_path=context_path, run_root=tmp_path / "run",
        stop_after_stage=stop_after, prior_parts_mode=mode, model="qwen3.5-plus", output_tokens=12000,
        thinking_budget=4000, timeout_seconds=900, max_retries=2, key_file=None, ledger=None,
        ledger_limit_cny=30, reuse_cache=True, client_factory=fake)


def test_runner_pause_resume_reuses_earlier_stages_and_counts_only_new_calls(tmp_path):
    calls = []
    call_ids = []

    def fake(stage, messages, call_id):
        calls.append(stage)
        call_ids.append(call_id)
        return response(stage)

    first = execute(tmp_path, stop_after="conception", fake=fake)
    assert first["status"] == "awaiting_review"
    assert first["generated"] == ["conception"]
    second = execute(tmp_path, stop_after="conclusion", fake=fake)
    assert second["status"] == "awaiting_review"
    assert second["generated"] == ["conception", "conclusion"]
    third = execute(tmp_path, fake=fake)
    assert third["status"] == "generated"
    assert third["generated"] == list(runner.STAGE_ORDER)
    fourth = execute(tmp_path, fake=fake)
    assert fourth["status"] == "generated"
    assert calls == ["conception", "conclusion", "introduction", "abstract"]
    assert all(":run:" in call_id for call_id in call_ids)
    assert all("attempt-" in call_id for call_id in call_ids)
    assert third["body_preserved"] is True
    assert fourth["body_preserved"] is True


def test_runner_full_to_none_only_calls_changed_downstream_stages(tmp_path):
    calls = []

    def fake(stage, messages, call_id):
        calls.append(stage)
        return response(stage)

    full = execute(tmp_path, fake=fake)
    assert full["status"] == "generated"
    before = len(calls)
    none = execute(tmp_path, mode="none", fake=fake)
    assert none["status"] == "generated"
    assert calls[before:] == ["introduction", "abstract"]


def test_runner_persists_parse_failure_then_reuses_same_response_after_offline_parser_repair(tmp_path, monkeypatch):
    calls = []

    original_validate = serial.validate_manuscript_parts_plan

    def old_validate(value):
        if isinstance(value, dict):
            for name in ("abstract", "introduction", "conclusion"):
                part = value.get(name)
                if isinstance(part, dict) and isinstance(part.get("boundary"), str):
                    raise serial.SerialPartsError("old_parser_boundary_scalar")
        return original_validate(value)

    monkeypatch.setattr(serial, "validate_manuscript_parts_plan", old_validate)

    def old_parser_provider(stage, messages, call_id):
        calls.append(stage)
        return response(stage, scalar_conception=(stage == "conception"))

    failed = execute(tmp_path, stop_after="conception", fake=old_parser_provider)
    assert failed["status"] == "failed"
    assert calls == ["conception"]
    event = json.loads((tmp_path / "run" / "attempts" / "attempt-001" / "RUN_EVENTS.json").read_text(encoding="utf-8"))
    cache_path = Path(event["cache_events"][0]["cache"])
    record = json.loads(cache_path.read_text(encoding="utf-8"))
    original_response = record["response"]
    original_content = json.loads(original_response["content"])
    assert isinstance(original_content["manuscript_parts_plan"]["abstract"]["boundary"], str)

    # Restore the current compatibility parser. The second attempt must use
    # the exact raw response persisted before the old parser rejected it.
    monkeypatch.setattr(serial, "validate_manuscript_parts_plan", original_validate)

    def should_not_call(stage, messages, call_id):
        raise AssertionError("repaired response must be reused without a new call")

    recovered = execute(tmp_path, stop_after="conception", fake=should_not_call)
    assert recovered["status"] == "awaiting_review"
    assert recovered["generated"] == ["conception"]
    recovered_record = json.loads(cache_path.read_text(encoding="utf-8"))
    assert recovered_record["response"] == original_response
    recovered_content = json.loads(recovered_record["response"]["content"])
    assert isinstance(recovered_content["manuscript_parts_plan"]["abstract"]["boundary"], str)


def test_preview_forwards_language_and_context_chapter_roles(tmp_path):
    draft = tmp_path / "BODY.md"
    draft.write_text(BODY, encoding="utf-8")
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({**context(), "chapter_roles": [
        {"chapter_id": "CH01", "title": "理论与证据", "role": "body"}
    ]}, ensure_ascii=False), encoding="utf-8")
    out_dir = tmp_path / "preview"
    result = runner.preview(draft=draft, context_path=context_path, out_dir=out_dir,
                            stage="conception", prior_parts_mode="full", language="en")
    messages = json.loads((out_dir / "PREVIEW" / "conception-full.messages.json").read_text(encoding="utf-8"))
    payload = json.loads(messages[1]["content"].split("【本轮任务】", 1)[0].split("\n", 1)[1])
    assert payload["language"] == "en"
    assert payload["context"]["chapter_roles"] == [{"chapter_id": "CH01", "title": "理论与证据", "role": "body"}]
    assert result["provider_calls"] == 0
