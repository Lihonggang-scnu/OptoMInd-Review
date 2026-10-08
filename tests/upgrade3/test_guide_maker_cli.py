"""Independent guide CLI boundaries. All provider activity is mocked/offline."""
import argparse
import json
from pathlib import Path

import pytest

from scripts.upgrade3 import guide_maker as cli
from scripts.upgrade3 import guided_body_writer as old_cli
from optomind_research.runtime.upgrade3.module4 import runtime
from test_fullbody_cli import case, dump, no_network  # noqa: F401
from test_guided_body_cli import existing_ledger


def command(case, output, *extra):
    return ['--manifest', str(case['manifest']), '--output', str(output), *extra]


def forbid(monkeypatch):
    def fail(*a, **kw):
        pytest.fail('Offline or invalid entry must never create a live factory or ledger')
    monkeypatch.setattr(cli, 'make_live_factory', fail)
    monkeypatch.setattr(runtime, 'QwenDirectClient', fail)
    monkeypatch.setattr(runtime, 'GlobalBudgetLedger', fail)
    return fail


def guide_response():
    guide = {'manuscript_guide': 'Explain conditional findings and their scientific limits for new researchers.',
            'chapters': [{'chapter_id': cid, 'title': cid,
                          'writing_arrangement': 'Explain evidence, boundary conditions, and the resulting limits.',
                          'required_content': ['Conditional finding'], 'source_handles': ['P0001']}
                         for cid in ('CH01', 'CH02')]}
    return {'guide': guide, 'reading_needs': [], 'complete': True, 'changes': []}


def test_defaults_require_no_manual_guide_or_old_draft():
    args = cli.parser().parse_args(['--manifest', 'source.json', '--output', 'out'])
    assert args.feedback is None and not args.run and not args.allow_max
    assert Path(args.config) == cli.DEFAULT_CONFIG
    for forbidden in ('--guide', '--draft', '--plan', '--route'):
        with pytest.raises(SystemExit):
            cli.parser().parse_args(['--manifest', 's', '--output', 'o', forbidden, 'x'])
    with pytest.raises(SystemExit):
        cli.parser().parse_args(['--manifest', 's', '--output', 'o', '--run', '--responses', 'r'])


def test_preview_no_credentials_budget_or_writer(tmp_path, case, monkeypatch):
    fail = forbid(monkeypatch)
    monkeypatch.setattr(cli, '_ledger_guard', fail)
    from optomind_research.runtime.upgrade3 import guided_body_writer
    monkeypatch.setattr(guided_body_writer, 'run_guided_body', fail)
    out = tmp_path / 'preview'
    assert cli.main(command(case, out, '--key-file', '/must-never-read-secret')) == 0
    result = cli.read_json(out / 'CLI_RUN.json')
    assert result['execution_mode'] == 'preview' and result['writing_tested'] is False
    context = cli.read_json(out / 'CLI_CONTEXT.json')
    assert context['feedback_file_sha256'] is None
    assert context['book_sha256'] and context['implementation_hashes']
    assert any('guide_maker.py' in p for p in context['implementation_hashes'])
    assert any('prompts/guide_maker/' in p for p in context['implementation_hashes'])
    assert '/must-never-read-secret' not in (out / 'CLI_CONTEXT.json').read_text()


def test_offline_replay_exact_guide_contract_and_resume(tmp_path, case, monkeypatch):
    forbid(monkeypatch)
    from optomind_research.runtime.upgrade3.guided_body_contracts import validate_guide
    response = dump(tmp_path / 'responses.json', {'maker': {'content': json.dumps(guide_response()), 'complete': True}})
    out = tmp_path / 'replay'
    args = command(case, out, '--responses', str(response))
    assert cli.main(args) == 0
    result = cli.read_json(out / 'CLI_RUN.json')
    assert result['execution_mode'] == 'recording' and result['complete'] and result['generation_complete']
    assert result['writing_tested'] is False and result['current_run_cost_cny'] == 0
    assert len(result['recorded_response_calls']) == 1
    book, _ = cli.load_body_manifest(case['manifest'])
    produced = cli.read_json(out / 'GUIDE.json')
    assert validate_guide(produced, book) == produced
    assert cli.main(args) == 0
    assert cli.read_json(out / 'CLI_RUN.json')['recorded_response_calls'] == []


