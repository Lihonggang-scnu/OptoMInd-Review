"""Writing instructions and explicit-input contracts, not model quality claims."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import serial_manuscript_parts as serial


def exporter():
    root = Path(__file__).resolve().parents[2]
    path = root / "docs/implementation/parts-checker-20261012/preview_parts.py"
    spec = importlib.util.spec_from_file_location("parts_preview", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return root, module


def test_two_domains_and_historical_answers_export_actual_messages(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("Network forbidden"))
    root, module = exporter()
    output = tmp_path / "evidence"
    reports = module.export(output, root)
    assert len(reports) == 3
    assert all(r["body_preserved"] and not r["scientific_acceptance"] for r in reports)
    for report in reports:
        name = report["case"]
        for stage in serial.STAGE_ORDER:
            messages = json.loads((output / name / f"run/messages/{stage}.json").read_text())
            task = messages[1]["content"].split("\n【本轮任务】\n", 1)[1]
            assert task == (serial._CONCEPTION if stage == "conception" else serial._DUTIES[stage])
            assert "BIO01" not in task and "FMT" not in task and "MAT05" not in task
            if name != "historical_response_fixture":
                assert '"writing_requirements"' in messages[1]["content"]
    provenance = json.loads((output / "historical_response_fixture/PROVENANCE.json").read_text())
    assert "NOT generated" in provenance["warning"]
    compact = tmp_path / "compact"
    module.summarize(output, compact)
    preview = json.loads((compact / "historical_response_fixture/introduction_MESSAGE_PREVIEW.json").read_text())
    assert preview["kind"] == "abridged_preview_not_exact_request"
    assert preview["prior_parts"] == ["conclusion"]
    assert preview["actual_messages_sha256"] == reports[-1]["messages_sha256"]["introduction"]
    assert not (compact / "historical_response_fixture/BODY.md").exists()
    with pytest.raises(ValueError, match="new output"):
        module.export(output, root)


def test_writing_methods_are_flexible_and_support_real_citations():
    intro = serial._DUTIES["introduction"]
    conclusion = serial._DUTIES["conclusion"]
    abstract = serial._DUTIES["abstract"]
    assert "不是固定段落模板" in intro and "不设每段引文配额" in intro
    assert "身份目录不是支持证据" in intro and "许可引用句柄放在对应主张旁" in intro
    assert "已有充分 Outlook" in conclusion and "不固定段数" in conclusion
    assert "并置证据不能补成因果链" in conclusion
    assert "分类轴" in abstract and "不先删改变科学含义" in abstract
    assert "叙述综述也可能结构化" in abstract
    assert all("第一段" not in duty for duty in serial._DUTIES.values())


@pytest.mark.parametrize("requirements", [[], "journal", {"unknown": "x"}, {"audience": ""},
    {"confirmed_format": 4}, {"audience": "x" * 10_001},
    {"audience": "x" * 6000, "confirmed_format": "y" * 6000}])
def test_requirements_reject_unbounded_or_ambiguous_inputs(requirements):
    with pytest.raises(serial.SerialPartsError):
        serial.normalize_context({"writing_requirements": requirements},
            research_question="q", body_text="Actual prose.")


def test_requirements_preserve_explicit_text_and_change_message_binding():
    context = {"writing_requirements": {"article_type": "Narrative review",
               "confirmed_format": "Scope and Interpretation; no abstract citations"}}
    original = copy.deepcopy(context)
    first = serial.build_stage_messages("conception", body_text="Actual prose.",
        research_question="q", context=context)
    assert context == original
    payload = json.loads(first[1]["content"].split("【本轮输入】\n")[1].split("\n【本轮任务】")[0])
    assert payload["context"]["writing_requirements"] == context["writing_requirements"]
    context["writing_requirements"]["confirmed_format"] = "Unstructured abstract"
    second = serial.build_stage_messages("conception", body_text="Actual prose.",
        research_question="q", context=context)
    assert serial.messages_sha256(first) != serial.messages_sha256(second)


def test_each_duty_changes_complete_prompt_contract(monkeypatch):
    before = serial.prompt_contract_sha256()
    for stage in serial._DUTIES:
        with monkeypatch.context() as patch:
            patch.setitem(serial._DUTIES, stage, serial._DUTIES[stage] + " A changed instruction.")
            assert serial.prompt_contract_sha256() != before
