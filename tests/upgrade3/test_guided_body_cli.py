"""Guide-first entry boundary. All execution and provider traffic are local/mock."""
import argparse
import sqlite3

import pytest

from scripts.upgrade3 import guided_body_writer as cli
from optomind_research.runtime.upgrade3.module4 import runtime
from test_fullbody_cli import case, dump, no_network  # noqa: F401


@pytest.fixture
def guide(tmp_path):
    return dump(tmp_path / 'guide.json', {
        'manuscript_guide': 'Explain how evidence conditions constrain interpretation for new researchers.',
        'chapters': [{'chapter_id': chapter_id, 'title': chapter_id,
                      'writing_arrangement': 'Develop the conditional finding and its limits.',
                      'required_content': ['Explain the evidence conditions'], 'source_handles': ['P0001']}
                     for chapter_id in ('CH01', 'CH02')]})


def command(case, guide, output, *extra):
    return ['--manifest', str(case['manifest']), '--guide', str(guide), '--output', str(output), *extra]


def forbid(monkeypatch):
    def fail(*a, **kw):
        pytest.fail('Offline or invalid entry must never create a live factory or ledger')
    monkeypatch.setattr(cli, 'make_live_factory', fail)
    monkeypatch.setattr(runtime, 'QwenDirectClient', fail)
    monkeypatch.setattr(runtime, 'GlobalBudgetLedger', fail)
    return fail


def existing_ledger(tmp_path):
    ledger = tmp_path / 'original-round-two.sqlite'
    instance = runtime.GlobalBudgetLedger(limit_cny=60, path=ledger)
    reservation = instance.reserve(8, 'previous-round-call')
    instance.settle(reservation['reservation_id'], 6.638)
    uncertain = instance.reserve(1.5, 'previous-uncertain-call')
    instance.settle(uncertain['reservation_id'], None, uncertain=True)
    marker = ledger.with_name(ledger.name + '.evidence_round2.json')
    dump(marker, {'schema_version': 'optomind.evidence_round2_budget.v1', 'ledger_path': str(ledger.resolve()), 'limit_cny': 60})
    return ledger, marker


def test_guide_is_actual_required_flag_and_no_plan_override():
    with pytest.raises(SystemExit):
        cli.parser().parse_args(['--manifest', 'source.json', '--output', 'out'])
    args = cli.parser().parse_args(['--manifest', 'source.json', '--guide', 'guide.json', '--output', 'out'])
    assert not args.run and not args.allow_max
    with pytest.raises(SystemExit):
        cli.parser().parse_args(['--manifest', 'source.json', '--guide', 'guide.json', '--output', 'out', '--plan', 'changed.json'])


def test_preview_uses_guide_without_credentials(tmp_path, case, guide, monkeypatch):
    fail = forbid(monkeypatch)
    monkeypatch.setattr(cli, '_ledger_guard', fail)
    out = tmp_path / 'preview'
    assert cli.main(command(case, guide, out, '--key-file', '/must-never-be-read')) == 0
    result = cli.read_json(out / 'CLI_RUN.json')
    assert result['execution_mode'] == 'preview'
    context = cli.read_json(out / 'CLI_CONTEXT.json')
    assert context['guide_file_sha256'] == cli.sha256_file(guide)
    assert context['guide_sha256'] and context['book_sha256'] and context['meter']
    assert (out / 'SOURCE_MANIFEST.json').is_file()
    assert (out / 'GUIDE.json').is_file()


@pytest.mark.parametrize('invalid', [{}, {'manuscript_guide': 'Explain', 'chapters': []},
    {'manuscript_guide': 'Explain', 'chapters': [{'chapter_id': 'CH01', 'title': 'One'}]}])
def test_invalid_live_guide_denied_before_factory_or_ledger(tmp_path, case, guide, monkeypatch, invalid):
    fail = forbid(monkeypatch)
    monkeypatch.setattr(cli, '_ledger_guard', fail)
    dump(guide, invalid)
    assert cli.main(command(case, guide, tmp_path / 'invalid', '--run')) == 2


def test_missing_live_guide_denied_before_factory_or_ledger(tmp_path, case, monkeypatch):
    fail = forbid(monkeypatch)
    monkeypatch.setattr(cli, '_ledger_guard', fail)
    assert cli.main(command(case, tmp_path / 'absent.json', tmp_path / 'missing', '--run')) == 2


def test_invalid_config_denied_before_ledger(tmp_path, case, guide, monkeypatch):
    fail = forbid(monkeypatch)
    monkeypatch.setattr(cli, '_ledger_guard', fail)
    config = cli.read_json(cli.DEFAULT_CONFIG)
    config['writer']['max_output_tokens'] = -1
    path = dump(tmp_path / 'bad-config.json', config)
    assert cli.main(command(case, guide, tmp_path / 'invalid-config', '--run', '--config', str(path))) == 2


def test_archive_never_becomes_live(tmp_path, case, guide, monkeypatch):
    fail = forbid(monkeypatch)
    monkeypatch.setattr(cli, '_ledger_guard', fail)
    book, _ = cli.load_body_manifest(case['manifest'])
    archive = dump(tmp_path / 'FULL_BODY_INPUT.json', book)
    assert cli.main(['--book', str(archive), '--guide', str(guide), '--output', str(tmp_path / 'out'), '--run']) == 2


def test_live_cannot_create_a_fresh_budget(tmp_path, case, guide, monkeypatch):
    forbid(monkeypatch)
    ledger = tmp_path / 'fresh.sqlite'
    assert cli.main(command(case, guide, tmp_path / 'out', '--run', '--budget-ledger', str(ledger), '--budget-limit', '60')) == 2
    assert not ledger.exists()
    assert not ledger.with_name(ledger.name + '.evidence_round2.json').exists()