def test_archive_preview_but_never_live(tmp_path, case, monkeypatch):
    fail = forbid(monkeypatch)
    monkeypatch.setattr(cli, '_ledger_guard', fail)
    book, _ = cli.load_body_manifest(case['manifest'])
    archive = dump(tmp_path / 'FULL_BODY_INPUT.json', book)
    args = ['--book', str(archive), '--output', str(tmp_path / 'archive')]
    assert cli.main(args) == 0
    assert cli.main(args + ['--run']) == 2
    assert cli.read_json(tmp_path / 'archive/CLI_EXCEPTION.json')['message'] == 'archived_book_is_offline_only'


@pytest.mark.parametrize('change', ['add_feedback', 'feedback', 'config', 'source', 'code'])
def test_changed_inputs_require_fresh_output(tmp_path, case, monkeypatch, change):
    forbid(monkeypatch)
    feedback = tmp_path / 'feedback.txt'
    feedback.write_text('Keep the causal limits visible.', encoding='utf-8')
    config = dump(tmp_path / 'config.json', cli.read_json(cli.DEFAULT_CONFIG))
    out = tmp_path / 'out'
    args = command(case, out, '--config', str(config))
    if change != 'add_feedback':
        args += ['--feedback', str(feedback)]
    assert cli.main(args) == 0
    prior = cli.read_json(out / 'CLI_CONTEXT.json')
    if change == 'add_feedback':
        args += ['--feedback', str(feedback)]
    elif change == 'feedback':
        feedback.write_text('Different writing feedback.', encoding='utf-8')
    elif change == 'config':
        config.write_text(config.read_text() + '\n', encoding='utf-8')
    elif change == 'source':
        case['plan'].write_text(case['plan'].read_text() + '\n', encoding='utf-8')
    else:
        original = cli._implementation_hashes()
        monkeypatch.setattr(cli, '_implementation_hashes', lambda: {**original, 'changed.py': 'different'})
    assert cli.main(args) == 2
    assert cli.read_json(out / 'CLI_CONTEXT.json') == prior
    assert 'new_output_directory' in cli.read_json(out / 'CLI_EXCEPTION.json')['message']


@pytest.mark.parametrize('problem', ['profile', 'role', 'max', 'secret', 'feedback', 'input', 'compile'])
def test_invalid_live_preflight_before_factory_and_ledger(tmp_path, case, monkeypatch, problem):
    fail = forbid(monkeypatch)
    monkeypatch.setattr(cli, '_ledger_guard', fail)
    config = cli.read_json(cli.DEFAULT_CONFIG)
    args = command(case, tmp_path / 'out', '--run')
    if problem == 'profile':
        config['maker']['max_output_tokens'] = -1
    elif problem == 'role':
        config['writer'] = config['maker']
    elif problem == 'max':
        config['maker']['model'] = 'qwen3.8-max'
    elif problem == 'secret':
        config['api_key'] = 'a-secret-that-must-not-be-read'
    elif problem == 'feedback':
        path = tmp_path / 'invalid-utf8'
        path.write_bytes(b'\xff')
        args += ['--feedback', str(path)]
    elif problem == 'input':
        case['manifest'].write_text('{}', encoding='utf-8')
    elif problem == 'compile':
        from optomind_research.runtime.upgrade3 import guide_maker_contracts
        def bad_compile(*a, **kw):
            raise ValueError('malformed_heavy_inputs')
        monkeypatch.setattr(guide_maker_contracts, 'compile_guide_input', bad_compile)
    path = dump(tmp_path / 'config.json', config)
    assert cli.main(args + ['--config', str(path)]) == 2


