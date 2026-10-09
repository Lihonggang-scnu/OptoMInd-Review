"""Explicit Plus-only test entry rejects other models without provider traffic."""
import pytest

from scripts.upgrade3 import guided_body_writer as cli
from test_guided_body_cli import command, forbid, guide  # noqa: F401
from test_fullbody_cli import case, dump, no_network  # noqa: F401


def test_plus_only_and_max_permission_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli.parser().parse_args(['--manifest', 'source.json', '--guide', 'guide.json',
                                 '--output', 'out', '--plus-only', '--allow-max'])


@pytest.mark.parametrize('mode', [[], ['--run'], ['--responses', '/not/read.json']])
@pytest.mark.parametrize('model', ['qwen3-max', 'qwen3.5-max', 'qwen3.5-flash'])
def test_non_plus_rejected_before_ledger_or_factory(tmp_path, case, guide, monkeypatch, mode, model):
    fail = forbid(monkeypatch)
    monkeypatch.setattr(cli, '_ledger_guard', fail)
    monkeypatch.setattr(cli, 'RecordingFactory', fail)
    config = cli.read_json(cli.DEFAULT_CONFIG)
    config['writer']['model'] = model
    config_path = dump(tmp_path / 'config.json', config)
    out = tmp_path / 'rejected'
    assert cli.main(command(case, guide, out, '--plus-only', '--config', str(config_path), *mode)) == 2
    assert not (out / 'CLI_CONTEXT.json').exists()


def test_plus_only_preview_is_audited_and_cannot_resume_as_generic(tmp_path, case, guide, monkeypatch):
    fail = forbid(monkeypatch)
    monkeypatch.setattr(cli, '_ledger_guard', fail)
    out = tmp_path / 'preview'
    assert cli.main(command(case, guide, out, '--plus-only')) == 0
    assert cli.read_json(out / 'CLI_CONTEXT.json')['plus_only'] is True
    invocation = next((out / 'cli_invocations').glob('*.json'))
    assert cli.read_json(invocation)['plus_only'] is True
    assert cli.main(command(case, guide, out)) == 2
    assert cli.read_json(out / 'CLI_EXCEPTION.json')['message'].endswith(':plus_only')


@pytest.mark.parametrize('role', ['writer', 'completer', 'late_completion'])
def test_factory_blocks_late_model_change_before_underlying_factory(role, tmp_path):
    calls = []
    def factory(*args):
        calls.append(args)
        return 'client'
    factory.execution_mode = 'live'
    factory.ledger_snapshot = lambda: {'remaining': 10}
    guarded = cli._PlusOnlyFactory(factory)
    assert guarded.execution_mode == 'live'
    assert guarded.ledger_snapshot() == {'remaining': 10}
    for profile in ({'model': 'qwen3-max'}, {'model': 'qwen3.5-max'}, {}, {'model': 'qwen3.5-plus-latest'}):
        with pytest.raises(ValueError, match='plus_only_requires_qwen3.5_plus'):
            guarded(role, tmp_path, profile)
    assert calls == []
    profile = {'model': 'qwen3.5-plus'}
    assert guarded(role, tmp_path, profile) == 'client'
    assert calls == [(role, tmp_path, profile)]


def test_profile_guard_checks_all_roles():
    with pytest.raises(ValueError, match=':completer$'):
        cli._require_plus_profiles({'writer': {'model': 'qwen3.5-plus'},
                                    'completer': {'model': 'qwen3-max'}})


def test_generic_explicit_max_opt_in_is_preserved():
    args = cli.parser().parse_args(['--manifest', 'source.json', '--guide', 'guide.json',
                                   '--output', 'out', '--run', '--allow-max'])
    assert not args.plus_only
    cli.shared._require_max_permission(args, {'writer': {'model': 'qwen3-max'}})


def test_plus_only_replay_preserves_recording_metadata_and_resume(tmp_path, case, guide, monkeypatch):
    forbid(monkeypatch)
    response = dump(tmp_path / 'responses.json', {'writer': {
        'body_markdown': 'The evidence supports a conditional finding [P0001].',
        'complete': True, 'remaining_content': []}})
    out = tmp_path / 'replay'
    cmd = command(case, guide, out, '--plus-only', '--responses', str(response))
    assert cli.main(cmd) == 0
    result = cli.read_json(out / 'CLI_RUN.json')
    assert result['execution_mode'] == 'recording' and result['complete']
    assert result['current_run_cost_cny'] == 0
    assert len(result['recorded_response_calls']) == 2
    assert cli.main(cmd) == 0
    assert cli.read_json(out / 'CLI_RUN.json')['recorded_response_calls'] == []


def test_main_dispatch_is_guarded_even_if_runtime_changes_completer(tmp_path, case, guide, monkeypatch):
    from optomind_research.runtime.upgrade3 import guided_body_writer as writer
    forbid(monkeypatch)
    response = dump(tmp_path / 'responses.json', {'writer': {'content': 'not consumed'}})
    def late_max(book, guide, output, config, *, client_factory, **kwargs):
        assert isinstance(client_factory, cli._PlusOnlyFactory)
        client_factory('completer', output, {'model': 'qwen3-max'})
        pytest.fail('Late Max request should never reach a provider')
    monkeypatch.setattr(writer, 'run_guided_body', late_max)
    out = tmp_path / 'late-max'
    assert cli.main(command(case, guide, out, '--plus-only', '--responses', str(response))) == 2
    assert cli.read_json(out / 'CLI_EXCEPTION.json')['message'] == 'plus_only_requires_qwen3.5_plus:completer'
