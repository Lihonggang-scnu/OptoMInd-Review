"""Four evidence-first full BODY routes. Preview/preparation/replay never read credentials.

Paid execution requires --run, a separately named round-two ledger, and a finite
absolute lifetime ceiling (at most CNY 60). Archived --book inputs are offline only.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import uuid
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scripts.upgrade3 import fullbody_writer as old
from scripts.upgrade3 import writer_candidates as shared
from optomind_research.runtime.upgrade3.fullbody_contracts import seal_fullbody_input, fullbody_task_catalog
from optomind_research.runtime.upgrade3.writer_candidates import _profile

DEFAULT_CONFIG = PROJECT_ROOT / 'config/evidence_body_writer/plus_first.json'
ROUTES = ('packed_whole', 'packed_continuous', 'dossier_author', 'scoped_revision')
read_json, write_json, sha256_file = old.read_json, old.write_json, old.sha256_file
load_body_manifest, make_live_factory = old.load_body_manifest, shared.make_live_factory
RecordingFactory, tokenizer_counter = old.RecordingFactory, shared.tokenizer_counter
_hash = old._hash


def load_book(path):
    source = Path(path).expanduser().resolve()
    book = read_json(source)
    if not isinstance(book, dict) or not book.get('book_sha256') or seal_fullbody_input(book) != book:
        raise ValueError('archived_book_seal_mismatch')
    catalog = fullbody_task_catalog(book)
    if not catalog:
        raise ValueError('archived_book_empty_task_scope')
    prepared = book.get('input_manifest', {}).get('fullbody_manifest')
    if not isinstance(prepared, dict) or _hash(prepared) != book['input_manifest'].get('fullbody_manifest_sha256'):
        raise ValueError('archived_book_requires_generated_source_manifest')
    actual = [row['chapter_id'] for row in book['chapters']]
    if actual != prepared.get('expected_chapter_ids') or actual != prepared.get('actual_chapter_ids'):
        raise ValueError('archived_book_chapter_scope_mismatch')
    return book, prepared


def load_draft(path, book, mapping_path=None, prior_book_path=None):
    source = Path(path).expanduser().resolve()
    draft = read_json(source)
    body = draft.get('body_markdown') if isinstance(draft, dict) else None
    if not body or draft.get('body_sha256') != hashlib.sha256(body.encode()).hexdigest():
        raise ValueError('draft_body_hash_required_or_mismatch')
    if not (draft.get('complete') or draft.get('body_complete')) or draft.get('pending_task_ids'):
        raise ValueError('draft_requires_complete_body')
    if set(draft.get('completed_task_ids', [])) != set(fullbody_task_catalog(book)):
        raise ValueError('draft_task_scope_mismatch')
    report = None
    if draft.get('input_hash') != _hash(book):
        prior_path = Path(prior_book_path or draft.get('input_path') or source.parent / 'FULL_BODY_INPUT.json').expanduser()
        if not prior_path.is_absolute():
            prior_path = source.parent / prior_path
        prior, _ = load_book(prior_path)
        if draft.get('input_hash') != _hash(prior):
            raise ValueError('draft_prior_book_hash_mismatch')
        def stable_identities(value):
            return {key: {k: v for k, v in row.items() if k != 'record_ids'}
                    for key, row in value.get('source_identities', {}).items()}
        if stable_identities(prior) != stable_identities(book):
            raise ValueError('draft_book_identity_or_scope_mismatch:source_identities')
        for field in ('source_aliases', 'task_catalog'):
            if prior.get(field) != book.get(field):
                raise ValueError('draft_book_identity_or_scope_mismatch:' + field)
        if [c['chapter_id'] for c in prior['chapters']] != [c['chapter_id'] for c in book['chapters']]:
            raise ValueError('draft_chapter_scope_mismatch')
        if not mapping_path:
            raise ValueError('historical_source_fingerprint_difference_requires_explicit_source_mapping')
        mapping = read_json(mapping_path)
        if (mapping.get('old_input_hash') != _hash(prior) or mapping.get('new_input_hash') != _hash(book)
                or not isinstance(mapping.get('reason'), str) or not mapping['reason'].strip()):
            raise ValueError('source_mapping_requires_exact_old_new_input_hash_and_reason')
        report = {'mapping': mapping, 'mapping_sha256': sha256_file(mapping_path),
                  'old_manifest': prior['input_manifest'], 'new_manifest': book['input_manifest'],
                  'old_sources_sha256': _hash(prior.get('sources')), 'new_sources_sha256': _hash(book.get('sources')),
                  'identity_and_task_scope_verified': True}
        draft = copy.deepcopy(draft)
        draft['historical_input_hash'] = draft['input_hash']
        draft['input_hash'] = _hash(book)
    draft['_loaded_from'] = {'path': str(source), 'sha256': sha256_file(source), 'body_sha256': draft['body_sha256']}
    return draft, report


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    inputs = p.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--manifest')
    inputs.add_argument('--book', help='Generated sealed FULL_BODY_INPUT.json; offline archive/replay only')
    p.add_argument('--plan')
    p.add_argument('--language')
    p.add_argument('--prepare-manifest')
    p.add_argument('--route', choices=ROUTES, default='packed_whole')
    p.add_argument('--output')
    p.add_argument('--config', default=str(DEFAULT_CONFIG))
    p.add_argument('--draft')
    p.add_argument('--draft-book', help='Prior sealed book when the draft input_path is no longer available')
    p.add_argument('--source-mapping', help='Explicit historical old_input_hash/new_input_hash/reason JSON')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--run', action='store_true')
    mode.add_argument('--responses')
    p.add_argument('--allow-max', action='store_true')
    p.add_argument('--retry-failed', action='store_true', help='Explicit retry only; uncertain spend holds remain reserved')
    p.add_argument('--budget-ledger')
    p.add_argument('--budget-limit', type=float)
    p.add_argument('--key-file')
    p.add_argument('--tokenizer')
    return p


def _ledger_guard(args):
    if not args.budget_ledger or args.budget_limit is None or not math.isfinite(args.budget_limit) or not 0 < args.budget_limit <= 60:
        raise ValueError('round_two_requires_separate_ledger_and_finite_absolute_budget_at_most_60_CNY')
    ledger = Path(args.budget_ledger).expanduser().resolve()
    marker = ledger.with_name(ledger.name + '.evidence_round2.json')
    expected = {'schema_version': 'optomind.evidence_round2_budget.v1', 'ledger_path': str(ledger), 'limit_cny': args.budget_limit}
    if ledger.exists() and not marker.is_file():
        raise ValueError('existing_unmarked_ledger_forbidden:use_new_separate_round_two_ledger')
    if marker.is_file() and read_json(marker) != expected:
        raise ValueError('round_two_ledger_cap_or_identity_changed')
    write_json(marker, expected)


def main(argv=None):
    args = parser().parse_args(argv)
    output = Path(args.output).expanduser().resolve() if args.output else None
    try:
        if args.book and (args.run or args.plan or args.language):
            raise ValueError('archived_book_is_offline_only_no_plan_or_language_override')
        if args.prepare_manifest and (args.run or args.responses or args.draft or args.book):
            raise ValueError('prepare_manifest_requires_offline_manifest_input')
        book, prepared = load_book(args.book) if args.book else load_body_manifest(args.manifest, plan_path=args.plan, language=args.language)
        if args.prepare_manifest:
            destination = Path(args.prepare_manifest).expanduser().resolve()
            if destination == Path(args.manifest).expanduser().resolve():
                raise ValueError('prepare_manifest_requires_new_destination')
            write_json(destination, prepared)
            print(json.dumps({'prepared_manifest': str(destination), 'sha256': sha256_file(destination), 'execution_mode': 'preview'}))
            return 0
        if output is None:
            raise ValueError('output_required')
        if (args.route == 'scoped_revision') != bool(args.draft):
            raise ValueError('scoped_revision_requires_draft_and_only_scoped_revision_accepts_draft')
        if (args.source_mapping or args.draft_book) and not args.draft:
            raise ValueError('source_mapping_requires_draft')
        config = read_json(args.config)
        shared._no_secrets(config)
        for role in ('writer', 'reader', 'reviser', 'curator'):
            config[role] = _profile(config.get(role, {}), role)
        roles = ('reader', 'reviser') if args.route == 'scoped_revision' else ('curator', 'writer') if args.route == 'dossier_author' else ('writer',)
        shared._require_max_permission(args, {r: config[r] for r in roles})
        base, mapping = load_draft(args.draft, book, args.source_mapping, args.draft_book) if args.draft else (None, None)
        if args.run and base is not None and base.get('execution_mode') != 'live':
            raise ValueError('paid_revision_requires_live_draft')
        counter, meter = tokenizer_counter(args.tokenizer)
        mode = 'live' if args.run else 'recording' if args.responses else 'preview'
        context = {'schema_version': 'optomind.evidence_body_cli.v1', 'execution_mode': mode,
                   'input_mode': 'offline_archive' if args.book else 'live_source_manifest',
                   'input_sha256': sha256_file(args.book or args.manifest), 'book_sha256': book['book_sha256'],
                   'source_manifest_sha256': _hash(prepared), 'config': config, 'meter': meter,
                   'cli_source_sha256': sha256_file(__file__), 'semantic_quality_unreviewed': True}
        snapshot = output / 'SOURCE_MANIFEST.json'
        if snapshot.is_file() and _hash(read_json(snapshot)) != _hash(prepared):
            raise ValueError('input_source_versions_changed_use_new_output_directory')
        shared._execution_context(output, context)
        write_json(snapshot, prepared)
        write_json(output / 'EFFECTIVE_CONFIG.json', config)
        if mapping:
            write_json(output / 'SOURCE_MAPPING_REPORT.json', mapping)
        if args.run:
            _ledger_guard(args)
        factory = make_live_factory(args, token_counter=counter) if args.run else RecordingFactory(args.responses) if args.responses else None
        invocation = output / 'cli_invocations' / (uuid.uuid4().hex + '.json')
        write_json(invocation, {**context, 'route': args.route, 'started_at': datetime.now(timezone.utc).isoformat(),
                                'allow_max': args.allow_max, 'retry_failed': args.retry_failed,
                                'fixture_sha256': getattr(factory, 'fixture_sha256', None),
                                'draft': base.get('_loaded_from') if base else None})
        if args.route == 'scoped_revision':
            from optomind_research.runtime.upgrade3.scoped_body_revision import run_scoped_revision
            runtime_config = {k: v for k, v in config.items() if k not in ('curator', 'completion_on_missing')}
            result = run_scoped_revision(book, base, output, runtime_config, client_factory=factory,
                                         run=bool(args.run or args.responses), retry_failed=args.retry_failed, counter=counter)
        else:
            from optomind_research.runtime.upgrade3.evidence_body_writer import run_evidence_body
            result = run_evidence_body(book, route=args.route, output_dir=output, config=config, client_factory=factory,
                                       run=bool(args.run or args.responses), retry_failed=args.retry_failed, token_counter=counter)
        result = {**result, 'execution_mode': mode, 'input_mode': context['input_mode'], 'meter': meter,
                  'cli_invocation': str(invocation), 'source_manifest_sha256': _hash(prepared), 'semantic_quality_unreviewed': True,
                  'expected_chapter_ids': prepared['expected_chapter_ids'], 'actual_chapter_ids': prepared['actual_chapter_ids']}
        if factory is not None and hasattr(factory, 'ledger_snapshot'):
            result['budget'] = factory.ledger_snapshot()
        if isinstance(factory, RecordingFactory):
            result.update(recorded_response_calls=factory.calls, current_run_cost_cny=0.0)
        write_json(output / 'CLI_RUN.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if mode == 'preview' or result.get('complete') is True else 3
    except (ValueError, OSError, RuntimeError) as exc:
        error = {'error': type(exc).__name__, 'message': str(exc), 'route': args.route}
        if output is not None:
            write_json(output / 'CLI_EXCEPTION.json', error)
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