def test_original_budget_guard_is_shared_and_read_only(tmp_path):
    assert cli._ledger_guard is old_cli._ledger_guard
    ledger, marker = existing_ledger(tmp_path)
    before = ledger.read_bytes(), marker.read_bytes()
    result = cli._ledger_guard(argparse.Namespace(budget_ledger=str(ledger), budget_limit=60))
    assert result['actual_cny'] == pytest.approx(6.638)
    assert result['reserved_cny'] == 1.5
    assert result['remaining_cny'] == pytest.approx(60 - 6.638 - 1.5)
    assert (ledger.read_bytes(), marker.read_bytes()) == before


def test_live_cannot_create_new_allowance(tmp_path, case, monkeypatch):
    forbid(monkeypatch)
    ledger = tmp_path / 'fresh.sqlite'
    assert cli.main(command(case, tmp_path / 'out', '--run', '--budget-ledger', str(ledger), '--budget-limit', '60')) == 2
    assert not ledger.exists() and not ledger.with_name(ledger.name + '.evidence_round2.json').exists()


def test_mock_live_uses_only_maker_and_retains_old_spend(tmp_path, case, monkeypatch):
    from test_fullbody_integration import install_controlled_http
    ledger, marker = existing_ledger(tmp_path)
    marker_before = marker.read_bytes()
    captured = install_controlled_http(monkeypatch, lambda payload: guide_response())
    out = tmp_path / 'mock-live'
    args = command(case, out, '--run', '--budget-ledger', str(ledger), '--budget-limit', '60', '--key-file', '/mock-key')
    assert cli.main(args) == 0
    result = cli.read_json(out / 'CLI_RUN.json')
    assert result['budget_before']['actual_cny'] == pytest.approx(6.638)
    assert result['budget_after']['actual_cny'] > 6.638
    assert result['budget_after']['reserved_cny'] == 1.5
    assert result['budget_after']['limit_cny'] == 60
    assert len(captured) == 1 and marker.read_bytes() == marker_before
    assert result['generation_complete'] and not result['writing_tested']
    assert cli.main(args) == 0 and len(captured) == 1


def test_increased_call_cap_resumes_cached_draft_only_calls_remaining_stage(tmp_path, case, monkeypatch):
    forbid(monkeypatch)
    config = cli.read_json(cli.DEFAULT_CONFIG)
    config['max_model_calls'] = 1
    config_path = dump(tmp_path / 'config.json', config)
    provisional = guide_response()
    provisional.update(complete=False, reading_needs=[{
        'need_id': 'N1', 'question': 'Read the complete conditional finding.', 'source_handles': ['P0001']}])
    fixture = dump(tmp_path / 'responses.json', {
        'maker_001': {'content': json.dumps(provisional), 'complete': True},
        'maker_002': {'content': json.dumps(guide_response()), 'complete': True}})
    out = tmp_path / 'resume'
    args = command(case, out, '--config', str(config_path), '--responses', str(fixture))
    assert cli.main(args) == 3
    first = cli.read_json(out / 'CLI_RUN.json')
    assert first['status'] == 'model_call_limit'
    assert first['recorded_response_calls'] == ['maker_001']
    assert (out / 'DRAFT_GUIDE.json').is_file()
    first_manifest = cli.read_json(out / 'RUN_MANIFEST.json')
    config['max_model_calls'] = 2
    dump(config_path, config)
    assert cli.main(args) == 0
    second = cli.read_json(out / 'CLI_RUN.json')
    assert second['generation_complete'] and second['recorded_response_calls'] == ['maker_002']
    second_manifest = cli.read_json(out / 'RUN_MANIFEST.json')
    assert second_manifest['stages'][0]['cache_key'] == first_manifest['stages'][0]['cache_key']
    assert cli.read_json(out / 'CLI_CONTEXT.json')['config']['max_model_calls'] == 2
    assert cli.read_json(out / 'CLI_CONTEXT.json')['config_file_sha256'] == cli.sha256_file(config_path)
    assert cli.main(args) == 0
    assert cli.read_json(out / 'CLI_RUN.json')['recorded_response_calls'] == []


