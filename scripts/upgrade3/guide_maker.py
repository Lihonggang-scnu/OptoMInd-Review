"""Independent outline-to-guide middleware, offline preview by default.

GUIDE.json is input to the existing guided BODY writer; generation does not run
or validate manuscript writing. Legacy runs reuse the original CNY 60 lifetime
ledger; explicit dedicated mode uses a guide-only lifetime cap of at most CNY 30.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import sqlite3
from pathlib import Path
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.upgrade3 import evidence_body_writer as evidence
from scripts.upgrade3 import guided_body_writer as guided
from scripts.upgrade3 import writer_candidates as shared

DEFAULT_CONFIG = PROJECT_ROOT / 'config/guide_maker/plus_first.json'
read_json, write_json, sha256_file = evidence.read_json, evidence.write_json, evidence.sha256_file
load_body_manifest, load_book = evidence.load_body_manifest, evidence.load_book
make_live_factory, RecordingFactory = shared.make_live_factory, evidence.RecordingFactory
tokenizer_counter, _hash = shared.tokenizer_counter, evidence._hash
# Keep the original ledger identity, marker and read-only lifetime-budget check.
_ledger_guard = guided._ledger_guard


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--manifest', help='Genuine source manifest; source versions are verified and frozen')
    source.add_argument('--book', help='Sealed FULL_BODY_INPUT.json; offline archive/replay only')
    p.add_argument('--feedback', help='Optional UTF-8 writing feedback file')
    p.add_argument('--output', required=True)
    p.add_argument('--config', default=str(DEFAULT_CONFIG))
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--run', action='store_true', help='Explicit future local paid execution; do not run alongside writer A/B')
    mode.add_argument('--responses', help='Offline recorded-response fixture')
    p.add_argument('--allow-max', action='store_true', help='Explicit permission; never automatically selects Max')
    p.add_argument('--retry-failed', action='store_true')
    p.add_argument('--budget-mode', choices=('legacy', 'dedicated'), default='legacy', help='Explicit dedicated guide ledger; legacy remains the default')
    p.add_argument('--budget-ledger', help='Legacy original round-two ledger, or explicitly selected dedicated guide ledger')
    p.add_argument('--budget-limit', type=float, help='Lifetime ceiling: legacy exactly 60 CNY; dedicated at most 30 CNY; never resets spend')
    p.add_argument('--key-file')
    p.add_argument('--tokenizer')
    return p


def _dedicated_ledger_guard(args):
    """Explicit guide-only capped ledger; reuse durable atomic accounting."""
    limit = args.budget_limit
    if not args.budget_ledger or limit is None or isinstance(limit, bool) or not math.isfinite(limit) or not 0 < limit <= 30:
        raise ValueError('dedicated_guide_budget_requires_finite_cap_at_most_30_CNY')
    ledger = Path(args.budget_ledger).expanduser().resolve()
    marker = ledger.with_name(ledger.name + '.guide_maker.json')
    expected = {'schema_version': 'optomind.guide_maker_budget.v1', 'ledger_path': str(ledger), 'limit_cny': float(limit)}
    if ledger.exists() and not marker.is_file():
        raise ValueError('dedicated_guide_ledger_must_be_new_or_previously_marked')
    if marker.is_file() and read_json(marker) != expected:
        raise ValueError('dedicated_guide_ledger_identity_or_limit_changed')
    if marker.exists() and not ledger.is_file():
        raise ValueError('dedicated_guide_ledger_missing:no_budget_reset')
    if not ledger.exists():
        from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
        GlobalBudgetLedger(limit_cny=float(limit), path=ledger)
        write_json(marker, expected)
    # Existing ledgers are checked read-only before a client can initialize or
    # migrate anything. Empty/corrupt databases must never become new budgets.
    try:
        with sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
            if row is None or not math.isfinite(float(row[0])) or float(row[0]) != float(limit):
                raise ValueError('dedicated_guide_stored_limit_changed')
            reservations = db.execute('SELECT amount_cny, actual_cny, status FROM reservations').fetchall()
    except sqlite3.Error as exc:
        raise ValueError('dedicated_guide_ledger_invalid_sqlite') from exc
    actual, held, used = 0.0, 0.0, 0.0
    for amount, cost, status in reservations:
        if status not in ('settled', 'reserved', 'uncertain'):
            raise ValueError('dedicated_guide_invalid_reservation_status')
        if amount is None or not math.isfinite(float(amount)) or float(amount) < 0 or (cost is not None and (not math.isfinite(float(cost)) or float(cost) < 0)):
            raise ValueError('dedicated_guide_invalid_reservation_amount')
        actual += float(cost or 0)
        held += float(amount) if status in ('reserved', 'uncertain') else 0
        used += float(cost if cost is not None else amount) if status == 'settled' else float(amount)
    if not all(math.isfinite(value) for value in (actual, held, used)):
        raise ValueError('dedicated_guide_invalid_total')
    return {'ledger_path': str(ledger), 'limit_cny': float(limit), 'actual_cny': actual,
            'reserved_cny': held, 'remaining_cny': max(0.0, float(limit) - used),
            'reservation_count': len(reservations), 'marker_sha256': sha256_file(marker),
            'budget_policy': 'dedicated_guide_lifetime_cap'}


def _execution_budget_guard(args):
    if getattr(args, 'budget_mode', 'legacy') == 'dedicated':
        return _dedicated_ledger_guard(args)
    return _ledger_guard(args)


def _implementation_hashes():
    """Bind replay/resume to code and guidance, including shared input adapters."""
    root = PROJECT_ROOT / 'optomind_research/runtime/upgrade3'
    paths = [Path(__file__), Path(evidence.__file__), Path(shared.__file__),
             Path(guided.__file__), PROJECT_ROOT / 'scripts/upgrade3/fullbody_writer.py']
    paths += [root / name for name in ('guide_maker.py', 'guide_maker_contracts.py', 'json_format_recovery.py', 'chapter_arrangement.py',
        'guided_body_contracts.py', 'writing_evidence.py', 'fullbody_contracts.py',
        'fullbody_writer.py', 'writer_candidates.py', 'writer_candidates_contracts.py', 'module4/runtime.py')]
    paths += sorted((PROJECT_ROOT / 'prompts/guide_maker').rglob('*.md'))
    return {path.relative_to(PROJECT_ROOT).as_posix(): sha256_file(path) for path in paths}


def _check_context(output, context):
    path = output / 'CLI_CONTEXT.json'
    if path.is_file():
        prior = read_json(path)
        previous_config = prior.get('config', {})
        next_config = context.get('config', {})
        old_cap, new_cap = previous_config.get('max_model_calls'), next_config.get('max_model_calls')
        cap_increase = (type(old_cap) is int and type(new_cap) is int and new_cap > old_cap
            and {k: v for k, v in previous_config.items() if k != 'max_model_calls'}
            == {k: v for k, v in next_config.items() if k != 'max_model_calls'})
        for field in ('input_sha256', 'book_sha256', 'source_manifest_sha256', 'feedback_sha256',
                      'feedback_file_sha256', 'config', 'config_file_sha256', 'meter', 'implementation_hashes'):
            if cap_increase and field in ('config', 'config_file_sha256'):
                continue
            if prior.get(field) != context.get(field):
                raise ValueError('guide_maker_input_or_configuration_changed_use_new_output_directory:' + field)
        # A preview may raise the bound but must retain the completed work's
        # execution provenance, just as the shared context guard normally does.
        if cap_increase and context['execution_mode'] == 'preview' and prior.get('execution_mode') not in (None, 'preview'):
            context = {**context, 'execution_mode': prior['execution_mode']}
            if 'budget_binding' in prior:
                context['budget_binding'] = prior['budget_binding']
    shared._execution_context(output, context)


def main(argv=None):
    args = parser().parse_args(argv)
    output = Path(args.output).expanduser().resolve()
    try:
        if args.book and args.run:
            raise ValueError('archived_book_is_offline_only')
        from optomind_research.runtime.upgrade3.guide_maker import run_guide_maker, validate_config
        from optomind_research.runtime.upgrade3.guide_maker_contracts import compile_guide_input
        config = read_json(args.config)
        shared._no_secrets(config)
        config = validate_config(config)
        shared._require_max_permission(args, {'maker': config['maker']})
        book, prepared = load_book(args.book) if args.book else load_body_manifest(args.manifest)
        feedback = Path(args.feedback).expanduser().read_text(encoding='utf-8') if args.feedback else None
        # Exercise actual source parsing before any ledger or live client setup.
        compile_guide_input(book, feedback=feedback)
        counter, meter = tokenizer_counter(args.tokenizer)
        mode = 'live' if args.run else 'recording' if args.responses else 'preview'
        context = {'schema_version': 'optomind.guide_maker_cli.v1', 'execution_mode': mode,
                   'input_mode': 'offline_archive' if args.book else 'live_source_manifest',
                   'input_sha256': sha256_file(args.book or args.manifest), 'book_sha256': book['book_sha256'],
                   'source_manifest_sha256': _hash(prepared), 'feedback_sha256': _hash(feedback),
                   'feedback_file_sha256': sha256_file(args.feedback) if args.feedback else None,
                   'config': config, 'config_file_sha256': sha256_file(args.config), 'meter': meter,
                   'implementation_hashes': _implementation_hashes(), 'semantic_quality_unreviewed': True,
                   'writing_tested': False}
        snapshot = output / 'SOURCE_MANIFEST.json'
        if snapshot.is_file() and _hash(read_json(snapshot)) != _hash(prepared):
            raise ValueError('input_source_versions_changed_use_new_output_directory')
        if args.run:
            binding = {'mode': args.budget_mode, 'ledger_path': str(Path(args.budget_ledger).expanduser().resolve()) if args.budget_ledger else None, 'limit_cny': args.budget_limit}
            prior_context_path = output / 'CLI_CONTEXT.json'
            if prior_context_path.is_file():
                prior_context = read_json(prior_context_path)
                if prior_context.get('execution_mode') == 'live' and prior_context.get('budget_binding') != binding:
                    raise ValueError('guide_live_resume_budget_changed')
            context['budget_binding'] = binding
        _check_context(output, context)
        budget = _execution_budget_guard(args) if args.run else None
        write_json(snapshot, prepared)
        write_json(output / 'EFFECTIVE_CONFIG.json', config)
        factory = make_live_factory(args, token_counter=counter) if args.run else RecordingFactory(args.responses) if args.responses else None
        invocation = output / 'cli_invocations' / (uuid.uuid4().hex + '.json')
        write_json(invocation, {**context, 'started_at': datetime.now(timezone.utc).isoformat(),
                               'allow_max': args.allow_max, 'retry_failed': args.retry_failed,
                               'fixture_sha256': getattr(factory, 'fixture_sha256', None), 'budget_before': budget})
        result = run_guide_maker(book, output, config, client_factory=factory,
                                run=bool(args.run or args.responses), retry_failed=args.retry_failed,
                                token_counter=counter, feedback=feedback)
        result = {**result, 'execution_mode': mode, 'input_mode': context['input_mode'], 'meter': meter,
                  'cli_invocation': str(invocation), 'source_manifest_sha256': _hash(prepared),
                  'semantic_quality_unreviewed': True, 'writing_tested': False,
                  'generation_complete': result.get('complete') is True,
                  'expected_chapter_ids': prepared['expected_chapter_ids'], 'actual_chapter_ids': prepared['actual_chapter_ids']}
        if args.run:
            result['budget_before'] = budget
            result['budget_after'] = _execution_budget_guard(args)
        if factory is not None and hasattr(factory, 'ledger_snapshot'):
            result['budget'] = factory.ledger_snapshot()
        if isinstance(factory, RecordingFactory):
            result.update(recorded_response_calls=factory.calls, current_run_cost_cny=0.0)
        write_json(output / 'CLI_RUN.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if mode == 'preview' or result.get('complete') is True else 3
    except (ValueError, OSError, RuntimeError) as exc:
        error = {'error': type(exc).__name__, 'message': str(exc), 'route': 'guide_maker'}
        write_json(output / 'CLI_EXCEPTION.json', error)
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