def test_budget_guard_is_read_only_reuses_actual_spend_and_uncertain_holds(tmp_path):
    ledger, marker = existing_ledger(tmp_path)
    before = (ledger.read_bytes(), marker.read_bytes())
    args = argparse.Namespace(budget_ledger=str(ledger), budget_limit=60)
    result = cli._ledger_guard(args)
    assert result['actual_cny'] == pytest.approx(6.638)
    assert result['reserved_cny'] == 1.5
    assert result['remaining_cny'] == pytest.approx(60 - 6.638 - 1.5)
    assert (ledger.read_bytes(), marker.read_bytes()) == before
    for limit in (None, 59, 61, 0, float('inf'), float('nan')):
        args.budget_limit = limit
        with pytest.raises(ValueError, match='absolute_limit_60'):
            cli._ledger_guard(args)
    assert (ledger.read_bytes(), marker.read_bytes()) == before


def test_ledger_guard_requires_original_marker_identity_and_stored_cap(tmp_path):
    ledger, marker = existing_ledger(tmp_path)
    args = argparse.Namespace(budget_ledger=str(ledger), budget_limit=60)
    data = cli.read_json(marker)
    data['ledger_path'] = str(tmp_path / 'different.sqlite')
    dump(marker, data)
    with pytest.raises(ValueError, match='identity_changed'):
        cli._ledger_guard(args)
    data['ledger_path'] = str(ledger.resolve())
    dump(marker, data)
    with sqlite3.connect(ledger) as db:
        db.execute("UPDATE budget_meta SET value='100' WHERE key='limit_cny'")
    with pytest.raises(ValueError, match='stored_limit'):
        cli._ledger_guard(args)
    marker.unlink()
    with pytest.raises(ValueError, match='must_already_exist'):
        cli._ledger_guard(args)
    assert not marker.exists()


def test_non_sqlite_file_is_not_an_existing_budget(tmp_path):
    ledger, marker = existing_ledger(tmp_path)
    ledger.write_text('not a ledger')
    with pytest.raises(ValueError, match='invalid_sqlite'):
        cli._ledger_guard(argparse.Namespace(budget_ledger=str(ledger), budget_limit=60))


def test_guide_change_requires_new_output_directory(tmp_path, case, guide, monkeypatch):
    forbid(monkeypatch)
    out = tmp_path / 'out'
    assert cli.main(command(case, guide, out)) == 0
    prior = cli.read_json(out / 'CLI_CONTEXT.json')
    changed = cli.read_json(guide)
    changed['manuscript_guide'] += ' A materially different organizing argument.'
    dump(guide, changed)
    assert cli.main(command(case, guide, out)) == 2
    assert cli.read_json(out / 'CLI_CONTEXT.json') == prior
    assert 'new_output_directory' in cli.read_json(out / 'CLI_EXCEPTION.json')['message']


def test_offline_replay_and_resume_have_zero_new_cost(tmp_path, case, guide, monkeypatch):
    forbid(monkeypatch)
    response = dump(tmp_path / 'responses.json', {'writer': {
        'body_markdown': 'The evidence supports a conditional finding [P0001].',
        'complete': True, 'remaining_content': []}})
    out = tmp_path / 'replay'
    cmd = command(case, guide, out, '--responses', str(response))
    assert cli.main(cmd) == 0
    result = cli.read_json(out / 'CLI_RUN.json')
    assert result['execution_mode'] == 'recording' and result['complete']
    assert result['current_run_cost_cny'] == 0 and len(result['recorded_response_calls']) == 2
    assert cli.main(cmd) == 0
    assert cli.read_json(out / 'CLI_RUN.json')['recorded_response_calls'] == []


def test_mock_live_reuses_original_ledger_without_resetting_spend(tmp_path, case, guide, monkeypatch):
    from test_fullbody_integration import install_controlled_http
    ledger, marker = existing_ledger(tmp_path)
    marker_before = marker.read_bytes()
    captured = install_controlled_http(monkeypatch, lambda payload: {
        'body_markdown': 'A conditional evidence finding [P0001].', 'complete': True, 'remaining_content': []})
    out = tmp_path / 'mock-live'
    cmd = command(case, guide, out, '--run', '--budget-ledger', str(ledger), '--budget-limit', '60', '--key-file', '/mock-key')
    assert cli.main(cmd) == 0
    result = cli.read_json(out / 'CLI_RUN.json')
    assert result['budget_before']['actual_cny'] == pytest.approx(6.638)
    assert result['budget_after']['actual_cny'] > 6.638
    assert result['budget_after']['reserved_cny'] == 1.5
    assert result['budget_after']['limit_cny'] == 60
    assert len(captured) == 2
    assert marker.read_bytes() == marker_before
    assert cli.main(cmd) == 0 and len(captured) == 2


def test_unapproved_max_denied_before_original_ledger_access(tmp_path, case, guide, monkeypatch):
    fail = forbid(monkeypatch)
    monkeypatch.setattr(cli, '_ledger_guard', fail)
    config = cli.read_json(cli.DEFAULT_CONFIG)
    config['writer']['model'] = 'qwen3.8-max'
    path = dump(tmp_path / 'max.json', config)
    out = tmp_path / 'out'
    assert cli.main(command(case, guide, out, '--run', '--config', str(path))) == 2
    assert 'allow_max' in cli.read_json(out / 'CLI_EXCEPTION.json')['message']
