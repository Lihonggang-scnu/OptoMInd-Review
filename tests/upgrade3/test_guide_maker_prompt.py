"""Offline instruction-presence and wiring checks, not model-quality evidence.

Phrase assertions only detect removal of reviewed prompt instructions. They do
not establish that a model preserves source roles, conditions, or good prose.
"""
import json
from pathlib import Path

import pytest

from test_guide_maker import book, config, execute, offline  # noqa: F401

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


@pytest.mark.parametrize('instructions', [
    pytest.param((
        '合并相近任务时保留独立解释、关键实验或对照、反例及改变比较含义的条件',
        '对合并或跨章复用时容易丢失、混淆的独立来源作用',
        '在 writing_arrangement 或 required_content 中用精确句柄标明其支撑的解释、对照或例外',
        '无需给所有来源逐项注解',
        '不把一个来源锁成单一用途',
        '不能只保留它的限制而丢掉已有贡献',
        '字段可选不代表内容责任可随意取消',
    ), id='content-and-source-roles'),
    pytest.param((
        '可能性不改成确定性',
        '特定对象、条件或测量不扩大到其他范围',
        '可以并存的解释不改成互相排斥',
        '正文作者再用原材料落实具体数字与引用',
        '不把指南当作新的事实证据',
    ), id='conditions-and-fact-boundary'),
    pytest.param((
        '全文安排与各章安排须一致',
        '说明表格保留哪些必要信息、邻近正文不再重复解释什么',
        '不能同时要求该章“只提概念”和完整分析同一结果',
        '不搬走后章独有内容',
    ), id='table-prose-and-chapter-consistency'),
    pytest.param((
        '不要让所有对象都套用同一套“背景→机制→应用→局限”流程',
        '不要新增“每段末尾报告验证状态”等统一仪式',
        '不自动推广到整章或全文',
        '仅示范决定的具体程度，不套用其章节或措辞',
        '不另输出核对表、审稿报告或思考过程',
    ), id='no-universal-template'),
    pytest.param((
        '已有信息足以决定时可以不补读',
        '不要预定论文数量',
        '不要求为每个小疑点追加审稿或补读',
        '不为追求完美追加无目的轮次',
        '若信息已有但写法尚空泛，就在本轮直接完善指南',
    ), id='no-forced-reading'),
    pytest.param((
        '输入中明确的全文引用范围或数量目标应在 manuscript_guide 中保留',
        '不机械平分到各章',
        '不把尚未写出的章节当作已完成指标',
        '未提供反馈时正常完成首次写作指南',
        '不能索要旧稿或依赖某个测试主题的已知答案',
    ), id='generic-feedback-contract'),
])
def test_reviewed_instructions_remain_present(instructions):
    text = PROMPT.read_text(encoding='utf-8')
    for instruction in instructions:
        assert instruction in text


def test_prompt_json_example_keeps_existing_wire_shape():
    # Parse the actual example rather than treating a schema-name phrase as
    # proof that its surrounding fields stayed unchanged.
    example_text = PROMPT.read_text(encoding='utf-8').split('输出 JSON：', 1)[1].lstrip()
    example, _ = json.JSONDecoder().raw_decode(example_text)
    assert set(example) == {'guide', 'reading_needs', 'complete', 'changes'}
    assert set(example['guide']) == {'schema_version', 'manuscript_guide', 'chapters'}
    assert example['guide']['schema_version'] == 'optomind.guided_body_guide.v1'
    assert len(example['guide']['chapters']) == 1
    assert set(example['guide']['chapters'][0]) == {
        'chapter_id', 'title', 'writing_arrangement', 'required_content', 'source_handles'}
    assert isinstance(example['guide']['chapters'][0]['required_content'], list)
    assert isinstance(example['guide']['chapters'][0]['source_handles'], list)
    assert len(example['reading_needs']) == 1
    assert set(example['reading_needs'][0]) == {'need_id', 'question', 'source_handles'}
    assert isinstance(example['reading_needs'][0]['source_handles'], list)
    assert example['complete'] is False
    assert isinstance(example['changes'], list)


def test_local_source_declaration_and_deferred_roles_are_explicit():
    # Instruction presence is not evidence of model compliance or role coverage.
    text = PROMPT.read_text(encoding='utf-8')
    assert '在该章 source_handles 中声明精确句柄' in text
    assert '须在接收章的 writing_arrangement 或 required_content 中实际安排' in text
    assert '某来源不足以支持某主张，不等于主张已被证伪' in text


def test_offline_maker_stage_loads_current_prompt_content(tmp_path, book, config):
    # Preview uses the real runtime stage builder without a provider factory.
    # This establishes prompt wiring only, not the quality of generated guides.
    result = execute(tmp_path, book, config, run=False)
    assert result['status'] == 'preview'
    assert result['model_calls'] == 0
    messages = json.loads(Path(result['stages'][0]['messages_path']).read_text(encoding='utf-8'))
    assert messages[0]['role'] == 'system'
    # Match the runtime's UTF-8 text read, including newline normalization on
    # Windows checkouts; compare the entire prompt, not selected phrases.
    assert messages[0]['content'] == PROMPT.read_text(encoding='utf-8')
