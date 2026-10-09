"""Optional, bounded GUIDE review experiment; offline preview by default.

Requires a complete baseline GUIDE. Live execution needs explicit Max permission
and a dedicated, at-most-CNY-30 lifetime ledger using shared guide accounting.
This route never writes a manuscript or replaces the baseline GUIDE.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.upgrade3 import guide_maker as maker
from scripts.upgrade3 import writer_candidates as shared

DEFAULT_CONFIG = PROJECT_ROOT / 'config/guide_review/plus_max_once.json'
read_json, write_json, sha256_file = maker.read_json, maker.write_json, maker.sha256_file
load_body_manifest, load_book = maker.load_body_manifest, maker.load_book
make_live_factory, RecordingFactory = maker.make_live_factory, maker.RecordingFactory
tokenizer_counter, _hash = maker.tokenizer_counter, maker._hash
# Deliberately reuse generation's marker and accounting; never create a separate
# review allowance or reinterpret a legacy CNY-60 ledger as a review budget.
_dedicated_ledger_guard = maker._dedicated_ledger_guard


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--manifest', help='Genuine source manifest; source versions are verified and frozen')
    source.add_argument('--book', help='Sealed FULL_BODY_INPUT.json; offline archive/replay only')
    p.add_argument('--guide', required=True, help='Complete baseline GUIDE.json for the same book')
    p.add_argument('--output', required=True)
    p.add_argument('--config', default=str(DEFAULT_CONFIG))
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--run', action='store_true', help='Explicit paid execution of the bounded experiment')
    mode.add_argument('--responses', help='Offline recorded-response fixture')
    p.add_argument('--allow-max', action='store_true', help='Required explicit permission for any live review experiment')
    p.add_argument('--budget-mode', choices=('dedicated',), default='dedicated')
    p.add_argument('--budget-ledger', help='Dedicated experiment lifetime ledger; existing marked guide ledgers retain all spend')
    p.add_argument('--budget-limit', type=float, help='Explicit lifetime cap at most 30 CNY; never resets spend')
    p.add_argument('--key-file')
    p.add_argument('--tokenizer')
    return p


def _implementation_hashes():
    from optomind_research.runtime.upgrade3.guide_review import implementation_hashes
    hashes = {**maker._implementation_hashes(), **implementation_hashes()}
    root = PROJECT_ROOT / 'optomind_research/runtime/upgrade3'
    paths = [Path(__file__)]
    paths += sorted(root.glob('guide_review*.py'))
    paths += sorted((PROJECT_ROOT / 'prompts/guide_review').rglob('*.md'))
    hashes.update({path.relative_to(PROJECT_ROOT).as_posix(): sha256_file(path) for path in paths})
    return hashes


def _check_context(output, context):
    path = output / 'CLI_CONTEXT.json'
    if path.is_file():
        prior = read_json(path)
        # No cap-increase exception: this experiment has fixed bounded rounds.
        for field in ('input_mode', 'input_sha256', 'book_sha256', 'source_manifest_sha256',
                      'guide_sha256', 'guide_file_sha256', 'config', 'config_file_sha256',
                      'meter', 'implementation_hashes'):
            if prior.get(field) != context.get(field):
                raise ValueError('guide_review_input_or_configuration_changed_use_new_output_directory:' + field)
        if prior.get('execution_mode') == context['execution_mode'] == 'recording':
            if prior.get('fixture_sha256') != context.get('fixture_sha256'):
                raise ValueError('guide_review_recording_changed_use_new_output_directory')
        if prior.get('execution_mode') == context['execution_mode'] == 'live':
            if prior.get('budget_binding') != context.get('budget_binding'):
                raise ValueError('guide_review_live_resume_budget_changed')
    shared._execution_context(output, context)


def main(argv=None):
    args = parser().parse_args(argv)
    output = Path(args.output).expanduser().resolve()
    guide_path = Path(args.guide).expanduser().resolve()
    # Even error reports must not modify the original maker artifact directory.
    if output == guide_path.parent or output in guide_path.parents:
        error = {'error': 'ValueError', 'message': 'guide_review_output_must_be_separate_from_baseline_directory',
                 'route': 'guide_review'}
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        if args.book and args.run:
            raise ValueError('archived_book_is_offline_only')
        if args.run and not args.allow_max:
            raise ValueError('guide_review_live_requires_allow_max')
        if args.run and (not args.budget_ledger or args.budget_limit is None
                         or not math.isfinite(args.budget_limit) or not 0 < args.budget_limit <= 30):
            raise ValueError('dedicated_guide_budget_requires_finite_cap_at_most_30_CNY')
        from optomind_research.runtime.upgrade3.guide_review import run_guide_review, validate_config
        from optomind_research.runtime.upgrade3.guide_maker_contracts import compile_guide_input
        from optomind_research.runtime.upgrade3.guided_body_contracts import validate_guide
        config = read_json(args.config)
        shared._no_secrets(config)
        config = validate_config(config)
        book, prepared = load_book(args.book) if args.book else load_body_manifest(args.manifest)
        # Exercise complete real source parsing and book/guide alignment before
        # touching a ledger, credentials, or live provider factory.
        compile_guide_input(book)
        guide = validate_guide(read_json(args.guide), book)
        counter, meter = tokenizer_counter(args.tokenizer)
        mode = 'live' if args.run else 'recording' if args.responses else 'preview'
        context = {'schema_version': 'optomind.guide_review_cli.v1', 'execution_mode': mode,
                   'input_mode': 'offline_archive' if args.book else 'live_source_manifest',
                   'input_sha256': sha256_file(args.book or args.manifest), 'book_sha256': book['book_sha256'],
                   'source_manifest_sha256': _hash(prepared), 'guide_sha256': _hash(guide),
                   'guide_file_sha256': sha256_file(args.guide), 'config': config,
                   'config_file_sha256': sha256_file(args.config), 'meter': meter,
                   'implementation_hashes': _implementation_hashes(),
                   'fixture_sha256': sha256_file(args.responses) if args.responses else None,
                   'semantic_quality_unreviewed': True, 'writing_tested': False}
        snapshot = output / 'SOURCE_MANIFEST.json'
        if snapshot.is_file() and _hash(read_json(snapshot)) != _hash(prepared):
            raise ValueError('input_source_versions_changed_use_new_output_directory')
        if args.run:
            context['budget_binding'] = {
                'mode': 'dedicated',
                'ledger_path': str(Path(args.budget_ledger).expanduser().resolve()) if args.budget_ledger else None,
                'limit_cny': args.budget_limit}
        _check_context(output, context)
        budget = _dedicated_ledger_guard(args) if args.run else None
        write_json(snapshot, prepared)
        write_json(output / 'EFFECTIVE_CONFIG.json', config)
        factory = make_live_factory(args, token_counter=counter) if args.run else RecordingFactory(args.responses) if args.responses else None
        invocation = output / 'cli_invocations' / (uuid.uuid4().hex + '.json')
        write_json(invocation, {**context, 'started_at': datetime.now(timezone.utc).isoformat(),
                               'allow_max': args.allow_max, 'budget_before': budget})
        result = run_guide_review(book, guide, output, config, client_factory=factory,
                                  run=bool(args.run or args.responses), token_counter=counter)
        result = {**result, 'execution_mode': mode, 'input_mode': context['input_mode'], 'meter': meter,
                  'cli_invocation': str(invocation), 'source_manifest_sha256': _hash(prepared),
                  'semantic_quality_unreviewed': True, 'writing_tested': False,
                  'expected_chapter_ids': prepared['expected_chapter_ids'],
                  'actual_chapter_ids': prepared['actual_chapter_ids']}
        if args.run:
            result['budget_before'] = budget
            result['budget_after'] = _dedicated_ledger_guard(args)
        if factory is not None and hasattr(factory, 'ledger_snapshot'):
            result['budget'] = factory.ledger_snapshot()
        if isinstance(factory, RecordingFactory):
            result.update(recorded_response_calls=factory.calls, current_run_cost_cny=0.0)
        write_json(output / 'CLI_RUN.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if mode == 'preview' or result.get('complete') is True else 3
    except (ValueError, OSError, RuntimeError) as exc:
        error = {'error': type(exc).__name__, 'message': str(exc), 'route': 'guide_review'}
        write_json(output / 'CLI_EXCEPTION.json', error)
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
