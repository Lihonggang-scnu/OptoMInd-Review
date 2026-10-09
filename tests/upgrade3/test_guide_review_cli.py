"""Bounded review CLI permission, provenance, and ledger tests. No paid calls."""
import argparse
from pathlib import Path

import pytest

from scripts.upgrade3 import guide_review as cli
from scripts.upgrade3 import guide_maker as maker
from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger


def test_parser_is_offline_and_dedicated_by_default():
    args = cli.parser().parse_args(['--manifest', 'm', '--guide', 'g', '--output', 'o'])
    assert not args.run and not args.allow_max
    assert args.budget_mode == 'dedicated'
    assert Path(args.config) == cli.DEFAULT_CONFIG
    for extra in (['--retry-failed'], ['--budget-mode', 'legacy'], ['--run', '--responses', 'r']):
        with pytest.raises(SystemExit):
            cli.parser().parse_args(['--manifest', 'm', '--guide', 'g', '--output', 'o', *extra])


def test_live_requires_max_permission_before_any_runtime_or_ledger(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail('No provider or ledger initialization permitted')
    monkeypatch.setattr(cli, '_dedicated_ledger_guard', forbidden)
    monkeypatch.setattr(cli, 'make_live_factory', forbidden)
    out = tmp_path / 'out'
    assert cli.main(['--manifest', 'missing', '--guide', 'missing', '--output', str(out), '--run']) == 2
    assert cli.read_json(out / 'CLI_EXCEPTION.json')['message'] == 'guide_review_live_requires_allow_max'


def test_archived_book_is_never_live(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail('No provider or ledger initialization permitted')
    monkeypatch.setattr(cli, '_dedicated_ledger_guard', forbidden)
    monkeypatch.setattr(cli, 'make_live_factory', forbidden)
    out = tmp_path / 'out'
    assert cli.main(['--book', 'missing', '--guide', 'missing', '--output', str(out), '--run', '--allow-max']) == 2
    assert cli.read_json(out / 'CLI_EXCEPTION.json')['message'] == 'archived_book_is_offline_only'


def context(mode='preview'):
    return {'execution_mode': mode, 'input_mode': 'live_source_manifest',
            'input_sha256': 'input', 'book_sha256': 'book', 'source_manifest_sha256': 'source',
            'guide_sha256': 'guide', 'guide_file_sha256': 'file',
            'config': {'max_model_calls': 2}, 'config_file_sha256': 'config',
            'meter': {}, 'implementation_hashes': {'code': 'hash'}}


@pytest.mark.parametrize('field', ['input_mode', 'input_sha256', 'book_sha256', 'source_manifest_sha256',
    'guide_sha256', 'guide_file_sha256', 'config', 'config_file_sha256', 'meter', 'implementation_hashes'])
def test_resume_is_strict_for_every_bound_input(tmp_path, field):
    original = context()
    cli._check_context(tmp_path, original)
    changed = {**original, field: 'changed'}
    if field == 'config':
        changed[field] = {'max_model_calls': 3}
    with pytest.raises(ValueError, match='new_output_directory'):
        cli._check_context(tmp_path, changed)
    assert cli.read_json(tmp_path / 'CLI_CONTEXT.json') == original


def test_live_binding_survives_preview_and_cannot_change(tmp_path):
    original = {**context('live'), 'budget_binding': {'mode': 'dedicated', 'ledger_path': 'a', 'limit_cny': 30}}
    cli._check_context(tmp_path, original)
    cli._check_context(tmp_path, context())
    assert cli.read_json(tmp_path / 'CLI_CONTEXT.json') == original
    changed = {**original, 'budget_binding': {**original['budget_binding'], 'ledger_path': 'b'}}
    with pytest.raises(ValueError, match='budget_changed'):
        cli._check_context(tmp_path, changed)


def test_replay_fixture_cannot_change(tmp_path):
    original = {**context('recording'), 'fixture_sha256': 'first'}
    cli._check_context(tmp_path, original)
    with pytest.raises(ValueError, match='recording_changed'):
        cli._check_context(tmp_path, {**original, 'fixture_sha256': 'second'})


def test_ledger_guard_is_shared_and_keeps_existing_generation_spend(tmp_path):
    assert cli._dedicated_ledger_guard is maker._dedicated_ledger_guard
    path = tmp_path / 'shared.sqlite'
    args = argparse.Namespace(budget_mode='dedicated', budget_ledger=str(path), budget_limit=30)
    maker._dedicated_ledger_guard(args)
    ledger = GlobalBudgetLedger(limit_cny=30, path=path)
    reservation = ledger.reserve(5, 'generation')
    ledger.settle(reservation['reservation_id'], 3)
    before = path.read_bytes()
    result = cli._dedicated_ledger_guard(args)
    assert result['actual_cny'] == 3 and result['remaining_cny'] == 27
    assert path.read_bytes() == before
    assert path.with_name(path.name + '.guide_maker.json').is_file()


@pytest.mark.parametrize('limit', [None, 31, 0, float('inf'), float('nan')])
def test_bad_budget_cap_never_creates_ledger(tmp_path, limit):
    path = tmp_path / 'new.sqlite'
    with pytest.raises(ValueError):
        cli._dedicated_ledger_guard(argparse.Namespace(budget_ledger=str(path), budget_limit=limit))
    assert not path.exists()


@pytest.mark.parametrize('ancestor', [False, True])
def test_output_collision_rejected_without_even_an_error_artifact(tmp_path, ancestor):
    baseline = tmp_path / 'maker'
    baseline.mkdir()
    guide = baseline / 'GUIDE.json'
    guide.write_text('{"baseline": "do not change"}', encoding='utf-8')
    output = tmp_path if ancestor else baseline
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert cli.main(['--manifest', 'missing', '--guide', str(guide), '--output', str(output)]) == 2
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert after == before


from test_fullbody_cli import case, dump, no_network  # noqa: E402,F401
from test_guide_maker_cli import guide_response  # noqa: E402


def review_command(tmp_path, case, *extra):
    guide = dump(tmp_path / 'baseline' / 'GUIDE.json', guide_response()['guide'])
    return ['--manifest', str(case['manifest']), '--guide', str(guide),
            '--output', str(tmp_path / 'review'), *extra]


def test_preview_has_no_provider_or_ledger_and_preserves_baseline(tmp_path, case, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail('Offline preview must never initialize provider or ledger')
    monkeypatch.setattr(cli, 'make_live_factory', forbidden)
    monkeypatch.setattr(cli, '_dedicated_ledger_guard', forbidden)
    args = review_command(tmp_path, case)
    baseline = (tmp_path / 'baseline' / 'GUIDE.json').read_bytes()
    assert cli.main(args) == 0
    result = cli.read_json(tmp_path / 'review' / 'CLI_RUN.json')
    assert result['execution_mode'] == 'preview' and result['model_calls'] == 0
    assert not result['writing_tested']
    context = cli.read_json(tmp_path / 'review' / 'CLI_CONTEXT.json')
    assert context['guide_file_sha256'] and context['guide_sha256']
    assert 'prompts/guide_review/review.md' in context['implementation_hashes']
    assert (tmp_path / 'baseline' / 'GUIDE.json').read_bytes() == baseline


def test_offline_two_role_replay_and_cache_resume(tmp_path, case, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail('Recording must never initialize provider or ledger')
    monkeypatch.setattr(cli, 'make_live_factory', forbidden)
    monkeypatch.setattr(cli, '_dedicated_ledger_guard', forbidden)
    fixture = dump(tmp_path / 'responses.json', {'reviewer': {'findings': [], 'limitations': []},
                                               'reviser': {'guide': guide_response()['guide'], 'decisions': []}})
    args = review_command(tmp_path, case, '--responses', str(fixture))
    assert cli.main(args) == 0
    result = cli.read_json(tmp_path / 'review' / 'CLI_RUN.json')
    assert result['complete'] and result['current_run_cost_cny'] == 0
    assert len(result['recorded_response_calls']) == 2
    assert (tmp_path / 'review' / 'REVISED_GUIDE.json').is_file()
    assert cli.main(args) == 0
    assert cli.read_json(tmp_path / 'review' / 'CLI_RUN.json')['recorded_response_calls'] == []


def test_baseline_byte_change_rejected_even_if_json_is_identical(tmp_path, case):
    args = review_command(tmp_path, case)
    assert cli.main(args) == 0
    baseline = tmp_path / 'baseline' / 'GUIDE.json'
    baseline.write_bytes(baseline.read_bytes() + b'\n')
    assert cli.main(args) == 2
    assert 'guide_file_sha256' in cli.read_json(tmp_path / 'review' / 'CLI_EXCEPTION.json')['message']
