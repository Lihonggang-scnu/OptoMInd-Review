"""Offline one-round review safety and provenance; never performs paid calls."""
from copy import deepcopy
import json
import socket

import pytest

from optomind_research.runtime.upgrade3 import guide_review as engine
from optomind_research.runtime.upgrade3.guide_review_contracts import parse_review, parse_revision
from optomind_research.runtime.upgrade3.fullbody_contracts import build_fullbody_input
from optomind_research.runtime.upgrade3.writer_candidates_contracts import INPUT_SCHEMA


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*a, **kw):
        raise AssertionError('network prohibited')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


@pytest.fixture
def book():
    return build_fullbody_input([dict(schema_version=INPUT_SCHEMA, chapter_id=f'C{i}', language='en',
        chapter_frame={'title': f'Chapter {i}', 'research_question': 'Explain observations'}, other_chapters=[],
        units=[dict(unit_id='U', focus='Explain', paragraph_tasks=[dict(paragraph_id='T1', point='Original task',
            source_handles=[f'P{i:04d}'])], table_tasks=[], owner_unit_context={}, source_handles=[f'P{i:04d}'])],
        sources=[dict(source_handle=f'P{i:04d}', paper_id=f'paper-{i}', study_summary_A={'finding': f'INTACT_EVIDENCE_{i}',
            'conditions': {'temperature': 321, 'limitation': 'complete conditions'}})],
        chapter_tool_materials=[], provenance={}, warnings=[]) for i in (1, 2)])


@pytest.fixture
def guide(book):
    return {'manuscript_guide': 'Explain observations with their conditions.', 'chapters': [
        dict(chapter_id=c['chapter_id'], title='Chapter', writing_arrangement='Develop the relationship in natural prose.',
             required_content=['Explain the boundary.'], source_handles=[f'P{i:04d}']) for i, c in enumerate(book['chapters'], 1)]}


@pytest.fixture
def config():
    def profile(model):
        return dict(model=model, thinking=False, max_output_tokens=4096, stream=True,
                    timeout_seconds=15, stream_overall_timeout_seconds=30,
                    prompt_token_multiplier=1.0, prompt_token_framing_margin=0)
    return dict(reviewer=profile('qwen3.5-plus'), reviser=profile('qwen3.8-max'))


class Factory:
    execution_mode = 'recording'
    fixture_sha256 = 'review-offline-v1'

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, role, directory, profile):
        def call(messages, **kwargs):
            self.calls.append((role, json.loads(messages[-1]['content'])))
            value = self.responses[len(self.calls) - 1]
            if isinstance(value, Exception):
                raise value
            return value if 'choices' in value else dict(content=json.dumps(value), complete=True, finish_reason='stop', usage={'prompt_tokens': 10, 'completion_tokens': 20})
        return call


def execute(tmp_path, book, guide, config, factory=None, **kwargs):
    return engine.run_guide_review(book, guide, tmp_path / 'out', config, client_factory=factory,
        run=kwargs.pop('run', True), token_counter=kwargs.pop('token_counter', lambda raw, messages: 100), **kwargs)


def review():
    return dict(findings=[], limitations=['Only supplied packets were visible.'])


def revision(guide):
    return dict(guide=deepcopy(guide), decisions=[])


def test_two_calls_and_safe_cached_resume(tmp_path, book, guide, config):
    factory = Factory(review(), revision(guide))
    result = execute(tmp_path, book, guide, config, factory)
    assert result['complete'] and result['model_calls'] == 2
    assert [r for r, _ in factory.calls] == ['reviewer', 'reviser']
    a, b = [p for _, p in factory.calls]
    for key in ('original_guide', 'full_outline', 'source_catalog', 'materials', 'material_scope'):
        assert a[key] == b[key]
    assert 'INTACT_EVIDENCE_1' in json.dumps(a['materials'])
    assert 'complete conditions' in json.dumps(a['materials'])
    assert b['advisory_review']['findings'] == []
    assert not result['scientific_correctness_established']
    baseline = (tmp_path / 'out/BASELINE_GUIDE.json').read_bytes()
    resumed = execute(tmp_path, book, guide, config, factory)
    assert resumed['complete'] and resumed['model_calls'] == 0 and len(factory.calls) == 2
    assert (tmp_path / 'out/BASELINE_GUIDE.json').read_bytes() == baseline
    assert result['cost_summary']['total_known_cost_including_base_cny'] == 0
    for stage in result['stages']:
        from pathlib import Path
        attempt = Path(stage['attempt_dir'])
        assert all((attempt / name).exists() for name in ('MESSAGES.json', 'ACTUAL_REQUEST.json', 'RAW_RESPONSE.json', 'USAGE.json', 'RESULT.json'))


