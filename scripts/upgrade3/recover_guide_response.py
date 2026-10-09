"""Recover a saved guide response offline into a NEW, derived artifact directory.

No provider, credentials, budget ledger, cache replay or paid retry is involved.
Syntax recovery does not establish scientific quality or satisfy pending reads.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.upgrade3.evidence_body_writer import load_book, load_body_manifest, read_json, sha256_file, write_json
from optomind_research.runtime.upgrade3.guide_maker import persist_format_recovery, render_guide, _source_file_hashes
from optomind_research.runtime.upgrade3.guide_maker_contracts import (
    compile_guide_input, decode_maker_response, parse_maker_response, resolve_material_requests)
from optomind_research.runtime.upgrade3.guided_body_contracts import validate_guide


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--book', help='Sealed FULL_BODY_INPUT.json')
    source.add_argument('--manifest', help='Original source manifest, verified by the existing loader')
    p.add_argument('--response', required=True, help='Original saved RAW_RESPONSE.json envelope')
    p.add_argument('--error-file', help='Additional saved provider ERROR.json; sibling ERROR.json is always checked')
    p.add_argument('--output', required=True, help='New directory; an existing directory is never overwritten')
    return p


def recover(args):
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise ValueError('recovery_output_must_be_new_directory')
    source = Path(args.book or args.manifest).expanduser().resolve()
    raw_path = Path(args.response).expanduser().resolve()
    # Refuse nesting inside the saved attempt or any enclosing run/cache root.
    for parent in [raw_path.parent, *raw_path.parents]:
        if (parent == raw_path.parent and parent.name.startswith("attempt_")) or any((parent / name).exists() for name in ('RUN_MANIFEST.json', 'CLI_CONTEXT.json')):
            if output == parent or parent in output.parents:
                raise ValueError('recovery_output_must_not_modify_original_run')
    book, prepared = load_book(source) if args.book else load_body_manifest(source)
    bundle = compile_guide_input(book)
    raw = read_json(raw_path)
    errors = []
    error_paths = {raw_path.with_name('ERROR.json')}
    if args.error_file:
        explicit = Path(args.error_file).expanduser().resolve()
        if not explicit.is_file():
            raise ValueError('recovery_error_file_missing')
        error_paths.add(explicit)
    error_evidence = []
    for path in sorted(error_paths):
        if path.exists():
            # Presence itself is blocking, even for an empty or damaged file.
            error_evidence.append({'path': str(path), 'sha256': sha256_file(path)})
            errors.append('saved_provider_error_present')
    parsed, audit, guide = {}, None, None
    try:
        candidate, transport, audit = decode_maker_response(raw)
        parsed = parse_maker_response(raw, book, bundle)
        guide = parsed['guide']
        errors.extend(parsed.get('errors', []))
        # Resolve, but never execute, valid outstanding requests.
        pending = resolve_material_requests(bundle, parsed['reading_needs'])
        if pending:
            errors.append('pending_reading_needs_require_model_work')
    except (ValueError, TypeError, KeyError) as exc:
        errors.append(str(exc))
        pending = []
        # A valid chapter draft can be retained without manufacturing missing
        # fields or claiming that the failed protocol became valid.
        try:
            guide = validate_guide(candidate.get('guide'), book)
        except (ValueError, TypeError, KeyError, AttributeError, UnboundLocalError):
            guide = None
    complete = bool(parsed.get('complete') is True and parsed.get('transport_complete') is True
                    and not parsed.get('reading_needs') and not errors)
    output.mkdir(parents=True, exist_ok=False)
    if audit:
        persist_format_recovery(output, {'format_recovery': audit}, raw_path)
    result = {'schema_version': 'optomind.guide_response_recovery.v1',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'execution_mode': 'offline_format_recovery', 'complete': complete,
        'status': 'complete' if complete else 'draft' if guide else 'invalid',
        'format_recovered': audit is not None, 'semantic_quality_unreviewed': True,
        'scientific_review_performed': False, 'writing_tested': False,
        'model_calls': 0, 'client_invocations': 0, 'paid_dispatch_count': 0, 'current_run_cost_cny': 0.0,
        'raw_response_path': str(raw_path), 'raw_response_file_sha256': sha256_file(raw_path),
        'source_input_path': str(source), 'source_input_sha256': sha256_file(source),
        'book_sha256': book['book_sha256'], 'provider_error_evidence': error_evidence,
        'source_file_hashes': {**_source_file_hashes(),
            str(Path(__file__).resolve().relative_to(PROJECT_ROOT)): sha256_file(__file__)},
        'expected_chapter_ids': prepared['expected_chapter_ids'],
        'actual_chapter_ids': [row['chapter_id'] for row in book['chapters']],
        'outstanding_reading_needs': parsed.get('reading_needs', []),
        'queued_source_handles': [unit['source_handle'] for unit in pending],
        'errors': errors, 'guide': guide, 'original_run_modified': False}
    write_json(output / 'RECOVERY_RESULT.json', result)
    if guide is not None:
        name = 'GUIDE' if complete else 'DRAFT_GUIDE'
        write_json(output / (name + '.json'), guide)
        (output / (name + '.md')).write_text(render_guide(guide), encoding='utf-8')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = recover(args)
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['complete'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
