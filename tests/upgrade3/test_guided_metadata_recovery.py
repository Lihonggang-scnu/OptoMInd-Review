"""Bounded, offline protocol recovery; completion remains a content decision."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_guided_body_writer import book, guide, config, offline, execute, Factory
from optomind_research.runtime.upgrade3.guided_body_contracts import parse_guided_response


class RecoveryFactory(Factory):
    def __init__(self, author='Exact prose.\n', decision=None, completion=None):
        super().__init__()
        self.author = author
        self.decision = decision if decision is not None else {'complete': True, 'remaining_content': []}
        self.completion = completion
        self.profiles = []

    def __call__(self, role, directory, profile):
        self.profiles.append((role, deepcopy(profile)))
        def call(messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            self.calls.append((role, payload))
            if role == 'metadata':
                assert profile['model'] == 'qwen3.5-plus'
                assert not profile['thinking'] and profile['thinking_budget'] == 0
                assert profile['max_output_tokens'] <= 2048
                response = deepcopy(self.decision)
            elif 'draft_body_markdown' in payload['chapter_assignment']:
                response = self.completion or {'insertions': [{'after_anchor': '', 'text': '\nMissing boundary.'}],
                    'complete': True, 'remaining_content': []}
            elif payload['chapter_assignment']['chapter_id'] == 'C1':
                response = deepcopy(self.author)
            else:
                response = {'body_markdown': 'Later chapter.', 'complete': True, 'remaining_content': []}
            return response if isinstance(response, dict) and ('content' in response or 'choices' in response) else {
                'content': response if isinstance(response, str) else json.dumps(response),
                'complete': True, 'finish_reason': 'stop'}
        return call


def auto_config(config, **extra):
    return {**config, 'automatic_metadata_recovery': True, **extra}


def test_missing_metadata_one_decision_immutable_draft_and_cached_resume(tmp_path, book, guide, config):
    factory = RecoveryFactory()
    result = execute(tmp_path, book, guide, auto_config(config), factory)
    assert result['complete'] and len(factory.calls) == 4, result
    assert result['body_markdown'].startswith(factory.author)
    assert factory.calls[1][1]['draft_body_markdown'] == factory.author
    assert factory.calls[2][1]['accepted_body_markdown'] == factory.author
    attempt = Path(result['stages'][0]['attempt_dir'])
    raw, parsed = (attempt / 'RAW_RESPONSE.json').read_bytes(), (attempt / 'RESULT.json').read_bytes()
    assert json.loads(parsed)['errors']
    audit = json.loads(next((tmp_path / 'out' / 'runs' / result['run_id'] / 'metadata_recovery').glob('*.json')).read_text())
    assert audit['source'] == 'bounded_plus_metadata_decision'
    resumed = execute(tmp_path, book, guide, auto_config(config), factory, retry_failed=True)
    assert resumed['complete'] and resumed['model_calls'] == 0 and len(factory.calls) == 4
    assert (attempt / 'RAW_RESPONSE.json').read_bytes() == raw
    assert (attempt / 'RESULT.json').read_bytes() == parsed


@pytest.mark.parametrize('author', [
    'Exact prose.\n```guide_writer_metadata\n{bad metadata}\n```',
    {'body_markdown': 'Exact prose.', 'complete': 'unknown', 'remaining_content': []},
])
def test_malformed_metadata_uses_bounded_decision(tmp_path, book, guide, config, author):
    result = execute(tmp_path, book, guide, auto_config(config), RecoveryFactory(author))
    assert result['complete'], result
    assert result['model_calls'] == 4


@pytest.mark.parametrize('suffix', [
    '\n  ``` GUIDE_WRITER_METADATA \n{"complete":true,"remaining_content":[]}\n```',
    '\n```json\n{"complete":true,"remaining_content":[]}\n```',
    '\n~~~guide_writer_metadata\n{"complete":true,"remaining_content":[]}\n~~~',
    '\n```guide_writer_metadata\n{"complete":true,"remaining_content":[]}',
])
def test_unambiguous_syntax_is_recovered_without_model(suffix):
    parsed = parse_guided_response('Exact prose.' + suffix)
    assert parsed['complete'] and parsed['body_markdown'] == 'Exact prose.'


@pytest.mark.parametrize('transport', [
    {'content': 'Truncated prose.', 'complete': False, 'finish_reason': 'length'},
    {'content': 'Truncated prose.', 'finish_reason': 'max_tokens'},
    {'choices': [{'message': {'content': 'Truncated prose.'}, 'finish_reason': 'length'}]},
])
def test_truncation_never_gets_metadata_recovery(tmp_path, book, guide, config, transport):
    factory = RecoveryFactory(transport)
    result = execute(tmp_path, book, guide, auto_config(config), factory)
    assert result['status'] == 'transport_failed' and len(factory.calls) == 1, result


def test_invalid_remaining_field_uses_validated_decision(tmp_path, book, guide, config):
    factory = RecoveryFactory({'body_markdown': 'Prose.', 'complete': False, 'remaining_content': 5})
    result = execute(tmp_path, book, guide, auto_config(config), factory)
    assert result['complete'] and result['segments'][0]['complete'] is True
    assert len(factory.calls) == 4


def test_actual_missing_table_preserved_by_validated_decision(tmp_path, book, guide, config):
    factory = RecoveryFactory({'body_markdown': 'Prose.', 'complete': 'invalid', 'remaining_content': ['Missing table']},
        decision={'complete': False, 'remaining_content': ['Missing table']})
    result = execute(tmp_path, book, guide, auto_config(config, completion_on_missing=False), factory)
    assert not result['complete'] and result['segments'][0]['remaining_content'] == ['Missing table']


@pytest.mark.parametrize('decision', ['Not JSON', {'complete': False, 'remaining_content': []},
    {'complete': True, 'remaining_content': [], 'body_markdown': 'Rewritten'},
    {'content': '{"complete":true,"remaining_content":[]}', 'complete': False, 'finish_reason': 'length'}])
def test_bad_decision_stops_and_resume_never_loops(tmp_path, book, guide, config, decision):
    factory = RecoveryFactory(decision=decision)
    result = execute(tmp_path, book, guide, auto_config(config), factory)
    assert not result['complete'] and len(factory.calls) == 2
    resumed = execute(tmp_path, book, guide, auto_config(config), factory)
    assert not resumed['complete'] and len(factory.calls) == 2
    assert result['body_markdown'] == factory.author


def test_metadata_recovery_on_valid_insertions_does_not_apply_twice(tmp_path, book, guide, config):
    factory = RecoveryFactory({'body_markdown': 'Prose.', 'complete': False, 'remaining_content': ['Missing boundary']},
        completion={'insertions': [{'after_anchor': '', 'text': '\nBoundary.'}]})
    result = execute(tmp_path, book, guide, auto_config(config), factory)
    assert result['complete'], result
    assert result['body_markdown'].count('Boundary.') == 1
    assert [role for role, _ in factory.calls].count('metadata') == 1
    resumed = execute(tmp_path, book, guide, auto_config(config), factory)
    assert resumed['complete'] and resumed['model_calls'] == 0


@pytest.mark.parametrize('chapter_number', [3, 4, 5, 6])
def test_real_archived_missing_markers_and_missing_table(tmp_path, book, guide, config, chapter_number):
    archive = Path(__file__).resolve().parents[2] / 'docs/acceptance/guided-body-frozen-max-plus-20261009/routes/frozen_max_plus'
    attempt = next((archive / 'stages' / f'author_{chapter_number:03d}').glob('*/attempt_001'))
    raw_path = attempt / 'RAW_RESPONSE.json'
    raw = json.loads(raw_path.read_text() if raw_path.exists() else b''.join(p.read_bytes() for p in sorted(attempt.glob('RAW_RESPONSE.json.part-*'))))
    message_path = attempt / 'MESSAGES.json'
    messages = json.loads(message_path.read_text() if message_path.exists() else b''.join(p.read_bytes() for p in sorted(attempt.glob('MESSAGES.json.part-*'))))
    archived_payload = json.loads(messages[-1]['content'])
    guide = deepcopy(guide)
    guide['chapters'][0].update({key: value for key, value in archived_payload['chapter_assignment'].items() if key != 'chapter_id'})
    actual = parse_guided_response(raw)
    assert actual['transport_complete'] and actual['body_markdown']
    gap = chapter_number == 6
    factory = RecoveryFactory(raw, decision={'complete': not gap, 'remaining_content': ['Add the required cross-comparison table.'] if gap else []})
    result = execute(tmp_path, book, guide, auto_config(config, completion_on_missing=False), factory)
    assert result['segments'][0]['body_markdown'] == actual['body_markdown']
    assert factory.calls[1][1]['chapter_assignment']['required_content'] == archived_payload['chapter_assignment']['required_content']
    assert result['segments'][0]['complete'] is not gap
    if gap:
        assert result['segments'][0]['remaining_content'] == ['Add the required cross-comparison table.']
        assert not result['complete']
    assert len(factory.calls) == 4


@pytest.mark.parametrize('reason', ['length', 'timeout', 'max_tokens'])
def test_nested_truncation_valid_metadata_remains_retryable(tmp_path, book, guide, config, reason):
    raw = {'choices': [{'message': {'content': json.dumps({'body_markdown': 'Draft.', 'complete': True, 'remaining_content': []})}, 'finish_reason': reason}]}
    factory = RecoveryFactory(raw)
    first = execute(tmp_path, book, guide, auto_config(config), factory)
    assert first['status'] == 'transport_failed'
    saved = json.loads((Path(first['stages'][0]['attempt_dir']) / 'RESULT.json').read_text())
    assert not saved['complete'] and not saved['transport_complete']
    factory.author = {'body_markdown': 'Finished.', 'complete': True, 'remaining_content': []}
    resumed = execute(tmp_path, book, guide, auto_config(config), factory, retry_failed=True)
    assert resumed['complete'] and resumed['model_calls'] == 3


def test_explicit_retry_retries_only_failed_metadata(tmp_path, book, guide, config):
    factory = RecoveryFactory(decision='Invalid')
    first = execute(tmp_path, book, guide, auto_config(config), factory)
    assert first['status'] == 'metadata_unresolved'
    assert 'explicitly retry the metadata stage' in first['required_action']['instruction']
    factory.decision = {'complete': True, 'remaining_content': []}
    resumed = execute(tmp_path, book, guide, auto_config(config), factory, retry_failed=True)
    assert resumed['complete'] and resumed['model_calls'] == 3
    assert [role for role, _ in factory.calls[:3]] == ['writer', 'metadata', 'metadata']


def test_real_gap_uses_existing_bounded_completer(tmp_path, book, guide, config):
    factory = RecoveryFactory(decision={'complete': False, 'remaining_content': ['Missing boundary.']})
    result = execute(tmp_path, book, guide, auto_config(config), factory)
    assert result['complete'] and len(factory.calls) == 5
    assert factory.calls[2][1]['chapter_assignment']['remaining_content'] == ['Missing boundary.']
    assert result['segments'][0]['body_markdown'] == 'Exact prose.\n\nMissing boundary.'


@pytest.mark.parametrize('stage_index', [0, 1])
def test_interrupted_author_or_metadata_explicit_retry(tmp_path, book, guide, config, stage_index):
    factory = RecoveryFactory()
    first = execute(tmp_path, book, guide, auto_config(config), factory)
    attempt = Path(first['stages'][stage_index]['attempt_dir'])
    (attempt / 'RESULT.json').unlink()
    (attempt / 'RAW_RESPONSE.json').unlink()
    cached = execute(tmp_path, book, guide, auto_config(config), factory)
    assert not cached['complete'] and cached['model_calls'] == 0
    resumed = execute(tmp_path, book, guide, auto_config(config), factory, retry_failed=True)
    assert resumed['complete'] and resumed['model_calls'] >= 1
    assert len(list(attempt.parent.glob('attempt_*'))) == 2
    if stage_index == 1:
        assert [role for role, _ in factory.calls].count('metadata') == 2
        assert len(list(Path(first['stages'][0]['attempt_dir']).parent.glob('attempt_*'))) == 1


def test_auto_default_and_boolean_setting(config):
    from optomind_research.runtime.upgrade3.guided_body_writer import validate_config
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import CandidateError
    config.pop('automatic_metadata_recovery')
    assert validate_config(config)['automatic_metadata_recovery'] is True
    with pytest.raises(CandidateError, match='automatic_metadata_recovery_must_be_boolean'):
        validate_config({**config, 'automatic_metadata_recovery': 'true'})


def test_metadata_obeys_total_call_limit_without_dispatch(tmp_path, book, guide, config):
    factory = RecoveryFactory()
    result = execute(tmp_path, book, guide, auto_config(config, max_author_calls=1), factory)
    assert not result['complete'] and len(factory.calls) == 1
    assert result['required_action']['automatic_recovery']['status'] == 'author_call_limit'


def test_metadata_budget_failure_is_recorded_and_not_retried(tmp_path, book, guide, config):
    class BudgetFactory(RecoveryFactory):
        def __call__(self, role, directory, profile):
            if role == 'metadata':
                raise RuntimeError('global_budget_exceeded')
            return super().__call__(role, directory, profile)
    factory = BudgetFactory()
    result = execute(tmp_path, book, guide, auto_config(config), factory)
    assert not result['complete'] and len(factory.calls) == 1
    assert 'global_budget_exceeded' in json.dumps(result['required_action'])
    resumed = execute(tmp_path, book, guide, auto_config(config), factory)
    assert not resumed['complete'] and resumed['model_calls'] == 0


def test_completion_metadata_explicit_retry_reuses_saved_insertions(tmp_path, book, guide, config):
    factory = RecoveryFactory({'body_markdown': 'Prose.', 'complete': False, 'remaining_content': ['Boundary']},
        decision='Invalid', completion={'insertions': [{'after_anchor': '', 'text': '\nBoundary.'}]})
    first = execute(tmp_path, book, guide, auto_config(config), factory)
    assert not first['complete'] and 'saved insertion response' in first['required_action']['instruction']
    factory.decision = {'complete': True, 'remaining_content': []}
    resumed = execute(tmp_path, book, guide, auto_config(config), factory, retry_failed=True)
    assert resumed['complete'] and resumed['body_markdown'].count('Boundary.') == 1
    assert sum(role == 'writer' and 'draft_body_markdown' in payload['chapter_assignment'] for role, payload in factory.calls) == 1


def test_contradictory_future_chapter_gaps_are_replaced_not_unioned(tmp_path, book, guide, config):
    factory = RecoveryFactory({'body_markdown': 'Prose.', 'complete': True,
        'remaining_content': ['Future chapter C2 explanation', 'Future chapter C3 table']})
    result = execute(tmp_path, book, guide, auto_config(config), factory)
    assert result['complete'], result
    assert len(factory.calls) == 4
    assert result['segments'][0]['body_markdown'] == 'Prose.'
    assert result['segments'][0]['remaining_content'] == []
    assert not any(stage['stage_id'].startswith('complete_') for stage in result['stages'])