@pytest.mark.parametrize('bad', [RuntimeError('transport timeout'), {'findings': [{'id': 'F1'}], 'limitations': []},
    {'choices': [{'finish_reason': 'length', 'message': {'content': json.dumps(review())}}]}])
def test_reviewer_failure_stops_and_never_retries(tmp_path, book, guide, config, bad):
    factory = Factory(bad)
    result = execute(tmp_path, book, guide, config, factory)
    assert not result['complete'] and result['model_calls'] == 1
    assert not (tmp_path / 'out/REVISED_GUIDE.json').exists()
    assert result['selected_guide_path'].endswith('BASELINE_GUIDE.json')
    again = execute(tmp_path, book, guide, config, factory)
    assert not again['complete'] and again['model_calls'] == 0 and len(factory.calls) == 1


def test_reviser_failure_preserves_baseline(tmp_path, book, guide, config):
    factory = Factory(review(), RuntimeError('timeout'))
    result = execute(tmp_path, book, guide, config, factory)
    assert not result['complete'] and len(factory.calls) == 2
    assert (tmp_path / 'out/BASELINE_GUIDE.json').exists()
    assert not (tmp_path / 'out/REVISED_GUIDE.json').exists()
    again = execute(tmp_path, book, guide, config, factory)
    assert not again['complete'] and len(factory.calls) == 2


def test_changed_input_or_config_cannot_redispatch(tmp_path, book, guide, config):
    factory = Factory(review(), revision(guide))
    execute(tmp_path, book, guide, config, factory)
    changed = deepcopy(guide); changed['manuscript_guide'] += ' Changed.'
    with pytest.raises(ValueError, match='input_or_config_changed'):
        execute(tmp_path, book, changed, config, factory)
    changed_config = deepcopy(config); changed_config['max_input_tokens'] = 500
    with pytest.raises(ValueError, match='input_or_config_changed'):
        execute(tmp_path, book, guide, changed_config, factory)
    assert len(factory.calls) == 2


def test_capacity_omission_is_explicit_and_never_truncates(tmp_path, book, guide, config):
    config['max_input_tokens'] = 42298
    def meter(raw, messages):
        return 100 + 100 * len(json.loads(messages[-1]['content'])['materials'])
    result = execute(tmp_path, book, guide, config, run=False, token_counter=meter)
    assert result['status'] == 'preview' and result['model_calls'] == 0
    assert len(result['material_scope']['included_source_handles']) == 1
    assert len(result['material_scope']['omitted_source_handles']) == 1
    with pytest.raises(ValueError, match='material_selection_changed'):
        execute(tmp_path, book, guide, config, run=False)


def test_no_capacity_never_initializes_client(tmp_path, book, guide, config):
    config['max_input_tokens'] = 50
    factory = Factory()
    result = execute(tmp_path, book, guide, config, factory)
    assert result['status'] == 'capacity_blocked' and not factory.calls


def test_preview_after_complete_keeps_previous_selection(tmp_path, book, guide, config):
    factory = Factory(review(), revision(guide))
    execute(tmp_path, book, guide, config, factory)
    prior = (tmp_path / 'out/REVIEW_RESULT.json').read_bytes()
    result = execute(tmp_path, book, guide, config, run=False)
    assert result['previous_revision_preserved'] and result['candidate_guide_path'].endswith('REVISED_GUIDE.json')
    assert (tmp_path / 'out/REVIEW_RESULT.json').read_bytes() == prior


def test_reviser_can_reject_supported_finding(tmp_path, book, guide, config):
    finding = dict(id='F1', location='C1', guide_excerpt='Develop the relationship', concern='Could clarify conditions.',
        proposed_change='Clarify this local statement.', evidence_basis='outline', evidence_excerpt='Original task')
    feedback = dict(findings=[finding], limitations=[])
    final = dict(guide=guide, decisions=[dict(finding_id='F1', decision='rejected', rationale='The surrounding guide already covers this.')])
    factory = Factory(feedback, final)
    result = execute(tmp_path, book, guide, config, factory)
    assert result['complete'] and result['decisions'][0]['decision'] == 'rejected'


