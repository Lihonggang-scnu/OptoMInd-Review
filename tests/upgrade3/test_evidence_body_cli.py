"""Round-two formal entry checks; all provider traffic remains mocked locally."""
import argparse
import copy
import hashlib
import json
import sqlite3

import pytest

from scripts.upgrade3 import evidence_body_writer as cli
from optomind_research.runtime.upgrade3.module4 import runtime
from optomind_research.runtime.upgrade3.fullbody_contracts import seal_fullbody_input
from test_fullbody_cli import case, dump, no_network  # noqa: F401
from test_fullbody_integration import install_controlled_http


def args(case, output, *extra):
    return ['--manifest', str(case['manifest']), '--output', str(output), *extra]


def author(book):
    return {'body_markdown': '## CH01\n\nConditions delimit this evidence [P0001].\n\n## CH02\n\nComparison retains the conditions [P0001].',
            'completed_task_ids': list(book['task_catalog']), 'complete': True}


def forbid(monkeypatch):
    def fail(*a, **k):
        pytest.fail('Offline modes must never instantiate live transport or budget ledger')
    monkeypatch.setattr(runtime, 'QwenDirectClient', fail)
    monkeypatch.setattr(runtime, 'GlobalBudgetLedger', fail)
    monkeypatch.setattr(cli, 'make_live_factory', fail)


def test_default_limits_and_only_four_routes():
    options = cli.parser().parse_args(['--manifest', 'x'])
    assert options.route == 'packed_whole' and not options.run and not options.allow_max
    assert len(cli.ROUTES) == 4
    config = cli.read_json(cli.DEFAULT_CONFIG)
    for role in ('writer', 'reader', 'reviser', 'curator'):
        p = config[role]
        assert p['model'] == 'qwen3.5-plus' and p['stream']
        assert p['timeout_seconds'] == 1800 and p['stream_overall_timeout_seconds'] == 3600
        assert p['thinking_budget'] == 16384
        assert p['max_output_tokens'] == (24576 if role in ('reader', 'curator') else 49152)
        assert p['thinking_budget'] + p['max_output_tokens'] <= runtime.model_pricing(p['model'])['max_output_tokens']


def test_prepare_and_archive_seal_no_credentials(tmp_path, case, monkeypatch):
    forbid(monkeypatch)
    prepared = tmp_path / 'pinned.json'
    assert cli.main(['--manifest', str(case['manifest']), '--prepare-manifest', str(prepared), '--key-file', '/never-read']) == 0
    book, original = cli.load_body_manifest(prepared)
    archive = dump(tmp_path / 'FULL_BODY_INPUT.json', book)
    loaded, manifest = cli.load_book(archive)
    assert loaded == book and manifest == original
    book['user_request'] = 'tampered'
    dump(archive, book)
    with pytest.raises(ValueError, match='seal_mismatch'):
        cli.load_book(archive)


def test_old_ledger_cannot_be_reused_and_budget_is_absolute(tmp_path):
    ledger = tmp_path / 'old.sqlite'
    runtime.GlobalBudgetLedger(limit_cny=60, path=ledger)
    ns = argparse.Namespace(budget_ledger=str(ledger), budget_limit=60)
    with pytest.raises(ValueError, match='unmarked_ledger'):
        cli._ledger_guard(ns)
    ns.budget_ledger = str(tmp_path / 'new.sqlite')
    cli._ledger_guard(ns)
    cli._ledger_guard(ns)
    ns.budget_limit = 59
    with pytest.raises(ValueError, match='cap_or_identity_changed'):
        cli._ledger_guard(ns)
    for limit in (float('inf'), float('nan'), 61, 0, -1, None):
        ns.budget_limit = limit
        with pytest.raises(ValueError, match='finite_absolute_budget'):
            cli._ledger_guard(ns)


def test_archive_never_silently_becomes_paid_input(tmp_path, case, monkeypatch):
    forbid(monkeypatch)
    book, _ = cli.load_body_manifest(case['manifest'])
    archive = dump(tmp_path / 'FULL_BODY_INPUT.json', book)
    out = tmp_path / 'out'
    assert cli.main(['--book', str(archive), '--run', '--output', str(out)]) == 2
    assert 'offline_only' in cli.read_json(out / 'CLI_EXCEPTION.json')['message']


def test_historical_source_hash_mapping_preserves_identity_checks(tmp_path, case):
    book, _ = cli.load_body_manifest(case['manifest'])
    prior = copy.deepcopy(book)
    prior['input_manifest']['historical_external_fingerprint'] = 'older export compiler'
    prior = seal_fullbody_input(prior)
    dump(tmp_path / 'FULL_BODY_INPUT.json', prior)
    response = author(prior)
    response.update(body_sha256=hashlib.sha256(response['body_markdown'].encode()).hexdigest(), input_hash=cli._hash(prior))
    draft = dump(tmp_path / 'FULL_BODY_RESULT.json', response)
    with pytest.raises(ValueError, match='explicit_source_mapping'):
        cli.load_draft(draft, book)
    mapping = dump(tmp_path / 'mapping.json', {'old_input_hash': cli._hash(prior), 'new_input_hash': cli._hash(book), 'reason': 'Same scientific identities; external historical export fingerprint reconciled explicitly'})
    loaded, report = cli.load_draft(draft, book, mapping)
    assert loaded['input_hash'] == cli._hash(book) and loaded['historical_input_hash'] == cli._hash(prior)
    assert report['identity_and_task_scope_verified']
    response['body_sha256'] = 'wrong'
    dump(draft, response)
    with pytest.raises(ValueError, match='body_hash'):
        cli.load_draft(draft, book, mapping)


