"""Offline prompt contract checks; these do not prove model prose quality."""
import json
from pathlib import Path

from test_guide_maker import book, config, execute, offline  # noqa: F401

PROMPT = Path(__file__).resolve().parents[2] / 'prompts/guide_maker/generate.md'


def test_prompt_preserves_content_and_autonomous_organization():
    text = PROMPT.read_text(encoding='utf-8')
    for requirement in ('正文充分展开，篇幅服从解释需要', '主讲位置', '独立解释', '阴性结果',
                        '研究设计', '适用条件', 'source_brief_details', '原样装入',
                        '段落和小标题由作者自主安排', '读取数量由实际需求决定',
                        '没有旧稿或反馈也正常工作', '全文引用目标', '完整指南'):
        assert requirement in text
    for forbidden in ('FMT', 'LPS', 'STING', 'TME', 'III期', '菌群', '癌种',
                      'scientific_critic', '至少读取', '29个', '150篇', '精炼篇幅'):
        assert forbidden not in text
    assert len(text) < 3000


def test_prompt_json_example_adds_light_content_connections():
    example_text = PROMPT.read_text(encoding='utf-8').split('输出 JSON：', 1)[1].lstrip()
    example, _ = json.JSONDecoder().raw_decode(example_text)
    assert set(example) == {'guide', 'reading_needs', 'complete', 'changes'}
    chapter = example['guide']['chapters'][0]
    assert set(chapter) == {'chapter_id', 'title', 'writing_arrangement',
                            'required_content', 'source_handles', 'writing_units'}
    unit = chapter['writing_units'][0]
    assert set(unit) == {'unit_id', 'title', 'writing_arrangement', 'content_task_ids', 'source_handles'}
    assert isinstance(unit['content_task_ids'], list)
    assert example['complete'] is False


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
