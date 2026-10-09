"""General delivery requirements, independent of any review topic."""
from pathlib import Path

def test_explicit_requirements_override_default_without_citation_stuffing():
    prompt = (Path(__file__).resolve().parents[2] / "prompts/guided_body_writer/writer.md").read_text(encoding="utf-8")
    assert "未明确指定时" in prompt
    assert "指南中明确的全文交付要求应保留并落实" in prompt
    assert "去重文献身份" in prompt
    assert "不机械平分到各章" in prompt
    assert "不把尚未写出的引言或后章计入已完成指标" in prompt
    assert "150" not in prompt

def test_prose_wrapper_distinguished_from_legitimate_code_and_metadata():
    prompt = (Path(__file__).resolve().parents[2] / "prompts/guided_body_writer/writer.md").read_text(encoding="utf-8")
    assert "不要给整章正文包裹外层代码围栏" in prompt
    assert "代码示例可保留各自代码块" in prompt
    assert "```guide_writer_metadata" in prompt