@pytest.mark.parametrize('route', ['packed_whole', 'packed_continuous', 'dossier_author'])
def test_formal_preview_no_model_or_keys(tmp_path, case, monkeypatch, route):
    forbid(monkeypatch)
    out = tmp_path / route
    assert cli.main(args(case, out, '--route', route, '--key-file', '/never-read')) == 0
    result = cli.read_json(out / 'CLI_RUN.json')
    assert result['execution_mode'] == 'preview'
    assert (out / 'EFFECTIVE_CONFIG.json').is_file()
    assert (out / 'SOURCE_MANIFEST.json').is_file()


def test_formal_replay_and_stable_resume_preserve_material(tmp_path, case, monkeypatch):
    forbid(monkeypatch)
    book, _ = cli.load_body_manifest(case['manifest'])
    fixture = dump(tmp_path / 'responses.json', {'writer': author(book)})
    out = tmp_path / 'recorded'
    command = args(case, out, '--responses', str(fixture))
    assert cli.main(command) == 0
    result = cli.read_json(out / 'CLI_RUN.json')
    assert result['complete'] and result['current_run_cost_cny'] == 0
    assert result['recorded_response_calls']
    messages = list(out.rglob('*MESSAGES*.json')) + list(out.rglob('messages.json'))
    assert messages
    serialized = '\n'.join(p.read_text() for p in messages)
    assert 'Complete useful observation' in serialized
    assert 'Write the entire body' in serialized
    assert cli.main(command) == 0
    assert cli.read_json(out / 'CLI_RUN.json')['recorded_response_calls'] == []


def test_formal_real_transport_boundary_stream_and_separate_budget(tmp_path, case, monkeypatch):
    book, _ = cli.load_body_manifest(case['manifest'])
    captured = install_controlled_http(monkeypatch, lambda payload: author(book))
    ledger = tmp_path / 'round-two.sqlite'
    out = tmp_path / 'live-boundary'
    command = args(case, out, '--run', '--budget-ledger', str(ledger), '--budget-limit', '60', '--key-file', '/not-a-real-key')
    assert cli.main(command) == 0
    assert len(captured) == 1
    wire = captured[0]['wire']
    assert wire['stream'] and wire['stream_options']['include_usage']
    assert captured[0]['timeout'] == 1800
    assert wire['thinking_budget'] == 16384 and wire['max_completion_tokens'] == 65536
    assert 'Complete useful observation' in json.dumps(wire['messages'])
    with sqlite3.connect(ledger) as db:
        rows = db.execute('SELECT status, actual_cny FROM reservations').fetchall()
    assert len(rows) == 1 and rows[0][0] == 'settled' and rows[0][1] > 0
    assert list(out.rglob('*.sse.raw'))
    assert cli.main(command) == 0 and len(captured) == 1


def test_budget_exhaustion_has_no_dispatch_and_safe_resume(tmp_path, case, monkeypatch):
    book, _ = cli.load_body_manifest(case['manifest'])
    captured = install_controlled_http(monkeypatch, lambda payload: author(book))
    ledger = tmp_path / 'tiny.sqlite'
    out = tmp_path / 'budget'
    command = args(case, out, '--run', '--budget-ledger', str(ledger), '--budget-limit', '0.000001')
    assert cli.main(command) in (2, 3)
    assert not captured
    assert cli.main(command) in (2, 3)
    assert not captured


def test_budget_exhaustion_explicit_retry_resumes_without_changing_cap(tmp_path, case, monkeypatch):
    book, _ = cli.load_body_manifest(case['manifest'])
    captured = install_controlled_http(monkeypatch, lambda payload: author(book))
    ledger_path = tmp_path / 'round-two.sqlite'
    cli._ledger_guard(argparse.Namespace(budget_ledger=str(ledger_path), budget_limit=60))
    ledger = runtime.GlobalBudgetLedger(limit_cny=60, path=ledger_path)
    blocked = ledger.reserve(59, 'other-in-progress-request')
    uncertain = ledger.reserve(1, 'unknown-spend')
    ledger.settle(uncertain['reservation_id'], None, uncertain=True)
    command = args(case, tmp_path / 'resumed', '--run', '--budget-ledger', str(ledger_path), '--budget-limit', '60')
    assert cli.main(command) == 3 and not captured
    ledger.settle(blocked['reservation_id'], 0)
    assert cli.main(command) == 3 and not captured  # no implicit retry
    assert cli.main(command + ['--retry-failed']) == 0 and len(captured) == 1
    with sqlite3.connect(ledger_path) as db:
        saved = db.execute('SELECT status, actual_cny FROM reservations WHERE call_id=?', ('unknown-spend',)).fetchone()
    assert saved == ('uncertain', None)


def test_scoped_revision_formal_entry_reuses_valid_draft(tmp_path, case, monkeypatch):
    forbid(monkeypatch)
    book, _ = cli.load_body_manifest(case['manifest'])
    fixture = dump(tmp_path / 'author.json', {'writer': author(book)})
    base = tmp_path / 'base'
    assert cli.main(args(case, base, '--responses', str(fixture))) == 0
    readings = dump(tmp_path / 'reader.json', {'reader': {'issues': [], 'complete': True}})
    out = tmp_path / 'revision'
    command = args(case, out, '--route', 'scoped_revision', '--draft', str(base / 'FULL_BODY_RESULT.json'), '--responses', str(readings))
    assert cli.main(command) == 0
    result = cli.read_json(out / 'CLI_RUN.json')
    assert result['complete'] and result['body_markdown'] == author(book)['body_markdown']
    assert len(result['recorded_response_calls']) == 1
    assert cli.main(command) == 0
    assert cli.read_json(out / 'CLI_RUN.json')['recorded_response_calls'] == []
