"""Offline prompt guardrails, not an assessment of model writing quality."""
from pathlib import Path

PROMPT = Path(__file__).resolve().parents[2] / 'prompts/guide_maker/generate.md'


def test_prompt_requires_specific_decisions_without_new_schema_or_fixed_read_count():
    text = PROMPT.read_text(encoding='utf-8')
    for requirement in ('具体对象、关系、条件和对照', '主要展开的位置', '只简短回指',
                        '比较条件', '研究设计', '前章使读者理解了什么',
                        '尚未确定的写作选择', '章节齐全', '可以不补读', '不要预定论文数量'):
        assert requirement in text
    assert '不另建任务对应表或固定小节模板' in text
    assert 'schema_version": "optomind.guided_body_guide.v1"' in text
    for forbidden in ('FMT', 'LPS', 'STING', 'TME', 'III期', '菌群', '癌种',
                      'task_map', 'scientific_critic', '固定阅读', '至少读取'):
        assert forbidden not in text