def test_default_profile_is_valid():
    path = engine.ROOT / 'config/guide_review/plus_max_once.json'
    config = engine.validate_config(json.loads(path.read_text()))
    assert config['reviewer']['model'] == 'qwen3.5-plus' and config['reviser']['model'] == 'qwen3.8-max'


def test_no_findings_cannot_trigger_unrelated_rewrite(tmp_path, book, guide, config):
    changed = deepcopy(guide); changed['manuscript_guide'] = 'Unrelated rewrite.'
    result = execute(tmp_path, book, guide, config, Factory(review(), revision(changed)))
    assert not result['complete'] and result['selected_guide_path'].endswith('BASELINE_GUIDE.json')
    assert 'revision_without_findings' in json.dumps(result)


def test_unmentioned_catalog_is_not_loaded(tmp_path, book, guide, config):
    for chapter in guide['chapters']:
        chapter.pop('source_handles')
    guide['chapters'][0]['writing_arrangement'] += ' Consider [P0002].'
    factory = Factory(review(), revision(guide))
    result = execute(tmp_path, book, guide, config, factory)
    assert result['complete']
    assert result['material_scope']['included_source_handles'] == ['P0002']
    assert result['material_scope']['omitted_source_handles'] == ['P0001']
    assert 'INTACT_EVIDENCE_1' not in json.dumps(factory.calls[0][1]['materials'])


def test_max_feedback_capacity_blocks_before_plus(tmp_path, book, guide, config):
    config['max_input_tokens'] = 20000
    factory = Factory()
    result = execute(tmp_path, book, guide, config, factory)
    assert result['status'] == 'capacity_blocked' and not factory.calls
    assert 'no review dispatched' in result['stages'][0]['required_action']


def test_total_feedback_bytes_are_bounded(tmp_path, book, guide, config):
    feedback = dict(findings=[], limitations=['界' * 1200] * 8)
    result = execute(tmp_path, book, guide, config, Factory(feedback))
    assert not result['complete'] and result['model_calls'] == 1
    assert 'review_total_feedback_exceeds_bound' in json.dumps(result)


def test_unknown_revised_source_is_invalid(tmp_path, book, guide, config):
    changed = deepcopy(guide); changed['chapters'][0]['source_handles'] = ['P9999']
    result = execute(tmp_path, book, guide, config, Factory(review(), revision(changed)))
    assert not result['complete']
    assert 'P9999' in json.dumps(result)


def test_changed_execution_fixture_cannot_redispatch(tmp_path, book, guide, config):
    factory = Factory(review(), revision(guide))
    execute(tmp_path, book, guide, config, factory)
    factory.fixture_sha256 = 'changed'
    with pytest.raises(ValueError, match='execution_mode_or_fixture_changed'):
        execute(tmp_path, book, guide, config, factory)
    assert len(factory.calls) == 2


def test_interrupted_uncertain_attempt_never_redispatches(tmp_path, book, guide, config):
    from pathlib import Path
    factory = Factory(review(), revision(guide))
    result = execute(tmp_path, book, guide, config, factory)
    attempt = Path(result['stages'][1]['attempt_dir'])
    (attempt / 'RAW_RESPONSE.json').unlink(); (attempt / 'RESULT.json').unlink()
    result = execute(tmp_path, book, guide, config, factory)
    assert result['status'] == 'uncertain_attempt' and len(factory.calls) == 2
    assert result['previous_revision_preserved']


def test_nested_max_truncation_is_not_completion(tmp_path, book, guide, config):
    final = {'choices': [{'finish_reason': 'length', 'message': {'content': json.dumps(revision(guide))}}]}
    result = execute(tmp_path, book, guide, config, Factory(review(), final))
    assert not result['complete'] and result['model_calls'] == 2
    assert result['stages'][1]['result']['provider_transport_complete'] is False
    assert not (tmp_path / 'out/REVISED_GUIDE.json').exists()


def test_local_format_recovery_never_adds_model_calls(tmp_path, book, guide, config):
    final = {'choices': [{'finish_reason': 'stop', 'message': {'content': '```json\n' + json.dumps(revision(guide)) + '\n```'}}]}
    result = execute(tmp_path, book, guide, config, Factory(review(), final))
    assert result['complete'] and result['model_calls'] == 2
    assert 'format_recovery_path' in result['stages'][1]
    from pathlib import Path
    attempt = Path(result['stages'][1]['attempt_dir'])
    assert '```json' in (attempt / 'RAW_RESPONSE.json').read_text()
    assert (attempt / 'NORMALIZED_RESPONSE.json').exists()
