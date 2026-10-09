"""Guide-first BODY writer: explicit guide, frozen source inputs, offline by default.

Live execution reuses the existing marked round-two CNY 60 lifetime ledger.
This entry point never creates a budget ledger or grants a fresh allowance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.upgrade3 import evidence_body_writer as evidence
from scripts.upgrade3 import writer_candidates as shared

DEFAULT_CONFIG = PROJECT_ROOT / 'config/guided_body_writer/plus_first.json'
read_json, write_json, sha256_file = evidence.read_json, evidence.write_json, evidence.sha256_file
load_body_manifest, load_book = evidence.load_body_manifest, evidence.load_book
make_live_factory, RecordingFactory = shared.make_live_factory, evidence.RecordingFactory
tokenizer_counter, _hash = shared.tokenizer_counter, evidence._hash


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--manifest', help='Genuine source manifest; source versions are verified and frozen')
    source.add_argument('--book', help='Sealed FULL_BODY_INPUT.json; offline archive/replay only')
    p.add_argument('--guide', required=True, help='Explicit manuscript guide and per-chapter writing arrangements JSON')
    p.add_argument('--output', required=True)
    p.add_argument('--config', default=str(DEFAULT_CONFIG))
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--run', action='store_true', help='Explicit paid execution using the original remaining budget')
    mode.add_argument('--responses', help='Offline recorded-response fixture')
    models = p.add_mutually_exclusive_group()
    models.add_argument('--allow-max', action='store_true', help='Explicit permission only; never automatically selects Max')
    models.add_argument('--plus-only', action='store_true', help='Restrict this test entry and every author/completer dispatch to qwen3.5-plus; no Max fallback')
    p.add_argument('--retry-failed', action='store_true')
    p.add_argument('--metadata-declarations', help='JSON list of explicit human completion declarations bound to saved stage/response/body hashes; no rewrite')
    p.add_argument('--budget-ledger', help='Existing original round-two SQLite ledger, with its original marker')
    p.add_argument('--budget-limit', type=float, help='Original lifetime ceiling, exactly 60 CNY, not a new allowance')
    p.add_argument('--key-file')
    p.add_argument('--tokenizer')
    return p


def _require_plus_profiles(profiles):
    """Exact allowlist for an explicitly Plus-only run, including late stages."""
    rejected = [role for role, profile in profiles.items()
                if profile.get('model') != 'qwen3.5-plus']
    if rejected:
        raise ValueError('plus_only_requires_qwen3.5_plus:' + ','.join(rejected))


class _PlusOnlyFactory:
    """Check before the underlying factory can construct a ledger or client."""
    def __init__(self, factory):
        self._factory = factory

    def __getattr__(self, name):
        return getattr(self._factory, name)

    def __call__(self, role, stage_dir, profile):
        _require_plus_profiles({role: profile})
        return self._factory(role, stage_dir, profile)


def _ledger_guard(args):
    """Read-only verification; unlike the old round-two initializer, never create."""
    if not args.budget_ledger or args.budget_limit is None or not math.isfinite(args.budget_limit) or args.budget_limit != 60:
        raise ValueError('guided_live_requires_original_round_two_ledger_and_absolute_limit_60_CNY')
    ledger = Path(args.budget_ledger).expanduser().resolve()
    marker = ledger.with_name(ledger.name + '.evidence_round2.json')
    if not ledger.is_file() or not marker.is_file():
        raise ValueError('original_round_two_ledger_and_marker_must_already_exist:no_new_allowance')
    expected = {'schema_version': 'optomind.evidence_round2_budget.v1', 'ledger_path': str(ledger), 'limit_cny': 60}
    if read_json(marker) != expected:
        raise ValueError('original_round_two_ledger_cap_or_identity_changed')
    try:
        # mode=ro prevents SQLite from creating a file, initializing tables, or
        # rewriting a stored limit during preflight.
        with sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
            if row is None or not math.isfinite(float(row[0])) or float(row[0]) != 60:
                raise ValueError('original_round_two_stored_limit_must_equal_60_CNY')
            reservations = db.execute('SELECT amount_cny, actual_cny, status FROM reservations').fetchall()
    except sqlite3.Error as exc:
        raise ValueError('original_round_two_ledger_invalid_sqlite') from exc
    actual, held, used = 0.0, 0.0, 0.0
    for amount, cost, status in reservations:
        if status not in ('settled', 'reserved', 'uncertain'):
            raise ValueError('original_round_two_ledger_invalid_reservation_status')
        if amount is None or not math.isfinite(float(amount)) or float(amount) < 0 or (cost is not None and (not math.isfinite(float(cost)) or float(cost) < 0)):
            raise ValueError('original_round_two_ledger_invalid_reservation_amount')
        actual += float(cost or 0)
        held += float(amount) if status in ('reserved', 'uncertain') else 0
        used += float(cost if cost is not None else amount) if status == 'settled' else float(amount)
    return {'ledger_path': str(ledger), 'limit_cny': 60.0, 'actual_cny': actual,
            'reserved_cny': held, 'remaining_cny': max(0.0, 60.0 - used),
            'reservation_count': len(reservations), 'marker_sha256': sha256_file(marker),
            'budget_policy': 'reuse_original_round_two_lifetime_remaining_balance'}


def _check_context(output, context):
    path = output / 'CLI_CONTEXT.json'
    if path.is_file():
        prior = read_json(path)
        for field in ('guide_sha256', 'guide_file_sha256', 'input_sha256', 'book_sha256',
                      'source_manifest_sha256', 'config', 'meter', 'cli_source_sha256', 'plus_only'):
            if prior.get(field) != context.get(field):
                raise ValueError('guided_input_or_configuration_changed_use_new_output_directory:' + field)
    shared._execution_context(output, context)


def main(argv=None):
    args = parser().parse_args(argv)
    output = Path(args.output).expanduser().resolve()
    try:
        if args.book and args.run:
            raise ValueError('archived_book_is_offline_only')
        from optomind_research.runtime.upgrade3.guided_body_contracts import validate_guide
        from optomind_research.runtime.upgrade3.guided_body_writer import run_guided_body, validate_config
        book, prepared = load_book(args.book) if args.book else load_body_manifest(args.manifest)
        guide = validate_guide(read_json(args.guide), book)
        config = read_json(args.config)
        shared._no_secrets(config)
        config = validate_config(config)
        profiles = {role: value for role, value in config.items() if isinstance(value, dict) and 'model' in value}
        if args.plus_only:
            _require_plus_profiles(profiles)
        shared._require_max_permission(args, profiles)
        counter, meter = tokenizer_counter(args.tokenizer)
        mode = 'live' if args.run else 'recording' if args.responses else 'preview'
        context = {'schema_version': 'optomind.guided_body_cli.v1', 'execution_mode': mode,
                   'input_mode': 'offline_archive' if args.book else 'live_source_manifest',
                   'input_sha256': sha256_file(args.book or args.manifest), 'book_sha256': book['book_sha256'],
                   'guide_sha256': _hash(guide), 'guide_file_sha256': sha256_file(args.guide),
                   'source_manifest_sha256': _hash(prepared), 'config': config, 'meter': meter,
                   'cli_source_sha256': sha256_file(__file__), 'plus_only': args.plus_only,
                   'semantic_quality_unreviewed': True}
        snapshot = output / 'SOURCE_MANIFEST.json'
        if snapshot.is_file() and _hash(read_json(snapshot)) != _hash(prepared):
            raise ValueError('input_source_versions_changed_use_new_output_directory')
        _check_context(output, context)
        budget = _ledger_guard(args) if args.run else None
        write_json(snapshot, prepared)
        write_json(output / 'GUIDE.json', guide)
        write_json(output / 'EFFECTIVE_CONFIG.json', config)
        factory = make_live_factory(args, token_counter=counter) if args.run else RecordingFactory(args.responses) if args.responses else None
        dispatch_factory = _PlusOnlyFactory(factory) if args.plus_only and factory is not None else factory
        invocation = output / 'cli_invocations' / (uuid.uuid4().hex + '.json')
        write_json(invocation, {**context, 'started_at': datetime.now(timezone.utc).isoformat(),
                               'allow_max': args.allow_max, 'retry_failed': args.retry_failed,
                               'fixture_sha256': getattr(factory, 'fixture_sha256', None), 'budget_before': budget})
        declarations = read_json(args.metadata_declarations) if args.metadata_declarations else None
        result = run_guided_body(book, guide, output, config, client_factory=dispatch_factory,
                                 run=bool(args.run or args.responses), retry_failed=args.retry_failed, token_counter=counter, metadata_declarations=declarations)
        result = {**result, 'execution_mode': mode, 'input_mode': context['input_mode'], 'meter': meter,
                  'cli_invocation': str(invocation), 'guide_sha256': context['guide_sha256'],
                  'source_manifest_sha256': _hash(prepared), 'semantic_quality_unreviewed': True,
                  'expected_chapter_ids': prepared['expected_chapter_ids'], 'actual_chapter_ids': prepared['actual_chapter_ids']}
        if args.run:
            result['budget_before'] = budget
            result['budget_after'] = _ledger_guard(args)
        if factory is not None and hasattr(factory, 'ledger_snapshot'):
            result['budget'] = factory.ledger_snapshot()
        if isinstance(factory, RecordingFactory):
            result.update(recorded_response_calls=factory.calls, current_run_cost_cny=0.0)
        write_json(output / 'CLI_RUN.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if mode == 'preview' or result.get('complete') is True else 3
    except (ValueError, OSError, RuntimeError) as exc:
        error = {'error': type(exc).__name__, 'message': str(exc), 'route': 'guided_body'}
        write_json(output / 'CLI_EXCEPTION.json', error)
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
