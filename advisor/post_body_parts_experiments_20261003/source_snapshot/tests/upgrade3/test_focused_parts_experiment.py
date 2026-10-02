"""Offline checks for the one-stage neutral opening experiment."""
import importlib.util
import json
from pathlib import Path

from optomind_research.runtime.upgrade3.serial_parts_application import extract_body


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "upgrade3" / "focused_parts_experiment.py"
spec = importlib.util.spec_from_file_location("focused_parts_experiment", SCRIPT)
experiment = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(experiment)


def source_attempt(tmp_path):
    source = tmp_path / "attempt-006"
    (source / "messages").mkdir(parents=True)
    payload = {
        "actual_body": "# 正文\n\n## CH01\n\n关系 A 在条件 B 下成立 [P0001]。\n",
        "body_sha256": "body",
        "context": {"research_question": "q", "chapter_roles": [], "allowed_source_handles": ["P0001"]},
        "language": "zh",
        "manuscript_parts_plan": {
            "context": "读者任务",
            "abstract": {"purpose": "a", "focus": ["a"], "boundary": ["a"], "placement": {"mode": "standalone", "anchor": "front"}, "finalize_from": ["正文"]},
            "introduction": {"purpose": "i", "focus": ["引言作用"], "boundary": ["i"], "placement": {"mode": "standalone", "anchor": "front"}, "finalize_from": ["引言"]},
            "conclusion": {"purpose": "c", "focus": ["c"], "boundary": ["c"], "placement": {"mode": "standalone", "anchor": "end"}, "finalize_from": ["正文"]},
        },
    }
    messages = [
        {"role": "system", "content": "生成 introduction；不要包含引言标题。"},
        {"role": "user", "content": "【本轮输入】\n" + json.dumps(payload, ensure_ascii=False) + "\n【本轮任务】\n旧任务"},
    ]
    (source / "messages" / "introduction.json").write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    draft = tmp_path / "BODY.md"
    draft.write_text(payload["actual_body"], encoding="utf-8")
    (source / "INPUT.json").write_text(json.dumps({"source_draft": str(draft)}, ensure_ascii=False), encoding="utf-8")
    return source, payload["actual_body"]


def test_neutral_messages_preserve_body_and_remove_labels(tmp_path):
    source, body = source_attempt(tmp_path)
    messages, info = experiment.build_neutral_messages(source)
    all_text = json.dumps(messages, ensure_ascii=False)
    assert "引言" not in all_text
    assert "introduction" not in all_text.casefold()
    assert info["body_sha256"] == __import__("hashlib").sha256(body.encode()).hexdigest()
    assert info["body_target_counts"] == {"引言": 0, "introduction": 0}
    assert info["prompt_target_counts_excluding_body"] == {"引言": 0, "introduction": 0}
    payload = info["payload"]
    assert "opening_text" in payload["manuscript_parts_plan"]
    assert payload["actual_body"] == body


def test_neutral_response_converts_and_assembles_without_body_change(tmp_path):
    source, body = source_attempt(tmp_path)
    _messages, info = experiment.build_neutral_messages(source)
    response = {"content": json.dumps({"opening_text": "开篇正文 [P0001]"}, ensure_ascii=False), "finish_reason": "stop"}
    canonical = experiment.normalize_opening_response(response, allowed_source_handles=["P0001"])
    assert canonical == {"introduction": "开篇正文 [P0001]"}
    from optomind_research.runtime.upgrade3.serial_parts_application import apply_serial_parts
    manuscript = Path(json.loads((source / "INPUT.json").read_text(encoding="utf-8"))["source_draft"]).read_text(encoding="utf-8")
    assembled, _log = apply_serial_parts(manuscript, canonical, [], language="zh")
    assert extract_body(assembled) == extract_body(manuscript)
    assert "开篇正文 [P0001]" in assembled