@pytest.mark.parametrize('change', ['decrease', 'profile', 'other', 'feedback', 'code'])
def test_call_cap_increase_exception_is_narrow(tmp_path, case, monkeypatch, change):
    forbid(monkeypatch)
    config = cli.read_json(cli.DEFAULT_CONFIG)
    config['max_model_calls'] = 2
    path = dump(tmp_path / 'config.json', config)
    out = tmp_path / 'out'
    args = command(case, out, '--config', str(path))
    assert cli.main(args) == 0
    prior = cli.read_json(out / 'CLI_CONTEXT.json')
    config['max_model_calls'] = 1 if change == 'decrease' else 3
    if change == 'profile':
        config['maker']['max_output_tokens'] -= 1
    elif change == 'other':
        config['purpose'] += ' Changed purpose.'
    elif change == 'feedback':
        feedback = tmp_path / 'feedback.txt'
        feedback.write_text('New feedback', encoding='utf-8')
        args += ['--feedback', str(feedback)]
    elif change == 'code':
        hashes = cli._implementation_hashes()
        monkeypatch.setattr(cli, '_implementation_hashes', lambda: {**hashes, 'changed.py': 'changed'})
    dump(path, config)
    assert cli.main(args) == 2
    assert cli.read_json(out / 'CLI_CONTEXT.json') == prior


def test_preview_cap_increase_preserves_recording_provenance(tmp_path, case, monkeypatch):
    forbid(monkeypatch)
    config = cli.read_json(cli.DEFAULT_CONFIG)
    config['max_model_calls'] = 1
    path = dump(tmp_path / 'config.json', config)
    fixture = dump(tmp_path / 'responses.json', {'maker': {'content': json.dumps(guide_response()), 'complete': True}})
    out = tmp_path / 'out'
    args = command(case, out, '--config', str(path))
    assert cli.main(args + ['--responses', str(fixture)]) == 0
    config['max_model_calls'] = 2
    dump(path, config)
    assert cli.main(args) == 0
    context = cli.read_json(out / 'CLI_CONTEXT.json')
    assert context['config']['max_model_calls'] == 2 and context['execution_mode'] == 'recording'


def test_dedicated_mock_live_resume_binds_original_ledger(tmp_path, case, monkeypatch):
    from test_fullbody_integration import install_controlled_http
    captured = install_controlled_http(monkeypatch, lambda payload: guide_response())
    ledger = tmp_path / 'guide.sqlite'
    out = tmp_path / 'dedicated'
    config = cli.read_json(cli.DEFAULT_CONFIG)
    config['max_model_calls'] = 1
    path = dump(tmp_path / 'config.json', config)
    base = command(case, out, '--config', str(path))
    live = ['--run', '--budget-mode', 'dedicated', '--budget-ledger', str(ledger),
            '--budget-limit', '30', '--key-file', '/mock-key']
    assert cli.main(base + live) == 0
    first = cli.read_json(out / 'CLI_RUN.json')
    assert first['budget_after']['actual_cny'] > 0
    binding = cli.read_json(out / 'CLI_CONTEXT.json')['budget_binding']
    # Increasing the cap via preview must retain budget provenance too.
    config['max_model_calls'] = 2
    dump(path, config)
    assert cli.main(base) == 0
    assert cli.read_json(out / 'CLI_CONTEXT.json')['budget_binding'] == binding
    assert cli.main(base + live) == 0
    assert len(captured) == 1
    assert cli.read_json(out / 'CLI_RUN.json')['budget_after']['actual_cny'] == first['budget_after']['actual_cny']
    changed = list(live)
    other = tmp_path / 'other.sqlite'
    changed[changed.index(str(ledger))] = str(other)
    assert cli.main(base + changed) == 2
    assert cli.read_json(out / 'CLI_EXCEPTION.json')['message'] == 'guide_live_resume_budget_changed'
    assert not other.exists() and len(captured) == 1


def test_implementation_hashes_use_portable_relative_keys():
    hashes = cli._implementation_hashes()
    assert 'scripts/upgrade3/guide_maker.py' in hashes
    assert 'prompts/guide_maker/generate.md' in hashes
    assert all('\\' not in path for path in hashes)
