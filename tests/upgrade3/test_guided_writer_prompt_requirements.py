"""Compact, positive authoring requirements independent of a review topic."""
from pathlib import Path

PROMPTS = Path(__file__).resolve().parents[2] / 'prompts/guided_body_writer'


def test_explicit_requirements_and_actual_citation_identity_count():
    prompt = (PROMPTS / 'writer.md').read_text(encoding='utf-8')
    assert '落实指南的全文引用要求' in prompt
    assert '已写正文中实际使用的去重文献身份' in prompt
    assert '按论证需要分配来源' in prompt
    assert '150' not in prompt


def test_natural_markdown_and_machine_metadata_are_separate():
    prompt = (PROMPTS / 'writer.md').read_text(encoding='utf-8')
    assert '直接可渲染的 Markdown' in prompt
    assert '代码示例使用各自的代码块' in prompt
    assert '```guide_writer_metadata' in prompt
    assert '"read_source_handles"' in prompt
    assert '"body_markdown"' in prompt


def test_both_author_and_completion_check_facts_and_preserve_explanation():
    for name in ('writer.md', 'completion.md'):
        prompt = (PROMPTS / name).read_text(encoding='utf-8')
        for requirement in ('逐句', '对象', '分组', '条件', '时间锚点', '数值分母',
                            '结果方向', '表格每行', '实验对照', '反例', '[P'):
            assert requirement in prompt, (name, requirement)
        assert '不要' not in prompt
        assert '而是' not in prompt
        assert '前列腺' not in prompt
        assert 'ICI' not in prompt
    author = (PROMPTS / 'writer.md').read_text(encoding='utf-8')
    assert '材料自身有分歧' in author
    assert '精炼篇幅' in author
    assert '共同问题' in author


def test_metadata_decision_handles_actual_gaps_without_rewriting():
    prompt = (PROMPTS / 'metadata.md').read_text(encoding='utf-8')
    assert '原正文逐字保留' in prompt
    assert '有效的未完成声明及已有缺口继续保留' in prompt
    assert '合理的组织、标题和表述变化' in prompt
    assert '"complete":true' in prompt
    assert '"remaining_content":[]' in prompt
