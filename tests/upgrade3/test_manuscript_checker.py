"""Offline contract probes, not claims of autonomous scientific quality."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from optomind_research.runtime.upgrade3.manuscript_checker import (
    ManuscriptCheckerError, build_checker_messages, execute_checker_response,
    run_manuscript_checker,
)
from optomind_research.runtime.upgrade3.post_body_revision_contracts import sha256_text


@pytest.fixture(autouse=True)
def no_network():
    with patch('socket.socket.connect', side_effect=AssertionError('offline only')):
        yield


def response(text, *changes, **extra):
    return {'snapshot_id': sha256_text(text), 'changes': list(changes), 'unresolved_questions': [], **extra}


def change(old, new, block='B0001', **extra):
    return {'target_block_id': block, 'operation': 'replace', 'original_text': old,
            'replacement_text': new, 'reason': 'A local consistency correction.', **extra}


def execute(text, *changes, **kwargs):
    return execute_checker_response(draft_text=text, response=response(text, *changes), **kwargs)


def evidence_id(text, block='B0001'):
    return f'snapshot:{sha256_text(text)}:{block}'


def test_exact_block_only_and_overlapping_occurrences():
    result = execute('Same.\n\nSame.\n', change('Same.', 'Clear.', 'B0002'))
    assert result['draft_text'] == 'Same.\n\nClear.\n'
    assert not result['pending']
    result = execute('aaa\n', change('aa', 'bb'))
    assert 'target_not_unique' in result['pending'][0]['code']


def test_conflicts_all_pending_and_independent_success():
    result = execute('Alpha Beta.\n\nGamma.\n', change('Alpha Beta', 'Combined'),
                     change('Beta', 'Better'), change('Gamma.', 'Clear.', 'B0002'))
    assert result['draft_text'] == 'Alpha Beta.\n\nClear.\n'
    assert {p['edit_id'] for p in result['pending']} == {'E0001', 'E0002'}
    assert [p['edit_id'] for p in result['applied']] == ['E0003']


def test_one_bad_proposal_does_not_discard_good_sibling():
    result = execute('Alpha.\n\nBeta.\n', change('Alpha.', 'Clear.'), {'operation': 'path'})
    assert result['draft_text'].startswith('Clear.')
    assert len(result['pending']) == 1


@pytest.mark.parametrize('field,value,code', [
    ('operation', [], 'operation_not_allowed'), ('operation', {}, 'operation_not_allowed'),
    ('kind', [], 'unknown_kind'), ('kind', {}, 'unknown_kind'),
])
def test_malformed_enum_field_preserves_sibling_and_uses_pending_repair(tmp_path, field, value, code):
    source = tmp_path / 'source.md'
    source.write_bytes(b'Alpha.\n\nBeta.\n')
    seen = []
    def fixture(payload, number):
        seen.append(number)
        if number == 1:
            invalid = change('Beta.', 'Better.', 'B0002')
            invalid[field] = value
            rows = [change('Alpha.', 'Clear.'), invalid]
        else:
            assert payload['feedback']['pending_edit_ids'] == ['E0002']
            assert payload['feedback']['failures'][0]['code'] == code
            assert payload['blocks'][0]['text'] == 'Clear.\n'
            rows = [change('Beta.', 'Better.', 'B0002', edit_id='E0002')]
        return {'snapshot_id': payload['snapshot_id'], 'changes': rows, 'unresolved_questions': []}
    report = run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'check', fixture_responses=fixture)
    assert report['status'] == 'edited' and seen == [1, 2]
    assert Path(report['selected_manuscript']['path']).read_bytes() == b'Clear.\n\nBetter.\n'
    receipt = json.loads((tmp_path / 'check/round_1/EXECUTION_RECEIPT.json').read_text(encoding='utf-8'))
    assert len(receipt['applied']) == len(receipt['pending']) == 1


def test_stale_snapshot_and_wrong_round_ids():
    with pytest.raises(ManuscriptCheckerError, match='stale_snapshot'):
        execute_checker_response(draft_text='Other.', response=response('Alpha.'))
    result = execute_checker_response(draft_text='Alpha.', response=response('Alpha.',
        change('Alpha.', 'Beta.', edit_id='E0002')), pending_ids=['E0001'], round_number=2)
    assert not result['applied']
    assert {p['code'] for p in result['pending']} == {'unknown_or_duplicate_pending_id', 'repair_omitted_pending_id'}


def test_numeric_and_formula_science_uses_real_evidence():
    text = 'The force is +10 N.\n\nMeasurement: the force is −10 N.\n'
    result = execute(text, change('+10', '−10'))
    assert 'requires_scientific_evidence' in result['pending'][0]['code']
    result = execute(text, change('+10', '−10', kind='scientific',
                      evidence_ids=[evidence_id(text, 'B0002')]))
    assert result['draft_text'].startswith('The force is −10 N.')
    assert not result['pending']
    result = execute(text, change('+10', '−10', kind='scientific'))
    assert 'requires_material' in result['pending'][0]['code']
    formula = 'Use $F=ma$.\n\nDefinition: acceleration is a, not v.\n'
    result = execute(formula, change('ma', 'mv'))
    assert 'requires_scientific_evidence' in result['pending'][0]['code']


def test_evidence_reference_must_be_real_nonempty_material():
    result = execute('Value: 10.', change('10', '20', kind='scientific', evidence_ids=['M']),
                     material_records=[{'material_id': 'M', 'summary': 'Value is 20.'}])
    assert 'empty_evidence_material' in result['pending'][0]['code']


def catalogs():
    return [{'namespace': 'a', 'entries': [{'handles': ['P001'], 'paper_id': 'one'}]},
            {'namespace': 'b', 'entries': [{'handles': ['P001'], 'paper_id': 'two'}]}]


def test_namespace_identity_change_and_inside_token_guard():
    text = 'Statement [a::P001].\n\nSupported [b::P001].\n'
    result = execute(text, change('a::', 'b::'), identity_catalogs=catalogs())
    assert 'requires_scientific_evidence' in result['pending'][0]['code']
    result = execute(text, change('a::', 'b::', kind='scientific',
                     evidence_ids=[evidence_id(text, 'B0002')]), identity_catalogs=catalogs())
    assert result['draft_text'].startswith('Statement [b::P001].')
    assert not result['pending']
    result = execute(text, change('a::', 'b::', kind='scientific',
                     evidence_ids=[evidence_id(text, 'B0001')]), identity_catalogs=catalogs())
    assert 'not_supported' in result['pending'][0]['code']
    result = execute(text, change('P001', 'P00x', kind='scientific',
                     evidence_ids=[evidence_id(text)]), identity_catalogs=catalogs())
    assert 'malformed_citation' in result['pending'][0]['code']


def test_structure_and_untouched_bytes():
    text = '<!-- owner:start -->\r\n\r\n| A | B |\r\n| - | - |\r\n| 甲 | 乙 |\r\n\r\n尾句。  \r\n\r\n<!-- owner:end -->\r\n'
    result = execute(text, change('尾句。', '收束。', 'B0003'))
    assert result['draft_text'].encode() == text.replace('尾句。', '收束。').encode()
    result = execute(text, change('owner:start', 'owner:other'))
    assert 'markers_changed' in result['pending'][0]['code']
    result = execute(text, change('| 甲 | 乙 |', '| 甲 乙 |', 'B0002'))
    assert 'table_column_mismatch' in result['pending'][0]['code']


def test_two_round_s1_and_block_migration_and_restart(tmp_path):
    source = tmp_path / 'source.md'
    original = 'Alpha.\n\nBeta.\n'
    source.write_bytes(original.encode())
    seen = []
    def fixture(payload, number):
        seen.append(payload)
        if number == 1:
            return {'snapshot_id': payload['snapshot_id'], 'changes': [
                change('Alpha.', 'Alpha improved.\n\nInserted independent note.'),
                change('Wrong Beta.', 'Beta improved.', 'B0002')], 'unresolved_questions': []}
        assert payload['blocks'][2]['text'] == 'Beta.\n'
        assert payload['feedback']['failures'][0]['current_target_block_id'] == 'B0003'
        return {'snapshot_id': payload['snapshot_id'], 'changes': [
            change('Beta.', 'Beta improved.', 'B0003', edit_id='E0002')], 'unresolved_questions': []}
    report = run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'check', fixture_responses=fixture)
    assert report['status'] == 'edited'
    assert report['logical_rounds'] == 2 and report['external_requests'] == 0
    selected = Path(report['selected_manuscript']['path']).read_bytes()
    assert selected == b'Alpha improved.\n\nInserted independent note.\n\nBeta improved.\n'
    assert source.read_bytes() == original.encode()
    again = run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'check', fixture_responses=fixture)
    assert again['cache_hit'] and len(seen) == 2
    assert Path(again['selected_manuscript']['path']).read_bytes() == selected


def test_raw_saved_recovery_without_repeated_prefix(tmp_path):
    source = tmp_path / 'source.md'
    source.write_bytes(b'Alpha.\n\nBeta.\n')
    fixture = response(source.read_bytes().decode('utf-8'), change('Alpha.', 'New Alpha.'))
    out = tmp_path / 'check'
    report = run_manuscript_checker(draft_path=source, out_dir=out, fixture_responses=fixture)
    (out / 'CHECKER_RESULT_SEAL.json').unlink()  # crash before the terminal seal
    replay = run_manuscript_checker(draft_path=source, out_dir=out, fixture_responses=fixture)
    assert Path(replay['selected_manuscript']['path']).read_text() == 'New Alpha.\n\nBeta.\n'
    assert replay['model_calls'] == 0


def test_invalid_json_uses_only_one_format_repair(tmp_path):
    source = tmp_path / 'source.md'
    source.write_text('Alpha.\n')
    def fixture(payload, number):
        return 'broken {' if number == 1 else {'snapshot_id': payload['snapshot_id'], 'changes': [], 'unresolved_questions': []}
    result = run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'check', fixture_responses=fixture)
    assert result['status'] == 'no_change' and result['logical_rounds'] == 2


def test_no_change_disabled_and_missing_provider(tmp_path):
    source = tmp_path / 'source.md'
    source.write_bytes(b'Alpha.\r\n')
    original = source.read_bytes().decode()
    report = run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'nochange', fixture_responses=response(original))
    assert report['selected_manuscript']['path'] == str(source)
    disabled = run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'disabled', enabled=False)
    assert disabled['status'] == 'disabled' and disabled['logical_rounds'] == 0
    missing = run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'missing')
    assert missing['status'] == 'pending' and missing['selected_manuscript'] is None
    assert Path(missing['rounds'][0]['messages_path']).exists()


def test_partial_success_preserved_after_repair_limit(tmp_path):
    source = tmp_path / 'source.md'
    source.write_text('Alpha.\n\nBeta.\n')
    def fixture(payload, number):
        changes = [change('Alpha.', 'Clear.'), change('Missing.', 'Better.', 'B0002')] if number == 1 else [change('Still missing.', 'Better.', 'B0002', edit_id='E0002')]
        return {'snapshot_id': payload['snapshot_id'], 'changes': changes, 'unresolved_questions': []}
    result = run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'check', fixture_responses=fixture)
    assert result['status'] == 'complete_with_diagnostics'
    assert Path(result['selected_manuscript']['path']).read_text() == 'Clear.\n\nBeta.\n'
    assert len(result['pending']) == 1


def test_cached_artifact_tampering_blocks(tmp_path):
    source = tmp_path / 'source.md'
    source.write_bytes(b'Alpha.\n')
    fixture = response(source.read_bytes().decode('utf-8'), change('Alpha.', 'Clear.'))
    report = run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'check', fixture_responses=fixture)
    Path(report['selected_manuscript']['path']).write_text('Tampered.')
    with pytest.raises(ManuscriptCheckerError, match='integrity'):
        run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'check', fixture_responses=fixture)


def test_messages_single_full_manuscript_no_old_quota():
    messages = build_checker_messages(draft_text='UNIQUE SENTINEL.\n\nSecond.\n')
    assert messages[1]['content'].count('UNIQUE SENTINEL.') == 1
    assert 'snapshot:' in messages[1]['content']
    payload = json.loads(messages[1]['content'])
    assert not payload['materials']


def test_native_parts_material_record_adapter():
    messages = build_checker_messages(draft_text='Alpha.', material_records=[
        {'source_handle': 'P001', 'text': 'The measured force is −10 N.'}])
    materials = json.loads(messages[1]['content'])['materials']
    assert materials['M0001']['source_handles'] == ['P001']
    result = execute('Force: +10 N.', change('+10', '−10', kind='scientific', evidence_ids=['M0001']),
                     material_records=[{'source_handle': 'P001', 'text': 'The measured force is −10 N.'}])
    assert not result['pending']


def test_unit_spacing_and_math_delimiter_format_repairs():
    text = 'Force: 10N.\n\nUse $F=ma$.\n'
    result = execute(text, change('10N', '10 N'), change('$F=ma$', r'\(F=ma\)', 'B0002'))
    assert not result['pending']
    assert result['draft_text'] == 'Force: 10 N.\n\nUse \\(F=ma\\).\n'


def test_display_dollar_pair_guard_preserves_valid_formula_and_delimiter_edits():
    text = '$$\nF=ma\n$$\n\nIndependent prose.\n'
    result = execute(text, change('$$\nF=ma', '\nF=ma'))
    assert not result['applied'] and result['draft_text'] == text
    assert 'unbalanced_display_dollars' in result['pending'][0]['code']
    result = execute(text, change('F=ma', 'F=mv', kind='scientific', evidence_ids=['M']),
                     material_records=[{'material_id': 'M', 'text': 'The supplied formula is F=mv.'}])
    assert not result['pending'] and result['draft_text'] == text.replace('F=ma', 'F=mv')
    result = execute(text, change('$$\nF=ma\n$$', '\\[\nF=ma\n\\]'))
    assert not result['pending'] and result['draft_text'] == text.replace('$$\nF=ma\n$$', '\\[\nF=ma\n\\]')


def test_display_dollar_guard_ignores_inline_and_fenced_code():
    text = 'Inline `token` example.\n\n~~~text\nliteral\n~~~\n\n$$\nF=ma\n$$\n'
    result = execute(text,
        change('token', '$$', kind='scientific', evidence_ids=[evidence_id(text)]),
        change('literal', '$$', 'B0002', kind='scientific', evidence_ids=[evidence_id(text, 'B0002')]))
    assert not result['pending'] and len(result['applied']) == 2
    assert result['draft_text'] == text.replace('token', '$$').replace('literal', '$$')


def test_orphan_citation_bracket_cannot_be_removed_even_scientific():
    text = 'Measured [P001].\n'
    result = execute(text, {'target_block_id': 'B0001', 'operation': 'remove', 'original_text': '[',
        'replacement_text': '', 'reason': 'Remove an extraneous character.', 'kind': 'scientific',
        'evidence_ids': [evidence_id(text)]}, identity_catalogs=[{'entries': [{'handles': ['P001'], 'paper_id': 'one'}]}])
    assert 'orphan_citation_delimiter' in result['pending'][0]['code']


def test_weak_canonical_handle_is_not_cross_namespace_evidence():
    text = 'Finding [b::P001].\n'
    catalogs = [{'namespace': name, 'entries': [{'handles': ['P001'], 'title': name}]} for name in ('a', 'b')]
    result = execute(text, change('b::P001', 'P001', kind='scientific', evidence_ids=[evidence_id(text)]),
                     identity_catalogs=catalogs)
    assert 'not_supported' in result['pending'][0]['code']


def test_pending_id_does_not_unlock_unrelated_block(tmp_path):
    source = tmp_path / 'source.md'
    source.write_text('Alpha.\n\nBeta.\n')
    def fixture(payload, number):
        rows = [change('Missing Alpha.', 'Clear.')] if number == 1 else [change('Beta.', 'New issue.', 'B0002', edit_id='E0001')]
        return {'snapshot_id': payload['snapshot_id'], 'changes': rows, 'unresolved_questions': []}
    report = run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'check', fixture_responses=fixture)
    assert report['status'] == 'complete_with_diagnostics'
    assert report['pending'][0]['code'] == 'repair_target_outside_pending_scope'
    assert not report['applied']


class InjectedFactory:
    execution_mode = 'injected'

    def __init__(self, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first

    def __call__(self, role, path, profile):
        def call(messages, **kwargs):
            self.calls += 1
            if self.fail_first and self.calls == 1:
                raise RuntimeError('controlled offline provider seam failure')
            payload = json.loads(messages[1]['content'])
            raw = {'snapshot_id': payload['snapshot_id'], 'changes': [], 'unresolved_questions': []}
            return {'content': json.dumps(raw), 'complete': True, 'finish_reason': 'stop',
                    'returned_model': profile['model'], 'execution_mode': 'injected'}
        return call


class InterruptedRepairFactory:
    execution_mode = 'injected'

    def __init__(self, failure):
        self.calls = 0
        self.failure = failure

    def __call__(self, role, path, profile):
        def call(messages, **kwargs):
            self.calls += 1
            payload = json.loads(messages[1]['content'])
            if payload['round'] == 1:
                rows = [change('Alpha.', 'Clear.'), change('Missing Beta.', 'Better.', 'B0002')]
            elif self.calls == 2:
                if self.failure == 'transport':
                    raise RuntimeError('controlled offline repair transport failure')
                return {'content': '{"snapshot_id":', 'complete': False, 'finish_reason': 'length',
                        'returned_model': profile['model'], 'execution_mode': 'injected'}
            else:
                rows = [change('Beta.', 'Better.', 'B0002', edit_id='E0002')]
            raw = {'snapshot_id': payload['snapshot_id'], 'changes': rows, 'unresolved_questions': []}
            return {'content': json.dumps(raw), 'complete': True, 'finish_reason': 'stop',
                    'returned_model': profile['model'], 'execution_mode': 'injected'}
        return call


@pytest.mark.parametrize('failure', ['incomplete', 'transport'])
def test_interrupted_second_round_retains_selection_and_resumes_without_rebuy(tmp_path, failure):
    source = tmp_path / 'source.md'
    source.write_bytes(b'Alpha.\n\nBeta.\n')
    factory = InterruptedRepairFactory(failure)
    out = tmp_path / 'check'
    kwargs = dict(draft_path=source, out_dir=out, client_factory=factory,
                  run=True, token_counter=lambda raw, messages: 100)
    first = run_manuscript_checker(**kwargs)
    assert first['status'] == first['selected_manuscript']['status'] == 'pending'
    assert first['selected_manuscript']['path'] == str(out / 'round_1/CANDIDATE_HANDLES.md')
    assert first['selected_manuscript']['sha256'] == sha256_text('Clear.\n\nBeta.\n')
    assert first['selected_manuscript']['provenance']['applied_edit_ids'] == ['E0001']
    assert Path(first['selected_manuscript']['path']).read_bytes() == b'Clear.\n\nBeta.\n'
    assert [p['edit_id'] for p in first['pending']] == ['E0002']
    expected_code = 'incomplete_provider_response' if failure == 'incomplete' else 'failed_saved_attempt'
    assert first['diagnostics'] == [{'code': expected_code, 'round': 2}]
    assert not (out / 'CHECKER_RESULT_SEAL.json').exists()
    resumed = run_manuscript_checker(**kwargs)
    assert resumed['selected_manuscript'] == first['selected_manuscript']
    assert resumed['status'] == 'pending' and resumed['provider_calls'] == 0 and factory.calls == 2
    assert not (out / 'CHECKER_RESULT_SEAL.json').exists()
    retried = run_manuscript_checker(**kwargs, retry_failed=True)
    if failure == 'incomplete':
        assert retried['status'] == 'pending' and retried['provider_calls'] == 0 and factory.calls == 2
        assert not (out / 'CHECKER_RESULT_SEAL.json').exists()
    else:
        assert retried['status'] == 'edited' and retried['logical_rounds'] == 2 and factory.calls == 3
        assert Path(retried['selected_manuscript']['path']).read_bytes() == b'Clear.\n\nBetter.\n'
        assert (out / 'round_2/retries/attempt_002/RAW_RESPONSE.json').exists()


def test_incomplete_first_round_still_has_no_selection(tmp_path):
    source = tmp_path / 'source.md'
    source.write_bytes(b'Alpha.\n')
    def incomplete_factory(role, path, profile):
        return lambda messages, **kwargs: {'content': '{', 'complete': False, 'finish_reason': 'length',
                                          'execution_mode': 'injected'}
    result = run_manuscript_checker(draft_path=source, out_dir=tmp_path / 'check',
        client_factory=incomplete_factory, run=True, token_counter=lambda raw, messages: 100)
    assert result['status'] == 'pending' and result['selected_manuscript'] is None
    assert not (tmp_path / 'check/CHECKER_RESULT_SEAL.json').exists()


def test_real_cached_call_transport_failure_retry_and_raw_resume(tmp_path):
    source = tmp_path / 'source.md'
    source.write_bytes(b'Alpha.\r\n')
    factory = InjectedFactory(fail_first=True)
    kwargs = dict(draft_path=source, out_dir=tmp_path / 'check', client_factory=factory,
                  run=True, token_counter=lambda raw, messages: 100)
    first = run_manuscript_checker(**kwargs)
    assert first['status'] == 'pending' and first['provider_calls'] == 1
    assert first['external_requests'] == 0 and factory.calls == 1
    resume = run_manuscript_checker(**kwargs)
    assert resume['status'] == 'pending' and resume['provider_calls'] == 0
    assert factory.calls == 1
    retried = run_manuscript_checker(**kwargs, retry_failed=True)
    assert retried['status'] == 'no_change' and factory.calls == 2
    assert retried['logical_rounds'] == 1 and retried['external_requests'] == 0
    assert (tmp_path / 'check/round_1/retries/attempt_002/RAW_RESPONSE.json').exists()
    # Simulate a crash before result sealing and prove saved RAW is sufficient.
    (tmp_path / 'check/CHECKER_RESULT_SEAL.json').unlink()
    restored = run_manuscript_checker(**kwargs)
    assert restored['status'] == 'no_change' and restored['provider_calls'] == 0
    assert factory.calls == 2


def test_uncertain_saved_request_never_dispatches_automatically(tmp_path):
    source = tmp_path / 'source.md'
    source.write_text('Alpha.\n')
    factory = InjectedFactory()
    kwargs = dict(draft_path=source, out_dir=tmp_path / 'check', client_factory=factory,
                  token_counter=lambda raw, messages: 100)
    preview = run_manuscript_checker(**kwargs, run=False)
    assert preview['status'] == 'pending' and factory.calls == 0
    step = tmp_path / 'check/round_1'
    profile = json.loads((step / 'PROFILE.json').read_text())
    import optomind_research.runtime.upgrade3.manuscript_checker as module
    messages = json.loads((step / 'MESSAGES.json').read_text(encoding='utf-8'))
    (step / 'REQUEST.json').write_text(json.dumps({'profile': profile, 'messages_sha256': module._hash(messages)}))
    resumed = run_manuscript_checker(**kwargs, run=True, retry_failed=True)
    assert resumed['status'] == 'pending' and factory.calls == 0
    assert resumed['rounds'][0]['state'] == 'pending_existing_attempt'


def test_whole_document_rewrite_is_not_an_edit_api():
    text = 'Sentence. ' * 300
    result = execute(text, change(text, 'A replacement article.'))
    assert result['pending'][0]['code'] == 'whole_manuscript_rewrite_not_allowed'
    result = execute('First.\n\nSecond.\n', change('First.\n\nSecond.\n', 'New article.'))
    assert 'target_not_unique' in result['pending'][0]['code']


@pytest.mark.parametrize('text,old,new', [
    ('Range 1e-3.\n', '-', '+'), ('Offset x−5.\n', '−', '+'),
    ('Results 10 and 20.\n', '10 and 20', '20 and 10'),
])
def test_number_sign_order_and_exponent_cannot_be_editorial(text, old, new):
    result = execute(text, change(old, new))
    assert result['pending'][0]['code'] == 'meaning_change_requires_scientific_evidence'


@pytest.mark.parametrize('old,new', [
    ('95% CI 0.51-0.76; 0.55-0.83', '95% CI 0.51–0.76; 0.55–0.83'),
    ('Interval 10 - 20', 'Interval 10 – 20'),
    ('Interval -5--3', 'Interval -5–-3'),
])
def test_numeric_range_separator_typography_is_editorial(old, new):
    text = old + '.\n'
    result = execute(text, change(old, new, kind='editorial'))
    assert not result['pending'] and len(result['applied']) == 1
    assert result['draft_text'] == new + '.\n'
    assert result['applied'][0]['kind'] == 'editorial'


@pytest.mark.parametrize('old,new', [
    ('Interval 0.51-0.76', 'Interval 0.51–0.77'),
    ('Interval -5--3', 'Interval 5–-3'),
    ('Offset -5', 'Offset –5'),
    ('Values 1 -2', 'Values 1 –2'),
    ('Threshold −5', 'Threshold -5'),
    ('Interval 1e-3-2e-3', 'Interval 1e+3–2e-3'),
    ('Expression $10-20$', 'Expression $10–20$'),
])
def test_range_normalization_preserves_value_sign_exponent_and_math_boundaries(old, new):
    result = execute(old + '.\n', change(old, new, kind='editorial'))
    assert not result['applied']
    assert result['pending'][0]['code'] == 'meaning_change_requires_scientific_evidence'


@pytest.mark.parametrize('old,new,anchor,replacement', [
    ('##### Confounding factors', '#### 7.2.1 Confounding factors',
     '##### Confounding factors', '#### 7.2.1 Confounding factors'),
    ('### Cohort n=23 at 37 °C', '### 2.3 Cohort n=23 at 37 °C',
     'Cohort', '2.3 Cohort'),
])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_only_added_heading_prefix_is_editorial_and_preserves_title_numbers(old, new, anchor, replacement, newline):
    text = old + newline + 'Attached paragraph.' + newline
    result = execute(text, change(anchor, replacement, kind='editorial'))
    assert not result['pending'] and len(result['applied']) == 1
    assert result['draft_text'] == new + newline + 'Attached paragraph.' + newline


@pytest.mark.parametrize('old,new', [
    ('### Cohort n=23', '### 7.2.1 Cohort n=24'),
    ('### Cohort n=23', '### 7.2.1 Cohort'),
    ('### Temperature 37 °C', '### 7.2.1 Temperature 38 °C'),
    ('### Effect 0.51', '### 7.2.1 Effect 0.76'),
    ('### Cohort n=23', '### 7.2.1 Cohort n=-23'),
    ('### 7.2.1 Cohort', '### 7.2.2 Cohort'),
    ('Plain title', '7.2.1 Plain title'),
    ('```text\n### Cohort\n```', '```text\n### 7.2.1 Cohort\n```'),
])
def test_heading_number_exception_does_not_hide_scientific_or_nonheading_changes(old, new):
    result = execute(old + '\n', change(old, new, kind='editorial'))
    assert not result['applied']
    assert result['pending'][0]['code'] == 'meaning_change_requires_scientific_evidence'


@pytest.mark.parametrize('text,old,new', [
    ('Study cohort (n=23) showed a result.\n', 'cohort (n=23)', 'cohort'),
    ('P0402, P0576. A source. DOI: 10.1000/123\n', 'P0402, P0576.', 'P0402.'),
])
def test_same_editorial_proposal_stays_pending_in_second_round(text, old, new):
    row = change(old, new, kind='editorial')
    first = execute(text, row)
    second = execute_checker_response(draft_text=text, response=response(text, {**row, 'edit_id': 'E0001'}),
        pending_ids=['E0001'], round_number=2)
    for result in (first, second):
        assert not result['applied'] and result['draft_text'] == text
        assert result['pending'][0]['code'] == 'meaning_change_requires_scientific_evidence'


@pytest.mark.parametrize('identity', [{}, {'paper_id': 'same-paper'}, {'doi': '10.1000/same'}])
def test_explicit_same_source_alias_is_executable_with_original_material(identity):
    text = 'Finding [source::P0001].\n\nIndependent context.\n'
    catalog = [{'namespace': 'source', 'references': [
        {'handles': ['P0001', 'P0002'], 'title': 'Same source with explicit aliases', **identity}]}]
    result = execute(text, change('source::P0001', 'source::P0002', kind='scientific',
        evidence_ids=['M0001']), identity_catalogs=catalog,
        material_records=[{'source_handle': 'source::P0001', 'text': 'This material supports the finding.'}])
    assert not result['pending']
    assert result['draft_text'] == text.replace('source::P0001', 'source::P0002')
    assert len(result['applied']) == 1
